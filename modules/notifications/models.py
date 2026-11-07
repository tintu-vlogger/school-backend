from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from core.db import Base


class Notification(Base):
    __tablename__ = "notifications"
    __audit__ = False  # system messages, not business records
    __table_args__ = (
        Index("ix_notifications_company_created", "company_id", text("created_at DESC")),
        Index("ix_notifications_company_employee", "company_id", "employee_id"),
        Index("ix_notifications_company_recipient_unread", "company_id", "employee_id", "read_at"),
        Index("ix_notifications_undispatched", "id", postgresql_where=text("dispatched_at IS NULL")),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)  # addressed to one employee
    channel = Column(String(20), nullable=False)
    message_type = Column(String(50), nullable=False)
    message_content = Column(Text, nullable=False)
    status = Column(String(20), nullable=False)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    # In-app notification centre: a short title, where clicking it goes, who caused it,
    # when the recipient read it, and a grouping key so repeats update one row.
    # "owner" for rows addressed to the company owner (employee_id is then NULL, but the
    # row is not a broadcast to everyone)
    recipient = Column(String(10), nullable=True)
    title = Column(String(200), nullable=True)
    link = Column(String(500), nullable=True)
    actor_name = Column(String(200), nullable=True)
    actor_employee_id = Column(Integer, nullable=True)
    group_key = Column(String(120), nullable=True)
    data = Column(JSONB, nullable=True)
    read_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # Sending through the other channels (email, push): set once the dispatcher has looked
    # at the row, so each notification goes out at most once.
    dispatched_at = Column(DateTime(timezone=True), nullable=True)

    company = relationship("Company", back_populates="notifications")


class NotificationPreference(Base):
    """One person's channels for one category. `recipient_key` is "e:<employee id>" or
    "owner". In-app is always on; email and push follow the company default when there
    is no row."""
    __tablename__ = "notification_preferences"
    __table_args__ = (
        UniqueConstraint("company_id", "recipient_key", "category", name="uq_notification_preferences_key"),
        Index("ix_notification_preferences_company", "company_id", "recipient_key"),
    )

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    recipient_key = Column(String(30), nullable=False)
    category = Column(String(40), nullable=False)
    email = Column(Boolean, nullable=False, server_default=text("true"))
    push = Column(Boolean, nullable=False, server_default=text("false"))
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class NotificationSettings(Base):
    """Company-wide notification settings: default channels per category, digests."""
    __tablename__ = "notification_settings"
    __table_args__ = (UniqueConstraint("company_id", name="uq_notification_settings_company"),)

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    config = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    updated_by_name = Column(String(200), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class NotificationTemplate(Base):
    """A company's own wording for an email (subject and body with {{placeholders}}).
    Without a row the built-in wording is used."""
    __tablename__ = "notification_templates"
    __table_args__ = (UniqueConstraint("company_id", "key", name="uq_notification_templates_company_key"),)

    id = Column(Integer, primary_key=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    key = Column(String(60), nullable=False)
    subject = Column(String(250), nullable=False)
    body = Column(Text, nullable=False)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    updated_by_name = Column(String(200), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class NotificationDelivery(Base):
    """One message sent (or tried) through one channel: the delivery log. Failed emails
    are retried with a growing wait, up to MAX_ATTEMPTS."""
    __tablename__ = "notification_deliveries"
    __audit__ = False  # the log itself; one row per message
    __table_args__ = (
        Index("ix_notification_deliveries_company_created", "company_id", text("created_at DESC")),
        Index("ix_notification_deliveries_retry", "status", "next_attempt_at"),
        Index("ix_notification_deliveries_company_notification", "company_id", "notification_id"),
    )

    id = Column(Integer, primary_key=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    notification_id = Column(Integer, ForeignKey("notifications.id", ondelete="SET NULL"), nullable=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    recipient_key = Column(String(30), nullable=True)
    channel = Column(String(20), nullable=False)          # email | push
    to_address = Column(String(320), nullable=True)
    category = Column(String(40), nullable=True)
    source = Column(String(40), nullable=False, server_default=text("'notification'"))  # notification | digest | automation | test
    subject = Column(String(250), nullable=True)
    body = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'queued'"))  # queued | sent | logged | failed | skipped
    attempts = Column(Integer, nullable=False, server_default=text("0"))
    last_error = Column(String(500), nullable=True)
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class NotificationDigest(Base):
    """A digest email already sent to someone for a day, so each goes out once."""
    __tablename__ = "notification_digests"
    __audit__ = False
    __table_args__ = (
        UniqueConstraint("company_id", "recipient_key", "kind", "digest_date", name="uq_notification_digests_once"),
        Index("ix_notification_digests_company_date", "company_id", "digest_date"),
    )

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    recipient_key = Column(String(30), nullable=False)
    kind = Column(String(20), nullable=False)  # manager | hr
    digest_date = Column(Date, nullable=False)
    items = Column(Integer, nullable=False, server_default=text("0"))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
