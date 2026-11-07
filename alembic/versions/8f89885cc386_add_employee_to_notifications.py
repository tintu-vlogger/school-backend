"""notifications can be addressed to one employee

Revision ID: 8f89885cc386
Revises: 851571592cff
Create Date: 2026-04-05 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '8f89885cc386'
down_revision: Union[str, None] = '851571592cff'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('notifications', sa.Column('employee_id', sa.Integer(), nullable=True))
    op.create_foreign_key('notifications_employee_id_fkey', 'notifications', 'employees', ['employee_id'], ['id'])


def downgrade() -> None:
    op.drop_constraint('notifications_employee_id_fkey', 'notifications', type_='foreignkey')
    op.drop_column('notifications', 'employee_id')
