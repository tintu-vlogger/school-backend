"""communication

Revision ID: cm1a2b3c4d5e
Revises: sc1b2c3d4e5f
Create Date: 2026-11-07 20:04:38.029033
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from core.rls import enable_tenant_rls_sql

TABLES = ["comms_settings", "comms_notices", "comms_notice_recipients", "comms_broadcasts", "comms_threads",
          "comms_messages", "comms_ptm_days", "comms_events", "comms_event_rsvps", "comms_ptm_teachers",
          "comms_ptm_slots", "comms_ptm_bookings", "comms_diary_entries"]

revision: str = 'cm1a2b3c4d5e'
down_revision: Union[str, None] = 'sc1b2c3d4e5f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('comms_notices',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('body', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('category', sa.String(length=20), server_default=sa.text("'general'"), nullable=False),
    sa.Column('attachments', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('audience', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('audience_label', sa.String(length=300), nullable=True),
    sa.Column('channels', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text('\'["in_app"]\'::jsonb'), nullable=False),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'draft'"), nullable=False),
    sa.Column('publish_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('pinned', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('require_ack', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('author_employee_id', sa.Integer(), nullable=True),
    sa.Column('author_name', sa.String(length=200), nullable=True),
    sa.Column('recipient_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('delivery', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['author_employee_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_comms_notices_company_status', 'comms_notices', ['company_id', 'status', 'published_at'], unique=False)
    op.create_table('comms_ptm_days',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('start_time', sa.Time(), nullable=False),
    sa.Column('end_time', sa.Time(), nullable=False),
    sa.Column('slot_minutes', sa.Integer(), server_default=sa.text('10'), nullable=False),
    sa.Column('gap_minutes', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('breaks', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('mode', sa.String(length=12), server_default=sa.text("'in_person'"), nullable=False),
    sa.Column('location', sa.String(length=200), nullable=True),
    sa.Column('meeting_link', sa.String(length=500), nullable=True),
    sa.Column('section_ids', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('instructions', sa.Text(), nullable=True),
    sa.Column('booking_closes_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('notes_visible_default', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('status', sa.String(length=12), server_default=sa.text("'draft'"), nullable=False),
    sa.Column('created_by', sa.String(length=200), nullable=True),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_comms_ptm_days_company_date', 'comms_ptm_days', ['company_id', 'date'], unique=False)
    op.create_table('comms_settings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', name='uq_comms_settings_company')
    )
    op.create_table('comms_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('category', sa.String(length=20), server_default=sa.text("'event'"), nullable=False),
    sa.Column('all_day', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('start_date', sa.Date(), nullable=False),
    sa.Column('end_date', sa.Date(), nullable=False),
    sa.Column('start_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('end_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('location', sa.String(length=200), nullable=True),
    sa.Column('audience', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('audience_label', sa.String(length=300), nullable=True),
    sa.Column('rsvp_required', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('rsvp_deadline', sa.Date(), nullable=True),
    sa.Column('status', sa.String(length=12), server_default=sa.text("'active'"), nullable=False),
    sa.Column('ptm_day_id', sa.Integer(), nullable=True),
    sa.Column('created_by', sa.String(length=200), nullable=True),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['ptm_day_id'], ['comms_ptm_days.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_comms_events_company_dates', 'comms_events', ['company_id', 'start_date', 'end_date'], unique=False)
    op.create_table('comms_ptm_teachers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('ptm_day_id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('location', sa.String(length=200), nullable=True),
    sa.Column('meeting_link', sa.String(length=500), nullable=True),
    sa.Column('start_time', sa.Time(), nullable=True),
    sa.Column('end_time', sa.Time(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['ptm_day_id'], ['comms_ptm_days.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('ptm_day_id', 'employee_id', name='uq_comms_ptm_teacher'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_comms_ptm_teachers_company_day', 'comms_ptm_teachers', ['company_id', 'ptm_day_id'], unique=False)
    op.create_table('comms_broadcasts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('section_id', sa.Integer(), nullable=False),
    sa.Column('sender_employee_id', sa.Integer(), nullable=True),
    sa.Column('sender_name', sa.String(length=200), nullable=True),
    sa.Column('subject', sa.String(length=200), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('attachments', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('recipient_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['section_id'], ['academic_sections.id'], ),
    sa.ForeignKeyConstraint(['sender_employee_id'], ['employees.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_comms_broadcasts_company', 'comms_broadcasts', ['company_id', 'created_at'], unique=False)
    op.create_table('comms_diary_entries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('section_id', sa.Integer(), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('attachments', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('author_employee_id', sa.Integer(), nullable=True),
    sa.Column('author_name', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['author_employee_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['section_id'], ['academic_sections.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('section_id', 'date', name='uq_comms_diary_section_date'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_comms_diary_company_section', 'comms_diary_entries', ['company_id', 'section_id', 'date'], unique=False)
    op.create_table('comms_event_rsvps',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('event_id', sa.Integer(), nullable=False),
    sa.Column('responder_key', sa.String(length=30), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=True),
    sa.Column('response', sa.String(length=10), nullable=False),
    sa.Column('guests', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('note', sa.String(length=300), nullable=True),
    sa.Column('responded_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['event_id'], ['comms_events.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('event_id', 'responder_key', name='uq_comms_event_rsvp')
    )
    op.create_index('ix_comms_event_rsvps_company_event', 'comms_event_rsvps', ['company_id', 'event_id'], unique=False)
    op.create_table('comms_ptm_slots',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('ptm_day_id', sa.Integer(), nullable=False),
    sa.Column('ptm_teacher_id', sa.Integer(), nullable=False),
    sa.Column('start_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('end_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('blocked', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['ptm_day_id'], ['comms_ptm_days.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['ptm_teacher_id'], ['comms_ptm_teachers.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('ptm_teacher_id', 'start_at', name='uq_comms_ptm_slot'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_comms_ptm_slots_company_day', 'comms_ptm_slots', ['company_id', 'ptm_day_id', 'start_at'], unique=False)
    op.create_table('comms_notice_recipients',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('notice_id', sa.Integer(), nullable=False),
    sa.Column('recipient_key', sa.String(length=30), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=True),
    sa.Column('guardian_id', sa.Integer(), nullable=True),
    sa.Column('student_id', sa.Integer(), nullable=True),
    sa.Column('portal_account_id', sa.Integer(), nullable=True),
    sa.Column('via_student_ids', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=True),
    sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('acknowledged_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('channels_sent', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('send_count', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('last_sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['guardian_id'], ['guardians.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['notice_id'], ['comms_notices.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['portal_account_id'], ['portal_accounts.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['student_id'], ['students.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('notice_id', 'recipient_key', name='uq_comms_notice_recipient')
    )
    op.create_index('ix_comms_notice_recipients_company_notice', 'comms_notice_recipients', ['company_id', 'notice_id'], unique=False)
    op.create_index('ix_comms_notice_recipients_employee', 'comms_notice_recipients', ['company_id', 'employee_id'], unique=False)
    op.create_index('ix_comms_notice_recipients_guardian', 'comms_notice_recipients', ['company_id', 'guardian_id'], unique=False)
    op.create_index('ix_comms_notice_recipients_student', 'comms_notice_recipients', ['company_id', 'student_id'], unique=False)
    op.create_table('comms_ptm_bookings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('ptm_day_id', sa.Integer(), nullable=False),
    sa.Column('ptm_teacher_id', sa.Integer(), nullable=False),
    sa.Column('slot_id', sa.Integer(), nullable=False),
    sa.Column('student_id', sa.Integer(), nullable=False),
    sa.Column('guardian_id', sa.Integer(), nullable=True),
    sa.Column('booked_by', sa.String(length=200), nullable=True),
    sa.Column('mode', sa.String(length=12), server_default=sa.text("'in_person'"), nullable=False),
    sa.Column('status', sa.String(length=12), server_default=sa.text("'booked'"), nullable=False),
    sa.Column('parent_note', sa.String(length=500), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('action_points', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('notes_visible', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('reminder_sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['guardian_id'], ['guardians.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['ptm_day_id'], ['comms_ptm_days.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['ptm_teacher_id'], ['comms_ptm_teachers.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['slot_id'], ['comms_ptm_slots.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['student_id'], ['students.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_comms_ptm_bookings_company_day', 'comms_ptm_bookings', ['company_id', 'ptm_day_id'], unique=False)
    op.create_index('uq_comms_ptm_booking_slot', 'comms_ptm_bookings', ['slot_id'], unique=True, postgresql_where=sa.text("status <> 'cancelled'"))
    op.create_index('uq_comms_ptm_booking_student_teacher', 'comms_ptm_bookings', ['ptm_teacher_id', 'student_id'], unique=True, postgresql_where=sa.text("status <> 'cancelled'"))
    op.create_table('comms_threads',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('subject', sa.String(length=200), nullable=False),
    sa.Column('staff_employee_id', sa.Integer(), nullable=True),
    sa.Column('to_office', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('party_kind', sa.String(length=10), nullable=False),
    sa.Column('guardian_id', sa.Integer(), nullable=True),
    sa.Column('student_id', sa.Integer(), nullable=True),
    sa.Column('about_student_id', sa.Integer(), nullable=True),
    sa.Column('section_id', sa.Integer(), nullable=True),
    sa.Column('broadcast_id', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=10), server_default=sa.text("'open'"), nullable=False),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('closed_by', sa.String(length=200), nullable=True),
    sa.Column('started_by', sa.String(length=10), server_default=sa.text("'staff'"), nullable=False),
    sa.Column('staff_unread', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('party_unread', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('message_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('last_message_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_message_preview', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['about_student_id'], ['students.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['broadcast_id'], ['comms_broadcasts.id'], ),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['guardian_id'], ['guardians.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['section_id'], ['academic_sections.id'], ),
    sa.ForeignKeyConstraint(['staff_employee_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['student_id'], ['students.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_comms_threads_company_guardian', 'comms_threads', ['company_id', 'guardian_id'], unique=False)
    op.create_index('ix_comms_threads_company_last', 'comms_threads', ['company_id', 'last_message_at'], unique=False)
    op.create_index('ix_comms_threads_company_staff', 'comms_threads', ['company_id', 'staff_employee_id', 'last_message_at'], unique=False)
    op.create_index('ix_comms_threads_company_student', 'comms_threads', ['company_id', 'student_id'], unique=False)
    op.create_table('comms_messages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('thread_id', sa.Integer(), nullable=False),
    sa.Column('sender_kind', sa.String(length=10), nullable=False),
    sa.Column('sender_employee_id', sa.Integer(), nullable=True),
    sa.Column('sender_portal_id', sa.Integer(), nullable=True),
    sa.Column('sender_name', sa.String(length=200), nullable=True),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('attachments', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('broadcast_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['broadcast_id'], ['comms_broadcasts.id'], ),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['sender_employee_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['sender_portal_id'], ['portal_accounts.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['thread_id'], ['comms_threads.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_comms_messages_company_thread', 'comms_messages', ['company_id', 'thread_id', 'created_at'], unique=False)
    for t in TABLES:
        for sql in enable_tenant_rls_sql(t):
            op.execute(sql)


def downgrade() -> None:
    op.drop_index('ix_comms_messages_company_thread', table_name='comms_messages')
    op.drop_table('comms_messages')
    op.drop_index('ix_comms_threads_company_student', table_name='comms_threads')
    op.drop_index('ix_comms_threads_company_staff', table_name='comms_threads')
    op.drop_index('ix_comms_threads_company_last', table_name='comms_threads')
    op.drop_index('ix_comms_threads_company_guardian', table_name='comms_threads')
    op.drop_table('comms_threads')
    op.drop_index('uq_comms_ptm_booking_student_teacher', table_name='comms_ptm_bookings', postgresql_where=sa.text("status <> 'cancelled'"))
    op.drop_index('uq_comms_ptm_booking_slot', table_name='comms_ptm_bookings', postgresql_where=sa.text("status <> 'cancelled'"))
    op.drop_index('ix_comms_ptm_bookings_company_day', table_name='comms_ptm_bookings')
    op.drop_table('comms_ptm_bookings')
    op.drop_index('ix_comms_notice_recipients_student', table_name='comms_notice_recipients')
    op.drop_index('ix_comms_notice_recipients_guardian', table_name='comms_notice_recipients')
    op.drop_index('ix_comms_notice_recipients_employee', table_name='comms_notice_recipients')
    op.drop_index('ix_comms_notice_recipients_company_notice', table_name='comms_notice_recipients')
    op.drop_table('comms_notice_recipients')
    op.drop_index('ix_comms_ptm_slots_company_day', table_name='comms_ptm_slots')
    op.drop_table('comms_ptm_slots')
    op.drop_index('ix_comms_event_rsvps_company_event', table_name='comms_event_rsvps')
    op.drop_table('comms_event_rsvps')
    op.drop_index('ix_comms_diary_company_section', table_name='comms_diary_entries')
    op.drop_table('comms_diary_entries')
    op.drop_index('ix_comms_broadcasts_company', table_name='comms_broadcasts')
    op.drop_table('comms_broadcasts')
    op.drop_index('ix_comms_ptm_teachers_company_day', table_name='comms_ptm_teachers')
    op.drop_table('comms_ptm_teachers')
    op.drop_index('ix_comms_events_company_dates', table_name='comms_events')
    op.drop_table('comms_events')
    op.drop_table('comms_settings')
    op.drop_index('ix_comms_ptm_days_company_date', table_name='comms_ptm_days')
    op.drop_table('comms_ptm_days')
    op.drop_index('ix_comms_notices_company_status', table_name='comms_notices')
    op.drop_table('comms_notices')
