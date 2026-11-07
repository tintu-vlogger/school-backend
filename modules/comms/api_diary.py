"""Staff API for the daily class diary: one short note per section per day."""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.db import get_db
from core.security import get_current_user
from modules.academics import service as academics
from modules.academics.models import Section
from modules.comms import service
from modules.comms.models import DiaryEntry
from modules.documents import storage

router = APIRouter()


class DiaryIn(BaseModel):
    section_id: int
    date: date
    body: str = Field(..., min_length=1, max_length=3000)
    attachments: list[str] = []


def diary_out(db: Session, e: DiaryEntry) -> dict:
    return {"id": e.id, "uuid": str(e.uuid), "section_id": e.section_id, "date": e.date, "body": e.body,
            "attachments": storage.with_links(db, e.company_id, e.attachments or []), "author_name": e.author_name,
            "updated_at": e.updated_at}


def _sections(db: Session, user: dict) -> list[Section]:
    cid = user["company_id"]
    if service.has(db, user, "comms.notices.manage"):
        sess = academics.current_session(db, cid)
        return db.query(Section).filter(Section.company_id == cid, Section.active.is_(True),
                                        Section.session_id == (sess.id if sess else -1)).all()
    if not service.has(db, user, "comms.diary.post"):
        return []
    ids = academics.teacher_section_ids(db, cid, user.get("employee_id"))
    return db.query(Section).filter(Section.id.in_(ids or {-1})).all()


@router.get("/diary/sections")
def diary_sections(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    if user.get("portal_id"):
        raise HTTPException(status_code=403, detail="This page is for staff.")
    rows = _sections(db, user)
    return sorted(({"id": s.id, "label": academics.section_label(s)} for s in rows), key=lambda x: x["label"])


@router.get("/diary")
def list_diary(section_id: int, start: date | None = None, end: date | None = None, db: Session = Depends(get_db),
               user: dict = Depends(get_current_user)):
    if section_id not in {s.id for s in _sections(db, user)}:
        raise HTTPException(status_code=403, detail="You don't have access to this.")
    q = db.query(DiaryEntry).filter(DiaryEntry.company_id == user["company_id"], DiaryEntry.section_id == section_id)
    if start:
        q = q.filter(DiaryEntry.date >= start)
    if end:
        q = q.filter(DiaryEntry.date <= end)
    return [diary_out(db, e) for e in q.order_by(DiaryEntry.date.desc()).limit(120).all()]


@router.put("/diary")
def save_diary(payload: DiaryIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    cid = user["company_id"]
    if payload.section_id not in {s.id for s in _sections(db, user)}:
        raise HTTPException(status_code=403, detail="You can write the diary only for your own sections.")
    e = db.query(DiaryEntry).filter(DiaryEntry.company_id == cid, DiaryEntry.section_id == payload.section_id,
                                    DiaryEntry.date == payload.date).first()
    new = e is None
    if new:
        e = DiaryEntry(company_id=cid, section_id=payload.section_id, date=payload.date)
        db.add(e)
    e.body = payload.body.strip()
    e.attachments = storage.attachments_from(db, cid, payload.attachments, limit=5)
    e.author_employee_id, e.author_name = user.get("employee_id"), service.actor_name(db, user)
    db.flush()
    if new and payload.date == date.today():
        from modules.notifications.service import notify_portal
        from modules.students import service as students
        ids = [s.id for s, _ in students.section_students(db, cid, payload.section_id)]
        accounts = students.portal_account_ids_for_students(db, cid, ids, parents=True, students=True)
        if accounts:
            notify_portal(db, cid, accounts, kind="comms_diary", title="Today's class diary",
                          body=e.body[:160], link="/dashboard/my/diary", actor_name=e.author_name)
    db.commit()
    return diary_out(db, e)
