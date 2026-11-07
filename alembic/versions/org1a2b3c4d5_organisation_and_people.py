"""organisation structure and full employee record

Revision ID: org1a2b3c4d5
Revises: w8x9y0z1a2b3
Create Date: 2026-11-07 02:02:07.747237
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision: str = 'org1a2b3c4d5'
down_revision: Union[str, None] = 'w8x9y0z1a2b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ['legal_entities', 'regions', 'cost_centres', 'locations', 'grades', 'teams', 'team_members', 'tags', 'tag_links', 'skills', 'employee_skills', 'saved_views', 'employee_files', 'employee_documents', 'employee_emergency_contacts', 'employee_family_members', 'employee_nominees', 'employee_bank_accounts', 'employee_education', 'employee_previous_employment', 'employee_job_history']


def upgrade() -> None:
    op.create_table('legal_entities',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('legal_name', sa.String(length=250), nullable=True),
    sa.Column('code', sa.String(length=20), nullable=True),
    sa.Column('pan', sa.String(length=10), nullable=True),
    sa.Column('tan', sa.String(length=10), nullable=True),
    sa.Column('cin', sa.String(length=21), nullable=True),
    sa.Column('gstin', sa.String(length=15), nullable=True),
    sa.Column('pf_code', sa.String(length=30), nullable=True),
    sa.Column('esi_code', sa.String(length=20), nullable=True),
    sa.Column('lin', sa.String(length=20), nullable=True),
    sa.Column('incorporation_date', sa.Date(), nullable=True),
    sa.Column('address_line1', sa.String(length=250), nullable=True),
    sa.Column('address_line2', sa.String(length=250), nullable=True),
    sa.Column('city', sa.String(length=100), nullable=True),
    sa.Column('state_id', sa.Integer(), nullable=True),
    sa.Column('pincode', sa.String(length=6), nullable=True),
    sa.Column('email', sa.String(length=255), nullable=True),
    sa.Column('phone', sa.String(length=20), nullable=True),
    sa.Column('registrations', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('bank_accounts', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('signatory', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('is_primary', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['state_id'], ['master_values.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_legal_entities_company', 'legal_entities', ['company_id', 'active'], unique=False)
    op.create_table('regions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=150), nullable=False),
    sa.Column('code', sa.String(length=20), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('head_employee_id', sa.Integer(), nullable=True),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['head_employee_id'], ['employees.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_regions_company', 'regions', ['company_id', 'active'], unique=False)
    op.create_table('cost_centres',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=150), nullable=False),
    sa.Column('code', sa.String(length=30), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('legal_entity_id', sa.Integer(), nullable=True),
    sa.Column('owner_employee_id', sa.Integer(), nullable=True),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['legal_entity_id'], ['legal_entities.id'], ),
    sa.ForeignKeyConstraint(['owner_employee_id'], ['employees.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_cost_centres_company', 'cost_centres', ['company_id', 'active'], unique=False)
    op.create_table('locations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=150), nullable=False),
    sa.Column('code', sa.String(length=20), nullable=True),
    sa.Column('kind', sa.String(length=20), server_default=sa.text("'branch'"), nullable=False),
    sa.Column('region_id', sa.Integer(), nullable=True),
    sa.Column('legal_entity_id', sa.Integer(), nullable=True),
    sa.Column('address_line1', sa.String(length=250), nullable=True),
    sa.Column('address_line2', sa.String(length=250), nullable=True),
    sa.Column('city', sa.String(length=100), nullable=True),
    sa.Column('state_id', sa.Integer(), nullable=True),
    sa.Column('pincode', sa.String(length=6), nullable=True),
    sa.Column('timezone', sa.String(length=64), nullable=True),
    sa.Column('latitude', sa.Numeric(precision=9, scale=6), nullable=True),
    sa.Column('longitude', sa.Numeric(precision=9, scale=6), nullable=True),
    sa.Column('radius_m', sa.Integer(), nullable=True),
    sa.Column('holiday_calendar_id', sa.Integer(), nullable=True),
    sa.Column('phone', sa.String(length=20), nullable=True),
    sa.Column('email', sa.String(length=255), nullable=True),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['legal_entity_id'], ['legal_entities.id'], ),
    sa.ForeignKeyConstraint(['region_id'], ['regions.id'], ),
    sa.ForeignKeyConstraint(['state_id'], ['master_values.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_locations_company', 'locations', ['company_id', 'active'], unique=False)
    op.create_index('ix_locations_company_entity', 'locations', ['company_id', 'legal_entity_id'], unique=False)
    op.create_index('ix_locations_company_region', 'locations', ['company_id', 'region_id'], unique=False)
    op.create_table('grades',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('code', sa.String(length=20), nullable=True),
    sa.Column('band', sa.String(length=60), nullable=True),
    sa.Column('rank', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('min_ctc', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('max_ctc', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_grades_company', 'grades', ['company_id', 'rank'], unique=False)
    op.create_table('teams',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=150), nullable=False),
    sa.Column('code', sa.String(length=20), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('lead_employee_id', sa.Integer(), nullable=True),
    sa.Column('department_id', sa.Integer(), nullable=True),
    sa.Column('color', sa.String(length=20), nullable=True),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['department_id'], ['departments.id'], ),
    sa.ForeignKeyConstraint(['lead_employee_id'], ['employees.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_teams_company', 'teams', ['company_id', 'active'], unique=False)
    op.create_table('team_members',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('team_id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('role', sa.String(length=60), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('team_id', 'employee_id', name='uq_team_members_team_employee')
    )
    op.create_index('ix_team_members_company_employee', 'team_members', ['company_id', 'employee_id'], unique=False)
    op.create_index('ix_team_members_company_team', 'team_members', ['company_id', 'team_id'], unique=False)
    op.create_table('tags',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('parent_id', sa.Integer(), nullable=True),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('color', sa.String(length=20), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('applies_to', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('sort_order', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['parent_id'], ['tags.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_tags_company_parent', 'tags', ['company_id', 'parent_id'], unique=False)
    op.create_table('tag_links',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('tag_id', sa.Integer(), nullable=False),
    sa.Column('entity_type', sa.String(length=50), nullable=False),
    sa.Column('entity_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['tag_id'], ['tags.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tag_id', 'entity_type', 'entity_id', name='uq_tag_links_tag_entity')
    )
    op.create_index('ix_tag_links_company_entity', 'tag_links', ['company_id', 'entity_type', 'entity_id'], unique=False)
    op.create_index('ix_tag_links_company_tag', 'tag_links', ['company_id', 'tag_id'], unique=False)
    op.create_table('skills',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('category_id', sa.Integer(), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['category_id'], ['master_values.id'], ),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_skills_company_category', 'skills', ['company_id', 'category_id'], unique=False)
    op.create_table('employee_skills',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('skill_id', sa.Integer(), nullable=False),
    sa.Column('level', sa.Integer(), server_default=sa.text('3'), nullable=False),
    sa.Column('years', sa.Numeric(precision=4, scale=1), nullable=True),
    sa.Column('verified', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('verified_by_name', sa.String(length=200), nullable=True),
    sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['skill_id'], ['skills.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('employee_id', 'skill_id', name='uq_employee_skills_employee_skill'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_employee_skills_company_employee', 'employee_skills', ['company_id', 'employee_id'], unique=False)
    op.create_index('ix_employee_skills_company_skill', 'employee_skills', ['company_id', 'skill_id'], unique=False)
    op.create_table('saved_views',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('entity', sa.String(length=40), nullable=False),
    sa.Column('owner_key', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('filters', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('columns', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('shared', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_saved_views_company_entity', 'saved_views', ['company_id', 'entity', 'owner_key'], unique=False)
    op.create_table('employee_files',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('filename', sa.String(length=255), nullable=False),
    sa.Column('content_type', sa.String(length=120), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('data', sa.LargeBinary(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_employee_files_company_employee', 'employee_files', ['company_id', 'employee_id'], unique=False)
    op.create_table('employee_documents',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('document_type_id', sa.Integer(), nullable=True),
    sa.Column('title', sa.String(length=200), nullable=True),
    sa.Column('document_number', sa.String(length=60), nullable=True),
    sa.Column('issue_date', sa.Date(), nullable=True),
    sa.Column('expiry_date', sa.Date(), nullable=True),
    sa.Column('issued_by', sa.String(length=150), nullable=True),
    sa.Column('file_id', sa.Integer(), nullable=True),
    sa.Column('verification_status', sa.String(length=20), server_default=sa.text("'pending'"), nullable=False),
    sa.Column('verified_by_name', sa.String(length=200), nullable=True),
    sa.Column('verified_by_employee_id', sa.Integer(), nullable=True),
    sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('remarks', sa.Text(), nullable=True),
    sa.Column('remind_days_before', sa.Integer(), server_default=sa.text('30'), nullable=False),
    sa.Column('reminder_sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('visible_to_employee', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['document_type_id'], ['master_values.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['file_id'], ['employee_files.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_employee_documents_company_employee', 'employee_documents', ['company_id', 'employee_id'], unique=False)
    op.create_index('ix_employee_documents_company_expiry', 'employee_documents', ['company_id', 'expiry_date'], unique=False)
    op.create_index('ix_employee_documents_company_status', 'employee_documents', ['company_id', 'verification_status'], unique=False)
    op.create_table('employee_emergency_contacts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('relation_id', sa.Integer(), nullable=True),
    sa.Column('phone', sa.String(length=20), nullable=False),
    sa.Column('alternate_phone', sa.String(length=20), nullable=True),
    sa.Column('email', sa.String(length=255), nullable=True),
    sa.Column('address', sa.Text(), nullable=True),
    sa.Column('is_primary', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['relation_id'], ['master_values.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_employee_emergency_contacts_company_employee', 'employee_emergency_contacts', ['company_id', 'employee_id'], unique=False)
    op.create_table('employee_family_members',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('relation_id', sa.Integer(), nullable=True),
    sa.Column('gender_id', sa.Integer(), nullable=True),
    sa.Column('date_of_birth', sa.Date(), nullable=True),
    sa.Column('occupation', sa.String(length=120), nullable=True),
    sa.Column('phone', sa.String(length=20), nullable=True),
    sa.Column('is_dependent', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('lives_with_employee', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['gender_id'], ['master_values.id'], ),
    sa.ForeignKeyConstraint(['relation_id'], ['master_values.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_employee_family_members_company_employee', 'employee_family_members', ['company_id', 'employee_id'], unique=False)
    op.create_table('employee_nominees',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('family_member_id', sa.Integer(), nullable=True),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('relation_id', sa.Integer(), nullable=True),
    sa.Column('date_of_birth', sa.Date(), nullable=True),
    sa.Column('share_percent', sa.Numeric(precision=5, scale=2), nullable=False),
    sa.Column('guardian_name', sa.String(length=200), nullable=True),
    sa.Column('address', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['family_member_id'], ['employee_family_members.id'], ),
    sa.ForeignKeyConstraint(['relation_id'], ['master_values.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_employee_nominees_company_employee', 'employee_nominees', ['company_id', 'employee_id', 'kind'], unique=False)
    op.create_table('employee_bank_accounts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('bank_id', sa.Integer(), nullable=True),
    sa.Column('account_holder', sa.String(length=200), nullable=False),
    sa.Column('account_number', sa.String(length=20), nullable=False),
    sa.Column('ifsc', sa.String(length=11), nullable=False),
    sa.Column('branch', sa.String(length=150), nullable=True),
    sa.Column('account_type', sa.String(length=20), server_default=sa.text("'savings'"), nullable=False),
    sa.Column('is_primary', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('verified', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['bank_id'], ['master_values.id'], ),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_employee_bank_accounts_company_employee', 'employee_bank_accounts', ['company_id', 'employee_id'], unique=False)
    op.create_table('employee_education',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('level_id', sa.Integer(), nullable=True),
    sa.Column('degree', sa.String(length=150), nullable=False),
    sa.Column('specialisation', sa.String(length=150), nullable=True),
    sa.Column('institution', sa.String(length=200), nullable=True),
    sa.Column('university', sa.String(length=200), nullable=True),
    sa.Column('start_year', sa.Integer(), nullable=True),
    sa.Column('end_year', sa.Integer(), nullable=True),
    sa.Column('score', sa.String(length=30), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['level_id'], ['master_values.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_employee_education_company_employee', 'employee_education', ['company_id', 'employee_id'], unique=False)
    op.create_table('employee_previous_employment',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('company_name', sa.String(length=200), nullable=False),
    sa.Column('designation', sa.String(length=150), nullable=True),
    sa.Column('from_date', sa.Date(), nullable=True),
    sa.Column('to_date', sa.Date(), nullable=True),
    sa.Column('last_ctc', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('reason_for_leaving', sa.String(length=300), nullable=True),
    sa.Column('is_relevant', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('financial_year', sa.String(length=7), nullable=True),
    sa.Column('fy_taxable_income', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('fy_tds', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('fy_pf', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('fy_pt', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('previous_pf_member_id', sa.String(length=30), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_employee_previous_employment_company_employee', 'employee_previous_employment', ['company_id', 'employee_id'], unique=False)
    op.create_table('employee_job_history',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('change_type', sa.String(length=30), nullable=False),
    sa.Column('effective_date', sa.Date(), nullable=False),
    sa.Column('changes', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('reason', sa.String(length=300), nullable=True),
    sa.Column('remarks', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'applied'"), nullable=False),
    sa.Column('applied_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by_name', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_employee_job_history_company_employee', 'employee_job_history', ['company_id', 'employee_id', 'effective_date'], unique=False)
    op.create_index('ix_employee_job_history_company_status', 'employee_job_history', ['company_id', 'status', 'effective_date'], unique=False)
    op.add_column('departments', sa.Column('parent_id', sa.Integer(), nullable=True))
    op.add_column('departments', sa.Column('cost_centre_id', sa.Integer(), nullable=True))
    op.add_column('departments', sa.Column('location_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_departments_cost_centre_id', 'departments', 'cost_centres', ['cost_centre_id'], ['id'])
    op.create_foreign_key('fk_departments_location_id', 'departments', 'locations', ['location_id'], ['id'])
    op.create_foreign_key('fk_departments_parent_id', 'departments', 'departments', ['parent_id'], ['id'])
    op.add_column('designations', sa.Column('code', sa.String(length=20), nullable=True))
    op.add_column('designations', sa.Column('description', sa.Text(), nullable=True))
    op.add_column('designations', sa.Column('grade_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_designations_grade_id', 'designations', 'grades', ['grade_id'], ['id'])
    op.add_column('employees', sa.Column('middle_name', sa.String(length=100), nullable=True))
    op.add_column('employees', sa.Column('preferred_name', sa.String(length=100), nullable=True))
    op.add_column('employees', sa.Column('gender_id', sa.Integer(), nullable=True))
    op.add_column('employees', sa.Column('date_of_birth', sa.Date(), nullable=True))
    op.add_column('employees', sa.Column('marital_status_id', sa.Integer(), nullable=True))
    op.add_column('employees', sa.Column('marriage_date', sa.Date(), nullable=True))
    op.add_column('employees', sa.Column('blood_group_id', sa.Integer(), nullable=True))
    op.add_column('employees', sa.Column('nationality_id', sa.Integer(), nullable=True))
    op.add_column('employees', sa.Column('father_name', sa.String(length=200), nullable=True))
    op.add_column('employees', sa.Column('spouse_name', sa.String(length=200), nullable=True))
    op.add_column('employees', sa.Column('personal_email', sa.String(length=255), nullable=True))
    op.add_column('employees', sa.Column('alternate_phone', sa.String(length=20), nullable=True))
    op.add_column('employees', sa.Column('work_phone', sa.String(length=20), nullable=True))
    op.add_column('employees', sa.Column('extension', sa.String(length=10), nullable=True))
    op.add_column('employees', sa.Column('current_address', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False))
    op.add_column('employees', sa.Column('permanent_address', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False))
    op.add_column('employees', sa.Column('pan', sa.String(length=10), nullable=True))
    op.add_column('employees', sa.Column('aadhaar_encrypted', sa.Text(), nullable=True))
    op.add_column('employees', sa.Column('aadhaar_hash', sa.String(length=64), nullable=True))
    op.add_column('employees', sa.Column('aadhaar_last4', sa.String(length=4), nullable=True))
    op.add_column('employees', sa.Column('uan', sa.String(length=12), nullable=True))
    op.add_column('employees', sa.Column('esi_number', sa.String(length=17), nullable=True))
    op.add_column('employees', sa.Column('passport_number', sa.String(length=20), nullable=True))
    op.add_column('employees', sa.Column('passport_expiry', sa.Date(), nullable=True))
    op.add_column('employees', sa.Column('driving_licence', sa.String(length=20), nullable=True))
    op.add_column('employees', sa.Column('driving_licence_expiry', sa.Date(), nullable=True))
    op.add_column('employees', sa.Column('legal_entity_id', sa.Integer(), nullable=True))
    op.add_column('employees', sa.Column('location_id', sa.Integer(), nullable=True))
    op.add_column('employees', sa.Column('grade_id', sa.Integer(), nullable=True))
    op.add_column('employees', sa.Column('cost_centre_id', sa.Integer(), nullable=True))
    op.add_column('employees', sa.Column('employment_type_id', sa.Integer(), nullable=True))
    op.add_column('employees', sa.Column('status', sa.String(length=20), server_default=sa.text("'confirmed'"), nullable=False))
    op.add_column('employees', sa.Column('employee_status_id', sa.Integer(), nullable=True))
    op.add_column('employees', sa.Column('date_of_joining', sa.Date(), nullable=True))
    op.add_column('employees', sa.Column('probation_end_date', sa.Date(), nullable=True))
    op.add_column('employees', sa.Column('confirmation_date', sa.Date(), nullable=True))
    op.add_column('employees', sa.Column('notice_period_days', sa.Integer(), nullable=True))
    op.add_column('employees', sa.Column('resignation_date', sa.Date(), nullable=True))
    op.add_column('employees', sa.Column('exit_date', sa.Date(), nullable=True))
    op.add_column('employees', sa.Column('secondary_manager_id', sa.Integer(), nullable=True))
    op.add_column('employees', sa.Column('work_mode', sa.String(length=20), nullable=True))
    op.add_column('employees', sa.Column('rehire_count', sa.Integer(), server_default=sa.text('0'), nullable=False))
    op.add_column('employees', sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('employees', sa.Column('archived_by', sa.String(length=200), nullable=True))
    op.create_index('ix_employees_company_aadhaar', 'employees', ['company_id', 'aadhaar_hash'], unique=False)
    op.create_index('ix_employees_company_entity', 'employees', ['company_id', 'legal_entity_id'], unique=False)
    op.create_index('ix_employees_company_location', 'employees', ['company_id', 'location_id'], unique=False)
    op.create_index('ix_employees_company_pan', 'employees', ['company_id', 'pan'], unique=False)
    op.create_index('ix_employees_company_status', 'employees', ['company_id', 'status'], unique=False)
    op.create_index('ix_employees_company_uan', 'employees', ['company_id', 'uan'], unique=False)
    op.create_foreign_key('fk_employees_cost_centre_id', 'employees', 'cost_centres', ['cost_centre_id'], ['id'])
    op.create_foreign_key('fk_employees_nationality_id', 'employees', 'master_values', ['nationality_id'], ['id'])
    op.create_foreign_key('fk_employees_employment_type_id', 'employees', 'master_values', ['employment_type_id'], ['id'])
    op.create_foreign_key('fk_employees_location_id', 'employees', 'locations', ['location_id'], ['id'])
    op.create_foreign_key('fk_employees_employee_status_id', 'employees', 'master_values', ['employee_status_id'], ['id'])
    op.create_foreign_key('fk_employees_gender_id', 'employees', 'master_values', ['gender_id'], ['id'])
    op.create_foreign_key('fk_employees_legal_entity_id', 'employees', 'legal_entities', ['legal_entity_id'], ['id'])
    op.create_foreign_key('fk_employees_blood_group_id', 'employees', 'master_values', ['blood_group_id'], ['id'])
    op.create_foreign_key('fk_employees_marital_status_id', 'employees', 'master_values', ['marital_status_id'], ['id'])
    op.create_foreign_key('fk_employees_grade_id', 'employees', 'grades', ['grade_id'], ['id'])
    op.create_foreign_key('fk_employees_secondary_manager_id', 'employees', 'employees', ['secondary_manager_id'], ['id'], ondelete='SET NULL')

    for table in TABLES:
        for sql in enable_tenant_rls_sql(table):
            op.execute(sql)
    # Existing people: inactive ones have left; joining date defaults to when they were added.
    op.execute("UPDATE employees SET status = 'exited' WHERE active = false")
    op.execute("UPDATE employees SET date_of_joining = (created_at AT TIME ZONE 'Asia/Kolkata')::date WHERE date_of_joining IS NULL")


EMPLOYEE_COLUMNS = [
    "middle_name", "preferred_name", "gender_id", "date_of_birth", "marital_status_id", "marriage_date", "blood_group_id",
    "nationality_id", "father_name", "spouse_name", "personal_email", "alternate_phone", "work_phone", "extension",
    "current_address", "permanent_address", "pan", "aadhaar_encrypted", "aadhaar_hash", "aadhaar_last4", "uan",
    "esi_number", "passport_number", "passport_expiry", "driving_licence", "driving_licence_expiry", "legal_entity_id",
    "location_id", "grade_id", "cost_centre_id", "employment_type_id", "status", "employee_status_id", "date_of_joining",
    "probation_end_date", "confirmation_date", "notice_period_days", "resignation_date", "exit_date",
    "secondary_manager_id", "work_mode", "rehire_count", "archived_at", "archived_by",
]


def downgrade() -> None:
    for col in EMPLOYEE_COLUMNS:
        op.drop_column("employees", col)
    for col in ("parent_id", "cost_centre_id", "location_id"):
        op.drop_column("departments", col)
    for col in ("code", "description", "grade_id"):
        op.drop_column("designations", col)
    for table in reversed(TABLES):
        for sql in disable_tenant_rls_sql(table):
            op.execute(sql)
        op.drop_table(table)
