"""Library: catalogue, copies, members, circulation, fines, holds, e-resources and stock checks.

- LibraryPolicy      borrowing rules per member type (student, staff): books, days, fine per day …
- LibraryCategory    subject / category of a title ("Fiction", "Science", "Reference")
- LibraryBook        one title in the catalogue (ISBN, authors, publisher, edition, shelf …)
- LibraryCopy        one physical copy with its accession number (also its barcode)
- LibraryMember      a learner or a staff member who can borrow (created automatically)
- LibraryLoan        a copy issued to a member: due date, renewals, return
- LibraryFine        money owed: overdue, lost or damaged; paid, waived (with reason) or billed via fees
- LibraryReservation a hold on a title; the next returned copy is kept for the member
- LibraryEResource   links to e-books, journals and databases
- LibraryStockCheck  a stock verification run; LibraryStockCheckItem = a copy seen on the shelf
"""
from sqlalchemy import (Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text,
                        UniqueConstraint, text)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from core.db import Base


def _uuid():
    return Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))


def _company():
    return Column(Integer, ForeignKey("companies.id"), nullable=False)


def _stamps():
    return (Column(DateTime(timezone=True), server_default=func.now()),
            Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now()))


class LibraryPolicy(Base):
    __tablename__ = "library_policies"
    __table_args__ = (UniqueConstraint("company_id", "member_type", name="uq_library_policies_type"),
                      Index("ix_library_policies_company", "company_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    member_type = Column(String(20), nullable=False)  # student | staff
    name = Column(String(80), nullable=False)
    max_books = Column(Integer, nullable=False, server_default=text("2"))
    loan_days = Column(Integer, nullable=False, server_default=text("14"))
    fine_per_day = Column(Numeric(10, 2), nullable=False, server_default=text("1"))
    grace_days = Column(Integer, nullable=False, server_default=text("0"))
    max_fine = Column(Numeric(10, 2), nullable=True)  # cap per loan; empty = no cap
    max_renewals = Column(Integer, nullable=False, server_default=text("1"))
    renew_when_overdue = Column(Boolean, nullable=False, server_default=text("false"))
    hold_days = Column(Integer, nullable=False, server_default=text("3"))  # days a returned copy waits for a hold
    max_reservations = Column(Integer, nullable=False, server_default=text("2"))
    block_fine_above = Column(Numeric(10, 2), nullable=True)  # unpaid fines above this block new issues
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class LibraryCategory(Base):
    __tablename__ = "library_categories"
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_library_categories_name"),
                      Index("ix_library_categories_company", "company_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(80), nullable=False)
    code = Column(String(20), nullable=True)  # classification, e.g. DDC "500"
    description = Column(Text, nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class LibraryBook(Base):
    __tablename__ = "library_books"
    __table_args__ = (Index("ix_library_books_company_title", "company_id", "title"),
                      Index("ix_library_books_company_isbn", "company_id", "isbn"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    title = Column(String(300), nullable=False)
    subtitle = Column(String(300), nullable=True)
    authors = Column(String(300), nullable=True)
    isbn = Column(String(20), nullable=True)
    publisher = Column(String(200), nullable=True)
    edition = Column(String(60), nullable=True)
    year = Column(Integer, nullable=True)
    category_id = Column(Integer, ForeignKey("library_categories.id"), nullable=True)
    subject = Column(String(120), nullable=True)
    language = Column(String(40), nullable=True)
    pages = Column(Integer, nullable=True)
    shelf = Column(String(60), nullable=True)
    cover_url = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    kind = Column(String(20), nullable=False, server_default=text("'book'"))  # book | reference | magazine | journal | cd
    price = Column(Numeric(10, 2), nullable=True)
    reference_only = Column(Boolean, nullable=False, server_default=text("false"))
    tags = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()

    category = relationship("LibraryCategory")
    copies = relationship("LibraryCopy", back_populates="book", order_by="LibraryCopy.id")


class LibraryCopy(Base):
    __tablename__ = "library_copies"
    __table_args__ = (UniqueConstraint("company_id", "accession_no", name="uq_library_copies_accession"),
                      Index("ix_library_copies_company_book", "company_id", "book_id"),
                      Index("ix_library_copies_company_status", "company_id", "status"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    book_id = Column(Integer, ForeignKey("library_books.id"), nullable=False)
    accession_no = Column(String(40), nullable=False)
    # available | issued | on_hold | lost | damaged | repair | withdrawn
    status = Column(String(20), nullable=False, server_default=text("'available'"))
    condition = Column(String(20), nullable=True)  # new | good | fair | poor
    shelf = Column(String(60), nullable=True)  # overrides the title's shelf
    acquired_on = Column(Date, nullable=True)
    source = Column(String(120), nullable=True)  # vendor, donation …
    price = Column(Numeric(10, 2), nullable=True)
    last_seen_on = Column(Date, nullable=True)  # last stock verification
    notes = Column(Text, nullable=True)
    created_at, updated_at = _stamps()

    book = relationship("LibraryBook", back_populates="copies")


class LibraryMember(Base):
    __tablename__ = "library_members"
    __table_args__ = (UniqueConstraint("company_id", "card_no", name="uq_library_members_card"),
                      UniqueConstraint("company_id", "student_id", name="uq_library_members_student"),
                      UniqueConstraint("company_id", "employee_id", name="uq_library_members_employee"),
                      Index("ix_library_members_company", "company_id", "member_type"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    member_type = Column(String(20), nullable=False)  # student | staff
    student_id = Column(Integer, ForeignKey("students.id"), nullable=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    card_no = Column(String(40), nullable=False)  # admission no. / staff code by default
    max_books = Column(Integer, nullable=True)  # override of the policy
    active = Column(Boolean, nullable=False, server_default=text("true"))
    notes = Column(Text, nullable=True)
    created_at, updated_at = _stamps()

    student = relationship("Student")
    employee = relationship("Employee")


class LibraryLoan(Base):
    __tablename__ = "library_loans"
    __table_args__ = (Index("ix_library_loans_company_status", "company_id", "status", "due_on"),
                      Index("ix_library_loans_member", "company_id", "member_id"),
                      Index("ix_library_loans_copy", "company_id", "copy_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    copy_id = Column(Integer, ForeignKey("library_copies.id"), nullable=False)
    book_id = Column(Integer, ForeignKey("library_books.id"), nullable=False)
    member_id = Column(Integer, ForeignKey("library_members.id"), nullable=False)
    issued_on = Column(Date, nullable=False)
    due_on = Column(Date, nullable=False)
    returned_on = Column(Date, nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'issued'"))  # issued | returned | lost
    renew_count = Column(Integer, nullable=False, server_default=text("0"))
    issued_by = Column(String(200), nullable=True)
    returned_by = Column(String(200), nullable=True)
    return_condition = Column(String(20), nullable=True)
    reminded_on = Column(Date, nullable=True)
    notes = Column(Text, nullable=True)
    created_at, updated_at = _stamps()

    copy = relationship("LibraryCopy")
    book = relationship("LibraryBook")
    member = relationship("LibraryMember")


class LibraryFine(Base):
    __tablename__ = "library_fines"
    __table_args__ = (Index("ix_library_fines_company_status", "company_id", "status"),
                      Index("ix_library_fines_member", "company_id", "member_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    member_id = Column(Integer, ForeignKey("library_members.id"), nullable=False)
    loan_id = Column(Integer, ForeignKey("library_loans.id"), nullable=True)
    kind = Column(String(20), nullable=False)  # overdue | lost | damaged | other
    amount = Column(Numeric(10, 2), nullable=False)
    days = Column(Integer, nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'pending'"))  # pending | paid | waived | billed
    reason = Column(Text, nullable=True)  # waiver reason or note
    settled_on = Column(Date, nullable=True)
    settled_by = Column(String(200), nullable=True)
    fee_ref = Column(String(80), nullable=True)  # charge id in fees when billed
    created_at, updated_at = _stamps()

    member = relationship("LibraryMember")
    loan = relationship("LibraryLoan")


class LibraryReservation(Base):
    __tablename__ = "library_reservations"
    __table_args__ = (Index("ix_library_reservations_company", "company_id", "book_id", "status"),
                      Index("ix_library_reservations_member", "company_id", "member_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    book_id = Column(Integer, ForeignKey("library_books.id"), nullable=False)
    member_id = Column(Integer, ForeignKey("library_members.id"), nullable=False)
    copy_id = Column(Integer, ForeignKey("library_copies.id"), nullable=True)  # the copy kept aside
    # waiting | ready | fulfilled | cancelled | expired
    status = Column(String(20), nullable=False, server_default=text("'waiting'"))
    reserved_at = Column(DateTime(timezone=True), server_default=func.now())
    ready_on = Column(Date, nullable=True)
    expires_on = Column(Date, nullable=True)
    created_by = Column(String(200), nullable=True)
    created_at, updated_at = _stamps()

    book = relationship("LibraryBook")
    member = relationship("LibraryMember")
    copy = relationship("LibraryCopy")


class LibraryEResource(Base):
    __tablename__ = "library_eresources"
    __table_args__ = (Index("ix_library_eresources_company", "company_id"),)
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    title = Column(String(300), nullable=False)
    url = Column(Text, nullable=False)
    kind = Column(String(20), nullable=False, server_default=text("'ebook'"))  # ebook | journal | database | video | website
    subject = Column(String(120), nullable=True)
    description = Column(Text, nullable=True)
    access_note = Column(String(300), nullable=True)  # e.g. "Use the school login"
    audience = Column(String(20), nullable=False, server_default=text("'all'"))  # all | students | staff
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class LibraryStockCheck(Base):
    __tablename__ = "library_stock_checks"
    __table_args__ = (Index("ix_library_stock_checks_company", "company_id", "started_on"),)
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(120), nullable=False)
    started_on = Column(Date, nullable=False)
    closed_on = Column(Date, nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'open'"))  # open | closed
    scope_shelf = Column(String(60), nullable=True)  # limit to one shelf; empty = whole library
    notes = Column(Text, nullable=True)
    result = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_by = Column(String(200), nullable=True)
    created_at, updated_at = _stamps()


class LibraryStockCheckItem(Base):
    __tablename__ = "library_stock_check_items"
    __audit__ = False  # one row per scan; the run itself is audited
    __table_args__ = (UniqueConstraint("check_id", "copy_id", name="uq_library_stock_items"),
                      Index("ix_library_stock_items_company", "company_id", "check_id"))
    id = Column(Integer, primary_key=True)
    company_id = _company()
    check_id = Column(Integer, ForeignKey("library_stock_checks.id", ondelete="CASCADE"), nullable=False)
    copy_id = Column(Integer, ForeignKey("library_copies.id"), nullable=False)
    seen_at = Column(DateTime(timezone=True), server_default=func.now())


TABLES = ["library_policies", "library_categories", "library_books", "library_copies", "library_members",
          "library_loans", "library_fines", "library_reservations", "library_eresources", "library_stock_checks",
          "library_stock_check_items"]
