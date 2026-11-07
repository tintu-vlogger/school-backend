"""Onboarding: flows chosen by rule, one onboarding per joiner with the pre-joining portal,
field-by-field review, joining documents and 30/60/90-day check-ins."""
from sqlalchemy import (Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
                        func, text)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from core.db import Base


def _uuid():
    return Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))


def _company():
    return Column(Integer, ForeignKey("companies.id"), nullable=False)


def _json(default="'{}'::jsonb"):
    return Column(JSONB, nullable=False, server_default=text(default))


def _stamps():
    return (Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
            Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False))


class OnboardingSettings(Base):
    __tablename__ = "onboarding_settings"
    __table_args__ = (UniqueConstraint("company_id", name="uq_onboarding_settings_company"),)

    id = Column(Integer, primary_key=True)
    company_id = _company()
    config = _json()
    created_at, updated_at = _stamps()


class OnboardingFlow(Base):
    """A joining flow for an audience (department, location, employment type …), with
    pre-joining and day-one stages, checklist tasks, documents, induction and courses."""
    __tablename__ = "onboarding_flows"
    __table_args__ = (Index("ix_onboarding_flows_company", "company_id", "active", "priority"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(150), nullable=False)
    description = Column(String(300), nullable=True)
    audience = _json()
    priority = Column(Integer, nullable=False, server_default=text("100"))
    pre_joining_enabled = Column(Boolean, nullable=False, server_default=text("true"))
    day_one_enabled = Column(Boolean, nullable=False, server_default=text("true"))
    # [{"title", "owner": hr|it|admin|manager|buddy|joiner|employee, "employee_id", "stage": pre_joining|day_one,
    #   "due_days": -3 (relative to joining), "checklist": []}]
    tasks = _json("'[]'::jsonb")
    # portal sections the joiner fills: personal, family, nominees, bank, statutory, previous_employment, tax, documents
    sections = _json("'[]'::jsonb")
    documents = _json("'[]'::jsonb")        # [{"label": "PAN card", "required": true}]
    induction = _json("'[]'::jsonb")        # [{"title", "day": 1, "time": "10:00", "duration_minutes": 60, "host_id"}]
    course_ids = _json("'[]'::jsonb")       # learning courses to enrol into on day one
    statutory_forms = _json("'[\"pf_form2\", \"pf_form11\", \"esi_declaration\", \"gratuity_form_f\"]'::jsonb")
    require_admin_closure = Column(Boolean, nullable=False, server_default=text("true"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class Onboarding(Base):
    __tablename__ = "onboardings"
    __table_args__ = (
        Index("ix_onboardings_company_status", "company_id", "status"),
        Index("ix_onboardings_company_employee", "company_id", "employee_id"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    offer_id = Column(Integer, ForeignKey("offers.id"), nullable=True)
    flow_id = Column(Integer, ForeignKey("onboarding_flows.id"), nullable=True)
    joining_date = Column(Date, nullable=False)
    # pre_joining | day_one | in_progress | completed | cancelled
    status = Column(String(20), nullable=False, server_default=text("'pre_joining'"))
    buddy_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    hr_owner_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    portal_token = Column(String(64), nullable=True, unique=True)
    portal_enabled = Column(Boolean, nullable=False, server_default=text("true"))
    portal_expires_at = Column(DateTime(timezone=True), nullable=True)
    portal_opened_at = Column(DateTime(timezone=True), nullable=True)
    portal_submitted_at = Column(DateTime(timezone=True), nullable=True)
    # what the joiner typed, by section: {"personal": {...}, "bank": {...}, "nominees": [...], ...}
    portal_data = _json()
    # HR review per field "section.field": {"status": "accepted|sent_back", "remark": "...", "at", "by"}
    review = _json()
    # pending | submitted | sent_back | accepted
    review_status = Column(String(20), nullable=False, server_default=text("'pending'"))
    uploads = _json("'[]'::jsonb")          # [{"label", "file_uuid", "name", "status", "remark"}]
    appointment_letter_id = Column(Integer, nullable=True)
    forms = _json("'[]'::jsonb")            # [{"kind", "label", "file_id", "at"}]
    account_requests = _json("'[]'::jsonb")  # [{"kind": "email|laptop|id_card|access", "status", "task_uuid"}]
    welcome_sent_at = Column(DateTime(timezone=True), nullable=True)
    buzz_posted_at = Column(DateTime(timezone=True), nullable=True)
    day_one_started_at = Column(DateTime(timezone=True), nullable=True)
    last_reminded_at = Column(DateTime(timezone=True), nullable=True)
    reminders_sent = Column(Integer, nullable=False, server_default=text("0"))
    completed_at = Column(DateTime(timezone=True), nullable=True)
    completed_by = Column(String(200), nullable=True)
    cancel_reason = Column(String(300), nullable=True)
    history = _json("'[]'::jsonb")
    created_at, updated_at = _stamps()

    flow = relationship("OnboardingFlow")
    checkins = relationship("OnboardingCheckin", back_populates="onboarding", order_by="OnboardingCheckin.day")


class OnboardingCheckin(Base):
    """30/60/90-day check-in between the joiner and their manager."""
    __tablename__ = "onboarding_checkins"
    __table_args__ = (Index("ix_onboarding_checkins_company_due", "company_id", "status", "due_on"),
                      UniqueConstraint("onboarding_id", "day", name="uq_onboarding_checkins_day"))

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    onboarding_id = Column(Integer, ForeignKey("onboardings.id"), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    manager_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    day = Column(Integer, nullable=False)
    due_on = Column(Date, nullable=False)
    status = Column(String(20), nullable=False, server_default=text("'pending'"))  # pending | done | skipped
    rating = Column(Integer, nullable=True)   # 1-5 how settled in
    went_well = Column(Text, nullable=True)
    concerns = Column(Text, nullable=True)
    support_needed = Column(Text, nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    completed_by = Column(String(200), nullable=True)
    reminded_on = Column(Date, nullable=True)
    created_at, updated_at = _stamps()

    onboarding = relationship("Onboarding", back_populates="checkins")
