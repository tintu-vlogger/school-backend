"""Attendance rules, daily processing, clock-in, views, locks and the payroll export.

Public functions other modules may use:
    recompute(db, company_id, employee_ids, start, end)      re-work days after any change
    payroll_summary(db, company_id, start, end)               payable/LOP/OT/allowances per employee
    is_locked(db, company_id, day)                            the pay period of a day is locked
    get_setting(db, company_id, key, on)                      versioned time settings
"""
from __future__ import annotations

import math
import uuid as uuid_mod
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from core import audit, context
from core.errors import BadRequest, Forbidden, NotFound
from core.timeutil import DEFAULT_TZ, iso_utc
from modules.attendance import engine, settings_store
from modules.attendance.models import (AttendanceAdjustment, AttendanceDay, AttendanceLock, AttendanceRequest,
                                       AttendanceRule, Geofence, Punch)
from modules.shifts import adapters
from modules.shifts import service as shifts_service

get_setting = settings_store.get
ZERO = Decimal("0.00")


# ── Company clock ───────────────────────────────────────────────────

def company_tz(db: Session, company_id: int) -> ZoneInfo:
    from modules.company.models import Company
    name = db.query(Company.timezone).filter(Company.id == company_id).scalar() or DEFAULT_TZ
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def now_local(db: Session, company_id: int) -> datetime:
    return datetime.now(company_tz(db, company_id))


def today(db: Session, company_id: int) -> date:
    return now_local(db, company_id).date()


def month_range(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1))
    return start, end


# ── Who can see whom ────────────────────────────────────────────────

def has_perm(db: Session, current_user: dict, perm: str) -> bool:
    from core.security import _get_role_permissions
    return perm in _get_role_permissions(current_user["company_id"], current_user["role"], db)


def can_manage(db: Session, current_user: dict) -> bool:
    return has_perm(db, current_user, "time.attendance.manage")


def report_ids(db: Session, company_id: int, manager_id: int | None) -> list[int]:
    """Everyone below a manager (direct and indirect reports)."""
    from modules.employees.models import Employee
    if not manager_id:
        return []
    rows = db.query(Employee.id, Employee.reporting_manager_id).filter(Employee.company_id == company_id,
                                                                        Employee.active.is_(True)).all()
    children = defaultdict(list)
    for eid, mid in rows:
        if mid:
            children[mid].append(eid)
    out, stack, seen = [], [manager_id], {manager_id}
    while stack:
        for c in children.get(stack.pop(), []):
            if c not in seen:
                seen.add(c)
                out.append(c)
                stack.append(c)
    return out


def visible_ids(db: Session, current_user: dict) -> list[int] | None:
    """Employee ids the person may see on team screens; None means everyone."""
    if can_manage(db, current_user):
        return None
    return report_ids(db, current_user["company_id"], current_user.get("employee_id"))


def check_can_see(db: Session, current_user: dict, employee_id: int) -> None:
    if employee_id == current_user.get("employee_id"):
        return
    ids = visible_ids(db, current_user)
    if ids is not None and employee_id not in ids:
        raise Forbidden("You can only see your own and your team's attendance.")


def employees_query(db: Session, company_id: int, ids: list[int] | None = None, department_id: int | None = None,
                    q: str | None = None, active_only: bool = True):
    from modules.employees.models import Employee
    from modules.employees import service as emp_service
    query = db.query(Employee).filter(Employee.company_id == company_id)
    if active_only:
        query = query.filter(Employee.active.is_(True))
    if ids is not None:
        query = query.filter(Employee.id.in_(ids or [-1]))
    if department_id:
        query = query.filter(Employee.department_id == department_id)
    query = emp_service.search(query, q)
    return query.order_by(Employee.first_name, Employee.last_name)


def employee(db: Session, company_id: int, employee_id: int):
    from modules.employees.models import Employee
    e = db.query(Employee).filter(Employee.company_id == company_id, Employee.id == employee_id).first()
    if not e:
        raise NotFound("Employee")
    return e


def employee_by_uuid(db: Session, company_id: int, uuid):
    from modules.employees.models import Employee
    e = db.query(Employee).filter(Employee.company_id == company_id, Employee.uuid == uuid).first()
    if not e:
        raise NotFound("Employee")
    return e


def name_of(e) -> str:
    return f"{e.first_name} {e.last_name}".strip() if e else ""


def names_for(db: Session, company_id: int, ids) -> dict[int, str]:
    from modules.employees.models import Employee
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    return {e.id: name_of(e) for e in db.query(Employee).filter(Employee.company_id == company_id,
                                                                  Employee.id.in_(ids)).all()}


# ── Attendance rules (versioned) ────────────────────────────────────

def rule_rows(db: Session, company_id: int, current_only: bool = True) -> list[AttendanceRule]:
    q = db.query(AttendanceRule).filter(AttendanceRule.company_id == company_id)
    if current_only:
        q = q.filter(AttendanceRule.is_current.is_(True))
    return q.order_by(AttendanceRule.priority, AttendanceRule.id).all()


def _clean_rule_config(config: dict | None) -> dict:
    merged = engine.merged_rule(config)
    full, half = float(merged["full_day_hours"]), float(merged["half_day_hours"])
    if not (0 < half <= full <= 24):
        raise BadRequest("Half-day hours must be more than 0 and not more than full-day hours (up to 24).")
    if merged["hours_calc"] not in ("first_last", "pairs"):
        raise BadRequest("Choose how hours are worked out.")
    ot = merged["overtime"]
    if float(ot.get("rate_multiplier") or 0) < 2:
        # Code on Wages, 2019 s.14: overtime at least twice the ordinary rate
        raise BadRequest("Overtime must be paid at least twice the ordinary rate (Code on Wages).")
    for key in ("late_penalty", "early_penalty"):
        p = merged[key]
        if p.get("enabled") and (int(p.get("count") or 0) < 1 or float(p.get("deduct_days") or 0) not in (0.25, 0.5, 1.0)):
            raise BadRequest("Penalties need a count of at least 1 and a deduction of a quarter, half or full day.")
    return merged


def create_rule(db: Session, company_id: int, data: dict) -> AttendanceRule:
    if not (data.get("name") or "").strip():
        raise BadRequest("Give the rule a name.")
    row = AttendanceRule(
        company_id=company_id, lineage_id=uuid_mod.uuid4(), version=1, name=data["name"].strip(),
        description=data.get("description"), effective_from=data.get("effective_from") or today(db, company_id),
        is_current=True, is_default=bool(data.get("is_default")), priority=int(data.get("priority") or 100),
        applies_to=_clean_applies(data.get("applies_to")), config=_clean_rule_config(data.get("config")),
        active=data.get("active", True), change_note=data.get("change_note"),
        created_by_name=context.current().actor_name,
    )
    if row.is_default:
        _clear_default(db, company_id)
    db.add(row)
    db.flush()
    return row


def _clean_applies(a: dict | None) -> dict:
    out = {}
    for key in ("employee_ids", "department_ids", "designation_ids", "shift_ids"):
        vals = [int(x) for x in ((a or {}).get(key) or []) if str(x).isdigit()]
        if vals:
            out[key] = sorted(set(vals))
    return out


def _clear_default(db: Session, company_id: int) -> None:
    for r in db.query(AttendanceRule).filter(AttendanceRule.company_id == company_id,
                                             AttendanceRule.is_default.is_(True)).all():
        r.is_default = False


def new_rule_version(db: Session, company_id: int, current: AttendanceRule, data: dict) -> AttendanceRule:
    """Change a rule from a date. The same start date edits the version in place; a later
    date closes the current version the day before and starts a new one."""
    eff = data.get("effective_from") or current.effective_from
    config = _clean_rule_config(data.get("config", current.config))
    fields = dict(name=(data.get("name") or current.name).strip(), description=data.get("description", current.description),
                  priority=int(data.get("priority", current.priority) or 100),
                  applies_to=_clean_applies(data.get("applies_to", current.applies_to)),
                  active=bool(data.get("active", current.active)), change_note=data.get("change_note"))
    if data.get("is_default"):
        _clear_default(db, company_id)
        fields["is_default"] = True
    elif "is_default" in data:
        fields["is_default"] = bool(data["is_default"])
    if eff < current.effective_from:
        raise BadRequest(f"The change must start on or after {current.effective_from:%d %b %Y}, when this version began.")
    if eff == current.effective_from:
        for k, v in fields.items():
            setattr(current, k, v)
        current.config = config
        db.flush()
        return current
    current.is_current = False
    current.effective_to = eff - timedelta(days=1)
    row = AttendanceRule(company_id=company_id, lineage_id=current.lineage_id, version=current.version + 1,
                         effective_from=eff, is_current=True, config=config,
                         is_default=fields.pop("is_default", current.is_default),
                         created_by_name=context.current().actor_name, **fields)
    db.add(row)
    db.flush()
    return row


def get_rule(db: Session, company_id: int, uuid) -> AttendanceRule:
    r = db.query(AttendanceRule).filter(AttendanceRule.company_id == company_id, AttendanceRule.uuid == uuid).first()
    if not r:
        raise NotFound("Attendance rule")
    return r


def rule_out(r: AttendanceRule, names: dict | None = None) -> dict:
    return {"uuid": str(r.uuid), "id": r.id, "lineage_id": str(r.lineage_id), "version": r.version, "name": r.name,
            "description": r.description, "effective_from": r.effective_from.isoformat(),
            "effective_to": r.effective_to.isoformat() if r.effective_to else None, "is_current": r.is_current,
            "is_default": r.is_default, "priority": r.priority, "applies_to": r.applies_to or {},
            "config": engine.merged_rule(r.config), "active": r.active, "change_note": r.change_note,
            "created_by_name": r.created_by_name, "created_at": iso_utc(r.created_at)}


class RuleBook:
    """Picks the rule version for an employee and a day."""

    def __init__(self, db: Session, company_id: int):
        self.rows = (db.query(AttendanceRule).filter(AttendanceRule.company_id == company_id,
                                                     AttendanceRule.active.is_(True))
                     .order_by(AttendanceRule.priority, AttendanceRule.id).all())

    def _versions_on(self, day: date) -> list[AttendanceRule]:
        return [r for r in self.rows if r.effective_from <= day and (r.effective_to is None or r.effective_to >= day)] \
            or [r for r in self.rows if r.is_current]

    def pick(self, emp, day: date, shift_id: int | None, rule_id: int | None) -> AttendanceRule | None:
        versions = self._versions_on(day)
        if rule_id:
            chosen = next((r for r in self.rows if r.id == rule_id), None)
            if chosen:
                same = [r for r in versions if r.lineage_id == chosen.lineage_id]
                return same[0] if same else chosen
        for r in versions:
            a = r.applies_to or {}
            if not a:
                continue
            if (emp.id in a.get("employee_ids", []) or (emp.department_id and emp.department_id in a.get("department_ids", []))
                    or (emp.designation_id and emp.designation_id in a.get("designation_ids", []))
                    or (shift_id and shift_id in a.get("shift_ids", []))):
                return r
        return next((r for r in versions if r.is_default), None) or next((r for r in versions if not r.applies_to), None)


DEFAULT_RULE_NAME = "Standard attendance rule"


def ensure_default_rule(db: Session, company_id: int, start: date) -> None:
    if db.query(AttendanceRule.id).filter(AttendanceRule.company_id == company_id).first():
        return
    db.add(AttendanceRule(company_id=company_id, lineage_id=uuid_mod.uuid4(), version=1, name=DEFAULT_RULE_NAME,
                          description="Grace of the shift, 3 late marks = half-day loss of pay, 8 h full day, "
                                      "4 h half day, overtime at twice the ordinary rate.",
                          effective_from=start, is_current=True, is_default=True, priority=1000, applies_to={},
                          config=engine.merged_rule({}), active=True, change_note="Default rule",
                          created_by_name="System"))


# ── Locks ───────────────────────────────────────────────────────────

def locks(db: Session, company_id: int) -> list[AttendanceLock]:
    return (db.query(AttendanceLock).filter(AttendanceLock.company_id == company_id, AttendanceLock.status == "locked")
            .order_by(AttendanceLock.period_start).all())


def lock_for(lock_rows: list[AttendanceLock], day: date) -> AttendanceLock | None:
    return next((l for l in lock_rows if l.period_start <= day <= l.period_end), None)


def is_locked(db: Session, company_id: int, day: date) -> bool:
    return lock_for(locks(db, company_id), day) is not None


def next_open_day(lock_rows: list[AttendanceLock], day: date) -> date:
    d = day
    while True:
        lk = lock_for(lock_rows, d)
        if not lk:
            return d
        d = lk.period_end + timedelta(days=1)


def pay_period_for(db: Session, company_id: int, day: date) -> tuple[date, date]:
    cfg = get_setting(db, company_id, "pay_period", day)
    sd = int(cfg.get("start_day") or 1)
    if sd <= 1:
        return month_range(day.year, day.month)
    if day.day >= sd:
        start = date(day.year, day.month, sd)
    else:
        prev = date(day.year, day.month, 1) - timedelta(days=1)
        start = date(prev.year, prev.month, min(sd, 28))
    nxt = (start.replace(day=1) + timedelta(days=32)).replace(day=min(sd, 28))
    return start, nxt - timedelta(days=1)


def lock_period(db: Session, company_id: int, start: date, end: date, note: str | None = None) -> AttendanceLock:
    if end < start:
        raise BadRequest("The end date can't be before the start date.")
    if any(l.period_start <= end and l.period_end >= start for l in locks(db, company_id)):
        raise BadRequest("Part of this period is already locked.")
    pending = (db.query(AttendanceRequest.id).filter(AttendanceRequest.company_id == company_id,
                                                     AttendanceRequest.status == "pending",
                                                     AttendanceRequest.date >= start, AttendanceRequest.date <= end).count())
    # bring every day up to date first, then freeze
    ids = [e.id for e in employees_query(db, company_id).all()]
    recompute(db, company_id, ids, start, min(end, today(db, company_id)))
    lk = AttendanceLock(company_id=company_id, period_start=start, period_end=end, status="locked",
                        locked_at=datetime.now(timezone.utc), locked_by_name=context.current().actor_name, note=note)
    db.add(lk)
    db.query(AttendanceDay).filter(AttendanceDay.company_id == company_id, AttendanceDay.date >= start,
                                   AttendanceDay.date <= end).update({"locked": True}, synchronize_session=False)
    db.flush()
    from modules.attendance import lop
    lop.finalise(db, company_id, start, end, str(lk.uuid))
    audit.event(db, "attendance.locked", "attendance_locks", lk.id,
                {"from": start.isoformat(), "to": end.isoformat(), "pending_requests": pending},
                entity_uuid=str(lk.uuid), entity_label=f"{start:%d %b} to {end:%d %b %Y}")
    return lk


def unlock_period(db: Session, company_id: int, lk: AttendanceLock, note: str | None = None) -> AttendanceLock:
    if lk.status != "locked":
        raise BadRequest("This period is not locked.")
    lk.status = "unlocked"
    lk.unlocked_at = datetime.now(timezone.utc)
    lk.unlocked_by_name = context.current().actor_name
    lk.note = note or lk.note
    db.query(AttendanceDay).filter(AttendanceDay.company_id == company_id, AttendanceDay.date >= lk.period_start,
                                   AttendanceDay.date <= lk.period_end).update({"locked": False}, synchronize_session=False)
    from modules.attendance import lop
    lop.unfinalise(db, company_id, lk.period_start, lk.period_end)
    audit.event(db, "attendance.unlocked", "attendance_locks", lk.id,
                {"from": lk.period_start.isoformat(), "to": lk.period_end.isoformat(), "note": note},
                entity_uuid=str(lk.uuid))
    db.flush()
    return lk


# ── Daily processing ────────────────────────────────────────────────

def _punch_rows(db: Session, company_id: int, employee_ids: list[int], start: date, end: date, tz) -> dict[int, list[Punch]]:
    lo = datetime.combine(start - timedelta(days=1), time(0), tzinfo=tz)
    hi = datetime.combine(end + timedelta(days=2), time(0), tzinfo=tz)
    out = defaultdict(list)
    for p in (db.query(Punch).filter(Punch.company_id == company_id, Punch.employee_id.in_(employee_ids),
                                     Punch.punched_at >= lo, Punch.punched_at < hi, Punch.ignored.is_(False))
              .order_by(Punch.punched_at).all()):
        out[p.employee_id].append(p)
    return out


def _window(plan, tz, rule: dict) -> tuple[datetime, datetime, datetime, datetime]:
    """(window start, window end, shift start, shift end) for assigning punches to a day."""
    pw = rule.get("punch_window") or {}
    before, after = int(pw.get("before_minutes", 240)), int(pw.get("after_minutes", 600))
    if plan.shift is not None:
        s, e = shifts_service.spec(plan.shift).window(plan.date, tz)
    else:
        s = datetime.combine(plan.date, time(6), tzinfo=tz)
        e = datetime.combine(plan.date, time(20), tzinfo=tz)
        before, after = 360, 240
    return s - timedelta(minutes=before), e + timedelta(minutes=after), s, e


def assign_punches(punches: list[Punch], plans: dict, employee_id: int, tz, rule_for) -> dict[date, list[Punch]]:
    """Give each punch to exactly one day: the day whose shift window holds it, the
    nearest shift winning when two windows overlap (night shifts)."""
    out = defaultdict(list)
    for p in punches:
        local = p.punched_at.astimezone(tz)
        best, best_dist = None, None
        for cand in (local.date() - timedelta(days=1), local.date()):
            plan = plans.get((employee_id, cand))
            if plan is None:
                continue
            ws, we, s, e = _window(plan, tz, rule_for(cand))
            if not (ws <= local <= we):
                continue
            dist = 0 if s <= local <= e else min(abs((local - s).total_seconds()), abs((local - e).total_seconds()))
            if best_dist is None or dist < best_dist:
                best, best_dist = cand, dist
        out[best or local.date()].append(p)
    return out


def _approved_requests(db: Session, company_id: int, employee_ids: list[int], start: date, end: date):
    rows = (db.query(AttendanceRequest).filter(
        AttendanceRequest.company_id == company_id, AttendanceRequest.employee_id.in_(employee_ids),
        AttendanceRequest.status == "approved", AttendanceRequest.kind.in_(["wfh", "on_duty", "permission", "overtime"]),
        AttendanceRequest.date <= end, func.coalesce(AttendanceRequest.end_date, AttendanceRequest.date) >= start).all())
    out = defaultdict(lambda: {"wfh": None, "on_duty": None, "permission": 0, "overtime": 0})
    for r in rows:
        last = r.end_date or r.date
        for d in adapters.daterange(max(r.date, start), min(last, end)):
            slot = out[(r.employee_id, d)]
            if r.kind in ("wfh", "on_duty"):
                slot[r.kind] = r.duration or "full_day"
            elif r.kind == "permission":
                slot["permission"] += int(r.minutes or 0)
            elif r.kind == "overtime":
                slot["overtime"] += int(r.minutes or 0)
    return out


def recompute(db: Session, company_id: int, employee_ids: list[int], start: date, end: date,
              now: datetime | None = None, hooks: bool = True) -> int:
    """Work out (again) every day in the range for these employees. Days in a locked
    period are not changed: a difference becomes an adjustment for the next period.
    With `hooks`, absences old enough become leave and work on days off earns comp off
    (see leave_link); those days are then worked out once more without hooks."""
    from modules.attendance import leave_link
    employee_ids = sorted({int(e) for e in employee_ids if e})
    if not employee_ids:
        return 0
    tz = company_tz(db, company_id)
    now = now or datetime.now(tz)
    last_day = min(end, now.astimezone(tz).date())
    if last_day < start:
        return 0
    from modules.employees.models import Employee
    emps = {e.id: e for e in db.query(Employee).filter(Employee.company_id == company_id,
                                                        Employee.id.in_(employee_ids)).all()}
    plans = shifts_service.day_plans(db, company_id, employee_ids, start - timedelta(days=1), last_day + timedelta(days=1))
    punches = _punch_rows(db, company_id, employee_ids, start, last_day, tz)
    _auto_shifts(db, company_id, employee_ids, plans, punches, tz, start - timedelta(days=1), last_day)
    reqs = _approved_requests(db, company_id, employee_ids, start, last_day)
    absence_links = leave_link.active_links(db, company_id, employee_ids, start, last_day, "absence")
    auto_ids = {l.leave_request_id for l in absence_links.values() if l.leave_request_id}
    leave = adapters.leave_days(db, company_id, employee_ids, start, last_day, exclude_request_ids=auto_ids)
    leave_all = adapters.leave_days(db, company_id, employee_ids, start, last_day) if auto_ids else leave
    undo: list = []
    book = RuleBook(db, company_id)
    lock_rows = locks(db, company_id)
    existing = {(d.employee_id, d.date): d for d in db.query(AttendanceDay).filter(
        AttendanceDay.company_id == company_id, AttendanceDay.employee_id.in_(employee_ids),
        AttendanceDay.date >= start, AttendanceDay.date <= last_day).all()}
    prior_adj = defaultdict(lambda: [ZERO, ZERO, 0])
    for a in db.query(AttendanceAdjustment).filter(AttendanceAdjustment.company_id == company_id,
                                                   AttendanceAdjustment.employee_id.in_(employee_ids),
                                                   AttendanceAdjustment.date >= start,
                                                   AttendanceAdjustment.date <= last_day).all():
        k = prior_adj[(a.employee_id, a.date)]
        k[0] += a.payable_days_delta
        k[1] += a.lop_days_delta
        k[2] += a.overtime_minutes_delta
    count = 0
    for eid in employee_ids:
        emp = emps.get(eid)
        if emp is None:
            continue
        joined, left = adapters.employment_dates(emp)
        rule_cache: dict[date, AttendanceRule | None] = {}

        def rule_for(day: date, _eid=eid, _emp=emp):
            if day not in rule_cache:
                plan = plans.get((_eid, day))
                rule_cache[day] = book.pick(_emp, day, plan.shift.id if plan and plan.shift else None,
                                            plan.rule_id if plan else None)
            r = rule_cache[day]
            return engine.merged_rule(r.config if r else None)

        by_day = assign_punches(punches.get(eid, []), plans, eid, tz, rule_for)
        for day in adapters.daterange(start, last_day):
            if (joined and day < joined) or (left and day > left):
                continue
            plan = plans[(eid, day)]
            rule_row = rule_cache.get(day) if day in rule_cache else (rule_for(day) and rule_cache.get(day))
            r = reqs.get((eid, day)) or {}
            lv = leave.get((eid, day))
            if lv is None and plan.on_roster_leave:
                lv = {"unit": "full", "paid": True, "name": "Leave (roster)"}
            elif lv is None and plan.half_day:
                lv = {"unit": "second_half", "paid": True, "name": "Half day off (roster)" if plan.source == "roster" else "Half working day"}
            row = existing.get((eid, day))
            inp = engine.DayInput(
                date=day, tz=tz, rule=rule_row.config if rule_row else {}, shift=shifts_service.spec(plan.shift),
                day_type=plan.day_type, holiday_name=plan.holiday_name,
                punches=[(p.punched_at, p.direction) for p in by_day.get(day, [])], leave=lv,
                wfh=r.get("wfh"), on_duty=r.get("on_duty"), permission_minutes=r.get("permission") or 0,
                overtime_approved_minutes=r.get("overtime") or 0, punch_required=plan.punch_required,
                override_status=row.override_status if row else None,
                now=now if day >= now.astimezone(tz).date() - timedelta(days=1) else None,
            )
            res = engine.compute_day(inp)
            link = absence_links.get((eid, day))
            if link is not None:
                if (row is None or not row.locked) and not leave_link.still_absent(res.status, res.lop_days, link):
                    undo.append(link)  # the absence was corrected: give the leave back
                elif link.leave_request_id and leave_all.get((eid, day)) is not None:
                    inp.leave = leave_all.get((eid, day))
                    res = engine.compute_day(inp)
            if plan.auto_shift and plan.shift is not None:
                res.notes.append(f"Shift picked from check-in time: {plan.shift.name}")
            lk = lock_for(lock_rows, day)
            if lk is not None and row is not None:
                adj = prior_adj[(eid, day)]
                dp = res.payable_days - (row.payable_days + adj[0])
                dl = res.lop_days - (row.lop_days + adj[1])
                do = res.overtime_minutes - (row.overtime_minutes + adj[2])
                if dp or dl or do:
                    db.add(AttendanceAdjustment(
                        company_id=company_id, employee_id=eid, date=day, apply_from=next_open_day(lock_rows, day),
                        payable_days_delta=dp, lop_days_delta=dl, overtime_minutes_delta=do,
                        old_status=row.status, new_status=res.status,
                        reason="Changed after the period was locked"))
                    adj[0] += dp
                    adj[1] += dl
                    adj[2] += do
                continue
            if row is None:
                row = AttendanceDay(company_id=company_id, employee_id=eid, date=day)
                db.add(row)
                existing[(eid, day)] = row
            _store(row, res, plan, rule_row)
            row.locked = lk is not None
            count += 1
    db.flush()
    for link in undo:
        leave_link.reverse(db, link, "The attendance was corrected")
    _penalties(db, company_id, employee_ids, start, last_day, book, emps, plans, lock_rows)
    if hooks:
        redo = leave_link.after_recompute(db, company_id, employee_ids, start, last_day)
        by_emp = defaultdict(list)
        for eid, d in redo:
            by_emp[eid].append(d)
        for eid, ds in by_emp.items():
            recompute(db, company_id, [eid], min(ds), max(ds), now=now, hooks=False)
    return count


def _auto_shifts(db: Session, company_id: int, employee_ids: list[int], plans: dict, punches: dict, tz,
                 start: date, end: date) -> None:
    """For people whose work arrangement says so, pick each working day's shift from the
    first check-in: the shift starting closest to it within the window (rosters win)."""
    arrangements = shifts_service.arrangements_for(db, company_id, employee_ids)
    flagged = [eid for eid in employee_ids if any(r.auto_shift for r in arrangements.get(eid, []))]
    if not flagged:
        return
    cfg = get_setting(db, company_id, "auto_shift", end)
    window = int(cfg.get("window_minutes") or 120)
    candidates = [s for s in _shift_map(db, company_id).values() if s.active]
    if cfg.get("shift_ids"):
        wanted = {int(x) for x in cfg["shift_ids"]}
        candidates = [s for s in candidates if s.id in wanted] or candidates
    for eid in flagged:
        rows = arrangements[eid]
        mine = [(p.punched_at.astimezone(tz), p) for p in punches.get(eid, [])]
        used_until = None
        for day in adapters.daterange(start, end):
            plan = plans.get((eid, day))
            arr = shifts_service.arrangement_on(rows, day)
            if plan is None or arr is None or not arr.auto_shift or plan.source == "roster" or plan.day_type != "working":
                continue
            todays = [lt for lt, _p in mine if lt.date() == day and (used_until is None or lt > used_until)]
            if not todays:
                continue
            picked = shifts_service.closest_shift(todays[0], day, candidates, window, tz)
            if picked is None:
                continue
            plan.shift, plan.auto_shift = picked, True
            sp = shifts_service.spec(picked)
            if sp.crosses_midnight:
                used_until = sp.window(day, tz)[1] + timedelta(hours=4)


def _store(row: AttendanceDay, res: engine.DayResult, plan, rule_row) -> None:
    values = dict(
        status=res.status, day_type=res.day_type, shift_id=plan.shift.id if plan.shift else None,
        shift_start=res.shift_start, shift_end=res.shift_end, first_in=res.first_in, last_out=res.last_out,
        punch_count=res.punch_count, worked_minutes=res.worked_minutes, break_minutes=res.break_minutes,
        late_minutes=res.late_minutes, early_minutes=res.early_minutes, overtime_minutes=res.overtime_minutes,
        unapproved_overtime_minutes=res.unapproved_overtime_minutes, night_minutes=res.night_minutes,
        permission_minutes=res.permission_minutes, is_late=res.is_late, is_early_exit=res.is_early_exit,
        is_night_shift=res.is_night_shift, shift_allowance=res.shift_allowance, payable_days=res.payable_days,
        lop_days=res.lop_days, pay_cut_days=res.pay_cut_days, holiday_name=plan.holiday_name, notes=res.notes,
        rule_id=rule_row.id if rule_row else None,
    )
    # only touch what changed, so the audit log stays meaningful
    for k, v in values.items():
        if getattr(row, k) != v:
            setattr(row, k, v)
    row.computed_at = datetime.now(timezone.utc)


def _penalties(db: Session, company_id: int, employee_ids: list[int], start: date, end: date, book: RuleBook,
               emps: dict, plans: dict, lock_rows) -> None:
    """Re-apply late/early penalties for every month the range touches (taken from leave
    when the leave_link settings say so)."""
    from modules.attendance import leave_link
    link_cfg = leave_link.config(db, company_id, end).get("late_penalty") or {}
    months = sorted({(d.year, d.month) for d in adapters.daterange(start, end)})
    for y, m in months:
        ms, me = month_range(y, m)
        rows = (db.query(AttendanceDay).filter(AttendanceDay.company_id == company_id,
                                               AttendanceDay.employee_id.in_(employee_ids),
                                               AttendanceDay.date >= ms, AttendanceDay.date <= me)
                .order_by(AttendanceDay.employee_id, AttendanceDay.date).all())
        by_emp = defaultdict(list)
        for r in rows:
            by_emp[r.employee_id].append(r)
        for eid, days in by_emp.items():
            emp = emps.get(eid)
            last = days[-1]
            plan = plans.get((eid, last.date))
            rule_row = book.pick(emp, last.date, last.shift_id, plan.rule_id if plan else None) if emp else None
            pen = engine.apply_penalties(
                [{"date": d.date, "is_late": d.is_late, "is_early_exit": d.is_early_exit, "status": d.status} for d in days],
                rule_row.config if rule_row else {})
            pen, taken = leave_link.sync_late_penalties(db, company_id, eid, days, pen, link_cfg)
            for d in days:
                if d.locked:
                    continue
                val = pen.get(d.date, ZERO)
                note = "Late-mark penalty"
                notes = [n for n in (d.notes or []) if not n.startswith(note)]
                if taken.get(d.date):
                    notes.append(taken[d.date])
                elif val:
                    notes.append(f"{note}: {val.normalize()} day loss of pay")
                if d.penalty_lop_days != val:
                    d.penalty_lop_days = val
                if notes != (d.notes or []):
                    d.notes = notes
    db.flush()


def recompute_for_punch_times(db: Session, company_id: int, items: list[tuple[int, datetime]]) -> int:
    """After new punches: re-work the day of each punch and the day before (night shifts)."""
    if not items:
        return 0
    tz = company_tz(db, company_id)
    by_emp = defaultdict(set)
    for eid, ts in items:
        d = ts.astimezone(tz).date()
        by_emp[eid].update({d - timedelta(days=1), d})
    n = 0
    for eid, days in by_emp.items():
        n += recompute(db, company_id, [eid], min(days), max(days))
    return n


def process_yesterday(db: Session) -> int:
    """Periodic job: make sure every active employee has yesterday's record (absent when
    nobody punched). Idempotent."""
    from modules.company.models import Company
    from modules.employees.models import Employee
    done = 0
    for (cid,) in db.query(Company.id).filter(Company.active.is_(True)).all():
        tz = company_tz(db, cid)
        y = datetime.now(tz).date() - timedelta(days=1)
        ids = [i for (i,) in db.query(Employee.id).filter(Employee.company_id == cid, Employee.active.is_(True)).all()]
        have = {i for (i,) in db.query(AttendanceDay.employee_id).filter(AttendanceDay.company_id == cid,
                                                                         AttendanceDay.date == y).all()}
        missing = [i for i in ids if i not in have]
        if missing:
            db.info["company_id"] = cid
            done += recompute(db, cid, missing, y, y)
    db.info.pop("company_id", None)
    return done


# ── Clock-in from the web ───────────────────────────────────────────

def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def fences(db: Session, company_id: int) -> list[dict]:
    own = [{"id": g.id, "uuid": str(g.uuid), "name": g.name, "latitude": g.latitude, "longitude": g.longitude,
            "radius_m": g.radius_m, "address": g.address, "source": "attendance"}
           for g in db.query(Geofence).filter(Geofence.company_id == company_id, Geofence.active.is_(True)).all()]
    return own + [{**f, "source": "location"} for f in adapters.location_fences(db, company_id)]


def nearest_fence(db: Session, company_id: int, lat: float, lon: float) -> tuple[dict | None, float | None, bool]:
    best, best_d = None, None
    for f in fences(db, company_id):
        d = distance_m(lat, lon, f["latitude"], f["longitude"])
        if best_d is None or d - f["radius_m"] < best_d - best["radius_m"]:
            best, best_d = f, d
    inside = best is not None and best_d is not None and best_d <= best["radius_m"]
    return best, best_d, inside


def _ip_allowed(ip: str | None, allowed: list[str]) -> bool:
    if not allowed:
        return True
    import ipaddress
    try:
        addr = ipaddress.ip_address(ip or "")
    except ValueError:
        return False
    for item in allowed:
        try:
            if addr in ipaddress.ip_network(item.strip(), strict=False):
                return True
        except ValueError:
            continue
    return False


def _face_required(db: Session, company_id: int, employee_id: int) -> bool:
    """Whether web and phone clock-in needs a selfie that matches the person's enrolled face
    (set in face attendance settings)."""
    try:
        from modules.face_attendance import service as face
    except ImportError:
        return False
    return face.self_clock_required(db, company_id, employee_id)


def clock(db: Session, company_id: int, employee_id: int, data: dict) -> Punch:
    cfg = get_setting(db, company_id, "clock_in", today(db, company_id))
    if not cfg.get("web_enabled", True):
        raise BadRequest("Clocking in from the web is switched off. Use the attendance device.")
    ctx = context.current()
    if not _ip_allowed(ctx.ip_address, cfg.get("allowed_ips") or []):
        raise BadRequest("You can clock in only from the office network.")
    lat, lon = data.get("latitude"), data.get("longitude")
    fence, dist, inside = (None, None, False)
    if lat is not None and lon is not None:
        fence, dist, inside = nearest_fence(db, company_id, float(lat), float(lon))
    if cfg.get("geo_required"):
        if lat is None or lon is None:
            raise BadRequest("Allow location access to clock in.")
        if fence is None:
            raise BadRequest("No office location is set up yet. Ask HR to add one.")
        if not inside:
            away = int((dist or 0) - fence["radius_m"])
            raise BadRequest(f"You are {away} m outside {fence['name']}. Move closer to clock in.")
    if not data.get("_face_ok") and _face_required(db, company_id, employee_id):
        raise BadRequest("A face check is needed to clock in. Use Clock in on the attendance page.")
    selfie = data.get("selfie")
    if cfg.get("selfie_required") and not selfie:
        raise BadRequest("Take a selfie to clock in.")
    if selfie and (not str(selfie).startswith("data:image/") or len(selfie) > 700_000):
        raise BadRequest("The selfie must be a photo under 500 KB.")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    last = (db.query(Punch).filter(Punch.company_id == company_id, Punch.employee_id == employee_id,
                                   Punch.ignored.is_(False)).order_by(Punch.punched_at.desc()).first())
    if last and (now - last.punched_at).total_seconds() < 60:
        raise BadRequest("You just punched. Try again in a minute.")
    direction = data.get("direction")
    if direction not in ("in", "out"):
        direction = None
    p = Punch(company_id=company_id, employee_id=employee_id, punched_at=now, direction=direction, source="web",
              latitude=lat, longitude=lon, accuracy_m=data.get("accuracy"),
              geofence_id=fence["id"] if fence and fence.get("id") else None,
              distance_m=int(dist) if dist is not None else None, selfie=selfie, ip_address=ctx.ip_address)
    db.add(p)
    db.flush()
    recompute_for_punch_times(db, company_id, [(employee_id, now)])
    return p


# ── Views ───────────────────────────────────────────────────────────

def day_out(d: AttendanceDay, shift=None, name: str | None = None) -> dict:
    return {
        "uuid": str(d.uuid), "employee_id": d.employee_id, "employee_name": name, "date": d.date.isoformat(),
        "status": d.status, "status_label": engine.STATUSES.get(d.status, d.status), "day_type": d.day_type,
        "shift_name": shift.name if shift else None, "shift_code": (shift.code or shift.name[:2].upper()) if shift else None,
        "shift_color": shift.color if shift else None,
        "shift_start": iso_utc(d.shift_start), "shift_end": iso_utc(d.shift_end),
        "first_in": iso_utc(d.first_in), "last_out": iso_utc(d.last_out), "punch_count": d.punch_count,
        "worked_minutes": d.worked_minutes, "break_minutes": d.break_minutes, "late_minutes": d.late_minutes,
        "early_minutes": d.early_minutes, "overtime_minutes": d.overtime_minutes,
        "unapproved_overtime_minutes": d.unapproved_overtime_minutes, "night_minutes": d.night_minutes,
        "permission_minutes": d.permission_minutes, "is_late": d.is_late, "is_early_exit": d.is_early_exit,
        "is_night_shift": d.is_night_shift, "shift_allowance": d.shift_allowance,
        "payable_days": str(d.payable_days), "lop_days": str(d.lop_days), "penalty_lop_days": str(d.penalty_lop_days),
        "holiday_name": d.holiday_name, "notes": d.notes or [], "override_status": d.override_status,
        "override_note": d.override_note, "locked": d.locked, "computed_at": iso_utc(d.computed_at),
    }


def punch_out(p: Punch, fence_names: dict | None = None) -> dict:
    return {"uuid": str(p.uuid), "punched_at": iso_utc(p.punched_at), "direction": p.direction, "source": p.source,
            "latitude": p.latitude, "longitude": p.longitude, "distance_m": p.distance_m,
            "fence_name": (fence_names or {}).get(p.geofence_id), "has_selfie": bool(p.selfie),
            "ignored": p.ignored, "note": p.note, "device_id": p.device_id}


def _shift_map(db: Session, company_id: int) -> dict:
    from modules.shifts.models import Shift
    return {s.id: s for s in db.query(Shift).filter(Shift.company_id == company_id).all()}


def today_card(db: Session, company_id: int, employee_id: int) -> dict:
    tz = company_tz(db, company_id)
    now = datetime.now(tz)
    d = now.date()
    recompute(db, company_id, [employee_id], d - timedelta(days=1), d, now=now)
    plans = shifts_service.day_plans(db, company_id, [employee_id], d - timedelta(days=1), d)
    row = db.query(AttendanceDay).filter(AttendanceDay.company_id == company_id, AttendanceDay.employee_id == employee_id,
                                         AttendanceDay.date == d).first()
    # a night shift that started yesterday and is still running counts as "today"
    prev = db.query(AttendanceDay).filter(AttendanceDay.company_id == company_id, AttendanceDay.employee_id == employee_id,
                                          AttendanceDay.date == d - timedelta(days=1)).first()
    if prev and prev.shift_end and prev.first_in and not prev.last_out and now < prev.shift_end + timedelta(hours=4):
        row, d = prev, prev.date
    plan = plans.get((employee_id, d))
    lo = datetime.combine(d, time(0), tzinfo=tz) - timedelta(hours=6)
    punches = (db.query(Punch).filter(Punch.company_id == company_id, Punch.employee_id == employee_id,
                                      Punch.punched_at >= lo, Punch.ignored.is_(False))
               .order_by(Punch.punched_at).all())
    if row and row.shift_start:
        punches = [p for p in punches if p.punched_at >= row.shift_start - timedelta(hours=4)]
    shifts = _shift_map(db, company_id)
    cfg = get_setting(db, company_id, "clock_in", d)
    clocked_in = len(punches) % 2 == 1
    return {
        "date": d.isoformat(), "now": iso_utc(datetime.now(timezone.utc)),
        "day": day_out(row, shifts.get(row.shift_id)) if row else None,
        "plan": {"day_type": plan.day_type if plan else "working",
                 "shift": shifts_service.shift_out(plan.shift) if plan and plan.shift else None,
                 "holiday_name": plan.holiday_name if plan else None,
                 "punch_required": plan.punch_required if plan else True},
        "punches": [punch_out(p) for p in punches],
        "clocked_in": clocked_in, "next_action": "out" if clocked_in else "in",
        "settings": {"web_enabled": cfg.get("web_enabled", True), "geo_required": cfg.get("geo_required", False),
                     "selfie_required": cfg.get("selfie_required", False),
                     "face_required": _face_required(db, company_id, employee_id)},
        "fences": [{"name": f["name"], "latitude": f["latitude"], "longitude": f["longitude"], "radius_m": f["radius_m"]}
                   for f in fences(db, company_id)] if cfg.get("geo_required") else [],
    }


def month_view(db: Session, company_id: int, employee_id: int, year: int, month: int) -> dict:
    start, end = month_range(year, month)
    td = today(db, company_id)
    recompute(db, company_id, [employee_id], start, min(end, td))
    rows = {d.date: d for d in db.query(AttendanceDay).filter(
        AttendanceDay.company_id == company_id, AttendanceDay.employee_id == employee_id,
        AttendanceDay.date >= start, AttendanceDay.date <= end).all()}
    plans = shifts_service.day_plans(db, company_id, [employee_id], start, end)
    shifts = _shift_map(db, company_id)
    leave = adapters.leave_days(db, company_id, [employee_id], start, end)
    reqs = (db.query(AttendanceRequest).filter(AttendanceRequest.company_id == company_id,
                                               AttendanceRequest.employee_id == employee_id,
                                               AttendanceRequest.date >= start - timedelta(days=31),
                                               AttendanceRequest.date <= end,
                                               AttendanceRequest.status.in_(["pending", "approved"])).all())
    days = []
    for day in adapters.daterange(start, end):
        row = rows.get(day)
        if row:
            item = day_out(row, shifts.get(row.shift_id))
        else:
            plan = plans[(employee_id, day)]
            sh = plan.shift
            lv = leave.get((employee_id, day))
            status = plan.day_type if plan.day_type != "working" else ("on_leave" if lv or plan.on_roster_leave else "upcoming")
            item = {"date": day.isoformat(), "status": status,
                    "status_label": engine.STATUSES.get(status, "Upcoming"), "day_type": plan.day_type,
                    "shift_name": sh.name if sh else None, "shift_code": (sh.code if sh else None),
                    "shift_color": sh.color if sh else None, "holiday_name": plan.holiday_name, "notes": [],
                    "future": True}
        item["requests"] = [request_brief(r) for r in reqs if r.date <= day <= (r.end_date or r.date)]
        days.append(item)
    summary = _summarise([rows[d] for d in sorted(rows)])
    return {"year": year, "month": month, "days": days, "summary": summary}


def _summarise(rows: list[AttendanceDay]) -> dict:
    s = defaultdict(lambda: 0)
    pay = lop = pen = ZERO
    for r in rows:
        s[r.status] += 1
        s["late"] += 1 if r.is_late else 0
        s["early_exit"] += 1 if r.is_early_exit else 0
        s["worked_minutes"] += r.worked_minutes
        s["overtime_minutes"] += r.overtime_minutes
        pay += r.payable_days
        lop += r.lop_days
        pen += r.penalty_lop_days
    out = dict(s)
    out.update({"payable_days": str(pay - pen), "lop_days": str(lop + pen), "penalty_lop_days": str(pen)})
    return out


def team_today(db: Session, company_id: int, ids: list[int] | None, department_id: int | None = None,
               q: str | None = None) -> dict:
    tz = company_tz(db, company_id)
    now = datetime.now(tz)
    d = now.date()
    emps = employees_query(db, company_id, ids, department_id, q).all()
    eids = [e.id for e in emps]
    recompute(db, company_id, eids, d, d, now=now)
    rows = {r.employee_id: r for r in db.query(AttendanceDay).filter(
        AttendanceDay.company_id == company_id, AttendanceDay.employee_id.in_(eids or [-1]), AttendanceDay.date == d).all()}
    shifts = _shift_map(db, company_id)
    people, counts = [], defaultdict(int)
    for e in emps:
        r = rows.get(e.id)
        status = r.status if r else "not_yet"
        bucket = {"present": "present", "half_day": "present", "wfh": "remote", "on_duty": "remote",
                  "on_leave": "on_leave", "absent": "absent", "missing_punch": "present", "not_yet": "not_yet",
                  "holiday": "off", "weekly_off": "off"}.get(status, "absent")
        counts[bucket] += 1
        if r and r.is_late:
            counts["late"] += 1
        people.append({"employee_id": e.id, "employee_uuid": str(e.uuid), "name": name_of(e),
                       "employee_code": e.employee_code, "photo": e.profile_image_url,
                       "department_id": e.department_id, "bucket": bucket,
                       **(day_out(r, shifts.get(r.shift_id)) if r else {"status": "not_yet", "status_label": "Not in yet"})})
    counts["total"] = len(emps)
    return {"date": d.isoformat(), "counts": dict(counts), "people": people}


def muster(db: Session, company_id: int, ids: list[int] | None, year: int, month: int,
           department_id: int | None = None, q: str | None = None) -> dict:
    start, end = month_range(year, month)
    td = today(db, company_id)
    emps = employees_query(db, company_id, ids, department_id, q).all()
    eids = [e.id for e in emps]
    if start <= td:
        recompute(db, company_id, eids, start, min(end, td))
    rows = defaultdict(dict)
    for r in db.query(AttendanceDay).filter(AttendanceDay.company_id == company_id,
                                            AttendanceDay.employee_id.in_(eids or [-1]),
                                            AttendanceDay.date >= start, AttendanceDay.date <= end).all():
        rows[r.employee_id][r.date] = r
    plans = shifts_service.day_plans(db, company_id, eids, start, end) if eids else {}
    out = []
    for e in emps:
        mine = rows.get(e.id, {})
        cells = {}
        for day in adapters.daterange(start, end):
            r = mine.get(day)
            if r:
                cells[day.isoformat()] = {"s": r.status, "late": r.is_late, "early": r.is_early_exit,
                                          "ot": r.overtime_minutes, "w": r.worked_minutes, "uuid": str(r.uuid)}
            else:
                p = plans.get((e.id, day))
                cells[day.isoformat()] = {"s": p.day_type if p and p.day_type != "working" else "upcoming", "future": True}
        out.append({"employee_id": e.id, "employee_uuid": str(e.uuid), "name": name_of(e),
                    "employee_code": e.employee_code, "cells": cells,
                    "summary": _summarise([mine[d] for d in sorted(mine)])})
    return {"year": year, "month": month, "start": start.isoformat(), "end": end.isoformat(), "rows": out}


def records_query(db: Session, company_id: int, ids: list[int] | None, start: date, end: date,
                  status: list[str] | None = None, flags: list[str] | None = None, department_id: int | None = None,
                  q: str | None = None):
    from modules.employees.models import Employee
    from modules.employees import service as emp_service
    query = (db.query(AttendanceDay, Employee).join(Employee, Employee.id == AttendanceDay.employee_id)
             .filter(AttendanceDay.company_id == company_id, AttendanceDay.date >= start, AttendanceDay.date <= end))
    if ids is not None:
        query = query.filter(AttendanceDay.employee_id.in_(ids or [-1]))
    if status:
        query = query.filter(AttendanceDay.status.in_(status))
    for f in flags or []:
        if f == "late":
            query = query.filter(AttendanceDay.is_late.is_(True))
        elif f == "early":
            query = query.filter(AttendanceDay.is_early_exit.is_(True))
        elif f == "overtime":
            query = query.filter(AttendanceDay.overtime_minutes > 0)
        elif f == "overridden":
            query = query.filter(AttendanceDay.override_status.isnot(None))
        elif f == "night":
            query = query.filter(AttendanceDay.is_night_shift.is_(True))
    if department_id:
        query = query.filter(Employee.department_id == department_id)
    query = emp_service.search(query, q)
    return query.order_by(AttendanceDay.date.desc(), Employee.first_name)


def day_detail(db: Session, company_id: int, row: AttendanceDay) -> dict:
    tz = company_tz(db, company_id)
    shifts = _shift_map(db, company_id)
    lo = row.shift_start - timedelta(hours=6) if row.shift_start else datetime.combine(row.date, time(0), tzinfo=tz)
    hi = row.shift_end + timedelta(hours=10) if row.shift_end else datetime.combine(row.date + timedelta(days=1), time(6), tzinfo=tz)
    punches = (db.query(Punch).filter(Punch.company_id == company_id, Punch.employee_id == row.employee_id,
                                      Punch.punched_at >= lo, Punch.punched_at <= hi).order_by(Punch.punched_at).all())
    fence_names = {g.id: g.name for g in db.query(Geofence).filter(Geofence.company_id == company_id).all()}
    reqs = (db.query(AttendanceRequest).filter(AttendanceRequest.company_id == company_id,
                                               AttendanceRequest.employee_id == row.employee_id,
                                               AttendanceRequest.date <= row.date,
                                               func.coalesce(AttendanceRequest.end_date, AttendanceRequest.date) >= row.date)
            .order_by(AttendanceRequest.created_at.desc()).all())
    emp = employee(db, company_id, row.employee_id)
    rule = db.query(AttendanceRule).filter(AttendanceRule.id == row.rule_id).first() if row.rule_id else None
    adj = (db.query(AttendanceAdjustment).filter(AttendanceAdjustment.company_id == company_id,
                                                 AttendanceAdjustment.employee_id == row.employee_id,
                                                 AttendanceAdjustment.date == row.date).all())
    return {**day_out(row, shifts.get(row.shift_id), name_of(emp)), "employee_uuid": str(emp.uuid),
            "punches": [punch_out(p, fence_names) for p in punches],
            "requests": [request_brief(r) for r in reqs],
            "rule": {"name": rule.name, "version": rule.version} if rule else None,
            "adjustments": [{"payable_days_delta": str(a.payable_days_delta), "lop_days_delta": str(a.lop_days_delta),
                             "overtime_minutes_delta": a.overtime_minutes_delta, "apply_from": a.apply_from.isoformat(),
                             "new_status": a.new_status} for a in adj]}


def set_override(db: Session, company_id: int, row: AttendanceDay, status: str | None, note: str | None) -> AttendanceDay:
    if status and status not in engine.STATUSES:
        raise BadRequest("Choose a valid status.")
    if status and not (note or "").strip():
        raise BadRequest("Add a note saying why the status is changed by hand.")
    row.override_status = status or None
    row.override_note = (note or "").strip() or None
    db.flush()
    recompute(db, company_id, [row.employee_id], row.date, row.date)
    return row


def selfie_of(db: Session, company_id: int, punch_uuid) -> str:
    p = db.query(Punch).filter(Punch.company_id == company_id, Punch.uuid == punch_uuid).first()
    if not p or not p.selfie:
        raise NotFound("Photo")
    return p.selfie


def purge_old_selfies(db: Session) -> int:
    """Periodic job: delete selfies older than each company's retention period (DPDP)."""
    from modules.company.models import Company
    n = 0
    for (cid,) in db.query(Company.id).all():
        days = int(get_setting(db, cid, "clock_in").get("selfie_retention_days") or 90)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        n += (db.query(Punch).filter(Punch.company_id == cid, Punch.selfie.isnot(None), Punch.punched_at < cutoff)
              .update({"selfie": None}, synchronize_session=False))
    return n


# ── Requests (shared helpers; rules per kind live in requests.py) ───

REQUEST_KINDS = {"regularisation": "Attendance correction", "on_duty": "On duty", "wfh": "Work from home",
                 "permission": "Permission hours", "overtime": "Overtime pre-approval"}


def request_brief(r: AttendanceRequest) -> dict:
    return {"uuid": str(r.uuid), "kind": r.kind, "kind_label": REQUEST_KINDS.get(r.kind, r.kind),
            "date": r.date.isoformat(), "end_date": r.end_date.isoformat() if r.end_date else None,
            "from_time": r.from_time.strftime("%H:%M") if r.from_time else None,
            "to_time": r.to_time.strftime("%H:%M") if r.to_time else None, "minutes": r.minutes,
            "duration": r.duration, "category_id": r.category_id, "reason": r.reason, "details": r.details or {},
            "status": r.status, "created_at": iso_utc(r.created_at), "decided_at": iso_utc(r.decided_at)}


# ── Payroll export and reports ──────────────────────────────────────

def payroll_summary(db: Session, company_id: int, start: date, end: date, employee_ids: list[int] | None = None) -> list[dict]:
    """Per employee for a pay period: payable days, loss-of-pay days, overtime hours,
    night-shift and shift-allowance counts, plus adjustments from earlier locked periods
    that are paid in this one. For the payroll module."""
    emps = employees_query(db, company_id, employee_ids, active_only=False).all()
    eids = [e.id for e in emps]
    # bring open days up to date (days without punches, e.g. weekly offs, are made here)
    active = [e.id for e in emps if e.active]
    if active and start <= today(db, company_id):
        recompute(db, company_id, active, start, min(end, today(db, company_id)))
    rows = defaultdict(list)
    for r in db.query(AttendanceDay).filter(AttendanceDay.company_id == company_id,
                                            AttendanceDay.employee_id.in_(eids or [-1]),
                                            AttendanceDay.date >= start, AttendanceDay.date <= end).all():
        rows[r.employee_id].append(r)
    adjs = defaultdict(list)
    for a in db.query(AttendanceAdjustment).filter(AttendanceAdjustment.company_id == company_id,
                                                   AttendanceAdjustment.employee_id.in_(eids or [-1]),
                                                   AttendanceAdjustment.apply_from >= start,
                                                   AttendanceAdjustment.apply_from <= end).all():
        adjs[a.employee_id].append(a)
    shifts = _shift_map(db, company_id)
    days_in_period = (end - start).days + 1
    # loss-of-pay cap, carry-over and manual changes apply to whole pay periods
    from modules.attendance import lop as lop_mod
    in_period = pay_period_for(db, company_id, start) == (start, end)
    figures = lop_mod.period_rows(db, company_id, start, end, eids) if in_period else {}
    out = []
    for e in emps:
        mine = rows.get(e.id, [])
        fig = figures.get(e.id)
        if not mine and not adjs.get(e.id) and not fig:
            continue
        pay = sum((r.payable_days for r in mine), ZERO)
        lop = sum((r.lop_days for r in mine), ZERO)
        pen = sum((r.penalty_lop_days for r in mine), ZERO)
        cut = sum((r.pay_cut_days for r in mine), ZERO)
        ot = sum(r.overtime_minutes for r in mine)
        allowance_amount = sum((Decimal(shifts[r.shift_id].allowance_amount or 0) for r in mine
                                if r.shift_allowance and r.shift_id in shifts), ZERO)
        a = adjs.get(e.id, [])
        adj_lop = sum((x.lop_days_delta for x in a), ZERO)
        lop_total = lop + pen
        extra = {}
        if fig is not None:
            # payroll adds adjustment_lop_days itself, so lop_days carries the rest of the final figure
            lop_total = fig["payable"] + fig["reduced_pay_days"] - adj_lop
            extra = {"lop_attendance_days": str(fig["attendance_lop"]), "lop_carried_in": str(fig["carried_in"]),
                     "lop_manual": str(fig["manual"]), "lop_carried_out": str(fig["carried_out"]),
                     "lop_waived": str(fig["waived"]), "lop_cap_days": fig["cap_days"], "lop_final": fig["locked"]}
        out.append({
            "employee_id": e.id, "employee_uuid": str(e.uuid), "employee_code": e.employee_code, "name": name_of(e),
            "days_in_period": days_in_period, "days_recorded": len(mine),
            "present_days": sum(1 for r in mine if r.status in ("present", "wfh", "on_duty")),
            "half_days": sum(1 for r in mine if r.status == "half_day"),
            "absent_days": sum(1 for r in mine if r.status in ("absent", "missing_punch")),
            "leave_days": sum(1 for r in mine if r.status == "on_leave"),
            "holidays": sum(1 for r in mine if r.status == "holiday"),
            "weekly_offs": sum(1 for r in mine if r.status == "weekly_off"),
            "late_marks": sum(1 for r in mine if r.is_late),
            "payable_days": str(pay - pen - (lop_total - lop - pen)), "lop_days": str(lop_total),
            "penalty_lop_days": str(pen), "reduced_pay_days": str(cut), **extra,
            "overtime_hours": str((Decimal(ot) / 60).quantize(Decimal("0.01"))),
            "night_shifts": sum(1 for r in mine if r.is_night_shift),
            "shift_allowance_days": sum(1 for r in mine if r.shift_allowance),
            "shift_allowance_amount": str(allowance_amount),
            "adjustment_payable_days": str(sum((x.payable_days_delta for x in a), ZERO)),
            "adjustment_lop_days": str(sum((x.lop_days_delta for x in a), ZERO)),
            "adjustment_overtime_hours": str((Decimal(sum(x.overtime_minutes_delta for x in a)) / 60).quantize(Decimal("0.01"))),
            "locked": all(r.locked for r in mine) if mine else False,
        })
    return out


def overtime_report(db: Session, company_id: int, ids: list[int] | None, start: date, end: date) -> dict:
    """Overtime per employee for the range, with the quarter-to-date total against the
    quarterly limit (OSH Code) and an alert when close to or over it."""
    emps = {e.id: e for e in employees_query(db, company_id, ids, active_only=False).all()}
    eids = list(emps)
    q_start = date(end.year, 3 * ((end.month - 1) // 3) + 1, 1)
    book = RuleBook(db, company_id)
    rng = defaultdict(int)
    qtd = defaultdict(int)
    days = defaultdict(int)
    unapproved = defaultdict(int)
    for r in db.query(AttendanceDay).filter(AttendanceDay.company_id == company_id,
                                            AttendanceDay.employee_id.in_(eids or [-1]),
                                            AttendanceDay.date >= min(start, q_start), AttendanceDay.date <= end,
                                            or_(AttendanceDay.overtime_minutes > 0,
                                                AttendanceDay.unapproved_overtime_minutes > 0)).all():
        if r.date >= start:
            rng[r.employee_id] += r.overtime_minutes
            days[r.employee_id] += 1 if r.overtime_minutes else 0
            unapproved[r.employee_id] += r.unapproved_overtime_minutes
        if r.date >= q_start:
            qtd[r.employee_id] += r.overtime_minutes
    rows = []
    for eid in set(rng) | set(qtd):
        e = emps[eid]
        rule = book.pick(e, end, None, None)
        ot = engine.merged_rule(rule.config if rule else None)["overtime"]
        cap = float(ot.get("quarterly_cap_hours") or 0)
        pct = float(ot.get("alert_at_percent") or 80)
        q_hours = qtd[eid] / 60
        level = "over" if cap and q_hours > cap else ("near" if cap and q_hours >= cap * pct / 100 else "ok")
        rows.append({"employee_id": eid, "employee_uuid": str(e.uuid), "name": name_of(e), "employee_code": e.employee_code,
                     "overtime_hours": round(rng[eid] / 60, 2), "overtime_days": days[eid],
                     "unapproved_hours": round(unapproved[eid] / 60, 2),
                     "quarter_hours": round(q_hours, 2), "quarter_cap_hours": cap, "alert": level,
                     "rate_multiplier": ot.get("rate_multiplier", 2)})
    rows.sort(key=lambda x: -x["quarter_hours"])
    return {"start": start.isoformat(), "end": end.isoformat(), "quarter_start": q_start.isoformat(), "rows": rows}


def late_early_report(db: Session, company_id: int, ids: list[int] | None, start: date, end: date) -> dict:
    emps = {e.id: e for e in employees_query(db, company_id, ids, active_only=False).all()}
    agg = defaultdict(lambda: {"late": 0, "late_minutes": 0, "early": 0, "early_minutes": 0, "penalty": ZERO, "dates": []})
    for r in db.query(AttendanceDay).filter(AttendanceDay.company_id == company_id,
                                            AttendanceDay.employee_id.in_(list(emps) or [-1]),
                                            AttendanceDay.date >= start, AttendanceDay.date <= end,
                                            or_(AttendanceDay.is_late.is_(True), AttendanceDay.is_early_exit.is_(True)))\
            .order_by(AttendanceDay.date).all():
        a = agg[r.employee_id]
        if r.is_late:
            a["late"] += 1
            a["late_minutes"] += r.late_minutes
        if r.is_early_exit:
            a["early"] += 1
            a["early_minutes"] += r.early_minutes
        a["penalty"] += r.penalty_lop_days
        a["dates"].append({"date": r.date.isoformat(), "late_minutes": r.late_minutes if r.is_late else 0,
                           "early_minutes": r.early_minutes if r.is_early_exit else 0})
    rows = [{"employee_id": eid, "employee_uuid": str(emps[eid].uuid), "name": name_of(emps[eid]),
             "employee_code": emps[eid].employee_code, **{k: (str(v) if k == "penalty" else v) for k, v in a.items()}}
            for eid, a in agg.items()]
    rows.sort(key=lambda x: (-x["late"], -x["early"]))
    return {"start": start.isoformat(), "end": end.isoformat(), "rows": rows}


MUSTER_CODES = {"present": "P", "absent": "A", "half_day": "HD", "on_leave": "L", "holiday": "H", "weekly_off": "WO",
                "wfh": "WFH", "on_duty": "OD", "missing_punch": "MP", "not_yet": "", "upcoming": ""}
