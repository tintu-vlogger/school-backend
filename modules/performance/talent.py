"""One-to-one meetings (shared agenda and notes, private notes per person), continuous
feedback (give, request, answer; private or shared with the receiver's manager) and
performance improvement plans (goals, duration, check-ins, outcome)."""
from __future__ import annotations

import uuid as uuid_mod
from datetime import timedelta

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy.orm.attributes import flag_modified

from core import audit
from core.errors import BadRequest, Forbidden, NotFound
from modules.performance.common import (
    Actor, emp_model, employee, is_manager_of, name_of, notify, now, person, reportee_ids, today,
)
from modules.performance.models import Feedback, OneOnOne, OneOnOneNote, Pip, PipCheckin

RECURRENCE = {"none": None, "weekly": 7, "fortnightly": 14, "monthly": 30}


# ── One-to-ones ─────────────────────────────────────────────────────

def _in_meeting(actor: Actor, m: OneOnOne) -> bool:
    if actor.is_owner:
        return m.manager_employee_id is None
    return actor.employee_id in (m.manager_employee_id, m.employee_id)


def _meeting_query(db: Session, company_id: int):
    E = emp_model()
    return (db.query(OneOnOne).options(joinedload(OneOnOne.manager).joinedload(E.designation),
                                       joinedload(OneOnOne.employee).joinedload(E.designation))
            .filter(OneOnOne.company_id == company_id))


def get_meeting(db: Session, actor: Actor, uuid) -> OneOnOne:
    m = _meeting_query(db, actor.company_id).filter(OneOnOne.uuid == uuid).first()
    if m is None or not (_in_meeting(actor, m) or actor.is_admin):
        raise NotFound("Meeting")
    return m


def meeting_out(db: Session, actor: Actor, m: OneOnOne, with_private: bool = False) -> dict:
    out = {
        "uuid": str(m.uuid), "title": m.title, "manager": person(m.manager) or {"name": "Company owner"},
        "employee": person(m.employee), "scheduled_at": m.scheduled_at.isoformat(),
        "duration_minutes": m.duration_minutes, "recurrence": m.recurrence, "status": m.status,
        "agenda": m.agenda or [], "shared_notes": m.shared_notes, "action_items": m.action_items or [],
        "is_participant": _in_meeting(actor, m),
        "role": "manager" if (actor.employee_id and actor.employee_id == m.manager_employee_id) or (actor.is_owner and m.manager_employee_id is None)
        else ("employee" if actor.employee_id == m.employee_id else "admin"),
    }
    if with_private and _in_meeting(actor, m):
        note = db.query(OneOnOneNote).filter(OneOnOneNote.company_id == m.company_id, OneOnOneNote.meeting_id == m.id,
                                             OneOnOneNote.author_key == actor.key).first()
        out["private_note"] = note.note if note else ""
    return out


def list_meetings(db: Session, actor: Actor, when: str | None = None, employee_id: int | None = None) -> list[dict]:
    q = _meeting_query(db, actor.company_id)
    if actor.is_owner:
        q = q.filter(OneOnOne.manager_employee_id.is_(None))
    else:
        q = q.filter(or_(OneOnOne.manager_employee_id == actor.employee_id, OneOnOne.employee_id == actor.employee_id))
    if employee_id:
        q = q.filter(or_(OneOnOne.employee_id == employee_id, OneOnOne.manager_employee_id == employee_id))
    at = now()
    if when == "upcoming":
        q = q.filter(OneOnOne.status == "scheduled", OneOnOne.scheduled_at >= at - timedelta(hours=12)).order_by(OneOnOne.scheduled_at)
    elif when == "past":
        q = q.filter(or_(OneOnOne.status != "scheduled", OneOnOne.scheduled_at < at - timedelta(hours=12))).order_by(OneOnOne.scheduled_at.desc())
    else:
        q = q.order_by(OneOnOne.scheduled_at.desc())
    return [meeting_out(db, actor, m) for m in q.limit(200).all()]


def create_meeting(db: Session, actor: Actor, data: dict) -> OneOnOne:
    other = data.get("employee_id")
    if not other:
        raise BadRequest("Pick who the meeting is with.")
    emp = employee(db, actor.company_id, other)
    if emp.id == actor.employee_id:
        # an employee books time with their own manager
        raise BadRequest("Pick someone other than yourself.")
    if actor.is_owner or actor.is_admin or is_manager_of(db, actor, emp.id):
        manager_id, employee_id = actor.employee_id, emp.id
    elif actor.employee_id and emp.id == employee(db, actor.company_id, actor.employee_id).reporting_manager_id:
        manager_id, employee_id = emp.id, actor.employee_id
    else:
        raise Forbidden("One-to-ones are between a manager and their team members.")
    if not data.get("scheduled_at"):
        raise BadRequest("Pick a date and time.")
    rec = data.get("recurrence") or "none"
    if rec not in RECURRENCE:
        raise BadRequest("Repeat weekly, fortnightly, monthly or not at all.")
    agenda = [{"id": uuid_mod.uuid4().hex[:8], "text": t.strip()[:300], "by": actor.name, "done": False}
              for t in (data.get("agenda") or []) if (t or "").strip()]
    m = OneOnOne(company_id=actor.company_id, manager_employee_id=manager_id, employee_id=employee_id,
                 title=(data.get("title") or f"1:1 with {name_of(emp)}").strip()[:200], scheduled_at=data["scheduled_at"],
                 duration_minutes=int(data.get("duration_minutes") or 30), recurrence=rec, agenda=agenda)
    db.add(m)
    db.flush()
    other_id = employee_id if actor.employee_id != employee_id else manager_id
    notify(db, actor.company_id, other_id, f"{actor.name} scheduled a one-to-one: {m.title}.")
    return m


def update_meeting(db: Session, actor: Actor, m: OneOnOne, data: dict) -> OneOnOne:
    if not _in_meeting(actor, m):
        raise Forbidden("Only the two people in this meeting can change it.")
    if "title" in data and (data["title"] or "").strip():
        m.title = data["title"].strip()[:200]
    if data.get("scheduled_at"):
        m.scheduled_at = data["scheduled_at"]
    if data.get("duration_minutes"):
        m.duration_minutes = int(data["duration_minutes"])
    if data.get("recurrence") in RECURRENCE:
        m.recurrence = data["recurrence"]
    if "shared_notes" in data:
        m.shared_notes = (data.get("shared_notes") or "")[:20000] or None
    if "agenda" in data and data["agenda"] is not None:
        m.agenda = _clean_items(data["agenda"], actor)
        flag_modified(m, "agenda")
    if "action_items" in data and data["action_items"] is not None:
        m.action_items = _clean_items(data["action_items"], actor)
        flag_modified(m, "action_items")
    if data.get("status") in ("scheduled", "cancelled"):
        m.status = data["status"]
    db.flush()
    return m


def _clean_items(items: list[dict], actor: Actor) -> list[dict]:
    out = []
    for it in items[:100]:
        t = (it.get("text") or "").strip()
        if t:
            out.append({"id": it.get("id") or uuid_mod.uuid4().hex[:8], "text": t[:300], "by": it.get("by") or actor.name,
                        "done": bool(it.get("done")), "owner": it.get("owner")})
    return out


def save_private_note(db: Session, actor: Actor, m: OneOnOne, note: str | None) -> None:
    if not _in_meeting(actor, m):
        raise Forbidden("Only people in the meeting keep private notes on it.")
    row = db.query(OneOnOneNote).filter(OneOnOneNote.company_id == m.company_id, OneOnOneNote.meeting_id == m.id,
                                        OneOnOneNote.author_key == actor.key).first()
    if row is None:
        row = OneOnOneNote(company_id=m.company_id, meeting_id=m.id, author_key=actor.key)
        db.add(row)
    row.note = (note or "")[:20000] or None
    db.flush()


def complete_meeting(db: Session, actor: Actor, m: OneOnOne) -> OneOnOne | None:
    """Mark done; a repeating meeting gets its next one with open agenda and actions carried over."""
    if not _in_meeting(actor, m):
        raise Forbidden("Only the two people in this meeting can change it.")
    if m.status != "scheduled":
        raise BadRequest("This meeting is not scheduled.")
    m.status = "done"
    nxt = None
    days = RECURRENCE.get(m.recurrence)
    if days:
        carry = [{**a, "id": uuid_mod.uuid4().hex[:8]} for a in (m.agenda or []) if not a.get("done")]
        carry += [{"id": uuid_mod.uuid4().hex[:8], "text": f"Follow up: {a['text']}", "by": a.get("by"), "done": False}
                  for a in (m.action_items or []) if not a.get("done")]
        nxt = OneOnOne(company_id=m.company_id, manager_employee_id=m.manager_employee_id, employee_id=m.employee_id,
                       title=m.title, scheduled_at=m.scheduled_at + timedelta(days=days), duration_minutes=m.duration_minutes,
                       recurrence=m.recurrence, agenda=carry[:50])
        db.add(nxt)
    db.flush()
    return nxt


# ── Feedback ────────────────────────────────────────────────────────

def _fb_query(db: Session, company_id: int):
    return (db.query(Feedback).options(joinedload(Feedback.giver), joinedload(Feedback.receiver), joinedload(Feedback.requester))
            .filter(Feedback.company_id == company_id))


def feedback_out(f: Feedback, actor: Actor) -> dict:
    return {"uuid": str(f.uuid), "giver": person(f.giver) or {"name": "Company owner"}, "receiver": person(f.receiver),
            "requested_by": person(f.requester), "status": f.status, "prompt": f.prompt, "message": f.message,
            "kind": f.kind, "visibility": f.visibility, "competency_id": f.competency_id,
            "given_at": f.given_at.isoformat() if f.given_at else None, "created_at": f.created_at.isoformat(),
            "mine_to_answer": f.status == "requested" and (f.giver_employee_id == actor.employee_id)}


def _kudos(db: Session, actor: Actor, receiver_id: int, message: str) -> bool:
    """Also post praise as kudos (engagement module's kudos.give, which posts it on Buzz
    and notifies). Runs in a savepoint: if kudos are off, the monthly limit is used up or
    the message is refused, the feedback itself is still saved."""
    try:
        from modules.engagement import kudos as engagement_kudos
        from modules.engagement.people import Actor as EngageActor
    except ImportError:
        return False
    rec = employee(db, actor.company_id, receiver_id, required=False)
    if rec is None:
        return False
    try:
        with db.begin_nested():
            engagement_kudos.give(db, EngageActor(company_id=actor.company_id, employee_id=actor.employee_id,
                                                  role=actor.role, name=actor.name),
                                  {"recipients": [str(rec.uuid)], "message": message[:1000]})
        return True
    except Exception:  # kudos is a courtesy; feedback is already saved
        return False


def give_feedback(db: Session, actor: Actor, data: dict) -> Feedback:
    rid = data.get("receiver_employee_id")
    if not rid:
        raise BadRequest("Pick who the feedback is for.")
    if rid == actor.employee_id:
        raise BadRequest("Feedback is for someone else.")
    rec = employee(db, actor.company_id, rid)
    msg = (data.get("message") or "").strip()
    if len(msg) < 3:
        raise BadRequest("Write your feedback.")
    vis = data.get("visibility") or "private"
    if vis not in ("private", "manager"):
        raise BadRequest("Share privately or with their manager too.")
    kind = data.get("kind") or "feedback"
    if kind not in ("feedback", "praise", "suggestion"):
        raise BadRequest("Pick praise, suggestion or general feedback.")
    f = Feedback(company_id=actor.company_id, giver_employee_id=actor.employee_id, receiver_employee_id=rec.id,
                 status="given", message=msg[:5000], kind=kind, visibility=vis, competency_id=data.get("competency_id"),
                 given_at=now())
    db.add(f)
    db.flush()
    notify(db, actor.company_id, rec.id, f"{actor.name} sent you feedback.")
    if vis == "manager" and rec.reporting_manager_id:
        notify(db, actor.company_id, rec.reporting_manager_id, f"{actor.name} shared feedback about {name_of(rec)}.")
    if data.get("also_kudos") and kind == "praise" and actor.employee_id:
        _kudos(db, actor, rec.id, msg)
    return f


def request_feedback(db: Session, actor: Actor, data: dict) -> list[Feedback]:
    if actor.employee_id is None:
        raise BadRequest("Only employees can ask for feedback.")
    about = data.get("about_employee_id") or actor.employee_id
    if about != actor.employee_id and not is_manager_of(db, actor, about):
        raise Forbidden("You can ask for feedback about yourself or your team.")
    givers = [g for g in dict.fromkeys(data.get("giver_employee_ids") or []) if g != about]
    if not givers:
        raise BadRequest("Pick at least one person to ask.")
    if len(givers) > 10:
        raise BadRequest("Ask at most 10 people at once.")
    prompt = (data.get("prompt") or "").strip() or "What should I keep doing, and what could I do better?"
    out = []
    for g in givers:
        employee(db, actor.company_id, g)
        f = Feedback(company_id=actor.company_id, giver_employee_id=g, receiver_employee_id=about,
                     requested_by_employee_id=actor.employee_id, status="requested", prompt=prompt[:1000],
                     visibility=data.get("visibility") or "private")
        db.add(f)
        out.append(f)
        notify(db, actor.company_id, g, f"{actor.name} asked you for feedback.")
    db.flush()
    return out


def answer_feedback(db: Session, actor: Actor, f: Feedback, message: str | None, decline: bool = False) -> None:
    if f.status != "requested" or f.giver_employee_id != actor.employee_id:
        raise BadRequest("This request isn't waiting for you.")
    if decline:
        f.status = "declined"
    else:
        msg = (message or "").strip()
        if len(msg) < 3:
            raise BadRequest("Write your feedback.")
        f.message, f.status, f.given_at = msg[:5000], "given", now()
        notify(db, actor.company_id, f.requested_by_employee_id or f.receiver_employee_id, f"{actor.name} answered your feedback request.")
    db.flush()


def get_feedback(db: Session, actor: Actor, uuid) -> Feedback:
    f = _fb_query(db, actor.company_id).filter(Feedback.uuid == uuid).first()
    if f is None:
        raise NotFound("Feedback")
    return f


def list_feedback(db: Session, actor: Actor, box: str) -> list[dict]:
    """box: received | given | requests (waiting for me) | asked (I asked) | team."""
    q = _fb_query(db, actor.company_id)
    me = actor.employee_id
    if box == "received":
        q = q.filter(Feedback.receiver_employee_id == me, Feedback.status == "given")
    elif box == "given":
        q = q.filter(Feedback.giver_employee_id == me, Feedback.status == "given") if me else q.filter(Feedback.giver_employee_id.is_(None))
    elif box == "requests":
        q = q.filter(Feedback.giver_employee_id == me, Feedback.status == "requested")
    elif box == "asked":
        q = q.filter(Feedback.requested_by_employee_id == me)
    elif box == "team":
        ids = reportee_ids(db, actor.company_id, me, deep=True) if me else set()
        if actor.is_admin and not ids:
            q = q.filter(Feedback.status == "given", Feedback.visibility == "manager")
        else:
            q = q.filter(Feedback.receiver_employee_id.in_(ids or {0}), Feedback.status == "given", Feedback.visibility == "manager")
    else:
        raise BadRequest("Unknown list.")
    return [feedback_out(f, actor) for f in q.order_by(Feedback.created_at.desc()).limit(200).all()]


# ── Improvement plans ───────────────────────────────────────────────

def _pip_query(db: Session, company_id: int):
    E = emp_model()
    return (db.query(Pip).options(joinedload(Pip.employee).joinedload(E.designation), joinedload(Pip.manager),
                                  selectinload(Pip.checkins))
            .filter(Pip.company_id == company_id))


def _can_manage_pip(db: Session, actor: Actor, employee_id: int) -> bool:
    return actor.is_admin or is_manager_of(db, actor, employee_id)


def pip_out(db: Session, actor: Actor, p: Pip) -> dict:
    t = today(db, p.company_id)
    total = max((p.end_date - p.start_date).days, 1)
    elapsed = min(max((t - p.start_date).days, 0), total)
    goals = p.goals or []
    return {
        "uuid": str(p.uuid), "employee": person(p.employee), "manager": person(p.manager), "reason": p.reason,
        "start_date": p.start_date.isoformat(), "end_date": p.end_date.isoformat(), "status": p.status,
        "goals": goals, "support": p.support, "outcome": p.outcome, "outcome_note": p.outcome_note,
        "days_left": (p.end_date - t).days, "time_pct": round(elapsed / total * 100),
        "goals_met": sum(1 for g in goals if g.get("status") == "met"),
        "acknowledged_at": p.acknowledged_at.isoformat() if p.acknowledged_at else None,
        "checkins": [{"uuid": str(c.uuid), "date": c.checkin_date.isoformat(), "rating": c.rating, "note": c.note,
                      "author": c.author_name} for c in p.checkins],
        "can_manage": _can_manage_pip(db, actor, p.employee_id), "is_mine": actor.employee_id == p.employee_id,
    }


def list_pips(db: Session, actor: Actor, status: str | None = None) -> list[dict]:
    q = _pip_query(db, actor.company_id)
    if not actor.is_admin:
        ids = reportee_ids(db, actor.company_id, actor.employee_id, deep=True)
        if actor.employee_id:
            ids.add(actor.employee_id)
        q = q.filter(Pip.employee_id.in_(ids or {0}))
        q = q.filter(or_(Pip.status != "draft", Pip.employee_id != actor.employee_id))
    if status:
        q = q.filter(Pip.status == status)
    return [pip_out(db, actor, p) for p in q.order_by(Pip.start_date.desc()).all()]


def get_pip(db: Session, actor: Actor, uuid) -> Pip:
    p = _pip_query(db, actor.company_id).filter(Pip.uuid == uuid).first()
    if p is None or not (_can_manage_pip(db, actor, p.employee_id) or (actor.employee_id == p.employee_id and p.status != "draft")):
        raise NotFound("Improvement plan")
    return p


def _clean_goals(goals: list[dict]) -> list[dict]:
    out = []
    for g in goals or []:
        t = (g.get("title") or "").strip()
        if t:
            st = g.get("status") or "open"
            if st not in ("open", "met", "not_met"):
                st = "open"
            out.append({"id": g.get("id") or uuid_mod.uuid4().hex[:8], "title": t[:300], "measure": (g.get("measure") or "")[:500],
                        "due_date": g.get("due_date") or None, "status": st})
    return out


def save_pip(db: Session, actor: Actor, data: dict, p: Pip | None = None) -> Pip:
    eid = p.employee_id if p else data.get("employee_id")
    if not eid:
        raise BadRequest("Pick the employee.")
    if not _can_manage_pip(db, actor, eid):
        raise Forbidden("Only the manager or HR can set up an improvement plan.")
    emp = employee(db, actor.company_id, eid)
    reason = (data.get("reason") if "reason" in data else (p.reason if p else "")) or ""
    if not reason.strip():
        raise BadRequest("Say why the plan is needed.")
    s, e = data.get("start_date") or (p.start_date if p else None), data.get("end_date") or (p.end_date if p else None)
    if not s or not e or e <= s:
        raise BadRequest("Give a start date and an end date after it.")
    goals = _clean_goals(data["goals"]) if "goals" in data else (p.goals if p else [])
    if not goals:
        raise BadRequest("Add at least one goal.")
    new = p is None
    if new:
        p = Pip(company_id=actor.company_id, employee_id=emp.id, manager_employee_id=emp.reporting_manager_id or actor.employee_id,
                status="draft" if data.get("draft") else "active")
        db.add(p)
    if p.status == "closed":
        raise BadRequest("This plan is closed.")
    p.reason, p.start_date, p.end_date, p.goals = reason.strip()[:5000], s, e, goals
    if "support" in data:
        p.support = data.get("support")
    flag_modified(p, "goals")
    db.flush()
    if new and p.status == "active":
        notify(db, actor.company_id, emp.id, "An improvement plan has been shared with you. Please read and acknowledge it.")
        audit.event(db, "pip.started", "perf_pips", p.id, {"employee_id": emp.id}, entity_uuid=str(p.uuid))
    return p


def activate_pip(db: Session, actor: Actor, p: Pip) -> None:
    if not _can_manage_pip(db, actor, p.employee_id):
        raise Forbidden()
    if p.status != "draft":
        raise BadRequest("Only a draft plan can be started.")
    p.status = "active"
    notify(db, actor.company_id, p.employee_id, "An improvement plan has been shared with you. Please read and acknowledge it.")
    audit.event(db, "pip.started", "perf_pips", p.id, {"employee_id": p.employee_id}, entity_uuid=str(p.uuid))
    db.flush()


def pip_checkin(db: Session, actor: Actor, p: Pip, data: dict) -> PipCheckin:
    if not _can_manage_pip(db, actor, p.employee_id):
        raise Forbidden("Only the manager or HR records check-ins.")
    if p.status not in ("active", "extended"):
        raise BadRequest("Check-ins are for active plans.")
    note = (data.get("note") or "").strip()
    if not note:
        raise BadRequest("Write a short note.")
    rating = data.get("rating")
    if rating and rating not in ("on_track", "some_progress", "off_track"):
        raise BadRequest("Pick on track, some progress or off track.")
    c = PipCheckin(company_id=actor.company_id, pip_id=p.id, checkin_date=data.get("checkin_date") or today(db, actor.company_id),
                   rating=rating, note=note[:5000], author_name=actor.name)
    p.checkins.append(c)
    for upd in data.get("goal_status") or []:
        for g in p.goals or []:
            if g["id"] == upd.get("id") and upd.get("status") in ("open", "met", "not_met"):
                g["status"] = upd["status"]
    flag_modified(p, "goals")
    db.flush()
    notify(db, actor.company_id, p.employee_id, "Your manager added a check-in to your improvement plan.")
    return c


def close_pip(db: Session, actor: Actor, p: Pip, outcome: str, note: str | None, new_end=None) -> None:
    if not _can_manage_pip(db, actor, p.employee_id):
        raise Forbidden()
    if p.status not in ("active", "extended"):
        raise BadRequest("This plan is not running.")
    if outcome == "extended":
        if not new_end or new_end <= p.end_date:
            raise BadRequest("Pick a new end date after the current one.")
        p.end_date, p.status, p.outcome = new_end, "extended", None
    elif outcome in ("successful", "unsuccessful"):
        p.status, p.outcome, p.closed_at = "closed", outcome, now()
    else:
        raise BadRequest("Pick successful, extended or unsuccessful.")
    p.outcome_note = (note or "").strip() or p.outcome_note
    audit.event(db, "pip.outcome", "perf_pips", p.id, {"outcome": outcome, "note": p.outcome_note}, entity_uuid=str(p.uuid))
    notify(db, actor.company_id, p.employee_id, f"Your improvement plan was {'extended' if outcome == 'extended' else 'closed'}.")
    db.flush()


def acknowledge_pip(db: Session, actor: Actor, p: Pip) -> None:
    if actor.employee_id != p.employee_id:
        raise Forbidden()
    p.acknowledged_at = p.acknowledged_at or now()
    db.flush()
