"""Live online classes: scheduling (one-off or weekly), built-in Jitsi video rooms or external
Zoom / Google Meet / Microsoft Teams links, the join log (attendance), recordings and the
"starting soon" reminder job.

Each LiveClass row is one occurrence; a weekly schedule creates one row per week sharing a
`series_uuid`, so attendance, recording and cancellation work per occurrence.
"""
from __future__ import annotations

import secrets
import uuid as uuidlib
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from core import audit
from core.errors import BadRequest, Forbidden, NotFound
from core.timeutil import iso_utc, utc_now
from modules.academics import service as academics
from modules.academics.models import Section
from modules.lms import service as svc
from modules.lms.models import LiveClass, LiveClassJoin
from modules.students import service as students

MAX_OCCURRENCES = 60
PROVIDER_LABELS = {"jitsi": "Built-in video room", "zoom": "Zoom", "meet": "Google Meet", "teams": "Microsoft Teams",
                   "other": "Other link"}


def new_room_name(prefix: str) -> str:
    """Unguessable room name (96 random bits)."""
    return f"{prefix or 'campusly'}-{secrets.token_hex(12)}"


def query(db: Session, user: dict):
    q = db.query(LiveClass).options(joinedload(LiveClass.section).joinedload(Section.program),
                                    joinedload(LiveClass.subject), joinedload(LiveClass.teacher)).filter(
        LiveClass.company_id == user["company_id"])
    scope = svc.staff_section_scope(db, user)
    if scope is not None:
        q = q.filter(or_(LiveClass.section_id.in_(scope or [-1]), LiveClass.teacher_id == user.get("employee_id")))
    return q


def get(db: Session, user: dict, uuid) -> LiveClass:
    row = query(db, user).filter(LiveClass.uuid == uuid).first()
    if row is None:
        raise NotFound("Class")
    return row


def phase(c: LiveClass, now: datetime | None = None, early_min: int = 10) -> str:
    """upcoming | open (joinable now) | live | ended | cancelled"""
    now = now or utc_now()
    if c.status == "cancelled":
        return "cancelled"
    if c.status == "completed":
        return "ended"
    start, end = svc.as_utc(c.starts_at), svc.as_utc(c.ends_at)
    if now > end + timedelta(minutes=30):
        return "ended"
    if c.status == "live":
        return "live"
    if now >= start - timedelta(minutes=early_min):
        return "open"
    return "upcoming"


def out(db: Session, c: LiveClass, cfg: dict | None = None, joins: dict | None = None, portal: bool = False) -> dict:
    cfg = cfg or svc.settings(db, c.company_id)
    d = {
        "id": c.id, "uuid": str(c.uuid), "series_uuid": str(c.series_uuid) if c.series_uuid else None,
        "title": c.title, "agenda": c.agenda,
        "section_id": c.section_id, "section_label": academics.section_label(c.section),
        "subject_id": c.subject_id, "subject_name": c.subject.name if c.subject else None,
        "teacher_id": c.teacher_id, "teacher_name": svc._emp_name(c.teacher),
        "starts_at": iso_utc(c.starts_at), "ends_at": iso_utc(c.ends_at),
        "duration_min": int((svc.as_utc(c.ends_at) - svc.as_utc(c.starts_at)).total_seconds() // 60),
        "provider": c.provider, "provider_label": PROVIDER_LABELS.get(c.provider, c.provider),
        "status": c.status, "phase": phase(c, early_min=cfg["join_early_minutes"]),
        "recording_url": c.recording_url, "started_at": iso_utc(c.started_at), "ended_at": iso_utc(c.ended_at),
        "cancel_reason": c.cancel_reason, "notes": c.notes if not portal else None,
    }
    if not portal:
        d.update({"join_url": c.join_url, "meeting_code": c.meeting_code, "passcode": c.passcode,
                  "room_name": c.room_name, "joined": (joins or {}).get(c.id)})
    return d


def join_counts(db: Session, ids: list[int]) -> dict[int, int]:
    from sqlalchemy import func
    if not ids:
        return {}
    return dict(db.query(LiveClassJoin.live_class_id, func.count(LiveClassJoin.id))
                .filter(LiveClassJoin.live_class_id.in_(ids), LiveClassJoin.role == "student")
                .group_by(LiveClassJoin.live_class_id).all())


def _check_provider(cfg: dict, data: dict) -> None:
    provider = data.get("provider") or cfg["video_provider"]
    if provider not in svc.PROVIDERS:
        raise BadRequest("Pick a video provider from the list.")
    data["provider"] = provider
    if provider != "jitsi":
        url = (data.get("join_url") or "").strip()
        if not url.startswith("https://"):
            raise BadRequest("Paste the meeting link (it starts with https://).")
        data["join_url"] = url


def create(db: Session, user: dict, data: dict) -> list[LiveClass]:
    """One class, or one per week until `repeat_until` on the chosen weekdays."""
    cid = user["company_id"]
    cfg = svc.settings(db, cid)
    svc.require_teaching(db, user, [data["section_id"]], data.get("subject_id"))
    _check_provider(cfg, data)
    starts = svc.as_utc(data["starts_at"])
    minutes = int(data.get("duration_min") or 40)
    if minutes < 5 or minutes > 480:
        raise BadRequest("A class lasts between 5 minutes and 8 hours.")
    if starts < utc_now() - timedelta(minutes=5):
        raise BadRequest("Pick a start time in the future.")
    teacher_id = data.get("teacher_id") or user.get("employee_id")
    if not svc.is_manager(db, user):
        teacher_id = user.get("employee_id")
    occurrences = [starts]
    if data.get("repeat") == "weekly":
        until: date | None = data.get("repeat_until")
        if not until:
            raise BadRequest("Pick the date the weekly classes end.")
        tz = svc.company_tz(db, cid)
        local = starts.astimezone(tz)
        weekdays = set(data.get("weekdays") or [local.isoweekday()])
        if not weekdays <= set(range(1, 8)):
            raise BadRequest("Pick days of the week.")
        occurrences = []
        day = local.date()
        while day <= until and len(occurrences) < MAX_OCCURRENCES:
            if day.isoweekday() in weekdays:
                occurrences.append(datetime(day.year, day.month, day.day, local.hour, local.minute, tzinfo=tz)
                                   .astimezone(timezone.utc))
            day += timedelta(days=1)
        if not occurrences:
            raise BadRequest("No class falls between the start date and the end date.")
    series = uuidlib.uuid4() if len(occurrences) > 1 else None
    rows = []
    for at in occurrences:
        row = LiveClass(company_id=cid, series_uuid=series, title=data["title"].strip(), agenda=data.get("agenda"),
                        section_id=data["section_id"], subject_id=data.get("subject_id"), teacher_id=teacher_id,
                        starts_at=at, ends_at=at + timedelta(minutes=minutes), provider=data["provider"],
                        room_name=new_room_name(cfg["jitsi_room_prefix"]) if data["provider"] == "jitsi" else None,
                        join_url=data.get("join_url") if data["provider"] != "jitsi" else None,
                        meeting_code=data.get("meeting_code"), passcode=data.get("passcode"), notes=data.get("notes"),
                        created_by_employee_id=user.get("employee_id"))
        db.add(row)
        rows.append(row)
    db.flush()
    _notify_scheduled(db, rows)
    return rows


def _notify_scheduled(db: Session, rows: list[LiveClass]) -> None:
    from modules.notifications.service import notify_portal
    if not rows:
        return
    first = rows[0]
    sids = [s.id for s, _ in students.section_students(db, first.company_id, first.section_id)]
    accounts = students.portal_account_ids_for_students(db, first.company_id, sids, parents=True, students=True)
    tz = svc.company_tz(db, first.company_id)
    when = svc.as_utc(first.starts_at).astimezone(tz).strftime("%a %d %b, %I:%M %p")
    more = f" and {len(rows) - 1} more weekly" if len(rows) > 1 else ""
    notify_portal(db, first.company_id, accounts, kind="lms.live_class", title=f"Live class: {first.title}",
                  body=f"{when}{more}.", link="/dashboard/my/classes")


def update(db: Session, user: dict, c: LiveClass, data: dict) -> LiveClass:
    svc.require_teaching(db, user, [data.get("section_id") or c.section_id], data.get("subject_id", c.subject_id))
    if c.status in ("cancelled", "completed") and set(data) - {"recording_url", "notes"}:
        raise BadRequest("This class has ended or was cancelled; only the recording and notes can change.")
    cfg = svc.settings(db, c.company_id)
    if "provider" in data or "join_url" in data:
        merged = {"provider": data.get("provider", c.provider), "join_url": data.get("join_url", c.join_url)}
        _check_provider(cfg, merged)
        data.update(merged)
        if merged["provider"] == "jitsi":
            data["join_url"] = None
            if not c.room_name:
                c.room_name = new_room_name(cfg["jitsi_room_prefix"])
    minutes = data.pop("duration_min", None)
    if "starts_at" in data and data["starts_at"] is not None:
        span = minutes or int((svc.as_utc(c.ends_at) - svc.as_utc(c.starts_at)).total_seconds() // 60)
        data["starts_at"] = svc.as_utc(data["starts_at"])
        data["ends_at"] = data["starts_at"] + timedelta(minutes=span)
        c.reminder_sent_at = None
    elif minutes:
        data["ends_at"] = svc.as_utc(c.starts_at) + timedelta(minutes=minutes)
    if "teacher_id" in data and not svc.is_manager(db, user):
        data.pop("teacher_id")
    for k, v in data.items():
        setattr(c, k, v)
    db.flush()
    if c.recording_url and "recording_url" in data:
        from modules.notifications.service import notify_portal
        sids = [s.id for s, _ in students.section_students(db, c.company_id, c.section_id)]
        notify_portal(db, c.company_id, students.portal_account_ids_for_students(db, c.company_id, sids, parents=False),
                      kind="lms.recording", title=f"Recording ready: {c.title}", body="Watch it any time.",
                      link="/dashboard/my/classes")
    return c


def cancel(db: Session, user: dict, c: LiveClass, reason: str | None, whole_series: bool = False) -> int:
    svc.require_teaching(db, user, [c.section_id], c.subject_id)
    rows = [c]
    if whole_series and c.series_uuid:
        rows = db.query(LiveClass).filter(LiveClass.company_id == c.company_id, LiveClass.series_uuid == c.series_uuid,
                                          LiveClass.status == "scheduled", LiveClass.starts_at >= utc_now()).all()
        if c not in rows and c.status == "scheduled":
            rows.append(c)
    n = 0
    for r in rows:
        if r.status in ("completed", "cancelled"):
            continue
        r.status, r.cancel_reason = "cancelled", (reason or "").strip() or None
        n += 1
    if n:
        from modules.notifications.service import notify_portal
        sids = [s.id for s, _ in students.section_students(db, c.company_id, c.section_id)]
        notify_portal(db, c.company_id, students.portal_account_ids_for_students(db, c.company_id, sids),
                      kind="lms.live_class", title=f"Class cancelled: {c.title}",
                      body=(reason or "") + (f" ({n} classes)" if n > 1 else ""), link="/dashboard/my/classes")
        audit.event(db, "lms.live_class_cancelled", "lms_live_classes", c.id, {"count": n, "reason": reason},
                    entity_label=c.title)
    db.flush()
    return n


def end(db: Session, user: dict, c: LiveClass) -> LiveClass:
    svc.require_teaching(db, user, [c.section_id], c.subject_id)
    if c.status == "cancelled":
        raise BadRequest("This class was cancelled.")
    c.status, c.ended_at = "completed", utc_now()
    db.flush()
    return c


def can_portal_open(db: Session, user: dict, learner: svc.Learner, c: LiveClass) -> None:
    if c.section_id != learner.section_id:
        raise NotFound("Class")


def join(db: Session, user: dict, c: LiveClass, learner: svc.Learner | None = None) -> dict:
    """Open the class: record who joined (attendance) and hand back what the join page needs."""
    cfg = svc.settings(db, c.company_id)
    now = utc_now()
    if c.status == "cancelled":
        raise BadRequest("This class was cancelled.")
    start, end_ = svc.as_utc(c.starts_at), svc.as_utc(c.ends_at)
    is_teacher = learner is None
    if learner is not None:
        if user.get("role") == "parent" and not cfg["parents_can_join"]:
            raise Forbidden("Only the learner can join this class.")
        if now < start - timedelta(minutes=cfg["join_early_minutes"]):
            raise BadRequest(f"The class opens {cfg['join_early_minutes']} minutes before it starts.")
        if c.status == "completed" or now > end_ + timedelta(minutes=30):
            raise BadRequest("This class has ended.")
    elif now < start - timedelta(hours=1):
        raise BadRequest("You can open the room up to an hour before the class.")
    if learner is not None:
        key = f"s:{learner.student.id}" if user.get("role") == "student" else f"p:{user['portal_id']}"
        role, name = user.get("role"), learner.student.full_name
        student_id, employee_id = learner.student.id, None
    else:
        emp = user.get("employee_id")
        key = f"e:{emp}" if emp else "o:owner"
        role = "teacher" if emp and emp == c.teacher_id else "staff"
        name = svc.actor_name()
        student_id, employee_id = None, emp
        if role == "teacher" and c.status == "scheduled":
            c.status, c.started_at = "live", now
    row = db.query(LiveClassJoin).filter(LiveClassJoin.live_class_id == c.id, LiveClassJoin.who_key == key).first()
    if row is None:
        db.add(LiveClassJoin(company_id=c.company_id, live_class_id=c.id, who_key=key, role=role, student_id=student_id,
                             employee_id=employee_id, portal_account_id=user.get("portal_id"), name=name,
                             first_joined_at=now, last_joined_at=now))
    else:
        row.last_joined_at, row.join_count = now, (row.join_count or 0) + 1
    db.flush()
    info = out(db, c, cfg, portal=not is_teacher)
    info.update({
        "display_name": name, "is_moderator": is_teacher,
        "jitsi_domain": cfg["jitsi_domain"] if c.provider == "jitsi" else None,
        "room_name": c.room_name if c.provider == "jitsi" else None,
        "join_url": c.join_url if c.provider != "jitsi" else f"https://{cfg['jitsi_domain']}/{c.room_name}",
        "meeting_code": c.meeting_code, "passcode": c.passcode,
    })
    return info


def attendance(db: Session, c: LiveClass) -> dict:
    joins = db.query(LiveClassJoin).filter(LiveClassJoin.live_class_id == c.id).all()
    by_student = {j.student_id: j for j in joins if j.student_id and j.role == "student"}
    roster = []
    for s, e in students.section_students(db, c.company_id, c.section_id):
        j = by_student.get(s.id)
        roster.append({"student_id": s.id, "student_uuid": str(s.uuid), "name": s.full_name, "roll_no": e.roll_no,
                       "joined": j is not None, "first_joined_at": iso_utc(j.first_joined_at) if j else None,
                       "last_joined_at": iso_utc(j.last_joined_at) if j else None,
                       "join_count": j.join_count if j else 0})
    others = [{"name": j.name, "role": j.role, "first_joined_at": iso_utc(j.first_joined_at),
               "join_count": j.join_count} for j in joins if j.role != "student"]
    present = sum(1 for r in roster if r["joined"])
    return {"roster": roster, "others": others, "present": present, "total": len(roster)}


def portal_list(db: Session, learner: svc.Learner, when: str = "upcoming", limit: int = 50) -> list[LiveClass]:
    if not learner.section_id:
        return []
    q = (db.query(LiveClass).options(joinedload(LiveClass.section).joinedload(Section.program),
                                     joinedload(LiveClass.subject), joinedload(LiveClass.teacher))
         .filter(LiveClass.company_id == learner.student.company_id, LiveClass.section_id == learner.section_id))
    now = utc_now()
    if when == "past":
        q = q.filter(LiveClass.ends_at < now - timedelta(minutes=30)).order_by(LiveClass.starts_at.desc())
    else:
        q = q.filter(LiveClass.ends_at >= now - timedelta(minutes=30), LiveClass.status != "cancelled").order_by(LiveClass.starts_at)
    return q.limit(limit).all()


def portal_joined(db: Session, learner: svc.Learner, ids: list[int]) -> set[int]:
    if not ids:
        return set()
    return {i for (i,) in db.query(LiveClassJoin.live_class_id).filter(
        LiveClassJoin.live_class_id.in_(ids), LiveClassJoin.student_id == learner.student.id).all()}


# ── Background job ───────────────────────────────────────────────────

def send_reminders(db: Session) -> int:
    """'Starting soon' to learners, parents and the teacher; close classes long past their end."""
    from modules.notifications.service import notify, notify_portal
    now = utc_now()
    done = 0
    rows = (db.query(LiveClass).filter(LiveClass.status == "scheduled", LiveClass.reminder_sent_at.is_(None),
                                       LiveClass.starts_at > now, LiveClass.starts_at <= now + timedelta(hours=4)).all())
    cfgs: dict[int, dict] = {}
    for c in rows:
        cfg = cfgs.setdefault(c.company_id, svc.settings(db, c.company_id))
        lead = max(int(cfg["reminder_minutes"]), 15)  # the job runs every 15 minutes
        if svc.as_utc(c.starts_at) > now + timedelta(minutes=lead):
            continue
        db.info["company_id"] = c.company_id
        mins = max(int((svc.as_utc(c.starts_at) - now).total_seconds() // 60), 1)
        sids = [s.id for s, _ in students.section_students(db, c.company_id, c.section_id)]
        notify_portal(db, c.company_id, students.portal_account_ids_for_students(db, c.company_id, sids),
                      kind="lms.starting_soon", title=f"Starting in {mins} min: {c.title}",
                      body="Open Online classes and press Join.", link=f"/dashboard/my/classes/{c.uuid}/join")
        if c.teacher_id:
            notify(db, c.company_id, [c.teacher_id], kind="lms.starting_soon", title=f"Your class starts in {mins} min",
                   body=c.title, link=f"/dashboard/lms/online-classes/{c.uuid}/join")
        c.reminder_sent_at = now
        done += 1
    stale = db.query(LiveClass).filter(LiveClass.status == "live", LiveClass.ends_at < now - timedelta(hours=1)).all()
    for c in stale:
        c.status, c.ended_at = "completed", c.ended_at or svc.as_utc(c.ends_at)
        done += 1
    db.info.pop("company_id", None)
    db.flush()
    return done
