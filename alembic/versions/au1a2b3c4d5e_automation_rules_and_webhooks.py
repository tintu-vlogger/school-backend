"""automation rules, run log and webhooks

Revision ID: au1a2b3c4d5e
Revises: org1a2b3c4d5
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision = "au1a2b3c4d5e"
down_revision = "org1a2b3c4d5"
branch_labels = None
depends_on = None

TABLES = ["automation_rules", "automation_runs", "automation_webhooks", "automation_webhook_deliveries"]


def _base():
    return [
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, unique=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
    ]


def _stamps():
    return [sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False)]


def upgrade() -> None:
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    op.create_table(
        "automation_rules", *_base(),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("entity", sa.String(40), nullable=False),
        sa.Column("trigger", sa.String(20), nullable=False),
        sa.Column("fields", jsonb, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("request_type", sa.String(80), nullable=True),
        sa.Column("date_field", sa.String(60), nullable=True),
        sa.Column("offset_days", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("repeat_yearly", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("condition", sa.Text(), nullable=True),
        sa.Column("actions", jsonb, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("run_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_name", sa.String(200), nullable=True),
        *_stamps(),
    )
    op.create_index("ix_automation_rules_company_entity", "automation_rules",
                    ["company_id", "entity", "trigger", "active"])
    op.create_table(
        "automation_runs", *_base(),
        sa.Column("rule_id", sa.Integer(), sa.ForeignKey("automation_rules.id"), nullable=False),
        sa.Column("trigger", sa.String(20), nullable=False),
        sa.Column("entity", sa.String(40), nullable=False),
        sa.Column("record_id", sa.Integer(), nullable=True),
        sa.Column("record_label", sa.String(250), nullable=True),
        sa.Column("occurrence_key", sa.String(80), nullable=True),
        sa.Column("matched", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("results", jsonb, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("error", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_automation_runs_company_created", "automation_runs", ["company_id", sa.text("created_at DESC")])
    op.create_index("ix_automation_runs_rule", "automation_runs", ["company_id", "rule_id", sa.text("created_at DESC")])
    op.create_index("uq_automation_runs_occurrence", "automation_runs", ["rule_id", "occurrence_key"], unique=True,
                    postgresql_where=sa.text("occurrence_key IS NOT NULL"))
    op.create_table(
        "automation_webhooks", *_base(),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("secret", sa.String(200), nullable=False),
        sa.Column("headers", jsonb, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
    )
    op.create_index("ix_automation_webhooks_company", "automation_webhooks", ["company_id", "active"])
    op.create_table(
        "automation_webhook_deliveries", *_base(),
        sa.Column("webhook_id", sa.Integer(), sa.ForeignKey("automation_webhooks.id"), nullable=False),
        sa.Column("rule_id", sa.Integer(), sa.ForeignKey("automation_rules.id"), nullable=True),
        sa.Column("event", sa.String(80), nullable=False),
        sa.Column("payload", jsonb, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.String(500), nullable=True),
        sa.Column("error", sa.String(500), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
    )
    op.create_index("ix_automation_webhook_deliveries_company", "automation_webhook_deliveries",
                    ["company_id", "webhook_id", sa.text("created_at DESC")])
    op.create_index("ix_automation_webhook_deliveries_retry", "automation_webhook_deliveries",
                    ["status", "next_attempt_at"])
    for table in TABLES:
        for sql in enable_tenant_rls_sql(table):
            op.execute(sql)


def downgrade() -> None:
    for table in reversed(TABLES):
        for sql in disable_tenant_rls_sql(table):
            op.execute(sql)
        op.drop_table(table)
