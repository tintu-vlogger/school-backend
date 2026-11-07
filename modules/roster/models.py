from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from core.db import Base


class RosterEntry(Base):
    """One employee's plan for one day: a shift (working), a weekly off, leave or a half
    day, overriding their usual work arrangement. Draft entries only count once published."""
    __tablename__ = "roster_entries"
    __table_args__ = (
        UniqueConstraint("employee_id", "date", name="uq_roster_entries_employee_date"),
        Index("ix_roster_entries_company_employee", "company_id", "employee_id"),
        Index("ix_roster_entries_company_date", "company_id", "date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    company_id = Column(Integer, ForeignKey("companies.id", name="fk_roster_entries_company_id"), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    date = Column(Date, nullable=False)
    status = Column(String(20), nullable=False)  # working, off, leave, half_day
    shift_id = Column(Integer, ForeignKey("shifts.id", name="fk_roster_entries_shift_id"), nullable=True)
    timing_override = Column(JSONB, nullable=True)
    note = Column(String(255), nullable=True)
    published = Column(Boolean, nullable=False, server_default=text("false"))
    published_at = Column(DateTime(timezone=True), nullable=True)
    # manual, pattern, swap, change_request, import
    source = Column(String(20), nullable=False, server_default=text("'manual'"))
    pattern_id = Column(Integer, ForeignKey("shift_patterns.id", name="fk_roster_entries_pattern_id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    employee = relationship("Employee", back_populates="roster_entries")
    shift = relationship("Shift")
