"""Dashboards: a grid of widgets (KPI tiles and charts), each fed by a saved report or by a
data source with grouping. Every viewer sees each widget with their own access."""
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from core.db import Base


class Dashboard(Base):
    __tablename__ = "dashboards"
    __table_args__ = (
        Index("ix_dashboards_company", "company_id", "is_standard"),
        Index("uq_dashboards_company_key", "company_id", "key", unique=True, postgresql_where=text("key IS NOT NULL")),
    )

    id = Column(Integer, primary_key=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    key = Column(String(60), nullable=True)
    name = Column(String(120), nullable=False)
    description = Column(Text, nullable=True)
    icon = Column(String(40), nullable=True)
    # [{id, type, title, x, y, w, h, report_uuid | source + config, options}]
    widgets = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    is_standard = Column(Boolean, nullable=False, server_default=text("false"))
    owner_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    owner_name = Column(String(200), nullable=True)
    shared_roles = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    shared_employee_ids = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    sort_order = Column(Integer, nullable=False, server_default=text("0"))
    archived_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
