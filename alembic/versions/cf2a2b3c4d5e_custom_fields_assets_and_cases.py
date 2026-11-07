"""Custom fields on assets and help desk cases.

Revision ID: cf2a2b3c4d5e
Revises: wp1a2b3c4d5e
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "cf2a2b3c4d5e"
down_revision = "wp1a2b3c4d5e"
branch_labels = None
depends_on = None

TABLES = ("assets", "helpdesk_cases")


def upgrade() -> None:
    for t in TABLES:
        op.add_column(t, sa.Column("custom", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))


def downgrade() -> None:
    for t in TABLES:
        op.drop_column(t, "custom")
