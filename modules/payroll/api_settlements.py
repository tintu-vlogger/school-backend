"""Final settlement API under /api/v1/payroll/settlements.

GET  /settlements?status=          with the 2-working-day countdown
POST /settlements                  start one for an employee (worked out at once)
GET  /settlements/{uuid}           statement with workings
PUT  /settlements/{uuid}           change inputs (leave days, notice, extra lines) and recompute
POST /settlements/{uuid}/recompute | /submit | /pay | /cancel | /pay-run (off-cycle run; made on approval)
GET  /settlements/{uuid}/pdf       settlement statement / letter
GET  /settlements/candidates       employees leaving soon without a settlement
"""
from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core import audit
from core.errors import BadRequest
from core.db import get_db
from core.security import require_permission
from core.timeutil import today_for_company_id
from modules.payroll import settlement
from modules.payroll import service as svc
from modules.payroll.models import FinalSettlement

router = APIRouter(prefix="/payroll/settlements", tags=["Final settlements"])

VIEW = require_permission("payroll.view")
FNF = require_permission("payroll.settlements.manage")


def _out(db: Session, cid: int, s: FinalSettlement, full: bool = False) -> dict:
    emp = svc.get_employee(db, cid, s.employee_id)
    o = settlement.out(s, emp, today_for_company_id(db, cid), full=full)
    o["employee_uuid"] = str(emp.uuid)
    run = settlement.run_of(db, cid, s)
    o["pay_run"] = {"uuid": str(run.uuid), "number": run.number, "status": run.status, "title": run.title} if run else None
    if full and s.workflow_instance_id:
        from core.workflow.models import WorkflowInstance
        from core.workflow.service import is_my_task
        inst = db.query(WorkflowInstance).filter(WorkflowInstance.id == s.workflow_instance_id).first()
        if inst:
            o["approval"] = {"status": inst.status, "reference": inst.reference_no,
                             "steps": [{"uuid": str(t.uuid), "name": t.step_name, "status": t.status, "approver": t.approver_name,
                                        "mine": t.status == "pending" and is_my_task(t)} for t in inst.tasks]}
    return o


@router.get("")
def list_settlements(status: str | None = None, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    cid = user["company_id"]
    q = db.query(FinalSettlement).filter(FinalSettlement.company_id == cid)
    if status:
        q = q.filter(FinalSettlement.status.in_(status.split(",")))
    rows = q.order_by(FinalSettlement.due_date.desc()).all()
    return {"items": [_out(db, cid, s) for s in rows], "kinds": settlement.EXIT_KINDS}


@router.get("/candidates")
def candidates(user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    cid = user["company_id"]
    today = today_for_company_id(db, cid)
    Employee = svc.employee_model()
    have = {r[0] for r in db.query(FinalSettlement.employee_id).filter(FinalSettlement.company_id == cid,
                                                                       FinalSettlement.status != "cancelled").all()}
    rows = db.query(Employee).filter(Employee.company_id == cid, Employee.exit_date.isnot(None),
                                     Employee.exit_date >= today - timedelta(days=60)).all()
    return [{"employee_uuid": str(e.uuid), "name": svc.name_of(e), "employee_code": e.employee_code,
             "exit_date": e.exit_date.isoformat(), "resignation_date": e.resignation_date.isoformat() if e.resignation_date else None}
            for e in rows if e.id not in have]


class SettlementIn(BaseModel):
    employee_uuid: UUID | None = None
    exit_kind: str | None = None
    last_working_day: date | None = None
    resignation_date: date | None = None
    inputs: dict | None = None


@router.post("", status_code=201)
def create(body: SettlementIn, user: dict = Depends(FNF), db: Session = Depends(get_db)):
    cid = user["company_id"]
    e = svc.employee_by_uuid(db, cid, body.employee_uuid)
    s = settlement.create(db, cid, e, body.model_dump(exclude_unset=True))
    db.commit()
    return _out(db, cid, s, full=True)


@router.get("/{uuid}")
def get(uuid: UUID, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    cid = user["company_id"]
    return _out(db, cid, settlement.get(db, cid, uuid), full=True)


@router.put("/{uuid}")
def update(uuid: UUID, body: SettlementIn, user: dict = Depends(FNF), db: Session = Depends(get_db)):
    cid = user["company_id"]
    s = settlement.update(db, cid, settlement.get(db, cid, uuid), body.model_dump(exclude_unset=True))
    db.commit()
    return _out(db, cid, s, full=True)


@router.post("/{uuid}/recompute")
def recompute(uuid: UUID, user: dict = Depends(FNF), db: Session = Depends(get_db)):
    cid = user["company_id"]
    s = settlement.get(db, cid, uuid)
    if s.status == "draft":
        settlement.recompute(db, cid, s)
    db.commit()
    return _out(db, cid, s, full=True)


@router.post("/{uuid}/submit")
def submit(uuid: UUID, user: dict = Depends(FNF), db: Session = Depends(get_db)):
    cid = user["company_id"]
    s = settlement.submit(db, cid, settlement.get(db, cid, uuid))
    db.commit()
    return _out(db, cid, s, full=True)


class PayIn(BaseModel):
    paid_on: date | None = None
    reference: str | None = None
    mode: str = "bank"


@router.post("/{uuid}/pay")
def pay(uuid: UUID, body: PayIn, user: dict = Depends(FNF), db: Session = Depends(get_db)):
    cid = user["company_id"]
    s = settlement.pay(db, cid, settlement.get(db, cid, uuid), paid_on=body.paid_on or today_for_company_id(db, cid),
                       reference=body.reference, mode=body.mode)
    db.commit()
    return _out(db, cid, s, full=True)


@router.post("/{uuid}/cancel")
def cancel(uuid: UUID, user: dict = Depends(FNF), db: Session = Depends(get_db)):
    cid = user["company_id"]
    s = settlement.get(db, cid, uuid)
    settlement.cancel(db, cid, s)
    db.commit()
    return _out(db, cid, s)


@router.post("/{uuid}/pay-run")
def pay_run(uuid: UUID, user: dict = Depends(FNF), db: Session = Depends(get_db)):
    """Make the off-cycle pay run of an approved settlement (made automatically on approval)."""
    cid = user["company_id"]
    s = settlement.get(db, cid, uuid)
    if s.status != "approved":
        raise BadRequest("Approve the settlement first.")
    settlement.make_run(db, cid, s, approved=True)
    db.commit()
    return _out(db, cid, s, full=True)


@router.get("/{uuid}/pdf")
def statement(uuid: UUID, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    cid = user["company_id"]
    s = settlement.get(db, cid, uuid)
    emp = svc.get_employee(db, cid, s.employee_id)
    content = settlement.statement_pdf(db, cid, s)
    audit.event(db, "export.downloaded", "final_settlements", s.id, {"report": "settlement_statement"}, entity_uuid=str(s.uuid))
    db.commit()
    return Response(content=content, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="settlement-{emp.employee_code or emp.id}.pdf"'})
