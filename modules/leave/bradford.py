"""Bradford factor: B = S x S x D, where S is the number of spells (separate stretches) of
unplanned absence and D the total days, over a rolling window (52 weeks by default).

Counted: approved leave of the types listed in the `bradford` leave setting (sick and
casual leave by default) and, when `include_absent` is on, days the attendance module
marked absent. Days next to each other, or separated only by weekly offs and holidays,
are one spell. Levels (e.g. 50 keep an eye, 125 return-to-work talk ...) are set by HR;
the daily job alerts HR and the manager once each time someone reaches a higher level.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session, joinedload

from core import audit
from modules.leave import policy
from modules.leave.calendar import WorkCalendar
from modules.leave.ledger import D
from modules.leave.models import BradfordAlert, LeaveRequest


def score(spells: int, days) -> int:
    return int(round(spells * spells * float(days)))


def level_for(value: int, levels: list[dict]) -> tuple[int, str | None]:
    """(1-based level reached, its label); (0, None) below the first level."""
    reached, label = 0, None
    for i, lv in enumerate(sorted(levels, key=lambda x: x["score"]), 1):
        if value >= int(lv["score"]):
            reached, label = i, lv["label"]
    return reached, label


def spells_of(days: list[date], is_working) -> list[list[date]]:
    """Group absence dates into spells: a new spell starts when a working day that was not
    an absence lies between two absence dates."""
    out: list[list[date]] = []
    for d in sorted(set(days)):
        if out:
            prev = out[-1][-1]
            gap = prev + timedelta(days=1)
            joined = True
            while gap < d:
                if is_working(gap):
                    joined = False
                    break
                gap += timedelta(days=1)
            if joined:
                out[-1].append(d)
                continue
        out.append([d])
    return out


def _absences(db: Session, company_id: int, start: date, end: date, codes: list[str], include_absent: bool,
              ids: set | None) -> dict[int, dict[date, Decimal]]:
    per: dict[int, dict[date, Decimal]] = defaultdict(dict)
    q = (db.query(LeaveRequest).options(joinedload(LeaveRequest.leave_type))
         .filter(LeaveRequest.company_id == company_id, LeaveRequest.status.in_(("approved", "cancel_pending")),
                 LeaveRequest.from_date <= end, LeaveRequest.to_date >= start))
    if ids is not None:
        q = q.filter(LeaveRequest.employee_id.in_(ids or {-1}))
    wanted = {c.upper() for c in codes}
    for r in q.all():
        if (r.leave_type.code or "").upper() not in wanted:
            continue
        for it in r.breakdown or []:
            d = date.fromisoformat(it["date"])
            if it.get("cancelled") or it.get("sandwich") or not (start <= d <= end) or D(it.get("amount")) <= 0:
                continue
            per[r.employee_id][d] = min(Decimal("1"), per[r.employee_id].get(d, Decimal("0")) + D(it["amount"]))
    if include_absent:
        try:
            from modules.attendance.models import AttendanceDay  # type: ignore
        except Exception:
            return per
        aq = db.query(AttendanceDay.employee_id, AttendanceDay.date, AttendanceDay.status).filter(
            AttendanceDay.company_id == company_id, AttendanceDay.date >= start, AttendanceDay.date <= end,
            AttendanceDay.status.in_(("absent", "half_day")))
        if ids is not None:
            aq = aq.filter(AttendanceDay.employee_id.in_(ids or {-1}))
        for eid, d, status in aq.all():
            if d not in per[eid]:
                per[eid][d] = Decimal("1") if status == "absent" else Decimal("0.5")
    return per


def report(db: Session, company_id: int, as_of: date, ids: set | None = None) -> dict:
    s = policy.get_settings(db, company_id)["bradford"]
    weeks = int(s.get("window_weeks") or 52)
    start = as_of - timedelta(days=weeks * 7 - 1)
    per = _absences(db, company_id, start, as_of, s.get("type_codes") or [], bool(s.get("include_absent")), ids)
    from modules.employees.models import Employee
    emps = {e.id: e for e in db.query(Employee).options(joinedload(Employee.department))
            .filter(Employee.company_id == company_id, Employee.id.in_(list(per) or [-1])).all()}
    cal = WorkCalendar(db, company_id)
    rows = []
    for eid, days in per.items():
        e = emps.get(eid)
        if e is None or not days:
            continue
        sp = spells_of(list(days), lambda d, _e=e: cal.day(_e, d)["kind"] in ("working", "half"))
        total = sum(days.values(), Decimal("0"))
        value = score(len(sp), total)
        lvl, label = level_for(value, s.get("levels") or [])
        rows.append({"employee_id": eid, "employee_uuid": str(e.uuid), "employee_code": e.employee_code or "",
                     "employee_name": f"{e.first_name} {e.last_name}".strip(),
                     "department": e.department.department_name if getattr(e, "department", None) else "",
                     "spells": len(sp), "days": float(total), "score": value, "level": lvl, "label": label,
                     "spell_list": [{"from": x[0].isoformat(), "to": x[-1].isoformat(),
                                     "days": float(sum((days[d] for d in x), Decimal("0")))} for x in sp]})
    rows.sort(key=lambda r: (-r["score"], r["employee_name"]))
    return {"as_of": as_of.isoformat(), "from": start.isoformat(), "window_weeks": weeks,
            "levels": s.get("levels") or [], "type_codes": s.get("type_codes") or [],
            "include_absent": bool(s.get("include_absent")), "rows": rows}


def employee_score(db: Session, company_id: int, employee_id: int, as_of: date) -> dict | None:
    rows = report(db, company_id, as_of, {employee_id})["rows"]
    return rows[0] if rows else None


def run_alerts(db: Session, company_id: int, as_of: date) -> int:
    """Alert HR (the owner's inbox) and the manager once per level reached within the window."""
    s = policy.get_settings(db, company_id)["bradford"]
    if not s.get("enabled") or not s.get("alert"):
        return 0
    res = report(db, company_id, as_of)
    window_start = date.fromisoformat(res["from"])
    last: dict[int, int] = {}
    for a in (db.query(BradfordAlert).filter(BradfordAlert.company_id == company_id,
                                             BradfordAlert.as_of >= window_start).all()):
        last[a.employee_id] = max(last.get(a.employee_id, 0), a.level)
    from modules.employees.models import Employee
    sent = 0
    for r in res["rows"]:
        if r["level"] <= last.get(r["employee_id"], 0):
            continue
        a = BradfordAlert(company_id=company_id, employee_id=r["employee_id"], level=r["level"], label=r["label"],
                          score=r["score"], spells=r["spells"], days=Decimal(str(r["days"])), as_of=as_of)
        db.add(a)
        db.flush()
        audit.event(db, "leave.bradford_alert", "leave_bradford_alerts", a.id,
                    {"employee_id": r["employee_id"], "score": r["score"], "level": r["label"]},
                    entity_uuid=str(a.uuid), entity_label=r["employee_name"])
        recipients: list = [None]
        if s.get("alert_manager"):
            mgr = db.query(Employee.reporting_manager_id).filter(Employee.id == r["employee_id"]).scalar()
            if mgr:
                recipients.append(mgr)
        try:
            from modules.notifications.service import notify
            notify(db, company_id, recipients, kind="leave.bradford",
                   title=f"{r['employee_name']}: absence score {r['score']} ({r['label']})",
                   body=f"{r['spells']} spells, {r['days']:g} days in the last {res['window_weeks']} weeks.",
                   link="/dashboard/leave/reports?tab=bradford")
        except Exception:
            pass
        sent += 1
    db.flush()
    return sent
