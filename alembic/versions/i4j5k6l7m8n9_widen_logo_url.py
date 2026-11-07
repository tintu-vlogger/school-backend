"""company logo can be stored inline (data URL)

Revision ID: i4j5k6l7m8n9
Revises: h3i4j5k6l7m8
Create Date: 2026-04-15 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'i4j5k6l7m8n9'
down_revision: Union[str, None] = 'h3i4j5k6l7m8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('companies', 'logo_url', existing_type=sa.String(length=500), type_=sa.Text(), existing_nullable=True)


def downgrade() -> None:
    op.alter_column('companies', 'logo_url', existing_type=sa.Text(), type_=sa.String(length=500), existing_nullable=True)
