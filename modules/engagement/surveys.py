"""Surveys, pulse checks and eNPS.

A survey has questions (single / multi choice, star rating, agreement scale, text, eNPS
0-10), an audience, an identity rule (anonymous, optional, named) and a schedule. Each
opening is a *run*; recurring surveys open a new run every week / month / quarter /
half-year, and the eNPS trend compares runs.

Anonymity: answers never carry a name unless the survey is named (or the person chose to
show it), the audit log is not written for answers, only the day of submission is kept,
and results of anonymous surveys stay hidden until at least `min_responses` (5 or more)
people have answered. Group breakdowns follow the same threshold.
"""
from __future__ import annotations

import random
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from core import audit
from core.email import send_email
from core.errors import BadRequest, Forbidden, NotFound
from core.timeutil import today_for_company_id
from modules.employees.models import Employee
from modules.engagement import audience as aud
from modules.engagement import people
from modules.engagement import settings as cfg
from modules.engagement.models import (BuzzPost, Survey, SurveyAnswer, SurveyParticipation, SurveyQuestion,
                                       SurveyResponse, SurveyRun)
from modules.engagement.people import Actor
from modules.notifications.service import notify

KINDS = {"survey": "Survey", "pulse": "Pulse check", "enps": "eNPS"}
QTYPES = {"single": "Single choice", "multi": "Multiple choice", "rating": "Star rating", "scale": "Agreement scale",
          "text": "Text answer", "enps": "eNPS (0–10)"}
IDENTITY = {"anonymous": "Anonymous", "optional": "Anonymous unless the person chooses to show their name",
            "named": "Named"}
RECURRENCE = {"none": None, "weekly": 7, "monthly": 1, "quarterly": 3, "half_yearly": 6}
MAX_QUESTIONS = {"enps": 3, "pulse": 10, "survey": 60}
ENPS_QUESTION = "How likely are you to recommend {company} as a place to work to a friend?"


def now() -> datetime:
    return datetime.now(timezone.utc)


def _add_months(dt: datetime, months: int) -> datetime:
    m = dt.month - 1 + months
    y, m = dt.year + m // 12, m % 12 + 1
    import calendar
    d = min(dt.day, calendar.monthrange(y, m)[1])
    return dt.replace(year=y, month=m, day=d)


def next_open(s: Survey, prev: datetime) -> datetime | None:
    step = RECURRENCE.get(s.recurrence)
    if not step:
        return None
    return prev + timedelta(days=7) if s.recurrence == "weekly" else _add_months(prev, step)


def get(db: Session, company_id: int, uuid) -> Survey:
    s = (db.query(Survey).options(joinedload(Survey.questions), joinedload(Survey.runs))
         .filter(Survey.company_id == company_id, Survey.uuid == uuid).first())
    if s is None:
        raise NotFound("Survey")
    return s


# ── Building ───────────────────────────────────────────────────────

def _clean_question(q: dict, i: int) -> dict:
    qtype = q.get("qtype")
    if qtype not in QTYPES:
        raise BadRequest(f"Question {i}: pick a question type.")
    text = (q.get("text") or "").strip()
    if not text:
        raise BadRequest(f"Question {i}: write the question.")
    options, settings = [], {}
    if qtype in ("single", "multi"):
        options = [str(o).strip()[:200] for o in (q.get("options") or []) if str(o).strip()]
        if len(options) < 2:
            raise BadRequest(f"Question {i}: add at least two choices.")
        if len(set(o.lower() for o in options)) != len(options):
            raise BadRequest(f"Question {i}: choices must be different.")
        options = options[:20]
    st = q.get("settings") or {}
    if qtype == "rating":
        settings = {"max": int(st.get("max") or 5)}
        if settings["max"] not in (3, 4, 5, 7, 10):
            raise BadRequest(f"Question {i}: a star rating has 3, 4, 5, 7 or 10 stars.")
    if qtype == "scale":
        points = int(st.get("points") or 5)
        if points not in (3, 4, 5, 7, 10):
            raise BadRequest(f"Question {i}: a scale has 3, 4, 5, 7 or 10 points.")
        settings = {"points": points, "low_label": (st.get("low_label") or "Strongly disagree")[:60],
                    "high_label": (st.get("high_label") or "Strongly agree")[:60]}
    if qtype == "enps":
        settings = {"low_label": (st.get("low_label") or "Not at all likely")[:60],
                    "high_label": (st.get("high_label") or "Extremely likely")[:60]}
    if qtype == "text":
        settings = {"long": bool(st.get("long", True))}
    return {"qtype": qtype, "text": text[:500], "help_text": (q.get("help_text") or "").strip()[:300] or None,
            "required": bool(q.get("required", True)), "options": options, "settings": settings,
            "uuid": q.get("uuid")}


def _apply(db: Session, s: Survey, data: dict) -> None:
    if "title" in data:
        title = (data["title"] or "").strip()
        if not title:
            raise BadRequest("Give the survey a title.")
        s.title = title[:200]
    if "kind" in data:
        if data["kind"] not in KINDS:
            raise BadRequest("Pick a survey type.")
        s.kind = data["kind"]
    for key, limit in (("description", 4000), ("thank_you", 500)):
        if key in data:
            setattr(s, key, (data[key] or "").strip()[:limit] or None)
    if "cover" in data:
        from modules.engagement.announcements import _cover
        s.cover = _cover(db, s.company_id, data["cover"])
    if "identity" in data:
        if data["identity"] not in IDENTITY:
            raise BadRequest("Pick how names are handled.")
        s.identity = data["identity"]
    if "min_responses" in data:
        floor = int(cfg.section(db, s.company_id, "surveys").get("min_anonymous_responses") or 5)
        n = int(data["min_responses"] or floor)
        if n < max(5, floor):
            raise BadRequest(f"Anonymous results need at least {max(5, floor)} responses before they show.")
        s.min_responses = min(n, 100)
    if "audience" in data:
        s.audience = aud.normalize(data["audience"])
    if "mandatory" in data:
        s.mandatory = bool(data["mandatory"])
    if "reminder_every_days" in data:
        r = data["reminder_every_days"]
        s.reminder_every_days = None if r in (None, "", 0) else max(1, min(14, int(r)))
    if "opens_at" in data:
        s.opens_at = data["opens_at"]
    if "duration_days" in data:
        d = int(data["duration_days"] or 7)
        if not 1 <= d <= 90:
            raise BadRequest("A survey stays open between 1 and 90 days.")
        s.duration_days = d
    if "recurrence" in data:
        if data["recurrence"] not in RECURRENCE:
            raise BadRequest("Pick how often the survey repeats.")
        s.recurrence = data["recurrence"]
    if "recurrence_count" in data:
        c = data["recurrence_count"]
        s.recurrence_count = None if c in (None, "", 0) else max(2, min(52, int(c)))
    if "post_to_buzz" in data:
        s.post_to_buzz = bool(data["post_to_buzz"])
    if "questions" in data:
        cleaned = [_clean_question(q, i + 1) for i, q in enumerate(data["questions"] or [])]
        existing = {str(q.uuid): q for q in s.questions}
        new_list = []
        for i, q in enumerate(cleaned):
            row = existing.get(str(q.pop("uuid"))) if q.get("uuid") else None
            q.pop("uuid", None)
            if row is None:
                row = SurveyQuestion(company_id=s.company_id)
            for k, v in q.items():
                setattr(row, k, v)
            row.position = i
            new_list.append(row)
        s.questions = new_list
    if s.recurrence != "none" and s.duration_days and s.recurrence == "weekly" and s.duration_days > 7:
        raise BadRequest("A weekly survey can stay open up to 7 days.")


def _validate_for_publish(s: Survey) -> None:
    if not s.questions:
        raise BadRequest("Add at least one question.")
    if len(s.questions) > MAX_QUESTIONS[s.kind]:
        raise BadRequest(f"A {KINDS[s.kind].lower()} can have up to {MAX_QUESTIONS[s.kind]} questions.")
    enps = [q for q in s.questions if q.qtype == "enps"]
    if s.kind == "enps" and len(enps) != 1:
        raise BadRequest("An eNPS survey has exactly one 0–10 question.")
    if len(enps) > 1:
        raise BadRequest("Use one eNPS question per survey.")


def create(db: Session, actor: Actor, data: dict) -> Survey:
    s = Survey(company_id=actor.company_id, created_by_employee_id=actor.employee_id, created_by_name=actor.name,
               audience={"all": True}, identity="anonymous", kind="survey", duration_days=7, recurrence="none",
               min_responses=max(5, int(cfg.section(db, actor.company_id, "surveys").get("min_anonymous_responses") or 5)))
    db.add(s)
    if data.get("kind") == "enps" and not data.get("questions"):
        from modules.company.models import Company
        company = db.query(Company).filter(Company.id == actor.company_id).first()
        data = {**data, "questions": [
            {"qtype": "enps", "text": ENPS_QUESTION.format(company=company.company_name if company else "us"), "required": True},
            {"qtype": "text", "text": "What is the main reason for your score?", "required": False},
        ]}
    _apply(db, s, data)
    db.flush()
    if data.get("action") == "publish":
        publish(db, actor, s)
    return s


def update(db: Session, actor: Actor, s: Survey, data: dict) -> Survey:
    has_answers = any(r.status != "scheduled" for r in s.runs)
    if has_answers and "questions" in data:
        raise BadRequest("Questions can't change once the survey has opened. Copy it to make a new version.")
    _apply(db, s, data)
    db.flush()
    if data.get("action") == "publish" and s.status == "draft":
        publish(db, actor, s)
    return s


def publish(db: Session, actor: Actor, s: Survey) -> None:
    _validate_for_publish(s)
    start = s.opens_at if s.opens_at and s.opens_at > now() else now()
    run = SurveyRun(company_id=s.company_id, survey_id=s.id, number=1, opens_at=start,
                    closes_at=start + timedelta(days=s.duration_days), status="scheduled")
    s.runs.append(run)
    s.status = "scheduled"
    db.flush()
    audit.event(db, "survey.published", "surveys", s.id, {"kind": s.kind, "identity": s.identity},
                entity_uuid=str(s.uuid), entity_label=s.title)
    if start <= now():
        open_run(db, s, run)


def copy(db: Session, actor: Actor, s: Survey) -> Survey:
    data = {"title": f"{s.title} (copy)", "kind": s.kind, "description": s.description, "thank_you": s.thank_you,
            "cover": s.cover, "identity": s.identity, "min_responses": s.min_responses, "audience": s.audience,
            "mandatory": s.mandatory, "reminder_every_days": s.reminder_every_days, "duration_days": s.duration_days,
            "recurrence": s.recurrence, "recurrence_count": s.recurrence_count, "post_to_buzz": s.post_to_buzz,
            "questions": [{"qtype": q.qtype, "text": q.text, "help_text": q.help_text, "required": q.required,
                           "options": q.options, "settings": q.settings} for q in s.questions]}
    return create(db, actor, data)


def open_run(db: Session, s: Survey, run: SurveyRun) -> None:
    run.status = "open"
    s.status = "active"
    ids = aud.resolve(db, s.company_id, s.audience)
    have = {e for (e,) in db.query(SurveyParticipation.employee_id).filter(SurveyParticipation.run_id == run.id).all()}
    for eid in ids:
        if eid not in have:
            db.add(SurveyParticipation(company_id=s.company_id, run_id=run.id, employee_id=eid))
    run.invited_count = len(set(ids) | have)
    db.flush()
    title = f"{'Quick pulse' if s.kind == 'pulse' else 'eNPS' if s.kind == 'enps' else 'Survey'}: {s.title}"
    notify(db, s.company_id, ids, kind="survey", title=title[:200],
           body=("Your answers are anonymous. " if s.identity != "named" else "") + ("Please take part." if s.mandatory else "We'd love your view."),
           link=f"/dashboard/surveys?take={s.uuid}")
    if s.post_to_buzz:
        exists = db.query(BuzzPost.id).filter(BuzzPost.company_id == s.company_id, BuzzPost.survey_id == s.id).first()
        if not exists:
            db.add(BuzzPost(company_id=s.company_id, kind="survey", author_employee_id=s.created_by_employee_id,
                            body=s.description or "", survey_id=s.id, audience=s.audience))
            db.flush()


def close_run(db: Session, s: Survey, run: SurveyRun, manual: bool = False) -> None:
    run.status = "closed"
    if manual:
        run.closes_at = min(run.closes_at, now())
    nxt = None
    if not manual:
        count_ok = s.recurrence_count is None or run.number < s.recurrence_count
        start = next_open(s, run.opens_at) if count_ok else None
        if start is not None:
            nxt = SurveyRun(company_id=s.company_id, survey_id=s.id, number=run.number + 1, opens_at=max(start, now()),
                            closes_at=max(start, now()) + timedelta(days=s.duration_days), status="scheduled")
            db.add(nxt)
    if nxt is None and not any(r.status in ("open", "scheduled") for r in s.runs if r is not run):
        s.status = "closed"
    db.flush()


def stop(db: Session, actor: Actor, s: Survey) -> None:
    for r in s.runs:
        if r.status == "open":
            close_run(db, s, r, manual=True)
        elif r.status == "scheduled":
            db.delete(r)
    s.status = "closed"
    db.flush()
    audit.event(db, "survey.closed", "surveys", s.id, {}, entity_uuid=str(s.uuid), entity_label=s.title)


def extend(db: Session, actor: Actor, s: Survey, days: int) -> None:
    run = next((r for r in sorted(s.runs, key=lambda r: -r.number) if r.status in ("open", "closed")), None)
    if run is None:
        raise BadRequest("This survey has not opened yet.")
    if not 1 <= days <= 30:
        raise BadRequest("Extend by 1 to 30 days.")
    run.closes_at = max(run.closes_at, now()) + timedelta(days=days)
    if run.status == "closed":
        run.status = "open"
    s.status = "active"
    audit.event(db, "survey.extended", "surveys", s.id, {"days": days}, entity_uuid=str(s.uuid), entity_label=s.title)
    pending = [p.employee_id for p in db.query(SurveyParticipation).filter(SurveyParticipation.run_id == run.id,
                                                                          SurveyParticipation.status == "pending").all()]
    notify(db, s.company_id, pending, kind="survey", title=f"More time to answer: {s.title}"[:200],
           link=f"/dashboard/surveys?take={s.uuid}")


def delete_draft(db: Session, s: Survey) -> None:
    if s.status != "draft":
        raise BadRequest("Only drafts can be deleted. Close the survey instead.")
    audit.event(db, "survey.draft_deleted", "surveys", s.id, {"title": s.title}, entity_uuid=str(s.uuid), entity_label=s.title)
    db.delete(s)


def run_due(db: Session, company_id: int) -> int:
    """Open scheduled runs, close finished ones (scheduling the next recurrence) and send
    reminders for mandatory surveys. Safe to call often."""
    n = 0
    for run in db.query(SurveyRun).filter(SurveyRun.company_id == company_id, SurveyRun.status == "scheduled",
                                          SurveyRun.opens_at <= now()).all():
        s = db.query(Survey).filter(Survey.id == run.survey_id).first()
        if s.status != "closed":
            open_run(db, s, run)
            n += 1
    for run in db.query(SurveyRun).filter(SurveyRun.company_id == company_id, SurveyRun.status == "open",
                                          SurveyRun.closes_at <= now()).all():
        s = db.query(Survey).filter(Survey.id == run.survey_id).first()
        close_run(db, s, run)
        n += 1
    n += send_reminders(db, company_id)
    return n


def send_reminders(db: Session, company_id: int, survey: Survey | None = None, force: bool = False) -> int:
    q = (db.query(SurveyRun, Survey).join(Survey, Survey.id == SurveyRun.survey_id)
         .filter(SurveyRun.company_id == company_id, SurveyRun.status == "open"))
    if survey is not None:
        q = q.filter(Survey.id == survey.id)
    else:
        q = q.filter(Survey.mandatory.is_(True))
    sent = 0
    for run, s in q.all():
        every = s.reminder_every_days or int(cfg.section(db, company_id, "surveys").get("default_reminder_days") or 2)
        cutoff = now() - timedelta(days=every)
        pending = db.query(SurveyParticipation).filter(SurveyParticipation.run_id == run.id,
                                                       SurveyParticipation.status == "pending").all()
        due = [p for p in pending if force or (
            (p.last_reminded_at or run.opens_at) <= cutoff)]
        if not due:
            continue
        for p in due:
            p.reminders_sent += 1
            p.last_reminded_at = now()
        ids = [p.employee_id for p in due]
        closes = run.closes_at.strftime("%d %b")
        notify(db, company_id, ids, kind="survey", title=f"Reminder: {s.title}"[:200],
               body=f"Please answer by {closes}." + (" It is mandatory." if s.mandatory else ""),
               link=f"/dashboard/surveys?take={s.uuid}")
        if s.mandatory:
            from core.config import get_settings
            link = f"{get_settings().FRONTEND_URL.rstrip('/')}/dashboard/surveys?take={s.uuid}"
            for (email,) in db.query(Employee.email).filter(Employee.company_id == company_id, Employee.id.in_(ids)).all():
                send_email(email, f"Reminder: {s.title}", f"Please answer the survey “{s.title}” by {closes}.\n\n{link}")
        sent += len(ids)
    return sent


# ── Taking a survey ────────────────────────────────────────────────

def _open_run(s: Survey) -> SurveyRun | None:
    return next((r for r in sorted(s.runs, key=lambda r: -r.number) if r.status == "open"), None)


def my_surveys(db: Session, actor: Actor) -> dict:
    if not actor.employee_id:
        return {"pending": [], "done": []}
    rows = (db.query(SurveyParticipation, SurveyRun, Survey)
            .join(SurveyRun, SurveyRun.id == SurveyParticipation.run_id)
            .join(Survey, Survey.id == SurveyRun.survey_id)
            .filter(SurveyParticipation.company_id == actor.company_id, SurveyParticipation.employee_id == actor.employee_id)
            .order_by(SurveyRun.opens_at.desc()).limit(100).all())
    pending, done = [], []
    for p, run, s in rows:
        item = {**summary(s), "run": run.number, "closes_at": run.closes_at, "opens_at": run.opens_at,
                "status": p.status if run.status == "open" or p.status == "submitted" else "missed",
                "submitted_on": p.submitted_on}
        (pending if p.status == "pending" and run.status == "open" else done).append(item)
    return {"pending": pending, "done": done}


def for_taking(db: Session, actor: Actor, s: Survey) -> dict:
    run = _open_run(s)
    if run is None:
        raise BadRequest("This survey is not open right now.")
    part = None
    if actor.employee_id:
        part = db.query(SurveyParticipation).filter(SurveyParticipation.run_id == run.id,
                                                    SurveyParticipation.employee_id == actor.employee_id).first()
    preview = part is None
    return {**summary(s), "questions": [question_dict(q) for q in s.questions], "closes_at": run.closes_at,
            "answered": bool(part and part.status == "submitted"), "preview": preview}


def submit(db: Session, actor: Actor, s: Survey, answers: dict, show_name: bool = False) -> None:
    run = _open_run(s)
    if run is None:
        raise BadRequest("This survey is closed.")
    if not actor.employee_id:
        raise BadRequest("The company owner can preview surveys but not answer them.")
    part = (db.query(SurveyParticipation).filter(SurveyParticipation.run_id == run.id,
                                                 SurveyParticipation.employee_id == actor.employee_id)
            .with_for_update().first())
    if part is None:
        raise Forbidden("This survey is not for you.")
    if part.status == "submitted":
        raise BadRequest("You have already answered this survey. Thank you!")
    rows = []
    for q in s.questions:
        raw = answers.get(str(q.uuid))
        empty = raw is None or raw == "" or raw == []
        if empty:
            if q.required:
                raise BadRequest(f"Please answer: {q.text}")
            continue
        a = SurveyAnswer(company_id=s.company_id, question_id=q.id)
        if q.qtype == "single":
            if raw not in q.options:
                raise BadRequest(f"Pick one of the choices for: {q.text}")
            a.value_choices = [raw]
        elif q.qtype == "multi":
            if not isinstance(raw, list) or any(x not in q.options for x in raw):
                raise BadRequest(f"Pick from the choices for: {q.text}")
            a.value_choices = list(dict.fromkeys(raw))
        elif q.qtype in ("rating", "scale", "enps"):
            lo, hi = {"rating": (1, (q.settings or {}).get("max", 5)), "scale": (1, (q.settings or {}).get("points", 5)),
                      "enps": (0, 10)}[q.qtype]
            try:
                v = int(raw)
            except (TypeError, ValueError):
                raise BadRequest(f"Pick a number for: {q.text}")
            if not lo <= v <= hi:
                raise BadRequest(f"Pick a number from {lo} to {hi} for: {q.text}")
            a.value_number = Decimal(v)
        else:
            text = str(raw).strip()
            if not text:
                if q.required:
                    raise BadRequest(f"Please answer: {q.text}")
                continue
            a.value_text = text[:4000]
        rows.append(a)
    named = s.identity == "named" or (s.identity == "optional" and show_name)
    emp = db.query(Employee).filter(Employee.id == actor.employee_id).first()
    today = today_for_company_id(db, actor.company_id)
    resp = SurveyResponse(company_id=s.company_id, run_id=run.id, employee_id=actor.employee_id if named else None,
                          department_id=emp.department_id if emp else None, submitted_on=today)
    resp.answers = rows
    db.add(resp)
    part.status, part.submitted_on = "submitted", today
    db.flush()


# ── Results ────────────────────────────────────────────────────────

def question_dict(q: SurveyQuestion) -> dict:
    return {"uuid": str(q.uuid), "qtype": q.qtype, "text": q.text, "help_text": q.help_text, "required": q.required,
            "options": q.options or [], "settings": q.settings or {}}


def summary(s: Survey) -> dict:
    from modules.engagement.announcements import cover_out
    latest = max(s.runs, key=lambda r: r.number) if s.runs else None
    return {"uuid": str(s.uuid), "title": s.title, "kind": s.kind, "kind_label": KINDS[s.kind],
            "description": s.description, "thank_you": s.thank_you, "cover": cover_out(s.cover),
            "identity": s.identity, "identity_label": IDENTITY[s.identity], "mandatory": s.mandatory,
            "status": s.status, "questions_count": len(s.questions), "min_responses": s.min_responses,
            "recurrence": s.recurrence, "opens_at": latest.opens_at if latest else s.opens_at,
            "closes_at": latest.closes_at if latest else None, "runs": len(s.runs)}


def detail(db: Session, s: Survey) -> dict:
    return {**summary(s), "audience": s.audience, "audience_label": aud.describe(db, s.company_id, s.audience),
            "duration_days": s.duration_days, "recurrence_count": s.recurrence_count,
            "reminder_every_days": s.reminder_every_days, "post_to_buzz": s.post_to_buzz,
            "created_by_name": s.created_by_name, "created_at": s.created_at,
            "questions": [question_dict(q) for q in s.questions],
            "run_list": [{"uuid": str(r.uuid), "number": r.number, "opens_at": r.opens_at, "closes_at": r.closes_at,
                          "status": r.status, "invited": r.invited_count} for r in sorted(s.runs, key=lambda r: r.number)]}


def list_admin(db: Session, company_id: int, status: str | None, kind: str | None, q: str | None, page: int, size: int) -> dict:
    query = db.query(Survey).options(joinedload(Survey.runs), joinedload(Survey.questions)).filter(Survey.company_id == company_id)
    if status:
        query = query.filter(Survey.status == status)
    if kind:
        query = query.filter(Survey.kind == kind)
    if q:
        query = query.filter(Survey.title.ilike(f"%{q}%"))
    total = query.order_by(None).count()
    rows = query.order_by(Survey.created_at.desc()).offset((page - 1) * size).limit(size).all()
    run_ids = [r.id for s in rows for r in s.runs]
    responses = dict(db.query(SurveyResponse.run_id, func.count(SurveyResponse.id)).filter(
        SurveyResponse.run_id.in_(run_ids or [0])).group_by(SurveyResponse.run_id).all())
    items = []
    for s in rows:
        latest = max(s.runs, key=lambda r: r.number) if s.runs else None
        n = responses.get(latest.id, 0) if latest else 0
        items.append({**summary(s), "responses": n, "invited": latest.invited_count if latest else 0,
                      "audience_label": aud.describe(db, company_id, s.audience)})
    return {"items": items, "total": total, "page": page, "size": size}


def enps_of(scores: list[int]) -> dict:
    n = len(scores)
    if not n:
        return {"score": None, "promoters": 0, "passives": 0, "detractors": 0, "responses": 0}
    pro = sum(1 for v in scores if v >= 9)
    det = sum(1 for v in scores if v <= 6)
    return {"score": round((pro - det) * 100 / n), "promoters": pro, "passives": n - pro - det, "detractors": det,
            "responses": n, "promoters_pct": round(pro * 100 / n), "detractors_pct": round(det * 100 / n),
            "passives_pct": round((n - pro - det) * 100 / n)}


def _hidden(s: Survey, n: int) -> bool:
    return s.identity != "named" and n < s.min_responses


def results(db: Session, s: Survey, run_uuid=None) -> dict:
    runs = sorted(s.runs, key=lambda r: r.number)
    if not runs:
        return {"survey": summary(s), "run": None, "locked": False, "responses": 0, "questions": []}
    counts = dict(db.query(SurveyResponse.run_id, func.count(SurveyResponse.id)).filter(
        SurveyResponse.run_id.in_([r.id for r in runs])).group_by(SurveyResponse.run_id).all())
    if run_uuid:
        run = next((r for r in runs if str(r.uuid) == str(run_uuid)), None)
        if run is None:
            raise NotFound("Survey run")
    else:
        opened = [r for r in runs if r.status != "scheduled"]
        run = opened[-1] if opened else runs[-1]
    n = counts.get(run.id, 0)
    submitted = db.query(func.count(SurveyParticipation.id)).filter(SurveyParticipation.run_id == run.id,
                                                                    SurveyParticipation.status == "submitted").scalar()
    base = {
        "survey": summary(s), "responses": n, "invited": run.invited_count,
        "response_rate": round(submitted * 100 / run.invited_count) if run.invited_count else 0,
        "run": {"uuid": str(run.uuid), "number": run.number, "opens_at": run.opens_at, "closes_at": run.closes_at,
                "status": run.status},
        "runs": [{"uuid": str(r.uuid), "number": r.number, "opens_at": r.opens_at, "status": r.status,
                  "responses": counts.get(r.id, 0)} for r in runs],
        "trend": trend(db, s, runs, counts),
    }
    if _hidden(s, n):
        return {**base, "locked": True, "needed": s.min_responses - n, "questions": []}

    responses = db.query(SurveyResponse).options(joinedload(SurveyResponse.answers)).filter(SurveyResponse.run_id == run.id).all()
    by_q: dict[int, list] = defaultdict(list)
    for r in responses:
        for a in r.answers:
            by_q[a.question_id].append((r, a))
    named_people = people.load(db, s.company_id, [r.employee_id for r in responses])
    dept_names = _dept_names(db, s.company_id, {r.department_id for r in responses})
    questions = []
    for q in s.questions:
        items = by_q.get(q.id, [])
        d = {**question_dict(q), "answered": len(items)}
        if q.qtype in ("single", "multi"):
            c = Counter(ch for _, a in items for ch in (a.value_choices or []))
            d["choices"] = [{"label": o, "count": c.get(o, 0),
                             "pct": round(c.get(o, 0) * 100 / len(items)) if items else 0} for o in q.options]
        elif q.qtype in ("rating", "scale", "enps"):
            vals = [int(a.value_number) for _, a in items if a.value_number is not None]
            hi = 10 if q.qtype == "enps" else (q.settings or {}).get("max" if q.qtype == "rating" else "points", 5)
            lo = 0 if q.qtype == "enps" else 1
            c = Counter(vals)
            d["distribution"] = [{"value": v, "count": c.get(v, 0)} for v in range(lo, hi + 1)]
            d["average"] = round(sum(vals) / len(vals), 2) if vals else None
            if q.qtype == "scale" and vals:
                fav = sum(1 for v in vals if v > (hi + 1) / 2)
                unfav = sum(1 for v in vals if v < (hi + 1) / 2)
                d["favourable_pct"], d["unfavourable_pct"] = round(fav * 100 / len(vals)), round(unfav * 100 / len(vals))
            if q.qtype == "enps":
                d["enps"] = enps_of(vals)
            d["groups"] = _groups(s, [(r, int(a.value_number)) for r, a in items if a.value_number is not None],
                                  dept_names, q.qtype)
        else:
            texts = [{"text": a.value_text,
                      "by": people.full_name(named_people.get(r.employee_id)) if r.employee_id else None}
                     for r, a in items if a.value_text]
            if s.identity != "named":
                random.Random(run.id * 7919 + q.id).shuffle(texts)  # stable shuffle: no order clues
            d["texts"] = texts[:500]
        questions.append(d)
    return {**base, "locked": False, "questions": questions}


def _dept_names(db: Session, company_id: int, ids) -> dict:
    from modules.departments.models import Department
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return dict(db.query(Department.id, Department.department_name).filter(Department.id.in_(ids)).all())


def _groups(s: Survey, pairs: list, names: dict, qtype: str) -> list[dict]:
    """Department breakdown; groups smaller than the threshold are merged into "Other
    groups" (and dropped if even that is too small) so nobody can be singled out."""
    buckets: dict[str, list] = defaultdict(list)
    for r, v in pairs:
        buckets[names.get(r.department_id) or "No department"].append(v)
    threshold = 1 if s.identity == "named" else s.min_responses
    out, other = [], []
    for name, vals in sorted(buckets.items()):
        if len(vals) >= threshold:
            out.append((name, vals))
        else:
            other += vals
    if other and len(other) >= threshold:
        out.append(("Other groups", other))
    res = []
    for name, vals in out:
        item = {"group": name, "responses": len(vals), "average": round(sum(vals) / len(vals), 2)}
        if qtype == "enps":
            item["enps"] = enps_of(vals)["score"]
        res.append(item)
    return res if len(res) > 1 else []


def trend(db: Session, s: Survey, runs: list[SurveyRun], counts: dict) -> list[dict]:
    """eNPS (or average of the first rating/scale question) for each run that reached the
    threshold."""
    q = next((q for q in s.questions if q.qtype == "enps"), None) or next(
        (q for q in s.questions if q.qtype in ("rating", "scale")), None)
    if q is None:
        return []
    out = []
    for r in runs:
        n = counts.get(r.id, 0)
        if n == 0 or _hidden(s, n):
            continue
        vals = [int(v) for (v,) in db.query(SurveyAnswer.value_number).join(SurveyResponse, SurveyResponse.id == SurveyAnswer.response_id)
                .filter(SurveyResponse.run_id == r.id, SurveyAnswer.question_id == q.id, SurveyAnswer.value_number.isnot(None)).all()]
        if not vals:
            continue
        out.append({"run": r.number, "opens_at": r.opens_at, "responses": n,
                    "value": enps_of(vals)["score"] if q.qtype == "enps" else round(sum(vals) / len(vals), 2),
                    "metric": "enps" if q.qtype == "enps" else "average"})
    return out


def company_enps(db: Session, company_id: int) -> dict:
    """eNPS across every eNPS question of the company, run by run (for the home page and
    the surveys page header)."""
    rows = (db.query(SurveyRun, Survey).join(Survey, Survey.id == SurveyRun.survey_id)
            .filter(SurveyRun.company_id == company_id, SurveyRun.status != "scheduled")
            .order_by(SurveyRun.opens_at).all())
    points = []
    for run, s in rows:
        q = next((q for q in s.questions if q.qtype == "enps"), None)
        if q is None:
            continue
        vals = [int(v) for (v,) in db.query(SurveyAnswer.value_number).join(SurveyResponse, SurveyResponse.id == SurveyAnswer.response_id)
                .filter(SurveyResponse.run_id == run.id, SurveyAnswer.question_id == q.id, SurveyAnswer.value_number.isnot(None)).all()]
        if len(vals) < s.min_responses and s.identity != "named":
            continue
        e = enps_of(vals)
        points.append({"survey": s.title, "survey_uuid": str(s.uuid), "opens_at": run.opens_at, "score": e["score"],
                       "responses": e["responses"]})
    latest = points[-1] if points else None
    prev = points[-2] if len(points) > 1 else None
    return {"latest": latest, "change": (latest["score"] - prev["score"]) if latest and prev else None, "points": points[-12:]}


def participation(db: Session, s: Survey, run_uuid=None) -> dict:
    run = next((r for r in s.runs if str(r.uuid) == str(run_uuid)), None) if run_uuid else (
        max(s.runs, key=lambda r: r.number) if s.runs else None)
    if run is None:
        return {"invited": 0, "submitted": 0, "pending": [], "names_shown": False}
    parts = db.query(SurveyParticipation).filter(SurveyParticipation.run_id == run.id).all()
    show = s.identity == "named" or s.mandatory
    pending = [p for p in parts if p.status == "pending"]
    emps = people.load(db, s.company_id, [p.employee_id for p in pending]) if show else {}
    return {"invited": len(parts), "submitted": len(parts) - len(pending), "names_shown": show,
            "pending": [{"person": people.card(emps[p.employee_id]), "reminders": p.reminders_sent}
                        for p in pending if p.employee_id in emps][:500]}
