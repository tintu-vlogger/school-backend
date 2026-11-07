"""Masters: master_types and master_values

Revision ID: s4t5u6v7w8x9
Revises: r3s4t5u6v7w8
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision = "s4t5u6v7w8x9"
down_revision = "r3s4t5u6v7w8"
branch_labels = None
depends_on = None


def _common():
    return [
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", postgresql.UUID(as_uuid=True), nullable=False, unique=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
    ]


def _stamps():
    return [
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    ]


def upgrade():
    op.create_table(
        "master_types", *_common(),
        sa.Column("key", sa.String(60), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("hierarchical", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        *_stamps(),
        sa.UniqueConstraint("company_id", "key", name="uq_master_types_company_key"),
    )
    op.create_index("ix_master_types_company_sort", "master_types", ["company_id", "sort_order"])
    op.create_table(
        "master_values", *_common(),
        sa.Column("type_id", sa.Integer(), sa.ForeignKey("master_types.id"), nullable=False),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("master_values.id"), nullable=True),
        sa.Column("code", sa.String(60), nullable=True),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("attributes", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        *_stamps(),
    )
    op.create_index("ix_master_values_company_type", "master_values", ["company_id", "type_id", "sort_order"])
    op.create_index("ix_master_values_company_parent", "master_values", ["company_id", "parent_id"])
    op.create_index("uq_master_values_type_code", "master_values", ["type_id", "code"], unique=True,
                    postgresql_where=sa.text("code IS NOT NULL"))
    for table in ("master_types", "master_values"):
        for sql in enable_tenant_rls_sql(table):
            op.execute(sql)


def downgrade():
    for table in ("master_values", "master_types"):
        for sql in disable_tenant_rls_sql(table):
            op.execute(sql)
    op.drop_table("master_values")
    op.drop_table("master_types")
