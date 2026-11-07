"""Workflow engine: definitions, instances, tasks, delegations

Revision ID: w8x9y0z1a2b3
Revises: v7w8x9y0z1a2
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision = "w8x9y0z1a2b3"
down_revision = "v7w8x9y0z1a2"
branch_labels = None
depends_on = None

TABLES = ("workflow_definitions", "workflow_instances", "workflow_tasks", "workflow_delegations")


def _base():
    return [
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", postgresql.UUID(as_uuid=True), nullable=False, unique=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
    ]


def _stamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    ]


def upgrade():
    op.create_table(
        "workflow_definitions", *_base(),
        sa.Column("lineage_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("request_type", sa.String(80), nullable=False),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("applies_when", sa.Text(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("100")),
        sa.Column("steps", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("change_note", sa.String(300), nullable=True),
        sa.Column("created_by_name", sa.String(200), nullable=True),
        *_stamps(),
    )
    op.create_index("ix_workflow_definitions_company_type", "workflow_definitions", ["company_id", "request_type", "is_current"])
    op.create_index("ix_workflow_definitions_company_lineage", "workflow_definitions", ["company_id", "lineage_id", "version"])

    op.create_table(
        "workflow_instances", *_base(),
        sa.Column("definition_id", sa.Integer(), sa.ForeignKey("workflow_definitions.id"), nullable=True),
        sa.Column("request_type", sa.String(80), nullable=False),
        sa.Column("reference_no", sa.String(60), nullable=True),
        sa.Column("title", sa.String(250), nullable=False),
        sa.Column("subject_type", sa.String(60), nullable=True),
        sa.Column("subject_id", sa.Integer(), nullable=True),
        sa.Column("requester_employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=True),
        sa.Column("requester_name", sa.String(200), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("current_step", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("result_note", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
    )
    op.create_index("ix_workflow_instances_company_status", "workflow_instances", ["company_id", "status", sa.text("created_at DESC")])
    op.create_index("ix_workflow_instances_company_requester", "workflow_instances", ["company_id", "requester_employee_id"])
    op.create_index("ix_workflow_instances_company_subject", "workflow_instances", ["company_id", "subject_type", "subject_id"])
    op.create_index("ix_workflow_instances_definition", "workflow_instances", ["definition_id"])

    op.create_table(
        "workflow_tasks", *_base(),
        sa.Column("instance_id", sa.Integer(), sa.ForeignKey("workflow_instances.id"), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("step_name", sa.String(150), nullable=False),
        sa.Column("approver_kind", sa.String(20), nullable=False, server_default=sa.text("'employee'")),
        sa.Column("approver_employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=True),
        sa.Column("approver_name", sa.String(200), nullable=True),
        sa.Column("delegated_from_employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("decided_by_name", sa.String(200), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escalated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        *_stamps(),
    )
    op.create_index("ix_workflow_tasks_company_approver_status", "workflow_tasks", ["company_id", "approver_employee_id", "status"])
    op.create_index("ix_workflow_tasks_company_owner_status", "workflow_tasks", ["company_id", "approver_kind", "status"])
    op.create_index("ix_workflow_tasks_instance", "workflow_tasks", ["instance_id"])
    op.create_index("ix_workflow_tasks_due", "workflow_tasks", ["company_id", "status", "due_at"])

    op.create_table(
        "workflow_delegations", *_base(),
        sa.Column("from_employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("to_employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("request_type", sa.String(80), nullable=True),
        sa.Column("note", sa.String(300), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        *_stamps(),
    )
    op.create_index("ix_workflow_delegations_company_from", "workflow_delegations", ["company_id", "from_employee_id", "active"])
    for table in TABLES:
        for sql in enable_tenant_rls_sql(table):
            op.execute(sql)


def downgrade():
    for table in reversed(TABLES):
        for sql in disable_tenant_rls_sql(table):
            op.execute(sql)
        op.drop_table(table)
