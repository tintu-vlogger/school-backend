"""Parent and student portal API (/comms/portal/...). Every endpoint checks that the person
only reaches their own records (a parent: their children; a learner: themselves)."""
from datetime import date, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from sqlalchemy.orm import Session

from core.crud import get_or_404
from core.db import get_db
from core.pagination import PageParams, paginate
from core.security import require_portal
from modules.comms import events, messaging, notices, ptm, service
from modules.comms.models import DiaryEntry, Event, Notice, NoticeRecipient, PtmBooking, PtmSlot, Thread
from modules.documents import storage

router = APIRouter(prefix="/portal")
portal_user = require_portal("parent", "student")


def _ctx(db: Session, user: dict) -> service.PortalCtx:
    return service.portal_context(db, user)


def _check_child(ctx: service.PortalCtx, student_id: int | None) -> None:
    if student_id is not None and student_id not in ctx.children:
        raise HTTPException(status_code=404, detail="Student not found")


@router.post("/uploads", status_code=201)
async def upload(file: UploadFile = File(...), db: Session = Depends(get_db), user: dict = Depends(portal_user)):
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Files can be up to 10 MB.")
    blob = storage.store(db, user["company_id"], file.filename or "file", data, file.content_type, "upload")
    db.commit()
    return storage.describe(blob)


# ── Notices ──────────────────────────────────────────────────────────

def _notice_query(db: Session, user: dict, ctx: service.PortalCtx, student_id: int | None, include_expired=False):
    q = notices.received_query(db, user["company_id"], ctx.key, include_expired)
    if student_id and ctx.role == "parent":
        q = q.filter(NoticeRecipient.via_student_ids.contains([student_id]))
    return q


@router.get("/notices")
def list_notices(student_id: int | None = None, q: str | None = None, category: str | None = None,
                 unread: bool = False, include_expired: bool = False, params: PageParams = Depends(),
                 db: Session = Depends(get_db), user: dict = Depends(portal_user)):
    ctx = _ctx(db, user)
    _check_child(ctx, student_id)
    query = _notice_query(db, user, ctx, student_id, include_expired)
    if category:
        query = query.filter(Notice.category.in_(category.split(",")))
    if unread:
        query = query.filter(NoticeRecipient.read_at.is_(None))
    if q:
        query = query.filter(Notice.title.ilike(f"%{q.strip()}%"))
    query = query.order_by(Notice.pinned.desc(), Notice.published_at.desc())
    return paginate(query, params, lambda r: notices.notice_out(r[0], notices.receipt_fields(r[1])))


@router.get("/notices/{uuid}")
def get_notice(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(portal_user)):
    """Opening a notice marks it read (read receipt)."""
    ctx = _ctx(db, user)
    n = get_or_404(db, Notice, user["company_id"], uuid, "Notice")
    if n.status != "published":
        raise HTTPException(status_code=404, detail="Notice not found")
    r = notices.mark_read(db, user["company_id"], n, ctx.key)
    db.commit()
    return notices.notice_out(n, notices.receipt_fields(r), storage.with_links(db, n.company_id, n.attachments or []))


@router.post("/notices/{uuid}/acknowledge")
def acknowledge(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(portal_user)):
    ctx = _ctx(db, user)
    n = get_or_404(db, Notice, user["company_id"], uuid, "Notice")
    r = notices.acknowledge(db, user["company_id"], n, ctx.key)
    db.commit()
    return notices.receipt_fields(r)


# ── Messages ─────────────────────────────────────────────────────────

class StartIn(BaseModel):
    employee_id: int | None = None
    to_office: bool = False
    student_id: int | None = None
    subject: str = Field(..., min_length=1, max_length=200)
    body: str = Field("", max_length=5000)
    attachments: list[str] = []


class PostIn(BaseModel):
    body: str = Field("", max_length=5000)
    attachments: list[str] = []


@router.get("/messages/contacts")
def contacts(student_id: int | None = None, db: Session = Depends(get_db), user: dict = Depends(portal_user)):
    ctx = _ctx(db, user)
    _check_child(ctx, student_id)
    return messaging.portal_contacts(db, user["company_id"], ctx, student_id)


@router.get("/messages/threads")
def threads(status: str | None = None, student_id: int | None = None, params: PageParams = Depends(),
            db: Session = Depends(get_db), user: dict = Depends(portal_user)):
    ctx = _ctx(db, user)
    _check_child(ctx, student_id)
    query = messaging.portal_threads(db, ctx, user["company_id"], status, student_id)
    return paginate(query, params, lambda t: messaging.thread_out(t, "portal"))


def _thread_full(db: Session, t: Thread) -> dict:
    out = messaging.thread_out(t, "portal", service.office_hours_state(db, t.company_id))
    out["messages"] = [messaging.message_out(db, m, "portal") for m in messaging.messages_of(db, t)]
    out["can_reply"] = t.status == "open"
    return out


@router.post("/messages/threads", status_code=201)
def start(payload: StartIn, db: Session = Depends(get_db), user: dict = Depends(portal_user)):
    ctx = _ctx(db, user)
    t = messaging.start_by_portal(db, user, ctx, employee_id=payload.employee_id, to_office=payload.to_office,
                                  student_id=payload.student_id, subject=payload.subject, body=payload.body,
                                  attachments=payload.attachments)
    db.commit()
    return _thread_full(db, t)


@router.get("/messages/threads/{uuid}")
def get_thread(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(portal_user)):
    ctx = _ctx(db, user)
    t = get_or_404(db, Thread, user["company_id"], uuid, "Conversation")
    messaging.portal_access(ctx, t)
    if t.party_unread:
        messaging.mark_read(db, t, "portal")
        db.commit()
        db.refresh(t)
    return _thread_full(db, t)


@router.post("/messages/threads/{uuid}/messages", status_code=201)
def reply(uuid: UUID, payload: PostIn, db: Session = Depends(get_db), user: dict = Depends(portal_user)):
    ctx = _ctx(db, user)
    t = get_or_404(db, Thread, user["company_id"], uuid, "Conversation")
    messaging.portal_access(ctx, t)
    if ctx.role == "student" and not service.get_settings(db, user["company_id"]).get("students_can_message", True):
        raise HTTPException(status_code=403, detail="Messaging staff is switched off for students.")
    messaging.post(db, user, t, payload.body, payload.attachments, side="portal")
    db.commit()
    return _thread_full(db, t)


@router.post("/messages/threads/{uuid}/read")
def read(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(portal_user)):
    ctx = _ctx(db, user)
    t = get_or_404(db, Thread, user["company_id"], uuid, "Conversation")
    messaging.portal_access(ctx, t)
    messaging.mark_read(db, t, "portal")
    db.commit()
    return {"ok": True}


# ── Events ───────────────────────────────────────────────────────────

class RsvpIn(BaseModel):
    response: str
    guests: int = 0
    note: str | None = Field(None, max_length=300)


@router.get("/events")
def list_events(start: date | None = None, end: date | None = None, student_id: int | None = None,
                category: str | None = None, db: Session = Depends(get_db), user: dict = Depends(portal_user)):
    ctx = _ctx(db, user)
    _check_child(ctx, student_id)
    start = start or date.today().replace(day=1)
    end = end or start + timedelta(days=41)
    if (end - start).days > 400:
        raise HTTPException(status_code=400, detail="Pick a shorter date range.")
    rows = [e for e in events.range_query(db, user["company_id"], start, end, category).limit(1000).all()
            if service.portal_matches(e.audience, ctx, student_id)]
    mine = events.my_rsvps(db, [e.id for e in rows], ctx.key)
    return {"items": [events.event_out(e, None, mine.get(e.id)) for e in rows], "start": start, "end": end}


@router.get("/events/feed-link")
def feed_link(db: Session = Depends(get_db), user: dict = Depends(portal_user)):
    ctx = _ctx(db, user)
    return {"path": events.feed_path(user["company_id"], ctx.key)}


@router.post("/events/{uuid}/rsvp")
def rsvp(uuid: UUID, payload: RsvpIn, db: Session = Depends(get_db), user: dict = Depends(portal_user)):
    ctx = _ctx(db, user)
    e = get_or_404(db, Event, user["company_id"], uuid, "Event")
    if not service.portal_matches(e.audience, ctx):
        raise HTTPException(status_code=404, detail="Event not found")
    r = events.respond(db, e, ctx.key, service.actor_name(db, user), payload.response, payload.guests, payload.note)
    db.commit()
    return {"response": r.response, "guests": r.guests, "note": r.note}


# ── Parent-teacher meetings ──────────────────────────────────────────

class BookIn(BaseModel):
    slot_id: int
    student_id: int
    mode: str | None = None
    note: str | None = Field(None, max_length=500)


@router.get("/ptm")
def ptm_days(student_id: int, db: Session = Depends(get_db), user: dict = Depends(portal_user)):
    ctx = _ctx(db, user)
    _check_child(ctx, student_id)
    return ptm.portal_days(db, ctx, user["company_id"], student_id)


@router.post("/ptm/book", status_code=201)
def book(payload: BookIn, db: Session = Depends(get_db), user: dict = Depends(require_portal("parent"))):
    ctx = _ctx(db, user)
    slot = db.query(PtmSlot).filter(PtmSlot.company_id == user["company_id"], PtmSlot.id == payload.slot_id).first()
    if slot is None:
        raise HTTPException(status_code=404, detail="Slot not found")
    b = ptm.book(db, user, ctx, slot, payload.student_id, payload.mode, payload.note)
    db.commit()
    db.refresh(b)
    return ptm.booking_out(b, for_parent=True)


@router.post("/ptm/bookings/{uuid}/cancel")
def cancel(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_portal("parent"))):
    ctx = _ctx(db, user)
    b = get_or_404(db, PtmBooking, user["company_id"], uuid, "Booking")
    if b.student_id not in ctx.children:
        raise HTTPException(status_code=404, detail="Booking not found")
    if b.status != "booked":
        raise HTTPException(status_code=400, detail="This booking can't be cancelled now.")
    if b.slot.start_at <= service.now():
        raise HTTPException(status_code=400, detail="This meeting has already started.")
    b.status, b.cancelled_at = "cancelled", service.now()
    from modules.notifications.service import notify
    notify(db, b.company_id, [b.teacher.employee_id], kind="comms_ptm", title="PTM booking cancelled",
           body=f"{b.booked_by or 'A parent'} cancelled their slot.", link="/dashboard/comms/ptm")
    db.commit()
    return {"ok": True}


# ── Diary ────────────────────────────────────────────────────────────

@router.get("/diary")
def diary(student_id: int, db: Session = Depends(get_db), user: dict = Depends(portal_user)):
    from modules.comms.api_diary import diary_out
    ctx = _ctx(db, user)
    _check_child(ctx, student_id)
    sec = ctx.sections.get(student_id)
    if not sec or not service.get_settings(db, user["company_id"]).get("diary_enabled", True):
        return []
    rows = db.query(DiaryEntry).filter(DiaryEntry.company_id == user["company_id"], DiaryEntry.section_id == sec,
                                       DiaryEntry.date >= date.today() - timedelta(days=60)).order_by(
        DiaryEntry.date.desc()).limit(60).all()
    return [diary_out(db, e) for e in rows]


# ── Badges and home card ─────────────────────────────────────────────

@router.get("/unread")
def unread(db: Session = Depends(get_db), user: dict = Depends(portal_user)):
    ctx = _ctx(db, user)
    n = notices.received_query(db, user["company_id"], ctx.key).filter(NoticeRecipient.read_at.is_(None)).count()
    return {"messages": messaging.portal_unread(db, ctx, user["company_id"]), "notices": n}


@router.get("/summary")
def summary(student_id: int | None = None, db: Session = Depends(get_db), user: dict = Depends(portal_user)):
    cid = user["company_id"]
    ctx = _ctx(db, user)
    _check_child(ctx, student_id)
    latest = (_notice_query(db, user, ctx, student_id).order_by(Notice.pinned.desc(), Notice.published_at.desc())
              .limit(4).all())
    unread_notices = _notice_query(db, user, ctx, student_id).filter(NoticeRecipient.read_at.is_(None)).count()
    today = date.today()
    upcoming = [e for e in events.range_query(db, cid, today, today + timedelta(days=30), include_cancelled=False)
                .limit(200).all() if service.portal_matches(e.audience, ctx, student_id)][:4]
    next_meeting = None
    if student_id:
        b = (db.query(PtmBooking).join(PtmSlot, PtmSlot.id == PtmBooking.slot_id)
             .filter(PtmBooking.company_id == cid, PtmBooking.student_id == student_id, PtmBooking.status == "booked",
                     PtmSlot.start_at > service.now()).order_by(PtmSlot.start_at).first())
        next_meeting = ptm.booking_out(b, for_parent=True) if b else None
    return {
        "notices": [notices.notice_out(n, notices.receipt_fields(r)) for n, r in latest],
        "unread_notices": unread_notices,
        "unread_messages": messaging.portal_unread(db, ctx, cid),
        "events": [events.event_out(e) for e in upcoming],
        "next_meeting": next_meeting,
    }


