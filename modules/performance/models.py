"""Performance tables: goals and OKRs, KRAs and competencies, review cycles and
calibration, one-to-ones, feedback, improvement plans, and pay reviews (increments and
promotions).

Every table is company-owned (company_id + RLS). Configuration that varies a lot between
companies (template sections, cycle stages, guideline matrices) is kept in JSONB columns
and checked in service.py.
"""
from sqlalchemy import (
    Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import declared_attr, relationship
from sqlalchemy.sql import func

from core.db import Base


class _Row:
    """id, uuid, company_id and time stamps shared by every table here."""
    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))

    @declared_attr
    def company_id(cls):
        return Column(Integer, ForeignKey("companies.id"), nullable=False)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


def _emp_fk(name: str | None = None, nullable: bool = True):
    return Column(Integer, ForeignKey("employees.id", ondelete="SET NULL" if nullable else None), nullable=nullable)


# ── Settings and scales ─────────────────────────────────────────────

class PerfSettings(_Row, Base):
    """One row per company: OKR statuses and cycle start, nine-box labels, reminder limits."""
    __tablename__ = "perf_settings"
    __table_args__ = (UniqueConstraint("company_id", name="uq_perf_settings_company"),)
    config = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))


class RatingScale(_Row, Base):
    __tablename__ = "perf_rating_scales"
    __table_args__ = (Index("ix_perf_rating_scales_company", "company_id"),)
    name = Column(String(120), nullable=False)
    description = Column(Text, nullable=True)
    # [{"value": 1, "label": "Needs improvement", "description": "...", "color": "#..."}] ascending
    points = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    is_default = Column(Boolean, nullable=False, server_default=text("false"))
    active = Column(Boolean, nullable=False, server_default=text("true"))


# ── Goals and OKRs ──────────────────────────────────────────────────

class GoalPeriod(_Row, Base):
    __tablename__ = "perf_periods"
    __table_args__ = (Index("ix_perf_periods_company_dates", "company_id", "start_date"),)
    name = Column(String(120), nullable=False)
    kind = Column(String(20), nullable=False, server_default=text("'quarter'"))  # quarter | half | year | custom
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    status = Column(String(20), nullable=False, server_default=text("'open'"))  # open | closed


class Objective(_Row, Base):
    __tablename__ = "perf_objectives"
    __table_args__ = (
        Index("ix_perf_objectives_company_period", "company_id", "period_id"),
        Index("ix_perf_objectives_company_owner", "company_id", "owner_employee_id"),
        Index("ix_perf_objectives_company_parent", "company_id", "parent_id"),
    )
    period_id = Column(Integer, ForeignKey("perf_periods.id"), nullable=False)
    level = Column(String(20), nullable=False)  # company | department | team | individual
    title = Column(String(300), nullable=False)
    description = Column(Text, nullable=True)
    owner_employee_id = _emp_fk()
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    parent_id = Column(Integer, ForeignKey("perf_objectives.id", ondelete="SET NULL"), nullable=True)
    # weight of this objective when it rolls up into its parent, and in a person's goal sheet
    weight = Column(Numeric(6, 2), nullable=False, server_default=text("1"))
    progress = Column(Numeric(6, 2), nullable=False, server_default=text("0"))
    status_key = Column(String(40), nullable=True)
    status_manual = Column(Boolean, nullable=False, server_default=text("false"))
    confidence = Column(Integer, nullable=True)  # 1..10, from the latest check-in
    due_date = Column(Date, nullable=True)
    visibility = Column(String(20), nullable=False, server_default=text("'everyone'"))  # everyone | team | private
    archived_at = Column(DateTime(timezone=True), nullable=True)
    created_by_name = Column(String(200), nullable=True)

    period = relationship("GoalPeriod")
    owner = relationship("Employee", foreign_keys=[owner_employee_id])
    department = relationship("Department")
    key_results = relationship("KeyResult", back_populates="objective", order_by="KeyResult.sort_order",
                               cascade="all, delete-orphan")


class KeyResult(_Row, Base):
    __tablename__ = "perf_key_results"
    __table_args__ = (Index("ix_perf_key_results_company_objective", "company_id", "objective_id"),)
    objective_id = Column(Integer, ForeignKey("perf_objectives.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(300), nullable=False)
    kind = Column(String(20), nullable=False, server_default=text("'number'"))  # number | percent | milestone | boolean
    unit = Column(String(30), nullable=True)
    start_value = Column(Numeric(16, 2), nullable=False, server_default=text("0"))
    target_value = Column(Numeric(16, 2), nullable=False, server_default=text("100"))
    current_value = Column(Numeric(16, 2), nullable=False, server_default=text("0"))
    # [{"id": "m1", "title": "...", "due_date": "YYYY-MM-DD", "weight": 1, "done": false, "done_on": null}]
    milestones = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    weight = Column(Numeric(6, 2), nullable=False, server_default=text("1"))
    owner_employee_id = _emp_fk()
    start_date = Column(Date, nullable=True)
    due_date = Column(Date, nullable=True)
    checkin_frequency = Column(String(20), nullable=False, server_default=text("'weekly'"))  # weekly|fortnightly|monthly|none
    progress = Column(Numeric(6, 2), nullable=False, server_default=text("0"))
    confidence = Column(Integer, nullable=True)
    status_key = Column(String(40), nullable=True)
    sort_order = Column(Integer, nullable=False, server_default=text("0"))

    objective = relationship("Objective", back_populates="key_results")
    owner = relationship("Employee", foreign_keys=[owner_employee_id])


class GoalCheckin(_Row, Base):
    __tablename__ = "perf_checkins"
    __table_args__ = (
        Index("ix_perf_checkins_company_kr", "company_id", "key_result_id"),
        Index("ix_perf_checkins_company_objective", "company_id", "objective_id"),
    )
    objective_id = Column(Integer, ForeignKey("perf_objectives.id", ondelete="CASCADE"), nullable=False)
    key_result_id = Column(Integer, ForeignKey("perf_key_results.id", ondelete="CASCADE"), nullable=True)
    employee_id = _emp_fk()
    author_name = Column(String(200), nullable=True)
    checkin_date = Column(Date, nullable=False)
    value = Column(Numeric(16, 2), nullable=True)
    progress = Column(Numeric(6, 2), nullable=True)
    confidence = Column(Integer, nullable=True)
    status_key = Column(String(40), nullable=True)
    note = Column(Text, nullable=True)
    milestone_id = Column(String(40), nullable=True)


# ── KRAs and competencies ───────────────────────────────────────────

class Kra(_Row, Base):
    __tablename__ = "perf_kras"
    __table_args__ = (Index("ix_perf_kras_company", "company_id"),)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    measure = Column(Text, nullable=True)  # how it is measured
    weight = Column(Numeric(6, 2), nullable=False, server_default=text("20"))
    # {"department_ids": [], "designation_ids": [], "location_ids": []}; empty lists = everyone
    tags = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    active = Column(Boolean, nullable=False, server_default=text("true"))


class EmployeeKra(_Row, Base):
    __tablename__ = "perf_employee_kras"
    __table_args__ = (
        UniqueConstraint("company_id", "employee_id", "kra_id", name="uq_perf_employee_kras"),
        Index("ix_perf_employee_kras_company_employee", "company_id", "employee_id"),
    )
    employee_id = Column(Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    kra_id = Column(Integer, ForeignKey("perf_kras.id", ondelete="CASCADE"), nullable=False)
    weight = Column(Numeric(6, 2), nullable=False, server_default=text("20"))
    source = Column(String(20), nullable=False, server_default=text("'manual'"))  # manual | auto

    kra = relationship("Kra")


class Competency(_Row, Base):
    __tablename__ = "perf_competencies"
    __table_args__ = (Index("ix_perf_competencies_company", "company_id"),)
    name = Column(String(200), nullable=False)
    category = Column(String(80), nullable=True)  # core | leadership | functional
    description = Column(Text, nullable=True)
    rating_scale_id = Column(Integer, ForeignKey("perf_rating_scales.id"), nullable=True)
    weight = Column(Numeric(6, 2), nullable=False, server_default=text("1"))
    # what each level looks like: [{"value": 1, "text": "..."}]
    behaviours = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    skill_ids = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))  # skill master value ids
    tags = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    active = Column(Boolean, nullable=False, server_default=text("true"))


# ── Review cycles ───────────────────────────────────────────────────

class ReviewTemplate(_Row, Base):
    __tablename__ = "perf_templates"
    __table_args__ = (Index("ix_perf_templates_company", "company_id"),)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    rating_scale_id = Column(Integer, ForeignKey("perf_rating_scales.id"), nullable=True)
    # [{"key": "goals", "kind": "goals|kras|competencies|questions", "title": "...", "weight": 40,
    #   "questions": [{"id": "q1", "text": "...", "type": "rating|text", "required": true}]}]
    sections = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    active = Column(Boolean, nullable=False, server_default=text("true"))

    rating_scale = relationship("RatingScale")


class ReviewCycle(_Row, Base):
    __tablename__ = "perf_cycles"
    __table_args__ = (Index("ix_perf_cycles_company_status", "company_id", "status"),)
    name = Column(String(200), nullable=False)
    kind = Column(String(20), nullable=False, server_default=text("'annual'"))  # annual | half_yearly | quarterly | probation
    template_id = Column(Integer, ForeignKey("perf_templates.id"), nullable=False)
    rating_scale_id = Column(Integer, ForeignKey("perf_rating_scales.id"), nullable=True)
    goal_period_id = Column(Integer, ForeignKey("perf_periods.id"), nullable=True)
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    status = Column(String(20), nullable=False, server_default=text("'draft'"))  # draft|active|calibration|published|closed
    # {"department_ids": [], "designation_ids": [], "condition": "", "include_ids": [], "exclude_ids": [],
    #  "joined_before": "YYYY-MM-DD"}
    participant_rule = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    # [{"key": "self|peer|manager|skip|calibration", "enabled": true, "deadline": "YYYY-MM-DD"}]
    stages = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    # {"self": 10, "manager": 70, "peer": 10, "skip": 10}
    weightage = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    # peer rules, reminders, calibration distribution, letters
    settings = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    launched_at = Column(DateTime(timezone=True), nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)

    template = relationship("ReviewTemplate")
    rating_scale = relationship("RatingScale")
    goal_period = relationship("GoalPeriod")


class ReviewParticipant(_Row, Base):
    __tablename__ = "perf_participants"
    __table_args__ = (
        UniqueConstraint("company_id", "cycle_id", "employee_id", name="uq_perf_participants_cycle_employee"),
        Index("ix_perf_participants_company_cycle", "company_id", "cycle_id"),
        Index("ix_perf_participants_company_employee", "company_id", "employee_id"),
    )
    cycle_id = Column(Integer, ForeignKey("perf_cycles.id", ondelete="CASCADE"), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    manager_employee_id = _emp_fk()
    skip_manager_employee_id = _emp_fk()
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'in_progress'"))  # in_progress|reviewed|calibrated|published
    scores = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))  # {"self": 3.8, "manager": 4.1, ...}
    computed_score = Column(Numeric(6, 3), nullable=True)
    proposed_rating = Column(Numeric(6, 2), nullable=True)
    calibrated_rating = Column(Numeric(6, 2), nullable=True)
    calibration_reason = Column(Text, nullable=True)
    calibrated_by_name = Column(String(200), nullable=True)
    calibrated_at = Column(DateTime(timezone=True), nullable=True)
    ack_status = Column(String(20), nullable=True)  # pending | accepted | declined
    ack_note = Column(Text, nullable=True)
    final_rating = Column(Numeric(6, 2), nullable=True)
    potential = Column(Integer, nullable=True)  # 1 low, 2 medium, 3 high
    published_at = Column(DateTime(timezone=True), nullable=True)
    letter_no = Column(String(60), nullable=True)
    letter_html = Column(Text, nullable=True)
    letter_generated_at = Column(DateTime(timezone=True), nullable=True)
    response = Column(String(20), nullable=True)  # agree | disagree
    response_note = Column(Text, nullable=True)

    cycle = relationship("ReviewCycle")
    employee = relationship("Employee", foreign_keys=[employee_id])
    manager = relationship("Employee", foreign_keys=[manager_employee_id])
    forms = relationship("ReviewForm", back_populates="participant", cascade="all, delete-orphan")


class ReviewForm(_Row, Base):
    """One reviewer's form for one participant (self, manager, peer or skip-level)."""
    __tablename__ = "perf_review_forms"
    __table_args__ = (
        Index("ix_perf_review_forms_company_participant", "company_id", "participant_id"),
        Index("ix_perf_review_forms_company_reviewer", "company_id", "reviewer_employee_id", "status"),
    )
    participant_id = Column(Integer, ForeignKey("perf_participants.id", ondelete="CASCADE"), nullable=False)
    reviewer_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)  # NULL = company owner
    reviewer_type = Column(String(20), nullable=False)  # self | manager | peer | skip
    status = Column(String(20), nullable=False, server_default=text("'pending'"))  # nominated|pending|draft|submitted|declined
    nominated_by_name = Column(String(200), nullable=True)
    # {"items": {"<section>:<ref>": {"rating": 4, "comment": "..."}}, "summary": "..."}
    answers = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    score = Column(Numeric(6, 3), nullable=True)
    due_date = Column(Date, nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    last_reminded_at = Column(DateTime(timezone=True), nullable=True)
    reminder_log = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))  # ISO times
    escalated_at = Column(DateTime(timezone=True), nullable=True)

    participant = relationship("ReviewParticipant", back_populates="forms")
    reviewer = relationship("Employee", foreign_keys=[reviewer_employee_id])


# ── One-to-ones, feedback, improvement plans ────────────────────────

class OneOnOne(_Row, Base):
    __tablename__ = "perf_one_on_ones"
    __table_args__ = (
        Index("ix_perf_one_on_ones_company_manager", "company_id", "manager_employee_id"),
        Index("ix_perf_one_on_ones_company_employee", "company_id", "employee_id"),
    )
    manager_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)  # NULL = company owner
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    title = Column(String(200), nullable=False)
    scheduled_at = Column(DateTime(timezone=True), nullable=False)
    duration_minutes = Column(Integer, nullable=False, server_default=text("30"))
    recurrence = Column(String(20), nullable=False, server_default=text("'none'"))  # none|weekly|fortnightly|monthly
    status = Column(String(20), nullable=False, server_default=text("'scheduled'"))  # scheduled|done|cancelled
    # [{"id": "a1", "text": "...", "by": "Name", "done": false}]
    agenda = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    shared_notes = Column(Text, nullable=True)
    action_items = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))

    manager = relationship("Employee", foreign_keys=[manager_employee_id])
    employee = relationship("Employee", foreign_keys=[employee_id])


class OneOnOneNote(_Row, Base):
    """Private note: only its author ever sees it."""
    __tablename__ = "perf_one_on_one_notes"
    __table_args__ = (
        UniqueConstraint("company_id", "meeting_id", "author_key", name="uq_perf_one_on_one_notes_author"),
        Index("ix_perf_one_on_one_notes_company_meeting", "company_id", "meeting_id"),
    )
    meeting_id = Column(Integer, ForeignKey("perf_one_on_ones.id", ondelete="CASCADE"), nullable=False)
    author_key = Column(String(30), nullable=False)  # "e:<employee id>" or "owner"
    note = Column(Text, nullable=True)


class Feedback(_Row, Base):
    __tablename__ = "perf_feedback"
    __table_args__ = (
        Index("ix_perf_feedback_company_receiver", "company_id", "receiver_employee_id"),
        Index("ix_perf_feedback_company_giver", "company_id", "giver_employee_id", "status"),
    )
    giver_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)  # NULL = company owner
    receiver_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    requested_by_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'given'"))  # requested | given | declined
    prompt = Column(Text, nullable=True)  # the question asked when requested
    message = Column(Text, nullable=True)
    kind = Column(String(20), nullable=False, server_default=text("'feedback'"))  # feedback | praise | suggestion
    visibility = Column(String(20), nullable=False, server_default=text("'private'"))  # private | manager
    competency_id = Column(Integer, ForeignKey("perf_competencies.id"), nullable=True)
    given_at = Column(DateTime(timezone=True), nullable=True)

    giver = relationship("Employee", foreign_keys=[giver_employee_id])
    receiver = relationship("Employee", foreign_keys=[receiver_employee_id])
    requester = relationship("Employee", foreign_keys=[requested_by_employee_id])


class Pip(_Row, Base):
    __tablename__ = "perf_pips"
    __table_args__ = (Index("ix_perf_pips_company_employee", "company_id", "employee_id"),)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    manager_employee_id = _emp_fk()
    reason = Column(Text, nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    status = Column(String(20), nullable=False, server_default=text("'active'"))  # draft|active|extended|closed
    # [{"id": "g1", "title": "...", "measure": "...", "due_date": "...", "status": "open|met|not_met"}]
    goals = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    support = Column(Text, nullable=True)  # help the company will give
    outcome = Column(String(20), nullable=True)  # successful | extended | unsuccessful
    outcome_note = Column(Text, nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)

    employee = relationship("Employee", foreign_keys=[employee_id])
    manager = relationship("Employee", foreign_keys=[manager_employee_id])
    checkins = relationship("PipCheckin", order_by="PipCheckin.checkin_date", cascade="all, delete-orphan")


class PipCheckin(_Row, Base):
    __tablename__ = "perf_pip_checkins"
    __table_args__ = (Index("ix_perf_pip_checkins_company_pip", "company_id", "pip_id"),)
    pip_id = Column(Integer, ForeignKey("perf_pips.id", ondelete="CASCADE"), nullable=False)
    checkin_date = Column(Date, nullable=False)
    rating = Column(String(20), nullable=True)  # on_track | some_progress | off_track
    note = Column(Text, nullable=False)
    author_name = Column(String(200), nullable=True)


# ── Pay reviews: increments and promotions ──────────────────────────

class IncrementCycle(_Row, Base):
    __tablename__ = "perf_increment_cycles"
    __table_args__ = (Index("ix_perf_increment_cycles_company", "company_id", "status"),)
    name = Column(String(200), nullable=False)
    review_cycle_id = Column(Integer, ForeignKey("perf_cycles.id"), nullable=True)
    effective_date = Column(Date, nullable=False)
    status = Column(String(20), nullable=False, server_default=text("'draft'"))  # draft|open|released|closed
    # {"bands": [{"min": 0, "max": 0.9}, ...], "rows": [{"rating": 5, "pcts": [14, 12, 10]}]}
    guidelines = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    # {"<designation id>": {"min": 300000, "mid": 450000, "max": 600000}}
    pay_ranges = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    letter_template = Column(Text, nullable=True)
    released_at = Column(DateTime(timezone=True), nullable=True)

    review_cycle = relationship("ReviewCycle")


class IncrementBudget(_Row, Base):
    __tablename__ = "perf_increment_budgets"
    __table_args__ = (
        UniqueConstraint("company_id", "cycle_id", "department_id", name="uq_perf_increment_budgets"),
        Index("ix_perf_increment_budgets_company_cycle", "company_id", "cycle_id"),
    )
    cycle_id = Column(Integer, ForeignKey("perf_increment_cycles.id", ondelete="CASCADE"), nullable=False)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)  # NULL = no department
    budget_pct = Column(Numeric(6, 2), nullable=True)
    budget_amount = Column(Numeric(14, 2), nullable=False, server_default=text("0"))


class IncrementProposal(_Row, Base):
    __tablename__ = "perf_increment_proposals"
    __table_args__ = (
        UniqueConstraint("company_id", "cycle_id", "employee_id", name="uq_perf_increment_proposals"),
        Index("ix_perf_increment_proposals_company_cycle", "company_id", "cycle_id"),
    )
    cycle_id = Column(Integer, ForeignKey("perf_increment_cycles.id", ondelete="CASCADE"), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    manager_employee_id = _emp_fk()
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    rating = Column(Numeric(6, 2), nullable=True)
    current_ctc = Column(Numeric(14, 2), nullable=False, server_default=text("0"))
    compa_ratio = Column(Numeric(6, 3), nullable=True)
    guideline_pct = Column(Numeric(6, 2), nullable=True)
    proposed_pct = Column(Numeric(6, 2), nullable=False, server_default=text("0"))
    increment_amount = Column(Numeric(14, 2), nullable=False, server_default=text("0"))
    new_ctc = Column(Numeric(14, 2), nullable=False, server_default=text("0"))
    promote = Column(Boolean, nullable=False, server_default=text("false"))
    current_designation_id = Column(Integer, ForeignKey("designations.id"), nullable=True)
    new_designation_id = Column(Integer, ForeignKey("designations.id"), nullable=True)
    justification = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'draft'"))  # draft|submitted|approved|rejected|released
    workflow_instance_id = Column(Integer, ForeignKey("workflow_instances.id"), nullable=True)
    letter_no = Column(String(60), nullable=True)
    letter_html = Column(Text, nullable=True)
    released_at = Column(DateTime(timezone=True), nullable=True)

    cycle = relationship("IncrementCycle")
    employee = relationship("Employee", foreign_keys=[employee_id])


class JobChange(_Row, Base):
    """Effective-dated change to an employee's job or pay (promotion, increment). Applied to
    the employee record on the effective date; kept as history."""
    __tablename__ = "perf_job_changes"
    __table_args__ = (
        Index("ix_perf_job_changes_company_employee", "company_id", "employee_id", "effective_date"),
        Index("ix_perf_job_changes_company_pending", "company_id", "status", "effective_date"),
    )
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    kind = Column(String(20), nullable=False)  # increment | promotion
    effective_date = Column(Date, nullable=False)
    before = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    after = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    source = Column(String(60), nullable=True)  # increment proposal uuid
    status = Column(String(20), nullable=False, server_default=text("'scheduled'"))  # scheduled | applied | cancelled
    applied_at = Column(DateTime(timezone=True), nullable=True)
    note = Column(Text, nullable=True)
