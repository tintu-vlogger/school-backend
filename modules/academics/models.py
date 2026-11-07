"""Academic structure of an institution.

Works for any kind of institution: a school (classes and sections), a college or university
(programmes, semesters, batches, credit-based courses), a coaching or training centre
(courses and batches). The words shown on screen come from the institution profile
(`companies.config["institution"]`, see service.profile), the tables stay the same:

- AcademicSession  an academic year or intake ("2026-27", "Fall 2026")
- Term             a part of a session: term, semester, trimester, quarter, module
- Program          what a learner is enrolled in: "Grade 5", "B.Sc Physics", "JEE 2027"
- Section          a group of learners of one program in one session: "5-A", "Batch 2"
- Subject          a subject or course ("Mathematics", "PHY101 Mechanics"), with credits
- ProgramSubject   the curriculum: which subjects a program teaches (per term when semester-based)
- SectionSubject   who teaches each subject to each section
- Room, House      classrooms/labs/online rooms, and student houses
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


class AcademicSession(Base):
    __tablename__ = "academic_sessions"
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_academic_sessions_name"),
                      Index("ix_academic_sessions_company", "company_id", "start_date"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(60), nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    is_current = Column(Boolean, nullable=False, server_default=text("false"))
    status = Column(String(20), nullable=False, server_default=text("'active'"))  # planned | active | closed
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()

    terms = relationship("Term", back_populates="session", order_by="Term.sort_order")


class Term(Base):
    __tablename__ = "academic_terms"
    __table_args__ = (UniqueConstraint("session_id", "name", name="uq_academic_terms_name"),
                      Index("ix_academic_terms_company", "company_id", "session_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    session_id = Column(Integer, ForeignKey("academic_sessions.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(60), nullable=False)
    kind = Column(String(20), nullable=False, server_default=text("'term'"))  # term | semester | trimester | quarter | module
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    sort_order = Column(Integer, nullable=False, server_default=text("1"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()

    session = relationship("AcademicSession", back_populates="terms")


class Program(Base):
    __tablename__ = "academic_programs"
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_academic_programs_name"),
                      Index("ix_academic_programs_company", "company_id", "sort_order"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(120), nullable=False)
    code = Column(String(30), nullable=True)
    # Free label for grouping: "Pre-primary", "Primary", "Secondary", "Undergraduate", "Diploma" …
    level = Column(String(60), nullable=True)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    # How long the program runs, in terms (e.g. 8 semesters); empty for one-year classes.
    duration_terms = Column(Integer, nullable=True)
    # Grading scheme used for results (exams module); empty = institution default.
    grading_scheme_id = Column(Integer, nullable=True)
    # The program learners move to when promoted (e.g. Grade 5 -> Grade 6).
    next_program_id = Column(Integer, ForeignKey("academic_programs.id"), nullable=True)
    sort_order = Column(Integer, nullable=False, server_default=text("1"))
    description = Column(Text, nullable=True)
    config = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()

    sections = relationship("Section", back_populates="program")


class Section(Base):
    __tablename__ = "academic_sections"
    __table_args__ = (UniqueConstraint("program_id", "session_id", "name", name="uq_academic_sections_name"),
                      Index("ix_academic_sections_company", "company_id", "session_id", "program_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    program_id = Column(Integer, ForeignKey("academic_programs.id"), nullable=False)
    session_id = Column(Integer, ForeignKey("academic_sessions.id"), nullable=False)
    name = Column(String(60), nullable=False)
    capacity = Column(Integer, nullable=True)
    class_teacher_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    room_id = Column(Integer, ForeignKey("academic_rooms.id"), nullable=True)
    # Medium of instruction / shift, where institutions run several.
    medium = Column(String(40), nullable=True)
    shift = Column(String(40), nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()

    program = relationship("Program", back_populates="sections")
    session = relationship("AcademicSession")
    class_teacher = relationship("Employee", foreign_keys=[class_teacher_id])
    room = relationship("Room")


class Subject(Base):
    __tablename__ = "academic_subjects"
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_academic_subjects_name"),
                      Index("ix_academic_subjects_company", "company_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(120), nullable=False)
    code = Column(String(30), nullable=True)
    # theory | practical | theory_practical | language | activity | project | elective
    kind = Column(String(30), nullable=False, server_default=text("'theory'"))
    credits = Column(Numeric(5, 2), nullable=True)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    color = Column(String(20), nullable=True)
    description = Column(Text, nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class ProgramSubject(Base):
    """The curriculum: subjects taught in a program (optionally only in one term number,
    for semester-based programs)."""
    __tablename__ = "academic_program_subjects"
    __table_args__ = (UniqueConstraint("program_id", "subject_id", "term_no", name="uq_program_subjects"),
                      Index("ix_program_subjects_company", "company_id", "program_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    program_id = Column(Integer, ForeignKey("academic_programs.id", ondelete="CASCADE"), nullable=False)
    subject_id = Column(Integer, ForeignKey("academic_subjects.id"), nullable=False)
    term_no = Column(Integer, nullable=True)  # e.g. semester 3; empty = whole program/session
    is_elective = Column(Boolean, nullable=False, server_default=text("false"))
    elective_group = Column(String(60), nullable=True)  # learners pick N from the same group
    credits = Column(Numeric(5, 2), nullable=True)  # overrides the subject's credits
    weekly_periods = Column(Integer, nullable=True)
    sort_order = Column(Integer, nullable=False, server_default=text("1"))
    created_at, updated_at = _stamps()

    program = relationship("Program")
    subject = relationship("Subject")


class SectionSubject(Base):
    """Teacher allocation: who teaches which subject to which section."""
    __tablename__ = "academic_section_subjects"
    __table_args__ = (UniqueConstraint("section_id", "subject_id", name="uq_section_subjects"),
                      Index("ix_section_subjects_company", "company_id", "section_id"),
                      Index("ix_section_subjects_teacher", "company_id", "teacher_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    section_id = Column(Integer, ForeignKey("academic_sections.id", ondelete="CASCADE"), nullable=False)
    subject_id = Column(Integer, ForeignKey("academic_subjects.id"), nullable=False)
    teacher_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    # Extra teachers (co-teaching, lab assistants): list of employee ids.
    assistant_ids = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    weekly_periods = Column(Integer, nullable=True)
    created_at, updated_at = _stamps()

    section = relationship("Section")
    subject = relationship("Subject")
    teacher = relationship("Employee", foreign_keys=[teacher_id])


class Room(Base):
    __tablename__ = "academic_rooms"
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_academic_rooms_name"),
                      Index("ix_academic_rooms_company", "company_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(80), nullable=False)
    code = Column(String(30), nullable=True)
    building = Column(String(80), nullable=True)
    floor = Column(String(20), nullable=True)
    capacity = Column(Integer, nullable=True)
    kind = Column(String(20), nullable=False, server_default=text("'classroom'"))  # classroom | lab | hall | library | sports | online
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class House(Base):
    __tablename__ = "academic_houses"
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_academic_houses_name"),
                      Index("ix_academic_houses_company", "company_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(60), nullable=False)
    color = Column(String(20), nullable=True)
    motto = Column(String(200), nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


TABLES = ["academic_sessions", "academic_terms", "academic_programs", "academic_sections", "academic_subjects",
          "academic_program_subjects", "academic_section_subjects", "academic_rooms", "academic_houses"]
