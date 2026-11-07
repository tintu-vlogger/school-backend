"""Google and Microsoft sign-in: platform OAuth apps and per-company switches.

Revision ID: ss1a2b3c4d5e
Revises: ig1a2b3c4d5e
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision = "ss1a2b3c4d5e"
down_revision = "ig1a2b3c4d5e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sso_providers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(20), nullable=False, unique=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("client_id", sa.String(255), nullable=True),
        sa.Column("client_secret_encrypted", sa.Text(), nullable=True),
        sa.Column("tenant", sa.String(80), nullable=True),
        sa.Column("updated_by", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "sso_company_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("google_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("microsoft_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("allowed_domains", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"),
                  nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_sso_company_settings_company", "sso_company_settings", ["company_id"], unique=True)
    for sql in enable_tenant_rls_sql("sso_company_settings"):
        op.execute(sql)


def downgrade() -> None:
    for sql in disable_tenant_rls_sql("sso_company_settings"):
        op.execute(sql)
    op.drop_table("sso_company_settings")
    op.drop_table("sso_providers")
