"""platform team, subscription payments, partner commissions and payouts, referral on companies

Revision ID: l7m8n9o0p1q2
Revises: k6l7m8n9o0p1
Create Date: 2026-10-22 23:30:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'l7m8n9o0p1q2'
down_revision: Union[str, None] = 'k6l7m8n9o0p1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "platform_users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("uuid", postgresql.UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()"), unique=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("phone", sa.String(20), nullable=True),
        sa.Column("password", sa.String(255), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("ref_code", sa.String(12), nullable=True, unique=True),
        sa.Column("upi_id", sa.String(100), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("failed_logins", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("token_version", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("platform_users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "platform_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("data", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "payouts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("platform_user_id", sa.Integer(), sa.ForeignKey("platform_users.id"), nullable=False, index=True),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("upi_id", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'requested'")),
        sa.Column("reference", sa.String(100), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_by", sa.Integer(), sa.ForeignKey("platform_users.id"), nullable=True),
    )
    op.create_table(
        "subscription_payments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("plan", sa.String(30), nullable=False),
        sa.Column("months", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reference", sa.String(100), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("recorded_by", sa.Integer(), sa.ForeignKey("platform_users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "commissions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("platform_user_id", sa.Integer(), sa.ForeignKey("platform_users.id"), nullable=False, index=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("payment_id", sa.Integer(), sa.ForeignKey("subscription_payments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("payout_id", sa.Integer(), sa.ForeignKey("payouts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.add_column("companies", sa.Column("referred_by_id", sa.Integer(), sa.ForeignKey("platform_users.id", ondelete="SET NULL"), nullable=True))
    op.create_index("ix_companies_referred_by_id", "companies", ["referred_by_id"])


def downgrade() -> None:
    op.drop_index("ix_companies_referred_by_id", table_name="companies")
    op.drop_column("companies", "referred_by_id")
    op.drop_table("commissions")
    op.drop_table("subscription_payments")
    op.drop_table("payouts")
    op.drop_table("platform_settings")
    op.drop_table("platform_users")
