"""Loans API under /api/v1/loans.

Types     GET/POST /loans/types, PUT /loans/types/{uuid}
Loans     GET /loans?scope=mine|all&status=, POST /loans (employee request, or HR for someone with employee_uuid)
          POST /loans/preview, GET /loans/{uuid}, POST /loans/{uuid}/disburse | /skip | /prepay
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.db import get_db
from core.errors import BadRequest, Forbidden
from core.security import _get_role_permissions, get_current_user, require_permission
from core.timeutil import today_for_company_id
from modules.loans import service
from modules.loans.models import Loan, LoanType
from modules.payroll import calc
from modules.payroll import service as psvc

router = APIRouter(prefix="/loans", tags=["Loans"])
MANAGE = require_permission("loans.manage")


def _can_manage(db: Session, user: dict) -> bool:
    return "loans.manage" in _get_role_permissions(user["company_id"], user["role"], db)


@router.get("/access")
def access(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    return {"can_manage": _can_manage(db, user), "is_employee": bool(user.get("employee_id"))}


class TypeIn(BaseModel):
    code: str | None = None
    name: str | None = None
    kind: str | None = None
    description: str | None = None
    max_amount: Decimal | None = None
    max_salary_multiple: Decimal | None = None
    max_instalments: int | None = None
    interest_rate: Decimal | None = None
    interest_method: str | None = None
    min_service_months: int | None = None
    eligibility: str | None = None
    max_active: int | None = None
    medical: bool | None = None
    active: bool | None = None


@router.get("/types")
def list_types(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    return [service.type_out(t) for t in service.types(db, user["company_id"], active_only=not _can_manage(db, user))]


@router.post("/types", status_code=201)
def create_type(body: TypeIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    t = service.save_type(db, user["company_id"], body.model_dump(exclude_unset=True))
    db.commit()
    return service.type_out(t)


@router.put("/types/{uuid}")
def update_type(uuid: UUID, body: TypeIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    t = service.get_type(db, user["company_id"], uuid)
    data = body.model_dump(exclude_unset=True)
    data.pop("code", None)
    service.save_type(db, user["company_id"], data, t)
    db.commit()
    return service.type_out(t)


@router.get("")
def list_loans(scope: str = "mine", status: str | None = None, user: dict = Depends(get_current_user),
               db: Session = Depends(get_db)):
    cid = user["company_id"]
    q = db.query(Loan, LoanType).join(LoanType, LoanType.id == Loan.loan_type_id).filter(Loan.company_id == cid)
    if scope == "all":
        if not _can_manage(db, user):
            raise Forbidden("Only HR can see everyone's loans.")
    else:
        if not user.get("employee_id"):
            return {"items": []}
        q = q.filter(Loan.employee_id == user["employee_id"])
    if status:
        q = q.filter(Loan.status.in_(status.split(",")))
    rows = q.order_by(Loan.created_at.desc()).all()
    E = psvc.employee_model()
    names = {e.id: psvc.name_of(e) for e in db.query(E).filter(E.id.in_([x.employee_id for x, _ in rows] or [-1])).all()}
    return {"items": [service.loan_out(db, x, t, names) for x, t in rows]}


class PreviewIn(BaseModel):
    loan_type_uuid: UUID
    amount: Decimal
    instalments: int
    start_month: date | None = None
    employee_uuid: UUID | None = None


@router.post("/preview")
def preview(body: PreviewIn, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    t = service.get_type(db, cid, body.loan_type_uuid)
    today = today_for_company_id(db, cid)
    start = (body.start_month or calc.add_months(today.replace(day=1), 1)).replace(day=1)
    out = service.preview(body.amount, t.interest_rate, body.instalments, start, t.interest_method)
    emp = None
    if body.employee_uuid and _can_manage(db, user):
        emp = psvc.employee_by_uuid(db, cid, body.employee_uuid)
    elif user.get("employee_id"):
        emp = psvc.get_employee(db, cid, user["employee_id"])
    out["problems"] = service.check_eligibility(db, cid, emp, t, Decimal(body.amount), body.instalments, today) if emp else []
    out["interest_rate"] = str(t.interest_rate)
    return out


class LoanIn(BaseModel):
    loan_type_uuid: UUID
    amount: Decimal
    instalments: int
    start_month: date | None = None
    purpose: str | None = None
    employee_uuid: UUID | None = None
    interest_rate: Decimal | None = None
    override_eligibility: bool = False
    note: str | None = None


@router.post("", status_code=201)
def request_loan(body: LoanIn, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    t = service.get_type(db, cid, body.loan_type_uuid)
    by_hr = bool(body.employee_uuid) and _can_manage(db, user)
    if body.employee_uuid and not by_hr:
        raise Forbidden("Only HR can add a loan for someone else.")
    if by_hr:
        emp = psvc.employee_by_uuid(db, cid, body.employee_uuid)
    else:
        if not user.get("employee_id"):
            raise BadRequest("Choose the employee.")
        emp = psvc.get_employee(db, cid, user["employee_id"])
    loan = service.request_loan(db, cid, emp, t, body.model_dump(), today_for_company_id(db, cid), by_hr=by_hr)
    db.commit()
    return service.loan_out(db, loan, t, {emp.id: psvc.name_of(emp)}, full=True)


def _loan(db: Session, user: dict, uuid) -> Loan:
    x = service.get_loan(db, user["company_id"], uuid)
    if x.employee_id != user.get("employee_id") and not _can_manage(db, user):
        raise Forbidden("You can only see your own loans.")
    return x


@router.get("/{uuid}")
def get_loan(uuid: UUID, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    x = _loan(db, user, uuid)
    e = psvc.get_employee(db, user["company_id"], x.employee_id)
    out = service.loan_out(db, x, None, {e.id: psvc.name_of(e)}, full=True)
    out["can_manage"] = _can_manage(db, user)
    out["perquisite_monthly"] = str(service.perquisite_value(db, user["company_id"], x.employee_id,
                                                             today_for_company_id(db, user["company_id"])))
    return out


class DisburseIn(BaseModel):
    disbursed_on: date | None = None
    reference: str | None = None


@router.post("/{uuid}/disburse")
def disburse(uuid: UUID, body: DisburseIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = user["company_id"]
    x = service.get_loan(db, cid, uuid)
    service.disburse(db, cid, x, body.disbursed_on or today_for_company_id(db, cid), body.reference)
    db.commit()
    return service.loan_out(db, x, full=True)


class ActionIn(BaseModel):
    months: int | None = None
    amount: Decimal | None = None
    reason: str | None = None
    reference: str | None = None


@router.post("/{uuid}/skip")
def skip(uuid: UUID, body: ActionIn, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    x = _loan(db, user, uuid)
    service.request_action(db, user["company_id"], x, "skip", body.model_dump())
    db.commit()
    return service.loan_out(db, x, full=True)


@router.post("/{uuid}/prepay")
def prepay(uuid: UUID, body: ActionIn, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    x = _loan(db, user, uuid)
    service.request_action(db, user["company_id"], x, "prepay", body.model_dump())
    db.commit()
    db.refresh(x)
    return service.loan_out(db, x, full=True)
