"""Exit request types and the daily exit job.

offboarding.resignation  an employee resigns (default: reporting manager, then HR)
offboarding.exit         a manager raises another kind of exit (default: HR)

Job (every 15 minutes, idempotent): retirements started by age, exits completed after
the last working day (sign-in disabled, record archived), settlement handoff on the last
day, clearance reminders and alumni access expiry.
"""
from core import periodic
from core.workflow.registry import RequestType, register


def _svc():
    from modules.offboarding import service
    return service


def _summ(instance) -> list[str]:
    p = instance.payload or {}
    lines = [f"{p.get('employee_name')} · {str(p.get('exit_type') or '').replace('_', ' ').title()}"]
    if p.get("reason"):
        lines.append(f"Reason: {p['reason']}")
    lines.append(f"Resigned {p.get('resignation_date')} · last day {p.get('last_working_day')}")
    if p.get("notice_shortfall_days"):
        lines.append(f"Notice short by {p['notice_shortfall_days']} day(s)")
    return lines


register(RequestType(
    key="offboarding.resignation",
    label="Resignation",
    description="An employee resigns with a reason and a proposed last working day. It can be withdrawn until accepted.",
    on_approved=lambda db, i: _svc().on_decided(db, i, "approved"),
    on_rejected=lambda db, i: _svc().on_decided(db, i, "rejected"),
    on_cancelled=lambda db, i: _svc().on_decided(db, i, "cancelled"),
    summarize=_summ,
    variables={"request.notice_shortfall_days": "Days of notice short", "request.exit_type": "Kind of exit"},
    default_steps=[{"name": "Manager acceptance", "approver_type": "reporting_manager", "mode": "any"},
                   {"name": "HR acceptance", "approver_type": "role", "approver_value": "hr", "mode": "any"}],
))

register(RequestType(
    key="offboarding.exit",
    label="Exit raised by a manager",
    description="A manager raises a termination, end of contract or absconding case for HR to approve.",
    on_approved=lambda db, i: _svc().on_decided(db, i, "approved"),
    on_rejected=lambda db, i: _svc().on_decided(db, i, "rejected"),
    on_cancelled=lambda db, i: _svc().on_decided(db, i, "cancelled"),
    summarize=_summ,
    variables={"request.exit_type": "Kind of exit"},
    default_steps=[{"name": "HR approval", "approver_type": "role", "approver_value": "hr", "mode": "any"}],
))

periodic.register("offboarding.daily", lambda db: _svc().run_daily(db))
