"""Tasks API: my tasks, tasks I assigned, my team's tasks and everyone's (for managers of
tasks), as a list or a board."""
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from core.db import get_db
from core.errors import BadRequest
from core.pagination import PageParams, paginate
from core.security import get_current_user
from modules.tasks import schemas, service
from modules.tasks.models import Task

router = APIRouter(prefix="/tasks", tags=["Tasks"])


def _viewer(db: Session, user: dict) -> service.Viewer:
    return service.Viewer(db, user)


def _out(db: Session, t: Task, v: service.Viewer, names: dict, detail: bool = False) -> dict:
    done = sum(1 for c in t.checklist if c.done)
    out = {"uuid": str(t.uuid), "title": t.title, "description": t.description if detail else None,
           "assignee_employee_id": t.assignee_employee_id, "assignee_name": names.get(t.assignee_employee_id),
           "created_by_employee_id": t.created_by_employee_id, "created_by_name": t.created_by_name,
           "priority": t.priority, "status": t.status, "status_label": service.STATUSES.get(t.status),
           "start_date": t.start_date, "due_date": t.due_date, "completed_at": t.completed_at, "position": t.position,
           "labels": t.labels or [], "recurrence": t.recurrence, "remind_days_before": t.remind_days_before,
           "source_module": t.source_module, "source_ref": t.source_ref,
           "checklist_total": len(t.checklist), "checklist_done": done,
           "comment_count": len(t.comments) if detail else None, "attachment_count": len(t.attachments or []),
           "created_at": t.created_at, "updated_at": t.updated_at,
           "can_edit": v.can_edit(t), "can_work": v.can_work(t)}
    if detail:
        from modules.documents import service as documents
        out["checklist"] = [{"uuid": str(c.uuid), "title": c.title, "done": c.done, "done_at": c.done_at,
                             "done_by_name": c.done_by_name} for c in t.checklist]
        out["attachments"] = documents.attachments_with_links(db, t.company_id, t.attachments)
        out["comments"] = [{"uuid": str(c.uuid), "body": c.body, "author_name": c.author_name,
                            "author_employee_id": c.author_employee_id, "created_at": c.created_at,
                            "attachments": documents.attachments_with_links(db, t.company_id, c.attachments)}
                           for c in t.comments]
    return out


@router.get("/summary")
def summary(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _viewer(db, user)
    return {**service.summary(db, v), "is_admin": v.admin, "has_team": bool(v.reports),
            "priorities": service.PRIORITIES, "statuses": service.STATUSES, "frequencies": service.FREQS}


@router.get("")
def list_tasks(scope: str = "mine", status_: str | None = Query(None, alias="status"), priority: str | None = None, due: str | None = None,
               assignee_id: int | None = None, q: str | None = None, source_module: str | None = None,
               board: bool = False, params: PageParams = Depends(), db: Session = Depends(get_db),
               user: dict = Depends(get_current_user)):
    """List (paginated) or board (every open task plus the last 30 done, ordered by position)."""
    v = _viewer(db, user)
    q_ = service.list_query(db, v, scope)
    q_ = service.apply_filters(db, q_, v.company_id, status=status_, priority=priority, due=due,
                               assignee_id=assignee_id, search=q, source_module=source_module)
    if board:
        rows = q_.filter(Task.status != "done").order_by(Task.position).limit(500).all()
        rows += (service.apply_filters(db, service.list_query(db, v, scope), v.company_id, status="done",
                                       priority=priority, assignee_id=assignee_id, search=q)
                 .order_by(Task.completed_at.desc()).limit(30).all()) if not status_ or "done" in status_ else []
        names = service.names_for(db, v.company_id, [t.assignee_employee_id for t in rows])
        return {"items": [_out(db, t, v, names) for t in rows]}
    from sqlalchemy import case, nullslast
    order = [case((Task.status.in_(("done", "cancelled")), 1), else_=0), nullslast(Task.due_date.asc()),
             Task.created_at.desc()]
    page = paginate(q_.order_by(*order), params)
    names = service.names_for(db, v.company_id, [t.assignee_employee_id for t in page["items"]])
    page["items"] = [_out(db, t, v, names) for t in page["items"]]
    return page


@router.post("", status_code=status.HTTP_201_CREATED)
def create(body: schemas.TaskIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    from modules.documents import service as documents
    v = _viewer(db, user)
    cid = user["company_id"]
    assignee = body.assignee_employee_id or v.employee_id
    t = service.create_task(db, cid, title=body.title, description=body.description, assignee_employee_id=assignee,
                            due_date=body.due_date, start_date=body.start_date, priority=body.priority,
                            checklist=body.checklist, labels=body.labels,
                            attachments=documents.attachments_from(db, cid, [str(u) for u in body.attachments]),
                            recurrence=body.recurrence.model_dump(mode="json") if body.recurrence else None,
                            remind_days_before=body.remind_days_before)
    db.commit()
    return detail(t.uuid, db, user)


@router.post("/bulk")
def bulk(body: schemas.BulkIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _viewer(db, user)
    done, failed = 0, []
    for u in body.uuids:
        try:
            with db.begin_nested():
                t = service.visible(db, v, u)
                if body.status:
                    service.set_status(db, v, t, body.status)
                if body.assignee_employee_id:
                    service.update(db, v, t, {"assignee_employee_id": body.assignee_employee_id})
                done += 1
        except Exception as exc:
            failed.append({"uuid": str(u), "reason": getattr(exc, "detail", "Could not be changed")})
    db.commit()
    return {"done": done, "failed": failed}


@router.get("/{uuid}")
def detail(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _viewer(db, user)
    t = service.visible(db, v, uuid)
    names = service.names_for(db, v.company_id, [t.assignee_employee_id])
    return _out(db, t, v, names, detail=True)


@router.put("/{uuid}")
def update(uuid: UUID, body: schemas.TaskUpdate, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _viewer(db, user)
    t = service.visible(db, v, uuid)
    data = body.model_dump(exclude_unset=True, mode="json")
    for k in ("due_date", "start_date"):
        if k in data:
            data[k] = getattr(body, k)
    service.update(db, v, t, data)
    db.commit()
    return detail(uuid, db, user)


@router.post("/{uuid}/status")
def set_status(uuid: UUID, body: schemas.StatusIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _viewer(db, user)
    t = service.visible(db, v, uuid)
    nxt = service.set_status(db, v, t, body.status)
    db.commit()
    out = detail(uuid, db, user)
    out["next_occurrence"] = {"uuid": str(nxt.uuid), "due_date": nxt.due_date} if nxt else None
    return out


@router.post("/{uuid}/move")
def move(uuid: UUID, body: schemas.MoveIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _viewer(db, user)
    t = service.visible(db, v, uuid)
    if not v.can_work(t):
        raise BadRequest("You can't move this task.")
    service.move(db, v, t, body.status, body.before_uuid, body.after_uuid)
    db.commit()
    return {"uuid": str(t.uuid), "status": t.status, "position": t.position}


@router.post("/{uuid}/checklist", status_code=status.HTTP_201_CREATED)
def add_item(uuid: UUID, body: schemas.ChecklistIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _viewer(db, user)
    service.add_checklist_item(db, v, service.visible(db, v, uuid), body.title)
    db.commit()
    return detail(uuid, db, user)


@router.put("/{uuid}/checklist/{item}")
def update_item(uuid: UUID, item: UUID, body: schemas.ChecklistUpdate, db: Session = Depends(get_db),
                user: dict = Depends(get_current_user)):
    v = _viewer(db, user)
    service.toggle_checklist_item(db, v, service.visible(db, v, uuid), item, body.done, body.title)
    db.commit()
    return detail(uuid, db, user)


@router.delete("/{uuid}/checklist/{item}")
def remove_item(uuid: UUID, item: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _viewer(db, user)
    service.remove_checklist_item(db, v, service.visible(db, v, uuid), item)
    db.commit()
    return detail(uuid, db, user)


@router.post("/{uuid}/comments", status_code=status.HTTP_201_CREATED)
def comment(uuid: UUID, body: schemas.CommentIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    from modules.documents import service as documents
    v = _viewer(db, user)
    t = service.visible(db, v, uuid)
    service.add_comment(db, v, t, body.body, documents.attachments_from(db, v.company_id, [str(u) for u in body.attachments]))
    db.commit()
    return detail(uuid, db, user)
