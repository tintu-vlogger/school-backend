"""Merge every module branch into one line: attendance with the leave/attendance second
pass, engagement, insights, organisation, automation, custom fields (organisation,
workplace and candidates), HR processes, performance and learning, payroll with its second
pass and benefits, workflow depth and channels.

Revision ID: mg1a2b3c4d5e
Revises: la2b3c4d5e6f, en1g2a3g4e5m, in1s2i3g4h5t, au1a2b3c4d5e, cf1a2b3c4d5e, hp1a2b3c4d5e, pf1a2b3c4d5e, py2b3c4d5e6f, cf2a2b3c4d5e, cf3a2b3c4d5e, wn2a1b2c3d4e
"""

revision = "mg1a2b3c4d5e"
down_revision = ("la2b3c4d5e6f", "en1g2a3g4e5m", "in1s2i3g4h5t", "au1a2b3c4d5e", "cf1a2b3c4d5e", "hp1a2b3c4d5e",
                 "pf1a2b3c4d5e", "py2b3c4d5e6f", "cf2a2b3c4d5e", "cf3a2b3c4d5e", "wn2a1b2c3d4e")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
