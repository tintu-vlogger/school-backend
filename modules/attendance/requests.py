"""Employee requests that change attendance, each approved through the workflow engine:

regularisation  fix a missed or wrong punch (monthly limit, time window)
on_duty         working away from the office (client visit, training, field work)
wfh             work from home, with categories from a master and weekly/monthly limits
permission      short leave in hours, with a monthly limit
overtime        overtime pre-approval; records the worker's consent (OSH Code)

The request row is kept in attendance_requests (for limits and the daily calculation);
the workflow instance carries the approvals. Nothing changes until approval.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from core import audit
from core.errors import BadRequest, NotFound
from modules.attendance import service
from modules.attendance.engine import parse_hhmm
from modules.attendance.models import AttendanceRequest, Punch
from modules.shifts import adapters
from modules.shifts import service as shifts_service

WORKFLOW_KEYS = {k: f"attendance.{k}" for k in service.REQUEST_KINDS}
ACTIVE = ("pending", "approved")
DURATIONS = {"full_day": 1.0, "first_half": 0.5, "second_half": 0.5}


def _time(value, label: str) -> time | None:
    if value in (None, ""):
        return None
    try:
        return parse_hhmm(value)
    except (TypeError, ValueError):
        raise BadRequest(f"Enter a valid {label} (HH:MM).")


def _master_ok(db: Session, company_id: int, key: str, value_id) -> int | None:
    if not value_id:
        return None
    from core.masters import MasterType, MasterValue
    ok = (db.query(MasterValue.id).join(MasterType, MasterType.id == MasterValue.type_id)
          .filter(MasterValue.company_id == company_id, MasterValue.id == int(value_id), MasterType.key == key,
                  MasterValue.active.is_(True)).first())
    if not ok:
        raise BadRequest("Choose a category from the list.")
    return int(value_id)


def _month(d: date) -> tuple[date, date]:
    return service.month_range(d.year, d.month)


def _overlapping(db: Session, company_id: int, employee_id: int, kind: str, start: date, end: date):
    return (db.query(AttendanceRequest).filter(
        AttendanceRequest.company_id == company_id, AttendanceRequest.employee_id == employee_id,
        AttendanceRequest.kind == kind, AttendanceRequest.status.in_(ACTIVE), AttendanceRequest.date <= end,
        func.coalesce(AttendanceRequest.end_date, AttendanceRequest.date) >= start))


def _days_used(db: Session, company_id: int, employee_id: int, kind: str, start: date, end: date) -> float:
    total = 0.0
    for r in _overlapping(db, company_id, employee_id, kind, start, end).all():
        for d in adapters.daterange(max(r.date, start), min(r.end_date or r.date, end)):
            total += DURATIONS.get(r.duration or "full_day", 1.0)
    return total


def _working_days(db: Session, company_id: int, employee_id: int, start: date, end: date) -> list[date]:
    plans = shifts_service.day_plans(db, company_id, [employee_id], start, end)
    return [d for d in adapters.daterange(start, end) if plans[(employee_id, d)].day_type == "working"]


def create(db: Session, company_id: int, employee, kind: str, data: dict):
    """Check the request against the settings, save it and start the approval."""
    from core.workflow import service as workflow
    if kind not in service.REQUEST_KINDS:
        raise BadRequest("Unknown kind of request.")
    td = service.today(db, company_id)
    d = data.get("date")
    if not isinstance(d, date):
        raise BadRequest("Choose the date.")
    end = data.get("end_date") or d
    if end < d:
        raise BadRequest("The end date can't be before the start date.")
    if (end - d).days > 31:
        raise BadRequest("A request can cover up to 31 days. Send another for the rest.")
    reason = (data.get("reason") or "").strip() or None
    req = AttendanceRequest(company_id=company_id, employee_id=employee.id, kind=kind, date=d, reason=reason,
                            status="pending", details={})
    title_bits = []
    if kind == "regularisation":
        _regularisation(db, company_id, employee, req, data, td)
        title_bits.append(f"{d:%d %b}")
    elif kind == "on_duty":
        _on_duty(db, company_id, employee, req, data, td, end)
        title_bits.append(f"{d:%d %b}" + (f" to {end:%d %b}" if end != d else ""))
    elif kind == "wfh":
        _wfh(db, company_id, employee, req, data, td, end)
        title_bits.append(f"{d:%d %b}" + (f" to {end:%d %b}" if end != d else ""))
    elif kind == "permission":
        _permission(db, company_id, employee, req, data, td)
        title_bits.append(f"{d:%d %b}, {req.minutes} min")
    elif kind == "overtime":
        _overtime(db, company_id, employee, req, data, td)
        title_bits.append(f"{d:%d %b}, {Decimal(req.minutes) / 60:.1f} h")
    db.add(req)
    db.flush()
    title = f"{service.REQUEST_KINDS[kind]}: {service.name_of(employee)}, {' '.join(title_bits)}"
    payload = {"request_id": req.id, "employee_id": employee.id, "kind": kind, "date": d.isoformat(),
               "end_date": (req.end_date or d).isoformat(), "days": _span_days(req), "minutes": req.minutes,
               "duration": req.duration, "reason": reason, "details": req.details}
    inst = workflow.submit(db, company_id, WORKFLOW_KEYS[kind], payload, title,
                           subject_type="attendance_requests", subject_id=req.id)
    req.workflow_instance_id = inst.id
    db.flush()
    return req, inst


def _span_days(req: AttendanceRequest) -> float:
    n = ((req.end_date or req.date) - req.date).days + 1
    return n * DURATIONS.get(req.duration or "full_day", 1.0) if req.kind in ("wfh", "on_duty") else 1


def _regularisation(db, company_id, employee, req, data, td) -> None:
    cfg = service.get_setting(db, company_id, "regularisation", req.date)
    d = req.date
    if d > td and not cfg.get("allow_future"):
        raise BadRequest("You can't correct attendance for a future date.")
    window = int(cfg.get("window_days") or 0)
    if window and (td - d).days > window:
        raise BadRequest(f"Corrections must be asked for within {window} days of the date.")
    if cfg.get("reason_required", True) and not req.reason:
        raise BadRequest("Give a reason for the correction.")
    t_in, t_out = _time(data.get("in_time"), "check-in time"), _time(data.get("out_time"), "check-out time")
    if t_in is None and t_out is None:
        raise BadRequest("Enter the check-in time, the check-out time or both.")
    plan = shifts_service.day_plans(db, company_id, [employee.id], d, d)[(employee.id, d)]
    sp = shifts_service.spec(plan.shift)
    night = bool(sp and sp.crosses_midnight)
    out_next = bool(data.get("out_next_day")) or bool(night and t_out and t_in and t_out <= t_in) \
        or bool(night and t_out and not t_in and t_out < sp.start)
    if t_in and t_out and not out_next and t_out <= t_in:
        raise BadRequest("Check-out must be after check-in (tick 'next day' for night shifts).")
    limit = int(cfg.get("monthly_limit") or 0)
    if limit:
        ms, me = _month(d)
        used = (db.query(AttendanceRequest.id).filter(
            AttendanceRequest.company_id == company_id, AttendanceRequest.employee_id == employee.id,
            AttendanceRequest.kind == "regularisation", AttendanceRequest.status.in_(ACTIVE),
            AttendanceRequest.date >= ms, AttendanceRequest.date <= me).count())
        if used >= limit:
            raise BadRequest(f"You have used all {limit} corrections allowed for {d:%B}.")
    if _overlapping(db, company_id, employee.id, "regularisation", d, d).filter(AttendanceRequest.status == "pending").first():
        raise BadRequest("A correction for this day is already waiting for approval.")
    mode = data.get("mode") if data.get("mode") in ("add", "replace") else "add"
    req.from_time, req.to_time = t_in, t_out
    req.details = {"in": t_in.strftime("%H:%M") if t_in else None, "out": t_out.strftime("%H:%M") if t_out else None,
                   "out_next_day": out_next, "mode": mode, "reason_code": data.get("reason_code")}


def _on_duty(db, company_id, employee, req, data, td, end) -> None:
    cfg = service.get_setting(db, company_id, "on_duty", req.date)
    if req.date > td and not cfg.get("allow_future", True):
        raise BadRequest("On duty can't be asked for future dates.")
    past = int(cfg.get("allow_past_days") or 0)
    if past and (td - req.date).days > past:
        raise BadRequest(f"On duty for past days must be asked for within {past} days.")
    dur = data.get("duration") or "full_day"
    if dur not in DURATIONS:
        raise BadRequest("Choose full day, first half or second half.")
    if dur != "full_day" and end != req.date:
        raise BadRequest("Half-day on duty is for a single day.")
    if not req.reason:
        raise BadRequest("Say where you will be and why.")
    if _overlapping(db, company_id, employee.id, "on_duty", req.date, end).first():
        raise BadRequest("You already have on duty for some of these days.")
    limit = int(cfg.get("monthly_limit") or 0)
    if limit:
        ms, me = _month(req.date)
        used = _days_used(db, company_id, employee.id, "on_duty", ms, me)
        if used + DURATIONS[dur] * ((end - req.date).days + 1) > limit:
            raise BadRequest(f"On duty is limited to {limit} days a month; you have used {used:g}.")
    req.end_date = end if end != req.date else None
    req.duration = dur
    req.category_id = _master_ok(db, company_id, "on_duty_type", data.get("category_id"))
    req.details = {"place": data.get("place")}


def _wfh(db, company_id, employee, req, data, td, end) -> None:
    cfg = service.get_setting(db, company_id, "wfh", req.date)
    past = int(cfg.get("allow_past_days") or 0)
    if (td - req.date).days > past:
        raise BadRequest(f"Work from home for past days must be asked for within {past} days." if past
                         else "Work from home must be asked for in advance.")
    dur = data.get("duration") or "full_day"
    if dur not in DURATIONS:
        raise BadRequest("Choose full day, first half or second half.")
    if dur != "full_day" and end != req.date:
        raise BadRequest("Half-day work from home is for a single day.")
    blocked = set(int(x) for x in cfg.get("blocked_weekdays") or [])
    days = _working_days(db, company_id, employee.id, req.date, end)
    if not days:
        raise BadRequest("These are not working days for you.")
    bad = [d for d in days if d.weekday() in blocked]
    if bad:
        raise BadRequest(f"Work from home isn't allowed on {bad[0]:%A}s.")
    if _overlapping(db, company_id, employee.id, "wfh", req.date, end).first():
        raise BadRequest("You already asked to work from home on some of these days.")
    if _overlapping(db, company_id, employee.id, "on_duty", req.date, end).first():
        raise BadRequest("You are on duty on some of these days.")
    per_day = DURATIONS[dur]
    wl, ml = float(cfg.get("weekly_limit") or 0), float(cfg.get("monthly_limit") or 0)
    weeks, months = {}, {}
    for d in days:
        wk = d - timedelta(days=d.weekday())
        weeks[wk] = weeks.get(wk, 0) + per_day
        months[(d.year, d.month)] = months.get((d.year, d.month), 0) + per_day
    if wl:
        for wk, n in weeks.items():
            used = _days_used(db, company_id, employee.id, "wfh", wk, wk + timedelta(days=6))
            if used + n > wl:
                raise BadRequest(f"Work from home is limited to {wl:g} days a week; the week of {wk:%d %b} "
                                 f"would have {used + n:g}.")
    if ml:
        for (y, m), n in months.items():
            ms, me = service.month_range(y, m)
            used = _days_used(db, company_id, employee.id, "wfh", ms, me)
            if used + n > ml:
                raise BadRequest(f"Work from home is limited to {ml:g} days a month; {date(y, m, 1):%B} "
                                 f"would have {used + n:g}.")
    req.end_date = end if end != req.date else None
    req.duration = dur
    req.category_id = _master_ok(db, company_id, "wfh_category", data.get("category_id"))
    if not req.category_id and _has_values(db, company_id, "wfh_category"):
        raise BadRequest("Choose a work from home category.")


def _has_values(db, company_id, key) -> bool:
    from core.masters import MasterType, MasterValue
    return bool(db.query(MasterValue.id).join(MasterType, MasterType.id == MasterValue.type_id)
                .filter(MasterValue.company_id == company_id, MasterType.key == key,
                        MasterValue.active.is_(True)).first())


def _permission(db, company_id, employee, req, data, td) -> None:
    cfg = service.get_setting(db, company_id, "permission", req.date)
    t1, t2 = _time(data.get("from_time"), "start time"), _time(data.get("to_time"), "end time")
    if not t1 or not t2 or t2 <= t1:
        raise BadRequest("Enter a start and end time, with the end after the start.")
    minutes = (datetime.combine(req.date, t2) - datetime.combine(req.date, t1)).seconds // 60
    lo, hi = int(cfg.get("min_minutes_per_request") or 0), int(cfg.get("max_minutes_per_request") or 0)
    if lo and minutes < lo:
        raise BadRequest(f"Permission must be at least {lo} minutes.")
    if hi and minutes > hi:
        raise BadRequest(f"Permission can be at most {hi} minutes at a time. Apply for leave for longer.")
    plan = shifts_service.day_plans(db, company_id, [employee.id], req.date, req.date)[(employee.id, req.date)]
    if plan.day_type != "working":
        raise BadRequest("That is not a working day for you.")
    if cfg.get("within_shift_only", True) and plan.shift:
        sp = shifts_service.spec(plan.shift)
        if not sp.crosses_midnight and (t1 < sp.start or t2 > sp.end):
            raise BadRequest(f"Permission must be within your shift ({sp.start:%H:%M} to {sp.end:%H:%M}).")
    ms, me = _month(req.date)
    used = _overlapping(db, company_id, employee.id, "permission", ms, me).all()
    cnt, mins = int(cfg.get("monthly_count") or 0), int(cfg.get("monthly_minutes") or 0)
    if cnt and len(used) >= cnt:
        raise BadRequest(f"You have used all {cnt} permissions allowed for {req.date:%B}.")
    if mins and sum(int(r.minutes or 0) for r in used) + minutes > mins:
        left = mins - sum(int(r.minutes or 0) for r in used)
        raise BadRequest(f"Only {max(left, 0)} minutes of permission are left for {req.date:%B}.")
    for r in used:
        if r.date == req.date and r.from_time and r.to_time and r.from_time < t2 and t1 < r.to_time:
            raise BadRequest("This overlaps another permission on the same day.")
    req.from_time, req.to_time, req.minutes, req.duration = t1, t2, minutes, "hours"


def _overtime(db, company_id, employee, req, data, td) -> None:
    cfg = service.get_setting(db, company_id, "overtime_request", req.date)
    if req.date < td:
        raise BadRequest("Overtime pre-approval is for today or a later date.")
    hours = data.get("hours")
    try:
        minutes = int(round(float(hours) * 60))
    except (TypeError, ValueError):
        raise BadRequest("Enter the overtime hours.")
    mx = float(cfg.get("max_hours_per_day") or 0)
    if minutes <= 0 or (mx and minutes > mx * 60):
        raise BadRequest(f"Overtime must be more than 0 and at most {mx:g} hours a day.")
    if not data.get("consent"):
        raise BadRequest("Overtime needs your consent. Tick the consent box.")
    if not req.reason:
        raise BadRequest("Say what the overtime is for.")
    if _overlapping(db, company_id, employee.id, "overtime", req.date, req.date).first():
        raise BadRequest("You already asked for overtime on this day.")
    req.minutes, req.duration = minutes, "hours"
    req.details = {"consent": True, "consent_text": cfg.get("consent_text"),
                   "consented_at": datetime.now(timezone.utc).isoformat()}


# ── Approval handlers (called by the workflow engine) ───────────────

def _row(db: Session, instance) -> AttendanceRequest:
    req = (db.query(AttendanceRequest).filter(AttendanceRequest.company_id == instance.company_id,
                                              AttendanceRequest.id == (instance.payload or {}).get("request_id")).first())
    if req is None:
        raise NotFound("Attendance request")
    return req


def on_approved(db: Session, instance) -> None:
    req = _row(db, instance)
    req.status = "approved"
    req.decided_at = datetime.now(timezone.utc)
    if req.kind == "regularisation":
        _apply_regularisation(db, req)
    if req.kind == "overtime":
        audit.event(db, "attendance.overtime_consent", "attendance_requests", req.id,
                    {"date": req.date.isoformat(), "minutes": req.minutes, "consent": True},
                    company_id=req.company_id, entity_uuid=str(req.uuid))
    db.flush()
    service.recompute(db, req.company_id, [req.employee_id], req.date - timedelta(days=1), req.end_date or req.date)


def on_rejected(db: Session, instance) -> None:
    req = _row(db, instance)
    req.status = "rejected"
    req.decided_at = datetime.now(timezone.utc)


def on_cancelled(db: Session, instance) -> None:
    req = _row(db, instance)
    req.status = "cancelled"
    req.decided_at = datetime.now(timezone.utc)


def _apply_regularisation(db: Session, req: AttendanceRequest) -> None:
    tz = service.company_tz(db, req.company_id)
    det = req.details or {}
    plan = shifts_service.day_plans(db, req.company_id, [req.employee_id], req.date, req.date)[(req.employee_id, req.date)]
    sp = shifts_service.spec(plan.shift)
    times = []
    if det.get("in"):
        t = parse_hhmm(det["in"])
        day = req.date
        times.append((datetime.combine(day, t, tzinfo=tz), "in"))
    if det.get("out"):
        t = parse_hhmm(det["out"])
        day = req.date + timedelta(days=1) if det.get("out_next_day") else req.date
        times.append((datetime.combine(day, t, tzinfo=tz), "out"))
    if det.get("mode") == "replace":
        # set aside the wrong punches of that day: those before the middle of the shift are
        # check-ins, the rest check-outs; only the sides being corrected are replaced
        if sp:
            s, e = sp.window(req.date, tz)
        else:
            s, e = datetime.combine(req.date, time(9), tzinfo=tz), datetime.combine(req.date, time(18), tzinfo=tz)
        mid = s + (e - s) / 2
        provided = {k for _t, k in times}
        for p in (db.query(Punch).filter(Punch.company_id == req.company_id, Punch.employee_id == req.employee_id,
                                         Punch.punched_at >= s - timedelta(hours=4), Punch.punched_at < e + timedelta(hours=10),
                                         Punch.ignored.is_(False)).all()):
            if ("in" if p.punched_at < mid else "out") in provided:
                p.ignored = True
                p.note = "Replaced by an approved correction"
    for ts, direction in times:
        ts_utc = ts.astimezone(timezone.utc)
        exists = db.query(Punch).filter(Punch.employee_id == req.employee_id, Punch.punched_at == ts_utc).first()
        if exists:
            exists.ignored = False
            continue
        db.add(Punch(company_id=req.company_id, employee_id=req.employee_id, punched_at=ts_utc, direction=direction,
                     source="regularisation", request_id=req.id, note=req.reason))
    db.flush()


def summarize(instance) -> list[str]:
    p = instance.payload or {}
    lines = []
    kind = p.get("kind")
    d = p.get("date")
    end = p.get("end_date")
    lines.append(f"Date: {d}" + (f" to {end}" if end and end != d else ""))
    det = p.get("details") or {}
    if kind == "regularisation":
        if det.get("in"):
            lines.append(f"Check-in: {det['in']}")
        if det.get("out"):
            lines.append(f"Check-out: {det['out']}" + (" (next day)" if det.get("out_next_day") else ""))
        lines.append("Replaces the recorded punches" if det.get("mode") == "replace" else "Adds the missing punches")
    elif kind in ("wfh", "on_duty"):
        lines.append({"full_day": "Full day", "first_half": "First half", "second_half": "Second half"}.get(
            p.get("duration") or "full_day", "Full day") + f" · {p.get('days')} day(s)")
        if det.get("place"):
            lines.append(f"Place: {det['place']}")
    elif kind == "permission":
        lines.append(f"{p.get('minutes')} minutes")
    elif kind == "overtime":
        lines.append(f"{(p.get('minutes') or 0) / 60:.1f} hours, consent given")
    if p.get("reason"):
        lines.append(f"Reason: {p['reason']}")
    return lines
