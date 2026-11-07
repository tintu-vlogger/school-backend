"""The institution calendar: events, holidays, exam dates, PTMs, sports days and trips, with an
audience (same shape as notices), all-day or timed, RSVP, and a private iCal feed per person."""
from __future__ import annotations

import hashlib
import hmac
from datetime import date, datetime, time, timedelta

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from core.config import get_settings as platform_settings
from modules.comms import service
from modules.comms.models import Event, EventRsvp


def event_out(e: Event, rsvp: dict | None = None, mine: EventRsvp | None = None) -> dict:
    return {
        "id": e.id, "uuid": str(e.uuid), "title": e.title, "description": e.description, "category": e.category,
        "category_label": service.EVENT_CATEGORIES.get(e.category, e.category.title()), "all_day": e.all_day,
        "start_date": e.start_date, "end_date": e.end_date, "start_at": e.start_at, "end_at": e.end_at,
        "location": e.location, "audience": service.clean_audience(e.audience), "audience_label": e.audience_label,
        "rsvp_required": e.rsvp_required, "rsvp_deadline": e.rsvp_deadline, "status": e.status,
        "ptm_day_id": e.ptm_day_id, "created_by": e.created_by, "rsvp": rsvp,
        "my_rsvp": {"response": mine.response, "guests": mine.guests, "note": mine.note} if mine else None,
    }


def apply_fields(db: Session, company_id: int, e: Event, data: dict) -> None:
    for key in ("title", "description", "category", "location", "rsvp_required", "rsvp_deadline", "all_day"):
        if key in data:
            setattr(e, key, data[key])
    if e.category not in service.EVENT_CATEGORIES:
        raise HTTPException(status_code=400, detail="Pick a category from the list.")
    if "audience" in data:
        e.audience = service.clean_audience(data["audience"] or {"everyone": True})
        e.audience_label = service.audience_label(db, company_id, e.audience)
    tz = service.company_tz(db, company_id)
    start_date = data.get("start_date", e.start_date)
    end_date = data.get("end_date") or start_date
    if not start_date:
        raise HTTPException(status_code=400, detail="Pick the date.")
    if e.all_day:
        e.start_date, e.end_date, e.start_at, e.end_at = start_date, end_date, None, None
    else:
        st, en = data.get("start_time"), data.get("end_time")
        if st is None and e.start_at:
            st = e.start_at.astimezone(tz).time()
        if en is None and e.end_at:
            en = e.end_at.astimezone(tz).time()
        if st is None:
            raise HTTPException(status_code=400, detail="Pick the start time, or make it an all-day event.")
        st = _time(st)
        en = _time(en) if en else (datetime.combine(start_date, st) + timedelta(hours=1)).time()
        e.start_at = datetime.combine(start_date, st, tzinfo=tz)
        e.end_at = datetime.combine(end_date, en, tzinfo=tz)
        if e.end_at <= e.start_at:
            raise HTTPException(status_code=400, detail="The event must end after it starts.")
        e.start_date, e.end_date = start_date, end_date
    if e.end_date < e.start_date:
        raise HTTPException(status_code=400, detail="The end date is before the start date.")


def _time(v) -> time:
    if isinstance(v, time):
        return v
    h, m = str(v).split(":")[:2]
    return time(int(h), int(m))


def range_query(db: Session, company_id: int, start: date, end: date, category: str | None = None,
                include_cancelled: bool = True):
    q = db.query(Event).filter(Event.company_id == company_id, Event.archived_at.is_(None),
                               Event.start_date <= end, Event.end_date >= start)
    if category:
        q = q.filter(Event.category.in_(category.split(",")))
    if not include_cancelled:
        q = q.filter(Event.status == "active")
    return q.order_by(Event.start_date, Event.start_at.nullsfirst(), Event.title)


def rsvp_counts(db: Session, ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    out: dict[int, dict] = {}
    for eid, resp, n, guests in db.query(EventRsvp.event_id, EventRsvp.response, func.count(EventRsvp.id),
                                         func.coalesce(func.sum(EventRsvp.guests), 0)).filter(
            EventRsvp.event_id.in_(ids)).group_by(EventRsvp.event_id, EventRsvp.response).all():
        d = out.setdefault(eid, {"yes": 0, "no": 0, "maybe": 0, "guests": 0})
        d[resp] = n
        if resp == "yes":
            d["guests"] += int(guests)
    return out


def respond(db: Session, e: Event, key: str, name: str, response: str, guests: int = 0, note: str | None = None) -> EventRsvp:
    if not e.rsvp_required:
        raise HTTPException(status_code=400, detail="This event does not take replies.")
    if response not in ("yes", "no", "maybe"):
        raise HTTPException(status_code=400, detail="Reply yes, no or maybe.")
    if e.status != "active":
        raise HTTPException(status_code=400, detail="This event was cancelled.")
    if e.rsvp_deadline and e.rsvp_deadline < date.today():
        raise HTTPException(status_code=400, detail="Replies for this event have closed.")
    r = db.query(EventRsvp).filter(EventRsvp.event_id == e.id, EventRsvp.responder_key == key).first()
    if r is None:
        r = EventRsvp(company_id=e.company_id, event_id=e.id, responder_key=key)
        db.add(r)
    r.name, r.response, r.guests, r.note = name, response, max(0, min(int(guests or 0), 10)), (note or None)
    db.flush()
    return r


def my_rsvps(db: Session, ids: list[int], key: str) -> dict[int, EventRsvp]:
    if not ids:
        return {}
    return {r.event_id: r for r in db.query(EventRsvp).filter(EventRsvp.event_id.in_(ids),
                                                             EventRsvp.responder_key == key).all()}


# ── iCal feed ────────────────────────────────────────────────────────

def _sig(company_id: int, key: str) -> str:
    secret = (platform_settings().SECRET_KEY or "dev").encode()
    return hmac.new(secret, f"ical:{company_id}:{key}".encode(), hashlib.sha256).hexdigest()[:32]


def feed_path(company_id: int, key: str) -> str:
    """Path of a person's private calendar feed (key "e:12", "g:5" or "s:9")."""
    k = key.replace(":", "-")
    return f"/api/v1/comms/ical/{company_id}/{k}/{_sig(company_id, key)}.ics"


def check_feed(company_id: int, k: str, sig: str) -> str:
    key = k.replace("-", ":", 1)
    if not hmac.compare_digest(_sig(company_id, key), sig or ""):
        raise HTTPException(status_code=404, detail="Calendar not found")
    return key


def ical(events: list[Event], name: str) -> str:
    def esc(s: str | None) -> str:
        return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

    def dt(v: datetime) -> str:
        from datetime import timezone
        return v.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Campusly//Calendar//EN", "CALSCALE:GREGORIAN",
             f"X-WR-CALNAME:{esc(name)}"]
    stamp = dt(service.now())
    for e in events:
        lines += ["BEGIN:VEVENT", f"UID:{e.uuid}@campusly", f"DTSTAMP:{stamp}", f"SUMMARY:{esc(e.title)}"]
        if e.all_day or not e.start_at:
            lines += [f"DTSTART;VALUE=DATE:{e.start_date.strftime('%Y%m%d')}",
                      f"DTEND;VALUE=DATE:{(e.end_date + timedelta(days=1)).strftime('%Y%m%d')}"]
        else:
            lines += [f"DTSTART:{dt(e.start_at)}", f"DTEND:{dt(e.end_at or e.start_at)}"]
        if e.location:
            lines.append(f"LOCATION:{esc(e.location)}")
        if e.description:
            lines.append(f"DESCRIPTION:{esc(e.description)}")
        lines.append(f"CATEGORIES:{esc(service.EVENT_CATEGORIES.get(e.category, e.category))}")
        if e.status == "cancelled":
            lines.append("STATUS:CANCELLED")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
