"""What the leave rules need to know about an employee, and who a rule applies to.

Employee details (joining date, gender, location, grade ...) are read from the employee
record when the column exists, else from a custom field with the same key, so leave
keeps working while the employee record grows. Tags come from the tags module when it
is installed.

`applies(applicability, facts)` is shared by leave types, holiday calendars and work
weeks: simple pickers (any of the chosen values must match; empty means anyone), a
minimum length of service, probation, named people in or out, and an optional rule
written in the rules language (core.expressions) over `employee.*`.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy.orm import Session

from core import expressions


def _as_date(v) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _as_int(v) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _field(emp, *names):
    custom = getattr(emp, "custom", None) or {}
    for n in names:
        v = getattr(emp, n, None)
        if v not in (None, ""):
            return v
    for n in names:
        v = custom.get(n)
        if v not in (None, ""):
            return v
    return None


class FactCache:
    """Per-call cache of lookups (locations, master codes, tags) so a run over many
    employees stays fast."""

    def __init__(self, db: Session, company_id: int):
        self.db, self.company_id = db, company_id
        self._locations: dict | None = None
        self._codes: dict[int, str | None] = {}
        self._tags: dict[int, list[int]] | None = None

    def location_state(self, location_id: int | None) -> int | None:
        if location_id is None:
            return None
        if self._locations is None:
            self._locations = {}
            try:
                from modules.organization.models import Location
                with self.db.begin_nested():
                    rows = (self.db.query(Location.id, Location.state_id, Location.holiday_calendar_id)
                            .filter(Location.company_id == self.company_id).all())
                for lid, sid, cal in rows:
                    self._locations[lid] = (sid, cal)
            except Exception:
                pass
        return (self._locations.get(location_id) or (None, None))[0]

    def location_calendar(self, location_id: int | None) -> int | None:
        if location_id is None:
            return None
        self.location_state(location_id)
        return (self._locations.get(location_id) or (None, None))[1] if self._locations else None

    def master_code(self, value_id: int | None) -> str | None:
        if value_id is None:
            return None
        if value_id not in self._codes:
            from core.masters import MasterValue
            row = self.db.query(MasterValue.code, MasterValue.label).filter(MasterValue.id == value_id).first()
            self._codes[value_id] = (row[0] or row[1]) if row else None
        return self._codes[value_id]

    def tags_of(self, employee_id: int) -> list[int]:
        if self._tags is None:
            self._tags = {}
            try:
                from modules.organization.models import TagLink
                with self.db.begin_nested():
                    rows = (self.db.query(TagLink.tag_id, TagLink.entity_id)
                            .filter(TagLink.company_id == self.company_id,
                                    TagLink.entity_type.in_(("employee", "employees"))).all())
                for tag_id, eid in rows:
                    self._tags.setdefault(eid, []).append(tag_id)
            except Exception:
                pass
        return self._tags.get(employee_id, [])


def employee_facts(db: Session, emp, cache: FactCache | None = None, today: date | None = None) -> dict:
    cache = cache or FactCache(db, emp.company_id)
    today = today or date.today()
    doj = _as_date(_field(emp, "date_of_joining", "joining_date", "doj"))
    if doj is None and getattr(emp, "created_at", None) is not None:
        doj = emp.created_at.date()
    exit_date = _as_date(_field(emp, "exit_date", "last_working_date", "date_of_exit"))
    probation_end = _as_date(_field(emp, "probation_end_date", "probation_end"))
    status = getattr(emp, "status", None)
    location_id = _as_int(_field(emp, "location_id", "location"))
    state_id = cache.location_state(location_id) or _as_int(_field(emp, "state_id", "work_state"))
    gender_id = _as_int(_field(emp, "gender_id", "gender"))
    on_probation = status == "probation" or (probation_end is not None and probation_end >= today and status != "confirmed")
    return {
        "id": emp.id,
        "name": f"{emp.first_name} {emp.last_name}".strip(),
        "date_of_joining": doj,
        "exit_date": exit_date,
        "probation_end_date": probation_end,
        "on_probation": on_probation,
        "status": status or "confirmed",
        "gender_id": gender_id,
        "gender": (cache.master_code(gender_id) or "") if gender_id else "",
        "marital_status_id": _as_int(_field(emp, "marital_status_id", "marital_status")),
        "employment_type_id": _as_int(_field(emp, "employment_type_id", "employment_type")),
        "location_id": location_id,
        "state_id": state_id,
        "department_id": emp.department_id,
        "designation_id": emp.designation_id,
        "grade_id": _as_int(_field(emp, "grade_id", "grade")),
        "tags": cache.tags_of(emp.id),
        "role": emp.role,
        "employee_code": emp.employee_code,
        "reporting_manager_id": emp.reporting_manager_id,
        "active": bool(emp.active),
        "custom": emp.custom or {},
    }


def tenure_days(facts: dict, on: date) -> int:
    doj = facts.get("date_of_joining")
    return (on - doj).days if doj else 0


def applies(app: dict | None, facts: dict, on: date | None = None) -> bool:
    """Whether an applicability block covers this employee on a date."""
    app = app or {}
    on = on or date.today()
    if facts["id"] in (app.get("exclude_employees") or []):
        return False
    if facts["id"] in (app.get("employees") or []):
        return True
    mode = app.get("mode") or "all"
    if mode == "pickers" or mode == "rule" or any(app.get(k) for k in _PICKERS):
        for key, fact in _PICKERS.items():
            wanted = app.get(key) or []
            if not wanted:
                continue
            have = facts.get(fact)
            if isinstance(have, list):
                if not set(have) & set(wanted):
                    return False
            elif have not in wanted:
                return False
    if app.get("min_tenure_days") and tenure_days(facts, on) < int(app["min_tenure_days"]):
        return False
    prob = app.get("probation") or "any"
    if prob == "only" and not facts.get("on_probation"):
        return False
    if prob == "exclude" and facts.get("on_probation"):
        return False
    rule = (app.get("rule") or "").strip()
    if rule:
        try:
            if not expressions.check_condition(rule, {"employee": _expr_vars(facts, on)}, today=on):
                return False
        except expressions.ExpressionError:
            return False
    return True


_PICKERS = {
    "genders": "gender_id", "marital_statuses": "marital_status_id", "employment_types": "employment_type_id",
    "locations": "location_id", "states": "state_id", "departments": "department_id",
    "designations": "designation_id", "grades": "grade_id", "tags": "tags",
}


def _expr_vars(facts: dict, on: date) -> dict:
    return {
        **{k: v for k, v in facts.items() if k != "custom"},
        "tenure_days": tenure_days(facts, on),
        "tenure_years": round(tenure_days(facts, on) / 365.25, 2),
        "custom": facts.get("custom") or {},
    }


EXPRESSION_FIELDS = {
    "employee.gender": "Gender code (M, F, T)",
    "employee.on_probation": "On probation (true/false)",
    "employee.tenure_days": "Days since joining",
    "employee.tenure_years": "Years since joining",
    "employee.date_of_joining": "Date of joining",
    "employee.department_id": "Department",
    "employee.designation_id": "Designation",
    "employee.location_id": "Location",
    "employee.grade_id": "Grade",
    "employee.employment_type_id": "Employment type",
    "employee.role": "Role (admin, hr, manager, employee)",
    "employee.custom.<field>": "Any custom field",
}
