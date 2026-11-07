"""Company web addresses (slugs).

A company's slug becomes its subdomain (<slug>.nextgenhrms.com), so it must never be a
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
    "nextgenhrms", "nextgen-hrms", "hrms",
})


def is_reserved(slug: str) -> bool:
    return slug in RESERVED_SLUGS


def unique_slug(db: Session, name: str, fallback: str = "company") -> str:
    """Build a free, non-reserved slug from a company name."""
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower().strip()).strip("-")[:50] or fallback
    if len(base) < 3 or is_reserved(base):
        base = f"{base}-company"
    slug, counter = base, 1
    while db.query(Company.id).filter(Company.slug == slug).first():
        slug = f"{base}-{counter}"
        counter += 1
    return slug
