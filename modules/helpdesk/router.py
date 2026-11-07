"""HR help desk API: settings, categories with agents, SLAs, cases and their
conversation, queues, canned responses, knowledge base and reports."""
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from core import audit
from core.db import get_db
from core.errors import BadRequest, Forbidden, NotFound
from core.pagination import PageParams, paginate
from core.security import get_current_user, require_permission
from modules.helpdesk import schemas, service
from modules.helpdesk.models import (HelpdeskAgent, HelpdeskArticle, HelpdeskCannedResponse, HelpdeskCase,
                                     HelpdeskCategory, HelpdeskMessage, HelpdeskSla)

router = APIRouter(prefix="/helpdesk", tags=["Help desk"])
MANAGE = "workplace.helpdesk.manage"


def _viewer(db: Session, user: dict) -> service.Viewer:
    return service.Viewer(db, user)


# ── Overview for the page ───────────────────────────────────────────

@router.get("/me")
def me(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """What the signed-in person can do here, the settings they need and their queue counts."""
    v = _viewer(db, user)
    cid = user["company_id"]
    conf = service.get_settings(db, cid)
    counts = {}
    for queue in (["my_requests"] + (["mine", "unassigned", "unmanaged", "escalated", "open"] if v.is_staff else [])):
        counts[queue] = service.queue_query(db, v, queue).order_by(None).count()
    return {"is_staff": v.is_staff, "is_admin": v.admin, "agent_of": sorted(v.agent_of), "owner_of": sorted(v.owner_of),
            "employee_id": v.employee_id, "counts": counts,
            "settings": {k: conf[k] for k in ("rating_enabled", "rating_scale", "rating_labels", "thank_you_message",
                                              "hide_priority_from_requester", "allow_requester_reopen",
                                              "reopen_window_days", "raise_for_others")},
            "priorities": service.PRIORITIES, "statuses": service.STATUSES, "sources": service.SOURCES}


@router.get("/settings")
def get_settings(db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    return service.get_settings(db, user["company_id"])


@router.put("/settings")
def put_settings(data: dict, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    out = service.update_settings(db, user["company_id"], data)
    db.commit()
    return out


@router.get("/locations")
def locations(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    return service.location_options(db, user["company_id"])


# ── Categories ──────────────────────────────────────────────────────

def _cat_out(c: HelpdeskCategory, names: dict, locs: dict, counts: dict | None = None) -> dict:
    return {"id": c.id, "uuid": str(c.uuid), "name": c.name, "parent_id": c.parent_id, "description": c.description,
            "icon": c.icon, "assignment_mode": c.assignment_mode, "owner_employee_id": c.owner_employee_id,
            "owner_name": names.get(c.owner_employee_id), "escalate_to_employee_id": c.escalate_to_employee_id,
            "escalate_to_name": names.get(c.escalate_to_employee_id), "require_subcategory": c.require_subcategory,
            "feedback_mandatory": c.feedback_mandatory, "default_priority": c.default_priority, "active": c.active,
            "sort_order": c.sort_order, "open_cases": (counts or {}).get(c.id, 0),
            "agents": [{"employee_id": a.employee_id, "name": names.get(a.employee_id), "location_id": a.location_id,
                        "location_name": locs.get(a.location_id)} for a in c.agents]}


@router.get("/categories")
def list_categories(include_inactive: bool = False, db: Session = Depends(get_db),
                    user: dict = Depends(get_current_user)):
    cid = user["company_id"]
    v = _viewer(db, user)
    rows = service.categories(db, cid, include_inactive and v.admin)
    ids = [r.owner_employee_id for r in rows] + [r.escalate_to_employee_id for r in rows] + \
          [a.employee_id for r in rows for a in r.agents]
    names = service.names_for(db, cid, ids)
    locs = service.location_names(db, cid, [a.location_id for r in rows for a in r.agents])
    counts: dict = {}
    if v.is_staff:  # main categories count all their cases; subcategories count their own
        for col in (HelpdeskCase.category_id, HelpdeskCase.subcategory_id):
            for key, n in (db.query(col, func.count(HelpdeskCase.id))
                           .filter(HelpdeskCase.company_id == cid, HelpdeskCase.status.in_(service.ACTIVE), col.isnot(None))
                           .group_by(col).all()):
                counts[key] = counts.get(key, 0) + n
    out = [_cat_out(r, names, locs, counts) for r in rows]
    if not v.is_staff:
        for o in out:
            o.pop("agents"), o.pop("escalate_to_employee_id"), o.pop("escalate_to_name")
    return {"items": out}


@router.post("/categories", status_code=status.HTTP_201_CREATED)
def create_category(body: schemas.CategoryIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    data = body.model_dump(exclude_unset=True)
    if body.agents is not None:
        data["agents"] = [a.model_dump() for a in body.agents]
    c = service.save_category(db, user["company_id"], data)
    db.commit()
    return {"uuid": str(c.uuid), "id": c.id}


@router.put("/categories/{uuid}")
def update_category(uuid: UUID, body: schemas.CategoryIn, db: Session = Depends(get_db),
                    user: dict = Depends(require_permission(MANAGE))):
    c = service.get_category(db, user["company_id"], uuid)
    data = body.model_dump(exclude_unset=True)
    if body.agents is not None:
        data["agents"] = [a.model_dump() for a in body.agents]
    service.save_category(db, user["company_id"], data, c)
    db.commit()
    return {"uuid": str(c.uuid), "id": c.id}


# ── SLAs ────────────────────────────────────────────────────────────

def _sla_out(s: HelpdeskSla, names: dict, cats: dict) -> dict:
    return {"uuid": str(s.uuid), "category_id": s.category_id, "category_name": cats.get(s.category_id),
            "priority": s.priority, "response_minutes": s.response_minutes, "resolution_minutes": s.resolution_minutes,
            "escalate_to_employee_id": s.escalate_to_employee_id, "escalate_to_name": names.get(s.escalate_to_employee_id),
            "raise_priority": s.raise_priority, "active": s.active}


@router.get("/slas")
def list_slas(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    cid = user["company_id"]
    rows = (db.query(HelpdeskSla).filter(HelpdeskSla.company_id == cid)
            .order_by(HelpdeskSla.category_id.nullsfirst(), HelpdeskSla.priority).all())
    names = service.names_for(db, cid, [r.escalate_to_employee_id for r in rows])
    cats = {c.id: c.name for c in db.query(HelpdeskCategory).filter(HelpdeskCategory.company_id == cid)}
    order = {p: i for i, p in enumerate(reversed(service.PRIORITIES))}
    rows.sort(key=lambda r: (r.category_id or 0, order.get(r.priority, 9)))
    return {"items": [_sla_out(r, names, cats) for r in rows]}


@router.post("/slas", status_code=status.HTTP_201_CREATED)
def create_sla(body: schemas.SlaIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    s = service.save_sla(db, user["company_id"], body.model_dump())
    db.commit()
    return {"uuid": str(s.uuid)}


@router.put("/slas/{uuid}")
def update_sla(uuid: UUID, body: schemas.SlaIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    s = db.query(HelpdeskSla).filter(HelpdeskSla.company_id == user["company_id"], HelpdeskSla.uuid == uuid).first()
    if s is None:
        raise NotFound("Target")
    service.save_sla(db, user["company_id"], body.model_dump(exclude_unset=True), s)
    db.commit()
    return {"uuid": str(s.uuid)}


# ── Cases ───────────────────────────────────────────────────────────

def _case_out(db: Session, c: HelpdeskCase, v: service.Viewer, names: dict, conf: dict, locs: dict | None = None,
              type_labels: dict | None = None) -> dict:
    staff = v.handles(c)
    hide_priority = conf["hide_priority_from_requester"] and not staff
    sla = service.sla_state(c)
    return {"uuid": str(c.uuid), "reference_no": c.reference_no, "subject": c.subject,
            "category_id": c.category_id, "category_name": c.category.name if c.category else None,
            "subcategory_id": c.subcategory_id, "subcategory_name": c.subcategory.name if c.subcategory else None,
            "case_type_id": c.case_type_id, "case_type_name": (type_labels or {}).get(c.case_type_id),
            "location_id": c.location_id, "location_name": (locs or {}).get(c.location_id),
            "priority": None if hide_priority else c.priority, "status": c.status,
            "status_label": service.STATUSES.get(c.status), "source": c.source,
            "requester_employee_id": c.requester_employee_id, "requester_name": names.get(c.requester_employee_id),
            "raised_by_name": c.raised_by_name, "assignee_employee_id": c.assignee_employee_id,
            "assignee_name": names.get(c.assignee_employee_id), "created_at": c.created_at,
            "last_activity_at": c.last_activity_at, "resolved_at": c.resolved_at, "closed_at": c.closed_at,
            "first_response_at": c.first_response_at, "escalation_level": c.escalation_level,
            "reopened_count": c.reopened_count, "rating": c.rating, "rating_comment": c.rating_comment,
            "sla": sla if staff else None,
            "breached": (c.response_breached or c.resolution_breached or sla["response"]["state"] == "breached"
                         or sla["resolution"]["state"] == "breached") if staff else None,
            "can_handle": staff, "can_manage": v.manages(c), "is_requester": v.is_requester(c),
            "custom": c.custom or {}}


def _cases_page(db: Session, v: service.Viewer, query, params: PageParams) -> dict:
    page = paginate(query, params)
    cid = v.company_id
    conf = service.get_settings(db, cid)
    names = service.names_for(db, cid, [c.requester_employee_id for c in page["items"]] +
                              [c.assignee_employee_id for c in page["items"]])
    from core.masters import labels_for
    types = labels_for(db, cid, [c.case_type_id for c in page["items"] if c.case_type_id])
    page["items"] = [_case_out(db, c, v, names, conf, type_labels=types) for c in page["items"]]
    return page


@router.get("/cases")
def list_cases(queue: str = "my_requests", q: str | None = None, status_: str | None = Query(None, alias="status"),
               priority: str | None = None, category_id: int | None = None, assignee_id: int | None = None,
               breached: bool | None = None, params: PageParams = Depends(), db: Session = Depends(get_db),
               user: dict = Depends(get_current_user)):
    v = _viewer(db, user)
    query = service.queue_query(db, v, queue)
    if status_:
        query = query.filter(HelpdeskCase.status.in_(status_.split(",")))
    if priority:
        query = query.filter(HelpdeskCase.priority == priority)
    if category_id:
        query = query.filter(or_(HelpdeskCase.category_id == category_id, HelpdeskCase.subcategory_id == category_id))
    if assignee_id:
        query = query.filter(HelpdeskCase.assignee_employee_id == assignee_id)
    if breached and v.is_staff:
        now = service._now()
        query = query.filter(or_(HelpdeskCase.response_breached.is_(True), HelpdeskCase.resolution_breached.is_(True),
                                 (HelpdeskCase.status.in_(service.ACTIVE)) & (HelpdeskCase.resolution_due_at < now)))
    if q:
        like = f"%{q.strip()}%"
        from modules.employees.models import Employee
        query = query.outerjoin(Employee, Employee.id == HelpdeskCase.requester_employee_id).filter(or_(
            HelpdeskCase.subject.ilike(like), HelpdeskCase.reference_no.ilike(like), Employee.first_name.ilike(like),
            Employee.last_name.ilike(like)))
    order = HelpdeskCase.last_activity_at.desc() if queue == "my_requests" else HelpdeskCase.created_at.desc()
    return _cases_page(db, v, query.order_by(order), params)


@router.post("/cases", status_code=status.HTTP_201_CREATED)
def create_case(body: schemas.CaseIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    data = body.model_dump()
    data["attachments"] = [str(u) for u in body.attachments]
    c = service.create_case(db, user, data)
    db.commit()
    return case_detail(c.uuid, db, user)


@router.get("/cases/{uuid}")
def case_detail(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    from modules.documents import service as documents
    v = _viewer(db, user)
    c = service.visible_case(db, v, uuid)
    cid = user["company_id"]
    conf = service.get_settings(db, cid)
    staff = v.handles(c)
    msgs = db.query(HelpdeskMessage).filter(HelpdeskMessage.case_id == c.id).order_by(HelpdeskMessage.id).all()
    if not staff:
        msgs = [m for m in msgs if m.kind != "note"]
    names = service.names_for(db, cid, [c.requester_employee_id, c.assignee_employee_id] +
                              [m.author_employee_id for m in msgs])
    from core.masters import labels_for
    out = _case_out(db, c, v, names, conf, service.location_names(db, cid, [c.location_id]),
                    labels_for(db, cid, [c.case_type_id] if c.case_type_id else []))
    requester = service.employee(db, cid, c.requester_employee_id)
    out["requester"] = {"name": names.get(c.requester_employee_id), "email": requester.email if requester else None,
                        "employee_code": requester.employee_code if requester else None,
                        "profile_image_url": requester.profile_image_url if requester else None,
                        "department": requester.department.department_name if requester and requester.department else None}
    out["description"] = c.description
    out["attachments"] = documents.attachments_with_links(db, cid, c.attachments)
    out["messages"] = [{"uuid": str(m.uuid), "kind": m.kind, "body": m.body, "author_name": m.author_name,
                        "author_employee_id": m.author_employee_id,
                        "from_requester": m.author_employee_id == c.requester_employee_id and m.kind != "event",
                        "attachments": documents.attachments_with_links(db, cid, m.attachments), "created_at": m.created_at}
                       for m in msgs]
    out["allowed_statuses"] = sorted(service.TRANSITIONS.get(c.status, set())) if staff else (
        ["cancelled"] if c.status in ("new", "open") else (["closed"] if c.status == "resolved" else []))
    out["can_reopen"] = c.status in ("resolved", "closed") and (staff or conf["allow_requester_reopen"])
    out["can_rate"] = v.is_requester(c) and conf["rating_enabled"] and c.status in ("resolved", "closed") and c.rating is None
    out["can_reply"] = not (c.status in ("closed", "cancelled") and conf["block_comments_when_closed"])
    if staff:
        out["agents"] = [{"employee_id": e, "name": service.names_for(db, cid, [e]).get(e)} for e in service._agents_for(db, c)]
    return out


def _act(db: Session, user: dict, uuid: UUID):
    v = _viewer(db, user)
    return v, service.visible_case(db, v, uuid)


@router.put("/cases/{uuid}")
def update_case(uuid: UUID, body: schemas.CaseUpdate, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v, c = _act(db, user, uuid)
    service.update_case(db, v, c, body.model_dump(exclude_unset=True))
    db.commit()
    return case_detail(uuid, db, user)


@router.post("/cases/{uuid}/messages", status_code=status.HTTP_201_CREATED)
def add_message(uuid: UUID, body: schemas.MessageIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v, c = _act(db, user, uuid)
    service.add_message(db, v, c, body.kind, body.body, [str(u) for u in body.attachments])
    db.commit()
    return case_detail(uuid, db, user)


@router.post("/cases/{uuid}/status")
def set_status(uuid: UUID, body: schemas.StatusIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v, c = _act(db, user, uuid)
    service.change_status(db, v, c, body.status, body.note)
    db.commit()
    return case_detail(uuid, db, user)


@router.post("/cases/{uuid}/pick")
def pick(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v, c = _act(db, user, uuid)
    service.pick(db, v, c)
    db.commit()
    return case_detail(uuid, db, user)


@router.post("/cases/{uuid}/assign")
def assign(uuid: UUID, body: schemas.AssignIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v, c = _act(db, user, uuid)
    service.assign(db, v, c, body.employee_id)
    db.commit()
    return case_detail(uuid, db, user)


@router.post("/cases/{uuid}/reopen")
def reopen(uuid: UUID, body: schemas.ReopenIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v, c = _act(db, user, uuid)
    service.reopen(db, v, c, body.reason)
    db.commit()
    return case_detail(uuid, db, user)


@router.post("/cases/{uuid}/rate")
def rate(uuid: UUID, body: schemas.RateIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v, c = _act(db, user, uuid)
    out = service.rate(db, v, c, body.rating, body.comment)
    db.commit()
    return {**out, "case": case_detail(uuid, db, user)}


@router.post("/cases/bulk")
def bulk(body: schemas.BulkIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _viewer(db, user)
    if not v.is_staff:
        raise Forbidden("Only help desk agents can do this.")
    done, failed = 0, []
    for u in body.uuids:
        try:
            with db.begin_nested():
                c = service.visible_case(db, v, u)
                if body.action == "pick":
                    service.pick(db, v, c)
                elif body.action == "assign":
                    service.assign(db, v, c, body.employee_id)
                elif body.action == "status" and body.status:
                    service.change_status(db, v, c, body.status, body.note)
                else:
                    raise BadRequest("Unknown action.")
                done += 1
        except Exception as exc:  # report each failure, keep going
            failed.append({"uuid": str(u), "reason": getattr(exc, "detail", "Could not be changed")})
    db.commit()
    return {"done": done, "failed": failed}


@router.post("/sla/run")
def run_sla(db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    """Check targets now (also runs every 15 minutes)."""
    n = service.check_sla(db, user["company_id"])
    db.commit()
    return {"updated": n}


# ── Canned responses ────────────────────────────────────────────────

def _canned_out(r: HelpdeskCannedResponse, cats: dict) -> dict:
    return {"uuid": str(r.uuid), "title": r.title, "body": r.body, "category_id": r.category_id,
            "category_name": cats.get(r.category_id), "active": r.active, "created_by_name": r.created_by_name,
            "updated_at": r.updated_at}


@router.get("/canned")
def list_canned(category_id: int | None = None, q: str | None = None, db: Session = Depends(get_db),
                user: dict = Depends(get_current_user)):
    v = _viewer(db, user)
    if not v.is_staff:
        raise Forbidden("Only help desk agents can see saved replies.")
    cid = user["company_id"]
    query = db.query(HelpdeskCannedResponse).filter(HelpdeskCannedResponse.company_id == cid)
    if category_id:
        query = query.filter(or_(HelpdeskCannedResponse.category_id == category_id,
                                 HelpdeskCannedResponse.category_id.is_(None)))
    if q:
        query = query.filter(or_(HelpdeskCannedResponse.title.ilike(f"%{q}%"), HelpdeskCannedResponse.body.ilike(f"%{q}%")))
    cats = {c.id: c.name for c in db.query(HelpdeskCategory).filter(HelpdeskCategory.company_id == cid)}
    return {"items": [_canned_out(r, cats) for r in query.order_by(HelpdeskCannedResponse.title)]}


@router.post("/canned", status_code=status.HTTP_201_CREATED)
def create_canned(body: schemas.CannedIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    r = service.save_canned(db, user["company_id"], body.model_dump())
    db.commit()
    return _canned_out(r, {})


@router.put("/canned/{uuid}")
def update_canned(uuid: UUID, body: schemas.CannedIn, db: Session = Depends(get_db),
                  user: dict = Depends(require_permission(MANAGE))):
    r = db.query(HelpdeskCannedResponse).filter(HelpdeskCannedResponse.company_id == user["company_id"],
                                                HelpdeskCannedResponse.uuid == uuid).first()
    if r is None:
        raise NotFound("Saved reply")
    service.save_canned(db, user["company_id"], body.model_dump(), r)
    db.commit()
    return _canned_out(r, {})


# ── Knowledge base ──────────────────────────────────────────────────

def _article_out(r: HelpdeskArticle, cats: dict, full: bool = True) -> dict:
    return {"uuid": str(r.uuid), "title": r.title, "body": r.body if full else r.body[:220],
            "category_id": r.category_id, "category_name": cats.get(r.category_id), "tags": r.tags or [],
            "published": r.published, "views": r.views, "helpful_yes": r.helpful_yes, "helpful_no": r.helpful_no,
            "author_name": r.author_name, "updated_at": r.updated_at}


@router.get("/articles")
def list_articles(q: str | None = None, category_id: int | None = None, all: bool = False,
                  limit: int = Query(30, ge=1, le=100), db: Session = Depends(get_db),
                  user: dict = Depends(get_current_user)):
    cid = user["company_id"]
    admin = service.has_perm(db, user, MANAGE)
    rows = service.search_articles(db, cid, q, category_id, published_only=not (all and admin), limit=limit)
    cats = {c.id: c.name for c in db.query(HelpdeskCategory).filter(HelpdeskCategory.company_id == cid)}
    return {"items": [_article_out(r, cats, full=False) for r in rows]}


@router.get("/articles/{uuid}")
def get_article(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    cid = user["company_id"]
    r = db.query(HelpdeskArticle).filter(HelpdeskArticle.company_id == cid, HelpdeskArticle.uuid == uuid).first()
    if r is None or (not r.published and not service.has_perm(db, user, MANAGE)):
        raise NotFound("Article")
    r.views += 1
    db.commit()
    cats = {c.id: c.name for c in db.query(HelpdeskCategory).filter(HelpdeskCategory.company_id == cid)}
    return _article_out(r, cats)


@router.post("/articles/{uuid}/helpful")
def helpful(uuid: UUID, body: schemas.HelpfulIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    r = db.query(HelpdeskArticle).filter(HelpdeskArticle.company_id == user["company_id"], HelpdeskArticle.uuid == uuid).first()
    if r is None:
        raise NotFound("Article")
    if body.helpful:
        r.helpful_yes += 1
    else:
        r.helpful_no += 1
    db.commit()
    return {"helpful_yes": r.helpful_yes, "helpful_no": r.helpful_no}


@router.post("/articles", status_code=status.HTTP_201_CREATED)
def create_article(body: schemas.ArticleIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    r = service.save_article(db, user["company_id"], body.model_dump())
    db.commit()
    return _article_out(r, {})


@router.put("/articles/{uuid}")
def update_article(uuid: UUID, body: schemas.ArticleIn, db: Session = Depends(get_db),
                   user: dict = Depends(require_permission(MANAGE))):
    r = db.query(HelpdeskArticle).filter(HelpdeskArticle.company_id == user["company_id"], HelpdeskArticle.uuid == uuid).first()
    if r is None:
        raise NotFound("Article")
    service.save_article(db, user["company_id"], body.model_dump(), r)
    db.commit()
    return _article_out(r, {})


# ── Reports ─────────────────────────────────────────────────────────

@router.get("/reports")
def reports(start: date | None = None, end: date | None = None, db: Session = Depends(get_db),
            user: dict = Depends(get_current_user)):
    v = _viewer(db, user)
    if not (v.admin or v.owner_of):
        raise Forbidden("Only help desk administrators and category owners can see reports.")
    end = end or date.today()
    start = start or end - timedelta(days=89)
    s = datetime.combine(start, time.min, tzinfo=timezone.utc)
    e = datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc)
    out = service.report(db, user["company_id"], s, e)
    out.update({"start": start, "end": end})
    return out
