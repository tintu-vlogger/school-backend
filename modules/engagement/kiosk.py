"""Kiosk displays: office TVs that cycle birthdays, anniversaries, new joiners,
announcements, news and kudos. Each kiosk has a secret link (no sign-in); anyone with
the link sees only what is meant for a wall screen: first names and photos, never
birth years, and only announcements marked for kiosks and meant for everyone (or for the
kiosk's own audience)."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from core import audit
from core.db import bypass_rls
from core.errors import BadRequest, NotFound
from core.timeutil import now_for, today_for
from modules.engagement import audience as aud
from modules.engagement import celebrations, content, people
from modules.engagement.models import Announcement, Kiosk, Kudos

SLIDE_TYPES = {
    "welcome": "Welcome and clock",
    "birthdays": "Birthdays",
    "anniversaries": "Work anniversaries",
    "new_joiners": "New joiners",
    "announcements": "Announcements",
    "news": "News",
    "kudos": "Kudos",
}
DEFAULT_SLIDES = [{"type": t, "enabled": True} for t in SLIDE_TYPES]


def new_token() -> str:
    return secrets.token_urlsafe(24)


def _clean(db: Session, company_id: int, data: dict, k: Kiosk) -> None:
    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            raise BadRequest("Give the kiosk a name, such as “Reception TV”.")
        k.name = name[:120]
    if "slides" in data:
        slides, seen = [], set()
        for s in data["slides"] or []:
            t = s.get("type")
            if t in SLIDE_TYPES and t not in seen:
                seen.add(t)
                slides.append({"type": t, "enabled": bool(s.get("enabled", True))})
        for t in SLIDE_TYPES:
            if t not in seen:
                slides.append({"type": t, "enabled": False})
        if not any(s["enabled"] for s in slides):
            raise BadRequest("Turn on at least one slide.")
        k.slides = slides
    if "seconds_per_slide" in data:
        sec = int(data["seconds_per_slide"] or 10)
        if not 5 <= sec <= 120:
            raise BadRequest("Show each slide for 5 to 120 seconds.")
        k.seconds_per_slide = sec
    if "audience" in data:
        k.audience = aud.normalize(data["audience"])
    if "theme" in data:
        if data["theme"] not in ("dark", "light", "festive"):
            raise BadRequest("Pick a theme.")
        k.theme = data["theme"]
    if "welcome_text" in data:
        k.welcome_text = (data["welcome_text"] or "").strip()[:200] or None
    if "active" in data:
        k.active = bool(data["active"])


def create(db: Session, company_id: int, data: dict) -> Kiosk:
    k = Kiosk(company_id=company_id, token=new_token(), slides=DEFAULT_SLIDES, seconds_per_slide=10, theme="dark",
              audience={"all": True})
    _clean(db, company_id, {"name": "Reception TV", **data}, k)
    db.add(k)
    db.flush()
    return k


def get(db: Session, company_id: int, uuid) -> Kiosk:
    k = db.query(Kiosk).filter(Kiosk.company_id == company_id, Kiosk.uuid == uuid).first()
    if k is None:
        raise NotFound("Kiosk")
    return k


def update(db: Session, k: Kiosk, data: dict) -> Kiosk:
    _clean(db, k.company_id, data, k)
    return k


def regenerate(db: Session, k: Kiosk) -> None:
    k.token = new_token()
    audit.event(db, "kiosk.link_changed", "kiosks", k.id, {}, entity_uuid=str(k.uuid), entity_label=k.name)


def as_dict(db: Session, k: Kiosk) -> dict:
    return {"uuid": str(k.uuid), "name": k.name, "token": k.token, "slides": k.slides, "seconds_per_slide": k.seconds_per_slide,
            "audience": k.audience, "audience_label": aud.describe(db, k.company_id, k.audience), "theme": k.theme,
            "welcome_text": k.welcome_text, "active": k.active, "created_at": k.created_at,
            "path": f"/kiosk/{k.token}"}


def _wall_person(emp) -> dict:
    """What a wall screen may show about a person."""
    return {"name": people.full_name(emp), "first_name": emp.first_name,
            "title": emp.designation.designation_name if emp.designation else None,
            "department": emp.department.department_name if emp.department else None,
            "image": emp.profile_image_url}


def display(db: Session, token: str) -> dict:
    """Everything the kiosk page needs, found by its secret link."""
    with bypass_rls(db):
        k = db.query(Kiosk).filter(Kiosk.token == token).first()
    if k is None or not k.active or len(token) < 20:
        raise NotFound("Kiosk")
    from core.db import set_tenant
    set_tenant(db, k.company_id)
    from modules.company.models import Company
    company = db.query(Company).filter(Company.id == k.company_id).first()
    today = today_for(company)
    in_aud = set(aud.resolve(db, k.company_id, k.audience))
    events = [e for e in celebrations.events_between(db, k.company_id, today, today + timedelta(days=7))
              if e["employee"].id in in_aud]

    def ev(e):
        return {"kind": e["kind"], "date": e["date"], "today": e["date"] == today, "years": e["years"],
                "person": _wall_person(e["employee"])}

    joiners = [e for e in celebrations.new_joiners(db, k.company_id, today, 14) if e.id in in_aud]
    now = datetime.now(timezone.utc)
    anns = (db.query(Announcement).filter(Announcement.company_id == k.company_id, Announcement.status == "published",
                                          Announcement.show_on_kiosk.is_(True),
                                          or_(Announcement.expires_at.is_(None), Announcement.expires_at > now))
            .order_by(Announcement.published_at.desc()).limit(12).all())
    anns = [a for a in anns if aud.is_everyone(a.audience) or a.audience == k.audience]
    from modules.engagement.announcements import cover_out

    def ann(a):
        return {"uuid": str(a.uuid), "kind": a.kind, "title": a.title,
                "summary": a.summary or content.html_text(a.body_html, 320), "cover": cover_out(a.cover),
                "published_at": a.published_at}

    kudos_rows = (db.query(Kudos).options(joinedload(Kudos.value), joinedload(Kudos.recipients))
                  .filter(Kudos.company_id == k.company_id, Kudos.created_at >= now - timedelta(days=14))
                  .order_by(Kudos.created_at.desc()).limit(8).all())
    emps = people.load(db, k.company_id, {r.employee_id for x in kudos_rows for r in x.recipients} |
                       {x.giver_employee_id for x in kudos_rows})
    from modules.engagement.kudos import value_dict
    kudos = [{"message": x.message, "value": value_dict(x.value) if x.value else None,
              "giver": _wall_person(emps[x.giver_employee_id]) if x.giver_employee_id in emps else {"name": people.owner_name(db, k.company_id)},
              "recipients": [_wall_person(emps[r.employee_id]) for r in x.recipients if r.employee_id in emps]}
             for x in kudos_rows if any(r.employee_id in in_aud for r in x.recipients)]
    local = now_for(company)
    return {
        "kiosk": {"name": k.name, "seconds_per_slide": k.seconds_per_slide, "theme": k.theme,
                  "welcome_text": k.welcome_text, "slides": [s for s in k.slides if s.get("enabled")]},
        "company": {"name": company.company_name, "logo": company.logo_url, "timezone": company.timezone},
        "local_time": local.isoformat(), "date": today,
        "birthdays": [ev(e) for e in events if e["kind"] == "birthday"],
        "anniversaries": [ev(e) for e in events if e["kind"] == "work_anniversary"],
        "new_joiners": [_wall_person(e) for e in joiners[:10]],
        "announcements": [ann(a) for a in anns if a.kind != "news"],
        "news": [ann(a) for a in anns if a.kind == "news"],
        "kudos": kudos,
    }
