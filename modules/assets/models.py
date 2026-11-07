"""Company assets: register, assignments with acknowledgement and return, maintenance,
employee requests and recovery for damage or loss (read by final settlement)."""
from sqlalchemy import (Column, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint,
                        func, text)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from core.db import Base


def _uuid():
    return Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))


def _company():
    return Column(Integer, ForeignKey("companies.id"), nullable=False)


def _created():
    return Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated():
    return Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("company_id", "asset_tag", name="uq_assets_company_tag"),
        Index("ix_assets_company_status", "company_id", "status"),
        Index("ix_assets_company_category", "company_id", "category_id"),
        Index("uq_assets_company_serial", "company_id", "serial_number", unique=True,
              postgresql_where=text("serial_number IS NOT NULL")),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    asset_tag = Column(String(40), nullable=False)
    name = Column(String(200), nullable=False)
    category_id = Column(Integer, ForeignKey("master_values.id"), nullable=True)
    brand = Column(String(100), nullable=True)
    model = Column(String(150), nullable=True)
    serial_number = Column(String(120), nullable=True)
    purchase_date = Column(Date, nullable=True)
    purchase_value = Column(Numeric(12, 2), nullable=True)
    vendor = Column(String(200), nullable=True)
    invoice_no = Column(String(80), nullable=True)
    warranty_until = Column(Date, nullable=True)
    location_id = Column(Integer, nullable=True)  # organisation location (locations.id)
    location_note = Column(String(200), nullable=True)
    condition = Column(String(20), nullable=False, server_default=text("'good'"))  # new | good | fair | poor | damaged
    # available | assigned | in_repair | lost | retired
    status = Column(String(20), nullable=False, server_default=text("'available'"))
    notes = Column(Text, nullable=True)
    retired_on = Column(Date, nullable=True)
    custom = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))  # custom field values
    created_at = _created()
    updated_at = _updated()

    assignments = relationship("AssetAssignment", back_populates="asset", order_by="AssetAssignment.id.desc()")


class AssetRequest(Base):
    __tablename__ = "asset_requests"
    __table_args__ = (Index("ix_asset_requests_company_status", "company_id", "status"),
                      Index("ix_asset_requests_company_employee", "company_id", "employee_id"))

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    category_id = Column(Integer, ForeignKey("master_values.id"), nullable=True)
    details = Column(String(1000), nullable=True)
    needed_by = Column(Date, nullable=True)
    # pending | approved | rejected | fulfilled | cancelled
    status = Column(String(20), nullable=False, server_default=text("'pending'"))
    workflow_instance_id = Column(Integer, ForeignKey("workflow_instances.id"), nullable=True)
    fulfilled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = _created()
    updated_at = _updated()


class AssetAssignment(Base):
    __tablename__ = "asset_assignments"
    __table_args__ = (
        Index("ix_asset_assignments_company_employee", "company_id", "employee_id", "status"),
        Index("ix_asset_assignments_company_asset", "company_id", "asset_id"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    request_id = Column(Integer, ForeignKey("asset_requests.id"), nullable=True)
    # pending_ack (waiting for the employee to confirm) | active | returned
    status = Column(String(20), nullable=False, server_default=text("'pending_ack'"))
    assigned_on = Column(Date, nullable=False)
    expected_return_on = Column(Date, nullable=True)
    assigned_by_name = Column(String(200), nullable=True)
    condition_out = Column(String(20), nullable=True)
    notes = Column(String(500), nullable=True)
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    returned_on = Column(Date, nullable=True)
    condition_in = Column(String(20), nullable=True)
    return_notes = Column(String(500), nullable=True)
    received_by_name = Column(String(200), nullable=True)
    recovery_amount = Column(Numeric(12, 2), nullable=True)
    recovery_reason = Column(String(300), nullable=True)
    recovery_status = Column(String(20), nullable=False, server_default=text("'none'"))  # none | pending | recovered | waived
    created_at = _created()
    updated_at = _updated()

    asset = relationship("Asset", back_populates="assignments")


class AssetMaintenance(Base):
    __tablename__ = "asset_maintenance"
    __table_args__ = (Index("ix_asset_maintenance_company_asset", "company_id", "asset_id"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False)
    kind = Column(String(20), nullable=False, server_default=text("'repair'"))  # repair | service | upgrade | inspection
    performed_on = Column(Date, nullable=False)
    vendor = Column(String(200), nullable=True)
    cost = Column(Numeric(12, 2), nullable=True)
    description = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'done'"))  # scheduled | in_progress | done
    next_due_on = Column(Date, nullable=True)
    created_by_name = Column(String(200), nullable=True)
    created_at = _created()
    updated_at = _updated()
