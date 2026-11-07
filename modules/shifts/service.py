"""Shifts: the shift master, patterns, employee work arrangements, night-shift consent,
and the day plan resolver every other part of attendance uses.

day_plans() answers "what was this person supposed to do on this day": the published
roster entry if there is one, else their work arrangement (working days and default
shift) in force that day, else the leave module's working days, else Monday to Friday
on the company's default shift. Holidays come from the leave module (see adapters).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from core import context
from core.errors import BadRequest, NotFound
from modules.attendance.engine import ShiftSpec, is_night_shift, parse_hhmm
from modules.shifts import adapters
from modules.shifts.models import NightShiftConsent, Shift, ShiftPattern, WorkArrangement

COLORS = ["indigo", "sky", "emerald", "amber", "rose", "violet", "teal", "slate", "orange", "lime"]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ── Shift master ────────────────────────────────────────────────────

def _t(value, label: str) -> time:
    try:
        t = parse_hhmm(value)
    except (TypeError, ValueError):
        t = None
    if t is None:
        raise BadRequest(f"Enter a valid {label} (HH:MM).")
    return t


def spec(shift: Shift | None) -> ShiftSpec | None:
    if shift is None:
        return None
    return ShiftSpec(
        id=shift.id, name=shift.name, start=shift.start_time, end=shift.end_time, breaks=list(shift.breaks or []),
        grace_in=shift.grace_in_minutes, grace_out=shift.grace_out_minutes,
        half_day_hours=float(shift.half_day_hours) if shift.half_day_hours is not None else None,
        full_day_hours=float(shift.full_day_hours) if shift.full_day_hours is not None else None,
        is_flexible=shift.is_flexible, core_start=shift.core_start, core_end=shift.core_end,
        overtime_after_minutes=shift.overtime_after_minutes,
        allowance_amount=float(shift.allowance_amount or 0),
        allowance_min_hours=float(shift.allowance_min_hours) if shift.allowance_min_hours is not None else None,
    )


def _clean_breaks(raw: list | None, start: time, end: time) -> list[dict]:
    out = []
    s = ShiftSpec(id=None, name="", start=start, end=end)
    win_s, win_e = s.window(date(2000, 1, 3), None)
    for b in raw or []:
        name = (b.get("name") or "Break").strip()[:40]
        bs, be = _t(b.get("start"), f"start of {name}"), _t(b.get("end"), f"end of {name}")
        probe = ShiftSpec(id=None, name="", start=start, end=end, breaks=[{"start": bs.strftime("%H:%M"),
                                                                          "end": be.strftime("%H:%M")}])
        (ps, pe), = probe.break_intervals(date(2000, 1, 3), None, paid=None)
        if ps < win_s or pe > win_e:
            raise BadRequest(f"{name} must be inside the shift timings.")
        out.append({"name": name, "start": bs.strftime("%H:%M"), "end": be.strftime("%H:%M"), "paid": bool(b.get("paid"))})
    return out


def save_shift(db: Session, company_id: int, data: dict, shift: Shift | None = None) -> Shift:
    name = (data.get("name") or (shift.name if shift else "")).strip()
    if not name:
        raise BadRequest("Give the shift a name.")
    dup = db.query(Shift.id).filter(Shift.company_id == company_id, Shift.name.ilike(name))
    if shift is not None:
        dup = dup.filter(Shift.id != shift.id)
    if dup.first():
        raise BadRequest(f"A shift called {name} already exists.")
    start = _t(data.get("start_time", shift.start_time if shift else None), "start time")
    end = _t(data.get("end_time", shift.end_time if shift else None), "end time")
    if start == end:
        raise BadRequest("Start and end time can't be the same.")
    if shift is None:
        shift = Shift(company_id=company_id)
        db.add(shift)
    shift.name = name
    shift.start_time, shift.end_time = start, end
    for key in ("code", "color", "description"):
        if key in data:
            setattr(shift, key, (data[key] or None) if key != "color" else (data[key] or "indigo"))
    shift.breaks = _clean_breaks(data.get("breaks", shift.breaks), start, end)
    for key in ("grace_in_minutes", "grace_out_minutes", "overtime_after_minutes"):
        if data.get(key) is not None:
            v = int(data[key])
            if v < 0 or v > 600:
                raise BadRequest("Minutes must be between 0 and 600.")
            setattr(shift, key, v)
    for key in ("half_day_hours", "full_day_hours", "allowance_min_hours"):
        if key in data:
            v = data[key]
            if v not in (None, "") and not (0 < float(v) <= 24):
                raise BadRequest("Hours must be between 0 and 24.")
            setattr(shift, key, None if v in (None, "") else Decimal(str(v)))
    if shift.half_day_hours and shift.full_day_hours and shift.half_day_hours > shift.full_day_hours:
        raise BadRequest("Half-day hours can't be more than full-day hours.")
    if "allowance_amount" in data:
        shift.allowance_amount = Decimal(str(data.get("allowance_amount") or 0)).quantize(Decimal("0.01"))
    if "is_flexible" in data:
        shift.is_flexible = bool(data["is_flexible"])
    if shift.is_flexible:
        cs = data.get("core_start", shift.core_start)
        ce = data.get("core_end", shift.core_end)
        shift.core_start = _t(cs, "core hours start") if cs not in (None, "") else None
        shift.core_end = _t(ce, "core hours end") if ce not in (None, "") else None
    else:
        shift.core_start = shift.core_end = None
    if "weekly_offs" in data:
        offs = sorted({int(x) for x in (data.get("weekly_offs") or []) if 0 <= int(x) <= 6})
        shift.weekly_offs = offs
    if "active" in data:
        shift.active = bool(data["active"])
    if data.get("is_default"):
        for other in db.query(Shift).filter(Shift.company_id == company_id, Shift.is_default.is_(True)).all():
            if other is not shift:
                other.is_default = False
        shift.is_default = True
    db.flush()
    return shift


def get_shift(db: Session, company_id: int, uuid) -> Shift:
    s = db.query(Shift).filter(Shift.company_id == company_id, Shift.uuid == uuid).first()
    if not s:
        raise NotFound("Shift")
    return s


def shift_by_id(db: Session, company_id: int, shift_id: int | None) -> Shift | None:
    if not shift_id:
        return None
    return db.query(Shift).filter(Shift.company_id == company_id, Shift.id == shift_id).first()


def default_shift(db: Session, company_id: int) -> Shift | None:
    return (db.query(Shift).filter(Shift.company_id == company_id, Shift.active.is_(True))
            .order_by(Shift.is_default.desc(), Shift.id).first())


def shift_out(s: Shift, rule: dict | None = None) -> dict:
    sp = spec(s)
    return {
        "uuid": str(s.uuid), "id": s.id, "name": s.name, "code": s.code or s.name[:2].upper(), "color": s.color,
        "start_time": s.start_time.strftime("%H:%M"), "end_time": s.end_time.strftime("%H:%M"),
        "crosses_midnight": sp.crosses_midnight, "is_night": is_night_shift(sp, rule or {}),
        "breaks": s.breaks or [], "grace_in_minutes": s.grace_in_minutes, "grace_out_minutes": s.grace_out_minutes,
        "half_day_hours": float(s.half_day_hours) if s.half_day_hours is not None else None,
        "full_day_hours": float(s.full_day_hours) if s.full_day_hours is not None else None,
        "is_flexible": s.is_flexible,
        "core_start": s.core_start.strftime("%H:%M") if s.core_start else None,
        "core_end": s.core_end.strftime("%H:%M") if s.core_end else None,
        "overtime_after_minutes": s.overtime_after_minutes,
        "allowance_amount": str(s.allowance_amount or 0),
        "allowance_min_hours": float(s.allowance_min_hours) if s.allowance_min_hours is not None else None,
        "weekly_offs": s.weekly_offs or [], "is_default": s.is_default, "active": s.active,
        "description": s.description, "scheduled_minutes": sp.scheduled_minutes(),
    }


DEFAULT_SHIFTS = [
    {"name": "General", "code": "G", "color": "indigo", "start_time": "09:30", "end_time": "18:30",
     "breaks": [{"name": "Lunch", "start": "13:30", "end": "14:30", "paid": False}],
     "grace_in_minutes": 15, "grace_out_minutes": 10, "half_day_hours": 4, "full_day_hours": 7.5,
     "weekly_offs": [5, 6], "is_default": True},
    {"name": "Morning", "code": "M", "color": "amber", "start_time": "06:00", "end_time": "14:00",
     "breaks": [{"name": "Tea and meal", "start": "10:00", "end": "10:30", "paid": True}],
     "grace_in_minutes": 10, "grace_out_minutes": 5, "half_day_hours": 4, "full_day_hours": 7.5, "weekly_offs": [6]},
    {"name": "Evening", "code": "E", "color": "teal", "start_time": "14:00", "end_time": "22:00",
     "breaks": [{"name": "Meal", "start": "18:00", "end": "18:30", "paid": True}],
     "grace_in_minutes": 10, "grace_out_minutes": 5, "half_day_hours": 4, "full_day_hours": 7.5, "weekly_offs": [6]},
    {"name": "Night", "code": "N", "color": "violet", "start_time": "22:00", "end_time": "06:00",
     "breaks": [{"name": "Meal", "start": "02:00", "end": "02:30", "paid": True}],
     "grace_in_minutes": 10, "grace_out_minutes": 5, "half_day_hours": 4, "full_day_hours": 7.5,
     "allowance_amount": 150, "allowance_min_hours": 6, "weekly_offs": [6]},
    {"name": "Flexible", "code": "F", "color": "sky", "start_time": "08:00", "end_time": "20:00",
     "is_flexible": True, "core_start": "11:00", "core_end": "16:00", "grace_in_minutes": 0, "grace_out_minutes": 0,
     "half_day_hours": 4.5, "full_day_hours": 9, "weekly_offs": [5, 6]},
]


def ensure_default_shifts(db: Session, company_id: int) -> None:
    if db.query(Shift.id).filter(Shift.company_id == company_id).first():
        return
    for data in DEFAULT_SHIFTS:
        save_shift(db, company_id, dict(data))


# ── Patterns ────────────────────────────────────────────────────────

PATTERN_KINDS = {"weekly": "Weekly", "week_of_month": "Week of the month", "cycle": "Custom cycle"}


def _shift_ids(db: Session, company_id: int) -> set[int]:
    return {i for (i,) in db.query(Shift.id).filter(Shift.company_id == company_id).all()}


def _slot(value, valid: set[int]):
    if value in (None, "", "off"):
        return None
    v = int(value)
    if v not in valid:
        raise BadRequest("A pattern uses a shift that doesn't exist.")
    return v


def save_pattern(db: Session, company_id: int, data: dict, pattern: ShiftPattern | None = None) -> ShiftPattern:
    name = (data.get("name") or "").strip()
    if not name:
        raise BadRequest("Give the pattern a name.")
    kind = data.get("kind")
    if kind not in PATTERN_KINDS:
        raise BadRequest("Choose weekly, week of the month or custom cycle.")
    valid = _shift_ids(db, company_id)
    raw = data.get("slots")
    if kind == "weekly":
        slots = {str(i): _slot((raw or {}).get(str(i)), valid) for i in range(7)}
        used = [v for v in slots.values() if v]
    elif kind == "week_of_month":
        slots = {str(w): {str(i): _slot(((raw or {}).get(str(w)) or {}).get(str(i)), valid) for i in range(7)}
                 for w in range(1, 6)}
        used = [v for wk in slots.values() for v in wk.values() if v]
    else:
        if not isinstance(raw, list) or not (2 <= len(raw) <= 62):
            raise BadRequest("A custom cycle needs between 2 and 62 days.")
        slots = [_slot(v, valid) for v in raw]
        used = [v for v in slots if v]
    if not used:
        raise BadRequest("Put at least one shift in the pattern.")
    if pattern is None:
        pattern = ShiftPattern(company_id=company_id)
        db.add(pattern)
    pattern.name, pattern.kind, pattern.slots = name, kind, slots
    pattern.description = data.get("description")
    if "active" in data:
        pattern.active = bool(data["active"])
    db.flush()
    return pattern


def pattern_slot(pattern: ShiftPattern, day: date, anchor: date) -> tuple[bool, int | None]:
    """(working, shift_id) the pattern gives for a day. Cycles count from the anchor date."""
    if pattern.kind == "weekly":
        sid = (pattern.slots or {}).get(str(day.weekday()))
    elif pattern.kind == "week_of_month":
        week = min((day.day - 1) // 7 + 1, 5)
        sid = ((pattern.slots or {}).get(str(week)) or {}).get(str(day.weekday()))
    else:
        cycle = pattern.slots or []
        sid = cycle[(day - anchor).days % len(cycle)] if cycle else None
    return (sid is not None), sid


def pattern_out(p: ShiftPattern) -> dict:
    return {"uuid": str(p.uuid), "id": p.id, "name": p.name, "kind": p.kind, "kind_label": PATTERN_KINDS.get(p.kind),
            "slots": p.slots, "active": p.active, "description": p.description}


# ── Work arrangements ───────────────────────────────────────────────

def arrangements_for(db: Session, company_id: int, employee_ids: list[int]) -> dict[int, list[WorkArrangement]]:
    out: dict[int, list[WorkArrangement]] = {}
    if not employee_ids:
        return out
    rows = (db.query(WorkArrangement).filter(WorkArrangement.company_id == company_id,
                                             WorkArrangement.employee_id.in_(employee_ids))
            .order_by(WorkArrangement.effective_from).all())
    for r in rows:
        out.setdefault(r.employee_id, []).append(r)
    return out


def arrangement_on(rows: list[WorkArrangement], day: date) -> WorkArrangement | None:
    found = None
    for r in rows:
        if r.effective_from <= day:
            found = r
    return found


def save_arrangement(db: Session, company_id: int, employee_id: int, data: dict) -> WorkArrangement:
    eff = data.get("effective_from")
    if not isinstance(eff, date):
        raise BadRequest("Choose the date this arrangement starts.")
    days = sorted({int(x) for x in (data.get("working_days") or []) if 0 <= int(x) <= 6})
    alt = {}
    for k, weeks in (data.get("alternate_days") or {}).items():
        if int(k) in days:
            alt[str(int(k))] = sorted({int(w) for w in weeks if 1 <= int(w) <= 5})
    shift_id = data.get("shift_id")
    if shift_id and not shift_by_id(db, company_id, int(shift_id)):
        raise BadRequest("That shift doesn't exist.")
    row = (db.query(WorkArrangement).filter(WorkArrangement.company_id == company_id,
                                            WorkArrangement.employee_id == employee_id,
                                            WorkArrangement.effective_from == eff).first())
    if row is None:
        row = WorkArrangement(company_id=company_id, employee_id=employee_id, effective_from=eff)
        db.add(row)
    row.working_days = days
    row.alternate_days = alt
    row.shift_id = int(shift_id) if shift_id else None
    row.punch_required = bool(data.get("punch_required", True))
    row.auto_shift = bool(data.get("auto_shift", False))
    row.attendance_rule_id = data.get("attendance_rule_id") or None
    row.note = data.get("note")
    db.flush()
    return row


def arrangement_out(r: WorkArrangement) -> dict:
    return {"uuid": str(r.uuid), "effective_from": r.effective_from.isoformat(),
            "shift_id": r.shift_id, "shift_name": r.shift.name if r.shift else None,
            "working_days": r.working_days or [], "alternate_days": r.alternate_days or {},
            "punch_required": r.punch_required, "auto_shift": bool(r.auto_shift),
            "attendance_rule_id": r.attendance_rule_id, "note": r.note}


def _arrangement_works(r: WorkArrangement, day: date) -> bool:
    wd = day.weekday()
    if wd not in (r.working_days or []):
        return False
    weeks = (r.alternate_days or {}).get(str(wd))
    return not weeks or ((day.day - 1) // 7 + 1) in weeks


def _settings_timing(db: Session, company_id: int, employee_ids: list[int]) -> dict[int, set[int]]:
    """Working weekdays from the employee profile's older 'timing' setting."""
    from modules.employees.models import EmployeeSettings
    out = {}
    for es in db.query(EmployeeSettings).filter(EmployeeSettings.company_id == company_id,
                                                EmployeeSettings.employee_id.in_(employee_ids)).all():
        timing = (es.config or {}).get("timing") or {}
        if timing:
            out[es.employee_id] = {i for i, name in enumerate(WEEKDAYS) if (timing.get(name) or {}).get("working")}
    return out


# ── Night-shift consent ─────────────────────────────────────────────

def consents_for(db: Session, company_id: int, employee_ids: list[int]) -> dict[int, list[NightShiftConsent]]:
    out: dict[int, list] = {}
    if not employee_ids:
        return out
    for c in (db.query(NightShiftConsent).filter(NightShiftConsent.company_id == company_id,
                                                 NightShiftConsent.employee_id.in_(employee_ids),
                                                 NightShiftConsent.withdrawn_at.is_(None)).all()):
        out.setdefault(c.employee_id, []).append(c)
    return out


def has_consent(consents: list[NightShiftConsent], day: date) -> bool:
    return any(c.valid_from <= day and (c.valid_to is None or c.valid_to >= day) for c in consents or [])


def consent_out(c: NightShiftConsent, name: str | None = None) -> dict:
    return {"uuid": str(c.uuid), "employee_id": c.employee_id, "employee_name": name,
            "consent_date": c.consent_date.isoformat(), "valid_from": c.valid_from.isoformat(),
            "valid_to": c.valid_to.isoformat() if c.valid_to else None, "safety_measures": c.safety_measures,
            "document_url": c.document_url, "withdrawn": c.withdrawn_at is not None,
            "recorded_by_name": c.recorded_by_name}


def record_consent(db: Session, company_id: int, employee_id: int, data: dict) -> NightShiftConsent:
    if not data.get("valid_from") or not data.get("consent_date"):
        raise BadRequest("Enter the consent date and the date it applies from.")
    if data.get("valid_to") and data["valid_to"] < data["valid_from"]:
        raise BadRequest("The end date can't be before the start date.")
    c = NightShiftConsent(company_id=company_id, employee_id=employee_id, consent_date=data["consent_date"],
                          valid_from=data["valid_from"], valid_to=data.get("valid_to"),
                          safety_measures=data.get("safety_measures"), document_url=data.get("document_url"),
                          recorded_by_name=context.current().actor_name)
    db.add(c)
    db.flush()
    return c


# ── Day plans ───────────────────────────────────────────────────────

@dataclass
class DayPlan:
    employee_id: int
    date: date
    day_type: str                    # working | weekly_off | holiday
    shift: Shift | None = None
    source: str = "default"          # roster | arrangement | leave_calendar | profile | default
    roster_status: str | None = None
    roster_published: bool = True
    holiday_name: str | None = None
    punch_required: bool = True
    rule_id: int | None = None
    half_day: bool = False           # roster says half day
    on_roster_leave: bool = False
    auto_shift: bool = False         # shift picked from the check-in time

    @property
    def working(self) -> bool:
        return self.day_type == "working" or (self.day_type == "holiday" and self.source == "roster"
                                              and self.roster_status == "working")


def day_plans(db: Session, company_id: int, employee_ids: list[int], start: date, end: date,
              include_drafts: bool = False) -> dict[tuple[int, date], DayPlan]:
    from modules.roster.models import RosterEntry
    if not employee_ids:
        return {}
    shifts = {s.id: s for s in db.query(Shift).filter(Shift.company_id == company_id).all()}
    fallback = next((s for s in shifts.values() if s.is_default and s.active), None) or \
        next((s for s in sorted(shifts.values(), key=lambda x: x.id) if s.active), None)
    rq = db.query(RosterEntry).filter(RosterEntry.company_id == company_id, RosterEntry.employee_id.in_(employee_ids),
                                      RosterEntry.date >= start, RosterEntry.date <= end)
    roster = {}
    for r in rq.all():
        if r.published or include_drafts:
            roster[(r.employee_id, r.date)] = r
    arrangements = arrangements_for(db, company_id, employee_ids)
    profile = _settings_timing(db, company_id, employee_ids)
    calendar = adapters.calendar_days(db, company_id, employee_ids, start, end)  # leave module's work calendar
    holidays = adapters.holidays_between(db, company_id, employee_ids, start, end, days=calendar)

    out: dict[tuple[int, date], DayPlan] = {}
    for eid in employee_ids:
        rows = arrangements.get(eid, [])
        for day in adapters.daterange(start, end):
            hol = holidays.get((eid, day))
            arr = arrangement_on(rows, day)
            plan = DayPlan(employee_id=eid, date=day, day_type="working")
            plan.punch_required = arr.punch_required if arr else True
            plan.rule_id = arr.attendance_rule_id if arr else None
            entry = roster.get((eid, day))
            if entry is not None:
                plan.source, plan.roster_status, plan.roster_published = "roster", entry.status, entry.published
                if entry.status == "off":
                    plan.day_type = "weekly_off"
                else:
                    plan.shift = shifts.get(entry.shift_id) or (shifts.get(arr.shift_id) if arr and arr.shift_id else fallback)
                    plan.half_day = entry.status == "half_day"
                    plan.on_roster_leave = entry.status == "leave"
                    plan.day_type = "holiday" if hol else "working"
            elif arr is not None:
                plan.source = "arrangement"
                plan.shift = shifts.get(arr.shift_id) if arr.shift_id else fallback
                plan.day_type = "working" if _arrangement_works(arr, day) else "weekly_off"
            else:
                plan.shift = fallback
                if calendar is not None and (eid, day) in calendar:
                    plan.source = "leave_calendar"
                    kind = calendar[(eid, day)]["kind"]
                    works = kind in ("working", "half", "holiday")
                    plan.half_day = kind == "half"
                elif eid in profile:
                    plan.source = "profile"
                    works = day.weekday() in profile[eid]
                elif fallback is not None:
                    plan.source = "default"
                    works = day.weekday() not in (fallback.weekly_offs or [])
                else:
                    works = day.weekday() < 5
                plan.day_type = "working" if works else "weekly_off"
            if hol and plan.day_type != "weekly_off" and plan.source != "roster":
                plan.day_type = "holiday"
            if hol and plan.day_type == "holiday":
                plan.holiday_name = hol
            out[(eid, day)] = plan
    return out


def plan_window(shift: Shift | None, day: date, tz) -> tuple[datetime, datetime] | None:
    sp = spec(shift)
    return sp.window(day, tz) if sp else None


def closest_shift(first_in: datetime, day: date, candidates: list[Shift], window_minutes: int, tz) -> Shift | None:
    """The shift whose start on `day` is closest to the first check-in, if within the window
    (minutes either side). Ties go to the earlier shift."""
    best, best_gap = None, None
    for sh in sorted(candidates, key=lambda x: (x.start_time, x.id)):
        start = datetime.combine(day, sh.start_time, tzinfo=tz)
        gap = abs((first_in - start).total_seconds()) / 60
        if gap <= window_minutes and (best_gap is None or gap < best_gap):
            best, best_gap = sh, gap
    return best
