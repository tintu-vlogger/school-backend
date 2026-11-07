"""HR help desk: categories with agents and SLAs, cases with a conversation, canned
responses and knowledge base articles."""
from sqlalchemy import (Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func,
                        text)
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


class HelpdeskSettings(Base):
    __tablename__ = "helpdesk_settings"
    __table_args__ = (UniqueConstraint("company_id", name="uq_helpdesk_settings_company"),)

    id = Column(Integer, primary_key=True)
    company_id = _company()
    config = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    updated_at = _updated()


class HelpdeskCategory(Base):
    """A category (parent_id NULL) or a subcategory."""
    __tablename__ = "helpdesk_categories"
    __table_args__ = (Index("ix_helpdesk_categories_company_parent", "company_id", "parent_id", "sort_order"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    parent_id = Column(Integer, ForeignKey("helpdesk_categories.id"), nullable=True)
    name = Column(String(120), nullable=False)
    description = Column(String(500), nullable=True)
    icon = Column(String(40), nullable=True)
    # pickup: agents pick from the queue | managed: the owner assigns | auto: least busy agent
    assignment_mode = Column(String(20), nullable=False, server_default=text("'pickup'"))
    owner_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    escalate_to_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    require_subcategory = Column(Boolean, nullable=False, server_default=text("false"))
    feedback_mandatory = Column(Boolean, nullable=False, server_default=text("false"))
    default_priority = Column(String(10), nullable=False, server_default=text("'medium'"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    sort_order = Column(Integer, nullable=False, server_default=text("0"))
    created_at = _created()
    updated_at = _updated()

    parent = relationship("HelpdeskCategory", remote_side=[id])
    agents = relationship("HelpdeskAgent", back_populates="category", cascade="all, delete-orphan")


class HelpdeskAgent(Base):
    """An employee who handles cases of a category, optionally only for one work location."""
    __tablename__ = "helpdesk_agents"
    __table_args__ = (
        UniqueConstraint("category_id", "employee_id", "location_id", name="uq_helpdesk_agents_cat_emp_loc"),
        Index("ix_helpdesk_agents_company_employee", "company_id", "employee_id"),
    )

    id = Column(Integer, primary_key=True)
    company_id = _company()
    category_id = Column(Integer, ForeignKey("helpdesk_categories.id"), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    location_id = Column(Integer, nullable=True)  # organisation location (locations.id)
    created_at = _created()

    category = relationship("HelpdeskCategory", back_populates="agents")


class HelpdeskSla(Base):
    """Target times for a priority, for one category or (category NULL) for all."""
    __tablename__ = "helpdesk_slas"
    __table_args__ = (Index("ix_helpdesk_slas_company_category", "company_id", "category_id", "priority"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    category_id = Column(Integer, ForeignKey("helpdesk_categories.id"), nullable=True)
    priority = Column(String(10), nullable=False)
    response_minutes = Column(Integer, nullable=False)
    resolution_minutes = Column(Integer, nullable=False)
    escalate_to_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    raise_priority = Column(Boolean, nullable=False, server_default=text("true"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = _created()
    updated_at = _updated()


class HelpdeskCase(Base):
    __tablename__ = "helpdesk_cases"
    __table_args__ = (
        Index("ix_helpdesk_cases_company_status", "company_id", "status", text("created_at DESC")),
        Index("ix_helpdesk_cases_company_requester", "company_id", "requester_employee_id"),
        Index("ix_helpdesk_cases_company_assignee", "company_id", "assignee_employee_id", "status"),
        Index("ix_helpdesk_cases_company_category", "company_id", "category_id"),
        Index("ix_helpdesk_cases_company_due", "company_id", "resolution_due_at"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    reference_no = Column(String(40), nullable=True)
    category_id = Column(Integer, ForeignKey("helpdesk_categories.id"), nullable=False)
    subcategory_id = Column(Integer, ForeignKey("helpdesk_categories.id"), nullable=True)
    case_type_id = Column(Integer, ForeignKey("master_values.id"), nullable=True)
    location_id = Column(Integer, nullable=True)  # organisation location (locations.id)
    subject = Column(String(250), nullable=False)
    description = Column(Text, nullable=False)
    priority = Column(String(10), nullable=False, server_default=text("'medium'"))
    # new | open | in_progress | waiting | resolved | closed | cancelled
    status = Column(String(20), nullable=False, server_default=text("'new'"))
    source = Column(String(20), nullable=False, server_default=text("'web'"))
    requester_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    raised_by_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    raised_by_name = Column(String(200), nullable=True)
    assignee_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    attachments = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    response_due_at = Column(DateTime(timezone=True), nullable=True)
    resolution_due_at = Column(DateTime(timezone=True), nullable=True)
    first_response_at = Column(DateTime(timezone=True), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    paused_at = Column(DateTime(timezone=True), nullable=True)  # SLA clock stops while waiting on the employee
    response_breached = Column(Boolean, nullable=False, server_default=text("false"))
    resolution_breached = Column(Boolean, nullable=False, server_default=text("false"))
    escalated_at = Column(DateTime(timezone=True), nullable=True)
    escalation_level = Column(Integer, nullable=False, server_default=text("0"))
    reopened_count = Column(Integer, nullable=False, server_default=text("0"))
    rating = Column(Integer, nullable=True)
    rating_comment = Column(String(1000), nullable=True)
    rated_at = Column(DateTime(timezone=True), nullable=True)
    last_activity_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    custom = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))  # custom field values
    created_at = _created()
    updated_at = _updated()

    category = relationship("HelpdeskCategory", foreign_keys=[category_id])
    subcategory = relationship("HelpdeskCategory", foreign_keys=[subcategory_id])
    messages = relationship("HelpdeskMessage", back_populates="case", order_by="HelpdeskMessage.id")


class HelpdeskMessage(Base):
    """A reply (seen by the requester), an internal note (agents only) or an event line."""
    __tablename__ = "helpdesk_messages"
    __table_args__ = (Index("ix_helpdesk_messages_company_case", "company_id", "case_id"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    case_id = Column(Integer, ForeignKey("helpdesk_cases.id"), nullable=False)
    kind = Column(String(10), nullable=False)  # reply | note | event
    body = Column(Text, nullable=False)
    author_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    author_name = Column(String(200), nullable=True)
    attachments = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    created_at = _created()

    case = relationship("HelpdeskCase", back_populates="messages")


class HelpdeskCannedResponse(Base):
    __tablename__ = "helpdesk_canned_responses"
    __table_args__ = (Index("ix_helpdesk_canned_company", "company_id", "category_id"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    title = Column(String(150), nullable=False)
    body = Column(Text, nullable=False)
    category_id = Column(Integer, ForeignKey("helpdesk_categories.id"), nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_by_name = Column(String(200), nullable=True)
    created_at = _created()
    updated_at = _updated()


class HelpdeskArticle(Base):
    """Knowledge base article (policy FAQs) employees can search before raising a case."""
    __tablename__ = "helpdesk_articles"
    __table_args__ = (Index("ix_helpdesk_articles_company_category", "company_id", "category_id", "published"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    category_id = Column(Integer, ForeignKey("helpdesk_categories.id"), nullable=True)
    title = Column(String(250), nullable=False)
    body = Column(Text, nullable=False)
    tags = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    published = Column(Boolean, nullable=False, server_default=text("true"))
    views = Column(Integer, nullable=False, server_default=text("0"))
    helpful_yes = Column(Integer, nullable=False, server_default=text("0"))
    helpful_no = Column(Integer, nullable=False, server_default=text("0"))
    author_name = Column(String(200), nullable=True)
    created_at = _created()
    updated_at = _updated()
