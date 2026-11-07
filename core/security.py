from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from core import context
from core.config import get_settings
from core.db import get_db, set_tenant
from core.cache import get_redis

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)
settings = get_settings()


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


# company_id -> (status, checked_at). Short cache so each request doesn't hit the database;
# disabling a company takes effect within this many seconds (immediately in the same process).
_ACTIVE_TTL = 20
_active_cache: dict[int, tuple[str, float]] = {}

DISABLED_DETAIL = "This institution has been disabled. Please contact Campusly support."
GONE_DETAIL = "This company does not exist."
WRONG_COMPANY_DETAIL = "Token does not belong to this company"


def company_status(db: Session, company_id: int) -> str:
    """'active', 'disabled' or 'gone' (removed)."""
    import time
    hit = _active_cache.get(company_id)
    if hit and time.monotonic() - hit[1] < _ACTIVE_TTL:
        return hit[0]
    from modules.company.models import Company
    row = db.query(Company.active).filter(Company.id == company_id).first()
    status_ = "gone" if row is None else ("active" if row[0] else "disabled")
    _active_cache[company_id] = (status_, time.monotonic())
    return status_


def forget_company_status(company_id: int) -> None:
    _active_cache.pop(company_id, None)


def get_current_user(
    request: Request,
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
):
    """The signed-in person: the company owner (role "admin", no employee_id) or an employee."""
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        if payload.get("scope") == "platform":
            raise HTTPException(status_code=401, detail="Invalid token")
        email: str = payload.get("sub")
        company_id: int = payload.get("company_id")
        role: str = payload.get("role")
        employee_id = payload.get("employee_id")
        portal_id = payload.get("portal_id")
        # The company owner: full admin rights, and (since owners have their own employee
        # record) usually an employee_id too. Older tokens mark the owner by having none.
        is_owner = bool(payload.get("owner")) or (role == "admin" and not employee_id)
        if email is None or company_id is None:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

    ctx = context.current()
    ctx.company_id = company_id
    ctx.employee_id = employee_id
    ctx.role = role
    ctx.portal_account_id = portal_id
    ctx.actor_type = "owner" if is_owner else (role if portal_id else "employee")
    ctx.actor_name = payload.get("name") or email
    set_tenant(db, company_id)

    state = company_status(db, company_id)
    if state == "gone":
        raise HTTPException(status_code=401, detail=GONE_DETAIL)
    if state == "disabled":
        raise HTTPException(status_code=403, detail=DISABLED_DETAIL)

    company_slug = request.headers.get("x-company-slug")
    if company_slug:
        from modules.company.models import Company
        slug_company_id = db.query(Company.id).filter(Company.slug == company_slug).scalar()
        if slug_company_id is None:
            raise HTTPException(status_code=404, detail=GONE_DETAIL)
        if slug_company_id != company_id:
            raise HTTPException(status_code=403, detail=WRONG_COMPANY_DETAIL)

    if portal_id:
        if not _portal_account_active(db, portal_id):
            raise HTTPException(status_code=401, detail="This sign-in has been switched off.")
        if not portal_path_allowed(request.url.path):
            raise HTTPException(status_code=403, detail="This page is not available in the parent and student portal.")

    return {
        "email": email,
        "company_id": company_id,
        "role": role,
        "employee_id": employee_id,
        "is_owner": is_owner,
        # Parent / student portal sign-ins (None for staff).
        "portal_id": portal_id,
        "student_id": payload.get("student_id"),
        "guardian_id": payload.get("guardian_id"),
    }


# API areas parent and student sign-ins may call at all (each endpoint still checks access).
# Modules add theirs from hooks.py with register_portal_prefix("/fees").
PORTAL_PREFIXES: set[str] = {"/auth", "/academics/profile", "/academics/section-options", "/students/mine",
                             "/students/", "/notifications", "/roles/my-permissions", "/workflow/my", "/portal"}


def register_portal_prefix(*prefixes: str) -> None:
    for p in prefixes:
        PORTAL_PREFIXES.add(p if p.startswith("/") else "/" + p)


def portal_path_allowed(path: str) -> bool:
    rest = path.split("/api/v1", 1)[1] if "/api/v1" in path else path
    return any(rest == p or rest.startswith(p.rstrip("/") + "/") or (p.endswith("/") and rest.startswith(p))
               for p in PORTAL_PREFIXES)


_portal_cache: dict[int, tuple[bool, float]] = {}


def _portal_account_active(db: Session, portal_id: int) -> bool:
    import time
    hit = _portal_cache.get(portal_id)
    if hit and time.monotonic() - hit[1] < _ACTIVE_TTL:
        return hit[0]
    from modules.students.models import PortalAccount
    row = db.query(PortalAccount.active).filter(PortalAccount.id == portal_id).first()
    ok = bool(row and row[0])
    _portal_cache[portal_id] = (ok, time.monotonic())
    return ok


def require_portal(*kinds: str):
    """Dependency: a parent and/or student portal sign-in, e.g. Depends(require_portal("parent", "student"))."""
    def checker(current_user: dict = Depends(get_current_user)):
        if not current_user.get("portal_id") or current_user["role"] not in kinds:
            raise HTTPException(status_code=403, detail="This page is for parents and students.")
        return current_user
    return checker


def require_admin(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


def _get_role_permissions(company_id: int, role: str, db: Session, employee_id: int | None = None) -> list[str]:
    """Permissions of the signed-in person (or of `role`): the base role's permissions from the
    company's roles table, plus those of any extra roles the employee holds. The HR admin
    always gets all. When `employee_id` is not given and `role` is the signed-in person's
    role, their extra roles are included."""
    from modules.roles.models import ALL_PERMISSIONS
    if role == "admin":
        return list(ALL_PERMISSIONS.keys())
    if employee_id is None:
        ctx = context.current()
        if ctx.employee_id and ctx.role == role and ctx.company_id == company_id:
            employee_id = ctx.employee_id
    perms = _base_role_permissions(company_id, role, db)
    if not employee_id:
        return perms
    extra = _extra_role_names(company_id, employee_id, db)
    if not extra:
        return perms
    if "admin" in extra:
        return list(ALL_PERMISSIONS.keys())
    merged = list(perms)
    seen = set(merged)
    for name in extra:
        for p in _base_role_permissions(company_id, name, db):
            if p not in seen:
                seen.add(p)
                merged.append(p)
    return merged


def _extra_role_names(company_id: int, employee_id: int, db: Session) -> list[str]:
    """Names of the extra roles an employee holds (cached briefly)."""
    import json
    from modules.roles.models import EmployeeRole, Role
    cache_key = f"perms:{company_id}:emp:{employee_id}"
    r = None
    try:
        r = get_redis()
        cached = r.get(cache_key)
        if cached is not None:
            return json.loads(cached)
    except Exception:
        r = None
    names = [n for (n,) in db.query(Role.role_name).join(EmployeeRole, EmployeeRole.role_id == Role.id)
             .filter(EmployeeRole.company_id == company_id, EmployeeRole.employee_id == employee_id).all()]
    if r is not None:
        try:
            r.setex(cache_key, 300, json.dumps(names))
        except Exception:
            pass
    return names


def _base_role_permissions(company_id: int, role: str, db: Session) -> list[str]:
    from modules.roles.models import ALL_PERMISSIONS, DEFAULT_ROLE_PERMISSIONS, Role
    if role == "admin":
        return list(ALL_PERMISSIONS.keys())

    import json
    r = None
    cache_key = f"perms:{company_id}:{role}"
    try:
        r = get_redis()
        cached = r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        r = None

    role_obj = db.query(Role).filter(Role.company_id == company_id, Role.role_name == role).first()
    if role_obj and isinstance(role_obj.permissions, list):
        perms = role_obj.permissions
        if r is not None:
            try:
                r.setex(cache_key, 300, json.dumps(perms))
            except Exception:
                pass
        return perms
    return DEFAULT_ROLE_PERMISSIONS.get(role, [])


def require_permission(*permissions: str):
    """Dependency factory: require the signed-in person to have every listed permission.
    Usage: Depends(require_permission("people.employees.manage"))
    """
    def checker(
        current_user: dict = Depends(get_current_user),
        db: Session = Depends(get_db),
    ):
        perms = _get_role_permissions(current_user["company_id"], current_user["role"], db)
        for perm in permissions:
            if perm not in perms:
                raise HTTPException(status_code=403, detail=f"Permission denied: {perm}")
        return current_user
    return checker
