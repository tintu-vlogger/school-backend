"""Shift master, shift patterns, employee work arrangements and women's night-shift consent.

Times of day (shift start and end, breaks, core hours) are local to the company. A shift
whose end is not after its start crosses midnight (a night shift).
"""
from sqlalchemy import (Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, Time,
                        UniqueConstraint, func, text)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from core.db import Base


class Shift(Base):
    __tablename__ = "shifts"
    __table_args__ = (
        UniqueConstraint("company_id", "name", name="uq_shifts_company_name"),
        Index("ix_shifts_company_active", "company_id", "active"),
    )

    id = Column(Integer, primary_key=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    name = Column(String(80), nullable=False)
    code = Column(String(12), nullable=True)  # short label for the roster grid, e.g. "G", "N"
    color = Column(String(20), nullable=False, server_default=text("'indigo'"))
    start_time = Column(Time, nullable=False)
    end_time = Column(Time, nullable=False)
    # [{"name": "Lunch", "start": "13:00", "end": "13:30", "paid": false}]
    breaks = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    grace_in_minutes = Column(Integer, nullable=False, server_default=text("10"))
    grace_out_minutes = Column(Integer, nullable=False, server_default=text("10"))
    # Minimum worked hours for a half and a full day; NULL means "use the attendance rule".
    half_day_hours = Column(Numeric(4, 2), nullable=True)
    full_day_hours = Column(Numeric(4, 2), nullable=True)
    is_flexible = Column(Boolean, nullable=False, server_default=text("false"))
    core_start = Column(Time, nullable=True)  # flexible shifts: must be present between these
    core_end = Column(Time, nullable=True)
    # Overtime counts only for time worked this many minutes after the shift's hours.
    overtime_after_minutes = Column(Integer, nullable=False, server_default=text("30"))
    allowance_amount = Column(Numeric(10, 2), nullable=False, server_default=text("0"))
    allowance_min_hours = Column(Numeric(4, 2), nullable=True)
    # Weekdays off for people on this shift when no work arrangement says otherwise (0 = Monday).
    weekly_offs = Column(JSONB, nullable=False, server_default=text("'[5, 6]'::jsonb"))
    is_default = Column(Boolean, nullable=False, server_default=text("false"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class ShiftPattern(Base):
    """A repeating schedule.
    kind = weekly:        slots = {"0": shift_id | null, ... "6": ...}  (null = weekly off)
    kind = week_of_month: slots = {"1": {"0": id, ...}, ... "5": {...}} (week 1 to 5 of the month)
    kind = cycle:         slots = [shift_id | null, ...]                (N-day cycle from the start date)
    """
    __tablename__ = "shift_patterns"
    __table_args__ = (Index("ix_shift_patterns_company", "company_id", "active"),)

    id = Column(Integer, primary_key=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    name = Column(String(100), nullable=False)
    kind = Column(String(20), nullable=False)
    slots = Column(JSONB, nullable=False)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class WorkArrangement(Base):
    """An employee's usual week from a date: working days, default shift and whether they
    need to punch. The row with the latest effective_from on or before a date applies."""
    __tablename__ = "work_arrangements"
    __table_args__ = (
        Index("ix_work_arrangements_company_employee", "company_id", "employee_id", "effective_from"),
        UniqueConstraint("employee_id", "effective_from", name="uq_work_arrangements_employee_from"),
    )

    id = Column(Integer, primary_key=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    effective_from = Column(Date, nullable=False)
    shift_id = Column(Integer, ForeignKey("shifts.id"), nullable=True)
    # Weekdays worked (0 = Monday). Alternate Saturdays: {"5": [1, 3, 5]} = weeks of the month worked.
    working_days = Column(JSONB, nullable=False, server_default=text("'[0, 1, 2, 3, 4]'::jsonb"))
    alternate_days = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    punch_required = Column(Boolean, nullable=False, server_default=text("true"))
    # pick the shift each day from the check-in time (closest shift start within a window)
    auto_shift = Column(Boolean, nullable=False, server_default=text("false"))
    attendance_rule_id = Column(Integer, ForeignKey("attendance_rules.id"), nullable=True)
    note = Column(String(300), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    shift = relationship("Shift")


class NightShiftConsent(Base):
    """A woman's written consent to work night shifts (OSH Code, 2020 s.43), with the
    safety arrangements agreed. Rosters on night shifts are blocked without a valid one."""
    __tablename__ = "night_shift_consents"
    __table_args__ = (Index("ix_night_shift_consents_company_employee", "company_id", "employee_id"),)

    id = Column(Integer, primary_key=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    consent_date = Column(Date, nullable=False)
    valid_from = Column(Date, nullable=False)
    valid_to = Column(Date, nullable=True)
    safety_measures = Column(Text, nullable=True)  # transport, escort, CCTV ...
    document_url = Column(Text, nullable=True)
    withdrawn_at = Column(DateTime(timezone=True), nullable=True)
    recorded_by_name = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
