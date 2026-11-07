"""Attendance devices: more ways to connect (vendor API, database read, LAN pull, ADMS
cloud push, vendor push, email) and the office connector.

Revision ID: dv1a2b3c4d5e
Revises: ig1a2b3c4d5e
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "dv1a2b3c4d5e"
down_revision = "ig1a2b3c4d5e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("attendance_devices", sa.Column("source_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("attendance_devices", sa.Column("runs_on", sa.String(length=20), server_default=sa.text("'server'"),
                                                  nullable=False))
    op.add_column("attendance_devices", sa.Column("sync_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("attendance_devices", sa.Column("connector_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("attendance_devices", sa.Column("connector_info", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_index("uq_attendance_devices_adms_serial", "attendance_devices", ["serial_number"], unique=True,
                    postgresql_where=sa.text("connection = 'adms' AND serial_number IS NOT NULL"))


def downgrade() -> None:
    op.drop_index("uq_attendance_devices_adms_serial", table_name="attendance_devices")
    for col in ("connector_info", "connector_seen_at", "sync_state", "runs_on", "source_config"):
        op.drop_column("attendance_devices", col)
