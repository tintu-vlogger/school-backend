"""Audience targeting: who a post, announcement, survey or kiosk is for.

Stored as JSONB on the record:

    {"all": true}                                     everyone
    {"match": "any", "rules": {"department": ["3", "7"], "location": ["2"]}}
    {"match": "all", "rules": {"department": ["3"], "employment_type": ["41"]}}

Within one dimension any listed value matches; `match` decides how dimensions combine
("any" = in any of the groups, "all" = in every group, e.g. Sales AND Mumbai). Values are
strings so Postgres `?|` can test them.

Dimensions are pluggable. Department, designation, role, employee and employment type
are built in. Location, team and tag light up automatically when the organisation module
adds `employees.location_id` or team/tag membership tables; another module can also call
`register_dimension(...)` from its hooks.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from sqlalchemy import ARRAY, Text, and_, cast, false, not_, or_, select, true
from sqlalchemy.orm import Session

from core.db import Base
from core.errors import BadRequest
from modules.employees.models import Employee

EVERYONE = {"all": True}


@dataclass
class Dimension:
    key: str
    label: str
    # (db, company_id, q) -> [{"value": "3", "label": "Sales"}]
    options: Callable
    # (db, company_id, employee_ids) -> {employee_id: set of value strings}
    values_for: Callable
    # False while the data behind it does not exist yet (shown disabled in the picker)
    available: Callable = lambda: True
    hint: str = ""


_DIMENSIONS: dict[str, Dimension] = {}


def register_dimension(dim: Dimension) -> None:
    _DIMENSIONS[dim.key] = dim


def dimensions() -> list[Dimension]:
    return list(_DIMENSIONS.values())


# ── Built-in dimensions ─────────────────────────────────────────────

def _column_values(column_name: str):
    def values_for(db: Session, company_id: int, ids):
        col = getattr(Employee, column_name, None)
        if col is None or not ids:
            return {}
        return {eid: ({str(v)} if v is not None else set())
                for eid, v in db.query(Employee.id, col).filter(Employee.company_id == company_id, Employee.id.in_(ids)).all()}
    return values_for


def _department_options(db, company_id, q=None):
    from modules.departments.models import Department
    query = db.query(Department).filter(Department.company_id == company_id, Department.active.is_(True))
    if q:
        query = query.filter(Department.department_name.ilike(f"%{q}%"))
    return [{"value": str(d.id), "label": d.department_name} for d in query.order_by(Department.department_name).limit(200)]


def _designation_options(db, company_id, q=None):
    from modules.designations.models import Designation
    query = db.query(Designation).filter((Designation.company_id == company_id) | (Designation.company_id.is_(None)))
    if hasattr(Designation, "active"):
        query = query.filter(Designation.active.is_(True))
    if q:
        query = query.filter(Designation.designation_name.ilike(f"%{q}%"))
    return [{"value": str(d.id), "label": d.designation_name} for d in query.order_by(Designation.designation_name).limit(200)]


def _role_options(db, company_id, q=None):
    from modules.roles.models import Role
    rows = db.query(Role).filter(Role.company_id == company_id).order_by(Role.display_name).all()
    return [{"value": r.role_name, "label": r.display_name} for r in rows if not q or q.lower() in r.display_name.lower()]


def _employee_options(db, company_id, q=None):
    query = db.query(Employee).filter(Employee.company_id == company_id, Employee.active.is_(True))
    if q:
        like = f"%{q}%"
        query = query.filter(or_((Employee.first_name + " " + Employee.last_name).ilike(like), Employee.email.ilike(like)))
    return [{"value": str(e.id), "label": f"{e.first_name} {e.last_name}".strip()}
            for e in query.order_by(Employee.first_name).limit(50)]


def _employee_values(db, company_id, ids):
    return {i: {str(i)} for i in ids}


def _employment_type_options(db, company_id, q=None):
    from core.masters import MasterType, MasterValue
    query = (db.query(MasterValue).join(MasterType, MasterType.id == MasterValue.type_id)
             .filter(MasterValue.company_id == company_id, MasterType.key == "employment_type", MasterValue.active.is_(True)))
    if q:
        query = query.filter(MasterValue.label.ilike(f"%{q}%"))
    return [{"value": str(v.id), "label": v.label} for v in query.order_by(MasterValue.sort_order)]


def _employment_type_values(db, company_id, ids):
    col = getattr(Employee, "employment_type_id", None)
    if col is not None:
        return _column_values("employment_type_id")(db, company_id, ids)
    out = {}
    for eid, custom in db.query(Employee.id, Employee.custom).filter(Employee.company_id == company_id, Employee.id.in_(ids)).all():
        v = (custom or {}).get("employment_type")
        out[eid] = {str(v)} if v not in (None, "") else set()
    return out


def _table(*names):
    for n in names:
        t = Base.metadata.tables.get(n)
        if t is not None:
            return t
    return None


def _label_column(table):
    for name in ("name", "label", "location_name", "team_name", "tag_name", "title"):
        if name in table.c:
            return table.c[name]
    return table.c.id


def _table_options(*names):
    def options(db, company_id, q=None):
        t = _table(*names)
        if t is None:
            return []
        label = _label_column(t)
        stmt = select(t.c.id, label)
        if "company_id" in t.c:
            stmt = stmt.where(t.c.company_id == company_id)
        if "active" in t.c:
            stmt = stmt.where(t.c.active.is_(True))
        if q and label is not t.c.id:
            stmt = stmt.where(label.ilike(f"%{q}%"))
        return [{"value": str(i), "label": str(lbl)} for i, lbl in db.execute(stmt.order_by(label).limit(200)).all()]
    return options


def _membership_values(link_names, key_col):
    def values_for(db, company_id, ids):
        t = _table(*link_names)
        if t is None or not ids or key_col not in t.c:
            return {}
        if "employee_id" in t.c:
            emp_col = t.c.employee_id
            stmt = select(emp_col, t.c[key_col]).where(emp_col.in_(ids))
        elif "entity_type" in t.c and "entity_id" in t.c:  # polymorphic links (tag_links)
            emp_col = t.c.entity_id
            stmt = select(emp_col, t.c[key_col]).where(t.c.entity_type.in_(("employees", "employee")), emp_col.in_(ids))
        else:
            return {}
        if "company_id" in t.c:
            stmt = stmt.where(t.c.company_id == company_id)
        out: dict[int, set] = {}
        for eid, v in db.execute(stmt).all():
            out.setdefault(eid, set()).add(str(v))
        return out
    return values_for


LOCATION_TABLES = ("locations", "work_locations", "office_locations")
TEAM_TABLES, TEAM_LINKS = ("teams",), ("team_members", "employee_teams", "team_memberships")
TAG_TABLES, TAG_LINKS = ("tags",), ("tag_links", "employee_tags")

register_dimension(Dimension("department", "Department", _department_options, _column_values("department_id")))
register_dimension(Dimension(
    "location", "Location", _table_options(*LOCATION_TABLES), _column_values("location_id"),
    available=lambda: getattr(Employee, "location_id", None) is not None and _table(*LOCATION_TABLES) is not None,
    hint="Add locations under Settings › Regions & locations to target by location."))
register_dimension(Dimension(
    "team", "Team", _table_options(*TEAM_TABLES), _membership_values(TEAM_LINKS, "team_id"),
    available=lambda: _table(*TEAM_TABLES) is not None and _table(*TEAM_LINKS) is not None,
    hint="Set up teams under People › Teams to target by team."))
register_dimension(Dimension(
    "tag", "Tag", _table_options(*TAG_TABLES), _membership_values(TAG_LINKS, "tag_id"),
    available=lambda: _table(*TAG_TABLES) is not None and _table(*TAG_LINKS) is not None,
    hint="Create tags under People › Tags to target by tag."))
register_dimension(Dimension("employment_type", "Employment type", _employment_type_options, _employment_type_values))
register_dimension(Dimension("designation", "Designation", _designation_options, _column_values("designation_id")))
register_dimension(Dimension("role", "Role", _role_options, _column_values("role")))
register_dimension(Dimension("employee", "Specific people", _employee_options, _employee_values))


# ── Working with audiences ──────────────────────────────────────────

def normalize(audience) -> dict:
    """Validate and tidy an audience from a form. Empty means everyone."""
    if not audience or audience.get("all"):
        return dict(EVERYONE)
    rules_in = audience.get("rules") or {}
    rules = {}
    for key, values in rules_in.items():
        dim = _DIMENSIONS.get(key)
        if dim is None:
            raise BadRequest(f"Unknown group “{key}”.")
        vals = sorted({str(v).strip() for v in (values or []) if str(v).strip()})
        if vals:
            if not dim.available():
                raise BadRequest(f"{dim.label} groups are not set up yet.")
            rules[key] = vals[:500]
    if not rules:
        return dict(EVERYONE)
    match = audience.get("match") if audience.get("match") in ("any", "all") else "any"
    return {"match": match, "rules": rules}


def is_everyone(audience) -> bool:
    return not audience or bool(audience.get("all")) or not (audience.get("rules") or {})


def employee_dims(db: Session, company_id: int, employee_ids) -> dict[int, dict[str, set]]:
    ids = list({i for i in employee_ids if i})
    out = {i: {} for i in ids}
    if not ids:
        return out
    for dim in _DIMENSIONS.values():
        if not dim.available():
            continue
        for eid, vals in (dim.values_for(db, company_id, ids) or {}).items():
            if eid in out:
                out[eid][dim.key] = set(vals)
    return out


def matches(audience, dims: dict[str, set]) -> bool:
    if is_everyone(audience):
        return True
    rules = audience.get("rules") or {}
    hits = [bool(set(vals) & dims.get(key, set())) for key, vals in rules.items()]
    return all(hits) if audience.get("match") == "all" else any(hits)


def resolve(db: Session, company_id: int, audience) -> list[int]:
    """Active employees in the audience."""
    from modules.engagement.people import working
    ids = [i for (i,) in db.query(Employee.id).filter(Employee.company_id == company_id, working()).all()]
    if is_everyone(audience):
        return ids
    dims = employee_dims(db, company_id, ids)
    return [i for i in ids if matches(audience, dims.get(i, {}))]


def includes(db: Session, company_id: int, audience, employee_id: int | None) -> bool:
    if is_everyone(audience) or employee_id is None:  # the owner sees everything
        return True
    return matches(audience, employee_dims(db, company_id, [employee_id]).get(employee_id, {}))


def sql_condition(column, dims: dict[str, set] | None):
    """A WHERE clause on a JSONB audience column, true when the viewer is in the audience.
    `dims=None` (the owner or a moderator) sees everything."""
    if dims is None:
        return true()
    everyone = or_(column["all"].astext == "true", not_(column.has_key("rules")))
    rules = column["rules"]
    any_parts, all_parts = [], []
    for dim in _DIMENSIONS.values():
        vals = sorted(dims.get(dim.key) or [])
        if vals:
            hit = rules[dim.key].has_any(cast(vals, ARRAY(Text)))
            any_parts.append(hit)
            all_parts.append(or_(not_(rules.has_key(dim.key)), hit))
        else:
            all_parts.append(not_(rules.has_key(dim.key)))
    is_all = column["match"].astext == "all"
    any_match = and_(not_(is_all), or_(*any_parts)) if any_parts else false()
    all_match = and_(is_all, *all_parts)
    return or_(everyone, any_match, all_match)


def viewer_dims(db: Session, company_id: int, employee_id: int | None) -> dict[str, set] | None:
    if employee_id is None:
        return None
    return employee_dims(db, company_id, [employee_id]).get(employee_id, {})


def describe(db: Session, company_id: int, audience) -> str:
    """Plain words for an audience: "Everyone" or "Sales, Support (department) · Mumbai"."""
    if is_everyone(audience):
        return "Everyone"
    parts = []
    for key, vals in (audience.get("rules") or {}).items():
        dim = _DIMENSIONS.get(key)
        if dim is None:
            continue
        labels = {o["value"]: o["label"] for o in dim.options(db, company_id, None)} if key != "employee" else {}
        if key == "employee":
            from modules.engagement.people import load, full_name
            people = load(db, company_id, [int(v) for v in vals if v.isdigit()])
            names = [full_name(people[int(v)]) for v in vals if v.isdigit() and int(v) in people]
        else:
            names = [labels.get(v, "?") for v in vals]
        shown = ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")
        parts.append(f"{shown}")
    joiner = " and " if audience.get("match") == "all" else " · "
    return joiner.join(parts) or "Everyone"


def picker_meta(db: Session, company_id: int) -> list[dict]:
    """Groups for the audience picker, with options for the small ones."""
    out = []
    for dim in _DIMENSIONS.values():
        ok = dim.available()
        out.append({
            "key": dim.key, "label": dim.label, "available": ok, "hint": None if ok else dim.hint,
            "searchable": dim.key == "employee",
            "options": (dim.options(db, company_id, None) if ok and dim.key != "employee" else []),
        })
    return out


def search_options(db: Session, company_id: int, key: str, q: str | None) -> list[dict]:
    dim = _DIMENSIONS.get(key)
    if dim is None or not dim.available():
        return []
    return dim.options(db, company_id, q)
