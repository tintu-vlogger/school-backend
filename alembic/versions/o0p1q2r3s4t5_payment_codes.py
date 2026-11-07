"""payment codes for subscription UPI payments, and the deal stored with each payment

Each code goes into the UPI payment note and records what the company chose, so the
platform admin can look the code up in the payment history and confirm the payment.

Revision ID: o0p1q2r3s4t5
Revises: l7m8n9o0p1q2
Create Date: 2026-11-05 21:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'o0p1q2r3s4t5'
down_revision: Union[str, None] = 'l7m8n9o0p1q2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "payment_codes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(16), nullable=False, unique=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plan", sa.String(30), nullable=False),
        sa.Column("months", sa.Integer(), nullable=False),
        sa.Column("coupon_code", sa.String(30), nullable=True),
        sa.Column("coupon_percent", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("discount_percent", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("payment_id", sa.Integer(), sa.ForeignKey("subscription_payments.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_payment_codes_company_id", "payment_codes", ["company_id"])
    op.create_index("ix_payment_codes_period", "payment_codes", ["company_id", "period_start"])
    # what each confirmed payment bought, so later plan or price changes don't alter it
    op.add_column("subscription_payments", sa.Column("details", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("subscription_payments", "details")
    op.drop_index("ix_payment_codes_period", table_name="payment_codes")
    op.drop_index("ix_payment_codes_company_id", table_name="payment_codes")
    op.drop_table("payment_codes")
