"""Employee self-service for pay and tax under /api/v1/payroll/me.

GET  /me/summary                    current salary (CTC split), last payslip, YTD, tax snapshot
GET  /me/payslips                   published payslips (PDF at /payroll/payslips/{uuid}/pdf, password protected)
GET  /me/declaration?fy=            my declaration (created on first visit); PUT to save or submit
POST /me/declaration/proofs         upload a proof (multipart: item, amount, file)
POST /me/declaration/submit-proofs
GET  /me/tax/compare?fy=, /me/tax/worksheet?fy=
GET  /me/form16?fy=                 my Form 16 (once the year's pay runs are locked)
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.db import get_db
from core.errors import BadRequest
from core.security import get_current_user
from core.timeutil import today_for_company_id
from modules.payroll import calc, pdf, runs, structures, taxsvc
from modules.payroll import service as svc
from modules.payroll.calc import ZERO, d, money
from modules.payroll.models import PayRun, Payslip

router = APIRouter(prefix="/payroll/me", tags=["My pay"])


def _me(db: Session, user: dict):
    if not user.get("employee_id"):
        raise BadRequest("Sign in as an employee to see your own pay.")
    return svc.get_employee(db, user["company_id"], user["employee_id"])


@router.get("/summary")
def summary(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    e = _me(db, user)
    today = today_for_company_id(db, cid)
    fy = calc.fy_of(today)
    s = svc.current_salary(db, cid, e.id, today)
    slips = (db.query(Payslip).filter(Payslip.company_id == cid, Payslip.employee_id == e.id, Payslip.published.is_(True))
             .order_by(Payslip.period_start.desc()).all())
    ytd = [p for p in slips if p.fy == fy]
    decl = taxsvc.declaration(db, cid, e.id, fy, create=False)
    snames = {x.id: x.name for x in structures.structures(db, cid, current_only=False)}
    return {"salary": svc.salary_out(s, snames.get(s.structure_id)) if s else None,
            "last_payslip": runs.payslip_row(slips[0]) if slips else None,
            "ytd": {"gross": money(sum((d(p.gross) for p in ytd), ZERO)), "net": money(sum((d(p.net) for p in ytd), ZERO)),
                    "tds": money(sum((d(((p.tax or {}).get("month") or {}).get("tds")) for p in ytd), ZERO)),
                    "pf": money(sum((d(((p.statutory or {}).get("pf") or {}).get("employee")) for p in ytd), ZERO))},
            "fy": fy, "regime": decl.regime if decl else svc.get_settings(db, cid)["tax"].get("default_regime", "new"),
            "declaration_status": decl.status if decl else "not_started",
            "windows": taxsvc.windows(svc.get_settings(db, cid), today, fy),
            "password_hint": pdf.password_hint((svc.get_settings(db, cid).get("payslip") or {}).get("password", "pan"))}


@router.get("/payslips")
def my_payslips(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    e = _me(db, user)
    rows = (db.query(Payslip, PayRun).join(PayRun, PayRun.id == Payslip.pay_run_id)
            .filter(Payslip.company_id == cid, Payslip.employee_id == e.id, Payslip.published.is_(True))
            .order_by(Payslip.period_start.desc(), Payslip.id.desc()).all())
    return {"items": [{**runs.payslip_row(p), "run_kind": r.kind, "run_title": r.title,
                       "pay_date": r.pay_date.isoformat() if r.pay_date else None} for p, r in rows]}


@router.get("/declaration")
def my_declaration(fy: str | None = None, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    e = _me(db, user)
    fy = fy or calc.fy_of(today_for_company_id(db, cid))
    x = taxsvc.declaration(db, cid, e.id, fy)
    db.commit()
    fbp_components = [structures.component_out(c) for c in structures.components(db, cid) if c.fbp and c.active]
    return {**taxsvc.declaration_out(x), "files": taxsvc.proof_files(db, cid, x), "fbp_components": fbp_components,
            "windows": taxsvc.windows(svc.get_settings(db, cid), today_for_company_id(db, cid), fy)}


class MyDeclarationIn(BaseModel):
    regime: str | None = None
    items: list[dict] | None = None
    hra: dict | None = None
    previous_employer: dict | None = None
    other_income: float | None = None
    extra_tds_monthly: float | None = None
    fbp: dict | None = None
    submit: bool = False


@router.put("/declaration")
def save_declaration(body: MyDeclarationIn, fy: str | None = None, user: dict = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    cid = user["company_id"]
    e = _me(db, user)
    x = taxsvc.declaration(db, cid, e.id, fy or calc.fy_of(today_for_company_id(db, cid)))
    taxsvc.save(db, cid, x, body.model_dump(exclude_unset=True), by_hr=False)
    db.commit()
    return taxsvc.declaration_out(x)


@router.post("/declaration/proofs", status_code=201)
async def add_proof(item: str = Form(...), amount: str | None = Form(None), fy: str | None = Form(None),
                    file: UploadFile = File(...), user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    e = _me(db, user)
    x = taxsvc.declaration(db, cid, e.id, fy or calc.fy_of(today_for_company_id(db, cid)))
    content = await file.read()
    f = taxsvc.add_proof(db, cid, x, item, file.filename or "proof", file.content_type or "application/octet-stream",
                         content, amount)
    db.commit()
    return {"uuid": str(f.uuid), "filename": f.filename}


@router.post("/declaration/submit-proofs")
def submit_proofs(fy: str | None = None, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    e = _me(db, user)
    x = taxsvc.declaration(db, cid, e.id, fy or calc.fy_of(today_for_company_id(db, cid)))
    taxsvc.submit_proofs(db, cid, x)
    db.commit()
    return taxsvc.declaration_out(x)


@router.get("/tax/compare")
def compare(fy: str | None = None, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    e = _me(db, user)
    out = taxsvc.compare(db, cid, e, fy or calc.fy_of(today_for_company_id(db, cid)))
    db.rollback()
    return out


@router.get("/tax/worksheet")
def worksheet(fy: str | None = None, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    e = _me(db, user)
    out = taxsvc.worksheet(db, cid, e, fy or calc.fy_of(today_for_company_id(db, cid)))
    db.rollback()
    return out


@router.get("/form16")
def my_form16(fy: str | None = None, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    e = _me(db, user)
    fy = fy or calc.fy_of(calc.add_months(today_for_company_id(db, cid), -12))
    content = taxsvc.form16_pdf(db, cid, e, fy)
    return Response(content=content, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="Form16-{fy}.pdf"'})


_ = UUID
