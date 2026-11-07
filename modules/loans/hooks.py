"""Loan permissions and approval request types.

loan.request  a loan or salary advance; default: reporting manager, then HR
loan.change   skip instalments or prepay; default: HR
"""
from core.workflow.registry import RequestType, register
from modules.roles.models import ALL_PERMISSIONS, DEFAULT_ROLE_PERMISSIONS

ALL_PERMISSIONS.setdefault("loans.manage", "Manage loans and salary advances")
for _role in ("admin", "hr"):
    lst = DEFAULT_ROLE_PERMISSIONS.setdefault(_role, [])
    if "loans.manage" not in lst:
        lst.append("loans.manage")


def _svc():
    from modules.loans import service
    return service


def _summ(instance) -> list[str]:
    p = instance.payload or {}
    return [f"{p.get('employee_name')}: {p.get('loan_type_name')} of ₹{p.get('amount')}",
            f"{p.get('instalments')} instalments of ₹{p.get('emi')} at {p.get('interest_rate')}%",
            p.get("purpose") or ""]


register(RequestType(
    key="loan.request",
    label="Loan or salary advance",
    description="An employee asks for a loan or a salary advance.",
    on_approved=lambda db, i: _svc().on_request_approved(db, i),
    on_rejected=lambda db, i: _svc().on_request_closed(db, i),
    on_cancelled=lambda db, i: _svc().on_request_closed(db, i),
    summarize=_summ,
    variables={"request.amount": "Amount", "request.instalments": "Instalments", "request.loan_type": "Loan type code",
               "request.kind": "loan or advance"},
    default_steps=[
        {"name": "Reporting manager", "approver_type": "reporting_manager", "mode": "any"},
        {"name": "HR approval", "approver_type": "role", "approver_value": "hr", "mode": "any"},
    ],
))

register(RequestType(
    key="loan.change",
    label="Loan change (skip or prepay)",
    description="Skip instalments or prepay a running loan.",
    on_approved=lambda db, i: _svc().on_change_approved(db, i),
    on_rejected=lambda db, i: _svc().on_change_closed(db, i),
    on_cancelled=lambda db, i: _svc().on_change_closed(db, i),
    summarize=lambda i: [str((i.payload or {}).get("kind")).capitalize(), (i.payload or {}).get("reason") or ""],
    variables={"request.kind": "skip or prepay", "request.amount": "Prepayment amount"},
    default_steps=[{"name": "HR approval", "approver_type": "role", "approver_value": "hr", "mode": "any"}],
))
