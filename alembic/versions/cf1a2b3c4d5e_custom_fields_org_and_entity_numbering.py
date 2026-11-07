"""Custom fields on departments, designations, locations and legal entities; number series
per legal entity ({ENTITY} token, shared or per-entity counters).

Revision ID: cf1a2b3c4d5e
Revises: org1a2b3c4d5
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "cf1a2b3c4d5e"
down_revision = "org1a2b3c4d5"
branch_labels = None
depends_on = None

TABLES = ("departments", "designations", "locations", "legal_entities")


def upgrade() -> None:
    for t in TABLES:
        op.add_column(t, sa.Column("custom", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.add_column("number_series", sa.Column("counter_scope", sa.String(20), nullable=False,
                                             server_default=sa.text("'shared'")))
    op.add_column("number_series_counters", sa.Column("legal_entity_id", sa.Integer(), nullable=False,
                                                      server_default=sa.text("0")))
    op.drop_constraint("uq_number_series_counters_series_period", "number_series_counters", type_="unique")
    op.create_unique_constraint("uq_number_series_counters_series_period_entity", "number_series_counters",
                                ["series_id", "period", "legal_entity_id"])


def downgrade() -> None:
    op.execute("DELETE FROM number_series_counters WHERE legal_entity_id <> 0")
    op.drop_constraint("uq_number_series_counters_series_period_entity", "number_series_counters", type_="unique")
    op.create_unique_constraint("uq_number_series_counters_series_period", "number_series_counters",
                                ["series_id", "period"])
    op.drop_column("number_series_counters", "legal_entity_id")
    op.drop_column("number_series", "counter_scope")
    for t in TABLES:
        op.drop_column(t, "custom")
