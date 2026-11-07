"""Staff API for notices and circulars."""
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core import audit
from core.crud import get_or_404
from core.db import get_db
from core.pagination import PageParams, paginate
from core.security import require_permission
from modules.comms import notices, service
from modules.comms.models import Notice, NoticeRecipient
from modules.documents import storage
from modules.students.models import Student

router = APIRouter()
VIEW = "comms.view"


class NoticeIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    body: str = Field("", max_length=20000)
    category: str = "general"
    attachments: list[str] = []
    audience: dict = {}
    channels: list[str] = ["in_app"]
    pinned: bool = False
    require_ack: bool = False
    publish_at: datetime | None = None
    expires_at: datetime | None = None


class NoticePatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    body: str | None = Field(None, max_length=20000)
    category: str | None = None
    attachments: list[str] | None = None
    audience: dict | None = None
    channels: list[str] | None = None
    pinned: bool | None = None
    require_ack: bool | None = None
    publish_at: datetime | None = None
    expires_at: datetime | None = None


class PublishIn(BaseModel):
    when: str = "now"  # now | schedule
    publish_at: datetime | None = None


class AudienceIn(BaseModel):
    audience: dict = {}


def _can_manage(db: Session, user: dict) -> bool:
    return service.has(db, user, "comms.notices.manage")


def _check_author(db: Session, user: dict, n: Notice) -> None:
    """Managers change any notice; teachers only their own."""
    if _can_manage(db, user):
        return
    if service.has(db, user, "comms.notices.section") and n.author_employee_id and \
            n.author_employee_id == user.get("employee_id"):
        return
    raise HTTPException(status_code=403, detail="You can change only notices you wrote.")


def _check_audience(db: Session, user: dict, audience: dict) -> None:
    if _can_manage(db, user):
        return
    if not service.has(db, user, "comms.notices.section"):
        raise HTTPException(status_code=403, detail="You don't have access to this.")
    service.check_teacher_audience(db, user["company_id"], user.get("employee_id"), audience)


def _full(db: Session, n: Notice) -> dict:
    stats = notices.stats_for(db, [n.id]).get(n.id, {"recipients": 0, "read": 0, "acknowledged": 0})
    return notices.notice_out(n, {"stats": stats}, storage.with_links(db, n.company_id, n.attachments or []))


@router.post("/audience/preview")
def preview_audience(payload: AudienceIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    """How many people an audience reaches, and its label, before sending."""
    cid = user["company_id"]
    if service.audience_empty(payload.audience):
        return {"label": "No one", "staff": 0, "parents": 0, "students": 0, "total": 0, "allowed": True}
    allowed, reason = True, None
    try:
        _check_audience(db, user, payload.audience)
    except HTTPException as exc:
        allowed, reason = False, exc.detail
    res = service.resolve_audience(db, cid, payload.audience)
    with_signin = service.portal_ids_for(db, cid, res.guardians.keys(), res.students)
    return {"label": service.audience_label(db, cid, payload.audience), "staff": len(res.staff),
            "parents": len(res.guardians), "students": len(res.students), "total": res.count,
            "portal_sign_ins": len(with_signin), "allowed": allowed, "reason": reason}


@router.get("/notices")
def list_notices(q: str | None = None, status: str | None = None, category: str | None = None,
                 mine: bool = False, params: PageParams = Depends(), db: Session = Depends(get_db),
                 user: dict = Depends(require_permission(VIEW))):
    """Every notice (people who manage notices) or the ones the signed-in teacher wrote."""
    cid = user["company_id"]
    query = db.query(Notice).filter(Notice.company_id == cid)
    if mine or not _can_manage(db, user):
        query = query.filter(Notice.author_employee_id == (user.get("employee_id") or -1))
    if status == "archived":
        query = query.filter(Notice.status == "archived")
    elif status == "expired":
        query = query.filter(Notice.status == "published", Notice.expires_at <= service.now())
    elif status:
        query = query.filter(Notice.status == status)
    else:
        query = query.filter(Notice.status != "archived")
    if category:
        query = query.filter(Notice.category.in_(category.split(",")))
    if q:
        query = query.filter(Notice.title.ilike(f"%{q.strip()}%"))
    query = query.order_by(Notice.pinned.desc(), Notice.published_at.desc().nullsfirst(), Notice.created_at.desc())
    page = paginate(query, params)
    stats = notices.stats_for(db, [n.id for n in page["items"]])
    page["items"] = [notices.notice_out(n, {"stats": stats.get(n.id, {"recipients": 0, "read": 0, "acknowledged": 0})})
                     for n in page["items"]]
    return page


@router.get("/notices/inbox")
def my_notices(q: str | None = None, category: str | None = None, unread: bool = False, include_expired: bool = False,
               params: PageParams = Depends(), db: Session = Depends(get_db),
               user: dict = Depends(require_permission(VIEW))):
    """Notices sent to the signed-in staff member."""
    cid = user["company_id"]
    key = f"e:{user.get('employee_id') or 0}"
    query = notices.received_query(db, cid, key, include_expired)
    if category:
        query = query.filter(Notice.category.in_(category.split(",")))
    if unread:
        query = query.filter(NoticeRecipient.read_at.is_(None))
    if q:
        query = query.filter(Notice.title.ilike(f"%{q.strip()}%"))
    query = query.order_by(Notice.pinned.desc(), Notice.published_at.desc())
    return paginate(query, params, lambda r: notices.notice_out(r[0], notices.receipt_fields(r[1])))


@router.post("/notices", status_code=201)
def create_notice(payload: NoticeIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    cid = user["company_id"]
    if not service.has(db, user, "comms.notices.manage", "comms.notices.section"):
        raise HTTPException(status_code=403, detail="You don't have access to this.")
    if not service.audience_empty(payload.audience):
        _check_audience(db, user, payload.audience)
    n = Notice(company_id=cid, author_employee_id=user.get("employee_id"), author_name=service.actor_name(db, user),
               status="draft")
    notices.apply_fields(db, cid, n, payload.model_dump())
    db.add(n)
    db.commit()
    db.refresh(n)
    return _full(db, n)


@router.get("/notices/{uuid}")
def get_notice(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    cid = user["company_id"]
    n = get_or_404(db, Notice, cid, uuid, "Notice")
    r = db.query(NoticeRecipient).filter(NoticeRecipient.notice_id == n.id,
                                         NoticeRecipient.recipient_key == f"e:{user.get('employee_id') or 0}").first()
    is_author = n.author_employee_id and n.author_employee_id == user.get("employee_id")
    if r is None and not is_author and not _can_manage(db, user):
        raise HTTPException(status_code=404, detail="Notice not found")
    out = _full(db, n)
    out.update(notices.receipt_fields(r))
    out["is_recipient"] = r is not None
    out["can_edit"] = bool(_can_manage(db, user) or (is_author and service.has(db, user, "comms.notices.section")))
    return out


@router.put("/notices/{uuid}")
def update_notice(uuid: UUID, payload: NoticePatch, db: Session = Depends(get_db),
                  user: dict = Depends(require_permission(VIEW))):
    cid = user["company_id"]
    n = get_or_404(db, Notice, cid, uuid, "Notice")
    _check_author(db, user, n)
    data = payload.model_dump(exclude_unset=True)
    if n.status == "published" and "audience" in data and service.clean_audience(data["audience"]) != service.clean_audience(n.audience):
        raise HTTPException(status_code=400, detail="This notice was already sent. Its audience can't change; "
                                                    "use Send again for people who haven't read it.")
    if "audience" in data:
        _check_audience(db, user, data["audience"])
    notices.apply_fields(db, cid, n, data)
    db.commit()
    db.refresh(n)
    return _full(db, n)


@router.post("/notices/{uuid}/publish")
def publish_notice(uuid: UUID, payload: PublishIn, db: Session = Depends(get_db),
                   user: dict = Depends(require_permission(VIEW))):
    cid = user["company_id"]
    n = get_or_404(db, Notice, cid, uuid, "Notice")
    _check_author(db, user, n)
    _check_audience(db, user, n.audience)
    if n.status not in ("draft", "scheduled"):
        raise HTTPException(status_code=400, detail="This notice was already sent.")
    if payload.when == "schedule":
        at = payload.publish_at or n.publish_at
        if not at or at <= service.now():
            raise HTTPException(status_code=400, detail="Pick a time in the future.")
        if service.audience_empty(n.audience):
            raise HTTPException(status_code=400, detail="Choose who should get this notice.")
        n.publish_at, n.status = at, "scheduled"
        audit.event(db, "comms.notice_scheduled", "comms_notices", n.id, {"publish_at": at.isoformat()},
                    entity_uuid=str(n.uuid), entity_label=n.title)
    else:
        n.publish_at = None
        notices.publish(db, n)
    db.commit()
    db.refresh(n)
    return _full(db, n)


@router.post("/notices/{uuid}/unschedule")
def unschedule(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    n = get_or_404(db, Notice, user["company_id"], uuid, "Notice")
    _check_author(db, user, n)
    if n.status != "scheduled":
        raise HTTPException(status_code=400, detail="This notice is not scheduled.")
    n.status = "draft"
    db.commit()
    return _full(db, n)


@router.post("/notices/{uuid}/archive")
def archive_notice(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    n = get_or_404(db, Notice, user["company_id"], uuid, "Notice")
    _check_author(db, user, n)
    n.status, n.archived_at, n.pinned = "archived", service.now(), False
    db.commit()
    return {"ok": True}


class PinIn(BaseModel):
    pinned: bool


@router.post("/notices/{uuid}/pin")
def pin_notice(uuid: UUID, payload: PinIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    n = get_or_404(db, Notice, user["company_id"], uuid, "Notice")
    _check_author(db, user, n)
    n.pinned = payload.pinned
    db.commit()
    return {"pinned": n.pinned}


@router.post("/notices/{uuid}/duplicate", status_code=201)
def duplicate_notice(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    src = get_or_404(db, Notice, user["company_id"], uuid, "Notice")
    _check_author(db, user, src)
    n = Notice(company_id=src.company_id, title=f"{src.title} (copy)"[:200], body=src.body, category=src.category,
               attachments=src.attachments, audience=src.audience, audience_label=src.audience_label,
               channels=src.channels, require_ack=src.require_ack, author_employee_id=user.get("employee_id"),
               author_name=service.actor_name(db, user), status="draft")
    db.add(n)
    db.commit()
    db.refresh(n)
    return _full(db, n)


@router.get("/notices/{uuid}/receipts")
def receipts(uuid: UUID, status: str | None = None, kind: str | None = None, q: str | None = None,
             params: PageParams = Depends(), db: Session = Depends(get_db),
             user: dict = Depends(require_permission(VIEW))):
    """Who got the notice, who read it and who acknowledged it."""
    n = get_or_404(db, Notice, user["company_id"], uuid, "Notice")
    _check_author(db, user, n)
    page = paginate(notices.receipts_query(db, n, status, kind, q), params)
    kids = {sid for r in page["items"] for sid in (r.via_student_ids or [])}
    names = {s.id: s.full_name for s in db.query(Student).filter(Student.id.in_(list(kids) or [-1])).all()}
    page["items"] = [notices.recipient_out(r, names) for r in page["items"]]
    page["stats"] = notices.stats_for(db, [n.id]).get(n.id, {"recipients": 0, "read": 0, "acknowledged": 0})
    return page


@router.post("/notices/{uuid}/resend")
def resend(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    """Send again to everyone who hasn't read it (or hasn't acknowledged, when required)."""
    n = get_or_404(db, Notice, user["company_id"], uuid, "Notice")
    _check_author(db, user, n)
    count = notices.resend_unread(db, n)
    db.commit()
    return {"sent": count}


@router.post("/notices/{uuid}/read")
def read_notice(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    n = get_or_404(db, Notice, user["company_id"], uuid, "Notice")
    r = notices.mark_read(db, user["company_id"], n, f"e:{user.get('employee_id') or 0}")
    db.commit()
    return notices.receipt_fields(r)


@router.post("/notices/{uuid}/acknowledge")
def ack_notice(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    n = get_or_404(db, Notice, user["company_id"], uuid, "Notice")
    r = notices.acknowledge(db, user["company_id"], n, f"e:{user.get('employee_id') or 0}")
    db.commit()
    return notices.receipt_fields(r)


@router.get("/notices-categories")
def categories(_: dict = Depends(require_permission(VIEW))):
    return [{"value": k, "label": v} for k, v in service.NOTICE_CATEGORIES.items()]


