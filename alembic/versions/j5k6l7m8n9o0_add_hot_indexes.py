"""indexes for the most-read lists

Revision ID: j5k6l7m8n9o0
Revises: i4j5k6l7m8n9
Create Date: 2026-04-18 17:00:00.000000
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'j5k6l7m8n9o0'
down_revision: Union[str, None] = 'i4j5k6l7m8n9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Every query in this multi-company app filters by company first.
INDEXES = [
    ("ix_employees_company_active",       "employees",     "(company_id, active)"),
    ("ix_employees_company_designation",  "employees",     "(company_id, designation_id)"),
    ("ix_notifications_company_created",  "notifications", "(company_id, created_at DESC)"),
    ("ix_notifications_company_employee", "notifications", "(company_id, employee_id)"),
]


def upgrade() -> None:
    for name, table, cols in INDEXES:
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} {cols};")


def downgrade() -> None:
    for name, _table, _cols in INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name};")
