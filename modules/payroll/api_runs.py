"""Pay runs API under /api/v1/payroll/runs.

GET  /runs, POST /runs                              list, open a period or an off-cycle run
GET  /runs/{uuid}                                   run with stage history, checks and totals
POST /runs/{uuid}/sync | /checks | /reminders | /acknowledge | /calculate (background) | /submit
     /approve-direct (payroll.approve, no flow) | /lock | /reopen | /paid | /cancel
GET/POST /runs/{uuid}/inputs, DELETE /runs/{uuid}/inputs/{input_uuid}
GET  /runs/{uuid}/inputs/template, POST /runs/{uuid}/inputs/import
GET  /runs/{uuid}/payslips, /runs/{uuid}/variance, /runs/{uuid}/register(.xlsx)
GET  /runs/{uuid}/files/{kind}   bank (?format=hdfc|icici|sbi|axis|kotak|generic_neft), ecr, ecr_arrears, esi,
                                 pt, lwf, journal_csv, tally, wage_register
POST /runs/{uuid}/publish        publish payslips (and email them)
GET  /payslips/{uuid}, GET /payslips/{uuid}/pdf, POST /payslips/{uuid}/hold | /release
"""
from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, File, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core import audit
from core.context import RequestContext, current, use_context
from core.db import get_db, tenant_session
from core.errors import BadRequest, Forbidden, NotFound
from core.security import get_current_user, require_permission
from core.timeutil import today_for_company_id
from modules.payroll import adapters, exports, pdf, runs
from modules.payroll import service as svc
from modules.payroll.models import PayGroup, PayRun, Payslip

router = APIRouter(prefix="/payroll", tags=["Payroll runs"])

VIEW = require_permission("payroll.view")
RUN = require_permission("payroll.run")
APPROVE = require_permission("payroll.approve")
REOPEN = require_permission("payroll.reopen")


def _file(name: str, media: str, content: bytes) -> Response:
    return Response(content=content, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _group_names(db: Session, cid: int) -> dict:
    return {g.id: g.name for g in db.query(PayGroup).filter(PayGroup.company_id == cid).all()}


def _run(db: Session, user: dict, uuid) -> PayRun:
    return runs.get_run(db, user["company_id"], uuid)


def workflow_is_mine(task) -> bool:
    from core.workflow.service import is_my_task
    return is_my_task(task)


def _full(db: Session, cid: int, r: PayRun) -> dict:
    out = runs.run_out(r, _group_names(db, cid).get(r.pay_group_id))
    out["employee_count"] = len(runs.run_employees(db, cid, r)) if r.status in runs.EDITABLE else (r.totals or {}).get("employees", 0)
    out["blocking"] = [f["key"] for f in runs.blocking(r)]
    out["can_submit"] = r.status == "review" and not out["blocking"]
    if r.kind == "settlement" and (r.options or {}).get("settlement_uuid"):
        from modules.payroll import settlement
        from modules.payroll.models import FinalSettlement
        s = db.query(FinalSettlement).filter(FinalSettlement.company_id == cid,
                                             FinalSettlement.uuid == r.options["settlement_uuid"]).first()
        if s is not None:
            out["settlement"] = {"uuid": str(s.uuid), "number": s.number, "status": s.status, "due_date": s.due_date.isoformat(),
                                 "last_working_day": s.last_working_day.isoformat(),
                                 "countdown": settlement.countdown(s, today_for_company_id(db, cid))}
    if r.workflow_instance_id:
        from core.workflow.models import WorkflowInstance
        inst = db.query(WorkflowInstance).filter(WorkflowInstance.id == r.workflow_instance_id).first()
        if inst:
            out["approval"] = {"uuid": str(inst.uuid), "status": inst.status, "reference": inst.reference_no,
                               "steps": [{"uuid": str(t.uuid), "name": t.step_name, "status": t.status,
                                          "approver": t.approver_name, "decided_by": t.decided_by_name,
                                          "comment": t.comment, "mine": t.status == "pending" and workflow_is_mine(t)}
                                         for t in sorted(inst.tasks, key=lambda x: x.id)]}
    return out


@router.get("/runs")
def list_runs(status: str | None = None, kind: str | None = None, fy: str | None = None, page: int = 1,
              size: int = Query(25, le=200), user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    cid = user["company_id"]
    q = runs.runs(db, cid, status, kind, fy)
    total = q.count()
    names = _group_names(db, cid)
    return {"items": [runs.run_out(r, names.get(r.pay_group_id)) for r in q.offset((page - 1) * size).limit(size).all()],
            "total": total, "page": page, "size": size}


class RunIn(BaseModel):
    month: date
    kind: str = "regular"
    pay_group_uuid: UUID | None = None
    title: str | None = None
    employee_uuids: list[UUID] | None = None
    pay_date: date | None = None
    options: dict | None = None


@router.post("/runs", status_code=201)
def create_run(body: RunIn, user: dict = Depends(RUN), db: Session = Depends(get_db)):
    cid = user["company_id"]
    group = svc.get_pay_group(db, cid, body.pay_group_uuid) if body.pay_group_uuid else None
    ids = [svc.employee_by_uuid(db, cid, u).id for u in body.employee_uuids or []]
    r = runs.create_run(db, cid, month=body.month, group=group, kind=body.kind, title=body.title, employee_ids=ids,
                        options=body.options, pay_date=body.pay_date)
    db.commit()
    return _full(db, cid, r)


@router.get("/runs/{uuid}")
def get_run(uuid: UUID, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    return _full(db, user["company_id"], _run(db, user, uuid))


@router.post("/runs/{uuid}/sync")
def sync(uuid: UUID, user: dict = Depends(RUN), db: Session = Depends(get_db)):
    r = _run(db, user, uuid)
    counts = runs.sync_inputs(db, user["company_id"], r)
    db.commit()
    return {"counts": counts, "run": _full(db, user["company_id"], r)}


@router.post("/runs/{uuid}/checks")
def checks(uuid: UUID, user: dict = Depends(RUN), db: Session = Depends(get_db)):
    r = _run(db, user, uuid)
    if r.status not in runs.EDITABLE:
        raise BadRequest("Checks run before calculation; reopen the run to check again.")
    runs.pre_checks(db, user["company_id"], r)
    db.commit()
    return _full(db, user["company_id"], r)


@router.post("/runs/{uuid}/reminders")
def reminders(uuid: UUID, user: dict = Depends(RUN), db: Session = Depends(get_db)):
    r = _run(db, user, uuid)
    sent = runs.send_reminders(db, user["company_id"], r)
    db.commit()
    return sent


class AckIn(BaseModel):
    key: str
    reason: str


@router.post("/runs/{uuid}/acknowledge")
def acknowledge(uuid: UUID, body: AckIn, user: dict = Depends(APPROVE), db: Session = Depends(get_db)):
    r = _run(db, user, uuid)
    runs.acknowledge(db, user["company_id"], r, body.key, body.reason)
    db.commit()
    return _full(db, user["company_id"], r)


def _calculate_in_background(company_id: int, run_id: int, ctx: RequestContext) -> None:
    db = tenant_session(company_id)
    with use_context(ctx):
        try:
            r = db.query(PayRun).filter(PayRun.id == run_id).first()
            runs.calculate(db, company_id, r)
            db.commit()
        except Exception as exc:  # leave the run in a state the screen can show
            db.rollback()
            r = db.query(PayRun).filter(PayRun.id == run_id).first()
            if r is not None:
                r.status, r.error = "failed", getattr(exc, "detail", None) or str(exc)
                db.commit()
        finally:
            db.close()


@router.post("/runs/{uuid}/calculate")
def calculate(uuid: UUID, background: BackgroundTasks, sync_now: bool = False, user: dict = Depends(RUN),
              db: Session = Depends(get_db)):
    """Starts the calculation as a background job; poll GET /runs/{uuid} for progress."""
    cid = user["company_id"]
    r = _run(db, user, uuid)
    if r.status not in runs.EDITABLE:
        raise BadRequest("This pay run is past review. Reopen it to recalculate.")
    if r.status in ("inputs", "draft"):
        runs.pre_checks(db, cid, r)
    if sync_now:
        runs.calculate(db, cid, r)
        db.commit()
        return _full(db, cid, r)
    r.status, r.progress, r.error = "calculating", 0, None
    r.progress_total = len(runs.run_employees(db, cid, r))
    db.commit()
    ctx = current()
    background.add_task(_calculate_in_background, cid, r.id,
                        RequestContext(company_id=cid, employee_id=ctx.employee_id, actor_type=ctx.actor_type,
                                       actor_name=ctx.actor_name, role=ctx.role, request_id=ctx.request_id))
    return _full(db, cid, r)


@router.post("/runs/{uuid}/submit")
def submit(uuid: UUID, user: dict = Depends(RUN), db: Session = Depends(get_db)):
    r = _run(db, user, uuid)
    runs.submit_for_approval(db, user["company_id"], r)
    db.commit()
    return _full(db, user["company_id"], r)


@router.post("/runs/{uuid}/lock")
def lock(uuid: UUID, user: dict = Depends(APPROVE), db: Session = Depends(get_db)):
    r = _run(db, user, uuid)
    runs.lock(db, user["company_id"], r)
    db.commit()
    return _full(db, user["company_id"], r)


class ReasonIn(BaseModel):
    reason: str


@router.post("/runs/{uuid}/reopen")
def reopen(uuid: UUID, body: ReasonIn, user: dict = Depends(REOPEN), db: Session = Depends(get_db)):
    r = _run(db, user, uuid)
    runs.reopen(db, user["company_id"], r, body.reason)
    db.commit()
    return _full(db, user["company_id"], r)


class PaidIn(BaseModel):
    reference: str | None = None
    failed_employee_ids: list[int] | None = None


@router.post("/runs/{uuid}/paid")
def paid(uuid: UUID, body: PaidIn, user: dict = Depends(APPROVE), db: Session = Depends(get_db)):
    r = _run(db, user, uuid)
    runs.mark_paid(db, user["company_id"], r, reference=body.reference, failed_employee_ids=body.failed_employee_ids)
    db.commit()
    return _full(db, user["company_id"], r)


@router.post("/runs/{uuid}/cancel")
def cancel(uuid: UUID, user: dict = Depends(RUN), db: Session = Depends(get_db)):
    r = _run(db, user, uuid)
    runs.cancel_run(db, user["company_id"], r)
    db.commit()
    return _full(db, user["company_id"], r)


# ── Inputs ──────────────────────────────────────────────────────────

@router.get("/runs/{uuid}/inputs")
def list_inputs(uuid: UUID, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    cid = user["company_id"]
    r = _run(db, user, uuid)
    rows = runs.inputs_of(db, cid, r)
    names = runs.employee_ids_named(db, cid, list({i.employee_id for i in rows}))
    emps = runs.run_employees(db, cid, r) if r.status in runs.EDITABLE else []
    return {"items": [runs.input_out(i, names) for i in rows], "kinds": runs.INPUT_KINDS,
            "employees": [{"id": e.id, "uuid": str(e.uuid), "name": svc.name_of(e), "employee_code": e.employee_code,
                           "joining": e.date_of_joining.isoformat() if e.date_of_joining and r.period_start <= e.date_of_joining <= r.period_end else None,
                           "leaving": e.exit_date.isoformat() if e.exit_date and r.period_start <= e.exit_date <= r.period_end else None}
                          for e in emps]}


class InputIn(BaseModel):
    employee_uuid: UUID
    kind: str
    component_code: str | None = None
    amount: float | None = None
    days: float | None = None
    note: str | None = None


@router.post("/runs/{uuid}/inputs", status_code=201)
def add_input(uuid: UUID, body: InputIn, user: dict = Depends(RUN), db: Session = Depends(get_db)):
    cid = user["company_id"]
    r = _run(db, user, uuid)
    e = svc.employee_by_uuid(db, cid, body.employee_uuid)
    row = runs.add_input(db, cid, r, e, body.model_dump())
    db.commit()
    return runs.input_out(row, {e.id: svc.name_of(e)})


@router.delete("/runs/{uuid}/inputs/{input_uuid}")
def delete_input(uuid: UUID, input_uuid: UUID, user: dict = Depends(RUN), db: Session = Depends(get_db)):
    from modules.payroll.models import PayRunInput
    r = _run(db, user, uuid)
    runs._require_editable(r)
    row = db.query(PayRunInput).filter(PayRunInput.company_id == user["company_id"], PayRunInput.pay_run_id == r.id,
                                       PayRunInput.uuid == input_uuid).first()
    if not row:
        raise NotFound("Input")
    db.delete(row)
    if r.status in ("checked", "review", "failed"):
        r.status = "inputs"
    db.commit()
    return {"ok": True}


@router.get("/runs/{uuid}/inputs/template")
def input_template(uuid: UUID, user: dict = Depends(VIEW)):
    return _file("payroll-inputs-template.xlsx", exports.XLSX, runs.input_template())


@router.post("/runs/{uuid}/inputs/import")
async def import_inputs(uuid: UUID, dry_run: bool = False, file: UploadFile = File(...), user: dict = Depends(RUN),
                        db: Session = Depends(get_db)):
    r = _run(db, user, uuid)
    content = await file.read()
    res = runs.import_inputs(db, user["company_id"], r, file.filename or "inputs.xlsx", content, dry_run=dry_run)
    if dry_run:
        db.rollback()
    else:
        db.commit()
    return res


# ── Review ──────────────────────────────────────────────────────────

@router.get("/runs/{uuid}/payslips")
def run_payslips(uuid: UUID, q: str | None = None, status: str | None = None, flagged: bool = False,
                 user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    r = _run(db, user, uuid)
    rows = [runs.payslip_row(p) for p in db.query(Payslip).filter(Payslip.pay_run_id == r.id).order_by(Payslip.id).all()]
    if q:
        ql = q.lower()
        rows = [x for x in rows if ql in (x["employee_name"] or "").lower() or ql in (x["employee_code"] or "").lower()]
    if status:
        rows = [x for x in rows if x["status"] in status.split(",")]
    if flagged:
        rows = [x for x in rows if any(w.get("level") in ("block", "warn") for w in x["warnings"])]
    rows.sort(key=lambda x: (x["employee_name"] or ""))
    return {"items": rows, "total": len(rows)}


@router.get("/runs/{uuid}/variance")
def variance(uuid: UUID, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    return runs.variance(db, user["company_id"], _run(db, user, uuid))


@router.get("/runs/{uuid}/register")
def register(uuid: UUID, format: str = "json", user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    r = _run(db, user, uuid)
    header, rows = runs.register_rows(db, user["company_id"], r)
    if format == "xlsx":
        audit.event(db, "export.downloaded", "pay_runs", r.id, {"report": "register"}, entity_uuid=str(r.uuid))
        db.commit()
        return _file(f"payroll-register-{r.period_start:%Y-%m}.xlsx", exports.XLSX, exports._xlsx([("Register", header, rows)]))
    return {"header": header, "rows": rows}


FILES = {"bank": exports.bank_file, "ecr": exports.ecr, "ecr_arrears": exports.ecr_arrears, "esi": exports.esi_file,
         "pt": exports.pt_return, "lwf": exports.lwf_return, "journal_csv": exports.journal_csv, "tally": exports.tally_xml,
         "wage_register": exports.wage_register}


@router.get("/runs/{uuid}/files/{kind}")
def run_file(uuid: UUID, kind: str, format: str | None = None, released_only: bool = False,
             user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    cid = user["company_id"]
    r = _run(db, user, uuid)
    fn = FILES.get(kind)
    if not fn:
        raise NotFound("File")
    if kind == "bank" and r.status not in ("approved", "locked", "paid"):
        raise BadRequest("Approve the pay run before making the bank file.")
    if kind == "bank":
        name, media, content = fn(db, cid, r, format, released_only=released_only)
    elif kind == "esi":
        name, media, content = fn(db, cid, r, as_csv=format == "csv")
    else:
        name, media, content = fn(db, cid, r)
    audit.event(db, "export.downloaded", "pay_runs", r.id, {"file": kind, "format": format}, entity_uuid=str(r.uuid))
    db.commit()
    return _file(name, media, content)


@router.get("/runs/{uuid}/journal")
def journal(uuid: UUID, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    return exports.journal(db, user["company_id"], _run(db, user, uuid))


@router.post("/runs/{uuid}/publish")
def publish(uuid: UUID, email: bool = True, user: dict = Depends(RUN), db: Session = Depends(get_db)):
    cid = user["company_id"]
    r = _run(db, user, uuid)
    if r.status not in ("locked", "paid"):
        raise BadRequest("Lock the pay run before publishing payslips.")
    settings = svc.get_settings(db, cid)
    company = svc._company(db, cid)
    sent = published = 0
    for p in db.query(Payslip).filter(Payslip.pay_run_id == r.id, Payslip.status.in_(("calculated", "held"))).all():
        p.published = True
        published += 1
        if email and (settings.get("payslip") or {}).get("email_on_publish", True) and (p.snapshot or {}).get("email"):
            if _email_payslip(db, cid, p, company, settings):
                sent += 1
    audit.event(db, "payroll.payslips_published", "pay_runs", r.id, {"published": published, "emailed": sent},
                entity_uuid=str(r.uuid))
    db.commit()
    return {"published": published, "emailed": sent}


def _company_card(db: Session, cid: int, p: Payslip) -> dict:
    company = svc._company(db, cid)
    ent = (p.snapshot or {}).get("entity") or {}
    return {"name": ent.get("name") or company.company_name, "address": ent.get("address") or "", "logo_url": company.logo_url}


def payslip_pdf_bytes(db: Session, cid: int, p: Payslip, with_password: bool = True) -> bytes:
    settings = svc.get_settings(db, cid)
    run = db.query(PayRun).filter(PayRun.id == p.pay_run_id).first()
    if run is not None and run.kind == "settlement" and (run.options or {}).get("settlement_uuid"):
        from modules.payroll import settlement
        s = settlement.get(db, cid, run.options["settlement_uuid"])
        body = settlement.statement_pdf(db, cid, s)
        scheme = (settings.get("payslip") or {}).get("password", "pan")
        return pdf.encrypt(body, pdf.payslip_password(p.snapshot or {}, scheme)) if with_password else body
    earlier = (db.query(Payslip).join(PayRun, PayRun.id == Payslip.pay_run_id)
               .filter(Payslip.company_id == cid, Payslip.employee_id == p.employee_id, Payslip.fy == p.fy,
                       Payslip.period_start < p.period_start, PayRun.status.in_(("approved", "locked", "paid"))).all())
    ytd = pdf.ytd_by_code(earlier)
    leave = adapters.leave_balances(db, cid, p.employee_id)
    return pdf.payslip_pdf(p, _company_card(db, cid, p), settings, ytd=ytd, leave=leave, with_password=with_password)


def _email_payslip(db: Session, cid: int, p: Payslip, company, settings) -> bool:
    """Payslip as a password-protected PDF attachment; the password hint goes in the body."""
    from email.message import EmailMessage
    import smtplib
    from core.config import get_settings as app_settings
    from core.email import email_configured
    s = app_settings()
    snap = p.snapshot or {}
    month = p.period_start.strftime("%B %Y")
    hint = pdf.password_hint((settings.get("payslip") or {}).get("password", "pan"))
    body = (f"Hello {snap.get('name')},\n\nYour payslip for {month} is attached. Net pay: ₹{p.net}.\n"
            f"Open it with: {hint}.\n\nYou can also see it in Campusly under My pay.\n")
    if not email_configured():
        import logging
        logging.getLogger(__name__).warning("[email not configured] payslip to %s for %s", snap.get("email"), month)
        p.emailed_at = None
        return False
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = s.SMTP_FROM, snap.get("email"), f"Payslip for {month} · {company.company_name}"
    msg.set_content(body)
    msg.add_attachment(payslip_pdf_bytes(db, cid, p), maintype="application", subtype="pdf",
                       filename=f"payslip-{p.period_start:%Y-%m}.pdf")
    try:
        with smtplib.SMTP(s.SMTP_HOST, s.SMTP_PORT, timeout=20) as smtp:
            if s.SMTP_TLS:
                smtp.starttls()
            if s.SMTP_USER:
                smtp.login(s.SMTP_USER, s.SMTP_PASSWORD)
            smtp.send_message(msg)
        from modules.payroll.runs import _now
        p.emailed_at = _now()
        return True
    except Exception:
        return False


# ── Payslips ────────────────────────────────────────────────────────

def _payslip(db: Session, user: dict, uuid, *, own_ok: bool = True) -> Payslip:
    p = db.query(Payslip).filter(Payslip.company_id == user["company_id"], Payslip.uuid == uuid).first()
    if not p:
        raise NotFound("Payslip")
    from modules.payroll.router import perms
    if "payroll.view" in perms(db, user):
        return p
    if own_ok and user.get("employee_id") == p.employee_id and p.published:
        return p
    raise Forbidden("You can only see your own published payslips.")


def payslip_detail(db: Session, p: Payslip) -> dict:
    run = db.query(PayRun).filter(PayRun.id == p.pay_run_id).first()
    return {**runs.payslip_row(p), "lines": p.lines or [], "statutory": p.statutory or {}, "tax": p.tax or {},
            "wage_rule": p.wage_rule or {}, "snapshot": p.snapshot or {}, "run": runs.run_out(run) if run else None,
            "released_at": p.released_at.isoformat() if p.released_at else None,
            "emailed_at": p.emailed_at.isoformat() if p.emailed_at else None}


@router.get("/payslips/{uuid}")
def get_payslip(uuid: UUID, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    return payslip_detail(db, _payslip(db, user, uuid))


@router.get("/payslips/{uuid}/pdf")
def payslip_pdf(uuid: UUID, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = user["company_id"]
    p = _payslip(db, user, uuid)
    own = user.get("employee_id") == p.employee_id
    content = payslip_pdf_bytes(db, cid, p, with_password=True)
    audit.event(db, "export.downloaded", "payslips", p.id, {"report": "payslip", "own": own}, entity_uuid=str(p.uuid))
    db.commit()
    return _file(f"payslip-{p.period_start:%Y-%m}-{(p.snapshot or {}).get('employee_code') or p.employee_id}.pdf",
                 "application/pdf", content)


class HoldIn(BaseModel):
    reason: str | None = None


@router.post("/payslips/{uuid}/hold")
def hold(uuid: UUID, body: HoldIn, user: dict = Depends(RUN), db: Session = Depends(get_db)):
    p = _payslip(db, user, uuid, own_ok=False)
    runs.hold(db, user["company_id"], p, body.reason or "On hold")
    db.commit()
    return payslip_detail(db, p)


@router.post("/payslips/{uuid}/release")
def release(uuid: UUID, body: HoldIn, user: dict = Depends(APPROVE), db: Session = Depends(get_db)):
    p = _payslip(db, user, uuid, own_ok=False)
    runs.release(db, user["company_id"], p, body.reason)
    db.commit()
    return payslip_detail(db, p)


@router.post("/runs/{uuid}/recalculate/{employee_uuid}")
def recalc_one(uuid: UUID, employee_uuid: UUID, user: dict = Depends(RUN), db: Session = Depends(get_db)):
    cid = user["company_id"]
    r = _run(db, user, uuid)
    e = svc.employee_by_uuid(db, cid, employee_uuid)
    runs.calculate(db, cid, r, employee_ids=[e.id])
    db.commit()
    return _full(db, cid, r)


_ = today_for_company_id  # re-exported for api modules
