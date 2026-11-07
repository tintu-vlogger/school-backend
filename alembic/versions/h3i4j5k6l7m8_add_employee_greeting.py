"""title (Mr., Ms., Dr.) before an employee's name

Revision ID: h3i4j5k6l7m8
Revises: 8f89885cc386
Create Date: 2026-04-10 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'h3i4j5k6l7m8'
down_revision: Union[str, None] = '8f89885cc386'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('employees', sa.Column('greeting', sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column('employees', 'greeting')
