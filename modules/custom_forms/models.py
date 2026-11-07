"""Custom services (no-code forms): the service, its form versions, the records people
submit, their comments and activity, and saved list views.

A service's form is stored as versioned JSON (`custom_form_versions.definition`). Saving the
form designer creates a new version; every record keeps the version it was filled in, so
old records always read with the fields they were made with.
"""
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from core.db import Base


def _uuid():
    return Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))


def _company():
    return Column(Integer, ForeignKey("companies.id"), nullable=False)


def _stamps():
    return (Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
            Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False))


class CustomService(Base):
    """A service people use without code (Training request, ID card request …): who may
    submit, who handles the records, whether it needs approval, its working statuses and
    where it shows in the menu. Its approval flow is the request type `form.<company>.<code>`."""
    __tablename__ = "custom_services"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_custom_services_company_code"),
        Index("ix_custom_services_company_active", "company_id", "active", "sort_order"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    code = Column(String(30), nullable=False)  # never changes: used in addresses, flows and numbers
    name = Column(String(50), nullable=False)
    description = Column(String(300), nullable=True)
    icon = Column(String(40), nullable=False, server_default=text("'clipboard-list'"))
    nav_section = Column(String(20), nullable=False, server_default=text("'home'"))
    template_key = Column(String(40), nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    needs_approval = Column(Boolean, nullable=False, server_default=text("true"))
    allow_drafts = Column(Boolean, nullable=False, server_default=text("true"))
    allow_cancel = Column(Boolean, nullable=False, server_default=text("true"))
    # {"everyone": true} or {"everyone": false, "roles": [...], "team_ids": [...], "department_ids": [...], "location_ids": [...]}
    submit_access = Column(JSONB, nullable=False, server_default=text("'{\"everyone\": true}'::jsonb"))
    manager_roles = Column(JSONB, nullable=False, server_default=text("'[\"hr\"]'::jsonb"))
    manager_employee_ids = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    # Working statuses after submission/approval: [{"key", "label", "tone", "closed"}]
    statuses = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    # {"managers_on_submit": true, "requester_on_status": true}
    notify = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    title_field = Column(String(40), nullable=True)  # field shown as the record's title
    current_version = Column(Integer, nullable=False, server_default=text("1"))
    sort_order = Column(Integer, nullable=False, server_default=text("0"))
    archived_at = Column(DateTime(timezone=True), nullable=True)
    archived_by_name = Column(String(200), nullable=True)
    created_by_name = Column(String(200), nullable=True)
    created_at, updated_at = _stamps()


class CustomFormVersion(Base):
    """One version of a service's form: {"fields": [...]} (see fields.py for the shape)."""
    __tablename__ = "custom_form_versions"
    __table_args__ = (
        UniqueConstraint("service_id", "version", name="uq_custom_form_versions_service_version"),
        Index("ix_custom_form_versions_company_service", "company_id", "service_id"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    service_id = Column(Integer, ForeignKey("custom_services.id"), nullable=False)
    version = Column(Integer, nullable=False)
    definition = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    change_note = Column(String(300), nullable=True)
    created_by_name = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CustomFormRecord(Base):
    """One filled-in form. status: draft → pending (in approval) → the service's working
    statuses; or rejected / cancelled."""
    __tablename__ = "custom_form_records"
    __table_args__ = (
        Index("ix_custom_form_records_company_service", "company_id", "service_id", "status", text("created_at DESC")),
        Index("ix_custom_form_records_company_requester", "company_id", "requester_employee_id"),
        Index("ix_custom_form_records_company_reference", "company_id", "reference_no"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    service_id = Column(Integer, ForeignKey("custom_services.id"), nullable=False)
    version_id = Column(Integer, ForeignKey("custom_form_versions.id"), nullable=False)
    reference_no = Column(String(60), nullable=True)
    title = Column(String(250), nullable=True)
    requester_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)  # NULL = company owner
    requester_name = Column(String(200), nullable=True)
    values = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status = Column(String(30), nullable=False, server_default=text("'draft'"))
    workflow_instance_id = Column(Integer, ForeignKey("workflow_instances.id"), nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    status_changed_at = Column(DateTime(timezone=True), nullable=True)
    created_by_name = Column(String(200), nullable=True)
    created_at, updated_at = _stamps()

    service = relationship("CustomService")
    version = relationship("CustomFormVersion")


class CustomFormComment(Base):
    """The record's activity: comments with files, status changes and edits (kind)."""
    __tablename__ = "custom_form_comments"
    __table_args__ = (Index("ix_custom_form_comments_company_record", "company_id", "record_id", "created_at"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    record_id = Column(Integer, ForeignKey("custom_form_records.id"), nullable=False)
    kind = Column(String(20), nullable=False, server_default=text("'comment'"))  # comment | status | edit | system
    author_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    author_name = Column(String(200), nullable=True)
    body = Column(Text, nullable=True)
    attachments = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    data = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CustomFormView(Base):
    """A saved set of filters and columns on a service's list, private or shared."""
    __tablename__ = "custom_form_views"
    __table_args__ = (Index("ix_custom_form_views_company_service", "company_id", "service_id", "owner_key"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    service_id = Column(Integer, ForeignKey("custom_services.id"), nullable=False)
    owner_key = Column(String(40), nullable=False)  # "employee:<id>" or "owner"
    name = Column(String(100), nullable=False)
    filters = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    columns = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    shared = Column(Boolean, nullable=False, server_default=text("false"))
    created_at, updated_at = _stamps()
