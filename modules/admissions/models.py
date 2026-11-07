"""Admissions and certificates.

- AdmissionSettings     one row per institution: required documents, acknowledgement email,
                        online form switch and wording, offer letter, merit weights
- AdmissionOffering     a program open for admission in a session, with seats and fee
- Enquiry               a lead: who asked, about which program, where they came from, stage
- Application           an admission application (online or at the office), with test and
                        interview scores, merit rank, offer and the student it became
- AdmissionActivity     append-only log of notes, calls, stage and status changes for
                        enquiries and applications
- CertificateTemplate   transfer / leaving / bonafide / character … certificate designs
- Certificate           the issue register (never deleted; cancelled with a reason)
"""
from sqlalchemy import (Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text,
                        UniqueConstraint, text)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from core.db import Base


def _uuid():
    return Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))


def _company():
    return Column(Integer, ForeignKey("companies.id"), nullable=False)


def _stamps():
    return (Column(DateTime(timezone=True), server_default=func.now()),
            Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now()))


class AdmissionSettings(Base):
    __tablename__ = "admission_settings"
    __table_args__ = (UniqueConstraint("company_id", name="uq_admission_settings_company"),)
    id = Column(Integer, primary_key=True)
    company_id = _company()
    config = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at, updated_at = _stamps()


class AdmissionOffering(Base):
    """A program open for admission in one session: seats, fee and the dates the form is open."""
    __tablename__ = "admission_offerings"
    __table_args__ = (UniqueConstraint("company_id", "session_id", "program_id", name="uq_admission_offerings"),
                      Index("ix_admission_offerings_company", "company_id", "session_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    session_id = Column(Integer, ForeignKey("academic_sessions.id"), nullable=False)
    program_id = Column(Integer, ForeignKey("academic_programs.id"), nullable=False)
    seats = Column(Integer, nullable=False, server_default=text("0"))
    application_fee = Column(Numeric(12, 2), nullable=False, server_default=text("0"))
    fee_required = Column(Boolean, nullable=False, server_default=text("false"))
    # Accepting online applications on the institution's website
    open_online = Column(Boolean, nullable=False, server_default=text("true"))
    opens_on = Column(Date, nullable=True)
    closes_on = Column(Date, nullable=True)
    test_required = Column(Boolean, nullable=False, server_default=text("false"))
    interview_required = Column(Boolean, nullable=False, server_default=text("false"))
    # Age eligibility, checked on `age_as_on` (e.g. 1 June of the session)
    min_age_years = Column(Numeric(4, 1), nullable=True)
    max_age_years = Column(Numeric(4, 1), nullable=True)
    age_as_on = Column(Date, nullable=True)
    notes = Column(Text, nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()

    session = relationship("AcademicSession")
    program = relationship("Program")


class Enquiry(Base):
    __tablename__ = "admission_enquiries"
    __table_args__ = (Index("ix_admission_enquiries_company_stage", "company_id", "stage"),
                      Index("ix_admission_enquiries_company_follow", "company_id", "next_follow_up"),
                      Index("ix_admission_enquiries_company_counsellor", "company_id", "counsellor_id"),
                      Index("ix_admission_enquiries_company_phone", "company_id", "phone"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    number = Column(String(40), nullable=True)
    # Master value id of the "enquiry_source" list (walk-in, website, phone, campaign, referral …)
    source_id = Column(Integer, ForeignKey("master_values.id"), nullable=True)
    source_detail = Column(String(200), nullable=True)  # campaign name, who referred …
    session_id = Column(Integer, ForeignKey("academic_sessions.id"), nullable=True)
    program_id = Column(Integer, ForeignKey("academic_programs.id"), nullable=True)
    child_first_name = Column(String(100), nullable=False)
    child_last_name = Column(String(100), nullable=True)
    child_dob = Column(Date, nullable=True)
    child_gender = Column(String(20), nullable=True)
    current_institution = Column(String(200), nullable=True)
    parent_name = Column(String(200), nullable=True)
    parent_relation = Column(String(30), nullable=True)
    phone = Column(String(20), nullable=True)
    email = Column(String(255), nullable=True)
    city = Column(String(100), nullable=True)
    address = Column(Text, nullable=True)
    # new | contacted | visit_scheduled | applied | converted | lost
    stage = Column(String(20), nullable=False, server_default=text("'new'"))
    next_follow_up = Column(Date, nullable=True)
    visit_at = Column(DateTime(timezone=True), nullable=True)
    counsellor_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    lost_reason = Column(String(300), nullable=True)
    notes = Column(Text, nullable=True)
    application_id = Column(Integer, ForeignKey("admission_applications.id", use_alter=True,
                                                name="fk_admission_enquiries_application"), nullable=True)
    stage_changed_at = Column(DateTime(timezone=True), server_default=func.now())
    archived_at = Column(DateTime(timezone=True), nullable=True)
    created_at, updated_at = _stamps()

    program = relationship("Program")
    session = relationship("AcademicSession")
    counsellor = relationship("Employee", foreign_keys=[counsellor_id])
    source = relationship("MasterValue", foreign_keys=[source_id])

    @property
    def child_name(self) -> str:
        return " ".join(p for p in (self.child_first_name, self.child_last_name) if p)


class Application(Base):
    __tablename__ = "admission_applications"
    __table_args__ = (UniqueConstraint("company_id", "application_no", name="uq_admission_applications_no"),
                      Index("ix_admission_applications_company_status", "company_id", "status"),
                      Index("ix_admission_applications_company_offering", "company_id", "session_id", "program_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    application_no = Column(String(40), nullable=False)
    enquiry_id = Column(Integer, ForeignKey("admission_enquiries.id"), nullable=True)
    session_id = Column(Integer, ForeignKey("academic_sessions.id"), nullable=False)
    program_id = Column(Integer, ForeignKey("academic_programs.id"), nullable=False)
    # draft | submitted | under_review | test_scheduled | interview | shortlisted | offered | accepted |
    # enrolled | rejected | waitlisted | withdrawn
    status = Column(String(20), nullable=False, server_default=text("'submitted'"))
    channel = Column(String(20), nullable=False, server_default=text("'office'"))  # office | online
    # The learner
    first_name = Column(String(100), nullable=False)
    middle_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)
    gender = Column(String(20), nullable=True)
    date_of_birth = Column(Date, nullable=True)
    blood_group = Column(String(10), nullable=True)
    nationality = Column(String(60), nullable=True)
    religion = Column(String(60), nullable=True)
    category = Column(String(40), nullable=True)
    mother_tongue = Column(String(60), nullable=True)
    national_id = Column(String(40), nullable=True)
    previous_institution = Column(String(200), nullable=True)
    previous_program = Column(String(120), nullable=True)
    previous_marks = Column(String(60), nullable=True)
    address = Column(Text, nullable=True)
    city = Column(String(100), nullable=True)
    state = Column(String(100), nullable=True)
    pincode = Column(String(12), nullable=True)
    # Contact used for the status check and messages
    contact_phone = Column(String(20), nullable=True)
    contact_email = Column(String(255), nullable=True)
    # [{relation, first_name, last_name, phone, email, occupation, is_primary}]
    guardians = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    # [{key, label, uuid, name, size, content_type, verified}]
    documents = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    custom = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    # Fee
    fee_amount = Column(Numeric(12, 2), nullable=False, server_default=text("0"))
    fee_paid = Column(Boolean, nullable=False, server_default=text("false"))
    fee_paid_on = Column(Date, nullable=True)
    fee_reference = Column(String(80), nullable=True)
    # Entrance test and interview
    test_at = Column(DateTime(timezone=True), nullable=True)
    test_venue = Column(String(200), nullable=True)
    test_score = Column(Numeric(7, 2), nullable=True)
    test_max = Column(Numeric(7, 2), nullable=True)
    interview_at = Column(DateTime(timezone=True), nullable=True)
    interviewer_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    interview_score = Column(Numeric(7, 2), nullable=True)
    interview_max = Column(Numeric(7, 2), nullable=True)
    interview_notes = Column(Text, nullable=True)
    merit_score = Column(Numeric(7, 2), nullable=True)
    merit_rank = Column(Integer, nullable=True)
    # Offer
    offer_state = Column(String(20), nullable=False, server_default=text("'none'"))  # none | pending | approved | rejected
    offer_workflow_id = Column(Integer, ForeignKey("workflow_instances.id"), nullable=True)
    offered_at = Column(DateTime(timezone=True), nullable=True)
    offer_expires_on = Column(Date, nullable=True)
    offer_note = Column(Text, nullable=True)
    accepted_at = Column(DateTime(timezone=True), nullable=True)
    # Result
    decision_reason = Column(String(300), nullable=True)  # rejection / withdrawal reason
    student_id = Column(Integer, ForeignKey("students.id"), nullable=True)
    section_id = Column(Integer, ForeignKey("academic_sections.id"), nullable=True)
    enrolled_at = Column(DateTime(timezone=True), nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    remarks = Column(Text, nullable=True)
    archived_at = Column(DateTime(timezone=True), nullable=True)
    created_at, updated_at = _stamps()

    program = relationship("Program")
    session = relationship("AcademicSession")
    enquiry = relationship("Enquiry", foreign_keys=[enquiry_id])
    student = relationship("Student")
    section = relationship("Section")
    interviewer = relationship("Employee", foreign_keys=[interviewer_id])

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.middle_name, self.last_name) if p)


class AdmissionActivity(Base):
    """Notes, calls, visits and every stage or status change of an enquiry or application."""
    __tablename__ = "admission_activities"
    __table_args__ = (Index("ix_admission_activities_enquiry", "company_id", "enquiry_id"),
                      Index("ix_admission_activities_application", "company_id", "application_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    enquiry_id = Column(Integer, ForeignKey("admission_enquiries.id"), nullable=True)
    application_id = Column(Integer, ForeignKey("admission_applications.id"), nullable=True)
    # note | call | email | visit | stage | status | assign | follow_up | system
    kind = Column(String(20), nullable=False, server_default=text("'note'"))
    body = Column(Text, nullable=True)
    from_value = Column(String(30), nullable=True)
    to_value = Column(String(30), nullable=True)
    actor_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    actor_name = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class CertificateTemplate(Base):
    __tablename__ = "certificate_templates"
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_certificate_templates_name"),
                      Index("ix_certificate_templates_company", "company_id", "kind"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(120), nullable=False)
    # transfer | leaving | bonafide | character | conduct | study | migration | custom
    kind = Column(String(20), nullable=False, server_default=text("'custom'"))
    title = Column(String(200), nullable=True)  # heading printed on the certificate
    body_html = Column(Text, nullable=False, server_default=text("''"))
    # Extra values asked for when issuing: [{key, label, required, default}]
    fields = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    page_size = Column(String(10), nullable=False, server_default=text("'A4'"))
    orientation = Column(String(10), nullable=False, server_default=text("'portrait'"))
    font_family = Column(String(40), nullable=False, server_default=text("'Georgia'"))
    show_letterhead = Column(Boolean, nullable=False, server_default=text("true"))
    # Issuing this certificate offers to mark the student transferred / left
    marks_student = Column(String(20), nullable=True)  # transferred | left | None
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class Certificate(Base):
    """One issued certificate. Never deleted: cancelled with a reason."""
    __tablename__ = "certificates"
    __table_args__ = (Index("ix_certificates_company_student", "company_id", "student_id"),
                      Index("ix_certificates_company_issued", "company_id", "issued_on"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    number = Column(String(60), nullable=True)
    template_id = Column(Integer, ForeignKey("certificate_templates.id"), nullable=False)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    kind = Column(String(20), nullable=False)
    title = Column(String(200), nullable=False)
    issued_on = Column(Date, nullable=False)
    values = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    body_html = Column(Text, nullable=False)  # the merged certificate as issued
    status = Column(String(20), nullable=False, server_default=text("'issued'"))  # issued | cancelled
    marked_student = Column(String(20), nullable=True)  # status the student was moved to, if any
    issued_by_name = Column(String(200), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    cancel_reason = Column(String(300), nullable=True)
    cancelled_by_name = Column(String(200), nullable=True)
    created_at, updated_at = _stamps()

    template = relationship("CertificateTemplate")
    student = relationship("Student")


TABLES = ["admission_settings", "admission_offerings", "admission_enquiries", "admission_applications",
          "admission_activities", "certificate_templates", "certificates"]
