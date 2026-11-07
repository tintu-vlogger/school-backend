"""Leave types, their versions and company leave settings.

`rules_on(type, day)` gives the rules in force on a day, so a period is always worked out
with the version that applied then. Saving new rules creates a new version from an
effective date; the version before it gets an end date and stays in the history.
"""
from __future__ import annotations

import copy
from datetime import date, timedelta

from sqlalchemy.orm import Session, selectinload
from sqlalchemy.orm.attributes import flag_modified

from core import context
from core.errors import BadRequest, NotFound
from modules.leave.models import LeaveLedgerEntry, LeavePolicyVersion, LeaveRequest, LeaveSettings, LeaveType
from modules.leave.rules import clean_rules, with_defaults

DEFAULT_SETTINGS = {
    "calendar_visibility": "team",          # team | department | all
    "show_type_on_calendar": True,
    "balance_display": "today",             # today | year_end
    "cancellation": {"allow_employee": True, "window": "current_year", "needs_approval": True,
                     "reason_required": False, "partial": True},
    "extension": {"enabled": True, "reason_required": True},
    "modify_pending": True,
    "comp_off": {"enabled": True, "on_weekly_off": True, "on_holiday": True, "half_day": True,
                 "expiry_days": 90, "backdated_days": 30, "reason_required": True, "allow_future": False},
    "lop": {"max_per_period": None},
    # Bradford factor: score = spells x spells x days of unplanned absence in a rolling window
    "bradford": {"enabled": True, "window_weeks": 52, "type_codes": ["SL", "CL"], "include_absent": True,
                 "levels": [{"score": 50, "label": "Keep an eye"}, {"score": 125, "label": "Return-to-work talk"},
                            {"score": 400, "label": "Formal review"}, {"score": 650, "label": "Serious concern"}],
                 "alert": True, "alert_manager": True},
    # Emails before comp off or carried leave lapses
    "expiry_reminders": {"enabled": True, "days_before": [30, 7]},
    # A holiday on a weekly off gives a substitute day off
    "substitute_holiday": {"enabled": False, "when": "next_working_day", "national_only": False},
}

ICONS = ("plane", "sun", "coffee", "thermometer", "baby", "heart", "flower", "home", "briefcase", "calendar",
         "gift", "graduation-cap", "stethoscope", "umbrella", "clock", "moon", "star", "party-popper", "ban", "hand-heart")


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


# ── Settings ────────────────────────────────────────────────────────

def settings_row(db: Session, company_id: int) -> LeaveSettings:
    row = db.query(LeaveSettings).filter(LeaveSettings.company_id == company_id).first()
    if row is None:
        row = LeaveSettings(company_id=company_id, config={})
        db.add(row)
        db.flush()
    return row


def get_settings(db: Session, company_id: int) -> dict:
    row = db.query(LeaveSettings).filter(LeaveSettings.company_id == company_id).first()
    return _merge(DEFAULT_SETTINGS, row.config if row else {})


def update_settings(db: Session, company_id: int, data: dict) -> dict:
    row = settings_row(db, company_id)
    merged = _merge(get_settings(db, company_id), data or {})
    if merged["calendar_visibility"] not in ("team", "department", "all"):
        raise BadRequest("Choose who can see the leave calendar.")
    if merged["cancellation"]["window"] not in ("upcoming", "current_month", "current_year", "any"):
        raise BadRequest("Choose how far back leave can be cancelled.")
    co = merged["comp_off"]
    try:
        co["expiry_days"] = int(co["expiry_days"]) if co.get("expiry_days") not in (None, "") else None
        co["backdated_days"] = int(co.get("backdated_days") or 0)
    except (TypeError, ValueError):
        raise BadRequest("Comp-off days must be whole numbers.")
    _clean_extra(merged)
    row.config = merged
    flag_modified(row, "config")
    return merged


def _clean_extra(m: dict) -> None:
    """Check the Bradford, expiry-reminder and substitute-holiday settings."""
    b = m["bradford"]
    try:
        b["window_weeks"] = int(b.get("window_weeks") or 52)
        levels = []
        for lv in b.get("levels") or []:
            score = int(lv.get("score"))
            label = str(lv.get("label") or "").strip()[:80]
            if score <= 0 or not label:
                raise ValueError
            levels.append({"score": score, "label": label})
    except (TypeError, ValueError):
        raise BadRequest("Each Bradford level needs a score above 0 and a name.")
    if not 4 <= b["window_weeks"] <= 156:
        raise BadRequest("The Bradford window must be between 4 and 156 weeks.")
    b["levels"] = sorted(levels, key=lambda x: x["score"])
    b["type_codes"] = [str(c).strip().upper() for c in (b.get("type_codes") or []) if str(c).strip()]
    r = m["expiry_reminders"]
    try:
        days = sorted({int(x) for x in (r.get("days_before") or [])}, reverse=True)
    except (TypeError, ValueError):
        raise BadRequest("Reminder days must be whole numbers.")
    if any(d < 1 or d > 180 for d in days):
        raise BadRequest("Reminders can go 1 to 180 days before the leave lapses.")
    r["days_before"] = days
    sub = m["substitute_holiday"]
    if sub.get("when") not in ("next_working_day", "previous_working_day"):
        raise BadRequest("Choose the working day before or after the holiday.")


# ── Leave year ──────────────────────────────────────────────────────

def year_bounds(rules: dict, d: date) -> tuple[int, date, date]:
    """(label, first day, last day) of the leave year containing d."""
    m = int(with_defaults(rules)["year_end"]["year_start_month"] or 1)
    y = d.year if d.month >= m else d.year - 1
    start = date(y, m, 1)
    end = date(y + 1, m, 1) - timedelta(days=1)
    return y, start, end


def year_range(rules: dict, label: int) -> tuple[date, date]:
    m = int(with_defaults(rules)["year_end"]["year_start_month"] or 1)
    return date(label, m, 1), date(label + 1, m, 1) - timedelta(days=1)


def year_text(rules: dict, label: int) -> str:
    m = int(with_defaults(rules)["year_end"]["year_start_month"] or 1)
    return str(label) if m == 1 else f"{label}-{str(label + 1)[-2:]}"


# ── Types and versions ──────────────────────────────────────────────

def types_query(db: Session, company_id: int):
    return (db.query(LeaveType).options(selectinload(LeaveType.versions))
            .filter(LeaveType.company_id == company_id))


def list_types(db: Session, company_id: int, include_inactive: bool = True) -> list[LeaveType]:
    q = types_query(db, company_id)
    if not include_inactive:
        q = q.filter(LeaveType.active.is_(True))
    return q.order_by(LeaveType.sort_order, LeaveType.name).all()


def get_type(db: Session, company_id: int, uuid) -> LeaveType:
    row = types_query(db, company_id).filter(LeaveType.uuid == uuid).first()
    if row is None:
        raise NotFound("Leave type")
    return row


def type_by_code(db: Session, company_id: int, code: str) -> LeaveType | None:
    return db.query(LeaveType).filter(LeaveType.company_id == company_id, LeaveType.code == code).first()


def version_on(t: LeaveType, d: date) -> LeavePolicyVersion | None:
    versions = sorted(t.versions, key=lambda v: (v.effective_from, v.version))
    if not versions:
        return None
    chosen = None
    for v in versions:
        if v.effective_from <= d:
            chosen = v
    return chosen or versions[0]


def current_version(t: LeaveType) -> LeavePolicyVersion | None:
    return max(t.versions, key=lambda v: (v.effective_from, v.version)) if t.versions else None


def rules_on(t: LeaveType, d: date) -> dict:
    v = version_on(t, d)
    return with_defaults(v.rules if v else {})


def _check_code(db: Session, company_id: int, code: str, current: LeaveType | None = None) -> str:
    code = (code or "").strip().upper()
    if not code or len(code) > 20 or not all(c.isalnum() or c in "_-" for c in code):
        raise BadRequest("The code must be 1 to 20 letters or numbers, e.g. EL.")
    dup = db.query(LeaveType.id).filter(LeaveType.company_id == company_id, LeaveType.code == code)
    if current is not None:
        dup = dup.filter(LeaveType.id != current.id)
    if dup.first():
        raise BadRequest(f"Another leave type already uses the code {code}.")
    return code


def _display(data: dict, row: LeaveType) -> None:
    if "name" in data and data["name"] is not None:
        name = data["name"].strip()
        if not name:
            raise BadRequest("Give the leave type a name.")
        row.name = name[:100]
    if data.get("color"):
        row.color = str(data["color"])[:20]
    if data.get("icon"):
        row.icon = data["icon"] if data["icon"] in ICONS else "plane"
    if "description" in data:
        row.description = (data.get("description") or "").strip() or None
    if data.get("sort_order") is not None:
        row.sort_order = int(data["sort_order"])


def create_type(db: Session, company_id: int, data: dict, *, effective_from: date | None = None,
                is_system: bool = False) -> LeaveType:
    code = _check_code(db, company_id, data.get("code"))
    kind = data.get("kind") or "standard"
    if kind not in ("standard", "comp_off", "lop", "optional_holiday"):
        kind = "standard"
    row = LeaveType(company_id=company_id, code=code, name="", kind=kind, is_system=is_system)
    _display({**data, "name": data.get("name") or ""}, row)
    db.add(row)
    db.flush()
    rules = clean_rules(data.get("rules") or {}, code=code)
    db.add(LeavePolicyVersion(company_id=company_id, leave_type_id=row.id, version=1,
                              effective_from=effective_from or date(2000, 1, 1), rules=rules,
                              change_note=data.get("change_note") or "First version",
                              created_by_name=context.current().actor_name or "System"))
    db.flush()
    db.refresh(row)
    return row


def update_type(db: Session, company_id: int, row: LeaveType, data: dict) -> LeaveType:
    """Name, colour, icon change in place. New rules create a version from `effective_from`."""
    if data.get("code") and data["code"].strip().upper() != row.code:
        if _has_records(db, row):
            raise BadRequest("The code can't change once leave has been recorded with it.")
        row.code = _check_code(db, company_id, data["code"], row)
    _display(data, row)
    if data.get("rules") is not None:
        new_rules = clean_rules(data["rules"], code=row.code)
        cur = current_version(row)
        eff = data.get("effective_from")
        eff = date.fromisoformat(eff) if isinstance(eff, str) and eff else eff
        if cur is not None and cur.rules == new_rules:
            return row
        if eff is None:
            from core.timeutil import today_for_company_id
            eff = today_for_company_id(db, company_id)
        if cur is not None and eff < cur.effective_from:
            raise BadRequest(f"The new rules must start on or after {cur.effective_from:%d %b %Y}, "
                             "when the current version started.")
        if cur is not None and eff == cur.effective_from and not _version_used(db, cur):
            # nothing has used this version yet: keep the history but let the newest win on the same day
            pass
        if cur is not None and (cur.effective_to is None or cur.effective_to >= eff):
            cur.effective_to = eff - timedelta(days=1) if eff > cur.effective_from else cur.effective_from
        v = LeavePolicyVersion(company_id=company_id, leave_type_id=row.id,
                               version=(max((x.version for x in row.versions), default=0) + 1),
                               effective_from=eff, rules=new_rules,
                               change_note=(data.get("change_note") or "").strip()[:300] or None,
                               created_by_name=context.current().actor_name)
        db.add(v)
        db.flush()
        db.refresh(row)
    return row


def _has_records(db: Session, row: LeaveType) -> bool:
    return bool(db.query(LeaveLedgerEntry.id).filter(LeaveLedgerEntry.leave_type_id == row.id).first()
                or db.query(LeaveRequest.id).filter(LeaveRequest.leave_type_id == row.id).first())


def _version_used(db: Session, v: LeavePolicyVersion) -> bool:
    return bool(db.query(LeaveRequest.id).filter(LeaveRequest.policy_version_id == v.id).first()
                or db.query(LeaveLedgerEntry.id).filter(LeaveLedgerEntry.policy_version_id == v.id).first())


def set_active(db: Session, row: LeaveType, active: bool) -> LeaveType:
    from core.timeutil import utc_now
    row.active = active
    row.archived_at = None if active else utc_now()
    return row
