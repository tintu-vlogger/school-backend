from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core.db import get_db
from core.security import get_current_user
from modules.company.models import Company
from modules.employees.models import EmployeeSettings
from modules.notifications.models import Notification
from modules.notifications import service
from modules.notifications.schemas import NotificationResponse

router = APIRouter(prefix="/notifications", tags=["Notifications"])


def _visible(query, current_user: dict):
    """Employees see messages addressed to them (and old-style broadcasts); the owner sees
    the ones addressed to the owner."""
    return service.visible(query, current_user["company_id"], current_user.get("employee_id"), current_user.get("portal_id"))


@router.get("/", response_model=list[NotificationResponse])
def list_notifications(
    channel: Optional[str] = Query(None, description="Filter by channel (e.g. whatsapp, email, push)"),
    message_type: Optional[str] = Query(None, description="Filter by message type"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = _visible(db.query(Notification), current_user)
    if channel:
        query = query.filter(Notification.channel == channel)
    if message_type:
        query = query.filter(Notification.message_type == message_type)
    rows = query.order_by(Notification.created_at.desc()).offset((page - 1) * limit).limit(limit).all()
    return [NotificationResponse.model_validate(n) for n in rows]


@router.get("/count")
def notification_count(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Notifications from the last 24 hours, for the bell badge."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    query = _visible(db.query(Notification), current_user).filter(Notification.created_at >= cutoff)
    return {"count": query.count()}


@router.get("/inbox")
def notification_inbox(
    unread: bool = Query(False),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The notification centre: newest first, with who caused each one and its link."""
    from modules.employees.models import Employee
    query = _visible(db.query(Notification), current_user)
    if unread:
        query = query.filter(Notification.read_at.is_(None))
    total = query.count()
    rows = query.order_by(Notification.created_at.desc(), Notification.id.desc()).offset((page - 1) * size).limit(size).all()
    actor_ids = {r.actor_employee_id for r in rows if r.actor_employee_id}
    photos = dict(db.query(Employee.id, Employee.profile_image_url).filter(
        Employee.company_id == current_user["company_id"], Employee.id.in_(actor_ids)).all()) if actor_ids else {}
    items = [{
        "uuid": str(r.uuid), "kind": r.message_type, "title": r.title or r.message_content,
        "body": r.message_content if r.title and r.message_content != r.title else None,
        "link": r.link or ("/dashboard/inbox" if r.message_type == "approval" else None),
        "actor_name": r.actor_name, "actor_image": photos.get(r.actor_employee_id),
        "read": r.read_at is not None, "created_at": r.created_at,
    } for r in rows]
    return {"items": items, "total": total, "page": page, "size": size,
            "unread": service.unread_count(db, current_user["company_id"], current_user.get("employee_id"), current_user.get("portal_id"))}


@router.get("/unread-count")
def notification_unread_count(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    return {"count": service.unread_count(db, current_user["company_id"], current_user.get("employee_id"), current_user.get("portal_id"))}


class _ReadRequest(BaseModel):
    uuids: Optional[list[UUID]] = None  # none = mark everything read


@router.post("/read")
def mark_notifications_read(req: _ReadRequest, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    n = service.mark_read(db, current_user["company_id"], current_user.get("employee_id"), req.uuids, current_user.get("portal_id"))
    db.commit()
    return {"marked": n, "unread": service.unread_count(db, current_user["company_id"], current_user.get("employee_id"), current_user.get("portal_id"))}


@router.post("/push-token")
def register_push_token(payload: dict, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Save the signed-in person's mobile push token (Expo or FCM)."""
    expo_token = (payload.get("expo_push_token") or "").strip()
    fcm_token = (payload.get("fcm_token") or "").strip()
    if not expo_token and not fcm_token:
        return {"saved": False, "reason": "missing_token"}

    employee_id = current_user.get("employee_id")
    if employee_id:
        settings = db.query(EmployeeSettings).filter(EmployeeSettings.employee_id == employee_id).first()
        if settings is None:
            settings = EmployeeSettings(employee_id=employee_id, config={})
            db.add(settings)
        config = dict(settings.config or {})
        if expo_token:
            config["expo_push_token"] = expo_token
        if fcm_token:
            config["fcm_token"] = fcm_token
        settings.config = config
        flag_modified(settings, "config")
    else:
        company = db.query(Company).filter(Company.id == current_user["company_id"]).first()
        if company:
            config = dict(company.config) if isinstance(company.config, dict) else {}
            if fcm_token:
                config["owner_fcm_token"] = fcm_token
            if expo_token:
                config["owner_expo_push_token"] = expo_token
            company.config = config
            flag_modified(company, "config")
    db.commit()
    return {"saved": True}


# ── Channels: my choices, company settings, email wording, delivery log ──

from core import context as _context  # noqa: E402
from core.errors import BadRequest as _BadRequest, NotFound as _NotFound  # noqa: E402
from core.pagination import PageParams as _PageParams, paginate as _paginate  # noqa: E402
from core.security import require_permission as _require_permission  # noqa: E402
from core.timeutil import iso_utc as _iso  # noqa: E402
from modules.notifications import channels as _ch  # noqa: E402
from modules.notifications.models import NotificationDelivery as _Delivery, NotificationTemplate as _Template  # noqa: E402

_MANAGE = _require_permission("settings.notifications.manage")


class _PrefItem(BaseModel):
    category: str
    email: bool = False
    push: bool = False


class _PrefsIn(BaseModel):
    items: list[_PrefItem]


@router.get("/preferences")
def my_preferences(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """The signed-in person's channels per category (in-app is always on)."""
    cid = current_user["company_id"]
    settings = _ch.company_settings(db, cid)
    return {"items": _ch.my_preferences(db, cid, current_user.get("employee_id")),
            "email_enabled": settings["email_enabled"], "push_enabled": settings["push_enabled"]}


@router.put("/preferences")
def save_my_preferences(req: _PrefsIn, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    items = [i.model_dump() for i in req.items]
    _ch.save_preferences(db, cid, current_user.get("employee_id"), items)
    if current_user.get("is_owner") and current_user.get("employee_id"):
        # the owner also gets messages addressed to "the company owner"
        _ch.save_preferences(db, cid, None, items)
    db.commit()
    return {"items": _ch.my_preferences(db, cid, current_user.get("employee_id"))}


@router.get("/settings")
def company_settings(current_user: dict = Depends(_MANAGE), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    s = _ch.company_settings(db, cid)
    email_ready = _ch.smtp_for(db, cid) is not None
    return {**s, "email_ready": email_ready,
            "categories_list": [{"key": k, "label": v[0], "description": v[1], **s["categories"][k]}
                                for k, v in _ch.CATEGORIES.items()]}


class _SettingsIn(BaseModel):
    email_enabled: Optional[bool] = None
    push_enabled: Optional[bool] = None
    categories: Optional[dict] = None
    digest: Optional[dict] = None


@router.put("/settings")
def save_company_settings(req: _SettingsIn, current_user: dict = Depends(_MANAGE), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    row = _ch.settings_row(db, cid, create=True)
    cfg = dict(row.config or {})
    data = req.model_dump(exclude_unset=True)
    for k in ("email_enabled", "push_enabled"):
        if k in data:
            cfg[k] = bool(data[k])
    if "categories" in data:
        cats = {}
        for k, v in (data["categories"] or {}).items():
            if k not in _ch.CATEGORIES:
                raise _BadRequest("Unknown kind of notification.")
            cats[k] = {"email": bool((v or {}).get("email")), "push": bool((v or {}).get("push"))}
        cfg["categories"] = {**(cfg.get("categories") or {}), **cats}
    if "digest" in data:
        d = data["digest"] or {}
        hour = d.get("hour", 8)
        try:
            hour = int(hour)
        except (TypeError, ValueError):
            raise _BadRequest("Pick the hour the daily summary goes out.")
        if hour < 0 or hour > 23:
            raise _BadRequest("Pick an hour between 0 and 23.")
        cfg["digest"] = {"manager": bool(d.get("manager", True)), "hr": bool(d.get("hr", True)), "hour": hour}
    row.config = cfg
    flag_modified(row, "config")
    row.updated_by_name = _context.current().actor_name
    db.commit()
    return company_settings(current_user, db)


@router.get("/templates")
def list_templates(current_user: dict = Depends(_MANAGE), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    out = []
    for key, base in _ch.TEMPLATES.items():
        t = _ch.template_for(db, cid, key)
        out.append({"key": key, "label": base["label"], "description": base["description"],
                    "subject": t["subject"], "body": t["body"], "custom": t["custom"],
                    "placeholders": base["placeholders"], "default_subject": base["subject"],
                    "default_body": base["body"]})
    return out


class _TemplateIn(BaseModel):
    subject: str
    body: str


@router.put("/templates/{key}")
def save_template(key: str, req: _TemplateIn, current_user: dict = Depends(_MANAGE), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    if key not in _ch.TEMPLATES:
        raise _NotFound("Email")
    subject, body = req.subject.strip(), req.body.strip()
    if not subject or not body:
        raise _BadRequest("Write a subject and a message.")
    if len(subject) > 250 or len(body) > 10000:
        raise _BadRequest("Keep the subject under 250 and the message under 10,000 characters.")
    row = db.query(_Template).filter(_Template.company_id == cid, _Template.key == key).first()
    if row is None:
        row = _Template(company_id=cid, key=key, subject=subject, body=body)
        db.add(row)
    row.subject, row.body, row.active = subject, body, True
    row.updated_by_name = _context.current().actor_name
    db.commit()
    return {"key": key, "custom": True}


@router.delete("/templates/{key}")
def reset_template(key: str, current_user: dict = Depends(_MANAGE), db: Session = Depends(get_db)):
    """Go back to the built-in wording."""
    cid = current_user["company_id"]
    row = db.query(_Template).filter(_Template.company_id == cid, _Template.key == key).first()
    if row is not None:
        db.delete(row)
        db.commit()
    return {"key": key, "custom": False}


class _PreviewIn(BaseModel):
    subject: str
    body: str


_SAMPLE = {"name": "Priya", "title": "Your leave request was approved.", "message": "Your leave request was approved.",
           "link_line": "Open it: https://yourcompany.example/dashboard", "company": "Your company",
           "requester": "Ravi Kumar", "request_title": "Leave: 3 days casual leave", "reference": "REQ-2611-0042",
           "summary": "12 Nov to 14 Nov", "approve_link": "https://yourcompany.example/approve/…",
           "reject_link": "https://yourcompany.example/approve/…", "view_link": "https://yourcompany.example/dashboard/inbox",
           "link_hours": 72, "count": 3, "items": "- Leave: 3 days (REQ-2611-0042) from Ravi Kumar, waiting 1 day(s)",
           "inbox_link": "https://yourcompany.example/dashboard/inbox", "date": "07 Nov 2026",
           "app_link": "https://yourcompany.example/dashboard"}


@router.post("/templates/{key}/preview")
def preview_template(key: str, req: _PreviewIn, current_user: dict = Depends(_MANAGE)):
    if key not in _ch.TEMPLATES:
        raise _NotFound("Email")
    return {"subject": _ch.render(req.subject, _SAMPLE), "body": _ch.render(req.body, _SAMPLE)}


def _delivery_out(d) -> dict:
    return {"uuid": str(d.uuid), "channel": d.channel, "to_address": d.to_address, "category": d.category,
            "category_label": _ch.CATEGORIES.get(d.category or "", (d.category or "",))[0], "source": d.source,
            "subject": d.subject, "status": d.status, "attempts": d.attempts, "last_error": d.last_error,
            "next_attempt_at": _iso(d.next_attempt_at), "sent_at": _iso(d.sent_at), "created_at": _iso(d.created_at)}


@router.get("/deliveries")
def list_deliveries(status: Optional[str] = None, channel: Optional[str] = None, source: Optional[str] = None,
                    q: Optional[str] = None, pages: _PageParams = Depends(), current_user: dict = Depends(_MANAGE),
                    db: Session = Depends(get_db)):
    """The delivery log: every email and push message, with its outcome and retries."""
    from sqlalchemy import or_
    query = db.query(_Delivery).filter(_Delivery.company_id == current_user["company_id"])
    if status:
        query = query.filter(_Delivery.status == status)
    if channel:
        query = query.filter(_Delivery.channel == channel)
    if source:
        query = query.filter(_Delivery.source == source)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(_Delivery.to_address.ilike(like), _Delivery.subject.ilike(like)))
    return _paginate(query.order_by(_Delivery.created_at.desc(), _Delivery.id.desc()), pages, _delivery_out)


@router.get("/deliveries/{uuid}")
def get_delivery(uuid: UUID, current_user: dict = Depends(_MANAGE), db: Session = Depends(get_db)):
    d = db.query(_Delivery).filter(_Delivery.company_id == current_user["company_id"], _Delivery.uuid == uuid).first()
    if d is None:
        raise _NotFound("Message")
    return {**_delivery_out(d), "body": d.body}


@router.post("/deliveries/{uuid}/retry")
def retry_delivery(uuid: UUID, current_user: dict = Depends(_MANAGE), db: Session = Depends(get_db)):
    d = db.query(_Delivery).filter(_Delivery.company_id == current_user["company_id"], _Delivery.uuid == uuid).first()
    if d is None:
        raise _NotFound("Message")
    if d.status in ("sent", "logged"):
        raise _BadRequest("This message was already delivered.")
    _ch.retry_now(db, d)
    db.commit()
    return _delivery_out(d)


class _TestIn(BaseModel):
    to: str


@router.post("/test-email")
def send_test_email(req: _TestIn, current_user: dict = Depends(_MANAGE), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    if "@" not in (req.to or ""):
        raise _BadRequest("Enter an email address.")
    _ch.send_email(db, cid, [req.to], "Test message from your HR system",
                   "Hello,\n\nThis is a test message. If you can read it, email notifications work.\n",
                   category="general", source="test")
    d = (db.query(_Delivery).filter(_Delivery.company_id == cid, _Delivery.source == "test")
         .order_by(_Delivery.id.desc()).first())
    db.commit()
    return _delivery_out(d)


@router.post("/digests/run")
def run_digests_now(current_user: dict = Depends(_MANAGE), db: Session = Depends(get_db)):
    """Send today's summaries now (each person still gets at most one a day)."""
    from modules.notifications import dispatch
    n = dispatch.send_digests(db, current_user["company_id"], force=True)
    db.commit()
    return {"sent": n}
