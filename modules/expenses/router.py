"""Expenses API under /api/v1/expenses.

Categories  GET/POST /expenses/categories, PUT /expenses/categories/{uuid}
Claims      GET /expenses/claims?scope=mine|all&status=&q=, POST /expenses/claims (draft),
            GET/PUT /expenses/claims/{uuid}, POST /expenses/claims/{uuid}/submit | /withdraw | /pay,
            POST /expenses/claims/{uuid}/lines/{line_uuid}/approve-amount, GET .../lines/{line_uuid}/receipt
            POST /expenses/claims/preview-limits (limits that apply to me)
Advances    GET/POST /expenses/advances, POST /expenses/advances/{uuid}/pay
Summary     GET /expenses/summary
"""
from __future__ import annotations

import base64
import re
from datetime import date
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from core.db import get_db
from core.errors import BadRequest, Forbidden, NotFound
from core.security import _get_role_permissions, get_current_user, require_permission
from core.timeutil import today_for_company_id
from modules.expenses import service
from modules.expenses.models import ExpenseAdvance, ExpenseClaim
from modules.payroll import service as psvc
from modules.payroll.calc import ZERO, d, money

router = APIRouter(prefix="/expenses", tags=["Expenses"])
MANAGE = require_permission("expenses.manage")


def _can_manage(db: Session, user: dict) -> bool:
    return "expenses.manage" in _get_role_permissions(user["company_id"], user["role"], db)


def _me(db: Session, user: dict):
    if not user.get("employee_id"):
        raise BadRequest("Sign in as an employee to claim expenses.")
    return psvc.get_employee(db, user["company_id"], user["employee_id"])


def _names(db: Session, cid: int, ids) -> dict:
    E = psvc.employee_model()
    return {e.id: psvc.name_of(e) for e in db.query(E).filter(E.company_id == cid, E.id.in_(list(ids) or [-1])).all()}


@router.get("/access")
def access(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    return {"can_manage": _can_manage(db, user), "is_employee": bool(user.get("employee_id"))}


# ── Categories ──────────────────────────────────────────────────────

class CategoryIn(BaseModel):
    code: str | None = None
    name: str | None = None
    icon: str | None = None
    description: str | None = None
    kind: str | None = None
    limits: list[dict] | None = None
    default_limit: Decimal | None = None
    monthly_limit: Decimal | None = None
    receipt_required_above: Decimal | None = None
    mileage_rates: dict | None = None
    gst_applicable: bool | None = None
    taxable: bool | None = None
    pay_via: str | None = None
    component_code: str | None = None
    gl_code: str | None = None
    sort_order: int | None = None
    active: bool | None = None


@router.get("/categories")
def list_categories(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    rows = service.categories(db, cid, active_only=not _can_manage(db, user))
    out = [service.category_out(c) for c in rows]
    if user.get("employee_id"):
        emp = psvc.get_employee(db, cid, user["employee_id"])
        for o, c in zip(out, rows):
            lim = service.limit_for(db, c, emp)
            o["my_limit"] = {k: (money(v) if v is not None else None) for k, v in lim.items()}
    return out


@router.post("/categories", status_code=201)
def create_category(body: CategoryIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    c = service.save_category(db, user["company_id"], body.model_dump(exclude_unset=True))
    db.commit()
    return service.category_out(c)


@router.put("/categories/{uuid}")
def update_category(uuid: UUID, body: CategoryIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    c = service.get_category(db, user["company_id"], uuid)
    data = body.model_dump(exclude_unset=True)
    data.pop("code", None)
    service.save_category(db, user["company_id"], data, c)
    db.commit()
    return service.category_out(c)


# ── Claims ──────────────────────────────────────────────────────────

@router.get("/claims")
def list_claims(scope: str = "mine", status: str | None = None, q: str | None = None, page: int = 1,
                size: int = Query(25, le=200), user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    qry = db.query(ExpenseClaim).filter(ExpenseClaim.company_id == cid)
    if scope == "all":
        if not _can_manage(db, user):
            raise Forbidden("Only HR and finance can see everyone's claims.")
    else:
        qry = qry.filter(ExpenseClaim.employee_id == _me(db, user).id)
    if status:
        qry = qry.filter(ExpenseClaim.status.in_(status.split(",")))
    if q:
        qry = qry.filter(ExpenseClaim.title.ilike(f"%{q}%") | ExpenseClaim.number.ilike(f"%{q}%"))
    total = qry.count()
    rows = qry.order_by(ExpenseClaim.created_at.desc()).offset((page - 1) * size).limit(size).all()
    names = _names(db, cid, {r.employee_id for r in rows})
    return {"items": [service.claim_out(r, names) for r in rows], "total": total, "page": page, "size": size}


def _claim(db: Session, user: dict, uuid, write: bool = False) -> ExpenseClaim:
    c = service.get_claim(db, user["company_id"], uuid)
    mine = user.get("employee_id") == c.employee_id
    if not mine and not _can_manage(db, user):
        from core.workflow.models import WorkflowTask
        if not (c.workflow_instance_id and db.query(WorkflowTask.id).filter(
                WorkflowTask.instance_id == c.workflow_instance_id,
                WorkflowTask.approver_employee_id == user.get("employee_id")).first()):
            raise Forbidden("You can only see your own claims.")
    if write and not mine:
        raise Forbidden("Only the person claiming can change a claim.")
    return c


class LineIn(BaseModel):
    category_uuid: UUID
    expense_date: date
    amount: Decimal | None = None
    description: str | None = None
    merchant: str | None = None
    distance_km: Decimal | None = None
    vehicle: str | None = None
    from_place: str | None = None
    to_place: str | None = None
    gstin: str | None = None
    invoice_number: str | None = None
    gst_amount: Decimal | None = None
    receipt_name: str | None = None
    receipt: str | None = None
    keep_receipt_of: UUID | None = None


class ClaimIn(BaseModel):
    title: str
    trip_from: date | None = None
    trip_to: date | None = None
    advance_uuid: UUID | None = None
    lines: list[LineIn]
    submit: bool = False


def _lines(c: ExpenseClaim | None, lines: list[LineIn]) -> list[dict]:
    old = {str(x.uuid): x for x in (c.lines if c else [])}
    out = []
    for ln in lines:
        x = ln.model_dump()
        if ln.receipt and not re.match(r"^data:(image/(png|jpe?g|webp|heic)|application/pdf);base64,", ln.receipt):
            raise BadRequest("Receipts must be a photo or a PDF.")
        if not ln.receipt and ln.keep_receipt_of and str(ln.keep_receipt_of) in old:
            x["receipt"], x["receipt_name"] = old[str(ln.keep_receipt_of)].receipt, old[str(ln.keep_receipt_of)].receipt_name
        out.append(x)
    return out


@router.post("/claims", status_code=201)
def create_claim(body: ClaimIn, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    emp = _me(db, user)
    data = body.model_dump()
    data["lines"] = _lines(None, body.lines)
    c = service.save_claim(db, cid, emp, data)
    if body.submit:
        service.submit(db, cid, c, emp)
    db.commit()
    return service.claim_out(c, {emp.id: psvc.name_of(emp)}, {x.id: x for x in service.categories(db, cid)}, full=True)


@router.get("/claims/{uuid}")
def get_claim(uuid: UUID, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    c = _claim(db, user, uuid)
    out = service.claim_out(c, _names(db, cid, {c.employee_id}), {x.id: x for x in service.categories(db, cid)}, full=True)
    if c.workflow_instance_id:
        from core.workflow.models import WorkflowInstance
        from core.workflow.service import is_my_task
        inst = db.query(WorkflowInstance).filter(WorkflowInstance.id == c.workflow_instance_id).first()
        if inst:
            out["approval"] = {"status": inst.status, "steps": [{"uuid": str(t.uuid), "name": t.step_name, "status": t.status,
                                                                 "approver": t.approver_name, "comment": t.comment,
                                                                 "mine": t.status == "pending" and is_my_task(t)}
                                                                for t in sorted(inst.tasks, key=lambda x: x.id)]}
    out["can_manage"] = _can_manage(db, user)
    return out


@router.put("/claims/{uuid}")
def update_claim(uuid: UUID, body: ClaimIn, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    c = _claim(db, user, uuid, write=True)
    emp = _me(db, user)
    data = body.model_dump()
    for k in ("advance_uuid", "trip_from", "trip_to"):  # keep what the form did not send (e.g. a trip's advance)
        if k not in body.model_fields_set:
            data.pop(k, None)
    data["lines"] = _lines(c, body.lines)
    service.save_claim(db, cid, emp, data, c)
    if body.submit:
        service.submit(db, cid, c, emp)
    db.commit()
    return service.claim_out(c, {emp.id: psvc.name_of(emp)}, {x.id: x for x in service.categories(db, cid)}, full=True)


@router.post("/claims/{uuid}/submit")
def submit_claim(uuid: UUID, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    c = _claim(db, user, uuid, write=True)
    service.submit(db, cid, c, _me(db, user))
    db.commit()
    return service.claim_out(c)


@router.post("/claims/{uuid}/withdraw")
def withdraw_claim(uuid: UUID, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    c = _claim(db, user, uuid, write=True)
    if c.status == "draft":
        c.status = "cancelled"
    elif c.status == "pending" and c.workflow_instance_id:
        from core.workflow import service as workflow
        from core.workflow.models import WorkflowInstance
        inst = db.query(WorkflowInstance).filter(WorkflowInstance.id == c.workflow_instance_id).first()
        workflow.cancel(db, cid, inst, "Withdrawn by the employee")
    else:
        raise BadRequest("Only a draft or pending claim can be withdrawn.")
    db.commit()
    return service.claim_out(c)


class LineAmountIn(BaseModel):
    amount: Decimal
    note: str | None = None


@router.post("/claims/{uuid}/lines/{line_uuid}/approve-amount")
def approve_amount(uuid: UUID, line_uuid: UUID, body: LineAmountIn, user: dict = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    cid = user["company_id"]
    c = _claim(db, user, uuid)
    if user.get("employee_id") == c.employee_id:
        raise Forbidden("You cannot approve your own claim.")
    service.adjust_line(db, cid, c, line_uuid, body.amount, body.note)
    db.commit()
    return service.claim_out(c, None, {x.id: x for x in service.categories(db, cid)}, full=True)


@router.get("/claims/{uuid}/lines/{line_uuid}/receipt")
def receipt(uuid: UUID, line_uuid: UUID, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    c = _claim(db, user, uuid)
    ln = next((x for x in c.lines if str(x.uuid) == str(line_uuid)), None)
    if not ln or not ln.receipt:
        raise NotFound("Receipt")
    head, b64 = ln.receipt.split(",", 1)
    media = head[5:].split(";")[0]
    return Response(content=base64.b64decode(b64), media_type=media,
                    headers={"Content-Disposition": f'inline; filename="{ln.receipt_name or "receipt"}"'})


class PayIn(BaseModel):
    paid_on: date | None = None
    reference: str | None = None


@router.post("/claims/{uuid}/pay")
def pay_claim(uuid: UUID, body: PayIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = user["company_id"]
    c = service.get_claim(db, cid, uuid)
    service.mark_paid_direct(db, cid, c, body.paid_on or today_for_company_id(db, cid), body.reference)
    db.commit()
    return service.claim_out(c)


# ── Advances ────────────────────────────────────────────────────────

class AdvanceIn(BaseModel):
    amount: Decimal
    purpose: str
    needed_by: date | None = None


@router.get("/advances")
def list_advances(scope: str = "mine", user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    q = db.query(ExpenseAdvance).filter(ExpenseAdvance.company_id == cid)
    if scope == "all":
        if not _can_manage(db, user):
            raise Forbidden("Only HR and finance can see everyone's advances.")
    else:
        q = q.filter(ExpenseAdvance.employee_id == _me(db, user).id)
    rows = q.order_by(ExpenseAdvance.created_at.desc()).all()
    names = _names(db, cid, {r.employee_id for r in rows})
    return {"items": [service.advance_out(r, names) for r in rows]}


@router.post("/advances", status_code=201)
def request_advance(body: AdvanceIn, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    emp = _me(db, user)
    a = service.request_advance(db, cid, emp, body.model_dump())
    db.commit()
    return service.advance_out(a, {emp.id: psvc.name_of(emp)})


@router.post("/advances/{uuid}/pay")
def pay_advance(uuid: UUID, body: PayIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = user["company_id"]
    a = service.get_advance(db, cid, uuid)
    service.pay_advance(db, cid, a, body.paid_on or today_for_company_id(db, cid), body.reference)
    db.commit()
    return service.advance_out(a)


@router.get("/summary")
def summary(user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = user["company_id"]
    rows = db.query(ExpenseClaim.status, func.count(ExpenseClaim.id), func.coalesce(func.sum(ExpenseClaim.total), 0)) \
        .filter(ExpenseClaim.company_id == cid).group_by(ExpenseClaim.status).all()
    by = {s: {"count": n, "amount": money(a)} for s, n, a in rows}
    adv = db.query(ExpenseAdvance).filter(ExpenseAdvance.company_id == cid, ExpenseAdvance.status == "paid").all()
    outstanding = sum((d(a.amount) - d(a.settled_amount) - d(a.recovered_amount) for a in adv), ZERO)
    return {"by_status": by, "advances_outstanding": money(outstanding)}
