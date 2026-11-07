"""Employee timezone, reporting manager and custom field values

Revision ID: r3s4t5u6v7w8
Revises: q2r3s4t5u6v7
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "r3s4t5u6v7w8"
down_revision = "q2r3s4t5u6v7"
branch_labels = None
depends_on = None


def upgrade():
    # NULL timezone = use the company's timezone.
    op.add_column("employees", sa.Column("timezone", sa.String(64), nullable=True))
    op.add_column("employees", sa.Column("reporting_manager_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_employees_reporting_manager_id", "employees", "employees", ["reporting_manager_id"], ["id"],
                          ondelete="SET NULL")
    op.create_index("ix_employees_company_manager", "employees", ["company_id", "reporting_manager_id"])
    op.create_index("ix_employees_company_department", "employees", ["company_id", "department_id"])
    op.add_column("employees", sa.Column("custom", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))


def downgrade():
    op.drop_column("employees", "custom")
    op.drop_index("ix_employees_company_department", table_name="employees")
    op.drop_index("ix_employees_company_manager", table_name="employees")
    op.drop_constraint("fk_employees_reporting_manager_id", "employees", type_="foreignkey")
    op.drop_column("employees", "reporting_manager_id")
    op.drop_column("employees", "timezone")
