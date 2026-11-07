"""Positions (approved seats), headcount plans and position requests; succession plans,
successors, development actions and talent pools; internal job postings and applications.

Every table is company-owned (company_id, an index starting with it, forced RLS).
"""
from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from core.db import Base


def _uuid():
    return Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))


def _company():
    return Column(Integer, ForeignKey("companies.id"), nullable=False)


def _emp(nullable: bool = True):
    return Column(Integer, ForeignKey("employees.id"), nullable=nullable)


def _created():
    return Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated():
    return Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


# ── Positions ───────────────────────────────────────────────────────

class PositionSettings(Base):
    """One row per company: whether positions are on, seat and headcount rules, marketplace rules."""
    __tablename__ = "position_settings"
    __table_args__ = (UniqueConstraint("company_id", name="uq_position_settings_company"),)

    id = Column(Integer, primary_key=True)
    company_id = _company()
    config = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    updated_at = _updated()


class Position(Base):
    """An approved (or budgeted) seat in the organisation. status: budgeted (planned, not yet
    approved for hiring) → approved (can be filled) → closed. headcount is the number of seats."""
    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_positions_company_code"),
        Index("ix_positions_company_department", "company_id", "department_id", "status"),
        Index("ix_positions_company_parent", "company_id", "parent_id"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    code = Column(String(40), nullable=False)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    designation_id = Column(Integer, ForeignKey("designations.id"), nullable=True)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=True)
    grade_id = Column(Integer, ForeignKey("grades.id"), nullable=True)
    cost_centre_id = Column(Integer, ForeignKey("cost_centres.id"), nullable=True)
    parent_id = Column(Integer, ForeignKey("positions.id"), nullable=True)  # reports-to position
    status = Column(String(20), nullable=False, server_default=text("'approved'"))  # budgeted | approved | closed
    headcount = Column(Integer, nullable=False, server_default=text("1"))
    fte = Column(Numeric(5, 2), nullable=False, server_default=text("1"))  # per seat (0.5 = half time)
    salary_min = Column(Numeric(14, 2), nullable=True)  # yearly CTC band; defaults from the grade
    salary_max = Column(Numeric(14, 2), nullable=True)
    is_critical = Column(Boolean, nullable=False, server_default=text("false"))
    skills = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))  # [{"skill_id": 1, "level": 3}]
    requisition_ids = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))  # hiring requests raised for it
    closed_at = Column(DateTime(timezone=True), nullable=True)
    closed_reason = Column(String(300), nullable=True)
    created_by_name = Column(String(200), nullable=True)
    created_at = _created()
    updated_at = _updated()

    parent = relationship("Position", remote_side=[id])


class PositionSeat(Base):
    """An employee in a position. status: reserved (starts later) → active → ended."""
    __tablename__ = "position_seats"
    __table_args__ = (
        Index("ix_position_seats_company_position", "company_id", "position_id", "status"),
        Index("ix_position_seats_company_employee", "company_id", "employee_id", "status"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    position_id = Column(Integer, ForeignKey("positions.id"), nullable=False)
    employee_id = _emp(False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'active'"))
    source = Column(String(20), nullable=False, server_default=text("'manual'"))  # hire | job_change | marketplace | manual
    job_change_id = Column(Integer, ForeignKey("employee_job_history.id"), nullable=True)
    ended_reason = Column(String(300), nullable=True)
    created_by_name = Column(String(200), nullable=True)
    created_at = _created()
    updated_at = _updated()

    position = relationship("Position")


class HeadcountPlan(Base):
    """Planned headcount for one department in one financial year (e.g. 2026-27), approved
    through the workflow engine. approved_headcount is the limit in force; planned_headcount
    is what was last proposed (a revision keeps the old limit until it is approved)."""
    __tablename__ = "headcount_plans"
    __table_args__ = (
        UniqueConstraint("company_id", "department_id", "financial_year", name="uq_headcount_plans_dept_year"),
        Index("ix_headcount_plans_company_year", "company_id", "financial_year"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=False)
    financial_year = Column(String(9), nullable=False)
    planned_headcount = Column(Integer, nullable=False)
    approved_headcount = Column(Integer, nullable=True)
    budget = Column(Numeric(16, 2), nullable=True)  # yearly CTC budget
    notes = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'draft'"))  # draft | pending | approved | rejected
    workflow_instance_id = Column(Integer, ForeignKey("workflow_instances.id"), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decision_note = Column(String(500), nullable=True)
    created_by_name = Column(String(200), nullable=True)
    created_at = _created()
    updated_at = _updated()


class PositionRequest(Base):
    """A request to add a new position, close one or change one, approved through the
    workflow engine. data holds the proposed fields."""
    __tablename__ = "position_requests"
    __table_args__ = (Index("ix_position_requests_company_status", "company_id", "status"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    number = Column(String(40), nullable=True)
    kind = Column(String(10), nullable=False)  # new | close | change
    position_id = Column(Integer, ForeignKey("positions.id"), nullable=True)
    data = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    reason = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'pending'"))  # pending | approved | rejected | cancelled
    workflow_instance_id = Column(Integer, ForeignKey("workflow_instances.id"), nullable=True)
    requested_by_employee_id = _emp()
    requested_by_name = Column(String(200), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decision_note = Column(String(500), nullable=True)
    result_position_id = Column(Integer, ForeignKey("positions.id"), nullable=True)
    created_at = _created()
    updated_at = _updated()


# ── Succession ──────────────────────────────────────────────────────

class SuccessionPlan(Base):
    """Succession for one key position: who could take over, how likely the incumbent is to
    leave (risk) and how much it would hurt (impact)."""
    __tablename__ = "succession_plans"
    __table_args__ = (Index("ix_succession_plans_company_position", "company_id", "position_id", "status"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    position_id = Column(Integer, ForeignKey("positions.id"), nullable=False)
    plan_manager_id = _emp()
    risk_of_loss = Column(String(10), nullable=False, server_default=text("'medium'"))  # low | medium | high
    impact_of_loss = Column(String(10), nullable=False, server_default=text("'medium'"))
    notes = Column(Text, nullable=True)
    review_on = Column(Date, nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'active'"))  # active | archived
    created_by_name = Column(String(200), nullable=True)
    created_at = _created()
    updated_at = _updated()

    position = relationship("Position")
    candidates = relationship("SuccessionCandidate", back_populates="plan", order_by="SuccessionCandidate.rank")


class SuccessionCandidate(Base):
    """A possible successor. readiness: ready_now | one_two_years | three_plus_years."""
    __tablename__ = "succession_candidates"
    __table_args__ = (
        Index("ix_succession_candidates_company_plan", "company_id", "plan_id"),
        Index("ix_succession_candidates_company_employee", "company_id", "employee_id"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    plan_id = Column(Integer, ForeignKey("succession_plans.id"), nullable=False)
    employee_id = _emp(False)
    readiness = Column(String(20), nullable=False, server_default=text("'one_two_years'"))
    rank = Column(Integer, nullable=False, server_default=text("1"))
    strengths = Column(Text, nullable=True)
    gaps = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'active'"))  # active | removed
    added_by_name = Column(String(200), nullable=True)
    created_at = _created()
    updated_at = _updated()

    plan = relationship("SuccessionPlan", back_populates="candidates")


class DevelopmentAction(Base):
    """A step to get a successor (or pool member) ready: a course from learning, a goal from
    performance, mentoring, a project or a stretch assignment."""
    __tablename__ = "development_actions"
    __table_args__ = (Index("ix_development_actions_company_employee", "company_id", "employee_id"),
                      Index("ix_development_actions_company_candidate", "company_id", "candidate_id"))

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = _emp(False)
    candidate_id = Column(Integer, ForeignKey("succession_candidates.id"), nullable=True)
    kind = Column(String(20), nullable=False)  # course | goal | mentoring | project | stretch | other
    title = Column(String(250), nullable=False)
    course_id = Column(Integer, ForeignKey("learning_courses.id"), nullable=True)
    objective_id = Column(Integer, ForeignKey("perf_objectives.id"), nullable=True)
    mentor_id = _emp()
    due_date = Column(Date, nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'planned'"))  # planned | in_progress | done | cancelled
    notes = Column(Text, nullable=True)
    created_by_name = Column(String(200), nullable=True)
    created_at = _created()
    updated_at = _updated()


class TalentPool(Base):
    """A named group of people, e.g. high potentials or future leaders."""
    __tablename__ = "talent_pools"
    __table_args__ = (Index("ix_talent_pools_company", "company_id", "active"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(150), nullable=False)
    description = Column(Text, nullable=True)
    manager_employee_id = _emp()
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = _created()
    updated_at = _updated()

    members = relationship("TalentPoolMember", back_populates="pool", order_by="TalentPoolMember.id")


class TalentPoolMember(Base):
    __tablename__ = "talent_pool_members"
    __table_args__ = (
        UniqueConstraint("company_id", "pool_id", "employee_id", name="uq_talent_pool_members"),
        Index("ix_talent_pool_members_company_employee", "company_id", "employee_id"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    pool_id = Column(Integer, ForeignKey("talent_pools.id"), nullable=False)
    employee_id = _emp(False)
    note = Column(String(500), nullable=True)
    added_by_name = Column(String(200), nullable=True)
    created_at = _created()
    updated_at = _updated()

    pool = relationship("TalentPool", back_populates="members")


# ── Internal job marketplace ────────────────────────────────────────

class InternalPosting(Base):
    """An opening for current employees: a job (a move into a role, often from a hiring
    request or an open position) or a gig (a project for some weeks and hours a week)."""
    __tablename__ = "internal_postings"
    __table_args__ = (Index("ix_internal_postings_company_status", "company_id", "status", "kind"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    number = Column(String(40), nullable=True)
    kind = Column(String(10), nullable=False, server_default=text("'job'"))  # job | gig
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    requisition_id = Column(Integer, ForeignKey("requisitions.id"), nullable=True)
    position_id = Column(Integer, ForeignKey("positions.id"), nullable=True)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    designation_id = Column(Integer, ForeignKey("designations.id"), nullable=True)
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=True)
    skills = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))  # [{"skill_id": 1, "level": 3}]
    openings = Column(Integer, nullable=False, server_default=text("1"))
    duration_weeks = Column(Integer, nullable=True)  # gigs
    hours_per_week = Column(Integer, nullable=True)  # gigs
    work_mode = Column(String(20), nullable=True)  # office | hybrid | remote
    starts_on = Column(Date, nullable=True)
    closes_on = Column(Date, nullable=True)
    hiring_manager_id = _emp()
    needs_manager_consent = Column(Boolean, nullable=False, server_default=text("true"))
    status = Column(String(20), nullable=False, server_default=text("'draft'"))  # draft | open | closed | filled | cancelled
    published_at = Column(DateTime(timezone=True), nullable=True)
    created_by_name = Column(String(200), nullable=True)
    created_at = _created()
    updated_at = _updated()


class InternalApplication(Base):
    """An employee's application to an internal posting.
    status: manager_review (waiting for their manager's consent) → submitted → shortlisted →
    selected → moved (job) or assigned (gig); or manager_declined, rejected, withdrawn."""
    __tablename__ = "internal_applications"
    __table_args__ = (
        Index("ix_internal_applications_company_posting", "company_id", "posting_id", "status"),
        Index("ix_internal_applications_company_employee", "company_id", "employee_id"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    posting_id = Column(Integer, ForeignKey("internal_postings.id"), nullable=False)
    employee_id = _emp(False)
    manager_employee_id = _emp()
    note = Column(Text, nullable=True)
    match_score = Column(Numeric(5, 1), nullable=True)
    match_detail = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    status = Column(String(20), nullable=False, server_default=text("'submitted'"))
    workflow_instance_id = Column(Integer, ForeignKey("workflow_instances.id"), nullable=True)
    manager_note = Column(String(500), nullable=True)
    decision_note = Column(String(500), nullable=True)
    effective_date = Column(Date, nullable=True)
    job_change_id = Column(Integer, ForeignKey("employee_job_history.id"), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    created_at = _created()
    updated_at = _updated()

    posting = relationship("InternalPosting")
