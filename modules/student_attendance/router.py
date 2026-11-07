"""Student attendance API (/student-attendance; /attendance belongs to staff attendance):
registers, overviews, reports, leave requests, settings, calendar and the portal."""
from datetime import date
from datetime import date as Date
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from core.crud import crud_router
from core.db import get_db
from core.errors import BadRequest, Forbidden
from core.pagination import PageParams, paginate
from core.security import _get_role_permissions, get_current_user, require_permission, require_portal
from core.timeutil import today_for_company_id
from modules.academics import service as academics
from modules.student_attendance import service
from modules.student_attendance.models import (AttendanceCorrection, AttendanceStatus, CalendarDay,
                                               StudentLeaveRequest)
from modules.students import service as students
from modules.students.models import Student

router = APIRouter(prefix="/student-attendance", tags=["Student attendance"])
VIEW, MARK, SETTINGS, LEAVE = "attendance.view", "attendance.mark", "attendance.settings", "attendance.leave.manage"


def _perms(db: Session, user: dict) -> list[str]:
    return _get_role_permissions(user["company_id"], user["role"], db)


def _staff(user: dict) -> None:
    if user.get("portal_id"):
        raise HTTPException(status_code=403, detail="Permission denied")


def _any(db: Session, user: dict, *keys: str) -> list[str]:
    _staff(user)
    perms = _perms(db, user)
    if user["role"] != "admin" and not any(k in perms for k in keys):
        raise HTTPException(status_code=403, detail=f"Permission denied: {keys[0]}")
    return perms


def _section_scope(db: Session, user: dict, perms: list[str]) -> set[int] | None:
    """None = every section; otherwise the sections the teacher teaches or leads."""
    if user["role"] == "admin" or VIEW in perms or "attendance.mark.any" in perms:
        return None
    return academics.teacher_section_ids(db, user["company_id"], user.get("employee_id"))


def _check_section(db: Session, user: dict, perms: list[str], section_id: int) -> None:
    scope = _section_scope(db, user, perms)
    if scope is not None and section_id not in scope:
        raise Forbidden("You don't teach this section.")


# ── Settings, statuses, calendar ─────────────────────────────────────

class SettingsIn(BaseModel):
    daily_sessions: int | None = Field(None, ge=1, le=2)
    lock_after_days: int | None = Field(None, ge=0, le=365)
    class_teacher_only: bool | None = None
    absence_alerts: bool | None = None
    absence_alert_when: str | None = Field(None, pattern="^(on_mark|after_time)$")
    absence_alert_time: str | None = Field(None, max_length=5)
    alert_on_late: bool | None = None
    low_attendance_threshold: int | None = Field(None, ge=1, le=100)
    low_attendance_alerts: bool | None = None
    low_alert_weekday: int | None = Field(None, ge=1, le=7)
    low_alert_min_units: int | None = Field(None, ge=0, le=500)
    notify_class_teacher_low: bool | None = None
    use_staff_holidays: bool | None = None
    leave_max_days: int | None = Field(None, ge=1, le=120)
    leave_past_days: int | None = Field(None, ge=0, le=60)
    leave_attachment_after_days: int | None = Field(None, ge=0, le=60)


@router.get("/settings")
def get_settings(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _staff(user)
    cid = user["company_id"]
    return {**service.get_settings(db, cid), "mode": service.mode(db, cid), "sessions": service.daily_sessions(db, cid),
            "week_days": service.profile(db, cid).get("week_days")}


@router.put("/settings")
def put_settings(payload: SettingsIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(SETTINGS))):
    out = service.save_settings(db, user["company_id"], payload.model_dump(exclude_unset=True))
    db.commit()
    return out


class StatusIn(BaseModel):
    code: str = Field(..., min_length=1, max_length=10)
    name: str = Field(..., min_length=1, max_length=60)
    counts_as: Decimal = Field(Decimal("1"), ge=0, le=1)
    in_total: bool = True
    is_absent: bool = False
    is_late: bool = False
    is_leave: bool = False
    color: str | None = Field(None, max_length=20)
    sort_order: int = 50


class StatusPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=60)
    counts_as: Decimal | None = Field(None, ge=0, le=1)
    in_total: bool | None = None
    is_absent: bool | None = None
    is_late: bool | None = None
    is_leave: bool | None = None
    color: str | None = Field(None, max_length=20)
    sort_order: int | None = None
    active: bool | None = None


def _status_validate(db: Session, cid: int, data: dict, row) -> None:
    if "code" in data:
        data["code"] = data["code"].strip().upper()
    if row is not None and row.is_system and data.get("active") is False and row.code in ("P", "A"):
        raise HTTPException(status_code=400, detail="Present and Absent are always available.")


router.include_router(crud_router(
    model=AttendanceStatus, prefix="/statuses", tag="Student attendance", create_schema=StatusIn,
    update_schema=StatusPatch, view_perm=MARK, manage_perm=SETTINGS, out=service.status_out,
    search_fields=("code", "name"), order_by=("sort_order", "code"), label="Status", validate=_status_validate,
))


class CalendarIn(BaseModel):
    date: Date
    kind: str = Field("holiday", pattern="^(holiday|working)$")
    name: str = Field(..., min_length=1, max_length=150)
    program_ids: list[int] = []


class CalendarPatch(BaseModel):
    date: Date | None = None
    kind: str | None = Field(None, pattern="^(holiday|working)$")
    name: str | None = Field(None, min_length=1, max_length=150)
    program_ids: list[int] | None = None
    active: bool | None = None


def _cal_out(c: CalendarDay) -> dict:
    return {"uuid": str(c.uuid), "id": c.id, "date": c.date.isoformat(), "kind": c.kind, "name": c.name,
            "program_ids": c.program_ids or [], "active": c.active}


router.include_router(crud_router(
    model=CalendarDay, prefix="/calendar", tag="Student attendance", create_schema=CalendarIn,
    update_schema=CalendarPatch, view_perm=MARK, manage_perm=SETTINGS, out=_cal_out,
    search_fields=("name",), order_by=("date",), label="Calendar day", filters=("kind",),
))


@router.get("/calendar-days")
def calendar_days(date_from: date, date_to: date, program_id: int | None = None, db: Session = Depends(get_db),
                  user: dict = Depends(get_current_user)):
    """Working days and holidays in a range (week days, staff holidays and the institution calendar)."""
    _staff(user)
    if (date_to - date_from).days > 400:
        raise BadRequest("Pick a range of at most a year.")
    cal = service.calendar_map(db, user["company_id"], date_from, date_to, program_id)
    return [{"date": d.isoformat(), "working": w, "reason": r} for d, (w, r) in sorted(cal.items())]


# ── Registers ────────────────────────────────────────────────────────

@router.get("/overview")
def overview(on: date | None = None, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Today's (or a date's) marking across sections: marked or not, present %, absentees."""
    perms = _any(db, user, VIEW, MARK, "attendance.mark.any")
    cid = user["company_id"]
    on = on or today_for_company_id(db, cid)
    return service.day_overview(db, cid, on, _section_scope(db, user, perms))


@router.get("/my-registers")
def my_registers(on: date | None = None, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    perms = _any(db, user, VIEW, MARK, "attendance.mark.any")
    cid = user["company_id"]
    on = on or today_for_company_id(db, cid)
    return {"date": on.isoformat(), "mode": service.mode(db, cid), "registers": service.my_registers(db, cid, user, perms, on)}


@router.get("/sections")
def section_options(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Sections this person may open registers for."""
    perms = _any(db, user, VIEW, MARK, "attendance.mark.any")
    scope = _section_scope(db, user, perms)
    return [{"id": s.id, "uuid": str(s.uuid), "label": academics.section_label(s), "program_id": s.program_id,
             "class_teacher_id": s.class_teacher_id}
            for s in service.current_sections(db, user["company_id"], scope)]


@router.get("/register")
def get_register(section_id: int, on: date, slot: str | None = None, period_no: int = 0, subject_id: int | None = None,
                 db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    perms = _any(db, user, VIEW, MARK, "attendance.mark.any")
    _check_section(db, user, perms, section_id)
    return service.load_register(db, user["company_id"], user, perms, section_id, on, slot, period_no, subject_id)


class MarkIn(BaseModel):
    student_id: int
    code: str = Field(..., max_length=10)
    late_minutes: int | None = Field(None, ge=0, le=600)
    remarks: str | None = Field(None, max_length=300)
    source: str | None = None


class RegisterIn(BaseModel):
    section_id: int
    date: Date
    slot: str = Field("day", pattern="^(day|am|pm|period)$")
    period_no: int = 0
    subject_id: int | None = None
    marks: list[MarkIn]
    reason: str | None = Field(None, max_length=500)


@router.post("/register")
def save_register(payload: RegisterIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    perms = _any(db, user, MARK, "attendance.mark.any", "attendance.correct")
    data = payload.model_dump()
    reg = service.save_register(db, user["company_id"], user, perms, data)
    db.commit()
    return service.load_register(db, user["company_id"], user, perms, payload.section_id, payload.date, reg.slot,
                                 reg.period_no, reg.subject_id)


class QuickIn(BaseModel):
    section_id: int
    date: Date
    slot: str = Field("day", pattern="^(day|am|pm|period)$")
    period_no: int = 0
    subject_id: int | None = None


@router.post("/register/all-present")
def all_present(payload: QuickIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    perms = _any(db, user, MARK, "attendance.mark.any")
    reg = service.mark_all_present(db, user["company_id"], user, perms, payload.section_id, payload.date, payload.slot,
                                   payload.period_no, payload.subject_id)
    db.commit()
    return service.register_out(reg)


@router.get("/corrections")
def corrections(date_from: date | None = None, date_to: date | None = None, section_id: int | None = None,
                params: PageParams = Depends(), db: Session = Depends(get_db),
                user: dict = Depends(require_permission(VIEW))):
    from sqlalchemy.orm import joinedload
    from modules.student_attendance.models import AttendanceRegister
    q = (db.query(AttendanceCorrection).options(joinedload(AttendanceCorrection.student),
                                                joinedload(AttendanceCorrection.register).joinedload(AttendanceRegister.section))
         .filter(AttendanceCorrection.company_id == user["company_id"]))
    if date_from:
        q = q.filter(AttendanceCorrection.date >= date_from)
    if date_to:
        q = q.filter(AttendanceCorrection.date <= date_to)
    if section_id:
        q = q.join(AttendanceRegister, AttendanceRegister.id == AttendanceCorrection.register_id).filter(
            AttendanceRegister.section_id == section_id)
    from core.timeutil import iso_utc
    return paginate(q.order_by(AttendanceCorrection.created_at.desc()), params, lambda c: {
        "uuid": str(c.uuid), "date": c.date.isoformat(), "student_name": c.student.full_name if c.student else None,
        "section_label": academics.section_label(c.register.section) if c.register and c.register.section else None,
        "old_code": c.old_code, "new_code": c.new_code, "reason": c.reason, "by_name": c.by_name,
        "at": iso_utc(c.created_at)})


# ── Reports ──────────────────────────────────────────────────────────

@router.get("/reports/monthly")
def monthly(section_id: int, month: str = Query(..., pattern=r"^\d{4}-\d{2}$"), format: str = "json",
            db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    perms = _any(db, user, VIEW, MARK, "attendance.mark.any")
    _check_section(db, user, perms, section_id)
    y, m = (int(x) for x in month.split("-"))
    data = service.monthly_register(db, user["company_id"], section_id, y, m)
    if format == "csv":
        from core import audit
        audit.event(db, "export.downloaded", "student_attendance_registers", None,
                    {"report": "monthly_register", "section_id": section_id, "month": month})
        db.commit()
        name = f"attendance-{data['section']['label'].replace(' ', '')}-{month}.csv"
        return Response(service.monthly_register_csv(data), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})
    return data


@router.get("/reports/percentages")
def learner_pct(section_id: int, term_id: int | None = None, date_from: date | None = None, date_to: date | None = None,
                db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    perms = _any(db, user, VIEW, MARK, "attendance.mark.any")
    _check_section(db, user, perms, section_id)
    cid = user["company_id"]
    start, end, label = service.range_for(db, cid, term_id, date_from, date_to)
    return {"from": start.isoformat(), "to": end.isoformat(), "label": label,
            "threshold": service.get_settings(db, cid)["low_attendance_threshold"],
            "rows": service.learner_percentages(db, cid, section_id, start, end)}


@router.get("/reports/subjects")
def subject_pct(section_id: int, term_id: int | None = None, date_from: date | None = None, date_to: date | None = None,
                db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    perms = _any(db, user, VIEW, MARK, "attendance.mark.any")
    _check_section(db, user, perms, section_id)
    cid = user["company_id"]
    start, end, label = service.range_for(db, cid, term_id, date_from, date_to)
    return {"from": start.isoformat(), "to": end.isoformat(), "label": label,
            **service.subject_percentages(db, cid, section_id, start, end)}


@router.get("/reports/defaulters")
def defaulters(threshold: float | None = Query(None, ge=1, le=100), section_id: int | None = None,
               term_id: int | None = None, date_from: date | None = None, date_to: date | None = None,
               db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    perms = _any(db, user, VIEW, MARK, "attendance.mark.any")
    cid = user["company_id"]
    scope = _section_scope(db, user, perms)
    if section_id:
        if scope is not None and section_id not in scope:
            raise Forbidden("You don't teach this section.")
        scope = {section_id}
    start, end, label = service.range_for(db, cid, term_id, date_from, date_to)
    t = threshold if threshold is not None else service.get_settings(db, cid)["low_attendance_threshold"]
    return {"from": start.isoformat(), "to": end.isoformat(), "label": label, "threshold": t,
            "rows": service.defaulters(db, cid, start, end, t, scope)}


class NotifyIn(BaseModel):
    student_ids: list[int] = Field(..., min_length=1)
    threshold: float | None = None


@router.post("/reports/defaulters/notify")
def notify_defaulters(payload: NotifyIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    cid = user["company_id"]
    start, end, _ = service.range_for(db, cid)
    t = payload.threshold or service.get_settings(db, cid)["low_attendance_threshold"]
    rows = [r for r in service.defaulters(db, cid, start, end, t) if r["student_id"] in set(payload.student_ids)]
    sent = service.notify_low_attendance(db, cid, rows, f"manual-{today_for_company_id(db, cid).isoformat()}", t)
    db.commit()
    return {"sent": sent}


@router.get("/students/{uuid}")
def student_detail(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """A learner's attendance for staff: summary, this month's calendar and recent absences."""
    perms = _any(db, user, VIEW, MARK, "attendance.mark.any")
    cid = user["company_id"]
    s = db.query(Student).filter(Student.company_id == cid, Student.uuid == uuid).first()
    if s is None:
        raise HTTPException(status_code=404, detail="Student not found")
    sec = students.student_section_id(db, cid, s.id)
    if sec:
        _check_section(db, user, perms, sec)
    today = today_for_company_id(db, cid)
    start, end, _ = service.range_for(db, cid)
    return {"student": {"id": s.id, "uuid": str(s.uuid), "name": s.full_name},
            "summary": service.student_summary(db, cid, s.id),
            "calendar": service.student_calendar(db, cid, s.id, today.year, today.month),
            "absences": service.student_absences(db, cid, s.id, start, end)}


@router.get("/summary")
def summary(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Home card: today's marking across sections (for viewers) and the teacher's own registers."""
    _staff(user)
    cid = user["company_id"]
    perms = _perms(db, user)
    today = today_for_company_id(db, cid)
    out = {"date": today.isoformat(), "mode": service.mode(db, cid), "overview": None, "registers": []}
    if user["role"] == "admin" or VIEW in perms:
        ov = service.day_overview(db, cid, today)
        out["overview"] = ov["totals"]
        out["unmarked"] = [r["label"] for r in ov["sections"] if r["working"] and r["expected"] and not r["complete"]][:6]
    if user.get("employee_id") and (MARK in perms or "attendance.mark.any" in perms or user["role"] == "admin"):
        out["registers"] = service.my_registers(db, cid, user, perms, today)
    return out


# ── Leave requests (staff) ───────────────────────────────────────────

@router.get("/leave")
def list_leave(status: str | None = None, section_id: int | None = None, q: str | None = None,
               date_from: date | None = None, date_to: date | None = None, mine: bool = False,
               params: PageParams = Depends(), db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Leave requests: everyone's for leave managers, the sections they teach for teachers."""
    perms = _any(db, user, VIEW, MARK, LEAVE, "attendance.mark.any")
    cid = user["company_id"]
    query = service.leave_query(db, cid)
    if not (user["role"] == "admin" or LEAVE in perms or VIEW in perms):
        query = query.filter(StudentLeaveRequest.section_id.in_(
            academics.teacher_section_ids(db, cid, user.get("employee_id")) or {0}))
    if mine and user.get("employee_id"):
        from core.workflow.models import WorkflowTask
        ids = [i for (i,) in db.query(WorkflowTask.instance_id).filter(
            WorkflowTask.company_id == cid, WorkflowTask.status == "pending",
            WorkflowTask.approver_employee_id == user["employee_id"])]
        query = query.filter(StudentLeaveRequest.workflow_instance_id.in_(ids or [0]))
    if status:
        query = query.filter(StudentLeaveRequest.status == status)
    if section_id:
        query = query.filter(StudentLeaveRequest.section_id == section_id)
    if date_from:
        query = query.filter(StudentLeaveRequest.to_date >= date_from)
    if date_to:
        query = query.filter(StudentLeaveRequest.from_date <= date_to)
    if q:
        from sqlalchemy import func
        like = f"%{q.strip()}%"
        query = query.join(Student, Student.id == StudentLeaveRequest.student_id).filter(or_(
            func.concat(Student.first_name, " ", Student.last_name).ilike(like), Student.admission_no.ilike(like)))
    page = paginate(query.order_by(StudentLeaveRequest.created_at.desc()), params, lambda r: service.leave_out(r))
    tasks = service.pending_task_names(db, cid, [r["workflow_instance_id"] for r in page["items"] if r["status"] == "pending"])
    for r in page["items"]:
        t = tasks.get(r["workflow_instance_id"], [])
        r["waiting_for"] = [x["name"] for x in t]
        r["can_decide"] = any(x["mine"] for x in t)
    counts = dict(db.query(StudentLeaveRequest.status, __import__("sqlalchemy").func.count(StudentLeaveRequest.id))
                  .filter(StudentLeaveRequest.company_id == cid).group_by(StudentLeaveRequest.status).all())
    page["counts"] = counts
    return page


@router.get("/leave/{uuid}")
def leave_detail(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    perms = _any(db, user, VIEW, MARK, LEAVE, "attendance.mark.any")
    cid = user["company_id"]
    r = service.get_leave(db, cid, uuid)
    if not (user["role"] == "admin" or LEAVE in perms or VIEW in perms) and r.section_id:
        _check_section(db, user, [], r.section_id)
    out = service.leave_out(r, db, links=True)
    t = service.pending_task_names(db, cid, [r.workflow_instance_id] if r.workflow_instance_id else [])
    out["waiting_for"] = [x["name"] for x in t.get(r.workflow_instance_id, [])]
    out["can_decide"] = any(x["mine"] for x in t.get(r.workflow_instance_id, []))
    return out


class StaffLeaveIn(BaseModel):
    student_id: int
    from_date: date
    to_date: date
    reason: str = Field(..., min_length=1, max_length=2000)
    status_code: str | None = Field(None, max_length=10)


@router.post("/leave", status_code=201)
def record_leave(payload: StaffLeaveIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(LEAVE))):
    r = service.create_leave(db, user["company_id"], user, payload.student_id, payload.from_date, payload.to_date,
                             payload.reason, payload.status_code, by_staff=True)
    db.commit()
    return service.leave_out(service.get_leave(db, user["company_id"], r.uuid))


class DecideIn(BaseModel):
    decision: str = Field(..., pattern="^(approve|reject)$")
    comment: str | None = Field(None, max_length=1000)


@router.post("/leave/{uuid}/decide")
def decide(uuid: UUID, payload: DecideIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _staff(user)
    r = service.get_leave(db, user["company_id"], uuid)
    service.decide_leave(db, user["company_id"], r, payload.decision, payload.comment)
    db.commit()
    return service.leave_out(service.get_leave(db, user["company_id"], uuid))


class ReasonIn(BaseModel):
    reason: str | None = Field(None, max_length=500)


@router.post("/leave/{uuid}/cancel")
def cancel_leave(uuid: UUID, payload: ReasonIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(LEAVE))):
    r = service.get_leave(db, user["company_id"], uuid)
    if r.status == "pending":
        service.withdraw_leave(db, user["company_id"], r)
    else:
        service.revoke_leave(db, user["company_id"], r, payload.reason)
    db.commit()
    return service.leave_out(service.get_leave(db, user["company_id"], uuid))


# ── Parent and student portal ────────────────────────────────────────

@router.get("/portal/summary")
def portal_summary(student_id: int, db: Session = Depends(get_db), user: dict = Depends(require_portal("parent", "student"))):
    students.require_student_access(db, user, student_id)
    return service.student_summary(db, user["company_id"], student_id)


@router.get("/portal/calendar")
def portal_calendar(student_id: int, month: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
                    db: Session = Depends(get_db), user: dict = Depends(require_portal("parent", "student"))):
    students.require_student_access(db, user, student_id)
    cid = user["company_id"]
    today = today_for_company_id(db, cid)
    y, m = (int(x) for x in month.split("-")) if month else (today.year, today.month)
    out = service.student_calendar(db, cid, student_id, y, m)
    start, end, label = service.range_for(db, cid)
    out["absences"] = service.student_absences(db, cid, student_id, start, end)
    out["session"] = service.percentages(db, cid, [student_id], start, end).get(student_id)
    out["session_label"] = label
    out["threshold"] = service.get_settings(db, cid)["low_attendance_threshold"]
    return out


@router.get("/portal/leave")
def portal_leave(student_id: int | None = None, db: Session = Depends(get_db),
                 user: dict = Depends(require_portal("parent", "student"))):
    cid = user["company_id"]
    ids = students.visible_student_ids(db, user) or set()
    if student_id is not None:
        students.require_student_access(db, user, student_id)
        ids = {student_id}
    rows = (service.leave_query(db, cid).filter(StudentLeaveRequest.student_id.in_(ids or {0}))
            .order_by(StudentLeaveRequest.from_date.desc()).limit(100).all())
    statuses = [service.status_out(s) for s in service.list_statuses(db, cid) if s.is_leave]
    cfg = service.get_settings(db, cid)
    return {"items": [service.leave_out(r, db, links=True) for r in rows], "leave_kinds": statuses,
            "rules": {k: cfg[k] for k in ("leave_max_days", "leave_past_days", "leave_attachment_after_days")}}


class PortalLeaveIn(BaseModel):
    student_id: int
    from_date: date
    to_date: date
    reason: str = Field(..., min_length=1, max_length=2000)
    status_code: str | None = Field(None, max_length=10)
    attachments: list[UUID] = []


@router.post("/portal/leave", status_code=201)
def portal_apply(payload: PortalLeaveIn, db: Session = Depends(get_db),
                 user: dict = Depends(require_portal("parent", "student"))):
    students.require_student_access(db, user, payload.student_id)
    r = service.create_leave(db, user["company_id"], user, payload.student_id, payload.from_date, payload.to_date,
                             payload.reason, payload.status_code, [str(a) for a in payload.attachments])
    db.commit()
    return service.leave_out(service.get_leave(db, user["company_id"], r.uuid))


@router.post("/portal/leave/{uuid}/withdraw")
def portal_withdraw(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_portal("parent", "student"))):
    r = service.get_leave(db, user["company_id"], uuid)
    students.require_student_access(db, user, r.student_id)
    service.withdraw_leave(db, user["company_id"], r)
    db.commit()
    return service.leave_out(service.get_leave(db, user["company_id"], uuid))


@router.post("/portal/uploads", status_code=201)
async def portal_upload(file: UploadFile = File(...), db: Session = Depends(get_db),
                        user: dict = Depends(require_portal("parent", "student"))):
    """A note or medical certificate for a leave request (up to 10 MB)."""
    from modules.documents import storage
    data = await file.read()
    blob = storage.store(db, user["company_id"], file.filename or "file", data, file.content_type, "student_leave")
    db.commit()
    return storage.describe(blob)
