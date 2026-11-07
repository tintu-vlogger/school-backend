"""Travel permissions and the approval request type.

travel.request  a business trip; default: reporting manager, then HR when the trip is
                outside the travel policy or international (request.outside_policy,
                request.international)
"""
from core.workflow.registry import RequestType, register
from modules.roles.models import ALL_PERMISSIONS, DEFAULT_ROLE_PERMISSIONS

ALL_PERMISSIONS.setdefault("payroll.travel.manage", "Travel desk: see every trip, record bookings and tickets, pay advances, travel reports")
ALL_PERMISSIONS.setdefault("payroll.travel.policy", "Set up the travel policy (modes, classes, hotel limits, daily allowance)")
for _role, _keys in (("admin", ("payroll.travel.manage", "payroll.travel.policy")),
                     ("hr", ("payroll.travel.manage", "payroll.travel.policy"))):
    lst = DEFAULT_ROLE_PERMISSIONS.setdefault(_role, [])
    for _k in _keys:
        if _k not in lst:
            lst.append(_k)


def _svc():
    from modules.travel import service
    return service


def _summ(instance) -> list[str]:
    p = instance.payload or {}
    lines = [f"{p.get('employee_name')}: {p.get('route') or p.get('purpose')}",
             f"{p.get('start_date')} to {p.get('end_date')} · estimated ₹{p.get('amount')}"
             + (f" · advance ₹{p.get('advance')}" if p.get("advance") else ""),
             "International trip" if p.get("international") else "Domestic trip"]
    for f in p.get("flag_text") or []:
        lines.append(f"Outside policy: {f}")
    if p.get("exception_reason"):
        lines.append(f"Reason: {p['exception_reason']}")
    return lines


register(RequestType(
    key="travel.request",
    label="Travel request",
    description="An employee asks to travel for work.",
    on_approved=lambda db, i: _svc().on_approved(db, i),
    on_rejected=lambda db, i: _svc().on_closed(db, i),
    on_cancelled=lambda db, i: _svc().on_closed(db, i),
    summarize=_summ,
    variables={"request.amount": "Estimated cost", "request.advance": "Advance asked for",
               "request.outside_policy": "Outside the travel policy (true or false)",
               "request.flags": "Number of policy warnings", "request.international": "International trip (true or false)",
               "request.billable": "Billable to a client project (true or false)"},
    default_steps=[
        {"name": "Reporting manager", "approver_type": "reporting_manager", "mode": "any", "sla_hours": 48},
        {"name": "Policy exception or international", "approver_type": "role", "approver_value": "hr", "mode": "any",
         "condition": "request.outside_policy == true or request.international == true"},
    ],
))
