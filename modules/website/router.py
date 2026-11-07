"""Institution website.

The institution's own address (<slug>.campusly.app) opens its public website: name and motto,
about, programmes offered (read live from the academic set-up), why families choose it, campus
life and facilities, leadership, notices and events, admissions (linking to the online
application at /apply), campuses and contact, with a sign-in button for staff, parents and
students. The office picks one of four designs and edits the content in Settings → Website.
Content lives in companies.config["website"]; nothing here needs its own table.

Field names are kept from the first version so saved sites keep loading:
values = highlights ("why choose us"), life = campus life and facilities, offices = campuses.
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

router = APIRouter(prefix="/company/website", tags=["Institution website"])
public_router = APIRouter(prefix="/public/companies", tags=["Institution website (public)"])
extra_routers = [public_router]

THEMES = ("classic", "modern", "serene", "pulse")
SECTIONS = ("about", "programs", "values", "life", "leaders", "notices", "admissions", "jobs", "offices", "contact")
# Sections that start switched off on a new site (staff vacancies are optional for schools).
OFF_BY_DEFAULT = ("jobs",)
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


class Notice(BaseModel):
    title: str = Field("", max_length=140)
    date: str = Field("", max_length=40)  # free text or YYYY-MM-DD, shown as written
    body: str = Field("", max_length=600)
    kind: Literal["notice", "event"] = "notice"


class Programs(BaseModel):
    title: str = Field("", max_length=120)
    body: str = Field("", max_length=600)


class Admissions(BaseModel):
    title: str = Field("", max_length=120)
    body: str = Field("", max_length=1000)
    open: bool = True
    button_label: str = Field("Apply online", max_length=40)


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
    programs: Programs = Field(default_factory=Programs)
    notices: list[Notice] = Field(default_factory=list, max_length=12)
    admissions: Admissions = Field(default_factory=Admissions)
    sections: dict[str, bool] = Field(default_factory=dict)
    show_signin: bool = True

    @field_validator("sections")
    @classmethod
    def _sections(cls, v):
        return {k: bool(v.get(k, k not in OFF_BY_DEFAULT)) for k in SECTIONS}


def _institution(company: Company) -> dict:
    """The institution profile (type, words used on screen, motto, board, principal)."""
    try:
        from modules.academics.service import profile
        return profile(company)
    except Exception:
        return {"type": "school", "type_label": "School", "terms": {}, "motto": "", "board": "",
                "principal_name": "", "established_year": None, "modules": {}}


HIGHLIGHTS = {
    "school": [("Caring teachers", "Every child is known by name, and teachers have time for each one."),
               ("Strong foundations", "A balanced curriculum that builds reading, numbers, science and curiosity."),
               ("Beyond the classroom", "Sports, arts, music and clubs, so every child finds what they love."),
               ("Parents in the loop", "Attendance, homework, results and fees on the parent app, every day.")],
    "preschool": [("Play-based learning", "Children learn through play, stories, music and hands-on activities."),
                  ("Safe and caring", "Secure premises, trained staff and small groups for every child."),
                  ("Healthy routines", "Nutritious meals, rest time and plenty of outdoor play."),
                  ("Parents in the loop", "Daily updates, photos and messages on the parent app.")],
    "college": [("Experienced faculty", "Teachers who know their subject and mentor every student."),
                ("Career ready", "Projects, internships and placement support from the first year."),
                ("Vibrant campus", "Clubs, sports, fests and a library that is open late."),
                ("Everything online", "Timetable, attendance, results and fees in one student app.")],
    "coaching": [("Results that speak", "Focused preparation with regular tests and clear feedback."),
                 ("Small batches", "Every student gets attention and doubts are cleared the same day."),
                 ("Expert tutors", "Subject experts who explain simply and track each student's progress."),
                 ("Learn anywhere", "Live online classes, recordings and practice tests on the app.")],
    "training": [("Practical skills", "Hands-on training that prepares you for real work."),
                 ("Expert trainers", "Trainers from industry who share what actually works."),
                 ("Flexible batches", "Weekday, weekend and online batches to fit your schedule."),
                 ("Recognised certificates", "Assessments and certificates you can show employers.")],
}
FACILITIES = {
    "school": ["Smart classrooms", "Science and computer labs", "Library", "Playground and sports",
               "Safe school transport", "Art and music rooms"],
    "preschool": ["Bright play rooms", "Outdoor play area", "Activity corners", "CCTV and secure entry",
                  "Healthy meals", "Safe transport"],
    "college": ["Modern classrooms", "Labs and workshops", "Central library", "Sports facilities",
                "Hostel", "Wi-Fi campus"],
    "coaching": ["Air-conditioned classrooms", "Study hall", "Test series", "Doubt-clearing sessions",
                 "Online classes", "Study material"],
    "training": ["Training labs", "Live projects", "Online and classroom batches", "Placement help",
                 "Study material", "Certificates"],
}


def _kind(inst: dict) -> str:
    kind = inst.get("type") or "school"
    return "college" if kind == "university" else kind if kind in HIGHLIGHTS else "school"


def defaults(company: Company) -> dict:
    """A complete, sensible first version so the site looks finished before anyone edits it."""
    name = company.company_name
    city = company.city or ""
    inst = _institution(company)
    kind = _kind(inst)
    words = inst.get("terms") or {}
    students = (words.get("students") or "Students").lower()
    programs = (words.get("programs") or "Classes").lower()
    motto = (inst.get("motto") or "").strip()
    board = (inst.get("board") or "").strip()
    headline = motto or {
        "school": "Where every child is known, challenged and cared for.",
        "preschool": "A happy, safe place to learn and grow.",
        "college": "Learn deeply. Think freely. Go further.",
        "coaching": "Focused teaching. Real results.",
        "training": "Skills that open doors.",
    }[kind]
    leaders = []
    if inst.get("principal_name"):
        role = "Principal" if kind in ("school", "preschool", "college") else "Director"
        leaders.append(Leader(name=inst["principal_name"][:100], role=role))
    stats = [st for st in (
        Stat(value=str(inst["established_year"]), label="Established") if inst.get("established_year") else None,
        Stat(value=board[:24], label="Affiliation") if board else None,
        Stat(value=city[:24], label="Campus") if city else None,
    ) if st]
    return Website(
        hero=Hero(eyebrow=" · ".join(x for x in (board, city) if x) or inst.get("type_label") or f"Welcome to {name}",
                  headline=headline[:140],
                  subline=(f"Welcome to {name}. We help {students} learn with confidence, grow in character "
                           "and discover what they are capable of.")[:320]),
        about=About(title=f"About {name}",
                    body=(f"{name} is a community of {students}, teachers and families"
                          f"{' in ' + city if city else ''}. We believe good teaching, a caring environment and "
                          "close partnership with families help every learner do their best.\n\n"
                          f"Our {programs} combine strong academics with activities, values and real-world skills, "
                          "and we keep parents informed at every step.")),
        stats=stats,
        values=[Value(title=t, body=b) for t, b in HIGHLIGHTS[kind]],
        life=Life(title="Campus life",
                  body="A safe, welcoming campus with the spaces and support to learn, play and grow.",
                  perks=FACILITIES[kind]),
        leaders=leaders,
        offices=[Office(city=city, address=company.address or "", phone=company.phone or "", head_office=True)] if city else [],
        contact=Contact(email=company.email or "", phone=company.phone or "", address=", ".join(
            x for x in (company.address, company.city, company.state, company.pincode) if x)),
        programs=Programs(title=f"{(words.get('programs') or 'Classes')} we offer",
                          body=f"Explore our {programs}. Admissions are open for the coming {(words.get('session') or 'academic year').lower()}."),
        notices=[],
        admissions=Admissions(title="Admissions are open",
                              body=("Apply online in a few minutes. Our admissions team will call you to plan a "
                                    "campus visit and guide you through the next steps.")),
        sections={k: k not in OFF_BY_DEFAULT for k in SECTIONS},
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
        return {"company": _brief(comp, slug), "site": site, "jobs": [], "programs": [], "institution": _inst_brief(comp)}
    return {"company": _brief(comp, slug), "site": site,
            "jobs": _jobs(db, comp.id) if site["sections"].get("jobs", True) else [],
            "programs": _programs(db, comp.id) if site["sections"].get("programs", True) else [],
            "institution": _inst_brief(comp)}


def _programs(db: Session, company_id: int) -> list[dict]:
    """Active classes / programmes / courses, in order, with how many sections run this session."""
    try:
        from sqlalchemy import func
        from modules.academics.models import Program, Section
        from modules.academics.service import current_session
        sess = current_session(db, company_id)
        counts = {}
        if sess:
            counts = dict(db.query(Section.program_id, func.count(Section.id))
                          .filter(Section.company_id == company_id, Section.session_id == sess.id,
                                  Section.active.is_(True)).group_by(Section.program_id).all())
        rows = (db.query(Program).filter(Program.company_id == company_id, Program.active.is_(True))
                .order_by(Program.sort_order, Program.name).limit(60).all())
        return [{"name": p.name, "code": p.code, "level": p.level, "description": (p.description or "")[:300],
                 "duration_terms": p.duration_terms, "sections": counts.get(p.id, 0)} for p in rows]
    except Exception:
        return []


def _inst_brief(comp: Company) -> dict:
    """What the public page needs from the institution profile (words and the admissions switch)."""
    inst = _institution(comp)
    words = inst.get("terms") or {}
    keep = ("program", "programs", "section", "sections", "student", "students", "teacher", "teachers",
            "guardian", "guardians", "session")
    return {"type": inst.get("type") or "school", "type_label": inst.get("type_label") or "",
            "motto": inst.get("motto") or "", "board": inst.get("board") or "",
            "established_year": inst.get("established_year"),
            "terms": {k: words[k] for k in keep if k in words},
            "admissions_online": bool((inst.get("modules") or {}).get("admissions", True))}


def _brief(comp: Company, slug: str) -> dict:
    return {"name": comp.company_name, "slug": slug, "industry": comp.industry, "city": comp.city,
            "logo_url": f"/api/v1/public/companies/{slug}/logo" if comp.logo_url else ""}
