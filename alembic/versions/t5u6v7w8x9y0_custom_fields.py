"""Custom fields: sections and field definitions

Revision ID: t5u6v7w8x9y0
Revises: s4t5u6v7w8x9
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision = "t5u6v7w8x9y0"
down_revision = "s4t5u6v7w8x9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "custom_field_sections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", postgresql.UUID(as_uuid=True), nullable=False, unique=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("entity", sa.String(40), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_custom_field_sections_company_entity", "custom_field_sections", ["company_id", "entity", "sort_order"])
    op.create_table(
        "custom_field_defs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", postgresql.UUID(as_uuid=True), nullable=False, unique=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("entity", sa.String(40), nullable=False),
        sa.Column("section_id", sa.Integer(), sa.ForeignKey("custom_field_sections.id"), nullable=True),
        sa.Column("key", sa.String(60), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("field_type", sa.String(30), nullable=False),
        sa.Column("master_type_key", sa.String(60), nullable=True),
        sa.Column("options", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("help_text", sa.String(300), nullable=True),
        sa.Column("placeholder", sa.String(120), nullable=True),
        sa.Column("validation", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("visible_to_employee", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("editable_by_employee", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("company_id", "entity", "key", name="uq_custom_field_defs_company_entity_key"),
    )
    op.create_index("ix_custom_field_defs_company_entity", "custom_field_defs", ["company_id", "entity", "sort_order"])
    op.create_index("ix_custom_field_defs_company_section", "custom_field_defs", ["company_id", "section_id"])
    for table in ("custom_field_sections", "custom_field_defs"):
        for sql in enable_tenant_rls_sql(table):
            op.execute(sql)


def downgrade():
    for table in ("custom_field_defs", "custom_field_sections"):
        for sql in disable_tenant_rls_sql(table):
            op.execute(sql)
    op.drop_table("custom_field_defs")
    op.drop_table("custom_field_sections")
