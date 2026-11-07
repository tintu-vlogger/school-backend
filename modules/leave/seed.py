"""Company setup and development demo data for the leave module (called by seed/seed_data.py).

setup_company(db, company_id)  Indian default leave types, work weeks and holiday calendars;
                               then credits accruals due up to today and expires old credits
                               (both idempotent, so every start keeps balances current).
seed_demo(db, company_id)      the demo company: opening balances and a realistic mix of
                               requests (approved, pending, rejected, half days, comp off,
                               maternity) for employees that have no leave yet.
"""
from __future__ import annotations

import logging
import random
from datetime import date, timedelta

from sqlalchemy.orm import Session

from core.context import RequestContext, use_context
from core.timeutil import today_for_company_id

log = logging.getLogger("hrms")


def setup_company(db: Session, company_id: int) -> None:
    from modules.leave import ledger
    from modules.leave.defaults import ensure_leave_defaults
    ensure_leave_defaults(db, company_id)
    db.flush()
    today = today_for_company_id(db, company_id)
    try:
        with db.begin_nested():
            ledger.run_expiry(db, company_id, today, record=False)
            res = ledger.run_accruals(db, company_id, today, record=False)
            if res["entries"]:
                from modules.leave.models import LeaveRun
                db.add(LeaveRun(company_id=company_id, kind="accrual", label=f"Accrual up to {today:%d %b %Y}",
                                as_of=today, entries=res["entries"], result=res, ran_by_name="System"))
    except Exception as exc:  # balances catch up on the next run; never block start-up
        log.warning("Leave accrual at start-up failed for company %s: %s", company_id, exc)


def _ctx(company_id: int, emp=None, owner: bool = False) -> RequestContext:
    if owner or emp is None:
        return RequestContext(company_id=company_id, actor_type="owner", actor_name="Company owner", role="admin")
    return RequestContext(company_id=company_id, employee_id=emp.id, actor_type="employee", role=emp.role,
                          actor_name=f"{emp.first_name} {emp.last_name}".strip())


def _working(cal, emp, d: date, step: int = 1) -> date:
    for _ in range(14):
        if cal.day(emp, d)["kind"] == "working":
            return d
        d += timedelta(days=step)
    return d


def seed_demo(db: Session, company_id: int) -> None:
    from core.workflow import service as workflow
    from core.workflow.models import WorkflowTask
    from modules.employees.models import Employee
    from modules.leave import ledger, policy, service
    from modules.leave.calendar import WorkCalendar
    from modules.leave.models import LeaveRequest, LeaveType

    db.info["company_id"] = company_id
    types = {t.code: t for t in db.query(LeaveType).filter(LeaveType.company_id == company_id).all()}
    if not {"EL", "CL", "SL"} <= set(types):
        return
    emps = (db.query(Employee).filter(Employee.company_id == company_id, Employee.active.is_(True))
            .order_by(Employee.id).all())
    have = {i for (i,) in db.query(LeaveRequest.employee_id).filter(LeaveRequest.company_id == company_id).distinct()}
    todo = [e for e in emps if e.id not in have]
    if not todo:
        return
    today = today_for_company_id(db, company_id)
    cal = WorkCalendar(db, company_id)
    year = today.year

    # opening balances brought over from an earlier system
    for i, e in enumerate(todo):
        for code, days in (("EL", 6 + (e.id % 5) * 1.5), ("SL", 2 + e.id % 3)):
            t = types[code]
            start, _ = policy.year_range(policy.rules_on(t, today), year)
            ledger.add_entry(db, company_id, e.id, t.id, year, start, "opening", days,
                             idem=f"open:{t.id}:{e.id}:{year}", note="Opening balance (brought from the old system)")
    db.flush()
    ledger.run_accruals(db, company_id, today, employee_ids=[e.id for e in todo], record=False)
    db.flush()

    def decide_all(inst, outcome: str) -> None:
        for _ in range(6):
            db.refresh(inst)
            if inst.status != "pending":
                return
            task = (db.query(WorkflowTask).filter(WorkflowTask.instance_id == inst.id, WorkflowTask.status == "pending")
                    .order_by(WorkflowTask.id).first())
            if task is None:
                return
            approver = db.query(Employee).filter(Employee.id == task.approver_employee_id).first() \
                if task.approver_kind == "employee" else None
            with use_context(_ctx(company_id, approver, owner=approver is None)):
                workflow.decide(db, company_id, task, "approve" if outcome == "approved" else "reject",
                                None if outcome == "approved" else "Too many people are away that week. Please pick other dates.")

    def make(e, code: str, start: date, end: date | None = None, *, outcome: str = "approved", unit: str = "day",
             half: str | None = None, reason: str = "", grant_option: str | None = None) -> None:
        t = types.get(code)
        if t is None:
            return
        data = {"from_date": start, "to_date": end or start, "unit": unit, "half": half, "reason": reason,
                "grant_option": grant_option, "has_attachment": True}
        with use_context(_ctx(company_id, e)):
            try:
                ev = service.evaluate(db, company_id, e, t, data, enforce=False, cal=cal)
            except Exception:
                return
            if any("already have" in x or "available" in x or "doesn't apply" in x for x in ev["errors"]):
                return
            req = service.create_from_evaluation(db, company_id, e, t, ev, {**data, "has_attachment": False})
        if outcome == "pending" or req.workflow_instance_id is None:
            return
        from core.workflow.models import WorkflowInstance
        inst = db.query(WorkflowInstance).filter(WorkflowInstance.id == req.workflow_instance_id).first()
        decide_all(inst, outcome)

    rng = random.Random(7)
    monday = today - timedelta(days=today.weekday())
    female_done = False
    for i, e in enumerate(todo):
        # a casual leave in the last three months
        d = _working(cal, e, today - timedelta(days=rng.randint(20, 90)))
        make(e, "CL", d, reason=rng.choice(["Personal work", "Bank and documents", "Family function", "House shifting"]))
        if i % 3 == 0:
            d = _working(cal, e, today - timedelta(days=rng.randint(25, 45)))
            make(e, "SL", d, _working(cal, e, d + timedelta(days=1)), reason="Viral fever")
        if i % 4 == 1:
            d = _working(cal, e, today + timedelta(days=rng.randint(14, 30)))
            make(e, "EL", d, _working(cal, e, d + timedelta(days=2)), outcome="pending", reason="Family trip to Kerala")
        if i % 5 == 2:
            start = _working(cal, e, monday - timedelta(days=3), -1)  # the Friday before this week
            make(e, "EL", start, _working(cal, e, monday + timedelta(days=1)), reason="Visiting parents for Diwali")
        if i % 6 == 3:
            d = _working(cal, e, today + timedelta(days=rng.randint(5, 12)))
            make(e, "CL", d, outcome="rejected", reason="Personal work")
        if i % 7 == 4:
            d = _working(cal, e, today - timedelta(days=rng.randint(3, 9)))
            make(e, "CL", d, unit="half_day", half=rng.choice(["first", "second"]), reason="Doctor's appointment")
        if i % 5 == 0:
            make(e, "OH", date(year, 11, 24), outcome="pending", reason="Guru Nanak Jayanti")
        if i % 4 == 3:
            d = _working(cal, e, today + timedelta(days=rng.randint(2, 6)))
            make(e, "SL", d, outcome="pending", reason="Dental surgery")
        if i % 8 == 5 and "CO" in types:
            with use_context(_ctx(company_id, e)):
                try:
                    service.request_comp_off(db, company_id, e, {"worked_date": date(year, 10, 2), "unit": "day",
                                                                 "reason": "Production release support"},
                                             source="admin", approved=True)
                except Exception:
                    pass
        if not female_done and "ML" in types:
            f = cal.facts(e)
            if f.get("gender") == "F" and i % 2 == 1:
                make(e, "ML", date(year, 10, 1), date(year, 10, 1) + timedelta(days=181), reason="Maternity",
                     grant_option="birth")
                female_done = db.query(LeaveRequest.id).filter(LeaveRequest.employee_id == e.id,
                                                               LeaveRequest.leave_type_id == types["ML"].id).first() is not None
        db.flush()
    db.flush()
