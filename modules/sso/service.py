"""Single sign-on rules.

- The platform team registers one OAuth app per provider (client id and secret, kept
  encrypted). Without that, or switched off, the buttons do not show anywhere. The Google app can
  also come from the GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET environment settings.
- Each company switches Google and Microsoft on or off and may list the email domains people
  must use (empty = any domain).
- Sign-in only finds people who already exist: the company owner (by the owner email) or an
  active employee, matched on the verified email the provider returns. Nobody is created.
- From a company's own address only that company is searched. From the main site every company
  that allows the provider is searched: one match signs in and moves to the company's address;
  several matches ask the person to use their company's address.
"""
from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass

from cryptography.fernet import Fernet
from sqlalchemy import func
from sqlalchemy.orm import Session

from core.config import get_settings
from core.db import bypass_rls
from core.errors import BadRequest
from modules.company.models import Company
from modules.employees.models import Employee
from modules.sso.models import SsoCompanySetting, SsoProvider
from modules.sso.providers import PROVIDERS

DOMAIN_RE = re.compile(r"^(?=.{3,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
MS_TENANT_RE = re.compile(r"^(organizations|common|consumers|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[a-z0-9.-]+\.[a-z]{2,})$")


# ── Secrets ──────────────────────────────────────────────────────────

def _key() -> bytes:
    secret = (get_settings().SECRET_KEY or "dev").encode()
    return base64.urlsafe_b64encode(hashlib.sha256(b"sso-providers-v1:" + secret).digest())


def encrypt(value: str) -> str:
    return Fernet(_key()).encrypt(value.encode()).decode()


def decrypt(token: str | None) -> str:
    if not token:
        return ""
    try:
        return Fernet(_key()).decrypt(token.encode()).decode()
    except Exception:
        return ""


# ── Platform providers ───────────────────────────────────────────────

@dataclass
class ProviderConfig:
    provider: str
    enabled: bool
    client_id: str
    client_secret: str
    tenant: str
    from_env: bool = False

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.client_id and self.client_secret)


def provider_row(db: Session, provider: str) -> SsoProvider | None:
    return db.query(SsoProvider).filter(SsoProvider.provider == provider).first()


def provider_config(db: Session, provider: str) -> ProviderConfig:
    row = provider_row(db, provider)
    if row is not None and row.client_id:
        return ProviderConfig(provider, bool(row.enabled), row.client_id, decrypt(row.client_secret_encrypted),
                              row.tenant or ("organizations" if provider == "microsoft" else ""))
    s = get_settings()
    if provider == "google" and getattr(s, "GOOGLE_CLIENT_ID", "") and getattr(s, "GOOGLE_CLIENT_SECRET", ""):
        enabled = row.enabled if row is not None else True
        return ProviderConfig(provider, bool(enabled), s.GOOGLE_CLIENT_ID, s.GOOGLE_CLIENT_SECRET, "", from_env=True)
    return ProviderConfig(provider, bool(row.enabled) if row else False, "", "", "organizations" if provider == "microsoft" else "")


def redirect_uri(provider: str) -> str:
    base = (get_settings().BACKEND_URL or "http://localhost:8000").rstrip("/")
    return f"{base}/api/v1/auth/sso/{provider}/callback"


def platform_out(db: Session) -> dict:
    out = {}
    for p, label in PROVIDERS.items():
        cfg = provider_config(db, p)
        row = provider_row(db, p)
        out[p] = {"label": label, "enabled": cfg.enabled, "client_id": cfg.client_id,
                  "has_secret": bool(cfg.client_secret), "tenant": cfg.tenant if p == "microsoft" else None,
                  "ready": cfg.ready, "from_env": cfg.from_env, "redirect_uri": redirect_uri(p),
                  "updated_at": row.updated_at if row else None, "updated_by": row.updated_by if row else None}
    return out


def save_provider(db: Session, provider: str, data: dict, actor: str) -> SsoProvider:
    if provider not in PROVIDERS:
        raise BadRequest("Unknown sign-in provider.")
    row = provider_row(db, provider)
    if row is None:
        row = SsoProvider(provider=provider)
        db.add(row)
    client_id = (data.get("client_id") or "").strip()
    secret = (data.get("client_secret") or "").strip()
    if client_id and len(client_id) > 255:
        raise BadRequest("The client id is too long.")
    row.client_id = client_id or None
    if secret:
        row.client_secret_encrypted = encrypt(secret)
    elif data.get("clear_secret"):
        row.client_secret_encrypted = None
    if provider == "microsoft":
        tenant = (data.get("tenant") or "organizations").strip().lower()
        if not MS_TENANT_RE.match(tenant):
            raise BadRequest("Use organizations, common, a directory (tenant) id or a directory domain.")
        row.tenant = tenant
    row.enabled = bool(data.get("enabled"))
    if row.enabled and not (row.client_id and (row.client_secret_encrypted or secret)):
        raise BadRequest(f"Enter the {PROVIDERS[provider]} client id and secret before switching it on.")
    row.updated_by = actor
    db.flush()
    return row


# ── Company switches ─────────────────────────────────────────────────

def company_row(db: Session, company_id: int, create: bool = False) -> SsoCompanySetting | None:
    row = db.query(SsoCompanySetting).filter(SsoCompanySetting.company_id == company_id).first()
    if row is None and create:
        row = SsoCompanySetting(company_id=company_id, allowed_domains=[])
        db.add(row)
        db.flush()
    return row


def company_enabled(row: SsoCompanySetting | None, provider: str) -> bool:
    if row is None:
        return False
    return bool(row.google_enabled if provider == "google" else row.microsoft_enabled)


def clean_domains(values) -> list[str]:
    out: list[str] = []
    for v in values or []:
        dom = str(v or "").strip().lower().lstrip("@")
        if not dom:
            continue
        if not DOMAIN_RE.match(dom):
            raise BadRequest(f"“{v}” is not an email domain (for example yourcompany.in).")
        if dom not in out:
            out.append(dom)
    if len(out) > 20:
        raise BadRequest("List at most 20 domains.")
    return out


def domain_allowed(row: SsoCompanySetting | None, email: str) -> bool:
    domains = (row.allowed_domains if row else None) or []
    if not domains:
        return True
    return email.rsplit("@", 1)[-1].lower() in domains


def company_out(db: Session, company_id: int) -> dict:
    row = company_row(db, company_id)
    company = db.query(Company).filter(Company.id == company_id).first()
    owner_domain = (company.email or "").rsplit("@", 1)[-1].lower() if company and company.email else ""
    providers = {}
    for p, label in PROVIDERS.items():
        cfg = provider_config(db, p)
        providers[p] = {"label": label, "available": cfg.ready, "enabled": company_enabled(row, p) and cfg.ready,
                        "switched_on": company_enabled(row, p)}
    return {"providers": providers, "allowed_domains": list((row.allowed_domains if row else []) or []),
            "suggested_domain": owner_domain if owner_domain and owner_domain not in PUBLIC_DOMAINS else ""}


PUBLIC_DOMAINS = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com", "yahoo.co.in",
                  "rediffmail.com", "icloud.com", "proton.me", "protonmail.com", "example.com"}


def save_company(db: Session, company_id: int, data: dict) -> dict:
    row = company_row(db, company_id, create=True)
    for p in PROVIDERS:
        key = f"{p}_enabled"
        if key in data:
            on = bool(data[key])
            if on and not provider_config(db, p).ready:
                raise BadRequest(f"{PROVIDERS[p]} sign-in is not set up on this service yet.")
            setattr(row, key, on)
    if "allowed_domains" in data:
        row.allowed_domains = clean_domains(data.get("allowed_domains"))
    db.flush()
    return company_out(db, company_id)


def options_for(db: Session, company_id: int | None) -> dict:
    """Which buttons the sign-in page shows."""
    row = company_row(db, company_id) if company_id else None
    out = {}
    for p in PROVIDERS:
        ready = provider_config(db, p).ready
        out[p] = ready and (company_enabled(row, p) if company_id else _any_company_allows(db, p))
    return out


def _any_company_allows(db: Session, provider: str) -> bool:
    col = SsoCompanySetting.google_enabled if provider == "google" else SsoCompanySetting.microsoft_enabled
    with bypass_rls(db):
        return db.query(SsoCompanySetting.id).filter(col.is_(True)).first() is not None


# ── Matching ─────────────────────────────────────────────────────────

@dataclass
class Match:
    company: Company
    employee: Employee | None   # None = the company owner


def find_accounts(db: Session, provider: str, email: str, company_id: int | None) -> tuple[list[Match], str | None]:
    """Existing people with this verified email who may use this provider.
    Returns (matches, reason) where reason explains an empty result."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return [], "no_email"
    with bypass_rls(db):
        cq = db.query(Company).filter(func.lower(Company.email) == email)
        eq = db.query(Employee).filter(func.lower(Employee.email) == email, Employee.active.is_(True))
        if company_id is not None:
            cq = cq.filter(Company.id == company_id)
            eq = eq.filter(Employee.company_id == company_id)
        owners = cq.all()
        employees = eq.all()
        ids = {c.id for c in owners} | {e.company_id for e in employees}
        companies = {c.id: c for c in db.query(Company).filter(Company.id.in_(ids or [-1])).all()}
        rows = {r.company_id: r for r in db.query(SsoCompanySetting).filter(SsoCompanySetting.company_id.in_(ids or [-1])).all()}
    found: dict[int, Match] = {}
    for c in owners:
        found[c.id] = Match(c, None)
    for e in employees:
        if e.company_id not in found and e.company_id in companies:
            found[e.company_id] = Match(companies[e.company_id], e)
    if not found:
        return [], "no_account"
    allowed = [m for m in found.values() if company_enabled(rows.get(m.company.id), provider)]
    if not allowed:
        return [], "not_enabled"
    allowed = [m for m in allowed if domain_allowed(rows.get(m.company.id), email)]
    if not allowed:
        return [], "domain_not_allowed"
    active = [m for m in allowed if m.company.active]
    if not active:
        return [], "company_disabled"
    return sorted(active, key=lambda m: m.company.company_name or ""), None
