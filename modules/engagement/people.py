"""Who is who, for the engagement module: the signed-in actor, small person cards for
the feed, and celebration dates.

Celebration dates come from the employee record when it has them (date of birth,
date of joining, wedding date columns, added by the employee records module), and
otherwise from `engagement_profiles`. So this module works before and after those
columns exist without a code change.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, joinedload

from modules.employees.models import Employee
from modules.engagement.models import EngagementProfile

OWNER_KEY = "owner"


@dataclass
class Actor:
    company_id: int
    employee_id: int | None
    role: str
    name: str
    # The company owner (who now also has an employee record for self-service).
    owner: bool = False

    @property
    def viewer_id(self) -> int | None:
        """The employee whose audience rules apply; None for the owner, who sees everything."""
        return None if self.is_owner else self.employee_id

    @property
    def key(self) -> str:
        return f"e{self.employee_id}" if self.employee_id else OWNER_KEY

    @property
    def is_owner(self) -> bool:
        return self.owner or self.employee_id is None


def owner_name(db: Session, company_id: int) -> str:
    from modules.company.models import Company
    c = db.query(Company).filter(Company.id == company_id).first()
    cfg = (c.config or {}) if c else {}
    return cfg.get("owner_name") or (c.company_name if c else "Company owner")


def actor_from_user(db: Session, user: dict) -> Actor:
    emp_id = user.get("employee_id")
    if emp_id:
        emp = db.query(Employee).filter(Employee.company_id == user["company_id"], Employee.id == emp_id).first()
        name = f"{emp.first_name} {emp.last_name}".strip() if emp else user.get("email")
    else:
        name = owner_name(db, user["company_id"])
    return Actor(company_id=user["company_id"], employee_id=emp_id, role=user.get("role") or "employee", name=name,
                 owner=bool(user.get("is_owner")))


def full_name(emp: Employee | None) -> str:
    return f"{emp.first_name} {emp.last_name}".strip() if emp else ""


def card(emp: Employee | None, *, owner: str | None = None) -> dict | None:
    """A small person card: what the feed shows next to a post or comment."""
    if emp is None:
        if owner is None:
            return None
        return {"uuid": None, "id": None, "name": owner, "title": "Company owner", "department": None, "image": None}
    return {
        "uuid": str(emp.uuid), "id": emp.id, "name": full_name(emp),
        "title": emp.designation.designation_name if emp.designation else None,
        "department": emp.department.department_name if emp.department else None,
        "image": emp.profile_image_url,
    }


def load(db: Session, company_id: int, ids) -> dict[int, Employee]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    rows = (db.query(Employee).options(joinedload(Employee.designation), joinedload(Employee.department))
            .filter(Employee.company_id == company_id, Employee.id.in_(ids)).all())
    return {e.id: e for e in rows}


def by_uuid(db: Session, company_id: int, uuids) -> dict[str, Employee]:
    uuids = [u for u in uuids if u]
    if not uuids:
        return {}
    rows = db.query(Employee).filter(Employee.company_id == company_id, Employee.uuid.in_(uuids)).all()
    return {str(e.uuid): e for e in rows}


def working():
    """SQL condition for people currently at work: active, and (when the employee record
    tracks a lifecycle stage) not yet to join, exited or archived."""
    status = getattr(Employee, "status", None)
    cond = Employee.active.is_(True)
    if status is not None and hasattr(status, "property"):
        cond = and_(cond, or_(status.is_(None), status.notin_(("pre_joining", "exited", "archived"))))
    return cond


def active_ids(db: Session, company_id: int) -> list[int]:
    return [i for (i,) in db.query(Employee.id).filter(Employee.company_id == company_id, working()).all()]


# ── Celebration dates ──────────────────────────────────────────────

_DATE_COLUMNS = {
    "birth": ("date_of_birth", "dob", "birth_date"),
    "joining": ("date_of_joining", "joining_date", "doj", "join_date"),
    "wedding": ("wedding_date", "date_of_marriage", "marriage_date", "wedding_anniversary"),
}


def _employee_column(kind: str):
    for name in _DATE_COLUMNS[kind]:
        col = getattr(Employee, name, None)
        if col is not None and hasattr(col, "property"):
            return col
    return None


def date_expr(kind: str):
    """SQL expression for an employee's date of the given kind ("birth", "joining", "wedding")."""
    own = {"birth": EngagementProfile.birth_date, "joining": EngagementProfile.joining_date,
           "wedding": EngagementProfile.wedding_date}[kind]
    col = _employee_column(kind)
    return func.coalesce(col, own) if col is not None else own


def dates_query(db: Session, company_id: int):
    """Active employees with their celebration dates and privacy choices:
    rows of (Employee, birth, joining, wedding, profile-or-None)."""
    return (db.query(Employee, date_expr("birth"), date_expr("joining"), date_expr("wedding"), EngagementProfile)
            .outerjoin(EngagementProfile, (EngagementProfile.employee_id == Employee.id)
                       & (EngagementProfile.company_id == Employee.company_id))
            .options(joinedload(Employee.designation), joinedload(Employee.department))
            .filter(Employee.company_id == company_id, working()))


def profile_for(db: Session, company_id: int, employee_id: int, create: bool = False) -> EngagementProfile | None:
    row = db.query(EngagementProfile).filter(EngagementProfile.company_id == company_id,
                                             EngagementProfile.employee_id == employee_id).first()
    if row is None and create:
        row = EngagementProfile(company_id=company_id, employee_id=employee_id)
        db.add(row)
        db.flush()
    return row


def set_fallback_dates(db: Session, company_id: int, employee_id: int, *, birth: date | None = None,
                       joining: date | None = None, wedding: date | None = None) -> None:
    """Record dates on the employee record when it has the columns, else on the profile."""
    emp = db.query(Employee).filter(Employee.company_id == company_id, Employee.id == employee_id).first()
    prof = profile_for(db, company_id, employee_id, create=True)
    for kind, value, attr in (("birth", birth, "birth_date"), ("joining", joining, "joining_date"),
                              ("wedding", wedding, "wedding_date")):
        if value is None:
            continue
        col = _employee_column(kind)
        if col is not None and emp is not None:
            setattr(emp, col.key, value)
        else:
            setattr(prof, attr, value)
