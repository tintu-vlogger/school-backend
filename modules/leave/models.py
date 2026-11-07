"""Leave, holidays and the work calendar.

leave_settings          one row per company: leave year, calendar visibility, cancellation,
                        extension and comp-off settings
leave_types             a kind of leave (name, code, colour, icon); its rules live in versions
leave_policy_versions   effective-dated rules of a leave type; editing creates a new version,
                        old periods keep using the version that was in force then
leave_ledger            every movement of a balance (opening, accrual, availed, adjustment,
                        encashment, lapse, carry forward, expiry, grant); balance = sum(days)
leave_requests          leave asked for, with a day-by-day breakdown and its approval
comp_off_requests       days off earned by working on a weekly off or a holiday
holiday_calendars       a list of holidays that applies to a location, a state or a group
holidays                one holiday (compulsory, festival or optional) in a calendar
work_week_patterns      which weekdays are working, off or half days (2nd/4th Saturday rules)
leave_runs              log of accrual, year-end and expiry runs (with their results)
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


class LeaveSettings(Base):
    __tablename__ = "leave_settings"
    __table_args__ = (UniqueConstraint("company_id", name="uq_leave_settings_company"),)

    id = Column(Integer, primary_key=True)
    company_id = _company()
    config = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at, updated_at = _stamps()


class LeaveType(Base):
    __tablename__ = "leave_types"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_leave_types_company_code"),
        Index("ix_leave_types_company_active", "company_id", "active", "sort_order"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    code = Column(String(20), nullable=False)
    name = Column(String(100), nullable=False)
    color = Column(String(20), nullable=False, server_default=text("'#4f46e5'"))
    icon = Column(String(40), nullable=False, server_default=text("'plane'"))
    description = Column(Text, nullable=True)
    # standard | comp_off | lop | optional_holiday  (comp_off and optional_holiday have their own flows)
    kind = Column(String(20), nullable=False, server_default=text("'standard'"))
    is_system = Column(Boolean, nullable=False, server_default=text("false"))
    sort_order = Column(Integer, nullable=False, server_default=text("100"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    archived_at = Column(DateTime(timezone=True), nullable=True)
    created_at, updated_at = _stamps()

    versions = relationship("LeavePolicyVersion", back_populates="leave_type", order_by="LeavePolicyVersion.version")


class LeavePolicyVersion(Base):
    __tablename__ = "leave_policy_versions"
    __table_args__ = (
        UniqueConstraint("leave_type_id", "version", name="uq_leave_policy_versions_type_version"),
        Index("ix_leave_policy_versions_company_type", "company_id", "leave_type_id", "effective_from"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    leave_type_id = Column(Integer, ForeignKey("leave_types.id"), nullable=False)
    version = Column(Integer, nullable=False)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)  # set when a later version starts
    rules = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    change_note = Column(String(300), nullable=True)
    created_by_name = Column(String(200), nullable=True)
    created_at, updated_at = _stamps()

    leave_type = relationship("LeaveType", back_populates="versions")


class LeaveLedgerEntry(Base):
    """One movement of a leave balance. Positive adds, negative takes away. `idem_key`
    makes scheduled runs safe to repeat."""
    __tablename__ = "leave_ledger"
    __table_args__ = (
        Index("ix_leave_ledger_company_employee_type", "company_id", "employee_id", "leave_type_id", "year"),
        Index("ix_leave_ledger_company_kind", "company_id", "kind", "entry_date"),
        Index("ix_leave_ledger_request", "request_id"),
        Index("uq_leave_ledger_company_idem", "company_id", "idem_key", unique=True,
              postgresql_where=text("idem_key IS NOT NULL")),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    leave_type_id = Column(Integer, ForeignKey("leave_types.id"), nullable=False)
    year = Column(Integer, nullable=False)  # leave year label: the calendar year in which it starts
    entry_date = Column(Date, nullable=False)
    # opening | accrual | availed | restored | adjustment | encashed | lapsed | carried_out |
    # carry_forward | expired | grant | comp_off
    kind = Column(String(20), nullable=False)
    days = Column(Numeric(8, 2), nullable=False)
    expires_on = Column(Date, nullable=True)  # credits that expire (carried leave, comp off)
    request_id = Column(Integer, ForeignKey("leave_requests.id"), nullable=True)
    policy_version_id = Column(Integer, ForeignKey("leave_policy_versions.id"), nullable=True)
    idem_key = Column(String(160), nullable=True)
    note = Column(String(300), nullable=True)
    meta = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_by_name = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class LeaveRequest(Base):
    __tablename__ = "leave_requests"
    __table_args__ = (
        Index("ix_leave_requests_company_employee", "company_id", "employee_id", "from_date"),
        Index("ix_leave_requests_company_status", "company_id", "status", "from_date"),
        Index("ix_leave_requests_company_dates", "company_id", "from_date", "to_date"),
        Index("ix_leave_requests_parent", "parent_id"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    reference_no = Column(String(60), nullable=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    leave_type_id = Column(Integer, ForeignKey("leave_types.id"), nullable=False)
    policy_version_id = Column(Integer, ForeignKey("leave_policy_versions.id"), nullable=True)
    kind = Column(String(20), nullable=False, server_default=text("'leave'"))  # leave | extension
    parent_id = Column(Integer, ForeignKey("leave_requests.id"), nullable=True)
    from_date = Column(Date, nullable=False)
    to_date = Column(Date, nullable=False)
    unit = Column(String(12), nullable=False, server_default=text("'day'"))  # day | half_day | quarter_day | hour
    half = Column(String(10), nullable=True)  # first | second (half and quarter days)
    hours = Column(Numeric(5, 2), nullable=True)
    days = Column(Numeric(8, 2), nullable=False)  # total counted, including sandwich days
    paid_days = Column(Numeric(8, 2), nullable=False, server_default=text("0"))
    lop_days = Column(Numeric(8, 2), nullable=False, server_default=text("0"))
    # [{date, amount, kind: working|half_working|weekend|holiday|sandwich, lop, cancelled, holiday}]
    breakdown = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    grant_option = Column(String(40), nullable=True)  # e.g. maternity "birth", "adoption"
    reason = Column(Text, nullable=True)
    # pending | approved | rejected | cancelled | cancel_pending
    status = Column(String(20), nullable=False, server_default=text("'pending'"))
    attachment_name = Column(String(255), nullable=True)
    attachment = Column(Text, nullable=True)  # data URL
    workflow_instance_id = Column(Integer, ForeignKey("workflow_instances.id"), nullable=True)
    cancel_instance_id = Column(Integer, ForeignKey("workflow_instances.id"), nullable=True)
    cancel_dates = Column(JSONB, nullable=True)  # dates asked to cancel (pending cancellation)
    cancel_reason = Column(Text, nullable=True)
    applied_by_name = Column(String(200), nullable=True)
    on_behalf = Column(Boolean, nullable=False, server_default=text("false"))
    decided_at = Column(DateTime(timezone=True), nullable=True)
    history = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))  # [{at, by, action, note}]
    created_at, updated_at = _stamps()

    leave_type = relationship("LeaveType")
    employee = relationship("Employee", foreign_keys=[employee_id])
    parent = relationship("LeaveRequest", remote_side=[id])


class CompOffRequest(Base):
    __tablename__ = "comp_off_requests"
    __table_args__ = (
        Index("ix_comp_off_requests_company_employee", "company_id", "employee_id", "worked_date"),
        Index("ix_comp_off_requests_company_status", "company_id", "status"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    worked_date = Column(Date, nullable=False)
    day_kind = Column(String(20), nullable=True)  # weekend | holiday
    unit = Column(String(12), nullable=False, server_default=text("'day'"))  # day | half_day
    days = Column(Numeric(5, 2), nullable=False)
    hours = Column(Numeric(5, 2), nullable=True)
    reason = Column(Text, nullable=True)
    source = Column(String(20), nullable=False, server_default=text("'request'"))  # request | attendance | admin
    status = Column(String(20), nullable=False, server_default=text("'pending'"))
    expires_on = Column(Date, nullable=True)
    workflow_instance_id = Column(Integer, ForeignKey("workflow_instances.id"), nullable=True)
    applied_by_name = Column(String(200), nullable=True)
    created_at, updated_at = _stamps()

    employee = relationship("Employee")


class HolidayCalendar(Base):
    __tablename__ = "holiday_calendars"
    __table_args__ = (Index("ix_holiday_calendars_company_active", "company_id", "active", "priority"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(150), nullable=False)
    description = Column(Text, nullable=True)
    # same shape as leave type applicability: {locations, states, departments, ...}
    applicability = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    is_default = Column(Boolean, nullable=False, server_default=text("false"))
    priority = Column(Integer, nullable=False, server_default=text("100"))
    optional_limit = Column(Integer, nullable=False, server_default=text("2"))  # optional holidays per year
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()

    holidays = relationship("Holiday", back_populates="calendar", order_by="Holiday.date")


class Holiday(Base):
    __tablename__ = "holidays"
    __table_args__ = (
        UniqueConstraint("calendar_id", "date", "name", name="uq_holidays_calendar_date_name"),
        Index("ix_holidays_company_date", "company_id", "date"),
        Index("ix_holidays_calendar", "calendar_id", "date"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    calendar_id = Column(Integer, ForeignKey("holiday_calendars.id"), nullable=False)
    name = Column(String(150), nullable=False)
    date = Column(Date, nullable=False)
    kind = Column(String(20), nullable=False, server_default=text("'festival'"))  # national | festival | optional
    description = Column(Text, nullable=True)
    created_at, updated_at = _stamps()

    calendar = relationship("HolidayCalendar", back_populates="holidays")


class WorkWeekPattern(Base):
    __tablename__ = "work_week_patterns"
    __table_args__ = (Index("ix_work_week_patterns_company_active", "company_id", "active", "priority"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(150), nullable=False)
    description = Column(Text, nullable=True)
    # 7 entries, Monday first: {"mode": "working"|"off"|"half", "off_weeks": [2, 4]}
    days = Column(JSONB, nullable=False)
    applicability = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    is_default = Column(Boolean, nullable=False, server_default=text("false"))
    priority = Column(Integer, nullable=False, server_default=text("100"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class LeaveRun(Base):
    __tablename__ = "leave_runs"
    __table_args__ = (Index("ix_leave_runs_company_created", "company_id", "kind", text("created_at DESC")),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    kind = Column(String(20), nullable=False)  # accrual | year_end | expiry | import
    label = Column(String(150), nullable=True)
    as_of = Column(Date, nullable=True)
    year = Column(Integer, nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'done'"))
    entries = Column(Integer, nullable=False, server_default=text("0"))
    result = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    ran_by_name = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class BradfordAlert(Base):
    """The highest Bradford level an employee has been alerted for, so an alert goes out
    once per step up rather than every day."""
    __tablename__ = "leave_bradford_alerts"
    __table_args__ = (Index("ix_leave_bradford_alerts_company_employee", "company_id", "employee_id"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    level = Column(Integer, nullable=False)
    label = Column(String(80), nullable=True)
    score = Column(Integer, nullable=False)
    spells = Column(Integer, nullable=False)
    days = Column(Numeric(6, 2), nullable=False)
    as_of = Column(Date, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class LeaveExpiryReminder(Base):
    """A reminder sent before an expiring credit (comp off, carried leave) lapses."""
    __tablename__ = "leave_expiry_reminders"
    __audit__ = False
    __table_args__ = (UniqueConstraint("ledger_entry_id", "days_before", name="uq_leave_expiry_reminders_entry"),
                      Index("ix_leave_expiry_reminders_company", "company_id", "employee_id"))

    id = Column(Integer, primary_key=True)
    company_id = _company()
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    ledger_entry_id = Column(Integer, ForeignKey("leave_ledger.id"), nullable=False)
    days_before = Column(Integer, nullable=False)
    expiring_days = Column(Numeric(6, 2), nullable=False)
    expires_on = Column(Date, nullable=False)
    emailed = Column(Boolean, nullable=False, server_default=text("false"))
    sent_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
