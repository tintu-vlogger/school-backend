"""Reminders before leave lapses: comp off and carried-forward leave that will expire in
the coming days (leave setting `expiry_reminders.days_before`, e.g. 30 and 7 days) are
emailed to the employee and shown in their notifications, once per credit and step.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from modules.leave import ledger, policy
from modules.leave.models import LeaveExpiryReminder, LeaveLedgerEntry


def due(db: Session, company_id: int, today: date) -> list[dict]:
    """Credits expiring within the largest reminder step that still have days left."""
    s = policy.get_settings(db, company_id)["expiry_reminders"]
    steps = sorted({int(x) for x in s.get("days_before") or []}, reverse=True)
    if not s.get("enabled") or not steps:
        return []
    horizon = today + timedelta(days=steps[0])
    rows = (db.query(LeaveLedgerEntry).filter(LeaveLedgerEntry.company_id == company_id,
                                              LeaveLedgerEntry.kind.in_(ledger.EXPIRING_KINDS),
                                              LeaveLedgerEntry.expires_on >= today,
                                              LeaveLedgerEntry.expires_on <= horizon,
                                              LeaveLedgerEntry.days > 0).all())
    types = {t.id: t for t in policy.list_types(db, company_id)}
    seen, out = set(), []
    for c in rows:
        t = types.get(c.leave_type_id)
        if t is None or (c.employee_id, t.id, c.year) in seen:
            continue
        seen.add((c.employee_id, t.id, c.year))
        entries = ledger._entries(db, company_id, c.employee_id, t, c.year)
        # what would expire if nothing more is taken before the horizon
        for credit, remaining in ledger.expiry_plan(entries, horizon + timedelta(days=1)):
            if credit.expires_on < today or remaining <= 0:
                continue
            left = (credit.expires_on - today).days
            step = min((x for x in steps if x >= left), default=None)
            if step is None:
                continue
            out.append({"entry": credit, "type": t, "remaining": remaining, "days_left": left, "step": step})
    return out


def send_due(db: Session, company_id: int, today: date) -> int:
    from core.email import send_email
    from modules.employees.models import Employee
    items = due(db, company_id, today)
    if not items:
        return 0
    done = {(r.ledger_entry_id, r.days_before) for r in db.query(LeaveExpiryReminder).filter(
        LeaveExpiryReminder.company_id == company_id,
        LeaveExpiryReminder.ledger_entry_id.in_([i["entry"].id for i in items])).all()}
    by_emp = defaultdict(list)
    for i in items:
        if (i["entry"].id, i["step"]) not in done:
            by_emp[i["entry"].employee_id].append(i)
    emps = {e.id: e for e in db.query(Employee).filter(Employee.company_id == company_id,
                                                        Employee.id.in_(list(by_emp) or [-1])).all()}
    sent = 0
    for eid, mine in by_emp.items():
        e = emps.get(eid)
        if e is None or not e.active:
            continue
        lines = [f"- {_fmt(i['remaining'])} day(s) of {i['type'].name} lapse on {i['entry'].expires_on:%d %b %Y}"
                 for i in sorted(mine, key=lambda x: x["entry"].expires_on)]
        subject = "Leave about to lapse"
        body = (f"Hello {e.first_name},\n\nSome of your leave will lapse soon if you don't use it:\n\n"
                + "\n".join(lines) + "\n\nApply for it from My leave before those dates.\n")
        emailed = bool(getattr(e, "email", None)) and send_email(e.email, subject, body)
        try:
            from modules.notifications.service import notify
            notify(db, company_id, [eid], kind="leave.expiry", title=subject, body="\n".join(lines),
                   link="/dashboard/leave", group_key="leave.expiry")
        except Exception:
            pass
        for i in mine:
            db.add(LeaveExpiryReminder(company_id=company_id, employee_id=eid, ledger_entry_id=i["entry"].id,
                                       days_before=i["step"], expiring_days=i["remaining"],
                                       expires_on=i["entry"].expires_on, emailed=emailed))
        sent += 1
    db.flush()
    return sent


def _fmt(n) -> str:
    n = Decimal(str(n))
    return str(int(n)) if n == n.to_integral_value() else f"{n.normalize()}"
