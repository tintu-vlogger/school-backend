"""Expense claims, travel advances and mileage.

expense_categories   kinds of expense with limits by grade and location, receipt rules,
                     mileage rates, tax treatment and how they are paid
expense_claims       a claim (several lines) by an employee, approved through the workflow engine
expense_lines        one bill: date, amount, GST details, receipt, distance for mileage
expense_advances     money paid ahead (travel advance) and settled against claims
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
    return (Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
            Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False))


class ExpenseCategory(Base):
    __tablename__ = "expense_categories"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_expense_categories_company_code"),
        Index("ix_expense_categories_company", "company_id", "active", "sort_order"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    code = Column(String(30), nullable=False)
    name = Column(String(120), nullable=False)
    icon = Column(String(40), nullable=False, server_default=text("'receipt'"))
    description = Column(Text, nullable=True)
    # amount | mileage | per_day
    kind = Column(String(20), nullable=False, server_default=text("'amount'"))
    # [{grade_ids: [], location_ids: [], per_claim, per_month, per_day}]  first match wins; empty lists = everyone
    limits = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    default_limit = Column(Numeric(14, 2), nullable=True)     # per claim line when no rule matches
    monthly_limit = Column(Numeric(14, 2), nullable=True)
    receipt_required_above = Column(Numeric(14, 2), nullable=True)
    mileage_rates = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))  # {car: 12, two_wheeler: 5}
    gst_applicable = Column(Boolean, nullable=False, server_default=text("true"))
    taxable = Column(Boolean, nullable=False, server_default=text("false"))
    pay_via = Column(String(20), nullable=False, server_default=text("'payroll'"))  # payroll | direct
    component_code = Column(String(30), nullable=True)         # payroll component (e.g. FBP fuel)
    gl_code = Column(String(40), nullable=True)
    sort_order = Column(Integer, nullable=False, server_default=text("100"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class ExpenseAdvance(Base):
    __tablename__ = "expense_advances"
    __table_args__ = (Index("ix_expense_advances_company_employee", "company_id", "employee_id", "status"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    number = Column(String(40), nullable=True)
    purpose = Column(String(300), nullable=False)
    amount = Column(Numeric(14, 2), nullable=False)
    needed_by = Column(Date, nullable=True)
    # pending | approved | paid | settled | rejected | cancelled
    status = Column(String(20), nullable=False, server_default=text("'pending'"))
    settled_amount = Column(Numeric(14, 2), nullable=False, server_default=text("0"))
    recovered_amount = Column(Numeric(14, 2), nullable=False, server_default=text("0"))
    paid_on = Column(Date, nullable=True)
    payment_ref = Column(String(80), nullable=True)
    workflow_instance_id = Column(Integer, ForeignKey("workflow_instances.id"), nullable=True)
    created_at, updated_at = _stamps()


class ExpenseClaim(Base):
    __tablename__ = "expense_claims"
    __table_args__ = (
        Index("ix_expense_claims_company_employee", "company_id", "employee_id", "status"),
        Index("ix_expense_claims_company_status", "company_id", "status", "pay_via"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    number = Column(String(40), nullable=True)
    title = Column(String(200), nullable=False)
    trip_from = Column(Date, nullable=True)
    trip_to = Column(Date, nullable=True)
    # draft | pending | approved | rejected | paid | cancelled
    status = Column(String(20), nullable=False, server_default=text("'draft'"))
    total = Column(Numeric(14, 2), nullable=False, server_default=text("0"))
    approved_total = Column(Numeric(14, 2), nullable=False, server_default=text("0"))
    taxable_total = Column(Numeric(14, 2), nullable=False, server_default=text("0"))
    pay_via = Column(String(20), nullable=False, server_default=text("'payroll'"))
    advance_id = Column(Integer, ForeignKey("expense_advances.id"), nullable=True)
    advance_adjusted = Column(Numeric(14, 2), nullable=False, server_default=text("0"))
    policy_flags = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    workflow_instance_id = Column(Integer, ForeignKey("workflow_instances.id"), nullable=True)
    pay_run_id = Column(Integer, ForeignKey("pay_runs.id"), nullable=True)
    paid_on = Column(Date, nullable=True)
    payment_ref = Column(String(80), nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    decided_note = Column(Text, nullable=True)
    created_at, updated_at = _stamps()

    lines = relationship("ExpenseLine", back_populates="claim", order_by="ExpenseLine.expense_date",
                         cascade="all, delete-orphan")


class ExpenseLine(Base):
    __tablename__ = "expense_lines"
    __table_args__ = (Index("ix_expense_lines_company_claim", "company_id", "claim_id"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    claim_id = Column(Integer, ForeignKey("expense_claims.id"), nullable=False)
    category_id = Column(Integer, ForeignKey("expense_categories.id"), nullable=False)
    expense_date = Column(Date, nullable=False)
    description = Column(String(300), nullable=True)
    merchant = Column(String(200), nullable=True)
    amount = Column(Numeric(14, 2), nullable=False)
    approved_amount = Column(Numeric(14, 2), nullable=True)
    distance_km = Column(Numeric(10, 2), nullable=True)
    vehicle = Column(String(20), nullable=True)
    from_place = Column(String(150), nullable=True)
    to_place = Column(String(150), nullable=True)
    gstin = Column(String(15), nullable=True)
    invoice_number = Column(String(50), nullable=True)
    gst_amount = Column(Numeric(14, 2), nullable=True)
    receipt_name = Column(String(255), nullable=True)
    receipt = Column(Text, nullable=True)  # data URL
    flags = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    created_at, updated_at = _stamps()

    claim = relationship("ExpenseClaim", back_populates="lines")
