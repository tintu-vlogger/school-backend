"""Clock-in kiosks: a shared tablet at a secret link where people punch with their
employee code and a PIN they set themselves (HR can set or clear one too).

- The kiosk link carries a token that is stored hashed and shown once (new link on demand).
- PINs are 4 to 6 digits, stored hashed. Five wrong tries lock the PIN for 15 minutes.
- An optional or required photo is kept like a web selfie (deleted after the retention period).
- Offline: the tablet keeps punches in its browser and sends them when back online with
  the time they were made (`at`). Times up to 7 days old are accepted; a punch already
  recorded for that person within a minute is reported as a duplicate, so retries are safe.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from core import audit, context
from core.errors import BadRequest, NotFound
from core.security import hash_password, verify_password
from core.timeutil import iso_utc
from modules.attendance.models import Kiosk, KioskPin, Punch

MAX_TRIES = 5
LOCK_MINUTES = 15
MAX_OFFLINE_DAYS = 7
PHOTO_MODES = ("off", "optional", "required")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(k: Kiosk) -> str:
    token = "ksk_" + secrets.token_urlsafe(24)
    k.token_prefix = token[:12]
    k.token_hash = _hash(token)
    return token


def kiosk_for_token(db: Session, token: str | None) -> Kiosk | None:
    from core.db import bypass_rls
    if not token or len(token) < 20:
        return None
    with bypass_rls(db):
        rows = db.query(Kiosk).filter(Kiosk.token_prefix == token[:12], Kiosk.active.is_(True)).all()
        return next((k for k in rows if k.token_hash and hmac.compare_digest(k.token_hash, _hash(token))), None)


def kiosk_out(k: Kiosk) -> dict:
    return {"uuid": str(k.uuid), "name": k.name, "location_note": k.location_note, "photo": k.photo,
            "allowed_ips": k.allowed_ips or [], "active": k.active, "has_link": bool(k.token_hash),
            "last_seen_at": iso_utc(k.last_seen_at), "punches_count": k.punches_count,
            "created_at": iso_utc(k.created_at)}


def save(db: Session, company_id: int, data: dict, row: Kiosk | None = None) -> tuple[Kiosk, str | None]:
    name = (data.get("name") or (row.name if row else "")).strip()
    if len(name) < 2:
        raise BadRequest("Give the kiosk a name, e.g. Reception tablet.")
    photo = data.get("photo") or (row.photo if row else "off")
    if photo not in PHOTO_MODES:
        raise BadRequest("Choose whether a photo is off, optional or required.")
    import ipaddress
    ips = [str(x).strip() for x in (data.get("allowed_ips") if data.get("allowed_ips") is not None
                                     else (row.allowed_ips if row else [])) or [] if str(x).strip()]
    for ip in ips:
        try:
            ipaddress.ip_network(ip, strict=False)
        except ValueError:
            raise BadRequest(f"{ip} is not a valid IP address or range.")
    token = None
    if row is None:
        row = Kiosk(company_id=company_id, name=name)
        token = new_token(row)
        db.add(row)
    row.name = name
    if "location_note" in data:
        row.location_note = (data.get("location_note") or "").strip() or None
    row.photo = photo
    row.allowed_ips = ips
    if "active" in data and data["active"] is not None:
        row.active = bool(data["active"])
    db.flush()
    return row, token


def get(db: Session, company_id: int, uuid) -> Kiosk:
    k = db.query(Kiosk).filter(Kiosk.company_id == company_id, Kiosk.uuid == uuid).first()
    if k is None:
        raise NotFound("Kiosk")
    return k


# ── PINs ────────────────────────────────────────────────────────────

def check_pin_format(pin: str) -> str:
    pin = (pin or "").strip()
    if not re.fullmatch(r"\d{4,6}", pin):
        raise BadRequest("The PIN must be 4 to 6 digits.")
    if len(set(pin)) == 1 or pin in "0123456789" or pin in "9876543210":
        raise BadRequest("Choose a PIN that is harder to guess (not 1111 or 1234).")
    return pin


def set_pin(db: Session, company_id: int, employee_id: int, pin: str, *, by_hr: bool = False) -> None:
    pin = check_pin_format(pin)
    row = db.query(KioskPin).filter(KioskPin.company_id == company_id, KioskPin.employee_id == employee_id).first()
    if row is None:
        row = KioskPin(company_id=company_id, employee_id=employee_id, pin_hash="")
        db.add(row)
    row.pin_hash = hash_password(pin)
    row.failed_count = 0
    row.locked_until = None
    row.set_by_name = context.current().actor_name
    db.flush()
    audit.event(db, "attendance.kiosk_pin_set", "kiosk_pins", row.id,
                {"employee_id": employee_id, "by_hr": by_hr})


def clear_pin(db: Session, company_id: int, employee_id: int) -> bool:
    row = db.query(KioskPin).filter(KioskPin.company_id == company_id, KioskPin.employee_id == employee_id).first()
    if row is None:
        return False
    audit.event(db, "attendance.kiosk_pin_cleared", "kiosk_pins", row.id, {"employee_id": employee_id})
    db.delete(row)
    db.flush()
    return True


def pin_status(db: Session, company_id: int, employee_id: int) -> dict:
    row = db.query(KioskPin).filter(KioskPin.company_id == company_id, KioskPin.employee_id == employee_id).first()
    return {"has_pin": row is not None, "set_at": iso_utc(row.updated_at) if row else None,
            "locked": bool(row and row.locked_until and row.locked_until > datetime.now(timezone.utc))}


def pins_set(db: Session, company_id: int) -> set[int]:
    return {e for (e,) in db.query(KioskPin.employee_id).filter(KioskPin.company_id == company_id).all()}


# ── Punching ────────────────────────────────────────────────────────

def _employee_by_code(db: Session, company_id: int, code: str):
    from modules.employees.models import Employee
    code = (code or "").strip()
    if not code:
        return None
    return (db.query(Employee).filter(Employee.company_id == company_id, Employee.active.is_(True),
                                      func.lower(Employee.employee_code) == code.lower()).first())


def _verify(db: Session, company_id: int, emp, pin: str, now: datetime) -> None:
    row = db.query(KioskPin).filter(KioskPin.company_id == company_id, KioskPin.employee_id == emp.id).first() \
        if emp is not None else None
    if row is None:
        # same answer for an unknown code and a person without a PIN
        raise BadRequest("Employee code or PIN is not right.")
    if row.locked_until and row.locked_until > now:
        mins = max(1, int((row.locked_until - now).total_seconds() // 60) + 1)
        raise BadRequest(f"Too many wrong PINs. Try again in {mins} min or ask HR.")
    if not verify_password((pin or "").strip(), row.pin_hash):
        row.failed_count = (row.failed_count or 0) + 1
        if row.failed_count >= MAX_TRIES:
            row.locked_until = now + timedelta(minutes=LOCK_MINUTES)
            row.failed_count = 0
            audit.event(db, "attendance.kiosk_pin_locked", "kiosk_pins", row.id, {"employee_id": emp.id})
        db.flush()
        raise BadRequest("Employee code or PIN is not right.")
    if row.failed_count:
        row.failed_count = 0


def _when(at, now: datetime) -> tuple[datetime, bool]:
    """The punch time: the tablet's time for punches kept offline, else now."""
    if not at:
        return now, False
    try:
        t = at if isinstance(at, datetime) else datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        raise BadRequest("The punch time is not valid.")
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    t = t.astimezone(timezone.utc).replace(microsecond=0)
    if abs((now - t).total_seconds()) <= 120:
        return now, False
    if t > now:
        raise BadRequest("The punch time is in the future. Check the tablet's clock.")
    if now - t > timedelta(days=MAX_OFFLINE_DAYS):
        raise BadRequest("This punch is more than 7 days old. Ask HR to add it.")
    return t, True


def punch(db: Session, k: Kiosk, data: dict) -> dict:
    """A punch with employee code and PIN. At a kiosk where the face check is required, the
    PIN is accepted only through the face check's fallback (`_face_fallback`, set by the face
    attendance module, never by the request)."""
    cid = k.company_id
    ctx = context.current()
    from modules.attendance import service
    if not service._ip_allowed(ctx.ip_address, k.allowed_ips or []):
        raise BadRequest("This kiosk can be used only from the office network.")
    if not data.get("_face_fallback"):
        try:
            from modules.face_attendance import service as face
            if face.kiosk_mode(db, k) == "required":
                raise BadRequest("This kiosk checks your face first. Look at the camera; your PIN is asked "
                                 "only if your face is not recognised.")
        except ImportError:
            pass
    now = datetime.now(timezone.utc).replace(microsecond=0)
    emp = _employee_by_code(db, cid, data.get("employee_code"))
    _verify(db, cid, emp, data.get("pin"), now)
    out = record(db, k, emp, data, now, note=data.get("_note"))
    if not data.get("_face_fallback"):
        for key in ("punch_id", "at", "employee_id"):
            out.pop(key, None)
    return out


def record(db: Session, k: Kiosk, emp, data: dict, now: datetime | None = None, note: str | None = None) -> dict:
    """Record a kiosk punch for a person already identified (by PIN or by face). Returns the
    result shown on the tablet; `punch_id` is the new punch (None for a duplicate)."""
    from modules.attendance import service
    cid = k.company_id
    ctx = context.current()
    if not service._ip_allowed(ctx.ip_address, k.allowed_ips or []):
        raise BadRequest("This kiosk can be used only from the office network.")
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    photo = data.get("photo")
    if k.photo == "required" and not photo:
        raise BadRequest("Look at the camera: a photo is needed to punch here.")
    if k.photo == "off":
        photo = None
    if photo and (not str(photo).startswith("data:image/") or len(photo) > 400_000):
        raise BadRequest("The photo must be an image under 300 KB.")
    at, offline = _when(data.get("at"), now)
    name = " ".join(x for x in (emp.first_name, emp.last_name) if x)
    near = (db.query(Punch).filter(Punch.company_id == cid, Punch.employee_id == emp.id, Punch.ignored.is_(False),
                                   Punch.punched_at >= at - timedelta(seconds=60),
                                   Punch.punched_at <= at + timedelta(seconds=60)).first())
    if near is not None:
        return {"status": "duplicate", "employee_name": name, "first_name": emp.first_name,
                "punched_at": iso_utc(near.punched_at), "direction": near.direction, "offline": offline,
                "punch_id": None, "at": at, "employee_id": emp.id}
    ctx.actor_type, ctx.actor_name = "system", f"Kiosk: {k.name}"
    text_note = f"{k.name}{' · ' + note if note else ''}{' (sent after being offline)' if offline else ''}"
    p = Punch(company_id=cid, employee_id=emp.id, punched_at=at, source="kiosk", selfie=photo,
              ip_address=ctx.ip_address, note=text_note[:300])
    db.add(p)
    k.punches_count = (k.punches_count or 0) + 1
    k.last_seen_at = now
    db.flush()
    service.recompute_for_punch_times(db, cid, [(emp.id, at)])
    # which punch of the day this was
    tz = service.company_tz(db, cid)
    day_start = datetime.combine(at.astimezone(tz).date(), datetime.min.time(), tzinfo=tz)
    n = (db.query(func.count(Punch.id)).filter(Punch.company_id == cid, Punch.employee_id == emp.id,
                                               Punch.ignored.is_(False), Punch.punched_at >= day_start,
                                               Punch.punched_at <= at).scalar()) or 1
    return {"status": "ok", "employee_name": name, "first_name": emp.first_name, "punched_at": iso_utc(at),
            "direction": "in" if n % 2 == 1 else "out", "offline": offline, "punch_id": p.id, "at": at,
            "employee_id": emp.id}


def info(db: Session, k: Kiosk) -> dict:
    from modules.company.models import Company
    from core.db import bypass_rls
    with bypass_rls(db):
        c = db.query(Company).filter(Company.id == k.company_id).first()
    k.last_seen_at = datetime.now(timezone.utc)
    return {"name": k.name, "location_note": k.location_note, "photo": k.photo,
            "company": {"name": c.company_name if c else "", "logo": c.logo_url if c else None,
                        "timezone": c.timezone if c else None},
            "now": iso_utc(datetime.now(timezone.utc))}
