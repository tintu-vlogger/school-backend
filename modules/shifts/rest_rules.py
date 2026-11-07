"""Rest and safety checks for a roster, with no database access.

check_schedule(days, rules, consent) looks at one employee's planned days and returns
the problems found:
- daily hours above the limit (OSH Code: 8 hours a day by default; states may allow 9)
- weekly hours above the limit (48 a week)
- too little rest between the end of one shift and the start of the next
- no weekly rest day (more than 6 working days in a row)
- too many night shifts in a row
- a woman rostered on a night shift without valid written consent (always blocks)

Each rule has an action: "block" (the roster cannot be saved), "warn" or "off".
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

DEFAULT_REST_RULES: dict = {
    "max_daily_hours": {"value": 9, "action": "warn"},
    "max_weekly_hours": {"value": 48, "action": "warn"},
    "min_rest_hours": {"value": 10, "action": "block"},
    "max_consecutive_days": {"value": 6, "action": "block"},
    "max_consecutive_nights": {"value": 6, "action": "warn"},
    # OSH Code s.43: women work between 7 pm and 6 am only with consent
    "women_night_window": {"start": "19:00", "end": "06:00"},
    "women_night_consent": {"action": "block"},
}


@dataclass
class PlannedDay:
    date: date
    working: bool
    start: datetime | None = None   # local, timezone-aware
    end: datetime | None = None
    net_minutes: int = 0
    night: bool = False             # a night shift (for the consecutive-nights rule)
    women_night: bool = False       # overlaps the women's night window
    shift_name: str | None = None
    new: bool = False               # part of the change being checked


@dataclass
class Violation:
    rule: str
    action: str
    date: date
    message: str

    def as_dict(self) -> dict:
        return {"rule": self.rule, "action": self.action, "date": self.date.isoformat(), "message": self.message}


def merged_rules(config: dict | None) -> dict:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_REST_RULES.items()}
    for k, v in (config or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def _on(rules: dict, key: str) -> bool:
    r = rules.get(key) or {}
    return r.get("action", "off") != "off"


def check_schedule(days: list[PlannedDay], rules: dict | None, *, is_woman: bool = False,
                   has_consent=lambda d: False) -> list[Violation]:
    """Check one employee's plan. Only problems touching a day marked `new` are returned,
    so old problems don't block unrelated changes."""
    rules = merged_rules(rules)
    days = sorted(days, key=lambda x: x.date)
    found: list[Violation] = []
    new_dates = {d.date for d in days if d.new}

    def add(rule: str, d: date, msg: str, touches: set[date]):
        if touches & new_dates:
            found.append(Violation(rule, rules[rule]["action"] if rule in rules else "block", d, msg))

    working = [d for d in days if d.working and d.start and d.end]

    if _on(rules, "max_daily_hours"):
        limit = float(rules["max_daily_hours"]["value"]) * 60
        for d in working:
            if d.net_minutes > limit:
                add("max_daily_hours", d.date,
                    f"{d.date:%d %b}: {d.shift_name or 'shift'} is {d.net_minutes / 60:.1f} h, above the "
                    f"{rules['max_daily_hours']['value']} h daily limit", {d.date})

    if _on(rules, "max_weekly_hours"):
        limit = float(rules["max_weekly_hours"]["value"]) * 60
        weeks: dict[date, list[PlannedDay]] = {}
        for d in working:
            weeks.setdefault(d.date - timedelta(days=d.date.weekday()), []).append(d)
        for monday, items in weeks.items():
            total = sum(x.net_minutes for x in items)
            if total > limit:
                add("max_weekly_hours", monday,
                    f"Week of {monday:%d %b}: {total / 60:.1f} h planned, above the "
                    f"{rules['max_weekly_hours']['value']} h weekly limit", {x.date for x in items})

    if _on(rules, "min_rest_hours"):
        need = float(rules["min_rest_hours"]["value"]) * 60
        for a, b in zip(working, working[1:]):
            gap = (b.start - a.end).total_seconds() / 60
            if gap < need:
                add("min_rest_hours", b.date,
                    f"{b.date:%d %b}: only {max(gap, 0) / 60:.1f} h rest after the shift on {a.date:%d %b} "
                    f"(minimum {rules['min_rest_hours']['value']} h)", {a.date, b.date})

    if _on(rules, "max_consecutive_days"):
        limit = int(rules["max_consecutive_days"]["value"])
        run: list[PlannedDay] = []
        for d in days:
            if d.working and (not run or d.date == run[-1].date + timedelta(days=1)):
                run.append(d)
            elif d.working:
                run = [d]
            else:
                run = []
            if len(run) == limit + 1:
                add("max_consecutive_days", d.date,
                    f"{d.date:%d %b}: {limit + 1} working days in a row with no weekly rest day",
                    {x.date for x in run})

    if _on(rules, "max_consecutive_nights"):
        limit = int(rules["max_consecutive_nights"]["value"])
        run = []
        for d in days:
            if d.working and d.night and (not run or d.date == run[-1].date + timedelta(days=1)):
                run.append(d)
            elif d.working and d.night:
                run = [d]
            else:
                run = []
            if len(run) == limit + 1:
                add("max_consecutive_nights", d.date, f"{d.date:%d %b}: more than {limit} night shifts in a row",
                    {x.date for x in run})

    if is_woman and _on(rules, "women_night_consent"):
        for d in working:
            if d.women_night and d.new and not has_consent(d.date):
                found.append(Violation(
                    "women_night_consent", "block", d.date,
                    f"{d.date:%d %b}: night shift needs her written consent first (OSH Code). Record consent "
                    f"under Shifts > Night-shift consent."))
    return found
