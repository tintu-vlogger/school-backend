"""Approval request types of the learning module.

learning.course_request   an employee asks for a course or training that isn't in the
                          catalogue; default flow: reporting manager, then HR.
learning.enrolment        enrolling in a course that needs approval; default: reporting manager.
"""
from core.workflow.registry import RequestType, register


def _svc():
    from modules.learning import service
    return service


def _request_lines(instance) -> list[str]:
    p = instance.payload or {}
    lines = [p.get("title") or "Course"]
    if p.get("provider"):
        lines.append(f"Provider: {p['provider']}")
    if p.get("estimated_cost"):
        lines.append(f"Estimated cost: ₹{float(p['estimated_cost']):,.0f}")
    if p.get("reason"):
        lines.append(f"Why: {p['reason']}")
    if p.get("link"):
        lines.append(p["link"])
    return lines


register(RequestType(
    key="learning.course_request",
    label="Course request",
    description="An employee asks for a course or training that isn't in the catalogue.",
    on_approved=lambda db, i: _svc().on_request_decision(db, i, "approved"),
    on_rejected=lambda db, i: _svc().on_request_decision(db, i, "rejected"),
    on_cancelled=lambda db, i: _svc().on_request_decision(db, i, "cancelled"),
    summarize=_request_lines,
    variables={"request.estimated_cost": "Estimated cost (₹)", "request.title": "Course asked for"},
    default_steps=[
        {"name": "Reporting manager", "approver_type": "reporting_manager", "mode": "any"},
        {"name": "HR approval", "approver_type": "role", "approver_value": "hr", "mode": "any",
         "condition": "request.estimated_cost > 0"},
    ],
))

register(RequestType(
    key="learning.enrolment",
    label="Course enrolment",
    description="Enrolling in a course that needs approval first.",
    on_approved=lambda db, i: _svc().on_enrolment_decision(db, i, "approved"),
    on_rejected=lambda db, i: _svc().on_enrolment_decision(db, i, "rejected"),
    on_cancelled=lambda db, i: _svc().on_enrolment_decision(db, i, "cancelled"),
    summarize=lambda i: [f"Course: {(i.payload or {}).get('course')}"],
    variables={"request.course_id": "Course"},
    default_steps=[{"name": "Reporting manager", "approver_type": "reporting_manager", "mode": "any"}],
))
