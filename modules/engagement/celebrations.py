"""Birthdays, work anniversaries, wedding anniversaries and new joiners.

Lists for Buzz, the home page and the kiosk respect each person's privacy choices (and
never show a birth year or age). The morning job (see jobs.py) posts celebration cards
at the company's local hour and sends the celebration emails, once per person, kind and
day: a `celebration_events` row is claimed first (unique), so a second run, a restart or
two API processes never post or email twice.
"""
from __future__ import annotations

import calendar
import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from core import audit
from core.config import get_settings
from core.email import send_email
from core.errors import BadRequest
from core.timeutil import now_for, today_for
from modules.employees.models import Employee
from modules.engagement import people
from modules.engagement import settings as cfg
from modules.engagement.models import BuzzComment, BuzzPost, BuzzReaction, CelebrationEvent, EngagementProfile
from modules.engagement.people import Actor

log = logging.getLogger("hrms.engagement")

THEMES = ["golden", "aurora", "disco", "fireworks", "synthwave", "bubble", "cosmic", "sunset", "ocean", "lavender"]
KIND_LABEL = {"birthday": "Birthday", "work_anniversary": "Work anniversary",
              "wedding_anniversary": "Wedding anniversary", "welcome": "New joiner"}


def _occurrence(d: date, year: int) -> date:
    """The anniversary of `d` in `year` (29 Feb falls on 28 Feb in other years)."""
    last = calendar.monthrange(year, d.month)[1]
    return date(year, d.month, min(d.day, last))


def next_occurrence(d: date, today: date) -> date:
    occ = _occurrence(d, today.year)
    return occ if occ >= today else _occurrence(d, today.year + 1)


def _privacy(profile: EngagementProfile | None, conf: dict) -> dict:
    default_hide = conf.get("default_birthday_privacy") == "hide"
    if profile is None:
        return {"birthday": not default_hide, "work_anniversary": True, "wedding_anniversary": False, "emails": True}
    return {"birthday": not profile.hide_birthday, "work_anniversary": not profile.hide_work_anniversary,
            "wedding_anniversary": not profile.hide_wedding_anniversary, "emails": profile.celebration_emails}


def events_between(db: Session, company_id: int, start: date, end: date, *, respect_privacy: bool = True,
                   conf: dict | None = None) -> list[dict]:
    """Every celebration from start to end (inclusive), soonest first."""
    from modules.company.owner import owner_employee_id
    conf = conf or cfg.section(db, company_id, "celebrations")
    owner_id = owner_employee_id(db, company_id)  # the owner founded the place; no "welcome"
    out = []
    for emp, birth, joining, wedding, profile in people.dates_query(db, company_id).all():
        allow = _privacy(profile, conf)
        for kind, d in (("birthday", birth), ("work_anniversary", joining), ("wedding_anniversary", wedding)):
            if d is None or (respect_privacy and not allow[kind]):
                continue
            occ = next_occurrence(d, start)
            while occ <= end:
                years = occ.year - d.year
                if kind == "birthday" or years >= 1:
                    out.append({"kind": kind, "date": occ, "employee": emp,
                                "years": years if kind != "birthday" else None})
                occ = _occurrence(d, occ.year + 1)
        if joining is not None and start <= joining <= end and emp.id != owner_id:
            out.append({"kind": "welcome", "date": joining, "employee": emp, "years": None})
    out.sort(key=lambda e: (e["date"], e["kind"] != "birthday", people.full_name(e["employee"])))
    return out


def new_joiners(db: Session, company_id: int, today: date, days: int = 30) -> list[Employee]:
    start = today - timedelta(days=days)
    from modules.company.owner import owner_employee_id
    rows = people.dates_query(db, company_id).all()
    owner_id = owner_employee_id(db, company_id)
    out = []
    for emp, _b, joining, _w, _p in rows:
        if joining is not None and start <= joining <= today and emp.id != owner_id:
            out.append((joining, emp))
    out.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in out]


def overview(db: Session, actor: Actor, days: int | None = None) -> dict:
    """Today, the rest of this week, and upcoming celebrations, with the Buzz post (for
    wishing) and whether the viewer has already wished."""
    from modules.company.models import Company
    company = db.query(Company).filter(Company.id == actor.company_id).first()
    conf = cfg.section(db, actor.company_id, "celebrations")
    days = days or int(conf.get("show_upcoming_days") or 14)
    today = today_for(company)
    events = [e for e in events_between(db, actor.company_id, today, today + timedelta(days=days), conf=conf)
              if e["kind"] != "welcome"]
    week_end = today + timedelta(days=6 - today.weekday())  # Sunday
    posts = {(p.kind, p.subject_employee_id, p.celebration_date): p for p in db.query(BuzzPost).filter(
        BuzzPost.company_id == actor.company_id, BuzzPost.celebration_date.isnot(None),
        BuzzPost.celebration_date >= today - timedelta(days=3), BuzzPost.deleted_at.is_(None)).all()}
    wished = set()
    if posts:
        ids = [p.id for p in posts.values()]
        wished = {pid for (pid,) in db.query(BuzzComment.post_id).filter(
            BuzzComment.post_id.in_(ids), BuzzComment.author_employee_id == actor.employee_id,
            BuzzComment.deleted_at.is_(None)).all()} if actor.employee_id else set()
        wished |= {pid for (pid,) in db.query(BuzzReaction.post_id).filter(
            BuzzReaction.post_id.in_(ids), BuzzReaction.actor_key == actor.key, BuzzReaction.comment_id.is_(None)).all()}

    def item(e):
        p = posts.get((e["kind"], e["employee"].id, e["date"]))
        return {"kind": e["kind"], "label": KIND_LABEL[e["kind"]], "date": e["date"], "years": e["years"],
                "person": people.card(e["employee"]), "is_me": e["employee"].id == actor.employee_id,
                "post_uuid": str(p.uuid) if p else None, "wished": bool(p and p.id in wished),
                "days_away": (e["date"] - today).days}

    joiners = new_joiners(db, actor.company_id, today, 21)
    return {
        "today": [item(e) for e in events if e["date"] == today],
        "this_week": [item(e) for e in events if today < e["date"] <= week_end],
        "upcoming": [item(e) for e in events if e["date"] > week_end],
        "new_joiners": [people.card(e) for e in joiners[:12]],
        "date": today,
    }


def wish(db: Session, actor: Actor, post: BuzzPost, message: str | None, emoji: str | None = "🎉") -> dict:
    """Quick wish on a celebration card: a reaction and (optionally) a comment."""
    from modules.engagement import feed
    if post.kind not in ("birthday", "work_anniversary", "wedding_anniversary", "welcome", "kudos"):
        raise BadRequest("This post is not a celebration.")
    if emoji:
        current = db.query(BuzzReaction).filter(BuzzReaction.post_id == post.id, BuzzReaction.actor_key == actor.key,
                                                BuzzReaction.comment_id.is_(None)).first()
        if current is None or current.emoji != emoji:
            feed.react(db, actor, post, emoji)
    comment = None
    if message and message.strip():
        comment = feed.add_comment(db, actor, post, message)
    return {"comment_uuid": str(comment.uuid) if comment else None}


# ── The morning job ─────────────────────────────────────────────────

def _claim(db: Session, company_id: int, employee_id: int, kind: str, day: date, years: int | None) -> int | None:
    """Insert the once-per-day row; None if it already existed (someone else did it)."""
    stmt = (insert(CelebrationEvent)
            .values(company_id=company_id, employee_id=employee_id, kind=kind, event_date=day, years=years)
            .on_conflict_do_nothing(constraint="uq_celebration_events_once")
            .returning(CelebrationEvent.id))
    return db.execute(stmt).scalar()


def _theme(employee_id: int, day: date, used: set) -> str:
    start = (employee_id + day.year + day.toordinal()) % len(THEMES)
    for i in range(len(THEMES)):
        t = THEMES[(start + i) % len(THEMES)]
        if t not in used:
            used.add(t)
            return t
    return THEMES[start]


DEFAULT_COPY = {
    "birthday": "Happy birthday, {first}! 🎂 Wishing you a wonderful year ahead.",
    "work_anniversary": "Celebrating {years} {years_word} with us, {first}! 🎉 Thank you for everything you do.",
    "wedding_anniversary": "Happy wedding anniversary, {first}! 💐",
    "welcome": "Please welcome {first} to the team! 👋 Say hello and help them settle in.",
}


def _values(emp: Employee, company, years: int | None, post: BuzzPost | None) -> dict:
    base = get_settings().FRONTEND_URL.rstrip("/")
    return {
        "first_name": emp.first_name, "full_name": people.full_name(emp), "company_name": company.company_name,
        "designation": emp.designation.designation_name if emp.designation else "a member of our team",
        "department": emp.department.department_name if emp.department else "",
        "years": years or 0, "years_word": "year" if years == 1 else "years",
        "years_ordinal": cfg.ordinal(years or 0),
        "buzz_link": f"{base}/dashboard/buzz" + (f"?post={post.uuid}" if post else ""),
    }


def send_celebration_email(db: Session, company, emp: Employee, kind: str, years: int | None,
                           post: BuzzPost | None, conf: dict) -> str:
    tpl = {"birthday": conf.get("email_birthday"), "work_anniversary": conf.get("email_work_anniversary"),
           "welcome": conf.get("email_welcome")}.get(kind)
    if not tpl or not emp.email:
        return "skipped"
    vals = _values(emp, company, years, post)
    try:
        subject, body = cfg.render_template(tpl["subject"], vals), cfg.render_template(tpl["body"], vals)
    except KeyError:
        return "failed"
    return "sent" if send_email(emp.email, subject, body) else "logged"


def run_morning(db: Session, company, force: bool = False) -> dict:
    """Post today's celebrations and send the emails. Runs once the company's local clock
    has passed the configured hour; calling it again the same day does nothing."""
    conf = cfg.section(db, company.id, "celebrations")
    local = now_for(company)
    if not force and local.hour < int(conf.get("post_hour", 9)):
        return {"posted": 0, "emailed": 0, "waiting": True}
    today = local.date()
    want = {"birthday": conf.get("post_birthdays", True), "work_anniversary": conf.get("post_work_anniversaries", True),
            "wedding_anniversary": conf.get("post_wedding_anniversaries", False), "welcome": conf.get("post_new_joiners", True)}
    used_themes = {t for (t,) in db.query(BuzzPost.theme).filter(BuzzPost.company_id == company.id,
                                                                  BuzzPost.celebration_date == today).all() if t}
    posted = emailed = 0
    profiles = {p.employee_id: p for p in db.query(EngagementProfile).filter(EngagementProfile.company_id == company.id).all()}
    events = events_between(db, company.id, today, today, respect_privacy=False, conf=conf)
    for e in events:
        emp, kind = e["employee"], e["kind"]
        allow = _privacy(profiles.get(emp.id), conf)
        visible = allow.get(kind, True) if kind != "welcome" else True
        email_on = conf.get("email_enabled", True) and allow["emails"] and (
            kind in ("birthday", "work_anniversary") or (kind == "welcome" and conf.get("email_welcome_enabled")))
        if not ((want.get(kind) and visible) or email_on):
            continue
        event_id = _claim(db, company.id, emp.id, kind, today, e["years"])
        if event_id is None:
            continue  # already done today
        post = None
        if want.get(kind) and visible:
            first = emp.first_name
            text = DEFAULT_COPY[kind].format(first=first, years=e["years"] or 0,
                                             years_word="year" if e["years"] == 1 else "years")
            post = BuzzPost(company_id=company.id, kind=kind, is_system=True, body=text, subject_employee_id=emp.id,
                            celebration_date=today, years=e["years"], theme=_theme(emp.id, today, used_themes),
                            mentions=[emp.id])
            db.add(post)
            db.flush()
            posted += 1
        status = send_celebration_email(db, company, emp, kind, e["years"], post, conf) if email_on else None
        if status in ("sent", "logged"):
            emailed += 1
        db.query(CelebrationEvent).filter(CelebrationEvent.id == event_id).update(
            {"post_id": post.id if post else None, "email_status": status,
             "email_sent_at": datetime.now(timezone.utc) if status in ("sent", "logged") else None},
            synchronize_session=False)
        if post is not None:
            from modules.notifications.service import notify
            msg = {"birthday": "Happy birthday! 🎂 Your colleagues can wish you on Buzz.",
                   "work_anniversary": f"Happy work anniversary! 🎉 {e['years']} {'year' if e['years'] == 1 else 'years'} with us.",
                   "wedding_anniversary": "Happy wedding anniversary! 💐",
                   "welcome": "Welcome aboard! 👋 Your colleagues are saying hello on Buzz."}[kind]
            notify(db, company.id, [emp.id], kind="celebration", title=msg, link=f"/dashboard/buzz?post={post.uuid}")
    if posted or emailed:
        audit.event(db, "celebrations.posted", "celebration_events", None, {"date": str(today), "posts": posted, "emails": emailed})
    return {"posted": posted, "emailed": emailed, "waiting": False}


def preview_email(db: Session, company_id: int, kind: str, template: dict | None = None) -> dict:
    from modules.company.models import Company
    company = db.query(Company).filter(Company.id == company_id).first()
    conf = cfg.section(db, company_id, "celebrations")
    tpl = template or conf.get({"birthday": "email_birthday", "work_anniversary": "email_work_anniversary",
                                "welcome": "email_welcome"}.get(kind, "email_birthday"))
    vals = {**cfg.SAMPLE_VALUES, "company_name": company.company_name}
    try:
        return {"subject": cfg.render_template(tpl["subject"], vals), "body": cfg.render_template(tpl["body"], vals)}
    except KeyError as exc:
        raise BadRequest(f"Unknown placeholder {{{exc.args[0]}}} in the email.")


def my_preferences(db: Session, actor: Actor) -> dict:
    if not actor.employee_id:
        raise BadRequest("The company owner has no celebration settings.")
    conf = cfg.section(db, actor.company_id, "celebrations")
    p = people.profile_for(db, actor.company_id, actor.employee_id)
    allow = _privacy(p, conf)
    row = people.dates_query(db, actor.company_id).filter(Employee.id == actor.employee_id).first()
    return {"show_birthday": allow["birthday"], "show_work_anniversary": allow["work_anniversary"],
            "show_wedding_anniversary": allow["wedding_anniversary"], "celebration_emails": allow["emails"],
            "has_birth_date": bool(row and row[1]), "has_joining_date": bool(row and row[2]),
            "wedding_date": row[3] if row else None}


def update_preferences(db: Session, actor: Actor, data: dict) -> dict:
    if not actor.employee_id:
        raise BadRequest("The company owner has no celebration settings.")
    conf = cfg.section(db, actor.company_id, "celebrations")
    p = people.profile_for(db, actor.company_id, actor.employee_id)
    if p is None:
        p = people.profile_for(db, actor.company_id, actor.employee_id, create=True)
        p.hide_birthday = conf.get("default_birthday_privacy") == "hide"
    for key, attr in (("show_birthday", "hide_birthday"), ("show_work_anniversary", "hide_work_anniversary"),
                      ("show_wedding_anniversary", "hide_wedding_anniversary")):
        if key in data and data[key] is not None:
            setattr(p, attr, not bool(data[key]))
    if "celebration_emails" in data and data["celebration_emails"] is not None:
        p.celebration_emails = bool(data["celebration_emails"])
    if "wedding_date" in data:
        people.set_fallback_dates(db, actor.company_id, actor.employee_id, wedding=data["wedding_date"]) if data["wedding_date"] \
            else setattr(p, "wedding_date", None)
    db.flush()
    return my_preferences(db, actor)
