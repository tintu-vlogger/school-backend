"""Payroll second pass API under /api/v1/payroll.

Salary revisions from pay reviews (review step):
GET  /revisions?status=, GET /revisions/{uuid} (with the CTC split and arrears estimate)
POST /revisions (enter one by hand), POST /revisions/{uuid}/apply | /reject, POST /revisions/apply-many

Flexible benefit plan:
GET  /fbp/claims?status=&fy=&q=, GET /fbp/claims/{uuid}, POST /fbp/claims/{uuid}/verify
GET  /fbp/declarations?fy=, POST /fbp/declarations/lock?fy=, GET /fbp/files/{uuid}
GET/PUT /me/fbp?fy=, GET/POST /me/fbp/claims, POST /me/fbp/claims/{uuid}/withdraw

Budgets:  GET /budgets?fy=&dimension=, PUT /budgets
Form 16 signing:  GET/PUT /tax/signing, POST/DELETE /tax/signing/certificate, POST /tax/form16/sign
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from core.db import get_db
from core.errors import BadRequest, Forbidden
from core.security import _get_role_permissions, get_current_user, require_permission
from core.timeutil import today_for_company_id
from modules.payroll import calc, fbp, revisions, signing, structures
from modules.payroll import service as svc
from modules.payroll.calc import ZERO, d, money
from modules.payroll.models import FbpClaim, FbpDeclaration, PayrollBudget, SalaryRevisionRequest, SalaryStructure

router = APIRouter(prefix="/payroll", tags=["Payroll: revisions, flexible benefits, budgets, signing"])

VIEW = require_permission("payroll.view")
SALARIES = require_permission("payroll.salaries.manage")
TAX = require_permission("payroll.tax.manage")
SETTINGS = require_permission("payroll.settings")


def _names(db: Session, cid: int, ids) -> dict:
    E = svc.employee_model()
    return {e.id: e for e in db.query(E).filter(E.company_id == cid, E.id.in_(list(ids) or [-1])).all()}


# ── Salary revisions ───────────────────────────────────────────────

def _rev_out(db: Session, cid: int, rows: list[SalaryRevisionRequest]) -> list[dict]:
    emps = _names(db, cid, {r.employee_id for r in rows})
    snames = {s.id: s.name for s in db.query(SalaryStructure).filter(SalaryStructure.company_id == cid).all()}
    return [revisions.out(r, emps.get(r.employee_id), snames.get(r.structure_id)) for r in rows]


@router.get("/revisions")
def list_revisions(status: str | None = None, q: str | None = None, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    cid = user["company_id"]
    qq = db.query(SalaryRevisionRequest).filter(SalaryRevisionRequest.company_id == cid)
    if status:
        qq = qq.filter(SalaryRevisionRequest.status.in_(status.split(",")))
    if q:
        E = svc.employee_model()
        like = f"%{q.strip()}%"
        ids = [e.id for e in db.query(E.id).filter(E.company_id == cid, or_(
            func.concat_ws(" ", E.first_name, E.last_name).ilike(like), E.employee_code.ilike(like))).all()]
        qq = qq.filter(SalaryRevisionRequest.employee_id.in_(ids or [-1]))
    rows = qq.order_by(SalaryRevisionRequest.status.desc(), SalaryRevisionRequest.effective_from.desc(),
                       SalaryRevisionRequest.id.desc()).limit(500).all()
    items = _rev_out(db, cid, rows)
    pending = [x for x in items if x["status"] == "pending"]
    return {"items": items, "summary": {"pending": len(pending),
                                        "arrears": money(sum((d(x["arrears_estimate"]) for x in pending), ZERO)),
                                        "yearly_change": money(sum((d(x["change"]) for x in pending), ZERO))}}


@router.get("/revisions/{uuid}")
def get_revision(uuid: UUID, structure_uuid: UUID | None = None, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    cid = user["company_id"]
    r = revisions.get(db, cid, uuid)
    out = _rev_out(db, cid, [r])[0]
    cur = svc.current_salary(db, cid, r.employee_id, r.effective_from)
    out["current_components"] = cur.components if cur else []
    try:
        out["estimate"] = revisions.estimate(db, cid, r, structure_uuid)
    except BadRequest as exc:
        out["estimate"] = None
        out["estimate_error"] = exc.detail if hasattr(exc, "detail") else str(exc)
    db.rollback()
    return out


class RevisionIn(BaseModel):
    employee_uuid: UUID
    new_ctc: Decimal
    effective_from: date
    reason: str | None = None
    note: str | None = None


@router.post("/revisions", status_code=201)
def create_revision(body: RevisionIn, user: dict = Depends(SALARIES), db: Session = Depends(get_db)):
    cid = user["company_id"]
    e = svc.employee_by_uuid(db, cid, body.employee_uuid)
    r = revisions.request(db, cid, e.id, body.new_ctc, body.effective_from, source="manual", reason=body.reason, note=body.note)
    db.commit()
    return _rev_out(db, cid, [r])[0]


class ApplyIn(BaseModel):
    structure_uuid: UUID | None = None
    note: str | None = None


@router.post("/revisions/{uuid}/apply")
def apply_revision(uuid: UUID, body: ApplyIn, user: dict = Depends(SALARIES), db: Session = Depends(get_db)):
    cid = user["company_id"]
    r = revisions.get(db, cid, uuid)
    revisions.apply(db, cid, r, structure_uuid=str(body.structure_uuid) if body.structure_uuid else None, note=body.note)
    db.commit()
    return _rev_out(db, cid, [r])[0]


class RejectIn(BaseModel):
    note: str


@router.post("/revisions/{uuid}/reject")
def reject_revision(uuid: UUID, body: RejectIn, user: dict = Depends(SALARIES), db: Session = Depends(get_db)):
    cid = user["company_id"]
    r = revisions.get(db, cid, uuid)
    revisions.reject(db, cid, r, body.note)
    db.commit()
    return _rev_out(db, cid, [r])[0]


class ManyIn(BaseModel):
    uuids: list[UUID]


@router.post("/revisions/apply-many")
def apply_many(body: ManyIn, user: dict = Depends(SALARIES), db: Session = Depends(get_db)):
    cid = user["company_id"]
    done, failed = 0, []
    for u in body.uuids:
        r = revisions.get(db, cid, u)
        try:
            with db.begin_nested():
                revisions.apply(db, cid, r)
            done += 1
        except BadRequest as exc:
            emp = svc.get_employee(db, cid, r.employee_id)
            failed.append({"employee": svc.name_of(emp), "reason": getattr(exc, "detail", str(exc))})
    db.commit()
    return {"applied": done, "failed": failed}


# ── Flexible benefits: HR ──────────────────────────────────────────

@router.get("/fbp/claims")
def hr_claims(status: str | None = None, fy: str | None = None, q: str | None = None, page: int = 1,
              size: int = Query(25, le=200), user: dict = Depends(TAX), db: Session = Depends(get_db)):
    cid = user["company_id"]
    qq = db.query(FbpClaim).filter(FbpClaim.company_id == cid)
    if status:
        qq = qq.filter(FbpClaim.status.in_(status.split(",")))
    if fy:
        qq = qq.filter(FbpClaim.fy == fy)
    if q:
        E = svc.employee_model()
        like = f"%{q.strip()}%"
        ids = [r[0] for r in db.query(E.id).filter(E.company_id == cid, or_(
            func.concat_ws(" ", E.first_name, E.last_name).ilike(like), E.employee_code.ilike(like))).all()]
        qq = qq.filter(FbpClaim.employee_id.in_(ids or [-1]))
    total = qq.count()
    rows = qq.order_by(FbpClaim.created_at.desc()).offset((page - 1) * size).limit(size).all()
    emps = _names(db, cid, {r.employee_id for r in rows})
    comps = fbp.fbp_components(db, cid)
    names = {k: svc.name_of(v) for k, v in emps.items()}
    waiting = db.query(func.count(FbpClaim.id), func.coalesce(func.sum(FbpClaim.amount), 0)).filter(
        FbpClaim.company_id == cid, FbpClaim.status == "submitted").first()
    return {"items": [fbp.claim_out(r, names, comps) for r in rows], "total": total, "page": page, "size": size,
            "waiting": {"count": waiting[0], "amount": money(waiting[1])}}


@router.get("/fbp/claims/{uuid}")
def hr_claim(uuid: UUID, user: dict = Depends(TAX), db: Session = Depends(get_db)):
    cid = user["company_id"]
    c = fbp.get_claim(db, cid, uuid)
    emp = svc.get_employee(db, cid, c.employee_id)
    out = fbp.claim_out(c, {emp.id: svc.name_of(emp)}, fbp.fbp_components(db, cid))
    out["ledger"] = fbp.summary(db, cid, c.employee_id, c.fy)
    db.commit()
    return out


class VerifyIn(BaseModel):
    approved_amount: Decimal
    comment: str | None = None


@router.post("/fbp/claims/{uuid}/verify")
def hr_verify(uuid: UUID, body: VerifyIn, user: dict = Depends(TAX), db: Session = Depends(get_db)):
    cid = user["company_id"]
    c = fbp.verify(db, cid, fbp.get_claim(db, cid, uuid), approved_amount=body.approved_amount, comment=body.comment)
    db.commit()
    emp = svc.get_employee(db, cid, c.employee_id)
    return fbp.claim_out(c, {emp.id: svc.name_of(emp)}, fbp.fbp_components(db, cid))


@router.get("/fbp/declarations")
def hr_declarations(fy: str | None = None, user: dict = Depends(TAX), db: Session = Depends(get_db)):
    cid = user["company_id"]
    fy = fy or calc.fy_of(today_for_company_id(db, cid))
    rows = db.query(FbpDeclaration).filter(FbpDeclaration.company_id == cid, FbpDeclaration.fy == fy).all()
    emps = _names(db, cid, {r.employee_id for r in rows})
    return {"fy": fy, "window": fbp.window(db, cid, fy),
            "components": [structures.component_out(c) for c in fbp.fbp_components(db, cid).values()],
            "items": [{**fbp.declaration_out(r), "employee_name": svc.name_of(emps.get(r.employee_id)),
                       "employee_code": emps[r.employee_id].employee_code if emps.get(r.employee_id) else None}
                      for r in rows if r.employee_id in emps]}


@router.post("/fbp/declarations/lock")
def hr_lock(fy: str, user: dict = Depends(TAX), db: Session = Depends(get_db)):
    n = fbp.lock_all(db, user["company_id"], fy)
    db.commit()
    return {"locked": n}


@router.get("/fbp/files/{uuid}")
def bill_file(uuid: UUID, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    f = fbp.bill_file(db, cid, uuid)
    perms = set(_get_role_permissions(cid, user["role"], db))
    if "payroll.tax.manage" not in perms and user.get("employee_id") != f.employee_id:
        raise Forbidden()
    return Response(content=f.content, media_type=f.content_type,
                    headers={"Content-Disposition": f'inline; filename="{f.filename}"'})


# ── Flexible benefits: the employee ────────────────────────────────

def _me(db: Session, user: dict):
    if not user.get("employee_id"):
        raise BadRequest("Sign in as an employee to see your flexible benefits.")
    return svc.get_employee(db, user["company_id"], user["employee_id"])


@router.get("/me/fbp")
def my_fbp(fy: str | None = None, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    e = _me(db, user)
    fy = fy or calc.fy_of(today_for_company_id(db, cid))
    fbp.declaration(db, cid, e.id, fy)
    out = fbp.summary(db, cid, e.id, fy)
    out["choices"] = [structures.component_out(c) for c in fbp.fbp_components(db, cid).values()]
    db.commit()
    return out


class MyFbpIn(BaseModel):
    allocation: dict
    submit: bool = False


@router.put("/me/fbp")
def save_my_fbp(body: MyFbpIn, fy: str | None = None, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    e = _me(db, user)
    fy = fy or calc.fy_of(today_for_company_id(db, cid))
    fbp.save_declaration(db, cid, fbp.declaration(db, cid, e.id, fy), body.allocation, submit=body.submit, by_hr=False)
    db.commit()
    return fbp.summary(db, cid, e.id, fy)


@router.get("/me/fbp/claims")
def my_claims(fy: str | None = None, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    e = _me(db, user)
    qq = db.query(FbpClaim).filter(FbpClaim.company_id == cid, FbpClaim.employee_id == e.id)
    if fy:
        qq = qq.filter(FbpClaim.fy == fy)
    comps = fbp.fbp_components(db, cid)
    return {"items": [fbp.claim_out(c, None, comps) for c in qq.order_by(FbpClaim.bill_date.desc(), FbpClaim.id.desc()).all()]}


@router.post("/me/fbp/claims", status_code=201)
async def my_claim(component_code: str = Form(...), bill_date: date = Form(...), amount: Decimal = Form(...),
                   bill_number: str | None = Form(None), vendor: str | None = Form(None),
                   description: str | None = Form(None), files: list[UploadFile] = File(...),
                   user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    e = _me(db, user)
    data = [(f.filename or "bill", f.content_type or "application/octet-stream", await f.read()) for f in files]
    c = fbp.submit_claim(db, cid, e.id, {"component_code": component_code, "bill_date": bill_date, "amount": amount,
                                         "bill_number": bill_number, "vendor": vendor, "description": description}, data)
    db.commit()
    return fbp.claim_out(c, None, fbp.fbp_components(db, cid))


@router.post("/me/fbp/claims/{uuid}/withdraw")
def my_withdraw(uuid: UUID, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    e = _me(db, user)
    c = fbp.get_claim(db, cid, uuid)
    fbp.withdraw(db, cid, c, e.id)
    db.commit()
    return fbp.claim_out(c, None, fbp.fbp_components(db, cid))


# ── Budgets ────────────────────────────────────────────────────────

@router.get("/budgets")
def list_budgets(fy: str | None = None, dimension: str = "department", user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    from modules.payroll import insights
    cid = user["company_id"]
    if dimension not in ("department", "cost_centre"):
        raise BadRequest("Choose departments or cost centres.")
    fy = fy or calc.fy_of(today_for_company_id(db, cid))
    names = insights.budget_names(db, cid, dimension)
    have = {b.ref_id: b for b in db.query(PayrollBudget).filter(PayrollBudget.company_id == cid, PayrollBudget.fy == fy,
                                                                 PayrollBudget.dimension == dimension).all()}
    actual = {r["name"]: r for r in insights.cost_vs_budget(db, cid, fy, dimension, today_for_company_id(db, cid))}
    items = []
    for ref_id, name in sorted(names.items(), key=lambda x: x[1] or ""):
        b = have.get(ref_id)
        a = actual.get(name) or {}
        items.append({"ref_id": ref_id, "name": name, "amount": money(b.amount) if b else None,
                      "headcount": b.headcount if b else None, "note": b.note if b else None,
                      "actual": a.get("actual", 0), "projected": a.get("projected", 0), "people": a.get("people", 0),
                      "status": a.get("status") if b else "No budget"})
    return {"fy": fy, "dimension": dimension, "items": items,
            "unassigned": actual.get("Not assigned")}


class BudgetItem(BaseModel):
    ref_id: int
    amount: Decimal | None = None
    headcount: int | None = None
    note: str | None = None


class BudgetsIn(BaseModel):
    fy: str
    dimension: str
    items: list[BudgetItem]


@router.put("/budgets")
def save_budgets(body: BudgetsIn, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    from modules.payroll import insights
    cid = user["company_id"]
    if body.dimension not in ("department", "cost_centre"):
        raise BadRequest("Choose departments or cost centres.")
    valid = insights.budget_names(db, cid, body.dimension)
    for it in body.items:
        if it.ref_id not in valid:
            raise BadRequest("Choose a department or cost centre of this company.")
        b = db.query(PayrollBudget).filter(PayrollBudget.company_id == cid, PayrollBudget.fy == body.fy,
                                           PayrollBudget.dimension == body.dimension, PayrollBudget.ref_id == it.ref_id).first()
        if it.amount is None or d(it.amount) <= 0:
            if b is not None:
                db.delete(b)
            continue
        if b is None:
            b = PayrollBudget(company_id=cid, fy=body.fy, dimension=body.dimension, ref_id=it.ref_id)
            db.add(b)
        b.amount, b.headcount, b.note = d(it.amount), it.headcount, it.note
    db.commit()
    return list_budgets(body.fy, body.dimension, user, db)


# ── Form 16 signing ────────────────────────────────────────────────

@router.get("/tax/signing")
def signing_info(user: dict = Depends(TAX), db: Session = Depends(get_db)):
    return signing.certificate(db, user["company_id"])


class SigningIn(BaseModel):
    signer_name: str | None = None
    signer_designation: str | None = None
    place: str | None = None


@router.put("/tax/signing")
def signing_details(body: SigningIn, user: dict = Depends(TAX), db: Session = Depends(get_db)):
    cid = user["company_id"]
    svc.update_settings(db, cid, {"signing": body.model_dump(exclude_unset=True)})
    db.commit()
    return signing.certificate(db, cid)


@router.post("/tax/signing/certificate")
async def upload_certificate(file: UploadFile = File(...), password: str = Form(...), signer_name: str | None = Form(None),
                             signer_designation: str | None = Form(None), place: str | None = Form(None),
                             user: dict = Depends(TAX), db: Session = Depends(get_db)):
    cid = user["company_id"]
    out = signing.upload_certificate(db, cid, file.filename or "certificate.pfx", await file.read(), password,
                                     signer_name=signer_name, signer_designation=signer_designation, place=place)
    db.commit()
    return out


@router.delete("/tax/signing/certificate")
def remove_certificate(user: dict = Depends(TAX), db: Session = Depends(get_db)):
    out = signing.remove_certificate(db, user["company_id"])
    db.commit()
    return out


class SignIn(BaseModel):
    fy: str
    password: str | None = None
    employee_uuids: list[UUID] | None = None
    place: str | None = None


@router.post("/tax/form16/sign")
def sign_form16(body: SignIn, user: dict = Depends(TAX), db: Session = Depends(get_db)):
    cid = user["company_id"]
    ids = [svc.employee_by_uuid(db, cid, u).id for u in body.employee_uuids or []] or None
    out = signing.sign_form16(db, cid, body.fy, password=body.password, employee_ids=ids, place=body.place)
    db.commit()
    return out

