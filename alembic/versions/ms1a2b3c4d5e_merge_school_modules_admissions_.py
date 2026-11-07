"""merge school modules: admissions, attendance, timetable, exams, lms, fees, comms, library, transport, hostel, wellbeing

Revision ID: ms1a2b3c4d5e
Revises: ad1a2b3c4d5e, cm1a2b3c4d5e, ex1a2b3c4d5e, fe1a2b3c4d5e, hs1a2b3c4d5e, lb1a2b3c4d5e, lm1a2b3c4d5e, sa1a2b3c4d5e, tr1a2b3c4d5e, tt1a2b3c4d5e, wb1a2b3c4d5e
Create Date: 2026-11-07 23:46:56.940108
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = 'ms1a2b3c4d5e'
down_revision: Union[str, None] = ('ad1a2b3c4d5e', 'cm1a2b3c4d5e', 'ex1a2b3c4d5e', 'fe1a2b3c4d5e', 'hs1a2b3c4d5e', 'lb1a2b3c4d5e', 'lm1a2b3c4d5e', 'sa1a2b3c4d5e', 'tr1a2b3c4d5e', 'tt1a2b3c4d5e', 'wb1a2b3c4d5e')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    pass

def downgrade() -> None:
    pass
