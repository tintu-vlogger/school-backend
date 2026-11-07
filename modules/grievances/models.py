"""Grievances (named or anonymous) and POSH complaints.

POSH complaints are visible only to members of the company's Internal Committee (and,
when the company turns it on, to the company owner). Anonymous grievances keep no link
to the person who raised it; they follow up with a tracking code whose hash is stored.
"""
from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
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


class GrievanceSettings(Base):
    __tablename__ = "grievance_settings"
    __table_args__ = (UniqueConstraint("company_id", name="uq_grievance_settings_company"),)

    id = Column(Integer, primary_key=True)
    company_id = _company()
    config = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    updated_at = _updated()


class GrievanceCommittee(Base):
    """kind: grievance (Grievance Redressal Committee) or posh_ic (Internal Committee)."""
    __tablename__ = "grievance_committees"
    __table_args__ = (Index("ix_grievance_committees_company_kind", "company_id", "kind", "active"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    kind = Column(String(20), nullable=False)
    name = Column(String(150), nullable=False)
    description = Column(String(500), nullable=True)
    constituted_on = Column(Date, nullable=True)
    valid_until = Column(Date, nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = _created()
    updated_at = _updated()

    members = relationship("GrievanceCommitteeMember", back_populates="committee", order_by="GrievanceCommitteeMember.id")


class GrievanceCommitteeMember(Base):
    __tablename__ = "grievance_committee_members"
    __table_args__ = (Index("ix_grievance_members_company_employee", "company_id", "employee_id", "active"),
                      Index("ix_grievance_members_committee", "committee_id"))

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    committee_id = Column(Integer, ForeignKey("grievance_committees.id"), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    external_name = Column(String(200), nullable=True)  # e.g. the NGO member of an Internal Committee
    external_email = Column(String(255), nullable=True)
    external_org = Column(String(200), nullable=True)
    role = Column(String(30), nullable=False, server_default=text("'member'"))  # presiding_officer | member | external_member | secretary
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = _created()
    updated_at = _updated()

    committee = relationship("GrievanceCommittee", back_populates="members")


class Grievance(Base):
    __tablename__ = "grievances"
    __table_args__ = (
        Index("ix_grievances_company_kind_status", "company_id", "kind", "status"),
        Index("ix_grievances_company_complainant", "company_id", "complainant_employee_id"),
        Index("ix_grievances_tracking", "tracking_hash"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    kind = Column(String(10), nullable=False)  # grievance | posh
    reference_no = Column(String(40), nullable=True)
    tracking_hash = Column(String(64), nullable=False)
    category_id = Column(Integer, ForeignKey("master_values.id"), nullable=True)
    subject = Column(String(250), nullable=False)
    description = Column(Text, nullable=False)
    anonymous = Column(Boolean, nullable=False, server_default=text("false"))
    complainant_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    respondent_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    respondent_name = Column(String(200), nullable=True)
    incident_date = Column(Date, nullable=True)
    incident_location = Column(String(250), nullable=True)
    witnesses = Column(String(500), nullable=True)
    # submitted | under_review | inquiry | report_submitted | resolved | closed | rejected | withdrawn
    status = Column(String(20), nullable=False, server_default=text("'submitted'"))
    committee_id = Column(Integer, ForeignKey("grievance_committees.id"), nullable=True)
    assigned_to_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    attachments = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    received_on = Column(Date, nullable=False)
    # POSH deadlines (dates in the company's timezone)
    filing_deadline = Column(Date, nullable=True)
    late_filing = Column(Boolean, nullable=False, server_default=text("false"))
    delay_condoned = Column(Boolean, nullable=False, server_default=text("false"))
    delay_reason = Column(String(500), nullable=True)
    acknowledge_due_on = Column(Date, nullable=True)
    acknowledged_on = Column(Date, nullable=True)
    inquiry_started_on = Column(Date, nullable=True)
    inquiry_due_on = Column(Date, nullable=True)
    inquiry_completed_on = Column(Date, nullable=True)
    report_due_on = Column(Date, nullable=True)
    report_submitted_on = Column(Date, nullable=True)
    action_due_on = Column(Date, nullable=True)
    action_completed_on = Column(Date, nullable=True)
    conciliation = Column(Boolean, nullable=False, server_default=text("false"))
    interim_relief = Column(String(500), nullable=True)
    # upheld | partly_upheld | not_upheld | settled | withdrawn | false_complaint
    outcome = Column(String(20), nullable=True)
    outcome_summary = Column(Text, nullable=True)
    action_taken = Column(Text, nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    deadline_alerts = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at = _created()
    updated_at = _updated()

    messages = relationship("GrievanceMessage", back_populates="grievance", order_by="GrievanceMessage.id")


class GrievanceMessage(Base):
    """reply: seen by the complainant | note: confidential, committee only | event: history line."""
    __tablename__ = "grievance_messages"
    __table_args__ = (Index("ix_grievance_messages_company_grievance", "company_id", "grievance_id"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    grievance_id = Column(Integer, ForeignKey("grievances.id"), nullable=False)
    kind = Column(String(10), nullable=False)
    body = Column(Text, nullable=False)
    from_complainant = Column(Boolean, nullable=False, server_default=text("false"))
    author_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    author_label = Column(String(200), nullable=True)
    attachments = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    created_at = _created()

    grievance = relationship("Grievance", back_populates="messages")


class PoshProgramme(Base):
    """Awareness programmes and workshops, counted in the annual POSH report."""
    __tablename__ = "posh_programmes"
    __table_args__ = (Index("ix_posh_programmes_company_date", "company_id", "held_on"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    held_on = Column(Date, nullable=False)
    title = Column(String(200), nullable=False)
    kind = Column(String(20), nullable=False, server_default=text("'workshop'"))  # workshop | awareness | training
    participants = Column(Integer, nullable=False, server_default=text("0"))
    notes = Column(String(500), nullable=True)
    created_at = _created()
    updated_at = _updated()
