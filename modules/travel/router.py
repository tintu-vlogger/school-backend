"""Travel API under /api/v1/travel.

Access      GET  /travel/access
Options     GET  /travel/options (modes, classes, tiers), GET /travel/projects?q= (billable projects)
Policy      GET  /travel/policy (in force today + versions), GET /travel/policy/for-me,
            POST /travel/policy (new version from a date)
Trips       GET  /travel/trips?scope=mine|team|all&status=&q=&type=&outside_policy=
            POST /travel/trips (draft, or submit=true), POST /travel/trips/preview (estimate + policy check)
            GET/PUT /travel/trips/{uuid}, POST /travel/trips/{uuid}/submit | /cancel | /settle
Desk        POST /travel/trips/{uuid}/bookings/{leg|stay}/{item_uuid}, GET .../file
            POST /travel/trips/{uuid}/mark-booked | /desk-cancel | /pay-advance | /return-advance
Reports     GET  /travel/summary, GET /travel/reports/spend?by=&start=&end=, GET /travel/reports/breaches
"""
from __future__ import annotations

import base64
from datetime import date
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from core.db import get_db
from core.errors import BadRequest, Forbidden, NotFound
from core.security import _get_role_permissions, get_current_user, require_permission
from modules.payroll import service as psvc
from modules.travel import policy as pol
from modules.travel import service
from modules.travel.models import TravelRequest

router = APIRouter(prefix="/travel", tags=["Travel"])
DESK = require_permission("payroll.travel.manage")
POLICY = require_permission("payroll.travel.policy")


def _perms(db: Session, user: dict) -> list[str]:
    return _get_role_permissions(user["company_id"], user["role"], db)


def _is_desk(db: Session, user: dict) -> bool:
    return "payroll.travel.manage" in _perms(db, user)


def _me(db: Session, user: dict):
    if not user.get("employee_id"):
        raise BadRequest("Sign in as an employee to plan a trip.")
    return psvc.get_employee(db, user["company_id"], user["employee_id"])


def _team_ids(db: Session, user: dict) -> list[int]:
    if not user.get("employee_id"):
        return []
    E = psvc.employee_model()
    return [i for (i,) in db.query(E.id).filter(E.company_id == user["company_id"],
                                                E.reporting_manager_id == user["employee_id"]).all()]


@router.get("/access")
def access(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    perms = _perms(db, user)
    return {"is_desk": "payroll.travel.manage" in perms, "can_policy": "payroll.travel.policy" in perms,
            "is_employee": bool(user.get("employee_id")), "has_team": bool(_team_ids(db, user))}


@router.get("/options")
def options(user: dict = Depends(get_current_user)):
    return pol.options()


@router.get("/projects")
def projects(q: str | None = None, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    from modules.timesheets import service as ts
    rows = ts.projects_for(db, user["company_id"], user.get("employee_id"))
    out = [{"value": str(p.uuid), "label": p.name + (f" · {p.client.name}" if p.client else ""), "billable": p.billable}
           for p in rows]
    if q:
        out = [o for o in out if q.lower() in o["label"].lower()]
    return out[:50]


# ── Policy ──────────────────────────────────────────────────────────

class PolicyIn(BaseModel):
    name: str | None = None
    effective_from: date
    rules: dict
    change_note: str | None = None


@router.get("/policy")
def get_policy(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    today = service._today(db, cid)
    cur = service.policy_on(db, cid, today)
    return {"current": service.policy_out(cur, today, cur.id) if cur else None,
            "versions": [service.policy_out(p, today, cur.id if cur else None) for p in service.policies(db, cid)],
            "defaults": pol.default_rules()}


@router.get("/policy/for-me")
def my_policy(on: date | None = None, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """The band that applies to me: modes, classes, hotel limits and daily allowance."""
    cid = user["company_id"]
    emp = _me(db, user)
    _, rules = service.rules_on(db, cid, on or service._today(db, cid))
    grade = service._grade(db, emp)
    return {"grade": grade, "band": pol.band_for(rules, grade), "advance_max_percent": rules.get("advance_max_percent")}


@router.post("/policy", status_code=201)
def save_policy(body: PolicyIn, user: dict = Depends(POLICY), db: Session = Depends(get_db)):
    cid = user["company_id"]
    p = service.save_policy(db, cid, body.model_dump())
    db.commit()
    today = service._today(db, cid)
    cur = service.policy_on(db, cid, today)
    return service.policy_out(p, today, cur.id if cur else None)


# ── Trips ───────────────────────────────────────────────────────────

class LegIn(BaseModel):
    mode: str
    travel_class: str | None = None
    from_place: str
    to_place: str
    depart_date: date
    preferred_time: str | None = None
    distance_km: Decimal | None = None
    estimated_cost: Decimal | None = None
    notes: str | None = None


class StayIn(BaseModel):
    city: str
    check_in: date
    check_out: date
    nightly_cost: Decimal | None = None
    notes: str | None = None


class TripIn(BaseModel):
    purpose: str
    travel_type: str = "domestic"
    start_date: date
    end_date: date
    project_uuid: UUID | None = None
    billable: bool = False
    accommodation_needed: bool = True
    advance_amount: Decimal | None = None
    notes: str | None = None
    exception_reason: str | None = None
    legs: list[LegIn]
    stays: list[StayIn] = []
    submit: bool = False


def _out(db: Session, cid: int, t: TravelRequest, full: bool = True) -> dict:
    from modules.expenses import service as exp
    lk = service.lookups(db, cid, [t])
    return service.trip_out(t, names=lk["names"], projects=lk["projects"], depts=lk["depts"],
                            claim=exp.claim_by_id(db, cid, t.claim_id), advance=exp.advance_by_id(db, cid, t.advance_id),
                            full=full)


def _trip(db: Session, user: dict, uuid, write: bool = False) -> TravelRequest:
    t = service.get_trip(db, user["company_id"], uuid)
    mine = user.get("employee_id") == t.employee_id
    if write and not mine:
        raise Forbidden("Only the traveller can change a trip.")
    if not mine and not _is_desk(db, user):
        team = t.employee_id in _team_ids(db, user)
        if not team:
            from core.workflow.models import WorkflowTask
            if not (t.workflow_instance_id and db.query(WorkflowTask.id).filter(
                    WorkflowTask.instance_id == t.workflow_instance_id,
                    WorkflowTask.approver_employee_id == user.get("employee_id")).first()):
                raise NotFound("Trip")
    return t


@router.get("/trips")
def list_trips(scope: str = "mine", status: str | None = None, q: str | None = None, type: str | None = None,
               outside_policy: bool | None = None, needs_booking: bool = False, page: int = 1,
               size: int = Query(25, le=200), user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    qry = db.query(TravelRequest).options(selectinload(TravelRequest.legs), selectinload(TravelRequest.stays)) \
        .filter(TravelRequest.company_id == cid)
    if scope == "all":
        if not _is_desk(db, user):
            raise Forbidden("Only the travel desk can see every trip.")
        qry = qry.filter(TravelRequest.status != "draft")
    elif scope == "team":
        qry = qry.filter(TravelRequest.employee_id.in_(_team_ids(db, user) or [-1]), TravelRequest.status != "draft")
    else:
        qry = qry.filter(TravelRequest.employee_id == _me(db, user).id)
    if status:
        qry = qry.filter(TravelRequest.status.in_(status.split(",")))
    if type in ("domestic", "international"):
        qry = qry.filter(TravelRequest.travel_type == type)
    if outside_policy is not None:
        qry = qry.filter(TravelRequest.outside_policy.is_(outside_policy))
    if needs_booking:
        qry = qry.filter(TravelRequest.status == "approved")
    if q:
        qry = qry.filter(TravelRequest.purpose.ilike(f"%{q}%") | TravelRequest.number.ilike(f"%{q}%"))
    total = qry.count()
    rows = qry.order_by(TravelRequest.start_date.desc(), TravelRequest.id.desc()).offset((page - 1) * size).limit(size).all()
    service.sync(db, cid, rows)
    db.commit()
    lk = service.lookups(db, cid, rows)
    return {"items": [service.trip_out(r, names=lk["names"], projects=lk["projects"], depts=lk["depts"]) for r in rows],
            "total": total, "page": page, "size": size}


@router.post("/trips/preview")
def preview(body: TripIn, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Estimate and policy check of a trip being planned, without saving it."""
    cid = user["company_id"]
    emp = _me(db, user)
    p, rules = service.rules_on(db, cid, body.start_date)
    grade = service._grade(db, emp)
    trip = body.model_dump()
    if not body.accommodation_needed:
        trip["stays"] = []
    est = pol.estimate(rules, grade, trip)
    trip["estimated_total"] = est["total"]
    flags = pol.check(rules, grade, trip, service._today(db, cid))
    intl = body.travel_type == "international"
    return {"estimate": {k: (service.money(v) if isinstance(v, Decimal) else v) for k, v in est.items()},
            "flags": flags, "outside_policy": bool(flags), "grade": grade, "band": pol.band_for(rules, grade),
            "policy_version": p.version if p else None,
            "stay_tiers": [pol.city_tier(rules, s["city"], intl) for s in trip.get("stays") or []]}


@router.post("/trips", status_code=201)
def create_trip(body: TripIn, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    emp = _me(db, user)
    t = service.save_trip(db, cid, emp, body.model_dump())
    if body.submit:
        service.submit(db, cid, t, emp)
    db.commit()
    return _out(db, cid, t)


@router.get("/trips/{uuid}")
def get_trip(uuid: UUID, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    t = _trip(db, user, uuid)
    service.sync(db, cid, [t])
    db.commit()
    out = _out(db, cid, t)
    if t.workflow_instance_id:
        from core.workflow.models import WorkflowInstance
        from core.workflow.service import is_my_task
        inst = db.query(WorkflowInstance).filter(WorkflowInstance.id == t.workflow_instance_id).first()
        if inst:
            out["approval"] = {"status": inst.status, "steps": [
                {"uuid": str(x.uuid), "name": x.step_name, "status": x.status, "approver": x.approver_name,
                 "comment": x.comment, "mine": x.status == "pending" and is_my_task(x)}
                for x in sorted(inst.tasks, key=lambda y: y.id)]}
    out["is_desk"] = _is_desk(db, user)
    out["mine"] = user.get("employee_id") == t.employee_id
    return out


@router.put("/trips/{uuid}")
def update_trip(uuid: UUID, body: TripIn, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    t = _trip(db, user, uuid, write=True)
    emp = _me(db, user)
    service.save_trip(db, cid, emp, body.model_dump(), t)
    if body.submit:
        service.submit(db, cid, t, emp)
    db.commit()
    return _out(db, cid, t)


@router.post("/trips/{uuid}/submit")
def submit_trip(uuid: UUID, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    t = _trip(db, user, uuid, write=True)
    service.submit(db, cid, t, _me(db, user))
    db.commit()
    return _out(db, cid, t)


@router.post("/trips/{uuid}/cancel")
def cancel_trip(uuid: UUID, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    t = _trip(db, user, uuid, write=True)
    service.cancel(db, cid, t)
    db.commit()
    return _out(db, cid, t)


@router.post("/trips/{uuid}/settle")
def settle_trip(uuid: UUID, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Turn the trip into an expense claim (draft) filled with its journeys, hotels and
    daily allowance, set off against the advance."""
    cid = user["company_id"]
    t = _trip(db, user, uuid, write=True)
    service.settle(db, cid, t, _me(db, user))
    db.commit()
    return _out(db, cid, t)


# ── Travel desk ─────────────────────────────────────────────────────

class BookingIn(BaseModel):
    booking_status: str = "booked"
    paid_by: str = "company"
    vendor: str | None = None
    booking_ref: str | None = None
    booked_cost: Decimal | None = None
    file_name: str | None = None
    file_data: str | None = None
    remove_file: bool = False


@router.post("/trips/{uuid}/bookings/{kind}/{item_uuid}")
def record_booking(uuid: UUID, kind: str, item_uuid: UUID, body: BookingIn, user: dict = Depends(DESK),
                   db: Session = Depends(get_db)):
    cid = user["company_id"]
    t = service.get_trip(db, cid, uuid)
    service.record_booking(db, cid, t, kind, item_uuid, body.model_dump())
    db.commit()
    return _out(db, cid, t)


@router.get("/trips/{uuid}/bookings/{kind}/{item_uuid}/file")
def booking_file(uuid: UUID, kind: str, item_uuid: UUID, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    t = _trip(db, user, uuid)
    it = service._item(t, kind, item_uuid)
    if not it.file_data:
        raise NotFound("Ticket")
    head, b64 = it.file_data.split(",", 1)
    media = head[5:].split(";")[0]
    return Response(content=base64.b64decode(b64), media_type=media,
                    headers={"Content-Disposition": f'inline; filename="{(it.file_name or "ticket").replace(chr(34), "")}"'})


class NoteIn(BaseModel):
    reason: str | None = None
    reference: str | None = None
    paid_on: date | None = None


@router.post("/trips/{uuid}/mark-booked")
def mark_booked(uuid: UUID, user: dict = Depends(DESK), db: Session = Depends(get_db)):
    cid = user["company_id"]
    t = service.get_trip(db, cid, uuid)
    service.mark_booked(db, cid, t)
    db.commit()
    return _out(db, cid, t)


@router.post("/trips/{uuid}/desk-cancel")
def desk_cancel(uuid: UUID, body: NoteIn, user: dict = Depends(DESK), db: Session = Depends(get_db)):
    cid = user["company_id"]
    t = service.get_trip(db, cid, uuid)
    service.desk_cancel(db, cid, t, body.reason)
    db.commit()
    return _out(db, cid, t)


@router.post("/trips/{uuid}/pay-advance")
def pay_advance(uuid: UUID, body: NoteIn, user: dict = Depends(DESK), db: Session = Depends(get_db)):
    cid = user["company_id"]
    t = service.get_trip(db, cid, uuid)
    service.pay_advance(db, cid, t, body.paid_on or service._today(db, cid), body.reference)
    db.commit()
    return _out(db, cid, t)


@router.post("/trips/{uuid}/return-advance")
def return_advance(uuid: UUID, body: NoteIn, user: dict = Depends(DESK), db: Session = Depends(get_db)):
    cid = user["company_id"]
    t = service.get_trip(db, cid, uuid)
    service.sync(db, cid, [t])
    service.return_advance(db, cid, t, body.reference)
    db.commit()
    return _out(db, cid, t)


# ── Reports ─────────────────────────────────────────────────────────

def _range(db: Session, cid: int, start: date | None, end: date | None) -> tuple[date, date]:
    today = service._today(db, cid)
    if not start:
        start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)  # financial year start
    end = end or date(start.year + 1, 3, 31)
    if end < start:
        raise BadRequest("The end date is before the start date.")
    return start, end


@router.get("/summary")
def summary(user: dict = Depends(DESK), db: Session = Depends(get_db)):
    cid = user["company_id"]
    rows = dict(db.query(TravelRequest.status, func.count(TravelRequest.id))
                .filter(TravelRequest.company_id == cid).group_by(TravelRequest.status).all())
    today = service._today(db, cid)
    on_trip = db.query(func.count(TravelRequest.id)).filter(
        TravelRequest.company_id == cid, TravelRequest.status.in_(("approved", "booked")),
        TravelRequest.start_date <= today, TravelRequest.end_date >= today).scalar()
    from modules.expenses import service as exp
    adv_ids = [a for (a,) in db.query(TravelRequest.advance_id).filter(TravelRequest.company_id == cid,
                                                                      TravelRequest.advance_id.isnot(None)).all()]
    advs = exp.advances_by_ids(db, cid, adv_ids).values()
    to_pay = sum((pol.d(a.amount) for a in advs if a.status == "approved"), Decimal("0"))
    return {"by_status": rows, "to_book": rows.get("approved", 0), "pending": rows.get("pending", 0),
            "on_trip_today": on_trip, "advances_to_pay": service.money(to_pay)}


@router.get("/reports/spend")
def spend(by: str = "department", start: date | None = None, end: date | None = None, user: dict = Depends(DESK),
          db: Session = Depends(get_db)):
    cid = user["company_id"]
    a, b = _range(db, cid, start, end)
    return {"start": a.isoformat(), "end": b.isoformat(), **service.spend_report(db, cid, a, b, by)}


@router.get("/reports/breaches")
def breaches(start: date | None = None, end: date | None = None, user: dict = Depends(DESK), db: Session = Depends(get_db)):
    cid = user["company_id"]
    a, b = _range(db, cid, start, end)
    return {"start": a.isoformat(), "end": b.isoformat(), **service.breaches_report(db, cid, a, b)}
