"""Field mapping for punches that arrive as JSON, XML, delimited text or database rows.

A mapping says where the records are and which field holds what:

    {"format": "json" | "xml" | "text",
     "records_path": "data.logs",          # dot path (JSON) or element name / path (XML)
     "text_field": "strDataList",          # text inside this field holds delimited lines
     "text_delimiter": "\\t", "text_header": false,
     "employee": "UserId", "match_by": "device_user_id" | "employee_code",
     "timestamp": "LogDate",               # or "date" + "time"
     "timestamp_format": "%d/%m/%Y %H:%M:%S" | "iso" | "epoch" | "epoch_ms" | "",
     "timezone": "Asia/Kolkata",           # for times without a zone (else the device's)
     "direction": "Direction", "in_values": "in,0", "out_values": "out,1",
     "device": "MID",                      # terminal / reader id, matched to our devices
     "id": "DeviceLogId"}                  # increasing id, for incremental reads

`extract()` turns a response into flat rows (dicts), `to_records()` turns rows into the
records `devices.ingest()` saves. Nested fields use dots: "user_id.user_id".
"""
from __future__ import annotations

import csv
import io
import json
import xml.etree.ElementTree as ET
from datetime import date, datetime, time, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from core.errors import BadRequest
from core.timeutil import is_valid_timezone

FIELDS = ("employee", "timestamp", "date", "time", "direction", "device", "id")
FORMATS = ("json", "xml", "text")
MAX_ROWS = 20000


# ── Paths ───────────────────────────────────────────────────────────

def get_path(obj, path: str | None):
    """Value at a dot path ("a.b.0.c"); a plain key with dots is tried first."""
    if path in (None, ""):
        return obj
    if isinstance(obj, dict) and path in obj:
        return obj[path]
    cur = obj
    for part in str(path).split("."):
        if isinstance(cur, dict):
            if part in cur:
                cur = cur[part]
                continue
            low = {str(k).lower(): v for k, v in cur.items()}
            if part.lower() in low:
                cur = low[part.lower()]
                continue
            return None
        if isinstance(cur, list) and part.lstrip("-").isdigit():
            i = int(part)
            if -len(cur) <= i < len(cur):
                cur = cur[i]
                continue
        return None
    return cur


def _first_list(obj, depth=0):
    """The first list of objects inside a response, for a mapping without records_path."""
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict) and depth < 4:
        for v in obj.values():
            if isinstance(v, list) and (not v or isinstance(v[0], (dict, list))):
                return v
        for v in obj.values():
            found = _first_list(v, depth + 1)
            if found is not None:
                return found
    return None


def flatten(obj, prefix: str = "", out: dict | None = None, depth: int = 0) -> dict:
    """{"a": {"b": 1}} -> {"a.b": 1}; lists of scalars are kept as they are."""
    out = {} if out is None else out
    if isinstance(obj, dict) and depth < 5:
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                flatten(v, key, out, depth + 1)
            else:
                out[key] = v
    else:
        out[prefix or "value"] = obj
    return out


# ── XML ─────────────────────────────────────────────────────────────

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else str(tag)


def _xml_root(text: str):
    if "<!DOCTYPE" in text[:2000].upper() or "<!ENTITY" in text[:5000].upper():
        raise BadRequest("The XML response has a document type declaration, which isn't read for safety.")
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise BadRequest(f"The response isn't valid XML: {exc}")


def _element_dict(el) -> dict:
    out = {_local(k): v for k, v in el.attrib.items()}
    for child in el:
        name = _local(child.tag)
        if len(child):
            for k, v in _element_dict(child).items():
                out[f"{name}.{k}"] = v
        else:
            out[name] = (child.text or "").strip()
    if not len(el) and (el.text or "").strip():
        out.setdefault("value", el.text.strip())
    return out


def xml_records(text: str, records_path: str | None) -> list[dict]:
    root = _xml_root(text)
    if records_path:
        parts = [p for p in records_path.replace(".", "/").split("/") if p]
        last = parts[-1]
        # every element with the last name whose parents end with the rest of the path
        found = []
        for el in root.iter():
            if _local(el.tag) == last:
                found.append(el)
        return [_element_dict(el) for el in found[:MAX_ROWS]]
    # no path: the repeated child elements at the deepest level that repeats
    best = []
    for el in root.iter():
        kids = list(el)
        if len(kids) >= 1 and len({_local(k.tag) for k in kids}) == 1 and len(kids[0]):
            if len(kids) > len(best):
                best = kids
    return [_element_dict(el) for el in best[:MAX_ROWS]]


def xml_text(text: str, field: str) -> str:
    root = _xml_root(text)
    for el in root.iter():
        if _local(el.tag) == field:
            return el.text or ""
    return ""


# ── Delimited text ──────────────────────────────────────────────────

def _delim(d: str | None) -> str:
    d = d or ","
    return {"\\t": "\t", "tab": "\t", "TAB": "\t", "pipe": "|", "space": " "}.get(d, d)


def text_records(text: str, delimiter: str | None, header: bool) -> list[dict]:
    d = _delim(delimiter)
    lines = [l for l in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n") if l.strip()]
    if d == " ":
        rows = [l.split() for l in lines]
    else:
        rows = list(csv.reader(lines, delimiter=d))
    if not rows:
        return []
    if header:
        names = [str(h).strip() or f"{i + 1}" for i, h in enumerate(rows[0])]
        body = rows[1:]
    else:
        names = [str(i + 1) for i in range(max(len(r) for r in rows))]
        body = rows
    return [{names[i]: (v.strip() if isinstance(v, str) else v) for i, v in enumerate(r) if i < len(names)}
            for r in body[:MAX_ROWS]]


# ── Extract ─────────────────────────────────────────────────────────

def _as_text(raw) -> str:
    if isinstance(raw, bytes):
        for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
    return str(raw or "")


def extract(raw, mapping: dict) -> list[dict]:
    """Rows (flat dicts) from a response body (bytes/str) or an already-parsed object."""
    fmt = (mapping or {}).get("format") or "json"
    path = (mapping or {}).get("records_path") or ""
    text_field = (mapping or {}).get("text_field") or ""
    if fmt == "text" and not text_field:
        return text_records(_as_text(raw), mapping.get("text_delimiter"), bool(mapping.get("text_header")))
    if fmt == "xml":
        text = _as_text(raw)
        if text_field:
            return text_records(xml_text(text, text_field), mapping.get("text_delimiter"), bool(mapping.get("text_header")))
        return xml_records(text, path)
    # json
    if isinstance(raw, (bytes, str)):
        text = _as_text(raw).strip()
        if not text:
            return []
        try:
            data = json.loads(text)
        except ValueError:
            raise BadRequest("The response isn't valid JSON. Choose XML or text if the service sends those.")
    else:
        data = raw
    if text_field:
        return text_records(str(get_path(data, text_field) or ""), mapping.get("text_delimiter"), bool(mapping.get("text_header")))
    if path == ".":  # the whole body is one record (one event per post)
        return [flatten(data)] if isinstance(data, dict) else []
    items = get_path(data, path) if path else _first_list(data)
    if items is None and path:
        return []
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        items = [data] if isinstance(data, dict) and not path else []
    return [flatten(x) if isinstance(x, dict) else {"value": x} for x in items[:MAX_ROWS]]


# ── Rows to punches ─────────────────────────────────────────────────

def _values(s) -> set[str]:
    if isinstance(s, (list, tuple)):
        return {str(x).strip().lower() for x in s if str(x).strip()}
    return {x.strip().lower() for x in str(s or "").split(",") if x.strip()}


def direction_of(value, in_values=None, out_values=None) -> str | None:
    from modules.attendance.devices import _direction
    v = str(value if value is not None else "").strip().lower()
    if v.endswith(".0"):
        v = v[:-2]
    ins, outs = _values(in_values), _values(out_values)
    if ins or outs:
        if v in ins:
            return "in"
        if v in outs:
            return "out"
        return None
    return _direction(v)


def _scalar(v):
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v


def parse_time(value, fmt: str | None, tz: ZoneInfo) -> datetime:
    """A punch time in UTC from a value in a mapped field."""
    from modules.attendance.devices import parse_timestamp
    fmt = (fmt or "").strip()
    value = _scalar(value)
    if fmt in ("epoch", "epoch_ms") or (not fmt and isinstance(value, (int, float)) and value > 10**8):
        n = float(value)
        if fmt == "epoch_ms" or (not fmt and n > 10**11):
            n /= 1000
        return datetime.fromtimestamp(n, tz=timezone.utc).replace(microsecond=0)
    if isinstance(value, str) and fmt in ("", "iso"):
        s = value.strip()
        if s.startswith("/Date(") and s.endswith(")/"):  # .NET JSON dates
            ms = int(s[6:-2].split("+")[0].split("-")[0] or 0)
            return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(microsecond=0)
    return parse_timestamp(value, tz, None if fmt in ("", "iso") else fmt)


def _combine(dv, tv):
    if isinstance(dv, datetime):
        dv = dv.date()
    if isinstance(dv, date) and isinstance(tv, (time, datetime)):
        return datetime.combine(dv, tv if isinstance(tv, time) else tv.time())
    if isinstance(tv, datetime):
        tv = tv.time()
    d = dv.isoformat() if isinstance(dv, date) else str(dv or "").strip()
    t = tv.strftime("%H:%M:%S") if isinstance(tv, time) else str(tv or "").strip()
    return f"{d} {t}".strip()


def to_records(rows: list[dict], mapping: dict, tz: ZoneInfo) -> list[dict]:
    """Records for devices.ingest(): device_user_id, timestamp (UTC), direction, plus the
    terminal (`_terminal`), the source id (`_id`) and the local time (`_local`)."""
    m = mapping or {}
    if m.get("timezone") and is_valid_timezone(m["timezone"]):
        tz = ZoneInfo(m["timezone"])
    out = []
    for n, r in enumerate(rows, start=1):
        def at(key):
            name = m.get(key)
            return _scalar(get_path(r, name)) if name else None
        uid = at("employee")
        if m.get("timestamp"):
            raw_ts = get_path(r, m["timestamp"])
        else:
            raw_ts = _combine(get_path(r, m.get("date")), get_path(r, m.get("time")))
        try:
            ts = parse_time(raw_ts, m.get("timestamp_format"), tz)
        except (ValueError, TypeError, OverflowError):
            ts = raw_ts if raw_ts not in (None, "") else ""
        rec = {"device_user_id": "" if uid is None else str(uid).strip(), "timestamp": ts,
               "direction": direction_of(at("direction"), m.get("in_values"), m.get("out_values")) if m.get("direction") else None,
               "_row": n}
        if m.get("device"):
            term = at("device")
            rec["_terminal"] = None if term in (None, "") else str(term).strip()
        if m.get("id"):
            rec["_id"] = at("id")
        if isinstance(raw_ts, datetime):
            rec["_local"] = raw_ts
        out.append(rec)
    return out


def guess(columns: list[str]) -> dict:
    low = [str(c or "").strip().lower() for c in columns]

    def find(*words, exact=False):
        for w in words:
            for i, h in enumerate(low):
                if (h == w) if exact else (w in h):
                    return columns[i]
        return None
    ts = (find("punch_time", "punchtime", "logdate", "log_date", "checktime", "check_time", "edatetime", "eventdatetime",
               "event_time", "datetime", "date_time", "timestamp", "punch time", "time", exact=True)
          or find("punch_time", "punchtime", "logdate", "datetime", "checktime", "timestamp", "event_time"))
    return {
        "employee": (find("emp_code", "empcode", "employeecode", "employee_code", "userid", "user_id", "pin", "badgenumber",
                          "enrollnumber", "enrollno", exact=True)
                     or find("emp_code", "empcode", "employee code", "userid", "user_id", "user id", "pin", "badge", "enroll", "card")),
        "timestamp": ts,
        "direction": find("direction", "punch_state", "inout", "in_out", "checktype", "entryexittype", "iotype", "status", exact=True)
                     or find("direction", "inout", "in/out", "punch_state", "checktype", "entryexit"),
        "device": find("terminal_sn", "deviceid", "device_id", "mid", "sn", "sensorid", "serialnumber", exact=True)
                  or find("terminal", "device", "serial"),
        "id": find("devicelogid", "id", "indexno", "seqno", "logid", exact=True),
    }


def clean(m: dict | None, *, need_format: bool = False) -> dict:
    m = m or {}
    out = {k: (str(m.get(k)).strip() if m.get(k) not in (None, "") else None) for k in
           FIELDS + ("records_path", "text_field", "text_delimiter", "timestamp_format", "timezone", "in_values", "out_values")}
    out["format"] = m.get("format") if m.get("format") in FORMATS else "json"
    out["text_header"] = bool(m.get("text_header"))
    out["match_by"] = m.get("match_by") if m.get("match_by") in ("device_user_id", "employee_code") else "device_user_id"
    if out["timezone"] and not is_valid_timezone(out["timezone"]):
        raise BadRequest("The timezone in the field mapping isn't recognised.")
    if not out["employee"]:
        raise BadRequest("Choose the field with the employee code or device user id.")
    if not out["timestamp"] and not (out["date"] and out["time"]):
        raise BadRequest("Choose the punch time field, or separate date and time fields.")
    return out


def jsonable(v):
    v = _scalar(v)
    if isinstance(v, (datetime, date, time)):
        return v.isoformat(sep=" ") if isinstance(v, datetime) else v.isoformat()
    if isinstance(v, (dict, list)):
        return json.dumps(v)[:200]
    if v is None or isinstance(v, (int, float, bool)):
        return v
    return str(v)[:200]


def sample(rows: list[dict], records: list[dict], tz: ZoneInfo, limit: int = 10) -> dict:
    """What a test shows: the fields found, the first rows, and how they read as punches."""
    cols = []
    for r in rows[:50]:
        for k in r:
            if k not in cols:
                cols.append(k)
    punches = []
    for rec in records[:limit]:
        ts = rec.get("timestamp")
        punches.append({"device_user_id": rec.get("device_user_id"),
                        "time": ts.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S") if isinstance(ts, datetime) else None,
                        "raw_time": None if isinstance(ts, datetime) else jsonable(ts),
                        "direction": rec.get("direction"), "terminal": rec.get("_terminal")})
    return {"columns": cols[:60], "rows": [{k: jsonable(r.get(k)) for k in cols[:60]} for r in rows[:limit]],
            "total": len(rows), "punches": punches, "guess": guess(cols)}
