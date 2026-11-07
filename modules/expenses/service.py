"""Expense claims, advances and mileage.

A claim has lines (one per bill). On submit every line is checked against the category's
policy (limits by grade and location, receipt rule, monthly limit, duplicates, GSTIN
format) and the flags travel with the approval request. Approved claims are paid with the
next salary (taxable or not per category) or separately by bank transfer. Travel advances
are approved, paid and then settled against claims; what is left is recovered.

Payroll calls payable_claims, mark_paid_in_run, unmark_run, fbp_bills; final settlement
calls pending_for and advance_balance.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session, joinedload

from core import audit
from core.errors import BadRequest, NotFound
from modules.expenses.models import ExpenseAdvance, ExpenseCategory, ExpenseClaim, ExpenseLine
from modules.payroll import calc
from modules.payroll.calc import ZERO, d, money

GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
STATUS = {"draft": "Draft", "pending": "Waiting for approval", "approved": "Approved", "rejected": "Rejected",
          "paid": "Paid", "cancelled": "Withdrawn"}
MAX_RECEIPT = 3_000_000  # characters of data URL (~2 MB file)


# ── Categories ──────────────────────────────────────────────────────

def categories(db: Session, company_id: int, active_only: bool = False) -> list[ExpenseCategory]:
    q = db.query(ExpenseCategory).filter(ExpenseCategory.company_id == company_id)
    if active_only:
        q = q.filter(ExpenseCategory.active.is_(True))
    return q.order_by(ExpenseCategory.sort_order, ExpenseCategory.name).all()


def get_category(db: Session, company_id: int, uuid) -> ExpenseCategory:
    c = db.query(ExpenseCategory).filter(ExpenseCategory.company_id == company_id, ExpenseCategory.uuid == uuid).first()
    if not c:
        raise NotFound("Expense category")
    return c


CAT_FIELDS = ("name", "icon", "description", "kind", "limits", "default_limit", "monthly_limit", "receipt_required_above",
              "mileage_rates", "gst_applicable", "taxable", "pay_via", "component_code", "gl_code", "sort_order", "active")


def save_category(db: Session, company_id: int, data: dict, c: ExpenseCategory | None = None) -> ExpenseCategory:
    if c is None:
        code = (data.get("code") or "").strip().upper()
        if not code:
            raise BadRequest("Give the category a short code.")
        if db.query(ExpenseCategory).filter(ExpenseCategory.company_id == company_id, ExpenseCategory.code == code).first():
            raise BadRequest(f"A category with code {code} already exists.")
        c = ExpenseCategory(company_id=company_id, code=code)
        db.add(c)
    for f in CAT_FIELDS:
        if f in data:
            setattr(c, f, data[f])
    if not (c.name or "").strip():
        raise BadRequest("Give the category a name.")
    if c.kind not in ("amount", "mileage", "per_day") or c.pay_via not in ("payroll", "direct"):
        raise BadRequest("Choose how the category is claimed and paid.")
    if c.kind == "mileage" and not c.mileage_rates:
        raise BadRequest("Set a rate per km for mileage.")
    if not isinstance(c.limits, list):
        raise BadRequest("Limits must be a list.")
    db.flush()
    return c


def category_out(c: ExpenseCategory) -> dict:
    return {"uuid": str(c.uuid), "code": c.code, "name": c.name, "icon": c.icon, "description": c.description, "kind": c.kind,
            "limits": c.limits or [], "default_limit": money(c.default_limit) if c.default_limit is not None else None,
            "monthly_limit": money(c.monthly_limit) if c.monthly_limit is not None else None,
            "receipt_required_above": money(c.receipt_required_above) if c.receipt_required_above is not None else None,
            "mileage_rates": c.mileage_rates or {}, "gst_applicable": c.gst_applicable, "taxable": c.taxable,
            "pay_via": c.pay_via, "component_code": c.component_code, "gl_code": c.gl_code, "sort_order": c.sort_order,
            "active": c.active}


def limit_for(db: Session, c: ExpenseCategory, emp) -> dict:
    """The first limit rule that matches the employee's grade and location, else the defaults."""
    from modules.payroll import adapters
    grade = adapters.grade_code(db, getattr(emp, "grade_id", None))
    for rule in c.limits or []:
        gids, gcodes, lids = rule.get("grade_ids") or [], rule.get("grade_codes") or [], rule.get("location_ids") or []
        if gids and getattr(emp, "grade_id", None) not in gids:
            continue
        if gcodes and grade not in gcodes:
            continue
        if lids and getattr(emp, "location_id", None) not in lids:
            continue
        return {"per_claim": rule.get("per_claim"), "per_day": rule.get("per_day"),
                "per_month": rule.get("per_month", c.monthly_limit)}
    return {"per_claim": c.default_limit if c.kind == "amount" else None,
            "per_day": c.default_limit if c.kind == "per_day" else None, "per_month": c.monthly_limit}


# ── Claims ──────────────────────────────────────────────────────────

def get_claim(db: Session, company_id: int, uuid) -> ExpenseClaim:
    c = (db.query(ExpenseClaim).options(joinedload(ExpenseClaim.lines))
         .filter(ExpenseClaim.company_id == company_id, ExpenseClaim.uuid == uuid).first())
    if not c:
        raise NotFound("Expense claim")
    return c


def _line_amount(cat: ExpenseCategory, ln: dict) -> Decimal:
    if cat.kind == "mileage":
        rate = d((cat.mileage_rates or {}).get(ln.get("vehicle") or "car"))
        if rate <= 0:
            raise BadRequest(f"{cat.name}: choose a vehicle with a rate.")
        km = d(ln.get("distance_km"))
        if km <= 0:
            raise BadRequest(f"{cat.name}: enter the distance in km.")
        return (km * rate).quantize(d("0.01"))
    amt = d(ln.get("amount"))
    if amt <= 0:
        raise BadRequest(f"{cat.name}: enter an amount above zero.")
    return amt


def save_claim(db: Session, company_id: int, emp, data: dict, claim: ExpenseClaim | None = None) -> ExpenseClaim:
    if claim is not None and claim.status not in ("draft", "rejected"):
        raise BadRequest("Only a draft claim can be changed.")
    if claim is None:
        claim = ExpenseClaim(company_id=company_id, employee_id=emp.id, status="draft")
        db.add(claim)
    claim.title = (data.get("title") or "").strip() or "Expense claim"
    if "trip_from" in data:
        claim.trip_from = data.get("trip_from")
    if "trip_to" in data:
        claim.trip_to = data.get("trip_to")
    if data.get("advance_uuid"):
        adv = db.query(ExpenseAdvance).filter(ExpenseAdvance.company_id == company_id, ExpenseAdvance.uuid == data["advance_uuid"],
                                              ExpenseAdvance.employee_id == emp.id).first()
        if not adv or adv.status not in ("paid", "approved"):
            raise BadRequest("Choose an advance that has been paid to you.")
        claim.advance_id = adv.id
    elif "advance_uuid" in data:
        claim.advance_id = None
    lines = data.get("lines")
    if not lines:
        raise BadRequest("Add at least one bill.")
    cats = {str(c.uuid): c for c in categories(db, company_id)}
    for old in list(claim.lines):
        claim.lines.remove(old)
    db.flush()
    total, pay_via = ZERO, set()
    for i, ln in enumerate(lines, start=1):
        cat = cats.get(str(ln.get("category_uuid")))
        if not cat or not cat.active:
            raise BadRequest(f"Bill {i}: choose an expense category.")
        if not ln.get("expense_date"):
            raise BadRequest(f"Bill {i}: enter the date.")
        receipt = ln.get("receipt")
        if receipt and len(receipt) > MAX_RECEIPT:
            raise BadRequest(f"Bill {i}: the receipt file is too large (2 MB at most).")
        amount = _line_amount(cat, ln)
        gstin = (ln.get("gstin") or "").strip().upper() or None
        claim.lines.append(ExpenseLine(
            company_id=company_id, category_id=cat.id, expense_date=ln["expense_date"], description=ln.get("description"),
            merchant=ln.get("merchant"), amount=amount, distance_km=d(ln["distance_km"]) if ln.get("distance_km") else None,
            vehicle=ln.get("vehicle"), from_place=ln.get("from_place"), to_place=ln.get("to_place"), gstin=gstin,
            invoice_number=ln.get("invoice_number"), gst_amount=d(ln["gst_amount"]) if ln.get("gst_amount") else None,
            receipt_name=ln.get("receipt_name"), receipt=receipt))
        total += amount
        pay_via.add(cat.pay_via)
    claim.total = total
    claim.pay_via = "direct" if pay_via == {"direct"} else "payroll"
    db.flush()
    return claim


def policy_check(db: Session, company_id: int, claim: ExpenseClaim, emp) -> list[dict]:
    flags = []
    cats = {c.id: c for c in categories(db, company_id)}
    month_used: dict = {}
    for ln in claim.lines:
        cat = cats[ln.category_id]
        lim = limit_for(db, cat, emp)
        f = []
        cap = lim.get("per_claim") or lim.get("per_day")
        if cap is not None and d(ln.amount) > d(cap):
            f.append(f"Above the {'daily ' if lim.get('per_day') else ''}limit of {calc.inr(cap)}")
        if cat.receipt_required_above is not None and d(ln.amount) > d(cat.receipt_required_above) and not ln.receipt:
            f.append("Receipt missing")
        if ln.gstin and not GSTIN_RE.match(ln.gstin):
            f.append("GSTIN does not look right")
        dup = (db.query(ExpenseLine).join(ExpenseClaim, ExpenseClaim.id == ExpenseLine.claim_id)
               .filter(ExpenseLine.company_id == company_id, ExpenseClaim.employee_id == emp.id, ExpenseLine.id != ln.id,
                       ExpenseClaim.status.in_(("pending", "approved", "paid")), ExpenseLine.category_id == ln.category_id,
                       ExpenseLine.expense_date == ln.expense_date, ExpenseLine.amount == ln.amount).first())
        if dup:
            f.append("Same bill already claimed")
        key = (cat.id, ln.expense_date.year, ln.expense_date.month)
        if key not in month_used:
            used = (db.query(ExpenseLine).join(ExpenseClaim, ExpenseClaim.id == ExpenseLine.claim_id)
                    .filter(ExpenseLine.company_id == company_id, ExpenseClaim.employee_id == emp.id,
                            ExpenseClaim.id != claim.id, ExpenseClaim.status.in_(("pending", "approved", "paid")),
                            ExpenseLine.category_id == cat.id).all())
            month_used[key] = sum((d(x.amount) for x in used if (x.expense_date.year, x.expense_date.month) == key[1:]), ZERO)
        month_used[key] += d(ln.amount)
        if lim.get("per_month") is not None and month_used[key] > d(lim["per_month"]):
            f.append(f"Monthly limit of {calc.inr(lim['per_month'])} crossed")
        ln.flags = f
        for x in f:
            flags.append({"line": str(ln.uuid), "category": cat.name, "message": x})
    return flags


def submit(db: Session, company_id: int, claim: ExpenseClaim, emp) -> ExpenseClaim:
    if claim.status not in ("draft", "rejected"):
        raise BadRequest("This claim has already been sent.")
    claim.policy_flags = policy_check(db, company_id, claim, emp)
    from core import numbering
    from core.timeutil import today_for_company_id
    from core.workflow import service as workflow
    from modules.payroll.service import name_of
    today = today_for_company_id(db, company_id)
    claim.number = claim.number or numbering.next_number(db, company_id, "request", today)
    claim.status = "pending"
    claim.submitted_at = calc_now()
    db.flush()
    inst = workflow.submit(db, company_id, "expense.claim",
                           {"claim_uuid": str(claim.uuid), "title": claim.title, "amount": float(claim.total),
                            "lines": len(claim.lines), "flags": len(claim.policy_flags or []),
                            "flag_text": [f["message"] for f in claim.policy_flags or []][:5],
                            "pay_via": claim.pay_via, "employee_name": name_of(emp)},
                           f"Expense claim: {claim.title} ({calc.inr(claim.total)})", subject_type="expense_claim",
                           subject_id=claim.id)
    claim.workflow_instance_id = inst.id
    db.flush()
    return claim


def calc_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _claim_from(db: Session, instance) -> ExpenseClaim | None:
    return db.query(ExpenseClaim).filter(ExpenseClaim.company_id == instance.company_id,
                                         ExpenseClaim.uuid == (instance.payload or {}).get("claim_uuid")).first()


def on_claim_approved(db: Session, instance) -> None:
    claim = _claim_from(db, instance)
    if not claim or claim.status != "pending":
        return
    cats = {c.id: c for c in categories(db, claim.company_id)}
    approved = taxable = ZERO
    for ln in claim.lines:
        amt = d(ln.approved_amount) if ln.approved_amount is not None else d(ln.amount)
        ln.approved_amount = amt
        approved += amt
        if cats[ln.category_id].taxable:
            taxable += amt
    claim.approved_total, claim.taxable_total = approved, taxable
    claim.status = "approved"
    claim.decided_note = instance.result_note
    if claim.advance_id:
        adv = db.query(ExpenseAdvance).filter(ExpenseAdvance.id == claim.advance_id).first()
        if adv:
            left = d(adv.amount) - d(adv.settled_amount) - d(adv.recovered_amount)
            use = min(left, approved)
            claim.advance_adjusted = use
            adv.settled_amount = d(adv.settled_amount) + use
            if d(adv.settled_amount) + d(adv.recovered_amount) >= d(adv.amount):
                adv.status = "settled"
            if approved - use <= 0:
                claim.status = "paid"
                claim.paid_on = date.today()
                claim.payment_ref = "Settled against advance"


def on_claim_closed(db: Session, instance) -> None:
    claim = _claim_from(db, instance)
    if claim and claim.status == "pending":
        claim.status = "rejected" if instance.status == "rejected" else "cancelled"
        claim.decided_note = instance.result_note


def adjust_line(db: Session, company_id: int, claim: ExpenseClaim, line_uuid, amount, note: str | None) -> None:
    """Approvers (HR) may approve less than claimed on a line while the claim is pending."""
    if claim.status != "pending":
        raise BadRequest("Only a pending claim can be adjusted.")
    ln = next((x for x in claim.lines if str(x.uuid) == str(line_uuid)), None)
    if not ln:
        raise NotFound("Bill")
    amount = d(amount)
    if amount < 0 or amount > d(ln.amount):
        raise BadRequest("Approve between zero and the amount claimed.")
    ln.approved_amount = amount
    ln.flags = list(ln.flags or []) + ([f"Approved {calc.inr(amount)}: {note}"] if note else [])


def mark_paid_direct(db: Session, company_id: int, claim: ExpenseClaim, on: date, reference: str | None) -> None:
    if claim.status != "approved":
        raise BadRequest("Only an approved claim can be marked paid.")
    claim.status, claim.paid_on, claim.payment_ref = "paid", on, reference
    audit.event(db, "expense.paid", "expense_claims", claim.id, {"reference": reference}, entity_uuid=str(claim.uuid))


def payable_claims(db: Session, company_id: int, employee_ids: list[int], until: date) -> list[dict]:
    rows = (db.query(ExpenseClaim).filter(ExpenseClaim.company_id == company_id, ExpenseClaim.employee_id.in_(employee_ids or [-1]),
                                          ExpenseClaim.status == "approved", ExpenseClaim.pay_via == "payroll",
                                          ExpenseClaim.pay_run_id.is_(None)).all())
    out = []
    for c in rows:
        amount = d(c.approved_total) - d(c.advance_adjusted)
        if amount <= 0:
            continue
        taxable = min(d(c.taxable_total), amount)
        out.append({"employee_id": c.employee_id, "amount": amount, "taxable": taxable, "ref": str(c.uuid),
                    "note": f"{c.number or ''} {c.title}".strip()})
    return out


def mark_paid_in_run(db: Session, company_id: int, refs: list[str], run_id: int, on: date) -> None:
    for c in db.query(ExpenseClaim).filter(ExpenseClaim.company_id == company_id, ExpenseClaim.uuid.in_(refs or ["00000000-0000-0000-0000-000000000000"])).all():
        c.status, c.pay_run_id, c.paid_on, c.payment_ref = "paid", run_id, on, "Paid with salary"


def unmark_run(db: Session, company_id: int, run_id: int) -> None:
    for c in db.query(ExpenseClaim).filter(ExpenseClaim.company_id == company_id, ExpenseClaim.pay_run_id == run_id).all():
        c.status, c.pay_run_id, c.paid_on, c.payment_ref = "approved", None, None, None


def fbp_bills(db: Session, company_id: int, employee_id: int, fy: str) -> Decimal:
    """Approved bills in the year for categories linked to flexible-benefit components."""
    from modules.payroll.models import SalaryComponent
    fbp_codes = {c.code for c in db.query(SalaryComponent).filter(SalaryComponent.company_id == company_id,
                                                                    SalaryComponent.fbp.is_(True)).all()}
    if not fbp_codes:
        return ZERO
    a, b = calc.fy_bounds(fy)
    rows = (db.query(ExpenseLine, ExpenseCategory).join(ExpenseClaim, ExpenseClaim.id == ExpenseLine.claim_id)
            .join(ExpenseCategory, ExpenseCategory.id == ExpenseLine.category_id)
            .filter(ExpenseLine.company_id == company_id, ExpenseClaim.employee_id == employee_id,
                    ExpenseClaim.status.in_(("approved", "paid")), ExpenseCategory.component_code.in_(fbp_codes),
                    ExpenseLine.expense_date >= a, ExpenseLine.expense_date <= b).all())
    return sum((d(ln.approved_amount if ln.approved_amount is not None else ln.amount) for ln, _ in rows), ZERO)


def pending_for(db: Session, company_id: int, employee_id: int) -> list[dict]:
    """Approved claims not paid yet (final settlement pays them)."""
    rows = db.query(ExpenseClaim).filter(ExpenseClaim.company_id == company_id, ExpenseClaim.employee_id == employee_id,
                                         ExpenseClaim.status == "approved").all()
    return [{"ref": str(c.uuid), "label": f"{c.number or ''} {c.title}".strip(),
             "amount": d(c.approved_total) - d(c.advance_adjusted), "taxable": d(c.taxable_total)} for c in rows]


def mark_settled(db: Session, company_id: int, refs: list[str], on: date, ref: str) -> None:
    for c in db.query(ExpenseClaim).filter(ExpenseClaim.company_id == company_id, ExpenseClaim.uuid.in_(refs or ["00000000-0000-0000-0000-000000000000"])).all():
        c.status, c.paid_on, c.payment_ref = "paid", on, f"Final settlement {ref}"


def claim_out(c: ExpenseClaim, names: dict | None = None, cats: dict | None = None, full: bool = False) -> dict:
    out = {"uuid": str(c.uuid), "number": c.number, "employee_id": c.employee_id, "employee_name": (names or {}).get(c.employee_id),
           "title": c.title, "trip_from": c.trip_from.isoformat() if c.trip_from else None,
           "trip_to": c.trip_to.isoformat() if c.trip_to else None, "status": c.status, "status_label": STATUS.get(c.status, c.status),
           "total": money(c.total), "approved_total": money(c.approved_total), "taxable_total": money(c.taxable_total),
           "pay_via": c.pay_via, "advance_adjusted": money(c.advance_adjusted), "policy_flags": c.policy_flags or [],
           "paid_on": c.paid_on.isoformat() if c.paid_on else None, "payment_ref": c.payment_ref,
           "submitted_at": c.submitted_at.isoformat() if c.submitted_at else None, "decided_note": c.decided_note,
           "lines_count": len(c.lines), "created_at": c.created_at.isoformat() if c.created_at else None}
    if full:
        out["lines"] = [{"uuid": str(x.uuid), "category_uuid": str(cats[x.category_id].uuid) if cats and x.category_id in cats else None,
                         "category_name": cats[x.category_id].name if cats and x.category_id in cats else None,
                         "expense_date": x.expense_date.isoformat(), "description": x.description, "merchant": x.merchant,
                         "amount": money(x.amount), "approved_amount": money(x.approved_amount) if x.approved_amount is not None else None,
                         "distance_km": str(x.distance_km) if x.distance_km is not None else None, "vehicle": x.vehicle,
                         "from_place": x.from_place, "to_place": x.to_place, "gstin": x.gstin, "invoice_number": x.invoice_number,
                         "gst_amount": money(x.gst_amount) if x.gst_amount is not None else None, "receipt_name": x.receipt_name,
                         "has_receipt": bool(x.receipt), "flags": x.flags or []} for x in c.lines]
    return out


# ── Advances ────────────────────────────────────────────────────────

def request_advance(db: Session, company_id: int, emp, data: dict) -> ExpenseAdvance:
    amount = d(data.get("amount"))
    if amount <= 0 or not (data.get("purpose") or "").strip():
        raise BadRequest("Enter the amount and what it is for.")
    from core import numbering
    from core.timeutil import today_for_company_id
    from core.workflow import service as workflow
    from modules.payroll.service import name_of
    adv = ExpenseAdvance(company_id=company_id, employee_id=emp.id, purpose=data["purpose"].strip(), amount=amount,
                         needed_by=data.get("needed_by"), status="pending")
    adv.number = numbering.next_number(db, company_id, "request", today_for_company_id(db, company_id))
    db.add(adv)
    db.flush()
    inst = workflow.submit(db, company_id, "expense.advance",
                           {"advance_uuid": str(adv.uuid), "amount": float(amount), "purpose": adv.purpose,
                            "employee_name": name_of(emp)},
                           f"Travel advance of {calc.inr(amount)}", subject_type="expense_advance", subject_id=adv.id)
    adv.workflow_instance_id = inst.id
    db.flush()
    return adv


def _adv_from(db: Session, instance) -> ExpenseAdvance | None:
    return db.query(ExpenseAdvance).filter(ExpenseAdvance.company_id == instance.company_id,
                                           ExpenseAdvance.uuid == (instance.payload or {}).get("advance_uuid")).first()


def on_advance_approved(db: Session, instance) -> None:
    adv = _adv_from(db, instance)
    if adv and adv.status == "pending":
        adv.status = "approved"


def on_advance_closed(db: Session, instance) -> None:
    adv = _adv_from(db, instance)
    if adv and adv.status == "pending":
        adv.status = "rejected" if instance.status == "rejected" else "cancelled"


def get_advance(db: Session, company_id: int, uuid) -> ExpenseAdvance:
    a = db.query(ExpenseAdvance).filter(ExpenseAdvance.company_id == company_id, ExpenseAdvance.uuid == uuid).first()
    if not a:
        raise NotFound("Advance")
    return a


def pay_advance(db: Session, company_id: int, adv: ExpenseAdvance, on: date, reference: str | None) -> None:
    if adv.status != "approved":
        raise BadRequest("Only an approved advance can be paid.")
    adv.status, adv.paid_on, adv.payment_ref = "paid", on, reference
    audit.event(db, "expense.advance_paid", "expense_advances", adv.id, {"reference": reference}, entity_uuid=str(adv.uuid))


def advance_balance(db: Session, company_id: int, employee_id: int) -> list[dict]:
    rows = db.query(ExpenseAdvance).filter(ExpenseAdvance.company_id == company_id, ExpenseAdvance.employee_id == employee_id,
                                           ExpenseAdvance.status == "paid").all()
    out = []
    for a in rows:
        left = d(a.amount) - d(a.settled_amount) - d(a.recovered_amount)
        if left > 0:
            out.append({"ref": str(a.uuid), "label": f"{a.number or ''} {a.purpose}".strip(), "amount": left})
    return out


def recover_advances(db: Session, company_id: int, refs: list[str]) -> None:
    for a in db.query(ExpenseAdvance).filter(ExpenseAdvance.company_id == company_id, ExpenseAdvance.uuid.in_(refs or ["00000000-0000-0000-0000-000000000000"])).all():
        a.recovered_amount = d(a.amount) - d(a.settled_amount)
        a.status = "settled"


def advance_out(a: ExpenseAdvance, names: dict | None = None) -> dict:
    return {"uuid": str(a.uuid), "number": a.number, "employee_id": a.employee_id, "employee_name": (names or {}).get(a.employee_id),
            "purpose": a.purpose, "amount": money(a.amount), "needed_by": a.needed_by.isoformat() if a.needed_by else None,
            "status": a.status, "settled_amount": money(a.settled_amount), "recovered_amount": money(a.recovered_amount),
            "balance": money(d(a.amount) - d(a.settled_amount) - d(a.recovered_amount)),
            "paid_on": a.paid_on.isoformat() if a.paid_on else None, "payment_ref": a.payment_ref,
            "created_at": a.created_at.isoformat() if a.created_at else None}


# ── Used by other modules (travel) ──────────────────────────────────

def create_advance(db: Session, company_id: int, employee_id: int, amount, purpose: str, needed_by: date | None,
                   number: str | None = None) -> ExpenseAdvance:
    """An advance that was already approved elsewhere (e.g. with a travel request): it goes
    straight to finance to pay, then is settled against the claim like any other advance."""
    adv = ExpenseAdvance(company_id=company_id, employee_id=employee_id, purpose=purpose[:300], amount=d(amount),
                         needed_by=needed_by, status="approved", number=number)
    db.add(adv)
    db.flush()
    return adv


def advance_by_id(db: Session, company_id: int, advance_id: int | None) -> ExpenseAdvance | None:
    if not advance_id:
        return None
    return db.query(ExpenseAdvance).filter(ExpenseAdvance.company_id == company_id, ExpenseAdvance.id == advance_id).first()


def cancel_advance(db: Session, company_id: int, adv: ExpenseAdvance) -> None:
    if adv.status in ("paid", "settled"):
        raise BadRequest("This advance has already been paid; settle it with a claim instead.")
    if adv.status in ("pending", "approved"):
        adv.status = "cancelled"


def claim_by_id(db: Session, company_id: int, claim_id: int | None) -> ExpenseClaim | None:
    if not claim_id:
        return None
    return db.query(ExpenseClaim).filter(ExpenseClaim.company_id == company_id, ExpenseClaim.id == claim_id).first()


def claims_by_ids(db: Session, company_id: int, ids) -> dict[int, ExpenseClaim]:
    ids = [i for i in ids if i]
    if not ids:
        return {}
    return {c.id: c for c in db.query(ExpenseClaim).filter(ExpenseClaim.company_id == company_id, ExpenseClaim.id.in_(ids)).all()}


def advances_by_ids(db: Session, company_id: int, ids) -> dict[int, ExpenseAdvance]:
    ids = [i for i in ids if i]
    if not ids:
        return {}
    return {a.id: a for a in db.query(ExpenseAdvance).filter(ExpenseAdvance.company_id == company_id, ExpenseAdvance.id.in_(ids)).all()}
