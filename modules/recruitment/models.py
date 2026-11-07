"""Hiring: requisitions, jobs, candidates and their applications, interviews and
scorecards, reference checks, offers and referrals."""
from sqlalchemy import (Boolean, Column, Date, DateTime, Float, ForeignKey, Index, Integer, Numeric, String, Text,
                        UniqueConstraint, func, text)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from core.db import Base


def _uuid():
    return Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))


def _company():
    return Column(Integer, ForeignKey("companies.id"), nullable=False)


def _json(default="'{}'::jsonb"):
    return Column(JSONB, nullable=False, server_default=text(default))


def _money():
    return Column(Numeric(14, 2), nullable=True)


def _stamps():
    return (Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
            Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False))


class RecruitmentSettings(Base):
    """One row per company: careers page, privacy, retention, job boards, screening."""
    __tablename__ = "recruitment_settings"
    __table_args__ = (UniqueConstraint("company_id", name="uq_recruitment_settings_company"),)

    id = Column(Integer, primary_key=True)
    company_id = _company()
    config = _json()
    created_at, updated_at = _stamps()


class Requisition(Base):
    """A manager's request for new or replacement headcount, approved through the workflow engine."""
    __tablename__ = "requisitions"
    __table_args__ = (Index("ix_requisitions_company_status", "company_id", "status"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    number = Column(String(40), nullable=True)
    title = Column(String(200), nullable=False)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    designation_id = Column(Integer, ForeignKey("designations.id"), nullable=True)
    location_id = Column(Integer, nullable=True)
    employment_type_id = Column(Integer, ForeignKey("master_values.id"), nullable=True)
    openings = Column(Integer, nullable=False, server_default=text("1"))
    kind = Column(String(20), nullable=False, server_default=text("'new'"))  # new | replacement
    replacement_for_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    budget_min = _money()
    budget_max = _money()
    min_experience = Column(Float, nullable=True)
    max_experience = Column(Float, nullable=True)
    needed_by = Column(Date, nullable=True)
    justification = Column(Text, nullable=True)
    hiring_manager_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    requested_by_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    requested_by_name = Column(String(200), nullable=True)
    # draft | pending | approved | rejected | cancelled | filled
    status = Column(String(20), nullable=False, server_default=text("'draft'"))
    workflow_instance_id = Column(Integer, ForeignKey("workflow_instances.id"), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decision_note = Column(String(500), nullable=True)
    created_at, updated_at = _stamps()


class Job(Base):
    """A job opening, shown on the careers page when published."""
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_company_status", "company_id", "status"),
        Index("ix_jobs_company_requisition", "company_id", "requisition_id"),
        UniqueConstraint("company_id", "slug", name="uq_jobs_company_slug"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    requisition_id = Column(Integer, ForeignKey("requisitions.id"), nullable=True)
    code = Column(String(40), nullable=True)
    title = Column(String(200), nullable=False)
    slug = Column(String(120), nullable=False)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    designation_id = Column(Integer, ForeignKey("designations.id"), nullable=True)
    location_id = Column(Integer, nullable=True)
    location_text = Column(String(150), nullable=True)
    work_mode = Column(String(20), nullable=False, server_default=text("'office'"))  # office | remote | hybrid
    employment_type_id = Column(Integer, ForeignKey("master_values.id"), nullable=True)
    summary = Column(String(300), nullable=True)
    description = Column(Text, nullable=True)
    skills = _json("'[]'::jsonb")           # ["Python", "SQL"]
    nice_skills = _json("'[]'::jsonb")
    min_experience = Column(Float, nullable=True)
    max_experience = Column(Float, nullable=True)
    salary_min = _money()
    salary_max = _money()
    show_salary = Column(Boolean, nullable=False, server_default=text("false"))
    openings = Column(Integer, nullable=False, server_default=text("1"))
    hiring_manager_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    team = _json("'[]'::jsonb")             # [{"employee_id": 3, "role": "recruiter" | "interviewer" | "coordinator"}]
    stages = _json("'[]'::jsonb")           # [{"key": "screening", "label": "Screening", "kind": "screening"}]
    questions = _json("'[]'::jsonb")        # [{"key": "q1", "label": "...", "type": "text|yesno|number|choice", "required": true, "options": []}]
    screening = _json()                     # {"auto_reject_below": 30, "auto_advance_above": 80}
    postings = _json()                      # {"naukri": {"status": "posted", "at": "...", "ref": "..."}}
    referral_bonus = _money()
    # draft | open | on_hold | closed | filled
    status = Column(String(20), nullable=False, server_default=text("'draft'"))
    published = Column(Boolean, nullable=False, server_default=text("false"))
    published_at = Column(DateTime(timezone=True), nullable=True)
    closes_on = Column(Date, nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    created_by_name = Column(String(200), nullable=True)
    created_at, updated_at = _stamps()


class Candidate(Base):
    __tablename__ = "candidates"
    __table_args__ = (
        Index("ix_candidates_company_email", "company_id", "email"),
        Index("ix_candidates_company_phone", "company_id", "phone"),
        Index("ix_candidates_company_status", "company_id", "status"),
        Index("ix_candidates_company_purge", "company_id", "purge_after"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    first_name = Column(String(100), nullable=False)
    last_name = Column(String(100), nullable=False, server_default=text("''"))
    email = Column(String(255), nullable=True)
    phone = Column(String(20), nullable=True)
    location = Column(String(150), nullable=True)
    current_company = Column(String(200), nullable=True)
    current_title = Column(String(150), nullable=True)
    total_experience = Column(Float, nullable=True)  # years
    current_ctc = _money()
    expected_ctc = _money()
    notice_days = Column(Integer, nullable=True)
    skills = _json("'[]'::jsonb")
    education = Column(String(300), nullable=True)
    linkedin_url = Column(String(300), nullable=True)
    # careers | referral | agency | job_board | walk_in | direct | campus | other
    source = Column(String(20), nullable=False, server_default=text("'direct'"))
    source_detail = Column(String(150), nullable=True)  # e.g. Naukri, agency name
    referred_by_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    tags = _json("'[]'::jsonb")
    resume_file_id = Column(Integer, nullable=True)
    resume_text = Column(Text, nullable=True)
    parsed = _json()                        # fields read from the CV
    notes = Column(Text, nullable=True)
    owner_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    follow_up_on = Column(Date, nullable=True)
    follow_up_note = Column(String(300), nullable=True)
    follow_up_sent_on = Column(Date, nullable=True)
    talent_pool = Column(Boolean, nullable=False, server_default=text("false"))
    # active | hired | archived | merged | purged
    status = Column(String(20), nullable=False, server_default=text("'active'"))
    merged_into_id = Column(Integer, ForeignKey("candidates.id"), nullable=True)
    consent_at = Column(DateTime(timezone=True), nullable=True)
    consent_text = Column(Text, nullable=True)
    consent_ip = Column(String(64), nullable=True)
    purge_after = Column(Date, nullable=True)
    purged_at = Column(DateTime(timezone=True), nullable=True)
    custom = _json()  # custom field values
    created_at, updated_at = _stamps()

    applications = relationship("modules.recruitment.models.Application", back_populates="candidate", order_by="modules.recruitment.models.Application.id")


class Application(Base):
    """A candidate in one job's pipeline."""
    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("job_id", "candidate_id", name="uq_applications_job_candidate"),
        Index("ix_applications_company_job_stage", "company_id", "job_id", "stage"),
        Index("ix_applications_company_candidate", "company_id", "candidate_id"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False)
    candidate_id = Column(Integer, ForeignKey("candidates.id"), nullable=False)
    stage = Column(String(40), nullable=False, server_default=text("'applied'"))
    stage_entered_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    position = Column(Float, nullable=False, server_default=text("0"))
    # active | rejected | withdrawn | hired
    status = Column(String(20), nullable=False, server_default=text("'active'"))
    rejection_reason = Column(String(300), nullable=True)
    source = Column(String(20), nullable=True)
    answers = _json()
    score = Column(Integer, nullable=True)            # 0-100 screening score
    score_detail = _json()                            # {"scorer": "rules", "parts": [...], "explanation": "..."}
    evaluation = _json()                              # summary computed from scorecards
    evaluation_override = _json()                     # {"recommendation": "hire", "note": "...", "by": "...", "at": "..."}
    bgv_status = Column(String(20), nullable=False, server_default=text("'not_started'"))
    bgv_vendor = Column(String(120), nullable=True)
    bgv_notes = Column(Text, nullable=True)
    bgv_consent_at = Column(DateTime(timezone=True), nullable=True)
    history = _json("'[]'::jsonb")                    # [{at, by, kind, text, from, to}]
    applied_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    hired_at = Column(DateTime(timezone=True), nullable=True)
    created_at, updated_at = _stamps()

    candidate = relationship("Candidate", back_populates="applications")
    job = relationship("Job")


class InterviewKit(Base):
    """Competencies and questions an interview panel uses, with the scorecard."""
    __tablename__ = "interview_kits"
    __table_args__ = (Index("ix_interview_kits_company", "company_id"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(150), nullable=False)
    description = Column(String(300), nullable=True)
    competencies = _json("'[]'::jsonb")     # [{"key": "problem_solving", "label": "Problem solving", "weight": 1}]
    questions = _json("'[]'::jsonb")        # ["Tell us about ..."]
    duration_minutes = Column(Integer, nullable=False, server_default=text("45"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class Interview(Base):
    __tablename__ = "interviews"
    __table_args__ = (
        Index("ix_interviews_company_starts", "company_id", "starts_at"),
        Index("ix_interviews_company_application", "company_id", "application_id"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=False)
    kit_id = Column(Integer, ForeignKey("interview_kits.id"), nullable=True)
    round_name = Column(String(120), nullable=False)
    mode = Column(String(20), nullable=False, server_default=text("'video'"))  # video | phone | in_person
    starts_at = Column(DateTime(timezone=True), nullable=False)
    ends_at = Column(DateTime(timezone=True), nullable=False)
    location = Column(String(300), nullable=True)
    meeting_link = Column(String(500), nullable=True)
    panel = _json("'[]'::jsonb")            # [employee ids]
    notes = Column(Text, nullable=True)
    # scheduled | completed | cancelled | no_show
    status = Column(String(20), nullable=False, server_default=text("'scheduled'"))
    sequence = Column(Integer, nullable=False, server_default=text("0"))  # .ics revision
    reminded_at = Column(DateTime(timezone=True), nullable=True)
    created_by_name = Column(String(200), nullable=True)
    created_at, updated_at = _stamps()

    application = relationship("modules.recruitment.models.Application")
    kit = relationship("InterviewKit")
    scorecards = relationship("Scorecard", back_populates="interview", order_by="Scorecard.id")


class Scorecard(Base):
    """One interviewer's ratings for one interview."""
    __tablename__ = "interview_scorecards"
    __table_args__ = (
        UniqueConstraint("interview_id", "interviewer_employee_id", name="uq_scorecards_interview_interviewer"),
        Index("ix_scorecards_company_application", "company_id", "application_id"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    interview_id = Column(Integer, ForeignKey("interviews.id"), nullable=False)
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=False)
    interviewer_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    interviewer_name = Column(String(200), nullable=True)
    ratings = _json()                       # {"problem_solving": 4}
    overall = Column(Integer, nullable=True)  # 1-5
    recommendation = Column(String(20), nullable=True)  # strong_yes | yes | no | strong_no
    strengths = Column(Text, nullable=True)
    concerns = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    created_at, updated_at = _stamps()

    interview = relationship("Interview", back_populates="scorecards")


class ReferenceCheck(Base):
    __tablename__ = "reference_checks"
    __table_args__ = (Index("ix_reference_checks_company_application", "company_id", "application_id"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=False)
    referee_name = Column(String(200), nullable=False)
    referee_email = Column(String(255), nullable=True)
    referee_phone = Column(String(20), nullable=True)
    relationship_ = Column("relationship", String(120), nullable=True)
    referee_company = Column(String(200), nullable=True)
    access_token = Column(String(64), nullable=True, unique=True)
    # draft | sent | received | declined | expired
    status = Column(String(20), nullable=False, server_default=text("'draft'"))
    sent_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    responded_at = Column(DateTime(timezone=True), nullable=True)
    responses = _json()
    created_at, updated_at = _stamps()


class Offer(Base):
    __tablename__ = "offers"
    __table_args__ = (
        Index("ix_offers_company_status", "company_id", "status"),
        Index("ix_offers_company_application", "company_id", "application_id"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    number = Column(String(40), nullable=True)
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=False)
    candidate_id = Column(Integer, ForeignKey("candidates.id"), nullable=False)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False)
    designation_id = Column(Integer, ForeignKey("designations.id"), nullable=True)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    location_id = Column(Integer, nullable=True)
    employment_type_id = Column(Integer, ForeignKey("master_values.id"), nullable=True)
    reporting_manager_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    joining_date = Column(Date, nullable=False)
    annual_ctc = Column(Numeric(14, 2), nullable=False)
    structure_id = Column(Integer, nullable=True)       # payroll salary structure (when payroll is set up)
    breakup = _json("'[]'::jsonb")                      # [{code, label, monthly, annual}]
    band_min = _money()
    band_max = _money()
    outside_band = Column(Boolean, nullable=False, server_default=text("false"))
    joining_bonus = _money()
    probation_months = Column(Integer, nullable=True)
    notice_days = Column(Integer, nullable=True)
    notes = Column(Text, nullable=True)
    # draft | pending_approval | approved | rejected | sent | accepted | declined | expired | withdrawn
    status = Column(String(20), nullable=False, server_default=text("'draft'"))
    workflow_instance_id = Column(Integer, ForeignKey("workflow_instances.id"), nullable=True)
    letter_id = Column(Integer, nullable=True)
    esign_request_id = Column(Integer, nullable=True)
    access_token = Column(String(64), nullable=True, unique=True)
    expires_on = Column(Date, nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    viewed_at = Column(DateTime(timezone=True), nullable=True)
    responded_at = Column(DateTime(timezone=True), nullable=True)
    decline_reason = Column(String(500), nullable=True)
    accepted_name = Column(String(200), nullable=True)
    accepted_ip = Column(String(64), nullable=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)  # pre-boarding record once accepted
    history = _json("'[]'::jsonb")
    created_by_name = Column(String(200), nullable=True)
    created_at, updated_at = _stamps()

    application = relationship("modules.recruitment.models.Application")
    candidate = relationship("Candidate")
    job = relationship("Job")


class Referral(Base):
    __tablename__ = "referrals"
    __table_args__ = (Index("ix_referrals_company_referrer", "company_id", "referrer_employee_id"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    referrer_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    candidate_id = Column(Integer, ForeignKey("candidates.id"), nullable=False)
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True)
    note = Column(Text, nullable=True)
    bonus_amount = _money()
    # in_process | not_eligible | pending | eligible | approved | paid | cancelled
    bonus_status = Column(String(20), nullable=False, server_default=text("'in_process'"))
    eligible_on = Column(Date, nullable=True)  # joining date + qualifying days
    paid_on = Column(Date, nullable=True)
    payment_ref = Column(String(80), nullable=True)
    created_at, updated_at = _stamps()
