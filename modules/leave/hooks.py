"""Approval request types of the leave module, and its permissions.

leave.request        apply for leave (also extensions); default flow: reporting manager,
                     then HR when the request is longer than 3 days
leave.cancellation   cancel approved leave (all or some days); default: reporting manager
leave.comp_off       a day off earned for working on a weekly off or holiday; default: reporting manager
"""
from core.workflow.registry import RequestType, register
from modules.roles.models import ALL_PERMISSIONS, DEFAULT_ROLE_PERMISSIONS

# Permissions owned by this module (time.leave.view and time.leave.approve already exist).
LEAVE_PERMISSIONS = {
    "time.leave.manage": "Manage leave: all requests, balances, accrual and year-end runs",
    "settings.leave.manage": "Set up leave types, holidays and work weeks",
}
for _k, _v in LEAVE_PERMISSIONS.items():
    ALL_PERMISSIONS.setdefault(_k, _v)
for _role, _perms in (("admin", list(LEAVE_PERMISSIONS)), ("hr", ["time.leave.manage", "settings.leave.manage"])):
    lst = DEFAULT_ROLE_PERMISSIONS.setdefault(_role, [])
    for _p in _perms:
        if _p not in lst:
            lst.append(_p)


def _svc():
    from modules.leave import service
    return service


def _summ_request(instance) -> list[str]:
    p = instance.payload or {}
    lines = [f"{p.get('leave_type_name') or p.get('leave_type')}: {p.get('days')} day(s)",
             f"From {p.get('from_date')} to {p.get('to_date')}"]
    if p.get("lop_days"):
        lines.append(f"Loss of pay: {p['lop_days']} day(s)")
    if p.get("kind") == "extension":
        lines.append("Extension of earlier leave")
    if p.get("reason"):
        lines.append(f"Reason: {p['reason']}")
    return lines


def _summ_cancel(instance) -> list[str]:
    p = instance.payload or {}
    lines = [f"Days to cancel: {', '.join(p.get('dates') or [])}"]
    if p.get("reason"):
        lines.append(f"Reason: {p['reason']}")
    return lines


def _summ_comp_off(instance) -> list[str]:
    p = instance.payload or {}
    lines = [f"Worked on {p.get('worked_date')} ({'holiday' if p.get('day_kind') == 'holiday' else 'weekly off'})",
             f"Comp off: {p.get('days')} day(s)"]
    if p.get("reason"):
        lines.append(f"Work done: {p['reason']}")
    return lines


register(RequestType(
    key="leave.request",
    label="Leave request",
    description="An employee applies for leave or extends leave.",
    on_approved=lambda db, i: _svc().on_request_approved(db, i),
    on_rejected=lambda db, i: _svc().on_request_rejected(db, i),
    on_cancelled=lambda db, i: _svc().on_request_cancelled(db, i),
    summarize=_summ_request,
    variables={"request.days": "Number of days", "request.leave_type": "Leave type code (EL, CL ...)",
               "request.lop_days": "Loss-of-pay days", "request.from_date": "First day", "request.kind": "leave or extension"},
    default_steps=[
        {"name": "Reporting manager", "approver_type": "reporting_manager", "mode": "any", "sla_hours": 48},
        {"name": "HR approval", "approver_type": "role", "approver_value": "hr", "mode": "any",
         "condition": "request.days > 3"},
    ],
))

register(RequestType(
    key="leave.cancellation",
    label="Leave cancellation",
    description="An employee cancels approved leave, fully or some days.",
    on_approved=lambda db, i: _svc().on_cancellation_approved(db, i),
    on_rejected=lambda db, i: _svc().on_cancellation_closed(db, i),
    on_cancelled=lambda db, i: _svc().on_cancellation_closed(db, i),
    summarize=_summ_cancel,
    variables={"request.days": "Days being cancelled", "request.leave_type": "Leave type code"},
    default_steps=[{"name": "Reporting manager", "approver_type": "reporting_manager", "mode": "any"}],
))

register(RequestType(
    key="leave.comp_off",
    label="Compensatory off",
    description="An employee asks for a day off for working on a weekly off or a holiday.",
    on_approved=lambda db, i: _svc().on_comp_off_approved(db, i),
    on_rejected=lambda db, i: _svc().on_comp_off_closed(db, i),
    on_cancelled=lambda db, i: _svc().on_comp_off_closed(db, i),
    summarize=_summ_comp_off,
    variables={"request.days": "Days of comp off", "request.day_kind": "weekend or holiday"},
    default_steps=[{"name": "Reporting manager", "approver_type": "reporting_manager", "mode": "any"}],
))


# ── Daily leave job: credit accruals and expire old credits ─────────
_LAST_RUN: dict[int, object] = {}


def daily_leave_job(db) -> int:
    """Once a day per company (the periodic loop calls it every 15 minutes): credit what
    has fallen due, expire carried leave and comp off past their date, send reminders
    before credits lapse and Bradford alerts. Idempotent."""
    from core.timeutil import today_for_company_id
    from modules.company.models import Company
    from modules.leave import ledger
    done = 0
    for (cid,) in db.query(Company.id).filter(Company.active.is_(True)).all():
        today = today_for_company_id(db, cid)
        if _LAST_RUN.get(cid) == today:
            continue
        db.info["company_id"] = cid
        with db.begin_nested():
            done += ledger.run_expiry(db, cid, today, record=False)["entries"]
            res = ledger.run_accruals(db, cid, today, record=False)
            if res["entries"]:
                from modules.leave.models import LeaveRun
                db.add(LeaveRun(company_id=cid, kind="accrual", label=f"Daily accrual {today:%d %b %Y}", as_of=today,
                                entries=res["entries"], result=res, ran_by_name="System"))
            done += res["entries"]
        try:
            with db.begin_nested():
                from modules.leave import bradford, reminders
                done += reminders.send_due(db, cid, today)
                done += bradford.run_alerts(db, cid, today)
        except Exception as exc:  # reminders and alerts never block accruals
            import logging
            logging.getLogger("hrms").warning("leave reminders/alerts failed for company %s: %s", cid, exc)
        _LAST_RUN[cid] = today
    db.info.pop("company_id", None)
    return done


try:
    from core import periodic
    periodic.register("leave.daily", daily_leave_job)
except ImportError:  # the periodic loop is not installed yet; setup_company still catches up at start-up
    pass
