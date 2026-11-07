"""signature image for employees who sign letters

Revision ID: 851571592cff
Revises: 4abd7333da52
Create Date: 2026-04-02 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '851571592cff'
down_revision: Union[str, None] = '4abd7333da52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('employees', sa.Column('signature_url', sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column('employees', 'signature_url')
