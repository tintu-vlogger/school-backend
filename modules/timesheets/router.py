"""Timesheets API: clients, projects, tasks, my week, time logs, submit, approvals and reports."""
from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, joinedload

from core.db import get_db
from core.errors import BadRequest, Forbidden, NotFound
from core.pagination import PageParams, paginate
from core.security import get_current_user, require_permission
from modules.attendance import service as att
from modules.timesheets import service
from modules.timesheets.models import Timesheet, TimeLog, TsClient, TsProject, TsTask

router = APIRouter(prefix="/timesheets", tags=["Timesheets"])

MANAGE = require_permission("time.timesheets.manage")


def _me(db, user):
    if not user.get("employee_id"):
        raise BadRequest("The company owner has no timesheet. Sign in as an employee to log time.")
    return att.employee(db, user["company_id"], user["employee_id"])


# ── Clients ─────────────────────────────────────────────────────────

class ClientIn(BaseModel):
    name: str | None = Field(None, max_length=150)
    contact_name: str | None = None
    email: str | None = None
    phone: str | None = None
    gstin: str | None = Field(None, max_length=15)
    address: str | None = Field(None, max_length=500)
    state_code: str | None = Field(None, max_length=4)
    active: bool | None = None


@router.get("/clients")
def list_clients(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.query(TsClient).filter(TsClient.company_id == user["company_id"]).order_by(TsClient.name).all()
    return [service.client_out(c) for c in rows]


@router.post("/clients", status_code=201)
def add_client(req: ClientIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    c = service.save_client(db, user["company_id"], req.model_dump(exclude_unset=True))
    db.commit()
    return service.client_out(c)


@router.put("/clients/{uuid}")
def edit_client(uuid: UUID, req: ClientIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    c = db.query(TsClient).filter(TsClient.company_id == user["company_id"], TsClient.uuid == uuid).first()
    if not c:
        raise NotFound("Client")
    service.save_client(db, user["company_id"], req.model_dump(exclude_unset=True), c)
    db.commit()
    return service.client_out(c)


# ── Projects and tasks ──────────────────────────────────────────────

class ProjectIn(BaseModel):
    name: str | None = Field(None, max_length=150)
    code: str | None = Field(None, max_length=30)
    client_id: int | None = None
    owner_employee_id: int | None = None
    billable: bool | None = None
    budget_hours: float | None = None
    start_date: date | None = None
    end_date: date | None = None
    status: str | None = None
    member_ids: list[int] | None = None
    description: str | None = None


@router.get("/projects")
def list_projects(status: str | None = None, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    q = db.query(TsProject).options(joinedload(TsProject.client)).filter(TsProject.company_id == cid)
    if status:
        q = q.filter(TsProject.status == status)
    rows = q.order_by(TsProject.name).all()
    if not att.has_perm(db, user, "time.timesheets.manage"):
        eid = user.get("employee_id")
        rows = [p for p in rows if not p.member_ids or eid in p.member_ids or p.owner_employee_id == eid]
    logged = service.logged_by_project(db, cid)
    names = att.names_for(db, cid, [p.owner_employee_id for p in rows])
    return [service.project_out(p, logged.get(p.id), names) for p in rows]


@router.post("/projects", status_code=201)
def add_project(req: ProjectIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    p = service.save_project(db, user["company_id"], req.model_dump(exclude_unset=True))
    db.commit()
    return service.project_out(p)


def _project(db, cid, uuid) -> TsProject:
    p = db.query(TsProject).options(joinedload(TsProject.client)).filter(TsProject.company_id == cid,
                                                                         TsProject.uuid == uuid).first()
    if not p:
        raise NotFound("Project")
    return p


@router.get("/projects/{uuid}")
def get_project(uuid: UUID, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    p = _project(db, cid, uuid)
    logged = service.logged_by_task(db, cid, p.id)
    tasks = db.query(TsTask).filter(TsTask.company_id == cid, TsTask.project_id == p.id).order_by(TsTask.name).all()
    names = att.names_for(db, cid, [p.owner_employee_id, *(p.member_ids or [])])
    return {**service.project_out(p, service.logged_by_project(db, cid).get(p.id), names),
            "members": [{"id": i, "name": names.get(i)} for i in (p.member_ids or [])],
            "tasks": [service.task_out(t, logged.get(t.id)) for t in tasks]}


@router.put("/projects/{uuid}")
def edit_project(uuid: UUID, req: ProjectIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    p = _project(db, user["company_id"], uuid)
    service.save_project(db, user["company_id"], req.model_dump(exclude_unset=True), p)
    db.commit()
    return service.project_out(p)


class TaskIn(BaseModel):
    name: str | None = Field(None, max_length=150)
    owner_employee_id: int | None = None
    billable: bool | None = None
    budget_hours: float | None = None
    status: str | None = None


@router.post("/projects/{uuid}/tasks", status_code=201)
def add_task(uuid: UUID, req: TaskIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    p = _project(db, user["company_id"], uuid)
    t = service.save_task(db, user["company_id"], p, req.model_dump(exclude_unset=True))
    db.commit()
    return service.task_out(t)


@router.put("/tasks/{uuid}")
def edit_task(uuid: UUID, req: TaskIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    t = db.query(TsTask).filter(TsTask.company_id == user["company_id"], TsTask.uuid == uuid).first()
    if not t:
        raise NotFound("Task")
    service.save_task(db, user["company_id"], t.project, req.model_dump(exclude_unset=True), t)
    db.commit()
    return service.task_out(t)


# ── My week and time logs ───────────────────────────────────────────

@router.get("/me/week")
def my_week(start: date, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    emp = _me(db, user)
    return service.week(db, user["company_id"], emp.id, start)


class WeekRow(BaseModel):
    project_id: int
    task_id: int | None = None
    billable: bool = True
    description: str | None = Field(None, max_length=1000)
    hours: dict[str, float | str | None] = Field(default_factory=dict)


class WeekIn(BaseModel):
    week_start: date
    rows: list[WeekRow]


@router.put("/me/week")
def save_my_week(req: WeekIn, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    emp = _me(db, user)
    out = service.save_week(db, user["company_id"], emp.id, req.week_start, [r.model_dump() for r in req.rows])
    db.commit()
    return out


class SubmitIn(BaseModel):
    week_start: date
    comment: str | None = Field(None, max_length=1000)


@router.post("/me/submit")
def submit_my_week(req: SubmitIn, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    emp = _me(db, user)
    ts = service.submit_week(db, user["company_id"], emp, req.week_start, req.comment)
    db.commit()
    return service.timesheet_out(ts)


class LogIn(BaseModel):
    date: date
    project_id: int | None = None
    task_id: int | None = None
    hours: float | None = None
    start_time: str | None = None
    end_time: str | None = None
    description: str | None = Field(None, max_length=1000)
    billable: bool | None = None


@router.get("/me/logs")
def my_logs(start: date, end: date, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    emp = _me(db, user)
    rows = (db.query(TimeLog).options(joinedload(TimeLog.project), joinedload(TimeLog.task))
            .filter(TimeLog.company_id == user["company_id"], TimeLog.employee_id == emp.id,
                    TimeLog.date >= start, TimeLog.date <= end).order_by(TimeLog.date.desc(), TimeLog.id).all())
    return [service.log_out(l) for l in rows]


@router.post("/me/logs", status_code=201)
def add_log(req: LogIn, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    emp = _me(db, user)
    l = service.save_log(db, user["company_id"], emp.id, req.model_dump(exclude_none=True))
    db.commit()
    db.refresh(l)
    return service.log_out(l)


def _my_log(db, user, uuid) -> TimeLog:
    l = db.query(TimeLog).filter(TimeLog.company_id == user["company_id"], TimeLog.uuid == uuid).first()
    if not l or l.employee_id != user.get("employee_id"):
        raise NotFound("Time entry")
    return l


@router.put("/me/logs/{uuid}")
def edit_log(uuid: UUID, req: LogIn, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    l = _my_log(db, user, uuid)
    service.save_log(db, user["company_id"], l.employee_id, req.model_dump(exclude_none=True), l)
    db.commit()
    db.refresh(l)
    return service.log_out(l)


@router.delete("/me/logs/{uuid}")
def remove_log(uuid: UUID, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    l = _my_log(db, user, uuid)
    service.delete_log(db, user["company_id"], l)
    db.commit()
    return {"detail": "Removed"}


# ── Approvals and lists ─────────────────────────────────────────────

@router.get("/approvals")
def approvals(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = service.approvals(db, user["company_id"])
    names = att.names_for(db, user["company_id"], [t.employee_id for t in rows])
    return [service.timesheet_out(t, names.get(t.employee_id)) for t in rows]


@router.get("/")
def list_timesheets(status: str | None = None, employee_id: int | None = None, pages: PageParams = Depends(),
                    user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    q = db.query(Timesheet).filter(Timesheet.company_id == cid)
    if not att.has_perm(db, user, "time.timesheets.manage"):
        ids = [user.get("employee_id"), *att.report_ids(db, cid, user.get("employee_id"))]
        q = q.filter(Timesheet.employee_id.in_([i for i in ids if i] or [-1]))
    if status:
        q = q.filter(Timesheet.status.in_(status.split(",")))
    if employee_id:
        q = q.filter(Timesheet.employee_id == employee_id)
    cache = {}

    def out(t):
        if t.employee_id not in cache:
            cache[t.employee_id] = att.names_for(db, cid, [t.employee_id]).get(t.employee_id)
        return service.timesheet_out(t, cache[t.employee_id])
    return paginate(q.order_by(Timesheet.week_start.desc(), Timesheet.id), pages, out)


def _timesheet(db, user, uuid) -> Timesheet:
    cid = user["company_id"]
    t = db.query(Timesheet).filter(Timesheet.company_id == cid, Timesheet.uuid == uuid).first()
    if not t:
        raise NotFound("Timesheet")
    if t.employee_id != user.get("employee_id") and not att.has_perm(db, user, "time.timesheets.manage") \
            and t.employee_id not in att.report_ids(db, cid, user.get("employee_id")) \
            and service.my_pending_task(db, cid, t) is None:
        raise NotFound("Timesheet")
    return t


@router.get("/reports/projects")
def project_report(start: date, end: date, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    return service.project_report(db, user["company_id"], start, end)


@router.post("/reminders")
def reminders(week_start: date, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    n = service.send_reminders(db, user["company_id"], week_start)
    db.commit()
    return {"sent": n}


@router.get("/{uuid}")
def get_timesheet(uuid: UUID, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    return service.timesheet_detail(db, user["company_id"], _timesheet(db, user, uuid))


class ReviewLine(BaseModel):
    log_uuid: str
    decision: str
    comment: str | None = Field(None, max_length=500)


class ReviewIn(BaseModel):
    lines: list[ReviewLine] = Field(default_factory=list)
    comment: str | None = Field(None, max_length=1000)


@router.post("/{uuid}/review")
def review(uuid: UUID, req: ReviewIn, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    t = _timesheet(db, user, uuid)
    service.review(db, user["company_id"], t, [l.model_dump() for l in req.lines], req.comment)
    db.commit()
    return service.timesheet_detail(db, user["company_id"], t)


# ── Billing: rates and invoices ─────────────────────────────────────

from fastapi.responses import Response  # noqa: E402
from decimal import Decimal  # noqa: E402

from core import audit  # noqa: E402
from modules.timesheets import billing  # noqa: E402
from modules.timesheets.models import TsBillingRate, TsInvoice  # noqa: E402


class RateIn(BaseModel):
    project_uuid: UUID | None = None
    employee_uuid: UUID | None = None
    rate: Decimal = Field(..., gt=0)
    effective_from: date
    note: str | None = Field(None, max_length=200)
    active: bool | None = None


def _rate_data(db, cid, req: RateIn) -> dict:
    data = {"rate": req.rate, "effective_from": req.effective_from, "note": req.note, "active": req.active}
    data["project_id"] = _project(db, cid, req.project_uuid).id if req.project_uuid else None
    data["employee_id"] = att.employee_by_uuid(db, cid, req.employee_uuid).id if req.employee_uuid else None
    return data


def _rates_payload(db, cid) -> list[dict]:
    rows = (db.query(TsBillingRate).filter(TsBillingRate.company_id == cid)
            .order_by(TsBillingRate.effective_from.desc(), TsBillingRate.id.desc()).all())
    projects = {p.id: p.name for p in db.query(TsProject).filter(TsProject.company_id == cid).all()}
    names = att.names_for(db, cid, [r.employee_id for r in rows])
    return [billing.rate_out(r, projects, names) for r in rows]


@router.get("/billing/rates")
def list_rates(user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    return {"items": _rates_payload(db, user["company_id"])}


@router.post("/billing/rates", status_code=201)
def add_rate(req: RateIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = user["company_id"]
    r = billing.save_rate(db, cid, _rate_data(db, cid, req))
    db.commit()
    return {"uuid": str(r.uuid)}


@router.put("/billing/rates/{uuid}")
def edit_rate(uuid: UUID, req: RateIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = user["company_id"]
    r = db.query(TsBillingRate).filter(TsBillingRate.company_id == cid, TsBillingRate.uuid == uuid).first()
    if r is None:
        raise NotFound("Rate")
    billing.save_rate(db, cid, _rate_data(db, cid, req), r)
    db.commit()
    return {"uuid": str(r.uuid)}


class InvoicePreviewIn(BaseModel):
    client_uuid: UUID
    period_start: date
    period_end: date


class InvoiceIn(InvoicePreviewIn):
    issue_date: date | None = None
    due_date: date | None = None
    notes: str | None = Field(None, max_length=2000)


class InvoiceEditIn(BaseModel):
    issue_date: date | None = None
    due_date: date | None = None
    notes: str | None = Field(None, max_length=2000)


class InvoiceStatusIn(BaseModel):
    status: str
    reference: str | None = Field(None, max_length=120)


@router.get("/billing/summary")
def billing_summary(user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = user["company_id"]
    return {"by_status": billing.summary(db, cid), "unbilled": billing.unbilled(db, cid),
            "settings": billing.settings(db, cid), "states": billing.STATES}


@router.post("/billing/invoices/preview")
def invoice_preview(req: InvoicePreviewIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = user["company_id"]
    out = billing.preview(db, cid, billing._client(db, cid, req.client_uuid), req.period_start, req.period_end)
    for l in out["lines"]:
        l.pop("log_ids", None)
    return out


@router.get("/billing/invoices")
def list_invoices(status: str | None = None, client_uuid: UUID | None = None, pages: PageParams = Depends(),
                  user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = user["company_id"]
    q = db.query(TsInvoice).options(joinedload(TsInvoice.client)).filter(TsInvoice.company_id == cid)
    if status:
        q = q.filter(TsInvoice.status == status)
    if client_uuid:
        q = q.filter(TsInvoice.client_id == billing._client(db, cid, client_uuid).id)
    q = q.order_by(TsInvoice.issue_date.desc(), TsInvoice.id.desc())
    return paginate(q, pages, billing.invoice_out)


@router.post("/billing/invoices", status_code=201)
def create_invoice(req: InvoiceIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = user["company_id"]
    inv = billing.create(db, cid, billing._client(db, cid, req.client_uuid), req.period_start, req.period_end,
                         req.model_dump(exclude_unset=True))
    db.commit()
    return billing.invoice_out(inv, full=True)


@router.get("/billing/invoices/{uuid}")
def get_invoice(uuid: UUID, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    return billing.invoice_out(billing.get(db, user["company_id"], uuid), full=True)


@router.put("/billing/invoices/{uuid}")
def edit_invoice(uuid: UUID, req: InvoiceEditIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = user["company_id"]
    inv = billing.refresh(db, cid, billing.get(db, cid, uuid), req.model_dump(exclude_unset=True))
    db.commit()
    return billing.invoice_out(inv, full=True)


@router.post("/billing/invoices/{uuid}/status")
def invoice_status(uuid: UUID, req: InvoiceStatusIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = user["company_id"]
    inv = billing.set_status(db, cid, billing.get(db, cid, uuid), req.status, req.reference)
    db.commit()
    return billing.invoice_out(inv, full=True)


@router.get("/billing/invoices/{uuid}/pdf")
def invoice_pdf(uuid: UUID, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    inv = billing.get(db, user["company_id"], uuid)
    audit.event(db, "export.downloaded", "ts_invoices", inv.id, {"invoice": inv.number or "draft"},
                entity_uuid=str(inv.uuid))
    db.commit()
    name = (inv.number or f"draft-{str(inv.uuid)[:8]}").replace("/", "-")
    return Response(billing.pdf(inv), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="invoice-{name}.pdf"'})
