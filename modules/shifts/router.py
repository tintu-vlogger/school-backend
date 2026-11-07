"""Shifts API: shift master, patterns, work arrangements, night-shift consent and
shift change / swap requests."""
from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.db import get_db
from core.errors import BadRequest, NotFound
from core.security import require_permission
from modules.attendance import service as att
from modules.attendance.engine import merged_rule
from modules.shifts import requests as shift_requests
from modules.shifts import service
from modules.shifts.models import NightShiftConsent, Shift, ShiftPattern, WorkArrangement

router = APIRouter(prefix="/shifts", tags=["Shifts"])

VIEW = require_permission("time.roster.view")
SETTINGS = require_permission("time.attendance.settings")
ROSTER = require_permission("time.roster.manage")


class Break(BaseModel):
    name: str = Field("Break", max_length=40)
    start: str
    end: str
    paid: bool = False


class ShiftIn(BaseModel):
    name: str | None = Field(None, max_length=80)
    code: str | None = Field(None, max_length=12)
    color: str | None = Field(None, max_length=20)
    start_time: str | None = None
    end_time: str | None = None
    breaks: list[Break] | None = None
    grace_in_minutes: int | None = None
    grace_out_minutes: int | None = None
    half_day_hours: float | None = None
    full_day_hours: float | None = None
    is_flexible: bool | None = None
    core_start: str | None = None
    core_end: str | None = None
    overtime_after_minutes: int | None = None
    allowance_amount: float | None = None
    allowance_min_hours: float | None = None
    weekly_offs: list[int] | None = None
    is_default: bool | None = None
    active: bool | None = None
    description: str | None = None


def _rule(db, cid):
    rows = att.rule_rows(db, cid)
    r = next((x for x in rows if x.is_default), rows[0] if rows else None)
    return merged_rule(r.config if r else None)


@router.get("/")
def list_shifts(include_inactive: bool = True, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    q = db.query(Shift).filter(Shift.company_id == user["company_id"])
    if not include_inactive:
        q = q.filter(Shift.active.is_(True))
    rule = _rule(db, user["company_id"])
    return [service.shift_out(s, rule) for s in q.order_by(Shift.active.desc(), Shift.start_time).all()]


@router.post("/", status_code=201)
def create_shift(req: ShiftIn, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    data = req.model_dump(exclude_unset=True)
    if data.get("breaks") is not None:
        data["breaks"] = [b for b in data["breaks"]]
    s = service.save_shift(db, user["company_id"], data)
    db.commit()
    return service.shift_out(s, _rule(db, user["company_id"]))


@router.put("/{uuid}")
def update_shift(uuid: UUID, req: ShiftIn, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    s = service.get_shift(db, user["company_id"], uuid)
    data = req.model_dump(exclude_unset=True)
    if data.get("active") is False and s.is_default:
        raise BadRequest("Make another shift the default before switching this one off.")
    s = service.save_shift(db, user["company_id"], data, s)
    db.commit()
    return service.shift_out(s, _rule(db, user["company_id"]))


# ── Patterns ────────────────────────────────────────────────────────

class PatternIn(BaseModel):
    name: str = Field(..., max_length=100)
    kind: str
    slots: dict | list
    description: str | None = None
    active: bool = True


@router.get("/patterns")
def list_patterns(user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    rows = db.query(ShiftPattern).filter(ShiftPattern.company_id == user["company_id"]).order_by(ShiftPattern.name).all()
    return {"items": [service.pattern_out(p) for p in rows], "kinds": service.PATTERN_KINDS}


@router.post("/patterns", status_code=201)
def create_pattern(req: PatternIn, user: dict = Depends(ROSTER), db: Session = Depends(get_db)):
    p = service.save_pattern(db, user["company_id"], req.model_dump())
    db.commit()
    return service.pattern_out(p)


@router.put("/patterns/{uuid}")
def update_pattern(uuid: UUID, req: PatternIn, user: dict = Depends(ROSTER), db: Session = Depends(get_db)):
    p = db.query(ShiftPattern).filter(ShiftPattern.company_id == user["company_id"], ShiftPattern.uuid == uuid).first()
    if not p:
        raise NotFound("Pattern")
    service.save_pattern(db, user["company_id"], req.model_dump(), p)
    db.commit()
    return service.pattern_out(p)


# ── Work arrangements ───────────────────────────────────────────────

class ArrangementIn(BaseModel):
    effective_from: date
    shift_id: int | None = None
    working_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    alternate_days: dict = Field(default_factory=dict)
    punch_required: bool = True
    auto_shift: bool = False
    attendance_rule_id: int | None = None
    note: str | None = Field(None, max_length=300)


@router.get("/arrangements/{employee_uuid}")
def get_arrangements(employee_uuid: UUID, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    cid = user["company_id"]
    emp = att.employee_by_uuid(db, cid, employee_uuid)
    att.check_can_see(db, user, emp.id)
    rows = service.arrangements_for(db, cid, [emp.id]).get(emp.id, [])
    td = att.today(db, cid)
    current = service.arrangement_on(rows, td)
    return {"employee": {"uuid": str(emp.uuid), "name": att.name_of(emp)},
            "current": service.arrangement_out(current) if current else None,
            "history": [service.arrangement_out(r) for r in reversed(rows)]}


@router.put("/arrangements/{employee_uuid}")
def save_arrangement(employee_uuid: UUID, req: ArrangementIn, user: dict = Depends(require_permission("time.attendance.manage")),
                     db: Session = Depends(get_db)):
    cid = user["company_id"]
    emp = att.employee_by_uuid(db, cid, employee_uuid)
    row = service.save_arrangement(db, cid, emp.id, req.model_dump())
    db.flush()
    td = att.today(db, cid)
    if req.effective_from <= td:
        start = max(req.effective_from, td.replace(day=1))
        att.recompute(db, cid, [emp.id], start, td)
    db.commit()
    return service.arrangement_out(row)


@router.get("/arrangements")
def list_arrangements(user: dict = Depends(require_permission("time.attendance.manage")), db: Session = Depends(get_db),
                      q: str | None = None):
    """Everyone's current arrangement (for the settings screen)."""
    cid = user["company_id"]
    emps = att.employees_query(db, cid, q=q).all()
    rows = service.arrangements_for(db, cid, [e.id for e in emps])
    td = att.today(db, cid)
    out = []
    for e in emps:
        cur = service.arrangement_on(rows.get(e.id, []), td)
        out.append({"employee_id": e.id, "employee_uuid": str(e.uuid), "name": att.name_of(e),
                    "employee_code": e.employee_code, "arrangement": service.arrangement_out(cur) if cur else None})
    return out


# ── Night-shift consent ─────────────────────────────────────────────

class ConsentIn(BaseModel):
    employee_id: int
    consent_date: date
    valid_from: date
    valid_to: date | None = None
    safety_measures: str | None = Field(None, max_length=2000)
    document_url: str | None = None


@router.get("/consents")
def list_consents(user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    cid = user["company_id"]
    rows = db.query(NightShiftConsent).filter(NightShiftConsent.company_id == cid) \
        .order_by(NightShiftConsent.valid_from.desc()).all()
    names = att.names_for(db, cid, [c.employee_id for c in rows])
    return [service.consent_out(c, names.get(c.employee_id)) for c in rows]


@router.post("/consents", status_code=201)
def add_consent(req: ConsentIn, user: dict = Depends(ROSTER), db: Session = Depends(get_db)):
    cid = user["company_id"]
    emp = att.employee(db, cid, req.employee_id)
    c = service.record_consent(db, cid, emp.id, req.model_dump())
    db.commit()
    return service.consent_out(c, att.name_of(emp))


@router.post("/consents/{uuid}/withdraw")
def withdraw_consent(uuid: UUID, user: dict = Depends(ROSTER), db: Session = Depends(get_db)):
    from datetime import datetime, timezone
    c = db.query(NightShiftConsent).filter(NightShiftConsent.company_id == user["company_id"],
                                           NightShiftConsent.uuid == uuid).first()
    if not c:
        raise NotFound("Consent")
    c.withdrawn_at = datetime.now(timezone.utc)
    db.commit()
    return service.consent_out(c)


# ── Requests ────────────────────────────────────────────────────────

class ChangeIn(BaseModel):
    start_date: date
    end_date: date | None = None
    shift_id: int
    reason: str = Field(..., max_length=1000)


class SwapIn(BaseModel):
    date: date
    counterpart_employee_id: int
    reason: str | None = Field(None, max_length=1000)


def _me(db, user):
    if not user.get("employee_id"):
        raise BadRequest("Only employees can ask for shift changes.")
    return att.employee(db, user["company_id"], user["employee_id"])


@router.post("/requests/change", status_code=201)
def request_change(req: ChangeIn, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    inst = shift_requests.create_change(db, user["company_id"], _me(db, user), req.model_dump())
    db.commit()
    return {"uuid": str(inst.uuid), "reference_no": inst.reference_no, "status": inst.status}


@router.post("/requests/swap", status_code=201)
def request_swap(req: SwapIn, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    inst = shift_requests.create_swap(db, user["company_id"], _me(db, user), req.model_dump())
    db.commit()
    return {"uuid": str(inst.uuid), "reference_no": inst.reference_no, "status": inst.status}


@router.get("/colleagues")
def swap_colleagues(on: date, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    """Colleagues in the same department with a different shift that day (for swaps)."""
    cid = user["company_id"]
    me = _me(db, user)
    q = att.employees_query(db, cid, department_id=me.department_id).all() if me.department_id else \
        att.employees_query(db, cid).limit(200).all()
    ids = [e.id for e in q if e.id != me.id]
    plans = service.day_plans(db, cid, ids + [me.id], on, on)
    mine = plans[(me.id, on)].shift
    out = []
    for e in q:
        if e.id == me.id:
            continue
        p = plans[(e.id, on)]
        if p.day_type == "working" and p.shift and (not mine or p.shift.id != mine.id):
            out.append({"value": e.id, "label": att.name_of(e), "hint": p.shift.name})
    return {"my_shift": mine.name if mine and plans[(me.id, on)].day_type == "working" else None, "options": out}
