"""Attendance and leave working together (settings key `leave_link`).

1. Absence to leave. An absent day (or the missing half of a half day) that is still absent
   `after_days` later is covered by leave from the listed leave types, the first one with
   enough balance, else marked loss of pay (an LOP leave entry when the company has an LOP
   leave type). With `needs_approval` the change goes to the employee's manager first
   (request type `attendance.absence_leave`). When the attendance is corrected later (an
   approved regularisation, a device punch, WFH or on duty), the leave is cancelled again
   and the balance restored.
2. Late-mark penalties. The penalty days that `engine.apply_penalties` gives (e.g. every
   3rd late arrival = half a day) are taken from the listed leave types in order; when none
   has the balance they stay loss of pay. Undone when the late mark goes away.
3. Comp off. Work on a weekly off or a holiday of at least `half_day_hours` credits comp off
   through the leave module (full day from `full_day_hours`), straight away or after
   approval. Taken back when the attendance is corrected below the minimum.

Every step is recorded in `attendance_leave_links` so it can be undone, and audited.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from core import audit, context
from core.errors import BadRequest
from modules.attendance import settings_store
from modules.attendance.models import AttendanceDay, LeaveLink

log = logging.getLogger("hrms.attendance")
ACTIVE = ("pending", "applied", "lop")
ZERO = Decimal("0")


def _leave():
    try:
        from modules.leave import ledger, policy, service  # type: ignore
        return service, policy, ledger
    except Exception:  # the leave module is not there
        return None


def config(db: Session, company_id: int, on: date | None = None) -> dict:
    return settings_store.get(db, company_id, "leave_link", on)


@contextmanager
def acting_as(employee_id: int):
    """Submit a request on the employee's behalf so it goes to their own manager."""
    ctx = context.current()
    saved = (ctx.employee_id, ctx.actor_type, ctx.actor_name)
    ctx.employee_id, ctx.actor_type = employee_id, "system"
    ctx.actor_name = "Attendance"
    try:
        yield
    finally:
        ctx.employee_id, ctx.actor_type, ctx.actor_name = saved


def active_links(db: Session, company_id: int, employee_ids: list[int], start: date, end: date,
                 kind: str) -> dict[tuple[int, date], LeaveLink]:
    rows = (db.query(LeaveLink).filter(LeaveLink.company_id == company_id, LeaveLink.employee_id.in_(employee_ids or [-1]),
                                       LeaveLink.kind == kind, LeaveLink.status.in_(ACTIVE),
                                       LeaveLink.date >= start, LeaveLink.date <= end).all())
    return {(r.employee_id, r.date): r for r in rows}


def link_out(l: LeaveLink, names: dict | None = None, types: dict | None = None) -> dict:
    return {"uuid": str(l.uuid), "employee_id": l.employee_id, "employee_name": (names or {}).get(l.employee_id),
            "date": l.date.isoformat(), "kind": l.kind, "status": l.status, "days": str(l.days), "unit": l.unit,
            "leave_type": (types or {}).get(l.leave_type_id), "note": l.note,
            "reversed_at": l.reversed_at.isoformat().replace("+00:00", "Z") if l.reversed_at else None,
            "reverse_reason": l.reverse_reason,
            "created_at": l.created_at.isoformat().replace("+00:00", "Z") if l.created_at else None}


def _event(db: Session, name: str, link: LeaveLink, extra: dict | None = None) -> None:
    audit.event(db, name, "attendance_leave_links", link.id,
                {"employee_id": link.employee_id, "date": link.date.isoformat(), "kind": link.kind,
                 "status": link.status, "days": float(link.days or 0), **(extra or {})},
                entity_uuid=str(link.uuid) if link.uuid else None,
                entity_label=f"{link.kind.replace('_', ' ')} {link.date:%d %b %Y}")


# ── 1. Absence to leave ─────────────────────────────────────────────

def still_absent(raw_status: str, raw_lop, link: LeaveLink) -> bool:
    """Whether a day worked out without the converted leave is still short by what the link
    covers (so the leave stays)."""
    return raw_status in ("absent", "half_day", "missing_punch") and Decimal(str(raw_lop)) >= Decimal(str(link.days))


def _employee(db: Session, company_id: int, employee_id: int):
    from modules.employees.models import Employee
    return db.query(Employee).filter(Employee.company_id == company_id, Employee.id == employee_id).first()


def _try_type(db: Session, company_id: int, emp, t, day: date, unit: str, reason: str):
    """Leave of type t for the day when it fits the balance; None otherwise."""
    lv = _leave()
    service = lv[0]
    data = {"from_date": day, "to_date": day, "unit": unit, "half": "second" if unit == "half_day" else None,
            "reason": reason}
    try:
        with db.begin_nested():
            ev = service.evaluate(db, company_id, emp, t, data, enforce=False)
            if ev["days"] <= 0:
                return None
            hard = [e for e in ev["errors"] if "available" in e or "already have" in e]
            if hard or (t.kind != "lop" and ev["lop_days"] > 0):
                return None
            return service.create_from_evaluation(db, company_id, emp, t, ev, data, on_behalf=True)
    except BadRequest:
        return None


def _types(db: Session, company_id: int, codes: list[str]) -> list:
    lv = _leave()
    if lv is None:
        return []
    policy = lv[1]
    out = []
    for code in codes or []:
        t = policy.type_by_code(db, company_id, str(code).upper())
        if t is not None and t.active and t.kind in ("standard", "comp_off"):
            out.append(t)
    return out


def _lop_type(db: Session, company_id: int):
    from modules.leave.models import LeaveType
    return (db.query(LeaveType).filter(LeaveType.company_id == company_id, LeaveType.kind == "lop",
                                       LeaveType.active.is_(True)).order_by(LeaveType.id).first())


def apply_absence(db: Session, link: LeaveLink, cfg: dict) -> LeaveLink:
    """Turn the absence into leave now (first type with balance), else loss of pay."""
    if _leave() is None:
        link.status = "lop"
        return link
    emp = _employee(db, link.company_id, link.employee_id)
    reason = f"Absent on {link.date:%d %b %Y}: recorded from attendance"
    req = None
    if cfg.get("action", "leave") == "leave":
        for t in _types(db, link.company_id, cfg.get("leave_type_codes") or []):
            req = _try_type(db, link.company_id, emp, t, link.date, link.unit, reason)
            if req is not None:
                link.status, link.leave_type_id, link.leave_request_id = "applied", t.id, req.id
                link.note = f"Taken from {t.name}"
                break
    if req is None:
        lop_t = _lop_type(db, link.company_id)
        req = _try_type(db, link.company_id, emp, lop_t, link.date, link.unit, reason) if lop_t is not None else None
        link.status = "lop"
        link.leave_type_id = lop_t.id if req is not None else None
        link.leave_request_id = req.id if req is not None else None
        link.note = "Loss of pay" + ("" if cfg.get("action") == "lop" else ": no leave balance")
    db.flush()
    _event(db, "attendance.absence_converted", link, {"note": link.note})
    return link


def create_absence_link(db: Session, company_id: int, row: AttendanceDay, cfg: dict) -> LeaveLink | None:
    days = Decimal("1") if row.status in ("absent", "missing_punch") else Decimal("0.5")
    if row.status == "half_day" and not cfg.get("half_days", True):
        return None
    link = LeaveLink(company_id=company_id, employee_id=row.employee_id, date=row.date, kind="absence",
                     status="pending", days=days, unit="day" if days == 1 else "half_day")
    db.add(link)
    db.flush()
    if cfg.get("needs_approval"):
        from core.workflow import service as workflow
        emp = _employee(db, company_id, row.employee_id)
        name = " ".join(x for x in (emp.first_name, emp.last_name) if x) if emp else "Employee"
        with acting_as(row.employee_id):
            inst = workflow.submit(
                db, company_id, "attendance.absence_leave",
                {"link_id": link.id, "employee_id": row.employee_id, "date": row.date.isoformat(),
                 "days": float(days)},
                f"Absence to leave: {name}, {row.date:%d %b %Y}", subject_type="attendance_leave_links",
                subject_id=link.id)
        link.workflow_instance_id = inst.id
        if link.status == "pending":
            link.note = "Waiting for approval"
        _event(db, "attendance.absence_conversion_asked", link)
        return link
    return apply_absence(db, link, cfg)


def _link_from_instance(db: Session, instance) -> LeaveLink | None:
    return db.query(LeaveLink).filter(LeaveLink.company_id == instance.company_id,
                                      LeaveLink.id == (instance.payload or {}).get("link_id")).first()


def on_absence_approved(db: Session, instance) -> None:
    link = _link_from_instance(db, instance)
    if link is None or link.status != "pending":
        return
    apply_absence(db, link, config(db, link.company_id, link.date)["absence"])
    from modules.attendance import service
    service.recompute(db, link.company_id, [link.employee_id], link.date, link.date, hooks=False)


def on_absence_closed(db: Session, instance) -> None:
    link = _link_from_instance(db, instance)
    if link is not None and link.status == "pending":
        link.status = "rejected" if instance.status == "rejected" else "reversed"
        link.note = "Not approved: stays loss of pay" if instance.status == "rejected" else "Withdrawn"
        _event(db, "attendance.absence_conversion_closed", link)


def reverse(db: Session, link: LeaveLink, reason: str) -> None:
    """Undo a link: cancel the leave day (balance back), take back the comp off or the
    penalty, or withdraw the pending approval."""
    lv = _leave()
    if link.kind == "absence":
        if link.leave_request_id and lv is not None:
            from modules.leave.models import LeaveRequest
            req = db.query(LeaveRequest).filter(LeaveRequest.id == link.leave_request_id).first()
            if req is not None and req.status in ("approved", "cancel_pending"):
                try:
                    with db.begin_nested():
                        req.status = "approved"
                        lv[0].cancel_days(db, req, [link.date.isoformat()], reason)
                except BadRequest:
                    pass
        _close_instance(db, link.workflow_instance_id, reason)
    elif link.kind == "late_penalty":
        if link.status == "applied" and lv is not None and link.leave_type_id:
            t = _type_by_id(db, link.leave_type_id)
            if t is not None:
                year = lv[1].year_bounds(lv[1].rules_on(t, link.date), link.date)[0]
                lv[2].add_entry(db, link.company_id, link.employee_id, t.id, year, link.date, "restored", link.days,
                                idem=f"latepen-undo:{link.id}", note=f"Late-mark penalty of {link.date:%d %b} undone",
                                meta={"source": "late_penalty", "link_id": link.id})
    elif link.kind == "comp_off":
        if link.comp_off_id and lv is not None:
            from modules.leave.models import CompOffRequest
            row = db.query(CompOffRequest).filter(CompOffRequest.id == link.comp_off_id).first()
            if row is not None:
                lv[0].withdraw_comp_off(db, row, reason)
    link.status = "reversed"
    link.reversed_at = datetime.now(timezone.utc)
    link.reverse_reason = reason[:300]
    db.flush()
    _event(db, "attendance.leave_link_reversed", link, {"reason": reason})


def _close_instance(db: Session, instance_id: int | None, reason: str) -> None:
    if not instance_id:
        return
    from core.workflow.models import WorkflowInstance
    inst = db.query(WorkflowInstance).filter(WorkflowInstance.id == instance_id).first()
    if inst is not None and inst.status == "pending":
        inst.status = "cancelled"
        inst.result_note = reason
        inst.completed_at = datetime.now(timezone.utc)
        for t in inst.tasks:
            if t.status == "pending":
                t.status = "cancelled"


def _type_by_id(db: Session, type_id: int):
    from modules.leave.models import LeaveType
    return db.query(LeaveType).filter(LeaveType.id == type_id).first()


# ── 2. Late-mark penalties from leave ───────────────────────────────

def _available(db: Session, company_id: int, employee_id: int, t, day: date) -> tuple[Decimal, int]:
    service, policy, ledger = _leave()
    year = policy.year_bounds(policy.rules_on(t, day), day)[0]
    bal = ledger.balance(db, company_id, employee_id, t, year) - ledger.booked(db, company_id, employee_id, t, year)
    return bal, year


def sync_late_penalties(db: Session, company_id: int, employee_id: int, days: list[AttendanceDay],
                        pen: dict[date, Decimal], cfg: dict) -> tuple[dict[date, Decimal], dict[date, str]]:
    """Take late-mark penalties from leave. Returns (loss of pay still to apply per day,
    {day: note}) after undoing links whose penalty went away."""
    if not days:
        return pen, {}
    start, end = days[0].date, days[-1].date
    links = active_links(db, company_id, [employee_id], start, end, "late_penalty")
    locked = {d.date for d in days if d.locked}
    out = dict(pen)
    notes: dict[date, str] = {}
    for (eid, d), link in links.items():
        if d in locked:
            continue
        if pen.get(d, ZERO) != Decimal(str(link.days)):
            reverse(db, link, "The late mark was corrected")
    links = active_links(db, company_id, [employee_id], start, end, "late_penalty")
    types = {}
    for d, amount in sorted(pen.items()):
        if d in locked or not amount:
            continue
        link = links.get((employee_id, d))
        if link is None and cfg.get("enabled") and _leave() is not None:
            link = LeaveLink(company_id=company_id, employee_id=employee_id, date=d, kind="late_penalty",
                             status="lop", days=amount, unit="day" if amount >= 1 else "half_day")
            db.add(link)
            db.flush()
            for t in _types(db, company_id, cfg.get("leave_type_codes") or []):
                bal, year = _available(db, company_id, employee_id, t, d)
                if bal >= amount:
                    _, _, ledger = _leave()
                    ledger.add_entry(db, company_id, employee_id, t.id, year, d, "availed", -amount,
                                     idem=f"latepen:{link.id}", note=f"Late-mark penalty on {d:%d %b %Y}",
                                     meta={"source": "late_penalty", "link_id": link.id})
                    link.status, link.leave_type_id, link.note = "applied", t.id, f"Taken from {t.name}"
                    break
            if link.status == "lop":
                link.note = "Loss of pay: no leave balance"
            _event(db, "attendance.late_penalty_taken", link, {"note": link.note})
        if link is not None and link.status == "applied":
            if link.leave_type_id not in types:
                t = _type_by_id(db, link.leave_type_id)
                types[link.leave_type_id] = t.name if t else "leave"
            out[d] = ZERO
            notes[d] = f"Late-mark penalty: {amount.normalize()} day taken from {types[link.leave_type_id]}"
    return out, notes


# ── 3. Comp off from attendance ─────────────────────────────────────

def _comp_off_due(row: AttendanceDay, cfg: dict) -> bool:
    if row.status not in ("weekly_off", "holiday"):
        return False
    if row.status == "weekly_off" and not cfg.get("on_weekly_off", True):
        return False
    if row.status == "holiday" and not cfg.get("on_holiday", True):
        return False
    return row.worked_minutes >= float(cfg.get("half_day_hours") or 4) * 60


def sync_comp_off(db: Session, company_id: int, rows: list[AttendanceDay], cfg: dict, today: date) -> int:
    lv = _leave()
    if lv is None or not rows:
        return 0
    eids = sorted({r.employee_id for r in rows})
    start, end = min(r.date for r in rows), max(r.date for r in rows)
    links = active_links(db, company_id, eids, start, end, "comp_off")
    done = 0
    for row in rows:
        if row.locked or row.date >= today:
            continue
        link = links.get((row.employee_id, row.date))
        due = _comp_off_due(row, cfg)
        if link is not None:
            if not due:
                reverse(db, link, "The work on this day off was corrected")
            continue
        if not due or not cfg.get("enabled"):
            continue
        hours = round(row.worked_minutes / 60, 2)
        if cfg.get("needs_approval"):
            with acting_as(row.employee_id):
                co = lv[0].credit_comp_off_from_attendance(
                    db, company_id, row.employee_id, row.date, hours,
                    full_day_hours=float(cfg.get("full_day_hours") or 6),
                    half_day_hours=float(cfg.get("half_day_hours") or 4), approved=False)
        else:
            co = lv[0].credit_comp_off_from_attendance(
                db, company_id, row.employee_id, row.date, hours,
                full_day_hours=float(cfg.get("full_day_hours") or 6),
                half_day_hours=float(cfg.get("half_day_hours") or 4), approved=True)
        if co is None:
            continue
        link = LeaveLink(company_id=company_id, employee_id=row.employee_id, date=row.date, kind="comp_off",
                         status="applied" if co.status == "approved" else "pending", days=co.days, unit=co.unit,
                         comp_off_id=co.id, workflow_instance_id=co.workflow_instance_id,
                         note=f"Comp off for {hours:g} h worked")
        db.add(link)
        db.flush()
        _event(db, "attendance.comp_off_credited", link, {"hours": hours})
        done += 1
    return done


# ── After a recompute, and the daily run ────────────────────────────

def after_recompute(db: Session, company_id: int, employee_ids: list[int], start: date, end: date) -> list[tuple[int, date]]:
    """Convert absences that are old enough and credit comp off for the days just worked
    out. Returns the (employee, day) pairs that need working out again."""
    cfg = config(db, company_id, end)
    ab, co = cfg.get("absence") or {}, cfg.get("comp_off") or {}
    from modules.attendance import service
    today = service.today(db, company_id)
    redo: list[tuple[int, date]] = []
    if ab.get("enabled"):
        last = min(end, today - timedelta(days=int(ab.get("after_days") or 0)))
        if last >= start:
            rows = (db.query(AttendanceDay).filter(
                AttendanceDay.company_id == company_id, AttendanceDay.employee_id.in_(employee_ids or [-1]),
                AttendanceDay.date >= start, AttendanceDay.date <= last, AttendanceDay.locked.is_(False),
                AttendanceDay.override_status.is_(None),
                AttendanceDay.status.in_(("absent", "half_day", "missing_punch"))).all())
            rows = [r for r in rows if r.status != "half_day" or r.lop_days >= Decimal("0.5")]
            if rows:
                taken = {(l.employee_id, l.date) for l in db.query(LeaveLink).filter(
                    LeaveLink.company_id == company_id, LeaveLink.kind == "absence",
                    LeaveLink.employee_id.in_({r.employee_id for r in rows}),
                    LeaveLink.date >= start, LeaveLink.date <= last,
                    LeaveLink.status.in_((*ACTIVE, "rejected"))).all()}
                for r in rows:
                    if (r.employee_id, r.date) in taken:
                        continue
                    link = create_absence_link(db, company_id, r, ab)
                    if link is not None and link.status in ("applied", "lop") and link.leave_request_id:
                        redo.append((r.employee_id, r.date))
    rows = (db.query(AttendanceDay).filter(
        AttendanceDay.company_id == company_id, AttendanceDay.employee_id.in_(employee_ids or [-1]),
        AttendanceDay.date >= start, AttendanceDay.date <= end,
        AttendanceDay.status.in_(("weekly_off", "holiday"))).all())
    if co.get("enabled") or rows:
        sync_comp_off(db, company_id, rows, co, today)
    return redo


_LAST_RUN: dict[int, date] = {}


def daily_job(db: Session) -> int:
    """Once a day per company: convert absences that are now old enough (they were too
    recent when the day was worked out) and credit comp off."""
    from modules.attendance import service
    from modules.company.models import Company
    from modules.employees.models import Employee
    done = 0
    for (cid,) in db.query(Company.id).filter(Company.active.is_(True)).all():
        td = service.today(db, cid)
        if _LAST_RUN.get(cid) == td:
            continue
        db.info["company_id"] = cid
        cfg = config(db, cid, td)
        if (cfg.get("absence") or {}).get("enabled") or (cfg.get("comp_off") or {}).get("enabled"):
            ids = [i for (i,) in db.query(Employee.id).filter(Employee.company_id == cid, Employee.active.is_(True)).all()]
            try:
                with db.begin_nested():
                    redo = after_recompute(db, cid, ids, td - timedelta(days=35), td - timedelta(days=1))
                    by_emp = defaultdict(list)
                    for eid, d in redo:
                        by_emp[eid].append(d)
                    for eid, ds in by_emp.items():
                        service.recompute(db, cid, [eid], min(ds), max(ds), hooks=False)
                    done += len(redo)
            except Exception as exc:  # never stop the other companies
                log.warning("attendance-leave run failed for company %s: %s", cid, exc)
        _LAST_RUN[cid] = td
    db.info.pop("company_id", None)
    return done


def list_links(db: Session, company_id: int, start: date, end: date, kind: str | None = None,
               employee_ids: list[int] | None = None) -> list[dict]:
    q = db.query(LeaveLink).filter(LeaveLink.company_id == company_id, LeaveLink.date >= start, LeaveLink.date <= end)
    if kind:
        q = q.filter(LeaveLink.kind == kind)
    if employee_ids is not None:
        q = q.filter(LeaveLink.employee_id.in_(employee_ids or [-1]))
    rows = q.order_by(LeaveLink.date.desc(), LeaveLink.id.desc()).limit(1000).all()
    from modules.attendance.service import names_for
    names = names_for(db, company_id, [r.employee_id for r in rows])
    types = {}
    if rows and _leave() is not None:
        from modules.leave.models import LeaveType
        types = {t.id: t.name for t in db.query(LeaveType).filter(LeaveType.company_id == company_id).all()}
    return [link_out(r, names, types) for r in rows]
