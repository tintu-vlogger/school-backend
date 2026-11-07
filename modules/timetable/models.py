"""Timetable: bell schedules, the weekly timetable of every section, and substitutions.

- BellSchedule      the day's periods and breaks, for some weekdays and optionally only for
                    some programs (a section group: "Primary", "Senior secondary", a college shift)
- BellPeriod        one slot of a bell schedule: a teaching period (numbered 1, 2, 3 …) or a break
- TimetableEntry    section x weekday x period -> subject, teacher, room. Several entries in one
                    cell are allowed for electives / split groups (same `elective_group`), and
                    entries in several sections that share a `group_key` are one combined class
                    (one teacher, one room, several sections).
- TimetablePublication  per section: when its timetable was last published, and whether it has
                    changed since (teachers, parents and students only see published timetables)
- Substitution      a dated change: someone else takes (or the institution cancels) one period
                    of an absent teacher
- TimetableSettings per institution limits used by clash checks and the generator
"""
from sqlalchemy import (Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, String, Time,
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


class BellSchedule(Base):
    __tablename__ = "timetable_bell_schedules"
    __table_args__ = (Index("ix_timetable_bell_schedules_company", "company_id", "active"),)
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(80), nullable=False)
    # ISO weekdays (1 = Monday) this schedule is used on.
    weekdays = Column(JSONB, nullable=False, server_default=text("'[1,2,3,4,5]'::jsonb"))
    # Programs (classes / programmes) it applies to; empty = every program.
    program_ids = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    is_default = Column(Boolean, nullable=False, server_default=text("false"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()

    periods = relationship("BellPeriod", back_populates="schedule", order_by="BellPeriod.sort_order",
                           cascade="all, delete-orphan")


class BellPeriod(Base):
    __tablename__ = "timetable_bell_periods"
    __table_args__ = (Index("ix_timetable_bell_periods_company", "company_id", "schedule_id"),)
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    schedule_id = Column(Integer, ForeignKey("timetable_bell_schedules.id", ondelete="CASCADE"), nullable=False)
    # Teaching periods carry their number (1, 2, 3 …), which timetable entries refer to; breaks have none.
    no = Column(Integer, nullable=True)
    name = Column(String(60), nullable=False)
    kind = Column(String(20), nullable=False, server_default=text("'class'"))  # class | break | assembly
    start_time = Column(Time, nullable=False)
    end_time = Column(Time, nullable=False)
    sort_order = Column(Integer, nullable=False, server_default=text("1"))
    created_at, updated_at = _stamps()

    schedule = relationship("BellSchedule", back_populates="periods")


class TimetableEntry(Base):
    __tablename__ = "timetable_entries"
    __table_args__ = (Index("ix_timetable_entries_section", "company_id", "section_id", "weekday", "period_no"),
                      Index("ix_timetable_entries_teacher", "company_id", "teacher_id", "weekday"),
                      Index("ix_timetable_entries_room", "company_id", "room_id", "weekday"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    session_id = Column(Integer, ForeignKey("academic_sessions.id"), nullable=False)
    section_id = Column(Integer, ForeignKey("academic_sections.id", ondelete="CASCADE"), nullable=False)
    weekday = Column(Integer, nullable=False)  # ISO 1 = Monday
    period_no = Column(Integer, nullable=False)
    subject_id = Column(Integer, ForeignKey("academic_subjects.id"), nullable=True)
    teacher_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    room_id = Column(Integer, ForeignKey("academic_rooms.id"), nullable=True)
    # Combined class: entries in several sections with the same key are one class.
    group_key = Column(String(40), nullable=True)
    # Electives / split groups taught at the same time in one section ("Language II").
    elective_group = Column(String(60), nullable=True)
    note = Column(String(200), nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()

    section = relationship("Section")
    subject = relationship("Subject")
    teacher = relationship("Employee", foreign_keys=[teacher_id])
    room = relationship("Room")


class TimetablePublication(Base):
    __tablename__ = "timetable_publications"
    __table_args__ = (UniqueConstraint("section_id", name="uq_timetable_publications_section"),
                      Index("ix_timetable_publications_company", "company_id"))
    id = Column(Integer, primary_key=True)
    company_id = _company()
    section_id = Column(Integer, ForeignKey("academic_sections.id", ondelete="CASCADE"), nullable=False)
    version = Column(Integer, nullable=False, server_default=text("0"))
    published_at = Column(DateTime(timezone=True), nullable=True)
    published_by_name = Column(String(200), nullable=True)
    changed = Column(Boolean, nullable=False, server_default=text("false"))
    created_at, updated_at = _stamps()


class Substitution(Base):
    __tablename__ = "timetable_substitutions"
    __table_args__ = (UniqueConstraint("entry_id", "date", name="uq_timetable_substitutions_entry_date"),
                      Index("ix_timetable_substitutions_company", "company_id", "date"),
                      Index("ix_timetable_substitutions_sub", "company_id", "substitute_teacher_id", "date"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    date = Column(Date, nullable=False)
    entry_id = Column(Integer, ForeignKey("timetable_entries.id", ondelete="CASCADE"), nullable=False)
    section_id = Column(Integer, ForeignKey("academic_sections.id"), nullable=False)
    period_no = Column(Integer, nullable=False)
    subject_id = Column(Integer, ForeignKey("academic_subjects.id"), nullable=True)
    absent_teacher_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    substitute_teacher_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    # substitute | cancelled (no class: learners go to the library, study period …)
    kind = Column(String(20), nullable=False, server_default=text("'substitute'"))
    note = Column(String(300), nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'active'"))  # active | cancelled
    created_by_name = Column(String(200), nullable=True)
    created_at, updated_at = _stamps()

    entry = relationship("TimetableEntry")
    substitute = relationship("Employee", foreign_keys=[substitute_teacher_id])
    absent_teacher = relationship("Employee", foreign_keys=[absent_teacher_id])


class TimetableSettings(Base):
    __tablename__ = "timetable_settings"
    __table_args__ = (UniqueConstraint("company_id", name="uq_timetable_settings_company"),)
    id = Column(Integer, primary_key=True)
    company_id = _company()
    config = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at, updated_at = _stamps()


TABLES = ["timetable_bell_schedules", "timetable_bell_periods", "timetable_entries", "timetable_publications",
          "timetable_substitutions", "timetable_settings"]
