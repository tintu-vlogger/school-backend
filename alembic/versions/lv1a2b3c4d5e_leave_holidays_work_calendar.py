"""Leave, holidays and work calendar

Revision ID: lv1a2b3c4d5e
Revises: w8x9y0z1a2b3
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision = "lv1a2b3c4d5e"
down_revision = "w8x9y0z1a2b3"
branch_labels = None
depends_on = None

TABLES = ("leave_settings", "leave_types", "leave_policy_versions", "leave_requests", "leave_ledger",
          "comp_off_requests", "holiday_calendars", "holidays", "work_week_patterns", "leave_runs")

JSONB = postgresql.JSONB


def _id():
    return sa.Column("id", sa.Integer(), primary_key=True)


def _uuid():
    return sa.Column("uuid", postgresql.UUID(as_uuid=True), nullable=False, unique=True,
                     server_default=sa.text("gen_random_uuid()"))


def _company():
    return sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False)


def _stamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    ]


def _json(name, default="'{}'::jsonb", nullable=False):
    return sa.Column(name, JSONB(), nullable=nullable, server_default=sa.text(default) if default else None)


def upgrade():
    op.create_table(
        "leave_settings", _id(), _company(), _json("config"), *_stamps(),
        sa.UniqueConstraint("company_id", name="uq_leave_settings_company"),
    )

    op.create_table(
        "leave_types", _id(), _uuid(), _company(),
        sa.Column("code", sa.String(20), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("color", sa.String(20), nullable=False, server_default=sa.text("'#4f46e5'")),
        sa.Column("icon", sa.String(40), nullable=False, server_default=sa.text("'plane'")),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("kind", sa.String(20), nullable=False, server_default=sa.text("'standard'")),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("100")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
        sa.UniqueConstraint("company_id", "code", name="uq_leave_types_company_code"),
    )
    op.create_index("ix_leave_types_company_active", "leave_types", ["company_id", "active", "sort_order"])

    op.create_table(
        "leave_policy_versions", _id(), _uuid(), _company(),
        sa.Column("leave_type_id", sa.Integer(), sa.ForeignKey("leave_types.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        _json("rules"),
        sa.Column("change_note", sa.String(300), nullable=True),
        sa.Column("created_by_name", sa.String(200), nullable=True),
        *_stamps(),
        sa.UniqueConstraint("leave_type_id", "version", name="uq_leave_policy_versions_type_version"),
    )
    op.create_index("ix_leave_policy_versions_company_type", "leave_policy_versions",
                    ["company_id", "leave_type_id", "effective_from"])

    op.create_table(
        "leave_requests", _id(), _uuid(), _company(),
        sa.Column("reference_no", sa.String(60), nullable=True),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("leave_type_id", sa.Integer(), sa.ForeignKey("leave_types.id"), nullable=False),
        sa.Column("policy_version_id", sa.Integer(), sa.ForeignKey("leave_policy_versions.id"), nullable=True),
        sa.Column("kind", sa.String(20), nullable=False, server_default=sa.text("'leave'")),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("leave_requests.id"), nullable=True),
        sa.Column("from_date", sa.Date(), nullable=False),
        sa.Column("to_date", sa.Date(), nullable=False),
        sa.Column("unit", sa.String(12), nullable=False, server_default=sa.text("'day'")),
        sa.Column("half", sa.String(10), nullable=True),
        sa.Column("hours", sa.Numeric(5, 2), nullable=True),
        sa.Column("days", sa.Numeric(8, 2), nullable=False),
        sa.Column("paid_days", sa.Numeric(8, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("lop_days", sa.Numeric(8, 2), nullable=False, server_default=sa.text("0")),
        _json("breakdown", "'[]'::jsonb"),
        sa.Column("grant_option", sa.String(40), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attachment_name", sa.String(255), nullable=True),
        sa.Column("attachment", sa.Text(), nullable=True),
        sa.Column("workflow_instance_id", sa.Integer(), sa.ForeignKey("workflow_instances.id"), nullable=True),
        sa.Column("cancel_instance_id", sa.Integer(), sa.ForeignKey("workflow_instances.id"), nullable=True),
        _json("cancel_dates", None, nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("applied_by_name", sa.String(200), nullable=True),
        sa.Column("on_behalf", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        _json("history", "'[]'::jsonb"),
        *_stamps(),
    )
    op.create_index("ix_leave_requests_company_employee", "leave_requests", ["company_id", "employee_id", "from_date"])
    op.create_index("ix_leave_requests_company_status", "leave_requests", ["company_id", "status", "from_date"])
    op.create_index("ix_leave_requests_company_dates", "leave_requests", ["company_id", "from_date", "to_date"])
    op.create_index("ix_leave_requests_parent", "leave_requests", ["parent_id"])

    op.create_table(
        "leave_ledger", _id(), _uuid(), _company(),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("leave_type_id", sa.Integer(), sa.ForeignKey("leave_types.id"), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("days", sa.Numeric(8, 2), nullable=False),
        sa.Column("expires_on", sa.Date(), nullable=True),
        sa.Column("request_id", sa.Integer(), sa.ForeignKey("leave_requests.id"), nullable=True),
        sa.Column("policy_version_id", sa.Integer(), sa.ForeignKey("leave_policy_versions.id"), nullable=True),
        sa.Column("idem_key", sa.String(160), nullable=True),
        sa.Column("note", sa.String(300), nullable=True),
        _json("meta"),
        sa.Column("created_by_name", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_leave_ledger_company_employee_type", "leave_ledger",
                    ["company_id", "employee_id", "leave_type_id", "year"])
    op.create_index("ix_leave_ledger_company_kind", "leave_ledger", ["company_id", "kind", "entry_date"])
    op.create_index("ix_leave_ledger_request", "leave_ledger", ["request_id"])
    op.create_index("uq_leave_ledger_company_idem", "leave_ledger", ["company_id", "idem_key"], unique=True,
                    postgresql_where=sa.text("idem_key IS NOT NULL"))

    op.create_table(
        "comp_off_requests", _id(), _uuid(), _company(),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("worked_date", sa.Date(), nullable=False),
        sa.Column("day_kind", sa.String(20), nullable=True),
        sa.Column("unit", sa.String(12), nullable=False, server_default=sa.text("'day'")),
        sa.Column("days", sa.Numeric(5, 2), nullable=False),
        sa.Column("hours", sa.Numeric(5, 2), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("source", sa.String(20), nullable=False, server_default=sa.text("'request'")),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("expires_on", sa.Date(), nullable=True),
        sa.Column("workflow_instance_id", sa.Integer(), sa.ForeignKey("workflow_instances.id"), nullable=True),
        sa.Column("applied_by_name", sa.String(200), nullable=True),
        *_stamps(),
    )
    op.create_index("ix_comp_off_requests_company_employee", "comp_off_requests", ["company_id", "employee_id", "worked_date"])
    op.create_index("ix_comp_off_requests_company_status", "comp_off_requests", ["company_id", "status"])

    op.create_table(
        "holiday_calendars", _id(), _uuid(), _company(),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        _json("applicability"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("100")),
        sa.Column("optional_limit", sa.Integer(), nullable=False, server_default=sa.text("2")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        *_stamps(),
    )
    op.create_index("ix_holiday_calendars_company_active", "holiday_calendars", ["company_id", "active", "priority"])

    op.create_table(
        "holidays", _id(), _uuid(), _company(),
        sa.Column("calendar_id", sa.Integer(), sa.ForeignKey("holiday_calendars.id"), nullable=False),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False, server_default=sa.text("'festival'")),
        sa.Column("description", sa.Text(), nullable=True),
        *_stamps(),
        sa.UniqueConstraint("calendar_id", "date", "name", name="uq_holidays_calendar_date_name"),
    )
    op.create_index("ix_holidays_company_date", "holidays", ["company_id", "date"])
    op.create_index("ix_holidays_calendar", "holidays", ["calendar_id", "date"])

    op.create_table(
        "work_week_patterns", _id(), _uuid(), _company(),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("days", JSONB(), nullable=False),
        _json("applicability"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("100")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        *_stamps(),
    )
    op.create_index("ix_work_week_patterns_company_active", "work_week_patterns", ["company_id", "active", "priority"])

    op.create_table(
        "leave_runs", _id(), _uuid(), _company(),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("label", sa.String(150), nullable=True),
        sa.Column("as_of", sa.Date(), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'done'")),
        sa.Column("entries", sa.Integer(), nullable=False, server_default=sa.text("0")),
        _json("result"),
        sa.Column("ran_by_name", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_leave_runs_company_created", "leave_runs", ["company_id", "kind", sa.text("created_at DESC")])

    for table in TABLES:
        for sql in enable_tenant_rls_sql(table):
            op.execute(sql)
    # the application role needs rights on the new tables (default privileges cover most setups)
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'school_app') THEN
            EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO school_app';
            EXECUTE 'GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO school_app';
          END IF;
        END $$;
    """)


def downgrade():
    for table in reversed(TABLES):
        for sql in disable_tenant_rls_sql(table):
            op.execute(sql)
    for table in ("leave_runs", "work_week_patterns", "holidays", "holiday_calendars", "comp_off_requests",
                  "leave_ledger", "leave_requests", "leave_policy_versions", "leave_types", "leave_settings"):
        op.drop_table(table)
