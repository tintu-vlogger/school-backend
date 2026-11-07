"""Fee defaults for every institution, and demo data for the demo school."""
from __future__ import annotations

import logging
import random
from datetime import timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

logger = logging.getLogger("hrms")

DEFAULT_CATEGORIES = [("General", "GEN"), ("New admission", "NEW"), ("RTE", "RTE"), ("Staff ward", "STAFF"), ("NRI", "NRI")]
DEFAULT_CONCESSION_TYPES = [("Merit scholarship", "merit", "percent", 25, None), ("Staff ward", "staff_ward", "percent", 50, "TUITION"),
                            ("Sports quota", "sports", "percent", 20, "TUITION"), ("Need-based aid", "need", "fixed", 2000, None)]
DEFAULT_LEDGER = {"income": ["Donations", "Canteen", "Hall rent", "Interest", "Other income"],
                  "expense": ["Salaries (cash)", "Electricity", "Water", "Maintenance", "Stationery", "Transport fuel",
                              "Events", "Other expenses"]}


def setup_company(db: Session, company_id: int) -> None:
    try:
        with db.begin_nested():
            from modules.fees import service
            from modules.fees.models import (FeeCategory, FeeConcessionType, FeeHead, FeeLateRule, FeeLedgerCategory)
            service.ensure_default_heads(db, company_id)
            if not db.query(FeeCategory.id).filter(FeeCategory.company_id == company_id).first():
                for name, code in DEFAULT_CATEGORIES:
                    db.add(FeeCategory(company_id=company_id, name=name, code=code))
            if not db.query(FeeConcessionType.id).filter(FeeConcessionType.company_id == company_id).first():
                heads = {h.code: h.id for h in db.query(FeeHead).filter(FeeHead.company_id == company_id).all()}
                for name, reason, kind, value, head in DEFAULT_CONCESSION_TYPES:
                    db.add(FeeConcessionType(company_id=company_id, name=name, reason=reason, kind=kind,
                                             value=Decimal(value), head_id=heads.get(head) if head else None))
            if not db.query(FeeLedgerCategory.id).filter(FeeLedgerCategory.company_id == company_id).first():
                for kind, names in DEFAULT_LEDGER.items():
                    for n in names:
                        db.add(FeeLedgerCategory(company_id=company_id, kind=kind, name=n))
            if not db.query(FeeLateRule.id).filter(FeeLateRule.company_id == company_id).first():
                db.add(FeeLateRule(company_id=company_id, name="Late fee", kind="per_day", amount=Decimal("10"),
                                   grace_days=7, max_amount=Decimal("500"), active=False))
            db.flush()
    except Exception as exc:  # set-up must never block start-up
        logger.warning("Fee defaults for company %s failed: %s", company_id, exc)


# Quarterly amounts by level (tuition per quarter, plus one-time heads).
def _amounts(level: str | None, sort: int) -> dict:
    base = {"Pre-primary": 9000, "Primary": 12000, "Middle": 15000, "Secondary": 18000, "Senior secondary": 21000}
    t = base.get(level or "", 12000) + (sort % 3) * 500
    return {"TUITION": t, "ANNUAL": 6000 if level != "Pre-primary" else 4000, "EXAM": 1000 if level != "Pre-primary" else 0,
            "LAB": 1500 if level in ("Secondary", "Senior secondary") else 0, "COMPUTER": 800 if level not in ("Pre-primary",) else 0,
            "ACTIVITY": 700, "TRANSPORT": 4500, "DEPOSIT": 5000}


def seed_demo(db: Session, company_id: int) -> None:
    from core import context
    from modules.academics.models import Program
    from modules.academics.service import current_session
    from modules.fees import service, settings as fee_settings
    from modules.fees.models import FeeHead, FeeInvoice, FeeLateRule, FeeOptIn, FeeStructure
    from modules.students.models import Enrollment, Student
    if db.query(FeeStructure.id).filter(FeeStructure.company_id == company_id).first():
        return
    sess = current_session(db, company_id)
    if sess is None:
        return
    setup_company(db, company_id)
    ctx = context.current()
    ctx.company_id, ctx.actor_name = company_id, "Ganesh Pillai"
    fee_settings.save(db, company_id, {"sibling_discount": {"enabled": True, "percent": 10, "head_codes": ["TUITION"]},
                                       "concession_approval": True})
    rule = db.query(FeeLateRule).filter(FeeLateRule.company_id == company_id).first()
    if rule:
        rule.active = True
    heads = {h.code: h for h in db.query(FeeHead).filter(FeeHead.company_id == company_id).all()}
    plan = service.plan_dates("quarterly", sess.start_date, day=10)
    programs = db.query(Program).filter(Program.company_id == company_id, Program.active.is_(True)).order_by(Program.sort_order).all()
    for p in programs:
        amt = _amounts(p.level, p.sort_order)
        items = [{"head_id": heads["TUITION"].id, "amount": amt["TUITION"], "billing": "per_instalment"}]
        for code in ("EXAM", "COMPUTER", "ACTIVITY", "LAB"):
            if amt[code]:
                items.append({"head_id": heads[code].id, "amount": amt[code], "billing": "per_instalment"})
        items.append({"head_id": heads["ANNUAL"].id, "amount": amt["ANNUAL"], "billing": "once", "instalment_no": 1})
        items.append({"head_id": heads["DEPOSIT"].id, "amount": amt["DEPOSIT"], "billing": "once", "instalment_no": 1})
        items.append({"head_id": heads["TRANSPORT"].id, "amount": amt["TRANSPORT"], "billing": "per_instalment", "optional": True})
        service.save_structure(db, company_id, {
            "name": f"{p.name} fees {sess.name}", "session_id": sess.id, "program_id": p.id, "plan": "quarterly",
            "instalments": [{"name": n, "due_date": d} for n, d in plan], "items": items})
    # Transport riders opt into the transport head.
    for s in db.query(Student).filter(Student.company_id == company_id, Student.uses_transport.is_(True)).all():
        db.add(FeeOptIn(company_id=company_id, student_id=s.id, session_id=sess.id, head_id=heads["TRANSPORT"].id))
    db.flush()
    today = service.today(db, company_id)
    # Bill the instalments that are already due, plus the next one.
    due_seqs = [i + 1 for i, (_, d) in enumerate(plan) if d <= today + timedelta(days=40)] or [1]
    service.generate_bulk(db, company_id, session_id=sess.id, instalment_seqs=due_seqs)
    rng = random.Random(11)
    students = (db.query(Student).join(Enrollment, Enrollment.student_id == Student.id)
                .filter(Enrollment.company_id == company_id, Enrollment.session_id == sess.id).order_by(Student.id).all())
    modes = ["cash", "upi", "upi", "bank_transfer", "card", "cheque"]
    for st in students:
        invs = service.open_invoices(db, company_id, st.id)
        if not invs:
            continue
        family = st.last_name == "Kumar" and st.first_name in ("Aarav", "Anika")
        roll = rng.random()
        if family:
            pay_n = len([i for i in invs if i.due_date < today]) - 1  # one instalment overdue for the demo family
        elif roll < 0.70:
            pay_n = len([i for i in invs if i.due_date < today])  # all due instalments paid
        elif roll < 0.85:
            pay_n = -1  # partial payment of the first instalment
        else:
            pay_n = 0  # overdue
        if pay_n == -1:
            first = invs[0]
            amount = (first.balance * Decimal("0.5")).quantize(Decimal("1"))
            _pay(db, company_id, st, [first.id], amount, first.due_date + timedelta(days=rng.randint(0, 20)), rng, modes)
            continue
        for inv in invs[:max(0, pay_n)]:
            when = min(today, inv.due_date - timedelta(days=rng.randint(0, 12)))
            _pay(db, company_id, st, [inv.id], inv.balance, max(when, sess.start_date), rng, modes)
    # Payments today for the dashboard.
    late_payers = [s for s in students if service.student_dues(db, company_id, s.id)["overdue"] > 0][:4]
    for st in late_payers[:3]:
        invs = service.open_invoices(db, company_id, st.id)
        if invs and st.last_name != "Kumar":
            service.collect(db, company_id, st, modes=[{"mode": "cash", "amount": invs[0].balance}],
                            invoice_ids=[invs[0].id], paid_on=today, waive_late_fee=True)
    # One sibling concession for the Kumar children (the younger one, Anika), from the sibling rule.
    anika = db.query(Student).filter(Student.company_id == company_id, Student.first_name == "Anika",
                                     Student.last_name == "Kumar").first()
    if anika:
        service.ensure_sibling_concession(db, company_id, anika, sess.id)
        service.reprice_open_invoices(db, company_id, anika.id, sess.id)
    # A few cash book entries.
    from modules.fees.models import FeeLedgerCategory, FeeLedgerEntry
    cats = {c.name: c for c in db.query(FeeLedgerCategory).filter(FeeLedgerCategory.company_id == company_id).all()}
    for days, kind, cat, amount, party, desc in [
        (2, "expense", "Electricity", 18450, "BESCOM", "Electricity bill"), (5, "expense", "Stationery", 3200, "Sapna Book House", "Chalk, registers and paper"),
        (9, "income", "Canteen", 7500, "Annapoorna Caterers", "Canteen licence fee"), (12, "expense", "Maintenance", 9800, "Sri Sai Electricals", "Classroom fans repaired"),
        (20, "income", "Donations", 25000, "Alumni association", "Library books donation"), (26, "expense", "Water", 2400, "BWSSB", "Water bill")]:
        e = FeeLedgerEntry(company_id=company_id, kind=kind, category_id=cats[cat].id if cat in cats else None,
                           entry_date=today - timedelta(days=days), amount=Decimal(amount), party=party, description=desc,
                           mode="bank_transfer" if amount > 5000 else "cash", created_by_name="Ganesh Pillai")
        e.number = service._next(db, company_id, "fee_voucher", "VCH", FeeLedgerEntry)
        db.add(e)
    db.flush()
    n = db.query(FeeInvoice.id).filter(FeeInvoice.company_id == company_id).count()
    logger.info("Fee demo data: %s invoices", n)


def _pay(db, company_id, st, invoice_ids, amount, when, rng, modes):
    from modules.fees import service
    mode = rng.choice(modes)
    m = {"mode": mode, "amount": amount}
    if mode == "cheque":
        m.update(reference=str(rng.randint(100000, 999999)), bank=rng.choice(["HDFC Bank", "SBI", "Canara Bank", "ICICI Bank"]),
                 instrument_date=when)
    elif mode in ("upi", "bank_transfer", "card"):
        m["reference"] = f"{rng.randint(10**11, 10**12 - 1)}"
    service.collect(db, company_id, st, modes=[m], invoice_ids=invoice_ids, paid_on=when, waive_late_fee=True)
