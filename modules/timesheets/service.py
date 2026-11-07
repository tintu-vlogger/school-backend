"""Timesheets: clients, projects and tasks; time logs (hours or start-end); weekly
timesheets submitted for approval and approved line by line; reminders.

A week runs Monday to Sunday. Submitting a week sends it through the workflow engine
(request type timesheets.timesheet). The approver can approve or reject each line; a
week with some lines rejected is "partly approved" and the rejected lines can be fixed
and sent again.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from core import audit
from core.errors import BadRequest, Forbidden, NotFound
from core.timeutil import iso_utc
from modules.attendance import service as att
from modules.attendance.engine import parse_hhmm
from modules.timesheets.models import Timesheet, TimeLog, TsClient, TsProject, TsTask

ZERO = Decimal("0.00")
EDITABLE = ("draft", "rejected")


def week_of(d: date) -> tuple[date, date]:
    start = d - timedelta(days=d.weekday())
    return start, start + timedelta(days=6)


def _dec(v) -> Decimal:
    try:
        return Decimal(str(v)).quantize(Decimal("0.01"))
    except Exception:
        raise BadRequest("Enter hours as a number, e.g. 1.5.")


# ── Clients, projects, tasks ────────────────────────────────────────

def save_client(db: Session, company_id: int, data: dict, row: TsClient | None = None) -> TsClient:
    name = (data.get("name") or (row.name if row else "")).strip()
    if not name:
        raise BadRequest("Enter the client's name.")
    if row is None:
        row = TsClient(company_id=company_id)
        db.add(row)
    row.name = name
    for k in ("contact_name", "email", "phone", "gstin", "address", "state_code"):
        if k in data:
            setattr(row, k, (str(data[k] or "")).strip() or None)
    if row.gstin:
        row.gstin = row.gstin.upper()
        if len(row.gstin) != 15 or not row.gstin[:2].isdigit():
            raise BadRequest("A GSTIN has 15 characters and starts with the state code.")
        if not row.state_code:
            row.state_code = row.gstin[:2]
    if row.state_code and not (row.state_code.isdigit() and 1 <= int(row.state_code) <= 38):
        raise BadRequest("Choose the client's state.")
    if row.state_code:
        row.state_code = f"{int(row.state_code):02d}"
    if "active" in data:
        row.active = bool(data["active"])
    db.flush()
    return row


def client_out(c: TsClient) -> dict:
    return {"uuid": str(c.uuid), "id": c.id, "name": c.name, "contact_name": c.contact_name, "email": c.email,
            "phone": c.phone, "gstin": c.gstin, "address": c.address, "state_code": c.state_code, "active": c.active}


def save_project(db: Session, company_id: int, data: dict, row: TsProject | None = None) -> TsProject:
    name = (data.get("name") or (row.name if row else "")).strip()
    if not name:
        raise BadRequest("Enter the project name.")
    if row is None:
        row = TsProject(company_id=company_id)
        db.add(row)
    row.name = name
    if "client_id" in data:
        cid = data.get("client_id")
        if cid and not db.query(TsClient.id).filter(TsClient.company_id == company_id, TsClient.id == int(cid)).first():
            raise BadRequest("That client doesn't exist.")
        row.client_id = int(cid) if cid else None
    for k in ("code", "description"):
        if k in data:
            setattr(row, k, (data[k] or "").strip() or None)
    if "owner_employee_id" in data:
        row.owner_employee_id = int(data["owner_employee_id"]) if data["owner_employee_id"] else None
    if "billable" in data:
        row.billable = bool(data["billable"])
    if "budget_hours" in data:
        row.budget_hours = _dec(data["budget_hours"]) if data["budget_hours"] not in (None, "") else None
    for k in ("start_date", "end_date"):
        if k in data:
            setattr(row, k, data[k])
    if row.start_date and row.end_date and row.end_date < row.start_date:
        raise BadRequest("The end date can't be before the start date.")
    if "status" in data:
        if data["status"] not in ("active", "on_hold", "completed"):
            raise BadRequest("Choose active, on hold or completed.")
        row.status = data["status"]
    if "member_ids" in data:
        row.member_ids = sorted({int(x) for x in data.get("member_ids") or []})
    db.flush()
    return row


def project_out(p: TsProject, logged: Decimal | None = None, names: dict | None = None) -> dict:
    return {"uuid": str(p.uuid), "id": p.id, "name": p.name, "code": p.code, "client_id": p.client_id,
            "client_name": p.client.name if p.client else None, "owner_employee_id": p.owner_employee_id,
            "owner_name": (names or {}).get(p.owner_employee_id), "billable": p.billable,
            "budget_hours": str(p.budget_hours) if p.budget_hours is not None else None,
            "start_date": p.start_date.isoformat() if p.start_date else None,
            "end_date": p.end_date.isoformat() if p.end_date else None, "status": p.status,
            "member_ids": p.member_ids or [], "description": p.description,
            "logged_hours": str(logged if logged is not None else ZERO)}


def save_task(db: Session, company_id: int, project: TsProject, data: dict, row: TsTask | None = None) -> TsTask:
    name = (data.get("name") or (row.name if row else "")).strip()
    if not name:
        raise BadRequest("Enter the task name.")
    if row is None:
        row = TsTask(company_id=company_id, project_id=project.id, billable=project.billable)
        db.add(row)
    row.name = name
    if "owner_employee_id" in data:
        row.owner_employee_id = int(data["owner_employee_id"]) if data["owner_employee_id"] else None
    if "billable" in data:
        row.billable = bool(data["billable"])
    if "budget_hours" in data:
        row.budget_hours = _dec(data["budget_hours"]) if data["budget_hours"] not in (None, "") else None
    if "status" in data:
        row.status = "done" if data["status"] == "done" else "open"
    db.flush()
    return row


def task_out(t: TsTask, logged: Decimal | None = None) -> dict:
    return {"uuid": str(t.uuid), "id": t.id, "project_id": t.project_id, "name": t.name,
            "owner_employee_id": t.owner_employee_id, "billable": t.billable,
            "budget_hours": str(t.budget_hours) if t.budget_hours is not None else None, "status": t.status,
            "logged_hours": str(logged if logged is not None else ZERO)}


def logged_by_project(db: Session, company_id: int) -> dict[int, Decimal]:
    return {pid: h for pid, h in db.query(TimeLog.project_id, func.sum(TimeLog.hours)).filter(
        TimeLog.company_id == company_id, TimeLog.status != "rejected").group_by(TimeLog.project_id).all()}


def logged_by_task(db: Session, company_id: int, project_id: int) -> dict[int, Decimal]:
    return {tid: h for tid, h in db.query(TimeLog.task_id, func.sum(TimeLog.hours)).filter(
        TimeLog.company_id == company_id, TimeLog.project_id == project_id, TimeLog.status != "rejected")
        .group_by(TimeLog.task_id).all()}


def projects_for(db: Session, company_id: int, employee_id: int | None) -> list[TsProject]:
    rows = (db.query(TsProject).options(joinedload(TsProject.client))
            .filter(TsProject.company_id == company_id, TsProject.status == "active").order_by(TsProject.name).all())
    if employee_id is None:
        return rows
    return [p for p in rows if not p.member_ids or employee_id in p.member_ids or p.owner_employee_id == employee_id]


# ── Time logs ───────────────────────────────────────────────────────

def _timesheet(db: Session, company_id: int, employee_id: int, week_start: date, create: bool = False) -> Timesheet | None:
    ts = db.query(Timesheet).filter(Timesheet.company_id == company_id, Timesheet.employee_id == employee_id,
                                    Timesheet.week_start == week_start).first()
    if ts is None and create:
        ts = Timesheet(company_id=company_id, employee_id=employee_id, week_start=week_start,
                       week_end=week_start + timedelta(days=6), status="draft")
        db.add(ts)
        db.flush()
    return ts


def _check_day(db: Session, company_id: int, employee_id: int, d: date) -> None:
    cfg = att.get_setting(db, company_id, "timesheets", d)
    if d > att.today(db, company_id) and not cfg.get("allow_future"):
        raise BadRequest("Time can't be logged for future days.")
    if att.is_locked(db, company_id, d):
        raise BadRequest(f"{d:%d %b} is in a locked pay period.")


def save_log(db: Session, company_id: int, employee_id: int, data: dict, row: TimeLog | None = None) -> TimeLog:
    d = data.get("date") or (row.date if row else None)
    if not isinstance(d, date):
        raise BadRequest("Choose the date.")
    if row is not None and row.status not in EDITABLE:
        raise BadRequest("This entry is already sent for approval.")
    _check_day(db, company_id, employee_id, d)
    pid = data.get("project_id") or (row.project_id if row else None)
    project = db.query(TsProject).filter(TsProject.company_id == company_id, TsProject.id == pid).first() if pid else None
    if project is None:
        raise BadRequest("Choose a project.")
    if project.member_ids and employee_id not in project.member_ids and project.owner_employee_id != employee_id:
        raise Forbidden("You are not on this project.")
    tid = data.get("task_id", row.task_id if row else None)
    task = None
    if tid:
        task = db.query(TsTask).filter(TsTask.company_id == company_id, TsTask.id == int(tid),
                                       TsTask.project_id == project.id).first()
        if task is None:
            raise BadRequest("That task is not part of the project.")
    cfg = att.get_setting(db, company_id, "timesheets", d)
    if cfg.get("require_task") and task is None:
        raise BadRequest("Choose a task.")
    st, et = parse_hhmm(data.get("start_time")) if data.get("start_time") else None, \
        parse_hhmm(data.get("end_time")) if data.get("end_time") else None
    if st and et:
        if et <= st:
            raise BadRequest("The end time must be after the start time.")
        hours = _dec((datetime.combine(d, et) - datetime.combine(d, st)).seconds / 3600)
    else:
        hours = _dec(data.get("hours", row.hours if row else 0))
    if hours <= 0 or hours > 24:
        raise BadRequest("Hours must be more than 0 and at most 24.")
    day_total = sum((l.hours for l in db.query(TimeLog).filter(
        TimeLog.company_id == company_id, TimeLog.employee_id == employee_id, TimeLog.date == d,
        TimeLog.status != "rejected").all() if row is None or l.id != row.id), ZERO)
    mx = float(cfg.get("max_daily_hours") or 24)
    if float(day_total + hours) > mx:
        raise BadRequest(f"That makes {day_total + hours} hours on {d:%d %b}; the limit is {mx:g} a day.")
    if cfg.get("no_logs_on_leave"):
        from modules.attendance.models import AttendanceDay
        status = db.query(AttendanceDay.status).filter(AttendanceDay.company_id == company_id,
                                                       AttendanceDay.employee_id == employee_id,
                                                       AttendanceDay.date == d).scalar()
        if status == "on_leave":
            raise BadRequest(f"You are on leave on {d:%d %b}.")
    if row is None:
        row = TimeLog(company_id=company_id, employee_id=employee_id)
        db.add(row)
    row.date, row.project_id, row.task_id = d, project.id, task.id if task else None
    row.hours, row.start_time, row.end_time = hours, st, et
    row.description = (data.get("description") or "").strip() or None
    row.billable = bool(data.get("billable", task.billable if task else project.billable))
    if row.status == "rejected":
        row.status, row.review_comment = "draft", None
    ts = _timesheet(db, company_id, employee_id, week_of(d)[0], create=True)
    row.timesheet_id = ts.id
    db.flush()
    _total(db, ts)
    return row


def delete_log(db: Session, company_id: int, row: TimeLog) -> None:
    if row.status not in EDITABLE:
        raise BadRequest("This entry is already sent for approval.")
    ts_id = row.timesheet_id
    db.delete(row)
    db.flush()
    ts = db.query(Timesheet).filter(Timesheet.id == ts_id).first() if ts_id else None
    if ts:
        _total(db, ts)


def _total(db: Session, ts: Timesheet) -> None:
    total = db.query(func.coalesce(func.sum(TimeLog.hours), 0)).filter(TimeLog.timesheet_id == ts.id,
                                                                       TimeLog.status != "rejected").scalar()
    ts.total_hours = _dec(total)


def log_out(l: TimeLog) -> dict:
    return {"uuid": str(l.uuid), "id": l.id, "date": l.date.isoformat(), "project_id": l.project_id,
            "project_name": l.project.name if l.project else None, "task_id": l.task_id,
            "task_name": l.task.name if l.task else None, "hours": str(l.hours),
            "start_time": l.start_time.strftime("%H:%M") if l.start_time else None,
            "end_time": l.end_time.strftime("%H:%M") if l.end_time else None, "description": l.description,
            "billable": l.billable, "status": l.status, "review_comment": l.review_comment}


def save_week(db: Session, company_id: int, employee_id: int, week_start: date, rows: list[dict]) -> dict:
    """Save the week grid: each row is a project/task with hours per day. Days with 0
    remove that row's entry. Lines already sent for approval are left alone."""
    week_start = week_of(week_start)[0]
    days = [week_start + timedelta(days=i) for i in range(7)]
    logs = (db.query(TimeLog).filter(TimeLog.company_id == company_id, TimeLog.employee_id == employee_id,
                                     TimeLog.date >= days[0], TimeLog.date <= days[-1]).all())
    by_key = defaultdict(list)
    for l in logs:
        by_key[(l.project_id, l.task_id, l.date)].append(l)
    kept = set()
    for r in rows:
        pid, tid = r.get("project_id"), r.get("task_id") or None
        for d in days:
            raw = (r.get("hours") or {}).get(d.isoformat())
            hours = _dec(raw) if raw not in (None, "") else ZERO
            existing = [l for l in by_key.get((pid, tid, d), []) if l.start_time is None]
            row = existing[0] if existing else None
            if row is not None:
                kept.add(row.id)
            if hours <= 0:
                if row is not None and row.status in EDITABLE:
                    delete_log(db, company_id, row)
                continue
            if row is not None and (row.status not in EDITABLE or row.hours == hours) and \
                    (row.description or None) == ((r.get("description") or "").strip() or None):
                continue
            save_log(db, company_id, employee_id, {"date": d, "project_id": pid, "task_id": tid, "hours": hours,
                                                   "description": r.get("description"),
                                                   "billable": r.get("billable", True)}, row)
    for l in logs:  # rows removed from the grid
        if l.id not in kept and l.start_time is None and l.status in EDITABLE and \
                not any((l.project_id, l.task_id) == (r.get("project_id"), r.get("task_id") or None) for r in rows):
            delete_log(db, company_id, l)
    db.flush()
    return week(db, company_id, employee_id, week_start)


def week(db: Session, company_id: int, employee_id: int, week_start: date) -> dict:
    ws, we = week_of(week_start)
    logs = (db.query(TimeLog).options(joinedload(TimeLog.project), joinedload(TimeLog.task))
            .filter(TimeLog.company_id == company_id, TimeLog.employee_id == employee_id,
                    TimeLog.date >= ws, TimeLog.date <= we).order_by(TimeLog.date, TimeLog.id).all())
    ts = _timesheet(db, company_id, employee_id, ws)
    rows = {}
    for l in logs:
        key = f"{l.project_id}:{l.task_id or ''}"
        r = rows.setdefault(key, {"key": key, "project_id": l.project_id, "project_name": l.project.name,
                                  "task_id": l.task_id, "task_name": l.task.name if l.task else None,
                                  "billable": l.billable, "description": l.description, "hours": {}, "logs": {}})
        d = l.date.isoformat()
        r["hours"][d] = str(_dec(Decimal(r["hours"].get(d, "0")) + l.hours))
        r["logs"].setdefault(d, []).append(log_out(l))
    from modules.attendance.models import AttendanceDay
    days = {a.date: a for a in db.query(AttendanceDay).filter(
        AttendanceDay.company_id == company_id, AttendanceDay.employee_id == employee_id,
        AttendanceDay.date >= ws, AttendanceDay.date <= we).all()}
    day_info = []
    for i in range(7):
        d = ws + timedelta(days=i)
        a = days.get(d)
        day_info.append({"date": d.isoformat(), "status": a.status if a else None,
                         "worked_minutes": a.worked_minutes if a else 0,
                         "logged": str(sum((l.hours for l in logs if l.date == d and l.status != "rejected"), ZERO))})
    projects = projects_for(db, company_id, employee_id)
    tasks = db.query(TsTask).filter(TsTask.company_id == company_id, TsTask.project_id.in_([p.id for p in projects] or [-1]),
                                    TsTask.status == "open").order_by(TsTask.name).all()
    return {"week_start": ws.isoformat(), "week_end": we.isoformat(),
            "timesheet": timesheet_out(ts) if ts else None, "rows": list(rows.values()), "days": day_info,
            "total": str(sum((l.hours for l in logs if l.status != "rejected"), ZERO)),
            "projects": [{"id": p.id, "name": p.name, "client_name": p.client.name if p.client else None,
                          "billable": p.billable} for p in projects],
            "tasks": [{"id": t.id, "project_id": t.project_id, "name": t.name, "billable": t.billable} for t in tasks],
            "editable": not ts or ts.status in ("draft", "rejected", "partly_approved")}


def timesheet_out(ts: Timesheet, name: str | None = None) -> dict:
    return {"uuid": str(ts.uuid), "employee_id": ts.employee_id, "employee_name": name,
            "week_start": ts.week_start.isoformat(), "week_end": ts.week_end.isoformat(), "status": ts.status,
            "total_hours": str(ts.total_hours), "submitted_at": iso_utc(ts.submitted_at),
            "decided_at": iso_utc(ts.decided_at), "comment": ts.comment}


# ── Submit and approve ──────────────────────────────────────────────

def submit_week(db: Session, company_id: int, employee, week_start: date, comment: str | None = None) -> Timesheet:
    from core.workflow import service as workflow
    from core.workflow.models import WorkflowInstance
    ws, we = week_of(week_start)
    ts = _timesheet(db, company_id, employee.id, ws, create=True)
    lines = db.query(TimeLog).filter(TimeLog.timesheet_id == ts.id, TimeLog.status.in_(EDITABLE)).all()
    if not lines:
        raise BadRequest("There is nothing new to send. Log some time first.")
    if ts.workflow_instance_id:
        inst = db.query(WorkflowInstance).filter(WorkflowInstance.id == ts.workflow_instance_id).first()
        if inst and inst.status == "pending":
            raise BadRequest("This week is already waiting for approval.")
    cfg = att.get_setting(db, company_id, "timesheets", ws)
    _total(db, ts)
    if float(cfg.get("min_weekly_hours") or 0) and float(ts.total_hours) < float(cfg["min_weekly_hours"]):
        raise BadRequest(f"Log at least {cfg['min_weekly_hours']} hours before sending the week.")
    for l in lines:
        l.status, l.review_comment = "submitted", None
    ts.status, ts.submitted_at, ts.comment = "submitted", datetime.now(timezone.utc), comment
    db.flush()
    payload = {"timesheet_id": ts.id, "employee_id": employee.id, "week_start": ws.isoformat(),
               "week_end": we.isoformat(), "hours": float(ts.total_hours), "lines": len(lines)}
    inst = workflow.submit(db, company_id, "timesheets.timesheet", payload,
                           f"Timesheet: {att.name_of(employee)}, week of {ws:%d %b}",
                           subject_type="ts_timesheets", subject_id=ts.id)
    ts.workflow_instance_id = inst.id
    db.flush()
    return ts


def _finish_lines(db: Session, ts: Timesheet, default: str) -> None:
    for l in db.query(TimeLog).filter(TimeLog.timesheet_id == ts.id, TimeLog.status == "submitted").all():
        l.status = default
    statuses = {s for (s,) in db.query(TimeLog.status).filter(TimeLog.timesheet_id == ts.id).all()}
    if statuses <= {"approved"}:
        ts.status = "approved"
    elif "approved" in statuses:
        ts.status = "partly_approved"
    else:
        ts.status = "rejected"
    ts.decided_at = datetime.now(timezone.utc)
    _total(db, ts)


def on_approved(db: Session, instance) -> None:
    ts = db.query(Timesheet).filter(Timesheet.company_id == instance.company_id,
                                    Timesheet.id == (instance.payload or {}).get("timesheet_id")).first()
    if ts:
        _finish_lines(db, ts, "approved")


def on_rejected(db: Session, instance) -> None:
    ts = db.query(Timesheet).filter(Timesheet.company_id == instance.company_id,
                                    Timesheet.id == (instance.payload or {}).get("timesheet_id")).first()
    if ts:
        for l in db.query(TimeLog).filter(TimeLog.timesheet_id == ts.id, TimeLog.status == "submitted").all():
            l.status, l.review_comment = "rejected", instance.result_note
        _finish_lines(db, ts, "rejected")


def on_cancelled(db: Session, instance) -> None:
    ts = db.query(Timesheet).filter(Timesheet.company_id == instance.company_id,
                                    Timesheet.id == (instance.payload or {}).get("timesheet_id")).first()
    if ts:
        for l in db.query(TimeLog).filter(TimeLog.timesheet_id == ts.id, TimeLog.status == "submitted").all():
            l.status = "draft"
        ts.status = "draft"


def my_pending_task(db: Session, company_id: int, ts: Timesheet):
    from core.workflow import service as workflow
    from core.workflow.models import WorkflowTask
    if not ts.workflow_instance_id:
        return None
    tasks = db.query(WorkflowTask).filter(WorkflowTask.company_id == company_id,
                                          WorkflowTask.instance_id == ts.workflow_instance_id,
                                          WorkflowTask.status == "pending").all()
    return next((t for t in tasks if workflow.is_my_task(t)), None)


def review(db: Session, company_id: int, ts: Timesheet, lines: list[dict], comment: str | None) -> Timesheet:
    """Approve or reject each line, then decide the approval task."""
    from core.workflow import service as workflow
    task = my_pending_task(db, company_id, ts)
    if task is None:
        raise Forbidden("This timesheet is not waiting for your approval.")
    logs = {str(l.uuid): l for l in db.query(TimeLog).filter(TimeLog.timesheet_id == ts.id,
                                                              TimeLog.status == "submitted").all()}
    decisions = {}
    for item in lines or []:
        l = logs.get(str(item.get("log_uuid")))
        if l is None:
            continue
        dec = item.get("decision")
        if dec not in ("approve", "reject"):
            raise BadRequest("Choose approve or reject for each line.")
        if dec == "reject" and not (item.get("comment") or comment or "").strip():
            raise BadRequest("Say why a line is rejected.")
        decisions[l.id] = (dec, (item.get("comment") or "").strip() or None)
    for l in logs.values():
        dec, note = decisions.get(l.id, ("approve", None))
        l.status = "approved" if dec == "approve" else "rejected"
        l.review_comment = note or (comment if dec == "reject" else None)
    db.flush()
    any_ok = any(l.status == "approved" for l in logs.values())
    rejected = sum(1 for l in logs.values() if l.status == "rejected")
    note = (comment or "").strip() or (f"{rejected} line(s) sent back" if rejected and any_ok else None)
    if not any_ok and not note:
        note = "All lines rejected"
    workflow.decide(db, company_id, task, "approve" if any_ok else "reject", note)
    audit.event(db, "timesheet.reviewed", "ts_timesheets", ts.id,
                {"approved": sum(1 for l in logs.values() if l.status == "approved"), "rejected": rejected},
                entity_uuid=str(ts.uuid))
    db.flush()
    return ts


def approvals(db: Session, company_id: int) -> list[Timesheet]:
    """Timesheets waiting for the signed-in person's approval."""
    from core.workflow import service as workflow
    from core.workflow.models import WorkflowTask
    tasks = (db.query(WorkflowTask).join(Timesheet, Timesheet.workflow_instance_id == WorkflowTask.instance_id)
             .filter(WorkflowTask.company_id == company_id, WorkflowTask.status == "pending").all())
    inst_ids = {t.instance_id for t in tasks if workflow.is_my_task(t)}
    if not inst_ids:
        return []
    return (db.query(Timesheet).filter(Timesheet.company_id == company_id, Timesheet.workflow_instance_id.in_(inst_ids))
            .order_by(Timesheet.week_start.desc()).all())


def timesheet_detail(db: Session, company_id: int, ts: Timesheet) -> dict:
    logs = (db.query(TimeLog).options(joinedload(TimeLog.project), joinedload(TimeLog.task))
            .filter(TimeLog.timesheet_id == ts.id).order_by(TimeLog.date, TimeLog.id).all())
    emp = att.employee(db, company_id, ts.employee_id)
    return {**timesheet_out(ts, att.name_of(emp)), "lines": [log_out(l) for l in logs],
            "can_review": my_pending_task(db, company_id, ts) is not None}


# ── Reminders ───────────────────────────────────────────────────────

def send_reminders(db: Session, company_id: int, week_start: date) -> int:
    """Remind people who log time (anyone on a project) about a week not yet sent."""
    from modules.notifications.models import Notification
    ws, we = week_of(week_start)
    projects = db.query(TsProject).filter(TsProject.company_id == company_id, TsProject.status == "active").all()
    if not projects:
        return 0
    from modules.employees.models import Employee
    everyone = any(not p.member_ids for p in projects)
    ids = {i for p in projects for i in (p.member_ids or [])} | {p.owner_employee_id for p in projects if p.owner_employee_id}
    q = db.query(Employee.id).filter(Employee.company_id == company_id, Employee.active.is_(True))
    if not everyone:
        q = q.filter(Employee.id.in_(ids or [-1]))
    sent = {ts.employee_id for ts in db.query(Timesheet).filter(Timesheet.company_id == company_id,
                                                                 Timesheet.week_start == ws,
                                                                 Timesheet.status != "draft").all()}
    key = f"Timesheet for the week of {ws:%d %b}"
    already = {e for (e,) in db.query(Notification.employee_id).filter(
        Notification.company_id == company_id, Notification.message_type == "timesheet_reminder",
        Notification.message_content.like(f"{key}%")).all()}
    n = 0
    for (eid,) in q.all():
        if eid in sent or eid in already:
            continue
        db.add(Notification(company_id=company_id, employee_id=eid, channel="in_app", message_type="timesheet_reminder",
                            message_content=f"{key} is not sent yet. Log your hours and send it for approval.",
                            status="sent", sent_at=datetime.now(timezone.utc)))
        n += 1
    db.flush()
    return n


def reminders_job(db: Session) -> int:
    """Periodic job: on each company's reminder weekday, remind about the current week."""
    from modules.company.models import Company
    n = 0
    for (cid,) in db.query(Company.id).filter(Company.active.is_(True)).all():
        d = att.today(db, cid)
        cfg = att.get_setting(db, cid, "timesheets", d)
        if d.weekday() == int(cfg.get("reminder_weekday", 4)) and att.now_local(db, cid).hour >= 15:
            db.info["company_id"] = cid
            n += send_reminders(db, cid, d)
    db.info.pop("company_id", None)
    return n


def project_report(db: Session, company_id: int, start: date, end: date) -> list[dict]:
    rows = (db.query(TimeLog.project_id, TimeLog.billable, func.sum(TimeLog.hours))
            .filter(TimeLog.company_id == company_id, TimeLog.date >= start, TimeLog.date <= end,
                    TimeLog.status != "rejected").group_by(TimeLog.project_id, TimeLog.billable).all())
    projects = {p.id: p for p in db.query(TsProject).options(joinedload(TsProject.client))
                .filter(TsProject.company_id == company_id).all()}
    total = logged_by_project(db, company_id)
    agg = defaultdict(lambda: {"billable": ZERO, "non_billable": ZERO})
    for pid, billable, hours in rows:
        agg[pid]["billable" if billable else "non_billable"] += hours
    out = []
    for pid, a in agg.items():
        p = projects.get(pid)
        out.append({"project_id": pid, "project_name": p.name if p else "", "client_name": p.client.name if p and p.client else None,
                    "billable_hours": str(a["billable"]), "non_billable_hours": str(a["non_billable"]),
                    "budget_hours": str(p.budget_hours) if p and p.budget_hours is not None else None,
                    "total_logged_hours": str(total.get(pid, ZERO))})
    return sorted(out, key=lambda x: x["project_name"])
