"""Positions, headcount plans and position requests; succession plans and talent pools;
internal job postings and applications

Revision ID: pos1a2b3c4d5
Revises: ig1a2b3c4d5e
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision = "pos1a2b3c4d5"
down_revision = "ig1a2b3c4d5e"
branch_labels = None
depends_on = None

TABLES = ("position_settings", "talent_pools", "talent_pool_members", "headcount_plans", "positions",
          "internal_postings", "position_requests", "position_seats", "succession_plans", "internal_applications",
          "succession_candidates", "development_actions")


def upgrade() -> None:
    op.create_table('position_settings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', name='uq_position_settings_company')
    )
    op.create_table('talent_pools',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=150), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('manager_employee_id', sa.Integer(), nullable=True),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['manager_employee_id'], ['employees.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_talent_pools_company', 'talent_pools', ['company_id', 'active'], unique=False)
    op.create_table('talent_pool_members',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('pool_id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('note', sa.String(length=500), nullable=True),
    sa.Column('added_by_name', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['pool_id'], ['talent_pools.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'pool_id', 'employee_id', name='uq_talent_pool_members'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_talent_pool_members_company_employee', 'talent_pool_members', ['company_id', 'employee_id'], unique=False)
    op.create_table('headcount_plans',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('department_id', sa.Integer(), nullable=False),
    sa.Column('financial_year', sa.String(length=9), nullable=False),
    sa.Column('planned_headcount', sa.Integer(), nullable=False),
    sa.Column('approved_headcount', sa.Integer(), nullable=True),
    sa.Column('budget', sa.Numeric(precision=16, scale=2), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'draft'"), nullable=False),
    sa.Column('workflow_instance_id', sa.Integer(), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decision_note', sa.String(length=500), nullable=True),
    sa.Column('created_by_name', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['department_id'], ['departments.id'], ),
    sa.ForeignKeyConstraint(['workflow_instance_id'], ['workflow_instances.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'department_id', 'financial_year', name='uq_headcount_plans_dept_year'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_headcount_plans_company_year', 'headcount_plans', ['company_id', 'financial_year'], unique=False)
    op.create_table('positions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=40), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('designation_id', sa.Integer(), nullable=True),
    sa.Column('department_id', sa.Integer(), nullable=True),
    sa.Column('location_id', sa.Integer(), nullable=True),
    sa.Column('grade_id', sa.Integer(), nullable=True),
    sa.Column('cost_centre_id', sa.Integer(), nullable=True),
    sa.Column('parent_id', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'approved'"), nullable=False),
    sa.Column('headcount', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('fte', sa.Numeric(precision=5, scale=2), server_default=sa.text('1'), nullable=False),
    sa.Column('salary_min', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('salary_max', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('is_critical', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('skills', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('requisition_ids', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('closed_reason', sa.String(length=300), nullable=True),
    sa.Column('created_by_name', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['cost_centre_id'], ['cost_centres.id'], ),
    sa.ForeignKeyConstraint(['department_id'], ['departments.id'], ),
    sa.ForeignKeyConstraint(['designation_id'], ['designations.id'], ),
    sa.ForeignKeyConstraint(['grade_id'], ['grades.id'], ),
    sa.ForeignKeyConstraint(['location_id'], ['locations.id'], ),
    sa.ForeignKeyConstraint(['parent_id'], ['positions.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'code', name='uq_positions_company_code'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_positions_company_department', 'positions', ['company_id', 'department_id', 'status'], unique=False)
    op.create_index('ix_positions_company_parent', 'positions', ['company_id', 'parent_id'], unique=False)
    op.create_table('internal_postings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('number', sa.String(length=40), nullable=True),
    sa.Column('kind', sa.String(length=10), server_default=sa.text("'job'"), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('requisition_id', sa.Integer(), nullable=True),
    sa.Column('position_id', sa.Integer(), nullable=True),
    sa.Column('department_id', sa.Integer(), nullable=True),
    sa.Column('designation_id', sa.Integer(), nullable=True),
    sa.Column('location_id', sa.Integer(), nullable=True),
    sa.Column('skills', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('openings', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('duration_weeks', sa.Integer(), nullable=True),
    sa.Column('hours_per_week', sa.Integer(), nullable=True),
    sa.Column('work_mode', sa.String(length=20), nullable=True),
    sa.Column('starts_on', sa.Date(), nullable=True),
    sa.Column('closes_on', sa.Date(), nullable=True),
    sa.Column('hiring_manager_id', sa.Integer(), nullable=True),
    sa.Column('needs_manager_consent', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'draft'"), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by_name', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['department_id'], ['departments.id'], ),
    sa.ForeignKeyConstraint(['designation_id'], ['designations.id'], ),
    sa.ForeignKeyConstraint(['hiring_manager_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['location_id'], ['locations.id'], ),
    sa.ForeignKeyConstraint(['position_id'], ['positions.id'], ),
    sa.ForeignKeyConstraint(['requisition_id'], ['requisitions.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_internal_postings_company_status', 'internal_postings', ['company_id', 'status', 'kind'], unique=False)
    op.create_table('position_requests',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('number', sa.String(length=40), nullable=True),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('position_id', sa.Integer(), nullable=True),
    sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'pending'"), nullable=False),
    sa.Column('workflow_instance_id', sa.Integer(), nullable=True),
    sa.Column('requested_by_employee_id', sa.Integer(), nullable=True),
    sa.Column('requested_by_name', sa.String(length=200), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decision_note', sa.String(length=500), nullable=True),
    sa.Column('result_position_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['position_id'], ['positions.id'], ),
    sa.ForeignKeyConstraint(['requested_by_employee_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['result_position_id'], ['positions.id'], ),
    sa.ForeignKeyConstraint(['workflow_instance_id'], ['workflow_instances.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_position_requests_company_status', 'position_requests', ['company_id', 'status'], unique=False)
    op.create_table('position_seats',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('position_id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('start_date', sa.Date(), nullable=False),
    sa.Column('end_date', sa.Date(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'active'"), nullable=False),
    sa.Column('source', sa.String(length=20), server_default=sa.text("'manual'"), nullable=False),
    sa.Column('job_change_id', sa.Integer(), nullable=True),
    sa.Column('ended_reason', sa.String(length=300), nullable=True),
    sa.Column('created_by_name', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['job_change_id'], ['employee_job_history.id'], ),
    sa.ForeignKeyConstraint(['position_id'], ['positions.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_position_seats_company_employee', 'position_seats', ['company_id', 'employee_id', 'status'], unique=False)
    op.create_index('ix_position_seats_company_position', 'position_seats', ['company_id', 'position_id', 'status'], unique=False)
    op.create_table('succession_plans',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('position_id', sa.Integer(), nullable=False),
    sa.Column('plan_manager_id', sa.Integer(), nullable=True),
    sa.Column('risk_of_loss', sa.String(length=10), server_default=sa.text("'medium'"), nullable=False),
    sa.Column('impact_of_loss', sa.String(length=10), server_default=sa.text("'medium'"), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('review_on', sa.Date(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'active'"), nullable=False),
    sa.Column('created_by_name', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['plan_manager_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['position_id'], ['positions.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_succession_plans_company_position', 'succession_plans', ['company_id', 'position_id', 'status'], unique=False)
    op.create_table('internal_applications',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('posting_id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('manager_employee_id', sa.Integer(), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('match_score', sa.Numeric(precision=5, scale=1), nullable=True),
    sa.Column('match_detail', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'submitted'"), nullable=False),
    sa.Column('workflow_instance_id', sa.Integer(), nullable=True),
    sa.Column('manager_note', sa.String(length=500), nullable=True),
    sa.Column('decision_note', sa.String(length=500), nullable=True),
    sa.Column('effective_date', sa.Date(), nullable=True),
    sa.Column('job_change_id', sa.Integer(), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['job_change_id'], ['employee_job_history.id'], ),
    sa.ForeignKeyConstraint(['manager_employee_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['posting_id'], ['internal_postings.id'], ),
    sa.ForeignKeyConstraint(['workflow_instance_id'], ['workflow_instances.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_internal_applications_company_employee', 'internal_applications', ['company_id', 'employee_id'], unique=False)
    op.create_index('ix_internal_applications_company_posting', 'internal_applications', ['company_id', 'posting_id', 'status'], unique=False)
    op.create_table('succession_candidates',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('plan_id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('readiness', sa.String(length=20), server_default=sa.text("'one_two_years'"), nullable=False),
    sa.Column('rank', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('strengths', sa.Text(), nullable=True),
    sa.Column('gaps', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'active'"), nullable=False),
    sa.Column('added_by_name', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['plan_id'], ['succession_plans.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_succession_candidates_company_employee', 'succession_candidates', ['company_id', 'employee_id'], unique=False)
    op.create_index('ix_succession_candidates_company_plan', 'succession_candidates', ['company_id', 'plan_id'], unique=False)
    op.create_table('development_actions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('candidate_id', sa.Integer(), nullable=True),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('title', sa.String(length=250), nullable=False),
    sa.Column('course_id', sa.Integer(), nullable=True),
    sa.Column('objective_id', sa.Integer(), nullable=True),
    sa.Column('mentor_id', sa.Integer(), nullable=True),
    sa.Column('due_date', sa.Date(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'planned'"), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_by_name', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['candidate_id'], ['succession_candidates.id'], ),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['course_id'], ['learning_courses.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['mentor_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['objective_id'], ['perf_objectives.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_development_actions_company_candidate', 'development_actions', ['company_id', 'candidate_id'], unique=False)
    op.create_index('ix_development_actions_company_employee', 'development_actions', ['company_id', 'employee_id'], unique=False)
    for t in TABLES:
        for sql in enable_tenant_rls_sql(t):
            op.execute(sql)


def downgrade() -> None:
    for t in reversed(TABLES):
        for sql in disable_tenant_rls_sql(t):
            op.execute(sql)
        op.drop_table(t)
