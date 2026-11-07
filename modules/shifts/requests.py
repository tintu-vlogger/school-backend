"""Shift change and shift swap requests, approved through the workflow engine.

shifts.change  an employee asks for another shift on some dates
shifts.swap    two colleagues swap their shifts on a day; the colleague accepts first
               (first approval step), then the manager approves

On approval the roster is updated (published straight away) after the rest rules and
the women's night-shift consent rule are checked again.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy.orm import Session

from core.errors import BadRequest
from modules.attendance import service as att
from modules.shifts import adapters
from modules.shifts import service as shifts_service


def _plan(db, company_id, emp_id, d):
    return shifts_service.day_plans(db, company_id, [emp_id], d, d, include_drafts=True)[(emp_id, d)]


def create_change(db: Session, company_id: int, employee, data: dict):
    from core.workflow import service as workflow
    from modules.roster import service as roster
    s, e = data.get("start_date"), data.get("end_date") or data.get("start_date")
    if not s or e < s:
        raise BadRequest("Choose the dates.")
    if (e - s).days > 31:
        raise BadRequest("Ask for up to 31 days at a time.")
    if s < att.today(db, company_id):
        raise BadRequest("Shift changes are for today or later.")
    shift = shifts_service.shift_by_id(db, company_id, data.get("shift_id"))
    if shift is None or not shift.active:
        raise BadRequest("Choose a shift.")
    reason = (data.get("reason") or "").strip()
    if not reason:
        raise BadRequest("Give a reason.")
    days = list(adapters.daterange(s, e))
    problems = roster.check(db, company_id, employee, {d: ("working", shift) for d in days})
    blocks = [p for p in problems if p["action"] == "block"]
    if blocks:
        raise BadRequest(blocks[0]["message"])
    payload = {"employee_id": employee.id, "start_date": s.isoformat(), "end_date": e.isoformat(),
               "shift_id": shift.id, "shift_name": shift.name, "reason": reason,
               "current": [{"date": d.isoformat(), "shift": (_plan(db, company_id, employee.id, d).shift or shift).name
                            if _plan(db, company_id, employee.id, d).day_type == "working" else "Off"}
                           for d in days[:7]],
               "warnings": [p["message"] for p in problems if p["action"] == "warn"]}
    title = f"Shift change: {att.name_of(employee)}, {s:%d %b}" + (f" to {e:%d %b}" if e != s else "") + f" to {shift.name}"
    return workflow.submit(db, company_id, "shifts.change", payload, title, subject_type="employees",
                           subject_id=employee.id)


def create_swap(db: Session, company_id: int, employee, data: dict):
    from core.workflow import service as workflow
    from modules.roster import service as roster
    d = data.get("date")
    if not isinstance(d, date):
        raise BadRequest("Choose the date.")
    if d < att.today(db, company_id):
        raise BadRequest("Swaps are for today or later.")
    other_id = int(data.get("counterpart_employee_id") or 0)
    if other_id == employee.id or not other_id:
        raise BadRequest("Choose a colleague to swap with.")
    other = att.employee(db, company_id, other_id)
    if not other.active:
        raise BadRequest("That colleague is not active.")
    mine, theirs = _plan(db, company_id, employee.id, d), _plan(db, company_id, other.id, d)
    if mine.day_type != "working" or not mine.shift:
        raise BadRequest("You have no shift that day to swap.")
    if theirs.day_type != "working" or not theirs.shift:
        raise BadRequest(f"{att.name_of(other)} has no shift that day.")
    if mine.shift.id == theirs.shift.id:
        raise BadRequest("You are both on the same shift that day.")
    for who, shift in ((employee, theirs.shift), (other, mine.shift)):
        blocks = [p for p in roster.check(db, company_id, who, {d: ("working", shift)}) if p["action"] == "block"]
        if blocks:
            raise BadRequest(f"{att.name_of(who)}: {blocks[0]['message']}")
    payload = {"employee_id": employee.id, "counterpart_employee_id": other.id,
               "counterpart_name": att.name_of(other), "date": d.isoformat(),
               "my_shift_id": mine.shift.id, "my_shift": mine.shift.name,
               "their_shift_id": theirs.shift.id, "their_shift": theirs.shift.name,
               "reason": (data.get("reason") or "").strip() or None}
    title = f"Shift swap: {att.name_of(employee)} and {att.name_of(other)}, {d:%d %b}"
    return workflow.submit(db, company_id, "shifts.swap", payload, title, subject_type="employees",
                           subject_id=employee.id)


def on_change_approved(db: Session, instance) -> None:
    from modules.roster import service as roster
    p = instance.payload or {}
    s, e = date.fromisoformat(p["start_date"]), date.fromisoformat(p["end_date"])
    r = roster.assign(db, instance.company_id, {"employee_ids": [p["employee_id"]], "start_date": s, "end_date": e,
                                                "shift_id": p["shift_id"], "note": "Shift change request"},
                      source="change_request", publish=True)
    if r["blocked"]:
        raise BadRequest(f"Can't approve: {r['blocked'][0]['message']}")


def on_swap_approved(db: Session, instance) -> None:
    from modules.roster import service as roster
    p = instance.payload or {}
    d = date.fromisoformat(p["date"])
    for emp_id, shift_id in ((p["employee_id"], p["their_shift_id"]), (p["counterpart_employee_id"], p["my_shift_id"])):
        r = roster.assign(db, instance.company_id, {"employee_ids": [emp_id], "dates": [d], "shift_id": shift_id,
                                                    "note": "Shift swap"}, source="swap", publish=True)
        if r["blocked"]:
            raise BadRequest(f"Can't approve: {r['blocked'][0]['message']}")


def summarize_change(instance) -> list[str]:
    p = instance.payload or {}
    lines = [f"New shift: {p.get('shift_name')}", f"Dates: {p.get('start_date')}"
             + (f" to {p.get('end_date')}" if p.get("end_date") != p.get("start_date") else "")]
    if p.get("current"):
        lines.append("Now: " + ", ".join(f"{c['date'][5:]} {c['shift']}" for c in p["current"]))
    for w in p.get("warnings") or []:
        lines.append(f"Note: {w}")
    if p.get("reason"):
        lines.append(f"Reason: {p['reason']}")
    return lines


def summarize_swap(instance) -> list[str]:
    p = instance.payload or {}
    lines = [f"Date: {p.get('date')}",
             f"{instance.requester_name}: {p.get('my_shift')} → {p.get('their_shift')}",
             f"{p.get('counterpart_name')}: {p.get('their_shift')} → {p.get('my_shift')}"]
    if p.get("reason"):
        lines.append(f"Reason: {p['reason']}")
    return lines
