"""Communication API (/comms): notices, messages, events calendar, parent-teacher meetings,
daily diary, settings, and the portal side under /comms/portal."""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core import audit
from core.db import get_db
from core.security import get_current_user, require_permission
from modules.comms import api_diary, api_events, api_messages, api_notices, api_portal, api_ptm
from modules.comms import channels, events, messaging, notices, service
from modules.comms.models import Notice, NoticeRecipient
from modules.documents import storage

router = APIRouter(prefix="/comms", tags=["Communication"])


def _staff(user: dict = Depends(get_current_user)) -> dict:
    if user.get("portal_id"):
        raise HTTPException(status_code=403, detail="This page is for staff.")
    return user


class SettingsIn(BaseModel):
    office_hours: dict | None = None
    parents_can_message: bool | None = None
    students_can_message: bool | None = None
    office_inbox: bool | None = None
    subject_teachers: bool | None = None
    default_channels: list[str] | None = None
    emergency_channels: list[str] | None = None
    diary_enabled: bool | None = None
    ptm_reminder_hours: int | None = None


@router.get("/settings")
def get_settings(db: Session = Depends(get_db), user: dict = Depends(_staff)):
    out = service.get_settings(db, user["company_id"])
    out["channels"] = channels.channel_status(db, user["company_id"])
    out["office_hours_state"] = service.office_hours_state(db, user["company_id"])
    return out


@router.put("/settings")
def put_settings(payload: SettingsIn, db: Session = Depends(get_db),
                 user: dict = Depends(require_permission("comms.settings.manage"))):
    data = payload.model_dump(exclude_none=True)
    out = service.save_settings(db, user["company_id"], data)
    audit.event(db, "comms.settings_changed", "comms_settings", None, {"keys": sorted(data)})
    db.commit()
    return out


@router.get("/meta")
def meta(db: Session = Depends(get_db), user: dict = Depends(_staff)):
    """Lists and switches the communication pages need."""
    from modules.academics import service as academics
    cid = user["company_id"]
    p = service.perms(db, user)
    return {
        "notice_categories": [{"value": k, "label": v} for k, v in service.NOTICE_CATEGORIES.items()],
        "event_categories": [{"value": k, "label": v} for k, v in service.EVENT_CATEGORIES.items()],
        "staff_roles": list(service.STAFF_ROLES),
        "channels": channels.channel_status(db, cid),
        "settings": service.get_settings(db, cid),
        "my_section_ids": sorted(academics.teacher_section_ids(db, cid, user.get("employee_id"))),
        "can": {k.replace("comms.", "").replace(".", "_"): k in p for k in (
            "comms.notices.manage", "comms.notices.section", "comms.messages.broadcast", "comms.messages.all",
            "comms.messages.office", "comms.messages.audit", "comms.events.manage", "comms.ptm.manage",
            "comms.diary.post", "comms.settings.manage")},
    }


@router.post("/uploads", status_code=201)
async def upload(file: UploadFile = File(...), db: Session = Depends(get_db), user: dict = Depends(_staff)):
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Files can be up to 10 MB.")
    blob = storage.store(db, user["company_id"], file.filename or "file", data, file.content_type, "upload")
    db.commit()
    return storage.describe(blob)


@router.get("/unread")
def unread(db: Session = Depends(get_db), user: dict = Depends(_staff)):
    """Badges: unread messages (own and office inbox) and unread notices."""
    key = f"e:{user.get('employee_id') or 0}"
    n = notices.received_query(db, user["company_id"], key).filter(NoticeRecipient.read_at.is_(None)).count()
    return {"messages": messaging.staff_unread(db, user), "notices": n}


@router.get("/summary")
def summary(db: Session = Depends(get_db), user: dict = Depends(_staff)):
    """Staff home card: latest notices, unread messages, today's events."""
    cid = user["company_id"]
    key = f"e:{user.get('employee_id') or 0}"
    latest = notices.received_query(db, cid, key).order_by(Notice.pinned.desc(), Notice.published_at.desc()).limit(4).all()
    today = date.today()
    todays = events.range_query(db, cid, today, today, include_cancelled=False).limit(6).all()
    upcoming = events.range_query(db, cid, today + timedelta(days=1), today + timedelta(days=14),
                                  include_cancelled=False).limit(3).all()
    return {
        "notices": [notices.notice_out(n, notices.receipt_fields(r)) for n, r in latest],
        "unread_notices": notices.received_query(db, cid, key).filter(NoticeRecipient.read_at.is_(None)).count(),
        "unread_messages": messaging.staff_unread(db, user),
        "today": [events.event_out(e) for e in todays],
        "upcoming": [events.event_out(e) for e in upcoming],
    }


for _r in (api_notices.router, api_messages.router, api_events.router, api_ptm.router, api_diary.router,
           api_portal.router):
    router.include_router(_r)
