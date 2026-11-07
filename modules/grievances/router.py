"""Grievances and POSH complaints API, plus public follow-up with a tracking code."""
from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from core.db import bypass_rls, get_db, set_tenant
from core.errors import BadRequest, Forbidden, NotFound
from core.pagination import PageParams, paginate
from core.security import get_current_user, require_permission
from modules.grievances import schemas, service
from modules.grievances.models import Grievance, GrievanceCommittee, GrievanceMessage, PoshProgramme

router = APIRouter(prefix="/grievances", tags=["Grievances and POSH"])
public_router = APIRouter(prefix="/public/grievances", tags=["Grievances and POSH"])
MANAGE = "workplace.grievances.manage"


def _viewer(db: Session, user: dict) -> service.Viewer:
    return service.Viewer(db, user)


@router.get("/me")
def me(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _viewer(db, user)
    conf = v.settings
    return {"sees_posh": v.sees_posh, "sees_grievances": v.sees_grievances, "is_manager": v.manager,
            "is_ic_member": v.ic_member, "is_grc_member": v.grc_member, "is_owner": v.is_owner,
            "can_configure": v.manager or v.is_owner,
            "settings": {k: conf[k] for k in ("allow_anonymous", "posh_enabled", "posh_filing_months")},
            "statuses": service.STATUSES, "outcomes": service.OUTCOMES, "roles": service.ROLES}


@router.get("/settings")
def get_settings(db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    return service.get_settings(db, user["company_id"])


@router.put("/settings")
def put_settings(data: dict, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    out = service.update_settings(db, user["company_id"], data)
    db.commit()
    return out


# ── Committees ──────────────────────────────────────────────────────

def _committee_out(db: Session, c: GrievanceCommittee) -> dict:
    names = service.names_for(db, c.company_id, [m.employee_id for m in c.members])
    return {"uuid": str(c.uuid), "kind": c.kind, "name": c.name, "description": c.description,
            "constituted_on": c.constituted_on, "valid_until": c.valid_until, "active": c.active,
            "members": [{"uuid": str(m.uuid), "employee_id": m.employee_id, "name": names.get(m.employee_id) or m.external_name,
                         "external_name": m.external_name, "external_email": m.external_email,
                         "external_org": m.external_org, "role": m.role, "role_label": service.ROLES.get(m.role),
                         "is_external": m.employee_id is None} for m in c.members if m.active]}


@router.get("/committees")
def committees(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Everyone may see who sits on the committees (the POSH Act requires the names to be shown)."""
    rows = (db.query(GrievanceCommittee).filter(GrievanceCommittee.company_id == user["company_id"])
            .order_by(GrievanceCommittee.active.desc(), GrievanceCommittee.kind, GrievanceCommittee.id.desc()).all())
    return {"items": [_committee_out(db, c) for c in rows]}


@router.post("/committees", status_code=status.HTTP_201_CREATED)
def create_committee(body: schemas.CommitteeIn, db: Session = Depends(get_db),
                     user: dict = Depends(require_permission(MANAGE))):
    data = body.model_dump(exclude_unset=True)
    if body.members is not None:
        data["members"] = [m.model_dump() for m in body.members]
    c = service.save_committee(db, user["company_id"], data)
    db.commit()
    return _committee_out(db, c)


@router.put("/committees/{uuid}")
def update_committee(uuid: UUID, body: schemas.CommitteeIn, db: Session = Depends(get_db),
                     user: dict = Depends(require_permission(MANAGE))):
    c = service.get_committee(db, user["company_id"], uuid)
    data = body.model_dump(exclude_unset=True)
    if body.members is not None:
        data["members"] = [m.model_dump() for m in body.members]
    service.save_committee(db, user["company_id"], data, c)
    db.commit()
    db.refresh(c)
    return _committee_out(db, c)


# ── Grievances ──────────────────────────────────────────────────────

def _out(db: Session, g: Grievance, v: service.Viewer | None, names: dict, labels: dict, detail: bool = False,
         public: bool = False) -> dict:
    handler = bool(v and v.handles(g))
    today = service._today(db, g.company_id)
    out = {"uuid": None if public else str(g.uuid), "reference_no": g.reference_no, "kind": g.kind,
           "category_id": g.category_id, "category_name": labels.get(g.category_id), "subject": g.subject,
           "anonymous": g.anonymous, "status": g.status, "status_label": service.STATUSES.get(g.status),
           "received_on": g.received_on, "created_at": g.created_at, "closed_at": g.closed_at,
           "outcome": g.outcome, "outcome_label": service.OUTCOMES.get(g.outcome or ""),
           "complainant_name": None if g.anonymous else names.get(g.complainant_employee_id),
           "assigned_to_name": names.get(g.assigned_to_employee_id) if handler else None,
           "late_filing": g.late_filing if handler else None,
           "next_due_on": None, "overdue": False, "can_handle": handler,
           "is_complainant": bool(v and v.is_complainant(g))}
    if handler:
        steps = service.timeline(g, today)
        pending = [s for s in steps if s["state"] in ("overdue", "due_soon", "upcoming") and s["due_on"]]
        if pending and g.status in service.OPEN:
            nxt = min(pending, key=lambda s: s["due_on"])
            out["next_due_on"], out["next_due_label"] = nxt["due_on"], nxt["label"]
            out["overdue"] = any(s["state"] == "overdue" for s in steps) and g.status in service.OPEN
    if detail:
        from modules.documents import service as documents
        msgs = db.query(GrievanceMessage).filter(GrievanceMessage.grievance_id == g.id).order_by(GrievanceMessage.id).all()
        if not handler:
            msgs = [m for m in msgs if m.kind != "note"]
        out.update({
            "description": g.description, "incident_date": g.incident_date, "incident_location": g.incident_location,
            "witnesses": g.witnesses if (handler or not public) else None,
            "respondent_name": g.respondent_name if (handler or (v and v.is_complainant(g))) else None,
            "attachments": documents.attachments_with_links(db, g.company_id, g.attachments) if not public else [],
            "outcome_summary": g.outcome_summary, "action_taken": g.action_taken if handler else None,
            "messages": [{"uuid": str(m.uuid), "kind": m.kind, "body": m.body, "from_complainant": m.from_complainant,
                          "author": ("You" if public and m.from_complainant else
                                     ("Complainant" if m.from_complainant and g.anonymous else
                                      (m.author_label if m.kind == "event" or not public else "Committee"))),
                          "attachments": documents.attachments_with_links(db, g.company_id, m.attachments) if not public else [],
                          "created_at": m.created_at} for m in msgs],
        })
        if handler:
            out.update({"timeline": service.timeline(g, today), "delay_condoned": g.delay_condoned,
                        "delay_reason": g.delay_reason, "interim_relief": g.interim_relief,
                        "conciliation": g.conciliation, "filing_deadline": g.filing_deadline,
                        "inquiry_due_on": g.inquiry_due_on, "report_due_on": g.report_due_on,
                        "action_due_on": g.action_due_on,
                        "respondent_employee_id": g.respondent_employee_id,
                        "assigned_to_employee_id": g.assigned_to_employee_id})
    return out


def _labels(db: Session, company_id: int, rows) -> dict:
    from core.masters import labels_for
    return labels_for(db, company_id, [g.category_id for g in rows if g.category_id])


@router.get("")
def list_grievances(scope: str = "mine", q: str | None = None, status_: str | None = Query(None, alias="status"),
                    params: PageParams = Depends(), db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _viewer(db, user)
    query = service.list_query(db, v, scope)
    if status_:
        query = query.filter(Grievance.status.in_(status_.split(",")))
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Grievance.subject.ilike(like), Grievance.reference_no.ilike(like)))
    page = paginate(query.order_by(Grievance.created_at.desc()), params)
    names = service.names_for(db, v.company_id, [g.complainant_employee_id for g in page["items"]] +
                              [g.assigned_to_employee_id for g in page["items"]])
    labels = _labels(db, v.company_id, page["items"])
    page["items"] = [_out(db, g, v, names, labels) for g in page["items"]]
    return page


@router.get("/stats")
def stats(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _viewer(db, user)
    kinds = v.kinds()
    out = {}
    for kind in kinds:
        rows = db.query(Grievance).filter(Grievance.company_id == v.company_id, Grievance.kind == kind).all()
        today = service._today(db, v.company_id)
        open_rows = [g for g in rows if g.status in service.OPEN]
        out[kind] = {"open": len(open_rows), "total": len(rows),
                     "overdue": sum(1 for g in open_rows if any(s["state"] == "overdue" for s in service.timeline(g, today))),
                     "closed_this_year": sum(1 for g in rows if g.closed_at and g.closed_at.year == today.year)}
    return out


@router.post("", status_code=status.HTTP_201_CREATED)
def submit(body: schemas.GrievanceIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    cid = user["company_id"]
    data = body.model_dump()
    from modules.documents import service as documents
    data["attachments"] = documents.attachments_from(db, cid, [str(u) for u in body.attachments])
    if body.anonymous:
        from modules.documents.models import FileBlob
        ids = [a["uuid"] for a in data["attachments"]]
        if ids:  # uploads must not point back to the person
            db.query(FileBlob).filter(FileBlob.company_id == cid, FileBlob.uuid.in_(ids)).update(
                {"created_by_employee_id": None}, synchronize_session=False)
        with service.anonymous_actor():
            g, code = service.submit(db, cid, data, None)
            db.commit()
    else:
        g, code = service.submit(db, cid, data, user.get("employee_id"))
        db.commit()
    return {"uuid": None if g.anonymous else str(g.uuid), "reference_no": g.reference_no, "tracking_code": code,
            "anonymous": g.anonymous, "late_filing": g.late_filing, "filing_deadline": g.filing_deadline}


# ── Follow-up with the tracking code (signed in, without revealing who) ──

def _track(db: Session, company_id: int, code: str) -> dict:
    g = service.by_code(db, company_id, code)
    return _out(db, g, None, {}, _labels(db, company_id, [g]), detail=True, public=True)


@router.post("/track")
def track(body: schemas.TrackIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    return _track(db, user["company_id"], body.code)


@router.post("/track/messages", status_code=status.HTTP_201_CREATED)
def track_message(body: schemas.TrackMessageIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    cid = user["company_id"]
    g = service.by_code(db, cid, body.code)
    with service.anonymous_actor():
        service.post_message(db, g, body.body, kind="reply", from_complainant=True,
                             author_label="Complainant" if g.anonymous else None)
        db.commit()
    return _track(db, cid, body.code)


# ── POSH report and awareness programmes ────────────────────────────

def _require_posh(v: service.Viewer) -> None:
    if not (v.sees_posh or v.is_owner):
        raise Forbidden("Only the Internal Committee can see POSH reports.")


@router.get("/posh/annual-report")
def annual_report(year: int | None = None, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _viewer(db, user)
    _require_posh(v)
    year = year or service._today(db, v.company_id).year
    out = service.annual_report(db, v.company_id, year)
    from modules.documents import service as documents
    out["policy_acknowledgement"] = documents.policy_ack_summary(db, v.company_id, posh_only=True)
    return out


@router.get("/posh/programmes")
def programmes(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    rows = (db.query(PoshProgramme).filter(PoshProgramme.company_id == user["company_id"])
            .order_by(PoshProgramme.held_on.desc()).all())
    return {"items": [{"uuid": str(p.uuid), "held_on": p.held_on, "title": p.title, "kind": p.kind,
                       "participants": p.participants, "notes": p.notes} for p in rows]}


@router.post("/posh/programmes", status_code=status.HTTP_201_CREATED)
def add_programme(body: schemas.ProgrammeIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _viewer(db, user)
    if not (v.manager or v.ic_member):
        raise Forbidden("Only HR or the Internal Committee can record programmes.")
    if body.kind not in ("workshop", "awareness", "training"):
        raise BadRequest("Pick workshop, awareness programme or training.")
    p = PoshProgramme(company_id=v.company_id, **body.model_dump())
    db.add(p)
    db.commit()
    return {"uuid": str(p.uuid)}


@router.post("/deadlines/run")
def run_deadlines(db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    return {"alerts": service.deadline_alerts(db)}


@router.get("/{uuid}")
def detail(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _viewer(db, user)
    g = service.visible(db, v, uuid)
    names = service.names_for(db, v.company_id, [g.complainant_employee_id, g.assigned_to_employee_id])
    out = _out(db, g, v, names, _labels(db, v.company_id, [g]), detail=True)
    if v.handles(g):
        committee = service.active_committee(db, v.company_id, "posh_ic" if g.kind == "posh" else "grievance")
        out["committee"] = _committee_out(db, committee) if committee else None
    return out


@router.post("/{uuid}/messages", status_code=status.HTTP_201_CREATED)
def message(uuid: UUID, body: schemas.MessageIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _viewer(db, user)
    g = service.visible(db, v, uuid)
    from modules.documents import service as documents
    atts = documents.attachments_from(db, v.company_id, [str(u) for u in body.attachments])
    handler = v.handles(g)
    if body.kind == "note" and not handler:
        raise Forbidden("Only the committee can add confidential notes.")
    from core import context
    service.post_message(db, g, body.body, kind=body.kind, from_complainant=not handler and v.is_complainant(g),
                         author_employee_id=v.employee_id, author_label=context.current().actor_name, attachments=atts)
    db.commit()
    return detail(uuid, db, user)


@router.post("/{uuid}/actions")
def action(uuid: UUID, body: schemas.ActionIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _viewer(db, user)
    g = service.visible(db, v, uuid)
    service.update(db, v, g, body.model_dump())
    db.commit()
    return detail(uuid, db, user)


@router.post("/{uuid}/withdraw")
def withdraw(uuid: UUID, body: schemas.WithdrawIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _viewer(db, user)
    g = service.visible(db, v, uuid)
    if not v.is_complainant(g):
        raise Forbidden("Only the complainant can withdraw.")
    service.withdraw(db, g, body.reason)
    db.commit()
    return detail(uuid, db, user)


# ── Public follow-up (no sign-in): company web address + tracking code ──

def _company_id(db: Session, slug: str) -> int:
    from modules.company.models import Company
    with bypass_rls(db):
        cid = db.query(Company.id).filter(Company.slug == slug, Company.active.is_(True)).scalar()
    if cid is None:
        raise NotFound("Company")
    set_tenant(db, cid)
    return cid


@public_router.post("/{slug}/track")
def public_track(slug: str, body: schemas.TrackIn, db: Session = Depends(get_db)):
    return _track(db, _company_id(db, slug), body.code)


@public_router.post("/{slug}/messages", status_code=status.HTTP_201_CREATED)
def public_message(slug: str, body: schemas.TrackMessageIn, db: Session = Depends(get_db)):
    cid = _company_id(db, slug)
    g = service.by_code(db, cid, body.code)
    with service.anonymous_actor():
        service.post_message(db, g, body.body, kind="reply", from_complainant=True,
                             author_label="Complainant" if g.anonymous else None)
        db.commit()
    return _track(db, cid, body.code)


@public_router.post("/{slug}", status_code=status.HTTP_201_CREATED)
def public_submit(slug: str, body: schemas.GrievanceIn, db: Session = Depends(get_db)):
    """Anonymous grievance without signing in (for example from a QR code on the notice board)."""
    cid = _company_id(db, slug)
    if not body.anonymous:
        raise BadRequest("Sign in to raise a named grievance, or tick anonymous.")
    data = body.model_dump()
    data["attachments"] = []
    with service.anonymous_actor():
        g, code = service.submit(db, cid, data, None)
        db.commit()
    return {"reference_no": g.reference_no, "tracking_code": code}


extra_routers = [public_router]
