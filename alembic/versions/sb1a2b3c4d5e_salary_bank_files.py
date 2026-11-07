"""Salary bank files: company debit accounts, custom bank file formats and the record of every
bank file made.

Revision ID: sb1a2b3c4d5e
Revises: ig1a2b3c4d5e
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision = "sb1a2b3c4d5e"
down_revision = "ig1a2b3c4d5e"
branch_labels = None
depends_on = None

TABLES = ["bank_debit_accounts", "bank_file_formats", "bank_file_batches"]


def _uuid():
    return sa.Column("uuid", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False,
                     unique=True)


def _stamp(name):
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "bank_debit_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        _uuid(),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("legal_entity_id", sa.Integer(), sa.ForeignKey("legal_entities.id"), nullable=True),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("bank_name", sa.String(150), nullable=True),
        sa.Column("account_number", sa.String(30), nullable=False),
        sa.Column("ifsc", sa.String(11), nullable=False),
        sa.Column("account_name", sa.String(200), nullable=True),
        sa.Column("format_key", sa.String(80), nullable=False),
        sa.Column("client_code", sa.String(40), nullable=True),
        sa.Column("corporate_id", sa.String(40), nullable=True),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        _stamp("created_at"), _stamp("updated_at"),
    )
    op.create_index("ix_bank_debit_accounts_company", "bank_debit_accounts", ["company_id", "legal_entity_id", "active"])
    op.create_table(
        "bank_file_formats",
        sa.Column("id", sa.Integer(), primary_key=True),
        _uuid(),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("based_on", sa.String(80), nullable=True),
        sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_by_name", sa.String(200), nullable=True),
        _stamp("created_at"), _stamp("updated_at"),
    )
    op.create_index("ix_bank_file_formats_company", "bank_file_formats", ["company_id", "active"])
    op.create_table(
        "bank_file_batches",
        sa.Column("id", sa.Integer(), primary_key=True),
        _uuid(),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("pay_run_id", sa.Integer(), sa.ForeignKey("pay_runs.id"), nullable=True),
        sa.Column("debit_account_id", sa.Integer(), sa.ForeignKey("bank_debit_accounts.id"), nullable=True),
        sa.Column("legal_entity_id", sa.Integer(), nullable=True),
        sa.Column("format_key", sa.String(80), nullable=False),
        sa.Column("format_name", sa.String(150), nullable=False),
        sa.Column("txn_group", sa.String(20), server_default=sa.text("'all'"), nullable=False),
        sa.Column("file_name", sa.String(200), nullable=False),
        sa.Column("content_type", sa.String(80), server_default=sa.text("'text/plain'"), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("record_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("total_amount", sa.Numeric(16, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("value_date", sa.Date(), nullable=True),
        sa.Column("released_only", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("debit_account_masked", sa.String(40), nullable=True),
        sa.Column("payslip_ids", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("left_out", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'generated'"), nullable=False),
        sa.Column("download_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_downloaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_name", sa.String(200), nullable=True),
        _stamp("created_at"),
    )
    op.create_index("ix_bank_file_batches_company_run", "bank_file_batches", ["company_id", "pay_run_id", "created_at"])
    for t in TABLES:
        for sql in enable_tenant_rls_sql(t):
            op.execute(sql)


def downgrade() -> None:
    for t in reversed(TABLES):
        for sql in disable_tenant_rls_sql(t):
            op.execute(sql)
        op.drop_table(t)
