"""Single sign-on with Google and Microsoft.

sso_providers         platform level (no company_id): the OAuth app of each provider, kept by
                      the platform team; the client secret is stored encrypted
sso_company_settings  company level (RLS): which providers the company allows and, optionally,
                      the email domains people must sign in with
"""
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from core.db import Base


class SsoProvider(Base):
    __tablename__ = "sso_providers"

    id = Column(Integer, primary_key=True)
    provider = Column(String(20), nullable=False, unique=True)     # google | microsoft
    enabled = Column(Boolean, nullable=False, server_default=text("false"))
    client_id = Column(String(255), nullable=True)
    client_secret_encrypted = Column(Text, nullable=True)
    # Microsoft only: which accounts may sign in -- organizations (work or school, any tenant),
    # common (also personal accounts) or one tenant id
    tenant = Column(String(80), nullable=True)
    updated_by = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class SsoCompanySetting(Base):
    __tablename__ = "sso_company_settings"
    __table_args__ = (Index("ix_sso_company_settings_company", "company_id", unique=True),)

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    google_enabled = Column(Boolean, nullable=False, server_default=text("false"))
    microsoft_enabled = Column(Boolean, nullable=False, server_default=text("false"))
    allowed_domains = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
