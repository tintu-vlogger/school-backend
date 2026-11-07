"""Compliance calendar and challan tracking.

For every month the calendar lists what is due, per legal entity and state: PF (ECR and
payment), ESI, TDS deposit, Professional Tax and LWF by state, salary payment (Code on
Wages), the quarterly TDS return and the annual tax certificate. Due dates come from the
statutory library (due_dates, and the PT/LWF rules of each state). Amounts come from locked
pay runs. Paying or filing is recorded on a challan row with its reference.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from sqlalchemy.orm import Session

from core import audit
from core.errors import BadRequest, NotFound
from core.timeutil import today_for_company_id
from modules.payroll import calc, statutory
from modules.payroll.calc import ZERO, d, money
from modules.payroll.models import PayRun, Payslip, StatutoryChallan

LABELS = {"pf": "PF payment and ECR", "esi": "ESI contribution", "tds": "TDS deposit", "pt": "Professional Tax",
          "lwf": "Labour Welfare Fund", "salary": "Salary payment", "tds_return": "Quarterly TDS return",
          "certificate": "Annual tax certificates"}


def _day(y: int, m: int, day: int) -> date:
    return date(y, m, min(int(day), calc.month_end(date(y, m, 1)).day))


def _month_amounts(db: Session, company_id: int, month: date) -> dict:
    """Totals from approved/locked/paid runs whose period starts in `month`."""
    slips = (db.query(Payslip, PayRun.status, PayRun.paid_at).join(PayRun, PayRun.id == Payslip.pay_run_id)
             .filter(Payslip.company_id == company_id, Payslip.period_start >= month,
                     Payslip.period_start <= calc.month_end(month), PayRun.status.in_(("approved", "locked", "paid")),
                     Payslip.status.in_(("calculated", "held"))).all())
    out = defaultdict(lambda: ZERO)
    states_pt, states_lwf = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO)
    paid = all(st == "paid" for _, st, _ in slips) if slips else False
    names = {}
    for p, _st, _paid in slips:
        ent = ((p.snapshot or {}).get("entity") or {}).get("id") or 0
        names[ent] = ((p.snapshot or {}).get("entity") or {}).get("name")
        for ln in p.lines or []:
            a = d(ln["amount"])
            if ln["code"] in ("PF_EE", "VPF", "PF_ER", "EPS_ER", "EDLI", "PF_ADMIN"):
                out[("pf", ent)] += a
            elif ln["code"] in ("ESI_EE", "ESI_ER"):
                out[("esi", ent)] += a
            elif ln["code"] == "TDS":
                out[("tds", ent)] += a
            elif ln["code"] == "PT":
                states_pt[(ent, ((p.statutory or {}).get("pt") or {}).get("state") or "")] += a
            elif ln["code"] in ("LWF_EE", "LWF_ER"):
                states_lwf[(ent, ((p.statutory or {}).get("lwf") or {}).get("state") or "")] += a
        out[("salary", ent)] += d(p.net)
    return {"totals": out, "pt": states_pt, "lwf": states_lwf, "salary_paid": paid, "has_runs": bool(slips), "names": names}


def items_for_month(db: Session, company_id: int, month: date) -> list[dict]:
    month = month.replace(day=1)
    book = statutory.RuleBook(db, company_id, calc.month_end(month))
    due = book.get("due_dates") or {}
    nxt = calc.add_months(month, 1)
    period = month.strftime("%Y-%m")
    am = _month_amounts(db, company_id, month)
    items = []
    entities = sorted({k[1] for k in am["totals"]} | {k[0] for k in am["pt"]} | {k[0] for k in am["lwf"]}) or [0]
    for ent in entities:
        for kind in ("pf", "esi", "tds", "salary"):
            amt = am["totals"].get((kind, ent), ZERO)
            if kind in ("pf", "esi") and amt == 0 and am["has_runs"]:
                continue
            rule = due.get(kind) or {}
            day = rule.get("day", 15)
            if kind == "tds" and month.month == 3:
                due_on = _day(nxt.year, nxt.month, rule.get("march_day", 30))
            else:
                due_on = _day(nxt.year, nxt.month, day)
            items.append({"kind": kind, "label": rule.get("label") or LABELS[kind], "legal_entity_id": ent, "state": "",
                          "period": period, "due_date": due_on, "amount": amt})
    for (ent, st), amt in am["pt"].items():
        if amt <= 0:
            continue
        r = book.get("pt", st) or {}
        items.append({"kind": "pt", "label": f"Professional Tax · {st}", "legal_entity_id": ent, "state": st, "period": period,
                      "due_date": _day(nxt.year, nxt.month, r.get("due_day", 15)),
                      "amount": amt})
    for (ent, st), amt in am["lwf"].items():
        if amt <= 0:
            continue
        r = book.get("lwf", st) or {}
        items.append({"kind": "lwf", "label": f"Labour Welfare Fund · {st}", "legal_entity_id": ent, "state": st,
                      "period": period, "due_date": _day(nxt.year, nxt.month, r.get("due_day", 15)), "amount": amt})
    # quarterly TDS return after the quarter's last month; tax certificate after March
    if month.month in (6, 9, 12, 3):
        q = {6: "1", 9: "2", 12: "3", 3: "4"}[month.month]
        mmdd = ((due.get("tds_return") or {}).get("quarters") or {}).get(q, "07-31")
        mm, dd = (int(x) for x in mmdd.split("-"))
        yy = month.year + (1 if mm < month.month else 0)
        fy = calc.fy_of(month)
        items.append({"kind": "tds_return", "label": (due.get("tds_return") or {}).get("label") or LABELS["tds_return"],
                      "legal_entity_id": 0, "state": "", "period": f"{fy}-Q{q}", "due_date": date(yy, mm, dd), "amount": ZERO})
    if month.month == 3:
        mmdd = (due.get("certificate") or {}).get("date", "06-15")
        mm, dd = (int(x) for x in mmdd.split("-"))
        items.append({"kind": "certificate", "label": (due.get("certificate") or {}).get("label") or LABELS["certificate"],
                      "legal_entity_id": 0, "state": "", "period": calc.fy_of(month), "due_date": date(month.year, mm, dd),
                      "amount": ZERO})
    many = len(entities) > 1
    for it in items:
        it["salary_paid"] = am["salary_paid"] if it["kind"] == "salary" else None
        it["entity_name"] = am["names"].get(it["legal_entity_id"])
        if many and it["entity_name"] and it["legal_entity_id"]:
            it["label"] = f"{it['label']} · {it['entity_name']}"
    return items


def calendar(db: Session, company_id: int, start: date, months: int = 6) -> list[dict]:
    today = today_for_company_id(db, company_id)
    rows = {(c.kind, c.legal_entity_id, c.state, c.period): c for c in
            db.query(StatutoryChallan).filter(StatutoryChallan.company_id == company_id).all()}
    out = []
    for i in range(months):
        for it in items_for_month(db, company_id, calc.add_months(start.replace(day=1), i)):
            c = rows.get((it["kind"], it["legal_entity_id"], it["state"], it["period"]))
            status = c.status if c else "pending"
            if it["kind"] == "salary" and it.get("salary_paid"):
                status = "paid"
            if status == "pending" and it["due_date"] < today:
                status = "late"
            out.append({**it, "uuid": str(c.uuid) if c else None, "due_date": it["due_date"].isoformat(),
                        "amount": money(c.amount if c and d(c.amount) else it["amount"]), "status": status,
                        "paid_on": c.paid_on.isoformat() if c and c.paid_on else None,
                        "reference": c.reference if c else None, "notes": c.notes if c else None,
                        "days_left": (it["due_date"] - today).days})
    out.sort(key=lambda x: (x["due_date"], x["kind"]))
    return out


def record(db: Session, company_id: int, data: dict) -> StatutoryChallan:
    kind = data.get("kind")
    if kind not in LABELS:
        raise BadRequest("Choose what was paid or filed.")
    key = dict(kind=kind, legal_entity_id=int(data.get("legal_entity_id") or 0), state=data.get("state") or "",
               period=data.get("period"))
    if not key["period"]:
        raise BadRequest("Choose the period.")
    c = db.query(StatutoryChallan).filter(StatutoryChallan.company_id == company_id,
                                          *[getattr(StatutoryChallan, k) == v for k, v in key.items()]).first()
    if c is None:
        c = StatutoryChallan(company_id=company_id, **key, due_date=data.get("due_date") or date.today())
        db.add(c)
    status = data.get("status") or "paid"
    if status not in ("pending", "paid", "filed", "not_applicable"):
        raise BadRequest("Choose a status.")
    if status in ("paid", "filed") and not (data.get("reference") or "").strip():
        raise BadRequest("Enter the challan or acknowledgement number.")
    c.status, c.reference, c.notes = status, data.get("reference"), data.get("notes")
    c.paid_on = data.get("paid_on") or (date.today() if status in ("paid", "filed") else None)
    if data.get("amount") is not None:
        c.amount = d(data["amount"])
    if data.get("due_date"):
        c.due_date = data["due_date"]
    db.flush()
    audit.event(db, "payroll.challan_recorded", "statutory_challans", c.id,
                {"kind": kind, "period": c.period, "reference": c.reference, "status": status}, entity_uuid=str(c.uuid))
    return c


def refresh_for_run(db: Session, company_id: int, run: PayRun) -> None:
    """Write amounts of a locked run into the challan rows of its month (status kept)."""
    for it in items_for_month(db, company_id, run.period_start):
        if it["kind"] in ("tds_return", "certificate"):
            continue
        c = db.query(StatutoryChallan).filter(StatutoryChallan.company_id == company_id, StatutoryChallan.kind == it["kind"],
                                              StatutoryChallan.legal_entity_id == it["legal_entity_id"],
                                              StatutoryChallan.state == it["state"], StatutoryChallan.period == it["period"]).first()
        if c is None:
            c = StatutoryChallan(company_id=company_id, kind=it["kind"], legal_entity_id=it["legal_entity_id"], state=it["state"],
                                 period=it["period"], due_date=it["due_date"], status="pending")
            db.add(c)
        c.amount = it["amount"]
        c.due_date = it["due_date"]
    db.flush()


def mark_salary_paid(db: Session, company_id: int, run: PayRun) -> None:
    for c in db.query(StatutoryChallan).filter(StatutoryChallan.company_id == company_id, StatutoryChallan.kind == "salary",
                                               StatutoryChallan.period == run.period_start.strftime("%Y-%m")).all():
        if c.status == "pending":
            c.status, c.paid_on, c.reference = "paid", date.today(), f"Pay run {run.number or ''}".strip()


def get(db: Session, company_id: int, uuid) -> StatutoryChallan:
    c = db.query(StatutoryChallan).filter(StatutoryChallan.company_id == company_id, StatutoryChallan.uuid == uuid).first()
    if not c:
        raise NotFound("Challan")
    return c


def due_reminders(db: Session, company_id: int) -> int:
    """Remind the owner and HR three days before something is due (once per item)."""
    from modules.notifications.models import Notification
    from modules.notifications.service import notify
    from modules.payroll.service import employee_model
    today = today_for_company_id(db, company_id)
    Employee = employee_model()
    hr = [r[0] for r in db.query(Employee.id).filter(Employee.company_id == company_id, Employee.active.is_(True),
                                                      Employee.role.in_(("admin", "hr"))).all()]
    sent = 0
    for it in calendar(db, company_id, calc.add_months(today.replace(day=1), -1), 2):
        if it["status"] not in ("pending", "late") or it["days_left"] > 3 or it["days_left"] < -1:
            continue
        if it["kind"] in ("pf", "esi", "pt", "lwf", "tds") and d(it["amount"]) == 0:
            continue
        key = f"due:{it['kind']}:{it['legal_entity_id']}:{it['state']}:{it['period']}:{'late' if it['days_left'] < 0 else 'soon'}"
        if db.query(Notification.id).filter(Notification.company_id == company_id, Notification.group_key == key).first():
            continue
        when = "is overdue" if it["days_left"] < 0 else "is due today" if it["days_left"] == 0 else f"is due in {it['days_left']} day(s)"
        notify(db, company_id, [None] + hr, kind="compliance", title=f"{it['label']} for {it['period']} {when}",
               body=f"Amount ₹{it['amount']}. Record the challan once paid.", link="/dashboard/statutory?tab=calendar",
               group_key=key)
        sent += 1
    return sent
