"""Roles and access: data scopes and field visibility per role, several roles per employee,
and the company owner's own employee record.

Revision ID: ig1a2b3c4d5e
Revises: mg1a2b3c4d5e
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision = "ig1a2b3c4d5e"
down_revision = "mg1a2b3c4d5e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("roles", sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()),
                                     server_default=sa.text("'{}'::jsonb"), nullable=False))
    op.add_column("roles", sa.Column("field_access", postgresql.JSONB(astext_type=sa.Text()),
                                     server_default=sa.text("'{}'::jsonb"), nullable=False))
    op.add_column("roles", sa.Column("customised", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.add_column("companies", sa.Column("owner_employee_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_companies_owner_employee_id", "companies", "employees", ["owner_employee_id"], ["id"],
                          ondelete="SET NULL")
    op.create_table(
        "employee_roles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("employee_id", "role_id", name="uq_employee_roles_employee_role"),
    )
    op.create_index("ix_employee_roles_company_id", "employee_roles", ["company_id"])
    op.create_index("ix_employee_roles_role_id", "employee_roles", ["role_id"])
    for sql in enable_tenant_rls_sql("employee_roles"):
        op.execute(sql)


def downgrade() -> None:
    for sql in disable_tenant_rls_sql("employee_roles"):
        op.execute(sql)
    op.drop_index("ix_employee_roles_role_id", table_name="employee_roles")
    op.drop_index("ix_employee_roles_company_id", table_name="employee_roles")
    op.drop_table("employee_roles")
    op.drop_constraint("fk_companies_owner_employee_id", "companies", type_="foreignkey")
    op.drop_column("companies", "owner_employee_id")
    op.drop_column("roles", "customised")
    op.drop_column("roles", "field_access")
    op.drop_column("roles", "scopes")
