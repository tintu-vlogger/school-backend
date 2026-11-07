"""Hostel API: blocks, rooms and beds, mess plans, allocation, roll-call, leave / out-passes,
visitors, summary and the portal (/hostel/portal/...)."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from core.crud import by_id_or_400, crud_router, get_or_404
from core.db import get_db
from core.pagination import PageParams, paginate
from core.security import get_current_user, require_permission
from modules.hostel import service
from modules.hostel.models import (HostelAllocation, HostelBed, HostelBlock, HostelLeave, HostelMessPlan, HostelRoom,
                                   HostelRollCall, HostelVisitor)

router = APIRouter(prefix="/hostel", tags=["Hostel"])
VIEW, MANAGE = "hostel.view", "hostel.manage"


def _cid(user: dict) -> int:
    return user["company_id"]


def _money(v) -> str | None:
    return None if v is None else f"{Decimal(v):.2f}"


def _emp_name(e) -> str | None:
    return f"{e.first_name} {e.last_name}".strip() if e is not None else None


# ── Mess plans ───────────────────────────────────────────────────────

class MessIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    fee: Decimal = Field(Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    description: str | None = None
    active: bool = True


class MessPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    fee: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=2)
    description: str | None = None
    active: bool | None = None


router.include_router(crud_router(
    model=HostelMessPlan, prefix="/mess-plans", tag="Hostel", create_schema=MessIn, update_schema=MessPatch,
    view_perm=VIEW, manage_perm=MANAGE, label="Mess plan",
    out=lambda m: {"id": m.id, "uuid": str(m.uuid), "name": m.name, "fee": _money(m.fee), "description": m.description,
                   "active": m.active}))


# ── Blocks ───────────────────────────────────────────────────────────

class BlockIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    code: str | None = Field(None, max_length=20)
    gender: str = Field("mixed", pattern="^(boys|girls|mixed)$")
    warden_id: int | None = None
    assistant_ids: list[int] = []
    floors: int | None = Field(None, ge=1, le=50)
    phone: str | None = Field(None, max_length=20)
    address: str | None = None
    notes: str | None = None
    active: bool = True


class BlockPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    code: str | None = None
    gender: str | None = Field(None, pattern="^(boys|girls|mixed)$")
    warden_id: int | None = None
    assistant_ids: list[int] | None = None
    floors: int | None = Field(None, ge=1, le=50)
    phone: str | None = None
    address: str | None = None
    notes: str | None = None
    active: bool | None = None


def _block_out(db: Session, b: HostelBlock) -> dict:
    from modules.employees.models import Employee
    assistants = []
    if b.assistant_ids:
        assistants = [{"id": e.id, "name": _emp_name(e)} for e in db.query(Employee).filter(
            Employee.company_id == b.company_id, Employee.id.in_(b.assistant_ids)).all()]
    rooms = db.query(func.count(HostelRoom.id)).filter(HostelRoom.block_id == b.id, HostelRoom.active.is_(True)).scalar()
    return {"id": b.id, "uuid": str(b.uuid), "name": b.name, "code": b.code, "gender": b.gender,
            "warden_id": b.warden_id, "warden_name": _emp_name(b.warden), "warden_phone": b.warden.phone if b.warden else None,
            "assistant_ids": b.assistant_ids or [], "assistants": assistants, "floors": b.floors, "phone": b.phone,
            "address": b.address, "notes": b.notes, "active": b.active, "rooms": rooms or 0,
            **service.occupancy(db, b.company_id, b.id)}


def _check_staff(db: Session, cid: int, data: dict) -> None:
    from modules.employees.models import Employee
    by_id_or_400(db, Employee, cid, data.get("warden_id"), "Warden")
    for i in data.get("assistant_ids") or []:
        by_id_or_400(db, Employee, cid, i, "Assistant warden")


@router.get("/blocks")
def list_blocks(active: bool | None = Query(True), db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    q = db.query(HostelBlock).options(joinedload(HostelBlock.warden)).filter(HostelBlock.company_id == _cid(user))
    if active is not None:
        q = q.filter(HostelBlock.active.is_(active))
    rows = q.order_by(HostelBlock.name).all()
    return {"items": [_block_out(db, b) for b in rows], "total": len(rows), "page": 1, "size": len(rows)}


@router.post("/blocks", status_code=201)
def create_block(payload: BlockIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    data = payload.model_dump()
    _check_staff(db, cid, data)
    if db.query(HostelBlock.id).filter(HostelBlock.company_id == cid, HostelBlock.name == data["name"].strip()).first():
        raise HTTPException(status_code=409, detail="A block with this name already exists.")
    b = HostelBlock(company_id=cid, **data)
    db.add(b)
    db.commit()
    return _block_out(db, b)


@router.put("/blocks/{uuid}")
def update_block(uuid: UUID, payload: BlockPatch, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    b = get_or_404(db, HostelBlock, cid, uuid, "Block")
    data = payload.model_dump(exclude_unset=True)
    _check_staff(db, cid, data)
    if data.get("gender") and data["gender"] != b.gender and data["gender"] != "mixed":
        from modules.students.models import Student
        want = service.GENDER_OK[data["gender"]]
        wrong = db.query(HostelAllocation.id).join(Student, Student.id == HostelAllocation.student_id).filter(
            HostelAllocation.block_id == b.id, HostelAllocation.status == "active",
            or_(Student.gender.is_(None), ~Student.gender.in_(want))).first()
        if wrong:
            raise HTTPException(status_code=400, detail="Some current boarders don't match; move them first.")
    for k, v in data.items():
        setattr(b, k, v)
    db.commit()
    return _block_out(db, b)


@router.delete("/blocks/{uuid}")
def archive_block(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    b = get_or_404(db, HostelBlock, cid, uuid, "Block")
    if service.occupancy(db, cid, b.id)["occupied"]:
        raise HTTPException(status_code=400, detail="Boarders still live in this block.")
    b.active = False
    db.commit()
    return {"detail": "Switched off"}


# ── Rooms and beds ───────────────────────────────────────────────────

class RoomIn(BaseModel):
    block_id: int
    floor: str | None = Field(None, max_length=20)
    room_no: str = Field(..., min_length=1, max_length=20)
    room_type: str = Field("double", pattern="^(single|double|triple|dorm)$")
    beds: int = Field(2, ge=1, le=40)
    fee: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=2)
    amenities: list[str] = []
    status: str = Field("open", pattern="^(open|maintenance)$")
    notes: str | None = None


class RoomPatch(BaseModel):
    floor: str | None = None
    room_no: str | None = Field(None, min_length=1, max_length=20)
    room_type: str | None = Field(None, pattern="^(single|double|triple|dorm)$")
    beds: int | None = Field(None, ge=1, le=40)
    fee: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=2)
    amenities: list[str] | None = None
    status: str | None = Field(None, pattern="^(open|maintenance)$")
    notes: str | None = None
    active: bool | None = None


def _room_out(r: HostelRoom, occupants: dict[int, HostelAllocation]) -> dict:
    beds = []
    for b in r.bed_rows:
        if not b.active:
            continue
        a = occupants.get(b.id)
        beds.append({"id": b.id, "uuid": str(b.uuid), "label": b.label,
                     "occupant": {"allocation_uuid": str(a.uuid), "student_id": a.student_id, "name": a.student.full_name,
                                  "admission_no": a.student.admission_no, "check_in": a.check_in} if a else None})
    return {"id": r.id, "uuid": str(r.uuid), "block_id": r.block_id, "block_name": r.block.name, "floor": r.floor,
            "room_no": r.room_no, "room_type": r.room_type, "beds": r.beds, "fee": _money(r.fee),
            "amenities": r.amenities or [], "status": r.status, "notes": r.notes, "active": r.active,
            "occupied": sum(1 for b in beds if b["occupant"]), "bed_list": beds}


def _occupants(db: Session, cid: int, room_ids: list[int]) -> dict[int, HostelAllocation]:
    if not room_ids:
        return {}
    return {a.bed_id: a for a in db.query(HostelAllocation).options(joinedload(HostelAllocation.student)).filter(
        HostelAllocation.company_id == cid, HostelAllocation.room_id.in_(room_ids), HostelAllocation.status == "active").all()}


@router.get("/rooms")
def list_rooms(block_id: int | None = None, q: str | None = None, availability: str | None = None,
               active: bool | None = Query(True), db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    cid = _cid(user)
    query = db.query(HostelRoom).options(joinedload(HostelRoom.block), joinedload(HostelRoom.bed_rows)).filter(
        HostelRoom.company_id == cid)
    if block_id:
        query = query.filter(HostelRoom.block_id == block_id)
    if active is not None:
        query = query.filter(HostelRoom.active.is_(active))
    if q:
        query = query.filter(HostelRoom.room_no.ilike(f"%{q.strip()}%"))
    rows = query.order_by(HostelRoom.block_id, HostelRoom.floor, HostelRoom.room_no).all()
    occ = _occupants(db, cid, [r.id for r in rows])
    items = [_room_out(r, occ) for r in rows]
    if availability == "free":
        items = [r for r in items if r["occupied"] < len(r["bed_list"]) and r["status"] == "open"]
    elif availability == "full":
        items = [r for r in items if r["occupied"] >= len(r["bed_list"])]
    return {"items": items, "total": len(items), "page": 1, "size": len(items)}


@router.post("/rooms", status_code=201)
def create_room(payload: RoomIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    by_id_or_400(db, HostelBlock, cid, payload.block_id, "Block")
    if db.query(HostelRoom.id).filter(HostelRoom.block_id == payload.block_id, HostelRoom.room_no == payload.room_no.strip()).first():
        raise HTTPException(status_code=409, detail=f"Room {payload.room_no} already exists in this block.")
    r = HostelRoom(company_id=cid, **payload.model_dump())
    db.add(r)
    db.flush()
    service.sync_beds(db, r)
    db.commit()
    db.refresh(r)
    return _room_out(r, {})


@router.put("/rooms/{uuid}")
def update_room(uuid: UUID, payload: RoomPatch, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    r = get_or_404(db, HostelRoom, cid, uuid, "Room")
    data = payload.model_dump(exclude_unset=True)
    if data.get("active") is False and db.query(HostelAllocation.id).filter(
            HostelAllocation.room_id == r.id, HostelAllocation.status == "active").first():
        raise HTTPException(status_code=400, detail="Boarders still live in this room.")
    for k, v in data.items():
        setattr(r, k, v)
    db.flush()
    if "beds" in data:
        service.sync_beds(db, r)
    db.commit()
    db.refresh(r)
    return _room_out(r, _occupants(db, cid, [r.id]))


@router.get("/beds/free")
def free_beds(block_id: int | None = None, student_id: int | None = None, db: Session = Depends(get_db),
              user: dict = Depends(require_permission(VIEW))):
    """Beds that can take a boarder (for the allocation picker); filtered by the learner's gender."""
    from modules.students.models import Student
    cid = _cid(user)
    gender = None
    if student_id:
        s = db.query(Student).filter(Student.company_id == cid, Student.id == student_id).first()
        gender = s.gender if s else None
    taken = db.query(HostelAllocation.bed_id).filter(HostelAllocation.company_id == cid, HostelAllocation.status == "active")
    q = (db.query(HostelBed).join(HostelRoom, HostelRoom.id == HostelBed.room_id).join(HostelBlock, HostelBlock.id == HostelRoom.block_id)
         .options(joinedload(HostelBed.room).joinedload(HostelRoom.block))
         .filter(HostelBed.company_id == cid, HostelBed.active.is_(True), HostelRoom.active.is_(True),
                 HostelRoom.status == "open", HostelBlock.active.is_(True), ~HostelBed.id.in_(taken)))
    if block_id:
        q = q.filter(HostelRoom.block_id == block_id)
    if gender == "male":
        q = q.filter(HostelBlock.gender.in_(("boys", "mixed")))
    elif gender == "female":
        q = q.filter(HostelBlock.gender.in_(("girls", "mixed")))
    elif student_id:
        q = q.filter(HostelBlock.gender == "mixed")
    rows = q.order_by(HostelBlock.name, HostelRoom.room_no, HostelBed.label).limit(500).all()
    return [{"uuid": str(b.uuid), "label": f"{b.room.block.name} · Room {b.room.room_no} · Bed {b.label}",
             "room_type": b.room.room_type, "fee": _money(b.room.fee)} for b in rows]


# ── Allocations ──────────────────────────────────────────────────────

def _alloc_out(db: Session, a: HostelAllocation) -> dict:
    from modules.students import service as students
    e = students.current_enrollment(db, a.company_id, a.student_id)
    return {"id": a.id, "uuid": str(a.uuid), "student_id": a.student_id, "student_uuid": str(a.student.uuid),
            "student_name": a.student.full_name, "admission_no": a.student.admission_no, "gender": a.student.gender,
            "photo_url": a.student.photo_url,
            "section_label": students.academics.section_label(e.section) if e and e.section else None,
            "block": a.block.name, "block_id": a.block_id, "room": a.room.room_no, "floor": a.room.floor,
            "bed": a.bed.label, "bed_uuid": str(a.bed.uuid), "mess_plan": a.mess_plan.name if a.mess_plan else None,
            "mess_plan_id": a.mess_plan_id, "check_in": a.check_in, "check_out": a.check_out, "status": a.status,
            "fee_amount": _money(a.fee_amount), "fee_ref": a.fee_ref, "notes": a.notes}


@router.get("/allocations")
def list_allocations(q: str | None = None, block_id: int | None = None, status: str | None = Query("active"),
                     params: PageParams = Depends(), db: Session = Depends(get_db),
                     user: dict = Depends(require_permission(VIEW))):
    from modules.students.models import Student
    cid = _cid(user)
    query = (db.query(HostelAllocation).join(Student, Student.id == HostelAllocation.student_id)
             .join(HostelRoom, HostelRoom.id == HostelAllocation.room_id)
             .options(joinedload(HostelAllocation.student), joinedload(HostelAllocation.block),
                      joinedload(HostelAllocation.room), joinedload(HostelAllocation.bed), joinedload(HostelAllocation.mess_plan))
             .filter(HostelAllocation.company_id == cid))
    if status:
        query = query.filter(HostelAllocation.status == status)
    if block_id:
        query = query.filter(HostelAllocation.block_id == block_id)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Student.first_name.ilike(like), Student.last_name.ilike(like),
                                 Student.admission_no.ilike(like), HostelRoom.room_no.ilike(like)))
    query = query.order_by(HostelAllocation.block_id, HostelRoom.room_no, Student.first_name)
    return paginate(query, params, lambda a: _alloc_out(db, a))


class AllocIn(BaseModel):
    student_id: int
    bed_uuid: UUID
    check_in: date | None = None
    mess_plan_id: int | None = None
    notes: str | None = None
    bill_fee: bool = False
    move: bool = False


@router.post("/allocations", status_code=201)
def create_allocation(payload: AllocIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    bed = get_or_404(db, HostelBed, cid, payload.bed_uuid, "Bed")
    mess = by_id_or_400(db, HostelMessPlan, cid, payload.mess_plan_id, "Mess plan")
    a = service.allocate(db, cid, student_id=payload.student_id, bed=bed, check_in=payload.check_in or service.today(db, cid),
                         mess_plan=mess, notes=payload.notes, bill=payload.bill_fee, move=payload.move)
    db.commit()
    return _alloc_out(db, a)


class CheckOutIn(BaseModel):
    check_out: date | None = None
    notes: str | None = None


@router.post("/allocations/{uuid}/check-out")
def check_out(uuid: UUID, payload: CheckOutIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    a = get_or_404(db, HostelAllocation, cid, uuid, "Allocation")
    service.check_out(db, cid, a, payload.check_out or service.today(db, cid), payload.notes)
    db.commit()
    return _alloc_out(db, a)


class AllocPatch(BaseModel):
    mess_plan_id: int | None = None
    notes: str | None = None


@router.put("/allocations/{uuid}")
def update_allocation(uuid: UUID, payload: AllocPatch, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    a = get_or_404(db, HostelAllocation, cid, uuid, "Allocation")
    data = payload.model_dump(exclude_unset=True)
    if "mess_plan_id" in data:
        by_id_or_400(db, HostelMessPlan, cid, data["mess_plan_id"], "Mess plan")
    for k, v in data.items():
        setattr(a, k, v)
    db.commit()
    return _alloc_out(db, a)


# ── Roll-call ────────────────────────────────────────────────────────

@router.get("/blocks/{uuid}/roll-call")
def get_roll_call(uuid: UUID, day: date | None = None, session: str = Query("night", pattern="^(night|morning)$"),
                  db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    cid = _cid(user)
    b = get_or_404(db, HostelBlock, cid, uuid, "Block")
    day = day or service.today(db, cid)
    boarders = service.block_boarders(db, cid, b.id)
    marks = {r.student_id: r for r in db.query(HostelRollCall).filter(
        HostelRollCall.company_id == cid, HostelRollCall.day == day, HostelRollCall.session == session,
        HostelRollCall.student_id.in_([a.student_id for a in boarders] or [0])).all()}
    away = service.on_leave_ids(db, cid, day)
    boarders.sort(key=lambda a: (a.room.room_no, a.bed.label))
    items = [{"student_id": a.student_id, "student_name": a.student.full_name, "admission_no": a.student.admission_no,
              "room": a.room.room_no, "bed": a.bed.label, "on_leave": a.student_id in away,
              "status": marks[a.student_id].status if a.student_id in marks else None,
              "remark": marks[a.student_id].remark if a.student_id in marks else None} for a in boarders]
    taken = bool(marks)
    return {"block": b.name, "day": day, "session": session, "taken": taken, "items": items,
            "counts": {"present": sum(1 for i in items if i["status"] == "present"),
                       "absent": sum(1 for i in items if i["status"] == "absent"),
                       "leave": sum(1 for i in items if i["status"] == "leave"), "total": len(items)}}


class RollIn(BaseModel):
    day: date
    session: str = Field("night", pattern="^(night|morning)$")
    marks: dict[int, str]
    remarks: dict[int, str] = {}


@router.post("/blocks/{uuid}/roll-call")
def save_roll_call(uuid: UUID, payload: RollIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    b = get_or_404(db, HostelBlock, cid, uuid, "Block")
    if payload.day > service.today(db, cid):
        raise HTTPException(status_code=400, detail="Roll-call can't be taken for a future date.")
    res = service.save_roll_call(db, cid, b, payload.day, payload.session, payload.marks, payload.remarks)
    if res["absent"]:
        try:
            from modules.notifications.service import notify
            from modules.students.models import Student
            names = [s.full_name for s in db.query(Student).filter(Student.id.in_(res["absent"])).all()]
            people = [p for p in [b.warden_id, *(b.assistant_ids or [])] if p]
            notify(db, cid, people or [None], kind="hostel.absent", title=f"{len(names)} absent at {payload.session} roll-call",
                   body=f"{b.name}: " + ", ".join(names[:8]), link="/dashboard/hostel?tab=rollcall")
        except Exception:
            pass
    db.commit()
    return res


# ── Leave and out-passes ─────────────────────────────────────────────

def _leave_out(lv: HostelLeave) -> dict:
    return {"id": lv.id, "uuid": str(lv.uuid), "student_id": lv.student_id, "student_name": lv.student.full_name,
            "admission_no": lv.student.admission_no, "kind": lv.kind, "from_at": lv.from_at, "to_at": lv.to_at,
            "reason": lv.reason, "escort": lv.escort, "requested_by": lv.requested_by, "requested_via": lv.requested_via,
            "status": lv.status, "decided_by": lv.decided_by, "decided_at": lv.decided_at, "decision_note": lv.decision_note,
            "out_at": lv.out_at, "returned_at": lv.returned_at, "created_at": lv.created_at}


@router.get("/leaves")
def list_leaves(status: str | None = None, q: str | None = None, params: PageParams = Depends(),
                db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    from modules.students.models import Student
    cid = _cid(user)
    query = db.query(HostelLeave).join(Student, Student.id == HostelLeave.student_id).options(
        joinedload(HostelLeave.student)).filter(HostelLeave.company_id == cid)
    if status == "open":
        query = query.filter(HostelLeave.status.in_(("pending", "approved", "out")))
    elif status:
        query = query.filter(HostelLeave.status == status)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Student.first_name.ilike(like), Student.last_name.ilike(like)))
    return paginate(query.order_by(HostelLeave.from_at.desc()), params, _leave_out)


class LeaveIn(BaseModel):
    student_id: int
    kind: str = Field("leave", pattern="^(leave|outpass)$")
    from_at: datetime
    to_at: datetime
    reason: str | None = Field(None, max_length=1000)
    escort: str | None = Field(None, max_length=200)
    approve: bool = False


def _aware(v: datetime) -> datetime:
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


@router.post("/leaves", status_code=201)
def create_leave(payload: LeaveIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    lv = service.request_leave(db, cid, student_id=payload.student_id, kind=payload.kind, from_at=_aware(payload.from_at),
                               to_at=_aware(payload.to_at), reason=payload.reason, escort=payload.escort, via="staff")
    if payload.approve:
        service.decide_leave(db, cid, lv, "approve")
    db.commit()
    return _leave_out(lv)


class DecideIn(BaseModel):
    action: str = Field(..., pattern="^(approve|reject|out|back|cancel)$")
    note: str | None = Field(None, max_length=500)


@router.post("/leaves/{uuid}/decide")
def decide(uuid: UUID, payload: DecideIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    lv = get_or_404(db, HostelLeave, cid, uuid, "Leave request")
    service.decide_leave(db, cid, lv, payload.action, payload.note)
    db.commit()
    return _leave_out(lv)


# ── Visitors ─────────────────────────────────────────────────────────

def _visitor_out(v: HostelVisitor) -> dict:
    return {"id": v.id, "uuid": str(v.uuid), "name": v.name, "relation": v.relation, "phone": v.phone,
            "id_proof": v.id_proof, "purpose": v.purpose, "in_at": v.in_at, "out_at": v.out_at, "logged_by": v.logged_by,
            "student_id": v.student_id, "student_name": v.student.full_name if v.student else None,
            "block_id": v.block_id, "block": v.block.name if v.block else None}


@router.get("/visitors")
def list_visitors(day: date | None = None, inside: bool = False, q: str | None = None, params: PageParams = Depends(),
                  db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    cid = _cid(user)
    query = db.query(HostelVisitor).options(joinedload(HostelVisitor.student), joinedload(HostelVisitor.block)).filter(
        HostelVisitor.company_id == cid)
    if day:
        start, end = service.day_bounds(db, cid, day)
        query = query.filter(HostelVisitor.in_at >= start, HostelVisitor.in_at < end)
    if inside:
        query = query.filter(HostelVisitor.out_at.is_(None))
    if q:
        query = query.filter(HostelVisitor.name.ilike(f"%{q.strip()}%"))
    return paginate(query.order_by(HostelVisitor.in_at.desc()), params, _visitor_out)


class VisitorIn(BaseModel):
    student_id: int | None = None
    name: str = Field(..., min_length=1, max_length=200)
    relation: str | None = Field(None, max_length=40)
    phone: str | None = Field(None, max_length=20)
    id_proof: str | None = Field(None, max_length=80)
    purpose: str | None = Field(None, max_length=200)


@router.post("/visitors", status_code=201)
def log_visitor(payload: VisitorIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    block_id = None
    if payload.student_id:
        a = service.current_allocation(db, cid, payload.student_id)
        if a is None:
            raise HTTPException(status_code=400, detail="This learner is not a boarder.")
        block_id = a.block_id
    v = HostelVisitor(company_id=cid, block_id=block_id, logged_by=service.actor(), **payload.model_dump())
    db.add(v)
    db.commit()
    return _visitor_out(v)


@router.post("/visitors/{uuid}/out")
def visitor_out(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    v = get_or_404(db, HostelVisitor, _cid(user), uuid, "Visitor")
    if v.out_at is None:
        v.out_at = datetime.now(timezone.utc)
    db.commit()
    return _visitor_out(v)


# ── Summary ──────────────────────────────────────────────────────────

@router.get("/summary")
def summary(db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    cid = _cid(user)
    on = service.today(db, cid)
    occ = service.occupancy(db, cid)
    pending = db.query(func.count(HostelLeave.id)).filter(HostelLeave.company_id == cid, HostelLeave.status == "pending").scalar()
    out_now = db.query(func.count(HostelLeave.id)).filter(HostelLeave.company_id == cid, HostelLeave.status == "out").scalar()
    blocks = db.query(func.count(HostelBlock.id)).filter(HostelBlock.company_id == cid, HostelBlock.active.is_(True)).scalar()
    marked = db.query(func.count(HostelRollCall.id)).filter(HostelRollCall.company_id == cid, HostelRollCall.day == on,
                                                            HostelRollCall.session == "night").scalar()
    absent = db.query(func.count(HostelRollCall.id)).filter(HostelRollCall.company_id == cid, HostelRollCall.day == on,
                                                            HostelRollCall.session == "night", HostelRollCall.status == "absent").scalar()
    inside = db.query(func.count(HostelVisitor.id)).filter(HostelVisitor.company_id == cid, HostelVisitor.out_at.is_(None)).scalar()
    return {**occ, "blocks": blocks or 0, "pending_leave": pending or 0, "out_now": out_now or 0,
            "roll_call_taken": bool(marked), "absent_tonight": absent or 0, "visitors_inside": inside or 0}


# ── Portal ───────────────────────────────────────────────────────────

def _portal_check(db: Session, user: dict, student_id: int) -> None:
    from modules.students.service import require_student_access
    if not user.get("portal_id"):
        raise HTTPException(status_code=403, detail="This page is for parents and students.")
    require_student_access(db, user, student_id)


@router.get("/portal/summary")
def portal_summary(student_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _portal_check(db, user, student_id)
    cid = user["company_id"]
    leaves = (db.query(HostelLeave).options(joinedload(HostelLeave.student))
              .filter(HostelLeave.company_id == cid, HostelLeave.student_id == student_id)
              .order_by(HostelLeave.from_at.desc()).limit(20).all())
    return {"boarder": service.boarder_summary(db, cid, student_id),
            "leaves": [{k: v for k, v in _leave_out(lv).items() if k not in ("decided_by",)} for lv in leaves]}


class PortalLeaveIn(BaseModel):
    student_id: int
    kind: str = Field("leave", pattern="^(leave|outpass)$")
    from_at: datetime
    to_at: datetime
    reason: str = Field(..., min_length=2, max_length=1000)
    escort: str | None = Field(None, max_length=200)


@router.post("/portal/leaves", status_code=201)
def portal_leave(payload: PortalLeaveIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _portal_check(db, user, payload.student_id)
    if user.get("role") != "parent":
        raise HTTPException(status_code=403, detail="Leave is requested by a parent.")
    cid = user["company_id"]
    if _aware(payload.from_at) < datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(hours=1):
        raise HTTPException(status_code=400, detail="The leaving time is in the past.")
    lv = service.request_leave(db, cid, student_id=payload.student_id, kind=payload.kind, from_at=_aware(payload.from_at),
                               to_at=_aware(payload.to_at), reason=payload.reason, escort=payload.escort, via="portal")
    db.commit()
    return _leave_out(lv)


@router.post("/portal/leaves/{uuid}/cancel")
def portal_cancel(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    cid = user["company_id"]
    lv = get_or_404(db, HostelLeave, cid, uuid, "Leave request")
    _portal_check(db, user, lv.student_id)
    if user.get("role") != "parent":
        raise HTTPException(status_code=403, detail="Only a parent can cancel leave.")
    service.decide_leave(db, cid, lv, "cancel")
    db.commit()
    return _leave_out(lv)
