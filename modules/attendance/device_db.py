"""Direct database read: punches straight from the attendance software's database
(SQL Server, MySQL / MariaDB, PostgreSQL, Oracle; MS Access on the office connector).

Settings (source_config of a device with connection "database"):

    engine mssql | mysql | postgresql | oracle | access (| sqlite outside production, for tests)
    host, port, database, username, password, options ("tds_version=7.0&charset=utf8")
    mode "table": table (a table or view; {M} {MM} {YYYY} {YY} make monthly tables such as
                  eSSL's DeviceLogs_{M}_{YYYY}) and the mapped columns
    mode "query": a read-only SELECT with :since (the last punch time, or the last id)
    incremental "time" | "id", lookback_days (first run), batch (rows per run), every_minutes
    mapping (device_mapping fields; for databases they are column names)

Reads are read-only: only SELECT / WITH queries are run, inside a transaction that is
rolled back; give us a database user that can only read.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from core.errors import BadRequest
from modules.attendance import device_mapping as fm
from modules.attendance.device_common import check_public_host, need, store_secrets
from modules.attendance.device_presets import ENGINES

SECRET_KEYS = ("password",)
_FORBIDDEN = re.compile(r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|exec|execute|call|into|"
                        r"attach|detach|pragma|shutdown|backup|restore|xp_\w+|sp_\w+|dbms_\w+|utl_\w+)\b", re.I)
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_ $#{}]*(\.[A-Za-z_][A-Za-z0-9_ $#{}]*){0,2}$")
_DRIVERNAMES = {"mssql": "mssql+pymssql", "mysql": "mysql+pymysql", "postgresql": "postgresql+psycopg2",
                "oracle": "oracle+oracledb", "sqlite": "sqlite"}


def engines_allowed() -> set[str]:
    from modules.attendance.device_common import allow_private_hosts
    out = set(ENGINES)
    if allow_private_hosts():
        out.add("sqlite")  # development and tests only: a file on the server
    return out


def check_query(sql: str) -> str:
    """Only one read-only SELECT (or WITH ... SELECT) statement."""
    q = (sql or "").strip().rstrip(";").strip()
    if not q:
        raise BadRequest("Write the query that reads the punches.")
    if ";" in q:
        raise BadRequest("Use one SELECT statement, without semicolons.")
    if not re.match(r"^(select|with)\b", q, re.I):
        raise BadRequest("The query must start with SELECT (or WITH). We only read.")
    stripped = re.sub(r"'(?:[^']|'')*'", "''", q)  # ignore words inside string literals
    bad = _FORBIDDEN.search(stripped)
    if bad:
        raise BadRequest(f"The query can't use {bad.group(0).upper()}. We only read.")
    return q


def clean(cfg: dict, old: dict | None) -> dict:
    eng = cfg.get("engine") or "mssql"
    if eng not in engines_allowed():
        raise BadRequest("Choose the kind of database.")
    mode = "query" if cfg.get("mode") == "query" else "table"
    out = {
        "preset": (cfg.get("preset") or "custom")[:40], "engine": eng,
        "host": (cfg.get("host") or "").strip()[:255],
        "port": int(cfg.get("port") or ENGINES.get(eng, {}).get("port") or 0) or None,
        "database": (cfg.get("database") or "").strip()[:255],
        "username": (cfg.get("username") or "").strip()[:200],
        "options": (cfg.get("options") or "").strip().lstrip("?")[:500],
        "file_path": (cfg.get("file_path") or "").strip()[:500],
        "odbc_driver": (cfg.get("odbc_driver") or "").strip()[:200],
        "mode": mode, "table": (cfg.get("table") or "").strip()[:200], "query": str(cfg.get("query") or "")[:10000],
        "incremental": "id" if cfg.get("incremental") == "id" else "time",
        "lookback_days": max(0, min(int(cfg.get("lookback_days") if cfg.get("lookback_days") not in (None, "") else 7), 365)),
        "batch": max(100, min(int(cfg.get("batch") or 5000), 5000)),
        "every_minutes": max(5, min(int(cfg.get("every_minutes") or 15), 1440)),
    }
    if eng in ("access", "sqlite"):
        if not out["file_path"]:
            raise BadRequest("Enter the path of the database file.")
    elif not out["host"]:
        raise BadRequest("Enter the database server address.")
    if mode == "table":
        if not out["table"] or not _IDENT.match(out["table"]):
            raise BadRequest("Enter the table or view name (letters, digits and underscores; schema.table is fine).")
    else:
        out["query"] = check_query(out["query"])
    return store_secrets(out, SECRET_KEYS, cfg, old)


# ── Connection ──────────────────────────────────────────────────────

def url_for(cfg: dict):
    from sqlalchemy.engine import URL
    eng = cfg.get("engine")
    if eng == "access":
        raise BadRequest("MS Access files are read on the office PC. Choose 'Office connector' for this device.")
    if eng == "sqlite":
        return URL.create("sqlite", database=cfg.get("file_path"))
    if eng not in _DRIVERNAMES:
        raise BadRequest("Choose the kind of database.")
    meta = ENGINES[eng]
    need(meta["driver"], meta["package"], f"Reading {meta['label']}")
    query = {}
    for part in (cfg.get("options") or "").split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            query[k.strip()] = v.strip()
    if eng == "oracle":
        if cfg.get("database"):
            query.setdefault("service_name", cfg["database"])
        return URL.create(_DRIVERNAMES[eng], username=cfg.get("username") or None, password=cfg.get("password") or None,
                          host=cfg.get("host"), port=cfg.get("port"), query=query)
    return URL.create(_DRIVERNAMES[eng], username=cfg.get("username") or None, password=cfg.get("password") or None,
                      host=cfg.get("host"), port=cfg.get("port"), database=cfg.get("database") or None, query=query)


def make_engine(cfg: dict):
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool
    eng = cfg.get("engine")
    if eng not in ("sqlite", "access"):
        check_public_host(cfg.get("host") or "")
    args = {"mssql": {"login_timeout": 15, "timeout": 120}, "mysql": {"connect_timeout": 15, "read_timeout": 120},
            "postgresql": {"connect_timeout": 15}, "oracle": {"tcp_connect_timeout": 15}}.get(eng, {})
    return create_engine(url_for(cfg), poolclass=NullPool, connect_args=args)


def _friendly(exc: Exception) -> str:
    msg = str(getattr(exc, "orig", None) or exc).strip().replace("\n", " ")
    low = msg.lower()
    if "login failed" in low or "access denied" in low or "authentication failed" in low or "invalid username" in low:
        return "The database refused the sign-in. Check the user name and password."
    if "timed out" in low or "timeout" in low or "unable to connect" in low or "could not connect" in low or "connection refused" in low:
        return "Couldn't reach the database server. Check the address and port, and that it accepts connections from us."
    return f"The database said: {msg[:300]}"


# ── Queries ─────────────────────────────────────────────────────────

def _quote(dialect, name: str) -> str:
    return ".".join(dialect.identifier_preparer.quote(p.strip()) for p in name.split("."))


def tables_for(template: str, since: datetime | None, now: datetime) -> list[str]:
    """Monthly tables from the month of `since` to this month (at most 24)."""
    if not any(t in template for t in ("{M}", "{MM}", "{YYYY}", "{YY}")):
        return [template]
    start = (since or now).date().replace(day=1)
    months, cur = [], date(now.year, now.month, 1)
    while cur >= start and len(months) < 24:
        months.append(cur)
        cur = (cur - timedelta(days=1)).replace(day=1)
    return [template.replace("{MM}", f"{m.month:02d}").replace("{M}", str(m.month))
            .replace("{YYYY}", str(m.year)).replace("{YY}", f"{m.year % 100:02d}") for m in reversed(months)]


def _columns(mapping: dict) -> list[str]:
    cols = []
    for k in ("employee", "timestamp", "date", "time", "direction", "device", "id"):
        c = mapping.get(k)
        if c and c not in cols:
            cols.append(c)
    return cols


def table_sql(dialect, table: str, mapping: dict, incremental: str) -> str:
    inc = mapping.get("id") if incremental == "id" else (mapping.get("timestamp") or mapping.get("date"))
    if not inc:
        raise BadRequest("Choose the id column to read by id." if incremental == "id" else "Choose the punch time column.")
    for c in _columns(mapping):
        if not _IDENT.match(c):
            raise BadRequest(f"“{c}” isn't a valid column name.")
    cols = ", ".join(_quote(dialect, c) for c in _columns(mapping))
    op = ">" if incremental == "id" else ">="
    return f"SELECT {cols} FROM {_quote(dialect, table)} WHERE {_quote(dialect, inc)} {op} :since ORDER BY {_quote(dialect, inc)}"


def since_value(cfg: dict, state: dict, tz: ZoneInfo, now: datetime):
    if cfg.get("incremental") == "id":
        return int(state.get("last_id") or 0)
    if state.get("last_time"):
        return datetime.fromisoformat(state["last_time"])
    local_now = now.astimezone(tz).replace(tzinfo=None, microsecond=0)
    return local_now - timedelta(days=int(cfg.get("lookback_days") if cfg.get("lookback_days") is not None else 7))


def _run(conn, sql: str, params: dict, limit: int) -> tuple[list[str], list[dict]]:
    from sqlalchemy import text
    res = conn.execute(text(sql), params)
    cols = list(res.keys())
    rows = [dict(zip(cols, r)) for r in res.fetchmany(limit)]
    res.close()
    return cols, rows


def _read_only(conn, eng: str) -> None:
    from sqlalchemy import text
    try:
        if eng in ("postgresql", "mysql"):
            conn.execute(text("SET TRANSACTION READ ONLY"))
        elif eng == "sqlite":
            conn.exec_driver_sql("PRAGMA query_only = ON")
    except Exception:
        pass


def fetch_rows(cfg: dict, mapping: dict, since, limit: int, engine=None, now: datetime | None = None) -> tuple[list[str], list[dict], list[str]]:
    """Columns, rows and notes, read in a rolled-back transaction."""
    now = now or datetime.now(timezone.utc)
    own = engine is None
    engine = engine or make_engine(cfg)
    notes, rows, cols = [], [], []
    try:
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                _read_only(conn, cfg.get("engine"))
                if cfg.get("mode") == "query":
                    sql = check_query(cfg.get("query") or "")
                    params = {"since": since} if ":since" in sql else {}
                    if not params:
                        notes.append("The query has no :since, so every run reads all of its rows.")
                    cols, rows = _run(conn, sql, params, limit)
                else:
                    tables = tables_for(cfg["table"], since if isinstance(since, datetime) else None, now)
                    errors = []
                    for t in tables:
                        sql = table_sql(engine.dialect, t, mapping, cfg.get("incremental"))
                        try:
                            nested = conn.begin_nested() if len(tables) > 1 else None
                            c, r = _run(conn, sql, {"since": since}, limit - len(rows))
                            if nested is not None:
                                nested.commit()
                        except Exception as exc:  # a monthly table that doesn't exist yet
                            if nested is not None:
                                nested.rollback()
                            errors.append(f"{t}: {_friendly(exc)}")
                            continue
                        cols = cols or c
                        rows.extend(r)
                        notes.append(f"{t}: {len(r)} rows")
                        if len(rows) >= limit:
                            break
                    if errors and len(errors) == len(tables):
                        raise BadRequest(errors[0])
                    if errors and len(tables) > 1:
                        notes.append(f"{len(errors)} monthly table(s) not found")
            finally:
                trans.rollback()
    except BadRequest:
        raise
    except Exception as exc:
        raise BadRequest(_friendly(exc))
    finally:
        if own:
            engine.dispose()
    return cols, rows, notes


def list_tables(cfg: dict, engine=None) -> list[str]:
    from sqlalchemy import inspect
    own = engine is None
    engine = engine or make_engine(cfg)
    try:
        insp = inspect(engine)
        names = list(insp.get_table_names()) + list(insp.get_view_names())
        return sorted(set(names))[:500]
    except Exception as exc:
        raise BadRequest(_friendly(exc))
    finally:
        if own:
            engine.dispose()


def read(cfg: dict, mapping: dict, state: dict, tz: ZoneInfo, engine=None, now: datetime | None = None
         ) -> tuple[list[dict], list[dict], dict, list[str]]:
    """(rows, records, new state, notes) for one incremental read."""
    now = now or datetime.now(timezone.utc)
    since = since_value(cfg, state, tz, now)
    limit = int(cfg.get("batch") or 5000)
    _, rows, notes = fetch_rows(cfg, mapping, since, limit, engine, now)
    records = fm.to_records(rows, mapping, tz)
    state = dict(state)
    if cfg.get("incremental") == "id":
        ids = [r.get("_id") for r in records if isinstance(r.get("_id"), (int, float))]
        if ids:
            state["last_id"] = int(max(ids))
    else:
        locals_ = [r["_local"] for r in records if isinstance(r.get("_local"), datetime)]
        if not locals_:
            locals_ = [r["timestamp"].astimezone(tz).replace(tzinfo=None) for r in records if isinstance(r.get("timestamp"), datetime)]
        if locals_:
            newest = max(x.replace(tzinfo=None) for x in locals_)
            if not state.get("last_time") or newest.isoformat() > state["last_time"]:
                state["last_time"] = newest.isoformat()
    state["more"] = len(rows) >= limit
    return rows, records, state, notes
