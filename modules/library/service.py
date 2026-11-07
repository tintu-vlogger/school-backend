"""Library rules: borrowing limits, due dates, renewals, fines, holds and stock checks.

Functions other modules may use:
    member_summary(db, company_id, student_id)  -> {"on_loan", "overdue", "fines_due"} for a learner
    compute_fine(policy, due_on, on)            -> (days_late, amount)

Rules (all numbers come from LibraryPolicy, one per member type, editable in settings):
- issue:  the member is active, the copy is on the shelf (or kept on hold for this member),
          the title is not reference-only, open loans < max books, unpaid fines are not above
          the blocking limit and the member does not already hold another copy of the title.
- renew:  renewals left, not overdue (unless the policy allows it) and nobody else waits for
          the title while no other copy is free. The new due date counts from today.
- return: overdue days beyond the grace days x fine per day (capped by max fine) become a
          pending fine; a returned copy goes to the first waiting hold.
"""
from datetime import date, timedelta
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from core import audit
from core.context import current as current_ctx
from modules.library.models import (LibraryBook, LibraryCategory, LibraryCopy, LibraryFine, LibraryLoan,
                                    LibraryMember, LibraryPolicy, LibraryReservation, LibraryStockCheck,
                                    LibraryStockCheckItem)

ZERO = Decimal("0.00")
DEFAULT_POLICIES = [
    # member_type, name, max_books, loan_days, fine_per_day, max_renewals, block_fine_above
    ("student", "Students", 2, 14, Decimal("1.00"), 1, Decimal("100.00")),
    ("staff", "Staff", 5, 30, Decimal("0.00"), 2, None),
]
DEFAULT_CATEGORIES = [("Fiction", "800"), ("Non-fiction", None), ("Science", "500"), ("Mathematics", "510"),
                      ("History", "900"), ("Geography", "910"), ("Languages", "400"), ("Reference", "000"),
                      ("Children", None), ("Biography", "920"), ("Computers", "004"), ("Arts", "700")]
OPEN_LOAN = "issued"


def actor() -> str:
    return current_ctx().actor_name or "System"


def has_manage(db: Session, user: dict) -> bool:
    from core.security import _get_role_permissions
    if user.get("role") == "admin":
        return True
    return "library.manage" in _get_role_permissions(user["company_id"], user["role"], db, user.get("employee_id"))


def today(db: Session, company_id: int) -> date:
    from core.timeutil import today_for_company_id
    return today_for_company_id(db, company_id)


# ── Set-up ───────────────────────────────────────────────────────────

def ensure_defaults(db: Session, company_id: int) -> None:
    have = {t for (t,) in db.query(LibraryPolicy.member_type).filter(LibraryPolicy.company_id == company_id).all()}
    for mtype, name, books, days, fine, renewals, block in DEFAULT_POLICIES:
        if mtype not in have:
            db.add(LibraryPolicy(company_id=company_id, member_type=mtype, name=name, max_books=books, loan_days=days,
                                 fine_per_day=fine, max_renewals=renewals, block_fine_above=block))
    if not db.query(LibraryCategory.id).filter(LibraryCategory.company_id == company_id).first():
        for name, code in DEFAULT_CATEGORIES:
            db.add(LibraryCategory(company_id=company_id, name=name, code=code))
    db.flush()


def policy_for(db: Session, company_id: int, member_type: str) -> LibraryPolicy:
    p = db.query(LibraryPolicy).filter(LibraryPolicy.company_id == company_id,
                                       LibraryPolicy.member_type == member_type).first()
    if p is None:
        ensure_defaults(db, company_id)
        p = db.query(LibraryPolicy).filter(LibraryPolicy.company_id == company_id,
                                           LibraryPolicy.member_type == member_type).first()
    return p


def compute_fine(policy: LibraryPolicy, due_on: date, on: date) -> tuple[int, Decimal]:
    """Days late beyond the grace days and the fine for them (capped by the policy's max fine)."""
    late = (on - due_on).days
    if late <= 0:
        return 0, ZERO
    days = max(0, late - int(policy.grace_days or 0))
    amount = (Decimal(policy.fine_per_day or 0) * days).quantize(Decimal("0.01"))
    if policy.max_fine is not None and amount > Decimal(policy.max_fine):
        amount = Decimal(policy.max_fine).quantize(Decimal("0.01"))
    return late, amount


# ── Numbers ──────────────────────────────────────────────────────────

def new_accession(db: Session, company_id: int) -> str:
    from core.numbering import next_number
    for _ in range(20):
        num = next_number(db, company_id, "library_accession")
        if not num:
            n = db.query(func.count(LibraryCopy.id)).filter(LibraryCopy.company_id == company_id).scalar() or 0
            num = f"ACC{n + 1:06d}"
        if not db.query(LibraryCopy.id).filter(LibraryCopy.company_id == company_id,
                                               LibraryCopy.accession_no == num).first():
            return num
    raise HTTPException(status_code=409, detail="Could not find a free accession number. Check the number series.")


def add_copies(db: Session, company_id: int, book: LibraryBook, count: int = 1, *, accession_nos: list[str] | None = None,
               acquired_on: date | None = None, price: Decimal | None = None, source: str | None = None,
               shelf: str | None = None, condition: str | None = "new") -> list[LibraryCopy]:
    out = []
    numbers = [a.strip() for a in (accession_nos or []) if a and a.strip()]
    for i in range(max(count, len(numbers))):
        acc = numbers[i] if i < len(numbers) else new_accession(db, company_id)
        if db.query(LibraryCopy.id).filter(LibraryCopy.company_id == company_id,
                                           func.lower(LibraryCopy.accession_no) == acc.lower()).first():
            raise HTTPException(status_code=409, detail=f"Accession number {acc} is already used.")
        c = LibraryCopy(company_id=company_id, book_id=book.id, accession_no=acc, acquired_on=acquired_on,
                        price=price if price is not None else book.price, source=source, shelf=shelf,
                        condition=condition)
        db.add(c)
        db.flush()
        out.append(c)
    return out


# ── Members ──────────────────────────────────────────────────────────

def ensure_student_member(db: Session, company_id: int, student_id: int) -> LibraryMember:
    m = db.query(LibraryMember).filter(LibraryMember.company_id == company_id,
                                       LibraryMember.student_id == student_id).first()
    if m:
        return m
    from modules.students.models import Student
    s = db.query(Student).filter(Student.company_id == company_id, Student.id == student_id).first()
    if s is None:
        raise HTTPException(status_code=404, detail="Student not found")
    m = LibraryMember(company_id=company_id, member_type="student", student_id=s.id,
                      card_no=_free_card(db, company_id, s.admission_no))
    db.add(m)
    db.flush()
    return m


def ensure_staff_member(db: Session, company_id: int, employee_id: int) -> LibraryMember:
    m = db.query(LibraryMember).filter(LibraryMember.company_id == company_id,
                                       LibraryMember.employee_id == employee_id).first()
    if m:
        return m
    from modules.employees.models import Employee
    e = db.query(Employee).filter(Employee.company_id == company_id, Employee.id == employee_id).first()
    if e is None:
        raise HTTPException(status_code=404, detail="Staff member not found")
    m = LibraryMember(company_id=company_id, member_type="staff", employee_id=e.id,
                      card_no=_free_card(db, company_id, e.employee_code or f"STF{e.id:04d}"))
    db.add(m)
    db.flush()
    return m


def _free_card(db: Session, company_id: int, wanted: str) -> str:
    card, n = wanted, 1
    while db.query(LibraryMember.id).filter(LibraryMember.company_id == company_id,
                                            func.lower(LibraryMember.card_no) == card.lower()).first():
        n += 1
        card = f"{wanted}-{n}"
    return card


def sync_members(db: Session, company_id: int) -> dict:
    """Every active learner and staff member is a library member; leavers are switched off."""
    from modules.employees.models import Employee
    from modules.students.models import Student
    have_s = {m.student_id: m for m in db.query(LibraryMember).filter(LibraryMember.company_id == company_id,
                                                                      LibraryMember.student_id.isnot(None)).all()}
    have_e = {m.employee_id: m for m in db.query(LibraryMember).filter(LibraryMember.company_id == company_id,
                                                                       LibraryMember.employee_id.isnot(None)).all()}
    added = off = 0
    for s in db.query(Student).filter(Student.company_id == company_id, Student.archived_at.is_(None)).all():
        m = have_s.get(s.id)
        if m is None and s.status == "active":
            ensure_student_member(db, company_id, s.id)
            added += 1
        elif m is not None and s.status != "active" and m.active:
            m.active = False
            off += 1
    for e in db.query(Employee).filter(Employee.company_id == company_id).all():
        m = have_e.get(e.id)
        if m is None and e.active:
            ensure_staff_member(db, company_id, e.id)
            added += 1
        elif m is not None and not e.active and m.active:
            m.active = False
            off += 1
    db.flush()
    return {"added": added, "switched_off": off}


def member_name(m: LibraryMember) -> str:
    if m.student is not None:
        return m.student.full_name
    if m.employee is not None:
        return f"{m.employee.first_name} {m.employee.last_name}".strip()
    return m.card_no


def find_member(db: Session, company_id: int, code: str) -> LibraryMember:
    """A member by card number, admission number / staff code, or uuid (barcode scan input)."""
    from modules.employees.models import Employee
    from modules.students.models import Student
    code = (code or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="Scan or type a library card or admission number.")
    q = db.query(LibraryMember).options(joinedload(LibraryMember.student), joinedload(LibraryMember.employee)).filter(
        LibraryMember.company_id == company_id)
    m = q.filter(func.lower(LibraryMember.card_no) == code.lower()).first()
    if m is None and len(code) == 36:
        m = q.filter(LibraryMember.uuid == code).first()
    if m is None:
        s = db.query(Student).filter(Student.company_id == company_id,
                                     func.lower(Student.admission_no) == code.lower()).first()
        if s:
            m = ensure_student_member(db, company_id, s.id)
    if m is None:
        e = db.query(Employee).filter(Employee.company_id == company_id,
                                      or_(func.lower(Employee.employee_code) == code.lower(),
                                          func.lower(Employee.email) == code.lower())).first()
        if e:
            m = ensure_staff_member(db, company_id, e.id)
    if m is None:
        raise HTTPException(status_code=404, detail=f"No member with card {code}.")
    return m


def find_copy(db: Session, company_id: int, code: str) -> LibraryCopy:
    code = (code or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="Scan or type an accession number.")
    q = db.query(LibraryCopy).options(joinedload(LibraryCopy.book)).filter(LibraryCopy.company_id == company_id)
    c = q.filter(func.lower(LibraryCopy.accession_no) == code.lower()).first()
    if c is None and len(code) == 36:
        c = q.filter(LibraryCopy.uuid == code).first()
    if c is None:
        raise HTTPException(status_code=404, detail=f"No copy with accession number {code}.")
    return c


def open_loans(db: Session, company_id: int, member_id: int) -> list[LibraryLoan]:
    return (db.query(LibraryLoan).options(joinedload(LibraryLoan.book), joinedload(LibraryLoan.copy))
            .filter(LibraryLoan.company_id == company_id, LibraryLoan.member_id == member_id,
                    LibraryLoan.status == OPEN_LOAN).order_by(LibraryLoan.due_on).all())


def unpaid_fines(db: Session, company_id: int, member_id: int) -> Decimal:
    v = db.query(func.coalesce(func.sum(LibraryFine.amount), 0)).filter(
        LibraryFine.company_id == company_id, LibraryFine.member_id == member_id,
        LibraryFine.status == "pending").scalar()
    return Decimal(v or 0).quantize(Decimal("0.01"))


def limit_for(db: Session, member: LibraryMember) -> int:
    pol = policy_for(db, member.company_id, member.member_type)
    return member.max_books if member.max_books is not None else pol.max_books


# ── Circulation ──────────────────────────────────────────────────────

def issue(db: Session, company_id: int, member: LibraryMember, copy: LibraryCopy, on: date,
          due_on: date | None = None) -> LibraryLoan:
    pol = policy_for(db, company_id, member.member_type)
    if not member.active:
        raise HTTPException(status_code=400, detail="This membership is switched off.")
    book = copy.book
    if book.reference_only or book.kind == "reference":
        raise HTTPException(status_code=400, detail=f"{book.title} is for reference only and can't be taken out.")
    held = None
    if copy.status == "on_hold":
        held = db.query(LibraryReservation).filter(LibraryReservation.company_id == company_id,
                                                   LibraryReservation.copy_id == copy.id,
                                                   LibraryReservation.status == "ready").first()
        if held and held.member_id != member.id:
            raise HTTPException(status_code=400, detail=f"Copy {copy.accession_no} is kept for another member's hold.")
    elif copy.status != "available":
        labels = {"issued": "already issued", "lost": "marked lost", "damaged": "marked damaged",
                  "repair": "away for repair", "withdrawn": "withdrawn"}
        raise HTTPException(status_code=400, detail=f"Copy {copy.accession_no} is {labels.get(copy.status, copy.status)}.")
    loans = open_loans(db, company_id, member.id)
    limit = limit_for(db, member)
    if len(loans) >= limit:
        raise HTTPException(status_code=400, detail=f"{member_name(member)} already has {len(loans)} book(s); the limit is {limit}.")
    if any(l.book_id == book.id for l in loans):
        raise HTTPException(status_code=400, detail=f"{member_name(member)} already has a copy of {book.title}.")
    if pol.block_fine_above is not None:
        due = unpaid_fines(db, company_id, member.id)
        if due > Decimal(pol.block_fine_above):
            raise HTTPException(status_code=400, detail=f"Unpaid fines of {due} are above the limit of {pol.block_fine_above}. Settle them first.")
    # A hold placed by someone else on this title, with no other free copy, still lets the
    # librarian issue an available copy; holds are served from returns.
    loan = LibraryLoan(company_id=company_id, copy_id=copy.id, book_id=book.id, member_id=member.id, issued_on=on,
                       due_on=due_on or (on + timedelta(days=int(pol.loan_days))), issued_by=actor())
    copy.status = "issued"
    db.add(loan)
    if held is None:
        held = db.query(LibraryReservation).filter(LibraryReservation.company_id == company_id,
                                                   LibraryReservation.book_id == book.id,
                                                   LibraryReservation.member_id == member.id,
                                                   LibraryReservation.status.in_(("waiting", "ready"))).first()
    if held is not None:
        if held.copy_id and held.copy_id != copy.id:
            other = db.get(LibraryCopy, held.copy_id)
            if other is not None and other.status == "on_hold":
                other.status = "available"
                _serve_holds(db, company_id, other, on)
        held.status = "fulfilled"
        held.copy_id = copy.id
    db.flush()
    return loan


def renew(db: Session, company_id: int, loan: LibraryLoan, on: date) -> LibraryLoan:
    if loan.status != OPEN_LOAN:
        raise HTTPException(status_code=400, detail="Only books on loan can be renewed.")
    member = loan.member
    pol = policy_for(db, company_id, member.member_type)
    if loan.renew_count >= int(pol.max_renewals):
        raise HTTPException(status_code=400, detail=f"This book has been renewed {loan.renew_count} time(s); no renewals left.")
    if loan.due_on < on and not pol.renew_when_overdue:
        raise HTTPException(status_code=400, detail="The book is overdue. Return it (and settle the fine) before renewing.")
    waiting = db.query(LibraryReservation.id).filter(
        LibraryReservation.company_id == company_id, LibraryReservation.book_id == loan.book_id,
        LibraryReservation.member_id != member.id, LibraryReservation.status == "waiting").first()
    if waiting:
        free = db.query(LibraryCopy.id).filter(LibraryCopy.company_id == company_id,
                                               LibraryCopy.book_id == loan.book_id,
                                               LibraryCopy.status == "available").first()
        if not free:
            raise HTTPException(status_code=400, detail="Someone else is waiting for this title, so it can't be renewed.")
    if loan.due_on < on:
        # Renewing an overdue book (when allowed) settles the days late as a fine first.
        _late, amount = compute_fine(pol, loan.due_on, on)
        if amount > 0:
            db.add(LibraryFine(company_id=company_id, member_id=member.id, loan_id=loan.id, kind="overdue",
                               amount=amount, days=(on - loan.due_on).days))
    loan.due_on = on + timedelta(days=int(pol.loan_days))
    loan.renew_count += 1
    db.flush()
    return loan


def return_loan(db: Session, company_id: int, loan: LibraryLoan, on: date, *, condition: str | None = None,
                damage_fine: Decimal | None = None, waive_overdue: bool = False, note: str | None = None) -> dict:
    if loan.status != OPEN_LOAN:
        raise HTTPException(status_code=400, detail="This book has already been returned.")
    pol = policy_for(db, company_id, loan.member.member_type)
    days, amount = compute_fine(pol, loan.due_on, on)
    loan.status = "returned"
    loan.returned_on = on
    loan.returned_by = actor()
    loan.return_condition = condition
    if note:
        loan.notes = note
    fines = []
    if amount > 0:
        f = LibraryFine(company_id=company_id, member_id=loan.member_id, loan_id=loan.id, kind="overdue",
                        amount=amount, days=days)
        if waive_overdue:
            f.status, f.reason, f.settled_on, f.settled_by = "waived", "Waived at return", on, actor()
        db.add(f)
        fines.append(f)
    copy = loan.copy
    if condition == "damaged":
        copy.status = "damaged"
        copy.condition = "poor"
        if damage_fine and damage_fine > 0:
            f = LibraryFine(company_id=company_id, member_id=loan.member_id, loan_id=loan.id, kind="damaged",
                            amount=Decimal(damage_fine).quantize(Decimal("0.01")), reason=note)
            db.add(f)
            fines.append(f)
    else:
        if condition:
            copy.condition = condition
        copy.status = "available"
        _serve_holds(db, company_id, copy, on)
    db.flush()
    return {"loan": loan, "late_days": days, "fines": fines, "copy_status": copy.status}


def mark_lost(db: Session, company_id: int, loan: LibraryLoan, on: date, amount: Decimal | None = None) -> LibraryFine | None:
    if loan.status != OPEN_LOAN:
        raise HTTPException(status_code=400, detail="Only books on loan can be marked lost.")
    pol = policy_for(db, company_id, loan.member.member_type)
    days, overdue = compute_fine(pol, loan.due_on, on)
    price = amount if amount is not None else (loan.copy.price or loan.book.price or ZERO)
    loan.status = "lost"
    loan.returned_on = on
    loan.copy.status = "lost"
    fine = None
    total = Decimal(price or 0) + overdue
    if total > 0:
        fine = LibraryFine(company_id=company_id, member_id=loan.member_id, loan_id=loan.id, kind="lost",
                           amount=total.quantize(Decimal("0.01")), days=days or None,
                           reason=f"Replacement {Decimal(price or 0):.2f}" + (f" + overdue {overdue:.2f}" if overdue else ""))
        db.add(fine)
    audit.event(db, "library.copy_lost", "library_copies", loan.copy_id, {"accession_no": loan.copy.accession_no},
                entity_label=loan.book.title)
    db.flush()
    return fine


# ── Fines ────────────────────────────────────────────────────────────

def fees_available() -> bool:
    try:
        from modules.fees import service as fees  # noqa: WPS433
    except Exception:
        return False
    return callable(getattr(fees, "add_charge", None))


def bill_via_fees(db: Session, company_id: int, student_id: int, head_code: str, amount: Decimal, description: str,
                  due_date: date | None) -> str | None:
    """Raise a charge in the fees module when it is installed; returns its reference or None."""
    try:
        from modules.fees import service as fees  # noqa: WPS433
    except Exception:
        return None
    fn = getattr(fees, "add_charge", None)
    if not callable(fn):
        return None
    with db.begin_nested():
        res = fn(db, company_id, student_id, head_code, amount, description, due_date)
    ref = getattr(res, "uuid", None) or getattr(res, "id", None) or (res.get("uuid") or res.get("id") if isinstance(res, dict) else res)
    return str(ref) if ref else "billed"


def settle_fine(db: Session, company_id: int, fine: LibraryFine, action: str, on: date, *, reason: str | None = None) -> LibraryFine:
    if fine.status != "pending":
        raise HTTPException(status_code=400, detail="This fine is already settled.")
    if action == "pay":
        fine.status = "paid"
        fine.reason = reason or fine.reason
    elif action == "waive":
        if not (reason or "").strip():
            raise HTTPException(status_code=400, detail="Give a reason for waiving the fine.")
        fine.status, fine.reason = "waived", reason.strip()
    elif action == "bill":
        m = fine.member
        if m.student_id is None:
            raise HTTPException(status_code=400, detail="Only fines of learners can be billed through fees.")
        if not fees_available():
            raise HTTPException(status_code=400, detail="Fee billing isn't available yet. Collect the fine at the desk or waive it.")
        title = fine.loan.book.title if fine.loan and fine.loan.book else "library"
        ref = bill_via_fees(db, company_id, m.student_id, "LIBRARY_FINE", Decimal(fine.amount),
                            f"Library fine ({fine.kind}): {title}", on + timedelta(days=15))
        fine.status, fine.fee_ref = "billed", ref
    else:
        raise HTTPException(status_code=400, detail="Unknown action.")
    fine.settled_on, fine.settled_by = on, actor()
    audit.event(db, f"library.fine_{fine.status}", "library_fines", fine.id,
                {"amount": str(fine.amount), "reason": fine.reason}, entity_label=member_name(fine.member))
    db.flush()
    return fine


# ── Holds ────────────────────────────────────────────────────────────

def reserve(db: Session, company_id: int, member: LibraryMember, book: LibraryBook, on: date) -> LibraryReservation:
    pol = policy_for(db, company_id, member.member_type)
    if not member.active:
        raise HTTPException(status_code=400, detail="This membership is switched off.")
    if book.reference_only or not book.active:
        raise HTTPException(status_code=400, detail="This title can't be reserved.")
    mine = db.query(LibraryReservation).filter(LibraryReservation.company_id == company_id,
                                               LibraryReservation.member_id == member.id,
                                               LibraryReservation.status.in_(("waiting", "ready"))).all()
    if any(r.book_id == book.id for r in mine):
        raise HTTPException(status_code=400, detail="There is already a reservation for this title.")
    if len(mine) >= int(pol.max_reservations):
        raise HTTPException(status_code=400, detail=f"You can reserve up to {pol.max_reservations} title(s) at a time.")
    if any(l.book_id == book.id for l in open_loans(db, company_id, member.id)):
        raise HTTPException(status_code=400, detail="This title is already on loan to this member.")
    r = LibraryReservation(company_id=company_id, book_id=book.id, member_id=member.id, created_by=actor())
    db.add(r)
    db.flush()
    # A copy on the shelf right now is kept aside at once.
    free = db.query(LibraryCopy).filter(LibraryCopy.company_id == company_id, LibraryCopy.book_id == book.id,
                                        LibraryCopy.status == "available").order_by(LibraryCopy.id).first()
    if free is not None:
        _serve_holds(db, company_id, free, on)
    return r


def cancel_reservation(db: Session, company_id: int, r: LibraryReservation, on: date) -> None:
    if r.status not in ("waiting", "ready"):
        raise HTTPException(status_code=400, detail="This reservation is already closed.")
    r.status = "cancelled"
    if r.copy_id:
        c = db.get(LibraryCopy, r.copy_id)
        if c is not None and c.status == "on_hold":
            c.status = "available"
            db.flush()
            _serve_holds(db, company_id, c, on)
    db.flush()


def _serve_holds(db: Session, company_id: int, copy: LibraryCopy, on: date) -> None:
    """Keep a copy that is back on the shelf for the first waiting hold on its title."""
    if copy.status != "available":
        return
    r = (db.query(LibraryReservation).filter(LibraryReservation.company_id == company_id,
                                             LibraryReservation.book_id == copy.book_id,
                                             LibraryReservation.status == "waiting")
         .order_by(LibraryReservation.reserved_at, LibraryReservation.id).first())
    if r is None:
        return
    pol = policy_for(db, company_id, r.member.member_type)
    r.status, r.copy_id, r.ready_on = "ready", copy.id, on
    r.expires_on = on + timedelta(days=int(pol.hold_days))
    copy.status = "on_hold"
    db.flush()
    _notify_member(db, company_id, r.member, kind="library.hold_ready", title="A reserved book is ready",
                   body=f"{copy.book.title} is kept at the library desk until {r.expires_on:%d %b}.")


def expire_holds(db: Session, company_id: int | None = None) -> int:
    q = db.query(LibraryReservation).filter(LibraryReservation.status == "ready",
                                            LibraryReservation.expires_on.isnot(None))
    if company_id:
        q = q.filter(LibraryReservation.company_id == company_id)
    n = 0
    for r in q.all():
        on = today(db, r.company_id)
        if r.expires_on >= on:
            continue
        r.status = "expired"
        if r.copy_id:
            c = db.get(LibraryCopy, r.copy_id)
            if c is not None and c.status == "on_hold":
                c.status = "available"
                db.flush()
                _serve_holds(db, r.company_id, c, on)
        n += 1
    db.flush()
    return n


def _notify_member(db: Session, company_id: int, m: LibraryMember, *, kind: str, title: str, body: str) -> None:
    try:
        if m.student_id:
            from modules.notifications.service import notify_portal
            from modules.students.service import portal_account_ids_for_students
            ids = portal_account_ids_for_students(db, company_id, [m.student_id])
            if ids:
                notify_portal(db, company_id, ids, kind=kind, title=title, body=body, link="/dashboard/my/library")
        elif m.employee_id:
            from modules.notifications.service import notify
            notify(db, company_id, [m.employee_id], kind=kind, title=title, body=body)
    except Exception:  # a notification problem never blocks the desk
        pass


def send_overdue_reminders(db: Session) -> int:
    """Once a day, tell members (and parents) about books past their due date."""
    n = 0
    rows = (db.query(LibraryLoan).options(joinedload(LibraryLoan.book), joinedload(LibraryLoan.member))
            .filter(LibraryLoan.status == OPEN_LOAN).all())
    for loan in rows:
        on = today(db, loan.company_id)
        if loan.due_on >= on or loan.reminded_on == on:
            continue
        loan.reminded_on = on
        _notify_member(db, loan.company_id, loan.member, kind="library.overdue", title="Library book overdue",
                       body=f"{loan.book.title} was due on {loan.due_on:%d %b %Y}. Please return it.")
        n += 1
    db.flush()
    return n


# ── Stock verification ───────────────────────────────────────────────

def stock_scope(db: Session, check: LibraryStockCheck):
    q = db.query(LibraryCopy).join(LibraryBook, LibraryBook.id == LibraryCopy.book_id).filter(
        LibraryCopy.company_id == check.company_id, LibraryCopy.status.in_(("available", "on_hold", "damaged", "repair")))
    if check.scope_shelf:
        q = q.filter(func.coalesce(LibraryCopy.shelf, LibraryBook.shelf) == check.scope_shelf)
    return q


def stock_scan(db: Session, check: LibraryStockCheck, code: str) -> dict:
    if check.status != "open":
        raise HTTPException(status_code=400, detail="This stock check is closed.")
    copy = find_copy(db, check.company_id, code)
    note = None
    if copy.status == "issued":
        note = "This copy is on loan; return it first if it is on the shelf."
    elif copy.status in ("lost", "withdrawn"):
        note = f"This copy was marked {copy.status}; it is back on the shelf now."
        copy.status = "available"
    seen = db.query(LibraryStockCheckItem.id).filter(LibraryStockCheckItem.check_id == check.id,
                                                     LibraryStockCheckItem.copy_id == copy.id).first()
    if not seen:
        db.add(LibraryStockCheckItem(company_id=check.company_id, check_id=check.id, copy_id=copy.id))
    copy.last_seen_on = check.started_on
    db.flush()
    return {"accession_no": copy.accession_no, "title": copy.book.title, "already_scanned": bool(seen), "note": note}


def stock_counts(db: Session, check: LibraryStockCheck) -> dict:
    expected = stock_scope(db, check).count()
    seen = db.query(func.count(LibraryStockCheckItem.id)).filter(LibraryStockCheckItem.check_id == check.id).scalar() or 0
    return {"expected": expected, "scanned": seen, "missing": max(0, expected - seen) if check.status == "open"
            else len((check.result or {}).get("missing", []))}


def stock_missing(db: Session, check: LibraryStockCheck) -> list[LibraryCopy]:
    seen = db.query(LibraryStockCheckItem.copy_id).filter(LibraryStockCheckItem.check_id == check.id)
    return stock_scope(db, check).filter(~LibraryCopy.id.in_(seen)).options(joinedload(LibraryCopy.book)).order_by(
        LibraryCopy.accession_no).all()


def close_stock_check(db: Session, check: LibraryStockCheck, on: date, mark_lost_missing: bool) -> dict:
    if check.status != "open":
        raise HTTPException(status_code=400, detail="This stock check is already closed.")
    missing = stock_missing(db, check)
    counts = stock_counts(db, check)
    if mark_lost_missing:
        for c in missing:
            c.status = "lost"
            c.notes = ((c.notes or "") + f"\nNot found in stock check {check.name} ({on:%d %b %Y}).").strip()
    check.status, check.closed_on = "closed", on
    check.result = {"expected": counts["expected"], "scanned": counts["scanned"], "marked_lost": mark_lost_missing,
                    "missing": [{"accession_no": c.accession_no, "title": c.book.title} for c in missing]}
    audit.event(db, "library.stock_check_closed", "library_stock_checks", check.id,
                {"missing": len(missing), "marked_lost": mark_lost_missing}, entity_label=check.name)
    db.flush()
    return check.result


# ── Summaries ────────────────────────────────────────────────────────

def member_summary(db: Session, company_id: int, student_id: int) -> dict:
    m = db.query(LibraryMember).filter(LibraryMember.company_id == company_id,
                                       LibraryMember.student_id == student_id).first()
    if m is None:
        return {"on_loan": 0, "overdue": 0, "fines_due": "0.00"}
    on = today(db, company_id)
    loans = open_loans(db, company_id, m.id)
    return {"on_loan": len(loans), "overdue": sum(1 for l in loans if l.due_on < on),
            "fines_due": str(unpaid_fines(db, company_id, m.id))}
