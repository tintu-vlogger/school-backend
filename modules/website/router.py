"""Company website.

The company's own address (<slug>.nextgenhrms.com) opens its organisation website: who they are,
what they value, life at work, leadership, offices, open jobs and contact, with an employee
sign-in. HR picks one of four designs and edits the content in Settings → Company website.
Content lives in companies.config["website"]; nothing here needs its own table.
"""
from typing import Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core import audit
from core.db import bypass_rls, get_db, set_tenant
from core.errors import NotFound
from core.security import get_current_user, require_permission
from modules.company.models import Company

router = APIRouter(prefix="/company/website", tags=["Company website"])
public_router = APIRouter(prefix="/public/companies", tags=["Company website (public)"])
extra_routers = [public_router]

THEMES = ("classic", "modern", "serene", "pulse")
SECTIONS = ("about", "values", "life", "leaders", "jobs", "offices", "contact")
MAX_IMAGE = 1_500_000  # characters of a data: URL


def _img(v: str | None) -> str | None:
    v = (v or "").strip() or None
    if v is None:
        return None
    if v.startswith("https://") or v.startswith("http://") or v.startswith("/"):
        return v[:2000]
    if v.startswith("data:image/") and len(v) <= MAX_IMAGE:
        return v
    raise ValueError("Use an image link (https://…) or upload a picture under 1 MB.")


class Stat(BaseModel):
    value: str = Field("", max_length=24)
    label: str = Field("", max_length=60)


class Value(BaseModel):
    title: str = Field("", max_length=80)
    body: str = Field("", max_length=400)


class Leader(BaseModel):
    name: str = Field("", max_length=100)
    role: str = Field("", max_length=100)
    bio: str = Field("", max_length=400)
    photo_url: str | None = None

    @field_validator("photo_url")
    @classmethod
    def _p(cls, v):
        return _img(v)


class Office(BaseModel):
    city: str = Field("", max_length=80)
    address: str = Field("", max_length=300)
    phone: str = Field("", max_length=30)
    head_office: bool = False


class Hero(BaseModel):
    eyebrow: str = Field("", max_length=80)
    headline: str = Field("", max_length=140)
    subline: str = Field("", max_length=320)
    image_url: str | None = None

    @field_validator("image_url")
    @classmethod
    def _i(cls, v):
        return _img(v)


class About(BaseModel):
    title: str = Field("", max_length=120)
    body: str = Field("", max_length=3000)
    image_url: str | None = None

    @field_validator("image_url")
    @classmethod
    def _i(cls, v):
        return _img(v)


class Life(BaseModel):
    title: str = Field("", max_length=120)
    body: str = Field("", max_length=1500)
    perks: list[str] = Field(default_factory=list, max_length=16)
    images: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("perks")
    @classmethod
    def _perks(cls, v):
        return [p.strip()[:80] for p in v if p and p.strip()]

    @field_validator("images")
    @classmethod
    def _imgs(cls, v):
        return [x for x in (_img(i) for i in v) if x]


class Contact(BaseModel):
    email: str = Field("", max_length=200)
    phone: str = Field("", max_length=30)
    address: str = Field("", max_length=400)


class Social(BaseModel):
    linkedin: str = Field("", max_length=300)
    instagram: str = Field("", max_length=300)
    x: str = Field("", max_length=300)
    facebook: str = Field("", max_length=300)
    youtube: str = Field("", max_length=300)


class Website(BaseModel):
    published: bool = True
    theme: Literal["classic", "modern", "serene", "pulse"] = "classic"
    accent: str = Field("#4f46e5", pattern=r"^#[0-9a-fA-F]{6}$")
    hero: Hero = Field(default_factory=Hero)
    about: About = Field(default_factory=About)
    stats: list[Stat] = Field(default_factory=list, max_length=6)
    values: list[Value] = Field(default_factory=list, max_length=8)
    life: Life = Field(default_factory=Life)
    leaders: list[Leader] = Field(default_factory=list, max_length=12)
    offices: list[Office] = Field(default_factory=list, max_length=12)
    contact: Contact = Field(default_factory=Contact)
    social: Social = Field(default_factory=Social)
    sections: dict[str, bool] = Field(default_factory=dict)
    show_signin: bool = True

    @field_validator("sections")
    @classmethod
    def _sections(cls, v):
        return {k: bool(v.get(k, True)) for k in SECTIONS}


def defaults(company: Company) -> dict:
    """A complete, sensible first version so the site looks finished before HR edits anything."""
    name = company.company_name
    city = company.city or "India"
    industry = (company.industry or "").strip()
    what = industry.lower() if industry else "our work"
    return Website(
        hero=Hero(eyebrow=" · ".join(x for x in (industry, company.city) if x) or f"Welcome to {name}",
                  headline="People who care, doing work that matters.",
                  subline=f"At {name} we build a workplace where good people grow, ideas are heard and every day counts."),
        about=About(title=f"About {name}",
                    body=(f"{name} is a team of curious, committed people based in {city}. We take pride in {what}, "
                          "in treating each other fairly and in keeping our promises to customers and colleagues alike.\n\n"
                          "We invest in learning, celebrate good work and keep things simple, so everyone can do the best work of their career here.")),
        # Only facts we know; HR adds founding year, ratings and the like themselves.
        stats=[st for st in (
            Stat(value=company.employee_count_band, label="People") if company.employee_count_band else None,
            Stat(value=company.city, label="Head office") if company.city else None,
        ) if st],
        values=[Value(title="Own it", body="We take responsibility, follow through and fix what we find."),
                Value(title="Keep it simple", body="Clear words, fewer steps, honest answers."),
                Value(title="Grow together", body="We learn every week and help each other get better."),
                Value(title="Do right", body="Fair to our people, our customers and the law, always.")],
        life=Life(title="Life at " + name,
                  body="Flexible hours, a friendly team and real ownership from day one.",
                  perks=["Health insurance for family", "Hybrid work", "Learning budget", "Festival celebrations",
                         "Paid parental leave", "Wellness days"]),
        leaders=[], offices=[Office(city=city, address=company.address or "", phone=company.phone or "", head_office=True)],
        contact=Contact(email=company.email or "", phone=company.phone or "", address=", ".join(
            x for x in (company.address, company.city, company.state, company.pincode) if x)),
        sections={k: True for k in SECTIONS},
    ).model_dump()


def current(company: Company) -> dict:
    stored = (company.config or {}).get("website") if isinstance(company.config, dict) else None
    base = defaults(company)
    if not isinstance(stored, dict):
        return base
    merged = {**base, **stored}
    try:
        return Website(**merged).model_dump()
    except Exception:
        return base


def _company(db: Session, company_id: int) -> Company:
    with bypass_rls(db):
        comp = db.get(Company, company_id)
    if comp is None:
        raise NotFound("Company")
    return comp


@router.get("")
def get_website(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    comp = _company(db, user["company_id"])
    return {"website": current(comp), "slug": comp.slug, "themes": list(THEMES)}


@router.put("")
def save_website(body: Website, user: dict = Depends(require_permission("settings.company.manage")),
                 db: Session = Depends(get_db)):
    comp = _company(db, user["company_id"])
    data = body.model_dump()
    with bypass_rls(db):
        config = dict(comp.config) if isinstance(comp.config, dict) else {}
        before = config.get("website")
        config["website"] = data
        comp.config = config
        flag_modified(comp, "config")
        db.flush()
    audit.event(db, "website.saved", entity_type="company", entity_id=comp.id,
                details={"theme": data["theme"], "published": data["published"], "had_previous": before is not None})
    db.commit()
    return {"website": current(comp), "slug": comp.slug, "themes": list(THEMES)}


@router.post("/reset")
def reset_website(user: dict = Depends(require_permission("settings.company.manage")), db: Session = Depends(get_db)):
    comp = _company(db, user["company_id"])
    with bypass_rls(db):
        config = dict(comp.config) if isinstance(comp.config, dict) else {}
        config.pop("website", None)
        comp.config = config
        flag_modified(comp, "config")
        db.flush()
    audit.event(db, "website.reset", entity_type="company", entity_id=comp.id)
    db.commit()
    return {"website": current(comp), "slug": comp.slug, "themes": list(THEMES)}


def _jobs(db: Session, company_id: int) -> list[dict]:
    try:
        from modules.recruitment import boards, settings as rsettings
        from modules.recruitment.router import _public_jobs
        if not rsettings.get(db, company_id)["careers"].get("enabled", True):
            return []
        jobs = _public_jobs(db, company_id)[:6]
        keep = ("uuid", "title", "slug", "department", "location", "work_mode", "work_mode_label", "employment_type",
                "min_experience", "max_experience")
        return [{k: d.get(k) for k in keep} for d in boards.feed_items(db, company_id, jobs)]
    except Exception:
        return []


@public_router.get("/{slug}/site")
def public_site(slug: str, response: Response, db: Session = Depends(get_db)):
    # Always check for a newer version, so a saved change shows on the next visit.
    response.headers["Cache-Control"] = "no-cache"
    with bypass_rls(db):
        comp = db.query(Company).filter(Company.slug == slug, Company.active.is_(True)).first()
    if comp is None:
        raise NotFound("Company")
    set_tenant(db, comp.id)
    site = current(comp)
    if not site["published"]:
        site = {"published": False, "theme": site["theme"], "accent": site["accent"], "show_signin": True}
        return {"company": _brief(comp, slug), "site": site, "jobs": []}
    return {"company": _brief(comp, slug), "site": site,
            "jobs": _jobs(db, comp.id) if site["sections"].get("jobs", True) else []}


def _brief(comp: Company, slug: str) -> dict:
    return {"name": comp.company_name, "slug": slug, "industry": comp.industry, "city": comp.city,
            "logo_url": f"/api/v1/public/companies/{slug}/logo" if comp.logo_url else ""}
