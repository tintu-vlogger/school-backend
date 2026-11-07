"""Automation: "when this happens, do that" rules, their run log, and outgoing webhooks
with a delivery log."""
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from core.db import Base


def _uuid():
    return Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))


def _company():
    return Column(Integer, ForeignKey("companies.id"), nullable=False)


class AutomationRule(Base):
    """One rule: a record type, a trigger, an optional condition and a list of actions."""
    __tablename__ = "automation_rules"
    __table_args__ = (Index("ix_automation_rules_company_entity", "company_id", "entity", "trigger", "active"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(150), nullable=False)
    description = Column(Text, nullable=True)
    entity = Column(String(40), nullable=False)          # employee | department | employee_document | helpdesk_case | candidate | request
    trigger = Column(String(20), nullable=False)         # created | updated | field_changed | approved | rejected | date
    fields = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))  # for field_changed
    request_type = Column(String(80), nullable=True)     # for approved / rejected: one kind of request, or any
    date_field = Column(String(60), nullable=True)       # for date rules
    offset_days = Column(Integer, nullable=False, server_default=text("0"))  # negative = before the date
    repeat_yearly = Column(Boolean, nullable=False, server_default=text("false"))  # birthdays, anniversaries
    condition = Column(Text, nullable=True)
    actions = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    run_count = Column(Integer, nullable=False, server_default=text("0"))
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    archived_at = Column(DateTime(timezone=True), nullable=True)
    created_by_name = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class AutomationRun(Base):
    """The run log: one row each time a rule's trigger fired, with the condition result and
    what each action did. `occurrence_key` makes date rules run once per record and date."""
    __tablename__ = "automation_runs"
    __audit__ = False
    __table_args__ = (
        Index("ix_automation_runs_company_created", "company_id", text("created_at DESC")),
        Index("ix_automation_runs_rule", "company_id", "rule_id", text("created_at DESC")),
        Index("uq_automation_runs_occurrence", "rule_id", "occurrence_key", unique=True,
              postgresql_where=text("occurrence_key IS NOT NULL")),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    rule_id = Column(Integer, ForeignKey("automation_rules.id"), nullable=False)
    trigger = Column(String(20), nullable=False)
    entity = Column(String(40), nullable=False)
    record_id = Column(Integer, nullable=True)
    record_label = Column(String(250), nullable=True)
    occurrence_key = Column(String(80), nullable=True)
    matched = Column(Boolean, nullable=False, server_default=text("true"))
    status = Column(String(20), nullable=False)  # done | partial | failed | skipped
    results = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    error = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AutomationWebhook(Base):
    """An address that receives signed JSON when a rule calls it."""
    __tablename__ = "automation_webhooks"
    __table_args__ = (Index("ix_automation_webhooks_company", "company_id", "active"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(100), nullable=False)
    url = Column(String(500), nullable=False)
    secret = Column(String(200), nullable=False)
    headers = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    archived_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class AutomationWebhookDelivery(Base):
    """One call to a webhook (and its retries)."""
    __tablename__ = "automation_webhook_deliveries"
    __audit__ = False
    __table_args__ = (
        Index("ix_automation_webhook_deliveries_company", "company_id", "webhook_id", text("created_at DESC")),
        Index("ix_automation_webhook_deliveries_retry", "status", "next_attempt_at"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    webhook_id = Column(Integer, ForeignKey("automation_webhooks.id"), nullable=False)
    rule_id = Column(Integer, ForeignKey("automation_rules.id"), nullable=True)
    event = Column(String(80), nullable=False)
    payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status = Column(String(20), nullable=False, server_default=text("'queued'"))  # queued | sent | failed
    attempts = Column(Integer, nullable=False, server_default=text("0"))
    response_status = Column(Integer, nullable=True)
    response_body = Column(String(500), nullable=True)
    error = Column(String(500), nullable=True)
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
