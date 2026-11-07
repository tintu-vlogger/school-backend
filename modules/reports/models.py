"""Saved reports, their email schedules, and the export log (every download, scheduled
email, assistant or MCP export is recorded here and in the audit log)."""
from sqlalchemy import (Boolean, Column, DateTime, ForeignKey, Index, Integer, LargeBinary, String, Text, func,
                        text)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from core.db import Base


def _uuid():
    return Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))


def _company():
    return Column(Integer, ForeignKey("companies.id"), nullable=False)


def _stamps():
    return (Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
            Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False))


class Report(Base):
    """A saved report: a source plus columns, filters, grouping, totals and sort. Standard
    reports (key set, is_standard) are seeded for every company and can be copied."""
    __tablename__ = "reports"
    __table_args__ = (
        Index("ix_reports_company_category", "company_id", "category"),
        Index("uq_reports_company_key", "company_id", "key", unique=True, postgresql_where=text("key IS NOT NULL")),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    key = Column(String(60), nullable=True)
    name = Column(String(120), nullable=False)
    description = Column(Text, nullable=True)
    category = Column(String(20), nullable=False, server_default=text("'people'"))
    source = Column(String(60), nullable=False)
    config = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    chart = Column(JSONB, nullable=True)            # default picture: {type, x, y}
    scope = Column(String(10), nullable=False, server_default=text("'auto'"))   # auto | self | direct | team | all
    is_standard = Column(Boolean, nullable=False, server_default=text("false"))
    owner_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)   # NULL: company owner
    owner_name = Column(String(200), nullable=True)
    shared_roles = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    shared_employee_ids = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    archived_at = Column(DateTime(timezone=True), nullable=True)
    created_at, updated_at = _stamps()


class ReportSchedule(Base):
    """Email a report on a schedule as Excel, PDF or CSV. It runs with the access of the
    person who set it up (run_as), so recipients never get more than that person sees."""
    __tablename__ = "report_schedules"
    __table_args__ = (Index("ix_report_schedules_company_next", "company_id", "active", "next_run_at"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    report_id = Column(Integer, ForeignKey("reports.id"), nullable=False)
    name = Column(String(120), nullable=False)
    frequency = Column(String(10), nullable=False, server_default=text("'weekly'"))  # daily | weekly | monthly
    weekday = Column(Integer, nullable=True)          # 0 Monday … 6 Sunday
    day_of_month = Column(Integer, nullable=True)     # 1–28
    hour = Column(Integer, nullable=False, server_default=text("9"))   # company local time
    format = Column(String(5), nullable=False, server_default=text("'xlsx'"))
    recipient_employee_ids = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    recipient_emails = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    run_as_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)   # NULL: company owner
    active = Column(Boolean, nullable=False, server_default=text("true"))
    next_run_at = Column(DateTime(timezone=True), nullable=True)
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    last_status = Column(String(20), nullable=True)   # sent | failed | not_sent
    last_message = Column(String(300), nullable=True)
    created_by_name = Column(String(200), nullable=True)
    created_at, updated_at = _stamps()


class ExportLog(Base):
    """Who exported what, when. The file itself is kept for a short while so the person can
    download it once; then only the record stays."""
    __tablename__ = "export_logs"
    __audit__ = False    # the record itself is the audit trail (plus an audit event per export)
    __table_args__ = (Index("ix_export_logs_company_created", "company_id", text("created_at DESC")),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    actor_name = Column(String(200), nullable=True)
    channel = Column(String(20), nullable=False, server_default=text("'download'"))  # download | schedule | data | assistant | mcp
    report_id = Column(Integer, ForeignKey("reports.id"), nullable=True)
    what = Column(String(200), nullable=False)        # report or data set name
    source = Column(String(60), nullable=True)
    format = Column(String(5), nullable=False)
    row_count = Column(Integer, nullable=False, server_default=text("0"))
    scope = Column(String(10), nullable=True)
    details = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))   # filters, params, recipients
    sensitive = Column(Boolean, nullable=False, server_default=text("false"))
    protected = Column(Boolean, nullable=False, server_default=text("false"))
    file_name = Column(String(200), nullable=True)
    content_type = Column(String(100), nullable=True)
    content = Column(LargeBinary, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    downloaded_at = Column(DateTime(timezone=True), nullable=True)
    ip_address = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
