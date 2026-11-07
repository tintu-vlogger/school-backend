"""Travel (policy versions, trips, legs, stays) and recycle bin settings

Revision ID: tv1a2b3c4d5e
Revises: ig1a2b3c4d5e
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision = "tv1a2b3c4d5e"
down_revision = "ig1a2b3c4d5e"
branch_labels = None
depends_on = None

TABLES = ("travel_policies", "travel_requests", "travel_legs", "travel_stays", "recycle_bin_settings")


def _stamps():
    return [sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False)]


def _booking():
    return [
        sa.Column("booking_status", sa.String(20), server_default=sa.text("'open'"), nullable=False),
        sa.Column("paid_by", sa.String(20), server_default=sa.text("'company'"), nullable=False),
        sa.Column("vendor", sa.String(150), nullable=True),
        sa.Column("booking_ref", sa.String(80), nullable=True),
        sa.Column("booked_cost", sa.Numeric(14, 2), nullable=True),
        sa.Column("file_name", sa.String(255), nullable=True),
        sa.Column("file_data", sa.Text(), nullable=True),
        sa.Column("booked_by_name", sa.String(200), nullable=True),
        sa.Column("booked_on", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    uuid_col = lambda: sa.Column("uuid", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, unique=True)  # noqa: E731
    company = lambda: sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False)  # noqa: E731
    op.create_table(
        "travel_policies",
        sa.Column("id", sa.Integer(), primary_key=True), uuid_col(), company(),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(150), server_default=sa.text("'Travel policy'"), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("rules", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("change_note", sa.String(300), nullable=True),
        sa.Column("created_by_name", sa.String(200), nullable=True),
        *_stamps(),
        sa.UniqueConstraint("company_id", "version", name="uq_travel_policies_company_version"),
    )
    op.create_index("ix_travel_policies_company_from", "travel_policies", ["company_id", "effective_from"])
    op.create_table(
        "travel_requests",
        sa.Column("id", sa.Integer(), primary_key=True), uuid_col(), company(),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("department_id", sa.Integer(), sa.ForeignKey("departments.id"), nullable=True),
        sa.Column("number", sa.String(40), nullable=True),
        sa.Column("purpose", sa.String(300), nullable=False),
        sa.Column("travel_type", sa.String(20), server_default=sa.text("'domestic'"), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("ts_projects.id"), nullable=True),
        sa.Column("billable", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("accommodation_needed", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("per_diem_days", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("per_diem_rate", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("estimated_total", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("advance_amount", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("advance_id", sa.Integer(), sa.ForeignKey("expense_advances.id"), nullable=True),
        sa.Column("policy_id", sa.Integer(), sa.ForeignKey("travel_policies.id"), nullable=True),
        sa.Column("grade_code", sa.String(30), nullable=True),
        sa.Column("policy_flags", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("outside_policy", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("exception_reason", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("workflow_instance_id", sa.Integer(), sa.ForeignKey("workflow_instances.id"), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_note", sa.Text(), nullable=True),
        sa.Column("booked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_id", sa.Integer(), sa.ForeignKey("expense_claims.id"), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
    )
    op.create_index("ix_travel_requests_company_employee", "travel_requests", ["company_id", "employee_id", "status"])
    op.create_index("ix_travel_requests_company_status", "travel_requests", ["company_id", "status", "start_date"])
    op.create_table(
        "travel_legs",
        sa.Column("id", sa.Integer(), primary_key=True), uuid_col(), company(),
        sa.Column("request_id", sa.Integer(), sa.ForeignKey("travel_requests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("travel_class", sa.String(30), nullable=True),
        sa.Column("from_place", sa.String(150), nullable=False),
        sa.Column("to_place", sa.String(150), nullable=False),
        sa.Column("depart_date", sa.Date(), nullable=False),
        sa.Column("preferred_time", sa.String(40), nullable=True),
        sa.Column("distance_km", sa.Numeric(10, 2), nullable=True),
        sa.Column("estimated_cost", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("notes", sa.String(300), nullable=True),
        sa.Column("flags", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        *_booking(), *_stamps(),
    )
    op.create_index("ix_travel_legs_company_request", "travel_legs", ["company_id", "request_id"])
    op.create_table(
        "travel_stays",
        sa.Column("id", sa.Integer(), primary_key=True), uuid_col(), company(),
        sa.Column("request_id", sa.Integer(), sa.ForeignKey("travel_requests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("city", sa.String(150), nullable=False),
        sa.Column("tier", sa.String(20), nullable=True),
        sa.Column("check_in", sa.Date(), nullable=False),
        sa.Column("check_out", sa.Date(), nullable=False),
        sa.Column("nightly_cost", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("notes", sa.String(300), nullable=True),
        sa.Column("flags", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        *_booking(), *_stamps(),
    )
    op.create_index("ix_travel_stays_company_request", "travel_stays", ["company_id", "request_id"])
    op.create_table(
        "recycle_bin_settings",
        sa.Column("id", sa.Integer(), primary_key=True), company(),
        sa.Column("retention_days", sa.Integer(), server_default=sa.text("90"), nullable=False),
        sa.Column("auto_purge", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("company_id", name="uq_recycle_bin_settings_company"),
    )
    for t in TABLES:
        for sql in enable_tenant_rls_sql(t):
            op.execute(sql)


def downgrade() -> None:
    for t in TABLES:
        for sql in disable_tenant_rls_sql(t):
            op.execute(sql)
    for t in reversed(TABLES):
        op.drop_table(t)
