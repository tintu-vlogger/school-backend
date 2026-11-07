"""Recycle bin settings (the bin itself lists soft-deleted rows of other modules; see
core/recycle_bin.py for the registry)."""
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, UniqueConstraint, text
from sqlalchemy.sql import func

from core.db import Base


class RecycleBinSetting(Base):
    """Per-company recycle bin settings (one row): how long deleted records are kept
    before they may be removed for good, and whether that happens on its own."""
    __tablename__ = "recycle_bin_settings"
    __table_args__ = (UniqueConstraint("company_id", name="uq_recycle_bin_settings_company"),)

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    retention_days = Column(Integer, nullable=False, server_default=text("90"))
    auto_purge = Column(Boolean, nullable=False, server_default=text("false"))
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
