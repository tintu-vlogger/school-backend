"""employee photo can be stored inline (data URL)

Revision ID: k6l7m8n9o0p1
Revises: j5k6l7m8n9o0
Create Date: 2026-04-20 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'k6l7m8n9o0p1'
down_revision: Union[str, None] = 'j5k6l7m8n9o0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("employees", "profile_image_url", type_=sa.Text(), existing_type=sa.String(500), existing_nullable=True)


def downgrade() -> None:
    op.alter_column("employees", "profile_image_url", type_=sa.String(500), existing_type=sa.Text(), existing_nullable=True)
