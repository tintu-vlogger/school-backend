"""Roles and access: roles with permissions, data scopes and sensitive-field visibility,
and who holds which roles (several per person). Every change is audited: role rows and
role assignments through the automatic model audit, plus a business event for each
assignment change."""
import re
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core import audit
from core.cache import get_redis
from core.db import get_db
from core.errors import BadRequest, Forbidden, NotFound
from core.pagination import PageParams
from core.security import get_current_user, require_permission
from modules.roles import access
from modules.roles.catalog import (DEFAULT_FIELD_ACCESS, DEFAULT_SCOPES, FIELD_KEYS, FIELD_RANK, NEW_ROLE_FIELD_ACCESS,
                                   SCOPABLE, SCOPE_KEYS, catalog)
from modules.roles.models import ALL_PERMISSIONS, EmployeeRole, Role

router = APIRouter(prefix="/roles", tags=["Roles & Permissions"])
MANAGE = require_permission("settings.roles.manage")

# Base roles, most powerful first. An employee's base role (employees.role) is the most
# powerful system role they hold; every other role they hold is an extra role.
BASE_ORDER = ["admin", "principal", "hr", "accountant", "manager", "teacher", "librarian", "employee"]


class RoleIn(BaseModel):
    display_name: str = Field(min_length=2, max_length=100)
    description: str | None = Field(default=None, max_length=300)
    permissions: list[str] = []
    scopes: dict[str, str] = {}
    field_access: dict[str, str] = {}


class RoleUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=2, max_length=100)
    description: str | None = Field(default=None, max_length=300)
    permissions: list[str] | None = None
    scopes: dict[str, str] | None = None
    field_access: dict[str, str] | None = None


class CloneIn(BaseModel):
    display_name: str | None = Field(default=None, min_length=2, max_length=100)


class AssignIn(BaseModel):
    role_ids: list[int]


class BulkAssignIn(BaseModel):
    employee_ids: list[int] = Field(min_length=1, max_length=500)
    role_id: int
    action: str = Field(default="add", pattern="^(add|remove)$")


# ── Helpers ─────────────────────────────────────────────────────────

def _clear_permission_cache(company_id: int):
    """Forget cached permissions and extra roles for everyone in a company."""
    try:
        r = get_redis()
        keys = r.keys(f"perms:{company_id}:*")
        if keys:
            r.delete(*keys)
    except Exception:
        pass


def _seed_default_roles(db: Session, company_id: int):
    from seed.seed_data import ensure_default_roles
    ensure_default_roles(db, company_id)
    _clear_permission_cache(company_id)


def _clean_permissions(perms: list[str]) -> list[str]:
    invalid = [p for p in perms if p not in ALL_PERMISSIONS]
    if invalid:
        raise BadRequest(f"Unknown permissions: {', '.join(invalid)}")
    return [p for p in ALL_PERMISSIONS if p in set(perms)]  # catalogue order, no duplicates


def _clean_scopes(scopes: dict[str, str], perms: list[str]) -> dict[str, str]:
    out = {}
    for key, value in (scopes or {}).items():
        if value not in SCOPE_KEYS:
            raise BadRequest(f"Unknown data scope: {value}")
        if key in SCOPABLE and key in perms and value != "everyone":
            out[key] = value
    return out


def _clean_fields(fields: dict[str, str], base: dict[str, str]) -> dict[str, str]:
    out = dict(base)
    for key, value in (fields or {}).items():
        if key not in FIELD_KEYS:
            raise BadRequest(f"Unknown field: {key}")
        if value not in FIELD_RANK:
            raise BadRequest(f"Unknown visibility: {value}")
        out[key] = value
    return out


def _role_key(db: Session, company_id: int, display_name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", display_name.lower()).strip("_")[:40] or "role"
    if base in BASE_ORDER:
        base = f"{base}_custom"
    key, n = base, 2
    while db.query(Role.id).filter(Role.company_id == company_id, Role.role_name == key).first():
        key, n = f"{base}_{n}"[:50], n + 1
    return key


def _get_role(db: Session, company_id: int, role_id: int) -> Role:
    role = db.query(Role).filter(Role.id == role_id, Role.company_id == company_id).first()
    if not role:
        raise NotFound("Role")
    return role


def _member_counts(db: Session, company_id: int) -> dict[str, int]:
    from sqlalchemy import func
    from modules.employees.models import Employee
    counts: dict[str, int] = {}
    active = (Employee.company_id == company_id, Employee.active.is_(True))
    for name, n in db.query(Employee.role, func.count(Employee.id)).filter(*active).group_by(Employee.role).all():
        counts[name] = counts.get(name, 0) + n
    rows = (db.query(Role.role_name, func.count(EmployeeRole.id))
            .join(EmployeeRole, EmployeeRole.role_id == Role.id)
            .join(Employee, Employee.id == EmployeeRole.employee_id)
            .filter(Role.company_id == company_id, *active).group_by(Role.role_name).all())
    for name, n in rows:
        counts[name] = counts.get(name, 0) + n
    return counts


def _role_out(r: Role, counts: dict[str, int]) -> dict:
    perms = list(ALL_PERMISSIONS.keys()) if r.role_name == "admin" else [p for p in (r.permissions or []) if p in ALL_PERMISSIONS]
    stored_fields = r.field_access if isinstance(r.field_access, dict) else {}
    default_fields = DEFAULT_FIELD_ACCESS.get(r.role_name, NEW_ROLE_FIELD_ACCESS) if r.is_system else NEW_ROLE_FIELD_ACCESS
    return {
        "id": r.id, "uuid": str(r.uuid), "role_name": r.role_name, "display_name": r.display_name,
        "description": r.description, "permissions": perms, "is_system": r.is_system,
        "locked": r.role_name == "admin" and r.is_system, "customised": bool(r.customised),
        "scopes": {k: v for k, v in (r.scopes or {}).items() if k in perms},
        "field_access": {f: stored_fields.get(f) or default_fields.get(f, "hidden") for f in FIELD_KEYS},
        "permission_count": len(perms), "total_permissions": len(ALL_PERMISSIONS),
        "member_count": counts.get(r.role_name, 0),
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


# ── Catalogue and own access ────────────────────────────────────────

@router.get("/permissions")
def list_all_permissions(current_user: dict = Depends(get_current_user)):
    """Every permission key with its plain-language label."""
    from modules.roles.catalog import group_of
    return [{"key": k, "description": v, "group": group_of(k), "scopable": k in SCOPABLE} for k, v in ALL_PERMISSIONS.items()]


@router.get("/catalog")
def permission_catalog(current_user: dict = Depends(get_current_user)):
    """Permissions grouped by module, the data scopes and the sensitive fields."""
    return catalog()


@router.get("/my-permissions")
def get_my_permissions(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """The signed-in person's roles, permissions, data scopes and field visibility."""
    acc = access.my_access(db, current_user)
    return {
        "role": current_user["role"],
        "roles": acc["roles"],
        "permissions": acc["permissions"],
        "scopes": acc["scopes"],
        "field_access": acc["field_access"],
        "is_owner": bool(current_user.get("is_owner")),
        "employee_id": current_user.get("employee_id"),
        "is_manager": _is_manager(db, current_user),
    }


def _is_manager(db: Session, user: dict) -> bool:
    from modules.employees.models import Employee
    if not user.get("employee_id"):
        return False
    return db.query(Employee.id).filter(Employee.company_id == user["company_id"],
                                        Employee.reporting_manager_id == user["employee_id"],
                                        Employee.active.is_(True)).first() is not None


# ── Roles ───────────────────────────────────────────────────────────

@router.get("/")
def list_roles(current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    """Every role in the company with its permissions and how many people hold it."""
    _seed_default_roles(db, current_user["company_id"])
    cid = current_user["company_id"]
    roles = db.query(Role).filter(Role.company_id == cid).all()
    order = {n: i for i, n in enumerate(BASE_ORDER)}
    roles.sort(key=lambda r: (0 if r.is_system else 1, order.get(r.role_name, 99), r.display_name.lower()))
    counts = _member_counts(db, cid)
    return [_role_out(r, counts) for r in roles]


@router.get("/{role_id}")
def get_role(role_id: int, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    return _role_out(_get_role(db, cid, role_id), _member_counts(db, cid))


@router.post("/", status_code=status.HTTP_201_CREATED)
def create_role(data: RoleIn, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    """Create a custom role."""
    cid = current_user["company_id"]
    name = data.display_name.strip()
    if db.query(Role.id).filter(Role.company_id == cid, Role.display_name.ilike(name)).first():
        raise BadRequest("A role with this name already exists.")
    perms = _clean_permissions(data.permissions)
    role = Role(company_id=cid, role_name=_role_key(db, cid, name), display_name=name,
                description=(data.description or "").strip() or None, permissions=perms,
                scopes=_clean_scopes(data.scopes, perms),
                field_access=_clean_fields(data.field_access, NEW_ROLE_FIELD_ACCESS), is_system=False)
    db.add(role)
    db.commit()
    _clear_permission_cache(cid)
    return _role_out(role, {})


@router.post("/{role_id}/clone", status_code=status.HTTP_201_CREATED)
def clone_role(role_id: int, data: CloneIn, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    """A new custom role starting from an existing one."""
    cid = current_user["company_id"]
    src = _get_role(db, cid, role_id)
    src_out = _role_out(src, {})
    name = (data.display_name or f"{src.display_name} (copy)").strip()
    base, n = name, 2
    while db.query(Role.id).filter(Role.company_id == cid, Role.display_name.ilike(name)).first():
        name, n = f"{base} {n}", n + 1
    role = Role(company_id=cid, role_name=_role_key(db, cid, name), display_name=name[:100],
                description=src.description, permissions=src_out["permissions"], scopes=dict(src_out["scopes"]),
                field_access=dict(src_out["field_access"]), is_system=False)
    db.add(role)
    db.commit()
    _clear_permission_cache(cid)
    return _role_out(role, {})


@router.put("/{role_id}")
def update_role(role_id: int, data: RoleUpdate, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    """Change a role's name, permissions, data scopes or field visibility. The HR admin role
    always has every permission and cannot be changed."""
    cid = current_user["company_id"]
    role = _get_role(db, cid, role_id)
    if role.is_system and role.role_name == "admin":
        raise BadRequest("The HR admin role always has full access and can't be changed.")
    if data.display_name is not None:
        name = data.display_name.strip()
        if db.query(Role.id).filter(Role.company_id == cid, Role.display_name.ilike(name), Role.id != role.id).first():
            raise BadRequest("A role with this name already exists.")
        role.display_name = name
    if data.description is not None:
        role.description = data.description.strip() or None
    touched = False
    if data.permissions is not None:
        role.permissions = _clean_permissions(data.permissions)
        flag_modified(role, "permissions")
        touched = True
    perms = list(role.permissions or [])
    if data.scopes is not None or data.permissions is not None:
        role.scopes = _clean_scopes(data.scopes if data.scopes is not None else (role.scopes or {}), perms)
        flag_modified(role, "scopes")
        touched = True
    if data.field_access is not None:
        current = _role_out(role, {})["field_access"]
        role.field_access = _clean_fields(data.field_access, current)
        flag_modified(role, "field_access")
        touched = True
    if touched and role.is_system:
        role.customised = True  # start-up no longer resets it to the defaults
    db.commit()
    _clear_permission_cache(cid)
    return _role_out(role, _member_counts(db, cid))


@router.post("/{role_id}/reset")
def reset_role(role_id: int, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    """Put a system role back to the product defaults."""
    from modules.roles.models import DEFAULT_ROLE_PERMISSIONS
    cid = current_user["company_id"]
    role = _get_role(db, cid, role_id)
    if not role.is_system:
        raise BadRequest("Only system roles have defaults.")
    role.permissions = list(DEFAULT_ROLE_PERMISSIONS.get(role.role_name, []))
    role.scopes = dict(DEFAULT_SCOPES.get(role.role_name, {}))
    role.field_access = dict(DEFAULT_FIELD_ACCESS.get(role.role_name, NEW_ROLE_FIELD_ACCESS))
    role.customised = False
    db.commit()
    _clear_permission_cache(cid)
    return _role_out(role, _member_counts(db, cid))


@router.delete("/{role_id}")
def delete_role(role_id: int, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    """Delete a custom role; the people holding it lose it. System roles can't be deleted."""
    cid = current_user["company_id"]
    role = _get_role(db, cid, role_id)
    if role.is_system:
        raise BadRequest("System roles can't be deleted.")
    holders = db.query(EmployeeRole).filter(EmployeeRole.company_id == cid, EmployeeRole.role_id == role.id).count()
    audit.event(db, "role.deleted", "roles", role.id, {"holders": holders}, entity_uuid=str(role.uuid),
                entity_label=role.display_name)
    db.query(EmployeeRole).filter(EmployeeRole.company_id == cid, EmployeeRole.role_id == role.id).delete()
    db.delete(role)
    db.commit()
    _clear_permission_cache(cid)
    return {"status": "deleted"}


@router.post("/sync-defaults")
def sync_default_roles(current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    """Bring system roles that were never changed up to date with the product defaults."""
    _seed_default_roles(db, current_user["company_id"])
    return {"status": "synced"}


# ── Who holds which roles ───────────────────────────────────────────

def _roles_by_employee(db: Session, company_id: int, emp_ids: list[int]) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    if not emp_ids:
        return out
    for eid, rid in (db.query(EmployeeRole.employee_id, EmployeeRole.role_id)
                     .filter(EmployeeRole.company_id == company_id, EmployeeRole.employee_id.in_(emp_ids)).all()):
        out.setdefault(eid, []).append(rid)
    return out


@router.get("/assignments/people")
def list_assignments(q: str | None = None, role_id: int | None = None, department_id: int | None = None,
                     pages: PageParams = Depends(), current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    """Active employees with the roles they hold."""
    from modules.company.owner import owner_employee_id
    from modules.employees import service as emp_service
    from modules.employees.models import Employee
    cid = current_user["company_id"]
    _seed_default_roles(db, cid)
    roles = {r.id: r for r in db.query(Role).filter(Role.company_id == cid).all()}
    by_name = {r.role_name: r for r in roles.values()}
    query = emp_service.search(emp_service.base_query(db, cid).filter(Employee.active.is_(True)), q)
    if department_id:
        query = query.filter(Employee.department_id == department_id)
    if role_id:
        role = roles.get(role_id)
        if role is None:
            raise NotFound("Role")
        extra = db.query(EmployeeRole.employee_id).filter(EmployeeRole.company_id == cid, EmployeeRole.role_id == role_id)
        query = query.filter(or_(Employee.role == role.role_name, Employee.id.in_(extra)))
    total = query.count()
    rows = query.order_by(Employee.first_name, Employee.last_name).offset(pages.offset).limit(pages.size).all()
    extras = _roles_by_employee(db, cid, [e.id for e in rows])
    owner_id = owner_employee_id(db, cid)
    items = []
    for e in rows:
        held = []
        base = by_name.get(e.role)
        if base:
            held.append(base)
        held += [roles[i] for i in extras.get(e.id, []) if i in roles and roles[i] is not base]
        items.append({
            "id": e.id, "uuid": str(e.uuid), "name": emp_service.display_name(e), "email": e.email,
            "employee_code": e.employee_code, "profile_image_url": e.profile_image_url,
            "designation": e.designation.designation_name if e.designation else None,
            "department": e.department.department_name if e.department else None,
            "base_role": e.role, "is_owner": e.id == owner_id,
            "roles": [{"id": r.id, "display_name": r.display_name, "role_name": r.role_name, "is_system": r.is_system}
                      for r in held],
        })
    return {"items": items, "total": total, "page": pages.page, "size": pages.size}


def _set_roles(db: Session, user: dict, emp, role_ids: list[int], roles: dict[int, Role], owner_id: int | None) -> dict:
    """Give `emp` exactly these roles. Returns {"added": [...], "removed": [...]} names."""
    cid = user["company_id"]
    wanted = [roles[i] for i in dict.fromkeys(role_ids) if i in roles]
    if len(wanted) != len(set(role_ids)):
        raise BadRequest("Unknown role.")
    if emp.id == owner_id and not any(r.role_name == "admin" for r in wanted):
        raise BadRequest("The company owner always keeps the HR admin role.")
    system = [r.role_name for r in wanted if r.is_system and r.role_name in BASE_ORDER]
    base = min(system, key=BASE_ORDER.index) if system else "employee"
    extras = {r.id for r in wanted if r.role_name != base}
    before_extra = {rid for (rid,) in db.query(EmployeeRole.role_id).filter(EmployeeRole.company_id == cid,
                                                                            EmployeeRole.employee_id == emp.id).all()}
    before = {emp.role} | {roles[i].role_name for i in before_extra if i in roles}
    after = {base} | {roles[i].role_name for i in extras}
    if "admin" in (before ^ after) and user.get("role") != "admin":
        raise Forbidden("Only an HR admin can give or take away the HR admin role.")
    if emp.role != base:
        emp.role = base
    for rid in before_extra - extras:
        db.query(EmployeeRole).filter(EmployeeRole.company_id == cid, EmployeeRole.employee_id == emp.id,
                                      EmployeeRole.role_id == rid).delete()
    for rid in extras - before_extra:
        db.add(EmployeeRole(company_id=cid, employee_id=emp.id, role_id=rid))
    labels = {r.role_name: r.display_name for r in roles.values()}
    change = {"added": sorted(labels.get(n, n) for n in after - before),
              "removed": sorted(labels.get(n, n) for n in before - after)}
    if change["added"] or change["removed"]:
        from modules.employees import service as emp_service
        audit.event(db, "roles.assigned", "employees", emp.id, change, entity_uuid=str(emp.uuid),
                    entity_label=emp_service.display_name(emp))
    return change


@router.put("/assignments/{employee_id}")
def assign_roles(employee_id: int, data: AssignIn, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    """Set every role one employee holds (at least one)."""
    from modules.company.owner import owner_employee_id
    from modules.employees.models import Employee
    cid = current_user["company_id"]
    emp = db.query(Employee).filter(Employee.company_id == cid, Employee.id == employee_id).first()
    if emp is None:
        raise NotFound("Employee")
    roles = {r.id: r for r in db.query(Role).filter(Role.company_id == cid).all()}
    change = _set_roles(db, current_user, emp, data.role_ids, roles, owner_employee_id(db, cid))
    db.commit()
    _clear_permission_cache(cid)
    return change


@router.post("/assignments/bulk")
def bulk_assign(data: BulkAssignIn, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    """Give one role to (or take it from) many employees at once."""
    from modules.company.owner import owner_employee_id
    from modules.employees.models import Employee
    cid = current_user["company_id"]
    roles = {r.id: r for r in db.query(Role).filter(Role.company_id == cid).all()}
    role = roles.get(data.role_id)
    if role is None:
        raise NotFound("Role")
    owner_id = owner_employee_id(db, cid)
    emps = db.query(Employee).filter(Employee.company_id == cid, Employee.id.in_(data.employee_ids)).all()
    extras = _roles_by_employee(db, cid, [e.id for e in emps])
    by_name = {r.role_name: r for r in roles.values()}
    changed, skipped = 0, []
    for e in emps:
        held = ([by_name[e.role].id] if e.role in by_name else []) + extras.get(e.id, [])
        if data.action == "add":
            wanted = held + ([role.id] if role.id not in held else [])
        else:
            wanted = [i for i in held if i != role.id] or [by_name["employee"].id]
        try:
            out = _set_roles(db, current_user, e, wanted, roles, owner_id)
        except (BadRequest, Forbidden) as exc:
            skipped.append({"id": e.id, "reason": str(getattr(exc, "detail", exc))})
            continue
        changed += 1 if (out["added"] or out["removed"]) else 0
    db.commit()
    _clear_permission_cache(cid)
    return {"changed": changed, "skipped": skipped}
