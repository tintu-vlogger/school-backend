"""Database engine and sessions, with tenant isolation by Postgres row-level security.

Every company-owned table has a policy that only lets rows of `app.company_id` through
(or every row when `app.bypass_rls` is 'on'). At the start of every transaction the
session sets both values with `set_config(..., true)` (transaction-local), taken from
the session's own `info` or else from the request context.

If the database login is a superuser (local development), RLS would be skipped, so
each new connection switches to the unprivileged `school_app` role with SET ROLE.
"""
import json
from collections.abc import Generator
from contextlib import contextmanager

import psycopg2.extras
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from core import context
from core.config import get_settings

# Ensure psycopg2 registers JSON/JSONB adapters so JSONB columns
# always return Python dicts, not raw JSON strings.
psycopg2.extras.register_default_jsonb(globally=True, loads=json.loads)
psycopg2.extras.register_default_json(globally=True, loads=json.loads)

settings = get_settings()

engine = create_engine(settings.database_url, pool_pre_ping=True, pool_size=10, max_overflow=20, pool_recycle=1800, pool_timeout=10)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    # Fetch server defaults (uuid, created_at) with RETURNING right after insert, so new
    # rows are complete without another query (the audit log uses them too).
    __mapper_args__ = {"eager_defaults": True}


# ── Unprivileged role for superuser logins ──────────────────────────

@event.listens_for(engine, "connect")
def _drop_privileges(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    # Timestamps leave the database in UTC; the web app shows them in the viewer's zone.
    cur.execute("SET TIME ZONE 'UTC'")
    dbapi_conn.commit()
    role = settings.DB_APP_ROLE
    if not role:
        cur.close()
        return
    try:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s), "
            "(SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user)",
            (role,),
        )
        exists, privileged = cur.fetchone()
        if exists and privileged:
            cur.execute(f'SET ROLE "{role}"')
        dbapi_conn.commit()
    finally:
        cur.close()


# ── Tenant settings at every transaction start ──────────────────────

def _tenant_values(session: Session) -> tuple[str, str]:
    ctx = context.current()
    company_id = session.info.get("company_id", ctx.company_id)
    bypass = session.info.get("bypass_rls", ctx.bypass_rls)
    return ("" if company_id is None else str(int(company_id))), ("on" if bypass else "off")


def _apply(session: Session, connection) -> None:
    company_id, bypass = _tenant_values(session)
    connection.execute(
        text("SELECT set_config('app.company_id', :c, true), set_config('app.bypass_rls', :b, true)"),
        {"c": company_id, "b": bypass},
    )


@event.listens_for(Session, "after_begin")
def _after_begin(session, _transaction, connection):
    _apply(session, connection)


@event.listens_for(Session, "before_flush")
def _fill_company_id(session, _flush_context, _instances):
    """New company-owned rows get the current company when the code left it empty."""
    company_id = session.info.get("company_id", context.current().company_id)
    if company_id is None:
        return
    for obj in session.new:
        table = getattr(type(obj), "__table__", None)
        if table is not None and "company_id" in table.columns and getattr(obj, "company_id", None) is None:
            obj.company_id = company_id


def _reapply_now(session: Session) -> None:
    if session.in_transaction():
        _apply(session, session.connection())


def set_tenant(session: Session, company_id: int | None) -> None:
    """Scope this session (and the request) to one company."""
    session.info["company_id"] = company_id
    ctx = context.current()
    if company_id is not None:
        ctx.company_id = company_id
    _reapply_now(session)


def enable_bypass(session: Session) -> None:
    """Let this session see every company's rows. Only for platform admin screens,
    sign-in lookups across companies and background jobs."""
    session.info["bypass_rls"] = True
    _reapply_now(session)


def disable_bypass(session: Session) -> None:
    session.info["bypass_rls"] = False
    _reapply_now(session)


@contextmanager
def bypass_rls(session: Session):
    """Temporarily see every company's rows inside a block."""
    before = session.info.get("bypass_rls", False)
    enable_bypass(session)
    try:
        yield session
    finally:
        session.info["bypass_rls"] = before
        _reapply_now(session)


def system_session() -> Session:
    """A session for startup tasks and background jobs that work across companies."""
    db = SessionLocal()
    db.info["bypass_rls"] = True
    return db


def tenant_session(company_id: int) -> Session:
    """A session scoped to one company (background jobs that act for a company)."""
    db = SessionLocal()
    db.info["company_id"] = company_id
    return db


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
