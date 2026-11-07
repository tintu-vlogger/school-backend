"""Authentication for platform accounts (admins and partners).

Separate from company auth on purpose:
- tokens carry scope="platform" and no company_id, so they are rejected by the
  company dependency and company tokens are rejected here;
- tokens carry the user's token_version, so a password change or deactivation
  logs that person out everywhere at once;
- failed logins lock the account for a while, and every login is rate limited by IP.
"""
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from core.config import get_settings
from core import context
from core.db import enable_bypass, get_db
from modules.platform.models import PlatformUser

settings = get_settings()
_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/platform/auth/login", auto_error=False)

PLATFORM_TOKEN_HOURS = 8
MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 15


def create_platform_token(user: PlatformUser) -> str:
    payload = {
        "scope": "platform",
        "sub": user.email,
        "pid": user.id,
        "role": user.role,
        "tv": user.token_version,
        "exp": datetime.now(timezone.utc) + timedelta(hours=PLATFORM_TOKEN_HOURS),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def current_platform_user(token: str = Depends(_scheme), db: Session = Depends(get_db)) -> PlatformUser:
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    if payload.get("scope") != "platform" or not payload.get("pid"):
        raise HTTPException(status_code=401, detail="Invalid token")
    # Platform screens work across companies: row-level security is bypassed, and every
    # change is still audited with the platform user as the actor.
    ctx = context.current()
    ctx.platform_user_id = payload["pid"]
    ctx.actor_type = "platform"
    ctx.actor_name = payload.get("sub")
    ctx.bypass_rls = True
    enable_bypass(db)
    user = db.query(PlatformUser).filter(PlatformUser.id == payload["pid"]).first()
    if not user or not user.is_active or user.token_version != payload.get("tv"):
        raise HTTPException(status_code=401, detail="Session expired")
    return user


def platform_admin(user: PlatformUser = Depends(current_platform_user)) -> PlatformUser:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def platform_executive(user: PlatformUser = Depends(current_platform_user)) -> PlatformUser:
    if user.role != "executive":
        raise HTTPException(status_code=403, detail="Executive access required")
    return user


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
