"""Google and Microsoft sign-in.

Public (sign-in page)
  GET  /auth/sso/options                 which buttons to show (X-Company-Slug on a company address)
  GET  /auth/sso/{provider}/start        browser goes here; sets a short-lived cookie and moves on
                                         to Google or Microsoft (?slug=<company address>)
  GET  /auth/sso/{provider}/callback     the provider sends the browser back here; we find the
                                         person and move them to <company address>/auth/callback?code=…
  POST /auth/sso/exchange                the web app swaps that one-time code for a session token
Company settings (settings.company.manage)
  GET/PUT /sso/settings                  switch providers on or off, allowed email domains
Platform admin
  GET  /platform/admin/sso               OAuth apps and the redirect address to register
  PUT  /platform/admin/sso/{provider}    save client id, secret (kept encrypted), tenant, on/off

The flow follows the clinic product's Google sign-in (the app builds the provider address, the
provider returns to the API, the API sends the browser to the company's address), with these
changes: a signed `state` tied to a cookie (no forged sign-ins), a one-time code instead of the
session token in the address, verified emails only, and no new companies from a sign-in.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core import audit
from core.config import get_settings
from core.db import get_db, set_tenant
from core.errors import BadRequest, NotFound
from core.security import require_permission
from modules.company.models import Company
from modules.platform.models import PlatformUser
from modules.platform.security import platform_admin
from modules.sso import providers, service
from modules.sso.providers import PROVIDERS, SsoError

settings = get_settings()

router = APIRouter(prefix="/auth/sso", tags=["Sign-in with Google and Microsoft"])
company_router = APIRouter(prefix="/sso", tags=["Sign-in with Google and Microsoft"])
platform_router = APIRouter(prefix="/platform/admin/sso", tags=["Platform sign-in providers"])

COOKIE = "hrms_sso"
STATE_MINUTES = 10
CODE_SECONDS = 120


# ── Helpers ──────────────────────────────────────────────────────────

def _main_url() -> str:
    return (settings.FRONTEND_URL or "http://localhost:3000").rstrip("/")


def _company_url(slug: str | None) -> str:
    main = _main_url()
    if not slug:
        return main
    scheme, _, host = main.partition("://")
    return f"{scheme}://{slug}.{host}" if host else main


def _provider(provider: str) -> str:
    if provider not in PROVIDERS:
        raise NotFound("Sign-in provider")
    return provider


def _fail(slug: str | None, code: str, **extra) -> RedirectResponse:
    q = {"sso_error": code, **{k: v for k, v in extra.items() if v}}
    resp = RedirectResponse(url=f"{_company_url(slug)}/login?{urlencode(q)}", status_code=302)
    resp.delete_cookie(COOKIE, path="/api/v1/auth/sso")
    return resp


def _company_by_slug(db: Session, slug: str | None) -> Company | None:
    if not slug:
        return None
    company = db.query(Company).filter(Company.slug == slug).first()
    if company is None:
        raise NotFound("Company")
    return company


def _sign_state(provider: str, slug: str | None, nonce: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=STATE_MINUTES)
    return jwt.encode({"typ": "sso_state", "p": provider, "s": slug or "", "n": nonce, "exp": exp},
                      settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def _read_state(state: str | None) -> dict | None:
    if not state:
        return None
    try:
        data = jwt.decode(state, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except JWTError:
        return None
    return data if data.get("typ") == "sso_state" else None


def _one_time_code(token: str, user: dict, provider: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(seconds=CODE_SECONDS)
    return jwt.encode({"typ": "sso_code", "jti": secrets.token_urlsafe(12), "t": token, "u": user, "p": provider,
                       "exp": exp}, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def _use_once(jti: str) -> bool:
    """True the first time a code is used. Without Redis the short expiry is the only limit."""
    try:
        from core.cache import get_redis
        return bool(get_redis().set(f"sso:code:{jti}", "1", nx=True, ex=CODE_SECONDS + 30))
    except Exception:
        return True


# ── Sign-in page ─────────────────────────────────────────────────────

@router.get("/options")
def options(request: Request, db: Session = Depends(get_db)):
    company = _company_by_slug(db, request.headers.get("x-company-slug"))
    if company is not None:
        set_tenant(db, company.id)
    return service.options_for(db, company.id if company else None)


@router.get("/{provider}/start")
def start(provider: str, slug: str | None = None, hint: str | None = None, db: Session = Depends(get_db)):
    provider = _provider(provider)
    slug = (slug or "").strip().lower() or None
    cfg = service.provider_config(db, provider)
    if not cfg.ready:
        return _fail(slug, "not_configured", provider=provider)
    if slug:
        company = _company_by_slug(db, slug)
        set_tenant(db, company.id)
        if not service.company_enabled(service.company_row(db, company.id), provider):
            return _fail(slug, "not_enabled", provider=provider)
    nonce = secrets.token_urlsafe(24)
    url = providers.authorize_url(provider, cfg.client_id, service.redirect_uri(provider), _sign_state(provider, slug, nonce),
                                  cfg.tenant, (hint or "").strip()[:200] or None)
    resp = RedirectResponse(url=url, status_code=302)
    resp.set_cookie(COOKIE, nonce, max_age=STATE_MINUTES * 60, httponly=True, samesite="lax",
                    secure=settings.BACKEND_URL.startswith("https"), path="/api/v1/auth/sso")
    return resp


@router.get("/{provider}/callback")
def callback(provider: str, request: Request, code: str | None = None, state: str | None = None,
             error: str | None = None, db: Session = Depends(get_db)):
    provider = _provider(provider)
    st = _read_state(state)
    slug = (st or {}).get("s") or None
    if error:
        return _fail(slug, "cancelled" if error == "access_denied" else "provider_error", provider=provider)
    if not st or st.get("p") != provider or not code:
        return _fail(slug, "expired", provider=provider)
    if not secrets.compare_digest(request.cookies.get(COOKIE) or "", st.get("n") or "-"):
        return _fail(slug, "expired", provider=provider)
    cfg = service.provider_config(db, provider)
    if not cfg.ready:
        return _fail(slug, "not_configured", provider=provider)
    try:
        ident = providers.fetch_identity(provider, code, service.redirect_uri(provider), cfg.client_id, cfg.client_secret,
                                         cfg.tenant)
    except SsoError as exc:
        return _fail(slug, exc.code, provider=provider)
    if not ident.email or not ident.email_verified:
        return _fail(slug, "email_not_verified", provider=provider)

    company = _company_by_slug(db, slug) if slug else None
    matches, reason = service.find_accounts(db, provider, ident.email, company.id if company else None)
    if not matches:
        return _fail(slug, reason or "no_account", provider=provider, email=ident.email)
    if len(matches) > 1:
        names = ", ".join(m.company.company_name for m in matches[:6])
        return _fail(None, "many_companies", provider=provider, companies=names)

    from modules.auth.router import _employee_payload, _employee_token, _owner_payload, _owner_token, _signed_in
    m = matches[0]
    set_tenant(db, m.company.id)
    if m.employee is None:
        token, user = _owner_token(m.company), _owner_payload(m.company)
        _signed_in(db, m.company, None, provider)
    else:
        token, user = _employee_token(m.employee), _employee_payload(m.employee, m.company)
        _signed_in(db, m.company, m.employee, provider)
    target = _company_url(m.company.slug)
    resp = RedirectResponse(url=f"{target}/auth/callback?code={quote(_one_time_code(token, user, provider))}", status_code=302)
    resp.delete_cookie(COOKIE, path="/api/v1/auth/sso")
    return resp


class ExchangeIn(BaseModel):
    code: str = Field(min_length=10, max_length=4000)


@router.post("/exchange")
def exchange(body: ExchangeIn):
    try:
        data = jwt.decode(body.code, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except JWTError:
        raise BadRequest("This sign-in link has expired. Please sign in again.")
    if data.get("typ") != "sso_code" or not _use_once(data.get("jti") or ""):
        raise BadRequest("This sign-in link has expired. Please sign in again.")
    return {"access_token": data["t"], "token_type": "bearer", "user": data.get("u") or {}}


# ── Company settings ─────────────────────────────────────────────────

MANAGE = require_permission("settings.company.manage")


class CompanySsoIn(BaseModel):
    google_enabled: bool | None = None
    microsoft_enabled: bool | None = None
    allowed_domains: list[str] | None = Field(default=None, max_length=20)


@company_router.get("/settings")
def company_settings(user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    return service.company_out(db, user["company_id"])


@company_router.put("/settings")
def save_company_settings(body: CompanySsoIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    out = service.save_company(db, user["company_id"], body.model_dump(exclude_none=True))
    db.commit()
    return out


# ── Platform admin ───────────────────────────────────────────────────

class ProviderIn(BaseModel):
    enabled: bool = False
    client_id: str = Field("", max_length=255)
    client_secret: str = Field("", max_length=500)
    clear_secret: bool = False
    tenant: str | None = Field(None, max_length=80)


@platform_router.get("")
def platform_settings(_: PlatformUser = Depends(platform_admin), db: Session = Depends(get_db)):
    return service.platform_out(db)


@platform_router.put("/{provider}")
def save_platform_provider(provider: str, body: ProviderIn, admin: PlatformUser = Depends(platform_admin),
                           db: Session = Depends(get_db)):
    provider = _provider(provider)
    row = service.save_provider(db, provider, body.model_dump(), admin.email)
    audit.event(db, "platform.sso_provider_saved", "sso_providers", row.id,
                {"provider": provider, "enabled": row.enabled, "secret_changed": bool(body.client_secret)})
    db.commit()
    return service.platform_out(db)


extra_routers = [company_router, platform_router]
