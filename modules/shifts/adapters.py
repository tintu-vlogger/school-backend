"""Small adapters to other modules that are built in parallel (leave and holidays,
organisation, employee records). Each one uses the other module's public functions when
they are there and falls back to a safe default when they are not, so attendance works
on its own.

Leave module (used when present):
    modules.leave.calendar.WorkCalendar / day_map   working, half, weekend and holiday days
    modules.leave.service.leave_on_dates           approved leave, day by day
Fallback: Monday to Friday are working days, no holidays, no approved leave.

Functions here:
    calendar_days(db, company_id, employee_ids, start, end) -> {(employee_id, date): kind} | None
    holidays_between(db, company_id, employee_ids, start, end) -> {(employee_id, date): name}
    leave_days(db, company_id, employee_ids, start, end)
        -> {(employee_id, date): {"unit": "full"|"first_half"|"second_half", "paid": bool, "name": str,
                                  "pay_percent": 100}}  (exclude_request_ids leaves some requests out)
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger("hrms.attendance")
LEAVE_COUNTS = ("approved", "cancel_pending")  # leave that is (still) granted


def _leave_calendar():
    try:
        from modules.leave import calendar  # type: ignore
        return calendar if hasattr(calendar, "WorkCalendar") else None
    except Exception:  # not built yet, or failing to import
        return None


def _leave_service():
    try:
        from modules.leave import service  # type: ignore
        return service
    except Exception:
        return None


def _employees(db: Session, company_id: int, employee_ids: list[int]) -> list:
    from modules.employees.models import Employee
    return db.query(Employee).filter(Employee.company_id == company_id, Employee.id.in_(employee_ids or [-1])).all()


def calendar_days(db: Session, company_id: int, employee_ids: list[int], start: date, end: date) -> dict | None:
    """Each employee's work calendar from the leave module: {(employee_id, date): {"kind":
    working|half|weekend|holiday, "holiday": name}}. None when the leave module is not there."""
    cal_mod = _leave_calendar()
    if cal_mod is None or not employee_ids:
        return None
    try:
        with db.begin_nested():
            cal = cal_mod.WorkCalendar(db, company_id)
            out = {}
            for emp in _employees(db, company_id, employee_ids):
                for item in cal.days(emp, start, end):
                    out[(emp.id, item["date"])] = {"kind": item["kind"], "holiday": item.get("holiday")}
            return out
    except Exception as exc:  # never let another module break attendance
        log.warning("leave calendar failed: %s", exc)
        return None


def is_working_day(db: Session, company_id: int, employee_id: int, day: date) -> bool:
    days = calendar_days(db, company_id, [employee_id], day, day)
    if days is not None and (employee_id, day) in days:
        return days[(employee_id, day)]["kind"] in ("working", "half")
    return day.weekday() < 5


def holidays_between(db: Session, company_id: int, employee_ids: list[int], start: date, end: date,
                     days: dict | None = None) -> dict:
    days = days if days is not None else calendar_days(db, company_id, employee_ids, start, end)
    return {k: (v.get("holiday") or "Holiday") for k, v in (days or {}).items() if v["kind"] == "holiday"}


def _pay_percent(req, d: date, cache: dict) -> float:
    """Pay kept on a leave day (100 normally, 50 for half-pay sick leave): the leave type's
    `pay_percent` rule in force on that day."""
    t = getattr(req, "leave_type", None)
    if t is None:
        return 100.0
    key = (t.id, req.policy_version_id, d.year)
    if key not in cache:
        try:
            from modules.leave import policy  # type: ignore
            cache[key] = float(policy.rules_on(t, d).get("pay_percent", 100) or 0)
        except Exception:
            cache[key] = 100.0
    return cache[key]


def leave_days(db: Session, company_id: int, employee_ids: list[int], start: date, end: date,
               exclude_request_ids: set | None = None) -> dict:
    svc = _leave_service()
    fn = getattr(svc, "leave_on_dates", None) if svc else None
    if fn is None or not employee_ids:
        return {}
    out = {}
    exclude = exclude_request_ids or set()
    cache: dict = {}
    try:
        with db.begin_nested():
            for eid in employee_ids:
                for d, items in fn(db, company_id, eid, start, end).items():
                    total, lop, name, half, pay_amount = 0.0, 0.0, None, None, 0.0
                    for req, it in items:
                        if req.status not in LEAVE_COUNTS or req.id in exclude:
                            continue
                        amount = float(it.get("amount") or 0)
                        paid_part = amount - float(it.get("lop") or 0)
                        total += amount
                        lop += amount - paid_part
                        pay_amount += paid_part * _pay_percent(req, d, cache)
                        name = name or (req.leave_type.name if getattr(req, "leave_type", None) else "Leave")
                        half = half or (req.half if req.unit in ("half_day", "quarter_day") else None)
                    if total <= 0:
                        continue
                    unit = "full" if total >= 1 else ("second_half" if half == "second" else "first_half")
                    paid = total - lop
                    pct = round(pay_amount / paid) if paid > 0 else 100
                    out[(eid, d)] = {"unit": unit, "paid": lop < total, "name": name or "Leave", "pay_percent": pct}
    except Exception as exc:
        log.warning("leave lookup failed: %s", exc)
    return out


def _rows(db: Session, table: str, sql: str, params: dict) -> list:
    """Rows from another module's table, or [] when the table is not there (yet)."""
    if db.execute(text("SELECT to_regclass(:n)"), {"n": table}).scalar() is None:
        return []
    try:
        with db.begin_nested():
            return db.execute(text(sql), params).fetchall()
    except Exception as exc:
        log.warning("reading %s failed: %s", table, exc)
        return []


# ── Employee details that other builders may add ────────────────────

def is_woman(db: Session, emp) -> bool:
    """True when the employee's gender is recorded as female (employee.gender_id from the
    gender master, or a 'gender' custom field)."""
    gid = getattr(emp, "gender_id", None)
    candidates = [gid, (emp.custom or {}).get("gender") if getattr(emp, "custom", None) else None]
    for value in candidates:
        if value in (None, ""):
            continue
        if isinstance(value, str) and not value.isdigit():
            if value.strip().upper() in ("F", "FEMALE", "WOMAN"):
                return True
            continue
        from core.masters import MasterValue
        mv = db.query(MasterValue).filter(MasterValue.id == int(value)).first()
        if mv and ((mv.code or "").upper() == "F" or (mv.label or "").lower() == "female"):
            return True
    return False


def employment_dates(emp) -> tuple[date | None, date | None]:
    """Joining and exit dates when the employee record has them."""
    return getattr(emp, "date_of_joining", None), getattr(emp, "exit_date", None)


def team_member_ids(db: Session, company_id: int, team_id: int) -> list[int]:
    try:
        from modules.organization import service as org  # type: ignore
        fn = getattr(org, "team_member_ids", None)
        if fn:
            r = fn(db, company_id, team_id)
            if isinstance(r, list):
                return r
    except Exception:
        pass
    rows = _rows(db, "team_members", "SELECT employee_id FROM team_members WHERE company_id = :c AND team_id = :t",
                 {"c": company_id, "t": team_id})
    return [r[0] for r in rows]


def teams(db: Session, company_id: int) -> list[dict]:
    rows = _rows(db, "teams", "SELECT id, name FROM teams WHERE company_id = :c AND active ORDER BY name",
                 {"c": company_id})
    return [{"value": r[0], "label": r[1]} for r in rows]


def location_fences(db: Session, company_id: int) -> list[dict]:
    """Offices from the organisation module that have a map point and radius."""
    rows = _rows(db, "locations",
                 "SELECT id, name, latitude, longitude, radius_m FROM locations "
                 "WHERE company_id = :c AND active AND latitude IS NOT NULL AND longitude IS NOT NULL",
                 {"c": company_id})
    return [{"id": None, "location_id": r[0], "name": r[1], "latitude": float(r[2]), "longitude": float(r[3]),
             "radius_m": int(r[4] or 200)} for r in rows]


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)
