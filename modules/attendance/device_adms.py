"""ADMS "cloud push" (the iclock protocol of ZKTeco, eSSL and other ZK-based devices).

The device is set up with our server address and port (Menu, Comm., Cloud Server Setting /
ADMS) and calls us; nothing needs to be opened on the office network:

    GET  /iclock/cdata?SN=...&options=all        handshake: we answer with its options
    POST /iclock/cdata?SN=...&table=ATTLOG       punches, one per line:
                                                 PIN \t YYYY-MM-DD HH:MM:SS \t state \t verify \t workcode ...
    POST /iclock/cdata?SN=...&table=OPERLOG      operations and user records (USER PIN=1\tName=...)
    GET  /iclock/getrequest?SN=...               "OK" or queued commands "C:<id>:<command>"
    POST /iclock/devicecmd?SN=...                results of those commands (ID=1&Return=0&CMD=...)

Older eSSL firmware uses the same paths with ".aspx". The device is recognised by its serial
number, registered on our Devices page (unique across all companies). Punch times are the
device's local time.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core.db import bypass_rls, get_db, set_tenant
from modules.attendance.device_zk import STATE_DIRECTION
from modules.attendance.models import Device

log = logging.getLogger("hrms.adms")
SEEN_EVERY_SECONDS = 60
MAX_COMMANDS = 20


# ── Parsing (pure) ──────────────────────────────────────────────────

def decode(body: bytes) -> str:
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            return body.decode(enc)
        except UnicodeDecodeError:
            continue
    return body.decode("latin-1", "replace")


def parse_attlog(text: str, use_status: bool = False) -> list[dict]:
    out = []
    for n, line in enumerate((text or "").replace("\r", "\n").split("\n"), start=1):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            parts = line.split()
            if len(parts) >= 3:  # "PIN date time state ..."
                parts = [parts[0], f"{parts[1]} {parts[2]}"] + parts[3:]
        if len(parts) < 2:
            continue
        pin, ts = parts[0].strip(), parts[1].strip()
        state = parts[2].strip() if len(parts) > 2 else ""
        direction = None
        if use_status and state.lstrip("-").isdigit():
            direction = STATE_DIRECTION.get(int(state))
        out.append({"device_user_id": pin, "timestamp": ts, "direction": direction, "_row": n,
                    "_state": state, "_verify": parts[3].strip() if len(parts) > 3 else ""})
    return out


def _kv(s: str) -> dict:
    out = {}
    for item in s.split("\t"):
        if "=" in item:
            k, v = item.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def parse_operlog(text: str) -> dict:
    """User records (USER PIN=..), and how many other lines (operations, fingerprints...)."""
    users, other = [], 0
    for line in (text or "").replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("USER "):
            kv = _kv(line[5:])
            if kv.get("PIN"):
                users.append({"pin": kv["PIN"], "name": kv.get("Name", ""), "card": kv.get("Card", ""),
                              "privilege": kv.get("Pri", "")})
        else:
            other += 1
    return {"users": users, "other": other}


def parse_info(s: str | None) -> dict:
    """INFO=Ver 6.60 Apr 2 2016,users,fingerprints,punches,IP,fp version,face version,faces,..."""
    if not s:
        return {}
    parts = [p.strip() for p in s.split(",")]
    keys = ["firmware", "users", "fingerprints", "punches", "ip", "fp_version", "face_version", "face_templates", "faces"]
    return {k: parts[i] for i, k in enumerate(keys) if i < len(parts) and parts[i]}


def _tz_value(tz: ZoneInfo) -> str:
    off = datetime.now(tz).utcoffset() or timedelta(0)
    mins = int(off.total_seconds() // 60)
    return str(mins // 60) if mins % 60 == 0 else str(mins)


def options_text(sn: str, state: dict, tz: ZoneInfo, realtime: bool = True) -> str:
    adms = state.get("adms") or {}
    lines = [
        f"GET OPTION FROM: {sn}",
        f"ATTLOGStamp={adms.get('att_stamp') or 0}",
        f"OPERLOGStamp={adms.get('op_stamp') or 0}",
        "ATTPHOTOStamp=None",
        "ErrorDelay=60",
        "Delay=30",
        "TransTimes=00:00;14:05",
        "TransInterval=1",
        "TransFlag=TransData AttLog OpLog EnrollUser",
        f"TimeZone={_tz_value(tz)}",
        f"Realtime={1 if realtime else 0}",
        "Encrypt=None",
    ]
    return "\n".join(lines) + "\n"


def pending_commands(state: dict) -> tuple[list[dict], dict]:
    """Commands waiting to be sent; they are marked sent in the returned state."""
    state = dict(state)
    adms = dict(state.get("adms") or {})
    cmds = [dict(c) for c in adms.get("commands") or []]
    out = [c for c in cmds if c.get("status") == "pending"]
    for c in out:
        c["status"], c["sent_at"] = "sent", datetime.now(timezone.utc).isoformat()
    adms["commands"] = cmds
    state["adms"] = adms
    return out, state


def command_results(text: str, state: dict) -> dict:
    state = dict(state)
    adms = dict(state.get("adms") or {})
    cmds = [dict(c) for c in adms.get("commands") or []]
    for line in (text or "").replace("\r", "\n").split("\n"):
        kv = dict(parse_qsl(line.strip(), keep_blank_values=True))
        if not kv.get("ID"):
            continue
        for c in cmds:
            if str(c.get("id")) == kv["ID"]:
                c["status"] = "done" if kv.get("Return", "0") in ("0", "") else "failed"
                c["result"] = kv.get("Return")
    adms["commands"] = cmds
    state["adms"] = adms
    return state


def queue_command(state: dict, command: str) -> dict:
    state = dict(state or {})
    adms = dict(state.get("adms") or {})
    cmds = [dict(c) for c in adms.get("commands") or []]
    next_id = max([int(c.get("id") or 0) for c in cmds] + [int(adms.get("last_cmd_id") or 0)]) + 1
    cmds.append({"id": next_id, "cmd": command, "status": "pending", "queued_at": datetime.now(timezone.utc).isoformat()})
    adms["commands"] = cmds[-MAX_COMMANDS:]
    adms["last_cmd_id"] = next_id
    state["adms"] = adms
    return state


def reload_command(tz: ZoneInfo, days: int = 31) -> str:
    end = datetime.now(tz).replace(tzinfo=None, microsecond=0)
    start = end - timedelta(days=days)
    return f"DATA QUERY ATTLOG StartTime={start:%Y-%m-%d %H:%M:%S}\tEndTime={end:%Y-%m-%d %H:%M:%S}"


# ── Endpoints ───────────────────────────────────────────────────────

router = APIRouter(prefix="/iclock", tags=["Attendance devices (ADMS)"], include_in_schema=False)


def _text(s: str, status: int = 200) -> PlainTextResponse:
    return PlainTextResponse(s, status_code=status)


def _device(db: Session, sn: str | None) -> Device | None:
    sn = (sn or "").strip()
    if not sn or len(sn) > 80:
        return None
    with bypass_rls(db):
        d = (db.query(Device).filter(Device.connection == "adms", Device.serial_number == sn,
                                     Device.active.is_(True)).first())
    return d


def _ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    return (fwd.split(",")[0].strip() if fwd else None) or (request.client.host if request.client else None)


def _open(db: Session, request: Request, sn: str | None):
    """The device for this serial, with the tenant set; None (and the reply) when refused."""
    from core import context
    d = _device(db, sn)
    if d is None:
        log.info("ADMS call from unknown serial %s (%s)", sn, _ip(request))
        return None, _text("Unknown device", 404)
    allowed = [x.strip() for x in str((d.source_config or {}).get("allowed_ips") or "").split(",") if x.strip()]
    if allowed and _ip(request) not in allowed:
        return None, _text("Not allowed from this address", 403)
    set_tenant(db, d.company_id)
    ctx = context.current()
    ctx.actor_type, ctx.actor_name = "system", f"Device: {d.name}"
    return d, None


def _tz(db: Session, d: Device) -> ZoneInfo:
    from modules.attendance import service
    if d.timezone:
        try:
            return ZoneInfo(d.timezone)
        except Exception:
            pass
    return service.company_tz(db, d.company_id)


def _touch(d: Device, request: Request, extra: dict | None = None, force: bool = False) -> bool:
    """Mark the device online (at most once a minute unless something changed)."""
    now = datetime.now(timezone.utc)
    changed = force or bool(extra)
    if d.last_seen_at is None or (now - d.last_seen_at).total_seconds() >= SEEN_EVERY_SECONDS:
        d.last_seen_at = now
        changed = True
    if extra or changed:
        state = dict(d.sync_state or {})
        adms = dict(state.get("adms") or {})
        adms["ip"] = _ip(request)
        adms.update(extra or {})
        state["adms"] = adms
        d.sync_state = state
        flag_modified(d, "sync_state")
    return changed


@router.get("/cdata")
@router.get("/cdata.aspx")
def handshake(request: Request, SN: str | None = None, db: Session = Depends(get_db)):
    d, refused = _open(db, request, SN)
    if refused:
        return refused
    q = request.query_params
    extra = {"pushver": q.get("pushver"), "language": q.get("language"), "handshake_at": datetime.now(timezone.utc).isoformat()}
    _touch(d, request, {k: v for k, v in extra.items() if v}, force=True)
    text = options_text(d.serial_number, d.sync_state or {}, _tz(db, d))
    db.commit()
    return _text(text)


@router.post("/cdata")
@router.post("/cdata.aspx")
async def upload(request: Request, SN: str | None = None, table: str | None = None, db: Session = Depends(get_db)):
    body = decode(await request.body())
    d, refused = _open(db, request, SN)
    if refused:
        return refused
    q = request.query_params
    t = (table or "").upper()
    lines = [l for l in body.replace("\r", "\n").split("\n") if l.strip()]
    if t == "ATTLOG":
        from modules.attendance import devices
        recs = parse_attlog(body, bool((d.source_config or {}).get("use_status")))
        for i in range(0, len(recs), devices.MAX_BATCH):
            devices.ingest(db, d.company_id, d, recs[i:i + devices.MAX_BATCH], "adms")
        stamp = q.get("Stamp")
        _touch(d, request, {"att_stamp": stamp} if stamp and stamp.isdigit() else {"last_attlog_at": datetime.now(timezone.utc).isoformat()}, force=True)
    elif t == "OPERLOG":
        parsed = parse_operlog(body)
        extra = {}
        if parsed["users"]:
            state = dict(d.sync_state or {})
            known = {u["pin"]: u for u in state.get("device_users") or []}
            for u in parsed["users"]:
                known[u["pin"]] = u
            state["device_users"] = list(known.values())[:10000]
            d.sync_state = state
            flag_modified(d, "sync_state")
        stamp = q.get("OpStamp") or q.get("Stamp")
        if stamp and stamp.isdigit():
            extra["op_stamp"] = stamp
        _touch(d, request, extra, force=True)
    else:  # ATTPHOTO, BIODATA, options and other tables: accepted and not kept
        _touch(d, request)
    db.commit()
    return _text(f"OK: {len(lines)}")


@router.get("/getrequest")
@router.get("/getrequest.aspx")
def get_request(request: Request, SN: str | None = None, INFO: str | None = None, db: Session = Depends(get_db)):
    d, refused = _open(db, request, SN)
    if refused:
        return refused
    extra = {"info": parse_info(INFO)} if INFO else None
    changed = _touch(d, request, extra)
    cmds, state = pending_commands(d.sync_state or {})
    if cmds:
        d.sync_state = state
        flag_modified(d, "sync_state")
        changed = True
    if changed:
        db.commit()
    if not cmds:
        return _text("OK")
    return _text("\n".join(f"C:{c['id']}:{c['cmd']}" for c in cmds) + "\n")


@router.post("/devicecmd")
@router.post("/devicecmd.aspx")
async def device_cmd(request: Request, SN: str | None = None, db: Session = Depends(get_db)):
    body = decode(await request.body())
    d, refused = _open(db, request, SN)
    if refused:
        return refused
    d.sync_state = command_results(body, d.sync_state or {})
    flag_modified(d, "sync_state")
    _touch(d, request)
    db.commit()
    return _text("OK")


@router.get("/ping")
@router.get("/ping.aspx")
def ping(request: Request, SN: str | None = None, db: Session = Depends(get_db)):
    d, refused = _open(db, request, SN)
    if refused:
        return refused
    if _touch(d, request):
        db.commit()
    return _text("OK")
