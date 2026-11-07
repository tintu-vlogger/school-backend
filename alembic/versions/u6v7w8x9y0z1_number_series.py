"""Number series and their counters

Revision ID: u6v7w8x9y0z1
Revises: t5u6v7w8x9y0
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision = "u6v7w8x9y0z1"
down_revision = "t5u6v7w8x9y0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "number_series",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", postgresql.UUID(as_uuid=True), nullable=False, unique=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("key", sa.String(60), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("prefix", sa.String(30), nullable=False, server_default=sa.text("''")),
        sa.Column("pattern", sa.String(120), nullable=False),
        sa.Column("reset", sa.String(20), nullable=False, server_default=sa.text("'never'")),
        sa.Column("start_number", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("allow_manual", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("company_id", "key", name="uq_number_series_company_key"),
    )
    op.create_table(
        "number_series_counters",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("series_id", sa.Integer(), sa.ForeignKey("number_series.id"), nullable=False),
        sa.Column("period", sa.String(20), nullable=False),
        sa.Column("last_value", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("series_id", "period", name="uq_number_series_counters_series_period"),
    )
    op.create_index("ix_number_series_counters_company", "number_series_counters", ["company_id", "series_id"])
    for table in ("number_series", "number_series_counters"):
        for sql in enable_tenant_rls_sql(table):
            op.execute(sql)


def downgrade():
    for table in ("number_series_counters", "number_series"):
        for sql in disable_tenant_rls_sql(table):
            op.execute(sql)
    op.drop_table("number_series_counters")
    op.drop_table("number_series")
