"""Integrations (AI model, email, accounting, sign-in …), personal access tokens for MCP
clients, and assistant conversations."""
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from core.db import Base


def _uuid():
    return Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))


class IntegrationSetting(Base):
    """One connection per kind per company. Secrets (keys, passwords) are encrypted in
    `secrets_encrypted` and never returned by the API; only their last 4 characters are."""
    __tablename__ = "integration_settings"
    __table_args__ = (Index("uq_integration_settings_company_kind", "company_id", "kind", unique=True),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    kind = Column(String(30), nullable=False)        # ai_model | smtp | accounting | sms | whatsapp | google | microsoft | job_boards
    enabled = Column(Boolean, nullable=False, server_default=text("false"))
    config = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    secrets_encrypted = Column(Text, nullable=True)
    secret_hints = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))   # {field: "…abcd"}
    status = Column(String(20), nullable=False, server_default=text("'not_set_up'"))  # not_set_up | saved | connected | error
    last_tested_at = Column(DateTime(timezone=True), nullable=True)
    last_message = Column(String(300), nullable=True)
    updated_by_name = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class AccessToken(Base):
    """A personal access token for MCP clients. Only a hash is stored; the token is shown
    once when it is made. It acts as its owner, limited to the tools chosen for it."""
    __tablename__ = "access_tokens"
    __table_args__ = (
        Index("ix_access_tokens_company_employee", "company_id", "employee_id"),
        Index("uq_access_tokens_hash", "token_hash", unique=True),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)    # NULL: company owner
    owner_name = Column(String(200), nullable=True)
    name = Column(String(100), nullable=False)
    token_hash = Column(String(64), nullable=False)
    prefix = Column(String(16), nullable=False)
    scopes = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))   # tool names; ["*"] = all
    expires_at = Column(DateTime(timezone=True), nullable=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    use_count = Column(Integer, nullable=False, server_default=text("0"))
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AssistantThread(Base):
    """A conversation with the data assistant (kept for the person; not audited row by row,
    every tool call is written as an audit event instead)."""
    __tablename__ = "assistant_threads"
    __audit__ = False
    __table_args__ = (Index("ix_assistant_threads_company_employee", "company_id", "employee_id", "updated_at"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    title = Column(String(200), nullable=False)
    messages = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
