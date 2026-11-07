"""Versioned time settings by key. A change is saved as a new version from a date;
the version in force on a day is the one with the latest effective_from on or before it.
"""
from __future__ import annotations

import copy
from datetime import date

from sqlalchemy.orm import Session

from core import context
from core.errors import BadRequest
from modules.attendance.models import TimeSetting
from modules.shifts.rest_rules import DEFAULT_REST_RULES

DEFAULTS: dict[str, dict] = {
    # Roster rest and safety rules (OSH Code limits)
    "rest_rules": DEFAULT_REST_RULES,
    # Web and phone clock-in
    "clock_in": {
        "web_enabled": True, "geo_required": False, "selfie_required": False, "allowed_ips": [],
        "selfie_retention_days": 90, "allow_outside_with_reason": False,
    },
    # Attendance corrections (missed or wrong punch)
    "regularisation": {"monthly_limit": 3, "window_days": 7, "allow_future": False, "reason_required": True},
    # Work from home (categories come from the "wfh_category" master)
    "wfh": {"weekly_limit": 2, "monthly_limit": 8, "blocked_weekdays": [], "allow_past_days": 3},
    # Permission hours (short leave in hours)
    "permission": {"monthly_count": 2, "monthly_minutes": 240, "max_minutes_per_request": 120,
                   "min_minutes_per_request": 30, "within_shift_only": True},
    # On duty (client visit, training, field work; types from the "on_duty_type" master)
    "on_duty": {"allow_future": True, "monthly_limit": 0, "allow_past_days": 7},
    # Overtime pre-approval requests and consent wording
    "overtime_request": {
        "max_hours_per_day": 4,
        "consent_text": "I agree to work overtime on this day. I understand overtime is voluntary and is paid "
                        "at twice my ordinary rate of wages.",
    },
    # Pay period and attendance cut-off
    "pay_period": {"start_day": 1, "cutoff_day": 0},  # 0 = month end
    # Timesheets
    "timesheets": {"min_weekly_hours": 0, "max_daily_hours": 16, "allow_future": False, "require_task": False,
                   "reminder_weekday": 4, "no_logs_on_leave": True},
    # Attendance and leave working together
    "leave_link": {
        # an absent day becomes leave from these types (first with balance), else loss of pay
        "absence": {"enabled": False, "after_days": 3, "action": "leave", "leave_type_codes": ["CL", "EL"],
                    "fallback": "lop", "needs_approval": False, "half_days": True},
        # late-mark penalty days are taken from leave in this order, else loss of pay
        "late_penalty": {"enabled": False, "leave_type_codes": ["CL", "EL"]},
        # comp off for work on a weekly off or holiday
        "comp_off": {"enabled": False, "full_day_hours": 6, "half_day_hours": 4, "on_weekly_off": True,
                     "on_holiday": True, "needs_approval": False},
    },
    # Loss of pay per pay period
    "lop_control": {"cap_days": None, "carry_excess": True},
    # Auto shift: the shift whose start is closest to the check-in, within the window
    "auto_shift": {"window_minutes": 120, "shift_ids": []},
    # Timesheet billing (GST)
    "billing": {"supplier_name": "", "supplier_gstin": "", "supplier_state": "29", "supplier_address": "",
                "gst_rate": 18, "sac_code": "998311", "payment_terms_days": 30, "invoice_prefix": "INV",
                "bank_details": ""},
    # Statutory registers: establishment details printed on the forms
    "registers": {"establishment_name": "", "registration_no": "", "address": "", "employer_name": "",
                  "nature_of_business": "", "form_labels": {}},
}

LABELS = {
    "rest_rules": "Rest and safety rules", "clock_in": "Clock-in", "regularisation": "Attendance corrections",
    "wfh": "Work from home", "permission": "Permission hours", "on_duty": "On duty",
    "overtime_request": "Overtime requests", "pay_period": "Pay period", "timesheets": "Timesheets",
    "leave_link": "Attendance and leave", "lop_control": "Loss of pay per period", "auto_shift": "Auto shift",
    "billing": "Timesheet billing", "registers": "Statutory registers",
}


def _merge(base: dict, extra: dict | None) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def get(db: Session, company_id: int, key: str, on: date | None = None) -> dict:
    if key not in DEFAULTS:
        raise BadRequest("Unknown setting.")
    q = db.query(TimeSetting).filter(TimeSetting.company_id == company_id, TimeSetting.key == key)
    if on is not None:
        q = q.filter(TimeSetting.effective_from <= on)
    row = q.order_by(TimeSetting.effective_from.desc()).first()
    if row is None and on is not None:  # nothing that early: use the oldest version
        row = (db.query(TimeSetting).filter(TimeSetting.company_id == company_id, TimeSetting.key == key)
               .order_by(TimeSetting.effective_from.asc()).first())
    return _merge(DEFAULTS[key], row.config if row else None)


def history(db: Session, company_id: int, key: str) -> list[TimeSetting]:
    return (db.query(TimeSetting).filter(TimeSetting.company_id == company_id, TimeSetting.key == key)
            .order_by(TimeSetting.effective_from.desc()).all())


def save(db: Session, company_id: int, key: str, config: dict, effective_from: date, note: str | None = None) -> TimeSetting:
    if key not in DEFAULTS:
        raise BadRequest("Unknown setting.")
    clean = _merge(DEFAULTS[key], {k: v for k, v in (config or {}).items() if k in DEFAULTS[key]})
    rows = history(db, company_id, key)
    same_day = next((r for r in rows if r.effective_from == effective_from), None)
    if same_day:
        same_day.config = clean
        same_day.change_note = note
        return same_day
    row = TimeSetting(company_id=company_id, key=key, config=clean, effective_from=effective_from,
                      version=(max((r.version for r in rows), default=0) + 1), change_note=note,
                      created_by_name=context.current().actor_name)
    db.add(row)
    db.flush()
    return row


def ensure_defaults(db: Session, company_id: int, start: date) -> None:
    for key, value in DEFAULTS.items():
        exists = db.query(TimeSetting.id).filter(TimeSetting.company_id == company_id, TimeSetting.key == key).first()
        if not exists:
            db.add(TimeSetting(company_id=company_id, key=key, config=copy.deepcopy(value), effective_from=start,
                               version=1, change_note="Default settings", created_by_name="System"))
