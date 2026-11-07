"""Attendance API: self-service, team views, admin records, rules and settings, locks,
devices and imports, reports and the payroll export."""
from __future__ import annotations

import io
import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core import audit, csv_safe
from core.db import get_db, set_tenant
from core.errors import BadRequest, Forbidden, NotFound
from core.pagination import PageParams, paginate
from core.security import get_current_user, require_permission
from core.timeutil import iso_utc
from modules.attendance import devices, engine, requests as att_requests, service, settings_store
from modules.attendance.models import (AttendanceAdjustment, AttendanceDay, AttendanceLock, AttendanceRequest,
                                       AttendanceRule, Device, DeviceEnrolment, DeviceSyncLog, Geofence, Punch)

router = APIRouter(prefix="/attendance", tags=["Attendance"])

VIEW = require_permission("time.attendance.view")
MANAGE = require_permission("time.attendance.manage")
SETTINGS = require_permission("time.attendance.settings")


def _me(db: Session, user: dict):
    if not user.get("employee_id"):
        raise BadRequest("The company owner has no attendance of their own. Use Team or Records.")
    return service.employee(db, user["company_id"], user["employee_id"])


def _csv(name: str, header: list[str], rows: list[list]) -> Response:
    buf = io.StringIO()
    w = csv_safe.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})


# ── Self service ────────────────────────────────────────────────────

class ClockIn(BaseModel):
    latitude: float | None = Field(None, ge=-90, le=90)
    longitude: float | None = Field(None, ge=-180, le=180)
    accuracy: float | None = None
    selfie: str | None = None
    direction: str | None = None


@router.get("/me/access")
def my_access(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    team = service.report_ids(db, cid, user.get("employee_id"))
    return {"employee_id": user.get("employee_id"), "can_manage": service.can_manage(db, user),
            "can_settings": service.has_perm(db, user, "time.attendance.settings"),
            "can_roster": service.has_perm(db, user, "time.roster.manage"),
            "can_timesheets": service.has_perm(db, user, "time.timesheets.manage"),
            "team_size": len(team), "today": service.today(db, cid).isoformat()}


@router.get("/me/today")
def my_today(user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    emp = _me(db, user)
    out = service.today_card(db, user["company_id"], emp.id)
    db.commit()
    return out


@router.post("/me/clock")
def clock(req: ClockIn, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    emp = _me(db, user)
    p = service.clock(db, user["company_id"], emp.id, req.model_dump())
    db.commit()
    out = service.today_card(db, user["company_id"], emp.id)
    db.commit()
    return {"punch": service.punch_out(p), "today": out}


@router.get("/me/month")
def my_month(year: int = Query(..., ge=2000, le=2100), month: int = Query(..., ge=1, le=12),
             user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    emp = _me(db, user)
    out = service.month_view(db, user["company_id"], emp.id, year, month)
    db.commit()
    return out


@router.get("/me/days/{day}")
def my_day(day: date, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    emp = _me(db, user)
    row = db.query(AttendanceDay).filter(AttendanceDay.company_id == user["company_id"],
                                         AttendanceDay.employee_id == emp.id, AttendanceDay.date == day).first()
    if not row:
        raise NotFound("Attendance for this day")
    return service.day_detail(db, user["company_id"], row)


class RequestIn(BaseModel):
    date: date
    end_date: date | None = None
    duration: str | None = None
    in_time: str | None = None
    out_time: str | None = None
    out_next_day: bool = False
    mode: str | None = None
    from_time: str | None = None
    to_time: str | None = None
    hours: float | None = None
    category_id: int | None = None
    reason: str | None = Field(None, max_length=1000)
    reason_code: str | None = None
    place: str | None = Field(None, max_length=200)
    consent: bool = False


@router.post("/me/requests/{kind}", status_code=201)
def create_request(kind: str, req: RequestIn, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    emp = _me(db, user)
    row, inst = att_requests.create(db, user["company_id"], emp, kind, req.model_dump())
    db.commit()
    return {**service.request_brief(row), "reference_no": inst.reference_no, "workflow_status": inst.status,
            "workflow_uuid": str(inst.uuid)}


def _requests_query(db, cid, ids, kind, status):
    q = db.query(AttendanceRequest).filter(AttendanceRequest.company_id == cid)
    if ids is not None:
        q = q.filter(AttendanceRequest.employee_id.in_(ids or [-1]))
    if kind:
        q = q.filter(AttendanceRequest.kind.in_(kind.split(",")))
    if status:
        q = q.filter(AttendanceRequest.status.in_(status.split(",")))
    return q.order_by(AttendanceRequest.created_at.desc())


def _request_out(db, cid):
    from core.workflow.models import WorkflowInstance
    cache: dict = {}

    def out(r: AttendanceRequest):
        if r.employee_id not in cache:
            cache[r.employee_id] = service.name_of(service.employee(db, cid, r.employee_id))
        inst = db.query(WorkflowInstance).filter(WorkflowInstance.id == r.workflow_instance_id).first() \
            if r.workflow_instance_id else None
        return {**service.request_brief(r), "employee_id": r.employee_id, "employee_name": cache[r.employee_id],
                "reference_no": inst.reference_no if inst else None,
                "workflow_uuid": str(inst.uuid) if inst else None}
    return out


@router.get("/me/requests")
def my_requests(kind: str | None = None, status: str | None = None, pages: PageParams = Depends(),
                user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    emp = _me(db, user)
    q = _requests_query(db, user["company_id"], [emp.id], kind, status)
    return paginate(q, pages, _request_out(db, user["company_id"]))


@router.get("/me/request-options")
def request_options(on: date | None = None, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    """Limits and what is used this month, to guide the request forms."""
    cid = user["company_id"]
    d = on or service.today(db, cid)
    ms, me = service.month_range(d.year, d.month)
    used = {}
    if user.get("employee_id"):
        rows = (db.query(AttendanceRequest).filter(AttendanceRequest.company_id == cid,
                                                   AttendanceRequest.employee_id == user["employee_id"],
                                                   AttendanceRequest.status.in_(["pending", "approved"]),
                                                   AttendanceRequest.date >= ms, AttendanceRequest.date <= me).all())
        used = {
            "regularisation": sum(1 for r in rows if r.kind == "regularisation"),
            "wfh_days": sum(att_requests.DURATIONS.get(r.duration or "full_day", 1) * (((r.end_date or r.date) - r.date).days + 1)
                            for r in rows if r.kind == "wfh"),
            "permission_count": sum(1 for r in rows if r.kind == "permission"),
            "permission_minutes": sum(int(r.minutes or 0) for r in rows if r.kind == "permission"),
        }
    return {"month": d.strftime("%Y-%m"), "used": used,
            **{k: service.get_setting(db, cid, k, d) for k in ("regularisation", "wfh", "permission", "on_duty",
                                                                  "overtime_request")}}


@router.get("/me/schedule")
def my_schedule(start: date, end: date, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    from modules.roster import service as roster
    emp = _me(db, user)
    if (end - start).days > 62:
        raise BadRequest("Show up to two months at a time.")
    return roster.my_schedule(db, user["company_id"], emp.id, start, end)


# ── Team ────────────────────────────────────────────────────────────

def _scope(db, user) -> list[int] | None:
    ids = service.visible_ids(db, user)
    if ids is not None and not ids:
        raise Forbidden("You have no team to show.")
    return ids


@router.get("/team/today")
def team_today(department_id: int | None = None, q: str | None = None, user: dict = Depends(VIEW),
               db: Session = Depends(get_db)):
    out = service.team_today(db, user["company_id"], _scope(db, user), department_id, q)
    db.commit()
    return out


@router.get("/team/muster")
def team_muster(year: int = Query(..., ge=2000, le=2100), month: int = Query(..., ge=1, le=12),
                department_id: int | None = None, q: str | None = None, user: dict = Depends(VIEW),
                db: Session = Depends(get_db)):
    out = service.muster(db, user["company_id"], _scope(db, user), year, month, department_id, q)
    db.commit()
    return out


@router.get("/employees/{uuid}/month")
def employee_month(uuid: UUID, year: int = Query(..., ge=2000, le=2100), month: int = Query(..., ge=1, le=12),
                   user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    emp = service.employee_by_uuid(db, user["company_id"], uuid)
    service.check_can_see(db, user, emp.id)
    out = service.month_view(db, user["company_id"], emp.id, year, month)
    db.commit()
    return {**out, "employee": {"uuid": str(emp.uuid), "name": service.name_of(emp), "employee_code": emp.employee_code}}


@router.get("/requests")
def team_requests(kind: str | None = None, status: str | None = None, pages: PageParams = Depends(),
                  user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    q = _requests_query(db, user["company_id"], _scope(db, user), kind, status)
    return paginate(q, pages, _request_out(db, user["company_id"]))


# ── Records (HR) ────────────────────────────────────────────────────

@router.get("/records")
def records(start: date, end: date, status: str | None = None, flags: str | None = None,
            department_id: int | None = None, q: str | None = None, pages: PageParams = Depends(),
            user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    if (end - start).days > 92:
        raise BadRequest("Show up to three months at a time.")
    cid = user["company_id"]
    ids = _scope(db, user)
    shifts = service._shift_map(db, cid)
    query = service.records_query(db, cid, ids, start, end, status.split(",") if status else None,
                                  flags.split(",") if flags else None, department_id, q)
    return paginate(query, pages, lambda row: {**service.day_out(row[0], shifts.get(row[0].shift_id), service.name_of(row[1])),
                                               "employee_uuid": str(row[1].uuid), "employee_code": row[1].employee_code})


def _record(db, user, uuid) -> AttendanceDay:
    row = db.query(AttendanceDay).filter(AttendanceDay.company_id == user["company_id"], AttendanceDay.uuid == uuid).first()
    if not row:
        raise NotFound("Attendance record")
    service.check_can_see(db, user, row.employee_id)
    return row


@router.get("/records/{uuid}")
def record(uuid: UUID, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    return service.day_detail(db, user["company_id"], _record(db, user, uuid))


class OverrideIn(BaseModel):
    status: str | None = None
    note: str | None = Field(None, max_length=300)


@router.post("/records/{uuid}/override")
def override(uuid: UUID, req: OverrideIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    row = _record(db, user, uuid)
    service.set_override(db, user["company_id"], row, req.status, req.note)
    db.commit()
    return service.day_detail(db, user["company_id"], row)


class AdminPunchIn(BaseModel):
    punched_at: datetime
    direction: str | None = None
    note: str = Field(..., min_length=2, max_length=300)


@router.post("/records/{uuid}/punches", status_code=201)
def add_punch(uuid: UUID, req: AdminPunchIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    row = _record(db, user, uuid)
    ts = req.punched_at if req.punched_at.tzinfo else req.punched_at.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc).replace(microsecond=0)
    if db.query(Punch.id).filter(Punch.employee_id == row.employee_id, Punch.punched_at == ts).first():
        raise BadRequest("There is already a punch at that time.")
    db.add(Punch(company_id=user["company_id"], employee_id=row.employee_id, punched_at=ts,
                 direction=req.direction if req.direction in ("in", "out") else None, source="admin", note=req.note))
    db.flush()
    service.recompute(db, user["company_id"], [row.employee_id], row.date - timedelta(days=1), row.date + timedelta(days=1))
    db.commit()
    return service.day_detail(db, user["company_id"], row)


class IgnoreIn(BaseModel):
    ignored: bool
    note: str | None = Field(None, max_length=300)


@router.post("/punches/{uuid}/ignore")
def ignore_punch(uuid: UUID, req: IgnoreIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    p = db.query(Punch).filter(Punch.company_id == user["company_id"], Punch.uuid == uuid).first()
    if not p:
        raise NotFound("Punch")
    p.ignored = req.ignored
    p.note = req.note or p.note
    db.flush()
    service.recompute_for_punch_times(db, user["company_id"], [(p.employee_id, p.punched_at)])
    db.commit()
    return service.punch_out(p)


@router.get("/punches/{uuid}/selfie")
def punch_selfie(uuid: UUID, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    p = db.query(Punch).filter(Punch.company_id == user["company_id"], Punch.uuid == uuid).first()
    if not p:
        raise NotFound("Punch")
    service.check_can_see(db, user, p.employee_id)
    return {"selfie": service.selfie_of(db, user["company_id"], uuid)}


class RecomputeIn(BaseModel):
    start: date
    end: date
    employee_ids: list[int] | None = None


@router.post("/recompute")
def recompute(req: RecomputeIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    if req.end < req.start or (req.end - req.start).days > 92:
        raise BadRequest("Choose up to three months.")
    cid = user["company_id"]
    ids = req.employee_ids or [e.id for e in service.employees_query(db, cid).all()]
    n = service.recompute(db, cid, ids, req.start, req.end)
    audit.event(db, "attendance.recomputed", "attendance_days", None,
                {"from": req.start.isoformat(), "to": req.end.isoformat(), "employees": len(ids), "days": n})
    db.commit()
    return {"days": n, "employees": len(ids)}


# ── Locks and payroll ───────────────────────────────────────────────

def _lock_out(l: AttendanceLock) -> dict:
    return {"uuid": str(l.uuid), "period_start": l.period_start.isoformat(), "period_end": l.period_end.isoformat(),
            "status": l.status, "locked_at": iso_utc(l.locked_at), "locked_by_name": l.locked_by_name,
            "unlocked_at": iso_utc(l.unlocked_at), "unlocked_by_name": l.unlocked_by_name, "note": l.note}


@router.get("/locks")
def list_locks(user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    rows = (db.query(AttendanceLock).filter(AttendanceLock.company_id == user["company_id"])
            .order_by(AttendanceLock.period_start.desc()).all())
    cid = user["company_id"]
    td = service.today(db, cid)
    prev = td.replace(day=1) - timedelta(days=1)
    s, e = service.pay_period_for(db, cid, prev)
    return {"items": [_lock_out(l) for l in rows], "suggested": {"start": s.isoformat(), "end": e.isoformat()}}


class LockIn(BaseModel):
    start: date
    end: date
    note: str | None = Field(None, max_length=300)


@router.post("/locks", status_code=201)
def lock(req: LockIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    lk = service.lock_period(db, user["company_id"], req.start, req.end, req.note)
    db.commit()
    return _lock_out(lk)


class UnlockIn(BaseModel):
    note: str = Field(..., min_length=3, max_length=300)


@router.post("/locks/{uuid}/unlock")
def unlock(uuid: UUID, req: UnlockIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    lk = db.query(AttendanceLock).filter(AttendanceLock.company_id == user["company_id"], AttendanceLock.uuid == uuid).first()
    if not lk:
        raise NotFound("Lock")
    service.unlock_period(db, user["company_id"], lk, req.note)
    db.commit()
    return _lock_out(lk)


@router.get("/adjustments")
def adjustments(start: date | None = None, end: date | None = None, user: dict = Depends(MANAGE),
                db: Session = Depends(get_db)):
    q = db.query(AttendanceAdjustment).filter(AttendanceAdjustment.company_id == user["company_id"])
    if start:
        q = q.filter(AttendanceAdjustment.apply_from >= start)
    if end:
        q = q.filter(AttendanceAdjustment.apply_from <= end)
    rows = q.order_by(AttendanceAdjustment.created_at.desc()).limit(500).all()
    names = service.names_for(db, user["company_id"], [a.employee_id for a in rows])
    return [{"uuid": str(a.uuid), "employee_id": a.employee_id, "employee_name": names.get(a.employee_id),
             "date": a.date.isoformat(), "apply_from": a.apply_from.isoformat(),
             "payable_days_delta": str(a.payable_days_delta), "lop_days_delta": str(a.lop_days_delta),
             "overtime_minutes_delta": a.overtime_minutes_delta, "old_status": a.old_status,
             "new_status": a.new_status, "reason": a.reason, "created_at": iso_utc(a.created_at)} for a in rows]


# ── Reports ─────────────────────────────────────────────────────────

def _range(start: date | None, end: date | None, db, cid) -> tuple[date, date]:
    td = service.today(db, cid)
    start = start or td.replace(day=1)
    end = end or td
    if end < start or (end - start).days > 370:
        raise BadRequest("Choose a range of up to a year.")
    return start, end


@router.get("/reports/payroll")
def report_payroll(start: date | None = None, end: date | None = None, format: str = "json",
                   user: dict = Depends(require_permission("insights.reports.view")), db: Session = Depends(get_db)):
    cid = user["company_id"]
    start, end = _range(start, end, db, cid)
    if not service.can_manage(db, user):
        raise Forbidden("Only HR can see the payroll summary.")
    rows = service.payroll_summary(db, cid, start, end)
    db.commit()
    if format == "csv":
        audit.event(db, "export.downloaded", "attendance_days", None, {"report": "payroll", "from": start.isoformat()})
        db.commit()
        cols = ["employee_code", "name", "days_in_period", "present_days", "half_days", "absent_days", "leave_days",
                "holidays", "weekly_offs", "late_marks", "payable_days", "lop_days", "penalty_lop_days",
                "overtime_hours", "night_shifts", "shift_allowance_days", "shift_allowance_amount",
                "adjustment_payable_days", "adjustment_lop_days", "adjustment_overtime_hours"]
        return _csv(f"attendance-payroll-{start}-{end}", cols, [[r[c] for c in cols] for r in rows])
    return {"start": start.isoformat(), "end": end.isoformat(), "rows": rows}


@router.get("/reports/overtime")
def report_overtime(start: date | None = None, end: date | None = None, format: str = "json",
                    user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    cid = user["company_id"]
    start, end = _range(start, end, db, cid)
    out = service.overtime_report(db, cid, _scope(db, user), start, end)
    if format == "csv":
        cols = ["employee_code", "name", "overtime_hours", "overtime_days", "unapproved_hours", "quarter_hours",
                "quarter_cap_hours", "alert"]
        return _csv(f"overtime-{start}-{end}", cols, [[r[c] for c in cols] for r in out["rows"]])
    return out


@router.get("/reports/late-early")
def report_late(start: date | None = None, end: date | None = None, format: str = "json",
                user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    cid = user["company_id"]
    start, end = _range(start, end, db, cid)
    out = service.late_early_report(db, cid, _scope(db, user), start, end)
    if format == "csv":
        cols = ["employee_code", "name", "late", "late_minutes", "early", "early_minutes", "penalty"]
        return _csv(f"late-early-{start}-{end}", cols, [[r[c] for c in cols] for r in out["rows"]])
    return out


@router.get("/reports/muster")
def report_muster(year: int = Query(..., ge=2000, le=2100), month: int = Query(..., ge=1, le=12),
                  department_id: int | None = None, format: str = "json", user: dict = Depends(VIEW),
                  db: Session = Depends(get_db)):
    """Muster roll in the register style (OSH Code / state forms): one row per person, a
    code per day, and totals."""
    out = service.muster(db, user["company_id"], _scope(db, user), year, month, department_id)
    db.commit()
    if format == "csv":
        days = sorted(out["rows"][0]["cells"]) if out["rows"] else []
        header = ["Employee ID", "Name", *[d[8:] for d in days], "Present", "Half days", "Absent", "Leave",
                  "Holidays", "Weekly offs", "Payable days", "LOP days", "OT hours"]
        rows = []
        for r in out["rows"]:
            s = r["summary"]
            rows.append([r["employee_code"] or "", r["name"],
                         *[service.MUSTER_CODES.get(r["cells"][d]["s"], "") for d in days],
                         s.get("present", 0) + s.get("wfh", 0) + s.get("on_duty", 0), s.get("half_day", 0),
                         s.get("absent", 0) + s.get("missing_punch", 0), s.get("on_leave", 0), s.get("holiday", 0),
                         s.get("weekly_off", 0), s.get("payable_days"), s.get("lop_days"),
                         round(s.get("overtime_minutes", 0) / 60, 2)])
        audit.event(db, "export.downloaded", "attendance_days", None, {"report": "muster", "month": f"{year}-{month:02d}"})
        db.commit()
        return _csv(f"muster-roll-{year}-{month:02d}", header, rows)
    return {**out, "codes": service.MUSTER_CODES}


# ── Rules ───────────────────────────────────────────────────────────

class RuleIn(BaseModel):
    name: str | None = Field(None, max_length=120)
    description: str | None = None
    effective_from: date | None = None
    is_default: bool | None = None
    priority: int | None = Field(None, ge=1, le=10000)
    applies_to: dict | None = None
    config: dict | None = None
    active: bool | None = None
    change_note: str | None = Field(None, max_length=300)


@router.get("/rules")
def list_rules(history: bool = False, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    rows = service.rule_rows(db, user["company_id"], current_only=not history)
    return [service.rule_out(r) for r in rows]


@router.get("/rules/defaults")
def rule_defaults(user: dict = Depends(VIEW)):
    return engine.merged_rule({})


@router.post("/rules", status_code=201)
def create_rule(req: RuleIn, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    r = service.create_rule(db, user["company_id"], req.model_dump(exclude_none=True))
    db.commit()
    return service.rule_out(r)


@router.get("/rules/{uuid}")
def get_rule(uuid: UUID, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    r = service.get_rule(db, user["company_id"], uuid)
    versions = (db.query(AttendanceRule).filter(AttendanceRule.company_id == user["company_id"],
                                                AttendanceRule.lineage_id == r.lineage_id)
                .order_by(AttendanceRule.version.desc()).all())
    return {**service.rule_out(r), "versions": [service.rule_out(v) for v in versions]}


@router.put("/rules/{uuid}")
def update_rule(uuid: UUID, req: RuleIn, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    r = service.get_rule(db, user["company_id"], uuid)
    if not r.is_current:
        raise BadRequest("Only the current version can be changed.")
    out = service.new_rule_version(db, user["company_id"], r, req.model_dump(exclude_none=True))
    db.commit()
    return service.rule_out(out)


# ── Settings ────────────────────────────────────────────────────────

class SettingIn(BaseModel):
    config: dict
    effective_from: date | None = None
    note: str | None = Field(None, max_length=300)


@router.get("/settings")
def all_settings(user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    cid = user["company_id"]
    td = service.today(db, cid)
    return {k: {"label": settings_store.LABELS.get(k, k), "config": settings_store.get(db, cid, k, td)}
            for k in settings_store.DEFAULTS}


@router.get("/settings/{key}")
def get_setting(key: str, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    cid = user["company_id"]
    rows = settings_store.history(db, cid, key)
    return {"key": key, "label": settings_store.LABELS.get(key, key),
            "config": settings_store.get(db, cid, key, service.today(db, cid)),
            "versions": [{"uuid": str(r.uuid), "version": r.version, "effective_from": r.effective_from.isoformat(),
                          "change_note": r.change_note, "created_by_name": r.created_by_name,
                          "created_at": iso_utc(r.created_at)} for r in rows]}


@router.put("/settings/{key}")
def save_setting(key: str, req: SettingIn, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    cid = user["company_id"]
    _validate_setting(key, req.config)
    row = settings_store.save(db, cid, key, req.config, req.effective_from or service.today(db, cid), req.note)
    db.commit()
    return {"key": key, "version": row.version, "config": settings_store.get(db, cid, key, service.today(db, cid))}


def _validate_setting(key: str, cfg: dict) -> None:
    def num(name, lo, hi):
        if name in cfg and cfg[name] is not None:
            try:
                v = float(cfg[name])
            except (TypeError, ValueError):
                raise BadRequest("Enter numbers only.")
            if not (lo <= v <= hi):
                raise BadRequest(f"{name.replace('_', ' ').capitalize()} must be between {lo} and {hi}.")
    if key == "regularisation":
        num("monthly_limit", 0, 31), num("window_days", 0, 365)
    elif key == "wfh":
        num("weekly_limit", 0, 7), num("monthly_limit", 0, 31), num("allow_past_days", 0, 60)
    elif key == "permission":
        num("monthly_count", 0, 31), num("monthly_minutes", 0, 6000), num("max_minutes_per_request", 0, 600)
    elif key == "rest_rules":
        for k in ("max_daily_hours", "max_weekly_hours", "min_rest_hours", "max_consecutive_days", "max_consecutive_nights"):
            r = cfg.get(k) or {}
            if r and r.get("action") not in (None, "block", "warn", "off"):
                raise BadRequest("Each rule must block, warn or be off.")
        if (cfg.get("women_night_consent") or {}).get("action") == "off":
            raise BadRequest("Women's night-shift consent is required by law and can't be switched off.")
    elif key == "lop_control":
        if cfg.get("cap_days") not in (None, ""):
            num("cap_days", 0, 31)
    elif key == "auto_shift":
        num("window_minutes", 15, 360)
    elif key == "billing":
        num("gst_rate", 0, 28), num("payment_terms_days", 0, 365)
        st = str(cfg.get("supplier_state") or "")
        if st and not (st.isdigit() and 1 <= int(st) <= 38):
            raise BadRequest("Choose the state the company is registered in for GST.")
        gstin = (cfg.get("supplier_gstin") or "").strip().upper()
        if gstin and len(gstin) != 15:
            raise BadRequest("A GSTIN has 15 characters.")
    elif key == "leave_link":
        ab, co = cfg.get("absence") or {}, cfg.get("comp_off") or {}
        if ab.get("action") not in (None, "leave", "lop"):
            raise BadRequest("Absent days become leave or loss of pay.")
        for name, v, lo, hi in (("Days to wait", ab.get("after_days"), 0, 31),
                                ("Hours for a full day of comp off", co.get("full_day_hours"), 1, 24),
                                ("Hours for a half day of comp off", co.get("half_day_hours"), 0.5, 24)):
            if v is not None:
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    raise BadRequest(f"{name}: enter a number.")
                if not lo <= f <= hi:
                    raise BadRequest(f"{name} must be between {lo:g} and {hi:g}.")
        if co and float(co.get("half_day_hours") or 4) > float(co.get("full_day_hours") or 6):
            raise BadRequest("Half-day comp off can't need more hours than a full day.")
    elif key == "clock_in":
        for ip in cfg.get("allowed_ips") or []:
            import ipaddress
            try:
                ipaddress.ip_network(str(ip).strip(), strict=False)
            except ValueError:
                raise BadRequest(f"{ip} is not a valid IP address or range.")


# ── Geo-fences ──────────────────────────────────────────────────────

class FenceIn(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    address: str | None = Field(None, max_length=300)
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    radius_m: int = Field(200, ge=20, le=10000)
    active: bool = True


def _fence_out(g: Geofence) -> dict:
    return {"uuid": str(g.uuid), "name": g.name, "address": g.address, "latitude": g.latitude,
            "longitude": g.longitude, "radius_m": g.radius_m, "active": g.active}


@router.get("/geofences")
def list_fences(user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    rows = db.query(Geofence).filter(Geofence.company_id == user["company_id"]).order_by(Geofence.name).all()
    from modules.shifts import adapters
    return {"items": [_fence_out(g) for g in rows], "locations": adapters.location_fences(db, user["company_id"])}


@router.post("/geofences", status_code=201)
def add_fence(req: FenceIn, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    g = Geofence(company_id=user["company_id"], **req.model_dump())
    db.add(g)
    db.commit()
    return _fence_out(g)


@router.put("/geofences/{uuid}")
def edit_fence(uuid: UUID, req: FenceIn, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    g = db.query(Geofence).filter(Geofence.company_id == user["company_id"], Geofence.uuid == uuid).first()
    if not g:
        raise NotFound("Office location")
    for k, v in req.model_dump().items():
        setattr(g, k, v)
    db.commit()
    return _fence_out(g)


# ── Devices ─────────────────────────────────────────────────────────

class DeviceIn(BaseModel):
    name: str | None = Field(None, max_length=120)
    vendor: str | None = None
    model: str | None = None
    serial_number: str | None = None
    kind: str | None = None
    location_name: str | None = None
    timezone: str | None = None
    connection: str | None = None
    active: bool | None = None
    ftp_config: dict | None = None
    column_mapping: dict | None = None
    source_config: dict | None = None
    runs_on: str | None = None


@router.get("/devices")
def list_devices(user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    from sqlalchemy import func
    cid = user["company_id"]
    counts = dict(db.query(DeviceEnrolment.device_id, func.count(DeviceEnrolment.id))
                  .filter(DeviceEnrolment.company_id == cid).group_by(DeviceEnrolment.device_id).all())
    general = counts.get(None, 0)
    rows = db.query(Device).filter(Device.company_id == cid).order_by(Device.name).all()
    return {"items": [devices.device_out(d, counts.get(d.id, 0) + general) for d in rows],
            "vendors": devices.VENDORS, "kinds": devices.KINDS, "connections": devices.CONNECTIONS,
            "push_url": "/api/v1/attendance/devices/push"}


@router.post("/devices", status_code=201)
def add_device(req: DeviceIn, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    d, key = devices.save(db, user["company_id"], req.model_dump(exclude_unset=True))
    db.commit()
    return {**devices.device_out(d), "api_key": key}


@router.get("/devices/{uuid}")
def get_device(uuid: UUID, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    return devices.device_out(devices.get(db, user["company_id"], uuid))


@router.put("/devices/{uuid}")
def edit_device(uuid: UUID, req: DeviceIn, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    d = devices.get(db, user["company_id"], uuid)
    d, key = devices.save(db, user["company_id"], req.model_dump(exclude_unset=True), d)
    db.commit()
    return {**devices.device_out(d), "api_key": key}


@router.post("/devices/{uuid}/key")
def regenerate_key(uuid: UUID, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    d = devices.get(db, user["company_id"], uuid)
    key = devices.new_key(d)
    audit.event(db, "attendance.device_key_changed", "attendance_devices", d.id, {}, entity_uuid=str(d.uuid),
                entity_label=d.name)
    db.commit()
    return {"api_key": key, "api_key_prefix": d.api_key_prefix}


@router.post("/devices/{uuid}/pull")
def pull_now(uuid: UUID, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    from modules.attendance import device_sources
    d = devices.get(db, user["company_id"], uuid)
    if d.connection == "ftp":
        logs = devices.poll_device(db, d)
    elif d.connection in device_sources.PULLED:
        if d.runs_on == "connector":
            raise BadRequest("The office connector reads this device; it sends new punches on its own schedule.")
        logs = device_sources.pull(db, d)
    else:
        raise BadRequest("This device sends its punches to us; there is nothing to pull.")
    db.commit()
    return {"logs": [devices.log_out(l, d.name) for l in logs]}


class PushPunch(BaseModel):
    device_user_id: str = Field(..., max_length=60)
    timestamp: str = Field(..., max_length=40)
    direction: str | None = Field(None, max_length=20)
    terminal: str | None = Field(None, max_length=80)  # reader / door id, matched to a device's serial or name


class PushIn(BaseModel):
    punches: list[PushPunch] = Field(..., max_length=devices.MAX_BATCH)


@router.post("/devices/push")
def device_push(req: PushIn, request: Request, x_device_key: str | None = Header(None),
                authorization: str | None = Header(None), db: Session = Depends(get_db)):
    """For attendance devices and their middleware: post punches with the device's key in
    the X-Device-Key header (or 'Authorization: Device <key>'). Repeated punches are ignored."""
    from core import context
    key = x_device_key or ((authorization or "")[7:].strip() if (authorization or "").lower().startswith("device ") else None)
    d = devices.device_for_key(db, key)
    if d is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Unknown or inactive device key.")
    ctx = context.current()
    ctx.actor_type, ctx.actor_name = "system", f"Device: {d.name}"
    set_tenant(db, d.company_id)
    from modules.attendance import device_sources
    kind = "connector" if d.runs_on == "connector" else "push"
    records = [{**p.model_dump(exclude={"terminal"}), "_terminal": p.terminal} for p in req.punches]
    logs = device_sources.ingest_records(db, d, records, kind)
    db.commit()
    return {"received": len(req.punches), "inserted": sum(l.inserted for l in logs),
            "duplicates": sum(l.duplicates for l in logs), "unmatched": sum(l.unmatched for l in logs),
            "errors": [e for l in logs for e in (l.errors or [])][:100]}


@router.post("/imports/preview")
async def import_preview(file: UploadFile = File(...), user: dict = Depends(SETTINGS)):
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise BadRequest("The file is larger than 10 MB. Split it into smaller files.")
    return devices.preview(content, file.filename or "upload.csv")


@router.post("/imports")
async def import_punches(file: UploadFile = File(...), mapping: str = Form(...), device_uuid: str | None = Form(None),
                         user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise BadRequest("The file is larger than 10 MB. Split it into smaller files.")
    try:
        m = json.loads(mapping)
    except ValueError:
        raise BadRequest("The column mapping is not valid.")
    d = devices.get(db, user["company_id"], device_uuid) if device_uuid else None
    entry = devices.import_file(db, user["company_id"], d, content, file.filename or "upload.csv", m)
    db.commit()
    return devices.log_out(entry, d.name if d else None)


class EnrolIn(BaseModel):
    device_uuid: str | None = None
    device_user_id: str = Field(..., min_length=1, max_length=60)
    employee_id: int
    card_number: str | None = Field(None, max_length=60)


@router.get("/enrolments")
def list_enrolments(device_uuid: str | None = None, q: str | None = None, pages: PageParams = Depends(),
                    user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    from modules.employees.models import Employee
    from modules.employees import service as emp_service
    cid = user["company_id"]
    query = (db.query(DeviceEnrolment, Employee).join(Employee, Employee.id == DeviceEnrolment.employee_id)
             .filter(DeviceEnrolment.company_id == cid))
    names = {d.id: d.name for d in db.query(Device).filter(Device.company_id == cid).all()}
    if device_uuid:
        d = devices.get(db, cid, device_uuid)
        query = query.filter((DeviceEnrolment.device_id == d.id) | (DeviceEnrolment.device_id.is_(None)))
    query = emp_service.search(query, q).order_by(DeviceEnrolment.device_user_id)
    return paginate(query, pages, lambda r: {
        "uuid": str(r[0].uuid), "device_id": r[0].device_id, "device_name": names.get(r[0].device_id, "All devices"),
        "device_user_id": r[0].device_user_id, "card_number": r[0].card_number, "employee_id": r[1].id,
        "employee_name": service.name_of(r[1]), "employee_code": r[1].employee_code})


@router.post("/enrolments", status_code=201)
def add_enrolment(req: EnrolIn, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    cid = user["company_id"]
    d = devices.get(db, cid, req.device_uuid) if req.device_uuid else None
    e = devices.enrol(db, cid, d.id if d else None, req.device_user_id, req.employee_id, req.card_number)
    db.commit()
    return {"uuid": str(e.uuid), "device_user_id": e.device_user_id, "employee_id": e.employee_id}


@router.delete("/enrolments/{uuid}")
def remove_enrolment(uuid: UUID, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    e = db.query(DeviceEnrolment).filter(DeviceEnrolment.company_id == user["company_id"],
                                         DeviceEnrolment.uuid == uuid).first()
    if not e:
        raise NotFound("Enrolment")
    db.delete(e)
    db.commit()
    return {"detail": "Removed"}


@router.get("/sync-logs")
def sync_logs(device_uuid: str | None = None, pages: PageParams = Depends(), user: dict = Depends(SETTINGS),
              db: Session = Depends(get_db)):
    cid = user["company_id"]
    q = db.query(DeviceSyncLog).filter(DeviceSyncLog.company_id == cid)
    if device_uuid:
        q = q.filter(DeviceSyncLog.device_id == devices.get(db, cid, device_uuid).id)
    names = {d.id: d.name for d in db.query(Device).filter(Device.company_id == cid).all()}
    return paginate(q.order_by(DeviceSyncLog.created_at.desc()), pages,
                    lambda l: devices.log_out(l, names.get(l.device_id, "File upload")))


# second part of the attendance API (leave links, loss of pay, kiosks, registers)
from modules.attendance.router_extra import router as _extra_router  # noqa: E402

router.include_router(_extra_router, prefix="")

# device connections: presets, tests, vendor push, office connector
from modules.attendance.router_devices import router as _devices_router  # noqa: E402

router.include_router(_devices_router, prefix="")

# the ADMS (iclock) endpoints are called by the devices themselves at the server root
from modules.attendance.device_adms import router as adms_router  # noqa: E402

root_routers = [adms_router]
