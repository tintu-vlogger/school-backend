"""initial schema: companies, employees, designations, roster, audit and notifications

Revision ID: 3d053072cdee
Revises:
Create Date: 2026-03-21 06:52:34.372529
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '3d053072cdee'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('companies',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_name', sa.String(length=255), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('password', sa.String(length=255), nullable=False),
    sa.Column('password_reset_token', sa.String(length=255), nullable=True),
    sa.Column('phone', sa.String(length=20), nullable=True),
    sa.Column('address', sa.Text(), nullable=True),
    sa.Column('city', sa.String(length=100), nullable=True),
    sa.Column('state', sa.String(length=100), nullable=True),
    sa.Column('pincode', sa.String(length=10), nullable=True),
    sa.Column('country', sa.String(length=100), nullable=True),
    sa.Column('timezone', sa.String(length=50), server_default=sa.text("'Asia/Kolkata'"), nullable=False),
    sa.Column('currency', sa.String(length=10), server_default=sa.text("'INR'"), nullable=False),
    sa.Column('logo_url', sa.String(length=500), nullable=True),
    sa.Column('industry', sa.String(length=100), nullable=True),
    sa.Column('employee_count_band', sa.String(length=20), nullable=True),
    sa.Column('pan', sa.String(length=10), nullable=True),
    sa.Column('tan', sa.String(length=10), nullable=True),
    sa.Column('gstin', sa.String(length=15), nullable=True),
    sa.Column('cin', sa.String(length=21), nullable=True),
    sa.Column('subscription_plan', sa.String(length=20), server_default=sa.text("'trial'"), nullable=False),
    sa.Column('trial_ends_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email'),
    sa.UniqueConstraint('uuid')
    )
    op.create_table('audit_logs',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('actor_type', sa.String(length=20), nullable=False),
    sa.Column('actor_id', sa.Integer(), nullable=True),
    sa.Column('action', sa.String(length=100), nullable=False),
    sa.Column('entity_type', sa.String(length=50), nullable=False),
    sa.Column('entity_id', sa.Integer(), nullable=True),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('designations',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('designation_name', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_table('whatsapp_connections',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('connection_name', sa.String(length=100), nullable=False),
    sa.Column('instance_name', sa.String(length=100), nullable=False),
    sa.Column('phone_number', sa.String(length=20), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_table('employees',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('designation_id', sa.Integer(), nullable=True),
    sa.Column('employee_code', sa.String(length=30), nullable=True),
    sa.Column('first_name', sa.String(length=100), nullable=False),
    sa.Column('last_name', sa.String(length=100), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('phone', sa.String(length=20), nullable=True),
    sa.Column('password', sa.String(length=255), nullable=False),
    sa.Column('password_reset_token', sa.String(length=255), nullable=True),
    sa.Column('role', sa.String(length=20), server_default=sa.text("'employee'"), nullable=False),
    sa.Column('profile_image_url', sa.String(length=500), nullable=True),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['designation_id'], ['designations.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'email', name='uq_employees_company_email'),
    sa.UniqueConstraint('uuid')
    )
    op.create_table('roster_entries',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('timing_override', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('note', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('employee_id', 'date', name='uq_roster_entries_employee_date')
    )
    op.create_table('employee_settings',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('employee_id')
    )
    op.create_table('notifications',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('channel', sa.String(length=20), nullable=False),
    sa.Column('message_type', sa.String(length=50), nullable=False),
    sa.Column('message_content', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )


def downgrade() -> None:
    op.drop_table('notifications')
    op.drop_table('employee_settings')
    op.drop_table('roster_entries')
    op.drop_table('employees')
    op.drop_table('whatsapp_connections')
    op.drop_table('designations')
    op.drop_table('audit_logs')
    op.drop_table('companies')
