"""Staff API for private messages with families and section broadcasts."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.crud import get_or_404
from core.db import get_db
from core.pagination import PageParams, paginate
from core.security import get_current_user, require_permission
from modules.academics import service as academics
from modules.academics.models import Section
from modules.comms import messaging, service
from modules.comms.models import Broadcast, Thread

router = APIRouter()


def staff_user(user: dict = Depends(get_current_user)) -> dict:
    """Any staff sign-in (not parents or students)."""
    if user.get("portal_id"):
        raise HTTPException(status_code=403, detail="This page is for staff.")
    return user


class StartIn(BaseModel):
    student_id: int
    guardian_id: int | None = None  # empty = write to the learner
    subject: str = Field(..., min_length=1, max_length=200)
    body: str = Field("", max_length=5000)
    attachments: list[str] = []


class PostIn(BaseModel):
    body: str = Field("", max_length=5000)
    attachments: list[str] = []


class StatusIn(BaseModel):
    status: str


class BroadcastIn(BaseModel):
    section_id: int
    subject: str = Field(..., min_length=1, max_length=200)
    body: str = Field(..., min_length=1, max_length=5000)
    attachments: list[str] = []


@router.get("/messages/threads")
def threads(box: str = "mine", status: str | None = None, q: str | None = None, params: PageParams = Depends(),
            db: Session = Depends(get_db), user: dict = Depends(staff_user)):
    query = messaging.staff_threads(db, user, box, status, q)
    return paginate(query, params, lambda t: messaging.thread_out(t, "staff"))


@router.get("/messages/boxes")
def boxes(db: Session = Depends(get_db), user: dict = Depends(staff_user)):
    """Which inboxes the signed-in person has, with unread counts."""
    from sqlalchemy import func
    cid, emp = user["company_id"], user.get("employee_id")
    mine = db.query(func.coalesce(func.sum(Thread.staff_unread), 0)).filter(
        Thread.company_id == cid, Thread.staff_employee_id == (emp or -1)).scalar()
    out = [{"key": "mine", "label": "My conversations", "unread": int(mine or 0)}]
    if service.has(db, user, "comms.messages.office"):
        office = db.query(func.coalesce(func.sum(Thread.staff_unread), 0)).filter(
            Thread.company_id == cid, Thread.to_office.is_(True)).scalar()
        out.append({"key": "office", "label": "Office inbox", "unread": int(office or 0)})
    if service.has(db, user, "comms.messages.audit"):
        out.append({"key": "all", "label": "All conversations", "unread": 0})
    can_broadcast = service.has(db, user, "comms.messages.broadcast")
    sections = []
    if can_broadcast:
        if service.has(db, user, "comms.messages.all"):
            sess = academics.current_session(db, cid)
            rows = db.query(Section).filter(Section.company_id == cid, Section.active.is_(True),
                                            Section.session_id == (sess.id if sess else -1)).all()
        else:
            ids = academics.teacher_section_ids(db, cid, emp)
            rows = db.query(Section).filter(Section.id.in_(ids or {-1})).all()
        sections = sorted(({"id": s.id, "label": academics.section_label(s)} for s in rows), key=lambda x: x["label"])
    return {"boxes": out, "can_broadcast": can_broadcast and bool(emp), "broadcast_sections": sections,
            "can_message_anyone": service.has(db, user, "comms.messages.all"), "has_profile": bool(emp),
            "office_hours": service.office_hours_state(db, cid)}


@router.get("/messages/contacts")
def contacts(q: str | None = None, section_id: int | None = None, db: Session = Depends(get_db),
             user: dict = Depends(staff_user)):
    return messaging.staff_contacts(db, user, q, section_id)


@router.post("/messages/threads", status_code=201)
def start(payload: StartIn, db: Session = Depends(get_db), user: dict = Depends(staff_user)):
    t = messaging.start_by_staff(db, user, student_id=payload.student_id, guardian_id=payload.guardian_id,
                                 subject=payload.subject, body=payload.body, attachments=payload.attachments)
    db.commit()
    return _thread_full(db, user, t, mark=False)


def _thread_full(db: Session, user: dict, t: Thread, mark: bool = True) -> dict:
    participant = (user.get("employee_id") and t.staff_employee_id == user["employee_id"]) or \
        (t.to_office and service.has(db, user, "comms.messages.office"))
    if mark and participant and t.staff_unread:
        messaging.mark_read(db, t, "staff")
        db.commit()
        db.refresh(t)
    out = messaging.thread_out(t, "staff", service.office_hours_state(db, t.company_id))
    out["messages"] = [messaging.message_out(db, m, "staff") for m in messaging.messages_of(db, t)]
    out["can_reply"] = bool(participant)
    out["read_only_reason"] = None if participant else "You are reading this conversation for safeguarding review."
    return out


@router.get("/messages/threads/{uuid}")
def get_thread(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(staff_user)):
    t = get_or_404(db, Thread, user["company_id"], uuid, "Conversation")
    messaging.staff_access(db, user, t)
    return _thread_full(db, user, t)


@router.post("/messages/threads/{uuid}/messages", status_code=201)
def reply(uuid: UUID, payload: PostIn, db: Session = Depends(get_db), user: dict = Depends(staff_user)):
    t = get_or_404(db, Thread, user["company_id"], uuid, "Conversation")
    messaging.staff_access(db, user, t, write=True)
    messaging.post(db, user, t, payload.body, payload.attachments, side="staff")
    db.commit()
    return _thread_full(db, user, t, mark=False)


@router.post("/messages/threads/{uuid}/read")
def read(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(staff_user)):
    t = get_or_404(db, Thread, user["company_id"], uuid, "Conversation")
    messaging.staff_access(db, user, t, write=True)
    messaging.mark_read(db, t, "staff")
    db.commit()
    return {"ok": True}


@router.post("/messages/threads/{uuid}/status")
def set_status(uuid: UUID, payload: StatusIn, db: Session = Depends(get_db), user: dict = Depends(staff_user)):
    t = get_or_404(db, Thread, user["company_id"], uuid, "Conversation")
    messaging.staff_access(db, user, t, write=True)
    messaging.set_status(db, user, t, payload.status)
    db.commit()
    return _thread_full(db, user, t, mark=False)


@router.post("/messages/broadcasts", status_code=201)
def send_broadcast(payload: BroadcastIn, db: Session = Depends(get_db),
                   user: dict = Depends(require_permission("comms.messages.broadcast"))):
    b = messaging.broadcast(db, user, section_id=payload.section_id, subject=payload.subject, body=payload.body,
                            attachments=payload.attachments)
    db.commit()
    return {"uuid": str(b.uuid), "families": b.recipient_count}


@router.get("/messages/broadcasts")
def list_broadcasts(params: PageParams = Depends(), db: Session = Depends(get_db),
                    user: dict = Depends(require_permission("comms.messages.broadcast"))):
    cid = user["company_id"]
    query = db.query(Broadcast).filter(Broadcast.company_id == cid)
    if not service.has(db, user, "comms.messages.audit"):
        query = query.filter(Broadcast.sender_employee_id == (user.get("employee_id") or -1))
    secs = {s.id: academics.section_label(s) for s in db.query(Section).filter(Section.company_id == cid).all()}
    return paginate(query.order_by(Broadcast.created_at.desc()), params, lambda b: {
        "uuid": str(b.uuid), "subject": b.subject, "body": b.body, "section_label": secs.get(b.section_id),
        "families": b.recipient_count, "sender_name": b.sender_name, "created_at": b.created_at})
