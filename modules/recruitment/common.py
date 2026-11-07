"""Small helpers shared by the talent lifecycle modules (recruitment, onboarding,
probation, offboarding): names, notifications, today in the company's timezone,
permission checks, settings rows and working-day arithmetic."""
from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from core import context
from core.errors import BadRequest
from core.timeutil import today_for_company_id


def now() -> datetime:
    return datetime.now(timezone.utc)


def today(db: Session, company_id: int) -> date:
    return today_for_company_id(db, company_id)


def actor_name() -> str:
    return context.current().actor_name or "System"


def token(n: int = 32) -> str:
    return secrets.token_urlsafe(n)[:43]


def Employee():
    from modules.employees.models import Employee as E
    return E


def employee(db: Session, company_id: int, eid):
    if not eid:
        return None
    E = Employee()
    return db.query(E).filter(E.company_id == company_id, E.id == eid).first()


def display_name(emp) -> str | None:
    if emp is None:
        return None
    return f"{emp.first_name} {emp.last_name}".strip()


def names_for(db: Session, company_id: int, ids) -> dict[int, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    E = Employee()
    return {r.id: f"{r.first_name} {r.last_name}".strip()
            for r in db.query(E.id, E.first_name, E.last_name).filter(E.company_id == company_id, E.id.in_(ids))}


def notify(db: Session, company_id: int, employee_id, message: str, kind: str = "talent") -> None:
    if not employee_id:
        return
    from modules.notifications.models import Notification
    db.add(Notification(company_id=company_id, employee_id=employee_id, channel="in_app", message_type=kind,
                        message_content=message[:1000], status="sent", sent_at=now()))


def role_ids(db: Session, company_id: int, roles=("hr", "admin")) -> list[int]:
    E = Employee()
    return [i for (i,) in db.query(E.id).filter(E.company_id == company_id, E.role.in_(list(roles)),
                                                E.active.is_(True)).all()]


def notify_hr(db: Session, company_id: int, message: str, kind: str = "talent") -> None:
    for eid in role_ids(db, company_id):
        notify(db, company_id, eid, message, kind)


def has_perm(db: Session, user: dict, perm: str) -> bool:
    from core.security import _get_role_permissions
    return perm in _get_role_permissions(user["company_id"], user["role"], db)


def money(value, field: str = "Amount", allow_none: bool = True) -> Decimal | None:
    if value in (None, ""):
        if allow_none:
            return None
        raise BadRequest(f"{field} is required.")
    try:
        out = Decimal(str(value).replace(",", "")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise BadRequest(f"{field} should be a number.")
    if out < 0:
        raise BadRequest(f"{field} can't be negative.")
    return out


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    year, month = d.year + y, m + 1
    import calendar
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def is_working_day_fn(db: Session, emp):
    """The leave module's work calendar when present; otherwise Monday to Saturday."""
    try:
        from modules.leave.calendar import is_working_day
    except ImportError:
        return lambda day: day.weekday() < 6

    def fn(day: date) -> bool:
        try:
            return bool(is_working_day(db, emp, day))
        except Exception:
            return day.weekday() < 6
    return fn


def add_working_days(start: date, n: int, is_working) -> date:
    """The date `n` working days after `start` (the start day itself does not count)."""
    day, left = start, n
    for _ in range(400):
        if left <= 0:
            return day
        day += timedelta(days=1)
        if is_working(day):
            left -= 1
    return day


def working_days_between(start: date, end: date, is_working) -> int:
    """Working days after `start` up to and including `end` (negative when end is earlier)."""
    if end == start:
        return 0
    sign = 1 if end > start else -1
    a, b = (start, end) if sign > 0 else (end, start)
    n, day = 0, a
    for _ in range(800):
        day += timedelta(days=1)
        if day > b:
            break
        if is_working(day):
            n += 1
    return n * sign


def audience_match(audience: dict | None, emp) -> bool:
    """Audience rules on flows and policies: department, location, employment type, grade,
    legal entity. An empty list means everyone."""
    a = audience or {}
    for key, attr in (("department_ids", "department_id"), ("location_ids", "location_id"),
                      ("employment_type_ids", "employment_type_id"), ("grade_ids", "grade_id"),
                      ("legal_entity_ids", "legal_entity_id"), ("designation_ids", "designation_id")):
        ids = [int(x) for x in (a.get(key) or []) if x not in (None, "")]
        if ids and getattr(emp, attr, None) not in ids:
            return False
    return True


def clean_audience(audience: dict | None) -> dict:
    out = {}
    for key in ("department_ids", "location_ids", "employment_type_ids", "grade_ids", "legal_entity_ids",
                "designation_ids"):
        ids = [int(x) for x in ((audience or {}).get(key) or []) if str(x).strip().lstrip("-").isdigit()]
        if ids:
            out[key] = ids
    return out


def company(db: Session, company_id: int):
    from modules.company.models import Company
    return db.query(Company).filter(Company.id == company_id).first()


def frontend_url(db: Session, company_id: int) -> str:
    """The company's own web address (subdomain) for links in emails."""
    from core.config import get_settings
    base = (get_settings().FRONTEND_URL or "http://localhost:3100").rstrip("/")
    c = company(db, company_id)
    if not c or not c.slug:
        return base
    proto, _, host = base.partition("://")
    if not host:
        return base
    if host.startswith(f"{c.slug}."):
        return base
    return f"{proto}://{c.slug}.{host}"


def deep_merge(base: dict, extra: dict | None) -> dict:
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def event(entry: dict | None = None, **kw) -> dict:
    """A timeline entry stored in a JSON history list."""
    out = {"at": now().isoformat(), "by": actor_name()}
    out.update(entry or {})
    out.update(kw)
    return out


def org_labels(db: Session, company_id: int, *, departments=(), designations=(), locations=(), masters=()) -> dict:
    """Names for department, designation, location and master ids in a few queries:
    {"department": {id: name}, "designation": {...}, "location": {...}, "master": {...}}."""
    from modules.departments.models import Department
    from modules.designations.models import Designation
    from modules.organization.models import Location
    out = {"department": {}, "designation": {}, "location": {}, "master": {}}
    d = {i for i in departments if i}
    if d:
        out["department"] = {r.id: r.department_name for r in db.query(Department.id, Department.department_name)
                             .filter(Department.id.in_(d))}
    g = {i for i in designations if i}
    if g:
        out["designation"] = {r.id: r.designation_name for r in db.query(Designation.id, Designation.designation_name)
                              .filter(Designation.id.in_(g))}
    loc = {i for i in locations if i}
    if loc:
        out["location"] = {r.id: (r.name, r.city) for r in db.query(Location.id, Location.name, Location.city)
                           .filter(Location.company_id == company_id, Location.id.in_(loc))}
    m = {i for i in masters if i}
    if m:
        from core.masters import labels_for
        out["master"] = labels_for(db, company_id, list(m))
    return out


def cancel_instance(db: Session, company_id: int, instance, reason: str | None) -> None:
    """Withdraw an open approval request. The requester (or the owner) withdraws it through
    the engine; HR acting on someone else's request closes it the same way, audited."""
    from core.errors import Forbidden
    from core.workflow import service as wf
    try:
        wf.cancel(db, company_id, instance, reason)
    except Forbidden:
        from core import audit
        audit.event(db, "workflow.cancelled", "workflow_instances", instance.id, {"reason": reason, "by": "hr"},
                    entity_uuid=str(instance.uuid), entity_label=instance.title)
        wf._finish(db, instance, "cancelled", reason)
