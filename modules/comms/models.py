"""Communication between the institution, its staff, parents and students.

- CommsSettings     per-institution settings (office hours, who may message, default channels)
- Notice            a notice or circular with an audience, channels, schedule, expiry and pin
- NoticeRecipient   one person a notice went to: staff member, guardian or learner, with
                    read and acknowledgement times (read receipts)
- Thread / Message  private conversations between one staff member (or the office) and one
                    parent or learner; every message is kept (safeguarding)
- Broadcast         a teacher's one-to-many message to the parents of a section; each family
                    gets its own thread so replies stay private
- Event / EventRsvp the institution calendar with audience and RSVP
- PtmDay, PtmTeacher, PtmSlot, PtmBooking   parent-teacher meetings: a day for some sections,
                    teachers with slots, parent bookings, notes and attendance
- DiaryEntry        a class teacher's short daily note for a section

Audiences (notices, events) are stored as JSON:
    {"everyone": bool,
     "staff": {"scope": "none" | "all" | "filter", "department_ids": [], "roles": [], "employee_ids": []},
     "learners": {"parents": bool, "students": bool,
                  "scope": "all" | "programs" | "sections" | "students",
                  "program_ids": [], "section_ids": [], "student_ids": []}}
"""
from sqlalchemy import (Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, String, Text, Time,
                        UniqueConstraint, text)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from core.db import Base


def _uuid():
    return Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))


def _company():
    return Column(Integer, ForeignKey("companies.id"), nullable=False)


def _created():
    return Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated():
    return Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


def _json(default: str = "'{}'::jsonb"):
    return Column(JSONB, nullable=False, server_default=text(default))


class CommsSettings(Base):
    __tablename__ = "comms_settings"
    __table_args__ = (UniqueConstraint("company_id", name="uq_comms_settings_company"),)

    id = Column(Integer, primary_key=True)
    company_id = _company()
    config = _json()
    updated_at = _updated()


# ── Notices ──────────────────────────────────────────────────────────

class Notice(Base):
    __tablename__ = "comms_notices"
    __table_args__ = (Index("ix_comms_notices_company_status", "company_id", "status", "published_at"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    title = Column(String(200), nullable=False)
    body = Column(Text, nullable=False, server_default=text("''"))  # markdown
    # academic | event | holiday | fee | exam | general | emergency
    category = Column(String(20), nullable=False, server_default=text("'general'"))
    attachments = _json("'[]'::jsonb")
    audience = _json()
    audience_label = Column(String(300), nullable=True)
    # in_app is always on; email | sms | whatsapp optional
    channels = _json("'[\"in_app\"]'::jsonb")
    # draft | scheduled | published | archived
    status = Column(String(20), nullable=False, server_default=text("'draft'"))
    publish_at = Column(DateTime(timezone=True), nullable=True)   # scheduled time
    published_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    pinned = Column(Boolean, nullable=False, server_default=text("false"))
    require_ack = Column(Boolean, nullable=False, server_default=text("false"))
    author_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    author_name = Column(String(200), nullable=True)
    recipient_count = Column(Integer, nullable=False, server_default=text("0"))
    delivery = _json()  # {"email": {"sent": n, "skipped": n}, "sms": {...}, "whatsapp": {...}}
    archived_at = Column(DateTime(timezone=True), nullable=True)
    created_at = _created()
    updated_at = _updated()


class NoticeRecipient(Base):
    """One person a notice went to. `recipient_key` is "e:<employee id>", "g:<guardian id>" or
    "s:<student id>"; `via_student_ids` lists the learners that put a guardian in the audience."""
    __tablename__ = "comms_notice_recipients"
    __audit__ = False  # one row per person per notice; reads and sends are noise in the audit log
    __table_args__ = (UniqueConstraint("notice_id", "recipient_key", name="uq_comms_notice_recipient"),
                      Index("ix_comms_notice_recipients_company_notice", "company_id", "notice_id"),
                      Index("ix_comms_notice_recipients_employee", "company_id", "employee_id"),
                      Index("ix_comms_notice_recipients_guardian", "company_id", "guardian_id"),
                      Index("ix_comms_notice_recipients_student", "company_id", "student_id"))

    id = Column(Integer, primary_key=True)
    company_id = _company()
    notice_id = Column(Integer, ForeignKey("comms_notices.id", ondelete="CASCADE"), nullable=False)
    recipient_key = Column(String(30), nullable=False)
    kind = Column(String(10), nullable=False)  # staff | parent | student
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    guardian_id = Column(Integer, ForeignKey("guardians.id", ondelete="CASCADE"), nullable=True)
    student_id = Column(Integer, ForeignKey("students.id", ondelete="CASCADE"), nullable=True)
    portal_account_id = Column(Integer, ForeignKey("portal_accounts.id", ondelete="SET NULL"), nullable=True)
    via_student_ids = _json("'[]'::jsonb")
    name = Column(String(200), nullable=True)
    read_at = Column(DateTime(timezone=True), nullable=True)
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    channels_sent = _json("'[]'::jsonb")
    send_count = Column(Integer, nullable=False, server_default=text("1"))
    last_sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = _created()

    notice = relationship("Notice")


# ── Messages ─────────────────────────────────────────────────────────

class Broadcast(Base):
    __tablename__ = "comms_broadcasts"
    __table_args__ = (Index("ix_comms_broadcasts_company", "company_id", "created_at"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    section_id = Column(Integer, ForeignKey("academic_sections.id"), nullable=False)
    sender_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    sender_name = Column(String(200), nullable=True)
    subject = Column(String(200), nullable=False)
    body = Column(Text, nullable=False)
    attachments = _json("'[]'::jsonb")
    recipient_count = Column(Integer, nullable=False, server_default=text("0"))
    created_at = _created()


class Thread(Base):
    """A private conversation between one staff member (or the office, when
    `staff_employee_id` is empty and `to_office` is set) and one parent or learner."""
    __tablename__ = "comms_threads"
    __table_args__ = (Index("ix_comms_threads_company_staff", "company_id", "staff_employee_id", "last_message_at"),
                      Index("ix_comms_threads_company_guardian", "company_id", "guardian_id"),
                      Index("ix_comms_threads_company_student", "company_id", "student_id"),
                      Index("ix_comms_threads_company_last", "company_id", "last_message_at"))

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    subject = Column(String(200), nullable=False)
    staff_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    to_office = Column(Boolean, nullable=False, server_default=text("false"))
    party_kind = Column(String(10), nullable=False)  # parent | student
    guardian_id = Column(Integer, ForeignKey("guardians.id", ondelete="CASCADE"), nullable=True)
    student_id = Column(Integer, ForeignKey("students.id", ondelete="CASCADE"), nullable=True)  # the learner party
    about_student_id = Column(Integer, ForeignKey("students.id", ondelete="CASCADE"), nullable=True)
    section_id = Column(Integer, ForeignKey("academic_sections.id"), nullable=True)
    broadcast_id = Column(Integer, ForeignKey("comms_broadcasts.id"), nullable=True)
    status = Column(String(10), nullable=False, server_default=text("'open'"))  # open | closed
    closed_at = Column(DateTime(timezone=True), nullable=True)
    closed_by = Column(String(200), nullable=True)
    started_by = Column(String(10), nullable=False, server_default=text("'staff'"))  # staff | parent | student
    staff_unread = Column(Integer, nullable=False, server_default=text("0"))
    party_unread = Column(Integer, nullable=False, server_default=text("0"))
    message_count = Column(Integer, nullable=False, server_default=text("0"))
    last_message_at = Column(DateTime(timezone=True), nullable=True)
    last_message_preview = Column(String(200), nullable=True)
    created_at = _created()
    updated_at = _updated()

    staff = relationship("Employee", foreign_keys=[staff_employee_id])
    guardian = relationship("Guardian", foreign_keys=[guardian_id])
    student = relationship("Student", foreign_keys=[student_id])
    about_student = relationship("Student", foreign_keys=[about_student_id])


class Message(Base):
    """One message in a thread. Never edited or deleted (kept for safeguarding)."""
    __tablename__ = "comms_messages"
    __table_args__ = (Index("ix_comms_messages_company_thread", "company_id", "thread_id", "created_at"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    thread_id = Column(Integer, ForeignKey("comms_threads.id", ondelete="CASCADE"), nullable=False)
    sender_kind = Column(String(10), nullable=False)  # staff | parent | student | system
    sender_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    sender_portal_id = Column(Integer, ForeignKey("portal_accounts.id", ondelete="SET NULL"), nullable=True)
    sender_name = Column(String(200), nullable=True)
    body = Column(Text, nullable=False)
    attachments = _json("'[]'::jsonb")
    broadcast_id = Column(Integer, ForeignKey("comms_broadcasts.id"), nullable=True)
    created_at = _created()


# ── Events calendar ──────────────────────────────────────────────────

class Event(Base):
    __tablename__ = "comms_events"
    __table_args__ = (Index("ix_comms_events_company_dates", "company_id", "start_date", "end_date"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    # event | holiday | exam | ptm | sports | trip | meeting | cultural | other
    category = Column(String(20), nullable=False, server_default=text("'event'"))
    all_day = Column(Boolean, nullable=False, server_default=text("true"))
    # Calendar dates in the institution's timezone (always set) ...
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    # ... and exact times in UTC for timed events.
    start_at = Column(DateTime(timezone=True), nullable=True)
    end_at = Column(DateTime(timezone=True), nullable=True)
    location = Column(String(200), nullable=True)
    audience = _json()
    audience_label = Column(String(300), nullable=True)
    rsvp_required = Column(Boolean, nullable=False, server_default=text("false"))
    rsvp_deadline = Column(Date, nullable=True)
    status = Column(String(12), nullable=False, server_default=text("'active'"))  # active | cancelled
    ptm_day_id = Column(Integer, ForeignKey("comms_ptm_days.id", ondelete="SET NULL"), nullable=True)
    created_by = Column(String(200), nullable=True)
    archived_at = Column(DateTime(timezone=True), nullable=True)
    created_at = _created()
    updated_at = _updated()


class EventRsvp(Base):
    __tablename__ = "comms_event_rsvps"
    __table_args__ = (UniqueConstraint("event_id", "responder_key", name="uq_comms_event_rsvp"),
                      Index("ix_comms_event_rsvps_company_event", "company_id", "event_id"))

    id = Column(Integer, primary_key=True)
    company_id = _company()
    event_id = Column(Integer, ForeignKey("comms_events.id", ondelete="CASCADE"), nullable=False)
    responder_key = Column(String(30), nullable=False)  # e:<employee> | g:<guardian> | s:<student>
    name = Column(String(200), nullable=True)
    response = Column(String(10), nullable=False)  # yes | no | maybe
    guests = Column(Integer, nullable=False, server_default=text("0"))
    note = Column(String(300), nullable=True)
    responded_at = _updated()


# ── Parent-teacher meetings ──────────────────────────────────────────

class PtmDay(Base):
    __tablename__ = "comms_ptm_days"
    __table_args__ = (Index("ix_comms_ptm_days_company_date", "company_id", "date"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    title = Column(String(200), nullable=False)
    date = Column(Date, nullable=False)
    start_time = Column(Time, nullable=False)
    end_time = Column(Time, nullable=False)
    slot_minutes = Column(Integer, nullable=False, server_default=text("10"))
    gap_minutes = Column(Integer, nullable=False, server_default=text("0"))
    breaks = _json("'[]'::jsonb")  # [{"start": "11:00", "end": "11:15", "label": "Tea"}]
    mode = Column(String(12), nullable=False, server_default=text("'in_person'"))  # in_person | online | both
    location = Column(String(200), nullable=True)
    meeting_link = Column(String(500), nullable=True)
    section_ids = _json("'[]'::jsonb")
    instructions = Column(Text, nullable=True)
    booking_closes_at = Column(DateTime(timezone=True), nullable=True)
    notes_visible_default = Column(Boolean, nullable=False, server_default=text("false"))
    status = Column(String(12), nullable=False, server_default=text("'draft'"))  # draft | open | closed | completed
    created_by = Column(String(200), nullable=True)
    archived_at = Column(DateTime(timezone=True), nullable=True)
    created_at = _created()
    updated_at = _updated()

    teachers = relationship("PtmTeacher", back_populates="day", order_by="PtmTeacher.id")


class PtmTeacher(Base):
    __tablename__ = "comms_ptm_teachers"
    __table_args__ = (UniqueConstraint("ptm_day_id", "employee_id", name="uq_comms_ptm_teacher"),
                      Index("ix_comms_ptm_teachers_company_day", "company_id", "ptm_day_id"))

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    ptm_day_id = Column(Integer, ForeignKey("comms_ptm_days.id", ondelete="CASCADE"), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    location = Column(String(200), nullable=True)
    meeting_link = Column(String(500), nullable=True)
    start_time = Column(Time, nullable=True)  # overrides the day's times
    end_time = Column(Time, nullable=True)
    created_at = _created()

    day = relationship("PtmDay", back_populates="teachers")
    employee = relationship("Employee")


class PtmSlot(Base):
    __tablename__ = "comms_ptm_slots"
    __table_args__ = (UniqueConstraint("ptm_teacher_id", "start_at", name="uq_comms_ptm_slot"),
                      Index("ix_comms_ptm_slots_company_day", "company_id", "ptm_day_id", "start_at"))

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    ptm_day_id = Column(Integer, ForeignKey("comms_ptm_days.id", ondelete="CASCADE"), nullable=False)
    ptm_teacher_id = Column(Integer, ForeignKey("comms_ptm_teachers.id", ondelete="CASCADE"), nullable=False)
    start_at = Column(DateTime(timezone=True), nullable=False)
    end_at = Column(DateTime(timezone=True), nullable=False)
    blocked = Column(Boolean, nullable=False, server_default=text("false"))
    created_at = _created()

    teacher = relationship("PtmTeacher")


class PtmBooking(Base):
    """A family's booking of one slot. Only one live booking per slot, and one per learner per
    teacher per day (partial unique indexes, so cancelled bookings don't count)."""
    __tablename__ = "comms_ptm_bookings"
    __table_args__ = (
        Index("uq_comms_ptm_booking_slot", "slot_id", unique=True, postgresql_where=text("status <> 'cancelled'")),
        Index("uq_comms_ptm_booking_student_teacher", "ptm_teacher_id", "student_id", unique=True,
              postgresql_where=text("status <> 'cancelled'")),
        Index("ix_comms_ptm_bookings_company_day", "company_id", "ptm_day_id"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    ptm_day_id = Column(Integer, ForeignKey("comms_ptm_days.id", ondelete="CASCADE"), nullable=False)
    ptm_teacher_id = Column(Integer, ForeignKey("comms_ptm_teachers.id", ondelete="CASCADE"), nullable=False)
    slot_id = Column(Integer, ForeignKey("comms_ptm_slots.id", ondelete="CASCADE"), nullable=False)
    student_id = Column(Integer, ForeignKey("students.id", ondelete="CASCADE"), nullable=False)
    guardian_id = Column(Integer, ForeignKey("guardians.id", ondelete="SET NULL"), nullable=True)
    booked_by = Column(String(200), nullable=True)
    mode = Column(String(12), nullable=False, server_default=text("'in_person'"))  # in_person | online
    # booked | met | no_show | cancelled
    status = Column(String(12), nullable=False, server_default=text("'booked'"))
    parent_note = Column(String(500), nullable=True)
    notes = Column(Text, nullable=True)
    action_points = _json("'[]'::jsonb")  # [{"text": "...", "done": false}]
    notes_visible = Column(Boolean, nullable=False, server_default=text("false"))
    reminder_sent_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = _created()
    updated_at = _updated()

    slot = relationship("PtmSlot")
    teacher = relationship("PtmTeacher")
    student = relationship("Student")
    guardian = relationship("Guardian")


# ── Daily diary ──────────────────────────────────────────────────────

class DiaryEntry(Base):
    __tablename__ = "comms_diary_entries"
    __table_args__ = (UniqueConstraint("section_id", "date", name="uq_comms_diary_section_date"),
                      Index("ix_comms_diary_company_section", "company_id", "section_id", "date"))

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    section_id = Column(Integer, ForeignKey("academic_sections.id"), nullable=False)
    date = Column(Date, nullable=False)
    body = Column(Text, nullable=False)
    attachments = _json("'[]'::jsonb")
    author_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    author_name = Column(String(200), nullable=True)
    created_at = _created()
    updated_at = _updated()


TABLES = ["comms_settings", "comms_notices", "comms_notice_recipients", "comms_broadcasts", "comms_threads",
          "comms_messages", "comms_ptm_days", "comms_events", "comms_event_rsvps", "comms_ptm_teachers",
          "comms_ptm_slots", "comms_ptm_bookings", "comms_diary_entries"]
