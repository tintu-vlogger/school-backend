"""Exams, grading and results.

Works for any assessment system: percentage with grade bands (CBSE, ICSE, state boards),
GPA / SGPA / CGPA on 4, 5 or 10 point scales with credits, pass / fail, descriptive or
competency levels, or plain marks.

- GradingScheme / GradeBand   how a percentage becomes a grade (and grade point); versioned
- ExamType                    unit test, mid-term, final, practical, project, viva …
- Exam                        one assessment event in a session (optionally a term), for some programs
- ExamPaper                   a subject of an exam for one program: date, time, room, max / pass marks,
                              components (theory / practical / internal)
- ExamRoom                    seating plan: roll ranges of a section in a room, with an invigilator
- MarkSheet                   status of marks entry for one paper and one section (draft -> submitted -> locked)
- Mark                        one learner's marks in one paper (or a code: absent, exempt, medical)
- ResultRule                  how exams combine into a term or final result, pass rules, grace, ranking
- ExamResult                  computed result of one learner for one exam or rule (cached, recomputed)
- CoScholasticArea            graded areas such as Work education, Art, Discipline
- TermRecord                  per learner per term: co-scholastic grades, remarks, attendance
- ReportTemplate              what a report card or transcript shows
"""
from sqlalchemy import (Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, Time,
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


def _json(default: str = "'{}'::jsonb"):
    return Column(JSONB, nullable=False, server_default=text(default))


class GradingScheme(Base):
    __tablename__ = "exam_grading_schemes"
    __table_args__ = (Index("ix_exam_grading_schemes_company", "company_id", "code"),)
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(120), nullable=False)
    # Stable key shared by every version, e.g. "percentage_grades", "gpa_10" (matches profile().grading).
    code = Column(String(40), nullable=False)
    # percentage_bands | gpa | pass_fail | descriptive | marks
    kind = Column(String(20), nullable=False, server_default=text("'percentage_bands'"))
    scale_max = Column(Numeric(5, 2), nullable=True)  # GPA scale: 4, 5, 10
    pass_percent = Column(Numeric(5, 2), nullable=False, server_default=text("33"))
    decimals = Column(Integer, nullable=False, server_default=text("2"))
    # Formula turning a CGPA into a percentage, e.g. "cgpa * 9.5" (core.expressions).
    percent_formula = Column(String(200), nullable=True)
    description = Column(Text, nullable=True)
    version = Column(Integer, nullable=False, server_default=text("1"))
    lineage_id = Column(UUID(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()"))
    effective_from = Column(Date, nullable=True)
    effective_to = Column(Date, nullable=True)
    is_system = Column(Boolean, nullable=False, server_default=text("false"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()

    bands = relationship("GradeBand", back_populates="scheme", order_by="GradeBand.min_percent.desc()",
                         cascade="all, delete-orphan")


class GradeBand(Base):
    __tablename__ = "exam_grade_bands"
    __table_args__ = (Index("ix_exam_grade_bands_company", "company_id", "scheme_id"),)
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    scheme_id = Column(Integer, ForeignKey("exam_grading_schemes.id", ondelete="CASCADE"), nullable=False)
    label = Column(String(40), nullable=False)  # A1, O, A+, Proficient, Competent …
    min_percent = Column(Numeric(6, 2), nullable=False)
    grade_point = Column(Numeric(5, 2), nullable=True)
    description = Column(String(120), nullable=True)  # Outstanding, Excellent …
    is_pass = Column(Boolean, nullable=False, server_default=text("true"))
    color = Column(String(20), nullable=True)
    created_at, updated_at = _stamps()

    scheme = relationship("GradingScheme", back_populates="bands")


class ExamType(Base):
    __tablename__ = "exam_types"
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_exam_types_name"),
                      Index("ix_exam_types_company", "company_id", "sort_order"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(80), nullable=False)
    code = Column(String(20), nullable=True)
    # unit_test | mid_term | final | internal | practical | project | viva | continuous | other
    category = Column(String(20), nullable=False, server_default=text("'other'"))
    default_max_marks = Column(Numeric(7, 2), nullable=True)
    sort_order = Column(Integer, nullable=False, server_default=text("1"))
    description = Column(Text, nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class Exam(Base):
    __tablename__ = "exams"
    __table_args__ = (Index("ix_exams_company", "company_id", "session_id", "start_date"),)
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    session_id = Column(Integer, ForeignKey("academic_sessions.id"), nullable=False)
    term_id = Column(Integer, ForeignKey("academic_terms.id"), nullable=True)
    exam_type_id = Column(Integer, ForeignKey("exam_types.id"), nullable=True)
    name = Column(String(120), nullable=False)
    code = Column(String(30), nullable=True)
    # Programs that write this exam (list of ids); empty = every program.
    program_ids = _json("'[]'::jsonb")
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    marks_due_on = Column(Date, nullable=True)
    grading_scheme_id = Column(Integer, ForeignKey("exam_grading_schemes.id"), nullable=True)  # overrides program / institution
    # draft | scheduled | published
    status = Column(String(20), nullable=False, server_default=text("'draft'"))
    ranking = Column(Boolean, nullable=False, server_default=text("true"))
    instructions = Column(Text, nullable=True)  # printed on hall tickets
    published_at = Column(DateTime(timezone=True), nullable=True)
    config = _json()
    archived_at = Column(DateTime(timezone=True), nullable=True)
    created_at, updated_at = _stamps()

    exam_type = relationship("ExamType")
    term = relationship("Term")
    session = relationship("AcademicSession")
    papers = relationship("ExamPaper", back_populates="exam", order_by="ExamPaper.paper_date")


class ExamPaper(Base):
    __tablename__ = "exam_papers"
    __table_args__ = (UniqueConstraint("exam_id", "program_id", "subject_id", name="uq_exam_papers"),
                      Index("ix_exam_papers_company", "company_id", "exam_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    exam_id = Column(Integer, ForeignKey("exams.id", ondelete="CASCADE"), nullable=False)
    program_id = Column(Integer, ForeignKey("academic_programs.id"), nullable=False)
    subject_id = Column(Integer, ForeignKey("academic_subjects.id"), nullable=False)
    paper_date = Column(Date, nullable=True)
    start_time = Column(Time, nullable=True)
    end_time = Column(Time, nullable=True)
    room_id = Column(Integer, ForeignKey("academic_rooms.id"), nullable=True)
    invigilator_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    max_marks = Column(Numeric(7, 2), nullable=False, server_default=text("100"))
    pass_marks = Column(Numeric(7, 2), nullable=True)
    # [{key, label, max_marks, pass_marks}] e.g. theory 80 + internal 20; empty = one mark.
    components = _json("'[]'::jsonb")
    # marks | grade (graded subjects entered directly as a grade)
    entry_mode = Column(String(10), nullable=False, server_default=text("'marks'"))
    counts_in_total = Column(Boolean, nullable=False, server_default=text("true"))
    credits = Column(Numeric(5, 2), nullable=True)  # overrides curriculum / subject credits
    grading_scheme_id = Column(Integer, ForeignKey("exam_grading_schemes.id"), nullable=True)
    sort_order = Column(Integer, nullable=False, server_default=text("1"))
    created_at, updated_at = _stamps()

    exam = relationship("Exam", back_populates="papers")
    program = relationship("Program")
    subject = relationship("Subject")
    room = relationship("Room")
    invigilator = relationship("Employee", foreign_keys=[invigilator_id])


class ExamRoom(Base):
    """Seating plan: a roll range of one section sits in one room (optionally on one date)."""
    __tablename__ = "exam_rooms"
    __table_args__ = (Index("ix_exam_rooms_company", "company_id", "exam_id"),)
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    exam_id = Column(Integer, ForeignKey("exams.id", ondelete="CASCADE"), nullable=False)
    room_id = Column(Integer, ForeignKey("academic_rooms.id"), nullable=False)
    section_id = Column(Integer, ForeignKey("academic_sections.id"), nullable=False)
    roll_from = Column(String(20), nullable=True)
    roll_to = Column(String(20), nullable=True)
    seats = Column(Integer, nullable=True)
    invigilator_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    created_at, updated_at = _stamps()

    room = relationship("Room")
    section = relationship("Section")
    invigilator = relationship("Employee", foreign_keys=[invigilator_id])


class MarkSheet(Base):
    __tablename__ = "exam_mark_sheets"
    __table_args__ = (UniqueConstraint("paper_id", "section_id", name="uq_exam_mark_sheets"),
                      Index("ix_exam_mark_sheets_company", "company_id", "exam_id", "section_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    exam_id = Column(Integer, ForeignKey("exams.id", ondelete="CASCADE"), nullable=False)
    paper_id = Column(Integer, ForeignKey("exam_papers.id", ondelete="CASCADE"), nullable=False)
    section_id = Column(Integer, ForeignKey("academic_sections.id"), nullable=False)
    # draft | submitted | locked
    status = Column(String(12), nullable=False, server_default=text("'draft'"))
    moderated = Column(Boolean, nullable=False, server_default=text("false"))
    moderation_note = Column(Text, nullable=True)
    entered_by = Column(Integer, ForeignKey("employees.id"), nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    submitted_by = Column(Integer, ForeignKey("employees.id"), nullable=True)
    locked_at = Column(DateTime(timezone=True), nullable=True)
    locked_by = Column(Integer, ForeignKey("employees.id"), nullable=True)
    created_at, updated_at = _stamps()


class Mark(Base):
    __tablename__ = "exam_marks"
    __table_args__ = (UniqueConstraint("paper_id", "student_id", name="uq_exam_marks"),
                      Index("ix_exam_marks_company", "company_id", "exam_id", "section_id"),
                      Index("ix_exam_marks_student", "company_id", "student_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    exam_id = Column(Integer, ForeignKey("exams.id", ondelete="CASCADE"), nullable=False)
    paper_id = Column(Integer, ForeignKey("exam_papers.id", ondelete="CASCADE"), nullable=False)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    section_id = Column(Integer, ForeignKey("academic_sections.id"), nullable=True)
    marks = Column(Numeric(7, 2), nullable=True)
    components = _json()  # {"theory": 56, "practical": 18}
    # AB absent (counts as zero) | EX exempt | ML medical (left out of totals) | null
    code = Column(String(4), nullable=True)
    grade = Column(String(40), nullable=True)  # graded papers
    grace = Column(Numeric(6, 2), nullable=False, server_default=text("0"))
    moderation = Column(Numeric(6, 2), nullable=False, server_default=text("0"))
    remarks = Column(String(300), nullable=True)
    created_at, updated_at = _stamps()


class ResultRule(Base):
    """How exams combine into a term or final result. `groups` is a list of
    {key, label, exam_ids, combine: average|best_of|sum|max|latest, best_of: n, weight};
    method `weighted` (default) or `formula` (expression over the group keys)."""
    __tablename__ = "exam_result_rules"
    __table_args__ = (Index("ix_exam_result_rules_company", "company_id", "session_id"),)
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    session_id = Column(Integer, ForeignKey("academic_sessions.id"), nullable=False)
    term_id = Column(Integer, ForeignKey("academic_terms.id"), nullable=True)  # empty = whole session (final)
    name = Column(String(120), nullable=False)
    program_ids = _json("'[]'::jsonb")
    grading_scheme_id = Column(Integer, ForeignKey("exam_grading_schemes.id"), nullable=True)
    method = Column(String(12), nullable=False, server_default=text("'weighted'"))  # weighted | formula
    formula = Column(String(500), nullable=True)
    groups = _json("'[]'::jsonb")
    # Pass rules
    pass_all_subjects = Column(Boolean, nullable=False, server_default=text("true"))
    subject_pass_percent = Column(Numeric(5, 2), nullable=True)  # empty = scheme's pass mark
    min_aggregate_percent = Column(Numeric(5, 2), nullable=True)
    max_failed_subjects = Column(Integer, nullable=True)  # failing up to N = supplementary / compartment
    min_attendance_percent = Column(Numeric(5, 2), nullable=True)
    # Grace marks (percentage points added to reach the pass mark)
    grace_per_subject = Column(Numeric(5, 2), nullable=True)
    grace_max_subjects = Column(Integer, nullable=True)
    grace_total = Column(Numeric(6, 2), nullable=True)
    ranking = Column(Boolean, nullable=False, server_default=text("true"))
    cumulative = Column(Boolean, nullable=False, server_default=text("false"))  # also compute CGPA across terms
    active = Column(Boolean, nullable=False, server_default=text("true"))
    config = _json()
    created_at, updated_at = _stamps()

    term = relationship("Term")


class ExamResult(Base):
    __tablename__ = "exam_results"
    __table_args__ = (UniqueConstraint("source", "student_id", name="uq_exam_results_source"),
                      Index("ix_exam_results_company", "company_id", "source", "section_id"),
                      Index("ix_exam_results_student", "company_id", "student_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    source = Column(String(30), nullable=False)  # "exam:12" or "rule:3"
    exam_id = Column(Integer, ForeignKey("exams.id", ondelete="CASCADE"), nullable=True)
    rule_id = Column(Integer, ForeignKey("exam_result_rules.id", ondelete="CASCADE"), nullable=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    section_id = Column(Integer, ForeignKey("academic_sections.id"), nullable=True)
    program_id = Column(Integer, ForeignKey("academic_programs.id"), nullable=True)
    session_id = Column(Integer, ForeignKey("academic_sessions.id"), nullable=True)
    term_id = Column(Integer, ForeignKey("academic_terms.id"), nullable=True)
    scheme_id = Column(Integer, ForeignKey("exam_grading_schemes.id"), nullable=True)
    total_obtained = Column(Numeric(9, 2), nullable=True)
    total_max = Column(Numeric(9, 2), nullable=True)
    percentage = Column(Numeric(6, 2), nullable=True)
    grade = Column(String(40), nullable=True)
    grade_point = Column(Numeric(5, 2), nullable=True)
    gpa = Column(Numeric(5, 2), nullable=True)  # SGPA for a term
    cgpa = Column(Numeric(5, 2), nullable=True)
    credits_attempted = Column(Numeric(7, 2), nullable=True)
    credits_earned = Column(Numeric(7, 2), nullable=True)
    rank = Column(Integer, nullable=True)  # within the section
    program_rank = Column(Integer, nullable=True)
    # pass | fail | compartment | not_eligible | incomplete
    status = Column(String(16), nullable=False, server_default=text("'incomplete'"))
    passed = Column(Boolean, nullable=True)
    failed_subjects = Column(Integer, nullable=False, server_default=text("0"))
    grace_applied = Column(Numeric(6, 2), nullable=False, server_default=text("0"))
    attendance_percent = Column(Numeric(5, 2), nullable=True)
    subjects = _json("'[]'::jsonb")  # per-subject breakdown
    computed_at = Column(DateTime(timezone=True), server_default=func.now())
    published_at = Column(DateTime(timezone=True), nullable=True)
    published_by = Column(Integer, ForeignKey("employees.id"), nullable=True)
    created_at, updated_at = _stamps()


class CoScholasticArea(Base):
    __tablename__ = "exam_coscholastic_areas"
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_exam_coscholastic_areas_name"),
                      Index("ix_exam_coscholastic_areas_company", "company_id", "sort_order"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(120), nullable=False)
    group_name = Column(String(80), nullable=True)  # Co-scholastic, Personal & social qualities …
    grading_scheme_id = Column(Integer, ForeignKey("exam_grading_schemes.id"), nullable=True)
    program_ids = _json("'[]'::jsonb")
    sort_order = Column(Integer, nullable=False, server_default=text("1"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class TermRecord(Base):
    """Per learner per term (or whole session): co-scholastic grades, class teacher's remarks
    and attendance (typed in, or read from the attendance module when it is there)."""
    __tablename__ = "exam_term_records"
    __table_args__ = (UniqueConstraint("student_id", "session_id", "scope", name="uq_exam_term_records"),
                      Index("ix_exam_term_records_company", "company_id", "section_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    session_id = Column(Integer, ForeignKey("academic_sessions.id"), nullable=False)
    term_id = Column(Integer, ForeignKey("academic_terms.id"), nullable=True)
    scope = Column(String(20), nullable=False)  # "term:5" | "session"
    section_id = Column(Integer, ForeignKey("academic_sections.id"), nullable=True)
    grades = _json()  # {area_id: "A"}
    remarks = Column(Text, nullable=True)
    days_present = Column(Numeric(6, 1), nullable=True)
    days_total = Column(Numeric(6, 1), nullable=True)
    entered_by = Column(Integer, ForeignKey("employees.id"), nullable=True)
    created_at, updated_at = _stamps()


class ReportTemplate(Base):
    __tablename__ = "exam_report_templates"
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_exam_report_templates_name"),
                      Index("ix_exam_report_templates_company", "company_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(120), nullable=False)
    kind = Column(String(20), nullable=False, server_default=text("'report_card'"))  # report_card | transcript
    title = Column(String(160), nullable=True)
    session_id = Column(Integer, ForeignKey("academic_sessions.id"), nullable=True)
    program_ids = _json("'[]'::jsonb")
    # [{source: "exam:12" | "rule:3", label}] in column order
    columns = _json("'[]'::jsonb")
    # The source whose overall result (total, %, grade, rank, pass) is printed at the bottom.
    summary_source = Column(String(30), nullable=True)
    show_marks = Column(Boolean, nullable=False, server_default=text("true"))
    show_grade = Column(Boolean, nullable=False, server_default=text("true"))
    show_rank = Column(Boolean, nullable=False, server_default=text("false"))
    show_coscholastic = Column(Boolean, nullable=False, server_default=text("true"))
    show_attendance = Column(Boolean, nullable=False, server_default=text("true"))
    show_remarks = Column(Boolean, nullable=False, server_default=text("true"))
    show_logo = Column(Boolean, nullable=False, server_default=text("true"))
    show_grading_key = Column(Boolean, nullable=False, server_default=text("true"))
    signatures = _json("'[\"Class teacher\", \"Principal\", \"Parent\"]'::jsonb")
    footer_note = Column(Text, nullable=True)
    is_default = Column(Boolean, nullable=False, server_default=text("false"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


TABLES = ["exam_grading_schemes", "exam_grade_bands", "exam_types", "exams", "exam_papers", "exam_rooms",
          "exam_mark_sheets", "exam_marks", "exam_result_rules", "exam_results", "exam_coscholastic_areas",
          "exam_term_records", "exam_report_templates"]
