"""Staff API for the institution calendar, and the private iCal feed (no sign-in, signed link)."""
from datetime import date, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core import audit
from core.crud import get_or_404
from core.db import get_db, set_tenant
from core.pagination import PageParams, paginate
from core.security import require_permission
from modules.comms import events, service
from modules.comms.models import Event, EventRsvp

router = APIRouter()
VIEW, MANAGE = "comms.view", "comms.events.manage"


class EventIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(None, max_length=5000)
    category: str = "event"
    all_day: bool = True
    start_date: date
    end_date: date | None = None
    start_time: str | None = None
    end_time: str | None = None
    location: str | None = Field(None, max_length=200)
    audience: dict = {"everyone": True}
    rsvp_required: bool = False
    rsvp_deadline: date | None = None
    notify: bool = False


class EventPatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = None
    category: str | None = None
    all_day: bool | None = None
    start_date: date | None = None
    end_date: date | None = None
    start_time: str | None = None
    end_time: str | None = None
    location: str | None = None
    audience: dict | None = None
    rsvp_required: bool | None = None
    rsvp_deadline: date | None = None


class RsvpIn(BaseModel):
    response: str
    guests: int = 0
    note: str | None = Field(None, max_length=300)


def _range(start: date | None, end: date | None) -> tuple[date, date]:
    start = start or date.today().replace(day=1)
    end = end or (start + timedelta(days=41))
    if (end - start).days > 400:
        raise HTTPException(status_code=400, detail="Pick a shorter date range.")
    return start, end


@router.get("/events")
def list_events(start: date | None = None, end: date | None = None, category: str | None = None,
                q: str | None = None, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    """Events overlapping the range (month, week or agenda views)."""
    cid = user["company_id"]
    start, end = _range(start, end)
    query = events.range_query(db, cid, start, end, category)
    if q:
        query = query.filter(Event.title.ilike(f"%{q.strip()}%"))
    rows = query.limit(1000).all()
    ids = [e.id for e in rows]
    counts = events.rsvp_counts(db, [e.id for e in rows if e.rsvp_required])
    mine = events.my_rsvps(db, ids, f"e:{user.get('employee_id') or 0}")
    return {"items": [events.event_out(e, counts.get(e.id), mine.get(e.id)) for e in rows],
            "start": start, "end": end, "can_manage": service.has(db, user, MANAGE)}


@router.post("/events", status_code=201)
def create_event(payload: EventIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = user["company_id"]
    e = Event(company_id=cid, created_by=service.actor_name(db, user))
    data = payload.model_dump()
    events.apply_fields(db, cid, e, data)
    db.add(e)
    db.flush()
    if payload.notify:
        _announce(db, e)
    db.commit()
    db.refresh(e)
    return events.event_out(e)


def _announce(db: Session, e: Event) -> None:
    """Tell the event's audience in the app."""
    from modules.notifications.service import notify, notify_portal
    res = service.resolve_audience(db, e.company_id, e.audience)
    when = e.start_date.strftime("%d %b %Y")
    if res.staff:
        notify(db, e.company_id, list(res.staff), kind="comms_event", title=e.title, body=f"{when}. {e.location or ''}".strip(),
               link="/dashboard/comms/events")
    accounts = service.portal_ids_for(db, e.company_id, res.guardians.keys(), res.students)
    if accounts:
        notify_portal(db, e.company_id, list(accounts.values()), kind="comms_event", title=e.title,
                      body=f"{when}. {e.location or ''}".strip(), link="/dashboard/my/events")


@router.get("/events/feed-link")
def feed_link(db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    """The signed-in person's private calendar link (subscribe from Google or Apple Calendar)."""
    return {"path": events.feed_path(user["company_id"], f"e:{user.get('employee_id') or 0}")}


@router.get("/events/{uuid}")
def get_event(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    e = get_or_404(db, Event, user["company_id"], uuid, "Event")
    counts = events.rsvp_counts(db, [e.id]) if e.rsvp_required else {}
    mine = events.my_rsvps(db, [e.id], f"e:{user.get('employee_id') or 0}")
    return events.event_out(e, counts.get(e.id), mine.get(e.id))


@router.put("/events/{uuid}")
def update_event(uuid: UUID, payload: EventPatch, db: Session = Depends(get_db),
                 user: dict = Depends(require_permission(MANAGE))):
    e = get_or_404(db, Event, user["company_id"], uuid, "Event")
    data = payload.model_dump(exclude_unset=True)
    events.apply_fields(db, user["company_id"], e, data)
    db.commit()
    db.refresh(e)
    return events.event_out(e)


@router.post("/events/{uuid}/cancel")
def cancel_event(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    e = get_or_404(db, Event, user["company_id"], uuid, "Event")
    e.status = "cancelled" if e.status == "active" else "active"
    audit.event(db, f"comms.event_{'cancelled' if e.status == 'cancelled' else 'restored'}", "comms_events", e.id, {},
                entity_uuid=str(e.uuid), entity_label=e.title)
    db.commit()
    return events.event_out(e)


@router.delete("/events/{uuid}")
def remove_event(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    e = get_or_404(db, Event, user["company_id"], uuid, "Event")
    e.archived_at = service.now()
    db.commit()
    return {"ok": True}


@router.get("/events/{uuid}/rsvps")
def list_rsvps(uuid: UUID, params: PageParams = Depends(), db: Session = Depends(get_db),
               user: dict = Depends(require_permission(VIEW))):
    e = get_or_404(db, Event, user["company_id"], uuid, "Event")
    q = db.query(EventRsvp).filter(EventRsvp.company_id == e.company_id, EventRsvp.event_id == e.id).order_by(
        EventRsvp.response, EventRsvp.name)
    page = paginate(q, params, lambda r: {"name": r.name, "response": r.response, "guests": r.guests, "note": r.note,
                                          "kind": {"e": "staff", "g": "parent", "s": "student"}.get(r.responder_key[0]),
                                          "responded_at": r.responded_at})
    page["counts"] = events.rsvp_counts(db, [e.id]).get(e.id, {"yes": 0, "no": 0, "maybe": 0, "guests": 0})
    return page


@router.post("/events/{uuid}/rsvp")
def staff_rsvp(uuid: UUID, payload: RsvpIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    e = get_or_404(db, Event, user["company_id"], uuid, "Event")
    if not user.get("employee_id"):
        raise HTTPException(status_code=400, detail="Replies are recorded against a staff profile.")
    r = events.respond(db, e, f"e:{user['employee_id']}", service.actor_name(db, user), payload.response,
                       payload.guests, payload.note)
    db.commit()
    return {"response": r.response, "guests": r.guests}


# ── Private iCal feed ────────────────────────────────────────────────

@router.get("/ical/{company_id}/{key}/{sig}.ics", include_in_schema=False)
def ical_feed(company_id: int, key: str, sig: str, db: Session = Depends(get_db)):
    """Calendar feed behind a signed link: staff get the whole calendar, families what is meant for them."""
    k = events.check_feed(company_id, key, sig)
    set_tenant(db, company_id)
    start, end = date.today() - timedelta(days=60), date.today() + timedelta(days=365)
    rows = events.range_query(db, company_id, start, end).limit(2000).all()
    if not k.startswith("e:"):
        user = {"company_id": company_id, "portal_id": -1, "role": "parent" if k.startswith("g:") else "student",
                "guardian_id": int(k[2:]) if k.startswith("g:") else None,
                "student_id": int(k[2:]) if k.startswith("s:") else None}
        ctx = service.portal_context(db, user)
        rows = [e for e in rows if service.portal_matches(e.audience, ctx)]
    from modules.company.models import Company
    name = db.query(Company.company_name).filter(Company.id == company_id).scalar() or "Calendar"
    return Response(content=events.ical(rows, name), media_type="text/calendar; charset=utf-8",
                    headers={"Cache-Control": "private, max-age=900"})
