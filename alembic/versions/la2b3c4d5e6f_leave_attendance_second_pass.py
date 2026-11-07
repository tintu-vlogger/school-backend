"""Leave and attendance, second pass: attendance-to-leave links, LOP per pay period,
clock-in kiosks and PINs, auto shift, reduced-pay leave, Bradford alerts, expiry
reminders and timesheet billing.

Revision ID: la2b3c4d5e6f
Revises: a7t1m2e3s4h5 (attendance), lv1a2b3c4d5e (leave)
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision = "la2b3c4d5e6f"
# Both parents as down revisions (a depends_on here leaves leave as a second "effective
# head" and breaks `alembic upgrade head` through the merge revision).
down_revision = ("a7t1m2e3s4h5", "lv1a2b3c4d5e")
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB
TABLES = ("attendance_leave_links", "lop_period_entries", "attendance_kiosks", "kiosk_pins", "ts_billing_rates",
          "ts_invoices", "leave_bradford_alerts", "leave_expiry_reminders")


def _id():
    return sa.Column("id", sa.Integer(), primary_key=True)


def _uuid():
    return sa.Column("uuid", postgresql.UUID(as_uuid=True), nullable=False, unique=True,
                     server_default=sa.text("gen_random_uuid()"))


def _company():
    return sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False)


def _created():
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"))


def _updated():
    return sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"))


def _emp(nullable=False):
    return sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=nullable)


def upgrade():
    op.add_column("attendance_days", sa.Column("pay_cut_days", sa.Numeric(4, 2), nullable=False,
                                               server_default=sa.text("0")))
    op.add_column("work_arrangements", sa.Column("auto_shift", sa.Boolean(), nullable=False,
                                                 server_default=sa.text("false")))

    op.create_table(
        "attendance_leave_links", _id(), _uuid(), _company(), _emp(),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("days", sa.Numeric(4, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("unit", sa.String(12), nullable=False, server_default=sa.text("'day'")),
        sa.Column("leave_type_id", sa.Integer(), sa.ForeignKey("leave_types.id"), nullable=True),
        sa.Column("leave_request_id", sa.Integer(), sa.ForeignKey("leave_requests.id"), nullable=True),
        sa.Column("comp_off_id", sa.Integer(), sa.ForeignKey("comp_off_requests.id"), nullable=True),
        sa.Column("workflow_instance_id", sa.Integer(), sa.ForeignKey("workflow_instances.id"), nullable=True),
        sa.Column("note", sa.String(300), nullable=True),
        sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reverse_reason", sa.String(300), nullable=True),
        _created(), _updated(),
    )
    op.create_index("ix_attendance_leave_links_company_employee_date", "attendance_leave_links",
                    ["company_id", "employee_id", "date"])
    op.create_index("ix_attendance_leave_links_company_status", "attendance_leave_links",
                    ["company_id", "kind", "status"])

    op.create_table(
        "lop_period_entries", _id(), _uuid(), _company(), _emp(),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("days", sa.Numeric(6, 2), nullable=False),
        sa.Column("note", sa.String(300), nullable=True),
        sa.Column("meta", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("void", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_by_name", sa.String(200), nullable=True),
        _created(),
    )
    op.create_index("ix_lop_period_entries_company_period", "lop_period_entries",
                    ["company_id", "period_start", "employee_id"])

    op.create_table(
        "attendance_kiosks", _id(), _uuid(), _company(),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("location_note", sa.String(200), nullable=True),
        sa.Column("token_prefix", sa.String(16), nullable=True),
        sa.Column("token_hash", sa.String(64), nullable=True),
        sa.Column("photo", sa.String(12), nullable=False, server_default=sa.text("'off'")),
        sa.Column("allowed_ips", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("punches_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        _created(), _updated(),
    )
    op.create_index("ix_attendance_kiosks_company", "attendance_kiosks", ["company_id", "active"])
    op.create_index("ix_attendance_kiosks_prefix", "attendance_kiosks", ["token_prefix"])

    op.create_table(
        "kiosk_pins", _id(), _company(), _emp(),
        sa.Column("pin_hash", sa.String(200), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("set_by_name", sa.String(200), nullable=True),
        _created(), _updated(),
        sa.UniqueConstraint("employee_id", name="uq_kiosk_pins_employee"),
    )
    op.create_index("ix_kiosk_pins_company_employee", "kiosk_pins", ["company_id", "employee_id"])

    op.add_column("ts_clients", sa.Column("address", sa.Text(), nullable=True))
    op.add_column("ts_clients", sa.Column("state_code", sa.String(4), nullable=True))

    op.create_table(
        "ts_billing_rates", _id(), _uuid(), _company(),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("ts_projects.id"), nullable=True),
        _emp(nullable=True),
        sa.Column("rate", sa.Numeric(12, 2), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("note", sa.String(200), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        _created(), _updated(),
    )
    op.create_index("ix_ts_billing_rates_company", "ts_billing_rates", ["company_id", "project_id", "employee_id"])

    op.create_table(
        "ts_invoices", _id(), _uuid(), _company(),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("ts_clients.id"), nullable=False),
        sa.Column("number", sa.String(40), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("issue_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(12), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("supplier", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("place_of_supply", sa.String(4), nullable=True),
        sa.Column("tax_mode", sa.String(12), nullable=False, server_default=sa.text("'intra'")),
        sa.Column("gst_rate", sa.Numeric(5, 2), nullable=False, server_default=sa.text("18")),
        sa.Column("sac_code", sa.String(10), nullable=True),
        sa.Column("lines", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("hours", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("subtotal", sa.Numeric(14, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("cgst", sa.Numeric(14, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("sgst", sa.Numeric(14, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("igst", sa.Numeric(14, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("total", sa.Numeric(14, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_reference", sa.String(120), nullable=True),
        sa.Column("created_by_name", sa.String(200), nullable=True),
        _created(), _updated(),
    )
    op.create_index("ix_ts_invoices_company", "ts_invoices", ["company_id", "status", "issue_date"])
    op.create_index("ix_ts_invoices_client", "ts_invoices", ["company_id", "client_id"])
    op.add_column("ts_time_logs", sa.Column("invoice_id", sa.Integer(), sa.ForeignKey("ts_invoices.id"), nullable=True))

    op.create_table(
        "leave_bradford_alerts", _id(), _uuid(), _company(), _emp(),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(80), nullable=True),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("spells", sa.Integer(), nullable=False),
        sa.Column("days", sa.Numeric(6, 2), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        _created(),
    )
    op.create_index("ix_leave_bradford_alerts_company_employee", "leave_bradford_alerts", ["company_id", "employee_id"])

    op.create_table(
        "leave_expiry_reminders", _id(), _company(), _emp(),
        sa.Column("ledger_entry_id", sa.Integer(), sa.ForeignKey("leave_ledger.id"), nullable=False),
        sa.Column("days_before", sa.Integer(), nullable=False),
        sa.Column("expiring_days", sa.Numeric(6, 2), nullable=False),
        sa.Column("expires_on", sa.Date(), nullable=False),
        sa.Column("emailed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("ledger_entry_id", "days_before", name="uq_leave_expiry_reminders_entry"),
    )
    op.create_index("ix_leave_expiry_reminders_company", "leave_expiry_reminders", ["company_id", "employee_id"])

    for table in TABLES:
        for sql in enable_tenant_rls_sql(table):
            op.execute(sql)


def downgrade():
    for table in TABLES:
        for sql in disable_tenant_rls_sql(table):
            op.execute(sql)
    op.drop_column("ts_time_logs", "invoice_id")
    for table in reversed(TABLES):
        op.drop_table(table)
    op.drop_column("ts_clients", "state_code")
    op.drop_column("ts_clients", "address")
    op.drop_column("work_arrangements", "auto_shift")
    op.drop_column("attendance_days", "pay_cut_days")
