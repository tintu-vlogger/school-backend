"""Payroll permissions, approval request types and background jobs.

payroll.run         approve a pay run (default: HR admin)
payroll.settlement  approve a full and final settlement (default: HR admin)

Jobs (every 15 minutes, idempotent): final-settlement deadline alerts (the 2-working-day
rule) and compliance due-date reminders.
"""
from core.workflow.registry import RequestType, register
from modules.roles.models import ALL_PERMISSIONS, DEFAULT_ROLE_PERMISSIONS

PAYROLL_PERMISSIONS = {
    "payroll.salaries.manage": "Assign and revise salaries",
    "payroll.reopen": "Reopen locked pay runs (with a reason)",
    "payroll.settings": "Set up salary components, structures, pay groups and statutory settings",
    "payroll.tax.manage": "Verify tax declarations, produce TDS returns and Form 16",
    "payroll.statutory.manage": "Produce PF, ESI, PT and LWF files and record challans",
    "payroll.settlements.manage": "Prepare and pay final settlements",
}
for _k, _v in PAYROLL_PERMISSIONS.items():
    ALL_PERMISSIONS.setdefault(_k, _v)
for _role, _perms in (("admin", list(PAYROLL_PERMISSIONS)),
                      ("hr", ["payroll.view", "payroll.run", "payroll.salaries.manage", "payroll.tax.manage",
                              "payroll.statutory.manage", "payroll.settlements.manage"])):
    lst = DEFAULT_ROLE_PERMISSIONS.setdefault(_role, [])
    for _p in _perms:
        if _p not in lst:
            lst.append(_p)


def _runs():
    from modules.payroll import runs
    return runs


def _settlement():
    from modules.payroll import settlement
    return settlement


def _summ_run(instance) -> list[str]:
    p = instance.payload or {}
    lines = [f"{p.get('employees')} employees", f"Gross ₹{p.get('gross')} · Net ₹{p.get('net')}",
             f"Employer cost ₹{p.get('employer_cost')}"]
    if p.get("flags"):
        lines.append(f"{p['flags']} point(s) raised by the payroll check assistant")
    return lines


def _summ_settlement(instance) -> list[str]:
    p = instance.payload or {}
    return [f"{p.get('employee_name')}: last day {p.get('last_working_day')}", f"Net payable ₹{p.get('net')}",
            f"Due by {p.get('due_date')} (2 working days)"]


register(RequestType(
    key="payroll.run",
    label="Pay run approval",
    description="Approve a calculated pay run before it is locked and paid.",
    on_approved=lambda db, i: _runs().on_approved(db, i),
    on_rejected=lambda db, i: _runs().on_rejected(db, i),
    on_cancelled=lambda db, i: _runs().on_rejected(db, i),
    summarize=_summ_run,
    variables={"request.amount": "Net pay of the run", "request.employees": "Number of employees",
               "request.kind": "regular, bonus, arrears, settlement or correction"},
    default_steps=[{"name": "Payroll approval", "approver_type": "role", "approver_value": "admin", "mode": "any",
                    "sla_hours": 24}],
))

register(RequestType(
    key="payroll.settlement",
    label="Final settlement approval",
    description="Approve a full and final settlement before it is paid (due within 2 working days of exit).",
    on_approved=lambda db, i: _settlement().on_approved(db, i),
    on_rejected=lambda db, i: _settlement().on_rejected(db, i),
    on_cancelled=lambda db, i: _settlement().on_rejected(db, i),
    summarize=_summ_settlement,
    variables={"request.amount": "Net payable", "request.exit_kind": "Kind of exit"},
    default_steps=[{"name": "Settlement approval", "approver_type": "role", "approver_value": "admin", "mode": "any",
                    "sla_hours": 8}],
))


def _jobs(db) -> int:
    from modules.company.models import Company
    from modules.payroll import compliance, settlement
    done = 0
    for (cid,) in db.query(Company.id).filter(Company.active.is_(True)).all():
        db.info["company_id"] = cid
        with db.begin_nested():
            done += settlement.deadline_alerts(db, cid)
            done += compliance.due_reminders(db, cid)
    db.info.pop("company_id", None)
    return done


try:
    from core import periodic
    periodic.register("payroll.deadlines", _jobs)
except ImportError:  # pragma: no cover
    pass
