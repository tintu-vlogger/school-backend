"""Direct LAN pull from ZKTeco-protocol devices (ZKTeco, eSSL and other ZK-based
machines) on TCP or UDP port 4370, with the optional `pyzk` package.

Usually the office connector does this (the device is on the office network); the server
can do it too when it can reach the device.

Settings (source_config, connection "zk"): ip, port (4370), password (the device's
communication key, a number, 0 when not set), protocol tcp | udp, timeout, use_status
(trust the device's in/out state; off = first punch in, next out), every_minutes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from core.errors import BadRequest
from modules.attendance.device_common import check_public_host, need, store_secrets

SECRET_KEYS = ("password",)
# punch states of ZK devices: 0 check-in, 1 check-out, 2 break-out, 3 break-in, 4 OT-in, 5 OT-out
STATE_DIRECTION = {0: "in", 1: "out", 2: "out", 3: "in", 4: "in", 5: "out"}


def clean(cfg: dict, old: dict | None) -> dict:
    ip = (cfg.get("ip") or "").strip()
    if not ip:
        raise BadRequest("Enter the device's IP address.")
    pw = cfg.get("password")
    if pw not in (None, "", "••••••", "-") and not str(pw).isdigit():
        raise BadRequest("The communication key is a number (Menu, Comm., Connection on the device).")
    out = {"ip": ip[:255], "port": int(cfg.get("port") or 4370),
           "protocol": "udp" if cfg.get("protocol") == "udp" else "tcp",
           "timeout": max(3, min(int(cfg.get("timeout") or 15), 120)),
           "use_status": bool(cfg.get("use_status")),
           "lookback_days": max(0, min(int(cfg.get("lookback_days") if cfg.get("lookback_days") not in (None, "") else 30), 3650)),
           "every_minutes": max(5, min(int(cfg.get("every_minutes") or 15), 1440))}
    return store_secrets(out, SECRET_KEYS, cfg, old)


def _connect(cfg: dict, factory=None):
    if factory is None:
        zk_mod = need("zk", "pyzk", "Reading a ZKTeco device over the network")
        factory = zk_mod.ZK
    zk = factory(cfg["ip"], port=int(cfg.get("port") or 4370), timeout=int(cfg.get("timeout") or 15),
                 password=int(cfg.get("password") or 0), force_udp=cfg.get("protocol") == "udp", ommit_ping=True)
    try:
        return zk.connect()
    except Exception as exc:
        raise BadRequest(f"Couldn't connect to the device at {cfg['ip']}:{cfg.get('port') or 4370}: {exc}")


def _info(conn) -> dict:
    out = {}
    for key, fn in (("serial", "get_serialnumber"), ("firmware", "get_firmware_version"), ("name", "get_device_name"),
                    ("platform", "get_platform")):
        try:
            out[key] = str(getattr(conn, fn)())
        except Exception:
            pass
    try:
        conn.read_sizes()
        out["users"], out["records"] = getattr(conn, "users", None), getattr(conn, "records", None)
    except Exception:
        pass
    return out


def read(cfg: dict, state: dict, tz: ZoneInfo, factory=None, now: datetime | None = None
         ) -> tuple[list[dict], dict, dict]:
    """(records, new state, device info): punches newer than the last one read."""
    now = now or datetime.now(timezone.utc)
    if not cfg.get("_skip_host_check"):
        check_public_host(cfg.get("ip") or "")
    conn = _connect(cfg, factory)
    try:
        try:
            conn.disable_device()
        except Exception:
            pass
        info = _info(conn)
        logs = conn.get_attendance() or []
    finally:
        try:
            conn.enable_device()
        except Exception:
            pass
        try:
            conn.disconnect()
        except Exception:
            pass
    last = datetime.fromisoformat(state["last_time"]) if state.get("last_time") else (
        now.astimezone(tz).replace(tzinfo=None) - timedelta(days=int(cfg.get("lookback_days") or 30)))
    records, newest = [], None
    for n, a in enumerate(logs, start=1):
        ts = getattr(a, "timestamp", None)
        if not isinstance(ts, datetime) or ts < last:
            continue
        direction = STATE_DIRECTION.get(int(getattr(a, "punch", -1) or 0)) if cfg.get("use_status") else None
        records.append({"device_user_id": str(getattr(a, "user_id", "") or "").strip(), "timestamp": ts,
                        "direction": direction, "_row": n, "_local": ts})
        newest = ts if newest is None or ts > newest else newest
    state = dict(state)
    if newest:
        state["last_time"] = newest.isoformat()
    return records, state, info


def test(cfg: dict, tz: ZoneInfo, factory=None) -> dict:
    records, _, info = read({**cfg, "lookback_days": 3650}, {}, tz, factory)
    tail = records[-10:]
    return {"info": info, "total": len(records),
            "punches": [{"device_user_id": r["device_user_id"], "time": r["timestamp"].strftime("%Y-%m-%d %H:%M:%S"),
                         "direction": r["direction"]} for r in tail]}
