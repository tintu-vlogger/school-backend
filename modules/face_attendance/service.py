"""Face attendance rules.

How it works
- Face detection, the liveness challenge and the face templates are made in the browser
  (kiosk tablet or the employee's phone or computer) with an open-source face model that
  the app serves itself. No picture or template is sent to any outside service.
- Enrolment: 3 to 5 samples after the employee agrees to the consent notice (DPDP Act,
  2023). Only the templates are kept, encrypted per company; they are never shown again.
- A kiosk downloads the templates of the people at its location and matches faces on the
  tablet, so it works offline. Every punch it sends is checked again here against the
  stored templates with the threshold in force, before it is recorded.
- When the face is not recognised the person uses their employee ID and PIN. Repeated
  failures, PINs at a kiosk that requires the face, suspected photo or screen spoofing and
  a kiosk/server disagreement are flagged for HR review.
- Web and phone clock-in can require a selfie that matches the person's own templates.

Thresholds are cosine distances (0 = identical); scores shown are similarities (1 - distance).
"""
from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from core import audit, context
from core.errors import BadRequest, NotFound
from core.timeutil import iso_utc
from modules.attendance.models import Kiosk, Punch
from modules.face_attendance import crypto, matching
from modules.face_attendance.models import FaceEnrolment, FaceEvent, FaceKioskSetting, FaceSettings

MODEL = "human-faceres"  # @vladmandic/human face description model (1024 numbers per template)
MODES = ("off", "on", "required")
LIVENESS_LEVELS = ("off", "basic", "standard", "strict")
SNAPSHOT_MODES = ("off", "flagged", "all")
SELF_CLOCK_MODES = ("off", "locations", "everyone")
MAX_SNAPSHOT = 120_000  # characters of a data URL (about 90 KB)

# What each liveness level asks of the camera check. real = texture check that the face is
# not a printed photo or a screen (anti-spoofing model), live = liveness model score,
# challenges = blink or head turn the person is asked to do, min_face_px = face size.
LIVENESS = {
    "off": {"real": 0.0, "live": 0.0, "challenges": 0, "min_face_px": 0},
    "basic": {"real": 0.5, "live": 0.5, "challenges": 0, "min_face_px": 90},
    "standard": {"real": 0.5, "live": 0.5, "challenges": 1, "min_face_px": 110},
    "strict": {"real": 0.7, "live": 0.7, "challenges": 2, "min_face_px": 130},
}

CONSENT_TEXT = (
    "Face attendance: your consent\n\n"
    "We would like to use your face to mark your attendance. If you agree:\n"
    "- This device takes 3 to 5 pictures of your face and turns them into face templates (a set of numbers). "
    "The pictures themselves are not kept.\n"
    "- The templates are stored encrypted and are used only to recognise you when you clock in at our "
    "attendance kiosks, or when you clock in on the web or your phone if the company asks for a selfie check.\n"
    "- When you punch, a small photo may be kept for a limited number of days so HR can check a doubtful punch. "
    "It is then deleted.\n"
    "- Your face data is not shared with anyone outside the company and is not used for anything else.\n"
    "- Saying no is fine: you can keep using your employee ID and PIN, and it will not affect your job.\n"
    "- You can withdraw your consent at any time in My attendance. Your face templates are then deleted at once.\n"
    "- Your templates are also deleted when you leave the company.\n\n"
    "This notice is given under the Digital Personal Data Protection Act, 2023. For questions or complaints, "
    "contact HR or the company's grievance officer."
)

DEFAULTS: dict = {
    "enabled": False,
    "threshold": 0.4,  # cosine distance limit (similarity at least 0.6)
    "margin": 0.04,  # best person must beat the next by this much
    "liveness": "standard",
    "snapshot": "flagged",
    "snapshot_retention_days": 30,
    "flag_after_failures": 3,  # failed face checks before a PIN punch is flagged
    "frequent_fallback_count": 3,  # PIN instead of face this many times in 7 days is flagged
    "refresh_minutes": 30,  # how often kiosks download templates again
    "location_ids": [],  # face check allowed at these locations' kiosks ([] = all)
    "require_location_ids": [],  # face required at these locations' kiosks
    "self_clock": "off",  # selfie match for web and phone clock-in: off | locations | everyone
    "self_clock_location_ids": [],
    "unenrolled_web": "allow",  # people not enrolled may clock in on the web without a face check
    "allow_self_enrol": True,
    "consent_version": 1,
    "consent_text": CONSENT_TEXT,
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _name(e) -> str:
    return " ".join(x for x in (e.first_name, e.last_name) if x) if e else ""


# ── Settings ────────────────────────────────────────────────────────

def _row(db: Session, company_id: int) -> FaceSettings | None:
    return db.query(FaceSettings).filter(FaceSettings.company_id == company_id).first()


def settings(db: Session, company_id: int) -> dict:
    row = _row(db, company_id)
    out = copy.deepcopy(DEFAULTS)
    out.update({k: v for k, v in ((row.config if row else None) or {}).items() if k in DEFAULTS})
    return out


def ensure_settings(db: Session, company_id: int) -> None:
    if _row(db, company_id) is None:
        db.add(FaceSettings(company_id=company_id, config=copy.deepcopy(DEFAULTS), updated_by_name="System"))
        db.flush()


def _int_ids(v) -> list[int]:
    try:
        return sorted({int(x) for x in (v or [])})
    except (TypeError, ValueError):
        raise BadRequest("Choose locations from the list.")


def save_settings(db: Session, company_id: int, data: dict) -> dict:
    cur = settings(db, company_id)
    new = copy.deepcopy(cur)
    for k, v in (data or {}).items():
        if k in DEFAULTS and k != "consent_version" and v is not None:
            new[k] = v
    new["enabled"] = bool(new["enabled"])
    new["allow_self_enrol"] = bool(new["allow_self_enrol"])
    try:
        new["threshold"] = round(float(new["threshold"]), 3)
        new["margin"] = round(float(new["margin"]), 3)
        for k in ("snapshot_retention_days", "flag_after_failures", "frequent_fallback_count", "refresh_minutes"):
            new[k] = int(new[k])
    except (TypeError, ValueError):
        raise BadRequest("Enter numbers for the threshold and limits.")
    if not 0.15 <= new["threshold"] <= 0.7:
        raise BadRequest("Keep the match threshold between 0.15 (very strict) and 0.70 (very loose).")
    if not 0 <= new["margin"] <= 0.2:
        raise BadRequest("Keep the margin between 0 and 0.20.")
    if new["liveness"] not in LIVENESS_LEVELS:
        raise BadRequest("Choose a liveness level.")
    if new["snapshot"] not in SNAPSHOT_MODES:
        raise BadRequest("Choose when to keep a snapshot.")
    if new["self_clock"] not in SELF_CLOCK_MODES:
        raise BadRequest("Choose who needs a selfie match on the web.")
    if new["unenrolled_web"] not in ("allow", "block"):
        raise BadRequest("Choose what happens to people who have not enrolled.")
    if not 1 <= new["snapshot_retention_days"] <= 365:
        raise BadRequest("Keep snapshots for 1 to 365 days.")
    if not 1 <= new["flag_after_failures"] <= 10 or not 1 <= new["frequent_fallback_count"] <= 30:
        raise BadRequest("Use a failure count between 1 and 10.")
    if not 5 <= new["refresh_minutes"] <= 1440:
        raise BadRequest("Kiosks refresh every 5 minutes to 24 hours.")
    for k in ("location_ids", "require_location_ids", "self_clock_location_ids"):
        new[k] = _int_ids(new[k])
        _check_locations(db, company_id, new[k])
    text = str(new.get("consent_text") or "").strip()
    if len(text) < 80:
        raise BadRequest("The consent notice is too short. Say what is collected, why, for how long and how to withdraw.")
    new["consent_text"] = text[:6000]
    if new["consent_text"] != cur["consent_text"]:
        new["consent_version"] = int(cur["consent_version"]) + 1
    row = _row(db, company_id)
    if row is None:
        row = FaceSettings(company_id=company_id, config={})
        db.add(row)
    row.config = new
    row.updated_by_name = context.current().actor_name
    db.flush()
    changed = {k: {"old": cur[k], "new": new[k]} for k in new if new[k] != cur.get(k) and k != "consent_text"}
    if new["consent_text"] != cur["consent_text"]:
        changed["consent_text"] = {"old": f"version {cur['consent_version']}", "new": f"version {new['consent_version']}"}
    if changed:
        audit.event(db, "face.settings_changed", "face_settings", row.id, {"changes": changed})
    return new


def _check_locations(db: Session, company_id: int, ids: list[int]) -> None:
    if not ids:
        return
    from modules.organization.models import Location
    found = {i for (i,) in db.query(Location.id).filter(Location.company_id == company_id, Location.id.in_(ids)).all()}
    if set(ids) - found:
        raise BadRequest("Choose locations from the list.")


# ── Kiosks ──────────────────────────────────────────────────────────

def kiosk_setting(db: Session, k: Kiosk) -> FaceKioskSetting | None:
    return db.query(FaceKioskSetting).filter(FaceKioskSetting.company_id == k.company_id,
                                             FaceKioskSetting.kiosk_id == k.id).first()


def _mode_for(cfg: dict, ks: FaceKioskSetting | None) -> str:
    if not cfg["enabled"] or ks is None or ks.mode == "off":
        return "off"
    if cfg["location_ids"] and ks.location_id not in cfg["location_ids"]:
        return "off"
    if ks.mode == "required" or (ks.location_id and ks.location_id in cfg["require_location_ids"]):
        return "required"
    return "on"


def kiosk_mode(db: Session, k: Kiosk) -> str:
    """off, on (face first, PIN any time) or required (PIN only after the face check fails)."""
    return _mode_for(settings(db, k.company_id), kiosk_setting(db, k))


def kiosk_config(db: Session, k: Kiosk) -> dict:
    cfg = settings(db, k.company_id)
    ks = kiosk_setting(db, k)
    mode = _mode_for(cfg, ks)
    level = (ks.liveness if ks and ks.liveness else None) or cfg["liveness"]
    return {
        "mode": mode, "model": MODEL,
        "threshold": float(ks.threshold) if ks and ks.threshold is not None else cfg["threshold"],
        "margin": cfg["margin"], "liveness": level, "liveness_rules": LIVENESS[level],
        "snapshot": cfg["snapshot"], "max_attempts": cfg["flag_after_failures"],
        "refresh_minutes": cfg["refresh_minutes"],
    }


def save_kiosk(db: Session, company_id: int, k: Kiosk, data: dict) -> FaceKioskSetting:
    mode = data.get("mode") or "off"
    if mode not in MODES:
        raise BadRequest("Choose off, on or required.")
    loc = data.get("location_id")
    if loc is not None:
        _check_locations(db, company_id, [int(loc)])
    thr = data.get("threshold")
    if thr is not None and not 0.15 <= float(thr) <= 0.7:
        raise BadRequest("Keep the match threshold between 0.15 and 0.70.")
    lv = data.get("liveness") or None
    if lv is not None and lv not in LIVENESS_LEVELS:
        raise BadRequest("Choose a liveness level.")
    ks = kiosk_setting(db, k)
    if ks is None:
        ks = FaceKioskSetting(company_id=company_id, kiosk_id=k.id)
        db.add(ks)
    ks.mode, ks.location_id = mode, (int(loc) if loc is not None else None)
    ks.threshold = float(thr) if thr is not None else None
    ks.liveness = lv
    db.flush()
    return ks


def kiosks(db: Session, company_id: int) -> list[dict]:
    from modules.organization.models import Location
    cfg = settings(db, company_id)
    rows = db.query(Kiosk).filter(Kiosk.company_id == company_id).order_by(Kiosk.name).all()
    sets = {s.kiosk_id: s for s in db.query(FaceKioskSetting).filter(FaceKioskSetting.company_id == company_id).all()}
    locs = {l.id: l.name for l in db.query(Location).filter(Location.company_id == company_id).all()}
    out = []
    for k in rows:
        ks = sets.get(k.id)
        out.append({"uuid": str(k.uuid), "name": k.name, "active": k.active, "location_note": k.location_note,
                    "mode": ks.mode if ks else "off", "effective_mode": _mode_for(cfg, ks),
                    "location_id": ks.location_id if ks else None,
                    "location_name": locs.get(ks.location_id) if ks else None,
                    "threshold": ks.threshold if ks else None, "liveness": ks.liveness if ks else None,
                    "people": _gallery_query(db, company_id, ks.location_id if ks else None).count()})
    return out


def _gallery_query(db: Session, company_id: int, location_id: int | None):
    from modules.employees.models import Employee
    q = (db.query(FaceEnrolment, Employee).join(Employee, Employee.id == FaceEnrolment.employee_id)
         .filter(FaceEnrolment.company_id == company_id, FaceEnrolment.status == "active",
                 FaceEnrolment.templates.isnot(None), Employee.active.is_(True)))
    if location_id:
        q = q.filter(Employee.location_id == location_id)
    return q


def gallery(db: Session, k: Kiosk, since: str | None = None) -> dict:
    """The templates a kiosk keeps for matching: active enrolments of active people at the
    kiosk's location (every location when the kiosk has none)."""
    conf = kiosk_config(db, k)
    if conf["mode"] == "off":
        return {"version": "off", "items": [], "config": conf}
    ks = kiosk_setting(db, k)
    rows = _gallery_query(db, k.company_id, ks.location_id if ks else None).order_by(FaceEnrolment.id).all()
    h = hashlib.sha256()
    for enr, emp in rows:
        h.update(f"{enr.uuid}:{enr.updated_at.isoformat()}:{emp.id}:{emp.employee_code};".encode())
    h.update(repr(sorted(conf.items())).encode())
    version = h.hexdigest()[:24]
    if since and since == version:
        return {"version": version, "unchanged": True, "config": conf}
    items = [{"employee_uuid": str(emp.uuid), "name": _name(emp), "first_name": emp.first_name,
              "employee_code": emp.employee_code, "dims": enr.dims,
              "templates": crypto.open_(k.company_id, enr.templates)} for enr, emp in rows]
    audit.event(db, "face.templates_downloaded", "attendance_kiosks", k.id,
                {"kiosk": k.name, "people": len(items)}, entity_uuid=str(k.uuid))
    return {"version": version, "items": [i for i in items if i["templates"]], "config": conf,
            "refreshed_at": iso_utc(_now())}


# ── Enrolment ───────────────────────────────────────────────────────

def active_enrolment(db: Session, company_id: int, employee_id: int) -> FaceEnrolment | None:
    return (db.query(FaceEnrolment).filter(FaceEnrolment.company_id == company_id,
                                           FaceEnrolment.employee_id == employee_id,
                                           FaceEnrolment.status == "active").order_by(FaceEnrolment.id.desc()).first())


def enrolment_out(e: FaceEnrolment | None) -> dict | None:
    if e is None:
        return None
    return {"uuid": str(e.uuid), "status": e.status, "samples": e.samples, "quality": round(e.quality or 0, 3),
            "consent_by": e.consent_by, "consent_at": iso_utc(e.consent_at), "consent_version": e.consent_version,
            "captured_by_name": e.captured_by_name, "withdrawn_at": iso_utc(e.withdrawn_at),
            "withdrawn_by_name": e.withdrawn_by_name, "withdraw_reason": e.withdraw_reason,
            "created_at": iso_utc(e.created_at)}


def enrol(db: Session, company_id: int, emp, data: dict, by: str) -> FaceEnrolment:
    cfg = settings(db, company_id)
    if not cfg["enabled"]:
        raise BadRequest("Face attendance is switched off for the company.")
    if by == "self" and not cfg["allow_self_enrol"]:
        raise BadRequest("Ask HR to set up face attendance for you.")
    if not emp.active:
        raise BadRequest("This employee is not active.")
    if data.get("consent") is not True:
        raise BadRequest("Face data is used only with consent. Read the notice and tick the box to agree.")
    if int(data.get("consent_version") or 0) != int(cfg["consent_version"]):
        raise BadRequest("The consent notice has changed. Read it again and agree to the new one.")
    if (data.get("model") or MODEL) != MODEL:
        raise BadRequest("These samples were made by a different face model. Reload the page and try again.")
    try:
        samples, quality = matching.check_samples(data.get("samples"), cfg["threshold"])
    except matching.TemplateError as exc:
        raise BadRequest(str(exc))
    # the same face must not be enrolled for two people (buddy punching)
    for other in (db.query(FaceEnrolment).filter(FaceEnrolment.company_id == company_id, FaceEnrolment.status == "active",
                                                 FaceEnrolment.employee_id != emp.id, FaceEnrolment.dims == len(samples[0]))
                  .all()):
        theirs = crypto.open_(company_id, other.templates)
        if theirs and any(matching.verify(s, theirs, cfg["threshold"])[0] for s in samples):
            audit.event(db, "face.duplicate_blocked", "face_enrolments", other.id,
                        {"employee_id": emp.id, "matches_employee_id": other.employee_id})
            raise BadRequest("This face looks like someone who is already enrolled. Ask HR to check.")
    now = _now()
    for old in db.query(FaceEnrolment).filter(FaceEnrolment.company_id == company_id, FaceEnrolment.employee_id == emp.id,
                                               FaceEnrolment.status == "active").all():
        old.status, old.templates, old.withdrawn_at = "replaced", None, now
        old.withdrawn_by_name, old.withdraw_reason = context.current().actor_name, "Enrolled again"
    row = FaceEnrolment(company_id=company_id, employee_id=emp.id, status="active", model=MODEL, dims=len(samples[0]),
                        samples=len(samples), templates=crypto.seal(company_id, samples), quality=quality,
                        consent_version=int(cfg["consent_version"]), consent_text=cfg["consent_text"],
                        consent_by=by, consent_at=now, captured_by_name=context.current().actor_name)
    db.add(row)
    db.flush()
    audit.event(db, "face.enrolled", "face_enrolments", row.id,
                {"employee_id": emp.id, "samples": len(samples), "consent_by": by,
                 "consent_version": row.consent_version, "quality": round(quality, 3)},
                entity_uuid=str(row.uuid), entity_label=_name(emp))
    return row


def withdraw(db: Session, company_id: int, emp, by: str, reason: str | None = None) -> int:
    """Consent withdrawn (or HR removes the face data): templates deleted at once, snapshots
    of this person deleted, the consent record kept without any face data."""
    now = _now()
    rows = db.query(FaceEnrolment).filter(FaceEnrolment.company_id == company_id, FaceEnrolment.employee_id == emp.id,
                                          FaceEnrolment.status == "active").all()
    for r in rows:
        r.status = "withdrawn" if by == "self" else "removed"
        r.templates = None
        r.withdrawn_at = now
        r.withdrawn_by_name = context.current().actor_name
        r.withdraw_reason = (reason or ("Consent withdrawn by the employee" if by == "self" else None))
        r.withdraw_reason = (r.withdraw_reason or "Removed by HR")[:300]
    snaps = (db.query(FaceEvent).filter(FaceEvent.company_id == company_id, FaceEvent.employee_id == emp.id,
                                        FaceEvent.snapshot.isnot(None)).update({"snapshot": None}, synchronize_session=False))
    db.flush()
    if rows:
        audit.event(db, "face.consent_withdrawn" if by == "self" else "face.data_removed", "face_enrolments", rows[0].id,
                    {"employee_id": emp.id, "templates_deleted": len(rows), "snapshots_deleted": snaps,
                     "reason": rows[0].withdraw_reason}, entity_uuid=str(rows[0].uuid), entity_label=_name(emp))
    return len(rows)


def enrolments(db: Session, company_id: int, q: str | None, status: str | None, page: int, size: int,
               location_id: int | None = None) -> dict:
    from modules.employees.models import Employee
    from modules.organization.models import Location
    act = (db.query(FaceEnrolment.employee_id).filter(FaceEnrolment.company_id == company_id,
                                                      FaceEnrolment.status == "active"))
    gone = (db.query(FaceEnrolment.employee_id).filter(FaceEnrolment.company_id == company_id,
                                                       FaceEnrolment.status.in_(("withdrawn", "removed"))))
    qry = db.query(Employee).filter(Employee.company_id == company_id, Employee.active.is_(True))
    if q:
        like = f"%{q.strip().lower()}%"
        qry = qry.filter(func.lower(func.concat(Employee.first_name, " ", Employee.last_name, " ",
                                                func.coalesce(Employee.employee_code, ""))).like(like))
    if location_id:
        qry = qry.filter(Employee.location_id == location_id)
    if status == "enrolled":
        qry = qry.filter(Employee.id.in_(act))
    elif status == "not_enrolled":
        qry = qry.filter(~Employee.id.in_(act))
    elif status == "withdrawn":
        qry = qry.filter(~Employee.id.in_(act), Employee.id.in_(gone))
    total = qry.count()
    emps = qry.order_by(Employee.first_name, Employee.last_name).offset((page - 1) * size).limit(size).all()
    ids = [e.id for e in emps]
    rows = (db.query(FaceEnrolment).filter(FaceEnrolment.company_id == company_id, FaceEnrolment.employee_id.in_(ids))
            .order_by(FaceEnrolment.id).all()) if ids else []
    latest: dict[int, FaceEnrolment] = {}
    for r in rows:
        if r.status == "active" or latest.get(r.employee_id) is None or latest[r.employee_id].status != "active":
            latest[r.employee_id] = r
    locs = {l.id: l.name for l in db.query(Location).filter(Location.company_id == company_id).all()}
    items = []
    for e in emps:
        r = latest.get(e.id)
        items.append({"employee_uuid": str(e.uuid), "employee_id": e.id, "name": _name(e),
                      "employee_code": e.employee_code, "image": e.profile_image_url if (e.profile_image_url or "").startswith("http") else None,
                      "location_name": locs.get(e.location_id),
                      "status": (r.status if r and r.status == "active" else ("withdrawn" if r and r.status in ("withdrawn", "removed") else "not_enrolled")),
                      "enrolment": enrolment_out(r)})
    return {"items": items, "total": total, "page": page, "size": size}


# ── Checks shared by kiosk and web ──────────────────────────────────

def _snapshot(cfg: dict, raw, flagged: bool) -> str | None:
    if not raw or cfg["snapshot"] == "off" or (cfg["snapshot"] == "flagged" and not flagged):
        return None
    s = str(raw)
    if not s.startswith("data:image/") or len(s) > MAX_SNAPSHOT:
        return None
    return s


def liveness_problems(level: str, data) -> list[str]:
    """What the liveness evidence from the camera check misses for this level (empty = fine)."""
    rules = LIVENESS.get(level, LIVENESS["standard"])
    if level == "off":
        return []
    d = data if isinstance(data, dict) else {}
    out = []
    if d.get("passed") is not True:
        out.append("liveness check not passed")
    try:
        if float(d.get("real") or 0) < rules["real"]:
            out.append("looks like a photo or a screen")
        if float(d.get("live") or 0) < rules["live"]:
            out.append("liveness score too low")
        if rules["min_face_px"] and float(d.get("face_px") or 0) < rules["min_face_px"]:
            out.append("face too small or too far")
    except (TypeError, ValueError):
        out.append("liveness data not valid")
    if len([c for c in (d.get("challenges") or []) if c in ("blink", "turn_left", "turn_right", "nod")]) < rules["challenges"]:
        out.append("blink or head turn not done")
    return out


def _clean_live(d) -> dict | None:
    if not isinstance(d, dict):
        return None
    out = {}
    for k in ("passed",):
        out[k] = bool(d.get(k))
    for k in ("real", "live", "face_px"):
        try:
            out[k] = round(float(d.get(k)), 3) if d.get(k) is not None else None
        except (TypeError, ValueError):
            out[k] = None
    out["challenges"] = [str(c)[:20] for c in (d.get("challenges") or [])][:5]
    return out


def _when(at, now: datetime) -> tuple[datetime, bool]:
    from modules.attendance import kiosk
    return kiosk._when(at, now)


def _seen(db: Session, company_id: int, client_id: str | None) -> FaceEvent | None:
    if not client_id:
        return None
    return db.query(FaceEvent).filter(FaceEvent.company_id == company_id, FaceEvent.client_id == client_id).first()


def _recent_failures(db: Session, company_id: int, since: datetime, *, kiosk_id: int | None = None,
                     employee_id: int | None = None, kinds=("no_match", "spoof", "selfie_fail")) -> int:
    q = db.query(func.count(FaceEvent.id)).filter(FaceEvent.company_id == company_id, FaceEvent.kind.in_(kinds),
                                                  FaceEvent.occurred_at >= since)
    if kiosk_id:
        q = q.filter(FaceEvent.kiosk_id == kiosk_id)
    if employee_id:
        q = q.filter(FaceEvent.employee_id == employee_id)
    return q.scalar() or 0


def _event(db: Session, company_id: int, kind: str, source: str, at: datetime, *, reasons: list[str] | None = None,
           **kw) -> FaceEvent:
    reasons = [r for r in (reasons or []) if r]
    ev = FaceEvent(company_id=company_id, kind=kind, source=source, occurred_at=at, flagged=bool(reasons),
                   flag_reasons=reasons, review_status="open" if reasons else None, **kw)
    db.add(ev)
    db.flush()
    return ev


REASONS = {
    "required_fallback": "PIN used at a kiosk that requires the face",
    "repeated_failures": "Face not recognised several times",
    "frequent_fallback": "Often needs the PIN instead of the face",
    "spoof_suspected": "Possible photo or screen held to the camera",
    "server_mismatch": "The kiosk matched but the server check did not agree",
    "no_match_left": "Face not recognised and no punch made",
    "not_enrolled": "Not enrolled for face attendance",
    "selfie_mismatch": "Selfie did not match several times",
}


def _employee_for_kiosk(db: Session, k: Kiosk, employee_uuid) -> tuple:
    from modules.employees.models import Employee
    ks = kiosk_setting(db, k)
    import uuid as _uuid
    try:
        key = _uuid.UUID(str(employee_uuid))
    except (TypeError, ValueError):
        key = None
    emp = (db.query(Employee).filter(Employee.company_id == k.company_id, Employee.uuid == key,
                                     Employee.active.is_(True)).first()) if key else None
    if emp is None or (ks and ks.location_id and emp.location_id != ks.location_id):
        raise BadRequest("This person is not set up for face attendance at this kiosk. Use your PIN.")
    return emp


# ── Kiosk: punch by face ────────────────────────────────────────────

def kiosk_face_punch(db: Session, k: Kiosk, data: dict) -> dict:
    from modules.attendance import kiosk as att_kiosk
    conf = kiosk_config(db, k)
    if conf["mode"] == "off":
        raise BadRequest("The face check is off at this kiosk. Use your PIN.")
    cfg = settings(db, k.company_id)
    now = _now()
    at, offline = _when(data.get("at"), now)
    if (seen := _seen(db, k.company_id, data.get("client_id"))) is not None:
        return {"status": "duplicate", "event": seen.kind}
    emp = _employee_for_kiosk(db, k, data.get("employee_uuid"))
    enr = active_enrolment(db, k.company_id, emp.id)
    samples = crypto.open_(k.company_id, enr.templates) if enr else []
    client_score = _float(data.get("score"))
    live = _clean_live(data.get("liveness"))
    common = dict(kiosk_id=k.id, employee_id=emp.id, client_score=client_score, threshold=conf["threshold"],
                  liveness=live, offline=offline, client_id=(data.get("client_id") or None),
                  attempts=_int(data.get("attempts")))
    problems = liveness_problems(conf["liveness"], data.get("liveness"))
    if problems:
        ev = _event(db, k.company_id, "spoof", "kiosk", at, reasons=["spoof_suspected"],
                    snapshot=_snapshot(cfg, data.get("snapshot"), True), **common)
        audit.event(db, "face.spoof_suspected", "face_events", ev.id, {"kiosk": k.name, "problems": problems},
                    entity_uuid=str(ev.uuid))
        return {"status": "rejected", "detail": "We could not confirm a live face. Look straight at the camera, "
                                                "or use your PIN."}
    try:
        probe = matching.clean_vector(data.get("descriptor"), enr.dims if enr else None)
    except matching.TemplateError as exc:
        raise BadRequest(str(exc))
    ok, score = matching.verify(probe, samples, conf["threshold"]) if samples else (False, 0.0)
    if not ok:
        _event(db, k.company_id, "no_match", "kiosk", at, reasons=["server_mismatch"], score=round(score, 4),
               snapshot=_snapshot(cfg, data.get("snapshot"), True), **common)
        return {"status": "rejected", "detail": "Your face was not recognised. Use your employee ID and PIN."}
    photo = data.get("snapshot") if k.photo != "off" else None
    out = att_kiosk.record(db, k, emp, {"photo": photo, "at": data.get("at")}, now, note=f"face match {score:.0%}")
    if out["status"] == "ok":
        _event(db, k.company_id, "face_punch", "kiosk", out["at"], score=round(score, 4), punch_id=out["punch_id"],
               snapshot=_snapshot(cfg, data.get("snapshot"), False), **common)
    return {k2: v for k2, v in out.items() if k2 not in ("punch_id", "at", "employee_id")} | {"score": round(score, 3)}


def kiosk_pin_fallback(db: Session, k: Kiosk, data: dict) -> dict:
    """Employee ID and PIN after the face check (failed, or the person is not enrolled)."""
    from modules.attendance import kiosk as att_kiosk
    conf = kiosk_config(db, k)
    cfg = settings(db, k.company_id)
    if (seen := _seen(db, k.company_id, data.get("client_id"))) is not None:
        return {"status": "duplicate", "event": seen.kind}
    attempts = _int(data.get("attempts")) or 0
    out = att_kiosk.punch(db, k, {**data, "_face_fallback": True,
                                  "_note": "PIN after face check" if attempts else "PIN"})
    eid = out["employee_id"]
    if out["status"] != "ok" or conf["mode"] == "off":
        return _public(out)
    reasons = []
    enrolled = active_enrolment(db, k.company_id, eid) is not None
    if conf["mode"] == "required":
        reasons.append("required_fallback")
    if attempts >= cfg["flag_after_failures"]:
        reasons.append("repeated_failures")
    week = _now() - timedelta(days=7)
    before = (db.query(func.count(FaceEvent.id)).filter(FaceEvent.company_id == k.company_id, FaceEvent.employee_id == eid,
                                                       FaceEvent.kind == "pin_fallback", FaceEvent.occurred_at >= week)
              .scalar() or 0)
    if enrolled and before + 1 >= cfg["frequent_fallback_count"]:
        reasons.append("frequent_fallback")
    if reasons and not enrolled:
        reasons.append("not_enrolled")
    _event(db, k.company_id, "pin_fallback", "kiosk", out["at"], reasons=reasons, kiosk_id=k.id, employee_id=eid,
           punch_id=out["punch_id"], attempts=attempts, liveness=_clean_live(data.get("liveness")),
           client_score=_float(data.get("score")), threshold=conf["threshold"], offline=out.get("offline", False),
           client_id=(data.get("client_id") or None), snapshot=_snapshot(cfg, data.get("snapshot"), bool(reasons)))
    return _public(out)


def kiosk_miss(db: Session, k: Kiosk, data: dict) -> dict:
    """The face check failed and the person left without punching, or liveness failed."""
    conf = kiosk_config(db, k)
    if conf["mode"] == "off":
        return {"status": "ignored"}
    cfg = settings(db, k.company_id)
    if _seen(db, k.company_id, data.get("client_id")) is not None:
        return {"status": "duplicate"}
    now = _now()
    at, offline = _when(data.get("at"), now)
    spoof = data.get("reason") == "liveness"
    attempts = _int(data.get("attempts")) or 1
    recent = _recent_failures(db, k.company_id, at - timedelta(minutes=10), kiosk_id=k.id)
    reasons = ["spoof_suspected"] if spoof else []
    if not spoof and (attempts >= cfg["flag_after_failures"] or recent + 1 >= cfg["flag_after_failures"]):
        reasons.append("no_match_left")
    _event(db, k.company_id, "spoof" if spoof else "no_match", "kiosk", at, reasons=reasons, kiosk_id=k.id,
           attempts=attempts, client_score=_float(data.get("score")), threshold=conf["threshold"],
           liveness=_clean_live(data.get("liveness")), offline=offline, client_id=(data.get("client_id") or None),
           snapshot=_snapshot(cfg, data.get("snapshot"), bool(reasons)))
    return {"status": "logged", "flagged": bool(reasons)}


def _public(out: dict) -> dict:
    return {k: v for k, v in out.items() if k not in ("punch_id", "at", "employee_id")}


def _float(v) -> float | None:
    try:
        return round(float(v), 4) if v is not None else None
    except (TypeError, ValueError):
        return None


def _int(v) -> int | None:
    try:
        return max(0, min(50, int(v))) if v is not None else None
    except (TypeError, ValueError):
        return None


# ── Web and phone clock-in with a selfie match ──────────────────────

def self_clock_required(db: Session, company_id: int, employee_id: int) -> bool:
    cfg = settings(db, company_id)
    if not cfg["enabled"] or cfg["self_clock"] == "off":
        return False
    if cfg["self_clock"] == "locations":
        from modules.employees.models import Employee
        loc = db.query(Employee.location_id).filter(Employee.company_id == company_id, Employee.id == employee_id).scalar()
        if not loc or loc not in cfg["self_clock_location_ids"]:
            return False
    if active_enrolment(db, company_id, employee_id) is None and cfg["unenrolled_web"] == "allow":
        return False
    return True


def self_status(db: Session, company_id: int, emp) -> dict:
    cfg = settings(db, company_id)
    enr = active_enrolment(db, company_id, emp.id)
    level = cfg["liveness"]
    return {"enabled": cfg["enabled"], "allow_self_enrol": cfg["allow_self_enrol"], "enrolled": enr is not None,
            "enrolment": enrolment_out(enr), "consent_text": cfg["consent_text"],
            "consent_version": cfg["consent_version"], "model": MODEL, "threshold": cfg["threshold"],
            "liveness": level, "liveness_rules": LIVENESS[level],
            "self_clock_required": self_clock_required(db, company_id, emp.id),
            "unenrolled_blocked": cfg["self_clock"] != "off" and cfg["unenrolled_web"] == "block"}


def self_clock_check(db: Session, company_id: int, emp, data: dict) -> tuple[bool, float, str | None]:
    """1:1 check of a selfie template against the person's own. Records the outcome; a run
    of failures is flagged for HR. Returns (matched, score, message when not matched)."""
    cfg = settings(db, company_id)
    enr = active_enrolment(db, company_id, emp.id)
    if enr is None:
        raise BadRequest("Set up face sign-in first: My attendance, Face sign-in.")
    now = _now()
    live = _clean_live(data.get("liveness"))
    problems = liveness_problems(cfg["liveness"], data.get("liveness"))
    try:
        probe = matching.clean_vector(data.get("descriptor"), enr.dims)
    except matching.TemplateError as exc:
        raise BadRequest(str(exc))
    ok, score = matching.verify(probe, crypto.open_(company_id, enr.templates), cfg["threshold"])
    if ok and not problems:
        return True, score, None
    recent = _recent_failures(db, company_id, now - timedelta(minutes=30), employee_id=emp.id, kinds=("selfie_fail",))
    reasons = ["spoof_suspected"] if problems else []
    if recent + 1 >= cfg["flag_after_failures"]:
        reasons.append("selfie_mismatch")
    _event(db, company_id, "selfie_fail", "web", now, reasons=reasons, employee_id=emp.id, score=round(score, 4),
           threshold=cfg["threshold"], liveness=live, snapshot=_snapshot(cfg, data.get("snapshot") or data.get("selfie"), bool(reasons)))
    msg = ("We could not confirm a live face. Hold the phone at eye level in good light and follow the prompt."
           if problems else "Your selfie did not match your enrolled face. Try again in good light, or ask HR.")
    return False, score, msg


def record_self_match(db: Session, company_id: int, emp, punch, score: float, data: dict) -> None:
    cfg = settings(db, company_id)
    _event(db, company_id, "selfie_match", "web", punch.punched_at, employee_id=emp.id, punch_id=punch.id,
           score=round(score, 4), threshold=cfg["threshold"], liveness=_clean_live(data.get("liveness")),
           snapshot=_snapshot(cfg, data.get("snapshot") or data.get("selfie"), False))


# ── Review ──────────────────────────────────────────────────────────

KIND_LABELS = {"face_punch": "Punch by face", "pin_fallback": "PIN instead of face", "no_match": "Face not recognised",
               "spoof": "Liveness failed", "selfie_match": "Selfie matched", "selfie_fail": "Selfie did not match"}


def event_out(ev: FaceEvent, names: dict, kiosks_: dict, punches: dict) -> dict:
    p = punches.get(ev.punch_id)
    return {"uuid": str(ev.uuid), "kind": ev.kind, "kind_label": KIND_LABELS.get(ev.kind, ev.kind), "source": ev.source,
            "kiosk_name": kiosks_.get(ev.kiosk_id), "employee": names.get(ev.employee_id),
            "occurred_at": iso_utc(ev.occurred_at), "score": ev.score, "client_score": ev.client_score,
            "threshold": ev.threshold, "attempts": ev.attempts, "liveness": ev.liveness, "offline": ev.offline,
            "has_snapshot": bool(ev.snapshot), "flagged": ev.flagged,
            "reasons": [{"code": r, "label": REASONS.get(r, r)} for r in (ev.flag_reasons or [])],
            "review_status": ev.review_status, "reviewed_by_name": ev.reviewed_by_name,
            "reviewed_at": iso_utc(ev.reviewed_at), "review_note": ev.review_note,
            "punch": {"uuid": str(p.uuid), "punched_at": iso_utc(p.punched_at), "ignored": p.ignored} if p else None}


def events(db: Session, company_id: int, *, status: str | None, kind: str | None, employee_id: int | None,
           start: datetime | None, end: datetime | None, page: int, size: int) -> dict:
    from modules.employees.models import Employee
    q = db.query(FaceEvent).filter(FaceEvent.company_id == company_id)
    if status == "open":
        q = q.filter(FaceEvent.flagged.is_(True), FaceEvent.review_status == "open")
    elif status == "flagged":
        q = q.filter(FaceEvent.flagged.is_(True))
    elif status == "reviewed":
        q = q.filter(FaceEvent.review_status.in_(("accepted", "rejected")))
    if kind:
        q = q.filter(FaceEvent.kind == kind)
    if employee_id:
        q = q.filter(FaceEvent.employee_id == employee_id)
    if start:
        q = q.filter(FaceEvent.occurred_at >= start)
    if end:
        q = q.filter(FaceEvent.occurred_at < end)
    total = q.count()
    rows = q.order_by(FaceEvent.occurred_at.desc(), FaceEvent.id.desc()).offset((page - 1) * size).limit(size).all()
    eids = {r.employee_id for r in rows if r.employee_id}
    names = {e.id: {"uuid": str(e.uuid), "name": _name(e), "employee_code": e.employee_code}
             for e in db.query(Employee).filter(Employee.company_id == company_id, Employee.id.in_(eids)).all()} if eids else {}
    kios = {k.id: k.name for k in db.query(Kiosk).filter(Kiosk.company_id == company_id).all()}
    pids = [r.punch_id for r in rows if r.punch_id]
    punches = {p.id: p for p in db.query(Punch).filter(Punch.company_id == company_id, Punch.id.in_(pids)).all()} if pids else {}
    return {"items": [event_out(r, names, kios, punches) for r in rows], "total": total, "page": page, "size": size}


def get_event(db: Session, company_id: int, uuid) -> FaceEvent:
    ev = db.query(FaceEvent).filter(FaceEvent.company_id == company_id, FaceEvent.uuid == uuid).first()
    if ev is None:
        raise NotFound("Face check")
    return ev


def review(db: Session, company_id: int, ev: FaceEvent, decision: str, note: str | None, add_punch: bool = False) -> FaceEvent:
    """HR decision on a flagged face check. Reject removes the punch from attendance (it is
    kept, marked ignored); accept can add the punch for a check that did not make one."""
    from modules.attendance import service as att
    if not ev.flagged:
        raise BadRequest("Only flagged checks need a review.")
    if ev.review_status != "open":
        raise BadRequest("This check has already been reviewed.")
    if decision not in ("accept", "reject"):
        raise BadRequest("Choose accept or reject.")
    note = (note or "").strip() or None
    if decision == "reject" and not note:
        raise BadRequest("Say why the punch is rejected.")
    punch = db.query(Punch).filter(Punch.company_id == company_id, Punch.id == ev.punch_id).first() if ev.punch_id else None
    tz = att.company_tz(db, company_id)
    if decision == "reject" and punch is not None and not punch.ignored:
        if att.is_locked(db, company_id, punch.punched_at.astimezone(tz).date()):
            raise BadRequest("This punch is in a locked pay period. Unlock it in attendance first.")
        punch.ignored = True
        punch.note = (f"{punch.note or ''} · rejected on face review").strip(" ·")[:300]
        db.flush()
        att.recompute_for_punch_times(db, company_id, [(punch.employee_id, punch.punched_at)])
    if decision == "accept" and add_punch and punch is None:
        if not ev.employee_id:
            raise BadRequest("This check has no person to add a punch for.")
        if att.is_locked(db, company_id, ev.occurred_at.astimezone(tz).date()):
            raise BadRequest("This day is in a locked pay period.")
        punch = Punch(company_id=company_id, employee_id=ev.employee_id, punched_at=ev.occurred_at,
                      source="kiosk" if ev.source == "kiosk" else "web", note="Added on face review")
        db.add(punch)
        db.flush()
        ev.punch_id = punch.id
        att.recompute_for_punch_times(db, company_id, [(punch.employee_id, punch.punched_at)])
    ev.review_status = "accepted" if decision == "accept" else "rejected"
    ev.reviewed_by_name = context.current().actor_name
    ev.reviewed_at = _now()
    ev.review_note = note[:300] if note else None
    db.flush()
    audit.event(db, f"face.review_{ev.review_status}", "face_events", ev.id,
                {"kind": ev.kind, "employee_id": ev.employee_id, "punch_id": ev.punch_id, "note": ev.review_note,
                 "added_punch": bool(add_punch and decision == "accept")}, entity_uuid=str(ev.uuid))
    return ev


def summary(db: Session, company_id: int) -> dict:
    from modules.employees.models import Employee
    cfg = settings(db, company_id)
    enrolled = (db.query(func.count(func.distinct(FaceEnrolment.employee_id)))
                .join(Employee, Employee.id == FaceEnrolment.employee_id)
                .filter(FaceEnrolment.company_id == company_id, FaceEnrolment.status == "active",
                        Employee.active.is_(True)).scalar() or 0)
    people = db.query(func.count(Employee.id)).filter(Employee.company_id == company_id, Employee.active.is_(True)).scalar() or 0
    open_ = db.query(func.count(FaceEvent.id)).filter(FaceEvent.company_id == company_id, FaceEvent.flagged.is_(True),
                                                     FaceEvent.review_status == "open").scalar() or 0
    since = _now() - timedelta(days=7)
    week = dict(db.query(FaceEvent.kind, func.count(FaceEvent.id)).filter(FaceEvent.company_id == company_id,
                                                                         FaceEvent.occurred_at >= since)
                .group_by(FaceEvent.kind).all())
    ks = kiosks(db, company_id)
    return {"enabled": cfg["enabled"], "enrolled": enrolled, "people": people, "open_flags": open_,
            "kiosks_on": sum(1 for k in ks if k["effective_mode"] != "off"), "kiosks": len(ks), "week": week}


# ── Retention (periodic job) ────────────────────────────────────────

def purge(db: Session) -> int:
    """Delete snapshots older than each company's retention period, and the templates of
    people who have left (their data is no longer needed)."""
    from modules.employees.models import Employee
    n = 0
    for (cid,) in db.query(FaceSettings.company_id).all():
        cfg = settings(db, cid)
        cutoff = _now() - timedelta(days=int(cfg["snapshot_retention_days"]))
        n += (db.query(FaceEvent).filter(FaceEvent.company_id == cid, FaceEvent.snapshot.isnot(None),
                                         FaceEvent.occurred_at < cutoff)
              .update({"snapshot": None}, synchronize_session=False))
        gone = (db.query(FaceEnrolment).join(Employee, Employee.id == FaceEnrolment.employee_id)
                .filter(FaceEnrolment.company_id == cid, FaceEnrolment.status == "active",
                        Employee.active.is_(False)).all())
        for r in gone:
            r.status, r.templates, r.withdrawn_at = "removed", None, _now()
            r.withdrawn_by_name, r.withdraw_reason = "System", "Employee left the company"
            audit.event(db, "face.data_removed", "face_enrolments", r.id,
                        {"employee_id": r.employee_id, "reason": r.withdraw_reason}, company_id=cid,
                        entity_uuid=str(r.uuid))
            n += 1
    return n
