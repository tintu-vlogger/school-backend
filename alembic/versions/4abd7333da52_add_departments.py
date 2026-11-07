"""departments, and each employee's department

Revision ID: 4abd7333da52
Revises: 4ff3df774c5c
Create Date: 2026-03-28 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '4abd7333da52'
down_revision: Union[str, None] = '4ff3df774c5c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('departments',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=True),
    sa.Column('department_name', sa.String(length=200), nullable=False),
    sa.Column('department_code', sa.String(length=20), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('head_employee_id', sa.Integer(), nullable=True),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('is_global', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['head_employee_id'], ['employees.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'department_name', name='uq_departments_company_name'),
    sa.UniqueConstraint('uuid')
    )
    op.add_column('employees', sa.Column('department_id', sa.Integer(), nullable=True))
    op.create_foreign_key('employees_department_id_fkey', 'employees', 'departments', ['department_id'], ['id'])


def downgrade() -> None:
    op.drop_constraint('employees_department_id_fkey', 'employees', type_='foreignkey')
    op.drop_column('employees', 'department_id')
    op.drop_table('departments')
