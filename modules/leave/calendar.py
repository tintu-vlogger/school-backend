"""Work calendar: which days are working days for an employee.

An employee's holiday calendar is the one set on their location (locations.holiday_calendar_id),
else the first active calendar (by priority) whose applicability covers them, else the
company's default calendar. Their work week is the first matching pattern, else the default.

A work week has seven entries (Monday first): {"mode": "working" | "off" | "half", "off_weeks": [2, 4]}.
`off_weeks` turns a working weekday into a day off on those occurrences in the month, e.g.
Saturday working with off_weeks [2, 4] is the "2nd and 4th Saturday off" pattern.

Day kinds: working, half (half working day), weekend (weekly off), holiday. Optional
(restricted) holidays are working days unless the employee takes one.

Substitute day off (leave settings `substitute_holiday`): a holiday that falls on an
employee's weekly off gives them the next (or previous) working day off instead, shown as a
holiday named "Substitute for <holiday>".

Other modules use `working_days(db, employee, from, to)`, `is_working_day(db, employee, day)`
and `day_map(db, employee, from, to)`.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from modules.leave.facts import FactCache, applies, employee_facts
from modules.leave.models import Holiday, HolidayCalendar, WorkWeekPattern

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

PRESETS = {
    "five_day": ("Monday to Friday", [{"mode": "working"}] * 5 + [{"mode": "off"}, {"mode": "off"}]),
    "six_day": ("Monday to Saturday", [{"mode": "working"}] * 6 + [{"mode": "off"}]),
    "second_fourth_saturday": ("2nd and 4th Saturdays off",
                               [{"mode": "working"}] * 5 + [{"mode": "working", "off_weeks": [2, 4]}, {"mode": "off"}]),
    "alternate_saturday": ("Alternate Saturdays off (1st, 3rd, 5th)",
                           [{"mode": "working"}] * 5 + [{"mode": "working", "off_weeks": [1, 3, 5]}, {"mode": "off"}]),
    "six_day_half_saturday": ("Monday to Friday, half day Saturday",
                              [{"mode": "working"}] * 5 + [{"mode": "half"}, {"mode": "off"}]),
}
FALLBACK_WEEK = PRESETS["five_day"][1]


def clean_days(days) -> list[dict]:
    from core.errors import BadRequest
    if not isinstance(days, list) or len(days) != 7:
        raise BadRequest("Set all seven days of the week.")
    out = []
    for i, d in enumerate(days):
        mode = (d or {}).get("mode")
        if mode not in ("working", "off", "half"):
            raise BadRequest(f"{WEEKDAYS[i]}: choose working, half day or off.")
        weeks = sorted({int(w) for w in ((d or {}).get("off_weeks") or []) if str(w).isdigit() and 1 <= int(w) <= 5})
        out.append({"mode": mode, "off_weeks": weeks if mode != "off" else []})
    if all(d["mode"] == "off" for d in out):
        raise BadRequest("At least one day of the week must be a working day.")
    return out


def week_of_month(d: date) -> int:
    return (d.day - 1) // 7 + 1


def pattern_kind(days: list[dict], d: date) -> str:
    spec = days[d.weekday()] if days and len(days) == 7 else FALLBACK_WEEK[d.weekday()]
    mode = spec.get("mode", "working")
    if mode == "off":
        return "weekend"
    if week_of_month(d) in (spec.get("off_weeks") or []):
        return "weekend"
    return "half" if mode == "half" else "working"


class WorkCalendar:
    """Calendars and patterns of one company, loaded once and reused for many employees."""

    def __init__(self, db: Session, company_id: int):
        self.db, self.company_id = db, company_id
        self.facts_cache = FactCache(db, company_id)
        self.calendars = (db.query(HolidayCalendar).filter(HolidayCalendar.company_id == company_id,
                                                           HolidayCalendar.active.is_(True))
                          .order_by(HolidayCalendar.priority, HolidayCalendar.id).all())
        self.patterns = (db.query(WorkWeekPattern).filter(WorkWeekPattern.company_id == company_id,
                                                          WorkWeekPattern.active.is_(True))
                         .order_by(WorkWeekPattern.priority, WorkWeekPattern.id).all())
        self._holidays: dict[int, dict[date, list[Holiday]]] = {}
        self._emp: dict[int, tuple] = {}
        self._subs: dict[tuple, dict[date, str]] = {}
        try:
            from modules.leave.policy import get_settings
            self.substitute = get_settings(db, company_id).get("substitute_holiday") or {}
        except Exception:
            self.substitute = {}

    # ── resolution ──
    def facts(self, emp) -> dict:
        if emp.id not in self._emp:
            f = employee_facts(self.db, emp, self.facts_cache)
            self._emp[emp.id] = (f, None, None)
        return self._emp[emp.id][0]

    def calendar_for(self, emp) -> HolidayCalendar | None:
        f = self.facts(emp)
        cached = self._emp[emp.id]
        if cached[1] is not None:
            return cached[1] or None
        cal = None
        loc_cal = self.facts_cache.location_calendar(f.get("location_id"))
        if loc_cal:
            cal = next((c for c in self.calendars if c.id == loc_cal), None)
        if cal is None:
            cal = next((c for c in self.calendars if not c.is_default and _has_rules(c.applicability)
                        and applies(c.applicability, f)), None)
        if cal is None:
            cal = next((c for c in self.calendars if c.is_default), None)
        self._emp[emp.id] = (f, cal or False, cached[2])
        return cal

    def pattern_for(self, emp) -> list[dict]:
        f = self.facts(emp)
        cached = self._emp[emp.id]
        if cached[2] is not None:
            return cached[2]
        pat = next((p for p in self.patterns if not p.is_default and _has_rules(p.applicability)
                    and applies(p.applicability, f)), None)
        if pat is None:
            pat = next((p for p in self.patterns if p.is_default), None)
        days = pat.days if pat is not None else FALLBACK_WEEK
        self._emp[emp.id] = (f, cached[1], days)
        return days

    def pattern_name_for(self, emp) -> str | None:
        f = self.facts(emp)
        pat = next((p for p in self.patterns if not p.is_default and _has_rules(p.applicability)
                    and applies(p.applicability, f)), None) or next((p for p in self.patterns if p.is_default), None)
        return pat.name if pat else None

    def holidays_of(self, calendar_id: int) -> dict[date, list[Holiday]]:
        if calendar_id not in self._holidays:
            rows = self.db.query(Holiday).filter(Holiday.company_id == self.company_id,
                                                 Holiday.calendar_id == calendar_id).all()
            out: dict[date, list[Holiday]] = {}
            for h in rows:
                out.setdefault(h.date, []).append(h)
            self._holidays[calendar_id] = out
        return self._holidays[calendar_id]

    # ── days ──
    def day(self, emp, d: date) -> dict:
        """{date, kind: working|half|weekend|holiday, holiday: name, optional: [names], value}"""
        cal = self.calendar_for(emp)
        hols = self.holidays_of(cal.id).get(d, []) if cal else []
        fixed = [h for h in hols if h.kind != "optional"]
        optional = [h.name for h in hols if h.kind == "optional"]
        if fixed:
            return {"date": d, "kind": "holiday", "holiday": fixed[0].name, "holiday_kind": fixed[0].kind,
                    "optional": optional, "value": Decimal("0")}
        pattern = self.pattern_for(emp)
        kind = pattern_kind(pattern, d)
        if kind in ("working", "half") and cal and self.substitute.get("enabled"):
            name = self.substitutes(cal, pattern).get(d)
            if name:
                return {"date": d, "kind": "holiday", "holiday": name, "holiday_kind": "substitute",
                        "optional": optional, "value": Decimal("0")}
        value = Decimal("1") if kind == "working" else Decimal("0.5") if kind == "half" else Decimal("0")
        return {"date": d, "kind": kind, "holiday": None, "optional": optional, "value": value}

    def substitutes(self, cal: HolidayCalendar, pattern: list[dict]) -> dict[date, str]:
        """{substitute date: name} for the holidays of a calendar that fall on a weekly off
        of this work week."""
        key = (cal.id, repr(pattern))
        if key in self._subs:
            return self._subs[key]
        step = -1 if self.substitute.get("when") == "previous_working_day" else 1
        national_only = bool(self.substitute.get("national_only"))
        hols = self.holidays_of(cal.id)
        fixed = {d for d, rows in hols.items() if any(h.kind != "optional" for h in rows)}
        out: dict[date, str] = {}
        for h in sorted(fixed):
            rows = [x for x in hols[h] if x.kind != "optional" and (not national_only or x.kind == "national")]
            if not rows or pattern_kind(pattern, h) != "weekend":
                continue
            d = h + timedelta(days=step)
            for _ in range(21):
                if pattern_kind(pattern, d) == "working" and d not in fixed and d not in out:
                    out[d] = f"Substitute for {rows[0].name}"
                    break
                d += timedelta(days=step)
        self._subs[key] = out
        return out

    def days(self, emp, start: date, end: date) -> list[dict]:
        out, d = [], start
        while d <= end:
            out.append(self.day(emp, d))
            d += timedelta(days=1)
        return out

    def working_days(self, emp, start: date, end: date) -> Decimal:
        return sum((x["value"] for x in self.days(emp, start, end)), Decimal("0"))


def _has_rules(app: dict | None) -> bool:
    app = app or {}
    return any(app.get(k) for k in ("genders", "marital_statuses", "employment_types", "locations", "states",
                                    "departments", "designations", "grades", "tags", "employees", "rule"))


# ── Public service functions for attendance, payroll and others ─────

def working_days(db: Session, employee, start: date, end: date, cal: WorkCalendar | None = None) -> Decimal:
    """Working days between two dates (inclusive) for an employee: weekly offs and
    holidays excluded, half working days count 0.5."""
    cal = cal or WorkCalendar(db, employee.company_id)
    return cal.working_days(employee, start, end)


def is_working_day(db: Session, employee, day: date, cal: WorkCalendar | None = None) -> bool:
    cal = cal or WorkCalendar(db, employee.company_id)
    return cal.day(employee, day)["kind"] in ("working", "half")


def day_map(db: Session, employee, start: date, end: date, cal: WorkCalendar | None = None) -> list[dict]:
    cal = cal or WorkCalendar(db, employee.company_id)
    return cal.days(employee, start, end)
