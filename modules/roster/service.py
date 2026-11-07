"""Roster: who works which shift on which day.

- assign() puts a shift (or a weekly off) on employees for dates; entries start as a
  draft and count for attendance only once published.
- apply_pattern() fills a date range from a weekly, week-of-month or N-day cycle pattern.
- Every change is checked against the rest rules (8 h/48 h limits, minimum rest between
  shifts, weekly rest day, consecutive nights) and the women's night-shift consent rule.
  Blocking problems stop the change for that person; warnings are returned.
- publish() makes drafts live, tells the employees and re-works their attendance.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from core import audit
from core.errors import BadRequest
from modules.attendance import service as att
from modules.attendance.engine import is_night_shift, merged_rule, parse_hhmm
from modules.roster.models import RosterEntry
from modules.shifts import adapters
from modules.shifts import service as shifts_service
from modules.shifts.models import Shift, ShiftPattern
from modules.shifts.rest_rules import PlannedDay, check_schedule, merged_rules

MAX_DAYS = 92
STATUSES = {"working", "off", "leave", "half_day"}


def _women_night(start: datetime, end: datetime, rules: dict) -> bool:
    from modules.attendance.engine import _night_minutes
    w = rules.get("women_night_window") or {}
    return _night_minutes(start, end, start.tzinfo, parse_hhmm(w.get("start", "19:00")), parse_hhmm(w.get("end", "06:00"))) > 0


def planned_days(db: Session, company_id: int, employee_id: int, start: date, end: date,
                 changes: dict[date, tuple[str, Shift | None]], rules: dict) -> list[PlannedDay]:
    """The employee's plan from start to end (drafts included) with `changes` applied."""
    tz = att.company_tz(db, company_id)
    plans = shifts_service.day_plans(db, company_id, [employee_id], start, end, include_drafts=True)
    rule = merged_rule({})
    out = []
    for day in adapters.daterange(start, end):
        if day in changes:
            status, shift = changes[day]
            working = status in ("working", "half_day")
            new = True
        else:
            p = plans[(employee_id, day)]
            shift = p.shift
            working = p.day_type == "working" or (p.source == "roster" and p.roster_status == "working")
            working = working and not p.on_roster_leave
            new = False
        pd = PlannedDay(date=day, working=bool(working and shift), new=new)
        if pd.working:
            sp = shifts_service.spec(shift)
            pd.start, pd.end = sp.window(day, tz)
            pd.net_minutes = sp.scheduled_minutes()
            pd.night = is_night_shift(sp, rule)
            pd.women_night = _women_night(pd.start, pd.end, rules)
            pd.shift_name = shift.name
        out.append(pd)
    return out


def check(db: Session, company_id: int, employee, changes: dict[date, tuple[str, Shift | None]],
          rules: dict | None = None, consents=None) -> list[dict]:
    if not changes:
        return []
    rules = rules or merged_rules(att.get_setting(db, company_id, "rest_rules", min(changes)))
    lo, hi = min(changes) - timedelta(days=7), max(changes) + timedelta(days=7)
    days = planned_days(db, company_id, employee.id, lo, hi, changes, rules)
    if consents is None:
        consents = shifts_service.consents_for(db, company_id, [employee.id]).get(employee.id, [])
    found = check_schedule(days, rules, is_woman=adapters.is_woman(db, employee),
                           has_consent=lambda d: shifts_service.has_consent(consents, d))
    return [{**v.as_dict(), "employee_id": employee.id, "employee_name": att.name_of(employee)} for v in found]


def _dates(data: dict) -> list[date]:
    if data.get("dates"):
        days = sorted(set(data["dates"]))
    else:
        s, e = data.get("start_date"), data.get("end_date") or data.get("start_date")
        if not s:
            raise BadRequest("Choose the dates.")
        if e < s:
            raise BadRequest("The end date can't be before the start date.")
        days = list(adapters.daterange(s, e))
        weekdays = data.get("weekdays")
        if weekdays:
            days = [d for d in days if d.weekday() in set(weekdays)]
    if len(days) > MAX_DAYS:
        raise BadRequest(f"Assign up to {MAX_DAYS} days at a time.")
    return days


def target_employees(db: Session, company_id: int, data: dict) -> list:
    ids = set(int(x) for x in data.get("employee_ids") or [])
    for team in data.get("team_ids") or []:
        ids.update(adapters.team_member_ids(db, company_id, int(team)))
    if data.get("department_ids"):
        from modules.employees.models import Employee
        ids.update(i for (i,) in db.query(Employee.id).filter(Employee.company_id == company_id,
                                                                Employee.active.is_(True),
                                                                Employee.department_id.in_(data["department_ids"])).all())
    if not ids:
        raise BadRequest("Choose at least one employee, team or department.")
    return att.employees_query(db, company_id, list(ids)).all()


def assign(db: Session, company_id: int, data: dict, *, dry_run: bool = False, source: str = "manual",
           publish: bool = False) -> dict:
    """data: employee_ids/team_ids/department_ids, dates or start_date/end_date (+weekdays),
    shift_id or status ('off', 'leave', 'half_day'), mode 'replace' | 'skip', note."""
    status = data.get("status") or "working"
    if status not in STATUSES:
        raise BadRequest("Choose a shift or a weekly off.")
    shift = None
    if status in ("working", "half_day"):
        shift = shifts_service.shift_by_id(db, company_id, data.get("shift_id"))
        if shift is None:
            raise BadRequest("Choose a shift.")
        if not shift.active:
            raise BadRequest(f"{shift.name} is switched off.")
    days = _dates(data)
    emps = target_employees(db, company_id, data)
    mode = data.get("mode") or "replace"
    rules = merged_rules(att.get_setting(db, company_id, "rest_rules", days[0]))
    consents = shifts_service.consents_for(db, company_id, [e.id for e in emps])
    existing = {(r.employee_id, r.date): r for r in db.query(RosterEntry).filter(
        RosterEntry.company_id == company_id, RosterEntry.employee_id.in_([e.id for e in emps]),
        RosterEntry.date.in_(days)).all()}
    lock_rows = att.locks(db, company_id)
    saved, skipped, blocked, warnings = 0, 0, [], []
    touched = defaultdict(list)
    for e in emps:
        joined, left = adapters.employment_dates(e)
        my_days = [d for d in days if not (mode == "skip" and (e.id, d) in existing)
                   and not att.lock_for(lock_rows, d)
                   and not (joined and d < joined) and not (left and d > left)]
        skipped += len(days) - len(my_days)
        if not my_days:
            continue
        changes = {d: (status, shift) for d in my_days}
        problems = check(db, company_id, e, changes, rules, consents.get(e.id, []))
        blocks = [p for p in problems if p["action"] == "block"]
        warnings += [p for p in problems if p["action"] == "warn"]
        if blocks:
            blocked += blocks
            bad = set()
            for p in blocks:
                d0 = date.fromisoformat(p["date"])
                if p["rule"] == "max_weekly_hours":  # the whole week of new days
                    bad.update(d for d in my_days if d0 <= d <= d0 + timedelta(days=6))
                else:
                    bad.add(d0)
            my_days = [d for d in my_days if d not in bad]
            skipped += len(changes) - len(my_days)
        if dry_run:
            continue
        for d in my_days:
            row = existing.get((e.id, d))
            if row is None:
                row = RosterEntry(company_id=company_id, employee_id=e.id, date=d)
                db.add(row)
                existing[(e.id, d)] = row
            row.status, row.shift_id = status, shift.id if shift else None
            row.note = data.get("note")
            row.source = source
            row.pattern_id = data.get("pattern_id")
            row.published = bool(publish)
            row.published_at = datetime.now(timezone.utc) if publish else None
            saved += 1
            touched[e.id].append(d)
    if not dry_run:
        db.flush()
        if publish:
            _after_publish(db, company_id, touched)
    return {"saved": saved, "skipped": skipped, "blocked": blocked, "warnings": warnings,
            "employees": len(emps), "days": len(days)}


def apply_pattern(db: Session, company_id: int, data: dict, dry_run: bool = False) -> dict:
    pattern = db.query(ShiftPattern).filter(ShiftPattern.company_id == company_id,
                                            ShiftPattern.uuid == data.get("pattern_uuid")).first()
    if pattern is None:
        raise BadRequest("Choose a pattern.")
    s, e = data.get("start_date"), data.get("end_date")
    if not s or not e or e < s:
        raise BadRequest("Choose a start and end date.")
    if (e - s).days + 1 > MAX_DAYS:
        raise BadRequest(f"Apply a pattern to up to {MAX_DAYS} days at a time.")
    shifts = {x.id: x for x in db.query(Shift).filter(Shift.company_id == company_id).all()}
    by_shift = defaultdict(list)
    for d in adapters.daterange(s, e):
        working, sid = shifts_service.pattern_slot(pattern, d, s)
        by_shift[sid if working else None].append(d)
    total = {"saved": 0, "skipped": 0, "blocked": [], "warnings": [], "employees": 0, "days": (e - s).days + 1}
    for sid, days in by_shift.items():
        part = {**data, "dates": days, "pattern_id": pattern.id,
                **({"status": "working", "shift_id": sid} if sid else {"status": "off"})}
        if sid and sid not in shifts:
            continue
        r = assign(db, company_id, part, dry_run=dry_run, source="pattern")
        total["saved"] += r["saved"]
        total["skipped"] += r["skipped"]
        total["blocked"] += r["blocked"]
        total["warnings"] += r["warnings"]
        total["employees"] = r["employees"]
    return total


def clear(db: Session, company_id: int, employee_ids: list[int], start: date, end: date) -> int:
    lock_rows = att.locks(db, company_id)
    rows = db.query(RosterEntry).filter(RosterEntry.company_id == company_id, RosterEntry.employee_id.in_(employee_ids),
                                        RosterEntry.date >= start, RosterEntry.date <= end).all()
    n = 0
    touched = defaultdict(list)
    for r in rows:
        if att.lock_for(lock_rows, r.date):
            continue
        if r.published:
            touched[r.employee_id].append(r.date)
        db.delete(r)
        n += 1
    db.flush()
    _recompute(db, company_id, touched)
    return n


def publish(db: Session, company_id: int, start: date, end: date, employee_ids: list[int] | None = None) -> int:
    q = db.query(RosterEntry).filter(RosterEntry.company_id == company_id, RosterEntry.published.is_(False),
                                     RosterEntry.date >= start, RosterEntry.date <= end)
    if employee_ids:
        q = q.filter(RosterEntry.employee_id.in_(employee_ids))
    rows = q.all()
    now = datetime.now(timezone.utc)
    touched = defaultdict(list)
    for r in rows:
        r.published, r.published_at = True, now
        touched[r.employee_id].append(r.date)
    db.flush()
    _after_publish(db, company_id, touched)
    audit.event(db, "roster.published", "roster_entries", None,
                {"from": start.isoformat(), "to": end.isoformat(), "entries": len(rows), "employees": len(touched)},
                entity_label=f"Roster {start:%d %b} to {end:%d %b %Y}")
    return len(rows)


def _recompute(db: Session, company_id: int, touched: dict) -> None:
    td = att.today(db, company_id)
    for eid, days in touched.items():
        past = [d for d in days if d <= td]
        if past:
            att.recompute(db, company_id, [eid], min(past) - timedelta(days=1), max(past))


def _after_publish(db: Session, company_id: int, touched: dict) -> None:
    from modules.notifications.models import Notification
    for eid, days in touched.items():
        if not days:
            continue
        db.add(Notification(company_id=company_id, employee_id=eid, channel="in_app", message_type="roster",
                            message_content=f"Your roster from {min(days):%d %b} to {max(days):%d %b} is published.",
                            status="sent", sent_at=datetime.now(timezone.utc)))
    _recompute(db, company_id, touched)


def grid(db: Session, company_id: int, start: date, end: date, ids: list[int] | None,
         department_id: int | None = None, team_id: int | None = None, q: str | None = None,
         published_only: bool = False) -> dict:
    if (end - start).days + 1 > MAX_DAYS:
        raise BadRequest(f"Show up to {MAX_DAYS} days at a time.")
    if team_id:
        members = set(adapters.team_member_ids(db, company_id, team_id))
        ids = [i for i in (ids if ids is not None else members) if i in members]
    emps = att.employees_query(db, company_id, ids, department_id, q).all()
    eids = [e.id for e in emps]
    plans = shifts_service.day_plans(db, company_id, eids, start, end, include_drafts=not published_only) if eids else {}
    entries = {(r.employee_id, r.date): r for r in db.query(RosterEntry).filter(
        RosterEntry.company_id == company_id, RosterEntry.employee_id.in_(eids or [-1]),
        RosterEntry.date >= start, RosterEntry.date <= end).all()}
    leave = adapters.leave_days(db, company_id, eids, start, end) if eids else {}
    consents = shifts_service.consents_for(db, company_id, eids)
    rows, drafts = [], 0
    for e in emps:
        cells = {}
        for d in adapters.daterange(start, end):
            p = plans[(e.id, d)]
            r = entries.get((e.id, d))
            if r is not None and published_only and not r.published:
                r = None
            sh = p.shift if p.day_type != "weekly_off" else None
            cell = {"day_type": p.day_type, "source": p.source, "holiday_name": p.holiday_name,
                    "shift_id": sh.id if sh else None, "status": r.status if r else (
                        "off" if p.day_type == "weekly_off" else "working"),
                    "published": r.published if r else True, "entry": r is not None,
                    "on_leave": bool(leave.get((e.id, d))) or p.on_roster_leave}
            if r is not None and not r.published:
                drafts += 1
            cells[d.isoformat()] = cell
        rows.append({"employee_id": e.id, "employee_uuid": str(e.uuid), "name": att.name_of(e),
                     "employee_code": e.employee_code, "department_id": e.department_id,
                     "photo": e.profile_image_url, "woman": adapters.is_woman(db, e),
                     "night_consent": bool(consents.get(e.id)), "cells": cells})
    rule = merged_rule({})
    shifts = [shifts_service.shift_out(s, rule) for s in db.query(Shift).filter(Shift.company_id == company_id)
              .order_by(Shift.active.desc(), Shift.start_time).all()]
    return {"start": start.isoformat(), "end": end.isoformat(), "rows": rows, "shifts": shifts, "drafts": drafts}


def my_schedule(db: Session, company_id: int, employee_id: int, start: date, end: date) -> list[dict]:
    plans = shifts_service.day_plans(db, company_id, [employee_id], start, end)
    out = []
    for d in adapters.daterange(start, end):
        p = plans[(employee_id, d)]
        sh = p.shift if p.day_type != "weekly_off" else None
        out.append({"date": d.isoformat(), "day_type": p.day_type, "holiday_name": p.holiday_name,
                    "shift": shifts_service.shift_out(sh) if sh else None, "source": p.source})
    return out
