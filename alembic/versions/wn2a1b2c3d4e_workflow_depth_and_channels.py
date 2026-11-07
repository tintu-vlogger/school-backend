"""Workflow depth (comments and attachments, email action links, nudges, reminders,
cancel after approval, settings) and notification channels (preferences, settings,
email templates, delivery log, digests)

Revision ID: wn2a1b2c3d4e
Revises: w8x9y0z1a2b3
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision = "wn2a1b2c3d4e"
down_revision = "w8x9y0z1a2b3"
branch_labels = None
depends_on = None

TABLES = ("workflow_comments", "workflow_action_tokens", "workflow_settings", "notification_preferences",
          "notification_settings", "notification_templates", "notification_deliveries", "notification_digests")


def _company():
    return sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False)


def _uuid():
    return sa.Column("uuid", postgresql.UUID(as_uuid=True), nullable=False, unique=True,
                     server_default=sa.text("gen_random_uuid()"))


def _ts(name):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"))


def upgrade():
    op.add_column("workflow_instances", sa.Column("last_nudged_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("workflow_instances", sa.Column("nudge_count", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.add_column("workflow_instances", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("workflow_instances", sa.Column("revoke_reason", sa.Text(), nullable=True))
    op.add_column("workflow_tasks", sa.Column("decided_via", sa.String(10), nullable=True))
    op.add_column("workflow_tasks", sa.Column("last_reminded_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("workflow_tasks", sa.Column("reminder_count", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.add_column("notifications", sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True))
    # Older notifications were never meant to be emailed.
    op.execute("UPDATE notifications SET dispatched_at = COALESCE(created_at, now()) WHERE dispatched_at IS NULL")
    op.create_index("ix_notifications_undispatched", "notifications", ["id"],
                    postgresql_where=sa.text("dispatched_at IS NULL"))

    op.create_table(
        "workflow_comments", sa.Column("id", sa.Integer(), primary_key=True), _uuid(), _company(),
        sa.Column("instance_id", sa.Integer(), sa.ForeignKey("workflow_instances.id"), nullable=False),
        sa.Column("author_employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=True),
        sa.Column("author_name", sa.String(200), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("attachments", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        _ts("created_at"),
    )
    op.create_index("ix_workflow_comments_company_instance", "workflow_comments",
                    ["company_id", "instance_id", "created_at"])
    op.create_table(
        "workflow_action_tokens", sa.Column("id", sa.Integer(), primary_key=True), _company(),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("workflow_tasks.id"), nullable=False),
        sa.Column("action", sa.String(10), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_ip", sa.String(64), nullable=True),
        sa.Column("used_user_agent", sa.String(300), nullable=True),
        _ts("created_at"),
    )
    op.create_index("ix_workflow_action_tokens_company_task", "workflow_action_tokens", ["company_id", "task_id"])
    op.create_index("ix_workflow_action_tokens_hash", "workflow_action_tokens", ["token_hash"], unique=True)
    op.create_table(
        "workflow_settings", sa.Column("id", sa.Integer(), primary_key=True), _company(),
        sa.Column("config", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("updated_by_name", sa.String(200), nullable=True),
        _ts("created_at"), _ts("updated_at"),
        sa.UniqueConstraint("company_id", name="uq_workflow_settings_company"),
    )

    op.create_table(
        "notification_preferences", sa.Column("id", sa.Integer(), primary_key=True), _company(),
        sa.Column("recipient_key", sa.String(30), nullable=False),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("email", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("push", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        _ts("updated_at"),
        sa.UniqueConstraint("company_id", "recipient_key", "category", name="uq_notification_preferences_key"),
    )
    op.create_index("ix_notification_preferences_company", "notification_preferences", ["company_id", "recipient_key"])
    op.create_table(
        "notification_settings", sa.Column("id", sa.Integer(), primary_key=True), _company(),
        sa.Column("config", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("updated_by_name", sa.String(200), nullable=True),
        _ts("updated_at"),
        sa.UniqueConstraint("company_id", name="uq_notification_settings_company"),
    )
    op.create_table(
        "notification_templates", sa.Column("id", sa.Integer(), primary_key=True), _uuid(), _company(),
        sa.Column("key", sa.String(60), nullable=False),
        sa.Column("subject", sa.String(250), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("updated_by_name", sa.String(200), nullable=True),
        _ts("updated_at"),
        sa.UniqueConstraint("company_id", "key", name="uq_notification_templates_company_key"),
    )
    op.create_table(
        "notification_deliveries", sa.Column("id", sa.Integer(), primary_key=True), _uuid(), _company(),
        sa.Column("notification_id", sa.Integer(), sa.ForeignKey("notifications.id", ondelete="SET NULL"), nullable=True),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=True),
        sa.Column("recipient_key", sa.String(30), nullable=True),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("to_address", sa.String(320), nullable=True),
        sa.Column("category", sa.String(40), nullable=True),
        sa.Column("source", sa.String(40), nullable=False, server_default=sa.text("'notification'")),
        sa.Column("subject", sa.String(250), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        _ts("created_at"), _ts("updated_at"),
    )
    op.create_index("ix_notification_deliveries_company_created", "notification_deliveries",
                    ["company_id", sa.text("created_at DESC")])
    op.create_index("ix_notification_deliveries_retry", "notification_deliveries", ["status", "next_attempt_at"])
    op.create_index("ix_notification_deliveries_company_notification", "notification_deliveries",
                    ["company_id", "notification_id"])
    op.create_table(
        "notification_digests", sa.Column("id", sa.Integer(), primary_key=True), _company(),
        sa.Column("recipient_key", sa.String(30), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("digest_date", sa.Date(), nullable=False),
        sa.Column("items", sa.Integer(), nullable=False, server_default=sa.text("0")),
        _ts("created_at"),
        sa.UniqueConstraint("company_id", "recipient_key", "kind", "digest_date", name="uq_notification_digests_once"),
    )
    op.create_index("ix_notification_digests_company_date", "notification_digests", ["company_id", "digest_date"])
    for table in TABLES:
        for sql in enable_tenant_rls_sql(table):
            op.execute(sql)


def downgrade():
    for table in reversed(TABLES):
        for sql in disable_tenant_rls_sql(table):
            op.execute(sql)
        op.drop_table(table)
    op.drop_index("ix_notifications_undispatched", table_name="notifications")
    op.drop_column("notifications", "dispatched_at")
    for col in ("reminder_count", "last_reminded_at", "decided_via"):
        op.drop_column("workflow_tasks", col)
    for col in ("revoke_reason", "revoked_at", "nudge_count", "last_nudged_at"):
        op.drop_column("workflow_instances", col)
