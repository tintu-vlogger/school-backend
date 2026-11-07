"""Parent-teacher meetings: a PTM day for some sections, the teachers taking part with their
slots (length, gaps, breaks, in person or online), parent bookings from the portal, reminders,
and the teacher's notes, action points and attendance (met / no-show)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from core import audit
from modules.academics import service as academics
from modules.academics.models import Section
from modules.comms import service
from modules.comms.models import Event, PtmBooking, PtmDay, PtmSlot, PtmTeacher
from modules.employees.models import Employee


def _t(v) -> time:
    if isinstance(v, time):
        return v
    h, m = str(v).split(":")[:2]
    return time(int(h), int(m))


def _hhmm(t: time | None) -> str | None:
    return t.strftime("%H:%M") if t else None


def day_out(db: Session, d: PtmDay, stats: dict | None = None) -> dict:
    secs = db.query(Section).filter(Section.id.in_(d.section_ids or [-1])).all()
    return {
        "id": d.id, "uuid": str(d.uuid), "title": d.title, "date": d.date, "start_time": _hhmm(d.start_time),
        "end_time": _hhmm(d.end_time), "slot_minutes": d.slot_minutes, "gap_minutes": d.gap_minutes,
        "breaks": d.breaks or [], "mode": d.mode, "location": d.location, "meeting_link": d.meeting_link,
        "section_ids": d.section_ids or [], "sections": sorted(academics.section_label(s) for s in secs),
        "instructions": d.instructions, "booking_closes_at": d.booking_closes_at,
        "notes_visible_default": d.notes_visible_default, "status": d.status, "created_by": d.created_by,
        "stats": stats or {},
    }


def day_stats(db: Session, ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    out = {i: {"teachers": 0, "slots": 0, "booked": 0, "met": 0, "no_show": 0} for i in ids}
    for did, n in db.query(PtmTeacher.ptm_day_id, func.count(PtmTeacher.id)).filter(
            PtmTeacher.ptm_day_id.in_(ids)).group_by(PtmTeacher.ptm_day_id).all():
        out[did]["teachers"] = n
    for did, n in db.query(PtmSlot.ptm_day_id, func.count(PtmSlot.id)).filter(
            PtmSlot.ptm_day_id.in_(ids), PtmSlot.blocked.is_(False)).group_by(PtmSlot.ptm_day_id).all():
        out[did]["slots"] = n
    for did, status, n in db.query(PtmBooking.ptm_day_id, PtmBooking.status, func.count(PtmBooking.id)).filter(
            PtmBooking.ptm_day_id.in_(ids), PtmBooking.status != "cancelled").group_by(
            PtmBooking.ptm_day_id, PtmBooking.status).all():
        out[did]["booked"] += n
        if status in ("met", "no_show"):
            out[did][status] = n
    return out


def apply_fields(db: Session, company_id: int, d: PtmDay, data: dict) -> None:
    for key in ("title", "date", "slot_minutes", "gap_minutes", "mode", "location", "meeting_link", "instructions",
                "booking_closes_at", "notes_visible_default"):
        if key in data:
            setattr(d, key, data[key])
    for key in ("start_time", "end_time"):
        if key in data and data[key]:
            setattr(d, key, _t(data[key]))
    if "breaks" in data:
        d.breaks = [{"start": _hhmm(_t(b["start"])), "end": _hhmm(_t(b["end"])), "label": (b.get("label") or "Break")[:40]}
                    for b in (data["breaks"] or []) if b.get("start") and b.get("end")]
    if "section_ids" in data:
        ids = {i for (i,) in db.query(Section.id).filter(Section.company_id == company_id,
                                                          Section.id.in_(data["section_ids"] or [-1])).all()}
        d.section_ids = sorted(ids)
    if d.mode not in ("in_person", "online", "both"):
        raise HTTPException(status_code=400, detail="Pick in person, online or both.")
    if not d.section_ids:
        raise HTTPException(status_code=400, detail="Pick at least one section.")
    if not (5 <= (d.slot_minutes or 0) <= 120):
        raise HTTPException(status_code=400, detail="Slots must be between 5 and 120 minutes.")
    if d.start_time >= d.end_time:
        raise HTTPException(status_code=400, detail="The day must end after it starts.")


def suggested_teachers(db: Session, company_id: int, section_ids: list[int]) -> list[int]:
    """Class teachers and subject teachers of the sections."""
    out: list[int] = []
    for sid in section_ids:
        for emp in service.staff_for_section(db, company_id, sid).keys():
            if emp not in out:
                out.append(emp)
    return out


def set_teachers(db: Session, d: PtmDay, employee_ids: list[int]) -> None:
    """Add the listed teachers (with slots); teachers left out are removed when nobody booked them."""
    cid = d.company_id
    valid = {i for (i,) in db.query(Employee.id).filter(Employee.company_id == cid, Employee.id.in_(employee_ids or [-1]),
                                                        Employee.active.is_(True)).all()}
    current = {t.employee_id: t for t in db.query(PtmTeacher).filter(PtmTeacher.ptm_day_id == d.id).all()}
    for emp in valid - set(current):
        t = PtmTeacher(company_id=cid, ptm_day_id=d.id, employee_id=emp)
        db.add(t)
        db.flush()
        generate_slots(db, d, t)
    for emp, t in current.items():
        if emp in valid:
            continue
        if db.query(PtmBooking.id).filter(PtmBooking.ptm_teacher_id == t.id, PtmBooking.status != "cancelled").first():
            raise HTTPException(status_code=400, detail=f"{service.employee_name(t.employee)} already has bookings; cancel them first.")
        db.delete(t)
    db.flush()


def generate_slots(db: Session, d: PtmDay, t: PtmTeacher) -> int:
    """(Re)build a teacher's slots from the day's times; booked slots are kept."""
    tz = service.company_tz(db, d.company_id)
    booked = {s for (s,) in db.query(PtmBooking.slot_id).filter(PtmBooking.ptm_teacher_id == t.id,
                                                                 PtmBooking.status != "cancelled").all()}
    existing = db.query(PtmSlot).filter(PtmSlot.ptm_teacher_id == t.id).all()
    keep_starts = set()
    for s in existing:
        if s.id in booked:
            keep_starts.add(s.start_at)
        else:
            db.delete(s)
    db.flush()
    start, end = t.start_time or d.start_time, t.end_time or d.end_time
    breaks = [(_t(b["start"]), _t(b["end"])) for b in (d.breaks or [])]
    cur = datetime.combine(d.date, start)
    stop = datetime.combine(d.date, end)
    step = timedelta(minutes=d.slot_minutes)
    gap = timedelta(minutes=d.gap_minutes or 0)
    n = 0
    while cur + step <= stop:
        s_end = cur + step
        clash = next((b for b in breaks if cur.time() < b[1] and s_end.time() > b[0]), None)
        if clash:
            cur = datetime.combine(d.date, clash[1])
            continue
        start_at = cur.replace(tzinfo=tz)
        if start_at not in keep_starts:
            db.add(PtmSlot(company_id=d.company_id, ptm_day_id=d.id, ptm_teacher_id=t.id, start_at=start_at,
                           end_at=s_end.replace(tzinfo=tz)))
            n += 1
        cur = s_end + gap
    db.flush()
    return n


def regenerate_all(db: Session, d: PtmDay) -> None:
    for t in db.query(PtmTeacher).filter(PtmTeacher.ptm_day_id == d.id).all():
        generate_slots(db, d, t)


def open_day(db: Session, d: PtmDay, actor: str) -> None:
    """Open bookings: put the day in the calendar and tell the parents."""
    from modules.notifications.service import notify, notify_portal
    from modules.students import service as students
    if not db.query(PtmTeacher.id).filter(PtmTeacher.ptm_day_id == d.id).first():
        raise HTTPException(status_code=400, detail="Add the teachers taking part first.")
    d.status = "open"
    ev = db.query(Event).filter(Event.ptm_day_id == d.id).first()
    audience = {"learners": {"parents": True, "scope": "sections", "section_ids": d.section_ids}}
    if ev is None:
        ev = Event(company_id=d.company_id, ptm_day_id=d.id, category="ptm", created_by=actor)
        db.add(ev)
    tz = service.company_tz(db, d.company_id)
    ev.title, ev.all_day = d.title, False
    ev.start_date = ev.end_date = d.date
    ev.start_at = datetime.combine(d.date, d.start_time, tzinfo=tz)
    ev.end_at = datetime.combine(d.date, d.end_time, tzinfo=tz)
    ev.location = d.location or ("Online" if d.mode == "online" else None)
    ev.description = d.instructions
    ev.audience = service.clean_audience(audience)
    ev.audience_label = service.audience_label(db, d.company_id, ev.audience)
    student_ids = []
    for sid in d.section_ids or []:
        student_ids += [s.id for s, _ in students.section_students(db, d.company_id, sid)]
    accounts = students.portal_account_ids_for_students(db, d.company_id, student_ids, parents=True, students=False)
    if accounts:
        notify_portal(db, d.company_id, accounts, kind="comms_ptm", title=f"Book your slot: {d.title}",
                      body=f"Parent-teacher meetings on {d.date.strftime('%d %b %Y')}. Pick a time with each teacher.",
                      link="/dashboard/my/meetings", actor_name=actor)
    teachers = [t.employee_id for t in db.query(PtmTeacher).filter(PtmTeacher.ptm_day_id == d.id).all()]
    notify(db, d.company_id, teachers, kind="comms_ptm", title=f"PTM bookings open: {d.title}",
           body=f"Parents can now book your slots for {d.date.strftime('%d %b %Y')}.", link="/dashboard/comms/ptm")
    audit.event(db, "comms.ptm_opened", "comms_ptm_days", d.id, {"families": len(accounts)},
                entity_uuid=str(d.uuid), entity_label=d.title)
    db.flush()


# ── Slots and bookings ───────────────────────────────────────────────

def booking_out(b: PtmBooking, for_parent: bool = False) -> dict:
    out = {"id": b.id, "uuid": str(b.uuid), "slot_id": b.slot_id, "start_at": b.slot.start_at if b.slot else None,
           "end_at": b.slot.end_at if b.slot else None, "student_id": b.student_id,
           "student_name": b.student.full_name if b.student else None, "guardian_id": b.guardian_id,
           "guardian_name": b.guardian.full_name if b.guardian else None, "booked_by": b.booked_by, "mode": b.mode,
           "status": b.status, "parent_note": b.parent_note, "notes_visible": b.notes_visible,
           "teacher_name": service.employee_name(b.teacher.employee) if b.teacher else None,
           "teacher_employee_id": b.teacher.employee_id if b.teacher else None, "created_at": b.created_at}
    if not for_parent or b.notes_visible:
        out.update({"notes": b.notes, "action_points": b.action_points or []})
    else:
        out.update({"notes": None, "action_points": []})
    return out


def teacher_schedule(db: Session, d: PtmDay, t: PtmTeacher) -> list[dict]:
    slots = db.query(PtmSlot).filter(PtmSlot.ptm_teacher_id == t.id).order_by(PtmSlot.start_at).all()
    bookings = {b.slot_id: b for b in db.query(PtmBooking).options(
        joinedload(PtmBooking.student), joinedload(PtmBooking.guardian), joinedload(PtmBooking.slot),
        joinedload(PtmBooking.teacher).joinedload(PtmTeacher.employee)).filter(
        PtmBooking.ptm_teacher_id == t.id, PtmBooking.status != "cancelled").all()}
    return [{"id": s.id, "uuid": str(s.uuid), "start_at": s.start_at, "end_at": s.end_at, "blocked": s.blocked,
             "booking": booking_out(bookings[s.id]) if s.id in bookings else None} for s in slots]


def teacher_out(db: Session, t: PtmTeacher) -> dict:
    total = db.query(func.count(PtmSlot.id)).filter(PtmSlot.ptm_teacher_id == t.id, PtmSlot.blocked.is_(False)).scalar()
    booked = db.query(func.count(PtmBooking.id)).filter(PtmBooking.ptm_teacher_id == t.id,
                                                        PtmBooking.status != "cancelled").scalar()
    return {"id": t.id, "uuid": str(t.uuid), "employee_id": t.employee_id, "name": service.employee_name(t.employee),
            "location": t.location, "meeting_link": t.meeting_link, "start_time": _hhmm(t.start_time),
            "end_time": _hhmm(t.end_time), "slots": total or 0, "booked": booked or 0}


def portal_days(db: Session, ctx: service.PortalCtx, company_id: int, student_id: int) -> list[dict]:
    """Open (and recent) PTM days for a child's section, with that child's teachers and free slots."""
    sec = ctx.sections.get(student_id)
    if not sec:
        return []
    days = db.query(PtmDay).filter(PtmDay.company_id == company_id, PtmDay.archived_at.is_(None),
                                   PtmDay.status.in_(("open", "closed", "completed")),
                                   PtmDay.date >= date.today() - timedelta(days=60)).order_by(PtmDay.date).all()
    days = [d for d in days if sec in (d.section_ids or [])]
    linked = service.staff_for_section(db, company_id, sec)
    out = []
    for d in days:
        teachers = db.query(PtmTeacher).options(joinedload(PtmTeacher.employee)).filter(
            PtmTeacher.ptm_day_id == d.id, PtmTeacher.employee_id.in_(list(linked) or [-1])).all()
        mine = {b.ptm_teacher_id: b for b in db.query(PtmBooking).options(
            joinedload(PtmBooking.slot), joinedload(PtmBooking.student), joinedload(PtmBooking.guardian),
            joinedload(PtmBooking.teacher).joinedload(PtmTeacher.employee)).filter(
            PtmBooking.ptm_day_id == d.id, PtmBooking.student_id == student_id, PtmBooking.status != "cancelled").all()}
        taken = {s for (s,) in db.query(PtmBooking.slot_id).filter(PtmBooking.ptm_day_id == d.id,
                                                                    PtmBooking.status != "cancelled").all()}
        now = service.now()
        items = []
        for t in teachers:
            slots = db.query(PtmSlot).filter(PtmSlot.ptm_teacher_id == t.id, PtmSlot.blocked.is_(False)).order_by(
                PtmSlot.start_at).all()
            items.append({
                "ptm_teacher_id": t.id, "employee_id": t.employee_id, "name": service.employee_name(t.employee),
                "roles": linked.get(t.employee_id, []), "location": t.location or d.location,
                "meeting_link": (t.meeting_link or d.meeting_link) if t.id in mine else None,
                "booking": booking_out(mine[t.id], for_parent=True) if t.id in mine else None,
                "slots": [{"id": s.id, "uuid": str(s.uuid), "start_at": s.start_at, "end_at": s.end_at,
                           "free": s.id not in taken and s.start_at > now} for s in slots],
            })
        items.sort(key=lambda x: ("Class teacher" not in x["roles"], x["name"]))
        out.append({**day_out(db, d), "can_book": _can_book(d), "teachers": items})
    return out


def _can_book(d: PtmDay) -> bool:
    if d.status != "open" or d.date < date.today():
        return False
    return not (d.booking_closes_at and d.booking_closes_at <= service.now())


def book(db: Session, user: dict, ctx: service.PortalCtx, slot: PtmSlot, student_id: int, mode: str | None,
         note: str | None) -> PtmBooking:
    cid = user["company_id"]
    if student_id not in ctx.children:
        raise HTTPException(status_code=404, detail="Student not found")
    d = db.query(PtmDay).filter(PtmDay.id == slot.ptm_day_id).first()
    if not _can_book(d):
        raise HTTPException(status_code=400, detail="Booking for this meeting day is closed.")
    sec = ctx.sections.get(student_id)
    if sec not in (d.section_ids or []):
        raise HTTPException(status_code=400, detail="This meeting day is not for your child's section.")
    t = db.query(PtmTeacher).filter(PtmTeacher.id == slot.ptm_teacher_id).first()
    if t.employee_id not in service.staff_for_section(db, cid, sec):
        raise HTTPException(status_code=400, detail="This teacher does not teach your child.")
    if slot.blocked or slot.start_at <= service.now():
        raise HTTPException(status_code=400, detail="This slot is not available.")
    # One slot per teacher per child, and no two meetings for the same child at the same time.
    if db.query(PtmBooking.id).filter(PtmBooking.ptm_teacher_id == t.id, PtmBooking.student_id == student_id,
                                      PtmBooking.status != "cancelled").first():
        raise HTTPException(status_code=409, detail="You already have a slot with this teacher. Cancel it to pick another.")
    clash = (db.query(PtmBooking.id).join(PtmSlot, PtmSlot.id == PtmBooking.slot_id)
             .filter(PtmBooking.ptm_day_id == d.id, PtmBooking.student_id == student_id,
                     PtmBooking.status != "cancelled", PtmSlot.start_at < slot.end_at,
                     PtmSlot.end_at > slot.start_at).first())
    if clash:
        raise HTTPException(status_code=409, detail="You have another meeting at this time.")
    mode = mode if mode in ("in_person", "online") else ("online" if d.mode == "online" else "in_person")
    if d.mode != "both":
        mode = d.mode
    b = PtmBooking(company_id=cid, ptm_day_id=d.id, ptm_teacher_id=t.id, slot_id=slot.id, student_id=student_id,
                   guardian_id=ctx.guardian_id, booked_by=service.actor_name(db, user), mode=mode,
                   parent_note=(note or None), notes_visible=d.notes_visible_default)
    try:
        with db.begin_nested():
            db.add(b)
            db.flush()
    except IntegrityError:
        raise HTTPException(status_code=409, detail="Someone just took this slot. Pick another time.")
    from modules.notifications.service import notify
    tz = service.company_tz(db, cid)
    notify(db, cid, [t.employee_id], kind="comms_ptm", title="New PTM booking",
           body=f"{b.booked_by} booked {slot.start_at.astimezone(tz).strftime('%H:%M')} on {d.date.strftime('%d %b')}.",
           link="/dashboard/comms/ptm")
    return b


def staff_can_edit(db: Session, user: dict, t: PtmTeacher) -> bool:
    return (user.get("employee_id") and t.employee_id == user["employee_id"]) or service.has(db, user, "comms.ptm.manage")


def send_reminders(db: Session) -> int:
    """Background: remind families and teachers of tomorrow's meetings (once per booking)."""
    from modules.notifications.service import notify, notify_portal
    from modules.students.models import PortalAccount
    now = service.now()
    rows = (db.query(PtmBooking).join(PtmSlot, PtmSlot.id == PtmBooking.slot_id)
            .options(joinedload(PtmBooking.slot), joinedload(PtmBooking.teacher).joinedload(PtmTeacher.employee))
            .filter(PtmBooking.status == "booked", PtmBooking.reminder_sent_at.is_(None),
                    PtmSlot.start_at > now, PtmSlot.start_at <= now + timedelta(hours=24)).limit(500).all())
    for b in rows:
        db.info["company_id"] = b.company_id
        tz = service.company_tz(db, b.company_id)
        when = b.slot.start_at.astimezone(tz).strftime("%d %b, %H:%M")
        teacher = service.employee_name(b.teacher.employee) if b.teacher else "the teacher"
        if b.guardian_id:
            pids = [i for (i,) in db.query(PortalAccount.id).filter(PortalAccount.guardian_id == b.guardian_id,
                                                                     PortalAccount.active.is_(True)).all()]
            if pids:
                notify_portal(db, b.company_id, pids, kind="comms_ptm", title=f"Reminder: meeting with {teacher}",
                              body=f"Your parent-teacher meeting is on {when}.", link="/dashboard/my/meetings")
        if b.teacher:
            notify(db, b.company_id, [b.teacher.employee_id], kind="comms_ptm", title="PTM reminder",
                   body=f"Meeting with {b.booked_by or 'a parent'} on {when}.", link="/dashboard/comms/ptm",
                   group_key=f"ptm_reminder:{b.ptm_day_id}")
        b.reminder_sent_at = now
    db.info.pop("company_id", None)
    db.flush()
    return len(rows)
