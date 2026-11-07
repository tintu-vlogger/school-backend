"""Benefits API under /api/v1/benefits.

GET  /benefits/access
GET  /benefits/plans, POST /benefits/plans, GET/PUT /benefits/plans/{uuid}
GET  /benefits/plans/{uuid}/insurer-file?month=YYYY-MM-01[&format=json]   additions and deletions for the insurer
GET  /benefits/enrolments?plan=&status=&q=, POST /benefits/enrolments (HR enrols someone)
PUT  /benefits/enrolments/{uuid}, POST /benefits/enrolments/{uuid}/terminate
POST /benefits/auto-enrol                       joiners in, leavers out (also runs on a schedule)
GET  /benefits/family/{employee_uuid}           family members HR can cover
GET  /benefits/me, POST /benefits/me/enrol, POST /benefits/me/waive   the employee's own cover
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from core.db import get_db
from core.errors import NotFound
from core.security import _get_role_permissions, get_current_user, require_permission
from core.timeutil import today_for_company_id
from modules.benefits import service as svc
from modules.benefits.models import BenefitEnrolment, BenefitPlan

router = APIRouter(prefix="/benefits", tags=["Benefits"])
MANAGE = require_permission("benefits.manage")


def _employee(db: Session, cid: int, uuid):
    E = svc._emp_model()
    e = db.query(E).filter(E.company_id == cid, E.uuid == uuid).first()
    if not e:
        raise NotFound("Employee")
    return e


@router.get("/access")
def access(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    p = set(_get_role_permissions(user["company_id"], user["role"], db))
    return {"manage": "benefits.manage" in p, "is_employee": bool(user.get("employee_id"))}


class PlanIn(BaseModel):
    name: str | None = None
    kind: str | None = None
    insurer: str | None = None
    policy_number: str | None = None
    tpa: str | None = None
    broker: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    website: str | None = None
    description: str | None = None
    policy_start: date | None = None
    policy_end: date | None = None
    sum_insured_basis: str | None = None
    sum_insured: Decimal | None = None
    salary_multiple: Decimal | None = None
    min_sum_insured: Decimal | None = None
    max_sum_insured: Decimal | None = None
    tiers: list[dict] | None = None
    age_bands: list[dict] | None = None
    employer_share_type: str | None = None
    employer_share_value: Decimal | None = None
    deduct_in_payroll: bool | None = None
    eligibility_rule: str | None = None
    waiting_days: int | None = None
    cover_starts: str | None = None
    mandatory: bool | None = None
    auto_enrol: bool | None = None
    joining_window_days: int | None = None
    renewal_window_from: date | None = None
    renewal_window_to: date | None = None
    active: bool | None = None


@router.get("/plans")
def list_plans(user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = user["company_id"]
    counts = svc.plan_counts(db, cid)
    today = today_for_company_id(db, cid)
    return [svc.plan_out(p, counts, today) for p in svc.plans(db, cid)]


@router.post("/plans", status_code=201)
def create_plan(body: PlanIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = user["company_id"]
    p = svc.save_plan(db, cid, body.model_dump(exclude_unset=True))
    db.commit()
    return svc.plan_out(p, today=today_for_company_id(db, cid))


@router.get("/plans/{uuid}")
def get_plan(uuid: UUID, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = user["company_id"]
    return svc.plan_out(svc.get_plan(db, cid, uuid), svc.plan_counts(db, cid), today_for_company_id(db, cid))


@router.put("/plans/{uuid}")
def update_plan(uuid: UUID, body: PlanIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = user["company_id"]
    p = svc.save_plan(db, cid, body.model_dump(exclude_unset=True), svc.get_plan(db, cid, uuid))
    db.commit()
    return svc.plan_out(p, svc.plan_counts(db, cid), today_for_company_id(db, cid))


@router.get("/plans/{uuid}/insurer-file")
def insurer_file(uuid: UUID, month: date, format: str = "xlsx", user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = user["company_id"]
    p = svc.get_plan(db, cid, uuid)
    if format == "json":
        rows = svc.insurer_rows(db, cid, p, month)
        db.rollback()
        return {"header": svc.INSURER_COLUMNS, "rows": rows,
                "additions": sum(1 for r in rows if r[0] == "Addition"), "deletions": sum(1 for r in rows if r[0] == "Deletion")}
    content = svc.insurer_file(db, cid, p, month)
    db.commit()
    name = f"{(p.policy_number or p.name).replace(' ', '-')}-{month:%Y-%m}-endorsements.xlsx"
    return Response(content=content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/enrolments")
def list_enrolments(plan: UUID | None = None, status: str | None = None, q: str | None = None, page: int = 1,
                    size: int = Query(25, le=200), user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = user["company_id"]
    E = svc._emp_model()
    qq = (db.query(BenefitEnrolment, BenefitPlan, E).join(BenefitPlan, BenefitPlan.id == BenefitEnrolment.plan_id)
          .join(E, E.id == BenefitEnrolment.employee_id).filter(BenefitEnrolment.company_id == cid))
    if plan:
        qq = qq.filter(BenefitPlan.uuid == plan)
    if status:
        qq = qq.filter(BenefitEnrolment.status.in_(status.split(",")))
    if q:
        like = f"%{q.strip()}%"
        qq = qq.filter(or_(func.concat_ws(" ", E.first_name, E.last_name).ilike(like), E.employee_code.ilike(like)))
    total = qq.count()
    rows = qq.order_by(E.first_name, BenefitPlan.name).offset((page - 1) * size).limit(size).all()
    return {"items": [svc.enrolment_out(x, p, e) for x, p, e in rows], "total": total, "page": page, "size": size}


class EnrolIn(BaseModel):
    plan_uuid: UUID
    employee_uuid: UUID | None = None
    tier: str | None = None
    dependent_ids: list[int] | None = None


@router.post("/enrolments", status_code=201)
def hr_enrol(body: EnrolIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = user["company_id"]
    p = svc.get_plan(db, cid, body.plan_uuid)
    e = _employee(db, cid, body.employee_uuid)
    x = svc.enrol(db, cid, p, e, tier=body.tier, dependent_ids=body.dependent_ids, source="hr", by_hr=True)
    db.commit()
    return svc.enrolment_out(x, p, e)


@router.put("/enrolments/{uuid}")
def hr_change(uuid: UUID, body: EnrolIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = user["company_id"]
    x = svc.get_enrolment(db, cid, uuid)
    p = db.query(BenefitPlan).filter(BenefitPlan.id == x.plan_id).first()
    e = svc._emp_model()
    emp = db.query(e).filter(e.id == x.employee_id).first()
    x = svc.enrol(db, cid, p, emp, tier=body.tier, dependent_ids=body.dependent_ids, source="hr", by_hr=True)
    db.commit()
    return svc.enrolment_out(x, p, emp)


class EndIn(BaseModel):
    end_date: date | None = None
    reason: str | None = None


@router.post("/enrolments/{uuid}/terminate")
def hr_terminate(uuid: UUID, body: EndIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = user["company_id"]
    x = svc.get_enrolment(db, cid, uuid)
    svc.terminate(db, cid, x, body.end_date or today_for_company_id(db, cid), body.reason)
    db.commit()
    p = db.query(BenefitPlan).filter(BenefitPlan.id == x.plan_id).first()
    E = svc._emp_model()
    return svc.enrolment_out(x, p, db.query(E).filter(E.id == x.employee_id).first())


@router.post("/auto-enrol")
def run_auto_enrol(user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    out = svc.auto_enrol(db, user["company_id"])
    db.commit()
    return out


@router.get("/family/{employee_uuid}")
def employee_family(employee_uuid: UUID, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = user["company_id"]
    return svc.family(db, cid, _employee(db, cid, employee_uuid).id)


# ── The employee's own cover ───────────────────────────────────────

def _me(db: Session, user: dict):
    E = svc._emp_model()
    return db.query(E).filter(E.company_id == user["company_id"], E.id == svc.require_own(user)).first()


@router.get("/me")
def my_benefits(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    return svc.my_benefits(db, user["company_id"], _me(db, user))


class MyEnrolIn(BaseModel):
    plan_uuid: UUID
    tier: str | None = None
    dependent_ids: list[int] | None = None


@router.post("/me/enrol")
def my_enrol(body: MyEnrolIn, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    e = _me(db, user)
    p = svc.get_plan(db, cid, body.plan_uuid)
    svc.enrol(db, cid, p, e, tier=body.tier, dependent_ids=body.dependent_ids, source="employee", by_hr=False)
    db.commit()
    return svc.my_benefits(db, cid, e)


class WaiveIn(BaseModel):
    plan_uuid: UUID
    reason: str


@router.post("/me/waive")
def my_waive(body: WaiveIn, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    e = _me(db, user)
    svc.waive(db, cid, svc.get_plan(db, cid, body.plan_uuid), e, body.reason, by_hr=False)
    db.commit()
    return svc.my_benefits(db, cid, e)
