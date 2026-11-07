"""Leave reports, and the figures payroll reads.

balance_report(year)          every employee x leave type: opening, earned, taken, encashed, lapsed, balance
taken_report(from, to, by)    approved days by leave type and department
lop_summary(period)           loss-of-pay days per employee for a pay period (payroll input)
absentees(day)                who is away on a day, and why
encashments(from, to)         days encashed (year end / exit) waiting for payroll
"""
from __future__ import annotations

import io
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session, joinedload

from modules.leave import ledger, policy
from modules.leave.ledger import D, ZERO
from modules.leave.models import LeaveLedgerEntry, LeaveRequest, LeaveType
from modules.leave.views import f2


def _employee_model():
    from modules.employees.models import Employee
    return Employee


def _emp_cols(e) -> dict:
    return {"employee_uuid": str(e.uuid), "employee_code": e.employee_code or "",
            "employee_name": f"{e.first_name} {e.last_name}".strip(),
            "department": e.department.department_name if getattr(e, "department", None) else ""}


def month_bounds(period: str) -> tuple[date, date]:
    y, m = (int(x) for x in period.split("-")[:2])
    start = date(y, m, 1)
    return start, date(y + m // 12, m % 12 + 1, 1) - timedelta(days=1)


def balance_report(db: Session, company_id: int, year: int | None, ids: set | None = None) -> dict:
    from core.timeutil import today_for_company_id
    Employee = _employee_model()
    today = today_for_company_id(db, company_id)
    types = [t for t in policy.list_types(db, company_id) if t.kind != "lop"]
    year_of = {t.id: year if year is not None else policy.year_bounds(policy.rules_on(t, today), today)[0] for t in types}
    eq = db.query(Employee).options(joinedload(Employee.department)).filter(Employee.company_id == company_id,
                                                                             Employee.active.is_(True))
    if ids is not None:
        eq = eq.filter(Employee.id.in_(ids or {-1}))
    emps = eq.order_by(Employee.first_name).all()
    totals = ledger.kind_totals(db, company_id, [e.id for e in emps], types, year_of)
    rows = []
    for e in emps:
        for t in types:
            k = totals.get((e.id, t.id))
            if not k:
                continue
            g = lambda *n: sum((D(k.get(x)) for x in n), ZERO)  # noqa: E731
            rows.append({**_emp_cols(e), "leave_type": t.code, "leave_type_name": t.name, "year": year_of[t.id],
                         "opening": f2(g("opening", "carry_forward")), "earned": f2(g("accrual", "grant", "comp_off")),
                         "taken": f2(-g("availed", "restored")), "adjusted": f2(g("adjustment")),
                         "encashed": f2(-g("encashed")), "lapsed": f2(-g("lapsed", "expired", "carried_out")),
                         "balance": f2(sum((D(v) for v in k.values()), ZERO))})
    return {"rows": rows, "types": [{"code": t.code, "name": t.name, "color": t.color} for t in types]}


def taken_report(db: Session, company_id: int, start: date, end: date, ids: set | None = None) -> dict:
    """Approved leave days between two dates, by type and by department."""
    Employee = _employee_model()
    q = (db.query(LeaveRequest).options(joinedload(LeaveRequest.leave_type),
                                        joinedload(LeaveRequest.employee).joinedload(Employee.department))
         .filter(LeaveRequest.company_id == company_id, LeaveRequest.status.in_(("approved", "cancel_pending")),
                 LeaveRequest.from_date <= end, LeaveRequest.to_date >= start))
    if ids is not None:
        q = q.filter(LeaveRequest.employee_id.in_(ids or {-1}))
    by_type = defaultdict(lambda: ZERO)
    by_dept = defaultdict(lambda: defaultdict(lambda: ZERO))
    people = defaultdict(set)
    meta = {}
    for r in q.all():
        dept = r.employee.department.department_name if r.employee.department else "No department"
        meta[r.leave_type.code] = {"code": r.leave_type.code, "name": r.leave_type.name, "color": r.leave_type.color}
        for it in r.breakdown or []:
            d = date.fromisoformat(it["date"])
            if it.get("cancelled") or not (start <= d <= end):
                continue
            by_type[r.leave_type.code] += D(it["amount"])
            by_dept[dept][r.leave_type.code] += D(it["amount"])
            people[r.leave_type.code].add(r.employee_id)
    return {
        "from": start.isoformat(), "to": end.isoformat(),
        "types": sorted(meta.values(), key=lambda x: -by_type[x["code"]]),
        "by_type": [{**meta[c], "days": f2(v), "people": len(people[c])} for c, v in sorted(by_type.items(), key=lambda x: -x[1])],
        "by_department": [{"department": d, "total": f2(sum(v.values(), ZERO)), "by_type": {c: f2(x) for c, x in v.items()}}
                          for d, v in sorted(by_dept.items(), key=lambda x: -sum(x[1].values(), ZERO))],
        "total": f2(sum(by_type.values(), ZERO)),
    }


def lop_summary(db: Session, company_id: int, start: date, end: date, ids: set | None = None) -> dict:
    """Loss-of-pay days per employee in a pay period: unpaid leave types, the part of
    paid leave that went beyond the balance, and the pay not kept on reduced-pay leave
    (half-pay sick leave counts half a day per day; shown as reduced_pay_days too).
    Pending requests are listed separately so payroll can chase them before locking."""
    Employee = _employee_model()
    q = (db.query(LeaveRequest).options(joinedload(LeaveRequest.leave_type),
                                        joinedload(LeaveRequest.employee).joinedload(Employee.department))
         .filter(LeaveRequest.company_id == company_id,
                 LeaveRequest.status.in_(("approved", "cancel_pending", "pending")),
                 LeaveRequest.from_date <= end, LeaveRequest.to_date >= start))
    if ids is not None:
        q = q.filter(LeaveRequest.employee_id.in_(ids or {-1}))
    per = {}
    for r in q.all():
        unpaid = r.leave_type.kind == "lop" or policy.rules_on(r.leave_type, r.from_date)["paid"] != "paid"
        for it in r.breakdown or []:
            d = date.fromisoformat(it["date"])
            if it.get("cancelled") or not (start <= d <= end):
                continue
            lop = D(it["amount"]) if unpaid else D(it.get("lop"))
            cut = ZERO
            if not unpaid:
                pct = D(policy.rules_on(r.leave_type, d).get("pay_percent", 100))
                cut = (D(it["amount"]) - D(it.get("lop"))) * (Decimal("100") - pct) / Decimal("100")
            if lop + cut <= 0:
                continue
            row = per.setdefault(r.employee_id, {**_emp_cols(r.employee), "employee_id": r.employee_id,
                                                 "lop_days": ZERO, "pending_days": ZERO, "reduced_pay_days": ZERO,
                                                 "dates": []})
            if r.status == "pending":
                row["pending_days"] += lop + cut
            else:
                row["lop_days"] += lop + cut
                row["reduced_pay_days"] += cut
            row["dates"].append({"date": it["date"], "days": float(lop + cut), "leave_type": r.leave_type.code,
                                 "status": r.status, "reference_no": r.reference_no})
    rows = sorted(per.values(), key=lambda x: x["employee_name"])
    for r in rows:
        r["lop_days"], r["pending_days"] = f2(r["lop_days"]), f2(r["pending_days"])
        r["reduced_pay_days"] = f2(r["reduced_pay_days"])
        r["dates"].sort(key=lambda x: x["date"])
    return {"from": start.isoformat(), "to": end.isoformat(), "rows": rows,
            "total": f2(sum((D(r["lop_days"]) for r in rows), ZERO)),
            "pending": f2(sum((D(r["pending_days"]) for r in rows), ZERO))}


def lop_days(db: Session, company_id: int, employee_id: int, start: date, end: date) -> float:
    """Approved loss-of-pay days of one employee in a period (for payroll)."""
    s = lop_summary(db, company_id, start, end, ids={employee_id})
    return s["rows"][0]["lop_days"] if s["rows"] else 0.0


def absentees(db: Session, company_id: int, day: date, ids: set | None = None) -> dict:
    Employee = _employee_model()
    q = (db.query(LeaveRequest).options(joinedload(LeaveRequest.leave_type),
                                        joinedload(LeaveRequest.employee).joinedload(Employee.department))
         .filter(LeaveRequest.company_id == company_id, LeaveRequest.status.in_(("approved", "pending", "cancel_pending")),
                 LeaveRequest.from_date <= day, LeaveRequest.to_date >= day))
    if ids is not None:
        q = q.filter(LeaveRequest.employee_id.in_(ids or {-1}))
    rows = []
    for r in q.all():
        it = next((x for x in r.breakdown or [] if x["date"] == day.isoformat() and not x.get("cancelled")), None)
        if it is None or D(it["amount"]) <= 0:
            continue
        rows.append({**_emp_cols(r.employee), "leave_type": r.leave_type.code, "leave_type_name": r.leave_type.name,
                     "color": r.leave_type.color, "amount": float(it["amount"]), "part": it.get("part"),
                     "status": r.status, "from_date": r.from_date.isoformat(), "to_date": r.to_date.isoformat(),
                     "request_uuid": str(r.uuid)})
    rows.sort(key=lambda x: (x["status"] == "pending", x["employee_name"]))
    return {"date": day.isoformat(), "rows": rows, "on_leave": sum(1 for r in rows if r["status"] != "pending"),
            "pending": sum(1 for r in rows if r["status"] == "pending")}


def encashments(db: Session, company_id: int, start: date, end: date) -> list[dict]:
    """Encashed days (year end and exit) in a period, for payroll to pay."""
    Employee = _employee_model()
    rows = (db.query(LeaveLedgerEntry, LeaveType, Employee)
            .join(LeaveType, LeaveType.id == LeaveLedgerEntry.leave_type_id)
            .join(Employee, Employee.id == LeaveLedgerEntry.employee_id)
            .filter(LeaveLedgerEntry.company_id == company_id, LeaveLedgerEntry.kind == "encashed",
                    LeaveLedgerEntry.entry_date >= start, LeaveLedgerEntry.entry_date <= end).all())
    return [{"entry_uuid": str(e.uuid), "employee_id": emp.id, "employee_name": f"{emp.first_name} {emp.last_name}".strip(),
             "employee_code": emp.employee_code, "leave_type": t.code, "days": f2(-D(e.days)), "date": e.entry_date.isoformat(),
             "reason": (e.meta or {}).get("reason"), "payroll_status": (e.meta or {}).get("payroll_status", "pending")}
            for e, t, emp in rows]


def to_csv(rows: list[dict], columns: list[tuple[str, str]]) -> str:
    from core import csv_safe
    buf = io.StringIO()
    w = csv_safe.writer(buf)
    w.writerow([h for _, h in columns])
    for r in rows:
        w.writerow([r.get(k) for k, _ in columns])
    return buf.getvalue()
