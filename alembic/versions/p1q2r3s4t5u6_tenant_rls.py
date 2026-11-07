"""Tenant isolation with row-level security

Adds company_id to employee_settings and roster_entries, creates the unprivileged
application role, and enables + forces RLS with the tenant policy on every
company-owned table.

Revision ID: p1q2r3s4t5u6
Revises: o0p1q2r3s4t5
"""
import sqlalchemy as sa
from alembic import op

from core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql, ensure_app_role_sql

revision = "p1q2r3s4t5u6"
down_revision = "o0p1q2r3s4t5"
branch_labels = None
depends_on = None

TENANT_TABLES = [
    "employees", "employee_settings", "roster_entries", "roles", "audit_logs", "notifications",
    "whatsapp_connections", "subscription_payments", "payment_codes", "commissions",
]
SHARED_TABLES = ["departments", "designations"]  # company_id NULL rows are shared templates


def upgrade():
    for table in ("employee_settings", "roster_entries"):
        op.add_column(table, sa.Column("company_id", sa.Integer(), nullable=True))
        op.execute(f"UPDATE {table} t SET company_id = e.company_id FROM employees e WHERE e.id = t.employee_id")
        op.alter_column(table, "company_id", nullable=False)
        op.create_foreign_key(f"fk_{table}_company_id", table, "companies", ["company_id"], ["id"])
        op.create_index(f"ix_{table}_company_employee", table, ["company_id", "employee_id"])

    for sql in ensure_app_role_sql():
        op.execute(sql)
    for table in TENANT_TABLES:
        for sql in enable_tenant_rls_sql(table):
            op.execute(sql)
    for table in SHARED_TABLES:
        for sql in enable_tenant_rls_sql(table, shared_rows_readable=True):
            op.execute(sql)


def downgrade():
    for table in TENANT_TABLES + SHARED_TABLES:
        for sql in disable_tenant_rls_sql(table):
            op.execute(sql)
    for table in ("employee_settings", "roster_entries"):
        op.drop_index(f"ix_{table}_company_employee", table_name=table)
        op.drop_constraint(f"fk_{table}_company_id", table, type_="foreignkey")
        op.drop_column(table, "company_id")
