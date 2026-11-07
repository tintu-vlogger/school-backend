"""Clients, projects, tasks, time logs and weekly timesheets."""
from sqlalchemy import (Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, Time,
                        UniqueConstraint, func, text)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from core.db import Base


def _id():
    return Column(Integer, primary_key=True)


def _uuid():
    return Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))


def _company():
    return Column(Integer, ForeignKey("companies.id"), nullable=False)


def _stamps():
    return (Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
            Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False))


class TsClient(Base):
    __tablename__ = "ts_clients"
    __table_args__ = (Index("ix_ts_clients_company", "company_id", "active"),)

    id = _id()
    uuid = _uuid()
    company_id = _company()
    name = Column(String(150), nullable=False)
    contact_name = Column(String(150), nullable=True)
    email = Column(String(255), nullable=True)
    phone = Column(String(30), nullable=True)
    gstin = Column(String(15), nullable=True)
    address = Column(Text, nullable=True)
    state_code = Column(String(4), nullable=True)  # GST state code (place of supply), e.g. "29" for Karnataka
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class TsProject(Base):
    __tablename__ = "ts_projects"
    __table_args__ = (Index("ix_ts_projects_company", "company_id", "status"),)

    id = _id()
    uuid = _uuid()
    company_id = _company()
    client_id = Column(Integer, ForeignKey("ts_clients.id"), nullable=True)
    name = Column(String(150), nullable=False)
    code = Column(String(30), nullable=True)
    owner_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    billable = Column(Boolean, nullable=False, server_default=text("true"))
    budget_hours = Column(Numeric(10, 2), nullable=True)
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'active'"))  # active, on_hold, completed
    member_ids = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))  # empty = everyone
    description = Column(Text, nullable=True)
    created_at, updated_at = _stamps()

    client = relationship("TsClient")


class TsTask(Base):
    __tablename__ = "ts_tasks"
    __table_args__ = (Index("ix_ts_tasks_company_project", "company_id", "project_id"),)

    id = _id()
    uuid = _uuid()
    company_id = _company()
    project_id = Column(Integer, ForeignKey("ts_projects.id"), nullable=False)
    name = Column(String(150), nullable=False)
    owner_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    billable = Column(Boolean, nullable=False, server_default=text("true"))
    budget_hours = Column(Numeric(10, 2), nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'open'"))  # open, done
    created_at, updated_at = _stamps()

    project = relationship("TsProject")


class Timesheet(Base):
    """One employee's week (Monday to Sunday), submitted for approval as a whole and
    approved line by line."""
    __tablename__ = "ts_timesheets"
    __table_args__ = (
        UniqueConstraint("employee_id", "week_start", name="uq_ts_timesheets_employee_week"),
        Index("ix_ts_timesheets_company_status", "company_id", "status", "week_start"),
    )

    id = _id()
    uuid = _uuid()
    company_id = _company()
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    week_start = Column(Date, nullable=False)
    week_end = Column(Date, nullable=False)
    # draft, submitted, approved, partly_approved, rejected
    status = Column(String(20), nullable=False, server_default=text("'draft'"))
    total_hours = Column(Numeric(6, 2), nullable=False, server_default=text("0"))
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    workflow_instance_id = Column(Integer, ForeignKey("workflow_instances.id"), nullable=True)
    comment = Column(Text, nullable=True)
    created_at, updated_at = _stamps()


class TimeLog(Base):
    __tablename__ = "ts_time_logs"
    __table_args__ = (
        Index("ix_ts_time_logs_company_employee_date", "company_id", "employee_id", "date"),
        Index("ix_ts_time_logs_company_project", "company_id", "project_id"),
        Index("ix_ts_time_logs_timesheet", "timesheet_id"),
    )

    id = _id()
    uuid = _uuid()
    company_id = _company()
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    date = Column(Date, nullable=False)
    project_id = Column(Integer, ForeignKey("ts_projects.id"), nullable=False)
    task_id = Column(Integer, ForeignKey("ts_tasks.id"), nullable=True)
    hours = Column(Numeric(5, 2), nullable=False)
    start_time = Column(Time, nullable=True)
    end_time = Column(Time, nullable=True)
    description = Column(Text, nullable=True)
    billable = Column(Boolean, nullable=False, server_default=text("true"))
    timesheet_id = Column(Integer, ForeignKey("ts_timesheets.id"), nullable=True)
    # draft, submitted, approved, rejected
    status = Column(String(20), nullable=False, server_default=text("'draft'"))
    review_comment = Column(String(500), nullable=True)
    invoice_id = Column(Integer, ForeignKey("ts_invoices.id"), nullable=True)  # billed on this invoice
    created_at, updated_at = _stamps()

    project = relationship("TsProject")
    task = relationship("TsTask")


class TsBillingRate(Base):
    """Hourly billing rate for a project, a person, or a person on a project, from a date.
    The most specific rate in force on the day of the work wins."""
    __tablename__ = "ts_billing_rates"
    __table_args__ = (Index("ix_ts_billing_rates_company", "company_id", "project_id", "employee_id"),)

    id = _id()
    uuid = _uuid()
    company_id = _company()
    project_id = Column(Integer, ForeignKey("ts_projects.id"), nullable=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    rate = Column(Numeric(12, 2), nullable=False)
    effective_from = Column(Date, nullable=False)
    note = Column(String(200), nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class TsInvoice(Base):
    """An invoice to a client for approved billable hours in a period, with GST."""
    __tablename__ = "ts_invoices"
    __table_args__ = (Index("ix_ts_invoices_company", "company_id", "status", "issue_date"),
                      Index("ix_ts_invoices_client", "company_id", "client_id"))

    id = _id()
    uuid = _uuid()
    company_id = _company()
    client_id = Column(Integer, ForeignKey("ts_clients.id"), nullable=False)
    number = Column(String(40), nullable=True)
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    issue_date = Column(Date, nullable=False)
    due_date = Column(Date, nullable=True)
    status = Column(String(12), nullable=False, server_default=text("'draft'"))  # draft, sent, paid, void
    supplier = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))  # name, gstin, state, address
    place_of_supply = Column(String(4), nullable=True)  # state code
    tax_mode = Column(String(12), nullable=False, server_default=text("'intra'"))  # intra (CGST+SGST) | inter (IGST)
    gst_rate = Column(Numeric(5, 2), nullable=False, server_default=text("18"))
    sac_code = Column(String(10), nullable=True)
    lines = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    hours = Column(Numeric(10, 2), nullable=False, server_default=text("0"))
    subtotal = Column(Numeric(14, 2), nullable=False, server_default=text("0"))
    cgst = Column(Numeric(14, 2), nullable=False, server_default=text("0"))
    sgst = Column(Numeric(14, 2), nullable=False, server_default=text("0"))
    igst = Column(Numeric(14, 2), nullable=False, server_default=text("0"))
    total = Column(Numeric(14, 2), nullable=False, server_default=text("0"))
    notes = Column(Text, nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    paid_at = Column(DateTime(timezone=True), nullable=True)
    paid_reference = Column(String(120), nullable=True)
    created_by_name = Column(String(200), nullable=True)
    created_at, updated_at = _stamps()

    client = relationship("TsClient")
