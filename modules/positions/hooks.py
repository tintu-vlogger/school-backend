"""Positions, succession and internal marketplace registrations: permissions, number series,
approval request types and the job that starts and ends seats on their dates.

positions.request               new / change / close a position (department head, then HR)
positions.headcount_plan        a department's headcount plan for a financial year (department head, then HR admin)
positions.internal_application  an employee's internal application (their current manager's consent)
"""
from sqlalchemy.orm import Session

from core import periodic
from core.workflow.registry import RequestType, register
from modules.positions.common import MANAGE, PERMISSIONS, VIEW
from modules.roles.models import ALL_PERMISSIONS, DEFAULT_ROLE_PERMISSIONS

for _k, _v in PERMISSIONS.items():
    ALL_PERMISSIONS.setdefault(_k, _v)
for _role, _perms in (("admin", list(PERMISSIONS)), ("hr", list(PERMISSIONS)), ("manager", [VIEW])):
    _lst = DEFAULT_ROLE_PERMISSIONS.setdefault(_role, [])
    for _p in _perms:
        if _p not in _lst:
            _lst.append(_p)

try:
    from core.numbering import DEFAULT_SERIES
    _have = {s[0] for s in DEFAULT_SERIES}
    for _row in (
        ("position", "Positions", "POS", "{PREFIX}-{SEQ:4}", "never", True, "Position codes."),
        ("position_request", "Position requests", "PRQ", "{PREFIX}/{FY}/{SEQ:4}", "financial_year", True,
         "Numbers for requests to add, change or close positions."),
        ("internal_posting", "Internal job postings", "IJP", "{PREFIX}-{SEQ:4}", "never", True,
         "Numbers for internal jobs and gigs."),
    ):
        if _row[0] not in _have:
            DEFAULT_SERIES.append(_row)
except ImportError:  # pragma: no cover
    pass


def _svc():
    from modules.positions import service
    return service


def _mkt():
    from modules.positions import marketplace
    return marketplace


def _decided(fn, status):
    def handler(db: Session, instance) -> None:
        fn()(db, instance, status)
    return handler


def _summ_request(instance) -> list[str]:
    p = instance.payload or {}
    lines = [p.get("kind_label") or "Position request"]
    if p.get("department"):
        lines.append(p["department"])
    if p.get("headcount"):
        lines.append(f"{p['headcount']} seat(s)")
    if p.get("reason"):
        lines.append(str(p["reason"])[:200])
    return lines


def _summ_plan(instance) -> list[str]:
    p = instance.payload or {}
    lines = [f"{p.get('department') or 'Department'} · {p.get('financial_year')}",
             f"Planned headcount {p.get('planned_headcount')}"
             + (f" (now {p['previous_headcount']})" if p.get("previous_headcount") is not None else "")]
    if p.get("budget"):
        lines.append(f"Budget ₹{p['budget']} a year")
    if p.get("notes"):
        lines.append(str(p["notes"])[:200])
    return lines


def _summ_app(instance) -> list[str]:
    p = instance.payload or {}
    lines = [f"{p.get('employee')} wants to apply for {p.get('posting')} ({p.get('kind_label')})"]
    if p.get("kind") == "gig" and p.get("hours_per_week"):
        lines.append(f"{p.get('duration_weeks')} weeks · {p.get('hours_per_week')} hours a week")
    if p.get("match_score") is not None:
        lines.append(f"Skills match {p['match_score']:g}%")
    return lines


register(RequestType(
    key="positions.request",
    label="Position request (new, change or close)",
    description="A request to add a new position, change one (seats, grade, reports-to …) or close one.",
    on_approved=_decided(lambda: _svc().on_request_decided, "approved"),
    on_rejected=_decided(lambda: _svc().on_request_decided, "rejected"),
    on_cancelled=_decided(lambda: _svc().on_request_decided, "cancelled"),
    summarize=_summ_request,
    variables={"request.kind": "new, change or close", "request.department_id": "Department of the position",
               "request.department_head_id": "That department's head", "request.headcount": "Seats asked for"},
    default_steps=[
        {"name": "Department head", "approver_type": "field", "approver_value": "request.department_head_id", "mode": "any"},
        {"name": "HR approval", "approver_type": "role", "approver_value": "hr", "mode": "any"},
    ],
))

register(RequestType(
    key="positions.headcount_plan",
    label="Headcount plan",
    description="A department's planned headcount for a financial year.",
    on_approved=_decided(lambda: _svc().on_plan_decided, "approved"),
    on_rejected=_decided(lambda: _svc().on_plan_decided, "rejected"),
    on_cancelled=_decided(lambda: _svc().on_plan_decided, "cancelled"),
    summarize=_summ_plan,
    variables={"request.department_id": "Department", "request.department_head_id": "Department head",
               "request.planned_headcount": "Planned headcount", "request.change": "Change from the approved number"},
    default_steps=[
        {"name": "Department head", "approver_type": "field", "approver_value": "request.department_head_id", "mode": "any"},
        {"name": "HR admin approval", "approver_type": "role", "approver_value": "admin", "mode": "any"},
    ],
))

register(RequestType(
    key="positions.internal_application",
    label="Internal job application (manager consent)",
    description="An employee's application for an internal job or gig, sent to their current manager.",
    on_approved=_decided(lambda: _mkt().on_consent, "approved"),
    on_rejected=_decided(lambda: _mkt().on_consent, "rejected"),
    on_cancelled=_decided(lambda: _mkt().on_consent, "cancelled"),
    summarize=_summ_app,
    variables={"request.manager_id": "Applicant's manager", "request.kind": "job or gig",
               "request.match_score": "Skills match %"},
    default_steps=[
        {"name": "Current manager's consent", "approver_type": "field", "approver_value": "request.manager_id", "mode": "any"},
    ],
))


def _refresh(db: Session) -> int:
    return _svc().refresh_seats(db)


periodic.register("positions.seats", _refresh)
