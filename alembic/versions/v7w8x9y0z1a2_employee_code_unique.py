"""Employee IDs are unique within a company

Revision ID: v7w8x9y0z1a2
Revises: u6v7w8x9y0z1
"""
import sqlalchemy as sa
from alembic import op

revision = "v7w8x9y0z1a2"
down_revision = "u6v7w8x9y0z1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("uq_employees_company_code", "employees", ["company_id", "employee_code"], unique=True,
                    postgresql_where=sa.text("employee_code IS NOT NULL"))


def downgrade():
    op.drop_index("uq_employees_company_code", table_name="employees")
