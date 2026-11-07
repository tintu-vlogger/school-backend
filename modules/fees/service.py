"""Fees: billing, concessions, late fees, collection, receipts, refunds and dues.

Functions other modules use:
    add_charge(db, company_id, student_id, head_code, amount, description, due_date=None) -> FeeInvoice
        bill a learner for something (transport, hostel, library fine …). The invoice is
        flushed, not committed; the caller commits.
    student_dues(db, company_id, student_id) -> {"total": Decimal, "overdue": Decimal, "next_due_date": date|None}

Money is Decimal with 2 places everywhere; amounts are in the institution's currency
(companies.currency).

Rules in one place
- Concessions: percentage of the line, or a fixed amount per invoice. A concession with a head
  applies to that head only; without a head it applies to every fee line (fixed amounts are
  spread over the lines in proportion). A concession never takes a line below zero.
- Tax (GST) is charged on the amount after concession, at the head's rate.
- Sibling rule (setting): when on, every learner who has an older brother or sister (sharing a
  guardian, both active) gets an automatic, approved concession on the chosen heads.
- Late fee: fixed, or an amount per day counted after the grace days (day 1 = the first day
  after the grace period), capped per invoice. Added as a line when money is collected.
- Payments pay the oldest dues first (by due date), and within an invoice line by line.
  Money above the dues is kept as advance (wallet) when the setting allows it.
- Invoices and receipts are never deleted: they are cancelled with a reason.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload, selectinload

from core import audit, context
from core.errors import BadRequest, NotFound
from modules.fees import settings as fee_settings
from modules.fees.models import (FeeAdjustment, FeeAllocation, FeeCategory, FeeConcession, FeeHead, FeeInstalment,
                                 FeeInvoice, FeeInvoiceLine, FeeLateRule, FeeOptIn, FeePayment, FeePaymentMode,
                                 FeeRefund, FeeStructure, FeeStructureItem, FeeWalletEntry)

D0 = Decimal("0.00")
CENT = Decimal("0.01")
MODES = {"cash": "Cash", "cheque": "Cheque", "dd": "Demand draft", "upi": "UPI", "card": "Card",
         "bank_transfer": "Bank transfer", "online": "Online"}
LIVE = ("open", "partial")
HEAD_KINDS = {"recurring": "Recurring", "one_time": "One-time", "optional": "Optional", "deposit": "Refundable deposit"}
REASONS = {"sibling": "Sibling", "merit": "Merit", "staff_ward": "Staff ward", "need": "Need-based", "sports": "Sports",
           "rte": "RTE / government quota", "other": "Other"}

# Heads every institution gets (code, name, kind, sort).
DEFAULT_HEADS = [
    ("TUITION", "Tuition fee", "recurring", 1), ("ADMISSION", "Admission fee", "one_time", 2),
    ("ANNUAL", "Annual charges", "one_time", 3), ("EXAM", "Examination fee", "recurring", 4),
    ("LAB", "Laboratory fee", "recurring", 5), ("LIBRARY", "Library fee", "one_time", 6),
    ("COMPUTER", "Computer fee", "recurring", 7), ("ACTIVITY", "Activity fee", "recurring", 8),
    ("TRANSPORT", "Transport fee", "optional", 9), ("HOSTEL", "Hostel fee", "optional", 10),
    ("UNIFORM", "Uniform", "optional", 11), ("BOOKS", "Books and stationery", "optional", 12),
    ("DEPOSIT", "Caution deposit (refundable)", "deposit", 13), ("LATE", "Late fee", "one_time", 50),
    ("BOUNCE", "Cheque bounce charge", "one_time", 51), ("FINE", "Fines", "one_time", 52),
    ("MISC", "Other charges", "one_time", 53),
]
SYSTEM_HEADS = {"LATE", "BOUNCE", "MISC"}


def q(x) -> Decimal:
    return Decimal(str(x if x is not None else 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def _actor() -> str:
    return context.current().actor_name or "System"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def today(db: Session, company_id: int) -> date:
    from core.timeutil import today_for_company_id
    return today_for_company_id(db, company_id)


# ── Heads ────────────────────────────────────────────────────────────

def ensure_default_heads(db: Session, company_id: int) -> None:
    have = {c for (c,) in db.query(FeeHead.code).filter(FeeHead.company_id == company_id).all()}
    for code, name, kind, sort in DEFAULT_HEADS:
        if code not in have:
            db.add(FeeHead(company_id=company_id, code=code, name=name, kind=kind, sort_order=sort,
                           is_system=code in SYSTEM_HEADS, ledger_account=f"{name} income"))
    db.flush()


def head_by_code(db: Session, company_id: int, code: str, create_name: str | None = None) -> FeeHead:
    code = (code or "").strip().upper()
    h = db.query(FeeHead).filter(FeeHead.company_id == company_id, FeeHead.code == code).first()
    if h is None:
        if not code:
            raise BadRequest("Pick a fee head.")
        h = FeeHead(company_id=company_id, code=code, name=create_name or code.replace("_", " ").title(),
                    kind="one_time", sort_order=40)
        db.add(h)
        db.flush()
    return h


def _head(db: Session, company_id: int, head_id: int | None) -> FeeHead | None:
    if head_id is None:
        return None
    h = db.query(FeeHead).filter(FeeHead.company_id == company_id, FeeHead.id == head_id).first()
    if h is None:
        raise BadRequest("The fee head was not found.")
    return h


# ── Structures ───────────────────────────────────────────────────────

PLANS = {"monthly": 12, "quarterly": 4, "term": 3, "half_yearly": 2, "yearly": 1}


def plan_dates(plan: str, start: date, n_terms: int | None = None, day: int = 10) -> list[tuple[str, date]]:
    """Suggested instalments for a plan from the session start: (name, due date)."""
    import calendar
    n = n_terms if plan == "term" and n_terms else PLANS.get(plan, 1)
    step = 12 // n if n else 12
    out = []
    for i in range(n):
        m0 = start.month - 1 + i * step
        y, m = start.year + m0 // 12, m0 % 12 + 1
        due = date(y, m, min(day, calendar.monthrange(y, m)[1]))
        if plan == "monthly":
            name = due.strftime("%B %Y")
        elif plan == "quarterly":
            end_m0 = m0 + step - 1
            name = f"Quarter {i + 1} ({calendar.month_abbr[m]}–{calendar.month_abbr[end_m0 % 12 + 1]})"
        elif plan == "half_yearly":
            name = f"Half {i + 1}"
        elif plan == "term":
            name = f"Term {i + 1}"
        else:
            name = "Annual"
        out.append((name, due))
    return out


def save_structure(db: Session, company_id: int, data: dict, row: FeeStructure | None = None) -> FeeStructure:
    from modules.academics.models import AcademicSession, Program
    if not db.query(AcademicSession.id).filter(AcademicSession.company_id == company_id,
                                               AcademicSession.id == data["session_id"]).first():
        raise BadRequest("The session was not found.")
    if data.get("program_id") and not db.query(Program.id).filter(Program.company_id == company_id,
                                                                  Program.id == data["program_id"]).first():
        raise BadRequest("The class or programme was not found.")
    if data.get("category_id") and not db.query(FeeCategory.id).filter(FeeCategory.company_id == company_id,
                                                                       FeeCategory.id == data["category_id"]).first():
        raise BadRequest("The fee category was not found.")
    items = data.get("items") or []
    insts = data.get("instalments") or []
    if not insts:
        raise BadRequest("Add at least one instalment with a due date.")
    if not items:
        raise BadRequest("Add at least one fee head with an amount.")
    seqs = list(range(1, len(insts) + 1))
    if row is None:
        row = FeeStructure(company_id=company_id)
        db.add(row)
    elif _structure_used(db, company_id, row.id):
        # Invoices exist: amounts may change for invoices still to be made, but instalments stay.
        if len(insts) != len(row.instalments):
            raise BadRequest("Invoices were already made from this structure, so the number of instalments "
                             "cannot change. Make a new structure instead.")
    for k in ("name", "session_id", "program_id", "category_id", "plan", "notes"):
        if k in data:
            setattr(row, k, data[k])
    if "active" in data and data["active"] is not None:
        row.active = data["active"]
    db.flush()
    # instalments: update in place by position so invoice links stay valid
    existing = list(row.instalments)
    for i, inst in enumerate(insts):
        if i < len(existing):
            existing[i].name, existing[i].due_date, existing[i].seq = inst["name"], inst["due_date"], seqs[i]
        else:
            row.instalments.append(FeeInstalment(company_id=company_id, seq=seqs[i], name=inst["name"],
                                                 due_date=inst["due_date"]))
    for extra in existing[len(insts):]:
        row.instalments.remove(extra)
    row.items.clear()
    db.flush()
    for i, it in enumerate(items):
        head = _head(db, company_id, it["head_id"])
        amount = q(it["amount"])
        if amount < 0:
            raise BadRequest("Amounts cannot be negative.")
        billing = it.get("billing") or ("once" if head.kind in ("one_time", "deposit") else "per_instalment")
        inst_no = it.get("instalment_no") or 1
        if billing == "once" and not 1 <= inst_no <= len(insts):
            raise BadRequest(f"{head.name}: pick an instalment between 1 and {len(insts)}.")
        row.items.append(FeeStructureItem(company_id=company_id, head_id=head.id, amount=amount, billing=billing,
                                          instalment_no=inst_no if billing == "once" else None,
                                          optional=bool(it.get("optional")) or head.kind == "optional",
                                          sort_order=i + 1))
    db.flush()
    return row


def _structure_used(db: Session, company_id: int, structure_id: int) -> bool:
    return db.query(FeeInvoice.id).filter(FeeInvoice.company_id == company_id,
                                          FeeInvoice.structure_id == structure_id).first() is not None


def structure_total(s: FeeStructure, include_optional: bool = False) -> Decimal:
    n = len(s.instalments)
    tot = D0
    for it in s.items:
        if it.optional and not include_optional:
            continue
        tot += q(it.amount) * (n if it.billing == "per_instalment" else 1)
    return tot


def structure_for(db: Session, company_id: int, student, enrollment) -> FeeStructure | None:
    """The structure that bills this learner: same session, the learner's program (or a structure
    for every program), and the learner's fee category when a structure exists for it."""
    if enrollment is None:
        return None
    rows = (db.query(FeeStructure).options(selectinload(FeeStructure.items).joinedload(FeeStructureItem.head),
                                           selectinload(FeeStructure.instalments), joinedload(FeeStructure.category))
            .filter(FeeStructure.company_id == company_id, FeeStructure.active.is_(True),
                    FeeStructure.session_id == enrollment.session_id,
                    or_(FeeStructure.program_id == enrollment.program_id, FeeStructure.program_id.is_(None)))
            .all())
    cat = (student.fee_category or "").strip().lower()

    def matches(s: FeeStructure) -> bool:
        if s.category_id is None:
            return True
        c = s.category
        return bool(cat) and cat in {(c.name or "").lower(), (c.code or "").lower()}

    rows = [s for s in rows if matches(s)]
    rows.sort(key=lambda s: (s.category_id is None, s.program_id is None, s.id))
    return rows[0] if rows else None


# ── Concessions ──────────────────────────────────────────────────────

def concessions_for(db: Session, company_id: int, student_id: int, session_id: int | None, on: date) -> list[FeeConcession]:
    qy = db.query(FeeConcession).filter(
        FeeConcession.company_id == company_id, FeeConcession.student_id == student_id,
        FeeConcession.status == "approved",
        or_(FeeConcession.valid_from.is_(None), FeeConcession.valid_from <= on),
        or_(FeeConcession.valid_to.is_(None), FeeConcession.valid_to >= on))
    if session_id:
        qy = qy.filter(or_(FeeConcession.session_id.is_(None), FeeConcession.session_id == session_id))
    return qy.order_by(FeeConcession.id).all()


def price_lines(lines: list[dict], concessions: list) -> list[dict]:
    """Fill concession, tax and net on fee lines. `lines`: dicts with head_id, amount, tax_rate,
    kind ('fee' lines get concessions). `concessions`: objects/dicts with head_id, kind, value, name."""
    def g(c, k):
        return c.get(k) if isinstance(c, dict) else getattr(c, k)

    for ln in lines:
        ln["concession"] = D0
        ln["_notes"] = []
    fee_lines = [ln for ln in lines if ln.get("kind", "fee") == "fee" and q(ln["amount"]) > 0]
    for c in concessions:
        value = q(g(c, "value"))
        if value <= 0:
            continue
        targets = [ln for ln in fee_lines if g(c, "head_id") in (None, ln["head_id"])]
        if not targets:
            continue
        if g(c, "kind") == "percent":
            for ln in targets:
                ln["concession"] += q(q(ln["amount"]) * value / 100)
                ln["_notes"].append(g(c, "name"))
        else:
            base = sum(q(ln["amount"]) for ln in targets)
            total = min(value, base)
            left = total
            for i, ln in enumerate(targets):
                part = left if i == len(targets) - 1 else min(left, q(total * q(ln["amount"]) / base))
                ln["concession"] += part
                left -= part
                ln["_notes"].append(g(c, "name"))
    for ln in lines:
        amount = q(ln["amount"])
        ln["concession"] = min(q(ln["concession"]), amount)
        rate = ln.get("tax_rate")
        ln["tax"] = q((amount - ln["concession"]) * Decimal(str(rate)) / 100) if rate else D0
        ln["net"] = amount - ln["concession"] + ln["tax"]
        ln["concession_note"] = ", ".join(dict.fromkeys(ln.pop("_notes")))[:200] or None
    return lines


def _older_sibling_exists(db: Session, company_id: int, student) -> bool:
    from modules.students.models import Student, StudentGuardian
    gids = [g for (g,) in db.query(StudentGuardian.guardian_id).filter(StudentGuardian.company_id == company_id,
                                                                       StudentGuardian.student_id == student.id).all()]
    if not gids:
        return False
    sibs = (db.query(Student).join(StudentGuardian, StudentGuardian.student_id == Student.id)
            .filter(StudentGuardian.company_id == company_id, StudentGuardian.guardian_id.in_(gids),
                    Student.id != student.id, Student.status == "active", Student.archived_at.is_(None)).all())

    def age_key(s):  # older first: earlier birth date, then earlier admission
        return (s.date_of_birth or date.max, s.id)
    return any(age_key(s) < age_key(student) for s in sibs)


def ensure_sibling_concession(db: Session, company_id: int, student, session_id: int | None) -> None:
    """Keep the automatic sibling concession in line with the setting and the family."""
    cfg = fee_settings.get(db, company_id)["sibling_discount"]
    existing = db.query(FeeConcession).filter(FeeConcession.company_id == company_id,
                                              FeeConcession.student_id == student.id, FeeConcession.auto.is_(True),
                                              FeeConcession.status == "approved").all()
    eligible = bool(cfg.get("enabled")) and q(cfg.get("percent")) > 0 and _older_sibling_exists(db, company_id, student)
    if not eligible:
        for c in existing:
            c.status = "cancelled"
            c.notes = "Sibling rule no longer applies"
        return
    pct = q(cfg.get("percent"))
    codes = cfg.get("head_codes") or []
    heads = [head_by_code(db, company_id, c) for c in codes] if codes else [None]
    want = {(h.id if h else None) for h in heads}
    for c in existing:
        if c.head_id not in want or q(c.value) != pct or (session_id and c.session_id not in (None, session_id)):
            c.status = "cancelled"
            c.notes = "Replaced after the sibling rule changed"
    have = {c.head_id for c in existing if c.status == "approved"}
    for h in heads:
        hid = h.id if h else None
        if hid in have:
            continue
        db.add(FeeConcession(company_id=company_id, student_id=student.id, session_id=session_id, name="Sibling discount",
                             reason="sibling", kind="percent", value=pct, head_id=hid, status="approved", auto=True,
                             requested_by_name="Sibling rule", decided_at=_now()))
    db.flush()


def create_concession(db: Session, company_id: int, data: dict) -> FeeConcession:
    from modules.fees.models import FeeConcessionType
    from modules.students.models import Student
    student = db.query(Student).filter(Student.company_id == company_id, Student.id == data["student_id"]).first()
    if student is None:
        raise BadRequest("The learner was not found.")
    t = None
    if data.get("type_id"):
        t = db.query(FeeConcessionType).filter(FeeConcessionType.company_id == company_id,
                                               FeeConcessionType.id == data["type_id"]).first()
        if t is None:
            raise BadRequest("The concession type was not found.")
    kind = data.get("kind") or (t.kind if t else "percent")
    value = q(data.get("value") if data.get("value") is not None else (t.value if t else 0))
    if value <= 0:
        raise BadRequest("Enter the concession amount or percentage.")
    if kind == "percent" and value > 100:
        raise BadRequest("A percentage cannot be more than 100.")
    head_id = data.get("head_id") if "head_id" in data else (t.head_id if t else None)
    _head(db, company_id, head_id)
    if data.get("valid_from") and data.get("valid_to") and data["valid_to"] < data["valid_from"]:
        raise BadRequest("The end date is before the start date.")
    sess_id = data.get("session_id")
    if sess_id is None:
        from modules.academics.service import current_session
        cur = current_session(db, company_id)
        sess_id = cur.id if cur else None
    c = FeeConcession(company_id=company_id, student_id=student.id, session_id=sess_id, type_id=t.id if t else None,
                      name=(data.get("name") or (t.name if t else REASONS.get(data.get("reason") or "other", "Concession")))[:120],
                      reason=data.get("reason") or (t.reason if t else "other"), kind=kind, value=value, head_id=head_id,
                      valid_from=data.get("valid_from"), valid_to=data.get("valid_to"), notes=data.get("notes"),
                      status="pending", requested_by_name=_actor())
    db.add(c)
    db.flush()
    if fee_settings.get(db, company_id)["concession_approval"]:
        from core.workflow import service as wf
        desc = f"{value}%" if kind == "percent" else f"{value}"
        inst = wf.submit(db, company_id, "fees.concession",
                         {"concession_id": c.id, "student": student.full_name, "admission_no": student.admission_no,
                          "name": c.name, "reason": c.reason, "kind": kind, "value": float(value), "label": desc,
                          "notes": c.notes},
                         f"Fee concession for {student.full_name}: {c.name} ({desc})",
                         subject_type="fee_concession", subject_id=c.id)
        if c.workflow_instance_id is None:
            c.workflow_instance_id = inst.id
    else:
        approve_concession(db, c)
    return c


def approve_concession(db: Session, c: FeeConcession) -> None:
    c.status = "approved"
    c.decided_at = _now()
    db.flush()
    reprice_open_invoices(db, c.company_id, c.student_id, c.session_id)
    audit.event(db, "fees.concession_approved", "fee_concessions", c.id, {"value": str(c.value), "kind": c.kind},
                company_id=c.company_id, entity_uuid=str(c.uuid), entity_label=c.name)


def concession_decided(db: Session, instance, status: str) -> None:
    cid = (instance.payload or {}).get("concession_id")
    c = db.query(FeeConcession).filter(FeeConcession.company_id == instance.company_id, FeeConcession.id == cid).first()
    if c is None or c.status != "pending":
        return
    c.workflow_instance_id = instance.id
    if status == "approved":
        approve_concession(db, c)
    else:
        c.status = status
        c.decided_at = _now()
        db.flush()


def cancel_concession(db: Session, c: FeeConcession, reason: str | None) -> None:
    if c.status in ("cancelled", "rejected"):
        raise BadRequest("This concession is already closed.")
    was = c.status
    c.status = "cancelled"
    c.notes = ((c.notes or "") + f"\nCancelled: {reason}").strip() if reason else c.notes
    db.flush()
    if was == "approved":
        reprice_open_invoices(db, c.company_id, c.student_id, c.session_id)


def reprice_open_invoices(db: Session, company_id: int, student_id: int, session_id: int | None) -> int:
    qy = db.query(FeeInvoice).options(selectinload(FeeInvoice.lines)).filter(
        FeeInvoice.company_id == company_id, FeeInvoice.student_id == student_id, FeeInvoice.status.in_(LIVE),
        FeeInvoice.source == "structure")
    if session_id:
        qy = qy.filter(FeeInvoice.session_id == session_id)
    n = 0
    for inv in qy.all():
        reprice(db, inv)
        n += 1
    return n


def reprice(db: Session, inv: FeeInvoice) -> None:
    """Recalculate concessions and tax of an open invoice's fee lines. A line never drops below
    what has already been paid or credited on it."""
    concs = concessions_for(db, inv.company_id, inv.student_id, inv.session_id, inv.due_date)
    fee_lines = [ln for ln in inv.lines if ln.kind == "fee"]
    dicts = [{"head_id": ln.head_id, "amount": ln.amount, "tax_rate": ln.tax_rate, "kind": "fee", "_ln": ln} for ln in fee_lines]
    price_lines(dicts, concs)
    for d in dicts:
        ln = d["_ln"]
        floor = q(ln.paid) + q(ln.credited)
        net = max(d["net"], floor)
        if net != d["net"]:  # keep what was paid: give back less concession
            d["concession"] = max(D0, q(ln.amount) + d["tax"] - net)
        ln.concession, ln.tax, ln.net, ln.concession_note = d["concession"], d["tax"], net, d["concession_note"]
    refresh_totals(inv)


# ── Invoices ─────────────────────────────────────────────────────────

def refresh_totals(inv: FeeInvoice) -> None:
    lines = list(inv.lines)
    inv.gross = sum((q(ln.amount) for ln in lines), D0)
    inv.concession = sum((q(ln.concession) for ln in lines), D0)
    inv.tax = sum((q(ln.tax) for ln in lines), D0)
    inv.total = sum((q(ln.net) for ln in lines), D0)
    inv.credited = sum((q(ln.credited) for ln in lines), D0)
    inv.paid = sum((q(ln.paid) for ln in lines), D0)
    if inv.status == "cancelled":
        return
    bal = inv.total - inv.credited - inv.paid
    inv.status = "paid" if bal <= 0 else ("partial" if inv.paid > 0 or inv.credited > 0 else "open")


def _next(db: Session, company_id: int, key: str, fallback_prefix: str, model) -> str:
    from core.numbering import next_number
    n = next_number(db, company_id, key)
    if n:
        return n
    count = db.query(func.count(model.id)).filter(model.company_id == company_id).scalar() or 0
    return f"{fallback_prefix}{count + 1:05d}"


def make_invoice(db: Session, company_id: int, student, *, title: str, issue_date: date, due_date: date,
                 lines: list[dict], enrollment=None, structure_id=None, instalment_id=None, source: str = "manual",
                 notes: str | None = None) -> FeeInvoice:
    inv = FeeInvoice(company_id=company_id, student_id=student.id, title=title[:200], issue_date=issue_date,
                     due_date=due_date, structure_id=structure_id, instalment_id=instalment_id, source=source,
                     notes=notes, created_by_name=_actor(),
                     session_id=enrollment.session_id if enrollment else None,
                     program_id=enrollment.program_id if enrollment else None,
                     section_id=enrollment.section_id if enrollment else None)
    inv.number = _next(db, company_id, "fee_invoice", "INV", FeeInvoice)
    for i, ln in enumerate(lines):
        inv.lines.append(FeeInvoiceLine(company_id=company_id, head_id=ln["head_id"], description=ln["description"][:200],
                                        amount=q(ln["amount"]), concession=q(ln.get("concession")),
                                        concession_note=ln.get("concession_note"), tax_rate=ln.get("tax_rate"),
                                        tax=q(ln.get("tax")), net=q(ln.get("net", ln["amount"])),
                                        kind=ln.get("kind", "fee"), sort_order=i + 1))
    db.add(inv)
    refresh_totals(inv)
    db.flush()
    return inv


def _enrollment(db: Session, company_id: int, student_id: int, session_id: int | None = None):
    from modules.students.models import Enrollment
    from modules.students.service import current_enrollment
    if session_id:
        return db.query(Enrollment).filter(Enrollment.company_id == company_id, Enrollment.student_id == student_id,
                                           Enrollment.session_id == session_id).first()
    return current_enrollment(db, company_id, student_id)


def generate_for_student(db: Session, company_id: int, student, *, session_id: int | None = None,
                         instalment_seqs: list[int] | None = None, issue_date: date | None = None) -> list[FeeInvoice]:
    """Make the invoices of a learner from the matching structure (skips instalments already billed)."""
    enr = _enrollment(db, company_id, student.id, session_id)
    if enr is None or enr.status not in ("enrolled",):
        return []
    s = structure_for(db, company_id, student, enr)
    if s is None:
        return []
    ensure_sibling_concession(db, company_id, student, enr.session_id)
    opted = {h for (h,) in db.query(FeeOptIn.head_id).filter(FeeOptIn.company_id == company_id,
                                                             FeeOptIn.student_id == student.id,
                                                             FeeOptIn.session_id == enr.session_id).all()}
    billed = {i for (i,) in db.query(FeeInvoice.instalment_id).filter(
        FeeInvoice.company_id == company_id, FeeInvoice.student_id == student.id,
        FeeInvoice.instalment_id.in_([x.id for x in s.instalments] or [0]), FeeInvoice.status != "cancelled").all()}
    issue_date = issue_date or today(db, company_id)
    out = []
    for inst in s.instalments:
        if instalment_seqs and inst.seq not in instalment_seqs:
            continue
        if inst.id in billed:
            continue
        lines = []
        for it in s.items:
            if it.optional and it.head_id not in opted:
                continue
            if it.billing == "once" and (it.instalment_no or 1) != inst.seq:
                continue
            lines.append({"head_id": it.head_id, "description": it.head.name, "amount": q(it.amount),
                          "tax_rate": it.head.tax_rate, "kind": "fee"})
        if not lines or sum(q(x["amount"]) for x in lines) <= 0:
            continue
        price_lines(lines, concessions_for(db, company_id, student.id, enr.session_id, inst.due_date))
        out.append(make_invoice(db, company_id, student, title=inst.name, issue_date=min(issue_date, inst.due_date),
                                due_date=inst.due_date, lines=lines, enrollment=enr, structure_id=s.id,
                                instalment_id=inst.id, source="structure"))
    return out


def generate_bulk(db: Session, company_id: int, *, session_id: int | None = None, program_id: int | None = None,
                  section_id: int | None = None, student_ids: list[int] | None = None,
                  instalment_seqs: list[int] | None = None) -> dict:
    from modules.academics.service import current_session
    from modules.students.models import Enrollment, Student
    if session_id is None:
        cur = current_session(db, company_id)
        if cur is None:
            raise BadRequest("Add an academic session first.")
        session_id = cur.id
    qy = (db.query(Student).join(Enrollment, Enrollment.student_id == Student.id)
          .filter(Enrollment.company_id == company_id, Enrollment.session_id == session_id,
                  Enrollment.status == "enrolled", Student.status == "active", Student.archived_at.is_(None)))
    if program_id:
        qy = qy.filter(Enrollment.program_id == program_id)
    if section_id:
        qy = qy.filter(Enrollment.section_id == section_id)
    if student_ids:
        qy = qy.filter(Student.id.in_(student_ids))
    created, learners, no_structure, total = 0, 0, 0, D0
    for st in qy.order_by(Student.id).all():
        learners += 1
        enr = _enrollment(db, company_id, st.id, session_id)
        if structure_for(db, company_id, st, enr) is None:
            no_structure += 1
            continue
        made = generate_for_student(db, company_id, st, session_id=session_id, instalment_seqs=instalment_seqs)
        created += len(made)
        total += sum((i.total for i in made), D0)
    audit.event(db, "fees.invoices_generated", "fee_invoices", None,
                {"session_id": session_id, "program_id": program_id, "section_id": section_id, "created": created,
                 "instalments": instalment_seqs}, company_id=company_id)
    return {"created": created, "learners": learners, "without_structure": no_structure, "amount": total}


def get_invoice(db: Session, company_id: int, uuid) -> FeeInvoice:
    inv = (db.query(FeeInvoice).options(selectinload(FeeInvoice.lines).joinedload(FeeInvoiceLine.head),
                                        joinedload(FeeInvoice.student))
           .filter(FeeInvoice.company_id == company_id, FeeInvoice.uuid == uuid).first())
    if inv is None:
        raise NotFound("Invoice")
    return inv


def add_manual_invoice(db: Session, company_id: int, student, title: str, due_date: date, lines: list[dict],
                       notes: str | None = None) -> FeeInvoice:
    enr = _enrollment(db, company_id, student.id)
    out = []
    for ln in lines:
        h = _head(db, company_id, ln["head_id"])
        amt = q(ln["amount"])
        if amt <= 0:
            raise BadRequest("Each line needs an amount above zero.")
        out.append({"head_id": h.id, "description": (ln.get("description") or h.name), "amount": amt,
                    "tax_rate": h.tax_rate, "kind": "charge"})
    if not out:
        raise BadRequest("Add at least one line.")
    price_lines(out, [])
    return make_invoice(db, company_id, student, title=title, issue_date=today(db, company_id), due_date=due_date,
                        lines=out, enrollment=enr, source="manual", notes=notes)


def add_charge(db: Session, company_id: int, student_id: int, head_code: str, amount, description: str,
               due_date: date | None = None) -> FeeInvoice:
    """Bill a learner from another module (transport, hostel, library fines …)."""
    from modules.students.models import Student
    st = db.query(Student).filter(Student.company_id == company_id, Student.id == student_id).first()
    if st is None:
        raise BadRequest("The learner was not found.")
    amount = q(amount)
    if amount <= 0:
        raise BadRequest("The amount must be above zero.")
    head = head_by_code(db, company_id, head_code, create_name=(head_code or "").replace("_", " ").title())
    t = today(db, company_id)
    lines = price_lines([{"head_id": head.id, "description": description or head.name, "amount": amount,
                          "tax_rate": head.tax_rate, "kind": "charge"}], [])
    inv = make_invoice(db, company_id, st, title=(description or head.name)[:200], issue_date=t, due_date=due_date or t,
                       lines=lines, enrollment=_enrollment(db, company_id, st.id), source="charge")
    audit.event(db, "fees.charge_added", "fee_invoices", inv.id, {"head": head.code, "amount": str(amount)},
                company_id=company_id, entity_uuid=str(inv.uuid), entity_label=inv.number)
    return inv


def cancel_invoice(db: Session, inv: FeeInvoice, reason: str) -> None:
    if inv.status == "cancelled":
        raise BadRequest("This invoice is already cancelled.")
    if not (reason or "").strip():
        raise BadRequest("Say why the invoice is cancelled.")
    if q(inv.paid) > 0:
        raise BadRequest("Money has been received against this invoice. Cancel those receipts first, "
                         "or give a credit note instead.")
    inv.status = "cancelled"
    inv.cancel_reason = reason.strip()
    inv.cancelled_at = _now()
    inv.cancelled_by_name = _actor()
    audit.event(db, "fees.invoice_cancelled", "fee_invoices", inv.id, {"reason": inv.cancel_reason},
                company_id=inv.company_id, entity_uuid=str(inv.uuid), entity_label=inv.number)
    db.flush()


def adjust_invoice(db: Session, inv: FeeInvoice, kind: str, amount, reason: str, head_id: int | None = None) -> FeeAdjustment:
    """Credit note (reduces what is due) or debit note (adds a line)."""
    if inv.status == "cancelled":
        raise BadRequest("This invoice is cancelled.")
    amount = q(amount)
    if amount <= 0:
        raise BadRequest("Enter an amount above zero.")
    if not (reason or "").strip():
        raise BadRequest("Give a reason for the adjustment.")
    cid = inv.company_id
    head = _head(db, cid, head_id)
    if kind == "credit":
        if amount > q(inv.balance):
            raise BadRequest(f"A credit note cannot be more than the balance ({q(inv.balance)}).")
        left = amount
        lines = sorted(inv.lines, key=lambda ln: (head is not None and ln.head_id != head.id, ln.sort_order))
        for ln in lines:
            take = min(q(ln.due), left)
            if take > 0:
                ln.credited = q(ln.credited) + take
                left -= take
        if left > 0:
            raise BadRequest("Nothing left to credit on this invoice.")
        number = _next(db, cid, "fee_credit_note", "CN", FeeAdjustment)
    elif kind == "debit":
        head = head or head_by_code(db, cid, "MISC")
        d = price_lines([{"head_id": head.id, "amount": amount, "tax_rate": head.tax_rate, "kind": "charge"}], [])[0]
        inv.lines.append(FeeInvoiceLine(company_id=cid, head_id=head.id, description=reason.strip()[:200],
                                        amount=amount, tax_rate=head.tax_rate, tax=d["tax"], net=d["net"],
                                        kind="debit_note", sort_order=len(inv.lines) + 1))
        number = _next(db, cid, "fee_debit_note", "DN", FeeAdjustment)
    else:
        raise BadRequest("Choose a credit or debit note.")
    adj = FeeAdjustment(company_id=cid, invoice_id=inv.id, number=number, kind=kind, head_id=head.id if head else None,
                        amount=amount, reason=reason.strip(), created_by_name=_actor())
    db.add(adj)
    db.flush()
    refresh_totals(inv)
    audit.event(db, f"fees.{kind}_note", "fee_invoices", inv.id, {"amount": str(amount), "reason": adj.reason,
                                                                  "number": number},
                company_id=cid, entity_uuid=str(inv.uuid), entity_label=inv.number)
    db.flush()
    return adj


# ── Late fee ─────────────────────────────────────────────────────────

def late_rule_for(db: Session, company_id: int, inv: FeeInvoice) -> FeeLateRule | None:
    rules = db.query(FeeLateRule).filter(FeeLateRule.company_id == company_id, FeeLateRule.active.is_(True)).all()
    specific = [r for r in rules if r.structure_id and r.structure_id == inv.structure_id]
    general = [r for r in rules if r.structure_id is None]
    return (specific or general or [None])[0]


def late_fee_amount(kind: str, amount, grace_days: int, max_amount, due_date: date, on: date) -> Decimal:
    """Late fee owed on `on` for something due on `due_date`."""
    late_days = (on - due_date).days - int(grace_days or 0)
    if late_days <= 0:
        return D0
    fee = q(amount) if kind == "fixed" else q(amount) * late_days
    if max_amount is not None and q(max_amount) > 0:
        fee = min(fee, q(max_amount))
    return q(fee)


def late_fee_due(db: Session, inv: FeeInvoice, on: date) -> Decimal:
    """Late fee still to add to an invoice (what is owed minus late fee already billed)."""
    if inv.status not in LIVE or inv.source != "structure":
        return D0
    if sum((q(ln.due) for ln in inv.lines if ln.kind != "late_fee"), D0) <= 0:
        return D0
    rule = late_rule_for(db, inv.company_id, inv)
    if rule is None:
        return D0
    owed = late_fee_amount(rule.kind, rule.amount, rule.grace_days, rule.max_amount, inv.due_date, on)
    already = sum((q(ln.net) for ln in inv.lines if ln.kind == "late_fee"), D0)
    return max(D0, owed - already)


def apply_late_fee(db: Session, inv: FeeInvoice, on: date) -> Decimal:
    fee = late_fee_due(db, inv, on)
    if fee <= 0:
        return D0
    head = head_by_code(db, inv.company_id, "LATE", "Late fee")
    inv.lines.append(FeeInvoiceLine(company_id=inv.company_id, head_id=head.id,
                                    description=f"Late fee ({(on - inv.due_date).days} days late)", amount=fee,
                                    net=fee, kind="late_fee", sort_order=len(inv.lines) + 1))
    refresh_totals(inv)
    db.flush()
    return fee


# ── Wallet ───────────────────────────────────────────────────────────

def wallet_balance(db: Session, company_id: int, student_id: int) -> Decimal:
    v = db.query(func.coalesce(func.sum(FeeWalletEntry.amount), 0)).filter(
        FeeWalletEntry.company_id == company_id, FeeWalletEntry.student_id == student_id).scalar()
    return q(v)


def _wallet(db: Session, company_id: int, student_id: int, amount: Decimal, reason: str, payment_id=None, refund_id=None):
    db.add(FeeWalletEntry(company_id=company_id, student_id=student_id, amount=q(amount), reason=reason[:200],
                          payment_id=payment_id, refund_id=refund_id, created_by_name=_actor()))


# ── Collection ───────────────────────────────────────────────────────

def open_invoices(db: Session, company_id: int, student_id: int, invoice_ids: list[int] | None = None,
                  lock: bool = False) -> list[FeeInvoice]:
    qy = (db.query(FeeInvoice).options(selectinload(FeeInvoice.lines))
          .filter(FeeInvoice.company_id == company_id, FeeInvoice.student_id == student_id,
                  FeeInvoice.status.in_(LIVE)))
    if invoice_ids is not None:
        qy = qy.filter(FeeInvoice.id.in_(invoice_ids or [0]))
    if lock:
        qy = qy.with_for_update(of=FeeInvoice)
    return qy.order_by(FeeInvoice.due_date, FeeInvoice.id).all()


def preview_dues(db: Session, company_id: int, student_id: int, on: date | None = None) -> dict:
    """Open invoices with the late fee that would be added if paid on `on`."""
    on = on or today(db, company_id)
    rows = []
    for inv in open_invoices(db, company_id, student_id):
        late = late_fee_due(db, inv, on)
        rows.append({"invoice": inv, "late_fee": late, "payable": q(inv.balance) + late})
    return {"rows": rows, "total": sum((r["payable"] for r in rows), D0),
            "late_fee": sum((r["late_fee"] for r in rows), D0), "wallet": wallet_balance(db, company_id, student_id)}


def collect(db: Session, company_id: int, student, *, modes: list[dict], paid_on: date | None = None,
            invoice_ids: list[int] | None = None, use_wallet=0, waive_late_fee: bool = False, notes: str | None = None,
            source: str = "counter", online_order_id: int | None = None, collected_by_name: str | None = None) -> FeePayment:
    """Record money received and pay the oldest dues first. Returns the receipt."""
    paid_on = paid_on or today(db, company_id)
    clean_modes = []
    for m in modes or []:
        amt = q(m.get("amount"))
        if amt <= 0:
            continue
        mode = m.get("mode") or "cash"
        if mode not in MODES:
            raise BadRequest("Unknown payment mode.")
        if mode in ("cheque", "dd") and not (m.get("reference") or "").strip():
            raise BadRequest(f"Enter the {MODES[mode].lower()} number.")
        clean_modes.append({**m, "mode": mode, "amount": amt})
    received = sum((m["amount"] for m in clean_modes), D0)
    use_wallet = q(use_wallet)
    if use_wallet < 0:
        raise BadRequest("Advance used cannot be negative.")
    if use_wallet > 0 and use_wallet > wallet_balance(db, company_id, student.id):
        raise BadRequest("The advance balance is lower than the amount to use.")
    if received + use_wallet <= 0:
        raise BadRequest("Enter the amount received.")
    invoices = open_invoices(db, company_id, student.id, invoice_ids, lock=True)
    if invoice_ids is not None and not invoices:
        raise BadRequest("The selected dues are already paid or cancelled.")
    late = D0
    if not waive_late_fee:
        for inv in invoices:
            late += apply_late_fee(db, inv, paid_on)
    pot = received + use_wallet
    pay = FeePayment(company_id=company_id, student_id=student.id, paid_on=paid_on, amount=received,
                     wallet_used=use_wallet, late_fee=late, source=source, notes=notes, online_order_id=online_order_id,
                     mode=clean_modes[0]["mode"] if len(clean_modes) == 1 else ("split" if clean_modes else "wallet"),
                     collected_by_employee_id=context.current().employee_id if source == "counter" else None,
                     collected_by_name=collected_by_name or _actor())
    pay.number = _next(db, company_id, "fee_receipt", "RCT", FeePayment)
    for m in clean_modes:
        pay.modes.append(FeePaymentMode(company_id=company_id, mode=m["mode"], amount=m["amount"],
                                        reference=(m.get("reference") or None), bank=(m.get("bank") or None),
                                        instrument_date=m.get("instrument_date")))
    db.add(pay)
    db.flush()
    allocated = D0
    for inv in invoices:
        if pot <= 0:
            break
        for ln in sorted(inv.lines, key=lambda x: x.sort_order):
            due = q(ln.due)
            if due <= 0 or pot <= 0:
                continue
            take = min(due, pot)
            ln.paid = q(ln.paid) + take
            pot -= take
            allocated += take
            db.add(FeeAllocation(company_id=company_id, payment_id=pay.id, invoice_id=inv.id, line_id=ln.id, amount=take))
        refresh_totals(inv)
    if pot > 0 and use_wallet > 0:  # did not need all the advance
        back = min(pot, use_wallet)
        use_wallet -= back
        pot -= back
        pay.wallet_used = use_wallet
    if pot > 0:
        if not fee_settings.get(db, company_id)["allow_advance"]:
            raise BadRequest(f"The amount is {pot} more than the dues.")
        pay.advance = pot
        _wallet(db, company_id, student.id, pot, f"Advance from receipt {pay.number}", payment_id=pay.id)
    if use_wallet > 0:
        _wallet(db, company_id, student.id, -use_wallet, f"Used for receipt {pay.number}", payment_id=pay.id)
    pay.allocated = allocated
    db.flush()
    audit.event(db, "fees.payment_collected", "fee_payments", pay.id,
                {"number": pay.number, "amount": str(received), "allocated": str(allocated), "late_fee": str(late),
                 "advance": str(pay.advance), "modes": [m["mode"] for m in clean_modes], "source": source},
                company_id=company_id, entity_uuid=str(pay.uuid), entity_label=pay.number)
    return pay


def get_payment(db: Session, company_id: int, uuid) -> FeePayment:
    p = (db.query(FeePayment).options(selectinload(FeePayment.modes),
                                      selectinload(FeePayment.allocations).joinedload(FeeAllocation.line)
                                      .joinedload(FeeInvoiceLine.head),
                                      selectinload(FeePayment.allocations).joinedload(FeeAllocation.invoice),
                                      joinedload(FeePayment.student))
         .filter(FeePayment.company_id == company_id, FeePayment.uuid == uuid).first())
    if p is None:
        raise NotFound("Receipt")
    return p


def cancel_payment(db: Session, pay: FeePayment, reason: str, *, bounced: bool = False, charge=None) -> FeeInvoice | None:
    """Cancel a receipt (or mark a cheque bounced): the dues it paid are open again. A bounce can
    bill a charge. Returns the charge invoice, if any."""
    if pay.status != "paid":
        raise BadRequest("This receipt is already cancelled.")
    if not (reason or "").strip():
        raise BadRequest("Say why the receipt is cancelled.")
    cid = pay.company_id
    if q(pay.advance) > 0 and wallet_balance(db, cid, pay.student_id) < q(pay.advance):
        raise BadRequest("The advance from this receipt has already been used. Cancel the later receipt first.")
    touched = {}
    for a in pay.allocations:
        if a.reversed:
            continue
        a.line.paid = q(a.line.paid) - q(a.amount)
        a.reversed = True
        touched[a.invoice_id] = a.invoice
    for inv in touched.values():
        refresh_totals(inv)
    if q(pay.advance) > 0:
        _wallet(db, cid, pay.student_id, -q(pay.advance), f"Receipt {pay.number} cancelled", payment_id=pay.id)
    if q(pay.wallet_used) > 0:
        _wallet(db, cid, pay.student_id, q(pay.wallet_used), f"Receipt {pay.number} cancelled", payment_id=pay.id)
    pay.status = "bounced" if bounced else "cancelled"
    pay.cancel_reason = reason.strip()
    pay.cancelled_at = _now()
    pay.cancelled_by_name = _actor()
    if bounced:
        for m in pay.modes:
            if m.mode in ("cheque", "dd"):
                m.status = "bounced"
    db.flush()
    charge_inv = None
    if bounced and charge is not None and q(charge) > 0:
        charge_inv = add_charge(db, cid, pay.student_id, "BOUNCE", charge, f"Cheque bounce charge ({pay.number})")
    audit.event(db, "fees.payment_bounced" if bounced else "fees.payment_cancelled", "fee_payments", pay.id,
                {"number": pay.number, "reason": pay.cancel_reason, "amount": str(pay.amount),
                 "charge": str(q(charge)) if charge else None},
                company_id=cid, entity_uuid=str(pay.uuid), entity_label=pay.number)
    return charge_inv


# ── Refunds ──────────────────────────────────────────────────────────

def deposit_refundable(db: Session, company_id: int, student_id: int) -> Decimal:
    v = (db.query(func.coalesce(func.sum(FeeInvoiceLine.paid - FeeInvoiceLine.refunded), 0))
         .join(FeeInvoice, FeeInvoice.id == FeeInvoiceLine.invoice_id)
         .join(FeeHead, FeeHead.id == FeeInvoiceLine.head_id)
         .filter(FeeInvoiceLine.company_id == company_id, FeeInvoice.student_id == student_id,
                 FeeInvoice.status != "cancelled", FeeHead.kind == "deposit").scalar())
    return q(v)


def _pending_refunds(db: Session, company_id: int, student_id: int, source: str) -> Decimal:
    v = db.query(func.coalesce(func.sum(FeeRefund.amount), 0)).filter(
        FeeRefund.company_id == company_id, FeeRefund.student_id == student_id, FeeRefund.source == source,
        FeeRefund.status.in_(("pending", "approved"))).scalar()
    return q(v)


def request_refund(db: Session, company_id: int, student, amount, reason: str, source: str = "wallet",
                   payment_id: int | None = None) -> FeeRefund:
    amount = q(amount)
    if amount <= 0:
        raise BadRequest("Enter the amount to refund.")
    if not (reason or "").strip():
        raise BadRequest("Give a reason for the refund.")
    if source == "wallet":
        avail = wallet_balance(db, company_id, student.id) - _pending_refunds(db, company_id, student.id, "wallet")
        if amount > avail:
            raise BadRequest(f"Only {q(avail)} of advance is available to refund.")
    elif source == "deposit":
        avail = deposit_refundable(db, company_id, student.id) - _pending_refunds(db, company_id, student.id, "deposit")
        if amount > avail:
            raise BadRequest(f"Only {q(avail)} of deposit is available to refund.")
    elif source == "payment":
        if not payment_id:
            raise BadRequest("Pick the receipt to refund.")
        p = db.query(FeePayment).filter(FeePayment.company_id == company_id, FeePayment.id == payment_id,
                                        FeePayment.student_id == student.id).first()
        if p is None or p.status != "paid":
            raise BadRequest("The receipt was not found or is cancelled.")
        if amount > q(p.amount):
            raise BadRequest("A refund cannot be more than the receipt.")
    else:
        raise BadRequest("Unknown refund type.")
    r = FeeRefund(company_id=company_id, student_id=student.id, source=source, payment_id=payment_id, amount=amount,
                  reason=reason.strip(), status="pending", requested_by_name=_actor())
    r.number = _next(db, company_id, "fee_refund", "RFD", FeeRefund)
    db.add(r)
    db.flush()
    from core.workflow import service as wf
    inst = wf.submit(db, company_id, "fees.refund",
                     {"refund_id": r.id, "student": student.full_name, "admission_no": student.admission_no,
                      "amount": float(amount), "source": source, "reason": r.reason, "number": r.number},
                     f"Fee refund {r.number} for {student.full_name}: {amount}", subject_type="fee_refund",
                     subject_id=r.id)
    if r.workflow_instance_id is None:
        r.workflow_instance_id = inst.id
    return r


def refund_decided(db: Session, instance, status: str) -> None:
    rid = (instance.payload or {}).get("refund_id")
    r = db.query(FeeRefund).filter(FeeRefund.company_id == instance.company_id, FeeRefund.id == rid).first()
    if r is None or r.status != "pending":
        return
    r.workflow_instance_id = instance.id
    r.status = status
    db.flush()


def pay_refund(db: Session, r: FeeRefund, mode: str, reference: str | None, paid_on: date | None = None) -> FeeRefund:
    if r.status != "approved":
        raise BadRequest("Only approved refunds can be paid out.")
    if mode not in MODES:
        raise BadRequest("Unknown payment mode.")
    cid = r.company_id
    if r.source == "wallet":
        if wallet_balance(db, cid, r.student_id) < q(r.amount):
            raise BadRequest("The advance balance is now lower than the refund.")
        _wallet(db, cid, r.student_id, -q(r.amount), f"Refund {r.number}", refund_id=r.id)
    elif r.source == "deposit":
        left = q(r.amount)
        lines = (db.query(FeeInvoiceLine).join(FeeInvoice, FeeInvoice.id == FeeInvoiceLine.invoice_id)
                 .join(FeeHead, FeeHead.id == FeeInvoiceLine.head_id)
                 .filter(FeeInvoiceLine.company_id == cid, FeeInvoice.student_id == r.student_id,
                         FeeInvoice.status != "cancelled", FeeHead.kind == "deposit").order_by(FeeInvoiceLine.id).all())
        for ln in lines:
            take = min(q(ln.paid) - q(ln.refunded), left)
            if take > 0:
                ln.refunded = q(ln.refunded) + take
                left -= take
        if left > 0:
            raise BadRequest("The deposit available is now lower than the refund.")
    r.status = "paid"
    r.mode = mode
    r.reference = reference
    r.paid_on = paid_on or today(db, cid)
    r.paid_by_name = _actor()
    db.flush()
    audit.event(db, "fees.refund_paid", "fee_refunds", r.id, {"number": r.number, "amount": str(r.amount), "mode": mode},
                company_id=cid, entity_uuid=str(r.uuid), entity_label=r.number)
    return r


# ── Dues ─────────────────────────────────────────────────────────────

def bucket(days: int) -> str:
    if days <= 0:
        return "not_due"
    if days <= 30:
        return "0_30"
    if days <= 60:
        return "31_60"
    if days <= 90:
        return "61_90"
    return "90_plus"


def dues_by_student(db: Session, company_id: int, *, session_id: int | None = None, program_id: int | None = None,
                    section_id: int | None = None, q_text: str | None = None, overdue_only: bool = False,
                    student_ids: list[int] | None = None) -> list[dict]:
    from modules.academics.models import Section
    from modules.students.models import Student
    on = today(db, company_id)
    qy = (db.query(FeeInvoice).options(joinedload(FeeInvoice.student), joinedload(FeeInvoice.section)
                                       .joinedload(Section.program))
          .filter(FeeInvoice.company_id == company_id, FeeInvoice.status.in_(LIVE)))
    if session_id:
        qy = qy.filter(FeeInvoice.session_id == session_id)
    if program_id:
        qy = qy.filter(FeeInvoice.program_id == program_id)
    if section_id:
        qy = qy.filter(FeeInvoice.section_id == section_id)
    if student_ids is not None:
        qy = qy.filter(FeeInvoice.student_id.in_(student_ids or [0]))
    if q_text:
        like = f"%{q_text.strip()}%"
        qy = qy.join(Student, Student.id == FeeInvoice.student_id).filter(or_(
            Student.first_name.ilike(like), Student.last_name.ilike(like), Student.admission_no.ilike(like),
            Student.phone.ilike(like), func.concat(Student.first_name, " ", Student.last_name).ilike(like)))
    by: dict[int, dict] = {}
    from modules.academics.service import section_label
    for inv in qy.order_by(FeeInvoice.due_date, FeeInvoice.id).all():
        bal = q(inv.balance)
        if bal <= 0:
            continue
        r = by.get(inv.student_id)
        if r is None:
            s = inv.student
            r = by[inv.student_id] = {
                "student_id": s.id, "student_uuid": str(s.uuid), "name": s.full_name, "admission_no": s.admission_no,
                "phone": s.phone, "section_id": inv.section_id,
                "section_label": section_label(inv.section) if inv.section else None,
                "program_id": inv.program_id, "due": D0, "overdue": D0, "invoices": 0, "oldest_due_date": None,
                "days_overdue": 0, "instalments": []}
        r["due"] += bal
        r["invoices"] += 1
        r["instalments"].append(inv.title)
        if inv.due_date < on:
            r["overdue"] += bal
        if r["oldest_due_date"] is None or inv.due_date < r["oldest_due_date"]:
            r["oldest_due_date"] = inv.due_date
    out = []
    for r in by.values():
        r["days_overdue"] = max(0, (on - r["oldest_due_date"]).days) if r["oldest_due_date"] else 0
        r["bucket"] = bucket(r["days_overdue"] if r["overdue"] > 0 else 0)
        if overdue_only and r["overdue"] <= 0:
            continue
        out.append(r)
    out.sort(key=lambda r: (-r["overdue"], -r["due"], r["name"]))
    return out


def student_dues(db: Session, company_id: int, student_id: int) -> dict:
    on = today(db, company_id)
    invs = open_invoices(db, company_id, student_id)
    total = sum((q(i.balance) for i in invs), D0)
    overdue = sum((q(i.balance) for i in invs if i.due_date < on), D0)
    upcoming = [i for i in invs if q(i.balance) > 0]
    nxt = upcoming[0] if upcoming else None
    return {"total": total, "overdue": overdue, "next_due_date": nxt.due_date if nxt else None,
            "next_due_amount": q(nxt.balance) if nxt else D0, "next_title": nxt.title if nxt else None,
            "invoices": len(upcoming)}


# ── Reminders ────────────────────────────────────────────────────────

def _guardian_emails(db: Session, company_id: int, student_id: int) -> list[str]:
    from modules.students.service import guardians_to_notify
    return sorted({g.email for g in guardians_to_notify(db, company_id, student_id) if g.email})


def send_reminder(db: Session, company_id: int, student, *, kind: str = "manual", key: str | None = None,
                  invoice: FeeInvoice | None = None, email: bool = True) -> int:
    """Remind a learner's family of their dues (portal notification + email). Returns recipients."""
    from modules.fees.models import FeeReminderLog
    from modules.notifications.service import notify_portal
    from modules.students.service import portal_account_ids_for_students
    dues = student_dues(db, company_id, student.id)
    if dues["total"] <= 0:
        return 0
    cur = fee_settings.currency(db, company_id)
    if invoice is not None:
        title = f"Fee reminder: {invoice.title} due {invoice.due_date:%d %b %Y}"
        body = f"{student.full_name}: {cur} {q(invoice.balance)} is due for {invoice.title}. Total dues {cur} {dues['total']}."
    else:
        title = f"Fee reminder for {student.full_name}"
        body = (f"Dues of {cur} {dues['total']}" + (f", of which {cur} {dues['overdue']} is overdue" if dues["overdue"] > 0 else "")
                + (f". Next due on {dues['next_due_date']:%d %b %Y}." if dues["next_due_date"] else "."))
    ids = portal_account_ids_for_students(db, company_id, [student.id])
    n = notify_portal(db, company_id, ids, kind="fees.reminder", title=title, body=body, link="/dashboard/my/fees") if ids else 0
    sent = int(n or 0)
    if email:
        from core.email import send_email
        for addr in _guardian_emails(db, company_id, student.id):
            send_email(addr, title, body + "\n\nYou can pay online from the parent portal.")
            sent += 1
    db.add(FeeReminderLog(company_id=company_id, student_id=student.id, invoice_id=invoice.id if invoice else None,
                          key=key or f"manual:{_now().timestamp():.0f}:{student.id}", kind=kind, recipients=sent,
                          sent_by_name=_actor() if kind == "manual" else "Automatic"))
    db.flush()
    return sent


def send_reminders(db: Session, company_id: int, student_ids: list[int]) -> dict:
    from modules.students.models import Student
    rows = db.query(Student).filter(Student.company_id == company_id, Student.id.in_(student_ids or [0])).all()
    email = fee_settings.get(db, company_id)["reminders"].get("email", True)
    families, recipients = 0, 0
    for st in rows:
        n = send_reminder(db, company_id, st, email=email)
        if n:
            families += 1
            recipients += n
    audit.event(db, "fees.reminders_sent", "fee_reminder_logs", None, {"learners": families, "recipients": recipients},
                company_id=company_id)
    return {"learners": families, "recipients": recipients}


def auto_reminders(db: Session, company_id: int, on: date) -> int:
    """Reminders N days before and after due dates (each goes out once per invoice)."""
    from modules.fees.models import FeeReminderLog
    cfg = fee_settings.get(db, company_id)["reminders"]
    if not cfg.get("enabled"):
        return 0
    done = 0
    plan = [(on + timedelta(days=d), f"auto:before:{d}") for d in cfg.get("days_before") or []] + \
           [(on - timedelta(days=d), f"auto:after:{d}") for d in cfg.get("days_after") or []]
    for due, key in plan:
        invs = (db.query(FeeInvoice).options(joinedload(FeeInvoice.student), selectinload(FeeInvoice.lines))
                .filter(FeeInvoice.company_id == company_id, FeeInvoice.status.in_(LIVE), FeeInvoice.due_date == due).all())
        for inv in invs:
            if q(inv.balance) <= 0 or inv.student.status != "active":
                continue
            if db.query(FeeReminderLog.id).filter(FeeReminderLog.invoice_id == inv.id, FeeReminderLog.key == key).first():
                continue
            if send_reminder(db, company_id, inv.student, kind="auto", key=key, invoice=inv, email=cfg.get("email", True)):
                done += 1
    return done


# ── Summary ──────────────────────────────────────────────────────────

def _collected(db: Session, company_id: int, start: date, end: date) -> Decimal:
    v = db.query(func.coalesce(func.sum(FeePayment.amount), 0)).filter(
        FeePayment.company_id == company_id, FeePayment.status == "paid", FeePayment.paid_on >= start,
        FeePayment.paid_on <= end).scalar()
    return q(v)


def summary(db: Session, company_id: int, full: bool = False) -> dict:
    from modules.academics.service import current_session
    on = today(db, company_id)
    sess = current_session(db, company_id)
    month_start = on.replace(day=1)
    out = {
        "currency": fee_settings.currency(db, company_id),
        "today": on,
        "collected_today": _collected(db, company_id, on, on),
        "collected_month": _collected(db, company_id, month_start, on),
        "collected_session": _collected(db, company_id, sess.start_date, sess.end_date) if sess else D0,
        "session_name": sess.name if sess else None,
    }
    live = (db.query(FeeInvoice.student_id, FeeInvoice.due_date, FeeInvoice.program_id,
                     (FeeInvoice.total - FeeInvoice.credited - FeeInvoice.paid).label("bal"))
            .filter(FeeInvoice.company_id == company_id, FeeInvoice.status.in_(LIVE)).all())
    out["outstanding"] = q(sum((r.bal for r in live if r.bal > 0), D0))
    out["overdue"] = q(sum((r.bal for r in live if r.bal > 0 and r.due_date < on), D0))
    out["defaulters"] = len({r.student_id for r in live if r.bal > 0 and r.due_date < on})
    if not full:
        return out
    # Billed in the session (for the collection rate)
    if sess:
        billed = db.query(func.coalesce(func.sum(FeeInvoice.total - FeeInvoice.credited), 0)).filter(
            FeeInvoice.company_id == company_id, FeeInvoice.status != "cancelled",
            FeeInvoice.session_id == sess.id).scalar()
        out["billed_session"] = q(billed)
    else:
        out["billed_session"] = D0
    # Daily trend: last 30 days
    start = on - timedelta(days=29)
    daily = dict(db.query(FeePayment.paid_on, func.sum(FeePayment.amount)).filter(
        FeePayment.company_id == company_id, FeePayment.status == "paid", FeePayment.paid_on >= start,
        FeePayment.paid_on <= on).group_by(FeePayment.paid_on).all())
    out["trend"] = [{"date": start + timedelta(days=i), "amount": q(daily.get(start + timedelta(days=i), 0))}
                    for i in range(30)]
    # Monthly over the session: billed (by due date) vs collected
    months = []
    if sess:
        cur_m = sess.start_date.replace(day=1)
        coll = defaultdict(lambda: D0)
        for d, amt in db.query(FeePayment.paid_on, FeePayment.amount).filter(
                FeePayment.company_id == company_id, FeePayment.status == "paid",
                FeePayment.paid_on >= sess.start_date, FeePayment.paid_on <= sess.end_date).all():
            coll[(d.year, d.month)] += q(amt)
        due = defaultdict(lambda: D0)
        for d, amt in db.query(FeeInvoice.due_date, FeeInvoice.total - FeeInvoice.credited).filter(
                FeeInvoice.company_id == company_id, FeeInvoice.status != "cancelled",
                FeeInvoice.session_id == sess.id).all():
            due[(d.year, d.month)] += q(amt)
        while cur_m <= sess.end_date and cur_m <= on.replace(day=1) + timedelta(days=95):
            k = (cur_m.year, cur_m.month)
            months.append({"month": cur_m, "collected": coll[k], "billed": due[k]})
            cur_m = (cur_m + timedelta(days=32)).replace(day=1)
    out["monthly"] = months
    # Dues by program
    from modules.academics.models import Program
    names = dict(db.query(Program.id, Program.name).filter(Program.company_id == company_id).all())
    by_prog = defaultdict(lambda: {"due": D0, "overdue": D0, "students": set()})
    for r in live:
        if r.bal <= 0:
            continue
        b = by_prog[r.program_id]
        b["due"] += q(r.bal)
        if r.due_date < on:
            b["overdue"] += q(r.bal)
        b["students"].add(r.student_id)
    out["by_program"] = sorted([{"program_id": pid, "program": names.get(pid, "Other"), "due": v["due"],
                                 "overdue": v["overdue"], "students": len(v["students"])}
                                for pid, v in by_prog.items()], key=lambda x: -x["due"])[:10]
    # This month by mode
    rows = (db.query(FeePaymentMode.mode, func.sum(FeePaymentMode.amount))
            .join(FeePayment, FeePayment.id == FeePaymentMode.payment_id)
            .filter(FeePayment.company_id == company_id, FeePayment.status == "paid", FeePayment.paid_on >= month_start,
                    FeePayment.paid_on <= on).group_by(FeePaymentMode.mode).all())
    out["by_mode"] = [{"mode": m, "label": MODES.get(m, m), "amount": q(a)} for m, a in rows]
    out["recent"] = [{"uuid": str(p.uuid), "number": p.number, "student": p.student.full_name, "amount": q(p.amount),
                      "mode": p.mode, "paid_on": p.paid_on, "created_at": p.created_at}
                     for p in db.query(FeePayment).options(joinedload(FeePayment.student))
                     .filter(FeePayment.company_id == company_id, FeePayment.status == "paid")
                     .order_by(FeePayment.created_at.desc()).limit(6).all()]
    return out
