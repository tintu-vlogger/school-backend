"""Flexible benefit plan (FBP).

The FBP amount is the yearly total of the salary's flexible components (fuel, phone, meal,
books ...). Each year, in the declaration window, the employee splits it among the FBP
components within each component's yearly limit. Nothing flexible is paid with the monthly
salary: it builds up (accrues) month by month. The employee uploads bills against a
component; payroll verifies them; approved amounts are paid with the next salary, up to
what is due so far (the yearly allocation spread evenly over the year), and are tax-free
when the employee's regime allows it for that component (configurable per component).
What is not claimed by the year end (March, or the exit) is paid as taxable pay.

Statuses of a claim: submitted -> approved | partly_approved | rejected -> paid (or withdrawn)
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from core import audit
from core.errors import BadRequest, Forbidden, NotFound
from core.timeutil import today_for_company_id
from modules.payroll import calc, structures
from modules.payroll import service as svc
from modules.payroll.calc import ZERO, d, money, rnd
from modules.payroll.models import (FbpClaim, FbpDeclaration, PayrollFile, PayRun, PayRunInput, Payslip, SalaryComponent,
                                    TaxDeclaration)

CLAIM_STATUS = {"submitted": "Waiting for verification", "approved": "Approved", "partly_approved": "Partly approved",
                "rejected": "Rejected", "paid": "Paid", "withdrawn": "Withdrawn"}
APPROVED = ("approved", "partly_approved", "paid")
MAX_FILE_MB = 8


def _now():
    return datetime.now(timezone.utc)


def settings_of(db: Session, company_id: int) -> dict:
    return svc.get_settings(db, company_id).get("fbp") or {}


def enabled(db: Session, company_id: int) -> bool:
    return bool(settings_of(db, company_id).get("enabled", True))


def fbp_components(db: Session, company_id: int) -> dict[str, SalaryComponent]:
    return {c.code: c for c in structures.components(db, company_id) if c.fbp and c.active}


def accrue_codes(db: Session, company_id: int) -> set[str]:
    return set(fbp_components(db, company_id)) if enabled(db, company_id) else set()


# ── The plan for a year ────────────────────────────────────────────

def _salary_split(db: Session, company_id: int, employee_id: int, fy: str) -> dict[str, Decimal]:
    """Yearly FBP amounts in the salary in force (today, or at the start of the year)."""
    a, b = calc.fy_bounds(fy)
    on = min(max(today_for_company_id(db, company_id), a), b)
    s = svc.current_salary(db, company_id, employee_id, on) or svc.current_salary(db, company_id, employee_id, b)
    codes = fbp_components(db, company_id)
    out: dict[str, Decimal] = {}
    for c in (s.components if s else []) or []:
        if c["code"] in codes and d(c.get("monthly")) > 0:
            out[c["code"]] = out.get(c["code"], ZERO) + d(c["monthly"]) * 12
    return out


def basket(db: Session, company_id: int, employee_id: int, fy: str) -> Decimal:
    return sum(_salary_split(db, company_id, employee_id, fy).values(), ZERO)


def declaration(db: Session, company_id: int, employee_id: int, fy: str, create: bool = True) -> FbpDeclaration | None:
    x = db.query(FbpDeclaration).filter(FbpDeclaration.company_id == company_id, FbpDeclaration.employee_id == employee_id,
                                        FbpDeclaration.fy == fy).first()
    if x is None and create:
        split = _salary_split(db, company_id, employee_id, fy)
        x = FbpDeclaration(company_id=company_id, employee_id=employee_id, fy=fy,
                           allocation={k: float(v) for k, v in split.items()}, total=sum(split.values(), ZERO),
                           status="draft")
        db.add(x)
        db.flush()
    return x


def allocation_for(db: Session, company_id: int, employee_id: int, fy: str) -> dict[str, Decimal]:
    """The employee's split once declared; the salary's own split until then."""
    x = declaration(db, company_id, employee_id, fy, create=False)
    if x and x.status in ("submitted", "locked") and x.allocation:
        return {k: d(v) for k, v in x.allocation.items() if d(v) > 0}
    return _salary_split(db, company_id, employee_id, fy)


def window(db: Session, company_id: int, fy: str, today: date | None = None) -> dict:
    st = settings_of(db, company_id)
    today = today or today_for_company_id(db, company_id)
    a, b = calc.fy_bounds(fy)

    def at(mmdd: str) -> date:
        m, dd = (int(x) for x in mmdd.split("-"))
        return date(a.year if m >= 4 else b.year, m, dd)
    frm, to = at(st.get("declaration_from", "04-01")), at(st.get("declaration_to", "05-31"))
    last = at(st.get("claim_last_date", "03-10"))
    return {"declaration_from": frm.isoformat(), "declaration_to": to.isoformat(), "declaration_open": frm <= today <= to,
            "claim_last_date": last.isoformat(), "claims_open": a <= today <= last or (today < a)}


def save_declaration(db: Session, company_id: int, x: FbpDeclaration, allocation: dict, *, submit: bool, by_hr: bool) -> FbpDeclaration:
    if not enabled(db, company_id):
        raise BadRequest("The flexible benefit plan is switched off.")
    if x.status == "locked" and not by_hr:
        raise BadRequest("Your flexible benefit choice for this year is locked.")
    w = window(db, company_id, x.fy)
    if not by_hr and not w["declaration_open"] and x.status != "draft":
        raise BadRequest(f"Choices can be changed between {w['declaration_from']} and {w['declaration_to']}.")
    if not by_hr and not w["declaration_open"] and x.status == "draft" and date.fromisoformat(w["declaration_to"]) < \
            today_for_company_id(db, company_id):
        raise BadRequest("The window to choose flexible benefits has closed; the salary's split applies.")
    comps = fbp_components(db, company_id)
    total_allowed = basket(db, company_id, x.employee_id, x.fy)
    clean: dict[str, float] = {}
    for code, amount in (allocation or {}).items():
        code = str(code).upper()
        amt = d(amount)
        if amt == 0:
            continue
        c = comps.get(code)
        if c is None:
            raise BadRequest(f"{code} is not a flexible benefit.")
        if amt < 0:
            raise BadRequest("Amounts cannot be negative.")
        if c.fbp_max is not None and amt > d(c.fbp_max):
            raise BadRequest(f"{c.name}: at most {calc.inr(c.fbp_max)} a year.")
        clean[code] = float(rnd(amt))
    total = sum((d(v) for v in clean.values()), ZERO)
    if total > total_allowed:
        raise BadRequest(f"You have split {calc.inr(total)}; your flexible benefit amount is {calc.inr(total_allowed)} a year.")
    # a component cannot go below what has already been approved against it
    approved = _approved_by_code(db, company_id, x.employee_id, x.fy)
    for code, amt in approved.items():
        if d(clean.get(code, 0)) < amt:
            raise BadRequest(f"{comps[code].name if code in comps else code}: {calc.inr(amt)} is already approved, "
                             "so the allocation cannot be lower.")
    x.allocation, x.total = clean, total_allowed
    if submit:
        x.status = "submitted" if not by_hr else x.status if x.status == "locked" else "submitted"
        x.submitted_at = _now()
    audit.event(db, "payroll.fbp_declared", "fbp_declarations", x.id, {"allocation": clean, "submit": submit},
                entity_uuid=str(x.uuid))
    db.flush()
    return x


def lock_all(db: Session, company_id: int, fy: str) -> int:
    rows = db.query(FbpDeclaration).filter(FbpDeclaration.company_id == company_id, FbpDeclaration.fy == fy,
                                           FbpDeclaration.status == "submitted").all()
    for x in rows:
        x.status = "locked"
    db.flush()
    return len(rows)


# ── Ledger ──────────────────────────────────────────────────────────

def _approved_by_code(db: Session, company_id: int, employee_id: int, fy: str) -> dict[str, Decimal]:
    out: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for c in db.query(FbpClaim).filter(FbpClaim.company_id == company_id, FbpClaim.employee_id == employee_id,
                                       FbpClaim.fy == fy, FbpClaim.status.in_(APPROVED)).all():
        out[c.component_code] += d(c.approved_amount)
    return out


def _slips(db: Session, company_id: int, employee_id: int, fy: str, exclude_run_id: int | None = None) -> list[Payslip]:
    q = (db.query(Payslip).join(PayRun, PayRun.id == Payslip.pay_run_id)
         .filter(Payslip.company_id == company_id, Payslip.employee_id == employee_id, Payslip.fy == fy,
                 PayRun.status.in_(("approved", "locked", "paid")), Payslip.status.in_(("calculated", "held"))))
    if exclude_run_id:
        q = q.filter(Payslip.pay_run_id != exclude_run_id)
    return q.all()


def accrued_ytd(db: Session, company_id: int, employee_id: int, fy: str, exclude_run_id: int | None = None) -> Decimal:
    return sum((d(v) for p in _slips(db, company_id, employee_id, fy, exclude_run_id)
                for v in (((p.statutory or {}).get("fbp_accrued")) or {}).values()), ZERO)


def balance_paid_ytd(db: Session, company_id: int, employee_id: int, fy: str, exclude_run_id: int | None = None) -> Decimal:
    return sum((d(ln["amount"]) for p in _slips(db, company_id, employee_id, fy, exclude_run_id) for ln in p.lines or []
                if ln["code"] == "FBP_BAL"), ZERO)


def months_elapsed(fy: str, on: date) -> int:
    a, b = calc.fy_bounds(fy)
    if on < a:
        return 0
    if on > b:
        return 12
    return (on.year - a.year) * 12 + on.month - a.month + 1


def summary(db: Session, company_id: int, employee_id: int, fy: str, on: date | None = None) -> dict:
    on = on or today_for_company_id(db, company_id)
    comps = fbp_components(db, company_id)
    alloc = allocation_for(db, company_id, employee_id, fy)
    claims = db.query(FbpClaim).filter(FbpClaim.company_id == company_id, FbpClaim.employee_id == employee_id,
                                       FbpClaim.fy == fy).all()
    n = months_elapsed(fy, on)
    total_alloc = sum(alloc.values(), ZERO)
    accrued = accrued_ytd(db, company_id, employee_id, fy)
    rows = []
    for code in sorted(set(alloc) | {c.component_code for c in claims}, key=lambda c: comps[c].sort_order if c in comps else 999):
        mine = [c for c in claims if c.component_code == code]
        pending = sum((d(c.amount) for c in mine if c.status == "submitted"), ZERO)
        approved = sum((d(c.approved_amount) for c in mine if c.status in APPROVED), ZERO)
        paid = sum((d(c.paid_amount) for c in mine), ZERO)
        a = alloc.get(code, ZERO)
        c = comps.get(code)
        rows.append({"code": code, "name": c.name if c else code, "limit": money(c.fbp_max) if c and c.fbp_max is not None else None,
                     "allocated": money(a), "due_so_far": money(rnd(a * n / 12)),
                     "accrued": money(rnd(accrued * a / total_alloc)) if total_alloc else "0.00",
                     "pending": money(pending), "approved": money(approved), "paid": money(paid),
                     "can_claim": money(max(a - approved - pending, ZERO)),
                     "tax_free_regimes": list(c.fbp_tax_free_regimes or []) if c else []})
    decl = declaration(db, company_id, employee_id, fy, create=False)
    return {"fy": fy, "enabled": enabled(db, company_id), "basket": money(basket(db, company_id, employee_id, fy)),
            "allocated": money(total_alloc), "accrued": money(accrued),
            "balance_paid": money(balance_paid_ytd(db, company_id, employee_id, fy)), "components": rows,
            "declaration": declaration_out(decl) if decl else None, "window": window(db, company_id, fy, on)}


def declaration_out(x: FbpDeclaration) -> dict:
    return {"uuid": str(x.uuid), "fy": x.fy, "allocation": {k: money(v) for k, v in (x.allocation or {}).items()},
            "total": money(x.total), "status": x.status,
            "submitted_at": x.submitted_at.isoformat() if x.submitted_at else None}


# ── Claims ─────────────────────────────────────────────────────────

def get_claim(db: Session, company_id: int, uuid) -> FbpClaim:
    c = db.query(FbpClaim).filter(FbpClaim.company_id == company_id, FbpClaim.uuid == uuid).first()
    if not c:
        raise NotFound("Claim")
    return c


def submit_claim(db: Session, company_id: int, employee_id: int, data: dict, files: list[tuple[str, str, bytes]]) -> FbpClaim:
    if not enabled(db, company_id):
        raise BadRequest("The flexible benefit plan is switched off.")
    bill_date: date = data["bill_date"]
    fy = calc.fy_of(bill_date)
    code = (data.get("component_code") or "").upper()
    comps = fbp_components(db, company_id)
    if code not in comps:
        raise BadRequest("Choose the benefit the bill is for.")
    amount = d(data.get("amount"))
    if amount <= 0:
        raise BadRequest("Enter the bill amount.")
    today = today_for_company_id(db, company_id)
    if bill_date > today:
        raise BadRequest("The bill date cannot be in the future.")
    w = window(db, company_id, fy, today)
    if today > date.fromisoformat(w["claim_last_date"]) and calc.fy_of(today) == fy:
        raise BadRequest(f"Bills for FY {fy} had to be sent by {date.fromisoformat(w['claim_last_date']):%d %b %Y}.")
    if calc.fy_of(today) != fy and today > calc.fy_bounds(fy)[1]:
        raise BadRequest("Bills from an earlier financial year cannot be claimed now.")
    if not files:
        raise BadRequest("Attach a photo or PDF of the bill.")
    row = next((r for r in summary(db, company_id, employee_id, fy, today)["components"] if r["code"] == code), None)
    room = d(row["can_claim"]) if row else ZERO
    if amount > room:
        raise BadRequest(f"You can claim up to {calc.inr(room)} more for {comps[code].name} this year.")
    c = FbpClaim(company_id=company_id, employee_id=employee_id, fy=fy, component_code=code, bill_date=bill_date,
                 bill_number=(data.get("bill_number") or None), vendor=(data.get("vendor") or None), amount=amount,
                 description=(data.get("description") or None), status="submitted")
    db.add(c)
    db.flush()
    ids = []
    for name, ctype, content in files:
        if len(content) > MAX_FILE_MB * 1024 * 1024:
            raise BadRequest(f"{name} is larger than {MAX_FILE_MB} MB.")
        if not (ctype.startswith("image/") or ctype == "application/pdf"):
            raise BadRequest("Upload a photo or a PDF of the bill.")
        f = PayrollFile(company_id=company_id, employee_id=employee_id, kind="fbp_bill", fy=fy, ref=str(c.uuid),
                        filename=name[:255], content_type=ctype, size=len(content), content=content,
                        uploaded_by_name=svc.actor())
        db.add(f)
        db.flush()
        ids.append(str(f.uuid))
    c.file_ids = ids
    audit.event(db, "payroll.fbp_claimed", "fbp_claims", c.id, {"code": code, "amount": money(amount)},
                entity_uuid=str(c.uuid))
    return c


def withdraw(db: Session, company_id: int, c: FbpClaim, employee_id: int | None) -> None:
    if employee_id is not None and c.employee_id != employee_id:
        raise Forbidden()
    if c.status != "submitted":
        raise BadRequest("Only a claim waiting for verification can be withdrawn.")
    c.status = "withdrawn"
    db.flush()


def verify(db: Session, company_id: int, c: FbpClaim, *, approved_amount, comment: str | None) -> FbpClaim:
    if c.status not in ("submitted", "approved", "partly_approved") or d(c.paid_amount) > 0:
        raise BadRequest("This claim has been paid or closed.")
    amt = d(approved_amount)
    if amt < 0 or amt > d(c.amount):
        raise BadRequest("The approved amount must be between zero and the bill amount.")
    alloc = allocation_for(db, company_id, c.employee_id, c.fy).get(c.component_code, ZERO)
    others = sum((d(x.approved_amount) for x in db.query(FbpClaim).filter(
        FbpClaim.company_id == company_id, FbpClaim.employee_id == c.employee_id, FbpClaim.fy == c.fy,
        FbpClaim.component_code == c.component_code, FbpClaim.status.in_(APPROVED), FbpClaim.id != c.id).all()), ZERO)
    if others + amt > alloc:
        raise BadRequest(f"Only {calc.inr(max(alloc - others, ZERO))} is left of this year's allocation for this benefit.")
    if amt == 0 and not (comment or "").strip():
        raise BadRequest("Say why the bill is not accepted.")
    c.approved_amount = amt
    c.status = "rejected" if amt == 0 else "approved" if amt == d(c.amount) else "partly_approved"
    c.hr_comment, c.verified_by_name, c.verified_at = (comment or None), svc.actor(), _now()
    audit.event(db, "payroll.fbp_verified", "fbp_claims", c.id, {"approved": money(amt), "status": c.status},
                entity_uuid=str(c.uuid))
    try:
        from modules.notifications.service import notify
        notify(db, company_id, [c.employee_id], kind="payroll",
               title=f"Bill {'accepted' if amt else 'not accepted'}: {calc.inr(c.amount)}",
               body=(f"{calc.inr(amt)} will be paid with your salary." if amt else (comment or "")),
               link="/dashboard/payroll/me?tab=benefits")
    except Exception:
        pass
    db.flush()
    return c


def claim_out(c: FbpClaim, names: dict | None = None, comps: dict | None = None) -> dict:
    comp = (comps or {}).get(c.component_code)
    return {"uuid": str(c.uuid), "employee_id": c.employee_id, "employee_name": (names or {}).get(c.employee_id),
            "fy": c.fy, "component_code": c.component_code, "component_name": comp.name if comp else c.component_code,
            "bill_date": c.bill_date.isoformat(), "bill_number": c.bill_number, "vendor": c.vendor,
            "amount": money(c.amount), "approved_amount": money(c.approved_amount) if c.approved_amount is not None else None,
            "paid_amount": money(c.paid_amount), "description": c.description, "file_ids": c.file_ids or [],
            "status": c.status, "status_label": CLAIM_STATUS.get(c.status, c.status), "hr_comment": c.hr_comment,
            "verified_by_name": c.verified_by_name, "verified_at": c.verified_at.isoformat() if c.verified_at else None,
            "paid_on": c.paid_on.isoformat() if c.paid_on else None,
            "created_at": c.created_at.isoformat() if c.created_at else None}


# ── Payroll ─────────────────────────────────────────────────────────

def _regime(db: Session, company_id: int, employee_id: int, fy: str) -> str:
    decl = db.query(TaxDeclaration).filter(TaxDeclaration.company_id == company_id, TaxDeclaration.employee_id == employee_id,
                                           TaxDeclaration.fy == fy).first()
    return decl.regime if decl else (svc.get_settings(db, company_id).get("tax") or {}).get("default_regime", "new")


def payable_items(db: Session, company_id: int, employee_ids: list[int], period_start: date, period_end: date, *,
                  year_end: set[int] | None = None) -> list[dict]:
    """Approved, unpaid bills to pay in a pay run: up to what is due so far for each component
    (the whole allocation at the year end or on exit)."""
    if not enabled(db, company_id) or not employee_ids:
        return []
    fy = calc.fy_of(period_end)
    comps = fbp_components(db, company_id)
    claims = (db.query(FbpClaim).filter(FbpClaim.company_id == company_id, FbpClaim.employee_id.in_(employee_ids),
                                        FbpClaim.fy == fy, FbpClaim.status.in_(("approved", "partly_approved")))
              .order_by(FbpClaim.employee_id, FbpClaim.bill_date, FbpClaim.id).all())
    by_emp: dict[int, list[FbpClaim]] = defaultdict(list)
    for c in claims:
        by_emp[c.employee_id].append(c)
    out = []
    n = months_elapsed(fy, period_end)
    for eid, rows in by_emp.items():
        alloc = allocation_for(db, company_id, eid, fy)
        regime = _regime(db, company_id, eid, fy)
        paid_by_code = defaultdict(lambda: ZERO)
        for c in db.query(FbpClaim).filter(FbpClaim.company_id == company_id, FbpClaim.employee_id == eid,
                                           FbpClaim.fy == fy).all():
            paid_by_code[c.component_code] += d(c.paid_amount)
        for c in rows:
            due = alloc.get(c.component_code, ZERO) if (year_end and eid in year_end) else rnd(alloc.get(c.component_code, ZERO) * n / 12)
            room = due - paid_by_code[c.component_code]
            amt = min(d(c.approved_amount) - d(c.paid_amount), room)
            if amt <= 0:
                continue
            paid_by_code[c.component_code] += amt
            comp = comps.get(c.component_code)
            free = comp is not None and regime in (comp.fbp_tax_free_regimes or [])
            out.append({"employee_id": eid, "code": c.component_code, "amount": rnd(amt), "taxable": ZERO if free else rnd(amt),
                        "ref": str(c.uuid), "note": f"Bill {c.bill_number or c.bill_date.strftime('%d %b')}"
                                                     f"{' (taxable in the ' + regime + ' regime)' if not free else ''}"})
    return out


def mark_paid(db: Session, company_id: int, run: PayRun, paid_ids: set[int]) -> None:
    rows = db.query(PayRunInput).filter(PayRunInput.pay_run_id == run.id, PayRunInput.source == "fbp").all()
    claims = {str(c.uuid): c for c in db.query(FbpClaim).filter(
        FbpClaim.company_id == company_id, FbpClaim.uuid.in_([r.source_ref for r in rows if r.source_ref] or
                                                             ["00000000-0000-0000-0000-000000000000"])).all()}
    for r in rows:
        c = claims.get(r.source_ref)
        if c is None or r.employee_id not in paid_ids:
            continue
        c.paid_amount = d(c.paid_amount) + d(r.amount)
        c.pay_run_id, c.paid_on = run.id, run.pay_date or run.period_end
        if d(c.paid_amount) >= d(c.approved_amount):
            c.status = "paid"


def unmark_run(db: Session, company_id: int, run: PayRun) -> None:
    rows = db.query(PayRunInput).filter(PayRunInput.pay_run_id == run.id, PayRunInput.source == "fbp").all()
    for r in rows:
        c = db.query(FbpClaim).filter(FbpClaim.company_id == company_id, FbpClaim.uuid == r.source_ref).first()
        if c is None:
            continue
        c.paid_amount = max(d(c.paid_amount) - d(r.amount), ZERO)
        if c.status == "paid":
            c.status = "approved" if d(c.approved_amount) == d(c.amount) else "partly_approved"
        if d(c.paid_amount) == 0:
            c.pay_run_id, c.paid_on = None, None


def year_end_balance(db: Session, company_id: int, employee_id: int, fy: str, *, run_id: int | None,
                     accrued_now: Decimal, claims_now: Decimal) -> Decimal:
    """What has accrued in the year (including now) less bills paid (including now) and any
    balance already paid: paid as taxable salary at the year end or on exit. Bills of the run
    being calculated are only marked paid when it locks (and unmarked on reopen)."""
    paid_claims = sum((d(c.paid_amount) for c in db.query(FbpClaim).filter(
        FbpClaim.company_id == company_id, FbpClaim.employee_id == employee_id, FbpClaim.fy == fy).all()), ZERO)
    bal = accrued_ytd(db, company_id, employee_id, fy, exclude_run_id=run_id) + d(accrued_now) - paid_claims - d(claims_now) \
        - balance_paid_ytd(db, company_id, employee_id, fy, exclude_run_id=run_id)
    return rnd(max(bal, ZERO))


def settlement_lines(db: Session, company_id: int, emp, lwd: date, accrued_now: Decimal) -> list[dict]:
    """On exit: approved bills not yet paid (tax-free per the rules, up to the allocation)
    and the unclaimed balance (taxable)."""
    if not enabled(db, company_id):
        return []
    fy = calc.fy_of(lwd)
    items = payable_items(db, company_id, [emp.id], lwd.replace(day=1), lwd, year_end={emp.id})
    claims_now = sum((x["amount"] for x in items), ZERO)
    out = [{"code": x["code"], "label": f"Flexible benefit bill: {x['note']}", "amount": x["amount"], "taxable": x["taxable"],
            "ref": x["ref"], "fbp": True} for x in items]
    bal = year_end_balance(db, company_id, emp.id, fy, run_id=None, accrued_now=accrued_now, claims_now=claims_now)
    if bal > 0:
        out.append({"code": "FBP_BAL", "label": "Flexible benefits not claimed (taxable)", "amount": bal, "taxable": bal,
                    "ref": None, "fbp": True})
    return out


def settle_claims(db: Session, company_id: int, refs_amounts: list[tuple[str, Decimal]], on: date) -> None:
    for ref, amt in refs_amounts:
        c = db.query(FbpClaim).filter(FbpClaim.company_id == company_id, FbpClaim.uuid == ref).first()
        if c is None:
            continue
        c.paid_amount = d(c.paid_amount) + d(amt)
        c.paid_on = on
        if d(c.paid_amount) >= d(c.approved_amount):
            c.status = "paid"


def bill_file(db: Session, company_id: int, uuid) -> PayrollFile:
    f = db.query(PayrollFile).filter(PayrollFile.company_id == company_id, PayrollFile.uuid == uuid,
                                     PayrollFile.kind == "fbp_bill").first()
    if not f:
        raise NotFound("Bill")
    return f
