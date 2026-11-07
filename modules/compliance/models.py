"""Compliance records: things HR marks as done (registers, renewals, annual reports) and
law notices they have read. Payments and returns (PF, ESI, PT, LWF, TDS) are tracked by
the payroll module's challans and only shown here."""
from sqlalchemy import Column, Date, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID

from core.db import Base


class ComplianceRecord(Base):
    __tablename__ = "compliance_records"
    __table_args__ = (Index("uq_compliance_records_company_key", "company_id", "key", unique=True),)

    id = Column(Integer, primary_key=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    key = Column(String(120), nullable=False)          # e.g. registers:2026-10, posh_report:2025, notice:pt-ka-2026
    status = Column(String(20), nullable=False, server_default=text("'done'"))   # done | not_applicable | read
    done_on = Column(Date, nullable=True)
    reference = Column(String(120), nullable=True)
    notes = Column(Text, nullable=True)
    by_name = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
