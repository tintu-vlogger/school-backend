"""Library API: catalogue, copies and labels, members, circulation desk, fines, holds, e-resources,
stock checks, reports, and the parent / student portal (/library/portal/...)."""
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from core.crud import by_id_or_400, crud_router, get_or_404
from core.db import get_db
from core.pagination import PageParams, paginate
from core.security import get_current_user, require_permission
from modules.library import service
from modules.library.models import (LibraryBook, LibraryCategory, LibraryCopy, LibraryEResource, LibraryFine,
                                    LibraryLoan, LibraryMember, LibraryPolicy, LibraryReservation, LibraryStockCheck)

router = APIRouter(prefix="/library", tags=["Library"])
VIEW, MANAGE, CIRC = "library.view", "library.manage", "library.circulate"


def _cid(user: dict) -> int:
    return user["company_id"]


def _money(v) -> str | None:
    return None if v is None else f"{Decimal(v):.2f}"


# ── Set-up lists ─────────────────────────────────────────────────────

class CategoryIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    code: str | None = Field(None, max_length=20)
    description: str | None = None
    active: bool = True


class CategoryPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    code: str | None = Field(None, max_length=20)
    description: str | None = None
    active: bool | None = None


router.include_router(crud_router(
    model=LibraryCategory, prefix="/categories", tag="Library", create_schema=CategoryIn, update_schema=CategoryPatch,
    view_perm=VIEW, manage_perm=MANAGE, search_fields=("name", "code"), order_by=("name",), label="Category",
    out=lambda r: {"id": r.id, "uuid": str(r.uuid), "name": r.name, "code": r.code, "description": r.description,
                   "active": r.active}))


class EResourceIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    url: str = Field(..., min_length=4, max_length=2000, pattern=r"^https?://")
    kind: str = Field("ebook", pattern="^(ebook|journal|database|video|website)$")
    subject: str | None = Field(None, max_length=120)
    description: str | None = None
    access_note: str | None = Field(None, max_length=300)
    audience: str = Field("all", pattern="^(all|students|staff)$")
    active: bool = True


class EResourcePatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=300)
    url: str | None = Field(None, min_length=4, max_length=2000, pattern=r"^https?://")
    kind: str | None = Field(None, pattern="^(ebook|journal|database|video|website)$")
    subject: str | None = None
    description: str | None = None
    access_note: str | None = None
    audience: str | None = Field(None, pattern="^(all|students|staff)$")
    active: bool | None = None


def _eres_out(r: LibraryEResource) -> dict:
    return {"id": r.id, "uuid": str(r.uuid), "title": r.title, "url": r.url, "kind": r.kind, "subject": r.subject,
            "description": r.description, "access_note": r.access_note, "audience": r.audience, "active": r.active}


router.include_router(crud_router(
    model=LibraryEResource, prefix="/eresources", tag="Library", create_schema=EResourceIn,
    update_schema=EResourcePatch, view_perm=VIEW, manage_perm=MANAGE, search_fields=("title", "subject"),
    order_by=("title",), label="E-resource", out=_eres_out))


def _policy_out(p: LibraryPolicy) -> dict:
    return {"id": p.id, "uuid": str(p.uuid), "member_type": p.member_type, "name": p.name, "max_books": p.max_books,
            "loan_days": p.loan_days, "fine_per_day": _money(p.fine_per_day), "grace_days": p.grace_days,
            "max_fine": _money(p.max_fine), "max_renewals": p.max_renewals,
            "renew_when_overdue": p.renew_when_overdue, "hold_days": p.hold_days,
            "max_reservations": p.max_reservations, "block_fine_above": _money(p.block_fine_above)}


class PolicyPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    max_books: int | None = Field(None, ge=0, le=100)
    loan_days: int | None = Field(None, ge=1, le=365)
    fine_per_day: Decimal | None = Field(None, ge=0, max_digits=10, decimal_places=2)
    grace_days: int | None = Field(None, ge=0, le=60)
    max_fine: Decimal | None = Field(None, ge=0, max_digits=10, decimal_places=2)
    max_renewals: int | None = Field(None, ge=0, le=20)
    renew_when_overdue: bool | None = None
    hold_days: int | None = Field(None, ge=1, le=30)
    max_reservations: int | None = Field(None, ge=0, le=20)
    block_fine_above: Decimal | None = Field(None, ge=0, max_digits=10, decimal_places=2)


@router.get("/policies")
def list_policies(db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    service.ensure_defaults(db, _cid(user))
    db.commit()
    rows = db.query(LibraryPolicy).filter(LibraryPolicy.company_id == _cid(user)).order_by(LibraryPolicy.member_type.desc()).all()
    return {"items": [_policy_out(p) for p in rows], "fees_billing": service.fees_available()}


@router.put("/policies/{uuid}")
def update_policy(uuid: UUID, payload: PolicyPatch, db: Session = Depends(get_db),
                  user: dict = Depends(require_permission(MANAGE))):
    p = get_or_404(db, LibraryPolicy, _cid(user), uuid, "Rule")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(p, k, v)
    db.commit()
    return _policy_out(p)


# ── Catalogue ────────────────────────────────────────────────────────

def _copy_out(c: LibraryCopy, loan: LibraryLoan | None = None) -> dict:
    out = {"id": c.id, "uuid": str(c.uuid), "accession_no": c.accession_no, "status": c.status, "condition": c.condition,
           "shelf": c.shelf or (c.book.shelf if c.book else None), "acquired_on": c.acquired_on, "source": c.source,
           "price": _money(c.price), "last_seen_on": c.last_seen_on, "notes": c.notes,
           "book_uuid": str(c.book.uuid) if c.book else None, "title": c.book.title if c.book else None,
           "authors": c.book.authors if c.book else None}
    if loan is not None:
        out["loan"] = {"uuid": str(loan.uuid), "member": service.member_name(loan.member),
                       "member_uuid": str(loan.member.uuid), "due_on": loan.due_on, "issued_on": loan.issued_on}
    return out


def _book_out(b: LibraryBook, counts: dict | None = None) -> dict:
    c = counts or {}
    return {"id": b.id, "uuid": str(b.uuid), "title": b.title, "subtitle": b.subtitle, "authors": b.authors,
            "isbn": b.isbn, "publisher": b.publisher, "edition": b.edition, "year": b.year,
            "category_id": b.category_id, "category_name": b.category.name if b.category else None,
            "subject": b.subject, "language": b.language, "pages": b.pages, "shelf": b.shelf, "cover_url": b.cover_url,
            "description": b.description, "kind": b.kind, "price": _money(b.price), "reference_only": b.reference_only,
            "tags": b.tags or [], "active": b.active, "copies": c.get("total", 0), "available": c.get("available", 0),
            "created_at": b.created_at}


def _copy_counts(db: Session, cid: int, book_ids: list[int]) -> dict[int, dict]:
    out: dict[int, dict] = {i: {"total": 0, "available": 0} for i in book_ids}
    if not book_ids:
        return out
    for bid, status, n in db.query(LibraryCopy.book_id, LibraryCopy.status, func.count(LibraryCopy.id)).filter(
            LibraryCopy.company_id == cid, LibraryCopy.book_id.in_(book_ids)).group_by(LibraryCopy.book_id, LibraryCopy.status):
        if status in ("withdrawn", "lost"):
            continue
        out[bid]["total"] += n
        if status == "available":
            out[bid]["available"] += n
    return out


def _book_query(db: Session, cid: int, q: str | None, category_id: int | None, kind: str | None,
                availability: str | None, active: bool | None = True):
    query = db.query(LibraryBook).options(joinedload(LibraryBook.category)).filter(LibraryBook.company_id == cid)
    if active is not None:
        query = query.filter(LibraryBook.active.is_(active))
    if q:
        like = f"%{q.strip()}%"
        acc = db.query(LibraryCopy.book_id).filter(LibraryCopy.company_id == cid, LibraryCopy.accession_no.ilike(like))
        query = query.filter(or_(LibraryBook.title.ilike(like), LibraryBook.authors.ilike(like),
                                 LibraryBook.isbn.ilike(like), LibraryBook.subject.ilike(like),
                                 LibraryBook.publisher.ilike(like), LibraryBook.id.in_(acc)))
    if category_id:
        query = query.filter(LibraryBook.category_id == category_id)
    if kind:
        query = query.filter(LibraryBook.kind == kind)
    if availability == "available":
        query = query.filter(LibraryBook.id.in_(db.query(LibraryCopy.book_id).filter(
            LibraryCopy.company_id == cid, LibraryCopy.status == "available")))
    elif availability == "out":
        query = query.filter(~LibraryBook.id.in_(db.query(LibraryCopy.book_id).filter(
            LibraryCopy.company_id == cid, LibraryCopy.status == "available")))
    return query.order_by(LibraryBook.title)


def _paged_books(db: Session, cid: int, query, params: PageParams) -> dict:
    page = paginate(query, params)
    counts = _copy_counts(db, cid, [b.id for b in page["items"]])
    page["items"] = [_book_out(b, counts.get(b.id)) for b in page["items"]]
    return page


@router.get("/books")
def list_books(q: str | None = None, category_id: int | None = None, kind: str | None = None,
               availability: str | None = None, active: bool | None = Query(True), params: PageParams = Depends(),
               db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    return _paged_books(db, _cid(user), _book_query(db, _cid(user), q, category_id, kind, availability, active), params)


class BookIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    subtitle: str | None = Field(None, max_length=300)
    authors: str | None = Field(None, max_length=300)
    isbn: str | None = Field(None, max_length=20)
    publisher: str | None = Field(None, max_length=200)
    edition: str | None = Field(None, max_length=60)
    year: int | None = Field(None, ge=1000, le=2200)
    category_id: int | None = None
    subject: str | None = Field(None, max_length=120)
    language: str | None = Field(None, max_length=40)
    pages: int | None = Field(None, ge=1, le=100000)
    shelf: str | None = Field(None, max_length=60)
    cover_url: str | None = Field(None, max_length=2000)
    description: str | None = None
    kind: str = Field("book", pattern="^(book|reference|magazine|journal|cd|other)$")
    price: Decimal | None = Field(None, ge=0, max_digits=10, decimal_places=2)
    reference_only: bool = False
    tags: list[str] = []


class BookCreate(BookIn):
    copies: int = Field(1, ge=0, le=200)
    accession_nos: list[str] = []
    acquired_on: date | None = None
    source: str | None = Field(None, max_length=120)


class BookPatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=300)
    subtitle: str | None = None
    authors: str | None = None
    isbn: str | None = None
    publisher: str | None = None
    edition: str | None = None
    year: int | None = Field(None, ge=1000, le=2200)
    category_id: int | None = None
    subject: str | None = None
    language: str | None = None
    pages: int | None = None
    shelf: str | None = None
    cover_url: str | None = None
    description: str | None = None
    kind: str | None = Field(None, pattern="^(book|reference|magazine|journal|cd|other)$")
    price: Decimal | None = Field(None, ge=0, max_digits=10, decimal_places=2)
    reference_only: bool | None = None
    tags: list[str] | None = None
    active: bool | None = None


def _clean_isbn(v: str | None) -> str | None:
    if not v:
        return None
    s = "".join(ch for ch in v if ch.isdigit() or ch in "Xx").upper()
    return s or None


@router.post("/books", status_code=201)
def create_book(payload: BookCreate, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    data = payload.model_dump(exclude={"copies", "accession_nos", "acquired_on", "source"})
    by_id_or_400(db, LibraryCategory, cid, data.get("category_id"), "Category")
    data["isbn"] = _clean_isbn(data.get("isbn"))
    b = LibraryBook(company_id=cid, **data)
    db.add(b)
    db.flush()
    service.add_copies(db, cid, b, payload.copies, accession_nos=payload.accession_nos, acquired_on=payload.acquired_on,
                       source=payload.source)
    db.commit()
    return _book_out(b, _copy_counts(db, cid, [b.id]).get(b.id))


@router.get("/books/{uuid}")
def get_book(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    cid = _cid(user)
    b = get_or_404(db, LibraryBook, cid, uuid, "Book")
    loans = {l.copy_id: l for l in db.query(LibraryLoan).options(joinedload(LibraryLoan.member).joinedload(LibraryMember.student),
                                                                  joinedload(LibraryLoan.member).joinedload(LibraryMember.employee))
             .filter(LibraryLoan.company_id == cid, LibraryLoan.book_id == b.id, LibraryLoan.status == "issued").all()}
    holds = (db.query(LibraryReservation).filter(LibraryReservation.company_id == cid, LibraryReservation.book_id == b.id,
                                                 LibraryReservation.status.in_(("waiting", "ready")))
             .order_by(LibraryReservation.reserved_at).all())
    times = db.query(func.count(LibraryLoan.id)).filter(LibraryLoan.company_id == cid, LibraryLoan.book_id == b.id).scalar()
    out = _book_out(b, _copy_counts(db, cid, [b.id]).get(b.id))
    out["copy_list"] = [_copy_out(c, loans.get(c.id)) for c in b.copies]
    out["holds"] = [_hold_out(r) for r in holds]
    out["times_borrowed"] = times or 0
    return out


@router.put("/books/{uuid}")
def update_book(uuid: UUID, payload: BookPatch, db: Session = Depends(get_db),
                user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    b = get_or_404(db, LibraryBook, cid, uuid, "Book")
    data = payload.model_dump(exclude_unset=True)
    if "category_id" in data:
        by_id_or_400(db, LibraryCategory, cid, data["category_id"], "Category")
    if "isbn" in data:
        data["isbn"] = _clean_isbn(data["isbn"])
    for k, v in data.items():
        setattr(b, k, v)
    db.commit()
    return _book_out(b, _copy_counts(db, cid, [b.id]).get(b.id))


@router.delete("/books/{uuid}")
def archive_book(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    b = get_or_404(db, LibraryBook, cid, uuid, "Book")
    if db.query(LibraryLoan.id).filter(LibraryLoan.company_id == cid, LibraryLoan.book_id == b.id,
                                       LibraryLoan.status == "issued").first():
        raise HTTPException(status_code=400, detail="Copies of this title are on loan. Take them back first.")
    b.active = False
    db.commit()
    return {"detail": "Removed from the catalogue"}


@router.get("/isbn/{isbn}")
def isbn_lookup(isbn: str, user: dict = Depends(require_permission(MANAGE))):
    """Fill a new title from Open Library (needs internet; returns 404 when nothing is found)."""
    import httpx
    code = _clean_isbn(isbn)
    if not code or len(code) not in (10, 13):
        raise HTTPException(status_code=400, detail="An ISBN has 10 or 13 digits.")
    try:
        r = httpx.get("https://openlibrary.org/api/books", params={"bibkeys": f"ISBN:{code}", "format": "json",
                                                                    "jscmd": "data"}, timeout=6.0)
        data = r.json().get(f"ISBN:{code}") if r.status_code == 200 else None
    except Exception:
        raise HTTPException(status_code=503, detail="Couldn't reach Open Library. Fill in the details by hand.")
    if not data:
        raise HTTPException(status_code=404, detail="No book found for this ISBN. Fill in the details by hand.")
    year = None
    for tok in str(data.get("publish_date") or "").replace(",", " ").split():
        if tok.isdigit() and len(tok) == 4:
            year = int(tok)
    return {
        "isbn": code, "title": data.get("title"), "subtitle": data.get("subtitle"),
        "authors": ", ".join(a.get("name", "") for a in data.get("authors") or [] if a.get("name")) or None,
        "publisher": ", ".join(p.get("name", "") for p in data.get("publishers") or [] if p.get("name")) or None,
        "year": year, "pages": data.get("number_of_pages"),
        "subject": ", ".join(s.get("name", "") for s in (data.get("subjects") or [])[:3]) or None,
        "cover_url": (data.get("cover") or {}).get("medium") or (data.get("cover") or {}).get("large"),
    }


# ── Copies and labels ────────────────────────────────────────────────

class CopiesIn(BaseModel):
    count: int = Field(1, ge=1, le=200)
    accession_nos: list[str] = []
    acquired_on: date | None = None
    price: Decimal | None = Field(None, ge=0, max_digits=10, decimal_places=2)
    source: str | None = Field(None, max_length=120)
    shelf: str | None = Field(None, max_length=60)


@router.post("/books/{uuid}/copies", status_code=201)
def add_copies(uuid: UUID, payload: CopiesIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    b = get_or_404(db, LibraryBook, cid, uuid, "Book")
    rows = service.add_copies(db, cid, b, payload.count, accession_nos=payload.accession_nos,
                              acquired_on=payload.acquired_on, price=payload.price, source=payload.source,
                              shelf=payload.shelf)
    db.commit()
    return [_copy_out(c) for c in rows]


class CopyPatch(BaseModel):
    status: str | None = Field(None, pattern="^(available|damaged|repair|withdrawn|lost)$")
    condition: str | None = Field(None, pattern="^(new|good|fair|poor)$")
    shelf: str | None = Field(None, max_length=60)
    price: Decimal | None = Field(None, ge=0, max_digits=10, decimal_places=2)
    source: str | None = None
    acquired_on: date | None = None
    notes: str | None = None


@router.put("/copies/{uuid}")
def update_copy(uuid: UUID, payload: CopyPatch, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    c = get_or_404(db, LibraryCopy, cid, uuid, "Copy")
    data = payload.model_dump(exclude_unset=True)
    if "status" in data and c.status in ("issued", "on_hold") and data["status"] != c.status:
        raise HTTPException(status_code=400, detail="This copy is on loan or kept for a hold; return it or cancel the hold first.")
    for k, v in data.items():
        setattr(c, k, v)
    if data.get("status") == "available":
        db.flush()
        service._serve_holds(db, cid, c, service.today(db, cid))
    db.commit()
    return _copy_out(c)


@router.get("/copies")
def list_copies(q: str | None = None, status: str | None = None, book_uuid: UUID | None = None,
                params: PageParams = Depends(), db: Session = Depends(get_db),
                user: dict = Depends(require_permission(VIEW))):
    cid = _cid(user)
    query = db.query(LibraryCopy).join(LibraryBook, LibraryBook.id == LibraryCopy.book_id).options(
        joinedload(LibraryCopy.book)).filter(LibraryCopy.company_id == cid)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(LibraryCopy.accession_no.ilike(like), LibraryBook.title.ilike(like)))
    if status:
        query = query.filter(LibraryCopy.status == status)
    if book_uuid:
        query = query.filter(LibraryBook.uuid == book_uuid)
    return paginate(query.order_by(LibraryCopy.accession_no), params, lambda c: _copy_out(c))


@router.get("/copies/lookup")
def lookup_copy(code: str, db: Session = Depends(get_db), user: dict = Depends(require_permission(CIRC, VIEW))):
    cid = _cid(user)
    c = service.find_copy(db, cid, code)
    loan = db.query(LibraryLoan).options(joinedload(LibraryLoan.member)).filter(
        LibraryLoan.company_id == cid, LibraryLoan.copy_id == c.id, LibraryLoan.status == "issued").first()
    out = _copy_out(c, loan)
    if loan is not None:
        pol = service.policy_for(db, cid, loan.member.member_type)
        days, amount = service.compute_fine(pol, loan.due_on, service.today(db, cid))
        out["loan"].update({"late_days": days, "fine_if_returned": _money(amount), "renew_count": loan.renew_count})
    return out


@router.get("/labels.pdf")
def labels_pdf(book_uuid: UUID | None = None, copies: str | None = None, kind: str = Query("barcode", pattern="^(barcode|qr)$"),
               db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    """Printable spine / barcode labels (A4, 3 x 8) for a title's copies or a list of copy uuids."""
    from modules.library.labels import labels_pdf as render
    cid = _cid(user)
    q = db.query(LibraryCopy).options(joinedload(LibraryCopy.book)).filter(LibraryCopy.company_id == cid)
    if book_uuid:
        q = q.join(LibraryBook, LibraryBook.id == LibraryCopy.book_id).filter(LibraryBook.uuid == book_uuid)
    elif copies:
        ids = [x for x in copies.split(",") if x.strip()]
        try:
            q = q.filter(LibraryCopy.uuid.in_([UUID(x.strip()) for x in ids]))
        except ValueError:
            raise HTTPException(status_code=400, detail="Some copies were not recognised.")
    else:
        raise HTTPException(status_code=400, detail="Pick a title or some copies to print.")
    rows = q.order_by(LibraryCopy.accession_no).limit(600).all()
    if not rows:
        raise HTTPException(status_code=404, detail="No copies to print.")
    from modules.company.models import Company
    company = db.get(Company, cid)
    pdf = render(rows, kind, company.company_name if company else "")
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": 'inline; filename="labels.pdf"'})


# ── Members ──────────────────────────────────────────────────────────

def _member_out(db: Session, m: LibraryMember, loans: list[LibraryLoan] | None = None, fines: Decimal | None = None) -> dict:
    from modules.students import service as students
    out = {"id": m.id, "uuid": str(m.uuid), "member_type": m.member_type, "card_no": m.card_no, "name": service.member_name(m),
           "active": m.active, "max_books": m.max_books, "notes": m.notes, "student_id": m.student_id,
           "employee_id": m.employee_id, "photo_url": None, "detail": None}
    if m.student is not None:
        e = students.current_enrollment(db, m.company_id, m.student_id)
        out["photo_url"] = m.student.photo_url
        out["detail"] = students.academics.section_label(e.section) if e and e.section else m.student.admission_no
    elif m.employee is not None:
        out["photo_url"] = m.employee.profile_image_url if (m.employee.profile_image_url or "").startswith("http") else None
        out["detail"] = m.employee.email
    if loans is not None:
        on = service.today(db, m.company_id)
        out["limit"] = service.limit_for(db, m)
        out["on_loan"] = len(loans)
        out["overdue"] = sum(1 for l in loans if l.due_on < on)
    if fines is not None:
        out["fines_due"] = _money(fines)
    return out


@router.get("/members")
def list_members(q: str | None = None, member_type: str | None = None, active: bool | None = Query(True),
                 params: PageParams = Depends(), db: Session = Depends(get_db),
                 user: dict = Depends(require_permission(VIEW))):
    from modules.employees.models import Employee
    from modules.students.models import Student
    cid = _cid(user)
    query = (db.query(LibraryMember).outerjoin(Student, Student.id == LibraryMember.student_id)
             .outerjoin(Employee, Employee.id == LibraryMember.employee_id)
             .options(joinedload(LibraryMember.student), joinedload(LibraryMember.employee))
             .filter(LibraryMember.company_id == cid))
    if member_type:
        query = query.filter(LibraryMember.member_type == member_type)
    if active is not None:
        query = query.filter(LibraryMember.active.is_(active))
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(LibraryMember.card_no.ilike(like), Student.first_name.ilike(like),
                                 Student.last_name.ilike(like), Employee.first_name.ilike(like),
                                 Employee.last_name.ilike(like)))
    query = query.order_by(LibraryMember.member_type.desc(), func.coalesce(Student.first_name, Employee.first_name))
    page = paginate(query, params)
    ids = [m.id for m in page["items"]]
    on_loan = dict(db.query(LibraryLoan.member_id, func.count(LibraryLoan.id)).filter(
        LibraryLoan.company_id == cid, LibraryLoan.member_id.in_(ids), LibraryLoan.status == "issued")
        .group_by(LibraryLoan.member_id).all()) if ids else {}
    fines = dict(db.query(LibraryFine.member_id, func.sum(LibraryFine.amount)).filter(
        LibraryFine.company_id == cid, LibraryFine.member_id.in_(ids), LibraryFine.status == "pending")
        .group_by(LibraryFine.member_id).all()) if ids else {}
    items = []
    for m in page["items"]:
        o = {"id": m.id, "uuid": str(m.uuid), "member_type": m.member_type, "card_no": m.card_no,
             "name": service.member_name(m), "active": m.active, "max_books": m.max_books,
             "on_loan": on_loan.get(m.id, 0), "fines_due": _money(fines.get(m.id) or 0),
             "detail": m.student.admission_no if m.student else (m.employee.email if m.employee else None)}
        items.append(o)
    page["items"] = items
    return page


@router.post("/members/sync")
def sync_members(db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE, CIRC))):
    res = service.sync_members(db, _cid(user))
    db.commit()
    return res


@router.get("/members/lookup")
def lookup_member(code: str, db: Session = Depends(get_db), user: dict = Depends(require_permission(CIRC, VIEW))):
    cid = _cid(user)
    m = service.find_member(db, cid, code)
    db.commit()
    return _member_detail(db, m)


@router.post("/members/for-student/{student_id}")
def member_for_student(student_id: int, db: Session = Depends(get_db), user: dict = Depends(require_permission(CIRC, MANAGE))):
    m = service.ensure_student_member(db, _cid(user), student_id)
    db.commit()
    return _member_detail(db, m)


@router.post("/members/for-employee/{employee_id}")
def member_for_employee(employee_id: int, db: Session = Depends(get_db), user: dict = Depends(require_permission(CIRC, MANAGE))):
    m = service.ensure_staff_member(db, _cid(user), employee_id)
    db.commit()
    return _member_detail(db, m)


def _member_detail(db: Session, m: LibraryMember) -> dict:
    cid = m.company_id
    loans = service.open_loans(db, cid, m.id)
    on = service.today(db, cid)
    pol = service.policy_for(db, cid, m.member_type)
    out = _member_out(db, m, loans, service.unpaid_fines(db, cid, m.id))
    out["policy"] = _policy_out(pol)
    out["loans"] = [_loan_out(l, on, pol) for l in loans]
    out["fines"] = [_fine_out(f) for f in db.query(LibraryFine).options(joinedload(LibraryFine.loan).joinedload(LibraryLoan.book))
                    .filter(LibraryFine.company_id == cid, LibraryFine.member_id == m.id, LibraryFine.status == "pending")
                    .order_by(LibraryFine.created_at).all()]
    out["holds"] = [_hold_out(r) for r in db.query(LibraryReservation).options(joinedload(LibraryReservation.book))
                    .filter(LibraryReservation.company_id == cid, LibraryReservation.member_id == m.id,
                            LibraryReservation.status.in_(("waiting", "ready"))).all()]
    return out


@router.get("/members/{uuid}")
def get_member(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    cid = _cid(user)
    m = get_or_404(db, LibraryMember, cid, uuid, "Member")
    out = _member_detail(db, m)
    hist = (db.query(LibraryLoan).options(joinedload(LibraryLoan.book), joinedload(LibraryLoan.copy))
            .filter(LibraryLoan.company_id == cid, LibraryLoan.member_id == m.id)
            .order_by(LibraryLoan.issued_on.desc(), LibraryLoan.id.desc()).limit(100).all())
    on = service.today(db, cid)
    out["history"] = [_loan_out(l, on) for l in hist]
    return out


class MemberPatch(BaseModel):
    max_books: int | None = Field(None, ge=0, le=100)
    active: bool | None = None
    notes: str | None = None
    card_no: str | None = Field(None, min_length=1, max_length=40)


@router.put("/members/{uuid}")
def update_member(uuid: UUID, payload: MemberPatch, db: Session = Depends(get_db),
                  user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    m = get_or_404(db, LibraryMember, cid, uuid, "Member")
    data = payload.model_dump(exclude_unset=True)
    if data.get("card_no") and db.query(LibraryMember.id).filter(
            LibraryMember.company_id == cid, func.lower(LibraryMember.card_no) == data["card_no"].lower(),
            LibraryMember.id != m.id).first():
        raise HTTPException(status_code=409, detail="This card number is already used.")
    for k, v in data.items():
        setattr(m, k, v)
    db.commit()
    return _member_detail(db, m)


# ── Circulation ──────────────────────────────────────────────────────

def _loan_out(l: LibraryLoan, on: date, pol: LibraryPolicy | None = None) -> dict:
    out = {"id": l.id, "uuid": str(l.uuid), "book_uuid": str(l.book.uuid), "title": l.book.title,
           "authors": l.book.authors, "cover_url": l.book.cover_url, "accession_no": l.copy.accession_no,
           "issued_on": l.issued_on, "due_on": l.due_on, "returned_on": l.returned_on, "status": l.status,
           "renew_count": l.renew_count, "overdue_days": max(0, (on - l.due_on).days) if l.status == "issued" else 0}
    if l.member is not None:
        out.update({"member_uuid": str(l.member.uuid), "member_name": service.member_name(l.member),
                    "member_type": l.member.member_type, "card_no": l.member.card_no})
    if pol is not None and l.status == "issued":
        _days, amount = service.compute_fine(pol, l.due_on, on)
        out["fine_so_far"] = _money(amount)
        out["renewals_left"] = max(0, pol.max_renewals - l.renew_count)
    return out


class IssueIn(BaseModel):
    member: str = Field(..., min_length=1, max_length=60)  # card / admission no. / member uuid
    copies: list[str] = Field(..., min_length=1, max_length=20)  # accession numbers
    due_on: date | None = None


@router.post("/issue")
def issue(payload: IssueIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(CIRC))):
    cid = _cid(user)
    m = service.find_member(db, cid, payload.member)
    on = service.today(db, cid)
    if payload.due_on and payload.due_on < on:
        raise HTTPException(status_code=400, detail="The due date can't be in the past.")
    loans = []
    for code in payload.copies:
        c = service.find_copy(db, cid, code)
        loans.append(service.issue(db, cid, m, c, on, payload.due_on))
    db.commit()
    return {"member": _member_detail(db, m), "issued": [_loan_out(l, on) for l in loans]}


class ReturnIn(BaseModel):
    code: str = Field(..., min_length=1, max_length=60)
    condition: str | None = Field(None, pattern="^(good|fair|poor|damaged)$")
    damage_fine: Decimal | None = Field(None, ge=0, max_digits=10, decimal_places=2)
    waive_overdue: bool = False
    note: str | None = Field(None, max_length=500)


@router.post("/return")
def return_copy(payload: ReturnIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(CIRC))):
    cid = _cid(user)
    c = service.find_copy(db, cid, payload.code)
    loan = db.query(LibraryLoan).filter(LibraryLoan.company_id == cid, LibraryLoan.copy_id == c.id,
                                        LibraryLoan.status == "issued").first()
    if loan is None:
        raise HTTPException(status_code=400, detail=f"Copy {c.accession_no} is not on loan.")
    if payload.waive_overdue and not service.has_manage(db, user):
        raise HTTPException(status_code=403, detail="Only library managers can waive fines.")
    on = service.today(db, cid)
    res = service.return_loan(db, cid, loan, on, condition=payload.condition, damage_fine=payload.damage_fine,
                              waive_overdue=payload.waive_overdue, note=payload.note)
    hold = db.query(LibraryReservation).options(joinedload(LibraryReservation.member)).filter(
        LibraryReservation.company_id == cid, LibraryReservation.copy_id == c.id,
        LibraryReservation.status == "ready").first()
    db.commit()
    return {"loan": _loan_out(loan, on), "late_days": res["late_days"], "copy_status": res["copy_status"],
            "fines": [_fine_out(f) for f in res["fines"]],
            "hold_for": service.member_name(hold.member) if hold else None}


@router.post("/loans/{uuid}/renew")
def renew(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(CIRC))):
    cid = _cid(user)
    loan = get_or_404(db, LibraryLoan, cid, uuid, "Loan")
    on = service.today(db, cid)
    service.renew(db, cid, loan, on)
    db.commit()
    return _loan_out(loan, on)


class LostIn(BaseModel):
    amount: Decimal | None = Field(None, ge=0, max_digits=10, decimal_places=2)


@router.post("/loans/{uuid}/lost")
def lost(uuid: UUID, payload: LostIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(CIRC))):
    cid = _cid(user)
    loan = get_or_404(db, LibraryLoan, cid, uuid, "Loan")
    on = service.today(db, cid)
    fine = service.mark_lost(db, cid, loan, on, payload.amount)
    db.commit()
    return {"loan": _loan_out(loan, on), "fine": _fine_out(fine) if fine else None}


@router.get("/loans")
def list_loans(status: str | None = Query("issued"), overdue: bool = False, member_uuid: UUID | None = None,
               q: str | None = None, params: PageParams = Depends(), db: Session = Depends(get_db),
               user: dict = Depends(require_permission(VIEW))):
    from modules.employees.models import Employee
    from modules.students.models import Student
    cid = _cid(user)
    on = service.today(db, cid)
    query = (db.query(LibraryLoan).join(LibraryBook, LibraryBook.id == LibraryLoan.book_id)
             .join(LibraryMember, LibraryMember.id == LibraryLoan.member_id)
             .outerjoin(Student, Student.id == LibraryMember.student_id)
             .outerjoin(Employee, Employee.id == LibraryMember.employee_id)
             .options(joinedload(LibraryLoan.book), joinedload(LibraryLoan.copy),
                      joinedload(LibraryLoan.member).joinedload(LibraryMember.student),
                      joinedload(LibraryLoan.member).joinedload(LibraryMember.employee))
             .filter(LibraryLoan.company_id == cid))
    if status:
        query = query.filter(LibraryLoan.status == status)
    if overdue:
        query = query.filter(LibraryLoan.status == "issued", LibraryLoan.due_on < on)
    if member_uuid:
        query = query.filter(LibraryMember.uuid == member_uuid)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(LibraryBook.title.ilike(like), LibraryMember.card_no.ilike(like),
                                 Student.first_name.ilike(like), Student.last_name.ilike(like),
                                 Employee.first_name.ilike(like), Employee.last_name.ilike(like)))
    query = query.order_by(LibraryLoan.due_on if status == "issued" or overdue else LibraryLoan.issued_on.desc())
    pols = {p.member_type: p for p in db.query(LibraryPolicy).filter(LibraryPolicy.company_id == cid).all()}
    return paginate(query, params, lambda l: _loan_out(l, on, pols.get(l.member.member_type)))


# ── Fines ────────────────────────────────────────────────────────────

def _fine_out(f: LibraryFine) -> dict:
    return {"id": f.id, "uuid": str(f.uuid), "kind": f.kind, "amount": _money(f.amount), "days": f.days,
            "status": f.status, "reason": f.reason, "settled_on": f.settled_on, "settled_by": f.settled_by,
            "fee_ref": f.fee_ref, "created_at": f.created_at,
            "title": f.loan.book.title if f.loan and f.loan.book else None,
            "member_uuid": str(f.member.uuid) if f.member else None,
            "member_name": service.member_name(f.member) if f.member else None,
            "member_type": f.member.member_type if f.member else None,
            "card_no": f.member.card_no if f.member else None}


@router.get("/fines")
def list_fines(status: str | None = Query("pending"), q: str | None = None, params: PageParams = Depends(),
               db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    from modules.employees.models import Employee
    from modules.students.models import Student
    cid = _cid(user)
    query = (db.query(LibraryFine).join(LibraryMember, LibraryMember.id == LibraryFine.member_id)
             .outerjoin(Student, Student.id == LibraryMember.student_id)
             .outerjoin(Employee, Employee.id == LibraryMember.employee_id)
             .options(joinedload(LibraryFine.loan).joinedload(LibraryLoan.book),
                      joinedload(LibraryFine.member).joinedload(LibraryMember.student),
                      joinedload(LibraryFine.member).joinedload(LibraryMember.employee))
             .filter(LibraryFine.company_id == cid))
    if status:
        query = query.filter(LibraryFine.status == status)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(LibraryMember.card_no.ilike(like), Student.first_name.ilike(like),
                                 Student.last_name.ilike(like), Employee.first_name.ilike(like)))
    total = db.query(func.coalesce(func.sum(LibraryFine.amount), 0)).filter(
        LibraryFine.company_id == cid, LibraryFine.status == "pending").scalar()
    page = paginate(query.order_by(LibraryFine.created_at.desc()), params, _fine_out)
    page["pending_total"] = _money(total or 0)
    page["fees_billing"] = service.fees_available()
    return page


class SettleIn(BaseModel):
    action: str = Field(..., pattern="^(pay|waive|bill)$")
    reason: str | None = Field(None, max_length=500)


@router.post("/fines/{uuid}/settle")
def settle(uuid: UUID, payload: SettleIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(CIRC))):
    cid = _cid(user)
    f = get_or_404(db, LibraryFine, cid, uuid, "Fine")
    if payload.action == "waive" and not service.has_manage(db, user):
        raise HTTPException(status_code=403, detail="Only library managers can waive fines.")
    service.settle_fine(db, cid, f, payload.action, service.today(db, cid), reason=payload.reason)
    db.commit()
    return _fine_out(f)


# ── Holds ────────────────────────────────────────────────────────────

def _hold_out(r: LibraryReservation) -> dict:
    return {"id": r.id, "uuid": str(r.uuid), "status": r.status, "reserved_at": r.reserved_at, "ready_on": r.ready_on,
            "expires_on": r.expires_on, "book_uuid": str(r.book.uuid), "title": r.book.title,
            "authors": r.book.authors, "accession_no": r.copy.accession_no if r.copy else None,
            "member_uuid": str(r.member.uuid), "member_name": service.member_name(r.member),
            "card_no": r.member.card_no}


@router.get("/reservations")
def list_reservations(status: str | None = Query(None), params: PageParams = Depends(), db: Session = Depends(get_db),
                      user: dict = Depends(require_permission(VIEW))):
    cid = _cid(user)
    query = db.query(LibraryReservation).options(joinedload(LibraryReservation.book), joinedload(LibraryReservation.copy),
                                                 joinedload(LibraryReservation.member)).filter(
        LibraryReservation.company_id == cid)
    query = query.filter(LibraryReservation.status == status) if status else \
        query.filter(LibraryReservation.status.in_(("waiting", "ready")))
    return paginate(query.order_by(LibraryReservation.reserved_at), params, _hold_out)


class ReserveIn(BaseModel):
    member: str = Field(..., min_length=1, max_length=60)
    book_uuid: UUID


@router.post("/reservations", status_code=201)
def reserve(payload: ReserveIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(CIRC))):
    cid = _cid(user)
    m = service.find_member(db, cid, payload.member)
    b = get_or_404(db, LibraryBook, cid, payload.book_uuid, "Book")
    r = service.reserve(db, cid, m, b, service.today(db, cid))
    db.commit()
    return _hold_out(r)


@router.post("/reservations/{uuid}/cancel")
def cancel_reservation(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(CIRC))):
    cid = _cid(user)
    r = get_or_404(db, LibraryReservation, cid, uuid, "Reservation")
    service.cancel_reservation(db, cid, r, service.today(db, cid))
    db.commit()
    return _hold_out(r)


# ── Stock verification ───────────────────────────────────────────────

def _check_out(db: Session, c: LibraryStockCheck) -> dict:
    return {"id": c.id, "uuid": str(c.uuid), "name": c.name, "started_on": c.started_on, "closed_on": c.closed_on,
            "status": c.status, "scope_shelf": c.scope_shelf, "notes": c.notes, "created_by": c.created_by,
            **service.stock_counts(db, c)}


@router.get("/stock-checks")
def list_checks(params: PageParams = Depends(), db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    q = db.query(LibraryStockCheck).filter(LibraryStockCheck.company_id == _cid(user)).order_by(
        LibraryStockCheck.started_on.desc(), LibraryStockCheck.id.desc())
    return paginate(q, params, lambda c: _check_out(db, c))


class CheckIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    scope_shelf: str | None = Field(None, max_length=60)
    notes: str | None = None


@router.post("/stock-checks", status_code=201)
def start_check(payload: CheckIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    if db.query(LibraryStockCheck.id).filter(LibraryStockCheck.company_id == cid, LibraryStockCheck.status == "open").first():
        raise HTTPException(status_code=400, detail="A stock check is already open. Close it first.")
    c = LibraryStockCheck(company_id=cid, name=payload.name, scope_shelf=payload.scope_shelf or None, notes=payload.notes,
                          started_on=service.today(db, cid), created_by=service.actor())
    db.add(c)
    db.commit()
    return _check_out(db, c)


@router.get("/stock-checks/{uuid}")
def get_check(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    c = get_or_404(db, LibraryStockCheck, _cid(user), uuid, "Stock check")
    out = _check_out(db, c)
    out["missing_list"] = ([{"accession_no": x.accession_no, "title": x.book.title, "shelf": x.shelf or x.book.shelf}
                            for x in service.stock_missing(db, c)[:500]] if c.status == "open"
                           else (c.result or {}).get("missing", []))
    return out


class ScanIn(BaseModel):
    code: str = Field(..., min_length=1, max_length=60)


@router.post("/stock-checks/{uuid}/scan")
def scan(uuid: UUID, payload: ScanIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE, CIRC))):
    c = get_or_404(db, LibraryStockCheck, _cid(user), uuid, "Stock check")
    res = service.stock_scan(db, c, payload.code)
    db.commit()
    return {**res, **service.stock_counts(db, c)}


class CloseIn(BaseModel):
    mark_missing_lost: bool = False


@router.post("/stock-checks/{uuid}/close")
def close_check(uuid: UUID, payload: CloseIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    c = get_or_404(db, LibraryStockCheck, cid, uuid, "Stock check")
    service.close_stock_check(db, c, service.today(db, cid), payload.mark_missing_lost)
    db.commit()
    return _check_out(db, c)


# ── Reports and summary ──────────────────────────────────────────────

@router.get("/summary")
def summary(db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    cid = _cid(user)
    on = service.today(db, cid)
    titles = db.query(func.count(LibraryBook.id)).filter(LibraryBook.company_id == cid, LibraryBook.active.is_(True)).scalar()
    copies = dict(db.query(LibraryCopy.status, func.count(LibraryCopy.id)).filter(
        LibraryCopy.company_id == cid).group_by(LibraryCopy.status).all())
    overdue = db.query(func.count(LibraryLoan.id)).filter(LibraryLoan.company_id == cid, LibraryLoan.status == "issued",
                                                          LibraryLoan.due_on < on).scalar()
    due_today = db.query(func.count(LibraryLoan.id)).filter(LibraryLoan.company_id == cid, LibraryLoan.status == "issued",
                                                            LibraryLoan.due_on == on).scalar()
    issued_today = db.query(func.count(LibraryLoan.id)).filter(LibraryLoan.company_id == cid, LibraryLoan.issued_on == on).scalar()
    returned_today = db.query(func.count(LibraryLoan.id)).filter(LibraryLoan.company_id == cid, LibraryLoan.returned_on == on,
                                                                 LibraryLoan.status == "returned").scalar()
    fines = db.query(func.coalesce(func.sum(LibraryFine.amount), 0)).filter(
        LibraryFine.company_id == cid, LibraryFine.status == "pending").scalar()
    holds = db.query(func.count(LibraryReservation.id)).filter(
        LibraryReservation.company_id == cid, LibraryReservation.status.in_(("waiting", "ready"))).scalar()
    return {"titles": titles or 0, "copies": sum(v for k, v in copies.items() if k not in ("withdrawn", "lost")),
            "available": copies.get("available", 0), "on_loan": copies.get("issued", 0),
            "lost": copies.get("lost", 0), "damaged": copies.get("damaged", 0), "overdue": overdue or 0,
            "due_today": due_today or 0, "issued_today": issued_today or 0, "returned_today": returned_today or 0,
            "fines_pending": _money(fines or 0), "holds": holds or 0}


@router.get("/reports/most-borrowed")
def most_borrowed(date_from: date | None = None, date_to: date | None = None, limit: int = Query(20, ge=1, le=100),
                  db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    cid = _cid(user)
    on = service.today(db, cid)
    date_from = date_from or (on - timedelta(days=365))
    date_to = date_to or on
    rows = (db.query(LibraryBook, func.count(LibraryLoan.id).label("n"))
            .join(LibraryLoan, LibraryLoan.book_id == LibraryBook.id)
            .filter(LibraryLoan.company_id == cid, LibraryLoan.issued_on >= date_from, LibraryLoan.issued_on <= date_to)
            .group_by(LibraryBook.id).order_by(func.count(LibraryLoan.id).desc(), LibraryBook.title).limit(limit).all())
    top = (db.query(LibraryLoan.member_id, func.count(LibraryLoan.id))
           .filter(LibraryLoan.company_id == cid, LibraryLoan.issued_on >= date_from, LibraryLoan.issued_on <= date_to)
           .group_by(LibraryLoan.member_id).order_by(func.count(LibraryLoan.id).desc()).limit(10).all())
    mem = {m.id: m for m in db.query(LibraryMember).options(joinedload(LibraryMember.student), joinedload(LibraryMember.employee))
           .filter(LibraryMember.id.in_([t[0] for t in top] or [0])).all()}
    readers = [(mem[mid], n) for mid, n in top if mid in mem]
    by_cat = (db.query(func.coalesce(LibraryCategory.name, "Uncategorised"), func.count(LibraryLoan.id))
              .select_from(LibraryLoan).join(LibraryBook, LibraryBook.id == LibraryLoan.book_id)
              .outerjoin(LibraryCategory, LibraryCategory.id == LibraryBook.category_id)
              .filter(LibraryLoan.company_id == cid, LibraryLoan.issued_on >= date_from, LibraryLoan.issued_on <= date_to)
              .group_by(LibraryCategory.name).order_by(func.count(LibraryLoan.id).desc()).all())
    return {"date_from": date_from, "date_to": date_to,
            "books": [{"uuid": str(b.uuid), "title": b.title, "authors": b.authors, "loans": n} for b, n in rows],
            "readers": [{"uuid": str(m.uuid), "name": service.member_name(m), "member_type": m.member_type,
                         "card_no": m.card_no, "loans": n} for m, n in readers],
            "categories": [{"name": name, "loans": n} for name, n in by_cat]}


# ── Portal (parents and students) ────────────────────────────────────

def _portal_member(db: Session, user: dict, student_id: int) -> LibraryMember:
    from modules.students.service import require_student_access
    if not user.get("portal_id"):
        raise HTTPException(status_code=403, detail="This page is for parents and students.")
    require_student_access(db, user, student_id)
    m = service.ensure_student_member(db, user["company_id"], student_id)
    db.commit()
    return m


@router.get("/portal/summary")
def portal_summary(student_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    m = _portal_member(db, user, student_id)
    cid = user["company_id"]
    on = service.today(db, cid)
    loans = service.open_loans(db, cid, m.id)
    nxt = min((l for l in loans), key=lambda l: l.due_on, default=None)
    return {"on_loan": len(loans), "overdue": sum(1 for l in loans if l.due_on < on),
            "fines_due": _money(service.unpaid_fines(db, cid, m.id)),
            "next_due": {"title": nxt.book.title, "due_on": nxt.due_on} if nxt else None,
            "ready_holds": db.query(func.count(LibraryReservation.id)).filter(
                LibraryReservation.company_id == cid, LibraryReservation.member_id == m.id,
                LibraryReservation.status == "ready").scalar() or 0}


@router.get("/portal/loans")
def portal_loans(student_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    m = _portal_member(db, user, student_id)
    cid = user["company_id"]
    on = service.today(db, cid)
    pol = service.policy_for(db, cid, m.member_type)
    hist = (db.query(LibraryLoan).options(joinedload(LibraryLoan.book), joinedload(LibraryLoan.copy))
            .filter(LibraryLoan.company_id == cid, LibraryLoan.member_id == m.id, LibraryLoan.status != "issued")
            .order_by(LibraryLoan.issued_on.desc()).limit(50).all())
    fines = (db.query(LibraryFine).options(joinedload(LibraryFine.loan).joinedload(LibraryLoan.book))
             .filter(LibraryFine.company_id == cid, LibraryFine.member_id == m.id)
             .order_by(LibraryFine.created_at.desc()).limit(50).all())
    holds = (db.query(LibraryReservation).options(joinedload(LibraryReservation.book), joinedload(LibraryReservation.copy))
             .filter(LibraryReservation.company_id == cid, LibraryReservation.member_id == m.id,
                     LibraryReservation.status.in_(("waiting", "ready"))).all())

    def clean(d: dict) -> dict:
        return {k: v for k, v in d.items() if k not in ("member_uuid", "member_name", "card_no", "member_type")}
    return {"card_no": m.card_no, "limit": service.limit_for(db, m), "loan_days": pol.loan_days,
            "fine_per_day": _money(pol.fine_per_day),
            "loans": [clean(_loan_out(l, on, pol)) for l in service.open_loans(db, cid, m.id)],
            "history": [clean(_loan_out(l, on)) for l in hist],
            "fines": [clean(_fine_out(f)) for f in fines],
            "holds": [clean(_hold_out(r)) for r in holds],
            "fines_due": _money(service.unpaid_fines(db, cid, m.id))}


@router.get("/portal/catalogue")
def portal_catalogue(student_id: int, q: str | None = None, category_id: int | None = None,
                     params: PageParams = Depends(), db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _portal_member(db, user, student_id)
    cid = user["company_id"]
    page = _paged_books(db, cid, _book_query(db, cid, q, category_id, None, None, True), params)
    keep = ("uuid", "title", "subtitle", "authors", "publisher", "year", "category_name", "subject", "language",
            "cover_url", "kind", "reference_only", "copies", "available", "shelf")
    page["items"] = [{k: b[k] for k in keep} for b in page["items"]]
    return page


@router.get("/portal/categories")
def portal_categories(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    if not user.get("portal_id"):
        raise HTTPException(status_code=403, detail="This page is for parents and students.")
    rows = db.query(LibraryCategory).filter(LibraryCategory.company_id == user["company_id"],
                                            LibraryCategory.active.is_(True)).order_by(LibraryCategory.name).all()
    return [{"id": c.id, "name": c.name} for c in rows]


@router.get("/portal/eresources")
def portal_eresources(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    if not user.get("portal_id"):
        raise HTTPException(status_code=403, detail="This page is for parents and students.")
    rows = db.query(LibraryEResource).filter(LibraryEResource.company_id == user["company_id"],
                                             LibraryEResource.active.is_(True),
                                             LibraryEResource.audience.in_(("all", "students"))).order_by(LibraryEResource.title).all()
    return [_eres_out(r) for r in rows]


class PortalReserveIn(BaseModel):
    student_id: int
    book_uuid: UUID


@router.post("/portal/reservations", status_code=201)
def portal_reserve(payload: PortalReserveIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    m = _portal_member(db, user, payload.student_id)
    cid = user["company_id"]
    b = get_or_404(db, LibraryBook, cid, payload.book_uuid, "Book")
    r = service.reserve(db, cid, m, b, service.today(db, cid))
    db.commit()
    return {k: v for k, v in _hold_out(r).items() if k not in ("member_uuid", "member_name", "card_no")}


@router.post("/portal/reservations/{uuid}/cancel")
def portal_cancel(uuid: UUID, student_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    m = _portal_member(db, user, student_id)
    cid = user["company_id"]
    r = get_or_404(db, LibraryReservation, cid, uuid, "Reservation")
    if r.member_id != m.id:
        raise HTTPException(status_code=404, detail="Reservation not found")
    service.cancel_reservation(db, cid, r, service.today(db, cid))
    db.commit()
    return {"detail": "Reservation cancelled"}


@router.get("/settings-info")
def settings_info(db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    return {"fees_billing": service.fees_available()}

