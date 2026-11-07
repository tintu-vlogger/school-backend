"""Teaching and learning: homework, study materials, live online classes, quizzes, syllabus
and lesson plans.

- LmsSettings          one row per institution: video provider, Jitsi domain, reminders, defaults
- Assignment           homework / assignment / project for one or more sections and one subject
- AssignmentSection    which sections an assignment is set for
- Submission           one learner's work on an assignment (text + files), marks and feedback
- Material             study material (file, link, video, note) by program, subject and topic
- MaterialSection      sections that may see a material (none = every section of the program)
- MaterialView         who opened a material and how often (view counts)
- LiveClass            one scheduled live class (weekly series share `series_uuid`)
- LiveClassJoin        who joined a live class and when (attendance)
- BankQuestion             question bank item (single / multiple choice, true/false, short, numeric, long)
- Quiz                 quiz or test assembled from questions (fixed list or random from the bank)
- QuizSection          sections a quiz is open to
- QuizQuestion         fixed questions of a quiz, in order
- QuizAttempt          one learner's attempt: frozen question list, answers, marks
- SyllabusUnit         unit of a program + subject syllabus (optionally for one term number)
- SyllabusTopic        topic inside a unit
- TopicCoverage        a topic marked covered for one section
- LessonPlan           a teacher's plan for one week of one section + subject
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


def _json_list():
    return Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))


class LmsSettings(Base):
    __tablename__ = "lms_settings"
    __table_args__ = (UniqueConstraint("company_id", name="uq_lms_settings_company"),
                      Index("ix_lms_settings_company", "company_id"))
    id = Column(Integer, primary_key=True)
    company_id = _company()
    config = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at, updated_at = _stamps()


# ── Homework and assignments ─────────────────────────────────────────

class Assignment(Base):
    __tablename__ = "lms_assignments"
    __table_args__ = (Index("ix_lms_assignments_company", "company_id", "due_at"),
                      Index("ix_lms_assignments_subject", "company_id", "subject_id"),
                      Index("ix_lms_assignments_teacher", "company_id", "created_by_employee_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    title = Column(String(200), nullable=False)
    instructions = Column(Text, nullable=True)  # markdown-ish plain text
    kind = Column(String(20), nullable=False, server_default=text("'homework'"))  # homework | assignment | project
    subject_id = Column(Integer, ForeignKey("academic_subjects.id"), nullable=False)
    program_id = Column(Integer, ForeignKey("academic_programs.id"), nullable=True)
    attachments = _json_list()  # [{uuid, name, size, content_type}]
    assigned_on = Column(Date, nullable=True)
    due_at = Column(DateTime(timezone=True), nullable=False)
    max_marks = Column(Numeric(7, 2), nullable=True)
    submission_type = Column(String(20), nullable=False, server_default=text("'online'"))  # online | offline
    allow_late = Column(Boolean, nullable=False, server_default=text("true"))
    status = Column(String(20), nullable=False, server_default=text("'published'"))  # draft | published | closed
    published_at = Column(DateTime(timezone=True), nullable=True)
    created_by_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    created_by_name = Column(String(200), nullable=True)
    archived_at = Column(DateTime(timezone=True), nullable=True)
    created_at, updated_at = _stamps()

    subject = relationship("Subject")
    program = relationship("Program")
    sections = relationship("modules.lms.models.AssignmentSection", cascade="all, delete-orphan", lazy="selectin")


class AssignmentSection(Base):
    __tablename__ = "lms_assignment_sections"
    __table_args__ = (UniqueConstraint("assignment_id", "section_id", name="uq_lms_assignment_sections"),
                      Index("ix_lms_assignment_sections_company", "company_id", "section_id"))
    id = Column(Integer, primary_key=True)
    company_id = _company()
    assignment_id = Column(Integer, ForeignKey("lms_assignments.id", ondelete="CASCADE"), nullable=False)
    section_id = Column(Integer, ForeignKey("academic_sections.id"), nullable=False)


class Submission(Base):
    __tablename__ = "lms_submissions"
    __table_args__ = (UniqueConstraint("assignment_id", "student_id", name="uq_lms_submissions"),
                      Index("ix_lms_submissions_company", "company_id", "assignment_id"),
                      Index("ix_lms_submissions_student", "company_id", "student_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    assignment_id = Column(Integer, ForeignKey("lms_assignments.id", ondelete="CASCADE"), nullable=False)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    content = Column(Text, nullable=True)
    attachments = _json_list()
    # submitted | graded | returned | resubmit (teacher asked for another try) | offline (handed in on paper)
    status = Column(String(20), nullable=False, server_default=text("'submitted'"))
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    is_late = Column(Boolean, nullable=False, server_default=text("false"))
    attempts = Column(Integer, nullable=False, server_default=text("1"))
    submitted_by_portal_id = Column(Integer, ForeignKey("portal_accounts.id"), nullable=True)
    marks = Column(Numeric(7, 2), nullable=True)
    feedback = Column(Text, nullable=True)
    graded_at = Column(DateTime(timezone=True), nullable=True)
    graded_by_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    returned_at = Column(DateTime(timezone=True), nullable=True)
    created_at, updated_at = _stamps()

    assignment = relationship("modules.lms.models.Assignment")
    student = relationship("Student")


# ── Study materials ──────────────────────────────────────────────────

class Material(Base):
    __tablename__ = "lms_materials"
    __table_args__ = (Index("ix_lms_materials_company", "company_id", "program_id", "subject_id"),)
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)  # notes (markdown-ish)
    kind = Column(String(20), nullable=False, server_default=text("'file'"))  # file | link | video | note
    url = Column(Text, nullable=True)
    attachments = _json_list()
    program_id = Column(Integer, ForeignKey("academic_programs.id"), nullable=False)
    subject_id = Column(Integer, ForeignKey("academic_subjects.id"), nullable=False)
    unit_id = Column(Integer, ForeignKey("lms_syllabus_units.id", ondelete="SET NULL"), nullable=True)
    topic_id = Column(Integer, ForeignKey("lms_syllabus_topics.id", ondelete="SET NULL"), nullable=True)
    topic = Column(String(200), nullable=True)  # free label when there is no syllabus topic
    published = Column(Boolean, nullable=False, server_default=text("true"))
    view_count = Column(Integer, nullable=False, server_default=text("0"))
    created_by_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    created_by_name = Column(String(200), nullable=True)
    archived_at = Column(DateTime(timezone=True), nullable=True)
    created_at, updated_at = _stamps()

    program = relationship("Program")
    subject = relationship("Subject")
    unit = relationship("modules.lms.models.SyllabusUnit")
    topic_row = relationship("modules.lms.models.SyllabusTopic")
    sections = relationship("modules.lms.models.MaterialSection", cascade="all, delete-orphan", lazy="selectin")


class MaterialSection(Base):
    __tablename__ = "lms_material_sections"
    __table_args__ = (UniqueConstraint("material_id", "section_id", name="uq_lms_material_sections"),
                      Index("ix_lms_material_sections_company", "company_id", "section_id"))
    id = Column(Integer, primary_key=True)
    company_id = _company()
    material_id = Column(Integer, ForeignKey("lms_materials.id", ondelete="CASCADE"), nullable=False)
    section_id = Column(Integer, ForeignKey("academic_sections.id"), nullable=False)


class MaterialView(Base):
    __tablename__ = "lms_material_views"
    __audit__ = False  # counters
    __table_args__ = (UniqueConstraint("material_id", "viewer_key", name="uq_lms_material_views"),
                      Index("ix_lms_material_views_company", "company_id", "material_id"))
    id = Column(Integer, primary_key=True)
    company_id = _company()
    material_id = Column(Integer, ForeignKey("lms_materials.id", ondelete="CASCADE"), nullable=False)
    viewer_key = Column(String(30), nullable=False)  # s:<student id> | e:<employee id> | p:<portal id>
    student_id = Column(Integer, ForeignKey("students.id"), nullable=True)
    views = Column(Integer, nullable=False, server_default=text("1"))
    first_viewed_at = Column(DateTime(timezone=True), server_default=func.now())
    last_viewed_at = Column(DateTime(timezone=True), server_default=func.now())


# ── Live online classes ──────────────────────────────────────────────

class LiveClass(Base):
    __tablename__ = "lms_live_classes"
    __table_args__ = (Index("ix_lms_live_classes_company", "company_id", "starts_at"),
                      Index("ix_lms_live_classes_section", "company_id", "section_id", "starts_at"),
                      Index("ix_lms_live_classes_teacher", "company_id", "teacher_id", "starts_at"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    series_uuid = Column(UUID(as_uuid=True), nullable=True)  # weekly classes created together
    title = Column(String(200), nullable=False)
    agenda = Column(Text, nullable=True)
    section_id = Column(Integer, ForeignKey("academic_sections.id"), nullable=False)
    subject_id = Column(Integer, ForeignKey("academic_subjects.id"), nullable=True)
    teacher_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    starts_at = Column(DateTime(timezone=True), nullable=False)
    ends_at = Column(DateTime(timezone=True), nullable=False)
    provider = Column(String(20), nullable=False, server_default=text("'jitsi'"))  # jitsi | zoom | meet | teams | other
    room_name = Column(String(120), nullable=True)  # built-in video room (unguessable)
    join_url = Column(Text, nullable=True)  # external meeting link
    meeting_code = Column(String(120), nullable=True)
    passcode = Column(String(60), nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'scheduled'"))  # scheduled | live | completed | cancelled
    started_at = Column(DateTime(timezone=True), nullable=True)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    recording_url = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    reminder_sent_at = Column(DateTime(timezone=True), nullable=True)
    cancel_reason = Column(String(300), nullable=True)
    created_by_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    created_at, updated_at = _stamps()

    section = relationship("Section")
    subject = relationship("Subject")
    teacher = relationship("Employee", foreign_keys=[teacher_id])


class LiveClassJoin(Base):
    __tablename__ = "lms_live_class_joins"
    __table_args__ = (UniqueConstraint("live_class_id", "who_key", name="uq_lms_live_class_joins"),
                      Index("ix_lms_live_class_joins_company", "company_id", "live_class_id"))
    id = Column(Integer, primary_key=True)
    company_id = _company()
    live_class_id = Column(Integer, ForeignKey("lms_live_classes.id", ondelete="CASCADE"), nullable=False)
    who_key = Column(String(30), nullable=False)  # s:<student id> | e:<employee id> | p:<portal id> | o:owner
    role = Column(String(20), nullable=False)  # teacher | student | parent | staff
    student_id = Column(Integer, ForeignKey("students.id"), nullable=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    portal_account_id = Column(Integer, ForeignKey("portal_accounts.id"), nullable=True)
    name = Column(String(200), nullable=True)
    first_joined_at = Column(DateTime(timezone=True), server_default=func.now())
    last_joined_at = Column(DateTime(timezone=True), server_default=func.now())
    join_count = Column(Integer, nullable=False, server_default=text("1"))


# ── Quizzes and tests ────────────────────────────────────────────────

class BankQuestion(Base):
    __tablename__ = "lms_questions"
    __table_args__ = (Index("ix_lms_questions_company", "company_id", "subject_id"),)
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    subject_id = Column(Integer, ForeignKey("academic_subjects.id"), nullable=False)
    program_id = Column(Integer, ForeignKey("academic_programs.id"), nullable=True)
    topic_id = Column(Integer, ForeignKey("lms_syllabus_topics.id", ondelete="SET NULL"), nullable=True)
    topic = Column(String(200), nullable=True)
    # single | multiple | true_false | short | numeric | long
    kind = Column(String(20), nullable=False)
    prompt = Column(Text, nullable=False)
    options = _json_list()  # [{key, text}] for single / multiple / true_false
    # single: ["b"]; multiple: ["a", "c"]; true_false: ["true"]; short: ["photosynthesis", ...];
    # numeric: {"value": 9.8, "tolerance": 0.1}; long: null (marked by hand)
    answer = Column(JSONB, nullable=True)
    marks = Column(Numeric(6, 2), nullable=False, server_default=text("1"))
    negative_marks = Column(Numeric(6, 2), nullable=False, server_default=text("0"))
    difficulty = Column(String(10), nullable=False, server_default=text("'medium'"))  # easy | medium | hard
    tags = _json_list()
    explanation = Column(Text, nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_by_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    created_at, updated_at = _stamps()

    subject = relationship("Subject")


class Quiz(Base):
    __tablename__ = "lms_quizzes"
    __table_args__ = (Index("ix_lms_quizzes_company", "company_id", "opens_at"),)
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    title = Column(String(200), nullable=False)
    instructions = Column(Text, nullable=True)
    kind = Column(String(20), nullable=False, server_default=text("'quiz'"))  # quiz | test
    subject_id = Column(Integer, ForeignKey("academic_subjects.id"), nullable=False)
    program_id = Column(Integer, ForeignKey("academic_programs.id"), nullable=True)
    mode = Column(String(10), nullable=False, server_default=text("'fixed'"))  # fixed | random
    # random: [{"count": 5, "difficulty": "easy"|None, "topic": str|None, "tag": str|None}]
    random_rules = _json_list()
    time_limit_min = Column(Integer, nullable=True)
    attempts_allowed = Column(Integer, nullable=False, server_default=text("1"))
    shuffle_questions = Column(Boolean, nullable=False, server_default=text("false"))
    shuffle_options = Column(Boolean, nullable=False, server_default=text("false"))
    opens_at = Column(DateTime(timezone=True), nullable=True)
    closes_at = Column(DateTime(timezone=True), nullable=True)
    show_answers = Column(String(20), nullable=False, server_default=text("'after_close'"))  # after_submit | after_close | never
    pass_percent = Column(Numeric(5, 2), nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'draft'"))  # draft | published | closed
    created_by_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    created_by_name = Column(String(200), nullable=True)
    archived_at = Column(DateTime(timezone=True), nullable=True)
    created_at, updated_at = _stamps()

    subject = relationship("Subject")
    sections = relationship("modules.lms.models.QuizSection", cascade="all, delete-orphan", lazy="selectin")
    questions = relationship("modules.lms.models.QuizQuestion", cascade="all, delete-orphan", order_by=lambda: QuizQuestion.sort_order)


class QuizSection(Base):
    __tablename__ = "lms_quiz_sections"
    __table_args__ = (UniqueConstraint("quiz_id", "section_id", name="uq_lms_quiz_sections"),
                      Index("ix_lms_quiz_sections_company", "company_id", "section_id"))
    id = Column(Integer, primary_key=True)
    company_id = _company()
    quiz_id = Column(Integer, ForeignKey("lms_quizzes.id", ondelete="CASCADE"), nullable=False)
    section_id = Column(Integer, ForeignKey("academic_sections.id"), nullable=False)


class QuizQuestion(Base):
    __tablename__ = "lms_quiz_questions"
    __table_args__ = (UniqueConstraint("quiz_id", "question_id", name="uq_lms_quiz_questions"),
                      Index("ix_lms_quiz_questions_company", "company_id", "quiz_id"))
    id = Column(Integer, primary_key=True)
    company_id = _company()
    quiz_id = Column(Integer, ForeignKey("lms_quizzes.id", ondelete="CASCADE"), nullable=False)
    question_id = Column(Integer, ForeignKey("lms_questions.id"), nullable=False)
    sort_order = Column(Integer, nullable=False, server_default=text("1"))
    marks = Column(Numeric(6, 2), nullable=True)  # overrides the question's marks

    question = relationship("modules.lms.models.BankQuestion")


class QuizAttempt(Base):
    __tablename__ = "lms_quiz_attempts"
    __table_args__ = (UniqueConstraint("quiz_id", "student_id", "attempt_no", name="uq_lms_quiz_attempts"),
                      Index("ix_lms_quiz_attempts_company", "company_id", "quiz_id"),
                      Index("ix_lms_quiz_attempts_student", "company_id", "student_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    quiz_id = Column(Integer, ForeignKey("lms_quizzes.id", ondelete="CASCADE"), nullable=False)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    attempt_no = Column(Integer, nullable=False, server_default=text("1"))
    status = Column(String(20), nullable=False, server_default=text("'in_progress'"))  # in_progress | submitted | marked
    started_at = Column(DateTime(timezone=True), server_default=func.now())
    deadline_at = Column(DateTime(timezone=True), nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    # [{"question_id": 1, "marks": "1", "options": ["b", "a", "c"]}] frozen at start
    paper = _json_list()
    answers = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))  # {question_id: answer}
    # {question_id: {"score": "1", "correct": true|false|null, "auto": true, "feedback": ""}}
    item_scores = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    score = Column(Numeric(7, 2), nullable=True)
    max_score = Column(Numeric(7, 2), nullable=True)
    needs_marking = Column(Boolean, nullable=False, server_default=text("false"))
    marked_by_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    created_at, updated_at = _stamps()

    quiz = relationship("modules.lms.models.Quiz")
    student = relationship("Student")


# ── Syllabus and lesson plans ────────────────────────────────────────

class SyllabusUnit(Base):
    __tablename__ = "lms_syllabus_units"
    __table_args__ = (Index("ix_lms_syllabus_units_company", "company_id", "program_id", "subject_id"),)
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    program_id = Column(Integer, ForeignKey("academic_programs.id"), nullable=False)
    subject_id = Column(Integer, ForeignKey("academic_subjects.id"), nullable=False)
    term_no = Column(Integer, nullable=True)  # term / semester number; empty = whole session
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    sort_order = Column(Integer, nullable=False, server_default=text("1"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()

    topics = relationship("modules.lms.models.SyllabusTopic", back_populates="unit", order_by=lambda: SyllabusTopic.sort_order)


class SyllabusTopic(Base):
    __tablename__ = "lms_syllabus_topics"
    __table_args__ = (Index("ix_lms_syllabus_topics_company", "company_id", "unit_id"),)
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    unit_id = Column(Integer, ForeignKey("lms_syllabus_units.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    planned_periods = Column(Integer, nullable=True)
    sort_order = Column(Integer, nullable=False, server_default=text("1"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()

    unit = relationship("modules.lms.models.SyllabusUnit", back_populates="topics")


class TopicCoverage(Base):
    __tablename__ = "lms_topic_coverage"
    __table_args__ = (UniqueConstraint("topic_id", "section_id", name="uq_lms_topic_coverage"),
                      Index("ix_lms_topic_coverage_company", "company_id", "section_id"))
    id = Column(Integer, primary_key=True)
    company_id = _company()
    topic_id = Column(Integer, ForeignKey("lms_syllabus_topics.id", ondelete="CASCADE"), nullable=False)
    section_id = Column(Integer, ForeignKey("academic_sections.id"), nullable=False)
    covered_on = Column(Date, nullable=False)
    covered_by_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    note = Column(String(300), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class LessonPlan(Base):
    __tablename__ = "lms_lesson_plans"
    __table_args__ = (Index("ix_lms_lesson_plans_company", "company_id", "section_id", "week_start"),
                      Index("ix_lms_lesson_plans_teacher", "company_id", "teacher_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    section_id = Column(Integer, ForeignKey("academic_sections.id"), nullable=False)
    subject_id = Column(Integer, ForeignKey("academic_subjects.id"), nullable=False)
    teacher_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    week_start = Column(Date, nullable=False)  # Monday of the week
    title = Column(String(200), nullable=True)
    topic_ids = _json_list()
    objectives = Column(Text, nullable=True)
    activities = Column(Text, nullable=True)
    resources = Column(Text, nullable=True)
    assessment = Column(Text, nullable=True)
    homework_id = Column(Integer, ForeignKey("lms_assignments.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'planned'"))  # planned | done | skipped
    reflection = Column(Text, nullable=True)
    archived_at = Column(DateTime(timezone=True), nullable=True)
    created_at, updated_at = _stamps()

    section = relationship("Section")
    subject = relationship("Subject")
    teacher = relationship("Employee", foreign_keys=[teacher_id])
    homework = relationship("modules.lms.models.Assignment")


TABLES = ["lms_settings", "lms_assignments", "lms_assignment_sections", "lms_submissions", "lms_materials",
          "lms_material_sections", "lms_material_views", "lms_live_classes", "lms_live_class_joins", "lms_questions",
          "lms_quizzes", "lms_quiz_sections", "lms_quiz_questions", "lms_quiz_attempts", "lms_syllabus_units",
          "lms_syllabus_topics", "lms_topic_coverage", "lms_lesson_plans"]
