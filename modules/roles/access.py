"""What the signed-in person may do and see, across every role they hold: permissions
(union), data scope per permission (widest wins) and sensitive-field visibility (most
open wins). The HR admin role always has everything, for everyone."""
from sqlalchemy.orm import Session

from modules.roles.catalog import (DEFAULT_FIELD_ACCESS, FIELD_KEYS, FIELD_RANK, NEW_ROLE_FIELD_ACCESS, SCOPE_RANK,
                                   SCOPABLE)


def role_names(db: Session, user: dict) -> list[str]:
    """The base role plus any extra roles the employee holds."""
    from core.security import _extra_role_names
    names = [user["role"]]
    if user.get("employee_id"):
        for n in _extra_role_names(user["company_id"], user["employee_id"], db):
            if n not in names:
                names.append(n)
    return names


def _roles(db: Session, company_id: int, names: list[str]):
    from modules.roles.models import Role
    return db.query(Role).filter(Role.company_id == company_id, Role.role_name.in_(names)).all()


def _field_access_of(role) -> dict:
    stored = role.field_access if isinstance(role.field_access, dict) else {}
    default = DEFAULT_FIELD_ACCESS.get(role.role_name, NEW_ROLE_FIELD_ACCESS)
    return {f: stored.get(f) or default.get(f, "hidden") for f in FIELD_KEYS}


def my_access(db: Session, user: dict) -> dict:
    from core.security import _get_role_permissions
    names = role_names(db, user)
    perms = _get_role_permissions(user["company_id"], user["role"], db, employee_id=user.get("employee_id"))
    if "admin" in names:
        return {"roles": names, "permissions": perms, "scopes": {p: "everyone" for p in perms if p in SCOPABLE},
                "field_access": {f: "full" for f in FIELD_KEYS}}
    rows = _roles(db, user["company_id"], names)
    perm_set = set(perms)
    scopes: dict[str, str] = {}
    fields = {f: "hidden" for f in FIELD_KEYS}
    for r in rows:
        held = set(r.permissions or [])
        stored = r.scopes if isinstance(r.scopes, dict) else {}
        for p in held & perm_set & SCOPABLE:
            s = stored.get(p) or "everyone"
            if p not in scopes or SCOPE_RANK.get(s, 0) > SCOPE_RANK.get(scopes[p], 0):
                scopes[p] = s
        for f, level in _field_access_of(r).items():
            if FIELD_RANK.get(level, 0) > FIELD_RANK[fields[f]]:
                fields[f] = level
    return {"roles": names, "permissions": perms, "scopes": scopes, "field_access": fields}


def permission_scope(db: Session, user: dict, perm: str) -> str | None:
    """The widest scope this person has for `perm`, or None when they lack it."""
    acc = my_access(db, user)
    if perm not in acc["permissions"]:
        return None
    return acc["scopes"].get(perm, "everyone")


def field_level(db: Session, user: dict, field: str) -> str:
    """'hidden', 'masked' or 'full' for other people's `field` (salary, bank, pan, aadhaar)."""
    if user.get("role") == "admin":
        return "full"
    return my_access(db, user)["field_access"].get(field, "hidden")


def scope_employee_ids(db: Session, user: dict, scope: str) -> set[int] | None:
    """Employees covered by `scope` for this person. None means everyone."""
    from modules.employees.models import Employee
    if scope in (None, "everyone"):
        return None
    me_id = user.get("employee_id")
    if not me_id:
        return None if user.get("role") == "admin" else set()
    me = db.query(Employee).filter(Employee.id == me_id).first()
    if me is None:
        return set()
    cid = user["company_id"]
    q = db.query(Employee.id).filter(Employee.company_id == cid)
    if scope == "own":
        return {me_id}
    if scope == "direct_reports":
        return {me_id} | {i for (i,) in q.filter(Employee.reporting_manager_id == me_id).all()}
    if scope == "all_reports":
        out, frontier = {me_id}, {me_id}
        while frontier:
            rows = {i for (i,) in q.filter(Employee.reporting_manager_id.in_(frontier)).all()} - out
            out |= rows
            frontier = rows
        return out
    column = {"department": Employee.department_id, "location": Employee.location_id,
              "legal_entity": Employee.legal_entity_id}.get(scope)
    if column is None:
        return {me_id}
    value = getattr(me, column.key)
    if value is None:
        return {me_id}
    return {me_id} | {i for (i,) in q.filter(column == value).all()}
