"""Staff API for parent-teacher meetings."""
from datetime import date as Date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, joinedload

from core import audit
from core.crud import get_or_404
from core.db import get_db
from core.pagination import PageParams, paginate
from core.security import require_permission
from modules.comms import ptm, service
from modules.comms.models import PtmBooking, PtmDay, PtmSlot, PtmTeacher

router = APIRouter()
VIEW, MANAGE = "comms.view", "comms.ptm.manage"


class BreakIn(BaseModel):
    start: str
    end: str
    label: str | None = None


class DayIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    date: Date
    start_time: str = "09:00"
    end_time: str = "13:00"
    slot_minutes: int = 10
    gap_minutes: int = 0
    breaks: list[BreakIn] = []
    mode: str = "in_person"
    location: str | None = Field(None, max_length=200)
    meeting_link: str | None = Field(None, max_length=500)
    section_ids: list[int] = []
    instructions: str | None = Field(None, max_length=3000)
    booking_closes_at: datetime | None = None
    notes_visible_default: bool = False
    teacher_ids: list[int] | None = None  # empty = class and subject teachers of the sections


class DayPatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    date: Date | None = None
    start_time: str | None = None
    end_time: str | None = None
    slot_minutes: int | None = None
    gap_minutes: int | None = None
    breaks: list[BreakIn] | None = None
    mode: str | None = None
    location: str | None = None
    meeting_link: str | None = None
    section_ids: list[int] | None = None
    instructions: str | None = None
    booking_closes_at: datetime | None = None
    notes_visible_default: bool | None = None


class TeachersIn(BaseModel):
    employee_ids: list[int]


class TeacherPatch(BaseModel):
    location: str | None = Field(None, max_length=200)
    meeting_link: str | None = Field(None, max_length=500)
    start_time: str | None = None
    end_time: str | None = None


class BookingPatch(BaseModel):
    status: str | None = None  # booked | met | no_show
    notes: str | None = Field(None, max_length=5000)
    action_points: list[dict] | None = None
    notes_visible: bool | None = None


class StaffBookIn(BaseModel):
    slot_id: int
    student_id: int


@router.get("/ptm/days")
def list_days(status: str | None = None, q: str | None = None, params: PageParams = Depends(),
              db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    cid = user["company_id"]
    query = db.query(PtmDay).filter(PtmDay.company_id == cid, PtmDay.archived_at.is_(None))
    if not service.has(db, user, MANAGE):
        # teachers see the days they take part in
        query = query.join(PtmTeacher, PtmTeacher.ptm_day_id == PtmDay.id).filter(
            PtmTeacher.employee_id == (user.get("employee_id") or -1))
    if status == "upcoming":
        query = query.filter(PtmDay.date >= Date.today())
    elif status == "past":
        query = query.filter(PtmDay.date < Date.today())
    elif status:
        query = query.filter(PtmDay.status == status)
    if q:
        query = query.filter(PtmDay.title.ilike(f"%{q.strip()}%"))
    page = paginate(query.order_by(PtmDay.date.desc()), params)
    stats = ptm.day_stats(db, [d.id for d in page["items"]])
    page["items"] = [ptm.day_out(db, d, stats.get(d.id)) for d in page["items"]]
    page["can_manage"] = service.has(db, user, MANAGE)
    return page


@router.get("/ptm/suggest-teachers")
def suggest(section_ids: str = "", db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    from modules.employees.models import Employee
    ids = [int(x) for x in section_ids.split(",") if x.strip().isdigit()]
    emp_ids = ptm.suggested_teachers(db, user["company_id"], ids)
    rows = db.query(Employee).filter(Employee.id.in_(emp_ids or [-1])).all()
    return [{"id": e.id, "name": service.employee_name(e)} for e in sorted(rows, key=service.employee_name)]


@router.post("/ptm/days", status_code=201)
def create_day(payload: DayIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = user["company_id"]
    d = PtmDay(company_id=cid, created_by=service.actor_name(db, user), status="draft",
               start_time=ptm._t(payload.start_time), end_time=ptm._t(payload.end_time))
    data = payload.model_dump(exclude={"teacher_ids"})
    data["breaks"] = [b.model_dump() for b in payload.breaks]
    ptm.apply_fields(db, cid, d, data)
    db.add(d)
    db.flush()
    teachers = payload.teacher_ids if payload.teacher_ids else ptm.suggested_teachers(db, cid, d.section_ids)
    ptm.set_teachers(db, d, teachers)
    db.commit()
    return _day_full(db, d)


def _day_full(db: Session, d: PtmDay) -> dict:
    out = ptm.day_out(db, d, ptm.day_stats(db, [d.id]).get(d.id))
    teachers = db.query(PtmTeacher).options(joinedload(PtmTeacher.employee)).filter(PtmTeacher.ptm_day_id == d.id).all()
    out["teachers"] = sorted((ptm.teacher_out(db, t) for t in teachers), key=lambda x: x["name"])
    return out


@router.get("/ptm/days/{uuid}")
def get_day(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    d = get_or_404(db, PtmDay, user["company_id"], uuid, "Meeting day")
    out = _day_full(db, d)
    out["can_manage"] = service.has(db, user, MANAGE)
    out["my_teacher_id"] = next((t["id"] for t in out["teachers"] if t["employee_id"] == user.get("employee_id")), None)
    if not out["can_manage"] and not out["my_teacher_id"]:
        raise HTTPException(status_code=404, detail="Meeting day not found")
    return out


@router.put("/ptm/days/{uuid}")
def update_day(uuid: UUID, payload: DayPatch, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    d = get_or_404(db, PtmDay, user["company_id"], uuid, "Meeting day")
    data = payload.model_dump(exclude_unset=True)
    if "breaks" in data:
        data["breaks"] = [b.model_dump() if hasattr(b, "model_dump") else b for b in (payload.breaks or [])]
    timing = any(k in data for k in ("date", "start_time", "end_time", "slot_minutes", "gap_minutes", "breaks"))
    ptm.apply_fields(db, user["company_id"], d, data)
    if timing:
        ptm.regenerate_all(db, d)
    db.commit()
    return _day_full(db, d)


@router.post("/ptm/days/{uuid}/teachers")
def set_teachers(uuid: UUID, payload: TeachersIn, db: Session = Depends(get_db),
                 user: dict = Depends(require_permission(MANAGE))):
    d = get_or_404(db, PtmDay, user["company_id"], uuid, "Meeting day")
    ptm.set_teachers(db, d, payload.employee_ids)
    db.commit()
    return _day_full(db, d)


@router.put("/ptm/teachers/{uuid}")
def update_teacher(uuid: UUID, payload: TeacherPatch, db: Session = Depends(get_db),
                   user: dict = Depends(require_permission(VIEW))):
    t = get_or_404(db, PtmTeacher, user["company_id"], uuid, "Teacher")
    if not ptm.staff_can_edit(db, user, t):
        raise HTTPException(status_code=403, detail="You don't have access to this.")
    data = payload.model_dump(exclude_unset=True)
    for k in ("location", "meeting_link"):
        if k in data:
            setattr(t, k, data[k] or None)
    retime = False
    for k in ("start_time", "end_time"):
        if k in data:
            setattr(t, k, ptm._t(data[k]) if data[k] else None)
            retime = True
    if retime:
        ptm.generate_slots(db, t.day, t)
    db.commit()
    return ptm.teacher_out(db, t)


@router.post("/ptm/days/{uuid}/status")
def set_day_status(uuid: UUID, payload: dict, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    d = get_or_404(db, PtmDay, user["company_id"], uuid, "Meeting day")
    status = payload.get("status")
    if status == "open":
        ptm.open_day(db, d, service.actor_name(db, user))
    elif status in ("closed", "completed", "draft"):
        d.status = status
        audit.event(db, f"comms.ptm_{status}", "comms_ptm_days", d.id, {}, entity_uuid=str(d.uuid), entity_label=d.title)
    elif status == "archived":
        d.archived_at = service.now()
    else:
        raise HTTPException(status_code=400, detail="Unknown status.")
    db.commit()
    return _day_full(db, d)


@router.get("/ptm/teachers/{uuid}/schedule")
def schedule(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    t = get_or_404(db, PtmTeacher, user["company_id"], uuid, "Teacher")
    if not ptm.staff_can_edit(db, user, t):
        raise HTTPException(status_code=403, detail="You don't have access to this.")
    return {"teacher": ptm.teacher_out(db, t), "slots": ptm.teacher_schedule(db, t.day, t),
            "can_edit": True}


@router.post("/ptm/slots/{uuid}/block")
def block_slot(uuid: UUID, payload: dict, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    s = get_or_404(db, PtmSlot, user["company_id"], uuid, "Slot")
    if not ptm.staff_can_edit(db, user, s.teacher):
        raise HTTPException(status_code=403, detail="You don't have access to this.")
    if payload.get("blocked") and db.query(PtmBooking.id).filter(PtmBooking.slot_id == s.id,
                                                                  PtmBooking.status != "cancelled").first():
        raise HTTPException(status_code=400, detail="This slot is booked. Cancel the booking first.")
    s.blocked = bool(payload.get("blocked"))
    db.commit()
    return {"blocked": s.blocked}


@router.put("/ptm/bookings/{uuid}")
def update_booking(uuid: UUID, payload: BookingPatch, db: Session = Depends(get_db),
                   user: dict = Depends(require_permission(VIEW))):
    """The teacher records attendance, notes and action points (and whether parents see them)."""
    b = get_or_404(db, PtmBooking, user["company_id"], uuid, "Booking")
    if not ptm.staff_can_edit(db, user, b.teacher):
        raise HTTPException(status_code=403, detail="You don't have access to this.")
    if b.status == "cancelled":
        raise HTTPException(status_code=400, detail="This booking was cancelled.")
    data = payload.model_dump(exclude_unset=True)
    if "status" in data:
        if data["status"] not in ("booked", "met", "no_show"):
            raise HTTPException(status_code=400, detail="Mark it met or no-show.")
        b.status = data["status"]
    if "notes" in data:
        b.notes = data["notes"]
    if "action_points" in data:
        b.action_points = [{"text": str(a.get("text", ""))[:300], "done": bool(a.get("done"))}
                           for a in (data["action_points"] or []) if str(a.get("text", "")).strip()][:20]
    if "notes_visible" in data:
        b.notes_visible = bool(data["notes_visible"])
    db.commit()
    db.refresh(b)
    return ptm.booking_out(b)


@router.post("/ptm/bookings/{uuid}/cancel")
def cancel_booking(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    b = get_or_404(db, PtmBooking, user["company_id"], uuid, "Booking")
    if not ptm.staff_can_edit(db, user, b.teacher):
        raise HTTPException(status_code=403, detail="You don't have access to this.")
    b.status, b.cancelled_at = "cancelled", service.now()
    if b.guardian_id:
        from modules.notifications.service import notify_portal
        accounts = service.portal_ids_for(db, b.company_id, [b.guardian_id])
        if accounts:
            notify_portal(db, b.company_id, list(accounts.values()), kind="comms_ptm",
                          title="Your PTM slot was cancelled", body="Please pick another time.",
                          link="/dashboard/my/meetings")
    db.commit()
    return {"ok": True}


@router.get("/ptm/mine")
def my_meetings(db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    """The signed-in teacher's upcoming meeting days with their schedule."""
    cid = user["company_id"]
    rows = (db.query(PtmTeacher).join(PtmDay, PtmDay.id == PtmTeacher.ptm_day_id)
            .filter(PtmTeacher.company_id == cid, PtmTeacher.employee_id == (user.get("employee_id") or -1),
                    PtmDay.archived_at.is_(None), PtmDay.date >= Date.today(), PtmDay.status != "draft")
            .order_by(PtmDay.date).all())
    return [{"day": ptm.day_out(db, t.day), "teacher": ptm.teacher_out(db, t)} for t in rows]
