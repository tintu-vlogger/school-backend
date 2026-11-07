"""Hiring API: requisitions, jobs, pipeline board, candidates, interviews and scorecards,
reference checks, offers, referrals, reports and settings; plus the public careers page,
job feeds, the reference form and the candidate's offer page."""
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from core.db import bypass_rls, get_db, set_tenant
from core.errors import BadRequest, Forbidden, NotFound
from core.pagination import PageParams, paginate
from core.security import get_current_user
from modules.recruitment import boards, common as c, schemas, service, settings as rsettings
from modules.recruitment.models import (Application, Candidate, Interview, InterviewKit, Job, Offer, ReferenceCheck,
                                        Referral, Requisition, Scorecard)

router = APIRouter(prefix="/recruitment", tags=["Hiring"])
public_router = APIRouter(prefix="/public", tags=["Hiring (public)"])


def _v(db: Session, user: dict) -> service.Viewer:
    return service.Viewer(db, user)


def _admin(db: Session, user: dict) -> service.Viewer:
    v = _v(db, user)
    v.require_admin()
    return v


# ── Meta, summary, settings ─────────────────────────────────────────

@router.get("/meta")
def meta(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    from modules.recruitment import screening
    return {"is_admin": v.admin, "can_offers": v.offers, "employee_id": v.employee_id,
            "sources": rsettings.SOURCES, "stage_kinds": rsettings.STAGE_KINDS, "work_modes": service.WORK_MODES,
            "job_statuses": service.JOB_STATUSES, "requisition_statuses": service.REQ_STATUSES,
            "offer_statuses": service.OFFER_STATUSES, "recommendations": service.RECOMMENDATIONS,
            "bgv_statuses": service.BGV_STATUSES, "modes": service.MODES,
            "referral_statuses": service.REFERRAL_STATUSES,
            "boards": {k: b["label"] for k, b in rsettings.BOARDS.items()}, "scorers": screening.available(),
            "default_stages": rsettings.get(db, user["company_id"])["default_stages"],
            "competencies": rsettings.get(db, user["company_id"])["interviews"]["competencies"]}


@router.get("/summary")
def summary(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    return service.summary(db, _admin(db, user))


@router.get("/settings")
def get_settings_(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _admin(db, user)
    conf = rsettings.get(db, user["company_id"])
    return {"settings": rsettings.public(conf), "widget": boards.widget_snippets(db, user["company_id"])}


@router.put("/settings")
def put_settings(body: dict, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _admin(db, user)
    conf = rsettings.save(db, user["company_id"], body)
    db.commit()
    return {"settings": rsettings.public(conf), "widget": boards.widget_snippets(db, user["company_id"])}


# ── Requisitions ────────────────────────────────────────────────────

def _req_jobs(db: Session, company_id: int, ids: list[int]) -> dict:
    out: dict[int, list] = {}
    if ids:
        for j in db.query(Job).filter(Job.company_id == company_id, Job.requisition_id.in_(ids)).all():
            out.setdefault(j.requisition_id, []).append({"uuid": str(j.uuid), "title": j.title, "status": j.status})
    return out


@router.get("/requisitions")
def list_requisitions(scope: str = "all", status_: str | None = Query(None, alias="status"), q: str | None = None,
                      params: PageParams = Depends(), db: Session = Depends(get_db),
                      user: dict = Depends(get_current_user)):
    v = _v(db, user)
    qr = db.query(Requisition).filter(Requisition.company_id == v.company_id)
    if scope == "mine" or not v.admin:
        qr = qr.filter(or_(Requisition.requested_by_employee_id == v.employee_id,
                           Requisition.hiring_manager_id == v.employee_id) if v.employee_id else Requisition.id == -1)
    if status_:
        qr = qr.filter(Requisition.status.in_(status_.split(",")))
    if q:
        qr = qr.filter(or_(Requisition.title.ilike(f"%{q}%"), Requisition.number.ilike(f"%{q}%")))
    page = paginate(qr.order_by(Requisition.created_at.desc()), params)
    rows = page["items"]
    labels = c.org_labels(db, v.company_id, departments=[r.department_id for r in rows],
                          designations=[r.designation_id for r in rows], locations=[r.location_id for r in rows],
                          masters=[r.employment_type_id for r in rows])
    names = c.names_for(db, v.company_id, [x for r in rows for x in (r.hiring_manager_id, r.replacement_for_id)])
    jobs = _req_jobs(db, v.company_id, [r.id for r in rows])
    page["items"] = [service.requisition_out(db, r, labels, names, jobs) for r in rows]
    return page


@router.post("/requisitions", status_code=status.HTTP_201_CREATED)
def create_requisition(body: schemas.RequisitionIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    data = body.model_dump(exclude_unset=True)
    submit = data.pop("submit", False)
    r = service.save_requisition(db, v, data)
    if submit:
        service.submit_requisition(db, v, r)
    db.commit()
    return service.requisition_out(db, r, jobs=_req_jobs(db, v.company_id, [r.id]))


def _my_req(db: Session, v: service.Viewer, uuid) -> Requisition:
    r = service.get_requisition(db, v.company_id, uuid)
    if not v.admin and v.employee_id not in (r.requested_by_employee_id, r.hiring_manager_id):
        raise NotFound("Hiring request")
    return r


@router.get("/requisitions/{uuid}")
def get_requisition(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    r = _my_req(db, v, uuid)
    out = service.requisition_out(db, r, jobs=_req_jobs(db, v.company_id, [r.id]))
    if r.workflow_instance_id:
        from core.workflow.models import WorkflowInstance
        inst = db.get(WorkflowInstance, r.workflow_instance_id)
        out["workflow_uuid"] = str(inst.uuid) if inst else None
    return out


@router.put("/requisitions/{uuid}")
def update_requisition(uuid: UUID, body: schemas.RequisitionIn, db: Session = Depends(get_db),
                       user: dict = Depends(get_current_user)):
    v = _v(db, user)
    r = _my_req(db, v, uuid)
    data = body.model_dump(exclude_unset=True)
    submit = data.pop("submit", False)
    service.save_requisition(db, v, data, r)
    if submit:
        service.submit_requisition(db, v, r)
    db.commit()
    return get_requisition(uuid, db, user)


@router.post("/requisitions/{uuid}/submit")
def submit_requisition(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    service.submit_requisition(db, v, _my_req(db, v, uuid))
    db.commit()
    return get_requisition(uuid, db, user)


@router.post("/requisitions/{uuid}/cancel")
def cancel_requisition(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    service.cancel_requisition(db, v, _my_req(db, v, uuid))
    db.commit()
    return get_requisition(uuid, db, user)


# ── Jobs ────────────────────────────────────────────────────────────

def _jobs_out(db: Session, company_id: int, rows: list[Job]) -> list[dict]:
    labels = c.org_labels(db, company_id, departments=[j.department_id for j in rows],
                          designations=[j.designation_id for j in rows], locations=[j.location_id for j in rows],
                          masters=[j.employment_type_id for j in rows])
    counts = service.job_counts(db, company_id, [j.id for j in rows])
    names = c.names_for(db, company_id, [x for j in rows for x in [j.hiring_manager_id, *[m["employee_id"] for m in j.team or []]]])
    return [service.job_out(db, j, labels, counts, names) for j in rows]


@router.get("/jobs")
def list_jobs(status_: str | None = Query(None, alias="status"), q: str | None = None, department_id: int | None = None,
              mine: bool = False, params: PageParams = Depends(), db: Session = Depends(get_db),
              user: dict = Depends(get_current_user)):
    v = _v(db, user)
    qr = db.query(Job).filter(Job.company_id == v.company_id)
    if not v.admin or mine:
        if not v.employee_id:
            return {"items": [], "total": 0, "page": 1, "size": params.size}
        eid = v.employee_id
        qr = qr.filter(or_(Job.hiring_manager_id == eid, Job.team.contains([{"employee_id": eid}])))
    if status_:
        qr = qr.filter(Job.status.in_(status_.split(",")))
    if department_id:
        qr = qr.filter(Job.department_id == department_id)
    if q:
        qr = qr.filter(or_(Job.title.ilike(f"%{q}%"), Job.code.ilike(f"%{q}%")))
    from sqlalchemy import case
    order = case((Job.status == "open", 0), (Job.status == "draft", 1), (Job.status == "on_hold", 2), else_=3)
    page = paginate(qr.order_by(order, Job.created_at.desc()), params)
    page["items"] = _jobs_out(db, v.company_id, page["items"])
    return page


@router.post("/jobs", status_code=status.HTTP_201_CREATED)
def create_job(body: schemas.JobIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    data = body.model_dump(exclude_unset=True, mode="json")
    for k in ("salary_min", "salary_max", "referral_bonus", "closes_on"):
        if k in data:
            data[k] = getattr(body, k)
    j = service.save_job(db, v, data)
    db.commit()
    return _jobs_out(db, v.company_id, [j])[0]


@router.get("/jobs/{uuid}")
def get_job(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    j = service.get_job(db, v.company_id, uuid)
    if not v.on_team(j):
        raise NotFound("Job")
    out = _jobs_out(db, v.company_id, [j])[0]
    out["share"] = boards.share_links(db, v.company_id, j)
    return out


@router.put("/jobs/{uuid}")
def update_job(uuid: UUID, body: schemas.JobIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    j = service.get_job(db, v.company_id, uuid)
    data = body.model_dump(exclude_unset=True, mode="json")
    for k in ("salary_min", "salary_max", "referral_bonus", "closes_on"):
        if k in data:
            data[k] = getattr(body, k)
    service.save_job(db, v, data, j)
    db.commit()
    return get_job(uuid, db, user)


@router.post("/jobs/{uuid}/status")
def job_status(uuid: UUID, body: schemas.ActionIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    service.set_job_status(db, v, service.get_job(db, v.company_id, uuid), body.action)
    db.commit()
    return get_job(uuid, db, user)


@router.post("/jobs/{uuid}/postings")
def post_to_board(uuid: UUID, body: schemas.BoardIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _admin(db, user)
    j = service.get_job(db, v.company_id, uuid)
    rec = boards.publish(db, v.company_id, j, body.board)
    db.commit()
    return {"board": body.board, **rec}


def _card(db: Session, a: Application, job: Job, next_iv: dict, offers: dict) -> dict:
    cand = a.candidate
    ov = a.evaluation_override or {}
    return {"uuid": str(a.uuid), "candidate_uuid": str(cand.uuid),
            "candidate_name": f"{cand.first_name} {cand.last_name}".strip(), "current_title": cand.current_title,
            "current_company": cand.current_company, "total_experience": cand.total_experience,
            "location": cand.location, "stage": a.stage, "position": a.position, "status": a.status,
            "score": a.score, "source": a.source, "source_label": rsettings.SOURCES.get(a.source or ""),
            "tags": cand.tags or [], "applied_at": a.applied_at, "stage_entered_at": a.stage_entered_at,
            "recommendation": ov.get("recommendation") or (a.evaluation or {}).get("recommendation"),
            "average_rating": (a.evaluation or {}).get("average"), "next_interview": next_iv.get(a.id),
            "offer_status": offers.get(a.id), "rejection_reason": a.rejection_reason}


@router.get("/jobs/{uuid}/board")
def board(uuid: UUID, include_closed: bool = False, q: str | None = None, db: Session = Depends(get_db),
          user: dict = Depends(get_current_user)):
    v = _v(db, user)
    j = service.get_job(db, v.company_id, uuid)
    if not v.on_team(j):
        raise NotFound("Job")
    qr = (db.query(Application).options(joinedload(Application.candidate))
          .filter(Application.company_id == v.company_id, Application.job_id == j.id))
    if not include_closed:
        qr = qr.filter(Application.status.in_(("active", "hired")))
    if q:
        qr = qr.join(Candidate, Candidate.id == Application.candidate_id).filter(or_(
            Candidate.first_name.ilike(f"%{q}%"), Candidate.last_name.ilike(f"%{q}%"), Candidate.email.ilike(f"%{q}%")))
    apps = qr.order_by(Application.position).limit(1000).all()
    ids = [a.id for a in apps]
    next_iv = {}
    if ids:
        for iv in (db.query(Interview).filter(Interview.application_id.in_(ids), Interview.status == "scheduled",
                                              Interview.starts_at >= c.now()).order_by(Interview.starts_at)):
            next_iv.setdefault(iv.application_id, {"starts_at": iv.starts_at, "round_name": iv.round_name})
    offers = {}
    if ids:
        for o in db.query(Offer).filter(Offer.application_id.in_(ids)).order_by(Offer.id):
            offers[o.application_id] = o.status
    closed = (db.query(Application.status, func.count(Application.id))
              .filter(Application.job_id == j.id, Application.status.in_(("rejected", "withdrawn")))
              .group_by(Application.status).all())
    return {"job": _jobs_out(db, v.company_id, [j])[0], "stages": j.stages,
            "cards": [_card(db, a, j, next_iv, offers) for a in apps],
            "closed": {s: n for s, n in closed}}


@router.get("/jobs/{uuid}/applications")
def job_applications(uuid: UUID, status_: str | None = Query(None, alias="status"), stage: str | None = None,
                     q: str | None = None, sort: str = "score", params: PageParams = Depends(),
                     db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    j = service.get_job(db, v.company_id, uuid)
    if not v.on_team(j):
        raise NotFound("Job")
    qr = (db.query(Application).options(joinedload(Application.candidate))
          .join(Candidate, Candidate.id == Application.candidate_id)
          .filter(Application.company_id == v.company_id, Application.job_id == j.id))
    if status_:
        qr = qr.filter(Application.status.in_(status_.split(",")))
    if stage:
        qr = qr.filter(Application.stage == stage)
    if q:
        qr = qr.filter(or_(Candidate.first_name.ilike(f"%{q}%"), Candidate.last_name.ilike(f"%{q}%"),
                           Candidate.email.ilike(f"%{q}%")))
    order = {"score": Application.score.desc().nullslast(), "applied": Application.applied_at.desc()}.get(
        sort, Application.score.desc().nullslast())
    page = paginate(qr.order_by(order), params)
    page["items"] = [_card(db, a, j, {}, {}) for a in page["items"]]
    return page


# ── Candidates ──────────────────────────────────────────────────────

@router.get("/candidates")
def list_candidates(q: str | None = None, source: str | None = None, tag: str | None = None,
                    job_uuid: UUID | None = None, stage: str | None = None, status_: str | None = Query(None, alias="status"),
                    talent_pool: bool | None = None, follow_up_due: bool = False, params: PageParams = Depends(),
                    db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _admin(db, user)
    qr = db.query(Candidate).filter(Candidate.company_id == v.company_id,
                                    Candidate.status.notin_(("merged", "purged")))
    if status_:
        qr = qr.filter(Candidate.status.in_(status_.split(",")))
    if source:
        qr = qr.filter(Candidate.source.in_(source.split(",")))
    if tag:
        qr = qr.filter(Candidate.tags.contains([tag]))
    if talent_pool is not None:
        qr = qr.filter(Candidate.talent_pool.is_(talent_pool))
    if follow_up_due:
        qr = qr.filter(Candidate.follow_up_on <= c.today(db, v.company_id))
    if job_uuid or stage:
        sub = db.query(Application.candidate_id).join(Job, Job.id == Application.job_id).filter(
            Application.company_id == v.company_id)
        if job_uuid:
            sub = sub.filter(Job.uuid == job_uuid)
        if stage:
            sub = sub.filter(Application.stage == stage, Application.status == "active")
        qr = qr.filter(Candidate.id.in_(sub))
    if q:
        like = f"%{q}%"
        qr = qr.filter(or_(Candidate.first_name.ilike(like), Candidate.last_name.ilike(like), Candidate.email.ilike(like),
                           Candidate.phone.ilike(like), Candidate.current_company.ilike(like),
                           (Candidate.first_name + " " + Candidate.last_name).ilike(like)))
    page = paginate(qr.order_by(Candidate.created_at.desc()), params)
    rows = page["items"]
    apps: dict[int, list] = {}
    if rows:
        for a in (db.query(Application).options(joinedload(Application.job))
                  .filter(Application.candidate_id.in_([r.id for r in rows])).order_by(Application.applied_at.desc())):
            st = service.stage_map(a.job).get(a.stage, {})
            apps.setdefault(a.candidate_id, []).append({
                "uuid": str(a.uuid), "job_uuid": str(a.job.uuid), "job_title": a.job.title, "stage": a.stage,
                "stage_label": st.get("label"), "status": a.status, "score": a.score})
    page["items"] = [{**service.candidate_out(db, r), "applications": apps.get(r.id, [])} for r in rows]
    return page


@router.post("/candidates/parse")
def parse_resume(body: schemas.ParseIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _admin(db, user)
    from modules.documents import storage
    blob = storage.get(db, v.company_id, body.resume_uuid)
    job = service.get_job(db, v.company_id, body.job_uuid) if body.job_uuid else None
    res = service.parse_resume_blob(db, v.company_id, blob, job)
    fields = res["fields"]
    dups = service.find_duplicates(db, v.company_id, fields.get("email"), fields.get("phone"))
    return {"fields": fields, "text_found": bool(res["text"]), "characters": len(res["text"]),
            "duplicates": [{"uuid": str(d.uuid), "name": f"{d.first_name} {d.last_name}".strip(), "email": d.email}
                           for d in dups]}


@router.post("/candidates", status_code=status.HTTP_201_CREATED)
def create_candidate(body: schemas.CandidateIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _admin(db, user)
    data = body.model_dump(exclude_unset=True)
    resume_uuid, job_uuid = data.pop("resume_uuid", None), data.pop("job_uuid", None)
    allow_dup = data.pop("allow_duplicate", False)
    blob, text, parsed = None, None, {}
    job = service.get_job(db, v.company_id, job_uuid) if job_uuid else None
    if resume_uuid:
        from modules.documents import storage
        blob = storage.get(db, v.company_id, resume_uuid)
        res = service.parse_resume_blob(db, v.company_id, blob, job)
        text, parsed = res["text"], res["fields"]
        for k, val in parsed.items():
            if data.get(k) in (None, "", []):
                data[k] = val
    cand = service.save_candidate(db, v.company_id, data, resume_file_id=blob.id if blob else None, resume_text=text,
                                  allow_duplicate=allow_dup)
    cand.parsed = parsed
    if job is not None:
        service.apply(db, v.company_id, job, cand, source=cand.source)
    db.commit()
    return get_candidate(cand.uuid, db, user)


@router.get("/candidates/{uuid}")
def get_candidate(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _admin(db, user)
    cand = service.get_candidate(db, v.company_id, uuid)
    out = service.candidate_out(db, cand, detail=True)
    apps = (db.query(Application).options(joinedload(Application.job))
            .filter(Application.candidate_id == cand.id).order_by(Application.applied_at.desc()).all())
    out["applications"] = [application_brief(db, a) for a in apps]
    refs = db.query(Referral).filter(Referral.candidate_id == cand.id).all()
    out["referrals"] = [service.referral_out(db, r) for r in refs]
    return out


def application_brief(db: Session, a: Application) -> dict:
    st = service.stage_map(a.job).get(a.stage, {})
    return {"uuid": str(a.uuid), "job_uuid": str(a.job.uuid), "job_title": a.job.title, "stage": a.stage,
            "stage_label": st.get("label"), "status": a.status, "score": a.score, "applied_at": a.applied_at,
            "rejection_reason": a.rejection_reason, "stages": a.job.stages}


@router.put("/candidates/{uuid}")
def update_candidate(uuid: UUID, body: schemas.CandidateIn, db: Session = Depends(get_db),
                     user: dict = Depends(get_current_user)):
    v = _admin(db, user)
    cand = service.get_candidate(db, v.company_id, uuid)
    data = body.model_dump(exclude_unset=True)
    resume_uuid = data.pop("resume_uuid", None)
    data.pop("job_uuid", None)
    data.pop("allow_duplicate", None)
    blob, text = None, None
    if resume_uuid:
        from modules.documents import storage
        blob = storage.get(db, v.company_id, resume_uuid)
        res = service.parse_resume_blob(db, v.company_id, blob)
        text = res["text"]
        cand.parsed = res["fields"]
    service.save_candidate(db, v.company_id, data, cand, resume_file_id=blob.id if blob else None, resume_text=text)
    for a in cand.applications:
        if a.status == "active":
            service.rescore(db, a)
    db.commit()
    return get_candidate(uuid, db, user)


@router.post("/candidates/{uuid}/merge")
def merge(uuid: UUID, body: schemas.MergeIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _admin(db, user)
    keep = service.get_candidate(db, v.company_id, uuid)
    other = service.get_candidate(db, v.company_id, body.other_uuid)
    service.merge_candidates(db, v.company_id, keep, other)
    db.commit()
    return get_candidate(uuid, db, user)


@router.post("/candidates/{uuid}/apply", status_code=status.HTTP_201_CREATED)
def apply_candidate(uuid: UUID, body: schemas.ApplyIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _admin(db, user)
    cand = service.get_candidate(db, v.company_id, uuid)
    job = service.get_job(db, v.company_id, body.job_uuid)
    a = service.apply(db, v.company_id, job, cand, answers=body.answers)
    db.commit()
    return get_application(a.uuid, db, user)


@router.post("/candidates/{uuid}/purge")
def purge(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Delete a candidate's personal data now (for example on their request)."""
    v = _admin(db, user)
    cand = service.get_candidate(db, v.company_id, uuid)
    if cand.status == "hired":
        raise BadRequest("This person joined; their data is now part of the employee record.")
    service.purge_candidate(db, cand, "Deleted on request")
    db.commit()
    return {"status": "purged"}


@router.post("/candidates/purge-due")
def purge_due(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _admin(db, user)
    n = service.purge_due(db, v.company_id)
    db.commit()
    return {"purged": n}


# ── Applications ────────────────────────────────────────────────────

def _app(db: Session, v: service.Viewer, uuid) -> Application:
    a = service.get_application(db, v.company_id, uuid)
    if not v.on_team(a.job):
        raise NotFound("Application")
    return a


@router.get("/applications/{uuid}")
def get_application(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    a = _app(db, v, uuid)
    ivs = (db.query(Interview).options(joinedload(Interview.scorecards))
           .filter(Interview.application_id == a.id).order_by(Interview.starts_at).all())
    for iv in ivs:
        iv.application = a
    names = c.names_for(db, v.company_id, [e for iv in ivs for e in iv.panel or []])
    refs = db.query(ReferenceCheck).filter(ReferenceCheck.application_id == a.id).order_by(ReferenceCheck.id).all()
    offers = (db.query(Offer).options(joinedload(Offer.candidate), joinedload(Offer.job), joinedload(Offer.application))
              .filter(Offer.application_id == a.id).order_by(Offer.id.desc()).all())
    out = application_brief(db, a)
    out.update({
        "candidate": service.candidate_out(db, a.candidate, detail=v.admin),
        "answers": a.answers or {}, "questions": a.job.questions or [], "source": a.source,
        "score_detail": a.score_detail or {}, "evaluation": a.evaluation or {},
        "evaluation_override": a.evaluation_override or {}, "bgv_status": a.bgv_status,
        "bgv_status_label": service.BGV_STATUSES.get(a.bgv_status), "bgv_vendor": a.bgv_vendor,
        "bgv_notes": a.bgv_notes, "bgv_consent_at": a.bgv_consent_at, "history": list(reversed(a.history or [])),
        "interviews": [service.interview_out(db, iv, names, full=True, viewer_id=v.employee_id) for iv in ivs],
        "references": [service.reference_out(r) for r in refs],
        "offers": [service.offer_out(db, o) for o in offers] if v.offers else [],
        "can_manage": v.admin, "can_offer": v.offers,
    })
    return out


@router.post("/applications/{uuid}/move")
def move(uuid: UUID, body: schemas.MoveIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    a = _app(db, v, uuid)
    service.move_stage(db, v, a, body.stage, body.before_uuid, body.after_uuid)
    db.commit()
    return {"uuid": str(a.uuid), "stage": a.stage, "position": a.position}


@router.post("/applications/{uuid}/reject")
def reject(uuid: UUID, body: schemas.RejectIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    service.reject(db, v, _app(db, v, uuid), body.reason, body.notify_candidate)
    db.commit()
    return get_application(uuid, db, user)


@router.post("/applications/{uuid}/reopen")
def reopen(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    service.reopen(db, v, _app(db, v, uuid))
    db.commit()
    return get_application(uuid, db, user)


@router.post("/applications/{uuid}/notes")
def note(uuid: UUID, body: schemas.NoteIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    service.add_note(db, _app(db, v, uuid), body.text)
    db.commit()
    return get_application(uuid, db, user)


@router.post("/applications/{uuid}/rescore")
def rescore(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    a = _app(db, v, uuid)
    service.rescore(db, a)
    db.commit()
    return get_application(uuid, db, user)


@router.post("/applications/{uuid}/evaluation")
def evaluation(uuid: UUID, body: schemas.OverrideIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    service.override_evaluation(db, v, _app(db, v, uuid), body.recommendation, body.note)
    db.commit()
    return get_application(uuid, db, user)


@router.post("/applications/{uuid}/bgv")
def bgv(uuid: UUID, body: schemas.BgvIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    service.set_bgv(db, v, _app(db, v, uuid), body.model_dump())
    db.commit()
    return get_application(uuid, db, user)


@router.post("/applications/{uuid}/interviews", status_code=status.HTTP_201_CREATED)
def schedule(uuid: UUID, body: schemas.InterviewIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    a = _app(db, v, uuid)
    data = body.model_dump(exclude_unset=True)
    if "kit_uuid" in data and data["kit_uuid"]:
        data["kit_uuid"] = str(data["kit_uuid"])
    iv = service.schedule_interview(db, v, a, data)
    db.commit()
    return interview_detail(iv.uuid, db, user)


@router.post("/applications/{uuid}/references", status_code=status.HTTP_201_CREATED)
def add_reference(uuid: UUID, body: schemas.ReferenceIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    data = body.model_dump()
    send = data.pop("send", True)
    service.add_reference(db, v, _app(db, v, uuid), data, send)
    db.commit()
    return get_application(uuid, db, user)


@router.post("/references/{uuid}/send")
def send_reference(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _admin(db, user)
    r = db.query(ReferenceCheck).filter(ReferenceCheck.company_id == v.company_id, ReferenceCheck.uuid == uuid).first()
    if r is None:
        raise NotFound("Reference")
    a = service.get_application(db, v.company_id, db.get(Application, r.application_id).uuid)
    service.send_reference(db, r, a)
    db.commit()
    return service.reference_out(r)


@router.post("/applications/{uuid}/offers", status_code=status.HTTP_201_CREATED)
def create_offer(uuid: UUID, body: schemas.OfferIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    a = _app(db, v, uuid)
    data = body.model_dump(exclude_unset=True)
    submit = data.pop("submit", False)
    o = service.save_offer(db, v, a, data)
    if submit:
        service.submit_offer(db, v, o)
    db.commit()
    return offer_detail(o.uuid, db, user)


@router.post("/applications/bulk")
def bulk(body: schemas.BulkIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    done, failed = 0, []
    for u in body.uuids:
        try:
            with db.begin_nested():
                a = _app(db, v, u)
                if body.action == "move":
                    service.move_stage(db, v, a, body.stage or "")
                elif body.action == "reject":
                    service.reject(db, v, a, body.reason)
                elif body.action == "tag":
                    if not (body.tag or "").strip():
                        raise BadRequest("Type a tag.")
                    a.candidate.tags = service._clean_list((a.candidate.tags or []) + [body.tag], 12, 30)
                elif body.action == "talent_pool":
                    a.candidate.talent_pool = True
                else:
                    raise BadRequest("Unknown action.")
                done += 1
        except Exception as exc:
            failed.append({"uuid": str(u), "reason": getattr(exc, "detail", "Could not be changed")})
    db.commit()
    return {"done": done, "failed": failed}


# ── Interviews ──────────────────────────────────────────────────────

@router.get("/interviews")
def list_interviews(start: date | None = None, end: date | None = None, mine: bool = False,
                    status_: str | None = Query(None, alias="status"), job_uuid: UUID | None = None,
                    db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Interviews between two dates (calendar). People who are not on the hiring team see
    only the interviews they sit on."""
    v = _v(db, user)
    t = c.today(db, v.company_id)
    start = start or (t - timedelta(days=t.weekday()))
    end = end or (start + timedelta(days=41))
    if (end - start).days > 120:
        raise BadRequest("Pick up to 120 days.")
    qr = (db.query(Interview).options(joinedload(Interview.application).joinedload(Application.candidate),
                                      joinedload(Interview.application).joinedload(Application.job),
                                      joinedload(Interview.scorecards))
          .filter(Interview.company_id == v.company_id,
                  Interview.starts_at >= datetime.combine(start, time.min, tzinfo=timezone.utc) - timedelta(hours=14),
                  Interview.starts_at < datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc) + timedelta(hours=14)))
    if mine or not v.admin:
        if not v.employee_id:
            return {"items": []}
        qr = qr.filter(Interview.panel.contains([v.employee_id]))
    if status_:
        qr = qr.filter(Interview.status.in_(status_.split(",")))
    if job_uuid:
        qr = qr.join(Application, Application.id == Interview.application_id).join(Job, Job.id == Application.job_id) \
               .filter(Job.uuid == job_uuid)
    rows = qr.order_by(Interview.starts_at).limit(1000).all()
    names = c.names_for(db, v.company_id, [e for iv in rows for e in iv.panel or []])
    rows = [iv for iv in rows if iv.application.candidate.status != "purged"]
    return {"items": [service.interview_out(db, iv, names, viewer_id=v.employee_id) for iv in rows],
            "start": start, "end": end}


def _iv(db: Session, v: service.Viewer, uuid) -> Interview:
    iv = service.get_interview(db, v.company_id, uuid)
    if not (v.on_team(iv.application.job) or (v.employee_id and v.employee_id in (iv.panel or []))):
        raise NotFound("Interview")
    return iv


@router.get("/interviews/{uuid}")
def interview_detail(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    iv = _iv(db, v, uuid)
    out = service.interview_out(db, iv, full=True, viewer_id=v.employee_id)
    cand = iv.application.candidate
    out["candidate"] = service.candidate_out(db, cand, detail=True)
    out["can_manage"] = v.on_team(iv.application.job)
    if not out["can_manage"]:
        # interviewers see their own scorecard, not the others' (independent ratings)
        out["scorecards"] = [s for s in out["scorecards"] if s["interviewer_employee_id"] == v.employee_id]
    return out


@router.put("/interviews/{uuid}")
def update_interview(uuid: UUID, body: schemas.InterviewIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    iv = _iv(db, v, uuid)
    data = body.model_dump(exclude_unset=True)
    if data.get("kit_uuid"):
        data["kit_uuid"] = str(data["kit_uuid"])
    service.schedule_interview(db, v, iv.application, data, iv)
    db.commit()
    return interview_detail(uuid, db, user)


@router.post("/interviews/{uuid}/cancel")
def cancel_interview(uuid: UUID, body: schemas.CancelIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    service.cancel_interview(db, v, _iv(db, v, uuid), body.reason)
    db.commit()
    return interview_detail(uuid, db, user)


@router.post("/interviews/{uuid}/status")
def interview_status(uuid: UUID, body: schemas.StatusIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    service.set_interview_status(db, v, _iv(db, v, uuid), body.status)
    db.commit()
    return interview_detail(uuid, db, user)


@router.post("/interviews/{uuid}/scorecard")
def scorecard(uuid: UUID, body: schemas.ScorecardIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    service.submit_scorecard(db, v, _iv(db, v, uuid), body.model_dump())
    db.commit()
    return interview_detail(uuid, db, user)


@router.get("/interviews/{uuid}/invite.ics")
def interview_ics(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    from modules.recruitment import mailer
    v = _v(db, user)
    iv = _iv(db, v, uuid)
    a = iv.application
    text = mailer.ics(uid=f"{iv.uuid}@nextgenhrms", start=iv.starts_at, end=iv.ends_at,
                      summary=f"Interview: {a.candidate.first_name} {a.candidate.last_name} ({a.job.title})",
                      description=iv.meeting_link or "", location=iv.meeting_link or iv.location or "",
                      sequence=iv.sequence, cancel=iv.status == "cancelled")
    return Response(text, media_type="text/calendar",
                    headers={"Content-Disposition": f'attachment; filename="interview-{str(iv.uuid)[:8]}.ics"'})


# ── Interview kits ──────────────────────────────────────────────────

def _kit_out(k: InterviewKit) -> dict:
    return {"uuid": str(k.uuid), "name": k.name, "description": k.description, "competencies": k.competencies or [],
            "questions": k.questions or [], "duration_minutes": k.duration_minutes, "active": k.active}


@router.get("/kits")
def list_kits(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    rows = db.query(InterviewKit).filter(InterviewKit.company_id == user["company_id"]).order_by(InterviewKit.name).all()
    return {"items": [_kit_out(k) for k in rows]}


def _save_kit(db: Session, company_id: int, body: schemas.KitIn, k: InterviewKit | None = None) -> InterviewKit:
    if not body.name.strip():
        raise BadRequest("Name the kit.")
    comps = []
    for i, x in enumerate(body.competencies):
        label = (x.get("label") or "").strip()
        if label:
            key = x.get("key") or "".join(ch if ch.isalnum() else "_" for ch in label.lower())[:40]
            comps.append({"key": key, "label": label[:80], "description": (x.get("description") or "")[:200]})
    if not comps:
        raise BadRequest("Add at least one competency to rate.")
    k = k or InterviewKit(company_id=company_id)
    k.name, k.description, k.competencies = body.name.strip()[:150], body.description, comps
    k.questions = [q.strip()[:300] for q in body.questions if q.strip()][:30]
    k.duration_minutes, k.active = max(10, min(body.duration_minutes, 480)), body.active
    db.add(k)
    db.flush()
    return k


@router.post("/kits", status_code=status.HTTP_201_CREATED)
def create_kit(body: schemas.KitIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _admin(db, user)
    k = _save_kit(db, user["company_id"], body)
    db.commit()
    return _kit_out(k)


@router.put("/kits/{uuid}")
def update_kit(uuid: UUID, body: schemas.KitIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _admin(db, user)
    k = db.query(InterviewKit).filter(InterviewKit.company_id == user["company_id"], InterviewKit.uuid == uuid).first()
    if k is None:
        raise NotFound("Interview kit")
    _save_kit(db, user["company_id"], body, k)
    db.commit()
    return _kit_out(k)


# ── Offers ──────────────────────────────────────────────────────────

@router.get("/offers")
def list_offers(status_: str | None = Query(None, alias="status"), q: str | None = None, params: PageParams = Depends(),
                db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    if not v.offers:
        raise Forbidden("Only offer administrators can see offers.")
    qr = (db.query(Offer).options(joinedload(Offer.candidate), joinedload(Offer.job), joinedload(Offer.application))
          .join(Candidate, Candidate.id == Offer.candidate_id).filter(Offer.company_id == v.company_id))
    if status_:
        qr = qr.filter(Offer.status.in_(status_.split(",")))
    if q:
        qr = qr.filter(or_(Candidate.first_name.ilike(f"%{q}%"), Candidate.last_name.ilike(f"%{q}%"),
                           Offer.number.ilike(f"%{q}%")))
    page = paginate(qr.order_by(Offer.created_at.desc()), params)
    page["items"] = [service.offer_out(db, o) for o in page["items"]]
    counts = dict(db.query(Offer.status, func.count(Offer.id)).filter(Offer.company_id == v.company_id)
                  .group_by(Offer.status).all())
    page["counts"] = counts
    return page


@router.get("/offers/structures")
def offer_structures(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    from modules.recruitment import salary
    return {"items": salary.structures(db, user["company_id"])}


@router.post("/offers/breakup")
def offer_breakup(body: schemas.BreakupIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    from modules.recruitment import salary
    _v(db, user)
    return salary.breakup(db, user["company_id"], body.annual_ctc, body.structure_id,
                          body.joining_date or c.today(db, user["company_id"]))


def _offer(db: Session, v: service.Viewer, uuid) -> Offer:
    if not v.offers:
        raise Forbidden("Only offer administrators can see offers.")
    return service.get_offer(db, v.company_id, uuid)


@router.get("/offers/{uuid}")
def offer_detail(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    o = _offer(db, v, uuid)
    out = service.offer_out(db, o, full=True)
    if o.workflow_instance_id:
        from core.workflow.models import WorkflowInstance
        inst = db.get(WorkflowInstance, o.workflow_instance_id)
        out["workflow_uuid"] = str(inst.uuid) if inst else None
        out["workflow_status"] = inst.status if inst else None
    return out


@router.put("/offers/{uuid}")
def update_offer(uuid: UUID, body: schemas.OfferIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    o = _offer(db, v, uuid)
    data = body.model_dump(exclude_unset=True)
    submit = data.pop("submit", False)
    service.save_offer(db, v, o.application, data, o)
    if submit:
        service.submit_offer(db, v, o)
    db.commit()
    return offer_detail(uuid, db, user)


@router.post("/offers/{uuid}/submit")
def submit_offer(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    service.submit_offer(db, v, _offer(db, v, uuid))
    db.commit()
    return offer_detail(uuid, db, user)


@router.post("/offers/{uuid}/send")
def send_offer(uuid: UUID, body: schemas.SendIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    service.send_offer(db, v, _offer(db, v, uuid), body.message)
    db.commit()
    return offer_detail(uuid, db, user)


@router.post("/offers/{uuid}/withdraw")
def withdraw_offer(uuid: UUID, body: schemas.DeclineIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    service.withdraw_offer(db, v, _offer(db, v, uuid), body.reason)
    db.commit()
    return offer_detail(uuid, db, user)


@router.post("/offers/{uuid}/accept-on-behalf")
def accept_on_behalf(uuid: UUID, body: schemas.AcceptIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """HR records an acceptance received on paper (physical signature) or by email."""
    v = _v(db, user)
    o = _offer(db, v, uuid)
    if o.esign_request_id:
        from modules.documents import service as documents
        from modules.documents.models import EsignRequest
        req = db.get(EsignRequest, o.esign_request_id)
        if req is not None and req.status == "out_for_signature":
            documents.cancel_signature(db, req, "Signed on paper")
        o.esign_request_id = None
    service.accept_offer(db, o, body.typed_name)
    service._offer_log(o, f"Acceptance recorded by {c.actor_name()} (signed copy received)")
    db.commit()
    return offer_detail(uuid, db, user)


# ── Referrals ───────────────────────────────────────────────────────

@router.get("/referrals")
def list_referrals(scope: str = "mine", status_: str | None = Query(None, alias="status"), params: PageParams = Depends(),
                   db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    qr = db.query(Referral).filter(Referral.company_id == v.company_id)
    if scope != "all" or not v.admin:
        qr = qr.filter(Referral.referrer_employee_id == (v.employee_id or -1))
    if status_:
        qr = qr.filter(Referral.bonus_status.in_(status_.split(",")))
    page = paginate(qr.order_by(Referral.created_at.desc()), params)
    names = c.names_for(db, v.company_id, [r.referrer_employee_id for r in page["items"]])
    page["items"] = [service.referral_out(db, r, names) for r in page["items"]]
    return page


@router.get("/referrals/jobs")
def referral_jobs(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    rows = (db.query(Job).filter(Job.company_id == user["company_id"], Job.status == "open")
            .order_by(Job.published_at.desc().nullslast()).all())
    conf = rsettings.get(db, user["company_id"])["referrals"]
    labels = c.org_labels(db, user["company_id"], departments=[j.department_id for j in rows],
                          locations=[j.location_id for j in rows], masters=[j.employment_type_id for j in rows])
    return {"items": [{**service.job_out(db, j, labels, public=True),
                       "referral_bonus": j.referral_bonus if j.referral_bonus is not None else conf.get("default_bonus")}
                      for j in rows]}


@router.post("/referrals", status_code=status.HTTP_201_CREATED)
def create_referral(body: schemas.ReferralIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _v(db, user)
    job = service.get_job(db, v.company_id, body.job_uuid)
    data = body.model_dump(exclude={"job_uuid", "resume_uuid"})
    blob = None
    if body.resume_uuid:
        from modules.documents import storage
        blob = storage.get(db, v.company_id, body.resume_uuid)
    r = service.refer(db, v, job, data, blob)
    db.commit()
    return service.referral_out(db, r)


@router.post("/referrals/{uuid}/status")
def referral_status(uuid: UUID, body: schemas.ReferralStatusIn, db: Session = Depends(get_db),
                    user: dict = Depends(get_current_user)):
    v = _v(db, user)
    r = db.query(Referral).filter(Referral.company_id == v.company_id, Referral.uuid == uuid).first()
    if r is None:
        raise NotFound("Referral")
    service.set_referral_status(db, v, r, body.status, body.payment_ref)
    db.commit()
    return service.referral_out(db, r)


# ── Reports ─────────────────────────────────────────────────────────

@router.get("/reports")
def reports(job_uuid: UUID | None = None, start: date | None = None, end: date | None = None,
            db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    v = _admin(db, user)
    return service.reports(db, v.company_id, job_uuid, start, end)


# ── Public: careers page, feeds, reference form, offer page ─────────

def _company_by_slug(db: Session, slug: str):
    from modules.company.models import Company
    with bypass_rls(db):
        comp = db.query(Company).filter(Company.slug == slug, Company.active.is_(True)).first()
    if comp is None:
        raise NotFound("Company")
    set_tenant(db, comp.id)
    return comp


def _public_jobs(db: Session, company_id: int) -> list[Job]:
    t = c.today(db, company_id)
    return (db.query(Job).filter(Job.company_id == company_id, Job.status == "open", Job.published.is_(True),
                                 or_(Job.closes_on.is_(None), Job.closes_on >= t))
            .order_by(Job.published_at.desc().nullslast()).all())


@public_router.get("/careers/{slug}")
def careers(slug: str, db: Session = Depends(get_db)):
    comp = _company_by_slug(db, slug)
    conf = rsettings.get(db, comp.id)
    if not conf["careers"].get("enabled", True):
        raise NotFound("Careers page")
    jobs = _public_jobs(db, comp.id)
    items = boards.feed_items(db, comp.id, jobs)
    for d, j in zip(items, jobs):
        d.pop("description", None)
        d.pop("questions", None)
    depts = sorted({d["department"] for d in items if d.get("department")})
    locs = sorted({d["location"] for d in items if d.get("location")})
    car = conf["careers"]
    logo = comp.logo_url if comp.logo_url and (comp.logo_url.startswith("data:image/") or comp.logo_url.startswith("http")) else None
    return {"company": {"name": comp.company_name, "logo_url": logo, "city": comp.city},
            "branding": {k: car.get(k) for k in ("headline", "intro", "about", "accent", "show_department_filter",
                                                 "show_location_filter")},
            "privacy_notice": conf["privacy"]["notice"], "retention_days": conf["privacy"]["retention_days"],
            "grievance_contact": conf["privacy"].get("grievance_contact") or comp.email,
            "jobs": items, "filters": {"departments": depts, "locations": locs,
                                       "work_modes": sorted({d["work_mode"] for d in items})}}


def _public_job(db: Session, company_id: int, job_slug: str) -> Job:
    j = db.query(Job).filter(Job.company_id == company_id, Job.slug == job_slug).first()
    if j is None or not j.published or j.status != "open":
        raise NotFound("Job")
    return j


@public_router.get("/careers/{slug}/jobs/{job_slug}")
def careers_job(slug: str, job_slug: str, db: Session = Depends(get_db)):
    comp = _company_by_slug(db, slug)
    j = _public_job(db, comp.id, job_slug)
    conf = rsettings.get(db, comp.id)
    d = boards.feed_items(db, comp.id, [j])[0]
    d["questions"] = (j.questions or []) or conf["careers"].get("default_questions") or []
    logo = comp.logo_url if comp.logo_url and (comp.logo_url.startswith("data:image/") or comp.logo_url.startswith("http")) else None
    return {"company": {"name": comp.company_name, "logo_url": logo},
            "branding": {"accent": conf["careers"].get("accent")}, "job": d,
            "privacy_notice": conf["privacy"]["notice"], "retention_days": conf["privacy"]["retention_days"]}


@public_router.post("/careers/{slug}/jobs/{job_slug}/apply", status_code=status.HTTP_201_CREATED)
async def careers_apply(slug: str, job_slug: str, request: Request, first_name: str = Form(...),
                        last_name: str = Form(""), email: str = Form(...), phone: str = Form(""),
                        location: str = Form(""), current_company: str = Form(""), current_title: str = Form(""),
                        total_experience: str = Form(""), expected_ctc: str = Form(""), notice_days: str = Form(""),
                        linkedin_url: str = Form(""), answers: str = Form("{}"), consent: str = Form(""),
                        website: str = Form(""), source: str = Form("careers"), source_detail: str = Form(""),
                        resume: UploadFile | None = File(None), db: Session = Depends(get_db)):
    import json
    if website:  # honeypot field: people never fill it, bots do
        return {"status": "received"}
    if consent not in ("1", "true", "yes", "on"):
        raise BadRequest("Please agree to the privacy notice so we can consider your application.")
    comp = _company_by_slug(db, slug)
    j = _public_job(db, comp.id, job_slug)
    conf = rsettings.get(db, comp.id)
    try:
        ans = json.loads(answers or "{}")
        if not isinstance(ans, dict):
            raise ValueError
    except ValueError:
        raise BadRequest("Answers could not be read.")
    questions = (j.questions or []) or conf["careers"].get("default_questions") or []
    for q in questions:
        if q.get("required") and ans.get(q["key"]) in (None, ""):
            raise BadRequest(f"Answer: {q['label']}")
    blob, text, parsed = None, None, {}
    if resume is not None and resume.filename:
        raw = await resume.read()
        if len(raw) > 5 * 1024 * 1024:
            raise BadRequest("Your CV should be smaller than 5 MB.")
        ext = resume.filename.lower().rsplit(".", 1)[-1]
        if ext not in ("pdf", "doc", "docx", "txt", "rtf"):
            raise BadRequest("Upload your CV as a PDF or Word file.")
        from modules.documents import storage
        blob = storage.store(db, comp.id, resume.filename, raw, resume.content_type, "resume")
        from modules.recruitment import resume as rparse
        text = rparse.extract_text(resume.filename, raw)
        parsed = rparse.parse(text, (j.skills or []) + (j.nice_skills or []))
    src = source if source in rsettings.SOURCES else "careers"
    data = {"first_name": first_name, "last_name": last_name, "email": email, "phone": phone, "location": location,
            "current_company": current_company, "current_title": current_title,
            "total_experience": total_experience or None, "expected_ctc": expected_ctc or None,
            "notice_days": notice_days or None, "linkedin_url": linkedin_url, "source": src,
            "source_detail": source_detail or None}
    data = {k: v for k, v in data.items() if v not in (None, "")}
    for k, val in parsed.items():
        if k in ("skills", "education", "current_ctc") or data.get(k) in (None, ""):
            if k not in ("first_name", "last_name", "email"):
                data.setdefault(k, val)
    ip = request.client.host if request.client else None
    consent_rec = {"text": conf["privacy"]["notice"], "ip": ip}
    dups = service.find_duplicates(db, comp.id, email, phone)
    if dups:
        cand = dups[0]
        service.save_candidate(db, comp.id, {k: val for k, val in data.items() if k not in ("source", "first_name", "last_name")},
                               cand, resume_file_id=blob.id if blob else None, resume_text=text, consent=consent_rec)
    else:
        cand = service.save_candidate(db, comp.id, data, resume_file_id=blob.id if blob else None, resume_text=text,
                                      consent=consent_rec)
    if parsed:
        cand.parsed = parsed
    try:
        service.apply(db, comp.id, j, cand, source=src, answers=ans)
    except BadRequest as exc:
        if "already applied" in str(exc.detail):
            raise BadRequest("You have already applied for this job. We will be in touch.")
        raise
    c.notify_hr(db, comp.id, f"New application for {j.title}: {cand.first_name} {cand.last_name}.", "hiring")
    from core.email import send_email
    send_email(cand.email or email, f"We received your application for {j.title}",
               f"Dear {first_name},\n\nThank you for applying for {j.title} at {comp.company_name}. Our team will review "
               f"your application and get in touch if your profile matches.\n\nHiring team")
    db.commit()
    return {"status": "received"}


@public_router.get("/careers/{slug}/feed.xml")
def feed_xml(slug: str, db: Session = Depends(get_db)):
    comp = _company_by_slug(db, slug)
    return Response(boards.xml_feed(db, comp.id, _public_jobs(db, comp.id)), media_type="application/xml",
                    headers={"Cache-Control": "public, max-age=900"})


@public_router.get("/careers/{slug}/feed.json")
def feed_json(slug: str, db: Session = Depends(get_db)):
    comp = _company_by_slug(db, slug)
    return JSONResponse(boards.json_feed(db, comp.id, _public_jobs(db, comp.id)),
                        headers={"Cache-Control": "public, max-age=900"})


def _by_token(db: Session, model, column, token: str):
    with bypass_rls(db):
        row = db.query(model.company_id).filter(column == token).first()
    if row is None or not token:
        raise NotFound("Link")
    set_tenant(db, row.company_id)
    return row.company_id


@public_router.get("/references/{token}")
def reference_form(token: str, db: Session = Depends(get_db)):
    _by_token(db, ReferenceCheck, ReferenceCheck.access_token, token)
    r = service.reference_by_token(db, token)
    a = db.get(Application, r.application_id)
    comp = c.company(db, r.company_id)
    return {"company_name": comp.company_name if comp else None, "referee_name": r.referee_name,
            "candidate_name": f"{a.candidate.first_name} {a.candidate.last_name}".strip(), "job_title": a.job.title,
            "status": r.status, "expired": bool(r.expires_at and r.expires_at < c.now()),
            "questions": service.REFERENCE_QUESTIONS}


@public_router.post("/references/{token}")
def reference_submit(token: str, body: schemas.ReferenceAnswerIn, db: Session = Depends(get_db)):
    _by_token(db, ReferenceCheck, ReferenceCheck.access_token, token)
    r = service.reference_by_token(db, token)
    service.submit_reference(db, r, body.answers, body.decline)
    db.commit()
    return {"status": r.status}


@public_router.get("/offers/{token}")
def offer_page(token: str, db: Session = Depends(get_db)):
    _by_token(db, Offer, Offer.access_token, token)
    o = service.offer_by_token(db, token)
    if o.status == "sent" and o.expires_on and o.expires_on < c.today(db, o.company_id):
        o.status = "expired"
    if o.viewed_at is None:
        o.viewed_at = c.now()
        service._offer_log(o, "Opened by the candidate")
    db.commit()
    comp = c.company(db, o.company_id)
    out = service.offer_out(db, o)
    es = service.esign_state(db, o)
    file = None
    if o.letter_id:
        from modules.documents import service as documents
        from modules.documents.models import EsignRequest, Letter
        letter = db.get(Letter, o.letter_id)
        req = db.get(EsignRequest, o.esign_request_id) if o.esign_request_id else None
        fid = req.signed_file_id if req is not None and req.signed_file_id else (letter.stored_file_id if letter else None)
        file = documents.file_link(db, o.company_id, fid) if fid else None
    logo = comp.logo_url if comp and comp.logo_url and (comp.logo_url.startswith("data:image/") or comp.logo_url.startswith("http")) else None
    keep = ("number", "status", "status_label", "candidate_name", "job_title", "designation", "department", "location",
            "employment_type", "reporting_manager", "joining_date", "annual_ctc", "joining_bonus", "breakup",
            "monthly_gross", "probation_months", "notice_days", "expires_on", "days_left", "decline_reason",
            "accepted_name", "responded_at")
    return {"company": {"name": comp.company_name if comp else None, "logo_url": logo},
            "offer": {k: out.get(k) for k in keep}, "letter": file,
            "esign": {"required": es["required"], "signed": es.get("signed", False), "status": es.get("status"),
                      "token": es.get("signer_token") if o.status == "sent" else None},
            "decline_reasons": _decline_reasons(db, o.company_id)}


def _decline_reasons(db: Session, company_id: int) -> list[str]:
    try:
        from core.masters import get_type, values_query
        t = get_type(db, company_id, "offer_decline_reason")
        return [v.label for v in values_query(db, company_id, t).all() if getattr(v, "active", True)]
    except Exception:
        return []


@public_router.post("/offers/{token}/accept")
def offer_accept(token: str, body: schemas.AcceptIn, request: Request, db: Session = Depends(get_db)):
    _by_token(db, Offer, Offer.access_token, token)
    o = service.offer_by_token(db, token)
    service.accept_offer(db, o, body.typed_name, request.client.host if request.client else None)
    db.commit()
    return {"status": o.status}


@public_router.post("/offers/{token}/decline")
def offer_decline(token: str, body: schemas.DeclineIn, db: Session = Depends(get_db)):
    _by_token(db, Offer, Offer.access_token, token)
    o = service.offer_by_token(db, token)
    service.decline_offer(db, o, body.reason)
    db.commit()
    return {"status": o.status}


extra_routers = [public_router]
