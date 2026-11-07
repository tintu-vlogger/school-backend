"""Probation API: board and dashboard, policies, one probation with reviews and outcome,
bulk actions, and the reviewer's own list."""
from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from core.db import get_db
from core.errors import BadRequest, NotFound
from core.pagination import PageParams, paginate
from core.security import get_current_user
from modules.probation import service
from modules.probation.models import Probation, ProbationPolicy, ProbationReview
from modules.recruitment import common as c

router = APIRouter(prefix="/probation", tags=["Probation"])


class _In(BaseModel):
    model_config = ConfigDict(extra="ignore")


class StartIn(_In):
    employee_id: int
    policy_uuid: UUID | None = None
    start_date: date | None = None
    end_date: date | None = None


class DecideIn(_In):
    outcome: str
    reason: str | None = None
    confirmation_date: date | None = None
    new_end_date: date | None = None
    last_working_day: date | None = None
    letter: bool = True


class ReviewIn(_In):
    rating: int | None = None
    recommendation: str | None = None
    strengths: str | None = None
    improvements: str | None = None
    comments: str | None = None


class ReasonIn(_In):
    reason: str | None = None


class EndDateIn(_In):
    end_date: date
    reason: str | None = None


class BulkIn(_In):
    uuids: list[UUID]
    action: str  # start_review | confirm | remind | cancel
    reason: str | None = None


def _m(db, user):
    service.require_manage(db, user)


@router.get("/meta")
def meta(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    return {"can_manage": service.can_manage(db, user), "statuses": service.STATUSES, "outcomes": service.OUTCOMES,
            "reviewer_types": service.REVIEWER_TYPES}


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _m(db, user)
    return service.dashboard(db, user["company_id"])


# ── Policies ────────────────────────────────────────────────────────

@router.get("/policies")
def policies(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _m(db, user)
    rows = (db.query(ProbationPolicy).filter(ProbationPolicy.company_id == user["company_id"])
            .order_by(ProbationPolicy.priority, ProbationPolicy.id).all())
    counts = dict(db.query(Probation.policy_id, func.count(Probation.id))
                  .filter(Probation.company_id == user["company_id"], Probation.status.in_(service.OPEN))
                  .group_by(Probation.policy_id).all())
    return {"items": [service.policy_out(db, p, counts) for p in rows]}


@router.post("/policies", status_code=status.HTTP_201_CREATED)
def create_policy(body: dict, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _m(db, user)
    p = service.save_policy(db, user["company_id"], body)
    db.commit()
    return service.policy_out(db, p)


@router.put("/policies/{uuid}")
def update_policy(uuid: UUID, body: dict, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _m(db, user)
    p = service.save_policy(db, user["company_id"], body, service.get_policy(db, user["company_id"], uuid))
    db.commit()
    return service.policy_out(db, p)


# ── Probations ──────────────────────────────────────────────────────

@router.get("/probations")
def list_probations(status_: str | None = Query(None, alias="status"), q: str | None = None,
                    ending_within: int | None = None, params: PageParams = Depends(),
                    db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _m(db, user)
    cid = user["company_id"]
    E = c.Employee()
    qr = (db.query(Probation).options(joinedload(Probation.policy), joinedload(Probation.reviews))
          .join(E, E.id == Probation.employee_id).filter(Probation.company_id == cid))
    if status_:
        qr = qr.filter(Probation.status.in_(status_.split(",")))
    if q:
        qr = qr.filter(or_(E.first_name.ilike(f"%{q}%"), E.last_name.ilike(f"%{q}%"), E.employee_code.ilike(f"%{q}%")))
    if ending_within is not None:
        from datetime import timedelta
        qr = qr.filter(Probation.end_date <= c.today(db, cid) + timedelta(days=ending_within))
    page = paginate(qr.order_by(Probation.end_date), params)
    rows = page["items"]
    emps = {e.id: e for e in db.query(E).filter(E.company_id == cid, E.id.in_([p.employee_id for p in rows] or [0]))}
    labels = c.org_labels(db, cid, departments=[e.department_id for e in emps.values()],
                          designations=[e.designation_id for e in emps.values()])
    page["items"] = [service.out(db, p, emps[p.employee_id], labels) for p in rows]
    return page


@router.post("/probations", status_code=status.HTTP_201_CREATED)
def start(body: StartIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _m(db, user)
    cid = user["company_id"]
    emp = c.employee(db, cid, body.employee_id)
    if emp is None:
        raise NotFound("Employee")
    if emp.status in ("exited", "archived"):
        raise BadRequest("This person has left.")
    pol = service.get_policy(db, cid, body.policy_uuid) if body.policy_uuid else None
    p = service.start_for(db, cid, emp, pol, body.start_date, body.end_date)
    if emp.status == "confirmed":
        from modules.employees import service as people
        people.set_status(db, cid, emp, "probation", reason="Probation started", extra={"probation_end_date": p.end_date})
    db.commit()
    return detail(p.uuid, db, user)


@router.get("/probations/{uuid}")
def detail(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _m(db, user)
    return service.out(db, service.get(db, user["company_id"], uuid), full=True)


@router.post("/probations/{uuid}/start-review")
def start_review(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _m(db, user)
    service.start_review(db, service.get(db, user["company_id"], uuid))
    db.commit()
    return detail(uuid, db, user)


@router.post("/probations/{uuid}/decide")
def decide(uuid: UUID, body: DecideIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _m(db, user)
    p = service.get(db, user["company_id"], uuid)
    data = body.model_dump(exclude_unset=True)
    for k in ("confirmation_date", "new_end_date", "last_working_day"):
        if data.get(k):
            data[k] = data[k].isoformat()
    service.decide(db, p, body.outcome, data)
    db.commit()
    return detail(uuid, db, user)


@router.post("/probations/{uuid}/resume")
def resume(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _m(db, user)
    service.resume(db, service.get(db, user["company_id"], uuid))
    db.commit()
    return detail(uuid, db, user)


@router.post("/probations/{uuid}/cancel")
def cancel(uuid: UUID, body: ReasonIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _m(db, user)
    service.cancel(db, service.get(db, user["company_id"], uuid), body.reason)
    db.commit()
    return detail(uuid, db, user)


@router.post("/probations/{uuid}/end-date")
def end_date(uuid: UUID, body: EndDateIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _m(db, user)
    service.change_end_date(db, service.get(db, user["company_id"], uuid), body.end_date, body.reason)
    db.commit()
    return detail(uuid, db, user)


@router.post("/probations/bulk")
def bulk(body: BulkIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _m(db, user)
    done, failed = 0, []
    for u in body.uuids:
        try:
            with db.begin_nested():
                p = service.get(db, user["company_id"], u)
                if body.action == "start_review":
                    service.start_review(db, p)
                elif body.action == "confirm":
                    service.decide(db, p, "confirm", {"reason": body.reason or "Bulk confirmation"})
                elif body.action == "cancel":
                    service.cancel(db, p, body.reason)
                elif body.action == "remind":
                    emp = c.employee(db, p.company_id, p.employee_id)
                    for r in p.reviews:
                        if r.status == "pending":
                            c.notify(db, p.company_id, r.reviewer_employee_id,
                                     f"Reminder: review {emp.first_name} {emp.last_name}'s probation.", "probation")
                else:
                    raise BadRequest("Unknown action.")
                done += 1
        except Exception as exc:
            failed.append({"uuid": str(u), "reason": getattr(exc, "detail", "Could not be changed")})
    db.commit()
    return {"done": done, "failed": failed}


@router.post("/run")
def run_now(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Start the reviews that are due now (the background job does this every 15 minutes)."""
    _m(db, user)
    cid = user["company_id"]
    n = service.ensure_records(db, cid)
    t0 = c.today(db, cid)
    from datetime import timedelta
    for p in (db.query(Probation).options(joinedload(Probation.policy), joinedload(Probation.reviews))
              .filter(Probation.company_id == cid, Probation.status.in_(("active", "extended"))).all()):
        if p.end_date - timedelta(days=p.policy.review_before_days if p.policy else 15) <= t0:
            service.start_review(db, p)
            n += 1
    db.commit()
    return {"started": n}


# ── Reviewer's own list ─────────────────────────────────────────────

@router.get("/my-reviews")
def my_reviews(status_: str | None = Query("pending", alias="status"), db: Session = Depends(get_db),
               user: dict = Depends(get_current_user)):
    eid = user.get("employee_id")
    if not eid:
        return {"items": []}
    qr = (db.query(ProbationReview).options(joinedload(ProbationReview.probation).joinedload(Probation.policy),
                                            joinedload(ProbationReview.probation).joinedload(Probation.reviews))
          .filter(ProbationReview.company_id == user["company_id"], ProbationReview.reviewer_employee_id == eid))
    if status_:
        qr = qr.filter(ProbationReview.status.in_(status_.split(",")))
    rows = qr.order_by(ProbationReview.created_at.desc()).limit(200).all()
    out = []
    for r in rows:
        p = service.out(db, r.probation)
        out.append({**service.review_out(r), "probation": p})
    return {"items": out}


@router.post("/reviews/{uuid}")
def submit_review(uuid: UUID, body: ReviewIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    r = (db.query(ProbationReview).options(joinedload(ProbationReview.probation).joinedload(Probation.reviews),
                                           joinedload(ProbationReview.probation).joinedload(Probation.policy))
         .filter(ProbationReview.company_id == user["company_id"], ProbationReview.uuid == uuid).first())
    if r is None:
        raise NotFound("Review")
    service.submit_review(db, user, r, body.model_dump(exclude_unset=True))
    db.commit()
    return service.review_out(r)
