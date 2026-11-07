"""Salary bank transfer tables (company-owned, RLS).

bank_debit_accounts   the company's own accounts salaries are paid from, per legal entity,
                      each with the bank file format its bank accepts
bank_file_formats     a company's own (custom) file layouts built in payroll settings
bank_file_batches     every bank file made: who, when, for which run and account, totals,
                      a fingerprint of the content and the file itself (kept for the audit)
"""
from sqlalchemy import (Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, LargeBinary, Numeric, String,
                        Text, text)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from core.db import Base


def _uuid():
    return Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))


def _company():
    return Column(Integer, ForeignKey("companies.id"), nullable=False)


class BankDebitAccount(Base):
    __tablename__ = "bank_debit_accounts"
    __table_args__ = (Index("ix_bank_debit_accounts_company", "company_id", "legal_entity_id", "active"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    legal_entity_id = Column(Integer, ForeignKey("legal_entities.id"), nullable=True)  # empty = any entity
    label = Column(String(120), nullable=False)
    bank_name = Column(String(150), nullable=True)
    account_number = Column(String(30), nullable=False)
    ifsc = Column(String(11), nullable=False)
    account_name = Column(String(200), nullable=True)     # name on the account
    format_key = Column(String(80), nullable=False)       # built-in key or "custom:<uuid>"
    client_code = Column(String(40), nullable=True)       # corporate / client code given by the bank
    corporate_id = Column(String(40), nullable=True)      # login corporate id (some banks print it in the file)
    is_default = Column(Boolean, nullable=False, server_default=text("false"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class BankFileFormat(Base):
    __tablename__ = "bank_file_formats"
    __table_args__ = (Index("ix_bank_file_formats_company", "company_id", "active"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(120), nullable=False)
    based_on = Column(String(80), nullable=True)          # built-in key it was copied from
    spec = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_by_name = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class BankFileBatch(Base):
    __tablename__ = "bank_file_batches"
    __audit__ = False   # the row itself is the record; creation and downloads are written with audit.event
    __table_args__ = (Index("ix_bank_file_batches_company_run", "company_id", "pay_run_id", "created_at"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    pay_run_id = Column(Integer, ForeignKey("pay_runs.id"), nullable=True)
    debit_account_id = Column(Integer, ForeignKey("bank_debit_accounts.id"), nullable=True)
    legal_entity_id = Column(Integer, nullable=True)
    format_key = Column(String(80), nullable=False)
    format_name = Column(String(150), nullable=False)
    txn_group = Column(String(20), nullable=False, server_default=text("'all'"))   # all | internal | other | neft | rtgs | imps
    file_name = Column(String(200), nullable=False)
    content_type = Column(String(80), nullable=False, server_default=text("'text/plain'"))
    content = Column(LargeBinary, nullable=False)
    sha256 = Column(String(64), nullable=False)
    record_count = Column(Integer, nullable=False, server_default=text("0"))
    total_amount = Column(Numeric(16, 2), nullable=False, server_default=text("0"))
    value_date = Column(Date, nullable=True)
    released_only = Column(Boolean, nullable=False, server_default=text("false"))
    debit_account_masked = Column(String(40), nullable=True)
    payslip_ids = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    left_out = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))     # [{name, code, reason}]
    status = Column(String(20), nullable=False, server_default=text("'generated'"))  # generated | superseded
    download_count = Column(Integer, nullable=False, server_default=text("0"))
    last_downloaded_at = Column(DateTime(timezone=True), nullable=True)
    created_by_name = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
