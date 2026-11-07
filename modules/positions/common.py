"""Shared helpers for positions, succession and the internal marketplace: permissions, the
signed-in viewer, names and labels, notifications, dates and the company's settings."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core import context
from core.errors import BadRequest, Forbidden
from core.timeutil import today_for_company_id
from modules.positions.models import PositionSettings

VIEW = "talent.positions.view"
MANAGE = "talent.positions.manage"
SUCCESSION = "talent.succession.manage"
MARKETPLACE = "talent.marketplace.manage"

PERMISSIONS = {
    VIEW: "See positions, vacancies and the position chart",
    MANAGE: "Manage positions, seats, headcount plans and position requests",
    SUCCESSION: "Manage succession plans, successors and talent pools",
    MARKETPLACE: "Post internal jobs and gigs and handle internal applications",
}

DEFAULT_SETTINGS = {
    "enabled": False,
    # Seats
    "allow_overfill": False,          # let more people sit in a position than it has seats
    "require_position_on_hire": True,  # new hires and job changes must pick a vacant position (when on)
    "sync_job_from_position": True,   # a seat change updates department, designation, location ... on the record
    # Headcount plans
    "enforce_headcount_plan": True,   # seats in a department can't go above its approved plan for the year
    # Marketplace
    "marketplace_enabled": True,
    "min_months_in_role": 6,          # time in the current role before applying
    "max_open_applications": 3,
    "manager_consent_default": True,
    "match_threshold": 60,            # % shown as a good match
    # Succession
    "bench_ready_now_target": 1,      # ready-now successors a key position should have
    "bench_total_target": 2,          # successors in all
}

LIMITS = {"min_months_in_role": (0, 60), "max_open_applications": (1, 20), "match_threshold": (0, 100),
          "bench_ready_now_target": (0, 10), "bench_total_target": (0, 20)}


def now() -> datetime:
    return datetime.now(timezone.utc)


def today(db: Session, company_id: int) -> date:
    return today_for_company_id(db, company_id)


def fy_of(d: date) -> str:
    """Indian financial year label: 2026-27 for 1 Apr 2026 to 31 Mar 2027."""
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def fy_bounds(fy: str) -> tuple[date, date]:
    try:
        start = int(fy[:4])
    except (TypeError, ValueError):
        raise BadRequest("Financial year should look like 2026-27.")
    if fy != f"{start}-{(start + 1) % 100:02d}":
        raise BadRequest("Financial year should look like 2026-27.")
    return date(start, 4, 1), date(start + 1, 3, 31)


def actor_name() -> str | None:
    return context.current().actor_name


def money(value, label: str) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        out = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise BadRequest(f"{label} should be an amount.")
    if out < 0:
        raise BadRequest(f"{label} can't be negative.")
    return out


def has_perm(db: Session, user: dict, perm: str) -> bool:
    from core.security import _get_role_permissions
    return perm in _get_role_permissions(user["company_id"], user["role"], db)


class Viewer:
    """Who is asking and what they may do."""

    def __init__(self, db: Session, user: dict):
        self.db, self.user = db, user
        self.company_id = user["company_id"]
        self.employee_id = user.get("employee_id")
        self.role = user.get("role")
        self.manage = has_perm(db, user, MANAGE)
        self.view = self.manage or has_perm(db, user, VIEW)
        self.succession = has_perm(db, user, SUCCESSION)
        self.marketplace = has_perm(db, user, MARKETPLACE)

    def require(self, flag: str, message: str) -> None:
        if not getattr(self, flag):
            raise Forbidden(message)


# ── People and labels ───────────────────────────────────────────────

def emp_model():
    from modules.employees.models import Employee
    return Employee


def employee(db: Session, company_id: int, eid):
    if not eid:
        return None
    E = emp_model()
    return db.query(E).filter(E.company_id == company_id, E.id == eid).first()


def require_employee(db: Session, company_id: int, eid, label: str = "employee"):
    emp = employee(db, company_id, eid)
    if emp is None:
        raise BadRequest(f"Pick the {label}.")
    return emp


def display_name(emp) -> str | None:
    if emp is None:
        return None
    return " ".join(x for x in (emp.first_name, emp.last_name) if x).strip()


def _photo(e) -> str | None:
    url = getattr(e, "profile_image_url", None)
    return url if url and not url.startswith("data:") else None


def people(db: Session, company_id: int, ids) -> dict[int, dict]:
    """{id: {id, uuid, name, code, photo, designation_id, department_id, manager_id, active}}."""
    ids = {int(i) for i in ids if i}
    if not ids:
        return {}
    E = emp_model()
    rows = db.query(E).filter(E.company_id == company_id, E.id.in_(ids)).all()
    return {e.id: {"id": e.id, "uuid": str(e.uuid), "name": display_name(e), "code": e.employee_code,
                   "photo": _photo(e), "designation_id": e.designation_id,
                   "department_id": e.department_id, "manager_id": e.reporting_manager_id,
                   "active": bool(e.active) and e.status not in ("exited", "archived"),
                   "date_of_joining": e.date_of_joining.isoformat() if e.date_of_joining else None} for e in rows}


def person_ref(p: dict | None) -> dict | None:
    if not p:
        return None
    return {"id": p["id"], "uuid": p["uuid"], "name": p["name"], "code": p.get("code"), "photo": p.get("photo")}


def org_labels(db: Session, company_id: int, *, departments=(), designations=(), locations=(), grades=(),
               cost_centres=()) -> dict:
    from modules.departments.models import Department
    from modules.designations.models import Designation
    from modules.organization.models import CostCentre, Grade, Location
    out = {"department": {}, "designation": {}, "location": {}, "grade": {}, "cost_centre": {}}

    def ids(xs):
        return {i for i in xs if i}
    if ids(departments):
        out["department"] = {r.id: r.department_name for r in db.query(Department.id, Department.department_name)
                             .filter(Department.id.in_(ids(departments)))}
    if ids(designations):
        out["designation"] = {r.id: r.designation_name for r in db.query(Designation.id, Designation.designation_name)
                              .filter(Designation.id.in_(ids(designations)))}
    if ids(locations):
        out["location"] = {r.id: r.name for r in db.query(Location.id, Location.name)
                           .filter(Location.company_id == company_id, Location.id.in_(ids(locations)))}
    if ids(grades):
        out["grade"] = {r.id: (f"{r.code} · {r.name}" if r.code else r.name) for r in db.query(Grade.id, Grade.code, Grade.name)
                        .filter(Grade.company_id == company_id, Grade.id.in_(ids(grades)))}
    if ids(cost_centres):
        out["cost_centre"] = {r.id: (f"{r.code} · {r.name}" if r.code else r.name)
                              for r in db.query(CostCentre.id, CostCentre.code, CostCentre.name)
                              .filter(CostCentre.company_id == company_id, CostCentre.id.in_(ids(cost_centres)))}
    return out


def skill_names(db: Session, company_id: int, ids) -> dict[int, str]:
    from modules.organization.models import Skill
    ids = {int(i) for i in ids if i}
    if not ids:
        return {}
    return {r.id: r.name for r in db.query(Skill.id, Skill.name).filter(Skill.company_id == company_id, Skill.id.in_(ids))}


def clean_skills(db: Session, company_id: int, items) -> list[dict]:
    """[{"skill_id": 1, "level": 3}] with known skills, levels 1..5, no repeats."""
    out, seen = [], set()
    if not items:
        return out
    known = skill_names(db, company_id, [i.get("skill_id") for i in items if isinstance(i, dict)])
    for i in items:
        if not isinstance(i, dict):
            continue
        sid = int(i.get("skill_id") or 0)
        if sid not in known or sid in seen:
            continue
        level = int(i.get("level") or 3)
        if not 1 <= level <= 5:
            raise BadRequest("Skill levels go from 1 to 5.")
        seen.add(sid)
        out.append({"skill_id": sid, "level": level})
    if len(out) > 25:
        raise BadRequest("Up to 25 skills.")
    return out


def notify(db: Session, company_id: int, employee_id, message: str, kind: str = "positions") -> None:
    if not employee_id:
        return
    from modules.notifications.models import Notification
    db.add(Notification(company_id=company_id, employee_id=employee_id, channel="in_app", message_type=kind,
                        message_content=message[:1000], status="sent", sent_at=now()))


# ── Settings ────────────────────────────────────────────────────────

def get_settings(db: Session, company_id: int) -> dict:
    row = db.query(PositionSettings).filter(PositionSettings.company_id == company_id).first()
    return {**DEFAULT_SETTINGS, **((row.config if row else None) or {})}


def update_settings(db: Session, company_id: int, data: dict) -> dict:
    row = db.query(PositionSettings).filter(PositionSettings.company_id == company_id).first()
    if row is None:
        row = PositionSettings(company_id=company_id, config={})
        db.add(row)
    config = {**DEFAULT_SETTINGS, **(row.config or {})}
    for k, v in (data or {}).items():
        if k not in DEFAULT_SETTINGS:
            continue
        if isinstance(DEFAULT_SETTINGS[k], bool):
            config[k] = bool(v)
        else:
            try:
                n = int(v)
            except (TypeError, ValueError):
                raise BadRequest("Settings must be whole numbers.")
            lo, hi = LIMITS[k]
            if not lo <= n <= hi:
                raise BadRequest(f"Keep this between {lo} and {hi}.")
            config[k] = n
    row.config = config
    flag_modified(row, "config")
    db.flush()
    return config
