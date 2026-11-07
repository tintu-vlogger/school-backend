"""Fee reports: daily collection, head-wise collection, outstanding, concessions, fee register,
instalment-wise dues; and CSV output for each."""
from __future__ import annotations

import io
from collections import defaultdict
from datetime import date

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from core import csv_safe
from modules.fees.models import (FeeAllocation, FeeHead, FeeInvoice, FeeInvoiceLine, FeePayment, FeePaymentMode)
from modules.fees.service import D0, LIVE, MODES, q


def _label(sec) -> str | None:
    from modules.academics.service import section_label
    return section_label(sec) if sec else None


def daily_collection(db: Session, company_id: int, start: date, end: date) -> dict:
    from modules.academics.models import Section
    pays = (db.query(FeePayment).options(joinedload(FeePayment.student), joinedload(FeePayment.modes))
            .filter(FeePayment.company_id == company_id, FeePayment.paid_on >= start, FeePayment.paid_on <= end)
            .order_by(FeePayment.paid_on, FeePayment.id).all())
    ids = [p.student_id for p in pays]
    from modules.students.models import Enrollment
    secs = {}
    if ids:
        from modules.academics.service import current_session
        sess = current_session(db, company_id)
        for e in (db.query(Enrollment).options(joinedload(Enrollment.section).joinedload(Section.program))
                  .filter(Enrollment.company_id == company_id, Enrollment.student_id.in_(ids),
                          Enrollment.session_id == (sess.id if sess else 0)).all()):
            secs[e.student_id] = _label(e.section)
    rows, by_mode, by_collector, by_day = [], defaultdict(lambda: D0), defaultdict(lambda: D0), defaultdict(lambda: D0)
    total = cancelled = D0
    for p in pays:
        modes = ", ".join(MODES.get(m.mode, m.mode) + (f" {m.reference}" if m.reference else "") for m in p.modes) or "Advance"
        rows.append({"uuid": str(p.uuid), "number": p.number, "paid_on": p.paid_on, "student": p.student.full_name,
                     "admission_no": p.student.admission_no, "section": secs.get(p.student_id),
                     "amount": q(p.amount), "modes": modes, "collected_by": p.collected_by_name, "status": p.status,
                     "source": p.source})
        if p.status != "paid":
            cancelled += q(p.amount)
            continue
        total += q(p.amount)
        by_day[p.paid_on] += q(p.amount)
        by_collector[p.collected_by_name or "Unknown"] += q(p.amount)
        for m in p.modes:
            by_mode[m.mode] += q(m.amount)
    return {"rows": rows, "total": total, "cancelled": cancelled, "count": sum(1 for r in rows if r["status"] == "paid"),
            "by_mode": [{"mode": k, "label": MODES.get(k, k), "amount": v} for k, v in sorted(by_mode.items(), key=lambda x: -x[1])],
            "by_collector": [{"name": k, "amount": v} for k, v in sorted(by_collector.items(), key=lambda x: -x[1])],
            "by_day": [{"date": k, "amount": v} for k, v in sorted(by_day.items())]}


def head_wise(db: Session, company_id: int, start: date, end: date) -> dict:
    rows = (db.query(FeeHead.id, FeeHead.name, FeeHead.code, FeeHead.ledger_account, func.sum(FeeAllocation.amount))
            .join(FeeInvoiceLine, FeeInvoiceLine.head_id == FeeHead.id)
            .join(FeeAllocation, FeeAllocation.line_id == FeeInvoiceLine.id)
            .join(FeePayment, FeePayment.id == FeeAllocation.payment_id)
            .filter(FeeAllocation.company_id == company_id, FeeAllocation.reversed.is_(False),
                    FeePayment.status == "paid", FeePayment.paid_on >= start, FeePayment.paid_on <= end)
            .group_by(FeeHead.id, FeeHead.name, FeeHead.code, FeeHead.ledger_account).all())
    items = sorted([{"head_id": h, "head": n, "code": c, "ledger_account": la, "amount": q(a)} for h, n, c, la, a in rows],
                   key=lambda r: -r["amount"])
    return {"rows": items, "total": sum((r["amount"] for r in items), D0)}


def outstanding(db: Session, company_id: int, session_id: int | None, group: str = "section") -> dict:
    from modules.academics.models import Program, Section
    qy = db.query(FeeInvoice).options(joinedload(FeeInvoice.section).joinedload(Section.program)).filter(
        FeeInvoice.company_id == company_id, FeeInvoice.status != "cancelled")
    if session_id:
        qy = qy.filter(FeeInvoice.session_id == session_id)
    progs = dict(db.query(Program.id, Program.name).filter(Program.company_id == company_id).all())
    agg: dict = defaultdict(lambda: {"billed": D0, "concession": D0, "collected": D0, "outstanding": D0, "students": set(),
                                     "defaulters": set()})
    from modules.fees.service import today
    on = today(db, company_id)
    for inv in qy.all():
        key = (_label(inv.section) or "No section") if group == "section" else progs.get(inv.program_id, "Other")
        a = agg[key]
        a["billed"] += q(inv.total) - q(inv.credited)
        a["concession"] += q(inv.concession)
        a["collected"] += q(inv.paid)
        bal = q(inv.balance) if inv.status in LIVE else D0
        a["outstanding"] += max(bal, D0)
        a["students"].add(inv.student_id)
        if bal > 0 and inv.due_date < on:
            a["defaulters"].add(inv.student_id)
    rows = [{"group": k, "billed": v["billed"], "concession": v["concession"], "collected": v["collected"],
             "outstanding": v["outstanding"], "students": len(v["students"]), "defaulters": len(v["defaulters"]),
             "rate": float(round(v["collected"] / v["billed"] * 100, 1)) if v["billed"] else 0.0}
            for k, v in sorted(agg.items())]
    tot = {k: sum((r[k] for r in rows), D0) for k in ("billed", "concession", "collected", "outstanding")}
    return {"rows": rows, "totals": tot}


def concession_report(db: Session, company_id: int, session_id: int | None) -> dict:
    from modules.academics.models import Section
    qy = (db.query(FeeInvoiceLine, FeeInvoice).join(FeeInvoice, FeeInvoice.id == FeeInvoiceLine.invoice_id)
          .options(joinedload(FeeInvoice.student), joinedload(FeeInvoice.section).joinedload(Section.program),
                   joinedload(FeeInvoiceLine.head))
          .filter(FeeInvoiceLine.company_id == company_id, FeeInvoiceLine.concession > 0,
                  FeeInvoice.status != "cancelled"))
    if session_id:
        qy = qy.filter(FeeInvoice.session_id == session_id)
    by: dict = {}
    by_reason: dict = defaultdict(lambda: D0)
    for ln, inv in qy.all():
        r = by.setdefault(inv.student_id, {"student": inv.student.full_name, "admission_no": inv.student.admission_no,
                                           "section": _label(inv.section), "amount": D0, "concessions": set(), "heads": set()})
        r["amount"] += q(ln.concession)
        r["heads"].add(ln.head.name)
        for n in (ln.concession_note or "Concession").split(", "):
            r["concessions"].add(n)
            by_reason[n] += q(ln.concession) / max(1, len((ln.concession_note or "x").split(", ")))
    rows = sorted([{**v, "concessions": ", ".join(sorted(v["concessions"])), "heads": ", ".join(sorted(v["heads"]))}
                   for v in by.values()], key=lambda r: -r["amount"])
    return {"rows": rows, "total": sum((r["amount"] for r in rows), D0),
            "by_concession": [{"name": k, "amount": q(v)} for k, v in sorted(by_reason.items(), key=lambda x: -x[1])]}


def fee_register(db: Session, company_id: int, session_id: int | None, program_id: int | None = None,
                 section_id: int | None = None) -> dict:
    from modules.academics.models import Section
    qy = db.query(FeeInvoice).options(joinedload(FeeInvoice.student), joinedload(FeeInvoice.section)
                                      .joinedload(Section.program)).filter(
        FeeInvoice.company_id == company_id, FeeInvoice.status != "cancelled")
    if session_id:
        qy = qy.filter(FeeInvoice.session_id == session_id)
    if program_id:
        qy = qy.filter(FeeInvoice.program_id == program_id)
    if section_id:
        qy = qy.filter(FeeInvoice.section_id == section_id)
    by: dict = {}
    for inv in qy.all():
        r = by.setdefault(inv.student_id, {"student_uuid": str(inv.student.uuid), "student": inv.student.full_name,
                                           "admission_no": inv.student.admission_no, "section": _label(inv.section),
                                           "gross": D0, "concession": D0, "tax": D0, "billed": D0, "paid": D0,
                                           "balance": D0})
        r["gross"] += q(inv.gross)
        r["concession"] += q(inv.concession)
        r["tax"] += q(inv.tax)
        r["billed"] += q(inv.total) - q(inv.credited)
        r["paid"] += q(inv.paid)
        r["balance"] += max(q(inv.balance), D0)
    rows = sorted(by.values(), key=lambda r: (r["section"] or "", r["student"]))
    tot = {k: sum((r[k] for r in rows), D0) for k in ("gross", "concession", "tax", "billed", "paid", "balance")}
    return {"rows": rows, "totals": tot}


def instalment_dues(db: Session, company_id: int, session_id: int | None) -> dict:
    qy = db.query(FeeInvoice).filter(FeeInvoice.company_id == company_id, FeeInvoice.status != "cancelled",
                                     FeeInvoice.source == "structure")
    if session_id:
        qy = qy.filter(FeeInvoice.session_id == session_id)
    agg: dict = {}
    for inv in qy.all():
        a = agg.setdefault(inv.title, {"instalment": inv.title, "due_date": inv.due_date, "billed": D0, "collected": D0,
                                       "outstanding": D0, "invoices": 0, "unpaid": 0})
        a["due_date"] = min(a["due_date"], inv.due_date)
        a["billed"] += q(inv.total) - q(inv.credited)
        a["collected"] += q(inv.paid)
        a["outstanding"] += max(q(inv.balance), D0)
        a["invoices"] += 1
        if inv.status in LIVE:
            a["unpaid"] += 1
    return {"rows": sorted(agg.values(), key=lambda r: r["due_date"])}


def to_csv(headers: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    w = csv_safe.writer(buf)
    w.writerow(headers)
    for r in rows:
        w.writerow(["" if v is None else v for v in r])
    return buf.getvalue()


def csv_for(kind: str, data: dict) -> tuple[str, str]:
    """(filename, csv text) for a report."""
    if kind == "daily":
        return "daily-collection.csv", to_csv(
            ["Receipt", "Date", "Student", "Admission no", "Class", "Amount", "Modes", "Collected by", "Status"],
            [[r["number"], r["paid_on"], r["student"], r["admission_no"], r["section"], r["amount"], r["modes"],
              r["collected_by"], r["status"]] for r in data["rows"]])
    if kind == "heads":
        return "head-wise-collection.csv", to_csv(["Head", "Code", "Ledger account", "Amount"],
                                                  [[r["head"], r["code"], r["ledger_account"], r["amount"]] for r in data["rows"]])
    if kind == "outstanding":
        return "outstanding.csv", to_csv(
            ["Group", "Students", "Billed", "Concession", "Collected", "Outstanding", "Defaulters", "Collected %"],
            [[r["group"], r["students"], r["billed"], r["concession"], r["collected"], r["outstanding"], r["defaulters"],
              r["rate"]] for r in data["rows"]])
    if kind == "concessions":
        return "concessions.csv", to_csv(["Student", "Admission no", "Class", "Concessions", "Heads", "Amount"],
                                         [[r["student"], r["admission_no"], r["section"], r["concessions"], r["heads"],
                                           r["amount"]] for r in data["rows"]])
    if kind == "register":
        return "fee-register.csv", to_csv(
            ["Student", "Admission no", "Class", "Gross", "Concession", "Tax", "Billed", "Paid", "Balance"],
            [[r["student"], r["admission_no"], r["section"], r["gross"], r["concession"], r["tax"], r["billed"],
              r["paid"], r["balance"]] for r in data["rows"]])
    if kind == "instalments":
        return "instalment-dues.csv", to_csv(["Instalment", "Due date", "Invoices", "Unpaid", "Billed", "Collected", "Outstanding"],
                                             [[r["instalment"], r["due_date"], r["invoices"], r["unpaid"], r["billed"],
                                               r["collected"], r["outstanding"]] for r in data["rows"]])
    if kind == "dues":
        return "dues.csv", to_csv(["Student", "Admission no", "Class", "Phone", "Due", "Overdue", "Oldest due date",
                                   "Days overdue", "Instalments"],
                                  [[r["name"], r["admission_no"], r["section_label"], r["phone"], r["due"], r["overdue"],
                                    r["oldest_due_date"], r["days_overdue"], "; ".join(r["instalments"])] for r in data["rows"]])
    raise ValueError(kind)
