"""Custom services (no-code forms): services, versioned form definitions, records, their
comments and saved list views.

Revision ID: csv7f3a1b2c9
Revises: ig1a2b3c4d5e
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision = "csv7f3a1b2c9"
down_revision = "ig1a2b3c4d5e"
branch_labels = None
depends_on = None

TABLES = ("custom_services", "custom_form_versions", "custom_form_records", "custom_form_comments",
          "custom_form_views")


def _uuid():
    return sa.Column("uuid", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False)


def _jsonb(name, default="'{}'::jsonb"):
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text(default), nullable=False)


def _ts(name):
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "custom_services",
        sa.Column("id", sa.Integer(), nullable=False),
        _uuid(),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("code", sa.String(30), nullable=False),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("description", sa.String(300), nullable=True),
        sa.Column("icon", sa.String(40), server_default=sa.text("'clipboard-list'"), nullable=False),
        sa.Column("nav_section", sa.String(20), server_default=sa.text("'home'"), nullable=False),
        sa.Column("template_key", sa.String(40), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("needs_approval", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("allow_drafts", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("allow_cancel", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        _jsonb("submit_access", "'{\"everyone\": true}'::jsonb"),
        _jsonb("manager_roles", "'[\"hr\"]'::jsonb"),
        _jsonb("manager_employee_ids", "'[]'::jsonb"),
        _jsonb("statuses", "'[]'::jsonb"),
        _jsonb("notify"),
        sa.Column("title_field", sa.String(40), nullable=True),
        sa.Column("current_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_by_name", sa.String(200), nullable=True),
        sa.Column("created_by_name", sa.String(200), nullable=True),
        _ts("created_at"), _ts("updated_at"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("uuid"),
        sa.UniqueConstraint("company_id", "code", name="uq_custom_services_company_code"),
    )
    op.create_index("ix_custom_services_company_active", "custom_services", ["company_id", "active", "sort_order"])

    op.create_table(
        "custom_form_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        _uuid(),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("service_id", sa.Integer(), sa.ForeignKey("custom_services.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        _jsonb("definition"),
        sa.Column("change_note", sa.String(300), nullable=True),
        sa.Column("created_by_name", sa.String(200), nullable=True),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("uuid"),
        sa.UniqueConstraint("service_id", "version", name="uq_custom_form_versions_service_version"),
    )
    op.create_index("ix_custom_form_versions_company_service", "custom_form_versions", ["company_id", "service_id"])

    op.create_table(
        "custom_form_records",
        sa.Column("id", sa.Integer(), nullable=False),
        _uuid(),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("service_id", sa.Integer(), sa.ForeignKey("custom_services.id"), nullable=False),
        sa.Column("version_id", sa.Integer(), sa.ForeignKey("custom_form_versions.id"), nullable=False),
        sa.Column("reference_no", sa.String(60), nullable=True),
        sa.Column("title", sa.String(250), nullable=True),
        sa.Column("requester_employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=True),
        sa.Column("requester_name", sa.String(200), nullable=True),
        _jsonb("values"),
        sa.Column("status", sa.String(30), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("workflow_instance_id", sa.Integer(), sa.ForeignKey("workflow_instances.id"), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_name", sa.String(200), nullable=True),
        _ts("created_at"), _ts("updated_at"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("uuid"),
    )
    op.create_index("ix_custom_form_records_company_service", "custom_form_records",
                    ["company_id", "service_id", "status", sa.text("created_at DESC")])
    op.create_index("ix_custom_form_records_company_requester", "custom_form_records",
                    ["company_id", "requester_employee_id"])
    op.create_index("ix_custom_form_records_company_reference", "custom_form_records", ["company_id", "reference_no"])

    op.create_table(
        "custom_form_comments",
        sa.Column("id", sa.Integer(), nullable=False),
        _uuid(),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("record_id", sa.Integer(), sa.ForeignKey("custom_form_records.id"), nullable=False),
        sa.Column("kind", sa.String(20), server_default=sa.text("'comment'"), nullable=False),
        sa.Column("author_employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=True),
        sa.Column("author_name", sa.String(200), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        _jsonb("attachments", "'[]'::jsonb"),
        _jsonb("data"),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("uuid"),
    )
    op.create_index("ix_custom_form_comments_company_record", "custom_form_comments",
                    ["company_id", "record_id", "created_at"])

    op.create_table(
        "custom_form_views",
        sa.Column("id", sa.Integer(), nullable=False),
        _uuid(),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("service_id", sa.Integer(), sa.ForeignKey("custom_services.id"), nullable=False),
        sa.Column("owner_key", sa.String(40), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        _jsonb("filters"),
        _jsonb("columns", "'[]'::jsonb"),
        sa.Column("shared", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        _ts("created_at"), _ts("updated_at"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("uuid"),
    )
    op.create_index("ix_custom_form_views_company_service", "custom_form_views", ["company_id", "service_id", "owner_key"])

    for t in TABLES:
        for sql in enable_tenant_rls_sql(t):
            op.execute(sql)


def downgrade() -> None:
    for t in reversed(TABLES):
        for sql in disable_tenant_rls_sql(t):
            op.execute(sql)
        op.drop_table(t)
