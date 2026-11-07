"""SQL helpers for migrations: the application role and the tenant policy.

Used by Alembic revisions so every company-owned table gets exactly the same policy.
"""
APP_ROLE = "school_app"

_COMPANY = "NULLIF(current_setting('app.company_id', true), '')::int"
_BYPASS = "current_setting('app.bypass_rls', true) = 'on'"
TENANT_CONDITION = f"(company_id = {_COMPANY} OR {_BYPASS})"


def ensure_app_role_sql() -> list[str]:
    """Create the unprivileged role (when allowed) and give it table access."""
    return [
        f"""
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
            BEGIN
              CREATE ROLE {APP_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS;
            EXCEPTION WHEN insufficient_privilege THEN
              RAISE NOTICE 'Cannot create role {APP_ROLE}; the API login itself must not be a superuser';
            END;
          END IF;
        END $$;
        """,
        f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
            EXECUTE 'GRANT USAGE ON SCHEMA public TO {APP_ROLE}';
            EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}';
            EXECUTE 'GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}';
            EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}';
            EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {APP_ROLE}';
          END IF;
        END $$;
        """,
    ]


def enable_tenant_rls_sql(table: str, shared_rows_readable: bool = False) -> list[str]:
    """Enable and force RLS on a company-owned table. `shared_rows_readable` also lets
    every company read rows with company_id NULL (shared templates) without changing them."""
    out = [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS tenant_isolation ON {table}",
        f"CREATE POLICY tenant_isolation ON {table} USING {TENANT_CONDITION} WITH CHECK {TENANT_CONDITION}",
    ]
    if shared_rows_readable:
        out += [
            f"DROP POLICY IF EXISTS shared_rows_read ON {table}",
            f"CREATE POLICY shared_rows_read ON {table} FOR SELECT USING (company_id IS NULL)",
        ]
    return out


def disable_tenant_rls_sql(table: str) -> list[str]:
    return [
        f"DROP POLICY IF EXISTS shared_rows_read ON {table}",
        f"DROP POLICY IF EXISTS tenant_isolation ON {table}",
        f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY",
    ]
