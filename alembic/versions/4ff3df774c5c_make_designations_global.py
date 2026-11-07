"""designations can be shared templates (no company)

Revision ID: 4ff3df774c5c
Revises: 4e43db128d0b
Create Date: 2026-03-26 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '4ff3df774c5c'
down_revision: Union[str, None] = '4e43db128d0b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('designations', sa.Column('is_global', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.alter_column('designations', 'company_id', existing_type=sa.INTEGER(), nullable=True)


def downgrade() -> None:
    op.alter_column('designations', 'company_id', existing_type=sa.INTEGER(), nullable=False)
    op.drop_column('designations', 'is_global')
