"""Salary bank files: formats, the company's debit accounts and making the files for a pay run.

How a pay run becomes bank files
1. Payees: every calculated payslip with take-home pay above zero that is not paid yet and whose
   employee is paid by bank (held salaries only once released, or only those with
   `released_only`). The bank account is the one frozen on the payslip when it was calculated.
2. Each payee's legal entity (from the payslip) picks the debit account: the entity's default
   account, else the company-wide default, else the old single account in payroll settings.
3. The debit account's format (or the one chosen for this download) decides the layout. Each
   payee is checked (IFSC, account number, name, amount); errors leave the person out (only
   when the user agrees), warnings do not.
4. Transaction type per payee: same bank as the debit account = internal transfer, otherwise
   NEFT, or RTGS from ₹2 lakh, or IMPS up to ₹5 lakh, following the payroll bank settings and
   what the bank's file accepts. Banks that want own-bank and other-bank payments (or each type)
   in separate files get separate files.
5. Every file is stored with its fingerprint (SHA-256), totals, who made it and when; downloads
   are written to the audit log. Making files again marks earlier ones of that run superseded.
"""
from __future__ import annotations

import hashlib
import re
import uuid as uuid_mod
from collections import OrderedDict
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from core import audit, context
from core.errors import BadRequest, NotFound
from modules.salary_bank import engine, formats
from modules.salary_bank.engine import Debit, Line, Payee, Problem, SpecError
from modules.salary_bank.models import BankDebitAccount, BankFileBatch, BankFileFormat

SAMPLE_PAYEES = [
    Payee("Priya Sharma", "50100234567891", "HDFC0001234", Decimal("48250.00"), "EMP001", "priya@example.com", "9876543210", "HDFC Bank"),
    Payee("Arjun Nair", "012345678901", "ICIC0000456", Decimal("61800.50"), "EMP002", "arjun@example.com", "9812345678", "ICICI Bank"),
    Payee("Meena Iyer", "30214567891", "SBIN0007890", Decimal("235000.00"), "EMP003", "meena@example.com", "9900112233", "State Bank of India"),
]


def actor() -> str:
    return context.current().actor_name or "System"


def mask(acct: str | None) -> str:
    a = acct or ""
    return ("•" * max(0, min(len(a) - 4, 8)) + a[-4:]) if a else ""


# ── Formats ──────────────────────────────────────────────────────────

def _custom_key(row: BankFileFormat) -> str:
    return f"custom:{row.uuid}"


def catalog(db: Session, company_id: int, include_inactive: bool = False) -> list[dict]:
    out = [{"key": k, "name": f["name"], "bank": f.get("bank"), "builtin": True, "spec": f["spec"],
            "notes": f.get("notes", ""), "sources": f.get("sources", []), "confidence": f.get("confidence"), "active": True}
           for k, f in formats.BUILTIN.items()]
    q = db.query(BankFileFormat).filter(BankFileFormat.company_id == company_id)
    if not include_inactive:
        q = q.filter(BankFileFormat.active.is_(True))
    for r in q.order_by(BankFileFormat.name).all():
        out.append({"key": _custom_key(r), "uuid": str(r.uuid), "name": r.name, "bank": (r.spec or {}).get("bank") or "Custom",
                    "builtin": False, "based_on": r.based_on, "spec": r.spec, "notes": (r.spec or {}).get("notes", ""),
                    "sources": [], "active": r.active, "updated_at": r.updated_at})
    return out


def spec_for(db: Session, company_id: int, key: str) -> tuple[str, dict]:
    """(name, spec) for a built-in key or "custom:<uuid>"."""
    if key in formats.BUILTIN:
        f = formats.BUILTIN[key]
        return f["name"], f["spec"]
    if key.startswith("custom:"):
        try:
            u = uuid_mod.UUID(key.split(":", 1)[1])
        except ValueError:
            raise BadRequest("Unknown bank file format.")
        r = db.query(BankFileFormat).filter(BankFileFormat.company_id == company_id, BankFileFormat.uuid == u).first()
        if r:
            return r.name, r.spec
    if key in formats.LEGACY_KEYS:
        return spec_for(db, company_id, formats.LEGACY_KEYS[key])
    raise BadRequest("Unknown bank file format.")


SPEC_KEYS = ("kind", "delimiter", "quote", "extension", "line_ending", "header_row", "header_lines", "trailer_lines", "columns",
             "date_format", "amount", "codes", "modes", "split", "clean", "file_name", "bank", "notes", "ifsc_prefixes",
             "requires", "variants")
VARIANT_KEYS = ("delimiter", "quote", "extension", "header_row", "header_lines", "trailer_lines", "columns", "date_format")


def clean_spec(spec: dict) -> dict:
    spec = {k: v for k, v in (spec or {}).items() if k in SPEC_KEYS}
    spec.setdefault("kind", "delimited")
    spec.setdefault("extension", "csv" if spec["kind"] == "delimited" and engine.delimiter(spec) == "," else "txt")
    if spec.get("extension") not in ("csv", "txt"):
        raise BadRequest("Files end in .csv or .txt.")
    if spec.get("quote") not in (None, "minimal", "all", "none"):
        raise BadRequest("Unknown quoting choice.")
    if spec.get("line_ending") not in (None, "crlf", "lf"):
        raise BadRequest("Lines end with CRLF (Windows) or LF.")
    modes = [m for m in (spec.get("modes") or ["internal", "neft", "rtgs"]) if m in ("internal", "neft", "rtgs", "imps")]
    if not modes:
        raise BadRequest("Pick at least one transaction type.")
    spec["modes"] = modes
    spec["codes"] = {k: str(v)[:20] for k, v in (spec.get("codes") or {}).items() if k in ("internal", "neft", "rtgs", "imps")}
    spec["ifsc_prefixes"] = [str(x).upper()[:4] for x in (spec.get("ifsc_prefixes") or []) if re.match(r"^[A-Za-z]{4}$", str(x))]
    spec["requires"] = [x for x in (spec.get("requires") or []) if x in ("client_code", "corporate_id")]

    def col(c: dict) -> dict:
        out = {"label": str(c.get("label") or "")[:60], "value": str(c.get("value") or "")[:200]}
        for k in ("width", "max"):
            if c.get(k) not in (None, "", 0):
                n = int(c[k])
                if n < 1 or n > 500:
                    raise BadRequest("Widths and lengths are between 1 and 500.")
                out[k] = n
        if c.get("align") in ("left", "right"):
            out["align"] = c["align"]
        if c.get("pad") in (" ", "0"):
            out["pad"] = c["pad"]
        if c.get("case") == "upper":
            out["case"] = "upper"
        return out

    def lines(rows) -> list:
        return [[col(c) for c in row] for row in (rows or []) if row][:5]

    spec["columns"] = [col(c) for c in spec.get("columns") or []]
    spec["header_lines"] = lines(spec.get("header_lines"))
    spec["trailer_lines"] = lines(spec.get("trailer_lines"))
    variants = {}
    for g, v in (spec.get("variants") or {}).items():
        if g not in ("internal", "other", "neft", "rtgs", "imps") or not isinstance(v, dict):
            continue
        v = {k: x for k, x in v.items() if k in VARIANT_KEYS}
        if "columns" in v:
            v["columns"] = [col(c) for c in v["columns"] or []]
        for k in ("header_lines", "trailer_lines"):
            if k in v:
                v[k] = lines(v[k])
        variants[g] = v
    spec["variants"] = variants
    try:
        engine.validate_spec(spec)
    except SpecError as exc:
        raise BadRequest(str(exc))
    return spec


def preview(spec: dict, debit: dict | None = None, rules: dict | None = None) -> dict:
    """Render a layout with three sample payees (one high value) for the builder's preview."""
    spec = clean_spec(spec)
    dd = debit or {}
    own = (spec.get("ifsc_prefixes") or ["HDFC"])[0]
    d = Debit(dd.get("account_number") or "00011122233344", (dd.get("ifsc") or f"{own}0000001").upper(),
              dd.get("account_name") or "Sample Industries Pvt Ltd", dd.get("client_code") or "CLIENT01",
              dd.get("corporate_id") or "CORP01")
    lines = [Line(p, engine.mode_for(p, d.ifsc, spec, rules or {})) for p in SAMPLE_PAYEES]
    groups: OrderedDict[str, list[Line]] = OrderedDict()
    for ln in lines:
        groups.setdefault(engine.group_of(ln.mode, spec), []).append(ln)
    today = date.today()
    files = []
    for i, (g, ls) in enumerate(groups.items(), start=1):
        ctx = _ctx(today, "Salary Nov 2026", "202611", "Nov 2026", i, today)
        gspec = engine.for_group(spec, g)
        try:
            r = engine.render(gspec, ls, d, ctx)
            name = engine.file_name(gspec, "preview", ctx, d, g)
        except SpecError as exc:
            raise BadRequest(str(exc))
        files.append({"group": g, "file_name": name, "lines": r.lines, "count": r.count, "total": str(r.total)})
    return {"files": files}


def save_format(db: Session, company_id: int, data: dict, row: BankFileFormat | None = None) -> BankFileFormat:
    name = (data.get("name") or "").strip()
    if not name:
        raise BadRequest("Give the format a name.")
    spec = clean_spec(data.get("spec") or {})
    if row is None:
        row = BankFileFormat(company_id=company_id, created_by_name=actor(), based_on=data.get("based_on"))
        db.add(row)
    row.name = name[:120]
    row.spec = spec
    if "active" in data and data["active"] is not None:
        row.active = bool(data["active"])
    db.flush()
    return row


def format_row(db: Session, company_id: int, uuid) -> BankFileFormat:
    r = db.query(BankFileFormat).filter(BankFileFormat.company_id == company_id, BankFileFormat.uuid == uuid).first()
    if not r:
        raise NotFound("Bank file format")
    return r


# ── Debit accounts ───────────────────────────────────────────────────

def account_out(r: BankDebitAccount, entities: dict, names: dict, full: bool = False) -> dict:
    return {"uuid": str(r.uuid), "label": r.label, "bank_name": r.bank_name, "account_number": r.account_number if full else mask(r.account_number),
            "account_masked": mask(r.account_number), "ifsc": r.ifsc, "account_name": r.account_name, "format_key": r.format_key,
            "format_name": names.get(r.format_key, r.format_key), "client_code": r.client_code, "corporate_id": r.corporate_id,
            "is_default": r.is_default, "active": r.active, "notes": r.notes, "legal_entity_id": r.legal_entity_id,
            "legal_entity_name": entities.get(r.legal_entity_id) if r.legal_entity_id else None, "updated_at": r.updated_at}


def entity_names(db: Session, company_id: int) -> dict:
    try:
        from modules.organization.models import LegalEntity
    except ImportError:
        return {}
    return dict(db.query(LegalEntity.id, LegalEntity.name).filter(LegalEntity.company_id == company_id).all())


def entity_id_for(db: Session, company_id: int, uuid) -> int | None:
    if not uuid:
        return None
    from modules.organization.models import LegalEntity
    e = db.query(LegalEntity).filter(LegalEntity.company_id == company_id, LegalEntity.uuid == uuid).first()
    if not e:
        raise NotFound("Legal entity")
    return e.id


def save_account(db: Session, company_id: int, data: dict, row: BankDebitAccount | None = None) -> BankDebitAccount:
    acct = re.sub(r"[\s-]", "", data.get("account_number") or (row.account_number if row else "") or "")
    ifsc = (data.get("ifsc") or (row.ifsc if row else "") or "").strip().upper()
    if not engine.ACCOUNT_RE.match(acct):
        raise BadRequest("Enter the account number (6 to 20 letters or digits).")
    if not engine.IFSC_RE.match(ifsc):
        raise BadRequest("Enter a valid IFSC (4 letters, a zero, then 6 letters or digits).")
    key = data.get("format_key") or (row.format_key if row else None)
    if not key:
        key = formats.suggest_for_ifsc(ifsc)
    spec_for(db, company_id, key)  # must exist
    label = (data.get("label") or "").strip() or f"{formats.bank_label(ifsc) or ifsc[:4]} {mask(acct)}"
    if row is None:
        row = BankDebitAccount(company_id=company_id)
        db.add(row)
    row.label, row.account_number, row.ifsc, row.format_key = label[:120], acct, ifsc, key
    row.legal_entity_id = data.get("legal_entity_id")
    row.bank_name = (data.get("bank_name") or formats.bank_label(ifsc) or "")[:150] or None
    for k, n in (("account_name", 200), ("client_code", 40), ("corporate_id", 40)):
        if k in data:
            row.__setattr__(k, (data.get(k) or "").strip()[:n] or None)
    if "notes" in data:
        row.notes = (data.get("notes") or "").strip() or None
    if data.get("active") is not None:
        row.active = bool(data["active"])
    if data.get("is_default") is not None:
        row.is_default = bool(data["is_default"])
    db.flush()
    if row.is_default:
        db.query(BankDebitAccount).filter(BankDebitAccount.company_id == company_id, BankDebitAccount.id != row.id,
                                          BankDebitAccount.legal_entity_id.is_(row.legal_entity_id) if row.legal_entity_id is None
                                          else BankDebitAccount.legal_entity_id == row.legal_entity_id
                                          ).update({"is_default": False}, synchronize_session=False)
    errs = engine.check_debit(_debit(row), spec_for(db, company_id, key)[1])
    if errs:
        raise BadRequest(errs[0][1])
    return row


def account_row(db: Session, company_id: int, uuid) -> BankDebitAccount:
    r = db.query(BankDebitAccount).filter(BankDebitAccount.company_id == company_id, BankDebitAccount.uuid == uuid).first()
    if not r:
        raise NotFound("Debit account")
    return r


def _debit(r: BankDebitAccount) -> Debit:
    return Debit(r.account_number, r.ifsc.upper(), r.account_name or "", r.client_code or "", r.corporate_id or "",
                 r.bank_name or "")


# ── Pay run files ────────────────────────────────────────────────────

def _settings(db: Session, company_id: int) -> dict:
    from modules.payroll import service as psvc
    return psvc.get_settings(db, company_id)


def _ctx(value_date: date, narration: str, period: str, month: str, batch: int, file_date: date) -> dict:
    return {"date": value_date, "narration": narration, "period": period, "month": month, "batch": batch,
            "batch_ref": f"SAL{period}{batch:02d}", "file_date": file_date}


def payees_for_run(db: Session, company_id: int, run, released_only: bool = False) -> tuple[list[tuple[int, Payee]], list[Problem]]:
    """[(legal entity id, payee)] and people left out for missing bank details."""
    from modules.employees.models import BankAccount
    from modules.payroll import service as psvc
    from modules.payroll.models import Payslip
    slips = (db.query(Payslip).filter(Payslip.pay_run_id == run.id, Payslip.company_id == company_id,
                                      Payslip.status.in_(("calculated", "held"))).order_by(Payslip.id).all())
    slips = [p for p in slips if (p.status == "calculated" and not released_only) or (p.status == "held" and p.released_at)]
    slips = [p for p in slips if Decimal(str(p.net or 0)) > 0 and p.payment_status != "paid"]
    acct_ids = [((p.snapshot or {}).get("bank") or {}).get("account_id") for p in slips]
    accounts = {b.id: b for b in db.query(BankAccount).filter(BankAccount.company_id == company_id,
                                                              BankAccount.id.in_([a for a in acct_ids if a] or [-1])).all()}
    profiles = psvc.profiles_for(db, company_id, [p.employee_id for p in slips])
    out, problems = [], []
    for p in slips:
        prof = profiles.get(p.employee_id)
        if prof and prof.payment_mode != "bank":
            continue
        s = p.snapshot or {}
        b = accounts.get((s.get("bank") or {}).get("account_id"))
        if not b:
            problems.append(Problem("error", "no_bank", "No bank account on the payslip.", s.get("name") or "",
                                    s.get("employee_code") or "", p.id))
            continue
        out.append(((s.get("entity") or {}).get("id") or 0,
                    Payee(b.account_holder or s.get("name") or "", b.account_number, (b.ifsc or "").upper(), engine.money(p.net),
                          s.get("employee_code") or "", s.get("email") or "", s.get("phone") or "",
                          (s.get("bank") or {}).get("name") or "", p.id, p.employee_id)))
    return out, problems


def _pick_accounts(db: Session, company_id: int) -> tuple[dict, BankDebitAccount | None]:
    rows = (db.query(BankDebitAccount).filter(BankDebitAccount.company_id == company_id, BankDebitAccount.active.is_(True))
            .order_by(BankDebitAccount.is_default.desc(), BankDebitAccount.id).all())
    by_entity: dict[int, BankDebitAccount] = {}
    company_wide = None
    for r in rows:
        if r.legal_entity_id is None:
            company_wide = company_wide or r
        else:
            by_entity.setdefault(r.legal_entity_id, r)
    return by_entity, company_wide


def plan(db: Session, company_id: int, run, format_key: str | None = None, released_only: bool = False) -> dict:
    """Work out the files without saving anything: batches, totals and problems."""
    settings = _settings(db, company_id)
    bank = settings.get("bank") or {}
    rules = {"other_bank_mode": bank.get("other_bank_mode") or "auto", "rtgs_from": bank.get("rtgs_from") or 200000}
    payees, problems = payees_for_run(db, company_id, run, released_only)
    by_entity, company_wide = _pick_accounts(db, company_id)
    legacy = Debit(re.sub(r"\s", "", bank.get("debit_account") or ""), (bank.get("debit_ifsc") or "").upper(), "",
                   bank.get("client_code") or "", "", "")
    batches: OrderedDict[tuple, dict] = OrderedDict()
    blocking: list[str] = []
    seen_accounts: dict[str, str] = {}
    for eid, p in payees:
        acc = by_entity.get(eid) or company_wide
        key = format_key or (acc.format_key if acc else bank.get("default_format") or "generic_neft")
        try:
            fname, spec = spec_for(db, company_id, key)
        except BadRequest as exc:
            blocking.append(str(exc))
            continue
        debit = _debit(acc) if acc else legacy
        found = engine.check_payee(p)
        problems.extend(found)
        if any(x.level == "error" for x in found):
            continue
        acct_key = re.sub(r"[\s-]", "", p.account) + p.ifsc
        if acct_key in seen_accounts:
            problems.append(Problem("warn", "duplicate_account", f"Same bank account as {seen_accounts[acct_key]}.", p.name,
                                    p.employee_code, p.payslip_id))
        seen_accounts.setdefault(acct_key, p.name)
        mode = engine.mode_for(p, debit.ifsc, spec, rules)
        if mode == "rtgs" and p.amount < engine.RTGS_MIN:
            mode = "neft"
        group = engine.group_of(mode, spec)
        bk = (acc.id if acc else None, key, group)
        if bk not in batches:
            batches[bk] = {"account": acc, "debit": debit, "key": key, "format_name": fname, "spec": spec, "group": group,
                           "lines": [], "entity_id": eid}
        batches[bk]["lines"].append(Line(p, mode))
    for b in batches.values():
        for kind, msg in engine.check_debit(b["debit"], b["spec"]):
            if kind == "account" and b["account"] is None:
                # no debit account set up yet: the file is still made, with the column empty
                if not any(x.code == "no_debit_account" for x in problems):
                    problems.append(Problem("warn", "no_debit_account", msg + " Add the company's debit account in Payroll "
                                            "settings → Bank files."))
                continue
            if msg not in blocking:
                blocking.append(msg)
    return {"batches": list(batches.values()), "problems": problems, "blocking": blocking, "rules": rules,
            "narration": _narration(run, bank)}


def _narration(run, bank: dict) -> str:
    if run.kind == "settlement":
        return f"Full and final settlement {run.period_start:%b %Y}"
    return (bank.get("narration") or "Salary {month}").replace("{month}", run.period_start.strftime("%b %Y"))


def plan_out(p: dict) -> dict:
    return {
        "files": [{"debit_account": b["account"].label if b["account"] else "Account in payroll settings",
                   "debit_masked": mask(b["debit"].account), "format_key": b["key"], "format_name": b["format_name"],
                   "group": b["group"], "count": len(b["lines"]),
                   "total": str(sum((ln.payee.amount for ln in b["lines"]), Decimal("0"))),
                   "modes": {m: sum(1 for ln in b["lines"] if ln.mode == m) for m in ("internal", "neft", "rtgs", "imps")
                             if any(ln.mode == m for ln in b["lines"])}}
                  for b in p["batches"]],
        "problems": [{"level": x.level, "code": x.code, "message": x.message, "name": x.name, "employee_code": x.employee_code}
                     for x in p["problems"]],
        "blocking": p["blocking"],
        "errors": sum(1 for x in p["problems"] if x.level == "error"),
        "warnings": sum(1 for x in p["problems"] if x.level == "warn"),
    }


def generate(db: Session, company_id: int, run, format_key: str | None = None, released_only: bool = False,
             leave_out_problems: bool = False) -> list[BankFileBatch]:
    p = plan(db, company_id, run, format_key, released_only)
    if p["blocking"]:
        raise BadRequest(p["blocking"][0])
    errors = [x for x in p["problems"] if x.level == "error"]
    if errors and not leave_out_problems:
        raise BadRequest(f"{len(errors)} payment(s) have problems (for example {errors[0].name}: {errors[0].message}) "
                         "Fix them, or choose to leave them out of the file.")
    if not p["batches"]:
        raise BadRequest("Nobody to pay in this file (check holds, payment mode and bank details).")
    left_out = [{"name": x.name, "employee_code": x.employee_code, "reason": x.message} for x in errors]
    prev = db.query(BankFileBatch).filter(BankFileBatch.company_id == company_id, BankFileBatch.pay_run_id == run.id,
                                          BankFileBatch.released_only.is_(released_only), BankFileBatch.status == "generated").all()
    for b in prev:
        b.status = "superseded"
    value_date = run.pay_date or date.today()
    period = run.period_start.strftime("%Y%m")
    month = run.period_start.strftime("%b %Y")
    today = datetime.now(timezone.utc).date()
    out = []
    for i, b in enumerate(p["batches"], start=1):
        ctx = _ctx(value_date, p["narration"], period, month, i, today)
        gspec = engine.for_group(b["spec"], b["group"])
        try:
            r = engine.render(gspec, b["lines"], b["debit"], ctx)
            name = engine.file_name(gspec, b["key"], ctx, b["debit"], b["group"])
        except SpecError as exc:
            raise BadRequest(str(exc))
        row = BankFileBatch(
            company_id=company_id, pay_run_id=run.id, debit_account_id=b["account"].id if b["account"] else None,
            legal_entity_id=b["entity_id"] or None, format_key=b["key"], format_name=b["format_name"], txn_group=b["group"],
            file_name=name, content_type="text/csv" if gspec.get("extension") == "csv" else "text/plain",
            content=r.content, sha256=hashlib.sha256(r.content).hexdigest(), record_count=r.count, total_amount=r.total,
            value_date=value_date, released_only=released_only, debit_account_masked=mask(b["debit"].account),
            payslip_ids=[ln.payee.payslip_id for ln in b["lines"]], left_out=left_out if i == 1 else [],
            created_by_name=actor())
        db.add(row)
        db.flush()
        audit.event(db, "payroll.bank_file_generated", "bank_file_batches", row.id,
                    {"run": run.title, "file": name, "format": b["format_name"], "records": r.count, "total": str(r.total),
                     "sha256": row.sha256, "debit_account": row.debit_account_masked, "left_out": len(left_out) if i == 1 else 0},
                    entity_uuid=str(row.uuid), entity_label=name)
        out.append(row)
    return out


def batch_out(b: BankFileBatch, run_title: str | None = None) -> dict:
    return {"uuid": str(b.uuid), "file_name": b.file_name, "format_key": b.format_key, "format_name": b.format_name,
            "group": b.txn_group, "count": b.record_count, "total": str(b.total_amount), "value_date": b.value_date,
            "released_only": b.released_only, "debit_account": b.debit_account_masked, "sha256": b.sha256,
            "status": b.status, "left_out": b.left_out or [], "download_count": b.download_count,
            "last_downloaded_at": b.last_downloaded_at, "created_by": b.created_by_name, "created_at": b.created_at,
            "run_title": run_title, "size": len(b.content or b"")}


def batch_row(db: Session, company_id: int, uuid) -> BankFileBatch:
    r = db.query(BankFileBatch).filter(BankFileBatch.company_id == company_id, BankFileBatch.uuid == uuid).first()
    if not r:
        raise NotFound("Bank file")
    return r


def downloaded(db: Session, b: BankFileBatch) -> None:
    b.download_count = (b.download_count or 0) + 1
    b.last_downloaded_at = datetime.now(timezone.utc)
    audit.event(db, "export.downloaded", "bank_file_batches", b.id, {"file": b.file_name, "sha256": b.sha256},
                entity_uuid=str(b.uuid), entity_label=b.file_name)
