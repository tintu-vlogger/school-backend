"""Expense permissions and approval request types.

expense.claim    an expense claim; default: reporting manager, then HR above ₹10,000
expense.advance  a travel advance; default: reporting manager, then HR
"""
from core.workflow.registry import RequestType, register
from modules.roles.models import ALL_PERMISSIONS, DEFAULT_ROLE_PERMISSIONS

ALL_PERMISSIONS.setdefault("expenses.manage", "See every expense claim and advance, pay them and set up categories")
for _role in ("admin", "hr"):
    lst = DEFAULT_ROLE_PERMISSIONS.setdefault(_role, [])
    if "expenses.manage" not in lst:
        lst.append("expenses.manage")


def _svc():
    from modules.expenses import service
    return service


def _summ_claim(instance) -> list[str]:
    p = instance.payload or {}
    lines = [f"{p.get('employee_name')}: ₹{p.get('amount')} in {p.get('lines')} bill(s)",
             "Paid with salary" if p.get("pay_via") == "payroll" else "Paid by bank transfer"]
    for f in p.get("flag_text") or []:
        lines.append(f"Policy: {f}")
    return lines


register(RequestType(
    key="expense.claim",
    label="Expense claim",
    description="An employee claims expenses with bills.",
    on_approved=lambda db, i: _svc().on_claim_approved(db, i),
    on_rejected=lambda db, i: _svc().on_claim_closed(db, i),
    on_cancelled=lambda db, i: _svc().on_claim_closed(db, i),
    summarize=_summ_claim,
    variables={"request.amount": "Total claimed", "request.flags": "Number of policy warnings", "request.pay_via": "payroll or direct"},
    default_steps=[
        {"name": "Reporting manager", "approver_type": "reporting_manager", "mode": "any", "sla_hours": 48},
        {"name": "Finance check", "approver_type": "role", "approver_value": "hr", "mode": "any",
         "condition": "request.amount > 10000 or request.flags > 0"},
    ],
))

register(RequestType(
    key="expense.advance",
    label="Travel advance",
    description="An employee asks for money ahead of travel.",
    on_approved=lambda db, i: _svc().on_advance_approved(db, i),
    on_rejected=lambda db, i: _svc().on_advance_closed(db, i),
    on_cancelled=lambda db, i: _svc().on_advance_closed(db, i),
    summarize=lambda i: [f"{(i.payload or {}).get('employee_name')}: ₹{(i.payload or {}).get('amount')}",
                         (i.payload or {}).get("purpose") or ""],
    variables={"request.amount": "Amount"},
    default_steps=[
        {"name": "Reporting manager", "approver_type": "reporting_manager", "mode": "any"},
        {"name": "Finance", "approver_type": "role", "approver_value": "hr", "mode": "any"},
    ],
))
