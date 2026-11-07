"""The newer device connections in one place: their settings, scheduled pulls, tests,
vendor pushes in any format, and the office connector's configuration and heartbeat.

    api       we call the vendor's / middleware's web API          (device_api)
    database  we read the attendance software's database            (device_db)
    zk        we read a ZKTeco-protocol device over the LAN (4370)   (device_zk)
    email     we read CSV / Excel attachments from a mailbox         (device_email)
    webhook   the device or software posts its own JSON to us        (mapping only)
    adms      ZKTeco / eSSL "cloud push" (iclock)                     (device_adms)

`database` and `zk` can run on our server or on the office connector (runs_on); the
connector downloads its settings with the device key and pushes punches to the normal
push address. Everything ends in devices.ingest().
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core.errors import BadRequest
from modules.attendance import device_api, device_db, device_email, device_mapping as fm, device_zk
from modules.attendance.device_common import masked, with_secrets
from modules.attendance.models import Device, DeviceSyncLog

log = logging.getLogger("hrms.devices")

PULLED = ("api", "database", "zk", "email")
CONNECTOR_CAPABLE = ("database", "zk")
SECRETS = {"api": device_api.SECRET_KEYS, "database": device_db.SECRET_KEYS, "zk": device_zk.SECRET_KEYS,
           "email": device_email.SECRET_KEYS}
ONLINE_MINUTES = 5          # ADMS devices call every few seconds; the connector every minute
CONNECTOR_ONLINE_MINUTES = 5
CONNECTOR_SETTINGS_EXCLUDE = ("preset",)


# ── Settings ────────────────────────────────────────────────────────

def _mapping(m: dict | None, conn: str) -> dict:
    out = fm.clean(m)
    if conn == "database":
        out["format"] = None
    return out


def clean(conn: str, cfg: dict | None, old: dict | None) -> dict | None:
    cfg = dict(cfg or {})
    if conn == "api":
        out = device_api.clean(cfg, old)
        out["mapping"] = _mapping(cfg.get("mapping"), conn)
    elif conn == "database":
        out = device_db.clean(cfg, old)
        out["mapping"] = _mapping(cfg.get("mapping"), conn)
        if out["mode"] == "table" and out["incremental"] == "id" and not out["mapping"].get("id"):
            raise BadRequest("Choose the id column, or read new rows by punch time.")
    elif conn == "zk":
        out = device_zk.clean(cfg, old)
    elif conn == "email":
        out = device_email.clean(cfg, old)
    elif conn == "webhook":
        out = {"preset": (cfg.get("preset") or "custom")[:40], "mapping": _mapping(cfg.get("mapping"), conn)}
    elif conn == "adms":
        ips = ",".join(x.strip() for x in str(cfg.get("allowed_ips") or "").split(",") if x.strip())
        out = {"use_status": bool(cfg.get("use_status")), "allowed_ips": ips[:500]}
    else:
        return None
    return out


def config_out(d: Device) -> dict | None:
    if not d.source_config:
        return None
    return masked(d.source_config, SECRETS.get(d.connection, ()))


def tz_of(db: Session, d: Device | None, company_id: int) -> ZoneInfo:
    from modules.attendance import service
    if d is not None and d.timezone:
        try:
            return ZoneInfo(d.timezone)
        except Exception:
            pass
    return service.company_tz(db, company_id)


def status_out(d: Device) -> dict:
    """Online state for the devices list: ADMS devices by their calls, connector devices by
    the connector's heartbeat."""
    now = datetime.now(timezone.utc)
    out = {"online": None, "connector": None}
    if d.connection in ("adms",):
        out["online"] = bool(d.last_seen_at and now - d.last_seen_at <= timedelta(minutes=ONLINE_MINUTES))
    if d.runs_on == "connector":
        info = dict(d.connector_info or {})
        out["connector"] = {
            "online": bool(d.connector_seen_at and now - d.connector_seen_at <= timedelta(minutes=CONNECTOR_ONLINE_MINUTES)),
            "seen_at": d.connector_seen_at.isoformat().replace("+00:00", "Z") if d.connector_seen_at else None,
            "version": info.get("version"), "host": info.get("host"), "queue": info.get("queue"),
            "last_error": info.get("last_error"), "last_read_at": info.get("last_read_at"),
        }
        out["online"] = out["connector"]["online"]
    return out


def state_out(d: Device) -> dict:
    s = dict(d.sync_state or {})
    adms = dict(s.get("adms") or {})
    return {"last_time": s.get("last_time"), "last_id": s.get("last_id"), "last_error": s.get("last_error"),
            "last_ok_at": s.get("last_ok_at"), "device_users": len(s.get("device_users") or []),
            "adms": {k: adms.get(k) for k in ("ip", "pushver", "info", "handshake_at", "att_stamp")} if adms else None,
            "commands": [{k: c.get(k) for k in ("id", "cmd", "status", "queued_at")} for c in (adms.get("commands") or [])][-5:],
            "has_sample": bool(s.get("last_payload"))}


# ── Ingest with terminals ──────────────────────────────────────────

def _terminals(db: Session, company_id: int) -> dict:
    rows = db.query(Device.id, Device.serial_number, Device.name).filter(Device.company_id == company_id).all()
    out = {}
    for i, sn, name in rows:
        if name:
            out.setdefault(name.strip().lower(), i)
        if sn:
            out[sn.strip().lower()] = i
    return out


def ingest_records(db: Session, device: Device, records: list[dict], kind: str, file_name: str | None = None,
                   match_by: str = "device_user_id") -> list[DeviceSyncLog]:
    from modules.attendance import devices
    terms = None
    keep = []
    for r in records:
        if not str(r.get("device_user_id") or "").strip():
            continue  # door events and heartbeats without a person
        t = r.get("_terminal")
        if t:
            terms = terms if terms is not None else _terminals(db, device.company_id)
            if terms.get(str(t).lower()):
                r["_device_id"] = terms[str(t).lower()]
        keep.append(r)
    if not keep:
        return [devices.ingest(db, device.company_id, device, [], kind, file_name, match_by)]
    return [devices.ingest(db, device.company_id, device, keep[i:i + devices.MAX_BATCH], kind, file_name, match_by)
            for i in range(0, len(keep), devices.MAX_BATCH)]


def _failed(db: Session, d: Device, kind: str, message: str) -> DeviceSyncLog:
    entry = DeviceSyncLog(company_id=d.company_id, device_id=d.id, kind=kind, status="failed",
                          errors=[{"row": None, "message": message[:500]}], message=message[:500],
                          received=0, inserted=0, duplicates=0, unmatched=0)
    db.add(entry)
    return entry


# ── Scheduled pulls ────────────────────────────────────────────────

def _set_state(d: Device, state: dict) -> None:
    d.sync_state = state
    flag_modified(d, "sync_state")


def pull(db: Session, d: Device, *, transport=None, engine=None, zk_factory=None, mail=None,
         now: datetime | None = None) -> list[DeviceSyncLog]:
    """Read new punches for one device now. `transport`, `engine`, `zk_factory` and `mail`
    replace the network in tests."""
    now = now or datetime.now(timezone.utc)
    cfg = with_secrets(d.source_config)
    state = dict(d.sync_state or {})
    tz = tz_of(db, d, d.company_id)
    logs: list[DeviceSyncLog] = []
    more = False
    try:
        if d.connection == "api":
            _, records, new_state, notes = device_api.read(cfg, cfg.get("mapping") or {}, state, tz, d.serial_number,
                                                           transport, now)
            with db.begin_nested():
                logs = ingest_records(db, d, records, "api", match_by=(cfg.get("mapping") or {}).get("match_by") or "device_user_id")
        elif d.connection == "database":
            _, records, new_state, notes = device_db.read(cfg, cfg.get("mapping") or {}, state, tz, engine, now)
            with db.begin_nested():
                logs = ingest_records(db, d, records, "database", match_by=(cfg.get("mapping") or {}).get("match_by") or "device_user_id")
        elif d.connection == "zk":
            records, new_state, info = device_zk.read(cfg, state, tz, zk_factory, now)
            new_state["zk_info"] = info
            with db.begin_nested():
                logs = ingest_records(db, d, records, "zk")
        elif d.connection == "email":
            logs, new_state = _pull_email(db, d, cfg, state, mail)
        else:
            raise BadRequest("This device sends its punches to us; there is nothing to pull.")
        more = bool(new_state.pop("more", False))
        new_state.update({"last_ok_at": now.isoformat(), "last_error": None})
        _set_state(d, {**state, **new_state})
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc) or exc.__class__.__name__
        logs.append(_failed(db, d, d.connection, f"Couldn't read: {detail}"))
        _set_state(d, {**state, "last_error": str(detail)[:500], "last_error_at": now.isoformat()})
    d.last_sync_at = now
    every = int(cfg.get("every_minutes") or 15)
    d.next_pull_at = now + (timedelta(minutes=1) if more else timedelta(minutes=every))
    db.flush()
    return logs


def _pull_email(db: Session, d: Device, cfg: dict, state: dict, mail=None) -> tuple[list[DeviceSyncLog], dict]:
    from modules.attendance import devices
    seen = list(state.get("seen_messages") or [])
    mapping = d.column_mapping
    if not mapping:
        raise BadRequest("Set the file columns for this device first (import one attachment by hand to set them).")
    logs = []
    fetch = mail or device_email.messages
    for uid, name, content in fetch(cfg, set(seen)):
        if uid not in seen:
            seen.append(uid)
        if not name:
            continue
        try:
            with db.begin_nested():
                logs.append(devices.import_file(db, d.company_id, d, content, name, mapping, kind="email"))
        except Exception as exc:
            detail = getattr(exc, "detail", None) or str(exc)
            logs.append(_failed(db, d, "email", f"{name}: {detail}"))
    return logs, {"seen_messages": seen[-500:]}


def poll_due(db: Session) -> int:
    """Periodic job: pull every server-side api / database / zk / email device that is due."""
    now = datetime.now(timezone.utc)
    due = (db.query(Device).filter(Device.connection.in_(PULLED), Device.active.is_(True), Device.runs_on == "server")
           .filter((Device.next_pull_at.is_(None)) | (Device.next_pull_at <= now)).all())
    n = 0
    for d in due:
        db.info["company_id"] = d.company_id
        try:
            n += sum(l.inserted or 0 for l in pull(db, d, now=now))
        except Exception as exc:  # never stop the other devices
            log.warning("device pull failed for %s: %s", d.id, exc)
    db.info.pop("company_id", None)
    return n


# ── Tests from the connection modal ────────────────────────────────

def _lenient_mapping(m: dict | None, conn: str) -> dict | None:
    try:
        return _mapping(m, conn)
    except BadRequest:
        return None


def test(db: Session, company_id: int, conn: str, cfg_in: dict, device: Device | None = None, *,
         transport=None, engine=None, zk_factory=None, mail=None) -> dict:
    """Try the settings from the form (saved secrets fill empty fields) and show what
    comes back: fields, first rows, and how they read as punches."""
    old = device.source_config if device is not None and device.connection == conn else None
    cfg_in = dict(cfg_in or {})
    mapping_in = cfg_in.get("mapping")
    tz = tz_of(db, device, company_id)
    if cfg_in.get("timezone_hint"):
        try:
            tz = ZoneInfo(cfg_in["timezone_hint"])
        except Exception:
            pass
    serial = (cfg_in.get("serial_hint") or (device.serial_number if device else None) or "")
    if conn == "webhook":
        mapping = _lenient_mapping(mapping_in, conn) or {**(mapping_in or {}), "employee": None}
        raw = cfg_in.get("sample_body")
        if not raw and device is not None:
            raw = (device.sync_state or {}).get("last_payload")
        if not raw:
            raise BadRequest("Paste a sample of what the device sends, or save first and let it send one.")
        rows = fm.extract(raw if isinstance(raw, (str, bytes)) else json.dumps(raw), mapping)
        records = fm.to_records(rows, mapping, tz) if mapping.get("employee") else []
        return {"ok": True, "notes": [], "sample": fm.sample(rows, records, tz)}
    if conn == "api":
        cfg = with_secrets(device_api.clean({k: v for k, v in cfg_in.items() if k != "mapping"}, old))
        mapping = _lenient_mapping(mapping_in, conn) or dict(mapping_in or {})
        start, end, _ = device_api.window(cfg, {}, tz)
        ctx = device_api.context(cfg, {}, tz, serial, start, end)
        rows, notes = device_api.fetch(cfg, mapping, ctx, transport, max_pages=2)
        records = fm.to_records(rows, mapping, tz) if mapping.get("employee") and (mapping.get("timestamp") or mapping.get("date")) else []
        notes.insert(0, f"Asked for {start:%d %b %Y %H:%M} to {end:%d %b %Y %H:%M}")
        return {"ok": True, "notes": notes, "sample": fm.sample(rows, records, tz)}
    if conn == "database":
        cfg = with_secrets(device_db.clean({k: v for k, v in cfg_in.items() if k != "mapping"}, old))
        mapping = _lenient_mapping(mapping_in, conn)
        return _test_db(cfg, mapping, tz, engine)
    if conn == "zk":
        cfg = with_secrets(device_zk.clean(cfg_in, old))
        if engine is None and zk_factory is not None:
            cfg["_skip_host_check"] = True
        res = device_zk.test(cfg, tz, zk_factory)
        return {"ok": True, "notes": [f"{k}: {v}" for k, v in res["info"].items() if v not in (None, "")],
                "sample": {"columns": [], "rows": [], "total": res["total"], "punches": res["punches"], "guess": {}}}
    if conn == "email":
        cfg = with_secrets(device_email.clean(cfg_in, old))
        found, preview = [], None
        from modules.attendance import devices
        for uid, name, content in (mail or device_email.messages)(cfg, set(), 10):
            if name:
                found.append(name)
                if preview is None:
                    try:
                        preview = devices.preview(content, name)
                    except Exception:
                        preview = None
        notes = [f"{len(found)} matching attachment(s) in the last {cfg.get('lookback_days')} days"] + found[:5]
        return {"ok": True, "notes": notes, "preview": preview,
                "sample": {"columns": preview["headers"] if preview else [], "total": preview["total_rows"] if preview else 0,
                           "rows": [dict(zip(preview["headers"], r)) for r in preview["rows"]] if preview else [],
                           "punches": [], "guess": {}}}
    raise BadRequest("There's nothing to test for this connection: the device sends to us.")


def _test_db(cfg: dict, mapping: dict | None, tz: ZoneInfo, engine=None) -> dict:
    now = datetime.now(timezone.utc)
    own = engine is None
    engine = engine or device_db.make_engine(cfg)
    try:
        notes, tables = [], None
        if cfg["mode"] == "table":
            table = device_db.tables_for(cfg["table"], None, now)[-1]
            try:
                cols, rows, _ = device_db.fetch_rows({**cfg, "mode": "query", "query": f"SELECT * FROM {device_db._quote(engine.dialect, table)}"},
                                                     {}, None, 20, engine, now)
            except BadRequest as exc:
                tables = device_db.list_tables(cfg, engine)
                return {"ok": False, "notes": [exc.detail, f"Reading {table} failed. These tables and views were found."],
                        "tables": tables, "sample": {"columns": [], "rows": [], "total": 0, "punches": [], "guess": {}}}
            notes.append(f"Connected. First rows of {table}.")
        else:
            since = device_db.since_value(cfg, {}, tz, now)
            cols, rows, notes = device_db.fetch_rows(cfg, mapping or {}, since, 20, engine, now)
            notes.insert(0, f"Connected. Rows since {since}.")
        records = []
        if mapping:
            if cfg["mode"] == "table":
                since = device_db.since_value(cfg, {}, tz, now)
                _, prow, _ = device_db.fetch_rows(cfg, mapping, since, 20, engine, now)
                records = fm.to_records(prow, mapping, tz)
                notes.append(f"Reading from {since}: {len(prow)} new rows in the first batch.")
            else:
                records = fm.to_records(rows, mapping, tz)
        sample = fm.sample(rows, records, tz)
        sample["columns"] = cols or sample["columns"]
        sample["guess"] = fm.guess(sample["columns"])
        return {"ok": True, "notes": notes, "tables": tables, "sample": sample}
    finally:
        if own:
            engine.dispose()


# ── Vendor push in any format (webhook) ────────────────────────────

async def read_hook_body(request) -> object:
    """JSON from the body, or from the first JSON field of a form / multipart post
    (Hikvision sends the event as a JSON part next to the photo)."""
    ctype = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in ctype or "application/x-www-form-urlencoded" in ctype:
        form = await request.form()
        for _, v in form.multi_items():
            if isinstance(v, str) and v.strip()[:1] in ("{", "["):
                try:
                    return json.loads(v)
                except ValueError:
                    continue
            elif hasattr(v, "read") and (getattr(v, "content_type", "") or "").endswith("json"):
                try:
                    return json.loads(await v.read())
                except ValueError:
                    continue
        return {k: v for k, v in form.items() if isinstance(v, str)}
    body = await request.body()
    if not body.strip():
        return {}
    if "xml" in ctype or body.lstrip()[:1] == b"<":
        return body.decode("utf-8", "replace")
    try:
        return json.loads(body)
    except ValueError:
        raise BadRequest("Send the punches as JSON.")


def hook(db: Session, d: Device, payload) -> dict:
    cfg = d.source_config or {}
    mapping = cfg.get("mapping") or {}
    state = dict(d.sync_state or {})
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    state["last_payload"] = raw[:5000]
    state["last_payload_at"] = datetime.now(timezone.utc).isoformat()
    _set_state(d, state)
    d.last_seen_at = datetime.now(timezone.utc)
    if not mapping.get("employee"):
        return {"received": 0, "inserted": 0, "detail": "Saved as a sample; set the field mapping to read punches."}
    tz = tz_of(db, d, d.company_id)
    rows = fm.extract(payload if isinstance(payload, (str, bytes)) else payload, mapping)
    records = fm.to_records(rows, mapping, tz)
    if not any(str(r.get("device_user_id") or "").strip() for r in records):
        return {"received": len(rows), "inserted": 0}
    logs = ingest_records(db, d, records, "webhook", match_by=mapping.get("match_by") or "device_user_id")
    return {"received": sum(l.received for l in logs), "inserted": sum(l.inserted for l in logs),
            "duplicates": sum(l.duplicates for l in logs), "unmatched": sum(l.unmatched for l in logs)}


# ── Office connector ───────────────────────────────────────────────

def connector_config(db: Session, d: Device) -> dict:
    """Everything the connector needs to read this device (secrets included: it holds the key)."""
    if d.runs_on != "connector" or d.connection not in CONNECTOR_CAPABLE:
        raise BadRequest("This device isn't set to run on the office connector.")
    cfg = with_secrets(d.source_config)
    mapping = cfg.pop("mapping", None)
    for k in CONNECTOR_SETTINGS_EXCLUDE:
        cfg.pop(k, None)
    tz = tz_of(db, d, d.company_id)
    state = dict(d.sync_state or {})
    return {"device": {"uuid": str(d.uuid), "name": d.name, "serial_number": d.serial_number},
            "connection": d.connection, "settings": cfg, "mapping": mapping, "timezone": str(tz.key),
            "every_minutes": int(cfg.get("every_minutes") or 15),
            "state": {"last_time": state.get("last_time"), "last_id": state.get("last_id")},
            "push_path": "/attendance/devices/push", "heartbeat_path": "/attendance/connector/heartbeat",
            "config_version": d.updated_at.isoformat() if d.updated_at else None}


def heartbeat(db: Session, d: Device, data: dict) -> dict:
    now = datetime.now(timezone.utc)
    info = {k: (str(data.get(k))[:300] if data.get(k) is not None else None)
            for k in ("version", "host", "platform", "last_error", "last_read_at")}
    try:
        info["queue"] = int(data.get("queue") or 0)
    except (TypeError, ValueError):
        info["queue"] = 0
    d.connector_seen_at = now
    d.connector_info = info
    cur = data.get("cursor") or {}
    if isinstance(cur, dict) and (cur.get("last_time") or cur.get("last_id") is not None):
        state = dict(d.sync_state or {})
        if cur.get("last_time"):
            state["last_time"] = str(cur["last_time"])[:40]
        if cur.get("last_id") is not None:
            try:
                state["last_id"] = int(cur["last_id"])
            except (TypeError, ValueError):
                pass
        if data.get("last_error"):
            state["last_error"] = str(data["last_error"])[:500]
        else:
            state["last_error"] = None
            state["last_ok_at"] = data.get("last_read_at") or now.isoformat()
        _set_state(d, state)
    db.flush()
    return {"ok": True, "config_version": d.updated_at.isoformat() if d.updated_at else None,
            "every_minutes": int((d.source_config or {}).get("every_minutes") or 15)}


def connector_file(base_url: str, d: Device, key: str) -> dict:
    return {"server": base_url.rstrip("/"), "devices": [{"name": d.name, "key": key}],
            "note": "Settings for the attendance connector. Keep this file private: the key lets it send punches."}

