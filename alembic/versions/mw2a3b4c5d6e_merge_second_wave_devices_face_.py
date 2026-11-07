"""merge second wave: devices, face attendance, positions, travel and recycle bin, custom forms, bank files, sso

Revision ID: mw2a3b4c5d6e
Revises: csv7f3a1b2c9, dv1a2b3c4d5e, fa1c3e5a7b9d, pos1a2b3c4d5, sb1a2b3c4d5e, ss1a2b3c4d5e, tv1a2b3c4d5e
Create Date: 2026-11-07 16:08:46.311650
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = 'mw2a3b4c5d6e'
down_revision: Union[str, None] = ('csv7f3a1b2c9', 'dv1a2b3c4d5e', 'fa1c3e5a7b9d', 'pos1a2b3c4d5', 'sb1a2b3c4d5e', 'ss1a2b3c4d5e', 'tv1a2b3c4d5e')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    pass

def downgrade() -> None:
    pass
