"""Indian defaults every company starts with (all editable on screen):

- leave types: Earned (OSH Code: 1 day per 20 days worked after 180 days), Casual, Sick,
  Maternity (26 weeks; 12 weeks from the third child, adoption and commissioning mothers),
  Paternity, Bereavement, Marriage, Compensatory off, Loss of pay, Optional holiday
- work weeks: Monday to Friday (default), 2nd and 4th Saturday off, Monday to Saturday
- holiday calendars: an all-India calendar (national holidays, common festivals, optional
  holidays) and calendars for several states with their own festival holidays

`ensure_leave_defaults(db, company_id)` only adds what is missing, so it runs at sign-up and
at every start.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from modules.leave import policy
from modules.leave.calendar import PRESETS
from modules.leave.models import Holiday, HolidayCalendar, LeaveType, WorkWeekPattern
from modules.leave.rules import clean_rules

_NO_CARRY = {"carry_forward": {"enabled": False}, "encashment": {"enabled": False}}

LEAVE_TYPES = [
    {
        "code": "EL", "name": "Earned leave", "color": "#4f46e5", "icon": "sun", "sort_order": 10,
        "description": "Paid annual leave (also called privileged leave), earned for days worked as the OSH Code requires.",
        "rules": {
            "statutory": True, "legal_kind": "annual",
            "entitlement": {"mode": "days_worked", "frequency": "monthly", "credit_at": "end",
                            "days_worked": {"per_days": 20, "credit": 1, "min_days_in_year": 180}},
            "year_end": {"carry_forward": {"enabled": True, "max_days": 30, "percent": 100},
                         "encashment": {"enabled": True, "max_days": 15, "percent": 100, "on_exit": True}},
            "restrictions": {"notice_days": 7, "notice_above_days": 2, "backdated_days": 7,
                             "sandwich": {"weekends": True, "holidays": True, "min_days": 3},
                             "negative": {"mode": "none"}},
        },
    },
    {
        "code": "CL", "name": "Casual leave", "color": "#0ea5e9", "icon": "coffee", "sort_order": 20,
        "description": "Short leave for personal matters.",
        "rules": {
            "entitlement": {"mode": "accrual", "yearly_quota": 12, "frequency": "monthly", "credit_at": "start",
                            "probation": {"credit": "full"}},
            "year_end": _NO_CARRY,
            "restrictions": {"max_days": 3, "max_consecutive": 3, "backdated_days": 3, "clubbing_blocked": ["EL"],
                             "negative": {"mode": "lop"}},
        },
    },
    {
        "code": "SL", "name": "Sick leave", "color": "#e11d48", "icon": "thermometer", "sort_order": 30,
        "description": "Leave when you are unwell. A medical certificate is needed for more than 2 days.",
        "rules": {
            "legal_kind": "sick",
            "entitlement": {"mode": "fixed", "yearly_quota": 12},
            "year_end": {"carry_forward": {"enabled": True, "max_days": 30, "percent": 100},
                         "encashment": {"enabled": False}},
            "restrictions": {"backdated_days": 15, "document_after_days": 2, "negative": {"mode": "limit", "limit": 2}},
        },
    },
    {
        "code": "ML", "name": "Maternity leave", "color": "#db2777", "icon": "baby", "sort_order": 40,
        "description": "Paid maternity benefit under the Code on Social Security, 2020.",
        "rules": {
            "statutory": True, "legal_kind": "maternity", "allow_half_day": False,
            "applicability": {"mode": "pickers", "rule": "employee.gender == 'F'"},
            "entitlement": {"mode": "grant"},
            "year_end": {"reset": True, **_NO_CARRY},
            "restrictions": {"count_weekends": True, "count_holidays": True, "backdated_days": 60,
                             "future_days": 365, "document_after_days": 0},
            "grant": {"options": [
                {"key": "birth", "label": "Birth (first two children)", "days": 182},
                {"key": "birth_third", "label": "Birth (third child onwards)", "days": 84},
                {"key": "adoption", "label": "Adoption of a child under 3 months", "days": 84},
                {"key": "commissioning", "label": "Commissioning mother (surrogacy)", "days": 84},
                {"key": "miscarriage", "label": "Miscarriage or medical termination", "days": 42},
            ]},
        },
    },
    {
        "code": "PTL", "name": "Paternity leave", "color": "#0d9488", "icon": "baby", "sort_order": 50,
        "description": "Leave for a new father, on the birth or adoption of a child.",
        "rules": {
            "legal_kind": "paternity", "allow_half_day": False,
            "applicability": {"mode": "pickers", "rule": "employee.gender == 'M'"},
            "entitlement": {"mode": "grant"},
            "year_end": _NO_CARRY,
            "restrictions": {"backdated_days": 30, "future_days": 180},
            "grant": {"options": [{"key": "birth", "label": "Birth of a child", "days": 5},
                                  {"key": "adoption", "label": "Adoption", "days": 5}],
                      "max_requests": 2, "period": "tenure"},
        },
    },
    {
        "code": "BRV", "name": "Bereavement leave", "color": "#64748b", "icon": "flower", "sort_order": 60,
        "description": "Leave on the death of a family member.",
        "rules": {
            "allow_half_day": False,
            "entitlement": {"mode": "grant"},
            "year_end": _NO_CARRY,
            "restrictions": {"backdated_days": 15},
            "grant": {"options": [{"key": "immediate", "label": "Immediate family (spouse, child, parent, sibling)", "days": 5},
                                  {"key": "extended", "label": "Grandparent or in-law", "days": 3}]},
        },
    },
    {
        "code": "MRG", "name": "Marriage leave", "color": "#f59e0b", "icon": "heart", "sort_order": 70,
        "description": "Leave for your own wedding.",
        "rules": {
            "allow_half_day": False,
            "entitlement": {"mode": "grant"},
            "year_end": _NO_CARRY,
            "restrictions": {"notice_days": 15, "backdated_days": 0},
            "grant": {"options": [{"key": "own", "label": "Own marriage", "days": 5}], "max_requests": 1,
                      "period": "tenure"},
        },
    },
    {
        "code": "CO", "name": "Compensatory off", "color": "#8b5cf6", "icon": "briefcase", "sort_order": 80, "kind": "comp_off",
        "description": "A day off for working on a weekly off or a holiday.",
        "rules": {
            "entitlement": {"mode": "none"},
            "year_end": {"reset": False, **_NO_CARRY},
            "restrictions": {"backdated_days": 30, "negative": {"mode": "none"}},
        },
    },
    {
        "code": "LOP", "name": "Loss of pay", "color": "#78716c", "icon": "ban", "sort_order": 90, "kind": "lop",
        "description": "Unpaid leave. These days are deducted from salary.",
        "rules": {
            "paid": "lop",
            "entitlement": {"mode": "none"},
            "year_end": {"reset": True, **_NO_CARRY},
            "restrictions": {"backdated_days": 30},
        },
    },
    {
        "code": "OH", "name": "Optional holiday", "color": "#16a34a", "icon": "party-popper", "sort_order": 100,
        "kind": "optional_holiday",
        "description": "Pick from the optional (restricted) holidays in your holiday calendar.",
        "rules": {
            "allow_half_day": False,
            "entitlement": {"mode": "fixed", "yearly_quota": 2,
                            "proration": {"on_joining": False, "on_exit": False}},
            "year_end": _NO_CARRY,
            "restrictions": {"only_optional_holidays": True, "backdated_days": 0},
        },
    },
]


# Holidays. Festival dates follow the lunar calendar; check them against the official
# notification each year (they are editable).
NATIONAL = [  # compulsory everywhere
    ("Republic Day", "01-26"), ("Independence Day", "08-15"), ("Gandhi Jayanti", "10-02"),
]
FESTIVALS = {
    2026: [("Holi", "2026-03-04"), ("Good Friday", "2026-04-03"), ("Id-ul-Fitr", "2026-03-21"),
           ("May Day", "2026-05-01"), ("Dussehra", "2026-10-20"), ("Diwali", "2026-11-08"),
           ("Christmas", "2026-12-25")],
    2027: [("Holi", "2027-03-22"), ("Good Friday", "2027-03-26"), ("May Day", "2027-05-01"),
           ("Diwali", "2027-10-29"), ("Christmas", "2027-12-25")],
}
OPTIONAL = {
    2026: [("Makar Sankranti / Pongal", "2026-01-14"), ("Maha Shivaratri", "2026-02-15"),
           ("Ugadi / Gudi Padwa", "2026-03-19"), ("Ram Navami", "2026-03-26"), ("Mahavir Jayanti", "2026-03-31"),
           ("Buddha Purnima", "2026-05-01"), ("Bakrid", "2026-05-27"), ("Raksha Bandhan", "2026-08-28"),
           ("Janmashtami", "2026-09-04"), ("Ganesh Chaturthi", "2026-09-14"), ("Guru Nanak Jayanti", "2026-11-24")],
    2027: [("Makar Sankranti / Pongal", "2027-01-14"), ("Maha Shivaratri", "2027-03-06"),
           ("Raksha Bandhan", "2027-08-17"), ("Guru Nanak Jayanti", "2027-11-14")],
}
STATES = {
    # state code -> (calendar name, [(holiday, date)])
    "KA": ("Karnataka", [("Ugadi", "2026-03-19"), ("Ganesh Chaturthi", "2026-09-14"),
                         ("Kannada Rajyotsava", "2026-11-01"), ("Kannada Rajyotsava", "2027-11-01")]),
    "MH": ("Maharashtra", [("Chhatrapati Shivaji Maharaj Jayanti", "2026-02-19"), ("Gudi Padwa", "2026-03-19"),
                           ("Ambedkar Jayanti", "2026-04-14"), ("Maharashtra Day", "2026-05-01"),
                           ("Ganesh Chaturthi", "2026-09-14"), ("Maharashtra Day", "2027-05-01")]),
    "TN": ("Tamil Nadu", [("Pongal", "2026-01-15"), ("Thiruvalluvar Day", "2026-01-16"),
                          ("Tamil New Year", "2026-04-14"), ("Pongal", "2027-01-15"), ("Tamil New Year", "2027-04-14")]),
    "TS": ("Telangana", [("Ugadi", "2026-03-19"), ("Telangana Formation Day", "2026-06-02"),
                         ("Telangana Formation Day", "2027-06-02")]),
    "KL": ("Kerala", [("Vishu", "2026-04-15"), ("Thiruvonam", "2026-08-26"), ("Kerala Piravi", "2026-11-01")]),
    "WB": ("West Bengal", [("Netaji Jayanti", "2026-01-23"), ("Poila Baisakh", "2026-04-15"),
                           ("Durga Puja (Maha Navami)", "2026-10-19"), ("Netaji Jayanti", "2027-01-23")]),
    "DL": ("Delhi", [("Ambedkar Jayanti", "2026-04-14"), ("Ambedkar Jayanti", "2027-04-14")]),
    "GJ": ("Gujarat", [("Uttarayan", "2026-01-14"), ("Gujarati New Year", "2026-11-10"), ("Uttarayan", "2027-01-14")]),
}


def ensure_leave_defaults(db: Session, company_id: int) -> None:
    policy.settings_row(db, company_id)
    _ensure_types(db, company_id)
    _ensure_work_weeks(db, company_id)
    _ensure_calendars(db, company_id)
    db.flush()


def _ensure_types(db: Session, company_id: int) -> None:
    have = {c for (c,) in db.query(LeaveType.code).filter(LeaveType.company_id == company_id).all()}
    if have:
        return  # a company that has leave types keeps its own set
    for spec in LEAVE_TYPES:
        policy.create_type(db, company_id, {**spec, "rules": clean_rules(spec["rules"], code=spec["code"]),
                                            "change_note": "Default policy"},
                           effective_from=date(2000, 1, 1), is_system=True)


def _ensure_work_weeks(db: Session, company_id: int) -> None:
    if db.query(WorkWeekPattern.id).filter(WorkWeekPattern.company_id == company_id).first():
        return
    for key, default in (("five_day", True), ("second_fourth_saturday", False), ("six_day", False)):
        name, days = PRESETS[key]
        db.add(WorkWeekPattern(company_id=company_id, name=name, days=[dict(d, off_weeks=d.get("off_weeks", []))
                                                                      for d in days],
                               is_default=default, priority=10 if default else 50,
                               description="Used for everyone not covered by another work week." if default else None))


def _state_ids(db: Session, company_id: int) -> dict[str, int]:
    from core.masters import MasterType, MasterValue
    rows = (db.query(MasterValue.code, MasterValue.id).join(MasterType, MasterType.id == MasterValue.type_id)
            .filter(MasterType.company_id == company_id, MasterType.key == "state").all())
    return {c: i for c, i in rows if c}


def _add_holidays(db: Session, company_id: int, cal: HolidayCalendar, rows: list[tuple[str, str, str]]) -> None:
    seen = set()
    for name, d, kind in rows:
        key = (d, name)
        if key in seen:
            continue
        seen.add(key)
        db.add(Holiday(company_id=company_id, calendar=cal, name=name, date=date.fromisoformat(d), kind=kind))


def _common_rows(include_optional: bool = True) -> list[tuple[str, str, str]]:
    rows = []
    for y in (2026, 2027):
        rows += [(n, f"{y}-{md}", "national") for n, md in NATIONAL]
        rows += [(n, d, "festival") for n, d in FESTIVALS.get(y, [])]
        if include_optional:
            rows += [(n, d, "optional") for n, d in OPTIONAL.get(y, [])]
    return rows


def _ensure_calendars(db: Session, company_id: int) -> None:
    if db.query(HolidayCalendar.id).filter(HolidayCalendar.company_id == company_id).first():
        return
    main = HolidayCalendar(company_id=company_id, name="All India", is_default=True, priority=100, optional_limit=2,
                           description="National holidays, common festivals and optional holidays. "
                                       "Used for everyone not covered by a state calendar.")
    db.add(main)
    _add_holidays(db, company_id, main, _common_rows())
    states = _state_ids(db, company_id)
    for i, (code, (name, extra)) in enumerate(STATES.items()):
        sid = states.get(code)
        cal = HolidayCalendar(company_id=company_id, name=name, priority=20 + i, optional_limit=2,
                              applicability={"mode": "pickers", "states": [sid] if sid else []},
                              description=f"For people working in {name}. Festival dates: check the state notification.")
        db.add(cal)
        state_rows = [(n, d, "festival") for n, d in extra]
        # a state holiday on the same day as an optional one replaces it
        state_days = {d for _, d, _ in state_rows}
        _add_holidays(db, company_id, cal, [r for r in _common_rows() if not (r[2] == "optional" and r[1] in state_days)]
                      + state_rows)
    db.flush()
