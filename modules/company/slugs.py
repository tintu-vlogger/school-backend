"""Company web addresses (slugs).

A company's slug becomes its subdomain (<slug>.campusly.app), so it must never be a
name the platform itself uses: our own hosts (api, www, mail...), app pages, or
names that could be mistaken for the platform.
"""
import re

from sqlalchemy.orm import Session

from modules.company.models import Company

RESERVED_SLUGS = frozenset({
    # Platform hosts and infrastructure
    "www", "api", "app", "admin", "superadmin", "platform", "backend", "frontend",
    "static", "assets", "cdn", "media", "files", "img", "images", "uploads",
    "staging", "dev", "test", "beta", "sandbox", "preview", "status", "health",
    "ns", "ns1", "ns2", "dns", "root", "system", "internal", "localhost",
    # Mail
    "mail", "email", "webmail", "smtp", "imap", "pop", "pop3", "mx", "mx1", "mx2",
    "autodiscover", "autoconfig", "postmaster", "hostmaster", "abuse", "noreply", "no-reply",
    # App pages and routes
    "dashboard", "login", "logout", "signin", "signup", "register", "onboarding",
    "forgot-password", "reset-password", "auth", "oauth", "callback", "account", "accounts",
    "settings", "profile", "careers", "jobs", "pay",
    "payment", "payments", "billing", "invoice", "checkout", "docs", "help", "support",
    "contact", "about", "pricing", "privacy", "terms", "legal", "blog", "news", "secure",
    # Messaging integrations
    "whatsapp", "wa", "evolution", "webhook", "webhooks",
    # Brand
    "campusly", "nextgen-hrms", "hrms",
})


def is_reserved(slug: str) -> bool:
    return slug in RESERVED_SLUGS


def unique_slug(db: Session, name: str, fallback: str = "institution") -> str:
    """Build a free, non-reserved slug from a company name."""
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower().strip()).strip("-")[:50] or fallback
    if len(base) < 3 or is_reserved(base):
        base = f"{base}-campus"
    slug, counter = base, 1
    while db.query(Company.id).filter(Company.slug == slug).first():
        slug = f"{base}-{counter}"
        counter += 1
    return slug


SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def normalise(slug: str | None) -> str:
    return (slug or "").strip().lower()


def slug_problem(db: Session, slug: str | None, company_id: int | None = None) -> str | None:
    """Why this web address cannot be used, as a plain sentence, or None when it is free."""
    s = normalise(slug)
    if not s:
        return "Enter a web address."
    if not SLUG_RE.fullmatch(s):
        return "Use only lowercase letters, numbers and hyphens (no spaces, no hyphen at the start or end)."
    if len(s) < 3 or len(s) > 60:
        return "Use 3 to 60 characters."
    if is_reserved(s):
        return f"'{s}' is reserved for the platform. Please choose another web address."
    q = db.query(Company.id).filter(Company.slug == s)
    if company_id is not None:
        q = q.filter(Company.id != company_id)
    if q.first():
        return "This web address is already taken."
    return None


def suggest_slug(db: Session, slug: str | None) -> str:
    """A free alternative close to the one asked for."""
    return unique_slug(db, normalise(slug) or "institution", fallback="institution")
