import json
import random
import uuid as uuid_mod
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core.config import get_settings
from core import audit, context
from core.db import bypass_rls, get_db, set_tenant
from core.security import (
    DISABLED_DETAIL,
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)
from modules.company.models import Company
from modules.employees.models import Employee
from modules.auth.schemas import (
    ForgotPasswordRequest,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenResponse,
    TimezoneUpdate,
    UserProfile,
)
from modules.auth import login_guard
from core.cache import get_redis
from modules.company.slugs import unique_slug

settings = get_settings()

router = APIRouter(prefix="/auth", tags=["Auth"])


# ── Helpers ──────────────────────────────────────────────────────────

def _company_from_header(request: Request, db: Session) -> tuple[str | None, int | None]:
    """Read the X-Company-Slug header (sent from a company's own address).
    Returns (slug, company_id) or (None, None) on the main site."""
    slug = request.headers.get("x-company-slug")
    if not slug:
        return None, None
    company_id = db.query(Company.id).filter(Company.slug == slug).scalar()
    if company_id is None:
        raise HTTPException(status_code=404, detail="Company not found")
    set_tenant(db, company_id)  # everything after this sees only this company's rows
    return slug, company_id


def _owner_name(company: Company) -> str:
    return (company.config or {}).get("owner_name", "") if isinstance(company.config, dict) else ""


def _owner_token(company: Company) -> str:
    # The owner keeps full admin rights and also acts as their own employee record
    # (company.owner_employee_id) for self-service.
    return create_access_token({"sub": company.email, "company_id": company.id, "role": "admin",
                                "employee_id": company.owner_employee_id, "owner": True,
                                "name": _owner_name(company) or company.email})


def _employee_token(emp: Employee) -> str:
    return create_access_token({"sub": emp.email, "company_id": emp.company_id, "role": emp.role, "employee_id": emp.id,
                                "name": f"{emp.first_name} {emp.last_name}".strip()})


def _owner_payload(company: Company) -> dict:
    return {
        "email": company.email,
        "role": "admin",
        "company_id": company.id,
        "company_name": company.company_name,
        "full_name": _owner_name(company),
        "slug": company.slug,
        "employee_id": company.owner_employee_id,
        "is_owner": True,
    }


def _employee_payload(emp: Employee, company: Company | None) -> dict:
    return {
        "email": emp.email,
        "role": emp.role,
        "company_id": emp.company_id,
        "company_name": company.company_name if company else "",
        "full_name": f"{emp.first_name} {emp.last_name}".strip(),
        "slug": company.slug if company else None,
        "employee_id": emp.id,
        "employee_uuid": str(emp.uuid),
    }


def _signed_in(db: Session, company: Company | None, emp: Employee | None, method: str) -> None:
    """Record a successful sign-in in the audit log."""
    ctx = context.current()
    company_id = emp.company_id if emp else company.id
    set_tenant(db, company_id)
    ctx.employee_id = emp.id if emp else None
    ctx.actor_type = "employee" if emp else "owner"
    ctx.actor_name = f"{emp.first_name} {emp.last_name}".strip() if emp else (_owner_name(company) or company.email)
    audit.event(db, "auth.signed_in", "employees" if emp else "companies", emp.id if emp else company.id,
                {"method": method}, entity_label=ctx.actor_name)
    db.commit()


def _start_trial(db: Session, company: Company) -> None:
    from modules.platform.subscription import trial_days
    company.trial_ends_at = datetime.now(timezone.utc) + timedelta(days=trial_days(db))


# ── Sign up and sign in ──────────────────────────────────────────────

@router.post("/register", response_model=TokenResponse)
def register(req: RegisterRequest, request: Request, db: Session = Depends(get_db)):
    """Create a new company and sign its owner in. Only on the main site."""
    if request.headers.get("x-company-slug"):
        raise HTTPException(status_code=403, detail="New companies can only sign up on the main site.")

    if db.query(Company.id).filter(Company.email == req.email).first():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email already registered")

    from modules.platform.router import resolve_referral

    company = Company(
        company_name=req.company_name,
        slug=unique_slug(db, req.company_name),
        email=req.email,
        password=hash_password(req.password),
        industry=req.industry,
        config={"owner_name": req.full_name, "plan": "standard"},
        referred_by_id=resolve_referral(db, req.ref_code),
    )
    _start_trial(db, company)
    db.add(company)
    db.commit()
    db.refresh(company)

    from seed.seed_data import setup_company
    set_tenant(db, company.id)
    setup_company(db, company.id)
    db.refresh(company)

    return TokenResponse(access_token=_owner_token(company), user=_owner_payload(company))


@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, request: Request, db: Session = Depends(get_db)):
    """Sign in, refusing further attempts after repeated failures."""
    login_guard.check("company", req.email, request)
    try:
        result = _login(req, request, db)
    except HTTPException as exc:
        if exc.status_code == 401:
            login_guard.record_failure("company", req.email, request)
        raise
    login_guard.record_success("company", req.email, request)
    return result


def _login(req: LoginRequest, request: Request, db: Session):
    _, header_company_id = _company_from_header(request, db)

    if header_company_id is not None:
        # On a company's own address: the owner or one of its employees.
        company = db.query(Company).filter(Company.id == header_company_id, Company.email == req.email).first()
        if company and verify_password(req.password, company.password):
            if not company.active:
                raise HTTPException(status_code=403, detail=DISABLED_DETAIL)
            _signed_in(db, company, None, "password")
            return TokenResponse(access_token=_owner_token(company), user=_owner_payload(company))

        emp = db.query(Employee).filter(Employee.company_id == header_company_id, Employee.email == req.email).first()
        if emp and emp.active and verify_password(req.password, emp.password):
            company = db.query(Company).filter(Company.id == emp.company_id).first()
            if company and not company.active:
                raise HTTPException(status_code=403, detail=DISABLED_DETAIL)
            _signed_in(db, company, emp, "password")
            return TokenResponse(access_token=_employee_token(emp), user=_employee_payload(emp, company))

        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Main site: company owners only.
    company = db.query(Company).filter(Company.email == req.email).first()
    if company and verify_password(req.password, company.password):
        if not company.active:
            raise HTTPException(status_code=403, detail=DISABLED_DETAIL)
        _signed_in(db, company, None, "password")
        return TokenResponse(access_token=_owner_token(company), user=_owner_payload(company))

    # An employee signing in on the main site: find the company whose employee record matches
    # the email and password. One match signs them in (the web app then moves them to their
    # company's address); several matches ask them to use their company's address.
    with bypass_rls(db):
        candidates = db.query(Employee).filter(Employee.email == req.email, Employee.active.is_(True)).all()
        matches = [e for e in candidates if verify_password(req.password, e.password)]
        companies = {c.id: c for c in db.query(Company).filter(Company.id.in_([e.company_id for e in matches])).all()} if matches else {}
    matches = [e for e in matches if companies.get(e.company_id) and companies[e.company_id].active]
    if len(matches) == 1:
        emp = matches[0]
        company = companies[emp.company_id]
        _signed_in(db, company, emp, "password")
        return TokenResponse(access_token=_employee_token(emp), user=_employee_payload(emp, company))
    if len(matches) > 1:
        names = ", ".join(sorted(companies[e.company_id].company_name for e in matches))
        raise HTTPException(status_code=409, detail=f"This email is used at more than one company ({names}). Sign in from your company's own address.")

    raise HTTPException(status_code=401, detail="Invalid credentials")


@router.get("/me", response_model=UserProfile)
def get_me(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    company = db.query(Company).filter(Company.id == current_user["company_id"]).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    config = company.config if isinstance(company.config, dict) else {}
    full_name = config.get("owner_name", "")
    employee_uuid = None
    own_tz = config.get("owner_timezone")
    if current_user.get("employee_id"):
        emp = db.query(Employee).filter(Employee.id == current_user["employee_id"]).first()
        if not current_user.get("is_owner"):
            own_tz = None
        if emp:
            if not (current_user.get("is_owner") and full_name):
                full_name = f"{emp.first_name} {emp.last_name}".strip()
            employee_uuid = str(emp.uuid)
            if not current_user.get("is_owner"):
                own_tz = emp.timezone
    from core.timeutil import effective_timezone, is_valid_timezone
    company_tz = effective_timezone(None, company)
    tz_source = "own" if is_valid_timezone(own_tz) else "company"

    from modules.platform.subscription import subscription_state
    return UserProfile(
        email=current_user["email"],
        role=current_user["role"],
        company_id=company.id,
        company_name=company.company_name,
        full_name=full_name,
        slug=company.slug,
        setup_complete=bool((company.company_name or "").strip() and company.slug),
        picture=config.get("owner_picture", ""),
        employee_id=current_user.get("employee_id"),
        employee_uuid=employee_uuid,
        is_owner=bool(current_user.get("is_owner")),
        features=config.get("features") if isinstance(config.get("features"), dict) else {},
        subscription=subscription_state(company),
        timezone=own_tz if tz_source == "own" else company_tz,
        timezone_source=tz_source,
        company_timezone=company_tz,
    )


@router.put("/me/timezone")
def update_my_timezone(req: TimezoneUpdate, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Set the signed-in person's own timezone (e.g. detected from the browser). Empty
    means follow the company's timezone."""
    from core.timeutil import is_valid_timezone
    tz = (req.timezone or "").strip() or None
    if tz and not is_valid_timezone(tz):
        raise HTTPException(status_code=400, detail="That timezone isn't recognised. Pick one from the list.")
    if current_user.get("employee_id") and not current_user.get("is_owner"):
        emp = db.query(Employee).filter(Employee.id == current_user["employee_id"]).first()
        if not emp:
            raise HTTPException(status_code=404, detail="Employee not found")
        emp.timezone = tz
    else:
        company = db.query(Company).filter(Company.id == current_user["company_id"]).first()
        config = dict(company.config) if isinstance(company.config, dict) else {}
        if tz:
            config["owner_timezone"] = tz
        else:
            config.pop("owner_timezone", None)
        company.config = config
    db.commit()
    return {"timezone": tz}


# ── Password reset ───────────────────────────────────────────────────

def _reset_key(email: str, slug: str | None) -> str:
    """Reset codes are kept per company address so two companies never collide."""
    return f"reset:{slug or '_main'}:{email}"


@router.post("/forgot-password")
def forgot_password(req: ForgotPasswordRequest, request: Request, db: Session = Depends(get_db)):
    slug, header_company_id = _company_from_header(request, db)
    r = get_redis()
    generic = {"message": "If that email is registered, a reset code has been sent. It is valid for 15 minutes."}

    # At most 3 code requests per email per 15 minutes.
    sends_key = f"pwreset:sends:{(slug or '-')}:{req.email.lower()}"
    sends = r.incr(sends_key)
    if sends == 1:
        r.expire(sends_key, 900)
    if sends > 3:
        return generic

    if header_company_id is not None:
        exists = bool(
            db.query(Company.id).filter(Company.id == header_company_id, Company.email == req.email).scalar()
            or db.query(Employee.id).filter(Employee.company_id == header_company_id, Employee.email == req.email).scalar()
        )
    else:
        exists = bool(db.query(Company.id).filter(Company.email == req.email).scalar())
    if not exists:
        return generic  # never reveal which emails have accounts

    code = f"{random.randint(100000, 999999)}"
    r.setex(_reset_key(req.email, slug), 900, code)
    r.delete(f"pwreset:tries:{(slug or '-')}:{req.email.lower()}")

    from core.email import email_configured, send_email
    sent = send_email(
        req.email,
        "Your password reset code",
        f"Your password reset code is {code}. It expires in 15 minutes.\n\nIf you did not ask for this, ignore this email.",
    )
    if not sent and not email_configured() and not settings.is_production:
        # Development only: no mail server, so hand the code back for testing.
        return {**generic, "dev_code": code}
    return generic


@router.post("/reset-password")
def reset_password(req: ResetPasswordRequest, request: Request, db: Session = Depends(get_db)):
    slug, header_company_id = _company_from_header(request, db)
    r = get_redis()
    stored = r.get(_reset_key(req.email, slug))
    if not stored:
        raise HTTPException(status_code=400, detail="Reset code has expired or is invalid. Please request a new one.")

    # 5 wrong guesses burn the code (a 6-digit code must not be brute-forceable).
    tries_key = f"pwreset:tries:{(slug or '-')}:{req.email.lower()}"
    import hmac
    if not hmac.compare_digest(str(stored), str(req.code).strip()):
        tries = r.incr(tries_key)
        r.expire(tries_key, 900)
        if tries >= 5:
            r.delete(_reset_key(req.email, slug))
            raise HTTPException(status_code=400, detail="Too many wrong attempts. Please request a new code.")
        raise HTTPException(status_code=400, detail="Invalid reset code")
    if len(req.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    hashed = hash_password(req.new_password)
    if header_company_id is not None:
        account = (
            db.query(Company).filter(Company.id == header_company_id, Company.email == req.email).first()
            or db.query(Employee).filter(Employee.company_id == header_company_id, Employee.email == req.email).first()
        )
    else:
        account = db.query(Company).filter(Company.email == req.email).first()
    if not account:
        raise HTTPException(status_code=404, detail="No account found with this email address")
    account.password = hashed
    if isinstance(account, Company):
        from modules.company.owner import sync_owner_password
        sync_owner_password(db, account.id, hashed)
    db.commit()
    r.delete(_reset_key(req.email, slug))
    return {"message": "Password reset successfully"}


@router.post("/change-password")
def change_password(payload: dict, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Change the signed-in person's own password."""
    current = payload.get("current_password") or ""
    new = payload.get("new_password") or ""
    if len(new) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if current_user.get("employee_id") and not current_user.get("is_owner"):
        account = db.query(Employee).filter(Employee.id == current_user["employee_id"]).first()
    else:
        account = db.query(Company).filter(Company.id == current_user["company_id"]).first()
    if not account or not verify_password(current, account.password):
        raise HTTPException(status_code=400, detail="Your current password is not correct")
    account.password = hash_password(new)
    if current_user.get("is_owner"):
        from modules.company.owner import sync_owner_password
        sync_owner_password(db, current_user["company_id"], account.password)
    db.commit()
    return {"detail": "Password changed"}


# ── View as an employee (company owner only) ─────────────────────────

@router.post("/impersonate-employee/{employee_id}")
def impersonate_employee(
    employee_id: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Let the HR admin see the app as one of their employees."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Only the HR admin can do this")
    emp = db.query(Employee).filter(Employee.id == employee_id, Employee.company_id == current_user["company_id"]).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    company = db.query(Company).filter(Company.id == emp.company_id).first()
    return {"access_token": _employee_token(emp), "token_type": "bearer", "user": _employee_payload(emp, company)}


# Google and Microsoft sign-in live in modules/sso (no new companies from a sign-in; only
# existing owners and employees, matched on the verified email).


# ── Profile ──────────────────────────────────────────────────────────

@router.put("/update-profile")
def update_profile(payload: dict, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Change the signed-in person's display name."""
    full_name = (payload.get("full_name") or "").strip()
    if not full_name:
        raise HTTPException(status_code=400, detail="Name is required")

    if current_user.get("employee_id"):
        emp = db.query(Employee).filter(Employee.id == current_user["employee_id"]).first()
        if emp:
            first, _, last = full_name.partition(" ")
            emp.first_name, emp.last_name = first, last
    if not current_user.get("employee_id") or current_user.get("is_owner"):
        company = db.query(Company).filter(Company.id == current_user["company_id"]).first()
        if company:
            config = dict(company.config) if isinstance(company.config, dict) else {}
            config["owner_name"] = full_name
            company.config = config
            flag_modified(company, "config")
    db.commit()
    return {"detail": "Profile updated"}


# ── Mobile sign-in by QR code ────────────────────────────────────────

@router.post("/mobile/generate-qr-code")
def generate_mobile_qr_code(current_user: dict = Depends(get_current_user)):
    """A one-time code, valid for 5 minutes, shown as a QR code for the mobile app."""
    code = str(uuid_mod.uuid4())
    get_redis().setex(f"mobile_qr:{code}", 300, json.dumps({
        "email": current_user["email"],
        "company_id": current_user["company_id"],
        "role": current_user.get("role", "employee"),
        "employee_id": current_user.get("employee_id"),
        "owner": bool(current_user.get("is_owner")),
    }))
    return {"code": code, "expires_in": 300}


@router.post("/mobile/verify-qr-code")
def verify_mobile_qr_code(payload: dict, db: Session = Depends(get_db)):
    """Swap a scanned QR code for a sign-in token for the mobile app."""
    code = payload.get("code", "")
    if not code:
        raise HTTPException(status_code=400, detail="Code is required")
    r = get_redis()
    raw = r.get(f"mobile_qr:{code}")
    if not raw:
        raise HTTPException(status_code=401, detail="Invalid or expired QR code")
    r.delete(f"mobile_qr:{code}")  # one use only
    data = json.loads(raw)
    set_tenant(db, data["company_id"])

    token = create_access_token({
        "sub": data["email"],
        "company_id": data["company_id"],
        "role": data["role"],
        "employee_id": data.get("employee_id"),
        "owner": bool(data.get("owner")),
    })
    company = db.query(Company).filter(Company.id == data["company_id"]).first()
    full_name = _owner_name(company) if company else ""
    if data.get("employee_id") and not data.get("owner"):
        emp = db.query(Employee).filter(Employee.id == data["employee_id"]).first()
        if emp:
            full_name = f"{emp.first_name} {emp.last_name}".strip()
    return {
        "access_token": token,
        "user": {
            "email": data["email"],
            "role": data["role"],
            "company_id": data["company_id"],
            "company_name": company.company_name if company else "",
            "full_name": full_name,
        },
    }
