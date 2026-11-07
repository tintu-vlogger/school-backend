"""Daily attendance calculation, with no database access so it is easy to test.

compute_day(DayInput) -> DayResult works out one employee's day from the punches, the
shift, the attendance rule, and what else is known about the day (holiday, weekly off,
leave, approved work-from-home, on-duty, permission hours and overtime pre-approval).

apply_penalties(days, rule) adds late-mark and early-exit penalties for a month
(e.g. every 3rd late arrival = half a day loss of pay).

Statuses: present, absent, half_day, on_leave, holiday, weekly_off, wfh, on_duty,
missing_punch and not_yet (today, before the shift is over, with no punches yet).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

STATUSES = {
    "present": "Present", "absent": "Absent", "half_day": "Half day", "on_leave": "On leave", "holiday": "Holiday",
    "weekly_off": "Weekly off", "wfh": "Work from home", "on_duty": "On duty", "missing_punch": "Missing punch",
    "not_yet": "Not in yet",
}

DEFAULT_RULE: dict = {
    "hours_calc": "first_last",            # first_last | pairs
    "full_day_hours": 8.0,                 # used when the shift does not set its own
    "half_day_hours": 4.0,
    "late_grace_minutes": None,            # None = the shift's grace
    "early_grace_minutes": None,
    "half_day_if_late_by_minutes": None,   # e.g. 120: arriving 2 h late makes it a half day
    "half_day_if_early_by_minutes": None,
    "late_penalty": {"enabled": True, "count": 3, "deduct_days": 0.5, "every_multiple": True},
    "early_penalty": {"enabled": False, "count": 3, "deduct_days": 0.5, "every_multiple": True},
    "rounding": {"in_minutes": 0, "out_minutes": 0},
    "auto_absent": True,                   # no punch on a working day = absent
    "missing_punch_payable": 0.0,          # one punch only: payable days until it is corrected
    "count_permission_hours": True,        # approved permission hours count as worked
    "overtime": {
        "enabled": True, "min_minutes": 30, "rate_multiplier": 2.0, "requires_preapproval": False,
        "daily_cap_minutes": 240, "off_day_all_overtime": True,
        "quarterly_cap_hours": 125, "alert_at_percent": 80, "count_early_arrival": False,
    },
    "night_window": {"start": "22:00", "end": "06:00"},
    "punch_window": {"before_minutes": 240, "after_minutes": 600},
}


def merged_rule(config: dict | None) -> dict:
    """The rule's config with defaults for anything missing (nested dicts merged)."""
    out = copy.deepcopy(DEFAULT_RULE)
    for k, v in (config or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def parse_hhmm(value) -> time | None:
    if value is None or value == "":
        return None
    if isinstance(value, time):
        return value
    parts = str(value).strip().split(":")
    return time(int(parts[0]) % 24, int(parts[1]) if len(parts) > 1 else 0)


@dataclass
class ShiftSpec:
    id: int | None
    name: str
    start: time
    end: time
    breaks: list = field(default_factory=list)
    grace_in: int = 10
    grace_out: int = 10
    half_day_hours: float | None = None
    full_day_hours: float | None = None
    is_flexible: bool = False
    core_start: time | None = None
    core_end: time | None = None
    overtime_after_minutes: int = 30
    allowance_amount: float = 0.0
    allowance_min_hours: float | None = None

    @property
    def crosses_midnight(self) -> bool:
        return self.end <= self.start

    def window(self, d: date, tz) -> tuple[datetime, datetime]:
        start = datetime.combine(d, self.start, tzinfo=tz)
        end = datetime.combine(d + timedelta(days=1) if self.crosses_midnight else d, self.end, tzinfo=tz)
        return start, end

    def break_intervals(self, d: date, tz, paid: bool | None = False) -> list[tuple[datetime, datetime]]:
        """Break periods on the shift that starts on date d (paid=None for all)."""
        out = []
        start, _ = self.window(d, tz)
        for b in self.breaks or []:
            if paid is not None and bool(b.get("paid")) != paid:
                continue
            bs, be = parse_hhmm(b.get("start")), parse_hhmm(b.get("end"))
            if bs is None or be is None:
                continue
            day = d + timedelta(days=1) if (self.crosses_midnight and bs < self.start) else d
            s = datetime.combine(day, bs, tzinfo=tz)
            e = datetime.combine(day + timedelta(days=1) if be <= bs else day, be, tzinfo=tz)
            out.append((s, e))
        return out

    def scheduled_minutes(self) -> int:
        """Shift length less unpaid breaks."""
        d = date(2000, 1, 3)
        tz = timezone.utc
        s, e = self.window(d, tz)
        total = int((e - s).total_seconds() // 60)
        unpaid = sum(int((be - bs).total_seconds() // 60) for bs, be in self.break_intervals(d, tz, paid=False))
        return max(total - unpaid, 0)

    def night_overlap_minutes(self, night_start: time, night_end: time) -> int:
        d = date(2000, 1, 3)
        tz = timezone.utc
        s, e = self.window(d, tz)
        return _night_minutes(s, e, tz, night_start, night_end)


@dataclass
class DayInput:
    date: date
    tz: ZoneInfo
    rule: dict
    shift: ShiftSpec | None = None
    day_type: str = "working"            # working | weekly_off | holiday
    holiday_name: str | None = None
    punches: list = field(default_factory=list)  # [(utc datetime, "in" | "out" | None)]
    leave: dict | None = None            # {"unit": "full" | "first_half" | "second_half", "paid": bool, "name": str,
                                         #  "pay_percent": 100 (e.g. 50 for half-pay sick leave)}
    wfh: str | None = None               # None | full_day | first_half | second_half
    on_duty: str | None = None           # None | full_day | first_half | second_half
    permission_minutes: int = 0
    overtime_approved_minutes: int | None = None
    punch_required: bool = True
    override_status: str | None = None
    now: datetime | None = None          # for today: decides "not yet" and running hours


@dataclass
class DayResult:
    status: str
    day_type: str
    payable_days: Decimal
    lop_days: Decimal
    shift_start: datetime | None = None
    shift_end: datetime | None = None
    first_in: datetime | None = None
    last_out: datetime | None = None
    punch_count: int = 0
    worked_minutes: int = 0
    break_minutes: int = 0
    late_minutes: int = 0
    early_minutes: int = 0
    overtime_minutes: int = 0
    unapproved_overtime_minutes: int = 0
    night_minutes: int = 0
    permission_minutes: int = 0
    is_late: bool = False
    is_early_exit: bool = False
    is_night_shift: bool = False
    shift_allowance: bool = False
    pay_cut_days: Decimal = Decimal("0.00")  # pay lost on reduced-pay leave (part of lop_days)
    notes: list = field(default_factory=list)


def _leave_pay(leave: dict | None) -> float:
    """Share of a day's pay kept on this leave: 0 when unpaid, 0.5 for half-pay leave."""
    if not leave or not leave.get("paid", True):
        return 0.0
    try:
        pct = float(leave.get("pay_percent", 100))
    except (TypeError, ValueError):
        pct = 100.0
    return max(0.0, min(pct, 100.0)) / 100


def _d(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"))


def _minutes(a: datetime, b: datetime) -> int:
    return max(int((b - a).total_seconds() // 60), 0)


def _overlap(a1, a2, b1, b2) -> int:
    s, e = max(a1, b1), min(a2, b2)
    return _minutes(s, e) if e > s else 0


def _night_minutes(s: datetime, e: datetime, tz, ns: time, ne: time) -> int:
    """Minutes of [s, e) inside the night window (e.g. 22:00 to 06:00), across days."""
    if e <= s:
        return 0
    total = 0
    day = s.astimezone(tz).date() - timedelta(days=1)
    last = e.astimezone(tz).date()
    while day <= last:
        ws = datetime.combine(day, ns, tzinfo=tz)
        we = datetime.combine(day + timedelta(days=1) if ne <= ns else day, ne, tzinfo=tz)
        total += _overlap(s, e, ws, we)
        day += timedelta(days=1)
    return total


def _round_up(dt: datetime, step: int) -> datetime:
    if not step:
        return dt
    base = dt.replace(second=0, microsecond=0)
    if base < dt:
        base += timedelta(minutes=1)
    return base + timedelta(minutes=(step - base.minute % step) % step)


def _round_down(dt: datetime, step: int) -> datetime:
    if not step:
        return dt
    base = dt.replace(second=0, microsecond=0)
    return base - timedelta(minutes=base.minute % step)


def is_night_shift(shift: ShiftSpec | None, rule: dict) -> bool:
    """A shift is a night shift when it crosses midnight or has 2+ hours in the night window."""
    if shift is None:
        return False
    if shift.crosses_midnight:
        return True
    nw = rule.get("night_window") or {}
    return shift.night_overlap_minutes(parse_hhmm(nw.get("start", "22:00")), parse_hhmm(nw.get("end", "06:00"))) >= 120


def _pairs(punches: list) -> tuple[list[tuple[datetime, datetime]], bool]:
    """In/out pairs. Directions are used when present, otherwise alternate in/out.
    Returns (pairs, complete) where complete is False when an in has no out."""
    pairs, open_in = [], None
    for ts, direction in punches:
        if direction == "in" or (direction is None and open_in is None):
            if open_in is None:
                open_in = ts
        else:  # out (or an unlabelled punch closing an in)
            if open_in is not None:
                pairs.append((open_in, ts))
                open_in = None
    return pairs, open_in is None


def compute_day(inp: DayInput) -> DayResult:
    rule = merged_rule(inp.rule)
    tz = inp.tz
    shift = inp.shift
    punches = sorted(((p[0] if p[0].tzinfo else p[0].replace(tzinfo=timezone.utc)), p[1]) for p in inp.punches)
    res = DayResult(status="absent", day_type=inp.day_type, payable_days=_d(0), lop_days=_d(0))
    res.punch_count = len(punches)
    res.permission_minutes = int(inp.permission_minutes or 0)
    notes = res.notes

    if shift:
        res.shift_start, res.shift_end = shift.window(inp.date, tz)
        res.is_night_shift = is_night_shift(shift, rule)

    # ── Hours from punches ──
    first_in = punches[0][0] if punches else None
    last_out = punches[-1][0] if len(punches) > 1 else None
    running = False
    if first_in and last_out is None and inp.now is not None:
        # today and still clocked in: count hours so far
        end_of_window = (res.shift_end or datetime.combine(inp.date, time(23, 59), tzinfo=tz)) \
            + timedelta(minutes=int(rule["punch_window"]["after_minutes"]))
        if inp.now < end_of_window:
            running = True
    rnd = rule.get("rounding") or {}
    worked = 0
    break_minutes = 0
    if first_in:
        res.first_in = first_in
        res.last_out = last_out
        r_in = _round_up(first_in, int(rnd.get("in_minutes") or 0))
        r_out = _round_down(last_out, int(rnd.get("out_minutes") or 0)) if last_out else (inp.now if running else None)
        if r_out and r_out > r_in:
            if rule["hours_calc"] == "pairs" and len(punches) > 2:
                pairs, _complete = _pairs(punches)
                if running and not _complete:
                    pairs.append((punches[-1][0], inp.now))
                worked = sum(_minutes(a, b) for a, b in pairs)
                break_minutes = max(_minutes(r_in, r_out) - worked, 0)
            else:
                worked = _minutes(r_in, r_out)
                if shift:
                    unpaid = sum(_overlap(r_in, r_out, bs, be) for bs, be in shift.break_intervals(inp.date, tz, paid=False))
                    worked -= unpaid
                    break_minutes = unpaid
        worked = max(worked, 0)
        nw = rule.get("night_window") or {}
        if r_out:
            res.night_minutes = _night_minutes(r_in, r_out, tz, parse_hhmm(nw.get("start", "22:00")),
                                               parse_hhmm(nw.get("end", "06:00")))
    res.worked_minutes = worked
    res.break_minutes = break_minutes

    # ── Late and early ──
    if shift and first_in and inp.day_type == "working":
        late_grace = rule.get("late_grace_minutes")
        early_grace = rule.get("early_grace_minutes")
        late_grace = shift.grace_in if late_grace is None else int(late_grace)
        early_grace = shift.grace_out if early_grace is None else int(early_grace)
        if shift.is_flexible:
            ref_in = datetime.combine(inp.date, shift.core_start, tzinfo=tz) if shift.core_start else None
            ref_out = None
            if shift.core_end:
                day = inp.date + timedelta(days=1) if (shift.core_start and shift.core_end <= shift.core_start) else inp.date
                ref_out = datetime.combine(day, shift.core_end, tzinfo=tz)
        else:
            ref_in, ref_out = res.shift_start, res.shift_end
        if ref_in is not None:
            late = _minutes(ref_in, first_in)
            if late > late_grace:
                res.late_minutes, res.is_late = late, True
                notes.append(f"Late by {_hm(late)}")
        if ref_out is not None and last_out is not None and not running:
            early = _minutes(last_out, ref_out)
            if early > early_grace:
                res.early_minutes, res.is_early_exit = early, True
                notes.append(f"Left early by {_hm(early)}")

    # ── Minimum hours ──
    full_h = shift.full_day_hours if shift and shift.full_day_hours else rule["full_day_hours"]
    half_h = shift.half_day_hours if shift and shift.half_day_hours else rule["half_day_hours"]
    full_min, half_min = int(float(full_h) * 60), int(float(half_h) * 60)

    # ── Overtime ──
    ot_rule = rule.get("overtime") or {}

    def overtime(worked_min: int, off_day: bool) -> None:
        if not ot_rule.get("enabled") or worked_min <= 0:
            return
        if off_day and ot_rule.get("off_day_all_overtime", True):
            extra = worked_min
        else:
            base = shift.scheduled_minutes() if shift else full_min
            extra = worked_min - base
            if shift and not shift.is_flexible and first_in and not ot_rule.get("count_early_arrival"):
                # time before the shift starts is not overtime unless the rule says so
                extra -= _minutes(first_in, res.shift_start) if first_in < res.shift_start else 0
            threshold = max(int(ot_rule.get("min_minutes") or 0), shift.overtime_after_minutes if shift else 0)
            if extra < threshold:
                return
        cap = ot_rule.get("daily_cap_minutes")
        if cap:
            extra = min(extra, int(cap))
        if ot_rule.get("requires_preapproval"):
            approved = min(extra, int(inp.overtime_approved_minutes or 0))
            res.overtime_minutes = approved
            res.unapproved_overtime_minutes = extra - approved
            if extra - approved > 0:
                notes.append(f"{_hm(extra - approved)} extra time without overtime approval")
        else:
            res.overtime_minutes = extra
        if res.overtime_minutes:
            notes.append(f"Overtime {_hm(res.overtime_minutes)}")

    pay_cut = [0.0]

    def finish(status: str, payable: float, lop: float | None = None) -> DayResult:
        res.status = status
        res.pay_cut_days = _d(pay_cut[0])
        res.payable_days = _d(payable)
        res.lop_days = _d(0 if lop is None else lop)
        if shift and shift.allowance_amount and status in ("present", "half_day", "wfh", "on_duty"):
            need = float(shift.allowance_min_hours or 0) * 60
            res.shift_allowance = worked >= need or not inp.punch_required
        if not (status in ("present", "half_day", "wfh", "on_duty") or (status in ("holiday", "weekly_off") and worked)):
            res.is_night_shift = False
        return res

    # ── Hand-set status wins ──
    if inp.override_status:
        notes.append("Status set by HR")
        payable = {"present": 1, "half_day": 0.5, "wfh": 1, "on_duty": 1, "holiday": 1, "weekly_off": 1}.get(
            inp.override_status, 0)
        if inp.override_status == "on_leave":
            payable = _leave_pay(inp.leave or {"paid": True})
            if (inp.leave or {}).get("paid", True):
                pay_cut[0] = 1 - payable
        lop = 0 if inp.override_status in ("holiday", "weekly_off") else 1 - payable
        res.is_late = res.is_early_exit = False
        return finish(inp.override_status, payable, lop)

    # ── Holidays and weekly offs ──
    if inp.day_type in ("holiday", "weekly_off"):
        if inp.day_type == "holiday" and inp.holiday_name:
            notes.insert(0, inp.holiday_name)
        if len(punches) == 1:
            notes.append("Only one punch on a day off")
        elif worked:
            notes.append(f"Worked {_hm(worked)} on a {'holiday' if inp.day_type == 'holiday' else 'weekly off'}")
            overtime(worked, off_day=True)
        return finish(inp.day_type, 1, 0)

    leave = inp.leave or None
    if leave and leave.get("unit", "full") == "full":
        keep = _leave_pay(leave)
        label = "" if leave.get("paid", True) else " (unpaid)"
        if leave.get("paid", True) and keep < 1:
            label = f" ({leave.get('pay_percent')} % pay)"
            pay_cut[0] = 1 - keep
        notes.insert(0, f"{leave.get('name') or 'Leave'}{label}")
        return finish("on_leave", keep, 1 - keep)
    if inp.on_duty == "full_day":
        notes.insert(0, "On duty")
        return finish("on_duty", 1, 0)
    if inp.wfh == "full_day":
        notes.insert(0, "Working from home")
        res.is_late = res.is_early_exit = False
        res.late_minutes = res.early_minutes = 0
        notes[:] = [n for n in notes if not n.startswith(("Late", "Left early"))]
        return finish("wfh", 1, 0)
    if not inp.punch_required:
        notes.insert(0, "Present by default (no punching needed)")
        return finish("present", 1, 0)

    # half-day cover from leave, on duty or WFH
    covered, covered_paid = 0.0, 0.0
    if leave and leave.get("unit") in ("first_half", "second_half"):
        covered = 0.5
        covered_paid = 0.5 * _leave_pay(leave)
        if leave.get("paid", True):
            pay_cut[0] = 0.5 - covered_paid
        notes.insert(0, f"Half-day {leave.get('name') or 'leave'}")
    elif inp.on_duty in ("first_half", "second_half"):
        covered = covered_paid = 0.5
        notes.insert(0, "Half-day on duty")
    elif inp.wfh in ("first_half", "second_half"):
        covered = covered_paid = 0.5
        notes.insert(0, "Half-day work from home")
    if covered:
        # the half not worked in the office is not late or early
        if (leave or {}).get("unit") == "first_half" or inp.on_duty == "first_half" or inp.wfh == "first_half":
            res.is_late, res.late_minutes = False, 0
        else:
            res.is_early_exit, res.early_minutes = False, 0
        notes[:] = [n for n in notes if not (n.startswith("Late") and not res.is_late)
                    and not (n.startswith("Left early") and not res.is_early_exit)]

    if not punches:
        if inp.now is not None and res.shift_end is not None and inp.now < res.shift_end:
            return finish("not_yet", 0, 0)
        if inp.now is not None and res.shift_end is None and inp.now.astimezone(tz).date() <= inp.date:
            return finish("not_yet", 0, 0)
        if covered:
            notes.append("No punches for the other half")
            return finish("half_day", covered_paid, 1 - covered_paid)
        if not rule.get("auto_absent", True):
            notes.append("No punches")
        return finish("absent", 0, 1)

    if last_out is None and not running:
        notes.append("Only one punch: check-out missing" if punches[0][1] != "out" else "Only one punch: check-in missing")
        pay = max(float(rule.get("missing_punch_payable") or 0), covered_paid)
        return finish("missing_punch", pay, 1 - pay)

    effective = worked + (res.permission_minutes if rule.get("count_permission_hours", True) else 0)
    if res.permission_minutes:
        notes.append(f"Permission {_hm(res.permission_minutes)}")
    if running:
        notes.append("Still clocked in")
        status, payable = "present", 1.0
    elif covered:
        status, payable = ("half_day", 0.5 + covered_paid) if effective >= half_min else ("half_day", covered_paid)
        if effective < half_min:
            notes.append(f"Short hours: {_hm(effective)}")
    elif effective >= full_min:
        status, payable = "present", 1.0
    elif effective >= half_min:
        status, payable = "half_day", 0.5
        notes.append(f"Short hours: {_hm(effective)} worked")
    else:
        status, payable = "absent", 0.0
        notes.append(f"Short hours: {_hm(effective)} worked")

    if status == "present" and not running:
        hl = rule.get("half_day_if_late_by_minutes")
        he = rule.get("half_day_if_early_by_minutes")
        if (hl and res.late_minutes > int(hl)) or (he and res.early_minutes > int(he)):
            status, payable = "half_day", 0.5
            notes.append("Half day: very late or very early")
    if not running:
        overtime(worked, off_day=False)
    return finish(status, payable, max(1 - payable, 0))


def apply_penalties(days: list[dict], rule: dict) -> dict:
    """Late-mark and early-exit penalties for one employee's month.

    days: [{"date": date, "is_late": bool, "is_early_exit": bool, "status": str}] in date
    order. Returns {date: penalty_lop_days} for the days that trigger a penalty (the 3rd,
    6th, ... late arrival when count is 3 and every_multiple is on)."""
    rule = merged_rule(rule)
    out: dict = {}
    counted = {"present", "half_day"}
    for flag, key in (("is_late", "late_penalty"), ("is_early_exit", "early_penalty")):
        p = rule.get(key) or {}
        if not p.get("enabled") or not p.get("count"):
            continue
        n, count, deduct = 0, int(p["count"]), float(p.get("deduct_days") or 0.5)
        for d in days:
            if d.get(flag) and d.get("status") in counted:
                n += 1
                if n % count == 0 and (p.get("every_multiple", True) or n == count):
                    out[d["date"]] = out.get(d["date"], 0) + deduct
    return {k: _d(v) for k, v in out.items()}


def _hm(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    if h and m:
        return f"{h} h {m} min"
    return f"{h} h" if h else f"{m} min"
