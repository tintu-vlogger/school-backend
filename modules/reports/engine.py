"""Run a report: fetch rows from a source within the person's scope, filter, mask what
they may not see, group, total and sort.

Report configuration (stored on a report, or sent for a preview):

    {
      "columns":  ["employee_name", "department", "date_of_joining"],
      "filters":  [{"field": "status", "op": "in", "value": ["confirmed", "probation"]}],
      "group_by": ["department", "date_of_joining:month"],      # up to 2; dates take :day|week|month|quarter|year
      "measures": [{"agg": "count"}, {"field": "overtime_hours", "agg": "sum"}],
      "sort":     [{"field": "department", "dir": "asc"}],
      "totals":   true,
      "params":   {"from": "@month_start", "to": "@today"},       # function sources
      "limit":    500                                           # optional top N
    }

Filter values may be relative: @today, @month_start, @month_end, @last_month_start,
@last_month_end, @quarter_start, @year_start, @fy_start, @fy_end, @month, @year and
@days:-30. Date fields also take the operators this_month, last_month, this_quarter,
this_year, this_fy, last_n_days and next_n_days.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, not_, or_, select
from sqlalchemy.orm import Session

from core.errors import BadRequest, Forbidden
from modules.reports.registry import (SCOPE_LABELS, Field, Principal, Source, max_scope, narrow,
                                      scope_ids)

MAX_ROWS = 100_000
OPS = {
    "eq": "is", "ne": "is not", "in": "is any of", "not_in": "is none of", "contains": "contains",
    "starts": "starts with", "gt": "more than", "gte": "at least", "lt": "less than", "lte": "at most",
    "between": "between", "empty": "is empty", "not_empty": "is not empty", "is_true": "is yes", "is_false": "is no",
    "this_month": "this month", "last_month": "last month", "this_quarter": "this quarter", "this_year": "this year",
    "this_fy": "this financial year", "last_n_days": "in the last N days", "next_n_days": "in the next N days",
}
DATE_OPS = {"this_month", "last_month", "this_quarter", "this_year", "this_fy", "last_n_days", "next_n_days"}
AGGS = {"count": "Count", "count_distinct": "Count of different", "sum": "Total", "avg": "Average",
        "min": "Lowest", "max": "Highest"}
GRAINS = ("day", "week", "month", "quarter", "year")
MASK = None


# ── Dates ──────────────────────────────────────────────────────────

def _month_end(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def _fy_start(d: date) -> date:
    return date(d.year if d.month >= 4 else d.year - 1, 4, 1)


def resolve(value, today: date):
    """Turn a relative token into a value; anything else is returned unchanged."""
    if not isinstance(value, str) or not value.startswith("@"):
        return value
    v = value[1:]
    if v.startswith("days:"):
        return today + timedelta(days=int(v[5:]))
    ms = today.replace(day=1)
    lm_end = ms - timedelta(days=1)
    q_start = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
    table = {
        "today": today, "yesterday": today - timedelta(days=1), "month_start": ms, "month_end": _month_end(today),
        "last_month_start": lm_end.replace(day=1), "last_month_end": lm_end, "quarter_start": q_start,
        "quarter_end": _month_end(date(q_start.year, q_start.month + 2, 1)), "year_start": date(today.year, 1, 1),
        "year_end": date(today.year, 12, 31), "fy_start": _fy_start(today),
        "fy_end": date(_fy_start(today).year + 1, 3, 31), "month": today.strftime("%Y-%m"), "year": today.year,
    }
    if v not in table:
        raise BadRequest(f"Unknown relative value {value}.")
    return table[v]


def date_window(op: str, value, today: date) -> tuple[date, date]:
    if op == "this_month":
        return today.replace(day=1), _month_end(today)
    if op == "last_month":
        e = today.replace(day=1) - timedelta(days=1)
        return e.replace(day=1), e
    if op == "this_quarter":
        s = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
        return s, _month_end(date(s.year, s.month + 2, 1))
    if op == "this_year":
        return date(today.year, 1, 1), date(today.year, 12, 31)
    if op == "this_fy":
        s = _fy_start(today)
        return s, date(s.year + 1, 3, 31)
    n = int(_num(value) or 0)
    if op == "last_n_days":
        return today - timedelta(days=n), today
    return today, today + timedelta(days=n)       # next_n_days


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float, Decimal)):
        return v
    try:
        return Decimal(str(v))
    except Exception:
        raise BadRequest(f"“{v}” is not a number.")


def _to_date(v, today: date):
    v = resolve(v, today)
    if v is None or v == "" or isinstance(v, date):
        return v.date() if isinstance(v, datetime) else v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        raise BadRequest(f"“{v}” is not a date (use YYYY-MM-DD).")


def _coerce(f: Field, v, today: date):
    v = resolve(v, today)
    if f.type in ("date", "datetime"):
        return _to_date(v, today)
    if f.numeric:
        return _num(v)
    if f.type == "bool":
        return v in (True, "true", "1", 1, "yes")
    if f.type == "enum" and f.options and isinstance(f.options[0][0], int) and v not in (None, ""):
        return int(v)
    return v


# ── Configuration checks ───────────────────────────────────────────

def fields_for(source: Source, params: dict) -> dict[str, Field]:
    fm = source.field_map()
    if source.dynamic_fields:
        for f in source.dynamic_fields(params):
            fm[f.key] = f
    return fm


def split_group(key: str) -> tuple[str, str | None]:
    if ":" in key:
        k, g = key.split(":", 1)
        return k, g
    return key, None


def clean_config(source: Source, config: dict | None, params: dict | None = None) -> dict:
    """Check a report configuration against the source and fill defaults."""
    c = dict(config or {})
    fm = fields_for(source, params or {})
    visible = [f for f in fm.values() if not f.hidden]
    cols = [k for k in (c.get("columns") or []) if k in fm and not fm[k].hidden]
    bad = [k for k in (c.get("columns") or []) if k not in fm]
    if bad:
        raise BadRequest(f"Unknown column: {bad[0]}.")
    if not cols:
        cols = [f.key for f in visible if f.default] or [f.key for f in visible[:6]]
    if source.dynamic_fields:
        for f in source.dynamic_fields(params or {}):
            if f.key not in cols:
                cols.append(f.key)
    c["columns"] = cols
    filters = []
    for flt in c.get("filters") or []:
        k, op = flt.get("field"), flt.get("op") or "eq"
        if k not in fm:
            raise BadRequest(f"Unknown filter field: {k}.")
        if op not in OPS:
            raise BadRequest(f"Unknown filter: {op}.")
        if op in DATE_OPS and fm[k].type not in ("date", "datetime"):
            raise BadRequest(f"“{OPS[op]}” works only on dates.")
        filters.append({"field": k, "op": op, "value": flt.get("value")})
    c["filters"] = filters
    groups = []
    for g in (c.get("group_by") or [])[:2]:
        k, grain = split_group(g)
        if k not in fm:
            raise BadRequest(f"Unknown group: {k}.")
        if grain and (grain not in GRAINS or fm[k].type not in ("date", "datetime")):
            raise BadRequest("Group dates by day, week, month, quarter or year.")
        groups.append(g)
    c["group_by"] = groups
    measures = []
    for m in c.get("measures") or []:
        agg = m.get("agg") or "count"
        if agg not in AGGS:
            raise BadRequest(f"Unknown total: {agg}.")
        k = m.get("field")
        if agg != "count":
            if k not in fm:
                raise BadRequest(f"Unknown field to total: {k}.")
            if agg in ("sum", "avg") and not fm[k].numeric:
                raise BadRequest(f"{fm[k].label} is not a number, so it can't be added up.")
        measures.append({"agg": agg, "field": k if agg != "count" else None, "label": m.get("label")})
    if groups and not measures:
        measures = [{"agg": "count", "field": None, "label": None}]
    c["summary"] = bool(measures)
    c["measures"] = measures
    sort = []
    for s in c.get("sort") or []:
        if s.get("field"):
            sort.append({"field": s["field"], "dir": "desc" if s.get("dir") == "desc" else "asc"})
    c["sort"] = sort
    c["totals"] = bool(c.get("totals", True))
    lim = c.get("limit")
    c["limit"] = int(lim) if lim not in (None, "", 0) else None
    return c


def resolve_params(source: Source, given: dict | None, today: date) -> dict:
    out = {}
    given = given or {}
    for p in source.params:
        v = given.get(p.key)
        if v in (None, ""):
            v = p.default
        v = resolve(v, today)
        if v in (None, "") and p.required:
            raise BadRequest(f"Choose {p.label.lower()}.")
        if p.type == "date" and v not in (None, ""):
            v = _to_date(v, today)
        elif p.type == "year" and v not in (None, ""):
            v = int(v)
        out[p.key] = v
    if "from" in out and "to" in out and out["from"] and out["to"]:
        if out["from"] > out["to"]:
            raise BadRequest("The start date is after the end date.")
        if (out["to"] - out["from"]).days > 731:
            raise BadRequest("Choose a period of at most 24 months.")
    return out


# ── Filtering ──────────────────────────────────────────────────────

def _sql_cond(f: Field, op: str, value, today: date, tz: str):
    e = f.expr
    if f.type == "datetime" and (op in DATE_OPS or op in ("eq", "ne", "gt", "gte", "lt", "lte", "between")):
        e = func.date(func.timezone(tz, e))
    if op in DATE_OPS:
        a, b = date_window(op, value, today)
        return and_(e >= a, e <= b)
    if op == "empty":
        return or_(e.is_(None), e == "") if f.type == "text" else e.is_(None)
    if op == "not_empty":
        return and_(e.isnot(None), e != "") if f.type == "text" else e.isnot(None)
    if op == "is_true":
        return e.is_(True)
    if op == "is_false":
        return or_(e.is_(False), e.is_(None))
    if op in ("in", "not_in"):
        vals = [_coerce(f, v, today) for v in (value if isinstance(value, list) else [value])]
        cond = e.in_(vals) if vals else False
        return not_(cond) if op == "not_in" else cond
    if op == "between":
        lo, hi = (value or [None, None])[:2] if isinstance(value, list) else (None, None)
        lo, hi = _coerce(f, lo, today), _coerce(f, hi, today)
        parts = ([e >= lo] if lo not in (None, "") else []) + ([e <= hi] if hi not in (None, "") else [])
        return and_(*parts) if parts else True
    v = _coerce(f, value, today)
    if op == "contains":
        return e.ilike(f"%{value}%")
    if op == "starts":
        return e.ilike(f"{value}%")
    return {"eq": lambda: e == v, "ne": lambda: or_(e != v, e.is_(None)), "gt": lambda: e > v,
            "gte": lambda: e >= v, "lt": lambda: e < v, "lte": lambda: e <= v}[op]()


def _py_value(f: Field, v, tz: ZoneInfo):
    if f.type == "datetime" and isinstance(v, datetime):
        return v.astimezone(tz).date() if v.tzinfo else v.date()
    if f.numeric and v is not None and not isinstance(v, (int, float, Decimal)):
        try:
            return Decimal(str(v))
        except Exception:
            return None
    return v


def _py_match(f: Field, op: str, value, raw, today: date, tz: ZoneInfo) -> bool:
    v = _py_value(f, raw, tz)
    if op in DATE_OPS:
        if v is None:
            return False
        a, b = date_window(op, value, today)
        return a <= v <= b
    if op == "empty":
        return v in (None, "")
    if op == "not_empty":
        return v not in (None, "")
    if op == "is_true":
        return bool(v)
    if op == "is_false":
        return not v
    if op in ("in", "not_in"):
        vals = {_coerce(f, x, today) for x in (value if isinstance(value, list) else [value])}
        if f.numeric:
            vals = {Decimal(str(x)) for x in vals if x is not None}
            v = Decimal(str(v)) if v is not None else None
        hit = v in vals
        return not hit if op == "not_in" else hit
    if op == "contains":
        return v is not None and str(value).lower() in str(v).lower()
    if op == "starts":
        return v is not None and str(v).lower().startswith(str(value).lower())
    if op == "between":
        lo, hi = (value or [None, None])[:2] if isinstance(value, list) else (None, None)
        lo, hi = _coerce(f, lo, today), _coerce(f, hi, today)
        if v is None:
            return False
        if f.numeric:
            v = Decimal(str(v))
        return (lo in (None, "") or v >= lo) and (hi in (None, "") or v <= hi)
    x = _coerce(f, value, today)
    if f.numeric and v is not None:
        v, x = Decimal(str(v)), Decimal(str(x)) if x is not None else None
    if op == "eq":
        return v == x
    if op == "ne":
        return v != x
    if v is None or x is None:
        return False
    return {"gt": v > x, "gte": v >= x, "lt": v < x, "lte": v <= x}[op]


# ── Running ────────────────────────────────────────────────────────

def _company_tz(db: Session, company_id: int) -> str:
    from modules.company.models import Company
    tz = db.query(Company.timezone).filter(Company.id == company_id).scalar()
    return tz or "Asia/Kolkata"


def effective_scope(db: Session, p: Principal, source: Source, limit: str | None = None) -> str:
    top = max_scope(db, p, source)
    if top is None:
        raise Forbidden(f"You don't have access to {source.label.lower()}.")
    return narrow(top, limit)


def _enrich(db: Session, company_id: int, rows: list[dict], wanted: set[str]) -> None:
    """Fill employee columns missing from a function source's rows."""
    need = wanted & {"employee_code", "employee_name", "department", "designation", "location", "legal_entity"}
    if not need or not rows or "employee_id" not in rows[0] and "employee_uuid" not in rows[0]:
        return
    from modules.reports.sources._common import EmpJoin
    j = EmpJoin("en")
    key = "employee_id" if "employee_id" in rows[0] else "employee_uuid"
    col = j.e.id if key == "employee_id" else j.e.uuid
    vals = {r.get(key) for r in rows if r.get(key) is not None}
    if key == "employee_uuid":
        import uuid as _u
        vals = {_u.UUID(str(v)) for v in vals}
    q = (select(col.label("k"), j.e.employee_code, func.concat_ws(" ", j.e.first_name, j.e.last_name).label("n"),
                j.d.department_name, j.ds.designation_name, j.l.name.label("loc"), j.le.name.label("le"))
         .select_from(_emp_from(j))
         .where(j.e.company_id == company_id, col.in_(vals)))
    info = {str(r[0]): r for r in db.execute(q).all()}
    for r in rows:
        i = info.get(str(r.get(key)))
        if not i:
            continue
        for k, v in (("employee_code", i[1]), ("employee_name", i[2]), ("department", i[3]), ("designation", i[4]),
                     ("location", i[5]), ("legal_entity", i[6])):
            if k in need and r.get(k) in (None, ""):
                r[k] = v


def _emp_from(j):
    from sqlalchemy.orm import join as ojoin
    f = ojoin(j.e, j.d, j.d.id == j.e.department_id, isouter=True)
    f = f.outerjoin(j.ds, j.ds.id == j.e.designation_id).outerjoin(j.l, j.l.id == j.e.location_id)
    return f.outerjoin(j.le, j.le.id == j.e.legal_entity_id)


def fetch_rows(db: Session, p: Principal, source: Source, config: dict, params: dict, ids: set[int] | None,
               today: date, tz: str) -> tuple[list[dict], bool]:
    fm = fields_for(source, params)
    wanted = set(config["columns"]) | {split_group(g)[0] for g in config["group_by"]} \
        | {m["field"] for m in config["measures"] if m.get("field")} | {f["field"] for f in config["filters"]} \
        | {s["field"] for s in config["sort"] if s["field"] in fm}
    for k in list(wanted):
        if k in fm:
            wanted |= set(fm[k].needs)
    wanted.add("employee_id")
    truncated = False
    sql_filters, py_filters = [], []
    for flt in config["filters"]:
        f = fm[flt["field"]]
        (sql_filters if (source.select_from and f.expr is not None) else py_filters).append(flt)
    if source.select_from:
        cols = [fm[k].expr.label(k) for k in wanted if k in fm and fm[k].expr is not None]
        if source.employee_col is not None and "employee_id" not in fm:
            cols.append(source.employee_col.label("employee_id"))
        q = select(*cols).select_from(source.select_from()).where(source.company_col == p.company_id)
        if source.base_where:
            q = q.where(*source.base_where())
        if ids is not None:
            q = q.where(source.employee_col.in_(ids or {-1}))
        for flt in sql_filters:
            q = q.where(_sql_cond(fm[flt["field"]], flt["op"], flt["value"], today, tz))
        q = q.limit(MAX_ROWS + 1)
        rows = [dict(r._mapping) for r in db.execute(q).all()]
        if len(rows) > MAX_ROWS:
            rows, truncated = rows[:MAX_ROWS], True
    else:
        rows = source.fetch(db, p.company_id, ids, params, {"today": today, "tz": tz}) or []
        _enrich(db, p.company_id, rows, wanted)
    ctx = {"today": today, "tz": tz}
    computed = [fm[k] for k in wanted if k in fm and fm[k].compute]
    for r in rows:
        for f in computed:
            try:
                r[f.key] = f.compute(r, ctx)
            except Exception:
                r[f.key] = None
    zone = ZoneInfo(tz)
    for flt in py_filters:
        f = fm[flt["field"]]
        rows = [r for r in rows if _py_match(f, flt["op"], flt["value"], r.get(f.key), today, zone)]
    return rows, truncated


def mask_rows(p: Principal, fields: list[Field], rows: list[dict]) -> list[str]:
    """Hide sensitive values of other people the viewer may not see. Returns masked keys."""
    masked = set()
    for f in fields:
        if not f.sensitive or p.can(f.sensitive):
            continue
        for r in rows:
            if p.employee_id and r.get("employee_id") == p.employee_id:
                continue
            if r.get(f.key) is not None:
                r[f.key] = MASK
                masked.add(f.key)
    return sorted(masked)


def _grain(v, grain: str | None, zone: ZoneInfo):
    if grain is None or v is None:
        return v
    if isinstance(v, datetime):
        v = v.astimezone(zone).date() if v.tzinfo else v.date()
    if grain == "day":
        return v
    if grain == "week":
        return v - timedelta(days=v.weekday())
    if grain == "month":
        return v.replace(day=1)
    if grain == "quarter":
        return date(v.year, 3 * ((v.month - 1) // 3) + 1, 1)
    return date(v.year, 1, 1)


def _agg(agg: str, field: str | None, rows: list[dict]):
    if agg == "count":
        return len(rows)
    vals = [r.get(field) for r in rows if r.get(field) is not None]
    if agg == "count_distinct":
        return len({str(v) for v in vals})
    if not vals:
        return None
    if agg in ("sum", "avg"):
        nums = [Decimal(str(v)) for v in vals]
        s = sum(nums, Decimal(0))
        return s if agg == "sum" else (s / len(nums)).quantize(Decimal("0.01"))
    return min(vals) if agg == "min" else max(vals)


def measure_key(m: dict) -> str:
    return "count" if m["agg"] == "count" else f"{m['agg']}__{m['field']}"


def _sort_key(v):
    if v is None:
        return (1, "")
    if isinstance(v, (int, float, Decimal)):
        return (0, float(v))
    if isinstance(v, (date, datetime)):
        return (0, v.isoformat())
    return (0, str(v).lower())


def run(db: Session, p: Principal, source: Source, config: dict, *, scope_limit: str | None = None) -> dict:
    from core.timeutil import today_for_company_id
    today = today_for_company_id(db, p.company_id)
    tz = _company_tz(db, p.company_id)
    zone = ZoneInfo(tz)
    params = resolve_params(source, (config or {}).get("params"), today)
    config = clean_config(source, config, params)
    scope = effective_scope(db, p, source, scope_limit)
    ids = scope_ids(db, p, scope)
    rows, truncated = fetch_rows(db, p, source, config, params, ids, today, tz)
    fm = fields_for(source, params)
    masked = mask_rows(p, list(fm.values()), rows)

    if config["group_by"] or config["measures"]:
        groups = [split_group(g) for g in config["group_by"]]
        buckets: dict[tuple, list[dict]] = defaultdict(list)
        if not groups:
            buckets[()] = []
        for r in rows:
            buckets[tuple(_grain(r.get(k), g, zone) for k, g in groups)].append(r)
        columns = []
        for (k, g), raw in zip(groups, config["group_by"]):
            f = fm[k]
            columns.append({"key": raw, "label": f.label + (f" ({g})" if g else ""),
                            "type": "date" if g else f.type, "grain": g,
                            "options": f.out()["options"]})
        for m in config["measures"]:
            f = fm.get(m["field"]) if m.get("field") else None
            label = m.get("label") or ("Count" if m["agg"] == "count" else f"{AGGS[m['agg']]} {f.label.lower()}")
            typ = "number" if m["agg"] in ("count", "count_distinct") else (f.type if f else "number")
            columns.append({"key": measure_key(m), "label": label, "type": typ, "measure": True})
        out_rows = []
        for key, items in buckets.items():
            row = {raw: key[i] for i, raw in enumerate(config["group_by"])}
            for m in config["measures"]:
                row[measure_key(m)] = _agg(m["agg"], m.get("field"), items)
            out_rows.append(row)
        totals = {measure_key(m): _agg(m["agg"], m.get("field"), rows) for m in config["measures"]} \
            if config["totals"] else None
        default_sort = [{"field": config["group_by"][0], "dir": "asc"}] if config["group_by"] else []
    else:
        columns = []
        for k in config["columns"]:
            f = fm[k]
            columns.append({"key": k, "label": f.label, "type": f.type, "sensitive": bool(f.sensitive),
                            "options": f.out()["options"]})
        out_rows = [{**{c["key"]: r.get(c["key"]) for c in columns}, "_employee_id": r.get("employee_id")}
                    for r in rows]
        totals = None
        if config["totals"]:
            totals = {}
            for c in columns:
                f = fm[c["key"]]
                if f.numeric:
                    totals[c["key"]] = _agg(f.agg or "sum", c["key"], rows)
        default_sort = []
    for s in reversed(config["sort"] or default_sort):
        out_rows.sort(key=lambda r: _sort_key(r.get(s["field"])), reverse=s["dir"] == "desc")
    if config["limit"]:
        out_rows = out_rows[: config["limit"]]
    return {
        "source": source.key, "source_label": source.label, "columns": columns, "rows": out_rows,
        "row_count": len(out_rows), "base_rows": len(rows), "totals": totals,
        "grouped": bool(config["group_by"] or config["measures"]),
        "scope": scope, "scope_label": SCOPE_LABELS[scope], "masked": masked, "truncated": truncated,
        "params": {k: (v.isoformat() if isinstance(v, date) else v) for k, v in params.items()},
        "config": config, "today": today.isoformat(), "timezone": tz,
    }


def jsonable(v: Any):
    if isinstance(v, datetime):
        return v.isoformat().replace("+00:00", "Z")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


def to_json(result: dict, page: int | None = None, size: int | None = None) -> dict:
    rows = result["rows"]
    if page and size:
        rows = rows[(page - 1) * size: page * size]
    out = {k: v for k, v in result.items() if k != "rows"}
    out["rows"] = [{k: jsonable(v) for k, v in r.items()} for r in rows]
    if out.get("totals"):
        out["totals"] = {k: jsonable(v) for k, v in out["totals"].items()}
    out["page"], out["size"] = page, size
    return out
