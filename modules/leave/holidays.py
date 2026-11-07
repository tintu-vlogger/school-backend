"""Holiday calendars, holidays and work-week patterns (settings), plus re-working leave
already applied when a holiday is added, moved or removed."""
from __future__ import annotations

import csv
import io
from datetime import date

from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from core import audit
from core.errors import BadRequest, NotFound
from modules.leave import ledger, policy
from modules.leave.calendar import WEEKDAYS, WorkCalendar, clean_days
from modules.leave.ledger import D, ZERO
from modules.leave.models import Holiday, HolidayCalendar, LeaveRequest, WorkWeekPattern
from modules.leave.rules import with_defaults

KINDS = {"national": "National (compulsory)", "festival": "Festival", "optional": "Optional"}


def _clean_app(app: dict | None) -> dict:
    return with_defaults({"applicability": app or {}})["applicability"] if app else {}


# ── Calendars ───────────────────────────────────────────────────────

def list_calendars(db: Session, company_id: int) -> list[HolidayCalendar]:
    return (db.query(HolidayCalendar).filter(HolidayCalendar.company_id == company_id)
            .order_by(HolidayCalendar.is_default.desc(), HolidayCalendar.priority, HolidayCalendar.name).all())


def get_calendar(db: Session, company_id: int, uuid) -> HolidayCalendar:
    row = db.query(HolidayCalendar).filter(HolidayCalendar.company_id == company_id, HolidayCalendar.uuid == uuid).first()
    if row is None:
        raise NotFound("Holiday calendar")
    return row


def calendar_out(db: Session, c: HolidayCalendar, year: int | None = None) -> dict:
    q = db.query(Holiday.kind, func.count(Holiday.id)).filter(Holiday.calendar_id == c.id)
    if year:
        q = q.filter(Holiday.date >= date(year, 1, 1), Holiday.date <= date(year, 12, 31))
    counts = dict(q.group_by(Holiday.kind).all())
    return {"uuid": str(c.uuid), "id": c.id, "name": c.name, "description": c.description,
            "applicability": c.applicability or {}, "is_default": c.is_default, "priority": c.priority,
            "optional_limit": c.optional_limit, "active": c.active,
            "counts": {"national": counts.get("national", 0), "festival": counts.get("festival", 0),
                       "optional": counts.get("optional", 0)},
            "locations": _locations_of(db, c)}


def _locations_of(db: Session, c: HolidayCalendar) -> list[dict]:
    try:
        from modules.organization.models import Location
        with db.begin_nested():
            rows = db.query(Location.id, Location.name).filter(Location.company_id == c.company_id,
                                                               Location.holiday_calendar_id == c.id).all()
        return [{"id": i, "name": n} for i, n in rows]
    except Exception:
        return []


def _set_locations(db: Session, c: HolidayCalendar, location_ids: list[int] | None) -> None:
    if location_ids is None:
        return
    try:
        from modules.organization.models import Location
        with db.begin_nested():
            for loc in db.query(Location).filter(Location.company_id == c.company_id).all():
                if loc.id in location_ids and loc.holiday_calendar_id != c.id:
                    loc.holiday_calendar_id = c.id
                elif loc.id not in location_ids and loc.holiday_calendar_id == c.id:
                    loc.holiday_calendar_id = None
            db.flush()
    except Exception:
        pass


def save_calendar(db: Session, company_id: int, data: dict, row: HolidayCalendar | None = None) -> HolidayCalendar:
    name = (data.get("name") or (row.name if row else "")).strip()
    if not name:
        raise BadRequest("Give the calendar a name.")
    if row is None:
        row = HolidayCalendar(company_id=company_id, name=name)
        db.add(row)
    row.name = name[:150]
    if "description" in data:
        row.description = (data.get("description") or "").strip() or None
    if "applicability" in data:
        row.applicability = _clean_app(data.get("applicability"))
    if data.get("priority") is not None:
        row.priority = int(data["priority"])
    if data.get("optional_limit") is not None:
        lim = int(data["optional_limit"])
        if lim < 0 or lim > 30:
            raise BadRequest("Optional holidays per year must be between 0 and 30.")
        row.optional_limit = lim
    if data.get("active") is not None:
        row.active = bool(data["active"])
    if data.get("is_default"):
        for other in db.query(HolidayCalendar).filter(HolidayCalendar.company_id == company_id,
                                                      HolidayCalendar.is_default.is_(True)).all():
            if other is not row:
                other.is_default = False
        row.is_default = True
    db.flush()
    _set_locations(db, row, data.get("location_ids"))
    if not db.query(HolidayCalendar.id).filter(HolidayCalendar.company_id == company_id,
                                               HolidayCalendar.is_default.is_(True)).first():
        raise BadRequest("Keep one calendar as the default.")
    return row


def copy_calendar(db: Session, company_id: int, src: HolidayCalendar, name: str) -> HolidayCalendar:
    row = save_calendar(db, company_id, {"name": name, "description": src.description,
                                         "optional_limit": src.optional_limit, "priority": src.priority + 1})
    for h in src.holidays:
        db.add(Holiday(company_id=company_id, calendar=row, name=h.name, date=h.date, kind=h.kind,
                       description=h.description))
    db.flush()
    return row


# ── Holidays ────────────────────────────────────────────────────────

def list_holidays(db: Session, company_id: int, calendar: HolidayCalendar | None, year: int | None) -> list[dict]:
    q = (db.query(Holiday).options(selectinload(Holiday.calendar))
         .filter(Holiday.company_id == company_id))
    if calendar is not None:
        q = q.filter(Holiday.calendar_id == calendar.id)
    if year:
        q = q.filter(Holiday.date >= date(year, 1, 1), Holiday.date <= date(year, 12, 31))
    return [holiday_out(h) for h in q.order_by(Holiday.date, Holiday.name).all()]


def holiday_out(h: Holiday) -> dict:
    return {"uuid": str(h.uuid), "name": h.name, "date": h.date.isoformat(), "kind": h.kind,
            "kind_label": KINDS.get(h.kind, h.kind), "description": h.description,
            "weekday": WEEKDAYS[h.date.weekday()], "calendar": {"uuid": str(h.calendar.uuid), "name": h.calendar.name}}


def _check_holiday(data: dict) -> tuple[str, date, str]:
    name = (data.get("name") or "").strip()
    if not name:
        raise BadRequest("Give the holiday a name.")
    d = data.get("date")
    try:
        d = date.fromisoformat(d) if isinstance(d, str) else d
    except ValueError:
        d = None
    if not isinstance(d, date):
        raise BadRequest("Pick the date of the holiday.")
    kind = data.get("kind") or "festival"
    if kind not in KINDS:
        raise BadRequest("Choose national, festival or optional.")
    return name[:150], d, kind


def add_holiday(db: Session, company_id: int, calendars: list[HolidayCalendar], data: dict,
                reprocess: bool = True) -> list[Holiday]:
    name, d, kind = _check_holiday(data)
    end = data.get("end_date")
    end = date.fromisoformat(end) if isinstance(end, str) and end else d
    if end < d or (end - d).days > 14:
        raise BadRequest("A holiday can run for at most 15 days.")
    if not calendars:
        raise BadRequest("Pick at least one calendar.")
    out = []
    cur = d
    while cur <= end:
        for c in calendars:
            exists = db.query(Holiday.id).filter(Holiday.calendar_id == c.id, Holiday.date == cur,
                                                 Holiday.name == name).first()
            if exists:
                continue
            h = Holiday(company_id=company_id, calendar_id=c.id, name=name, date=cur, kind=kind,
                        description=(data.get("description") or "").strip() or None)
            db.add(h)
            out.append(h)
        cur = date.fromordinal(cur.toordinal() + 1)
    db.flush()
    if reprocess and kind != "optional":
        reprocess_dates(db, company_id, {h.date for h in out})
    return out


def get_holiday(db: Session, company_id: int, uuid) -> Holiday:
    h = db.query(Holiday).filter(Holiday.company_id == company_id, Holiday.uuid == uuid).first()
    if h is None:
        raise NotFound("Holiday")
    return h


def update_holiday(db: Session, company_id: int, h: Holiday, data: dict) -> Holiday:
    name, d, kind = _check_holiday({"name": data.get("name", h.name), "date": data.get("date", h.date.isoformat()),
                                    "kind": data.get("kind", h.kind)})
    old = h.date
    h.name, h.date, h.kind = name, d, kind
    if "description" in data:
        h.description = (data.get("description") or "").strip() or None
    db.flush()
    reprocess_dates(db, company_id, {old, d})
    return h


def delete_holiday(db: Session, company_id: int, h: Holiday) -> None:
    d = h.date
    audit.event(db, "leave.holiday_removed", "holidays", h.id, {"name": h.name, "date": d.isoformat()},
                entity_uuid=str(h.uuid), entity_label=h.name)
    db.delete(h)
    db.flush()
    reprocess_dates(db, company_id, {d})


def import_holidays(db: Session, company_id: int, calendar: HolidayCalendar, text: str) -> dict:
    """CSV with columns name, date (YYYY-MM-DD or DD-MM-YYYY), kind (national|festival|optional)."""
    reader = csv.DictReader(io.StringIO(text.strip().lstrip("﻿")))
    cols = {c.strip().lower(): c for c in (reader.fieldnames or [])}
    if "name" not in cols or "date" not in cols:
        raise BadRequest("The file needs the columns name and date (kind is optional).")
    added, errors, dates = 0, [], set()
    for i, raw in enumerate(reader, start=2):
        name = (raw.get(cols["name"]) or "").strip()
        ds = (raw.get(cols["date"]) or "").strip()
        kind = (raw.get(cols.get("kind", ""), "") or "festival").strip().lower() if "kind" in cols else "festival"
        d = None
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                from datetime import datetime
                d = datetime.strptime(ds, fmt).date()
                break
            except ValueError:
                continue
        if not name or d is None:
            errors.append({"row": i, "detail": "Name and a valid date are needed."})
            continue
        if kind not in KINDS:
            kind = "optional" if kind.startswith(("opt", "rest")) else "national" if kind.startswith("nat") else "festival"
        if db.query(Holiday.id).filter(Holiday.calendar_id == calendar.id, Holiday.date == d, Holiday.name == name).first():
            continue
        db.add(Holiday(company_id=company_id, calendar_id=calendar.id, name=name[:150], date=d, kind=kind))
        dates.add(d)
        added += 1
    db.flush()
    reprocess_dates(db, company_id, dates)
    return {"added": added, "errors": errors}


def reprocess_dates(db: Session, company_id: int, dates: set[date]) -> int:
    """After a holiday change, re-work leave on those dates: a day that became a holiday
    is no longer taken from the balance (and the other way round)."""
    dates = {d for d in dates if d}
    if not dates:
        return 0
    lo, hi = min(dates), max(dates)
    reqs = (db.query(LeaveRequest).filter(LeaveRequest.company_id == company_id,
                                          LeaveRequest.status.in_(("pending", "approved", "cancel_pending")),
                                          LeaveRequest.from_date <= hi, LeaveRequest.to_date >= lo).all())
    if not reqs:
        return 0
    cal = WorkCalendar(db, company_id)
    from modules.leave.service import _history, _recalc
    changed = 0
    for r in reqs:
        rules = policy.rules_on(r.leave_type, r.from_date)
        s = rules["restrictions"]
        items = [dict(it) for it in r.breakdown or []]
        delta = ZERO
        touched = False
        for it in items:
            d = date.fromisoformat(it["date"])
            if d not in dates or it.get("cancelled") or it.get("kind") == "optional":
                continue
            x = cal.day(r.employee, d)
            if x["kind"] == it["kind"] and x.get("holiday") == it.get("holiday"):
                continue
            if x["kind"] in ("working", "half"):
                new = x["value"] if r.unit == "day" else min(D(it["amount"]), x["value"]) or D(it["amount"])
            elif x["kind"] == "weekend":
                new = D(1) if s["count_weekends"] else ZERO
            else:
                new = D(1) if s["count_holidays"] else ZERO
            if it.get("sandwich") and new == 0:
                new = D(it["amount"])  # still sandwiched
            old_paid = D(it["amount"]) - D(it.get("lop"))
            it["kind"], it["amount"] = x["kind"], float(new)
            it["lop"] = float(min(D(it.get("lop")), new))
            if x.get("holiday"):
                it["holiday"] = x["holiday"]
            else:
                it.pop("holiday", None)
            delta += (new - D(it["lop"])) - old_paid
            touched = True
        if not touched:
            continue
        r.breakdown = items
        _recalc(r)
        if r.status in ("approved", "cancel_pending") and delta != 0 and r.leave_type.kind != "lop" and rules["paid"] == "paid":
            y = policy.year_bounds(rules, r.from_date)[0]
            ledger.add_entry(db, company_id, r.employee_id, r.leave_type_id, y, r.from_date,
                             "restored" if delta < 0 else "availed", -delta, request_id=r.id,
                             note="Holiday list changed")
        _history(r, "Re-worked after a holiday change")
        changed += 1
    db.flush()
    return changed


# ── Work weeks ──────────────────────────────────────────────────────

def list_patterns(db: Session, company_id: int) -> list[WorkWeekPattern]:
    return (db.query(WorkWeekPattern).filter(WorkWeekPattern.company_id == company_id)
            .order_by(WorkWeekPattern.is_default.desc(), WorkWeekPattern.priority, WorkWeekPattern.name).all())


def get_pattern(db: Session, company_id: int, uuid) -> WorkWeekPattern:
    row = db.query(WorkWeekPattern).filter(WorkWeekPattern.company_id == company_id, WorkWeekPattern.uuid == uuid).first()
    if row is None:
        raise NotFound("Work week")
    return row


def pattern_out(p: WorkWeekPattern) -> dict:
    working = sum(1 if d["mode"] == "working" and not d.get("off_weeks") else 0.5 if d["mode"] == "half" else
                  (1 - len(d.get("off_weeks") or []) / 4.35 if d["mode"] == "working" else 0) for d in p.days)
    return {"uuid": str(p.uuid), "name": p.name, "description": p.description, "days": p.days,
            "applicability": p.applicability or {}, "is_default": p.is_default, "priority": p.priority,
            "active": p.active, "working_days_per_week": round(working, 1)}


def save_pattern(db: Session, company_id: int, data: dict, row: WorkWeekPattern | None = None) -> WorkWeekPattern:
    name = (data.get("name") or (row.name if row else "")).strip()
    if not name:
        raise BadRequest("Give the work week a name.")
    if row is None:
        row = WorkWeekPattern(company_id=company_id, name=name, days=clean_days(data.get("days")))
        db.add(row)
    row.name = name[:150]
    if data.get("days") is not None:
        row.days = clean_days(data["days"])
    if "description" in data:
        row.description = (data.get("description") or "").strip() or None
    if "applicability" in data:
        row.applicability = _clean_app(data.get("applicability"))
    if data.get("priority") is not None:
        row.priority = int(data["priority"])
    if data.get("active") is not None:
        row.active = bool(data["active"])
    if data.get("is_default"):
        for other in db.query(WorkWeekPattern).filter(WorkWeekPattern.company_id == company_id,
                                                      WorkWeekPattern.is_default.is_(True)).all():
            if other is not row:
                other.is_default = False
        row.is_default = True
    db.flush()
    if not db.query(WorkWeekPattern.id).filter(WorkWeekPattern.company_id == company_id,
                                               WorkWeekPattern.is_default.is_(True)).first():
        raise BadRequest("Keep one work week as the default.")
    return row


# ── Employee view ───────────────────────────────────────────────────

def my_holidays(db: Session, company_id: int, emp, year: int) -> dict:
    cal = WorkCalendar(db, company_id)
    c = cal.calendar_for(emp)
    if c is None:
        return {"calendar": None, "holidays": [], "optional_limit": 0, "optional_taken": 0}
    rows = (db.query(Holiday).filter(Holiday.calendar_id == c.id, Holiday.date >= date(year, 1, 1),
                                     Holiday.date <= date(year, 12, 31)).order_by(Holiday.date).all())
    taken = {}
    oh = [r for r in (db.query(LeaveRequest).filter(LeaveRequest.company_id == company_id, LeaveRequest.employee_id == emp.id,
                                                    LeaveRequest.status.in_(("pending", "approved")),
                                                    LeaveRequest.from_date >= date(year, 1, 1),
                                                    LeaveRequest.from_date <= date(year, 12, 31)).all())
          if r.leave_type.kind == "optional_holiday"]
    for r in oh:
        for it in r.breakdown or []:
            if not it.get("cancelled"):
                taken[it["date"]] = r.status
    items = [{**{k: v for k, v in holiday_out(h).items() if k != "calendar"},
              "taken": taken.get(h.date.isoformat())} for h in rows]
    if cal.substitute.get("enabled"):
        for d, name in cal.substitutes(c, cal.pattern_for(emp)).items():
            if d.year == year:
                items.append({"uuid": None, "name": name, "date": d.isoformat(), "kind": "substitute",
                              "kind_label": "Substitute day off", "description": None,
                              "weekday": WEEKDAYS[d.weekday()], "taken": None})
        items.sort(key=lambda x: x["date"])
    return {"calendar": {"uuid": str(c.uuid), "name": c.name}, "optional_limit": c.optional_limit,
            "optional_taken": len(taken), "holidays": items}
