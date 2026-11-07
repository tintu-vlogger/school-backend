"""Face attendance API.

Admin (permission time.face.manage): settings, kiosks, enrolments (HR captures with the
employee's consent), consent removal and the review of flagged checks.
Self-service (signed in): my face sign-in status, enrol with consent, withdraw consent,
clock in with a selfie match.
Kiosk (no sign-in, the kiosk's secret link): face settings, the templates of the people at
its location, punches by face, PIN after the face check, failed checks and offline sync.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core import context
from core.db import get_db, set_tenant
from core.errors import BadRequest, NotFound
from core.security import require_permission
from modules.attendance import kiosk as att_kiosk, service as att
from modules.attendance.models import Kiosk
from modules.face_attendance import service

router = APIRouter(prefix="/face-attendance", tags=["Face attendance"])

MANAGE = require_permission("time.face.manage")
SELF = require_permission("time.attendance.view")


def _me(db: Session, user: dict):
    if not user.get("employee_id"):
        raise BadRequest("Sign in with your employee account to use face sign-in.")
    return att.employee(db, user["company_id"], user["employee_id"])


# ── Admin ───────────────────────────────────────────────────────────

@router.get("/summary")
def summary(user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    return service.summary(db, user["company_id"])


@router.get("/settings")
def get_settings(user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    return {"config": service.settings(db, user["company_id"]), "default_consent_text": service.CONSENT_TEXT,
            "liveness_levels": service.LIVENESS}


class SettingsIn(BaseModel):
    config: dict[str, Any]


@router.put("/settings")
def put_settings(req: SettingsIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    out = service.save_settings(db, user["company_id"], req.config)
    db.commit()
    return {"config": out}


@router.get("/kiosks")
def list_kiosks(user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    return {"items": service.kiosks(db, user["company_id"])}


class KioskFaceIn(BaseModel):
    mode: str = "off"
    location_id: int | None = None
    threshold: float | None = None
    liveness: str | None = None


@router.put("/kiosks/{uuid}")
def put_kiosk(uuid: UUID, req: KioskFaceIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = user["company_id"]
    k = att_kiosk.get(db, cid, uuid)
    service.save_kiosk(db, cid, k, req.model_dump())
    db.commit()
    return next(x for x in service.kiosks(db, cid) if x["uuid"] == str(uuid))


@router.get("/enrolments")
def list_enrolments(q: str | None = None, status: str | None = None, location_id: int | None = None,
                    page: int = Query(1, ge=1), size: int = Query(25, ge=1, le=200),
                    user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    return service.enrolments(db, user["company_id"], q, status, page, size, location_id)


class EnrolIn(BaseModel):
    samples: list[list[float]] = Field(..., min_length=1, max_length=5)
    model: str | None = None
    consent: bool = False
    consent_version: int = 0
    liveness: dict | None = None


@router.post("/enrolments/{employee_uuid}", status_code=201)
def hr_enrol(employee_uuid: UUID, req: EnrolIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    """HR captures the samples in person after the employee has read and agreed to the notice."""
    cid = user["company_id"]
    emp = att.employee_by_uuid(db, cid, employee_uuid)
    row = service.enrol(db, cid, emp, req.model_dump(), by="hr")
    db.commit()
    return service.enrolment_out(row)


class RemoveIn(BaseModel):
    reason: str = Field(..., min_length=3, max_length=300)


@router.post("/enrolments/{employee_uuid}/remove")
def hr_remove(employee_uuid: UUID, req: RemoveIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    """Delete a person's face data (the employee withdrew consent in writing, or a wrong enrolment)."""
    cid = user["company_id"]
    emp = att.employee_by_uuid(db, cid, employee_uuid)
    if not service.withdraw(db, cid, emp, by="hr", reason=req.reason):
        raise BadRequest("This person has no face data.")
    db.commit()
    return {"ok": True}


@router.get("/events")
def list_events(status: str | None = "open", kind: str | None = None, employee_uuid: UUID | None = None,
                start: date | None = None, end: date | None = None,
                page: int = Query(1, ge=1), size: int = Query(25, ge=1, le=200),
                user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = user["company_id"]
    tz = att.company_tz(db, cid)
    eid = att.employee_by_uuid(db, cid, employee_uuid).id if employee_uuid else None
    s = datetime.combine(start, time(0), tzinfo=tz) if start else None
    e = datetime.combine(end + timedelta(days=1), time(0), tzinfo=tz) if end else None
    return service.events(db, cid, status=status, kind=kind, employee_id=eid, start=s, end=e, page=page, size=size)


@router.get("/events/{uuid}/snapshot")
def event_snapshot(uuid: UUID, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    from core import audit
    ev = service.get_event(db, user["company_id"], uuid)
    if not ev.snapshot:
        raise NotFound("Snapshot")
    audit.event(db, "face.snapshot_viewed", "face_events", ev.id, {"kind": ev.kind}, entity_uuid=str(ev.uuid))
    db.commit()
    return {"image": ev.snapshot}


class ReviewIn(BaseModel):
    decision: str
    note: str | None = Field(None, max_length=300)
    add_punch: bool = False


@router.post("/events/{uuid}/review")
def review_event(uuid: UUID, req: ReviewIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = user["company_id"]
    ev = service.get_event(db, cid, uuid)
    service.review(db, cid, ev, req.decision, req.note, req.add_punch)
    db.commit()
    return {"ok": True, "review_status": ev.review_status}


# ── Self-service ────────────────────────────────────────────────────

@router.get("/me")
def my_status(user: dict = Depends(SELF), db: Session = Depends(get_db)):
    emp = _me(db, user)
    return service.self_status(db, user["company_id"], emp)


@router.post("/me/enrol", status_code=201)
def my_enrol(req: EnrolIn, user: dict = Depends(SELF), db: Session = Depends(get_db)):
    emp = _me(db, user)
    row = service.enrol(db, user["company_id"], emp, req.model_dump(), by="self")
    db.commit()
    return service.enrolment_out(row)


@router.post("/me/withdraw")
def my_withdraw(user: dict = Depends(SELF), db: Session = Depends(get_db)):
    emp = _me(db, user)
    n = service.withdraw(db, user["company_id"], emp, by="self")
    db.commit()
    return {"ok": True, "deleted": n}


class SelfClockIn(BaseModel):
    descriptor: list[float] = Field(..., min_length=64, max_length=2048)
    liveness: dict | None = None
    selfie: str | None = None  # kept with the punch when the company asks for selfies
    snapshot: str | None = Field(None, max_length=120_000)  # small photo for the face check record
    latitude: float | None = Field(None, ge=-90, le=90)
    longitude: float | None = Field(None, ge=-180, le=180)
    accuracy: float | None = None
    direction: str | None = None


@router.post("/me/clock")
def my_clock(req: SelfClockIn, user: dict = Depends(SELF), db: Session = Depends(get_db)):
    """Clock in or out on the web or phone with a selfie that matches your enrolled face."""
    cid = user["company_id"]
    emp = _me(db, user)
    data = req.model_dump()
    ok, score, msg = service.self_clock_check(db, cid, emp, data)
    if not ok:
        db.commit()  # keep the failed check (a run of them is flagged for HR)
        raise BadRequest(msg)
    p = att.clock(db, cid, emp.id, {**{k: data[k] for k in ("latitude", "longitude", "accuracy", "selfie", "direction")},
                                    "_face_ok": True})
    service.record_self_match(db, cid, emp, p, score, data)
    db.commit()
    out = att.today_card(db, cid, emp.id)
    db.commit()
    return {"punch": att.punch_out(p), "today": out, "score": round(score, 3)}


# ── Kiosk (public, opened at the kiosk link) ────────────────────────

def _kiosk(db: Session, token: str) -> Kiosk:
    k = att_kiosk.kiosk_for_token(db, token)
    if k is None:
        raise NotFound("Kiosk")
    ctx = context.current()
    ctx.actor_type, ctx.actor_name = "system", f"Kiosk: {k.name}"
    set_tenant(db, k.company_id)
    if not att._ip_allowed(ctx.ip_address, k.allowed_ips or []):
        raise BadRequest("This kiosk can be used only from the office network.")
    return k


@router.get("/kiosk/{token}/config")
def kiosk_config(token: str, db: Session = Depends(get_db)):
    k = _kiosk(db, token)
    return service.kiosk_config(db, k)


@router.get("/kiosk/{token}/gallery")
def kiosk_gallery(token: str, since: str | None = None, db: Session = Depends(get_db)):
    k = _kiosk(db, token)
    out = service.gallery(db, k, since)
    db.commit()
    return out


class KioskFaceIn_(BaseModel):
    method: str = "face"  # face | pin | miss
    employee_uuid: str | None = Field(None, max_length=40)
    descriptor: list[float] | None = Field(None, max_length=2048)
    score: float | None = None
    attempts: int | None = None
    liveness: dict | None = None
    snapshot: str | None = None
    reason: str | None = Field(None, max_length=20)
    employee_code: str | None = Field(None, max_length=30)
    pin: str | None = Field(None, max_length=12)
    at: datetime | None = None
    client_id: str | None = Field(None, max_length=60)


def _one(db: Session, k: Kiosk, item: dict) -> dict:
    m = item.get("method")
    if m == "face":
        return service.kiosk_face_punch(db, k, item)
    if m == "pin":
        if not item.get("employee_code") or not item.get("pin"):
            raise BadRequest("Enter your employee ID and PIN.")
        return service.kiosk_pin_fallback(db, k, item)
    if m == "miss":
        return service.kiosk_miss(db, k, item)
    raise BadRequest("Unknown kind of punch.")


@router.post("/kiosk/{token}/punch")
def kiosk_punch(token: str, req: KioskFaceIn_, db: Session = Depends(get_db)):
    k = _kiosk(db, token)
    try:
        out = _one(db, k, req.model_dump())
    except BadRequest:
        db.commit()  # keep failed-PIN counts
        raise
    db.commit()
    return out


class KioskSyncIn(BaseModel):
    items: list[KioskFaceIn_] = Field(..., max_length=200)


@router.post("/kiosk/{token}/sync")
def kiosk_sync(token: str, req: KioskSyncIn, db: Session = Depends(get_db)):
    """Face checks and PIN punches kept on the tablet while it was offline, each checked on its own."""
    k = _kiosk(db, token)
    results: list[dict[str, Any]] = []
    for item in req.items:
        try:
            with db.begin_nested():
                r = _one(db, k, item.model_dump())
            results.append({"client_id": item.client_id, **r})
        except BadRequest as exc:
            results.append({"client_id": item.client_id, "status": "error", "detail": exc.detail})
        db.commit()
        set_tenant(db, k.company_id)
    return {"results": results}
