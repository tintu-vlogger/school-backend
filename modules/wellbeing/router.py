"""Wellbeing API: health records and check-ups, infirmary, discipline and merits with house
totals, counselling notes (restricted), the gate visitor log and early pickup gate passes,
and the parent portal (/wellbeing/portal/...)."""
import datetime as dt
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from core import audit
from core.crud import by_id_or_400, crud_router, get_or_404
from core.db import get_db
from core.pagination import PageParams, paginate
from core.security import _get_role_permissions, get_current_user, require_permission
from modules.wellbeing import service
from modules.wellbeing.models import (WbCheckup, WbCheckupCamp, WbCounsellingNote, WbGatePass, WbHealthRecord,
                                      WbIncident, WbIncidentType, WbInfirmaryVisit, WbPoint, WbVisitor)

router = APIRouter(prefix="/wellbeing", tags=["Wellbeing"])
VIEW, MANAGE, RECORD, COUNSEL = "wellbeing.view", "wellbeing.manage", "wellbeing.record", "wellbeing.counselling"


def _cid(user: dict) -> int:
    return user["company_id"]


def _can(db: Session, user: dict, perm: str) -> bool:
    return user.get("role") == "admin" or perm in _get_role_permissions(user["company_id"], user["role"], db,
                                                                         user.get("employee_id"))


def _num(v) -> float | None:
    return None if v is None else float(v)


def _student_bits(db: Session, s) -> dict:
    from modules.students import service as students
    e = students.current_enrollment(db, s.company_id, s.id)
    return {"student_id": s.id, "student_uuid": str(s.uuid), "student_name": s.full_name, "admission_no": s.admission_no,
            "photo_url": s.photo_url, "section_label": students.academics.section_label(e.section) if e and e.section else None}


def _student_or_400(db: Session, cid: int, student_id: int):
    from modules.students.models import Student
    return by_id_or_400(db, Student, cid, student_id, "Student")


def _day_bounds(db: Session, cid: int, day: date):
    from core.timeutil import _load_company, tz_for
    tz = tz_for(_load_company(db, cid))
    start = datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(timezone.utc)
    return start, start + timedelta(days=1)


# ── Incident types (configurable list) ───────────────────────────────

class TypeIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    default_severity: str = Field("low", pattern="^(low|medium|high|critical)$")
    default_points: int = Field(0, ge=-100, le=0)
    active: bool = True


class TypePatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    default_severity: str | None = Field(None, pattern="^(low|medium|high|critical)$")
    default_points: int | None = Field(None, ge=-100, le=0)
    active: bool | None = None


router.include_router(crud_router(
    model=WbIncidentType, prefix="/incident-types", tag="Wellbeing", create_schema=TypeIn, update_schema=TypePatch,
    view_perm=VIEW, manage_perm=MANAGE, label="Incident type",
    out=lambda t: {"id": t.id, "uuid": str(t.uuid), "name": t.name, "default_severity": t.default_severity,
                   "default_points": t.default_points, "active": t.active}))


# ── Health records ───────────────────────────────────────────────────

def _health_out(db: Session, s, r: WbHealthRecord | None, full: bool = False) -> dict:
    value = service.bmi(r.height_cm, r.weight_kg) if r else None
    out = {**_student_bits(db, s), "uuid": str(r.uuid) if r else None, "blood_group": s.blood_group,
           "allergies": s.allergies, "medical_notes": s.medical_notes,
           "height_cm": _num(r.height_cm) if r else None, "weight_kg": _num(r.weight_kg) if r else None,
           "bmi": _num(value), "bmi_band": service.bmi_band(value), "measured_on": r.measured_on if r else None,
           "chronic_conditions": r.chronic_conditions if r else None}
    if full:
        out.update({k: (getattr(r, k) if r else None) for k in (
            "vision_left", "vision_right", "dental", "hearing", "doctor_name", "doctor_phone", "insurance", "notes")})
        out["immunisations"] = (r.immunisations if r else None) or []
    return out


@router.get("/health")
def list_health(q: str | None = None, section_id: int | None = None, flag: str | None = None,
                params: PageParams = Depends(), db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    """Learners with their health summary (`flag`: allergies, conditions, missing)."""
    from modules.students.models import Enrollment, Student
    cid = _cid(user)
    query = (db.query(Student, WbHealthRecord).outerjoin(
        WbHealthRecord, (WbHealthRecord.student_id == Student.id) & (WbHealthRecord.company_id == cid))
        .filter(Student.company_id == cid, Student.status == "active"))
    if section_id:
        query = query.join(Enrollment, Enrollment.student_id == Student.id).filter(Enrollment.section_id == section_id,
                                                                                   Enrollment.status == "enrolled")
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Student.first_name.ilike(like), Student.last_name.ilike(like), Student.admission_no.ilike(like)))
    if flag == "allergies":
        query = query.filter(Student.allergies.isnot(None), Student.allergies != "")
    elif flag == "conditions":
        query = query.filter(or_(WbHealthRecord.chronic_conditions.isnot(None), Student.medical_notes.isnot(None)))
    elif flag == "missing":
        query = query.filter(or_(WbHealthRecord.id.is_(None), WbHealthRecord.height_cm.is_(None)))
    query = query.order_by(Student.first_name, Student.last_name)
    return paginate(query, params, lambda row: _health_out(db, row[0], row[1]))


@router.get("/health/{student_id}")
def get_health(student_id: int, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    cid = _cid(user)
    s = _student_or_400(db, cid, student_id)
    r = service.health_record(db, cid, student_id)
    out = _health_out(db, s, r, full=True)
    out["checkups"] = [_checkup_out(db, c, brief=True) for c in db.query(WbCheckup).options(joinedload(WbCheckup.camp)).filter(
        WbCheckup.company_id == cid, WbCheckup.student_id == student_id).order_by(WbCheckup.checked_on.desc()).limit(20)]
    out["visits"] = [_visit_out(db, v, brief=True) for v in db.query(WbInfirmaryVisit).filter(
        WbInfirmaryVisit.company_id == cid, WbInfirmaryVisit.student_id == student_id)
        .order_by(WbInfirmaryVisit.visit_at.desc()).limit(20)]
    from modules.students.models import Guardian, StudentGuardian
    out["contacts"] = [{"name": g.full_name, "relation": l.relation, "phone": g.phone} for l, g in db.query(
        StudentGuardian, Guardian).join(Guardian, Guardian.id == StudentGuardian.guardian_id).filter(
        StudentGuardian.company_id == cid, StudentGuardian.student_id == student_id).all()]
    return out


class Immunisation(BaseModel):
    vaccine: str = Field(..., min_length=1, max_length=80)
    dose: str | None = Field(None, max_length=40)
    date: dt.date | None = None
    notes: str | None = Field(None, max_length=200)


class HealthIn(BaseModel):
    height_cm: Decimal | None = Field(None, ge=30, le=250, max_digits=5, decimal_places=1)
    weight_kg: Decimal | None = Field(None, ge=2, le=300, max_digits=5, decimal_places=1)
    measured_on: date | None = None
    vision_left: str | None = Field(None, max_length=20)
    vision_right: str | None = Field(None, max_length=20)
    dental: str | None = Field(None, max_length=200)
    hearing: str | None = Field(None, max_length=60)
    chronic_conditions: str | None = None
    immunisations: list[Immunisation] | None = None
    doctor_name: str | None = Field(None, max_length=200)
    doctor_phone: str | None = Field(None, max_length=20)
    insurance: str | None = Field(None, max_length=200)
    notes: str | None = None


@router.put("/health/{student_id}")
def save_health(student_id: int, payload: HealthIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    _student_or_400(db, cid, student_id)
    r = service.health_record(db, cid, student_id, create=True)
    data = payload.model_dump(exclude_unset=True, mode="json")
    for k, v in data.items():
        if k in ("height_cm", "weight_kg") and v is not None:
            v = Decimal(str(v))
        if k == "measured_on" and v:
            v = date.fromisoformat(v)
        setattr(r, k, v)
    if ("height_cm" in data or "weight_kg" in data) and not data.get("measured_on"):
        r.measured_on = service.today(db, cid)
    db.commit()
    return get_health(student_id, db, user)


# ── Check-up camps ───────────────────────────────────────────────────

class CampIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=160)
    kind: str = Field("general", pattern="^(general|dental|eye|other)$")
    held_on: date
    doctor: str | None = Field(None, max_length=200)
    agency: str | None = Field(None, max_length=200)
    notes: str | None = None
    active: bool = True


class CampPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=160)
    kind: str | None = Field(None, pattern="^(general|dental|eye|other)$")
    held_on: date | None = None
    doctor: str | None = None
    agency: str | None = None
    notes: str | None = None
    active: bool | None = None


def _camp_out(c: WbCheckupCamp) -> dict:
    from sqlalchemy.orm import object_session
    db = object_session(c)
    n = db.query(func.count(WbCheckup.id)).filter(WbCheckup.camp_id == c.id).scalar() if db else 0
    return {"id": c.id, "uuid": str(c.uuid), "name": c.name, "kind": c.kind, "held_on": c.held_on, "doctor": c.doctor,
            "agency": c.agency, "notes": c.notes, "active": c.active, "checked": n or 0}


router.include_router(crud_router(
    model=WbCheckupCamp, prefix="/camps", tag="Wellbeing", create_schema=CampIn, update_schema=CampPatch,
    view_perm=VIEW, manage_perm=MANAGE, label="Check-up camp", order_by=("held_on",), out=_camp_out))


def _checkup_out(db: Session, c: WbCheckup, brief: bool = False) -> dict:
    value = service.bmi(c.height_cm, c.weight_kg)
    out = {"id": c.id, "uuid": str(c.uuid), "checked_on": c.checked_on, "camp": c.camp.name if c.camp else None,
           "height_cm": _num(c.height_cm), "weight_kg": _num(c.weight_kg), "bmi": _num(value),
           "vision_left": c.vision_left, "vision_right": c.vision_right, "dental": c.dental,
           "blood_pressure": c.blood_pressure, "pulse": c.pulse, "findings": c.findings,
           "recommendations": c.recommendations, "examined_by": c.examined_by}
    if not brief:
        out.update(_student_bits(db, c.student))
    return out


@router.get("/checkups")
def list_checkups(camp_uuid: UUID | None = None, student_id: int | None = None, params: PageParams = Depends(),
                  db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    cid = _cid(user)
    q = db.query(WbCheckup).options(joinedload(WbCheckup.student), joinedload(WbCheckup.camp)).filter(WbCheckup.company_id == cid)
    if camp_uuid:
        q = q.join(WbCheckupCamp, WbCheckupCamp.id == WbCheckup.camp_id).filter(WbCheckupCamp.uuid == camp_uuid)
    if student_id:
        q = q.filter(WbCheckup.student_id == student_id)
    return paginate(q.order_by(WbCheckup.checked_on.desc(), WbCheckup.id.desc()), params, lambda c: _checkup_out(db, c))


class CheckupIn(BaseModel):
    student_id: int
    camp_uuid: UUID | None = None
    checked_on: date | None = None
    height_cm: Decimal | None = Field(None, ge=30, le=250, max_digits=5, decimal_places=1)
    weight_kg: Decimal | None = Field(None, ge=2, le=300, max_digits=5, decimal_places=1)
    vision_left: str | None = Field(None, max_length=20)
    vision_right: str | None = Field(None, max_length=20)
    dental: str | None = Field(None, max_length=200)
    blood_pressure: str | None = Field(None, max_length=20)
    pulse: int | None = Field(None, ge=20, le=250)
    findings: str | None = None
    recommendations: str | None = None
    examined_by: str | None = Field(None, max_length=200)
    update_record: bool = True


@router.post("/checkups", status_code=201)
def add_checkup(payload: CheckupIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    _student_or_400(db, cid, payload.student_id)
    camp = get_or_404(db, WbCheckupCamp, cid, payload.camp_uuid, "Camp") if payload.camp_uuid else None
    data = payload.model_dump(exclude={"camp_uuid", "update_record"})
    data["checked_on"] = data["checked_on"] or (camp.held_on if camp else service.today(db, cid))
    c = WbCheckup(company_id=cid, camp_id=camp.id if camp else None, **data)
    db.add(c)
    if payload.update_record:
        r = service.health_record(db, cid, payload.student_id, create=True)
        if r.measured_on is None or r.measured_on <= c.checked_on:
            for k in ("height_cm", "weight_kg", "vision_left", "vision_right", "dental"):
                if getattr(c, k) not in (None, ""):
                    setattr(r, k, getattr(c, k))
            r.measured_on = c.checked_on
    db.commit()
    db.refresh(c)
    return _checkup_out(db, c)


# ── Infirmary ────────────────────────────────────────────────────────

def _visit_out(db: Session, v: WbInfirmaryVisit, brief: bool = False) -> dict:
    out = {"id": v.id, "uuid": str(v.uuid), "visit_at": v.visit_at, "complaint": v.complaint,
           "temperature": _num(v.temperature), "treatment": v.treatment, "medicine_given": v.medicine_given,
           "sent_home": v.sent_home, "parent_informed": v.parent_informed, "follow_up": v.follow_up,
           "attended_by": v.attended_by, "left_at": v.left_at}
    if not brief:
        out.update(_student_bits(db, v.student))
        out["allergies"] = v.student.allergies
    return out


@router.get("/infirmary")
def list_visits(q: str | None = None, day: date | None = None, sent_home: bool | None = None, params: PageParams = Depends(),
                db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    from modules.students.models import Student
    cid = _cid(user)
    query = db.query(WbInfirmaryVisit).join(Student, Student.id == WbInfirmaryVisit.student_id).options(
        joinedload(WbInfirmaryVisit.student)).filter(WbInfirmaryVisit.company_id == cid)
    if day:
        a, b = _day_bounds(db, cid, day)
        query = query.filter(WbInfirmaryVisit.visit_at >= a, WbInfirmaryVisit.visit_at < b)
    if sent_home is not None:
        query = query.filter(WbInfirmaryVisit.sent_home.is_(sent_home))
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Student.first_name.ilike(like), Student.last_name.ilike(like),
                                 WbInfirmaryVisit.complaint.ilike(like)))
    return paginate(query.order_by(WbInfirmaryVisit.visit_at.desc()), params, lambda v: _visit_out(db, v))


class VisitIn(BaseModel):
    student_id: int
    visit_at: datetime | None = None
    complaint: str = Field(..., min_length=1, max_length=300)
    temperature: Decimal | None = Field(None, ge=30, le=45, max_digits=4, decimal_places=1)
    treatment: str | None = None
    medicine_given: str | None = Field(None, max_length=300)
    sent_home: bool = False
    parent_informed: bool = False
    follow_up: str | None = None


class VisitPatch(BaseModel):
    complaint: str | None = Field(None, min_length=1, max_length=300)
    temperature: Decimal | None = Field(None, ge=30, le=45, max_digits=4, decimal_places=1)
    treatment: str | None = None
    medicine_given: str | None = None
    sent_home: bool | None = None
    parent_informed: bool | None = None
    follow_up: str | None = None
    left_at: datetime | None = None


def _visit_notice(db: Session, cid: int, v: WbInfirmaryVisit) -> None:
    s = v.student
    bits = [v.complaint]
    if v.medicine_given:
        bits.append(f"given {v.medicine_given}")
    if v.sent_home:
        bits.append("sent home; please collect")
    service.tell_parents(db, cid, s.id, "wellbeing.infirmary", f"{s.first_name} visited the infirmary",
                         ". ".join(bits).capitalize() + ".", "/dashboard/my/wellbeing")


@router.post("/infirmary", status_code=201)
def add_visit(payload: VisitIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE, RECORD))):
    cid = _cid(user)
    _student_or_400(db, cid, payload.student_id)
    data = payload.model_dump()
    data["visit_at"] = data["visit_at"] or datetime.now(timezone.utc)
    v = WbInfirmaryVisit(company_id=cid, attended_by=service.actor(), **data)
    db.add(v)
    db.flush()
    db.refresh(v)
    if v.parent_informed or v.sent_home:
        v.parent_informed = True
        _visit_notice(db, cid, v)
    db.commit()
    return _visit_out(db, v)


@router.put("/infirmary/{uuid}")
def update_visit(uuid: UUID, payload: VisitPatch, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE, RECORD))):
    cid = _cid(user)
    v = get_or_404(db, WbInfirmaryVisit, cid, uuid, "Visit")
    was = v.parent_informed
    for k, val in payload.model_dump(exclude_unset=True).items():
        setattr(v, k, val)
    if v.sent_home:
        v.parent_informed = True
    if v.parent_informed and not was:
        _visit_notice(db, cid, v)
    db.commit()
    return _visit_out(db, v)


# ── Discipline: incidents ────────────────────────────────────────────

def _incident_out(db: Session, i: WbIncident) -> dict:
    pts = db.query(func.coalesce(func.sum(WbPoint.points), 0)).filter(WbPoint.incident_id == i.id, WbPoint.voided.is_(False)).scalar()
    return {"id": i.id, "uuid": str(i.uuid), **_student_bits(db, i.student), "type_id": i.type_id,
            "type_name": i.type.name if i.type else None, "occurred_at": i.occurred_at, "location": i.location,
            "severity": i.severity, "description": i.description, "action_taken": i.action_taken,
            "follow_up_on": i.follow_up_on, "follow_up_notes": i.follow_up_notes, "status": i.status,
            "parent_informed": i.parent_informed, "confidential": i.confidential, "reported_by": i.reported_by,
            "points": int(pts or 0), "created_at": i.created_at}


@router.get("/incidents")
def list_incidents(q: str | None = None, status: str | None = None, severity: str | None = None, student_id: int | None = None,
                   params: PageParams = Depends(), db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    from modules.students.models import Student
    cid = _cid(user)
    query = db.query(WbIncident).join(Student, Student.id == WbIncident.student_id).options(
        joinedload(WbIncident.student), joinedload(WbIncident.type)).filter(WbIncident.company_id == cid)
    if not _can(db, user, MANAGE):
        query = query.filter(or_(WbIncident.confidential.is_(False), WbIncident.reported_by_id == user.get("employee_id")))
    if status:
        query = query.filter(WbIncident.status == status)
    if severity:
        query = query.filter(WbIncident.severity == severity)
    if student_id:
        query = query.filter(WbIncident.student_id == student_id)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Student.first_name.ilike(like), Student.last_name.ilike(like), WbIncident.description.ilike(like)))
    return paginate(query.order_by(WbIncident.occurred_at.desc()), params, lambda i: _incident_out(db, i))


class IncidentIn(BaseModel):
    student_ids: list[int] = Field(..., min_length=1, max_length=40)
    type_id: int | None = None
    occurred_at: datetime | None = None
    location: str | None = Field(None, max_length=120)
    severity: str = Field("low", pattern="^(low|medium|high|critical)$")
    description: str = Field(..., min_length=2)
    action_taken: str | None = None
    follow_up_on: date | None = None
    parent_informed: bool = False
    confidential: bool = False
    points: int | None = Field(None, ge=-100, le=0)


class IncidentPatch(BaseModel):
    type_id: int | None = None
    location: str | None = None
    severity: str | None = Field(None, pattern="^(low|medium|high|critical)$")
    description: str | None = Field(None, min_length=2)
    action_taken: str | None = None
    follow_up_on: date | None = None
    follow_up_notes: str | None = None
    status: str | None = Field(None, pattern="^(open|closed)$")
    parent_informed: bool | None = None
    confidential: bool | None = None


@router.post("/incidents", status_code=201)
def add_incident(payload: IncidentIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE, RECORD))):
    cid = _cid(user)
    t = by_id_or_400(db, WbIncidentType, cid, payload.type_id, "Incident type")
    pts = payload.points if payload.points is not None else (t.default_points if t else 0)
    when = payload.occurred_at or datetime.now(timezone.utc)
    out = []
    for sid in dict.fromkeys(payload.student_ids):
        s = _student_or_400(db, cid, sid)
        i = WbIncident(company_id=cid, student_id=sid, type_id=t.id if t else None, occurred_at=when,
                       location=payload.location, severity=payload.severity, description=payload.description,
                       action_taken=payload.action_taken, follow_up_on=payload.follow_up_on,
                       parent_informed=payload.parent_informed, confidential=payload.confidential,
                       reported_by=service.actor(), reported_by_id=user.get("employee_id"))
        db.add(i)
        db.flush()
        if pts:
            db.add(WbPoint(company_id=cid, student_id=sid, points=pts, reason=t.name if t else "Incident",
                           category="Conduct", awarded_on=service.today(db, cid), awarded_by=service.actor(), incident_id=i.id))
        if payload.parent_informed and not payload.confidential:
            service.tell_parents(db, cid, sid, "wellbeing.incident", f"A note about {s.first_name}",
                                 (t.name if t else "Behaviour incident") + (f": {payload.action_taken}" if payload.action_taken else ""),
                                 "/dashboard/my/wellbeing")
        db.flush()
        db.refresh(i)
        out.append(i)
    db.commit()
    return [_incident_out(db, i) for i in out]


@router.put("/incidents/{uuid}")
def update_incident(uuid: UUID, payload: IncidentPatch, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    i = get_or_404(db, WbIncident, cid, uuid, "Incident")
    data = payload.model_dump(exclude_unset=True)
    if "type_id" in data:
        by_id_or_400(db, WbIncidentType, cid, data["type_id"], "Incident type")
    for k, v in data.items():
        setattr(i, k, v)
    db.commit()
    return _incident_out(db, i)


# ── Merit and demerit points ─────────────────────────────────────────

def _point_out(db: Session, p: WbPoint) -> dict:
    s = p.student
    return {"id": p.id, "uuid": str(p.uuid), **_student_bits(db, s), "house_id": s.house_id,
            "house_name": s.house.name if s.house else None, "points": p.points, "reason": p.reason, "category": p.category,
            "awarded_on": p.awarded_on, "awarded_by": p.awarded_by, "incident_uuid": None, "voided": p.voided}


@router.get("/points")
def list_points(q: str | None = None, kind: str | None = None, house_id: int | None = None, student_id: int | None = None,
                params: PageParams = Depends(), db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    from modules.students.models import Student
    cid = _cid(user)
    query = db.query(WbPoint).join(Student, Student.id == WbPoint.student_id).options(
        joinedload(WbPoint.student).joinedload(Student.house)).filter(WbPoint.company_id == cid, WbPoint.voided.is_(False))
    if kind == "merit":
        query = query.filter(WbPoint.points > 0)
    elif kind == "demerit":
        query = query.filter(WbPoint.points < 0)
    if house_id:
        query = query.filter(Student.house_id == house_id)
    if student_id:
        query = query.filter(WbPoint.student_id == student_id)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Student.first_name.ilike(like), Student.last_name.ilike(like), WbPoint.reason.ilike(like)))
    return paginate(query.order_by(WbPoint.awarded_on.desc(), WbPoint.id.desc()), params, lambda p: _point_out(db, p))


class PointIn(BaseModel):
    student_ids: list[int] = Field(..., min_length=1, max_length=60)
    points: int = Field(..., ge=-50, le=50)
    reason: str = Field(..., min_length=2, max_length=300)
    category: str | None = Field(None, max_length=60)
    awarded_on: date | None = None


@router.post("/points", status_code=201)
def add_points(payload: PointIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE, RECORD))):
    cid = _cid(user)
    if payload.points == 0:
        raise HTTPException(status_code=400, detail="Give some points (plus for merit, minus for demerit).")
    out = []
    for sid in dict.fromkeys(payload.student_ids):
        _student_or_400(db, cid, sid)
        p = WbPoint(company_id=cid, student_id=sid, points=payload.points, reason=payload.reason, category=payload.category,
                    awarded_on=payload.awarded_on or service.today(db, cid), awarded_by=service.actor())
        db.add(p)
        out.append(p)
    db.commit()
    return [_point_out(db, p) for p in out]


@router.post("/points/{uuid}/void")
def void_point(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    p = get_or_404(db, WbPoint, _cid(user), uuid, "Points")
    p.voided = True
    db.commit()
    return {"detail": "Points cancelled"}


@router.get("/houses")
def houses(since: date | None = None, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    return service.house_totals(db, _cid(user), since)


@router.get("/points/leaders")
def leaders(limit: int = Query(10, ge=1, le=50), db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    from modules.students.models import Student
    cid = _cid(user)
    rows = (db.query(Student, func.sum(WbPoint.points).label("t")).join(WbPoint, WbPoint.student_id == Student.id)
            .filter(WbPoint.company_id == cid, WbPoint.voided.is_(False)).group_by(Student.id)
            .order_by(func.sum(WbPoint.points).desc()).limit(limit).all())
    return [{**_student_bits(db, s), "points": int(t)} for s, t in rows]


# ── Counselling (restricted) ─────────────────────────────────────────

def _note_out(db: Session, n: WbCounsellingNote) -> dict:
    return {"id": n.id, "uuid": str(n.uuid), **_student_bits(db, n.student), "session_on": n.session_on,
            "counsellor": n.counsellor, "concern": n.concern, "notes": n.notes, "plan": n.plan,
            "next_session_on": n.next_session_on, "created_at": n.created_at}


@router.get("/counselling")
def list_notes(student_id: int | None = None, q: str | None = None, params: PageParams = Depends(),
               db: Session = Depends(get_db), user: dict = Depends(require_permission(COUNSEL))):
    from modules.students.models import Student
    cid = _cid(user)
    query = db.query(WbCounsellingNote).join(Student, Student.id == WbCounsellingNote.student_id).options(
        joinedload(WbCounsellingNote.student)).filter(WbCounsellingNote.company_id == cid, WbCounsellingNote.archived_at.is_(None))
    if student_id:
        query = query.filter(WbCounsellingNote.student_id == student_id)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Student.first_name.ilike(like), Student.last_name.ilike(like), WbCounsellingNote.concern.ilike(like)))
    audit.event(db, "wellbeing.counselling_viewed", "wb_counselling_notes", None, {"student_id": student_id})
    db.commit()
    return paginate(query.order_by(WbCounsellingNote.session_on.desc()), params, lambda n: _note_out(db, n))


class NoteIn(BaseModel):
    student_id: int
    session_on: date | None = None
    concern: str | None = Field(None, max_length=200)
    notes: str = Field(..., min_length=2)
    plan: str | None = None
    next_session_on: date | None = None


class NotePatch(BaseModel):
    concern: str | None = None
    notes: str | None = Field(None, min_length=2)
    plan: str | None = None
    next_session_on: date | None = None


@router.post("/counselling", status_code=201)
def add_note(payload: NoteIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(COUNSEL))):
    cid = _cid(user)
    _student_or_400(db, cid, payload.student_id)
    data = payload.model_dump()
    data["session_on"] = data["session_on"] or service.today(db, cid)
    n = WbCounsellingNote(company_id=cid, counsellor=service.actor(), counsellor_id=user.get("employee_id"), **data)
    db.add(n)
    db.commit()
    db.refresh(n)
    return _note_out(db, n)


@router.put("/counselling/{uuid}")
def update_note(uuid: UUID, payload: NotePatch, db: Session = Depends(get_db), user: dict = Depends(require_permission(COUNSEL))):
    n = get_or_404(db, WbCounsellingNote, _cid(user), uuid, "Note")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(n, k, v)
    db.commit()
    return _note_out(db, n)


@router.delete("/counselling/{uuid}")
def archive_note(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(COUNSEL))):
    n = get_or_404(db, WbCounsellingNote, _cid(user), uuid, "Note")
    n.archived_at = datetime.now(timezone.utc)
    db.commit()
    return {"detail": "Archived"}


# ── Gate: visitors ───────────────────────────────────────────────────

def _visitor_out(v: WbVisitor) -> dict:
    return {"id": v.id, "uuid": str(v.uuid), "pass_no": v.pass_no, "name": v.name, "phone": v.phone,
            "company_name": v.company_name, "purpose": v.purpose, "person_to_meet": v.person_to_meet,
            "employee_id": v.employee_id, "employee_name": f"{v.employee.first_name} {v.employee.last_name}".strip() if v.employee else None,
            "student_id": v.student_id, "student_name": v.student.full_name if v.student else None,
            "id_kind": v.id_kind, "id_number": v.id_number, "photo_url": v.photo_url, "vehicle_no": v.vehicle_no,
            "people_count": v.people_count, "in_at": v.in_at, "out_at": v.out_at, "logged_by": v.logged_by, "notes": v.notes}


@router.get("/visitors")
def list_visitors(day: date | None = None, inside: bool = False, q: str | None = None, params: PageParams = Depends(),
                  db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    cid = _cid(user)
    query = db.query(WbVisitor).options(joinedload(WbVisitor.employee), joinedload(WbVisitor.student)).filter(WbVisitor.company_id == cid)
    if day:
        a, b = _day_bounds(db, cid, day)
        query = query.filter(WbVisitor.in_at >= a, WbVisitor.in_at < b)
    if inside:
        query = query.filter(WbVisitor.out_at.is_(None))
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(WbVisitor.name.ilike(like), WbVisitor.phone.ilike(like), WbVisitor.purpose.ilike(like),
                                 WbVisitor.pass_no.ilike(like)))
    return paginate(query.order_by(WbVisitor.in_at.desc()), params, _visitor_out)


class VisitorIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    phone: str | None = Field(None, max_length=20)
    company_name: str | None = Field(None, max_length=200)
    purpose: str = Field(..., min_length=1, max_length=200)
    person_to_meet: str | None = Field(None, max_length=200)
    employee_id: int | None = None
    student_id: int | None = None
    id_kind: str | None = Field(None, max_length=40)
    id_number: str | None = Field(None, max_length=60)
    photo_url: str | None = Field(None, max_length=400_000)
    vehicle_no: str | None = Field(None, max_length=20)
    people_count: int = Field(1, ge=1, le=50)
    notes: str | None = None


@router.post("/visitors", status_code=201)
def log_visitor(payload: VisitorIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE, RECORD))):
    from modules.employees.models import Employee
    cid = _cid(user)
    data = payload.model_dump()
    e = by_id_or_400(db, Employee, cid, data.get("employee_id"), "Staff member")
    if data.get("student_id"):
        _student_or_400(db, cid, data["student_id"])
    if e and not data.get("person_to_meet"):
        data["person_to_meet"] = f"{e.first_name} {e.last_name}".strip()
    if data.get("photo_url") and not str(data["photo_url"]).startswith(("data:image/", "http")):
        raise HTTPException(status_code=400, detail="The photo is not an image.")
    v = WbVisitor(company_id=cid, pass_no=service.new_pass_no(db, cid, "visitor_pass", "VP"), logged_by=service.actor(), **data)
    db.add(v)
    db.flush()
    if e:
        try:
            from modules.notifications.service import notify
            notify(db, cid, [e.id], kind="wellbeing.visitor", title=f"{v.name} is here to see you",
                   body=v.purpose, link="/dashboard/wellbeing/visitors")
        except Exception:
            pass
    db.commit()
    db.refresh(v)
    return _visitor_out(v)


@router.post("/visitors/{uuid}/out")
def visitor_out(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE, RECORD))):
    v = get_or_404(db, WbVisitor, _cid(user), uuid, "Visitor")
    if v.out_at is None:
        v.out_at = datetime.now(timezone.utc)
    db.commit()
    return _visitor_out(v)


# ── Gate: early pickup passes ────────────────────────────────────────

@router.get("/pickups/{student_id}")
def pickup_people(student_id: int, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    """Guardians of a learner with whether each may pick them up (the gate checks this list)."""
    from modules.students.models import StudentGuardian
    cid = _cid(user)
    s = _student_or_400(db, cid, student_id)
    links = db.query(StudentGuardian).options(joinedload(StudentGuardian.guardian)).filter(
        StudentGuardian.company_id == cid, StudentGuardian.student_id == student_id).all()
    return {**_student_bits(db, s), "guardians": [
        {"guardian_id": l.guardian_id, "name": l.guardian.full_name, "relation": l.relation, "phone": l.guardian.phone,
         "photo_url": l.guardian.photo_url, "can_pickup": l.can_pickup} for l in links]}


def _pass_out(db: Session, p: WbGatePass) -> dict:
    return {"id": p.id, "uuid": str(p.uuid), "pass_no": p.pass_no, **_student_bits(db, p.student),
            "guardian_id": p.guardian_id, "guardian_name": p.guardian.full_name, "guardian_phone": p.guardian.phone,
            "reason": p.reason, "out_at": p.out_at, "returning": p.returning, "returned_at": p.returned_at,
            "issued_by": p.issued_by, "status": p.status, "notes": p.notes}


@router.get("/gate-passes")
def list_passes(day: date | None = None, q: str | None = None, params: PageParams = Depends(), db: Session = Depends(get_db),
                user: dict = Depends(require_permission(VIEW))):
    from modules.students.models import Student
    cid = _cid(user)
    query = db.query(WbGatePass).join(Student, Student.id == WbGatePass.student_id).options(
        joinedload(WbGatePass.student), joinedload(WbGatePass.guardian)).filter(WbGatePass.company_id == cid)
    if day:
        a, b = _day_bounds(db, cid, day)
        query = query.filter(WbGatePass.out_at >= a, WbGatePass.out_at < b)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Student.first_name.ilike(like), Student.last_name.ilike(like), WbGatePass.pass_no.ilike(like)))
    return paginate(query.order_by(WbGatePass.out_at.desc()), params, lambda p: _pass_out(db, p))


class PassIn(BaseModel):
    student_id: int
    guardian_id: int
    reason: str = Field(..., min_length=2, max_length=300)
    returning: bool = False
    notes: str | None = None


@router.post("/gate-passes", status_code=201)
def issue_pass(payload: PassIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE, RECORD))):
    cid = _cid(user)
    s, link = service.check_pickup(db, cid, payload.student_id, payload.guardian_id)
    p = WbGatePass(company_id=cid, student_id=s.id, guardian_id=link.guardian_id, reason=payload.reason,
                   returning=payload.returning, notes=payload.notes, issued_by=service.actor(),
                   pass_no=service.new_pass_no(db, cid, "gate_pass", "GP"))
    db.add(p)
    db.flush()
    db.refresh(p)
    service.tell_parents(db, cid, s.id, "wellbeing.gate_pass", f"{s.first_name} left school early",
                         f"Picked up by {link.guardian.full_name} ({link.relation}) at {datetime.now(timezone.utc):%H:%M} UTC. Reason: {payload.reason}",
                         "/dashboard/my/wellbeing")
    audit.event(db, "wellbeing.gate_pass_issued", "wb_gate_passes", p.id,
                {"guardian": link.guardian.full_name, "reason": payload.reason}, entity_label=s.full_name)
    db.commit()
    return _pass_out(db, p)


@router.post("/gate-passes/{uuid}/returned")
def pass_returned(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE, RECORD))):
    p = get_or_404(db, WbGatePass, _cid(user), uuid, "Gate pass")
    if p.status != "issued":
        raise HTTPException(status_code=400, detail="This pass is already closed.")
    p.status, p.returned_at = "returned", datetime.now(timezone.utc)
    db.commit()
    return _pass_out(db, p)


# ── Summary ──────────────────────────────────────────────────────────

@router.get("/summary")
def summary(db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    cid = _cid(user)
    on = service.today(db, cid)
    a, b = _day_bounds(db, cid, on)
    visits = db.query(func.count(WbInfirmaryVisit.id)).filter(WbInfirmaryVisit.company_id == cid,
                                                              WbInfirmaryVisit.visit_at >= a, WbInfirmaryVisit.visit_at < b).scalar()
    sent_home = db.query(func.count(WbInfirmaryVisit.id)).filter(WbInfirmaryVisit.company_id == cid, WbInfirmaryVisit.sent_home.is_(True),
                                                                 WbInfirmaryVisit.visit_at >= a, WbInfirmaryVisit.visit_at < b).scalar()
    inc_q = db.query(func.count(WbIncident.id)).filter(WbIncident.company_id == cid, WbIncident.status == "open")
    if not _can(db, user, MANAGE):
        inc_q = inc_q.filter(WbIncident.confidential.is_(False))
    follow = db.query(func.count(WbIncident.id)).filter(WbIncident.company_id == cid, WbIncident.status == "open",
                                                        WbIncident.follow_up_on.isnot(None), WbIncident.follow_up_on <= on).scalar()
    inside = db.query(func.count(WbVisitor.id)).filter(WbVisitor.company_id == cid, WbVisitor.out_at.is_(None),
                                                       WbVisitor.in_at >= a - timedelta(days=1)).scalar()
    visitors = db.query(func.count(WbVisitor.id)).filter(WbVisitor.company_id == cid, WbVisitor.in_at >= a, WbVisitor.in_at < b).scalar()
    passes = db.query(func.count(WbGatePass.id)).filter(WbGatePass.company_id == cid, WbGatePass.out_at >= a, WbGatePass.out_at < b).scalar()
    return {"infirmary_today": visits or 0, "sent_home_today": sent_home or 0, "open_incidents": inc_q.scalar() or 0,
            "follow_ups_due": follow or 0, "visitors_today": visitors or 0, "visitors_inside": inside or 0,
            "gate_passes_today": passes or 0, "houses": service.house_totals(db, cid)[:6],
            "can_counsel": _can(db, user, COUNSEL), "can_manage": _can(db, user, MANAGE),
            "can_record": _can(db, user, RECORD) or _can(db, user, MANAGE)}


# ── Portal (parents and students) ────────────────────────────────────

@router.get("/portal/summary")
def portal_summary(student_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    from modules.students.models import Student
    from modules.students.service import require_student_access
    if not user.get("portal_id"):
        raise HTTPException(status_code=403, detail="This page is for parents and students.")
    require_student_access(db, user, student_id)
    cid = user["company_id"]
    s = db.query(Student).filter(Student.company_id == cid, Student.id == student_id).first()
    visits = (db.query(WbInfirmaryVisit).filter(WbInfirmaryVisit.company_id == cid, WbInfirmaryVisit.student_id == student_id)
              .order_by(WbInfirmaryVisit.visit_at.desc()).limit(20).all())
    pts = (db.query(WbPoint).filter(WbPoint.company_id == cid, WbPoint.student_id == student_id, WbPoint.voided.is_(False))
           .order_by(WbPoint.awarded_on.desc(), WbPoint.id.desc()).limit(30).all())
    house = None
    if s and s.house_id:
        house = next((h for h in service.house_totals(db, cid) if h["house_id"] == s.house_id), None)
    rec = service.health_record(db, cid, student_id)
    value = service.bmi(rec.height_cm, rec.weight_kg) if rec else None
    return {
        "visits": [{"uuid": str(v.uuid), "visit_at": v.visit_at, "complaint": v.complaint, "treatment": v.treatment,
                    "medicine_given": v.medicine_given, "sent_home": v.sent_home} for v in visits],
        "points_total": service.points_total(db, cid, student_id),
        "merits": sum(p.points for p in pts if p.points > 0), "demerits": sum(p.points for p in pts if p.points < 0),
        "points": [{"uuid": str(p.uuid), "points": p.points, "reason": p.reason, "category": p.category,
                    "awarded_on": p.awarded_on} for p in pts],
        "house": house,
        "health": {"height_cm": _num(rec.height_cm) if rec else None, "weight_kg": _num(rec.weight_kg) if rec else None,
                   "bmi": _num(value), "measured_on": rec.measured_on if rec else None,
                   "allergies": s.allergies if s else None, "blood_group": s.blood_group if s else None},
    }
