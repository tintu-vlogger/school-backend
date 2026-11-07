"""HR processes: company-defined change processes and their requests

Revision ID: hp1a2b3c4d5e
Revises: org1a2b3c4d5
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision = "hp1a2b3c4d5e"
down_revision = "org1a2b3c4d5"
branch_labels = None
depends_on = None

TABLES = ("hr_process_types", "hr_process_requests")
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "hr_process_types",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, unique=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("code", sa.String(10), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("change_type", sa.String(30), server_default=sa.text("'other'"), nullable=False),
        sa.Column("fields", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("key_field", sa.String(80), nullable=True),
        sa.Column("target_status", sa.String(20), nullable=True),
        sa.Column("letter_template_uuids", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("revocable", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("needs_approval", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("is_system", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("company_id", "code", name="uq_hr_process_types_company_code"),
    )
    op.create_index("ix_hr_process_types_company_active", "hr_process_types", ["company_id", "active", "sort_order"])
    op.create_table(
        "hr_process_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, unique=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("process_type_id", sa.Integer(), sa.ForeignKey("hr_process_types.id"), nullable=False),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("batch_id", sa.UUID(), nullable=True),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("new_values", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("old_values", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("reason", sa.String(300), nullable=True),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("workflow_instance_id", sa.Integer(), sa.ForeignKey("workflow_instances.id"), nullable=True),
        sa.Column("job_change_id", sa.Integer(), sa.ForeignKey("employee_job_history.id"), nullable=True),
        sa.Column("revoke_job_change_id", sa.Integer(), sa.ForeignKey("employee_job_history.id"), nullable=True),
        sa.Column("letter_uuids", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_name", sa.String(200), nullable=True),
        sa.Column("revoke_reason", sa.String(300), nullable=True),
        sa.Column("created_by_name", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_hr_process_requests_company_status", "hr_process_requests",
                    ["company_id", "status", "effective_date"])
    op.create_index("ix_hr_process_requests_company_employee", "hr_process_requests",
                    ["company_id", "employee_id", "process_type_id"])
    op.create_index("ix_hr_process_requests_company_batch", "hr_process_requests", ["company_id", "batch_id"])
    for t in TABLES:
        for sql in enable_tenant_rls_sql(t):
            op.execute(sql)


def downgrade() -> None:
    for t in reversed(TABLES):
        for sql in disable_tenant_rls_sql(t):
            op.execute(sql)
        op.drop_table(t)
