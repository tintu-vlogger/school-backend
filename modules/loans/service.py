"""Loans and salary advances: types, requests through the workflow engine, the
instalment plan, deduction in payroll, skip and prepayment (with approval), the taxable
perquisite on concessional loans and recovery of the balance in the final settlement.

Payroll calls:
    due_instalments(db, company_id, employee_ids, month) -> [{employee_id, code, amount, ref, note}]
    mark_deducted(db, company_id, refs, run_id, on) / unmark_run(db, company_id, run_id)
    perquisite_value(db, company_id, employee_id, on) -> Decimal (monthly)
    outstanding_for(db, company_id, employee_id) -> [{loan, balance}]
    recover_in_settlement(db, company_id, employee_id, settlement_ref, on)
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session, joinedload

from core import audit, expressions
from core.errors import BadRequest, NotFound
from modules.loans.models import Loan, LoanInstalment, LoanType
from modules.payroll import calc
from modules.payroll.calc import ZERO, d, money

STATUS = {"pending": "Waiting for approval", "approved": "Approved, to be paid out", "active": "Running",
          "closed": "Closed", "rejected": "Rejected", "cancelled": "Withdrawn"}


def _emp(db: Session, company_id: int, employee_id: int):
    from modules.payroll.service import get_employee
    return get_employee(db, company_id, employee_id)


def types(db: Session, company_id: int, active_only: bool = False) -> list[LoanType]:
    q = db.query(LoanType).filter(LoanType.company_id == company_id)
    if active_only:
        q = q.filter(LoanType.active.is_(True))
    return q.order_by(LoanType.name).all()


def get_type(db: Session, company_id: int, uuid) -> LoanType:
    t = db.query(LoanType).filter(LoanType.company_id == company_id, LoanType.uuid == uuid).first()
    if not t:
        raise NotFound("Loan type")
    return t


TYPE_FIELDS = ("name", "kind", "description", "max_amount", "max_salary_multiple", "max_instalments", "interest_rate",
               "interest_method", "min_service_months", "eligibility", "max_active", "medical", "active")


def save_type(db: Session, company_id: int, data: dict, t: LoanType | None = None) -> LoanType:
    if t is None:
        code = (data.get("code") or "").strip().upper()
        if not code:
            raise BadRequest("Give the loan type a short code.")
        if db.query(LoanType).filter(LoanType.company_id == company_id, LoanType.code == code).first():
            raise BadRequest(f"A loan type with code {code} already exists.")
        t = LoanType(company_id=company_id, code=code)
        db.add(t)
    for f in TYPE_FIELDS:
        if f in data:
            setattr(t, f, data[f])
    if not (t.name or "").strip():
        raise BadRequest("Give the loan type a name.")
    if t.kind not in ("loan", "advance") or t.interest_method not in ("reducing", "flat"):
        raise BadRequest("Choose loan or advance, and reducing or flat interest.")
    if int(t.max_instalments or 0) < 1:
        raise BadRequest("Allow at least one instalment.")
    if t.eligibility:
        try:
            expressions.validate(t.eligibility, set(ELIGIBILITY_FIELDS))
        except expressions.ExpressionError as e:
            raise BadRequest(f"Eligibility: {e}")
    db.flush()
    return t


ELIGIBILITY_FIELDS = {"employee.service_months": "Months of service", "employee.monthly_gross": "Monthly gross pay",
                      "employee.grade": "Grade code", "employee.confirmed": "Confirmed (true/false)",
                      "employee.active_loans": "Loans running now", "request.amount": "Amount asked for",
                      "request.instalments": "Instalments asked for"}


def type_out(t: LoanType) -> dict:
    return {"uuid": str(t.uuid), "code": t.code, "name": t.name, "kind": t.kind, "description": t.description,
            "max_amount": money(t.max_amount) if t.max_amount is not None else None,
            "max_salary_multiple": str(t.max_salary_multiple) if t.max_salary_multiple is not None else None,
            "max_instalments": t.max_instalments, "interest_rate": str(t.interest_rate), "interest_method": t.interest_method,
            "min_service_months": t.min_service_months, "eligibility": t.eligibility, "max_active": t.max_active,
            "medical": t.medical, "active": t.active}


def check_eligibility(db: Session, company_id: int, emp, t: LoanType, amount: Decimal, instalments: int, on: date) -> list[str]:
    from modules.payroll import adapters
    from modules.payroll.service import monthly_gross
    problems = []
    months = 0
    if emp.date_of_joining:
        y, m, _ = calc.service_length(emp.date_of_joining, on)
        months = y * 12 + m
    gross = monthly_gross(db, company_id, emp.id, on)
    active = db.query(Loan).filter(Loan.company_id == company_id, Loan.employee_id == emp.id, Loan.loan_type_id == t.id,
                                   Loan.status.in_(("pending", "approved", "active"))).count()
    if months < (t.min_service_months or 0):
        problems.append(f"Needs {t.min_service_months} months of service ({months} so far).")
    if t.max_amount is not None and amount > d(t.max_amount):
        problems.append(f"The most you can borrow is {calc.inr(t.max_amount)}.")
    if t.max_salary_multiple is not None and gross > 0 and amount > gross * d(t.max_salary_multiple):
        problems.append(f"The most is {t.max_salary_multiple} × monthly gross ({calc.inr(gross * d(t.max_salary_multiple))}).")
    if instalments > t.max_instalments:
        problems.append(f"At most {t.max_instalments} instalments.")
    if active >= (t.max_active or 1):
        problems.append("You already have a loan of this type running or waiting.")
    if t.eligibility:
        vars_ = {"employee": {"service_months": months, "monthly_gross": gross, "grade": adapters.grade_code(db, emp.grade_id),
                              "confirmed": emp.status == "confirmed", "active_loans": active},
                 "request": {"amount": amount, "instalments": instalments}}
        try:
            if not expressions.check_condition(t.eligibility, vars_, today=on):
                problems.append("You do not meet the eligibility rule for this loan.")
        except expressions.ExpressionError:
            problems.append("The eligibility rule has a mistake; ask HR.")
    return problems


def preview(principal, rate, instalments: int, start: date, method: str) -> dict:
    rows = calc.emi_schedule(principal, rate, instalments, start, method)
    total_interest = sum((r["interest"] for r in rows), ZERO)
    return {"emi": money(rows[0]["emi"]) if rows else "0.00", "total_interest": money(total_interest),
            "total_payable": money(d(principal) + total_interest),
            "schedule": [{"seq": r["seq"], "month": r["month"].isoformat(), "principal": money(r["principal"]),
                          "interest": money(r["interest"]), "emi": money(r["emi"]), "balance": money(r["balance"])} for r in rows]}


def request_loan(db: Session, company_id: int, emp, t: LoanType, data: dict, on: date, *, by_hr: bool = False) -> Loan:
    amount = d(data.get("amount"))
    n = int(data.get("instalments") or 0)
    if amount <= 0 or n <= 0:
        raise BadRequest("Enter the amount and the number of instalments.")
    start = (data.get("start_month") or calc.add_months(on.replace(day=1), 1)).replace(day=1)
    problems = check_eligibility(db, company_id, emp, t, amount, n, on)
    if problems and not (by_hr and data.get("override_eligibility")):
        raise BadRequest(" ".join(problems))
    rate = d(data["interest_rate"]) if by_hr and data.get("interest_rate") is not None else d(t.interest_rate)
    loan = Loan(company_id=company_id, employee_id=emp.id, loan_type_id=t.id, purpose=data.get("purpose"), principal=amount,
                interest_rate=rate, interest_method=t.interest_method, instalments=n, start_month=start, status="pending",
                outstanding=amount, note=data.get("note"))
    from core import numbering
    loan.number = numbering.next_number(db, company_id, "request", on)
    loan.emi = d(preview(amount, rate, n, start, t.interest_method)["emi"])
    db.add(loan)
    db.flush()
    from core.workflow import service as workflow
    from modules.payroll.service import name_of
    inst = workflow.submit(db, company_id, "loan.request",
                           {"loan_uuid": str(loan.uuid), "loan_type": t.code, "loan_type_name": t.name,
                            "amount": float(amount), "instalments": n, "emi": float(loan.emi), "interest_rate": float(rate),
                            "employee_name": name_of(emp), "purpose": loan.purpose, "kind": t.kind},
                           f"{t.name} of {calc.inr(amount)} for {name_of(emp)}", subject_type="loan", subject_id=loan.id)
    loan.workflow_instance_id = inst.id
    db.flush()
    return loan


def get_loan(db: Session, company_id: int, uuid) -> Loan:
    loan = (db.query(Loan).options(joinedload(Loan.instalments_rows))
            .filter(Loan.company_id == company_id, Loan.uuid == uuid).first())
    if not loan:
        raise NotFound("Loan")
    return loan


def _by_payload(db: Session, instance) -> Loan | None:
    return db.query(Loan).filter(Loan.company_id == instance.company_id,
                                 Loan.uuid == (instance.payload or {}).get("loan_uuid")).first()


def on_request_approved(db: Session, instance) -> None:
    loan = _by_payload(db, instance)
    if loan and loan.status == "pending":
        loan.status = "approved"
        build_schedule(db, loan)


def on_request_closed(db: Session, instance) -> None:
    loan = _by_payload(db, instance)
    if loan and loan.status == "pending":
        loan.status = "rejected" if instance.status == "rejected" else "cancelled"


def build_schedule(db: Session, loan: Loan) -> None:
    for r in list(loan.instalments_rows):
        if r.status == "due":
            db.delete(r)
    db.flush()
    done = [r for r in loan.instalments_rows if r.status != "due"]
    paid_principal = sum((d(r.principal) for r in done), ZERO)
    left = d(loan.principal) - paid_principal
    n = loan.instalments - len(done)
    start = loan.start_month if not done else calc.add_months(max(r.due_month for r in done), 1)
    for r in calc.emi_schedule(left, loan.interest_rate, n, start, loan.interest_method):
        db.add(LoanInstalment(company_id=loan.company_id, loan_id=loan.id, seq=len(done) + r["seq"], due_month=r["month"],
                              principal=r["principal"], interest=r["interest"], emi=r["emi"], status="due"))
    db.flush()
    db.refresh(loan)


def disburse(db: Session, company_id: int, loan: Loan, on: date, reference: str | None) -> Loan:
    if loan.status != "approved":
        raise BadRequest("Only an approved loan can be paid out.")
    loan.status, loan.disbursed_on, loan.disbursal_ref = "active", on, reference
    if not loan.instalments_rows:
        build_schedule(db, loan)
    audit.event(db, "loan.disbursed", "loans", loan.id, {"amount": money(loan.principal), "reference": reference},
                entity_uuid=str(loan.uuid))
    db.flush()
    return loan


def due_instalments(db: Session, company_id: int, employee_ids: list[int], month: date) -> list[dict]:
    m = month.replace(day=1)
    rows = (db.query(LoanInstalment, Loan, LoanType).join(Loan, Loan.id == LoanInstalment.loan_id)
            .join(LoanType, LoanType.id == Loan.loan_type_id)
            .filter(LoanInstalment.company_id == company_id, Loan.employee_id.in_(employee_ids or [-1]),
                    Loan.status == "active", LoanInstalment.status == "due", LoanInstalment.due_month <= m).all())
    return [{"employee_id": loan.employee_id, "code": "ADVANCE" if t.kind == "advance" else "LOAN", "amount": d(i.emi),
             "ref": str(i.uuid), "note": f"{t.name} {loan.number or ''} instalment {i.seq}/{loan.instalments}".replace("  ", " ")}
            for i, loan, t in rows]


def _recalc_outstanding(db: Session, loan: Loan) -> None:
    db.flush()
    db.refresh(loan)
    paid = sum((d(r.principal) for r in loan.instalments_rows if r.status in ("deducted", "prepaid", "recovered", "waived")), ZERO)
    loan.outstanding = max(d(loan.principal) - paid, ZERO)
    if loan.outstanding == 0 and loan.status == "active":
        loan.status, loan.closed_on = "closed", max((r.settled_on for r in loan.instalments_rows if r.settled_on), default=None)


def mark_deducted(db: Session, company_id: int, refs: list[str], run_id: int, on: date) -> None:
    if not refs:
        return
    rows = db.query(LoanInstalment).filter(LoanInstalment.company_id == company_id, LoanInstalment.uuid.in_(refs)).all()
    for r in rows:
        r.status, r.pay_run_id, r.settled_on = "deducted", run_id, on
    for loan_id in {r.loan_id for r in rows}:
        _recalc_outstanding(db, db.query(Loan).filter(Loan.id == loan_id).first())


def unmark_run(db: Session, company_id: int, run_id: int) -> None:
    rows = db.query(LoanInstalment).filter(LoanInstalment.company_id == company_id, LoanInstalment.pay_run_id == run_id).all()
    for r in rows:
        r.status, r.pay_run_id, r.settled_on = "due", None, None
    for loan_id in {r.loan_id for r in rows}:
        loan = db.query(Loan).filter(Loan.id == loan_id).first()
        if loan.status == "closed":
            loan.status, loan.closed_on = "active", None
        _recalc_outstanding(db, loan)


def request_action(db: Session, company_id: int, loan: Loan, kind: str, data: dict) -> Loan:
    """Skip next instalment(s) or prepay (part or all) — both go through approval."""
    if loan.status != "active":
        raise BadRequest("Only a running loan can be changed.")
    if loan.pending_action:
        raise BadRequest("A change to this loan is already waiting for approval.")
    from core.workflow import service as workflow
    if kind == "skip":
        months = int(data.get("months") or 1)
        due = [r for r in loan.instalments_rows if r.status == "due"]
        if not due:
            raise BadRequest("Nothing left to skip.")
        payload = {"loan_uuid": str(loan.uuid), "kind": "skip", "months": months, "reason": data.get("reason"),
                   "from_month": due[0].due_month.isoformat()}
        title = f"Skip {months} loan instalment(s)"
    elif kind == "prepay":
        amount = d(data.get("amount") or loan.outstanding)
        if amount <= 0 or amount > d(loan.outstanding):
            raise BadRequest(f"Prepay between ₹1 and the balance of {calc.inr(loan.outstanding)}.")
        payload = {"loan_uuid": str(loan.uuid), "kind": "prepay", "amount": float(amount), "reason": data.get("reason"),
                   "reference": data.get("reference")}
        title = f"Prepay {calc.inr(amount)} of a loan"
    else:
        raise BadRequest("Choose skip or prepay.")
    inst = workflow.submit(db, company_id, "loan.change", payload, title, subject_type="loan", subject_id=loan.id)
    if inst.status == "pending":
        loan.pending_action = {**payload, "instance_id": inst.id}
    db.flush()
    return loan


def on_change_approved(db: Session, instance) -> None:
    loan = _by_payload(db, instance)
    if not loan:
        return
    p = instance.payload or {}
    loan.pending_action = None
    today = instance.completed_at.date() if instance.completed_at else date.today()
    if p.get("kind") == "skip":
        months = int(p.get("months") or 1)
        due = [r for r in loan.instalments_rows if r.status == "due"]
        for r in due[:months]:
            r.status, r.note = "skipped", p.get("reason") or "Skipped with approval"
        loan.instalments += min(months, len(due))
        build_schedule(db, loan)
    elif p.get("kind") == "prepay":
        amount = d(p.get("amount"))
        due = [r for r in loan.instalments_rows if r.status == "due"]
        if amount >= d(loan.outstanding):
            for r in due:
                r.status, r.settled_on, r.note = "prepaid", today, p.get("reference") or "Closed early"
        else:
            db.add(LoanInstalment(company_id=loan.company_id, loan_id=loan.id, seq=0, due_month=today.replace(day=1),
                                  principal=amount, interest=ZERO, emi=amount, status="prepaid", settled_on=today,
                                  note=p.get("reference") or "Part prepayment"))
            db.flush()
            db.refresh(loan)
            build_schedule_after_prepay(db, loan, amount)
    _recalc_outstanding(db, loan)


def build_schedule_after_prepay(db: Session, loan: Loan, amount: Decimal) -> None:
    due = [r for r in loan.instalments_rows if r.status == "due"]
    if not due:
        return
    start = due[0].due_month
    done_principal = sum((d(r.principal) for r in loan.instalments_rows if r.status != "due"), ZERO)
    left = max(d(loan.principal) - done_principal, ZERO)
    n = len(due)
    for r in due:
        db.delete(r)
    db.flush()
    base_seq = max((r.seq for r in loan.instalments_rows if r.status != "due"), default=0)
    for r in calc.emi_schedule(left, loan.interest_rate, n, start, loan.interest_method):
        db.add(LoanInstalment(company_id=loan.company_id, loan_id=loan.id, seq=base_seq + r["seq"], due_month=r["month"],
                              principal=r["principal"], interest=r["interest"], emi=r["emi"], status="due"))
    db.flush()
    db.refresh(loan)


def on_change_closed(db: Session, instance) -> None:
    loan = _by_payload(db, instance)
    if loan:
        loan.pending_action = None


def perquisite_value(db: Session, company_id: int, employee_id: int, on: date) -> Decimal:
    """Monthly taxable value of concessional loans (none when all loans together stay within
    the exempt limit, or for medical loans)."""
    from modules.payroll.statutory import rule
    rules = (rule(db, company_id, "perquisites", None, on) or {}).get("loan") or {}
    if not rules:
        return ZERO
    loans = (db.query(Loan, LoanType).join(LoanType, LoanType.id == Loan.loan_type_id)
             .filter(Loan.company_id == company_id, Loan.employee_id == employee_id, Loan.status == "active").all())
    aggregate = sum((d(x.outstanding) for x, _ in loans), ZERO)
    return sum((calc.loan_perquisite(x.outstanding, x.interest_rate, rules, aggregate_outstanding=aggregate, medical=t.medical)
                for x, t in loans), ZERO)


def outstanding_for(db: Session, company_id: int, employee_id: int) -> list[dict]:
    rows = (db.query(Loan, LoanType).join(LoanType, LoanType.id == Loan.loan_type_id)
            .filter(Loan.company_id == company_id, Loan.employee_id == employee_id, Loan.status.in_(("active", "approved"))).all())
    return [{"uuid": str(x.uuid), "number": x.number, "name": t.name, "kind": t.kind,
             "balance": d(x.outstanding) if x.status == "active" else ZERO} for x, t in rows if d(x.outstanding) > 0]


def recover_in_settlement(db: Session, company_id: int, employee_id: int, settlement_ref: str, on: date) -> None:
    for x in db.query(Loan).filter(Loan.company_id == company_id, Loan.employee_id == employee_id, Loan.status == "active").all():
        for r in x.instalments_rows:
            if r.status == "due":
                r.status, r.settled_on, r.note = "recovered", on, f"Recovered in final settlement {settlement_ref}"
        _recalc_outstanding(db, x)
        x.outstanding = ZERO
        x.status, x.closed_on = "closed", on


def loan_out(db: Session, x: Loan, t: LoanType | None = None, names: dict | None = None, full: bool = False) -> dict:
    t = t or db.query(LoanType).filter(LoanType.id == x.loan_type_id).first()
    out = {"uuid": str(x.uuid), "number": x.number, "employee_id": x.employee_id,
           "employee_name": (names or {}).get(x.employee_id), "loan_type": t.name if t else None,
           "loan_type_code": t.code if t else None, "kind": t.kind if t else None, "purpose": x.purpose,
           "principal": money(x.principal), "interest_rate": str(x.interest_rate), "interest_method": x.interest_method,
           "instalments": x.instalments, "emi": money(x.emi), "start_month": x.start_month.isoformat(), "status": x.status,
           "status_label": STATUS.get(x.status, x.status), "outstanding": money(x.outstanding),
           "disbursed_on": x.disbursed_on.isoformat() if x.disbursed_on else None, "disbursal_ref": x.disbursal_ref,
           "closed_on": x.closed_on.isoformat() if x.closed_on else None, "pending_action": x.pending_action,
           "note": x.note, "created_at": x.created_at.isoformat() if x.created_at else None}
    if full:
        out["schedule"] = [{"uuid": str(r.uuid), "seq": r.seq, "due_month": r.due_month.isoformat(), "principal": money(r.principal),
                            "interest": money(r.interest), "emi": money(r.emi), "status": r.status,
                            "settled_on": r.settled_on.isoformat() if r.settled_on else None, "note": r.note}
                           for r in sorted(x.instalments_rows, key=lambda r: (r.due_month, r.seq))]
    return out
