"""Who is asking, and the company's performance settings.

Actor: the signed-in person with their permissions. Admin rights for performance come
from `talent.performance.admin`; managers act on their reportees (reporting line), and
everyone acts on their own records.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from fastapi import Depends
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core.db import get_db
from core.errors import BadRequest, Forbidden, NotFound
from core.security import _get_role_permissions, get_current_user
from modules.performance import calc
from modules.performance.models import PerfSettings, RatingScale

ADMIN = "talent.performance.admin"
INCREMENTS = "talent.increments.manage"


@dataclass
class Actor:
    company_id: int
    employee_id: int | None
    role: str
    name: str
    perms: set = field(default_factory=set)

    @property
    def is_owner(self) -> bool:
        return self.employee_id is None

    def can(self, perm: str) -> bool:
        return self.is_owner or perm in self.perms

    @property
    def is_admin(self) -> bool:
        return self.can(ADMIN)

    def require(self, perm: str = ADMIN) -> None:
        if not self.can(perm):
            raise Forbidden("Only performance administrators can do this.")

    @property
    def key(self) -> str:
        return "owner" if self.is_owner else f"e:{self.employee_id}"


def get_actor(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> Actor:
    from core import context
    perms = set(_get_role_permissions(current_user["company_id"], current_user["role"], db))
    return Actor(company_id=current_user["company_id"], employee_id=current_user.get("employee_id"),
                 role=current_user["role"], name=context.current().actor_name or "Company owner", perms=perms)


def now() -> datetime:
    return datetime.now(timezone.utc)


def today(db: Session, company_id: int) -> date:
    from core.workflow.service import _today
    return _today(db, company_id)


# ── Employees ───────────────────────────────────────────────────────

def emp_model():
    from modules.employees.models import Employee
    return Employee


def name_of(emp) -> str:
    if emp is None:
        return "Company owner"
    return f"{emp.first_name} {emp.last_name}".strip()


def employee(db: Session, company_id: int, employee_id: int | None, *, required: bool = True):
    E = emp_model()
    emp = None
    if employee_id is not None:
        emp = db.query(E).filter(E.company_id == company_id, E.id == employee_id).first()
    if emp is None and required:
        raise BadRequest("That employee was not found.")
    return emp


def employees_by_id(db: Session, company_id: int, ids) -> dict:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    E = emp_model()
    return {e.id: e for e in db.query(E).filter(E.company_id == company_id, E.id.in_(ids)).all()}


def person(emp) -> dict | None:
    if emp is None:
        return None
    return {"id": emp.id, "uuid": str(emp.uuid), "name": name_of(emp), "photo": emp.profile_image_url,
            "designation": emp.designation.designation_name if getattr(emp, "designation", None) else None,
            "department": emp.department.department_name if getattr(emp, "department", None) else None,
            "department_id": emp.department_id}


def reportee_ids(db: Session, company_id: int, manager_id: int | None, deep: bool = False) -> set[int]:
    if manager_id is None:
        return set()
    E = emp_model()
    out: set[int] = set()
    frontier = {manager_id}
    while frontier:
        rows = {i for (i,) in db.query(E.id).filter(E.company_id == company_id, E.active.is_(True),
                                                       E.reporting_manager_id.in_(frontier)).all()}
        rows -= out
        rows.discard(manager_id)
        out |= rows
        frontier = rows if deep else set()
    return out


def is_manager_of(db: Session, actor: Actor, employee_id: int, deep: bool = True) -> bool:
    if actor.employee_id is None:
        return False
    return employee_id in reportee_ids(db, actor.company_id, actor.employee_id, deep=deep)


def can_see_employee(db: Session, actor: Actor, employee_id: int) -> bool:
    return actor.is_admin or actor.employee_id == employee_id or is_manager_of(db, actor, employee_id)


def ensure_can_see(db: Session, actor: Actor, employee_id: int) -> None:
    if not can_see_employee(db, actor, employee_id):
        raise Forbidden("You can only see your own records and your team's.")


# ── Settings ────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "okr": {
        "cycle_start_month": 4,  # April: the Indian financial year
        "period_kind": "quarter",
        "statuses": calc.DEFAULT_STATUSES,
        "auto_status": True,
        "grace_days": 2,
        "levels": ["company", "department", "team", "individual"],
        "max_objectives_per_person": 8,
    },
    "nine_box": {
        "labels": {
            "1": "Low performer", "2": "Effective, low potential", "3": "Trusted professional",
            "4": "Inconsistent", "5": "Core player", "6": "High performer",
            "7": "Rough diamond", "8": "Future star", "9": "Star",
        },
    },
    "reviews": {
        "reminder_min_hours": 1,  # never remind the same person twice within this
        "reminder_max_per_day": 2,
        "escalate_after_days": 3,  # days past the deadline before the manager's manager is told
        "hide_self_review_from_manager": False,
        "allow_response": True,
    },
    "feedback": {"allow_anonymous": False, "default_visibility": "private"},
    "letters": {
        "review_template": (
            "Dear {employee_name},\n\nThank you for your contribution during {cycle_name} "
            "({period}). Based on your reviews and the company's calibration, your final rating is "
            "{final_rating} – {rating_label}.\n\n{manager_comment}\n\nWe look forward to another strong year "
            "together.\n\nWarm regards,\n{company_name}"),
        "increment_template": (
            "Dear {employee_name},\n\nWe are pleased to share that, following the {cycle_name}, your annual "
            "compensation is revised from {current_ctc} to {new_ctc} ({increment_pct}% increase), effective "
            "{effective_date}.{promotion_line}\n\nThis letter is confidential. Thank you for your continued "
            "contribution.\n\nWarm regards,\n{company_name}"),
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def settings_row(db: Session, company_id: int) -> PerfSettings:
    row = db.query(PerfSettings).filter(PerfSettings.company_id == company_id).first()
    if row is None:
        row = PerfSettings(company_id=company_id, config={})
        db.add(row)
        db.flush()
    return row


def config(db: Session, company_id: int) -> dict:
    row = db.query(PerfSettings).filter(PerfSettings.company_id == company_id).first()
    return _merge(DEFAULT_CONFIG, row.config if row else {})


def save_config(db: Session, company_id: int, data: dict) -> dict:
    row = settings_row(db, company_id)
    merged = _merge(row.config or {}, data)
    statuses = ((merged.get("okr") or {}).get("statuses"))
    if statuses is not None:
        if not (3 <= len(statuses) <= 10):
            raise BadRequest("Keep between 3 and 10 goal statuses.")
        keys = [s.get("key") for s in statuses]
        if any(not k for k in keys) or len(set(keys)) != len(keys):
            raise BadRequest("Every goal status needs its own short key.")
    month = (merged.get("okr") or {}).get("cycle_start_month")
    if month is not None and not (1 <= int(month) <= 12):
        raise BadRequest("Pick a month between January and December.")
    row.config = merged
    flag_modified(row, "config")
    db.flush()
    return config(db, company_id)


# ── Rating scales ───────────────────────────────────────────────────

def default_scale(db: Session, company_id: int) -> RatingScale | None:
    q = db.query(RatingScale).filter(RatingScale.company_id == company_id, RatingScale.active.is_(True))
    return q.filter(RatingScale.is_default.is_(True)).first() or q.order_by(RatingScale.id).first()


def scale_values(scale: RatingScale | None) -> list[float]:
    if scale is None:
        return [1.0, 2.0, 3.0, 4.0, 5.0]
    return sorted(float(p["value"]) for p in (scale.points or []))


def scale_label(scale: RatingScale | None, value) -> str | None:
    if value is None:
        return None
    for p in (scale.points if scale else []) or []:
        if float(p["value"]) == float(value):
            return p.get("label")
    return None


def get_by_uuid(db: Session, model, company_id: int, uuid, what: str):
    row = db.query(model).filter(model.company_id == company_id, model.uuid == uuid).first()
    if row is None:
        raise NotFound(what)
    return row


def notify(db: Session, company_id: int, employee_id: int | None, message: str, kind: str = "performance") -> None:
    if employee_id is None:
        return
    from modules.notifications.models import Notification
    db.add(Notification(company_id=company_id, employee_id=employee_id, channel="in_app", message_type=kind,
                        message_content=message[:1000], status="sent", sent_at=now()))
