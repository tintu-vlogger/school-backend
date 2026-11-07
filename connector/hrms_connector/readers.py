"""Reading punches on the office network: the attendance software's database (SQL Server,
MySQL, PostgreSQL, Oracle, MS Access, SQLite) and ZKTeco-protocol devices (port 4370).

The settings come from the HR system (the same ones shown on its Devices page). Each read
returns punches in the push format, newest position included, so the next read continues
from there.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal

_FORBIDDEN = re.compile(r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|exec|execute|call|into|"
                        r"attach|detach|pragma|shutdown|backup|restore|xp_\w+|sp_\w+|dbms_\w+|utl_\w+)\b", re.I)
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_ $#{}]*(\.[A-Za-z_][A-Za-z0-9_ $#{}]*){0,2}$")
DRIVERS = {"mssql": ("mssql+pymssql", "pymssql"), "mysql": ("mysql+pymysql", "pymysql"),
           "postgresql": ("postgresql+psycopg2", "psycopg2"), "oracle": ("oracle+oracledb", "oracledb"),
           "sqlite": ("sqlite", None)}
STATE_DIRECTION = {0: "in", 1: "out", 2: "out", 3: "in", 4: "in", 5: "out"}


class ReadError(Exception):
    pass


def _need(module: str, package: str, what: str):
    import importlib
    try:
        return importlib.import_module(module)
    except ImportError:
        raise ReadError(f"{what} needs the '{package}' package. Install it with: pip install {package}")


# ── Mapping rows to punches ─────────────────────────────────────────

def _get(row: dict, name: str | None):
    if not name:
        return None
    if name in row:
        return row[name]
    low = {str(k).lower(): v for k, v in row.items()}
    return low.get(name.lower())


def _values(s) -> set[str]:
    return {x.strip().lower() for x in str(s or "").split(",") if x.strip()}


def _scalar(v):
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v


def direction_of(value, in_values, out_values) -> str | None:
    v = str(_scalar(value) if value is not None else "").strip().lower()
    ins, outs = _values(in_values), _values(out_values)
    if ins or outs:
        return "in" if v in ins else "out" if v in outs else None
    if v in ("in", "i", "0", "checkin", "check-in", "entry"):
        return "in"
    if v in ("out", "o", "1", "checkout", "check-out", "exit"):
        return "out"
    return None


def local_time(row: dict, m: dict):
    """The punch time as a datetime without a zone (the device's local time) where possible."""
    if m.get("timestamp"):
        v = _get(row, m["timestamp"])
    else:
        d, t = _get(row, m.get("date")), _get(row, m.get("time"))
        if isinstance(d, datetime):
            d = d.date()
        if isinstance(t, datetime):
            t = t.time()
        if isinstance(d, date) and isinstance(t, time):
            v = datetime.combine(d, t)
        else:
            v = f"{d} {t}".strip()
    if isinstance(v, str):
        fmt = m.get("timestamp_format")
        try:
            if fmt and fmt not in ("iso", "epoch", "epoch_ms"):
                v = datetime.strptime(v.strip(), fmt)
            elif "Z" not in v and "+" not in v[10:]:
                v = datetime.fromisoformat(v.strip())
        except ValueError:
            pass
    return v


def to_punches(rows: list[dict], m: dict) -> list[dict]:
    out = []
    for r in rows:
        uid = _scalar(_get(r, m.get("employee")))
        if uid in (None, ""):
            continue
        ts = local_time(r, m)
        if isinstance(ts, datetime):
            ts_out = ts.replace(microsecond=0).isoformat() if ts.tzinfo else ts.replace(microsecond=0).isoformat(sep="T")
        else:
            ts_out = str(_scalar(ts) or "")[:40]
        p = {"device_user_id": str(uid).strip()[:60], "timestamp": ts_out}
        if m.get("direction"):
            p["direction"] = direction_of(_get(r, m["direction"]), m.get("in_values"), m.get("out_values"))
        if m.get("device"):
            term = _scalar(_get(r, m["device"]))
            if term not in (None, ""):
                p["terminal"] = str(term)[:80]
        out.append(p)
    return out


# ── Databases ───────────────────────────────────────────────────────

def check_query(sql: str) -> str:
    q = (sql or "").strip().rstrip(";").strip()
    if not q or ";" in q or not re.match(r"^(select|with)\b", q, re.I):
        raise ReadError("The query must be one SELECT statement.")
    bad = _FORBIDDEN.search(re.sub(r"'(?:[^']|'')*'", "''", q))
    if bad:
        raise ReadError(f"The query can't use {bad.group(0).upper()}.")
    return q


def tables_for(template: str, since, now: datetime) -> list[str]:
    if not any(t in template for t in ("{M}", "{MM}", "{YYYY}", "{YY}")):
        return [template]
    start = (since if isinstance(since, datetime) else now).date().replace(day=1)
    months, cur = [], date(now.year, now.month, 1)
    while cur >= start and len(months) < 24:
        months.append(cur)
        cur = (cur - timedelta(days=1)).replace(day=1)
    return [template.replace("{MM}", f"{x.month:02d}").replace("{M}", str(x.month))
            .replace("{YYYY}", str(x.year)).replace("{YY}", f"{x.year % 100:02d}") for x in reversed(months)]


def _cols(m: dict) -> list[str]:
    out = []
    for k in ("employee", "timestamp", "date", "time", "direction", "device", "id"):
        if m.get(k) and m[k] not in out:
            if not _IDENT.match(m[k]):
                raise ReadError(f"“{m[k]}” isn't a valid column name.")
            out.append(m[k])
    return out


def since_value(s: dict, cursor: dict, now: datetime):
    if s.get("incremental") == "id":
        return int(cursor.get("last_id") or 0)
    if cursor.get("last_time"):
        return datetime.fromisoformat(cursor["last_time"])
    return now - timedelta(days=int(s.get("lookback_days") if s.get("lookback_days") is not None else 7))


class _Access:
    """MS Access through ODBC (Windows, with the Access Database Engine installed)."""

    def __init__(self, s: dict):
        pyodbc = _need("pyodbc", "pyodbc", "Reading an MS Access file")
        driver = s.get("odbc_driver") or "Microsoft Access Driver (*.mdb, *.accdb)"
        conn_str = f"DRIVER={{{driver}}};DBQ={s.get('file_path')};"
        if s.get("password"):
            conn_str += f"PWD={s['password']};"
        try:
            self.conn = pyodbc.connect(conn_str, readonly=True, timeout=30)
        except Exception as exc:
            raise ReadError(f"Couldn't open {s.get('file_path')}: {exc}. Is the Microsoft Access Database Engine "
                            f"installed (same 32/64-bit as this program)?")

    @staticmethod
    def quote(name: str) -> str:
        return ".".join(f"[{p.strip()}]" for p in name.split("."))

    def run(self, sql: str, since, limit: int):
        n = sql.count(":since")
        cur = self.conn.cursor()
        cur.execute(sql.replace(":since", "?"), *([since] * n))
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchmany(limit)]
        cur.close()
        return cols, rows

    def close(self):
        self.conn.close()


class _Sql:
    """Every other database through SQLAlchemy, in a rolled-back read-only transaction."""

    def __init__(self, s: dict):
        sa = _need("sqlalchemy", "sqlalchemy", "Reading a database")
        from sqlalchemy.engine import URL
        from sqlalchemy.pool import NullPool
        eng = s.get("engine")
        if eng not in DRIVERS:
            raise ReadError(f"Unknown database kind: {eng}")
        drivername, module = DRIVERS[eng]
        if module:
            _need(module, {"psycopg2": "psycopg2-binary"}.get(module, module), f"Reading {eng}")
        query = dict(p.split("=", 1) for p in (s.get("options") or "").split("&") if "=" in p)
        if eng == "sqlite":
            url = URL.create("sqlite", database=s.get("file_path"))
        elif eng == "oracle":
            if s.get("database"):
                query.setdefault("service_name", s["database"])
            url = URL.create(drivername, username=s.get("username") or None, password=s.get("password") or None,
                             host=s.get("host"), port=s.get("port"), query=query)
        else:
            url = URL.create(drivername, username=s.get("username") or None, password=s.get("password") or None,
                             host=s.get("host"), port=s.get("port"), database=s.get("database") or None, query=query)
        self.engine = sa.create_engine(url, poolclass=NullPool)
        self.eng = eng
        self.text = sa.text

    def quote(self, name: str) -> str:
        prep = self.engine.dialect.identifier_preparer
        return ".".join(prep.quote(p.strip()) for p in name.split("."))

    def run(self, sql: str, since, limit: int):
        with self.engine.connect() as conn:
            trans = conn.begin()
            try:
                if self.eng in ("postgresql", "mysql"):
                    try:
                        conn.execute(self.text("SET TRANSACTION READ ONLY"))
                    except Exception:
                        pass
                res = conn.execute(self.text(sql), {"since": since} if ":since" in sql else {})
                cols = list(res.keys())
                rows = [dict(zip(cols, r)) for r in res.fetchmany(limit)]
                res.close()
                return cols, rows
            finally:
                trans.rollback()

    def close(self):
        self.engine.dispose()


def open_db(s: dict):
    return _Access(s) if s.get("engine") == "access" else _Sql(s)


def read_database(s: dict, m: dict, cursor: dict, now: datetime | None = None, limit: int | None = None,
                  db=None) -> tuple[list[dict], list[dict], dict]:
    """(rows, punches, new cursor)."""
    now = now or datetime.now()
    limit = limit or int(s.get("batch") or 5000)
    since = since_value(s, cursor, now)
    own = db is None
    db = db or open_db(s)
    rows: list[dict] = []
    try:
        if s.get("mode") == "query":
            _, rows = db.run(check_query(s.get("query") or ""), since, limit)
        else:
            inc = m.get("id") if s.get("incremental") == "id" else (m.get("timestamp") or m.get("date"))
            cols = ", ".join(db.quote(c) for c in _cols(m))
            op = ">" if s.get("incremental") == "id" else ">="
            tables = tables_for(s.get("table") or "", since, now)
            errors = []
            for t in tables:
                sql = f"SELECT {cols} FROM {db.quote(t)} WHERE {db.quote(inc)} {op} :since ORDER BY {db.quote(inc)}"
                try:
                    _, r = db.run(sql, since, limit - len(rows))
                except Exception as exc:
                    errors.append(f"{t}: {exc}")
                    continue
                rows.extend(r)
                if len(rows) >= limit:
                    break
            if errors and len(errors) == len(tables):
                raise ReadError(errors[0][:300])
    except ReadError:
        raise
    except Exception as exc:
        raise ReadError(str(exc)[:300])
    finally:
        if own:
            db.close()
    punches = to_punches(rows, m)
    new = dict(cursor)
    if s.get("incremental") == "id":
        ids = [_scalar(_get(r, m.get("id"))) for r in rows]
        ids = [i for i in ids if isinstance(i, (int, float))]
        if ids:
            new["last_id"] = int(max(ids))
    else:
        times = [t for t in (local_time(r, m) for r in rows) if isinstance(t, datetime)]
        if times:
            newest = max(t.replace(tzinfo=None) for t in times).isoformat()
            if not new.get("last_time") or newest > new["last_time"]:
                new["last_time"] = newest
    new["more"] = len(rows) >= limit
    return rows, punches, new


# ── ZKTeco devices (port 4370) ──────────────────────────────────────

def read_zk(s: dict, cursor: dict, now: datetime | None = None, factory=None) -> tuple[list[dict], dict, dict]:
    """(punches, new cursor, device info)."""
    if factory is None:
        factory = _need("zk", "pyzk", "Reading a ZKTeco device").ZK
    now = now or datetime.now()
    zk = factory(s["ip"], port=int(s.get("port") or 4370), timeout=int(s.get("timeout") or 15),
                 password=int(s.get("password") or 0), force_udp=s.get("protocol") == "udp", ommit_ping=True)
    try:
        conn = zk.connect()
    except Exception as exc:
        raise ReadError(f"Couldn't connect to the device at {s['ip']}: {exc}")
    info = {}
    try:
        try:
            conn.disable_device()
        except Exception:
            pass
        try:
            info["serial"] = str(conn.get_serialnumber())
        except Exception:
            pass
        logs = conn.get_attendance() or []
    finally:
        try:
            conn.enable_device()
            conn.disconnect()
        except Exception:
            pass
    last = datetime.fromisoformat(cursor["last_time"]) if cursor.get("last_time") else now - timedelta(
        days=int(s.get("lookback_days") or 30))
    punches, newest = [], None
    for a in logs:
        ts = getattr(a, "timestamp", None)
        if not isinstance(ts, datetime) or ts < last:
            continue
        p = {"device_user_id": str(getattr(a, "user_id", "")).strip(), "timestamp": ts.replace(microsecond=0).isoformat()}
        if s.get("use_status"):
            p["direction"] = STATE_DIRECTION.get(int(getattr(a, "punch", -1)))
        punches.append(p)
        newest = ts if newest is None or ts > newest else newest
    new = dict(cursor)
    if newest:
        new["last_time"] = newest.replace(microsecond=0).isoformat()
    return punches, new, info
