"""Income tax for employees: regime choice with comparison, investment declarations,
proof submission and HR verification, perquisites, the tax worksheet and Form 16
(Part B generated, Part A uploaded from TRACES and merged).
"""
from __future__ import annotations

import io
import re
import zipfile
from collections import defaultdict
from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from core import audit
from core.errors import BadRequest, NotFound
from core.timeutil import today_for_company_id
from modules.payroll import adapters, calc, pdf, statutory, taxcalc
from modules.payroll import runs as runsvc
from modules.payroll import service as svc
from modules.payroll.calc import ZERO, d, money
from modules.payroll.models import PayRun, PayrollFile, Payslip, Perquisite, TaxDeclaration

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")

# Items an employee can declare, by section.
ITEMS = [
    ("80C", "ppf", "Public Provident Fund (PPF)"), ("80C", "elss", "ELSS mutual funds"),
    ("80C", "life_insurance", "Life insurance premium"), ("80C", "tuition", "Children's tuition fees"),
    ("80C", "home_loan_principal", "Home loan principal"), ("80C", "nsc", "National Savings Certificate"),
    ("80C", "tax_saver_fd", "5-year tax saver deposit"), ("80C", "ssy", "Sukanya Samriddhi"),
    ("80CCD1B", "nps_own", "Own NPS contribution (extra ₹50,000)"),
    ("80D_self", "health_self", "Health insurance: self, spouse, children"),
    ("80D_self", "checkup", "Preventive health check-up"),
    ("80D_parents", "health_parents", "Health insurance: parents"),
    ("80E", "education_loan", "Education loan interest"), ("80G", "donations", "Donations (qualifying amount)"),
    ("80TTA", "savings_interest", "Savings account interest"),
    ("24b", "home_loan_interest", "Home loan interest (self-occupied)"),
    ("lta", "lta", "Leave travel (tickets for journeys this year)"),
]
ITEM_INDEX = {k: (s, label) for s, k, label in ITEMS}


def _now():
    return datetime.now(timezone.utc)


def _in_window(today: date, frm: str, to: str, fy: str) -> bool:
    a, b = calc.fy_bounds(fy)

    def at(mmdd: str) -> date:
        mm, dd = (int(x) for x in mmdd.split("-"))
        return date(a.year if mm >= 4 else b.year, mm, dd)
    return at(frm) <= today <= at(to)


def windows(settings: dict, today: date, fy: str) -> dict:
    t = settings.get("tax") or {}
    return {"declaration_open": _in_window(today, t.get("declaration_from", "04-01"), t.get("declaration_to", "07-31"), fy),
            "proof_open": _in_window(today, t.get("proof_from", "01-01"), t.get("proof_to", "02-28"), fy),
            "declaration_from": t.get("declaration_from"), "declaration_to": t.get("declaration_to"),
            "proof_from": t.get("proof_from"), "proof_to": t.get("proof_to"),
            "allow_regime_change": t.get("allow_regime_change", True)}


def declaration(db: Session, company_id: int, employee_id: int, fy: str, create: bool = True) -> TaxDeclaration | None:
    row = db.query(TaxDeclaration).filter(TaxDeclaration.company_id == company_id, TaxDeclaration.employee_id == employee_id,
                                          TaxDeclaration.fy == fy).first()
    if row is None and create:
        settings = svc.get_settings(db, company_id)
        row = TaxDeclaration(company_id=company_id, employee_id=employee_id, fy=fy,
                             regime=settings["tax"].get("default_regime", "new"), items=[], hra={}, previous_employer={},
                             fbp={})
        db.add(row)
        db.flush()
    return row


def get_by_uuid(db: Session, company_id: int, uuid) -> TaxDeclaration:
    row = db.query(TaxDeclaration).filter(TaxDeclaration.company_id == company_id, TaxDeclaration.uuid == uuid).first()
    if not row:
        raise NotFound("Tax declaration")
    return row


def set_regime(db: Session, company_id: int, decl: TaxDeclaration, regime: str, *, by_hr: bool) -> None:
    if regime not in ("new", "old"):
        raise BadRequest("Choose the new or the old regime.")
    settings = svc.get_settings(db, company_id)
    if not by_hr and (decl.regime_locked or not settings["tax"].get("allow_regime_change", True)):
        raise BadRequest("Your regime for this year is fixed. Ask HR to change it.")
    if decl.regime != regime:
        audit.event(db, "tax.regime_changed", "tax_declarations", decl.id, {"from": decl.regime, "to": regime, "fy": decl.fy},
                    entity_uuid=str(decl.uuid))
    decl.regime = regime


def save(db: Session, company_id: int, decl: TaxDeclaration, data: dict, *, by_hr: bool) -> TaxDeclaration:
    today = today_for_company_id(db, company_id)
    w = windows(svc.get_settings(db, company_id), today, decl.fy)
    if not by_hr and not (w["declaration_open"] or w["proof_open"]) and decl.status != "draft":
        raise BadRequest("The declaration window is closed. Ask HR to update it.")
    if not by_hr and decl.status == "verified":
        raise BadRequest("HR has verified your proofs; ask HR to change anything.")
    if "regime" in data and data["regime"]:
        set_regime(db, company_id, decl, data["regime"], by_hr=by_hr)
    if "items" in data:
        old = {it["key"]: it for it in decl.items or []}
        items = []
        for it in data["items"] or []:
            key = it.get("key")
            if key not in ITEM_INDEX:
                continue
            amt = d(it.get("declared"))
            if amt < 0:
                raise BadRequest("Amounts cannot be negative.")
            if amt == 0 and not old.get(key, {}).get("file_ids"):
                continue
            section, label = ITEM_INDEX[key]
            prev = old.get(key, {})
            items.append({"key": key, "section": section, "label": label, "declared": money(amt),
                          "senior": bool(it.get("senior")) if section == "80D_parents" else False,
                          "proof_amount": it.get("proof_amount", prev.get("proof_amount")),
                          "approved": prev.get("approved"), "status": prev.get("status", "declared"),
                          "comment": prev.get("comment"), "file_ids": prev.get("file_ids", [])})
        decl.items = items
    if "hra" in data:
        h = data["hra"] or {}
        months = [{"month": m.get("month"), "rent": money(d(m.get("rent"))), "approved": None}
                  for m in h.get("months") or [] if d(m.get("rent")) > 0]
        total = sum((d(m["rent"]) for m in months), ZERO)
        pan = (h.get("landlord_pan") or "").strip().upper()
        sec = statutory.rule(db, company_id, "tax_sections", None, calc.fy_bounds(decl.fy)[0]) or {}
        limit = d((sec.get("hra") or {}).get("landlord_pan_above", 100000))
        if total > limit and not PAN_RE.match(pan):
            raise BadRequest(f"Rent above {calc.inr(limit)} a year needs the landlord's PAN.")
        decl.hra = {"months": months, "landlord_name": h.get("landlord_name"), "landlord_pan": pan or None,
                    "landlord_address": h.get("landlord_address"), "metro": bool(h.get("metro"))}
    if "previous_employer" in data:
        p = data["previous_employer"] or {}
        decl.previous_employer = {k: (money(d(p.get(k))) if k in ("income", "tds", "pf", "pt") else p.get(k))
                                  for k in ("employer_name", "tan", "income", "tds", "pf", "pt")}
    if "other_income" in data:
        decl.other_income = d(data["other_income"])
    if "extra_tds_monthly" in data:
        decl.extra_tds_monthly = max(d(data["extra_tds_monthly"]), ZERO)
    if "fbp" in data:
        decl.fbp = clean_fbp(db, company_id, data["fbp"] or {})
    if data.get("submit"):
        decl.status = "submitted" if decl.status in ("draft", "submitted") else decl.status
        decl.submitted_at = _now()
    db.flush()
    return decl


def clean_fbp(db: Session, company_id: int, fbp: dict) -> dict:
    from modules.payroll.structures import components
    allowed = {c.code: c for c in components(db, company_id) if c.fbp and c.active}
    out = {}
    for k, v in fbp.items():
        c = allowed.get(k.upper())
        if not c:
            continue
        amt = d(v)
        if c.fbp_max is not None and amt > d(c.fbp_max):
            raise BadRequest(f"{c.name}: at most {calc.inr(c.fbp_max)} a year.")
        if amt > 0:
            out[c.code] = money(amt)
    return out


def add_proof(db: Session, company_id: int, decl: TaxDeclaration, item_key: str, filename: str, content_type: str,
              content: bytes, amount=None, *, by_hr: bool = False) -> PayrollFile:
    today = today_for_company_id(db, company_id)
    if not by_hr and not windows(svc.get_settings(db, company_id), today, decl.fy)["proof_open"]:
        raise BadRequest("The proof window is closed.")
    if len(content) > 5 * 1024 * 1024:
        raise BadRequest("Files must be 5 MB or smaller.")
    if item_key != "hra" and item_key not in {it["key"] for it in decl.items or []}:
        raise BadRequest("Declare the item first, then add its proof.")
    f = PayrollFile(company_id=company_id, employee_id=decl.employee_id, kind="tax_proof", fy=decl.fy,
                    ref=f"{decl.uuid}:{item_key}", filename=filename[:255], content_type=content_type or "application/octet-stream",
                    size=len(content), content=content, uploaded_by_name=svc.actor())
    db.add(f)
    db.flush()
    if item_key == "hra":
        decl.hra = {**(decl.hra or {}), "file_ids": list((decl.hra or {}).get("file_ids") or []) + [str(f.uuid)]}
    else:
        items = []
        for it in decl.items or []:
            if it["key"] == item_key:
                it = {**it, "file_ids": list(it.get("file_ids") or []) + [str(f.uuid)], "status": "proof_submitted",
                      "proof_amount": money(d(amount)) if amount not in (None, "") else it.get("proof_amount") or it["declared"]}
            items.append(it)
        decl.items = items
    return f


def submit_proofs(db: Session, company_id: int, decl: TaxDeclaration) -> None:
    decl.status = "proofs_submitted"
    decl.proofs_submitted_at = _now()


def verify(db: Session, company_id: int, decl: TaxDeclaration, data: dict) -> TaxDeclaration:
    """HR accepts, rejects or partly accepts each item (with a comment); `complete` makes
    the approved amounts count for TDS from the next payroll."""
    by_key = {x["key"]: x for x in data.get("items") or []}
    items = []
    for it in decl.items or []:
        v = by_key.get(it["key"])
        if v:
            status = v.get("status")
            if status not in ("accepted", "rejected", "partial"):
                raise BadRequest("Accept, reject or partly accept each item.")
            approved = d(it.get("proof_amount") or it["declared"]) if status == "accepted" else ZERO if status == "rejected" \
                else d(v.get("approved"))
            if status == "partial" and approved <= 0:
                raise BadRequest(f"{it['label']}: enter the amount accepted.")
            it = {**it, "status": status, "approved": money(approved), "comment": v.get("comment")}
        items.append(it)
    decl.items = items
    if "hra_approved" in data:
        hra = dict(decl.hra or {})
        approved = d(data["hra_approved"])
        total = sum((d(m["rent"]) for m in hra.get("months") or []), ZERO)
        months = hra.get("months") or []
        for m in months:
            m["approved"] = money(d(m["rent"]) * approved / total) if total else "0.00"
        hra["months"], hra["comment"] = months, data.get("hra_comment")
        decl.hra = hra
    if data.get("complete"):
        decl.status = "verified"
        decl.verified_at = _now()
        decl.verified_by_name = svc.actor()
        audit.event(db, "tax.proofs_verified", "tax_declarations", decl.id, {"fy": decl.fy}, entity_uuid=str(decl.uuid))
    db.flush()
    return decl


def declaration_out(decl: TaxDeclaration, names: dict | None = None) -> dict:
    declared = sum((d(it.get("declared")) for it in decl.items or []), ZERO)
    approved = sum((d(it.get("approved")) for it in decl.items or [] if it.get("approved") is not None), ZERO)
    rent = sum((d(m.get("rent")) for m in (decl.hra or {}).get("months") or []), ZERO)
    return {"uuid": str(decl.uuid), "employee_id": decl.employee_id, "employee_name": (names or {}).get(decl.employee_id),
            "fy": decl.fy, "regime": decl.regime, "regime_locked": decl.regime_locked, "status": decl.status,
            "items": decl.items or [], "hra": decl.hra or {}, "previous_employer": decl.previous_employer or {},
            "other_income": money(decl.other_income), "extra_tds_monthly": money(decl.extra_tds_monthly), "fbp": decl.fbp or {},
            "opening_ytd": decl.opening_ytd or {},
            "declared_total": money(declared), "approved_total": money(approved), "rent_total": money(rent),
            "submitted_at": decl.submitted_at.isoformat() if decl.submitted_at else None,
            "proofs_submitted_at": decl.proofs_submitted_at.isoformat() if decl.proofs_submitted_at else None,
            "verified_at": decl.verified_at.isoformat() if decl.verified_at else None, "verified_by_name": decl.verified_by_name}


def proof_files(db: Session, company_id: int, decl: TaxDeclaration) -> list[dict]:
    rows = db.query(PayrollFile).filter(PayrollFile.company_id == company_id, PayrollFile.kind == "tax_proof",
                                        PayrollFile.ref.like(f"{decl.uuid}:%")).all()
    return [{"uuid": str(f.uuid), "item": f.ref.split(":", 1)[1], "filename": f.filename, "size": f.size,
             "content_type": f.content_type, "uploaded_by_name": f.uploaded_by_name,
             "created_at": f.created_at.isoformat()} for f in rows]


def get_file(db: Session, company_id: int, uuid) -> PayrollFile:
    f = db.query(PayrollFile).filter(PayrollFile.company_id == company_id, PayrollFile.uuid == uuid).first()
    if not f:
        raise NotFound("File")
    return f


# ── Projection, comparison and worksheet ───────────────────────────

def projection(db: Session, company_id: int, emp, fy: str, regime: str | None = None) -> taxcalc.TaxInputs:
    """The year as payroll sees it today: paid months from payslips, the rest from the
    current salary, declarations as declared (or as verified)."""
    today = today_for_company_id(db, company_id)
    a, b = calc.fy_bounds(fy)
    on = min(max(today, a), b)
    book = statutory.RuleBook(db, company_id, on)
    settings = svc.get_settings(db, company_id)
    stub = PayRun(id=-1, period_start=calc.add_months(on.replace(day=1), 1) if on < b else b, period_end=b)
    ytd = runsvc._ytd(db, company_id, emp.id, fy, stub)
    paid_months = len({p.period_start for p in ytd["rows"] if p.status != "skipped"})
    months_left = max(12 - paid_months, 0)
    if emp.exit_date and emp.exit_date <= b:
        months_left = 0
    sal = svc.current_salary(db, company_id, emp.id, on)
    comps = runsvc.comp_attrs(db, company_id)
    full = {c["code"]: d(c["monthly"]) for c in (sal.components if sal else []) if c.get("kind") in ("earning", "reimbursement", "employer")}
    profile = svc.profile_for(db, company_id, emp)
    taxable = sum((v for k, v in full.items() if comps.get(k, {}).get("kind") in ("earning", "reimbursement")
                   and comps.get(k, {}).get("taxability", "taxable") != "exempt"), ZERO)
    epf = book.need("epf")
    pf_wage = sum((v for k, v in full.items() if comps.get(k, {}).get("pf_wage")), ZERO)
    pf = calc.pf(pf_wage, epf, applicable=profile.pf_applicable, employee_basis=profile.pf_employee_basis,
                 vpf_rate=profile.vpf_rate, vpf_amount=profile.vpf_amount)
    company = svc._company(db, company_id)
    state = profile.pt_state or adapters.employee_state(db, emp, company, settings)
    pt_rules = book.get("pt", state) if state else None
    pt_m = calc.professional_tax(pt_rules, month=5, gross=sum((v for k, v in full.items() if comps.get(k, {}).get("kind") == "earning"), ZERO),
                                 gender=svc.gender_of(db, emp)) if pt_rules and pt_rules.get("basis") == "monthly" else ZERO
    decl = declaration(db, company_id, emp.id, fy, create=False)
    regime = regime or (decl.regime if decl else settings["tax"].get("default_regime", "new"))
    dec = runsvc.declaration_amounts(decl)
    perqs = runsvc.perquisites_monthly(db, company_id, emp.id, on)
    # model all remaining months as "future" with months_left+1 so the current month is the first of them
    return taxcalc.TaxInputs(
        regime=regime, regime_rules=book.need("income_tax", regime), sections=book.need("tax_sections"),
        age=calc.age_on(emp.date_of_birth, on), has_pan=bool(emp.pan and PAN_RE.match(emp.pan.upper())),
        months_left=months_left + 1, ytd_taxable=ytd["taxable"], ytd_basic_da=ytd["basic_da"], ytd_hra=ytd["hra"],
        ytd_pf=ytd["pf"], ytd_pt=ytd["pt"], ytd_tds=ytd["tds"], ytd_employer_nps=ytd["nps"], ytd_perquisites=ytd["perqs"],
        future_taxable=taxable, future_basic_da=full.get("BASIC", ZERO) + full.get("DA", ZERO), future_hra=full.get("HRA", ZERO),
        future_pf=pf.employee + pf.vpf, future_pt=pt_m, future_employer_nps=full.get("NPS_ER", ZERO), future_perquisites=perqs,
        deductions=dec["deductions"], rent_paid=dec["rent"],
        metro=bool(dec["metro"] if dec["metro"] is not None else profile.metro), lta_exempt=dec["lta"],
        previous_income=d(dec["prev"].get("income")), previous_tds=d(dec["prev"].get("tds")),
        previous_pf=d(dec["prev"].get("pf")), previous_pt=d(dec["prev"].get("pt")), other_income=dec["other_income"],
        extra_tds=dec["extra_tds"])


def compare(db: Session, company_id: int, emp, fy: str) -> dict:
    t = projection(db, company_id, emp, fy)
    book = statutory.RuleBook(db, company_id, calc.fy_bounds(fy)[0])
    return taxcalc.compare(t, {"new": book.need("income_tax", "new"), "old": book.need("income_tax", "old")})


def worksheet(db: Session, company_id: int, emp, fy: str) -> dict:
    t = projection(db, company_id, emp, fy)
    ws = taxcalc.annual_worksheet(t)
    a, _ = calc.fy_bounds(fy)
    slips = (db.query(Payslip).join(PayRun, PayRun.id == Payslip.pay_run_id)
             .filter(Payslip.company_id == company_id, Payslip.employee_id == emp.id, Payslip.fy == fy,
                     PayRun.status.in_(("approved", "locked", "paid"))).order_by(Payslip.period_start).all())
    by_month = defaultdict(lambda: {"gross": ZERO, "taxable": ZERO, "tds": ZERO, "paid": True})
    for p in slips:
        m = by_month[p.period_start.strftime("%Y-%m")]
        m["gross"] += d(p.gross)
        m["taxable"] += d(((p.tax or {}).get("month") or {}).get("taxable"))
        m["tds"] += d(((p.tax or {}).get("month") or {}).get("tds"))
    paid_tds = sum((m["tds"] for m in by_month.values()), ZERO)
    remaining = max(d(ws["annual_tax"]) - paid_tds - t.previous_tds, ZERO)
    future_months = [calc.add_months(a, i).strftime("%Y-%m") for i in range(12) if calc.add_months(a, i).strftime("%Y-%m") not in by_month]
    future_months = future_months[: max(t.months_left - 1, 0)]
    per = (remaining / len(future_months)).quantize(d("1")) if future_months else ZERO
    months = [{"month": k, "gross": money(v["gross"]), "taxable": money(v["taxable"]), "tds": money(v["tds"]), "paid": True}
              for k, v in sorted(by_month.items())]
    months += [{"month": k, "gross": money(t.future_taxable), "taxable": money(t.future_taxable), "tds": money(per), "paid": False}
               for k in future_months]
    return {"fy": fy, "worksheet": ws, "months": months, "tds_paid": money(paid_tds), "tds_remaining": money(remaining)}


# ── Perquisites ─────────────────────────────────────────────────────

def perquisite_value(db: Session, company_id: int, kind: str, details: dict, on: date, emp) -> object:
    rules = statutory.rule(db, company_id, "perquisites", None, on) or {}
    if kind == "car":
        r = rules.get("car") or {}
        v = d(r.get("own_large") if d(details.get("engine_cc")) > d(r.get("engine_cc_limit", 1600)) else r.get("own_small"))
        if details.get("driver"):
            v += d(r.get("driver"))
        if details.get("personal_only"):
            v = d(details.get("monthly_cost"))
        return v
    if kind == "accommodation":
        r = rules.get("accommodation") or {}
        band = details.get("city_band") or "other"
        pct = d(r.get(band, r.get("other", 5)))
        sal = svc.current_salary(db, company_id, emp.id, on)
        base = d(sal.monthly_gross) if sal else ZERO
        v = base * pct / 100
        if details.get("rent_paid_by_employer"):
            v = min(v, d(details["rent_paid_by_employer"]))
        return max(v - d(details.get("recovered")), ZERO).quantize(d("1"))
    return d(details.get("monthly_value"))


def save_perquisite(db: Session, company_id: int, emp, data: dict, row: Perquisite | None = None) -> Perquisite:
    kind = data.get("kind") or (row.kind if row else None)
    if kind not in ("car", "accommodation", "other"):
        raise BadRequest("Choose car, accommodation or other.")
    if row is None:
        row = Perquisite(company_id=company_id, employee_id=emp.id, kind=kind)
        db.add(row)
    row.kind = kind
    row.details = data.get("details") or {}
    row.effective_from = data.get("effective_from") or today_for_company_id(db, company_id)
    row.effective_to = data.get("effective_to")
    row.note = data.get("note")
    row.active = data.get("active", True)
    row.monthly_value = perquisite_value(db, company_id, kind, row.details, row.effective_from, emp)
    db.flush()
    return row


def perquisite_out(p: Perquisite, names: dict | None = None) -> dict:
    return {"uuid": str(p.uuid), "employee_id": p.employee_id, "employee_name": (names or {}).get(p.employee_id),
            "kind": p.kind, "details": p.details or {}, "monthly_value": money(p.monthly_value),
            "effective_from": p.effective_from.isoformat(), "effective_to": p.effective_to.isoformat() if p.effective_to else None,
            "note": p.note, "active": p.active}


# ── Form 16 ─────────────────────────────────────────────────────────

def form16_data(db: Session, company_id: int, emp, fy: str) -> tuple[dict, list[dict]]:
    slips = (db.query(Payslip).join(PayRun, PayRun.id == Payslip.pay_run_id)
             .filter(Payslip.company_id == company_id, Payslip.employee_id == emp.id, Payslip.fy == fy,
                     PayRun.status.in_(("locked", "paid")), Payslip.status.in_(("calculated", "held")))
             .order_by(Payslip.period_start).all())
    if not slips:
        raise BadRequest("No locked payslips for this employee in the year.")
    t = projection(db, company_id, emp, fy)
    t.months_left, t.future_taxable = 1, ZERO  # actuals only
    t.future_basic_da = t.future_hra = t.future_pf = t.future_pt = t.future_employer_nps = t.future_perquisites = ZERO
    ws = taxcalc.annual_worksheet(t)
    months = defaultdict(lambda: {"gross": ZERO, "tds": ZERO})
    for p in slips:
        m = months[p.period_start.strftime("%b %Y")]
        m["gross"] += d(p.gross)
        m["tds"] += d(((p.tax or {}).get("month") or {}).get("tds"))
    return ws, [{"month": k, "gross": money(v["gross"]), "tds": money(v["tds"])} for k, v in months.items()]


def form16_pdf(db: Session, company_id: int, emp, fy: str, prefer_signed: bool = True) -> bytes:
    if prefer_signed:   # once signed, everyone gets the signed copy
        from modules.payroll import signing
        f = signing.signed_file(db, company_id, emp.id, fy)
        if f is not None:
            return f.content
    ws, months = form16_data(db, company_id, emp, fy)
    company = svc._company(db, company_id)
    entity = adapters.legal_entity(db, company, getattr(emp, "legal_entity_id", None))
    forms = statutory.rule(db, company_id, "forms", None, calc.fy_bounds(fy)[0]) or {}
    designation = emp.designation.designation_name if getattr(emp, "designation", None) else None
    body = pdf.render(pdf.form16_html({"name": svc.name_of(emp), "pan": emp.pan, "employee_code": emp.employee_code,
                                       "designation": designation}, entity, fy, ws, months, forms))
    part_a = db.query(PayrollFile).filter(PayrollFile.company_id == company_id, PayrollFile.employee_id == emp.id,
                                          PayrollFile.kind == "form16_part_a", PayrollFile.fy == fy).order_by(PayrollFile.id.desc()).first()
    if part_a is not None:
        try:
            body = pdf.merge([part_a.content, body])
        except Exception:
            pass
    return body


def form16_bulk(db: Session, company_id: int, fy: str) -> bytes:
    ids = [r[0] for r in db.query(Payslip.employee_id).join(PayRun, PayRun.id == Payslip.pay_run_id)
           .filter(Payslip.company_id == company_id, Payslip.fy == fy, PayRun.status.in_(("locked", "paid"))).distinct().all()]
    if not ids:
        raise BadRequest("No locked payslips in this year.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for eid in ids:
            emp = svc.get_employee(db, company_id, eid)
            try:
                content = form16_pdf(db, company_id, emp, fy)
            except BadRequest:
                continue
            z.writestr(f"Form16-{fy}-{emp.employee_code or eid}-{(emp.pan or 'NOPAN').upper()}.pdf", content)
    audit.event(db, "export.downloaded", "payslips", None, {"report": "form16_bulk", "fy": fy, "employees": len(ids)})
    return buf.getvalue()


def upload_part_a(db: Session, company_id: int, fy: str, files: list[tuple[str, bytes]]) -> dict:
    """Part A PDFs from TRACES are named with the employee's PAN; each is matched by PAN."""
    Employee = svc.employee_model()
    by_pan = {(e.pan or "").upper(): e for e in db.query(Employee).filter(Employee.company_id == company_id).all() if e.pan}
    matched, unmatched = 0, []
    for name, content in files:
        m = re.search(r"[A-Z]{5}[0-9]{4}[A-Z]", name.upper())
        emp = by_pan.get(m.group(0)) if m else None
        if not emp:
            unmatched.append(name)
            continue
        db.add(PayrollFile(company_id=company_id, employee_id=emp.id, kind="form16_part_a", fy=fy, filename=name[:255],
                           content_type="application/pdf", size=len(content), content=content, uploaded_by_name=svc.actor()))
        matched += 1
    db.flush()
    return {"matched": matched, "unmatched": unmatched}


# ── Opening balances (moving from another payroll mid-year) ────────

OPENING_FIELDS = ("gross", "taxable", "tds", "pf", "pt", "basic_da", "hra")


def import_opening(db: Session, company_id: int, fy: str, rows: list[dict]) -> dict:
    """Pay already made earlier in the year by the previous payroll system, per employee:
    employee_code, gross, taxable, tds, pf, pt, basic_da, hra. Counted in TDS, PT cap and Form 16."""
    Employee = svc.employee_model()
    by_code = {(e.employee_code or "").upper(): e for e in db.query(Employee).filter(Employee.company_id == company_id).all()}
    done, errors = 0, []
    for i, r in enumerate(rows, start=2):
        e = by_code.get(str(r.get("employee_code") or "").strip().upper())
        if not e:
            errors.append({"row": i, "message": f"No employee with ID {r.get('employee_code')}."})
            continue
        try:
            vals = {k: money(d(r.get(k))) for k in OPENING_FIELDS if r.get(k) not in (None, "")}
        except Exception:
            errors.append({"row": i, "message": "Amounts must be numbers."})
            continue
        decl = declaration(db, company_id, e.id, fy)
        decl.opening_ytd = vals
        done += 1
    db.flush()
    audit.event(db, "tax.opening_imported", "tax_declarations", None, {"fy": fy, "rows": done, "errors": len(errors)})
    return {"imported": done, "errors": errors}
