"""Attendance, shifts, rosters and timesheets

Revision ID: a7t1m2e3s4h5
Revises: w8x9y0z1a2b3
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision = "a7t1m2e3s4h5"
down_revision = "w8x9y0z1a2b3"
branch_labels = None
depends_on = None

TABLES = ['shifts', 'shift_patterns', 'attendance_rules', 'time_settings', 'attendance_geofences', 'attendance_devices', 'attendance_locks', 'night_shift_consents', 'work_arrangements', 'device_enrolments', 'device_sync_logs', 'attendance_requests', 'attendance_punches', 'attendance_days', 'attendance_adjustments', 'ts_clients', 'ts_projects', 'ts_tasks', 'ts_timesheets', 'ts_time_logs']


def upgrade():
    op.create_table('shifts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('code', sa.String(length=12), nullable=True),
    sa.Column('color', sa.String(length=20), server_default=sa.text("'indigo'"), nullable=False),
    sa.Column('start_time', sa.Time(), nullable=False),
    sa.Column('end_time', sa.Time(), nullable=False),
    sa.Column('breaks', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('grace_in_minutes', sa.Integer(), server_default=sa.text('10'), nullable=False),
    sa.Column('grace_out_minutes', sa.Integer(), server_default=sa.text('10'), nullable=False),
    sa.Column('half_day_hours', sa.Numeric(precision=4, scale=2), nullable=True),
    sa.Column('full_day_hours', sa.Numeric(precision=4, scale=2), nullable=True),
    sa.Column('is_flexible', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('core_start', sa.Time(), nullable=True),
    sa.Column('core_end', sa.Time(), nullable=True),
    sa.Column('overtime_after_minutes', sa.Integer(), server_default=sa.text('30'), nullable=False),
    sa.Column('allowance_amount', sa.Numeric(precision=10, scale=2), server_default=sa.text('0'), nullable=False),
    sa.Column('allowance_min_hours', sa.Numeric(precision=4, scale=2), nullable=True),
    sa.Column('weekly_offs', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[5, 6]'::jsonb"), nullable=False),
    sa.Column('is_default', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'name', name='uq_shifts_company_name'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_shifts_company_active', 'shifts', ['company_id', 'active'], unique=False)
    op.create_table('shift_patterns',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('slots', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_shift_patterns_company', 'shift_patterns', ['company_id', 'active'], unique=False)
    op.create_table('attendance_rules',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('lineage_id', sa.UUID(), nullable=False),
    sa.Column('version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('effective_from', sa.Date(), nullable=False),
    sa.Column('effective_to', sa.Date(), nullable=True),
    sa.Column('is_current', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('is_default', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('priority', sa.Integer(), server_default=sa.text('100'), nullable=False),
    sa.Column('applies_to', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('change_note', sa.String(length=300), nullable=True),
    sa.Column('created_by_name', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_attendance_rules_company_lineage', 'attendance_rules', ['company_id', 'lineage_id', 'effective_from'], unique=False)
    op.create_table('time_settings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('key', sa.String(length=40), nullable=False),
    sa.Column('version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('effective_from', sa.Date(), nullable=False),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('change_note', sa.String(length=300), nullable=True),
    sa.Column('created_by_name', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'key', 'effective_from', name='uq_time_settings_company_key_from'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_time_settings_company_key', 'time_settings', ['company_id', 'key', 'effective_from'], unique=False)
    op.create_table('attendance_geofences',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('address', sa.String(length=300), nullable=True),
    sa.Column('latitude', sa.Float(), nullable=False),
    sa.Column('longitude', sa.Float(), nullable=False),
    sa.Column('radius_m', sa.Integer(), server_default=sa.text('200'), nullable=False),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_attendance_geofences_company', 'attendance_geofences', ['company_id', 'active'], unique=False)
    op.create_table('attendance_devices',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('vendor', sa.String(length=80), nullable=True),
    sa.Column('model', sa.String(length=80), nullable=True),
    sa.Column('serial_number', sa.String(length=80), nullable=True),
    sa.Column('kind', sa.String(length=20), server_default=sa.text("'biometric'"), nullable=False),
    sa.Column('location_name', sa.String(length=150), nullable=True),
    sa.Column('timezone', sa.String(length=64), nullable=True),
    sa.Column('connection', sa.String(length=20), server_default=sa.text("'push'"), nullable=False),
    sa.Column('api_key_prefix', sa.String(length=16), nullable=True),
    sa.Column('api_key_hash', sa.String(length=128), nullable=True),
    sa.Column('ftp_config', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('column_mapping', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_sync_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('next_pull_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_attendance_devices_company', 'attendance_devices', ['company_id', 'active'], unique=False)
    op.create_index('ix_attendance_devices_key_prefix', 'attendance_devices', ['api_key_prefix'], unique=False)
    op.create_table('attendance_locks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('period_start', sa.Date(), nullable=False),
    sa.Column('period_end', sa.Date(), nullable=False),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'locked'"), nullable=False),
    sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('locked_by_name', sa.String(length=200), nullable=True),
    sa.Column('unlocked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('unlocked_by_name', sa.String(length=200), nullable=True),
    sa.Column('note', sa.String(length=300), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_attendance_locks_company_period', 'attendance_locks', ['company_id', 'period_start', 'period_end'], unique=False)
    op.create_table('night_shift_consents',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('consent_date', sa.Date(), nullable=False),
    sa.Column('valid_from', sa.Date(), nullable=False),
    sa.Column('valid_to', sa.Date(), nullable=True),
    sa.Column('safety_measures', sa.Text(), nullable=True),
    sa.Column('document_url', sa.Text(), nullable=True),
    sa.Column('withdrawn_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('recorded_by_name', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_night_shift_consents_company_employee', 'night_shift_consents', ['company_id', 'employee_id'], unique=False)
    op.create_table('work_arrangements',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('effective_from', sa.Date(), nullable=False),
    sa.Column('shift_id', sa.Integer(), nullable=True),
    sa.Column('working_days', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[0, 1, 2, 3, 4]'::jsonb"), nullable=False),
    sa.Column('alternate_days', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('punch_required', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('attendance_rule_id', sa.Integer(), nullable=True),
    sa.Column('note', sa.String(length=300), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['attendance_rule_id'], ['attendance_rules.id'], ),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['shift_id'], ['shifts.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('employee_id', 'effective_from', name='uq_work_arrangements_employee_from'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_work_arrangements_company_employee', 'work_arrangements', ['company_id', 'employee_id', 'effective_from'], unique=False)
    op.create_table('device_enrolments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('device_id', sa.Integer(), nullable=True),
    sa.Column('device_user_id', sa.String(length=60), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('card_number', sa.String(length=60), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['device_id'], ['attendance_devices.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_device_enrolments_company_device', 'device_enrolments', ['company_id', 'device_id', 'device_user_id'], unique=False)
    op.create_index('uq_device_enrolments_any_device_user', 'device_enrolments', ['company_id', 'device_user_id'], unique=True, postgresql_where=sa.text('device_id IS NULL'))
    op.create_index('uq_device_enrolments_device_user', 'device_enrolments', ['company_id', 'device_id', 'device_user_id'], unique=True, postgresql_where=sa.text('device_id IS NOT NULL'))
    op.create_table('device_sync_logs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('device_id', sa.Integer(), nullable=True),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('file_name', sa.String(length=255), nullable=True),
    sa.Column('received', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('inserted', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('duplicates', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('unmatched', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('errors', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('message', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['device_id'], ['attendance_devices.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_device_sync_logs_company_created', 'device_sync_logs', ['company_id', sa.text('created_at DESC')], unique=False)
    op.create_table('attendance_requests',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('end_date', sa.Date(), nullable=True),
    sa.Column('from_time', sa.Time(), nullable=True),
    sa.Column('to_time', sa.Time(), nullable=True),
    sa.Column('minutes', sa.Integer(), nullable=True),
    sa.Column('duration', sa.String(length=20), nullable=True),
    sa.Column('category_id', sa.Integer(), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'pending'"), nullable=False),
    sa.Column('workflow_instance_id', sa.Integer(), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['category_id'], ['master_values.id'], ),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['workflow_instance_id'], ['workflow_instances.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_attendance_requests_company_employee', 'attendance_requests', ['company_id', 'employee_id', 'kind', 'date'], unique=False)
    op.create_index('ix_attendance_requests_company_status', 'attendance_requests', ['company_id', 'status'], unique=False)
    op.create_table('attendance_punches',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('punched_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('direction', sa.String(length=10), nullable=True),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('device_id', sa.Integer(), nullable=True),
    sa.Column('device_user_id', sa.String(length=60), nullable=True),
    sa.Column('latitude', sa.Float(), nullable=True),
    sa.Column('longitude', sa.Float(), nullable=True),
    sa.Column('accuracy_m', sa.Float(), nullable=True),
    sa.Column('geofence_id', sa.Integer(), nullable=True),
    sa.Column('distance_m', sa.Integer(), nullable=True),
    sa.Column('selfie', sa.Text(), nullable=True),
    sa.Column('ip_address', sa.String(length=64), nullable=True),
    sa.Column('request_id', sa.Integer(), nullable=True),
    sa.Column('ignored', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('note', sa.String(length=300), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['device_id'], ['attendance_devices.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['geofence_id'], ['attendance_geofences.id'], ),
    sa.ForeignKeyConstraint(['request_id'], ['attendance_requests.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('employee_id', 'punched_at', name='uq_attendance_punches_employee_time'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_attendance_punches_company_employee_time', 'attendance_punches', ['company_id', 'employee_id', 'punched_at'], unique=False)
    op.create_table('attendance_days',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('day_type', sa.String(length=20), server_default=sa.text("'working'"), nullable=False),
    sa.Column('shift_id', sa.Integer(), nullable=True),
    sa.Column('shift_start', sa.DateTime(timezone=True), nullable=True),
    sa.Column('shift_end', sa.DateTime(timezone=True), nullable=True),
    sa.Column('first_in', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_out', sa.DateTime(timezone=True), nullable=True),
    sa.Column('punch_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('worked_minutes', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('break_minutes', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('late_minutes', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('early_minutes', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('overtime_minutes', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('unapproved_overtime_minutes', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('night_minutes', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('permission_minutes', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('is_late', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('is_early_exit', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('is_night_shift', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('shift_allowance', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('payable_days', sa.Numeric(precision=4, scale=2), server_default=sa.text('0'), nullable=False),
    sa.Column('lop_days', sa.Numeric(precision=4, scale=2), server_default=sa.text('0'), nullable=False),
    sa.Column('penalty_lop_days', sa.Numeric(precision=4, scale=2), server_default=sa.text('0'), nullable=False),
    sa.Column('leave_info', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('holiday_name', sa.String(length=150), nullable=True),
    sa.Column('notes', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('rule_id', sa.Integer(), nullable=True),
    sa.Column('override_status', sa.String(length=20), nullable=True),
    sa.Column('override_note', sa.String(length=300), nullable=True),
    sa.Column('locked', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('computed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['rule_id'], ['attendance_rules.id'], ),
    sa.ForeignKeyConstraint(['shift_id'], ['shifts.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('employee_id', 'date', name='uq_attendance_days_employee_date'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_attendance_days_company_date', 'attendance_days', ['company_id', 'date'], unique=False)
    op.create_index('ix_attendance_days_company_employee_date', 'attendance_days', ['company_id', 'employee_id', 'date'], unique=False)
    op.create_table('attendance_adjustments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('apply_from', sa.Date(), nullable=False),
    sa.Column('payable_days_delta', sa.Numeric(precision=4, scale=2), server_default=sa.text('0'), nullable=False),
    sa.Column('lop_days_delta', sa.Numeric(precision=4, scale=2), server_default=sa.text('0'), nullable=False),
    sa.Column('overtime_minutes_delta', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('old_status', sa.String(length=20), nullable=True),
    sa.Column('new_status', sa.String(length=20), nullable=True),
    sa.Column('reason', sa.String(length=300), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_attendance_adjustments_company_period', 'attendance_adjustments', ['company_id', 'apply_from', 'employee_id'], unique=False)
    op.create_table('ts_clients',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=150), nullable=False),
    sa.Column('contact_name', sa.String(length=150), nullable=True),
    sa.Column('email', sa.String(length=255), nullable=True),
    sa.Column('phone', sa.String(length=30), nullable=True),
    sa.Column('gstin', sa.String(length=15), nullable=True),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_ts_clients_company', 'ts_clients', ['company_id', 'active'], unique=False)
    op.create_table('ts_projects',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('client_id', sa.Integer(), nullable=True),
    sa.Column('name', sa.String(length=150), nullable=False),
    sa.Column('code', sa.String(length=30), nullable=True),
    sa.Column('owner_employee_id', sa.Integer(), nullable=True),
    sa.Column('billable', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('budget_hours', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('start_date', sa.Date(), nullable=True),
    sa.Column('end_date', sa.Date(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'active'"), nullable=False),
    sa.Column('member_ids', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['client_id'], ['ts_clients.id'], ),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['owner_employee_id'], ['employees.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_ts_projects_company', 'ts_projects', ['company_id', 'status'], unique=False)
    op.create_table('ts_tasks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=150), nullable=False),
    sa.Column('owner_employee_id', sa.Integer(), nullable=True),
    sa.Column('billable', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('budget_hours', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'open'"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['owner_employee_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['ts_projects.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_ts_tasks_company_project', 'ts_tasks', ['company_id', 'project_id'], unique=False)
    op.create_table('ts_timesheets',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('week_start', sa.Date(), nullable=False),
    sa.Column('week_end', sa.Date(), nullable=False),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'draft'"), nullable=False),
    sa.Column('total_hours', sa.Numeric(precision=6, scale=2), server_default=sa.text('0'), nullable=False),
    sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('workflow_instance_id', sa.Integer(), nullable=True),
    sa.Column('comment', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['workflow_instance_id'], ['workflow_instances.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('employee_id', 'week_start', name='uq_ts_timesheets_employee_week'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_ts_timesheets_company_status', 'ts_timesheets', ['company_id', 'status', 'week_start'], unique=False)
    op.create_table('ts_time_logs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=True),
    sa.Column('hours', sa.Numeric(precision=5, scale=2), nullable=False),
    sa.Column('start_time', sa.Time(), nullable=True),
    sa.Column('end_time', sa.Time(), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('billable', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('timesheet_id', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'draft'"), nullable=False),
    sa.Column('review_comment', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['ts_projects.id'], ),
    sa.ForeignKeyConstraint(['task_id'], ['ts_tasks.id'], ),
    sa.ForeignKeyConstraint(['timesheet_id'], ['ts_timesheets.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_ts_time_logs_company_employee_date', 'ts_time_logs', ['company_id', 'employee_id', 'date'], unique=False)
    op.create_index('ix_ts_time_logs_company_project', 'ts_time_logs', ['company_id', 'project_id'], unique=False)
    op.create_index('ix_ts_time_logs_timesheet', 'ts_time_logs', ['timesheet_id'], unique=False)
    op.add_column("roster_entries", sa.Column("shift_id", sa.Integer(), sa.ForeignKey("shifts.id", name="fk_roster_entries_shift_id"), nullable=True))
    op.add_column("roster_entries", sa.Column("published", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.add_column("roster_entries", sa.Column("published_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("roster_entries", sa.Column("source", sa.String(length=20), server_default=sa.text("'manual'"), nullable=False))
    op.add_column("roster_entries", sa.Column("pattern_id", sa.Integer(), sa.ForeignKey("shift_patterns.id", name="fk_roster_entries_pattern_id"), nullable=True))
    op.add_column("roster_entries", sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True))
    op.create_index("ix_roster_entries_company_date", "roster_entries", ["company_id", "date"], unique=False)
    # entries made before rosters had drafts were live already
    op.execute("UPDATE roster_entries SET published = true, published_at = now()")
    for table in TABLES:
        for sql in enable_tenant_rls_sql(table):
            op.execute(sql)


def downgrade():
    op.drop_index("ix_roster_entries_company_date", table_name="roster_entries")
    for col in ("updated_at", "pattern_id", "source", "published_at", "published", "shift_id"):
        op.drop_column("roster_entries", col)
    for table in reversed(TABLES):
        for sql in disable_tenant_rls_sql(table):
            op.execute(sql)
        op.drop_table(table)
