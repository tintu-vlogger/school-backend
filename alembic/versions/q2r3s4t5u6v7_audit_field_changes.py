"""Field-level audit log, append-only

Revision ID: q2r3s4t5u6v7
Revises: p1q2r3s4t5u6
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "q2r3s4t5u6v7"
down_revision = "p1q2r3s4t5u6"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("audit_logs", "company_id", nullable=True)
    op.alter_column("audit_logs", "created_at", nullable=False, server_default=sa.text("now()"))
    op.add_column("audit_logs", sa.Column("platform_user_id", sa.Integer(), nullable=True))
    op.add_column("audit_logs", sa.Column("actor_name", sa.String(200), nullable=True))
    op.add_column("audit_logs", sa.Column("entity_uuid", sa.String(64), nullable=True))
    op.add_column("audit_logs", sa.Column("entity_label", sa.String(200), nullable=True))
    op.add_column("audit_logs", sa.Column("changes", postgresql.JSONB(), nullable=True))
    op.add_column("audit_logs", sa.Column("ip_address", sa.String(64), nullable=True))
    op.add_column("audit_logs", sa.Column("user_agent", sa.String(400), nullable=True))
    op.add_column("audit_logs", sa.Column("request_id", sa.String(64), nullable=True))
    op.create_index("ix_audit_logs_company_created", "audit_logs", ["company_id", sa.text("created_at DESC")])
    op.create_index("ix_audit_logs_company_entity", "audit_logs", ["company_id", "entity_type", "entity_id"])
    op.create_index("ix_audit_logs_company_actor", "audit_logs", ["company_id", "actor_type", "actor_id"])

    # Platform-level entries (no company) may be written from any context but are only
    # readable with the bypass.
    op.execute("DROP POLICY IF EXISTS platform_entries_insert ON audit_logs")
    op.execute("CREATE POLICY platform_entries_insert ON audit_logs FOR INSERT WITH CHECK (company_id IS NULL)")

    # Append-only. The only exception is permanent removal of a whole company by the
    # platform admin, which sets app.audit_purge for its own transaction.
    op.execute("""
        CREATE OR REPLACE FUNCTION audit_logs_append_only() RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' AND current_setting('app.audit_purge', true) = 'on' THEN
            RETURN OLD;
          END IF;
          RAISE EXCEPTION 'audit_logs is append-only: % is not allowed', TG_OP;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("DROP TRIGGER IF EXISTS audit_logs_append_only ON audit_logs")
    op.execute("""
        CREATE TRIGGER audit_logs_append_only BEFORE UPDATE OR DELETE ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION audit_logs_append_only()
    """)


def downgrade():
    op.execute("DROP TRIGGER IF EXISTS audit_logs_append_only ON audit_logs")
    op.execute("DROP FUNCTION IF EXISTS audit_logs_append_only()")
    op.execute("DROP POLICY IF EXISTS platform_entries_insert ON audit_logs")
    op.drop_index("ix_audit_logs_company_actor", table_name="audit_logs")
    op.drop_index("ix_audit_logs_company_entity", table_name="audit_logs")
    op.drop_index("ix_audit_logs_company_created", table_name="audit_logs")
    for col in ("request_id", "user_agent", "ip_address", "changes", "entity_label", "entity_uuid", "actor_name",
                "platform_user_id"):
        op.drop_column("audit_logs", col)
    op.alter_column("audit_logs", "created_at", nullable=True)
    op.execute("DELETE FROM audit_logs WHERE company_id IS NULL")
    op.alter_column("audit_logs", "company_id", nullable=False)
