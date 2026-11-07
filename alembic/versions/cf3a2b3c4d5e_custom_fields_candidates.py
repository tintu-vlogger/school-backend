"""Custom fields on candidates.

Revision ID: cf3a2b3c4d5e
Revises: tl1a2b3c4d5e
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "cf3a2b3c4d5e"
down_revision = "tl1a2b3c4d5e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("candidates", sa.Column("custom", postgresql.JSONB(), nullable=False,
                                          server_default=sa.text("'{}'::jsonb")))


def downgrade() -> None:
    op.drop_column("candidates", "custom")
