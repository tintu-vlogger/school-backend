"""Device connection endpoints: presets, testing a connection before saving, ADMS commands,
the vendor push address, and the office connector (settings file, configuration, heartbeat).
Included in the attendance router (prefix /attendance)."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core import audit
from core.db import get_db, set_tenant
from core.errors import BadRequest
from core.security import require_permission
from modules.attendance import device_adms, device_presets, device_sources, devices
from modules.attendance.models import Device

router = APIRouter()
SETTINGS = require_permission("time.attendance.settings")


@router.get("/device-presets")
def presets(user: dict = Depends(SETTINGS)):
    return device_presets.public()


class TestIn(BaseModel):
    connection: str = Field(..., max_length=20)
    config: dict = Field(default_factory=dict)
    device_uuid: str | None = None


@router.post("/devices/test")
def test_connection(req: TestIn, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    """Try settings from the connection form and show sample rows. Nothing is saved."""
    cid = user["company_id"]
    d = devices.get(db, cid, req.device_uuid) if req.device_uuid else None
    try:
        return device_sources.test(db, cid, req.connection, req.config, d)
    except HTTPException as exc:
        return {"ok": False, "notes": [exc.detail], "sample": None}
    except Exception as exc:  # a driver or network error: show it, don't fail the page
        return {"ok": False, "notes": [f"{exc.__class__.__name__}: {exc}"[:500]], "sample": None}


@router.get("/devices/{uuid}/device-users")
def device_users(uuid: UUID, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    """Users the device reported (ADMS), to help with enrolments."""
    d = devices.get(db, user["company_id"], uuid)
    return {"items": (d.sync_state or {}).get("device_users") or []}


class CommandIn(BaseModel):
    action: str = Field(..., max_length=20)  # reload | info
    days: int = Field(31, ge=1, le=365)


@router.post("/devices/{uuid}/command")
def adms_command(uuid: UUID, req: CommandIn, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    """Queue a command for an ADMS device; it picks it up on its next call."""
    d = devices.get(db, user["company_id"], uuid)
    if d.connection != "adms":
        raise BadRequest("Commands are only for ADMS (cloud push) devices.")
    tz = device_sources.tz_of(db, d, d.company_id)
    if req.action == "reload":
        cmd = device_adms.reload_command(tz, req.days)
    elif req.action == "info":
        cmd = "INFO"
    else:
        raise BadRequest("Unknown command.")
    d.sync_state = device_adms.queue_command(d.sync_state or {}, cmd)
    flag_modified(d, "sync_state")
    audit.event(db, "attendance.device_command", "attendance_devices", d.id, {"command": req.action},
                entity_uuid=str(d.uuid), entity_label=d.name)
    db.commit()
    return {"detail": "Queued. The device picks it up within a minute when it is online."}


def _public_base(request: Request) -> str:
    """The API's own address as the browser reached it, e.g. https://api.example.com/api/v1."""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}/api/v1"


@router.post("/devices/{uuid}/connector-file")
def connector_file(uuid: UUID, request: Request, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    """Settings file for the office connector. Makes a new key for the device (the old one
    stops working), because keys are stored only as a hash."""
    d = devices.get(db, user["company_id"], uuid)
    if d.runs_on != "connector":
        raise BadRequest("Set this device to run on the office connector first.")
    key = devices.new_key(d)
    audit.event(db, "attendance.connector_file", "attendance_devices", d.id, {}, entity_uuid=str(d.uuid),
                entity_label=d.name)
    db.commit()
    return {"file_name": "connector.json", "content": device_sources.connector_file(_public_base(request), d, key)}


# ── Called by devices and the connector with the device key ────────

def _by_key(db: Session, key: str | None) -> Device:
    from core import context
    d = devices.device_for_key(db, key)
    if d is None:
        raise HTTPException(status_code=401, detail="Unknown or inactive device key.")
    set_tenant(db, d.company_id)
    ctx = context.current()
    ctx.actor_type, ctx.actor_name = "system", f"Device: {d.name}"
    return d


def _key(x_device_key: str | None, authorization: str | None) -> str | None:
    if x_device_key:
        return x_device_key
    a = authorization or ""
    return a[7:].strip() if a.lower().startswith("device ") else None


@router.get("/connector/config")
def connector_config(x_device_key: str | None = Header(None), authorization: str | None = Header(None),
                     db: Session = Depends(get_db)):
    d = _by_key(db, _key(x_device_key, authorization))
    return device_sources.connector_config(db, d)


@router.post("/connector/heartbeat")
def connector_heartbeat(data: dict, x_device_key: str | None = Header(None), authorization: str | None = Header(None),
                        db: Session = Depends(get_db)):
    d = _by_key(db, _key(x_device_key, authorization))
    out = device_sources.heartbeat(db, d, data or {})
    db.commit()
    return out


@router.post("/devices/hook/{key}")
async def vendor_push(key: str, request: Request, db: Session = Depends(get_db)):
    """For devices and software that post their own format (Hikvision event push, any JSON).
    The device key is part of the address because many devices can't add headers."""
    payload = await device_sources.read_hook_body(request)
    d = _by_key(db, key)
    if d.connection != "webhook":
        raise HTTPException(status_code=400, detail="This key isn't for a vendor push device.")
    out = device_sources.hook(db, d, payload)
    db.commit()
    return out
