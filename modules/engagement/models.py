"""Engagement tables: Buzz feed, celebrations, kudos, announcements, surveys and kiosks.

Every table is company-owned (company_id + RLS). People are referenced by employee id;
a NULL employee id on an authored row means the company owner (who has no employee
record), and `actor_key` ("e<id>" or "owner") keeps one reaction or vote per person.

Survey answers are deliberately NOT audited (`__audit__ = False`): the audit log stores
who made a change, which would reveal anonymous respondents. Participation (who has
answered) is kept in a separate table that never links to the answers.
"""
from sqlalchemy import (Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, LargeBinary, Numeric, String,
                        Text, UniqueConstraint, text)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from core.db import Base


def _uuid():
    return Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))


def _company(name: str):
    return Column(Integer, ForeignKey("companies.id", name=f"fk_{name}_company_id"), nullable=False)


def _emp(name: str, col: str, nullable=True):
    return Column(Integer, ForeignKey("employees.id", name=f"fk_{name}_{col}"), nullable=nullable)


def _stamps():
    return (Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
            Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False))


# ── Settings and per-person preferences ─────────────────────────────

class EngagementSettings(Base):
    """One row per company: Buzz, celebration, kudos and survey preferences (JSONB merged
    over the defaults in settings.py)."""
    __tablename__ = "engagement_settings"
    __table_args__ = (UniqueConstraint("company_id", name="uq_engagement_settings_company"),)

    id = Column(Integer, primary_key=True)
    company_id = _company("engagement_settings")
    config = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at, updated_at = _stamps()


class EngagementProfile(Base):
    """An employee's celebration privacy choices. The date columns are a fallback used only
    until the employee record carries date of birth / joining / wedding itself."""
    __tablename__ = "engagement_profiles"
    __table_args__ = (UniqueConstraint("company_id", "employee_id", name="uq_engagement_profiles_employee"),)

    id = Column(Integer, primary_key=True)
    company_id = _company("engagement_profiles")
    employee_id = _emp("engagement_profiles", "employee_id", nullable=False)
    birth_date = Column(Date, nullable=True)
    joining_date = Column(Date, nullable=True)
    wedding_date = Column(Date, nullable=True)
    hide_birthday = Column(Boolean, nullable=False, server_default=text("false"))
    hide_work_anniversary = Column(Boolean, nullable=False, server_default=text("false"))
    hide_wedding_anniversary = Column(Boolean, nullable=False, server_default=text("true"))
    celebration_emails = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class EngagementFile(Base):
    """Uploaded images and files for posts, announcements and surveys. Served through a
    signed address so <img> tags work without a sign-in header."""
    __tablename__ = "engagement_files"
    __audit__ = False  # binary blobs; the record that uses the file is audited
    __table_args__ = (Index("ix_engagement_files_company_created", "company_id", "created_at"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company("engagement_files")
    uploaded_by_employee_id = _emp("engagement_files", "uploaded_by")
    kind = Column(String(10), nullable=False)  # image | file
    name = Column(String(255), nullable=False)
    mime = Column(String(120), nullable=False)
    size = Column(Integer, nullable=False)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    data = Column(LargeBinary, nullable=False)
    thumb = Column(LargeBinary, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ── Buzz feed ───────────────────────────────────────────────────────

class BuzzPost(Base):
    __tablename__ = "buzz_posts"
    __table_args__ = (
        Index("ix_buzz_posts_company_created", "company_id", text("created_at DESC")),
        Index("ix_buzz_posts_company_kind", "company_id", "kind"),
        Index("ix_buzz_posts_company_pinned", "company_id", "pinned_at"),
        Index("ix_buzz_posts_company_subject", "company_id", "subject_employee_id"),
        # one automatic celebration post per person, kind and day
        Index("uq_buzz_posts_celebration", "company_id", "kind", "subject_employee_id", "celebration_date",
              unique=True, postgresql_where=text("celebration_date IS NOT NULL")),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company("buzz_posts")
    # post | poll | birthday | work_anniversary | wedding_anniversary | welcome | kudos | announcement | survey
    kind = Column(String(30), nullable=False, server_default=text("'post'"))
    author_employee_id = _emp("buzz_posts", "author_employee_id")
    is_system = Column(Boolean, nullable=False, server_default=text("false"))  # posted by the company bot
    body = Column(Text, nullable=True)
    mentions = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))  # employee ids
    hashtags = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))  # lower-case tags
    attachments = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))  # [{uuid, kind, name, size, mime, width, height}]
    link_preview = Column(JSONB, nullable=True)
    poll = Column(JSONB, nullable=True)  # {multiple, closes_at, show_voters}
    audience = Column(JSONB, nullable=False, server_default=text("'{\"all\": true}'::jsonb"))
    theme = Column(String(40), nullable=True)  # celebration card colour theme
    subject_employee_id = _emp("buzz_posts", "subject_employee_id")  # the person celebrated / welcomed
    celebration_date = Column(Date, nullable=True)
    years = Column(Integer, nullable=True)
    kudos_id = Column(Integer, ForeignKey("kudos.id", name="fk_buzz_posts_kudos_id"), nullable=True)
    announcement_id = Column(Integer, ForeignKey("announcements.id", name="fk_buzz_posts_announcement_id"), nullable=True)
    survey_id = Column(Integer, ForeignKey("surveys.id", name="fk_buzz_posts_survey_id"), nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'published'"))  # published | pending | hidden | rejected
    allow_comments = Column(Boolean, nullable=False, server_default=text("true"))
    pinned_at = Column(DateTime(timezone=True), nullable=True)
    pinned_by_name = Column(String(200), nullable=True)
    hidden_reason = Column(String(500), nullable=True)
    hidden_by_name = Column(String(200), nullable=True)
    edited_at = Column(DateTime(timezone=True), nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    workflow_instance_id = Column(Integer, nullable=True)
    created_at, updated_at = _stamps()

    author = relationship("Employee", foreign_keys=[author_employee_id])
    subject = relationship("Employee", foreign_keys=[subject_employee_id])
    poll_options = relationship("BuzzPollOption", order_by="BuzzPollOption.position", cascade="all, delete-orphan")


class BuzzPollOption(Base):
    __tablename__ = "buzz_poll_options"
    __table_args__ = (Index("ix_buzz_poll_options_company_post", "company_id", "post_id"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company("buzz_poll_options")
    post_id = Column(Integer, ForeignKey("buzz_posts.id", name="fk_buzz_poll_options_post_id", ondelete="CASCADE"), nullable=False)
    label = Column(String(200), nullable=False)
    position = Column(Integer, nullable=False, server_default=text("0"))


class BuzzPollVote(Base):
    __tablename__ = "buzz_poll_votes"
    __table_args__ = (
        UniqueConstraint("option_id", "actor_key", name="uq_buzz_poll_votes_option_actor"),
        Index("ix_buzz_poll_votes_company_post", "company_id", "post_id"),
    )

    id = Column(Integer, primary_key=True)
    company_id = _company("buzz_poll_votes")
    post_id = Column(Integer, ForeignKey("buzz_posts.id", name="fk_buzz_poll_votes_post_id", ondelete="CASCADE"), nullable=False)
    option_id = Column(Integer, ForeignKey("buzz_poll_options.id", name="fk_buzz_poll_votes_option_id", ondelete="CASCADE"), nullable=False)
    actor_key = Column(String(20), nullable=False)
    employee_id = _emp("buzz_poll_votes", "employee_id")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class BuzzComment(Base):
    __tablename__ = "buzz_comments"
    __table_args__ = (Index("ix_buzz_comments_company_post", "company_id", "post_id", "created_at"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company("buzz_comments")
    post_id = Column(Integer, ForeignKey("buzz_posts.id", name="fk_buzz_comments_post_id"), nullable=False)
    parent_id = Column(Integer, ForeignKey("buzz_comments.id", name="fk_buzz_comments_parent_id"), nullable=True)
    author_employee_id = _emp("buzz_comments", "author_employee_id")
    body = Column(Text, nullable=False)
    mentions = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    status = Column(String(20), nullable=False, server_default=text("'published'"))  # published | hidden
    edited_at = Column(DateTime(timezone=True), nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    created_at, updated_at = _stamps()

    author = relationship("Employee", foreign_keys=[author_employee_id])


class BuzzReaction(Base):
    """One reaction per person per post or comment; reacting again with another emoji
    switches it. Not audited (high volume, no business meaning)."""
    __tablename__ = "buzz_reactions"
    __audit__ = False
    __table_args__ = (
        Index("uq_buzz_reactions_post_actor", "post_id", "actor_key", unique=True,
              postgresql_where=text("comment_id IS NULL")),
        Index("uq_buzz_reactions_comment_actor", "comment_id", "actor_key", unique=True,
              postgresql_where=text("comment_id IS NOT NULL")),
        Index("ix_buzz_reactions_company_post", "company_id", "post_id"),
    )

    id = Column(Integer, primary_key=True)
    company_id = _company("buzz_reactions")
    post_id = Column(Integer, ForeignKey("buzz_posts.id", name="fk_buzz_reactions_post_id"), nullable=False)
    comment_id = Column(Integer, ForeignKey("buzz_comments.id", name="fk_buzz_reactions_comment_id"), nullable=True)
    actor_key = Column(String(20), nullable=False)
    employee_id = _emp("buzz_reactions", "employee_id")
    emoji = Column(String(16), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class BuzzReport(Base):
    __tablename__ = "buzz_reports"
    __table_args__ = (Index("ix_buzz_reports_company_status", "company_id", "status"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company("buzz_reports")
    post_id = Column(Integer, ForeignKey("buzz_posts.id", name="fk_buzz_reports_post_id"), nullable=False)
    comment_id = Column(Integer, ForeignKey("buzz_comments.id", name="fk_buzz_reports_comment_id"), nullable=True)
    reporter_key = Column(String(20), nullable=False)
    reporter_employee_id = _emp("buzz_reports", "reporter_employee_id")
    reason = Column(String(40), nullable=False)
    note = Column(String(1000), nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'open'"))  # open | actioned | dismissed
    resolved_by_name = Column(String(200), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    created_at, updated_at = _stamps()


class CelebrationEvent(Base):
    """Idempotency record for the daily celebration job: one row per person, kind and day.
    Records whether the feed post and the email went out."""
    __tablename__ = "celebration_events"
    __table_args__ = (
        UniqueConstraint("company_id", "employee_id", "kind", "event_date", name="uq_celebration_events_once"),
        Index("ix_celebration_events_company_date", "company_id", "event_date"),
    )

    id = Column(Integer, primary_key=True)
    company_id = _company("celebration_events")
    employee_id = _emp("celebration_events", "employee_id", nullable=False)
    kind = Column(String(30), nullable=False)  # birthday | work_anniversary | wedding_anniversary | welcome
    event_date = Column(Date, nullable=False)
    years = Column(Integer, nullable=True)
    post_id = Column(Integer, ForeignKey("buzz_posts.id", name="fk_celebration_events_post_id"), nullable=True)
    email_status = Column(String(20), nullable=True)  # sent | logged | skipped | failed
    email_sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ── Kudos ───────────────────────────────────────────────────────────

class KudosValue(Base):
    """A company value / badge people give kudos for."""
    __tablename__ = "kudos_values"
    __table_args__ = (Index("ix_kudos_values_company_position", "company_id", "position"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company("kudos_values")
    name = Column(String(80), nullable=False)
    description = Column(String(300), nullable=True)
    emoji = Column(String(16), nullable=False, server_default=text("'👏'"))
    color = Column(String(20), nullable=False, server_default=text("'amber'"))
    points = Column(Integer, nullable=False, server_default=text("10"))
    position = Column(Integer, nullable=False, server_default=text("0"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class Kudos(Base):
    __tablename__ = "kudos"
    __table_args__ = (Index("ix_kudos_company_created", "company_id", text("created_at DESC")),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company("kudos")
    giver_employee_id = _emp("kudos", "giver_employee_id")
    value_id = Column(Integer, ForeignKey("kudos_values.id", name="fk_kudos_value_id"), nullable=True)
    message = Column(Text, nullable=False)
    points = Column(Integer, nullable=False, server_default=text("0"))
    created_at, updated_at = _stamps()

    value = relationship("KudosValue")
    giver = relationship("Employee", foreign_keys=[giver_employee_id])
    recipients = relationship("KudosRecipient", cascade="all, delete-orphan")


class KudosRecipient(Base):
    __tablename__ = "kudos_recipients"
    __table_args__ = (
        UniqueConstraint("kudos_id", "employee_id", name="uq_kudos_recipients_once"),
        Index("ix_kudos_recipients_company_employee", "company_id", "employee_id"),
    )

    id = Column(Integer, primary_key=True)
    company_id = _company("kudos_recipients")
    kudos_id = Column(Integer, ForeignKey("kudos.id", name="fk_kudos_recipients_kudos_id", ondelete="CASCADE"), nullable=False)
    employee_id = _emp("kudos_recipients", "employee_id", nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    employee = relationship("Employee")


# ── Announcements ───────────────────────────────────────────────────

class Announcement(Base):
    __tablename__ = "announcements"
    __table_args__ = (
        Index("ix_announcements_company_publish", "company_id", "publish_at"),
        Index("ix_announcements_company_status", "company_id", "status"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company("announcements")
    kind = Column(String(20), nullable=False, server_default=text("'announcement'"))  # announcement | news | policy | event
    title = Column(String(200), nullable=False)
    summary = Column(String(400), nullable=True)
    body_html = Column(Text, nullable=False, server_default=text("''"))
    cover = Column(JSONB, nullable=True)  # {"preset": "aurora"} or {"file": uuid}
    audience = Column(JSONB, nullable=False, server_default=text("'{\"all\": true}'::jsonb"))
    status = Column(String(20), nullable=False, server_default=text("'draft'"))  # draft | scheduled | published | archived
    publish_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    requires_ack = Column(Boolean, nullable=False, server_default=text("false"))
    ack_due_on = Column(Date, nullable=True)
    pinned = Column(Boolean, nullable=False, server_default=text("true"))
    show_on_kiosk = Column(Boolean, nullable=False, server_default=text("true"))
    allow_comments = Column(Boolean, nullable=False, server_default=text("true"))
    send_email = Column(Boolean, nullable=False, server_default=text("false"))
    author_employee_id = _emp("announcements", "author_employee_id")
    author_name = Column(String(200), nullable=True)
    notified_at = Column(DateTime(timezone=True), nullable=True)
    created_at, updated_at = _stamps()


class AnnouncementReceipt(Base):
    """Who has read and who has acknowledged an announcement."""
    __tablename__ = "announcement_receipts"
    __table_args__ = (
        UniqueConstraint("announcement_id", "employee_id", name="uq_announcement_receipts_once"),
        Index("ix_announcement_receipts_company_ann", "company_id", "announcement_id"),
    )

    id = Column(Integer, primary_key=True)
    company_id = _company("announcement_receipts")
    announcement_id = Column(Integer, ForeignKey("announcements.id", name="fk_announcement_receipts_announcement_id"), nullable=False)
    employee_id = _emp("announcement_receipts", "employee_id", nullable=False)
    read_at = Column(DateTime(timezone=True), nullable=True)
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    reminded_at = Column(DateTime(timezone=True), nullable=True)
    created_at, updated_at = _stamps()


# ── Surveys ─────────────────────────────────────────────────────────

class Survey(Base):
    __tablename__ = "surveys"
    __table_args__ = (Index("ix_surveys_company_status", "company_id", "status"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company("surveys")
    kind = Column(String(20), nullable=False, server_default=text("'survey'"))  # survey | pulse | enps
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    thank_you = Column(String(500), nullable=True)
    cover = Column(JSONB, nullable=True)
    identity = Column(String(20), nullable=False, server_default=text("'anonymous'"))  # anonymous | optional | named
    min_responses = Column(Integer, nullable=False, server_default=text("5"))
    audience = Column(JSONB, nullable=False, server_default=text("'{\"all\": true}'::jsonb"))
    mandatory = Column(Boolean, nullable=False, server_default=text("false"))
    reminder_every_days = Column(Integer, nullable=True)  # mandatory surveys remind non-respondents
    status = Column(String(20), nullable=False, server_default=text("'draft'"))  # draft | scheduled | active | closed
    opens_at = Column(DateTime(timezone=True), nullable=True)
    duration_days = Column(Integer, nullable=False, server_default=text("7"))
    recurrence = Column(String(20), nullable=False, server_default=text("'none'"))  # none | weekly | monthly | quarterly | half_yearly
    recurrence_count = Column(Integer, nullable=True)  # stop after N runs (NULL = until stopped)
    post_to_buzz = Column(Boolean, nullable=False, server_default=text("true"))
    created_by_employee_id = _emp("surveys", "created_by_employee_id")
    created_by_name = Column(String(200), nullable=True)
    created_at, updated_at = _stamps()

    questions = relationship("SurveyQuestion", order_by="SurveyQuestion.position", cascade="all, delete-orphan")
    runs = relationship("SurveyRun", order_by="SurveyRun.number", cascade="all, delete-orphan")


class SurveyQuestion(Base):
    __tablename__ = "survey_questions"
    __table_args__ = (Index("ix_survey_questions_company_survey", "company_id", "survey_id", "position"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company("survey_questions")
    survey_id = Column(Integer, ForeignKey("surveys.id", name="fk_survey_questions_survey_id", ondelete="CASCADE"), nullable=False)
    position = Column(Integer, nullable=False, server_default=text("0"))
    qtype = Column(String(20), nullable=False)  # single | multi | rating | scale | text | enps
    help_text = Column(String(300), nullable=True)
    required = Column(Boolean, nullable=False, server_default=text("true"))
    options = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))  # choice labels
    settings = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))  # {min, max, low_label, high_label}
    # last on purpose: the attribute name shadows sqlalchemy's text() inside this class body
    text = Column(String(500), nullable=False)


class SurveyRun(Base):
    """One opening of a survey. A one-off survey has one run; recurring surveys get a new
    run each period, which is what the trend compares."""
    __tablename__ = "survey_runs"
    __table_args__ = (
        UniqueConstraint("survey_id", "number", name="uq_survey_runs_number"),
        Index("ix_survey_runs_company_status", "company_id", "status"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company("survey_runs")
    survey_id = Column(Integer, ForeignKey("surveys.id", name="fk_survey_runs_survey_id", ondelete="CASCADE"), nullable=False)
    number = Column(Integer, nullable=False)
    opens_at = Column(DateTime(timezone=True), nullable=False)
    closes_at = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(20), nullable=False, server_default=text("'scheduled'"))  # scheduled | open | closed
    invited_count = Column(Integer, nullable=False, server_default=text("0"))
    created_at, updated_at = _stamps()


class SurveyParticipation(Base):
    """Who was invited and whether they answered. Never linked to the answers."""
    __tablename__ = "survey_participations"
    __audit__ = False
    __table_args__ = (
        UniqueConstraint("run_id", "employee_id", name="uq_survey_participations_once"),
        Index("ix_survey_participations_company_employee", "company_id", "employee_id", "status"),
    )

    id = Column(Integer, primary_key=True)
    company_id = _company("survey_participations")
    run_id = Column(Integer, ForeignKey("survey_runs.id", name="fk_survey_participations_run_id", ondelete="CASCADE"), nullable=False)
    employee_id = _emp("survey_participations", "employee_id", nullable=False)
    status = Column(String(20), nullable=False, server_default=text("'pending'"))  # pending | submitted
    submitted_on = Column(Date, nullable=True)
    reminders_sent = Column(Integer, nullable=False, server_default=text("0"))
    last_reminded_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SurveyResponse(Base):
    """One submitted answer sheet. employee_id is NULL unless the survey is named or the
    person chose to show their name. Only the day is kept, never the exact time."""
    __tablename__ = "survey_responses"
    __audit__ = False
    __table_args__ = (Index("ix_survey_responses_company_run", "company_id", "run_id"),)

    id = Column(Integer, primary_key=True)
    company_id = _company("survey_responses")
    run_id = Column(Integer, ForeignKey("survey_runs.id", name="fk_survey_responses_run_id", ondelete="CASCADE"), nullable=False)
    employee_id = _emp("survey_responses", "employee_id")
    department_id = Column(Integer, nullable=True)  # snapshot for group results (shown only above the threshold)
    submitted_on = Column(Date, nullable=False)

    answers = relationship("SurveyAnswer", cascade="all, delete-orphan")


class SurveyAnswer(Base):
    __tablename__ = "survey_answers"
    __audit__ = False
    __table_args__ = (Index("ix_survey_answers_company_question", "company_id", "question_id"),)

    id = Column(Integer, primary_key=True)
    company_id = _company("survey_answers")
    response_id = Column(Integer, ForeignKey("survey_responses.id", name="fk_survey_answers_response_id", ondelete="CASCADE"), nullable=False)
    question_id = Column(Integer, ForeignKey("survey_questions.id", name="fk_survey_answers_question_id", ondelete="CASCADE"), nullable=False)
    value_number = Column(Numeric(6, 2), nullable=True)
    value_text = Column(Text, nullable=True)
    value_choices = Column(JSONB, nullable=True)


# ── Kiosks ──────────────────────────────────────────────────────────

class Kiosk(Base):
    """An office TV. Its secret link shows a full-screen slideshow without signing in."""
    __tablename__ = "kiosks"
    __table_args__ = (
        Index("ix_kiosks_company", "company_id"),
        Index("uq_kiosks_token", "token", unique=True),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company("kiosks")
    name = Column(String(120), nullable=False)
    token = Column(String(64), nullable=False)
    slides = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))  # [{type, enabled}]
    seconds_per_slide = Column(Integer, nullable=False, server_default=text("10"))
    audience = Column(JSONB, nullable=False, server_default=text("'{\"all\": true}'::jsonb"))
    theme = Column(String(20), nullable=False, server_default=text("'dark'"))
    welcome_text = Column(String(200), nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()
