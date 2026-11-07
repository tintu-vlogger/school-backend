"""HR processes: company-defined change processes (transfer, promotion, confirmation …)
and the change requests raised under them."""
from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from core.db import Base


class HrProcessType(Base):
    """A kind of change HR runs: which employee fields it changes, the key field, the stage it
    moves people to (confirmation), the letters it issues and whether it can be revoked.
    Its approval flow is the `hr.process` request type, targeted with `request.process_code`."""
    __tablename__ = "hr_process_types"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_hr_process_types_company_code"),
        Index("ix_hr_process_types_company_active", "company_id", "active", "sort_order"),
    )

    id = Column(Integer, primary_key=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    code = Column(String(10), nullable=False)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    change_type = Column(String(30), nullable=False, server_default=text("'other'"))  # kind in the job history
    fields = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))  # employee fields it changes
    key_field = Column(String(80), nullable=True)  # must be given on every request
    target_status = Column(String(20), nullable=True)  # e.g. confirmed
    letter_template_uuids = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    revocable = Column(Boolean, nullable=False, server_default=text("true"))
    needs_approval = Column(Boolean, nullable=False, server_default=text("true"))
    is_system = Column(Boolean, nullable=False, server_default=text("false"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    sort_order = Column(Integer, nullable=False, server_default=text("0"))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class HrProcessRequest(Base):
    """One employee's change under a process. status: pending (in approval) → scheduled
    (approved, future date) → applied; or rejected, cancelled, revoked."""
    __tablename__ = "hr_process_requests"
    __table_args__ = (
        Index("ix_hr_process_requests_company_status", "company_id", "status", "effective_date"),
        Index("ix_hr_process_requests_company_employee", "company_id", "employee_id", "process_type_id"),
        Index("ix_hr_process_requests_company_batch", "company_id", "batch_id"),
    )

    id = Column(Integer, primary_key=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    process_type_id = Column(Integer, ForeignKey("hr_process_types.id"), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    batch_id = Column(UUID(as_uuid=True), nullable=True)
    effective_date = Column(Date, nullable=False)
    new_values = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    old_values = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))  # captured when applied
    reason = Column(String(300), nullable=True)
    remarks = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'pending'"))
    workflow_instance_id = Column(Integer, ForeignKey("workflow_instances.id"), nullable=True)
    job_change_id = Column(Integer, ForeignKey("employee_job_history.id"), nullable=True)
    revoke_job_change_id = Column(Integer, ForeignKey("employee_job_history.id"), nullable=True)
    letter_uuids = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    note = Column(Text, nullable=True)  # what happened (letters skipped …)
    applied_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoked_by_name = Column(String(200), nullable=True)
    revoke_reason = Column(String(300), nullable=True)
    created_by_name = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    process_type = relationship("HrProcessType")
