"""Hostel rules: bed allocation, check-out, roll-call, leave / out-pass and Student.is_boarder.

Functions other modules may use:
    current_allocation(db, company_id, student_id) -> HostelAllocation | None
    boarder_summary(db, company_id, student_id)    -> dict for the portal / home card

Rules:
- A bed holds one active boarder; a boarder holds one active bed.
- The block's gender must suit the learner (boys / girls blocks; mixed takes anyone).
- Rooms under maintenance or switched off take no new boarders.
- Moving a boarder checks out the old bed the day before and checks in the new one.
- Student.is_boarder follows whether the learner has an active allocation.
- Leave and out-passes: parents ask from the portal (or the warden records it); the warden
  approves or rejects; approved passes are marked out and back at the gate.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from core import audit
from core.context import current as current_ctx
from modules.hostel.models import (HostelAllocation, HostelBed, HostelBlock, HostelLeave, HostelMessPlan, HostelRoom,
                                   HostelRollCall)

GENDER_OK = {"boys": {"male"}, "girls": {"female"}}


def actor() -> str:
    return current_ctx().actor_name or "System"


def today(db: Session, company_id: int) -> date:
    from core.timeutil import today_for_company_id
    return today_for_company_id(db, company_id)


def day_bounds(db: Session, company_id: int, day: date) -> tuple[datetime, datetime]:
    """UTC start and end of a calendar day in the institution's timezone."""
    from core.timeutil import _load_company, tz_for
    tz = tz_for(_load_company(db, company_id))
    start = datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(timezone.utc)
    return start, start + timedelta(days=1)


def sync_beds(db: Session, room: HostelRoom) -> None:
    """Keep one bed row per bed in the room (labels 1..n); extra beds are switched off, never removed."""
    have = {b.label: b for b in db.query(HostelBed).filter(HostelBed.room_id == room.id).all()}
    for i in range(1, int(room.beds) + 1):
        label = str(i)
        if label in have:
            have[label].active = True
        else:
            db.add(HostelBed(company_id=room.company_id, room_id=room.id, label=label))
    for label, b in have.items():
        if label.isdigit() and int(label) > int(room.beds):
            occupied = db.query(HostelAllocation.id).filter(HostelAllocation.bed_id == b.id,
                                                            HostelAllocation.status == "active").first()
            if occupied:
                raise HTTPException(status_code=400, detail=f"Bed {label} is occupied; check that boarder out first.")
            b.active = False
    db.flush()


def current_allocation(db: Session, company_id: int, student_id: int) -> HostelAllocation | None:
    return (db.query(HostelAllocation).options(joinedload(HostelAllocation.block).joinedload(HostelBlock.warden),
                                               joinedload(HostelAllocation.room), joinedload(HostelAllocation.bed),
                                               joinedload(HostelAllocation.mess_plan))
            .filter(HostelAllocation.company_id == company_id, HostelAllocation.student_id == student_id,
                    HostelAllocation.status == "active").first())


def _sync_flag(db: Session, company_id: int, student_id: int) -> None:
    from modules.students.models import Student
    s = db.query(Student).filter(Student.company_id == company_id, Student.id == student_id).first()
    if s is None:
        return
    live = db.query(HostelAllocation.id).filter(HostelAllocation.company_id == company_id,
                                                HostelAllocation.student_id == student_id,
                                                HostelAllocation.status == "active").first() is not None
    if s.is_boarder != live:
        s.is_boarder = live


def allocate(db: Session, company_id: int, *, student_id: int, bed: HostelBed, check_in: date,
             mess_plan: HostelMessPlan | None = None, notes: str | None = None, bill: bool = False,
             move: bool = False) -> HostelAllocation:
    from modules.students.models import Student
    s = db.query(Student).filter(Student.company_id == company_id, Student.id == student_id).first()
    if s is None:
        raise HTTPException(status_code=400, detail="The student was not found.")
    if s.status != "active":
        raise HTTPException(status_code=400, detail=f"{s.full_name} is not an active student.")
    room, block = bed.room, bed.room.block
    if not bed.active or not room.active or not block.active:
        raise HTTPException(status_code=400, detail="This bed is not in use.")
    if room.status == "maintenance":
        raise HTTPException(status_code=400, detail=f"Room {room.room_no} is under maintenance.")
    allowed = GENDER_OK.get(block.gender)
    if allowed and (s.gender or "") not in allowed:
        raise HTTPException(status_code=400, detail=f"{block.name} is for {block.gender} only.")
    taken = db.query(HostelAllocation).options(joinedload(HostelAllocation.student)).filter(
        HostelAllocation.company_id == company_id, HostelAllocation.bed_id == bed.id,
        HostelAllocation.status == "active").first()
    if taken is not None and taken.student_id != student_id:
        raise HTTPException(status_code=400, detail=f"Bed {bed.label} in room {room.room_no} is taken by {taken.student.full_name}.")
    mine = current_allocation(db, company_id, student_id)
    if mine is not None:
        if mine.bed_id == bed.id:
            raise HTTPException(status_code=400, detail=f"{s.full_name} already has this bed.")
        if not move:
            raise HTTPException(status_code=400, detail=f"{s.full_name} already has bed {mine.bed.label} in room {mine.room.room_no}. Move them instead.")
        mine.status = "checked_out"
        mine.check_out = max(mine.check_in, check_in - timedelta(days=1))
    fee = Decimal(room.fee or 0) + Decimal(mess_plan.fee if mess_plan else 0)
    a = HostelAllocation(company_id=company_id, student_id=student_id, block_id=block.id, room_id=room.id, bed_id=bed.id,
                         mess_plan_id=mess_plan.id if mess_plan else None, check_in=check_in,
                         fee_amount=fee if fee else None, notes=notes)
    db.add(a)
    db.flush()
    if bill and fee and mine is None:
        from modules.library.service import bill_via_fees
        a.fee_ref = bill_via_fees(db, company_id, student_id, "HOSTEL", fee,
                                  f"Hostel: {block.name} room {room.room_no}" + (f" + {mess_plan.name}" if mess_plan else ""),
                                  check_in)
    _sync_flag(db, company_id, student_id)
    audit.event(db, "hostel.moved" if mine else "hostel.checked_in", "hostel_allocations", a.id,
                {"block": block.name, "room": room.room_no, "bed": bed.label}, entity_label=s.full_name)
    db.flush()
    return a


def check_out(db: Session, company_id: int, a: HostelAllocation, on: date, notes: str | None = None) -> HostelAllocation:
    if a.status != "active":
        raise HTTPException(status_code=400, detail="This boarder has already checked out.")
    a.status, a.check_out = "checked_out", max(on, a.check_in)
    if notes:
        a.notes = ((a.notes or "") + "\n" + notes).strip()
    db.flush()
    _sync_flag(db, company_id, a.student_id)
    audit.event(db, "hostel.checked_out", "hostel_allocations", a.id, {"on": str(a.check_out)},
                entity_label=a.student.full_name if a.student else None)
    return a


def block_boarders(db: Session, company_id: int, block_id: int) -> list[HostelAllocation]:
    return (db.query(HostelAllocation).options(joinedload(HostelAllocation.student), joinedload(HostelAllocation.room),
                                               joinedload(HostelAllocation.bed))
            .filter(HostelAllocation.company_id == company_id, HostelAllocation.block_id == block_id,
                    HostelAllocation.status == "active").all())


def on_leave_ids(db: Session, company_id: int, day: date) -> set[int]:
    """Boarders away on approved leave that covers this night."""
    from core.timeutil import _load_company, tz_for
    tz = tz_for(_load_company(db, company_id))
    start = datetime(day.year, day.month, day.day, 22, tzinfo=tz).astimezone(timezone.utc)
    rows = db.query(HostelLeave.student_id).filter(HostelLeave.company_id == company_id,
                                                   HostelLeave.status.in_(("approved", "out")),
                                                   HostelLeave.from_at <= start, HostelLeave.to_at >= start).all()
    return {sid for (sid,) in rows}


def save_roll_call(db: Session, company_id: int, block: HostelBlock, day: date, session: str,
                   marks: dict[int, str], remarks: dict[int, str] | None = None) -> dict:
    allowed = {a.student_id for a in block_boarders(db, company_id, block.id)}
    existing = {r.student_id: r for r in db.query(HostelRollCall).filter(
        HostelRollCall.company_id == company_id, HostelRollCall.day == day, HostelRollCall.session == session,
        HostelRollCall.student_id.in_(allowed or {0})).all()}
    absent = []
    for sid, status in marks.items():
        if sid not in allowed:
            continue
        if status not in ("present", "absent", "leave"):
            raise HTTPException(status_code=400, detail="Mark each boarder present, absent or on leave.")
        r = existing.get(sid)
        if r is None:
            r = HostelRollCall(company_id=company_id, block_id=block.id, student_id=sid, day=day, session=session, status=status)
            db.add(r)
        r.status, r.marked_by, r.block_id = status, actor(), block.id
        r.remark = (remarks or {}).get(sid)
        if status == "absent":
            absent.append(sid)
    db.flush()
    return {"saved": len([s for s in marks if s in allowed]), "absent": absent}


# ── Leave and out-passes ─────────────────────────────────────────────

def request_leave(db: Session, company_id: int, *, student_id: int, kind: str, from_at: datetime, to_at: datetime,
                  reason: str | None, escort: str | None, via: str) -> HostelLeave:
    if to_at <= from_at:
        raise HTTPException(status_code=400, detail="The return time must be after the leaving time.")
    if current_allocation(db, company_id, student_id) is None:
        raise HTTPException(status_code=400, detail="Only boarders can ask for hostel leave.")
    clash = db.query(HostelLeave.id).filter(HostelLeave.company_id == company_id, HostelLeave.student_id == student_id,
                                            HostelLeave.status.in_(("pending", "approved", "out")),
                                            HostelLeave.from_at < to_at, HostelLeave.to_at > from_at).first()
    if clash:
        raise HTTPException(status_code=400, detail="There is already a leave request for this time.")
    lv = HostelLeave(company_id=company_id, student_id=student_id, kind=kind, from_at=from_at, to_at=to_at, reason=reason,
                     escort=escort, requested_by=actor(), requested_via=via)
    db.add(lv)
    db.flush()
    if via == "portal":
        a = current_allocation(db, company_id, student_id)
        wardens = [a.block.warden_id] + list(a.block.assistant_ids or []) if a else []
        try:
            from modules.notifications.service import notify
            notify(db, company_id, [w for w in wardens if w] or [None], kind="hostel.leave_requested",
                   title="Hostel leave request", body=f"{lv.student.full_name}: {from_at:%d %b %H:%M} to {to_at:%d %b %H:%M}",
                   link="/dashboard/hostel?tab=leave")
        except Exception:
            pass
    return lv


def decide_leave(db: Session, company_id: int, lv: HostelLeave, action: str, note: str | None = None) -> HostelLeave:
    now = datetime.now(timezone.utc)
    flow = {"approve": ("pending", "approved"), "reject": ("pending", "rejected"), "out": ("approved", "out"),
            "back": ("out", "returned")}
    if action == "cancel":
        if lv.status not in ("pending", "approved"):
            raise HTTPException(status_code=400, detail="Only pending or approved leave can be cancelled.")
        lv.status = "cancelled"
    else:
        need, to = flow[action]
        if lv.status != need:
            raise HTTPException(status_code=400, detail=f"This request is {lv.status}.")
        lv.status = to
    if action in ("approve", "reject"):
        lv.decided_by, lv.decided_at, lv.decision_note = actor(), now, note
    elif action == "out":
        lv.out_at = now
    elif action == "back":
        lv.returned_at = now
    db.flush()
    if action in ("approve", "reject", "out", "back"):
        words = {"approve": "approved", "reject": "not approved", "out": "has left the hostel", "back": "is back in the hostel"}
        title = f"Hostel {'leave' if lv.kind == 'leave' else 'out-pass'} {words[action]}" if action in ("approve", "reject") \
            else f"{lv.student.first_name} {words[action]}"
        try:
            from modules.notifications.service import notify_portal
            from modules.students.service import portal_account_ids_for_students
            ids = portal_account_ids_for_students(db, company_id, [lv.student_id])
            if ids:
                notify_portal(db, company_id, ids, kind=f"hostel.leave_{action}", title=title,
                              body=(note or f"{lv.from_at:%d %b %H:%M} to {lv.to_at:%d %b %H:%M}"),
                              link="/dashboard/my/hostel")
        except Exception:
            pass
    audit.event(db, f"hostel.leave_{lv.status}", "hostel_leaves", lv.id, {"note": note}, entity_label=lv.student.full_name)
    return lv


def occupancy(db: Session, company_id: int, block_id: int | None = None) -> dict:
    q = db.query(func.count(HostelBed.id)).join(HostelRoom, HostelRoom.id == HostelBed.room_id).filter(
        HostelBed.company_id == company_id, HostelBed.active.is_(True), HostelRoom.active.is_(True))
    o = db.query(func.count(HostelAllocation.id)).filter(HostelAllocation.company_id == company_id,
                                                         HostelAllocation.status == "active")
    if block_id:
        q = q.filter(HostelRoom.block_id == block_id)
        o = o.filter(HostelAllocation.block_id == block_id)
    beds, used = q.scalar() or 0, o.scalar() or 0
    return {"beds": beds, "occupied": used, "free": max(0, beds - used)}


def boarder_summary(db: Session, company_id: int, student_id: int) -> dict | None:
    a = current_allocation(db, company_id, student_id)
    if a is None:
        return None
    w = a.block.warden
    return {"block": a.block.name, "gender": a.block.gender, "room": a.room.room_no, "floor": a.room.floor,
            "bed": a.bed.label, "room_type": a.room.room_type, "check_in": a.check_in,
            "mess_plan": a.mess_plan.name if a.mess_plan else None,
            "warden": {"name": f"{w.first_name} {w.last_name}".strip(), "phone": w.phone} if w else None,
            "hostel_phone": a.block.phone}
