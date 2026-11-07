"""Positions API (/positions), succession (/succession) and the internal job marketplace
(/internal-jobs)."""
from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from core.db import get_db
from core.errors import BadRequest, Forbidden
from core.pagination import PageParams, paginate
from core.security import get_current_user
from modules.positions import common as c
from modules.positions import marketplace as mkt
from modules.positions import service as svc
from modules.positions import succession as suc
from modules.positions.models import HeadcountPlan, InternalPosting, PositionRequest, TalentPool

router = APIRouter(prefix="/positions", tags=["Positions"])
succession_router = APIRouter(prefix="/succession", tags=["Succession"])
market_router = APIRouter(prefix="/internal-jobs", tags=["Internal job marketplace"])
extra_routers = [succession_router, market_router]


def viewer(db: Session = Depends(get_db), user: dict = Depends(get_current_user)) -> c.Viewer:
    return c.Viewer(db, user)


def _view(v: c.Viewer) -> None:
    v.require("view", "You don't have access to positions.")


def _manage(v: c.Viewer) -> None:
    v.require("manage", "Only HR can change positions.")


def _date(value, label: str, default: date | None = None) -> date:
    if value in (None, ""):
        if default is None:
            raise BadRequest(f"Pick the {label}.")
        return default
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise BadRequest(f"The {label} doesn't look like a date.")


# ── Meta and settings ───────────────────────────────────────────────

@router.get("/meta")
def meta(db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    s = c.get_settings(db, v.company_id)
    fy = c.fy_of(c.today(db, v.company_id))
    start = int(fy[:4])
    can_people = c.has_perm(db, v.user, "people.employees.manage")
    return {"enabled": s["enabled"], "marketplace_enabled": s["marketplace_enabled"],
            "require_position_on_hire": s["require_position_on_hire"],
            "can_view": v.view, "can_manage": v.manage, "can_succession": v.succession, "can_marketplace": v.marketplace,
            "can_pick": v.view or can_people, "has_employee": v.employee_id is not None,
            "statuses": svc.STATUSES, "fill_labels": svc.FILL_LABELS, "request_kinds": svc.REQUEST_KINDS,
            "plan_statuses": svc.PLAN_STATUSES, "readiness": suc.READINESS, "levels": suc.LEVELS,
            "action_kinds": suc.ACTION_KINDS, "action_statuses": suc.ACTION_STATUSES, "bench_labels": suc.BENCH_LABELS,
            "posting_statuses": mkt.POSTING_STATUSES, "application_statuses": mkt.APP_STATUSES, "kinds": mkt.KINDS,
            "work_modes": mkt.WORK_MODES, "financial_year": fy,
            "financial_years": [f"{y}-{(y + 1) % 100:02d}" for y in range(start - 1, start + 3)]}


@router.get("/settings")
def get_settings(db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    if not (v.manage or v.marketplace or v.succession):
        raise Forbidden("Only HR can see these settings.")
    return c.get_settings(db, v.company_id)


@router.put("/settings")
def put_settings(data: dict, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    if not (v.manage or v.marketplace or v.succession):
        raise Forbidden("Only HR can change these settings.")
    allowed = set(c.DEFAULT_SETTINGS)
    if not v.manage:
        allowed -= {"enabled", "allow_overfill", "require_position_on_hire", "sync_job_from_position", "enforce_headcount_plan"}
    if not v.marketplace and not v.manage:
        allowed -= {"marketplace_enabled", "min_months_in_role", "max_open_applications", "manager_consent_default", "match_threshold"}
    out = c.update_settings(db, v.company_id, {k: val for k, val in data.items() if k in allowed})
    db.commit()
    return out


@router.get("/summary")
def summary(db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _view(v)
    return svc.summary(db, v.company_id)


# ── Positions ───────────────────────────────────────────────────────

@router.get("/positions")
def list_positions(q: str | None = None, status_: str | None = Query(None, alias="status"), department_id: int | None = None,
                   fill: str | None = None, critical: bool | None = None, params: PageParams = Depends(),
                   db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _view(v)
    query = svc.list_query(db, v.company_id, q=q, status=status_, department_id=department_id, fill=fill, critical=critical)
    page = paginate(query, params)
    page["items"] = svc.outputs(db, v.company_id, page["items"])
    return page


@router.post("/positions", status_code=status.HTTP_201_CREATED)
def create_position(data: dict, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _manage(v)
    row = svc.save(db, v.company_id, data)
    db.commit()
    return svc.output(db, v.company_id, row)


@router.get("/positions/{uuid}")
def position_detail(uuid: UUID, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _view(v)
    return svc.output(db, v.company_id, svc.get(db, v.company_id, uuid))


@router.put("/positions/{uuid}")
def update_position(uuid: UUID, data: dict, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _manage(v)
    row = svc.save(db, v.company_id, data, svc.get(db, v.company_id, uuid))
    db.commit()
    return svc.output(db, v.company_id, row)


@router.post("/positions/{uuid}/close")
def close_position(uuid: UUID, data: dict | None = None, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _manage(v)
    row = svc.close(db, v.company_id, svc.get(db, v.company_id, uuid), (data or {}).get("reason"))
    db.commit()
    return svc.output(db, v.company_id, row)


@router.post("/positions/{uuid}/reopen")
def reopen_position(uuid: UUID, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _manage(v)
    row = svc.reopen(db, v.company_id, svc.get(db, v.company_id, uuid))
    db.commit()
    return svc.output(db, v.company_id, row)


@router.post("/positions/{uuid}/seats", status_code=status.HTTP_201_CREATED)
def assign_seat(uuid: UUID, data: dict, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    """Put someone in the position (new hire or job change). HR or people managers."""
    if not (v.manage or c.has_perm(db, v.user, "people.employees.manage")):
        raise Forbidden("Only HR can put people in positions.")
    row = svc.get(db, v.company_id, uuid)
    eid = data.get("employee_id")
    if not eid and data.get("employee_uuid"):
        from modules.employees.models import Employee
        eid = db.query(Employee.id).filter(Employee.company_id == v.company_id, Employee.uuid == data["employee_uuid"]).scalar()
    change_type = data.get("change_type") or "transfer"
    if change_type not in ("transfer", "promotion", "redesignation", "department_change", "manager_change", "grade_change", "other"):
        raise BadRequest("Pick what kind of change this is.")
    source = data.get("source") if data.get("source") in ("hire", "job_change", "manual") else "manual"
    seat = svc.assign(db, v.company_id, row, eid, _date(data.get("effective_date"), "start date", c.today(db, v.company_id)),
                      reason=data.get("reason"), change_type=change_type, source=source,
                      sync_job=data.get("sync_job"))
    db.commit()
    return {"seat_uuid": str(seat.uuid), "status": seat.status, "job_change": seat.job_change_id is not None,
            "position": svc.output(db, v.company_id, row)}


@router.post("/seats/{uuid}/release")
def release_seat(uuid: UUID, data: dict | None = None, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _manage(v)
    data = data or {}
    seat = svc.get_seat(db, v.company_id, uuid)
    svc.release(db, v.company_id, seat, _date(data.get("end_date"), "end date", c.today(db, v.company_id)), data.get("reason"))
    db.commit()
    return svc.output(db, v.company_id, svc.by_id(db, v.company_id, seat.position_id))


@router.post("/positions/{uuid}/hiring-request", status_code=status.HTTP_201_CREATED)
def hiring_request(uuid: UUID, data: dict | None = None, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _manage(v)
    out = svc.raise_hiring_request(db, v.user, svc.get(db, v.company_id, uuid), data or {})
    db.commit()
    return out


@router.get("/tree")
def tree(db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _view(v)
    return {"items": svc.tree(db, v.company_id)}


@router.get("/vacant")
def vacant(q: str | None = None, include: str | None = None, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    """Approved positions with a free seat, for the hire and job-change forms."""
    if not (v.view or c.has_perm(db, v.user, "people.employees.manage")):
        raise Forbidden("You don't have access to positions.")
    return {"enabled": svc.enabled(db, v.company_id), "items": svc.vacant_options(db, v.company_id, q, include)}


@router.get("/employees/{employee_id}/seat")
def employee_seat(employee_id: int, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    if not (v.view or v.employee_id == employee_id or c.has_perm(db, v.user, "people.employees.view")):
        raise Forbidden("You don't have access to positions.")
    seat = svc.current_seat(db, v.company_id, employee_id)
    if seat is None:
        return {"seat": None}
    p = svc.by_id(db, v.company_id, seat.position_id)
    return {"seat": {"uuid": str(seat.uuid), "start_date": seat.start_date.isoformat(),
                     "position": {"uuid": str(p.uuid), "code": p.code, "title": p.title}}}


# ── Headcount plans ─────────────────────────────────────────────────

@router.get("/headcount-plans")
def list_plans(financial_year: str | None = None, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _view(v)
    q = db.query(HeadcountPlan).filter(HeadcountPlan.company_id == v.company_id)
    if financial_year:
        q = q.filter(HeadcountPlan.financial_year == financial_year)
    return {"items": svc.plan_outputs(db, v.company_id, q.order_by(HeadcountPlan.financial_year.desc(), HeadcountPlan.id).all())}


@router.post("/headcount-plans", status_code=status.HTTP_201_CREATED)
def create_plan(data: dict, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _manage(v)
    row = svc.save_plan(db, v.company_id, data)
    if data.get("submit"):
        svc.submit_plan(db, v.company_id, row)
    db.commit()
    return svc.plan_outputs(db, v.company_id, [row])[0]


@router.put("/headcount-plans/{uuid}")
def update_plan(uuid: UUID, data: dict, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _manage(v)
    row = svc.save_plan(db, v.company_id, data, svc.get_plan(db, v.company_id, uuid))
    if data.get("submit"):
        svc.submit_plan(db, v.company_id, row)
    db.commit()
    return svc.plan_outputs(db, v.company_id, [row])[0]


@router.post("/headcount-plans/{uuid}/submit")
def submit_plan(uuid: UUID, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _manage(v)
    row = svc.submit_plan(db, v.company_id, svc.get_plan(db, v.company_id, uuid))
    db.commit()
    return svc.plan_outputs(db, v.company_id, [row])[0]


@router.post("/headcount-plans/{uuid}/withdraw")
def withdraw_plan(uuid: UUID, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _manage(v)
    row = svc.withdraw_plan(db, v.company_id, svc.get_plan(db, v.company_id, uuid))
    db.commit()
    return svc.plan_outputs(db, v.company_id, [row])[0]


# ── Position requests ───────────────────────────────────────────────

@router.get("/requests")
def list_requests(status_: str | None = Query(None, alias="status"), mine: bool = False, params: PageParams = Depends(),
                  db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _view(v)
    q = db.query(PositionRequest).filter(PositionRequest.company_id == v.company_id)
    if mine or not v.manage:
        q = q.filter(PositionRequest.requested_by_employee_id == (v.employee_id or 0)) if v.employee_id else q
    if status_:
        q = q.filter(PositionRequest.status.in_(status_.split(",")))
    page = paginate(q.order_by(PositionRequest.id.desc()), params)
    page["items"] = svc.request_outputs(db, v.company_id, page["items"])
    return page


@router.post("/requests", status_code=status.HTTP_201_CREATED)
def create_request(data: dict, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _view(v)
    row = svc.create_request(db, v, data)
    db.commit()
    db.refresh(row)
    return svc.request_outputs(db, v.company_id, [row])[0]


@router.post("/requests/{uuid}/cancel")
def cancel_request(uuid: UUID, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _view(v)
    row = svc.cancel_request(db, v, svc.get_request(db, v.company_id, uuid))
    db.commit()
    return svc.request_outputs(db, v.company_id, [row])[0]


# ── Succession ──────────────────────────────────────────────────────

def _succ(v: c.Viewer) -> None:
    v.require("succession", "Only HR can see succession plans.")


@succession_router.get("/plans")
def plans(q: str | None = None, status_: str = Query("active", alias="status"), bench: str | None = None,
          urgency: str | None = None, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _succ(v)
    return {"items": suc.list_plans(db, v.company_id, status=status_, q=q, bench=bench, urgency_filter=urgency)}


@succession_router.post("/plans", status_code=status.HTTP_201_CREATED)
def create_succession_plan(data: dict, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _succ(v)
    row = suc.save_plan(db, v.company_id, data)
    db.commit()
    db.refresh(row)
    return suc.plan_outputs(db, v.company_id, [row], detail=True)[0]


@succession_router.get("/plans/{uuid}")
def succession_plan(uuid: UUID, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _succ(v)
    return suc.plan_outputs(db, v.company_id, [suc.get_plan(db, v.company_id, uuid)], detail=True)[0]


@succession_router.put("/plans/{uuid}")
def update_succession_plan(uuid: UUID, data: dict, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _succ(v)
    row = suc.save_plan(db, v.company_id, data, suc.get_plan(db, v.company_id, uuid))
    db.commit()
    return suc.plan_outputs(db, v.company_id, [row], detail=True)[0]


@succession_router.post("/plans/{uuid}/archive")
def archive_succession_plan(uuid: UUID, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _succ(v)
    suc.archive_plan(db, v.company_id, suc.get_plan(db, v.company_id, uuid))
    db.commit()
    return {"ok": True}


@succession_router.post("/plans/{uuid}/candidates", status_code=status.HTTP_201_CREATED)
def add_candidate(uuid: UUID, data: dict, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _succ(v)
    plan = suc.get_plan(db, v.company_id, uuid)
    suc.add_candidate(db, v.company_id, plan, data)
    db.commit()
    db.refresh(plan)
    return suc.plan_outputs(db, v.company_id, [plan], detail=True)[0]


@succession_router.put("/candidates/{uuid}")
def update_candidate(uuid: UUID, data: dict, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _succ(v)
    x = suc.update_candidate(db, v.company_id, suc.get_candidate(db, v.company_id, uuid), data)
    db.commit()
    return suc.plan_outputs(db, v.company_id, [x.plan], detail=True)[0]


@succession_router.post("/candidates/{uuid}/remove")
def remove_candidate(uuid: UUID, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _succ(v)
    x = suc.get_candidate(db, v.company_id, uuid)
    suc.remove_candidate(db, v.company_id, x)
    db.commit()
    return suc.plan_outputs(db, v.company_id, [x.plan], detail=True)[0]


@succession_router.post("/candidates/{uuid}/actions", status_code=status.HTTP_201_CREATED)
def add_action(uuid: UUID, data: dict, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _succ(v)
    x = suc.get_candidate(db, v.company_id, uuid)
    a = suc.add_action(db, v.user, x, data)
    db.commit()
    return suc.action_outputs(db, v.company_id, [a])[0]


@succession_router.put("/actions/{uuid}")
def update_action(uuid: UUID, data: dict, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _succ(v)
    a = suc.update_action(db, v.company_id, suc.get_action(db, v.company_id, uuid), data)
    db.commit()
    return suc.action_outputs(db, v.company_id, [a])[0]


@succession_router.get("/bench")
def bench(db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _succ(v)
    return suc.bench_report(db, v.company_id)


@succession_router.get("/nine-box")
def nine_box(department_id: int | None = None, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _succ(v)
    return suc.nine_box(db, v.company_id, department_id)


@succession_router.get("/employees/{employee_id}/talent")
def employee_talent(employee_id: int, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _succ(v)
    return {"talent": suc.talent_snapshot(db, v.company_id, [employee_id]).get(employee_id),
            "successor_for": suc._successor_positions(db, v.company_id, [employee_id]).get(employee_id, []),
            "pools": suc._pool_names_by_employee(db, v.company_id, [employee_id]).get(employee_id, [])}


@succession_router.get("/options/courses")
def course_options(q: str | None = None, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _succ(v)
    try:
        from modules.learning.models import Course
    except ImportError:
        return []
    query = db.query(Course).filter(Course.company_id == v.company_id)
    if hasattr(Course, "status"):
        query = query.filter(Course.status == "published")
    if q:
        query = query.filter(Course.title.ilike(f"%{q}%"))
    return [{"value": r.id, "label": r.title} for r in query.order_by(Course.title).limit(30)]


@succession_router.get("/options/goals")
def goal_options(employee_id: int, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _succ(v)
    try:
        from modules.performance.models import Objective
    except ImportError:
        return []
    rows = (db.query(Objective).filter(Objective.company_id == v.company_id, Objective.owner_employee_id == employee_id,
                                       Objective.archived_at.is_(None)).order_by(Objective.id.desc()).limit(30))
    return [{"value": r.id, "label": r.title, "description": f"{float(r.progress or 0):g}% done"} for r in rows]


@succession_router.get("/pools")
def pools(db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _succ(v)
    rows = db.query(TalentPool).filter(TalentPool.company_id == v.company_id).order_by(TalentPool.active.desc(), TalentPool.name).all()
    return {"items": suc.pool_outputs(db, v.company_id, rows)}


@succession_router.post("/pools", status_code=status.HTTP_201_CREATED)
def create_pool(data: dict, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _succ(v)
    row = suc.save_pool(db, v.company_id, data)
    if data.get("employee_ids"):
        suc.add_members(db, v.company_id, row, data["employee_ids"])
    db.commit()
    db.refresh(row)
    return suc.pool_outputs(db, v.company_id, [row], detail=True)[0]


@succession_router.get("/pools/{uuid}")
def pool(uuid: UUID, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _succ(v)
    return suc.pool_outputs(db, v.company_id, [suc.get_pool(db, v.company_id, uuid)], detail=True)[0]


@succession_router.put("/pools/{uuid}")
def update_pool(uuid: UUID, data: dict, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _succ(v)
    row = suc.save_pool(db, v.company_id, data, suc.get_pool(db, v.company_id, uuid))
    db.commit()
    return suc.pool_outputs(db, v.company_id, [row], detail=True)[0]


@succession_router.post("/pools/{uuid}/members")
def add_pool_members(uuid: UUID, data: dict, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _succ(v)
    row = suc.get_pool(db, v.company_id, uuid)
    n = suc.add_members(db, v.company_id, row, list(data.get("employee_ids") or []), data.get("note"))
    db.commit()
    db.refresh(row)
    return {"added": n, "pool": suc.pool_outputs(db, v.company_id, [row], detail=True)[0]}


@succession_router.delete("/pools/{uuid}/members/{member_uuid}")
def remove_pool_member(uuid: UUID, member_uuid: UUID, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _succ(v)
    row = suc.get_pool(db, v.company_id, uuid)
    suc.remove_member(db, v.company_id, row, member_uuid)
    db.commit()
    db.refresh(row)
    return suc.pool_outputs(db, v.company_id, [row], detail=True)[0]


@succession_router.get("/pools/{uuid}/suggestions")
def pool_suggestions(uuid: UUID, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    _succ(v)
    return {"items": suc.pool_suggestions(db, v.company_id, suc.get_pool(db, v.company_id, uuid))}


# ── Internal job marketplace ────────────────────────────────────────

@market_router.get("/postings")
def postings(scope: str = "open", q: str | None = None, kind: str | None = None, status_: str | None = Query(None, alias="status"),
             params: PageParams = Depends(), db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    page = paginate(mkt.list_postings(db, v, scope=scope, q=q, kind=kind, status=status_), params)
    page["items"] = mkt.posting_outputs(db, v, page["items"])
    return page


@market_router.post("/postings", status_code=status.HTTP_201_CREATED)
def create_posting(data: dict, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    row = mkt.save_posting(db, v, data)
    if data.get("publish"):
        mkt.set_posting_status(db, v, row, "publish")
    db.commit()
    return mkt.posting_outputs(db, v, [row])[0]


@market_router.get("/postings/{uuid}")
def posting(uuid: UUID, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    row = mkt.get_posting(db, v.company_id, uuid)
    if row.status != "open" and not mkt.can_manage(v, row):
        mine = v.employee_id and db.query(mkt.InternalApplication.id).filter(
            mkt.InternalApplication.company_id == v.company_id, mkt.InternalApplication.posting_id == row.id,
            mkt.InternalApplication.employee_id == v.employee_id).first()
        if not mine:
            raise Forbidden("This posting is not open.")
    return mkt.posting_outputs(db, v, [row])[0]


@market_router.put("/postings/{uuid}")
def update_posting(uuid: UUID, data: dict, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    row = mkt.save_posting(db, v, data, mkt.get_posting(db, v.company_id, uuid))
    db.commit()
    return mkt.posting_outputs(db, v, [row])[0]


@market_router.post("/postings/{uuid}/status")
def posting_status(uuid: UUID, data: dict, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    row = mkt.set_posting_status(db, v, mkt.get_posting(db, v.company_id, uuid), data.get("action") or "")
    db.commit()
    return mkt.posting_outputs(db, v, [row])[0]


@market_router.get("/postings/{uuid}/applications")
def posting_applications(uuid: UUID, status_: str | None = Query(None, alias="status"), db: Session = Depends(get_db),
                         v: c.Viewer = Depends(viewer)):
    row = mkt.get_posting(db, v.company_id, uuid)
    rows = mkt.list_applications(db, v, posting=row, status=status_).all()
    return {"items": mkt.application_outputs(db, v.company_id, rows)}


@market_router.get("/postings/{uuid}/suggestions")
def posting_suggestions(uuid: UUID, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    row = mkt.get_posting(db, v.company_id, uuid)
    if not mkt.can_manage(v, row):
        raise Forbidden("Only HR or the hiring manager can see suggestions.")
    return {"items": mkt.suggested_people(db, v, row)}


@market_router.post("/postings/{uuid}/apply", status_code=status.HTTP_201_CREATED)
def apply(uuid: UUID, data: dict | None = None, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    a = mkt.apply(db, v, mkt.get_posting(db, v.company_id, uuid), (data or {}).get("note"))
    db.commit()
    return mkt.application_outputs(db, v.company_id, [a], for_manager=False)[0]


@market_router.get("/applications/mine")
def my_applications(db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    rows = mkt.list_applications(db, v, mine=True).all()
    return {"items": mkt.application_outputs(db, v.company_id, rows, for_manager=False)}


@market_router.get("/applications")
def applications(status_: str | None = Query(None, alias="status"), params: PageParams = Depends(),
                 db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    page = paginate(mkt.list_applications(db, v, status=status_), params)
    page["items"] = mkt.application_outputs(db, v.company_id, page["items"])
    return page


@market_router.post("/applications/{uuid}/withdraw")
def withdraw(uuid: UUID, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    a = mkt.withdraw(db, v, mkt.get_application(db, v.company_id, uuid))
    db.commit()
    return mkt.application_outputs(db, v.company_id, [a], for_manager=False)[0]


@market_router.post("/applications/{uuid}/decision")
def decision(uuid: UUID, data: dict, db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    eff = _date(data.get("effective_date"), "effective date") if data.get("effective_date") else None
    a = mkt.decide(db, v, mkt.get_application(db, v.company_id, uuid), data.get("action") or "", data.get("note"), eff)
    db.commit()
    return mkt.application_outputs(db, v.company_id, [a])[0]


@market_router.get("/requisitions")
def postable_requisitions(db: Session = Depends(get_db), v: c.Viewer = Depends(viewer)):
    """Approved hiring requests that are not yet posted inside the company."""
    v.require("marketplace", "Only HR can post internal jobs.")
    from modules.recruitment.models import Requisition
    posted = {i for (i,) in db.query(InternalPosting.requisition_id).filter(
        InternalPosting.company_id == v.company_id, InternalPosting.requisition_id.isnot(None),
        InternalPosting.status.in_(("draft", "open")))}
    rows = (db.query(Requisition).filter(Requisition.company_id == v.company_id, Requisition.status == "approved")
            .order_by(Requisition.id.desc()).limit(50).all())
    return [{"value": str(r.uuid), "label": f"{r.number or 'Request'} · {r.title}", "description": f"{r.openings} opening(s)"}
            for r in rows if r.id not in posted]
