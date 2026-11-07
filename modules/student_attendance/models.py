"""Learner attendance.

- AttendanceStatus     the marks a register can carry (P, A, late, half day, leave, medical,
                       on duty …) with how much each counts as present; seeded, editable
- AttendanceRegister   one register: a section on a date for one slot: the whole day, the
                       morning or afternoon session, or one timetable period (period mode)
- AttendanceMark       one learner's mark in a register (late minutes, remarks)
- StudentLeaveRequest  leave asked for by a parent or learner (or recorded by staff); approved
                       through the workflow engine; approved leave pre-fills registers
- CalendarDay          the institution's own calendar: extra holidays and extra working days,
                       for everyone or some programs (on top of week days and staff holidays)
- AttendanceCorrection a change to a locked register (who, when, old and new mark, why)
- AttendanceAlert      absence and low-attendance alerts already sent (so each goes once)
- AttendanceSettings   per institution settings (lock days, sessions, alerts, thresholds)
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


class AttendanceStatus(Base):
    __tablename__ = "student_attendance_statuses"
    __table_args__ = (UniqueConstraint("company_id", "code", name="uq_student_attendance_statuses_code"),
                      Index("ix_student_attendance_statuses_company", "company_id", "sort_order"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    code = Column(String(10), nullable=False)
    name = Column(String(60), nullable=False)
    # How much of a present day this mark is worth: 1 present, 0.5 half day, 0 absent.
    counts_as = Column(Numeric(3, 2), nullable=False, server_default=text("1"))
    # Whether the slot counts at all in the percentage (false = left out of the total).
    in_total = Column(Boolean, nullable=False, server_default=text("true"))
    is_absent = Column(Boolean, nullable=False, server_default=text("false"))  # sends absence alerts
    is_late = Column(Boolean, nullable=False, server_default=text("false"))  # asks for late minutes
    is_leave = Column(Boolean, nullable=False, server_default=text("false"))  # used for approved leave
    color = Column(String(20), nullable=True)
    sort_order = Column(Integer, nullable=False, server_default=text("1"))
    is_system = Column(Boolean, nullable=False, server_default=text("false"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class AttendanceRegister(Base):
    __tablename__ = "student_attendance_registers"
    __table_args__ = (UniqueConstraint("section_id", "date", "slot", "period_no", "subject_key",
                                       name="uq_student_attendance_registers_slot"),
                      Index("ix_student_attendance_registers_date", "company_id", "date"),
                      Index("ix_student_attendance_registers_section", "company_id", "section_id", "date"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    section_id = Column(Integer, ForeignKey("academic_sections.id"), nullable=False)
    date = Column(Date, nullable=False)
    slot = Column(String(10), nullable=False, server_default=text("'day'"))  # day | am | pm | period
    period_no = Column(Integer, nullable=False, server_default=text("0"))
    subject_id = Column(Integer, ForeignKey("academic_subjects.id"), nullable=True)
    subject_key = Column(Integer, nullable=False, server_default=text("0"))  # subject_id or 0 (for the unique key)
    timetable_entry_id = Column(Integer, nullable=True)
    marked_by_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    marked_by_name = Column(String(200), nullable=True)
    marked_at = Column(DateTime(timezone=True), nullable=True)
    updated_by_name = Column(String(200), nullable=True)
    total = Column(Integer, nullable=False, server_default=text("0"))
    present = Column(Integer, nullable=False, server_default=text("0"))
    absent = Column(Integer, nullable=False, server_default=text("0"))
    created_at, updated_at = _stamps()

    section = relationship("Section")
    subject = relationship("Subject")
    marks = relationship("AttendanceMark", back_populates="register", cascade="all, delete-orphan")


class AttendanceMark(Base):
    __tablename__ = "student_attendance_marks"
    __table_args__ = (UniqueConstraint("register_id", "student_id", name="uq_student_attendance_marks"),
                      Index("ix_student_attendance_marks_student", "company_id", "student_id", "date"),
                      Index("ix_student_attendance_marks_date", "company_id", "date", "section_id"))
    id = Column(Integer, primary_key=True)
    company_id = _company()
    register_id = Column(Integer, ForeignKey("student_attendance_registers.id", ondelete="CASCADE"), nullable=False)
    student_id = Column(Integer, ForeignKey("students.id", ondelete="CASCADE"), nullable=False)
    date = Column(Date, nullable=False)
    section_id = Column(Integer, nullable=False)
    subject_id = Column(Integer, nullable=True)
    status_id = Column(Integer, ForeignKey("student_attendance_statuses.id"), nullable=False)
    code = Column(String(10), nullable=False)
    late_minutes = Column(Integer, nullable=True)
    remarks = Column(String(300), nullable=True)
    source = Column(String(12), nullable=False, server_default=text("'manual'"))  # manual | leave | correction
    leave_request_id = Column(Integer, nullable=True)
    created_at, updated_at = _stamps()

    register = relationship("AttendanceRegister", back_populates="marks")
    status = relationship("AttendanceStatus")


class StudentLeaveRequest(Base):
    __tablename__ = "student_leave_requests"
    __table_args__ = (Index("ix_student_leave_requests_student", "company_id", "student_id", "from_date"),
                      Index("ix_student_leave_requests_status", "company_id", "status", "from_date"),
                      Index("ix_student_leave_requests_section", "company_id", "section_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    student_id = Column(Integer, ForeignKey("students.id", ondelete="CASCADE"), nullable=False)
    section_id = Column(Integer, ForeignKey("academic_sections.id"), nullable=True)
    from_date = Column(Date, nullable=False)
    to_date = Column(Date, nullable=False)
    days = Column(Integer, nullable=False, server_default=text("1"))
    # Status code put on the register for approved days (a status with is_leave, e.g. EX or ML).
    status_code = Column(String(10), nullable=False, server_default=text("'EX'"))
    reason = Column(Text, nullable=False)
    attachments = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    status = Column(String(15), nullable=False, server_default=text("'pending'"))  # pending | approved | rejected | cancelled
    requested_by_kind = Column(String(10), nullable=False, server_default=text("'parent'"))  # parent | student | staff
    requested_by_portal_id = Column(Integer, nullable=True)
    requested_by_name = Column(String(200), nullable=True)
    workflow_instance_id = Column(Integer, nullable=True)
    decided_by_name = Column(String(200), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decision_note = Column(Text, nullable=True)
    created_at, updated_at = _stamps()

    student = relationship("Student")
    section = relationship("Section")


class CalendarDay(Base):
    __tablename__ = "student_calendar_days"
    __table_args__ = (Index("ix_student_calendar_days_company", "company_id", "date"),)
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    date = Column(Date, nullable=False)
    kind = Column(String(10), nullable=False, server_default=text("'holiday'"))  # holiday | working
    name = Column(String(150), nullable=False)
    program_ids = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))  # empty = everyone
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class AttendanceCorrection(Base):
    __tablename__ = "student_attendance_corrections"
    __table_args__ = (Index("ix_student_attendance_corrections_company", "company_id", "date"),)
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    register_id = Column(Integer, ForeignKey("student_attendance_registers.id", ondelete="CASCADE"), nullable=False)
    student_id = Column(Integer, ForeignKey("students.id", ondelete="CASCADE"), nullable=False)
    date = Column(Date, nullable=False)
    old_code = Column(String(10), nullable=True)
    new_code = Column(String(10), nullable=False)
    reason = Column(Text, nullable=False)
    by_employee_id = Column(Integer, nullable=True)
    by_name = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    student = relationship("Student")
    register = relationship("AttendanceRegister")


class AttendanceAlert(Base):
    __tablename__ = "student_attendance_alerts"
    __audit__ = False  # a send log, not business data
    __table_args__ = (UniqueConstraint("company_id", "kind", "student_id", "ref", name="uq_student_attendance_alerts"),
                      Index("ix_student_attendance_alerts_company", "company_id", "kind", "ref"))
    id = Column(Integer, primary_key=True)
    company_id = _company()
    kind = Column(String(12), nullable=False)  # absence | late | low
    student_id = Column(Integer, ForeignKey("students.id", ondelete="CASCADE"), nullable=False)
    ref = Column(String(20), nullable=False)  # the date, or the week for low-attendance alerts
    recipients = Column(Integer, nullable=False, server_default=text("0"))
    sent_at = Column(DateTime(timezone=True), server_default=func.now())


class AttendanceSettings(Base):
    __tablename__ = "student_attendance_settings"
    __table_args__ = (UniqueConstraint("company_id", name="uq_student_attendance_settings_company"),)
    id = Column(Integer, primary_key=True)
    company_id = _company()
    config = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at, updated_at = _stamps()


TABLES = ["student_attendance_statuses", "student_attendance_registers", "student_attendance_marks",
          "student_leave_requests", "student_calendar_days", "student_attendance_corrections",
          "student_attendance_alerts", "student_attendance_settings"]
