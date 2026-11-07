"""Loans and salary advances.

loan_types         kinds of loan (personal, vehicle, emergency, festival advance, salary advance)
                   with eligibility, limits, interest and instalments
loans              a loan for an employee: request, approval, disbursal and balance
loan_instalments   the instalment plan; each one is deducted in a pay run, skipped, prepaid
                   or recovered in the final settlement
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


class LoanType(Base):
    __tablename__ = "loan_types"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_loan_types_company_code"),
        Index("ix_loan_types_company", "company_id", "active"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    code = Column(String(30), nullable=False)
    name = Column(String(120), nullable=False)
    kind = Column(String(20), nullable=False, server_default=text("'loan'"))  # loan | advance
    description = Column(Text, nullable=True)
    max_amount = Column(Numeric(14, 2), nullable=True)
    max_salary_multiple = Column(Numeric(6, 2), nullable=True)   # times monthly gross
    max_instalments = Column(Integer, nullable=False, server_default=text("12"))
    interest_rate = Column(Numeric(6, 2), nullable=False, server_default=text("0"))
    interest_method = Column(String(10), nullable=False, server_default=text("'reducing'"))  # reducing | flat
    min_service_months = Column(Integer, nullable=False, server_default=text("0"))
    eligibility = Column(Text, nullable=True)                      # condition (core/expressions)
    max_active = Column(Integer, nullable=False, server_default=text("1"))
    medical = Column(Boolean, nullable=False, server_default=text("false"))  # exempt from the perquisite
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class Loan(Base):
    __tablename__ = "loans"
    __table_args__ = (Index("ix_loans_company_employee", "company_id", "employee_id", "status"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    loan_type_id = Column(Integer, ForeignKey("loan_types.id"), nullable=False)
    number = Column(String(40), nullable=True)
    purpose = Column(String(300), nullable=True)
    principal = Column(Numeric(14, 2), nullable=False)
    interest_rate = Column(Numeric(6, 2), nullable=False, server_default=text("0"))
    interest_method = Column(String(10), nullable=False, server_default=text("'reducing'"))
    instalments = Column(Integer, nullable=False)
    emi = Column(Numeric(14, 2), nullable=False, server_default=text("0"))
    start_month = Column(Date, nullable=False)          # first of the month of the first instalment
    # pending | approved | active | closed | rejected | cancelled
    status = Column(String(20), nullable=False, server_default=text("'pending'"))
    disbursed_on = Column(Date, nullable=True)
    disbursal_ref = Column(String(80), nullable=True)
    outstanding = Column(Numeric(14, 2), nullable=False, server_default=text("0"))
    closed_on = Column(Date, nullable=True)
    workflow_instance_id = Column(Integer, ForeignKey("workflow_instances.id"), nullable=True)
    pending_action = Column(JSONB, nullable=True)   # {kind: skip|prepay, ... , instance_id}
    note = Column(Text, nullable=True)
    created_at, updated_at = _stamps()

    instalments_rows = relationship("LoanInstalment", back_populates="loan", order_by="LoanInstalment.seq",
                                    cascade="all, delete-orphan")


class LoanInstalment(Base):
    __tablename__ = "loan_instalments"
    __table_args__ = (Index("ix_loan_instalments_company_loan", "company_id", "loan_id", "due_month"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    loan_id = Column(Integer, ForeignKey("loans.id"), nullable=False)
    seq = Column(Integer, nullable=False)
    due_month = Column(Date, nullable=False)
    principal = Column(Numeric(14, 2), nullable=False)
    interest = Column(Numeric(14, 2), nullable=False, server_default=text("0"))
    emi = Column(Numeric(14, 2), nullable=False)
    # due | deducted | skipped | prepaid | recovered | waived
    status = Column(String(20), nullable=False, server_default=text("'due'"))
    pay_run_id = Column(Integer, ForeignKey("pay_runs.id"), nullable=True)
    settled_on = Column(Date, nullable=True)
    note = Column(String(300), nullable=True)
    created_at, updated_at = _stamps()

    loan = relationship("Loan", back_populates="instalments_rows")
