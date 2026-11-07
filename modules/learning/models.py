"""Learning tables: courses and lessons, learning paths, enrolment rules and enrolments,
trainer-led batches with seats and attendance, the question bank, assessments and
attempts, and course requests. Every table is company-owned (company_id + RLS)."""
from sqlalchemy import (
    Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import declared_attr, relationship
from sqlalchemy.sql import func

from core.db import Base


class _Row:
    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))

    @declared_attr
    def company_id(cls):
        return Column(Integer, ForeignKey("companies.id"), nullable=False)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class LearningSettings(_Row, Base):
    """One row per company: certificate wording, reminder days."""
    __tablename__ = "learning_settings"
    __table_args__ = (UniqueConstraint("company_id", name="uq_learning_settings_company"),)
    config = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))


class Course(_Row, Base):
    __tablename__ = "learning_courses"
    __table_args__ = (Index("ix_learning_courses_company_status", "company_id", "status"),)
    title = Column(String(200), nullable=False)
    summary = Column(String(400), nullable=True)
    description = Column(Text, nullable=True)
    category_id = Column(Integer, nullable=True)  # master value of course_category
    level = Column(String(20), nullable=False, server_default=text("'beginner'"))  # beginner|intermediate|advanced
    delivery = Column(String(20), nullable=False, server_default=text("'online'"))  # online | classroom | blended
    cover = Column(String(40), nullable=True)  # palette key for the card art
    cover_image_url = Column(Text, nullable=True)
    duration_minutes = Column(Integer, nullable=False, server_default=text("0"))
    sequential = Column(Boolean, nullable=False, server_default=text("true"))  # lessons unlock in order
    self_enrol = Column(Boolean, nullable=False, server_default=text("true"))
    enrol_approval = Column(Boolean, nullable=False, server_default=text("false"))
    mandatory = Column(Boolean, nullable=False, server_default=text("false"))
    due_days = Column(Integer, nullable=True)  # days to finish after assignment
    recert_months = Column(Integer, nullable=True)  # re-certify after
    assessment_id = Column(Integer, ForeignKey("learning_assessments.id", ondelete="SET NULL"), nullable=True)
    certificate = Column(Boolean, nullable=False, server_default=text("true"))
    status = Column(String(20), nullable=False, server_default=text("'draft'"))  # draft | published | archived
    tags = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    skill_ids = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    owner_name = Column(String(200), nullable=True)

    lessons = relationship("Lesson", back_populates="course", order_by="Lesson.sort_order", cascade="all, delete-orphan")
    assessment = relationship("Assessment", foreign_keys=[assessment_id])


class Lesson(_Row, Base):
    __tablename__ = "learning_lessons"
    __table_args__ = (Index("ix_learning_lessons_company_course", "company_id", "course_id"),)
    course_id = Column(Integer, ForeignKey("learning_courses.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(200), nullable=False)
    kind = Column(String(20), nullable=False, server_default=text("'text'"))  # video | file | text | assessment
    body = Column(Text, nullable=True)  # text lessons (plain text with blank-line paragraphs)
    url = Column(Text, nullable=True)  # video link or file link / data URL
    file_name = Column(String(200), nullable=True)
    assessment_id = Column(Integer, ForeignKey("learning_assessments.id", ondelete="SET NULL"), nullable=True)
    duration_minutes = Column(Integer, nullable=False, server_default=text("5"))
    sort_order = Column(Integer, nullable=False, server_default=text("0"))

    course = relationship("Course", back_populates="lessons")


class LearningPath(_Row, Base):
    __tablename__ = "learning_paths"
    __table_args__ = (Index("ix_learning_paths_company", "company_id"),)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    kind = Column(String(20), nullable=False, server_default=text("'path'"))  # plan (assigned curriculum) | path
    # [{"course_id": 1, "required": true}] in order
    items = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    sequential = Column(Boolean, nullable=False, server_default=text("false"))
    due_days = Column(Integer, nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'published'"))


class EnrolmentRule(_Row, Base):
    """Assign a course or path to everyone who matches (department, designation, condition)."""
    __tablename__ = "learning_rules"
    __table_args__ = (Index("ix_learning_rules_company", "company_id"),)
    name = Column(String(200), nullable=False)
    course_id = Column(Integer, ForeignKey("learning_courses.id", ondelete="CASCADE"), nullable=True)
    path_id = Column(Integer, ForeignKey("learning_paths.id", ondelete="CASCADE"), nullable=True)
    # {"all": true, "department_ids": [], "designation_ids": [], "condition": ""}
    criteria = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    mandatory = Column(Boolean, nullable=False, server_default=text("true"))
    due_days = Column(Integer, nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    last_run_count = Column(Integer, nullable=False, server_default=text("0"))


class Enrolment(_Row, Base):
    __tablename__ = "learning_enrolments"
    __table_args__ = (
        Index("ix_learning_enrolments_company_employee", "company_id", "employee_id", "status"),
        Index("ix_learning_enrolments_company_course", "company_id", "course_id"),
        Index("uq_learning_enrolments_open", "company_id", "course_id", "employee_id", unique=True,
              postgresql_where=text("status NOT IN ('cancelled', 'expired', 'rejected')")),
    )
    course_id = Column(Integer, ForeignKey("learning_courses.id", ondelete="CASCADE"), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    source = Column(String(20), nullable=False, server_default=text("'self'"))  # self|assigned|rule|manager|path
    assigned_by_name = Column(String(200), nullable=True)
    rule_id = Column(Integer, ForeignKey("learning_rules.id", ondelete="SET NULL"), nullable=True)
    path_id = Column(Integer, ForeignKey("learning_paths.id", ondelete="SET NULL"), nullable=True)
    mandatory = Column(Boolean, nullable=False, server_default=text("false"))
    due_date = Column(Date, nullable=True)
    # requested | enrolled | in_progress | completed | cancelled | rejected | expired
    status = Column(String(20), nullable=False, server_default=text("'enrolled'"))
    progress = Column(Numeric(6, 2), nullable=False, server_default=text("0"))
    lessons_done = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))  # lesson ids
    last_lesson_id = Column(Integer, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    score = Column(Numeric(6, 2), nullable=True)
    passed = Column(Boolean, nullable=True)
    certificate_no = Column(String(60), nullable=True)
    certificate_issued_at = Column(DateTime(timezone=True), nullable=True)
    expires_on = Column(Date, nullable=True)
    workflow_instance_id = Column(Integer, ForeignKey("workflow_instances.id"), nullable=True)

    course = relationship("Course")
    employee = relationship("Employee", foreign_keys=[employee_id])


class Batch(_Row, Base):
    __tablename__ = "learning_batches"
    __table_args__ = (Index("ix_learning_batches_company_course", "company_id", "course_id"),)
    course_id = Column(Integer, ForeignKey("learning_courses.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(200), nullable=False)
    trainer_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    trainer_name = Column(String(200), nullable=True)  # external trainer
    mode = Column(String(20), nullable=False, server_default=text("'classroom'"))  # classroom | virtual
    venue = Column(String(300), nullable=True)
    meeting_link = Column(Text, nullable=True)
    capacity = Column(Integer, nullable=False, server_default=text("20"))
    # [{"id": "s1", "date": "YYYY-MM-DD", "start": "10:00", "end": "13:00", "topic": "..."}]
    sessions = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    status = Column(String(20), nullable=False, server_default=text("'scheduled'"))  # scheduled|completed|cancelled

    course = relationship("Course")
    trainer = relationship("Employee", foreign_keys=[trainer_employee_id])
    seats = relationship("BatchSeat", back_populates="batch", cascade="all, delete-orphan", order_by="BatchSeat.id")


class BatchSeat(_Row, Base):
    __tablename__ = "learning_batch_seats"
    __table_args__ = (
        UniqueConstraint("company_id", "batch_id", "employee_id", name="uq_learning_batch_seats"),
        Index("ix_learning_batch_seats_company_batch", "company_id", "batch_id"),
    )
    batch_id = Column(Integer, ForeignKey("learning_batches.id", ondelete="CASCADE"), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    enrolment_id = Column(Integer, ForeignKey("learning_enrolments.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'confirmed'"))  # confirmed | waitlisted | cancelled
    attendance = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))  # {"s1": "present"|"absent"}

    batch = relationship("Batch", back_populates="seats")
    employee = relationship("Employee", foreign_keys=[employee_id])


class Question(_Row, Base):
    __tablename__ = "learning_questions"
    __table_args__ = (Index("ix_learning_questions_company_category", "company_id", "category"),)
    category = Column(String(100), nullable=False, server_default=text("'General'"))
    kind = Column(String(20), nullable=False)  # single | multi | true_false | short
    body = Column(Text, nullable=False)
    # [{"id": "a", "text": "...", "correct": true}]
    options = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    accepted_answers = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))  # short answer
    marks = Column(Numeric(6, 2), nullable=False, server_default=text("1"))
    difficulty = Column(String(20), nullable=False, server_default=text("'medium'"))  # easy | medium | hard
    explanation = Column(Text, nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))


class Assessment(_Row, Base):
    __tablename__ = "learning_assessments"
    __table_args__ = (Index("ix_learning_assessments_company", "company_id"),)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    selection = Column(String(20), nullable=False, server_default=text("'fixed'"))  # fixed | random
    question_ids = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    # {"count": 10, "categories": ["POSH"], "difficulty": null}
    random_rule = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    time_limit_minutes = Column(Integer, nullable=True)
    max_attempts = Column(Integer, nullable=True)  # NULL = unlimited
    pass_pct = Column(Numeric(5, 2), nullable=False, server_default=text("60"))
    negative_marking = Column(String(20), nullable=False, server_default=text("'none'"))  # none | fixed | percent
    negative_value = Column(Numeric(6, 2), nullable=False, server_default=text("0"))
    shuffle = Column(Boolean, nullable=False, server_default=text("true"))
    show_answers = Column(String(20), nullable=False, server_default=text("'after_submit'"))  # after_submit|after_pass|never
    final_score = Column(String(20), nullable=False, server_default=text("'highest'"))  # highest | latest
    status = Column(String(20), nullable=False, server_default=text("'published'"))  # draft | published


class Attempt(_Row, Base):
    __tablename__ = "learning_attempts"
    __table_args__ = (Index("ix_learning_attempts_company_assessment", "company_id", "assessment_id", "employee_id"),)
    assessment_id = Column(Integer, ForeignKey("learning_assessments.id", ondelete="CASCADE"), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    enrolment_id = Column(Integer, ForeignKey("learning_enrolments.id", ondelete="SET NULL"), nullable=True)
    number = Column(Integer, nullable=False, server_default=text("1"))
    question_ids = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))  # what this attempt got
    answers = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))  # {"<qid>": ["a"] | "text"}
    started_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    deadline_at = Column(DateTime(timezone=True), nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'in_progress'"))  # in_progress | submitted
    score = Column(Numeric(8, 2), nullable=True)
    max_score = Column(Numeric(8, 2), nullable=True)
    pct = Column(Numeric(6, 2), nullable=True)
    passed = Column(Boolean, nullable=True)
    result = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))  # per question marks

    assessment = relationship("Assessment")


class CourseRequest(_Row, Base):
    __tablename__ = "learning_course_requests"
    __table_args__ = (Index("ix_learning_course_requests_company_employee", "company_id", "employee_id"),)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    requester_name = Column(String(200), nullable=True)
    title = Column(String(200), nullable=False)
    reason = Column(Text, nullable=True)
    link = Column(Text, nullable=True)
    provider = Column(String(200), nullable=True)
    estimated_cost = Column(Numeric(12, 2), nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'pending'"))  # pending|approved|rejected|cancelled|fulfilled
    workflow_instance_id = Column(Integer, ForeignKey("workflow_instances.id"), nullable=True)
    course_id = Column(Integer, ForeignKey("learning_courses.id", ondelete="SET NULL"), nullable=True)
    decision_note = Column(Text, nullable=True)
