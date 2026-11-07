"""Digital signing of Form 16 with the employer's DSC (a .pfx / .p12 file).

The certificate file is kept (it is useless without its password); the password is typed in
each time Form 16 is signed and is never stored or logged. Each PDF gets a PAdES signature
(pyHanko) with a visible stamp on the last page. Signed copies are stored per employee and
year and are what HR and the employee download from then on.

Stub mode: with no certificate uploaded, signing still runs, with a throwaway self-signed
certificate made on the spot and a stamp that says it is a test signature. It lets a company
try the flow (and our tests run it) but is not valid for filing.
"""
from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from core import audit
from core.errors import BadRequest
from modules.payroll import service as svc
from modules.payroll.models import PayrollFile, PayRun, Payslip

MAX_PFX_BYTES = 64 * 1024


def _now():
    return datetime.now(timezone.utc)


def _load_pfx(data: bytes, password: str):
    from cryptography.hazmat.primitives.serialization import pkcs12
    try:
        key, cert, extra = pkcs12.load_key_and_certificates(data, (password or "").encode() or None)
    except ValueError:
        raise BadRequest("The password does not open this certificate.")
    if key is None or cert is None:
        raise BadRequest("The file has no signing key and certificate.")
    return key, cert, extra or []


def _cn(name) -> str:
    from cryptography.x509.oid import NameOID
    attrs = name.get_attributes_for_oid(NameOID.COMMON_NAME)
    return attrs[0].value if attrs else name.rfc4514_string()


def certificate(db: Session, company_id: int) -> dict:
    st = svc.get_settings(db, company_id).get("signing") or {}
    has = bool(st.get("certificate_file_uuid")) and _cert_file(db, company_id, st) is not None
    return {"has_certificate": has, "mode": "certificate" if has else "stub", "subject": st.get("subject") if has else None,
            "issuer": st.get("issuer") if has else None, "valid_to": st.get("valid_to") if has else None,
            "signer_name": st.get("signer_name") or "", "signer_designation": st.get("signer_designation") or "",
            "place": st.get("place") or "", "signed": st.get("signed") or {}}


def _cert_file(db: Session, company_id: int, st: dict) -> PayrollFile | None:
    if not st.get("certificate_file_uuid"):
        return None
    return db.query(PayrollFile).filter(PayrollFile.company_id == company_id, PayrollFile.kind == "dsc_pfx",
                                        PayrollFile.uuid == st["certificate_file_uuid"]).first()


def upload_certificate(db: Session, company_id: int, filename: str, data: bytes, password: str, *,
                       signer_name: str | None = None, signer_designation: str | None = None, place: str | None = None) -> dict:
    if not data or len(data) > MAX_PFX_BYTES:
        raise BadRequest("Upload the .pfx or .p12 file of the digital signature certificate.")
    _key, cert, _extra = _load_pfx(data, password)   # checked now; the password is not kept
    valid_to = cert.not_valid_after_utc
    if valid_to < _now():
        raise BadRequest(f"This certificate expired on {valid_to:%d %b %Y}.")
    for old in db.query(PayrollFile).filter(PayrollFile.company_id == company_id, PayrollFile.kind == "dsc_pfx").all():
        db.delete(old)
    f = PayrollFile(company_id=company_id, kind="dsc_pfx", filename=(filename or "certificate.pfx")[:255],
                    content_type="application/x-pkcs12", size=len(data), content=data, uploaded_by_name=svc.actor())
    db.add(f)
    db.flush()
    subject = _cn(cert.subject)
    svc.update_settings(db, company_id, {"signing": {
        "certificate_file_uuid": str(f.uuid), "subject": subject, "issuer": _cn(cert.issuer),
        "valid_to": valid_to.date().isoformat(), "signer_name": signer_name or subject,
        **({"signer_designation": signer_designation} if signer_designation is not None else {}),
        **({"place": place} if place is not None else {})}})
    audit.event(db, "payroll.dsc_uploaded", "payroll_files", f.id, {"subject": subject, "valid_to": valid_to.date().isoformat()},
                entity_uuid=str(f.uuid))
    return certificate(db, company_id)


def remove_certificate(db: Session, company_id: int) -> dict:
    for old in db.query(PayrollFile).filter(PayrollFile.company_id == company_id, PayrollFile.kind == "dsc_pfx").all():
        db.delete(old)
    svc.update_settings(db, company_id, {"signing": {"certificate_file_uuid": None, "subject": None, "issuer": None,
                                                      "valid_to": None}})
    audit.event(db, "payroll.dsc_removed", "payroll_files", None, {})
    return certificate(db, company_id)


def stub_pfx(name: str) -> tuple[bytes, str]:
    """A throwaway self-signed certificate (one day) for stub mode."""
    import secrets
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{name} (test signature)"),
                         x509.NameAttribute(NameOID.COUNTRY_NAME, "IN")])
    now = _now()
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    password = secrets.token_hex(8)
    data = pkcs12.serialize_key_and_certificates(b"stub", key, cert, None,
                                                 serialization.BestAvailableEncryption(password.encode()))
    return data, password


def sign_pdf(pdf_bytes: bytes, pfx: bytes, password: str, *, reason: str, location: str, signer_label: str,
             test: bool = False) -> bytes:
    from pyhanko import stamp
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign import fields, signers
    signer = signers.SimpleSigner.load_pkcs12_data(pfx, other_certs=[], passphrase=(password or "").encode() or None)
    if signer is None:
        raise BadRequest("The password does not open the certificate.")
    w = IncrementalPdfFileWriter(io.BytesIO(pdf_bytes), strict=False)
    pages = int(PdfFileReader(io.BytesIO(pdf_bytes), strict=False).root["/Pages"]["/Count"])
    text = ("TEST SIGNATURE, not valid for filing\n" if test else "Digitally signed by\n") + "%(signer)s\nDate: %(ts)s"
    style = stamp.TextStampStyle(stamp_text=text, border_width=0, background=None)
    meta = signers.PdfSignatureMetadata(field_name="EmployerSignature", reason=reason, location=location or None,
                                        name=signer_label)
    spec = fields.SigFieldSpec("EmployerSignature", on_page=pages - 1, box=(360, 40, 560, 110))
    out = signers.PdfSigner(meta, signer=signer, stamp_style=style, new_field_spec=spec).sign_pdf(w)
    return out.getvalue()


def signed_file(db: Session, company_id: int, employee_id: int, fy: str) -> PayrollFile | None:
    return (db.query(PayrollFile).filter(PayrollFile.company_id == company_id, PayrollFile.employee_id == employee_id,
                                         PayrollFile.kind == "form16_signed", PayrollFile.fy == fy)
            .order_by(PayrollFile.id.desc()).first())


def employees_for(db: Session, company_id: int, fy: str) -> list[int]:
    return [r[0] for r in db.query(Payslip.employee_id).join(PayRun, PayRun.id == Payslip.pay_run_id)
            .filter(Payslip.company_id == company_id, Payslip.fy == fy, PayRun.status.in_(("locked", "paid"))).distinct().all()]


def sign_form16(db: Session, company_id: int, fy: str, *, password: str | None, employee_ids: list[int] | None = None,
                place: str | None = None) -> dict:
    """Sign Form 16 of everyone with locked pay in the year (or the chosen people)."""
    from modules.payroll import taxsvc
    st = svc.get_settings(db, company_id).get("signing") or {}
    f = _cert_file(db, company_id, st)
    company = svc._company(db, company_id)
    if f is not None:
        if not password:
            raise BadRequest("Enter the certificate's password to sign.")
        pfx, pwd, test = f.content, password, False
        _load_pfx(pfx, pwd)          # a wrong password stops here, before anything is signed
        label = st.get("signer_name") or st.get("subject") or company.company_name
    else:
        label = st.get("signer_name") or company.company_name
        pfx, pwd = stub_pfx(label)
        test = True
    ids = employee_ids or employees_for(db, company_id, fy)
    if not ids:
        raise BadRequest("No locked payslips in this year.")
    place = place if place is not None else (st.get("place") or "")
    reason = f"Form 16 for FY {fy}" + (f", {st.get('signer_designation')}" if st.get("signer_designation") else "")
    done, skipped = 0, []
    for eid in ids:
        emp = svc.get_employee(db, company_id, eid)
        try:
            body = taxsvc.form16_pdf(db, company_id, emp, fy, prefer_signed=False)
        except BadRequest as exc:
            skipped.append({"employee": svc.name_of(emp), "reason": exc.detail if hasattr(exc, "detail") else str(exc)})
            continue
        signed = sign_pdf(body, pfx, pwd, reason=reason, location=place, signer_label=label, test=test)
        for old in db.query(PayrollFile).filter(PayrollFile.company_id == company_id, PayrollFile.employee_id == eid,
                                                PayrollFile.kind == "form16_signed", PayrollFile.fy == fy).all():
            db.delete(old)
        db.add(PayrollFile(company_id=company_id, employee_id=eid, kind="form16_signed", fy=fy, ref="stub" if test else "dsc",
                           filename=f"Form16-{fy}-{emp.employee_code or eid}.pdf", content_type="application/pdf",
                           size=len(signed), content=signed, uploaded_by_name=svc.actor()))
        done += 1
    db.flush()
    signed_log = dict(st.get("signed") or {})
    signed_log[fy] = {"at": _now().isoformat(), "by": svc.actor(), "count": done, "mode": "stub" if test else "certificate"}
    svc.update_settings(db, company_id, {"signing": {"signed": signed_log}})
    audit.event(db, "payroll.form16_signed", "payroll_files", None,
                {"fy": fy, "signed": done, "skipped": len(skipped), "mode": "stub" if test else "certificate"})
    return {"signed": done, "skipped": skipped, "mode": "stub" if test else "certificate", "fy": fy}


def signature_info(pdf_bytes: bytes) -> list[dict]:
    """Signatures in a PDF (for checks and tests): signer, intact or not."""
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.validation import validate_pdf_signature
    r = PdfFileReader(io.BytesIO(pdf_bytes), strict=False)
    out = []
    for sig in r.embedded_signatures:
        st = validate_pdf_signature(sig)
        out.append({"field": sig.field_name, "signer": _cn_asn1(sig.signer_cert), "intact": st.intact,
                    "valid": st.valid, "coverage": str(st.coverage)})
    return out


def _cn_asn1(cert) -> str:
    try:
        return cert.subject.native.get("common_name") or ""
    except Exception:  # pragma: no cover
        return ""
