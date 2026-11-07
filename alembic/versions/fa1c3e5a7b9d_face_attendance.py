"""Face attendance: settings, kiosk face settings, enrolments (encrypted templates) and
face check events with HR review.

Revision ID: fa1c3e5a7b9d
Revises: ig1a2b3c4d5e
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision = "fa1c3e5a7b9d"
down_revision = "ig1a2b3c4d5e"
branch_labels = None
depends_on = None

TABLES = ("face_settings", "face_kiosk_settings", "face_enrolments", "face_events")
JSONB = postgresql.JSONB


def _company():
    return sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False)


def _uuid():
    return sa.Column("uuid", postgresql.UUID(as_uuid=True), nullable=False, unique=True,
                     server_default=sa.text("gen_random_uuid()"))


def _created():
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"))


def _updated():
    return sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"))


def upgrade():
    op.create_table(
        "face_settings", sa.Column("id", sa.Integer(), primary_key=True), _company(),
        sa.Column("config", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("updated_by_name", sa.String(200), nullable=True),
        _created(), _updated(),
        sa.UniqueConstraint("company_id", name="uq_face_settings_company"),
    )
    op.create_index("ix_face_settings_company", "face_settings", ["company_id"])

    op.create_table(
        "face_kiosk_settings", sa.Column("id", sa.Integer(), primary_key=True), _company(),
        sa.Column("kiosk_id", sa.Integer(), sa.ForeignKey("attendance_kiosks.id"), nullable=False),
        sa.Column("location_id", sa.Integer(), sa.ForeignKey("locations.id"), nullable=True),
        sa.Column("mode", sa.String(12), nullable=False, server_default=sa.text("'off'")),
        sa.Column("threshold", sa.Float(), nullable=True),
        sa.Column("liveness", sa.String(12), nullable=True),
        _created(), _updated(),
        sa.UniqueConstraint("kiosk_id", name="uq_face_kiosk_settings_kiosk"),
    )
    op.create_index("ix_face_kiosk_settings_company", "face_kiosk_settings", ["company_id", "kiosk_id"])

    op.create_table(
        "face_enrolments", sa.Column("id", sa.Integer(), primary_key=True), _uuid(), _company(),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default=sa.text("'active'")),
        sa.Column("model", sa.String(40), nullable=False),
        sa.Column("dims", sa.Integer(), nullable=False),
        sa.Column("samples", sa.Integer(), nullable=False),
        sa.Column("templates", sa.Text(), nullable=True),
        sa.Column("quality", sa.Float(), nullable=True),
        sa.Column("consent_version", sa.Integer(), nullable=False),
        sa.Column("consent_text", sa.Text(), nullable=False),
        sa.Column("consent_by", sa.String(10), nullable=False),
        sa.Column("consent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_by_name", sa.String(200), nullable=True),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawn_by_name", sa.String(200), nullable=True),
        sa.Column("withdraw_reason", sa.String(300), nullable=True),
        _created(), _updated(),
    )
    op.create_index("ix_face_enrolments_company_employee", "face_enrolments", ["company_id", "employee_id", "status"])

    op.create_table(
        "face_events", sa.Column("id", sa.Integer(), primary_key=True), _uuid(), _company(),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("source", sa.String(10), nullable=False),
        sa.Column("kiosk_id", sa.Integer(), sa.ForeignKey("attendance_kiosks.id"), nullable=True),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=True),
        sa.Column("punch_id", sa.Integer(), sa.ForeignKey("attendance_punches.id"), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("client_score", sa.Float(), nullable=True),
        sa.Column("threshold", sa.Float(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=True),
        sa.Column("liveness", JSONB, nullable=True),
        sa.Column("snapshot", sa.Text(), nullable=True),
        sa.Column("offline", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("client_id", sa.String(60), nullable=True),
        sa.Column("flagged", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("flag_reasons", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("review_status", sa.String(12), nullable=True),
        sa.Column("reviewed_by_name", sa.String(200), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.String(300), nullable=True),
        _created(),
    )
    op.create_index("ix_face_events_company_time", "face_events", ["company_id", "occurred_at"])
    op.create_index("ix_face_events_company_review", "face_events", ["company_id", "flagged", "review_status"])
    op.create_index("ix_face_events_company_employee", "face_events", ["company_id", "employee_id", "occurred_at"])
    op.create_index("ix_face_events_client", "face_events", ["company_id", "client_id"])

    for t in TABLES:
        for sql in enable_tenant_rls_sql(t):
            op.execute(sql)


def downgrade():
    for t in reversed(TABLES):
        for sql in disable_tenant_rls_sql(t):
            op.execute(sql)
        op.drop_table(t)
