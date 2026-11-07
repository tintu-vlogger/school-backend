"""Fees and finance.

Set-up
- FeeHead            what is charged: tuition, admission, transport, late fee … (type, tax, ledger account)
- FeeCategory        groups of learners billed differently: new admission, RTE, staff ward, NRI …
- FeeStructure       amounts per head for a session + program (+ optional category), with an instalment plan
- FeeStructureItem   one head in a structure: amount per instalment or once, optional heads learners opt into
- FeeInstalment      due dates of a structure (monthly, quarterly, term-wise, yearly or custom)
- FeeOptIn           a learner's choice of an optional head for a session
- FeeConcessionType  reusable concession / scholarship (sibling, merit, staff ward …)
- FeeConcession      a concession given to one learner, with validity and approval status
- FeeLateRule        late fee: fixed or per day after the due date, grace days, cap

Billing and collection
- FeeInvoice / FeeInvoiceLine   the demand for one learner (usually one instalment), never deleted
- FeeAdjustment      credit and debit notes on an invoice (audited)
- FeePayment         a receipt (counter or online); FeePaymentMode the split by payment mode
- FeeAllocation      which invoice line each part of a payment paid (oldest first)
- FeeWalletEntry     advance / excess balance of a learner (+ credit, - used or refunded)
- FeeRefund          a refund request (approval flow fees.refund)
- FeeGatewaySetting  online payment provider settings (secrets encrypted)
- FeeOnlineOrder     one online checkout from the portal
- FeeReminderLog     reminders sent (so automatic reminders go out once)

Cash book
- FeeLedgerCategory / FeeLedgerEntry   other income and expenses
"""
from sqlalchemy import (Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text,
                        UniqueConstraint, text)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from core.db import Base

MONEY = Numeric(14, 2)


def _uuid():
    return Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))


def _company():
    return Column(Integer, ForeignKey("companies.id"), nullable=False)


def _created():
    return Column(DateTime(timezone=True), server_default=func.now())


def _updated():
    return Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


def _zero():
    return Column(MONEY, nullable=False, server_default=text("0"))


# ── Set-up ───────────────────────────────────────────────────────────

class FeeHead(Base):
    __tablename__ = "fee_heads"
    __table_args__ = (UniqueConstraint("company_id", "code", name="uq_fee_heads_code"),
                      Index("ix_fee_heads_company", "company_id", "sort_order"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(120), nullable=False)
    code = Column(String(30), nullable=False)
    # recurring | one_time | optional | deposit (refundable)
    kind = Column(String(20), nullable=False, server_default=text("'recurring'"))
    tax_rate = Column(Numeric(5, 2), nullable=True)  # GST % (empty = no tax)
    tax_code = Column(String(20), nullable=True)  # HSN / SAC
    ledger_account = Column(String(120), nullable=True)
    description = Column(Text, nullable=True)
    is_system = Column(Boolean, nullable=False, server_default=text("false"))
    sort_order = Column(Integer, nullable=False, server_default=text("1"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = _created()
    updated_at = _updated()


class FeeCategory(Base):
    __tablename__ = "fee_categories"
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_fee_categories_name"),
                      Index("ix_fee_categories_company", "company_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(80), nullable=False)
    code = Column(String(30), nullable=True)
    description = Column(Text, nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = _created()
    updated_at = _updated()


class FeeStructure(Base):
    __tablename__ = "fee_structures"
    __table_args__ = (Index("ix_fee_structures_company", "company_id", "session_id", "program_id"),)
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(160), nullable=False)
    session_id = Column(Integer, ForeignKey("academic_sessions.id"), nullable=False)
    program_id = Column(Integer, ForeignKey("academic_programs.id"), nullable=True)  # empty = every program
    category_id = Column(Integer, ForeignKey("fee_categories.id"), nullable=True)  # empty = everyone else
    plan = Column(String(20), nullable=False, server_default=text("'quarterly'"))  # monthly|quarterly|term|half_yearly|yearly|custom
    notes = Column(Text, nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = _created()
    updated_at = _updated()

    items = relationship("FeeStructureItem", back_populates="structure", order_by="FeeStructureItem.sort_order",
                         cascade="all, delete-orphan")
    instalments = relationship("FeeInstalment", back_populates="structure", order_by="FeeInstalment.seq",
                               cascade="all, delete-orphan")
    session = relationship("AcademicSession")
    program = relationship("Program")
    category = relationship("FeeCategory")


class FeeStructureItem(Base):
    __tablename__ = "fee_structure_items"
    __table_args__ = (Index("ix_fee_structure_items_company", "company_id", "structure_id"),)
    id = Column(Integer, primary_key=True)
    company_id = _company()
    structure_id = Column(Integer, ForeignKey("fee_structures.id", ondelete="CASCADE"), nullable=False)
    head_id = Column(Integer, ForeignKey("fee_heads.id"), nullable=False)
    amount = Column(MONEY, nullable=False)
    # per_instalment: charged in every instalment; once: only in instalment `instalment_no`
    billing = Column(String(20), nullable=False, server_default=text("'per_instalment'"))
    instalment_no = Column(Integer, nullable=True)
    optional = Column(Boolean, nullable=False, server_default=text("false"))
    sort_order = Column(Integer, nullable=False, server_default=text("1"))

    structure = relationship("FeeStructure", back_populates="items")
    head = relationship("FeeHead")


class FeeInstalment(Base):
    __tablename__ = "fee_instalments"
    __table_args__ = (Index("ix_fee_instalments_company", "company_id", "structure_id"),)
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    structure_id = Column(Integer, ForeignKey("fee_structures.id", ondelete="CASCADE"), nullable=False)
    seq = Column(Integer, nullable=False)
    name = Column(String(80), nullable=False)
    due_date = Column(Date, nullable=False)

    structure = relationship("FeeStructure", back_populates="instalments")


class FeeOptIn(Base):
    __tablename__ = "fee_opt_ins"
    __table_args__ = (UniqueConstraint("student_id", "session_id", "head_id", name="uq_fee_opt_ins"),
                      Index("ix_fee_opt_ins_company", "company_id", "student_id"))
    id = Column(Integer, primary_key=True)
    company_id = _company()
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    session_id = Column(Integer, ForeignKey("academic_sessions.id"), nullable=False)
    head_id = Column(Integer, ForeignKey("fee_heads.id"), nullable=False)
    created_at = _created()


class FeeConcessionType(Base):
    __tablename__ = "fee_concession_types"
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_fee_concession_types_name"),
                      Index("ix_fee_concession_types_company", "company_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(120), nullable=False)
    reason = Column(String(30), nullable=False, server_default=text("'other'"))  # sibling|merit|staff_ward|need|sports|rte|other
    kind = Column(String(10), nullable=False, server_default=text("'percent'"))  # percent | fixed
    value = Column(MONEY, nullable=False, server_default=text("0"))
    head_id = Column(Integer, ForeignKey("fee_heads.id"), nullable=True)  # empty = whole invoice
    description = Column(Text, nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = _created()
    updated_at = _updated()


class FeeConcession(Base):
    __tablename__ = "fee_concessions"
    __table_args__ = (Index("ix_fee_concessions_company", "company_id", "student_id", "status"),)
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    session_id = Column(Integer, ForeignKey("academic_sessions.id"), nullable=True)
    type_id = Column(Integer, ForeignKey("fee_concession_types.id"), nullable=True)
    name = Column(String(120), nullable=False)
    reason = Column(String(30), nullable=False, server_default=text("'other'"))
    kind = Column(String(10), nullable=False, server_default=text("'percent'"))  # percent | fixed (per invoice)
    value = Column(MONEY, nullable=False)
    head_id = Column(Integer, ForeignKey("fee_heads.id"), nullable=True)
    valid_from = Column(Date, nullable=True)
    valid_to = Column(Date, nullable=True)
    # pending | approved | rejected | cancelled
    status = Column(String(20), nullable=False, server_default=text("'pending'"))
    auto = Column(Boolean, nullable=False, server_default=text("false"))  # given by the sibling rule
    notes = Column(Text, nullable=True)
    workflow_instance_id = Column(Integer, nullable=True)
    requested_by_name = Column(String(200), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    created_at = _created()
    updated_at = _updated()

    student = relationship("Student")
    head = relationship("FeeHead")


class FeeLateRule(Base):
    __tablename__ = "fee_late_rules"
    __table_args__ = (Index("ix_fee_late_rules_company", "company_id"),)
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(120), nullable=False)
    kind = Column(String(10), nullable=False, server_default=text("'per_day'"))  # fixed | per_day
    amount = Column(MONEY, nullable=False)
    grace_days = Column(Integer, nullable=False, server_default=text("0"))
    max_amount = Column(MONEY, nullable=True)  # cap per invoice
    structure_id = Column(Integer, ForeignKey("fee_structures.id"), nullable=True)  # empty = all invoices
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = _created()
    updated_at = _updated()


# ── Invoices ─────────────────────────────────────────────────────────

class FeeInvoice(Base):
    __tablename__ = "fee_invoices"
    __table_args__ = (Index("ix_fee_invoices_company_student", "company_id", "student_id", "status"),
                      Index("ix_fee_invoices_company_due", "company_id", "due_date"),
                      Index("ix_fee_invoices_instalment", "company_id", "instalment_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    number = Column(String(60), nullable=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    session_id = Column(Integer, ForeignKey("academic_sessions.id"), nullable=True)
    program_id = Column(Integer, ForeignKey("academic_programs.id"), nullable=True)
    section_id = Column(Integer, ForeignKey("academic_sections.id"), nullable=True)
    structure_id = Column(Integer, ForeignKey("fee_structures.id"), nullable=True)
    instalment_id = Column(Integer, ForeignKey("fee_instalments.id"), nullable=True)
    title = Column(String(200), nullable=False)
    issue_date = Column(Date, nullable=False)
    due_date = Column(Date, nullable=False)
    gross = _zero()        # before concession and tax
    concession = _zero()
    tax = _zero()
    total = _zero()        # gross - concession + tax (+ debit notes)
    credited = _zero()     # credit notes
    paid = _zero()
    # open | partial | paid | cancelled
    status = Column(String(20), nullable=False, server_default=text("'open'"))
    source = Column(String(20), nullable=False, server_default=text("'structure'"))  # structure | charge | manual
    notes = Column(Text, nullable=True)
    cancel_reason = Column(Text, nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_by_name = Column(String(200), nullable=True)
    created_by_name = Column(String(200), nullable=True)
    created_at = _created()
    updated_at = _updated()

    lines = relationship("FeeInvoiceLine", back_populates="invoice", order_by="FeeInvoiceLine.sort_order",
                         cascade="all, delete-orphan")
    student = relationship("Student")
    section = relationship("Section")
    program = relationship("Program")

    @property
    def balance(self):
        return (self.total or 0) - (self.credited or 0) - (self.paid or 0)


class FeeInvoiceLine(Base):
    __tablename__ = "fee_invoice_lines"
    __table_args__ = (Index("ix_fee_invoice_lines_company", "company_id", "invoice_id"),
                      Index("ix_fee_invoice_lines_head", "company_id", "head_id"))
    id = Column(Integer, primary_key=True)
    company_id = _company()
    invoice_id = Column(Integer, ForeignKey("fee_invoices.id", ondelete="CASCADE"), nullable=False)
    head_id = Column(Integer, ForeignKey("fee_heads.id"), nullable=False)
    description = Column(String(200), nullable=False)
    amount = _zero()       # gross
    concession = _zero()
    concession_note = Column(String(200), nullable=True)
    tax_rate = Column(Numeric(5, 2), nullable=True)
    tax = _zero()
    net = _zero()          # amount - concession + tax
    credited = _zero()
    paid = _zero()
    refunded = _zero()     # deposits returned
    kind = Column(String(20), nullable=False, server_default=text("'fee'"))  # fee | late_fee | debit_note | charge
    sort_order = Column(Integer, nullable=False, server_default=text("1"))

    invoice = relationship("FeeInvoice", back_populates="lines")
    head = relationship("FeeHead")

    @property
    def due(self):
        return (self.net or 0) - (self.credited or 0) - (self.paid or 0)


class FeeAdjustment(Base):
    __tablename__ = "fee_adjustments"
    __table_args__ = (Index("ix_fee_adjustments_company", "company_id", "invoice_id"),)
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    invoice_id = Column(Integer, ForeignKey("fee_invoices.id"), nullable=False)
    number = Column(String(60), nullable=True)
    kind = Column(String(10), nullable=False)  # credit | debit
    head_id = Column(Integer, ForeignKey("fee_heads.id"), nullable=True)
    amount = Column(MONEY, nullable=False)
    reason = Column(Text, nullable=False)
    created_by_name = Column(String(200), nullable=True)
    created_at = _created()

    head = relationship("FeeHead")


# ── Payments ─────────────────────────────────────────────────────────

class FeePayment(Base):
    __tablename__ = "fee_payments"
    __table_args__ = (Index("ix_fee_payments_company_date", "company_id", "paid_on"),
                      Index("ix_fee_payments_company_student", "company_id", "student_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    number = Column(String(60), nullable=True)  # receipt number
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    paid_on = Column(Date, nullable=False)
    amount = Column(MONEY, nullable=False)  # money received (all modes, without wallet use)
    wallet_used = _zero()
    allocated = _zero()
    late_fee = _zero()
    advance = _zero()       # excess kept as advance
    mode = Column(String(20), nullable=False)  # main mode, or "split"
    source = Column(String(20), nullable=False, server_default=text("'counter'"))  # counter | online
    # paid | cancelled | bounced
    status = Column(String(20), nullable=False, server_default=text("'paid'"))
    notes = Column(Text, nullable=True)
    collected_by_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    collected_by_name = Column(String(200), nullable=True)
    cancel_reason = Column(Text, nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_by_name = Column(String(200), nullable=True)
    online_order_id = Column(Integer, nullable=True)
    created_at = _created()
    updated_at = _updated()

    modes = relationship("FeePaymentMode", back_populates="payment", cascade="all, delete-orphan")
    allocations = relationship("FeeAllocation", back_populates="payment")
    student = relationship("Student")


class FeePaymentMode(Base):
    __tablename__ = "fee_payment_modes"
    __table_args__ = (Index("ix_fee_payment_modes_company", "company_id", "payment_id"),)
    id = Column(Integer, primary_key=True)
    company_id = _company()
    payment_id = Column(Integer, ForeignKey("fee_payments.id", ondelete="CASCADE"), nullable=False)
    # cash | cheque | dd | upi | card | bank_transfer | online
    mode = Column(String(20), nullable=False)
    amount = Column(MONEY, nullable=False)
    reference = Column(String(120), nullable=True)  # cheque / DD / transaction number
    bank = Column(String(120), nullable=True)
    instrument_date = Column(Date, nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'cleared'"))  # cleared | bounced

    payment = relationship("FeePayment", back_populates="modes")


class FeeAllocation(Base):
    __tablename__ = "fee_allocations"
    __table_args__ = (Index("ix_fee_allocations_company", "company_id", "payment_id"),
                      Index("ix_fee_allocations_invoice", "company_id", "invoice_id"))
    id = Column(Integer, primary_key=True)
    company_id = _company()
    payment_id = Column(Integer, ForeignKey("fee_payments.id"), nullable=False)
    invoice_id = Column(Integer, ForeignKey("fee_invoices.id"), nullable=False)
    line_id = Column(Integer, ForeignKey("fee_invoice_lines.id"), nullable=False)
    amount = Column(MONEY, nullable=False)
    reversed = Column(Boolean, nullable=False, server_default=text("false"))

    payment = relationship("FeePayment", back_populates="allocations")
    invoice = relationship("FeeInvoice")
    line = relationship("FeeInvoiceLine")


class FeeWalletEntry(Base):
    __tablename__ = "fee_wallet_entries"
    __table_args__ = (Index("ix_fee_wallet_entries_company", "company_id", "student_id"),)
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    amount = Column(MONEY, nullable=False)  # + added, - used / refunded
    reason = Column(String(200), nullable=False)
    payment_id = Column(Integer, ForeignKey("fee_payments.id"), nullable=True)
    refund_id = Column(Integer, nullable=True)
    created_by_name = Column(String(200), nullable=True)
    created_at = _created()


class FeeRefund(Base):
    __tablename__ = "fee_refunds"
    __table_args__ = (Index("ix_fee_refunds_company", "company_id", "student_id", "status"),)
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    number = Column(String(60), nullable=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    source = Column(String(20), nullable=False, server_default=text("'wallet'"))  # wallet | deposit | payment
    payment_id = Column(Integer, ForeignKey("fee_payments.id"), nullable=True)
    amount = Column(MONEY, nullable=False)
    reason = Column(Text, nullable=False)
    # pending | approved | paid | rejected | cancelled
    status = Column(String(20), nullable=False, server_default=text("'pending'"))
    mode = Column(String(20), nullable=True)
    reference = Column(String(120), nullable=True)
    paid_on = Column(Date, nullable=True)
    workflow_instance_id = Column(Integer, nullable=True)
    requested_by_name = Column(String(200), nullable=True)
    paid_by_name = Column(String(200), nullable=True)
    created_at = _created()
    updated_at = _updated()

    student = relationship("Student")


# ── Online payments ──────────────────────────────────────────────────

class FeeGatewaySetting(Base):
    __tablename__ = "fee_gateway_settings"
    __table_args__ = (UniqueConstraint("company_id", name="uq_fee_gateway_settings_company"),)
    id = Column(Integer, primary_key=True)
    company_id = _company()
    provider = Column(String(20), nullable=False, server_default=text("'test'"))  # test|razorpay|stripe|payu|cashfree
    enabled = Column(Boolean, nullable=False, server_default=text("true"))
    test_mode = Column(Boolean, nullable=False, server_default=text("true"))
    config = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    secrets_encrypted = Column(Text, nullable=True)
    secret_hints = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    updated_by_name = Column(String(200), nullable=True)
    created_at = _created()
    updated_at = _updated()


class FeeOnlineOrder(Base):
    __tablename__ = "fee_online_orders"
    __table_args__ = (Index("ix_fee_online_orders_company", "company_id", "student_id"),
                      Index("ix_fee_online_orders_provider", "provider", "provider_order_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    portal_account_id = Column(Integer, nullable=True)
    invoice_ids = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    amount = Column(MONEY, nullable=False)
    currency = Column(String(10), nullable=False)
    provider = Column(String(20), nullable=False)
    test_mode = Column(Boolean, nullable=False, server_default=text("false"))
    provider_order_id = Column(String(120), nullable=True)
    provider_payment_id = Column(String(120), nullable=True)
    # created | paid | failed
    status = Column(String(20), nullable=False, server_default=text("'created'"))
    payment_id = Column(Integer, ForeignKey("fee_payments.id"), nullable=True)
    error = Column(Text, nullable=True)
    paid_at = Column(DateTime(timezone=True), nullable=True)
    created_at = _created()
    updated_at = _updated()


class FeeReminderLog(Base):
    __tablename__ = "fee_reminder_logs"
    __audit__ = False  # high volume; the reminder itself is logged here
    __table_args__ = (Index("ix_fee_reminder_logs_company", "company_id", "invoice_id"),
                      UniqueConstraint("invoice_id", "key", name="uq_fee_reminder_logs_key"))
    id = Column(Integer, primary_key=True)
    company_id = _company()
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    invoice_id = Column(Integer, ForeignKey("fee_invoices.id"), nullable=True)
    key = Column(String(60), nullable=False)  # auto:before:3, auto:after:7, manual:<timestamp>
    kind = Column(String(20), nullable=False, server_default=text("'manual'"))  # manual | auto
    recipients = Column(Integer, nullable=False, server_default=text("0"))
    sent_by_name = Column(String(200), nullable=True)
    created_at = _created()


# ── Cash book ────────────────────────────────────────────────────────

class FeeLedgerCategory(Base):
    __tablename__ = "fee_ledger_categories"
    __table_args__ = (UniqueConstraint("company_id", "kind", "name", name="uq_fee_ledger_categories_name"),
                      Index("ix_fee_ledger_categories_company", "company_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    kind = Column(String(10), nullable=False)  # income | expense
    name = Column(String(120), nullable=False)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = _created()
    updated_at = _updated()


class FeeLedgerEntry(Base):
    __tablename__ = "fee_ledger_entries"
    __table_args__ = (Index("ix_fee_ledger_entries_company", "company_id", "entry_date"),)
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    number = Column(String(60), nullable=True)  # voucher number
    kind = Column(String(10), nullable=False)  # income | expense
    category_id = Column(Integer, ForeignKey("fee_ledger_categories.id"), nullable=True)
    entry_date = Column(Date, nullable=False)
    amount = Column(MONEY, nullable=False)
    mode = Column(String(20), nullable=False, server_default=text("'cash'"))
    reference = Column(String(120), nullable=True)
    party = Column(String(200), nullable=True)  # vendor / payer
    description = Column(Text, nullable=True)
    attachments = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    status = Column(String(20), nullable=False, server_default=text("'active'"))  # active | cancelled
    cancel_reason = Column(Text, nullable=True)
    created_by_name = Column(String(200), nullable=True)
    created_at = _created()
    updated_at = _updated()

    category = relationship("FeeLedgerCategory")


TABLES = ["fee_heads", "fee_categories", "fee_structures", "fee_structure_items", "fee_instalments", "fee_opt_ins",
          "fee_concession_types", "fee_concessions", "fee_late_rules", "fee_invoices", "fee_invoice_lines",
          "fee_adjustments", "fee_payments", "fee_payment_modes", "fee_allocations", "fee_wallet_entries",
          "fee_refunds", "fee_gateway_settings", "fee_online_orders", "fee_reminder_logs", "fee_ledger_categories",
          "fee_ledger_entries"]
