"""fees and finance: heads, structures, concessions, invoices, payments, refunds, online orders, cash book

Revision ID: fe1a2b3c4d5e
Revises: sc1b2c3d4e5f
Create Date: 2026-11-07 21:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql, ensure_app_role_sql

revision: str = "fe1a2b3c4d5e"
down_revision: Union[str, None] = "sc1b2c3d4e5f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ["fee_heads", "fee_categories", "fee_structures", "fee_structure_items", "fee_instalments", "fee_opt_ins",
          "fee_concession_types", "fee_concessions", "fee_late_rules", "fee_invoices", "fee_invoice_lines",
          "fee_adjustments", "fee_payments", "fee_payment_modes", "fee_allocations", "fee_wallet_entries",
          "fee_refunds", "fee_gateway_settings", "fee_online_orders", "fee_reminder_logs", "fee_ledger_categories",
          "fee_ledger_entries"]


def upgrade() -> None:
    op.create_table('fee_categories',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('code', sa.String(length=30), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'name', name='uq_fee_categories_name'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_fee_categories_company', 'fee_categories', ['company_id'], unique=False)
    op.create_table('fee_gateway_settings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('provider', sa.String(length=20), server_default=sa.text("'test'"), nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('test_mode', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('secrets_encrypted', sa.Text(), nullable=True),
    sa.Column('secret_hints', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('updated_by_name', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', name='uq_fee_gateway_settings_company')
    )
    op.create_table('fee_heads',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('code', sa.String(length=30), nullable=False),
    sa.Column('kind', sa.String(length=20), server_default=sa.text("'recurring'"), nullable=False),
    sa.Column('tax_rate', sa.Numeric(precision=5, scale=2), nullable=True),
    sa.Column('tax_code', sa.String(length=20), nullable=True),
    sa.Column('ledger_account', sa.String(length=120), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('is_system', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('sort_order', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'code', name='uq_fee_heads_code'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_fee_heads_company', 'fee_heads', ['company_id', 'sort_order'], unique=False)
    op.create_table('fee_ledger_categories',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'kind', 'name', name='uq_fee_ledger_categories_name'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_fee_ledger_categories_company', 'fee_ledger_categories', ['company_id'], unique=False)
    op.create_table('fee_concession_types',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('reason', sa.String(length=30), server_default=sa.text("'other'"), nullable=False),
    sa.Column('kind', sa.String(length=10), server_default=sa.text("'percent'"), nullable=False),
    sa.Column('value', sa.Numeric(precision=14, scale=2), server_default=sa.text('0'), nullable=False),
    sa.Column('head_id', sa.Integer(), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['head_id'], ['fee_heads.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'name', name='uq_fee_concession_types_name'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_fee_concession_types_company', 'fee_concession_types', ['company_id'], unique=False)
    op.create_table('fee_ledger_entries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('number', sa.String(length=60), nullable=True),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('category_id', sa.Integer(), nullable=True),
    sa.Column('entry_date', sa.Date(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('mode', sa.String(length=20), server_default=sa.text("'cash'"), nullable=False),
    sa.Column('reference', sa.String(length=120), nullable=True),
    sa.Column('party', sa.String(length=200), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('attachments', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'active'"), nullable=False),
    sa.Column('cancel_reason', sa.Text(), nullable=True),
    sa.Column('created_by_name', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['category_id'], ['fee_ledger_categories.id'], ),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_fee_ledger_entries_company', 'fee_ledger_entries', ['company_id', 'entry_date'], unique=False)
    op.create_table('fee_structures',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('session_id', sa.Integer(), nullable=False),
    sa.Column('program_id', sa.Integer(), nullable=True),
    sa.Column('category_id', sa.Integer(), nullable=True),
    sa.Column('plan', sa.String(length=20), server_default=sa.text("'quarterly'"), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['category_id'], ['fee_categories.id'], ),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['program_id'], ['academic_programs.id'], ),
    sa.ForeignKeyConstraint(['session_id'], ['academic_sessions.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_fee_structures_company', 'fee_structures', ['company_id', 'session_id', 'program_id'], unique=False)
    op.create_table('fee_concessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('student_id', sa.Integer(), nullable=False),
    sa.Column('session_id', sa.Integer(), nullable=True),
    sa.Column('type_id', sa.Integer(), nullable=True),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('reason', sa.String(length=30), server_default=sa.text("'other'"), nullable=False),
    sa.Column('kind', sa.String(length=10), server_default=sa.text("'percent'"), nullable=False),
    sa.Column('value', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('head_id', sa.Integer(), nullable=True),
    sa.Column('valid_from', sa.Date(), nullable=True),
    sa.Column('valid_to', sa.Date(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'pending'"), nullable=False),
    sa.Column('auto', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('workflow_instance_id', sa.Integer(), nullable=True),
    sa.Column('requested_by_name', sa.String(length=200), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['head_id'], ['fee_heads.id'], ),
    sa.ForeignKeyConstraint(['session_id'], ['academic_sessions.id'], ),
    sa.ForeignKeyConstraint(['student_id'], ['students.id'], ),
    sa.ForeignKeyConstraint(['type_id'], ['fee_concession_types.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_fee_concessions_company', 'fee_concessions', ['company_id', 'student_id', 'status'], unique=False)
    op.create_table('fee_instalments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('structure_id', sa.Integer(), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('due_date', sa.Date(), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['structure_id'], ['fee_structures.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_fee_instalments_company', 'fee_instalments', ['company_id', 'structure_id'], unique=False)
    op.create_table('fee_late_rules',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('kind', sa.String(length=10), server_default=sa.text("'per_day'"), nullable=False),
    sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('grace_days', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('max_amount', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('structure_id', sa.Integer(), nullable=True),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['structure_id'], ['fee_structures.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_fee_late_rules_company', 'fee_late_rules', ['company_id'], unique=False)
    op.create_table('fee_opt_ins',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('student_id', sa.Integer(), nullable=False),
    sa.Column('session_id', sa.Integer(), nullable=False),
    sa.Column('head_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['head_id'], ['fee_heads.id'], ),
    sa.ForeignKeyConstraint(['session_id'], ['academic_sessions.id'], ),
    sa.ForeignKeyConstraint(['student_id'], ['students.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('student_id', 'session_id', 'head_id', name='uq_fee_opt_ins')
    )
    op.create_index('ix_fee_opt_ins_company', 'fee_opt_ins', ['company_id', 'student_id'], unique=False)
    op.create_table('fee_payments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('number', sa.String(length=60), nullable=True),
    sa.Column('student_id', sa.Integer(), nullable=False),
    sa.Column('paid_on', sa.Date(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('wallet_used', sa.Numeric(precision=14, scale=2), server_default=sa.text('0'), nullable=False),
    sa.Column('allocated', sa.Numeric(precision=14, scale=2), server_default=sa.text('0'), nullable=False),
    sa.Column('late_fee', sa.Numeric(precision=14, scale=2), server_default=sa.text('0'), nullable=False),
    sa.Column('advance', sa.Numeric(precision=14, scale=2), server_default=sa.text('0'), nullable=False),
    sa.Column('mode', sa.String(length=20), nullable=False),
    sa.Column('source', sa.String(length=20), server_default=sa.text("'counter'"), nullable=False),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'paid'"), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('collected_by_employee_id', sa.Integer(), nullable=True),
    sa.Column('collected_by_name', sa.String(length=200), nullable=True),
    sa.Column('cancel_reason', sa.Text(), nullable=True),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancelled_by_name', sa.String(length=200), nullable=True),
    sa.Column('online_order_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['collected_by_employee_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['student_id'], ['students.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_fee_payments_company_date', 'fee_payments', ['company_id', 'paid_on'], unique=False)
    op.create_index('ix_fee_payments_company_student', 'fee_payments', ['company_id', 'student_id'], unique=False)
    op.create_table('fee_structure_items',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('structure_id', sa.Integer(), nullable=False),
    sa.Column('head_id', sa.Integer(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('billing', sa.String(length=20), server_default=sa.text("'per_instalment'"), nullable=False),
    sa.Column('instalment_no', sa.Integer(), nullable=True),
    sa.Column('optional', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('sort_order', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['head_id'], ['fee_heads.id'], ),
    sa.ForeignKeyConstraint(['structure_id'], ['fee_structures.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_fee_structure_items_company', 'fee_structure_items', ['company_id', 'structure_id'], unique=False)
    op.create_table('fee_invoices',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('number', sa.String(length=60), nullable=True),
    sa.Column('student_id', sa.Integer(), nullable=False),
    sa.Column('session_id', sa.Integer(), nullable=True),
    sa.Column('program_id', sa.Integer(), nullable=True),
    sa.Column('section_id', sa.Integer(), nullable=True),
    sa.Column('structure_id', sa.Integer(), nullable=True),
    sa.Column('instalment_id', sa.Integer(), nullable=True),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('issue_date', sa.Date(), nullable=False),
    sa.Column('due_date', sa.Date(), nullable=False),
    sa.Column('gross', sa.Numeric(precision=14, scale=2), server_default=sa.text('0'), nullable=False),
    sa.Column('concession', sa.Numeric(precision=14, scale=2), server_default=sa.text('0'), nullable=False),
    sa.Column('tax', sa.Numeric(precision=14, scale=2), server_default=sa.text('0'), nullable=False),
    sa.Column('total', sa.Numeric(precision=14, scale=2), server_default=sa.text('0'), nullable=False),
    sa.Column('credited', sa.Numeric(precision=14, scale=2), server_default=sa.text('0'), nullable=False),
    sa.Column('paid', sa.Numeric(precision=14, scale=2), server_default=sa.text('0'), nullable=False),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'open'"), nullable=False),
    sa.Column('source', sa.String(length=20), server_default=sa.text("'structure'"), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('cancel_reason', sa.Text(), nullable=True),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancelled_by_name', sa.String(length=200), nullable=True),
    sa.Column('created_by_name', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['instalment_id'], ['fee_instalments.id'], ),
    sa.ForeignKeyConstraint(['program_id'], ['academic_programs.id'], ),
    sa.ForeignKeyConstraint(['section_id'], ['academic_sections.id'], ),
    sa.ForeignKeyConstraint(['session_id'], ['academic_sessions.id'], ),
    sa.ForeignKeyConstraint(['structure_id'], ['fee_structures.id'], ),
    sa.ForeignKeyConstraint(['student_id'], ['students.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_fee_invoices_company_due', 'fee_invoices', ['company_id', 'due_date'], unique=False)
    op.create_index('ix_fee_invoices_company_student', 'fee_invoices', ['company_id', 'student_id', 'status'], unique=False)
    op.create_index('ix_fee_invoices_instalment', 'fee_invoices', ['company_id', 'instalment_id'], unique=False)
    op.create_table('fee_online_orders',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('student_id', sa.Integer(), nullable=False),
    sa.Column('portal_account_id', sa.Integer(), nullable=True),
    sa.Column('invoice_ids', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('currency', sa.String(length=10), nullable=False),
    sa.Column('provider', sa.String(length=20), nullable=False),
    sa.Column('test_mode', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('provider_order_id', sa.String(length=120), nullable=True),
    sa.Column('provider_payment_id', sa.String(length=120), nullable=True),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'created'"), nullable=False),
    sa.Column('payment_id', sa.Integer(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['payment_id'], ['fee_payments.id'], ),
    sa.ForeignKeyConstraint(['student_id'], ['students.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_fee_online_orders_company', 'fee_online_orders', ['company_id', 'student_id'], unique=False)
    op.create_index('ix_fee_online_orders_provider', 'fee_online_orders', ['provider', 'provider_order_id'], unique=False)
    op.create_table('fee_payment_modes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('payment_id', sa.Integer(), nullable=False),
    sa.Column('mode', sa.String(length=20), nullable=False),
    sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('reference', sa.String(length=120), nullable=True),
    sa.Column('bank', sa.String(length=120), nullable=True),
    sa.Column('instrument_date', sa.Date(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'cleared'"), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['payment_id'], ['fee_payments.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_fee_payment_modes_company', 'fee_payment_modes', ['company_id', 'payment_id'], unique=False)
    op.create_table('fee_refunds',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('number', sa.String(length=60), nullable=True),
    sa.Column('student_id', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=20), server_default=sa.text("'wallet'"), nullable=False),
    sa.Column('payment_id', sa.Integer(), nullable=True),
    sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'pending'"), nullable=False),
    sa.Column('mode', sa.String(length=20), nullable=True),
    sa.Column('reference', sa.String(length=120), nullable=True),
    sa.Column('paid_on', sa.Date(), nullable=True),
    sa.Column('workflow_instance_id', sa.Integer(), nullable=True),
    sa.Column('requested_by_name', sa.String(length=200), nullable=True),
    sa.Column('paid_by_name', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['payment_id'], ['fee_payments.id'], ),
    sa.ForeignKeyConstraint(['student_id'], ['students.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_fee_refunds_company', 'fee_refunds', ['company_id', 'student_id', 'status'], unique=False)
    op.create_table('fee_wallet_entries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('student_id', sa.Integer(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('reason', sa.String(length=200), nullable=False),
    sa.Column('payment_id', sa.Integer(), nullable=True),
    sa.Column('refund_id', sa.Integer(), nullable=True),
    sa.Column('created_by_name', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['payment_id'], ['fee_payments.id'], ),
    sa.ForeignKeyConstraint(['student_id'], ['students.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_fee_wallet_entries_company', 'fee_wallet_entries', ['company_id', 'student_id'], unique=False)
    op.create_table('fee_adjustments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('invoice_id', sa.Integer(), nullable=False),
    sa.Column('number', sa.String(length=60), nullable=True),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('head_id', sa.Integer(), nullable=True),
    sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('created_by_name', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['head_id'], ['fee_heads.id'], ),
    sa.ForeignKeyConstraint(['invoice_id'], ['fee_invoices.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_fee_adjustments_company', 'fee_adjustments', ['company_id', 'invoice_id'], unique=False)
    op.create_table('fee_invoice_lines',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('invoice_id', sa.Integer(), nullable=False),
    sa.Column('head_id', sa.Integer(), nullable=False),
    sa.Column('description', sa.String(length=200), nullable=False),
    sa.Column('amount', sa.Numeric(precision=14, scale=2), server_default=sa.text('0'), nullable=False),
    sa.Column('concession', sa.Numeric(precision=14, scale=2), server_default=sa.text('0'), nullable=False),
    sa.Column('concession_note', sa.String(length=200), nullable=True),
    sa.Column('tax_rate', sa.Numeric(precision=5, scale=2), nullable=True),
    sa.Column('tax', sa.Numeric(precision=14, scale=2), server_default=sa.text('0'), nullable=False),
    sa.Column('net', sa.Numeric(precision=14, scale=2), server_default=sa.text('0'), nullable=False),
    sa.Column('credited', sa.Numeric(precision=14, scale=2), server_default=sa.text('0'), nullable=False),
    sa.Column('paid', sa.Numeric(precision=14, scale=2), server_default=sa.text('0'), nullable=False),
    sa.Column('refunded', sa.Numeric(precision=14, scale=2), server_default=sa.text('0'), nullable=False),
    sa.Column('kind', sa.String(length=20), server_default=sa.text("'fee'"), nullable=False),
    sa.Column('sort_order', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['head_id'], ['fee_heads.id'], ),
    sa.ForeignKeyConstraint(['invoice_id'], ['fee_invoices.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_fee_invoice_lines_company', 'fee_invoice_lines', ['company_id', 'invoice_id'], unique=False)
    op.create_index('ix_fee_invoice_lines_head', 'fee_invoice_lines', ['company_id', 'head_id'], unique=False)
    op.create_table('fee_reminder_logs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('student_id', sa.Integer(), nullable=False),
    sa.Column('invoice_id', sa.Integer(), nullable=True),
    sa.Column('key', sa.String(length=60), nullable=False),
    sa.Column('kind', sa.String(length=20), server_default=sa.text("'manual'"), nullable=False),
    sa.Column('recipients', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('sent_by_name', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['invoice_id'], ['fee_invoices.id'], ),
    sa.ForeignKeyConstraint(['student_id'], ['students.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('invoice_id', 'key', name='uq_fee_reminder_logs_key')
    )
    op.create_index('ix_fee_reminder_logs_company', 'fee_reminder_logs', ['company_id', 'invoice_id'], unique=False)
    op.create_table('fee_allocations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('payment_id', sa.Integer(), nullable=False),
    sa.Column('invoice_id', sa.Integer(), nullable=False),
    sa.Column('line_id', sa.Integer(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('reversed', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['invoice_id'], ['fee_invoices.id'], ),
    sa.ForeignKeyConstraint(['line_id'], ['fee_invoice_lines.id'], ),
    sa.ForeignKeyConstraint(['payment_id'], ['fee_payments.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_fee_allocations_company', 'fee_allocations', ['company_id', 'payment_id'], unique=False)
    op.create_index('ix_fee_allocations_invoice', 'fee_allocations', ['company_id', 'invoice_id'], unique=False)
    # One live invoice per learner and instalment (cancelled ones are kept for history).
    op.execute("CREATE UNIQUE INDEX uq_fee_invoices_live_instalment ON fee_invoices (student_id, instalment_id) "
               "WHERE instalment_id IS NOT NULL AND status <> 'cancelled'")
    for sql in ensure_app_role_sql():
        op.execute(sql)
    for t in TABLES:
        for sql in enable_tenant_rls_sql(t):
            op.execute(sql)


def downgrade() -> None:
    for t in TABLES:
        for sql in disable_tenant_rls_sql(t):
            op.execute(sql)
    op.execute("DROP INDEX IF EXISTS uq_fee_invoices_live_instalment")
    op.drop_index('ix_fee_allocations_invoice', table_name='fee_allocations')
    op.drop_index('ix_fee_allocations_company', table_name='fee_allocations')
    op.drop_table('fee_allocations')
    op.drop_index('ix_fee_reminder_logs_company', table_name='fee_reminder_logs')
    op.drop_table('fee_reminder_logs')
    op.drop_index('ix_fee_invoice_lines_head', table_name='fee_invoice_lines')
    op.drop_index('ix_fee_invoice_lines_company', table_name='fee_invoice_lines')
    op.drop_table('fee_invoice_lines')
    op.drop_index('ix_fee_adjustments_company', table_name='fee_adjustments')
    op.drop_table('fee_adjustments')
    op.drop_index('ix_fee_wallet_entries_company', table_name='fee_wallet_entries')
    op.drop_table('fee_wallet_entries')
    op.drop_index('ix_fee_refunds_company', table_name='fee_refunds')
    op.drop_table('fee_refunds')
    op.drop_index('ix_fee_payment_modes_company', table_name='fee_payment_modes')
    op.drop_table('fee_payment_modes')
    op.drop_index('ix_fee_online_orders_provider', table_name='fee_online_orders')
    op.drop_index('ix_fee_online_orders_company', table_name='fee_online_orders')
    op.drop_table('fee_online_orders')
    op.drop_index('ix_fee_invoices_instalment', table_name='fee_invoices')
    op.drop_index('ix_fee_invoices_company_student', table_name='fee_invoices')
    op.drop_index('ix_fee_invoices_company_due', table_name='fee_invoices')
    op.drop_table('fee_invoices')
    op.drop_index('ix_fee_structure_items_company', table_name='fee_structure_items')
    op.drop_table('fee_structure_items')
    op.drop_index('ix_fee_payments_company_student', table_name='fee_payments')
    op.drop_index('ix_fee_payments_company_date', table_name='fee_payments')
    op.drop_table('fee_payments')
    op.drop_index('ix_fee_opt_ins_company', table_name='fee_opt_ins')
    op.drop_table('fee_opt_ins')
    op.drop_index('ix_fee_late_rules_company', table_name='fee_late_rules')
    op.drop_table('fee_late_rules')
    op.drop_index('ix_fee_instalments_company', table_name='fee_instalments')
    op.drop_table('fee_instalments')
    op.drop_index('ix_fee_concessions_company', table_name='fee_concessions')
    op.drop_table('fee_concessions')
    op.drop_index('ix_fee_structures_company', table_name='fee_structures')
    op.drop_table('fee_structures')
    op.drop_index('ix_fee_ledger_entries_company', table_name='fee_ledger_entries')
    op.drop_table('fee_ledger_entries')
    op.drop_index('ix_fee_concession_types_company', table_name='fee_concession_types')
    op.drop_table('fee_concession_types')
    op.drop_index('ix_fee_ledger_categories_company', table_name='fee_ledger_categories')
    op.drop_table('fee_ledger_categories')
    op.drop_index('ix_fee_heads_company', table_name='fee_heads')
    op.drop_table('fee_heads')
    op.drop_table('fee_gateway_settings')
    op.drop_index('ix_fee_categories_company', table_name='fee_categories')
    op.drop_table('fee_categories')
