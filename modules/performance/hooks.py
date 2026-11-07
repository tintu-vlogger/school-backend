"""Approval request types of the performance module.

performance.increment   a manager's increment and promotion proposals in a pay review;
                        default flow: the manager's own manager, then HR.
"""
from core.workflow.registry import RequestType, register


def _on_approved(db, instance):
    from modules.performance import increments
    increments.on_decision(db, instance, "approved")


def _on_rejected(db, instance):
    from modules.performance import increments
    increments.on_decision(db, instance, "rejected")


def _on_cancelled(db, instance):
    from modules.performance import increments
    increments.on_decision(db, instance, "cancelled")


def _summarize(instance) -> list[str]:
    p = instance.payload or {}
    lines = [f"{p.get('people', 0)} people · average {p.get('avg_pct', 0)}% · total ₹{float(p.get('total_increase') or 0):,.0f} a year"]
    if p.get("promotions"):
        lines.append(f"{p['promotions']} promotion(s)")
    lines += list(p.get("lines") or [])[:10]
    return lines


register(RequestType(
    key="performance.increment",
    label="Increment and promotion proposals",
    description="A manager sends pay review proposals (increments and promotions) for approval.",
    on_approved=_on_approved,
    on_rejected=_on_rejected,
    on_cancelled=_on_cancelled,
    summarize=_summarize,
    variables={"request.people": "Number of people", "request.total_increase": "Total yearly increase (₹)",
               "request.avg_pct": "Average increment %", "request.promotions": "Number of promotions"},
    default_steps=[
        {"name": "Manager's manager", "approver_type": "reporting_manager", "mode": "any"},
        {"name": "HR approval", "approver_type": "role", "approver_value": "hr", "mode": "any"},
    ],
))
