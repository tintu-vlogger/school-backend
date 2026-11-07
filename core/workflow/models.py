from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from core.db import Base


class WorkflowDefinition(Base):
    """One version of an approval flow. Editing a flow creates a new version with the same
    lineage; requests already running keep the version they started with."""
    __tablename__ = "workflow_definitions"
    __table_args__ = (
        Index("ix_workflow_definitions_company_type", "company_id", "request_type", "is_current"),
        Index("ix_workflow_definitions_company_lineage", "company_id", "lineage_id", "version"),
    )

    id = Column(Integer, primary_key=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    lineage_id = Column(UUID(as_uuid=True), nullable=False)
    version = Column(Integer, nullable=False, server_default=text("1"))
    is_current = Column(Boolean, nullable=False, server_default=text("true"))
    request_type = Column(String(80), nullable=False)
    name = Column(String(150), nullable=False)
    description = Column(Text, nullable=True)
    applies_when = Column(Text, nullable=True)  # condition; empty = always
    priority = Column(Integer, nullable=False, server_default=text("100"))  # lower is checked first
    steps = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    change_note = Column(String(300), nullable=True)
    created_by_name = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class WorkflowInstance(Base):
    """One request going through approval."""
    __tablename__ = "workflow_instances"
    __table_args__ = (
        Index("ix_workflow_instances_company_status", "company_id", "status", text("created_at DESC")),
        Index("ix_workflow_instances_company_requester", "company_id", "requester_employee_id"),
        Index("ix_workflow_instances_company_subject", "company_id", "subject_type", "subject_id"),
        Index("ix_workflow_instances_definition", "definition_id"),
    )

    id = Column(Integer, primary_key=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    definition_id = Column(Integer, ForeignKey("workflow_definitions.id"), nullable=True)
    request_type = Column(String(80), nullable=False)
    reference_no = Column(String(60), nullable=True)
    title = Column(String(250), nullable=False)
    subject_type = Column(String(60), nullable=True)
    subject_id = Column(Integer, nullable=True)
    requester_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)  # NULL = company owner
    requester_name = Column(String(200), nullable=True)
    payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status = Column(String(20), nullable=False, server_default=text("'pending'"))  # pending|approved|rejected|cancelled
    current_step = Column(Integer, nullable=False, server_default=text("0"))
    result_note = Column(Text, nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    # The requester's reminders to the approvers ("nudge"), limited by a cooldown.
    last_nudged_at = Column(DateTime(timezone=True), nullable=True)
    nudge_count = Column(Integer, nullable=False, server_default=text("0"))
    # Cancelled after approval (status "revoked"): when and why.
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoke_reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    definition = relationship("WorkflowDefinition")
    tasks = relationship("WorkflowTask", back_populates="instance", order_by="WorkflowTask.id")


class WorkflowTask(Base):
    """One approver's decision on one step of a request."""
    __tablename__ = "workflow_tasks"
    __table_args__ = (
        Index("ix_workflow_tasks_company_approver_status", "company_id", "approver_employee_id", "status"),
        Index("ix_workflow_tasks_company_owner_status", "company_id", "approver_kind", "status"),
        Index("ix_workflow_tasks_instance", "instance_id"),
        Index("ix_workflow_tasks_due", "company_id", "status", "due_at"),
    )

    id = Column(Integer, primary_key=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    instance_id = Column(Integer, ForeignKey("workflow_instances.id"), nullable=False)
    step_index = Column(Integer, nullable=False)
    step_name = Column(String(150), nullable=False)
    approver_kind = Column(String(20), nullable=False, server_default=text("'employee'"))  # employee | owner
    approver_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    approver_name = Column(String(200), nullable=True)
    delegated_from_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    # pending | approved | rejected | cancelled | skipped | reassigned | escalated
    status = Column(String(20), nullable=False, server_default=text("'pending'"))
    comment = Column(Text, nullable=True)
    decided_by_name = Column(String(200), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    due_at = Column(DateTime(timezone=True), nullable=True)
    escalated = Column(Boolean, nullable=False, server_default=text("false"))
    # How the decision was made: app | email | bulk | auto (time limit) | system
    decided_via = Column(String(10), nullable=True)
    last_reminded_at = Column(DateTime(timezone=True), nullable=True)
    reminder_count = Column(Integer, nullable=False, server_default=text("0"))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    instance = relationship("WorkflowInstance", back_populates="tasks")


class WorkflowDelegation(Base):
    """An approver hands their approvals to someone else for a date range."""
    __tablename__ = "workflow_delegations"
    __table_args__ = (Index("ix_workflow_delegations_company_from", "company_id", "from_employee_id", "active"),)

    id = Column(Integer, primary_key=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    from_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    to_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    request_type = Column(String(80), nullable=True)  # NULL = every kind of request
    note = Column(String(300), nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class WorkflowComment(Base):
    """A comment on a request, with optional attachments (files kept by the documents
    module's storage: [{"uuid", "name", "size", "content_type"}])."""
    __tablename__ = "workflow_comments"
    __table_args__ = (Index("ix_workflow_comments_company_instance", "company_id", "instance_id", "created_at"),)

    id = Column(Integer, primary_key=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    instance_id = Column(Integer, ForeignKey("workflow_instances.id"), nullable=False)
    author_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)  # NULL = company owner or system
    author_name = Column(String(200), nullable=True)
    body = Column(Text, nullable=True)
    attachments = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class WorkflowActionToken(Base):
    """A one-time link in an approval email ("Approve" or "Reject"). Only a hash of the
    secret part is stored; the link also carries a signature over the task and action, and
    stops working when it expires, when it is used, or when the task is decided."""
    __tablename__ = "workflow_action_tokens"
    __audit__ = False  # use is recorded with audit.event
    __table_args__ = (
        Index("ix_workflow_action_tokens_company_task", "company_id", "task_id"),
        Index("ix_workflow_action_tokens_hash", "token_hash", unique=True),
    )

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    task_id = Column(Integer, ForeignKey("workflow_tasks.id"), nullable=False)
    action = Column(String(10), nullable=False)  # approve | reject
    token_hash = Column(String(64), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    used_ip = Column(String(64), nullable=True)
    used_user_agent = Column(String(300), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class WorkflowSettings(Base):
    """Company-wide approval settings: who decides when no approver is found or an
    approver leaves, the nudge cooldown, email approval links and which kinds of request
    may be cancelled after approval."""
    __tablename__ = "workflow_settings"
    __table_args__ = (UniqueConstraint("company_id", name="uq_workflow_settings_company"),)

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    config = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    updated_by_name = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
