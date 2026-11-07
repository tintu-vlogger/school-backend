"""Exit API: board and dashboard, flows and settings, one exit with notice, handover,
clearances, interview, settlement handoff, letters and completion; the clearance list;
the employee's own resignation; the manager's team exits; and the public alumni portal."""
from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from core.db import bypass_rls, get_db, set_tenant
from core.errors import BadRequest, Forbidden, NotFound
from core.pagination import PageParams, paginate
from core.security import get_current_user
from modules.offboarding import service, settings as xsettings
from modules.offboarding.models import Exit, ExitClearance, ExitHandover, ExitInterview
from modules.recruitment import common as c

router = APIRouter(prefix="/exits", tags=["Exits"])
public_router = APIRouter(prefix="/public/alumni", tags=["Alumni (public)"])


class _In(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ExitIn(_In):
    employee_id: int
    exit_type: str
    reason_id: int | None = None
    reason_text: str | None = None
    resignation_date: date | None = None
    last_working_day: date | None = None
    notice_action: str | None = None
    replacement_needed: bool = False
    compensation: dict | None = None


class ResignIn(_In):
    reason_id: int | None = None
    reason_text: str | None = None
    proposed_last_day: date | None = None
    replacement_needed: bool = False


class UpdateIn(_In):
    last_working_day: date | None = None
    notice_action: str | None = None
    notice_required_days: int | None = None
    reason_id: int | None = None
    reason_text: str | None = None
    replacement_needed: bool | None = None
    rehire_eligible: bool | None = None
    rehire_note: str | None = None
    handover_notes: str | None = None
    compensation: dict | None = None


class ReasonIn(_In):
    reason: str | None = None


class HandoverIn(_In):
    kind: str = "task"
    title: str
    details: str | None = None
    to_employee_id: int | None = None


class StatusIn(_In):
    status: str


class DelegateIn(_In):
    to_employee_id: int
    reassign_tasks: bool = True


class PaidIn(_In):
    paid_on: date
    reference: str | None = None


class LettersIn(_In):
    types: list[str] | None = None


class CompleteIn(_In):
    force: bool = False


class ClearanceIn(_In):
    status: str | None = None
    remarks: str | None = None
    items: list[dict] | None = None
    recovery_amount: str | None = None
    owner_employee_id: int | None = None
    force: bool = False


class InterviewIn(_In):
    ratings: dict = {}
    answers: dict = {}
    would_rejoin: str | None = None
    would_recommend: int | None = None
    primary_reason_id: int | None = None
    checklist: dict | None = None
    interviewer_notes: str | None = None


def _m(db, user):
    service.require_manage(db, user)


def _reports(db: Session, company_id: int, manager_id: int | None) -> list[int]:
    if not manager_id:
        return []
    E = c.Employee()
    return [i for (i,) in db.query(E.id).filter(E.company_id == company_id, E.reporting_manager_id == manager_id)]


def _visible(db: Session, user: dict, x: Exit) -> None:
    if service.can_manage(db, user):
        return
    eid = user.get("employee_id")
    if eid and (x.employee_id == eid or x.employee_id in _reports(db, user["company_id"], eid)):
        return
    raise NotFound("Exit")


# ── Meta, dashboard, settings, flows ────────────────────────────────

@router.get("/meta")
def meta(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    return {"can_manage": service.can_manage(db, user), "statuses": service.STATUSES,
            "types": {k: v["label"] for k, v in xsettings.EXIT_TYPES.items()},
            "type_rules": xsettings.EXIT_TYPES, "notice_actions": xsettings.NOTICE_ACTIONS,
            "clearance_departments": xsettings.CLEARANCE_DEPTS, "rating_areas": xsettings.RATING_AREAS,
            "interview_questions": xsettings.INTERVIEW_QUESTIONS,
            "has_team": bool(_reports(db, user["company_id"], user.get("employee_id")))}


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _m(db, user)
    return service.dashboard(db, user["company_id"])


@router.get("/settings")
def get_settings_(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _m(db, user)
    return xsettings.get(db, user["company_id"])


@router.put("/settings")
def put_settings(body: dict, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _m(db, user)
    out = xsettings.save(db, user["company_id"], body)
    db.commit()
    return out


@router.get("/flows")
def flows(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _m(db, user)
    from sqlalchemy import func
    from modules.offboarding.models import ExitFlow
    rows = (db.query(ExitFlow).filter(ExitFlow.company_id == user["company_id"])
            .order_by(ExitFlow.exit_type, ExitFlow.priority, ExitFlow.id).all())
    counts = dict(db.query(Exit.flow_id, func.count(Exit.id)).filter(Exit.company_id == user["company_id"],
                                                                     Exit.status.in_(service.LIVE))
                  .group_by(Exit.flow_id).all())
    return {"items": [service.flow_out(db, f, counts) for f in rows]}


@router.post("/flows", status_code=status.HTTP_201_CREATED)
def create_flow(body: dict, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _m(db, user)
    f = service.save_flow(db, user["company_id"], body)
    db.commit()
    return service.flow_out(db, f)


@router.put("/flows/{uuid}")
def update_flow(uuid: UUID, body: dict, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _m(db, user)
    f = service.save_flow(db, user["company_id"], body, service.get_flow(db, user["company_id"], uuid))
    db.commit()
    return service.flow_out(db, f)


# ── Exits ───────────────────────────────────────────────────────────

@router.get("")
def list_exits(status_: str | None = Query(None, alias="status"), exit_type: str | None = None, q: str | None = None,
               scope: str = "all", params: PageParams = Depends(), db: Session = Depends(get_db),
               user: dict = Depends(get_current_user)):
    cid = user["company_id"]
    E = c.Employee()
    qr = (db.query(Exit).options(joinedload(Exit.clearances), joinedload(Exit.handovers), joinedload(Exit.flow))
          .join(E, E.id == Exit.employee_id).filter(Exit.company_id == cid))
    if scope == "team" or not service.can_manage(db, user):
        ids = _reports(db, cid, user.get("employee_id"))
        qr = qr.filter(Exit.employee_id.in_(ids or [0]))
    if status_:
        qr = qr.filter(Exit.status.in_(status_.split(",")))
    if exit_type:
        qr = qr.filter(Exit.exit_type.in_(exit_type.split(",")))
    if q:
        qr = qr.filter(or_(E.first_name.ilike(f"%{q}%"), E.last_name.ilike(f"%{q}%"), E.employee_code.ilike(f"%{q}%"),
                           Exit.number.ilike(f"%{q}%")))
    page = paginate(qr.order_by(Exit.last_working_day.desc()), params)
    rows = page["items"]
    emps = {e.id: e for e in db.query(E).filter(E.company_id == cid, E.id.in_([x.employee_id for x in rows] or [0]))}
    labels = c.org_labels(db, cid, departments=[e.department_id for e in emps.values()],
                          designations=[e.designation_id for e in emps.values()], masters=[x.reason_id for x in rows])
    page["items"] = [service.out(db, x, emp=emps[x.employee_id], labels=labels) for x in rows]
    db.commit()
    return page


@router.post("", status_code=status.HTTP_201_CREATED)
def create_exit(body: ExitIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    cid = user["company_id"]
    emp = c.employee(db, cid, body.employee_id)
    if emp is None:
        raise NotFound("Employee")
    if service.can_manage(db, user):
        raised_by = "hr"
    elif user.get("employee_id") and emp.reporting_manager_id == user["employee_id"]:
        raised_by = "manager"
        rules = xsettings.EXIT_TYPES.get(body.exit_type) or {}
        if "manager" not in rules.get("raised_by", []):
            raise Forbidden("Only HR can raise this kind of exit.")
    else:
        raise Forbidden("Only HR or the reporting manager can raise an exit.")
    data = body.model_dump(exclude_unset=True)
    x = service.start_exit(db, cid, emp, body.exit_type, data, raised_by=raised_by)
    db.commit()
    return detail(x.uuid, db, user)


@router.get("/clearances")
def list_clearances(scope: str = "mine", status_: str | None = Query("pending", alias="status"), delayed: bool = False,
                    db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    cid = user["company_id"]
    qr = (db.query(ExitClearance).options(joinedload(ExitClearance.exit))
          .join(Exit, Exit.id == ExitClearance.exit_id).filter(ExitClearance.company_id == cid,
                                                               Exit.status.in_(("in_progress", "completed"))))
    if scope == "all":
        _m(db, user)
    else:
        qr = qr.filter(ExitClearance.owner_employee_id == (user.get("employee_id") or -1))
    if status_:
        qr = qr.filter(ExitClearance.status.in_(status_.split(",")))
    t0 = c.today(db, cid)
    if delayed:
        qr = qr.filter(ExitClearance.status == "pending", ExitClearance.due_date < t0)
    rows = qr.order_by(ExitClearance.due_date).limit(500).all()
    names = c.names_for(db, cid, [x for r in rows for x in (r.owner_employee_id, r.exit.employee_id)])
    return {"items": [{**service.clearance_out(r, names, t0), "exit_uuid": str(r.exit.uuid),
                       "employee_name": names.get(r.exit.employee_id), "last_working_day": r.exit.last_working_day,
                       "exit_type_label": xsettings.EXIT_TYPES[r.exit.exit_type]["label"]} for r in rows]}


@router.put("/clearances/{uuid}")
def update_clearance(uuid: UUID, body: ClearanceIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    cl = (db.query(ExitClearance).options(joinedload(ExitClearance.exit).joinedload(Exit.clearances))
          .filter(ExitClearance.company_id == user["company_id"], ExitClearance.uuid == uuid).first())
    if cl is None:
        raise NotFound("Clearance")
    service.update_clearance(db, user, cl, body.model_dump(exclude_unset=True))
    db.commit()
    names = c.names_for(db, user["company_id"], [cl.owner_employee_id])
    return service.clearance_out(cl, names, c.today(db, user["company_id"]))


@router.post("/clearances/{uuid}/remind")
def remind_clearance(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _m(db, user)
    cl = (db.query(ExitClearance).options(joinedload(ExitClearance.exit))
          .filter(ExitClearance.company_id == user["company_id"], ExitClearance.uuid == uuid).first())
    if cl is None:
        raise NotFound("Clearance")
    service.remind_clearance(db, cl)
    db.commit()
    return {"reminded": True}


@router.get("/analytics")
def analytics(start: date | None = None, end: date | None = None, db: Session = Depends(get_db),
              user: dict = Depends(get_current_user)):
    _m(db, user)
    return service.interview_analytics(db, user["company_id"], start, end)


# ── Self-service: my resignation ────────────────────────────────────

def _me(db: Session, user: dict):
    eid = user.get("employee_id")
    if not eid:
        raise BadRequest("Only employees can resign.")
    return c.employee(db, user["company_id"], eid)


@router.get("/me")
def my_exit(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    emp = _me(db, user)
    cid = user["company_id"]
    x = (db.query(Exit).options(joinedload(Exit.clearances), joinedload(Exit.handovers), joinedload(Exit.flow))
         .filter(Exit.company_id == cid, Exit.employee_id == emp.id).order_by(Exit.id.desc()).first())
    req = service.required_notice(db, cid, emp, "resignation")
    t0 = c.today(db, cid)
    from datetime import timedelta
    preview = {"notice_days": req, "earliest_last_day": t0 + timedelta(days=req), "today": t0}
    if x is None or x.status in ("withdrawn", "rejected", "cancelled"):
        return {"exit": service.out(db, x, emp=emp, full=True) if x else None, "can_resign": emp.status in ("probation", "confirmed"),
                "notice": preview}
    out = service.out(db, x, emp=emp, full=True)
    out.pop("compensation", None)
    db.commit()
    return {"exit": out, "can_resign": False, "notice": preview}


@router.post("/me/resign", status_code=status.HTTP_201_CREATED)
def resign(body: ResignIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    emp = _me(db, user)
    data = body.model_dump(exclude_unset=True)
    service.start_exit(db, user["company_id"], emp, "resignation", data, raised_by="employee")
    db.commit()
    return my_exit(db, user)


@router.post("/me/withdraw")
def withdraw_mine(body: ReasonIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    emp = _me(db, user)
    x = service.live_exit(db, user["company_id"], emp.id)
    if x is None:
        raise BadRequest("You have no resignation to withdraw.")
    x = service.get(db, user["company_id"], x.uuid)
    service.withdraw(db, user, x, body.reason)
    db.commit()
    return my_exit(db, user)


@router.post("/me/interview")
def my_interview(body: InterviewIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    emp = _me(db, user)
    iv = (db.query(ExitInterview).filter(ExitInterview.company_id == user["company_id"], ExitInterview.employee_id == emp.id)
          .order_by(ExitInterview.id.desc()).first())
    if iv is None:
        raise BadRequest("There is no exit interview for you.")
    service.submit_interview(db, user, iv, body.model_dump(exclude_unset=True))
    db.commit()
    return my_exit(db, user)


# ── One exit ────────────────────────────────────────────────────────

def _exit(db: Session, user: dict, uuid) -> Exit:
    x = service.get(db, user["company_id"], uuid)
    _visible(db, user, x)
    return x


@router.get("/{uuid}")
def detail(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    x = _exit(db, user, uuid)
    out = service.out(db, x, full=True)
    out["can_manage"] = service.can_manage(db, user)
    db.commit()
    return out


@router.put("/{uuid}")
def update(uuid: UUID, body: UpdateIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _m(db, user)
    data = body.model_dump(exclude_unset=True)
    if data.get("last_working_day"):
        data["last_working_day"] = data["last_working_day"].isoformat()
    service.update(db, service.get(db, user["company_id"], uuid), data)
    db.commit()
    return detail(uuid, db, user)


@router.post("/{uuid}/withdraw")
def withdraw(uuid: UUID, body: ReasonIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    x = _exit(db, user, uuid)
    service.withdraw(db, user, x, body.reason)
    db.commit()
    return detail(uuid, db, user)


@router.post("/{uuid}/handovers", status_code=status.HTTP_201_CREATED)
def add_handover(uuid: UUID, body: HandoverIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    x = _exit(db, user, uuid)
    if x.status not in service.LIVE:
        raise BadRequest("This exit is closed.")
    service.add_handover(db, x, body.model_dump())
    db.commit()
    return detail(uuid, db, user)


@router.put("/handovers/{uuid}")
def set_handover(uuid: UUID, body: StatusIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    h = (db.query(ExitHandover).options(joinedload(ExitHandover.exit))
         .filter(ExitHandover.company_id == user["company_id"], ExitHandover.uuid == uuid).first())
    if h is None:
        raise NotFound("Handover item")
    _visible(db, user, h.exit)
    service.set_handover(db, h, body.status)
    db.commit()
    return {"uuid": str(h.uuid), "status": h.status}


@router.post("/{uuid}/delegate")
def delegate(uuid: UUID, body: DelegateIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    x = _exit(db, user, uuid)
    if not (service.can_manage(db, user) or user.get("employee_id") == x.employee_id):
        raise Forbidden("Only HR or the leaver can hand over approvals.")
    res = service.delegate_approvals(db, x, body.to_employee_id, body.reassign_tasks)
    db.commit()
    return {**detail(uuid, db, user), "moved": res}


@router.post("/{uuid}/settlement")
def to_payroll(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _m(db, user)
    service.handover_to_payroll(db, service.get(db, user["company_id"], uuid))
    db.commit()
    return detail(uuid, db, user)


@router.post("/{uuid}/settlement/paid")
def settlement_paid(uuid: UUID, body: PaidIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _m(db, user)
    service.mark_settlement_paid(db, service.get(db, user["company_id"], uuid), body.paid_on, body.reference)
    db.commit()
    return detail(uuid, db, user)


@router.post("/{uuid}/letters")
def letters(uuid: UUID, body: LettersIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _m(db, user)
    made = service.generate_letters(db, service.get(db, user["company_id"], uuid), body.types)
    if not made:
        raise BadRequest("Nothing new to issue: the letters are already issued or no template is set up.")
    db.commit()
    return detail(uuid, db, user)


@router.post("/{uuid}/complete")
def complete(uuid: UUID, body: CompleteIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _m(db, user)
    service.complete(db, service.get(db, user["company_id"], uuid), force=body.force)
    db.commit()
    return detail(uuid, db, user)


@router.post("/{uuid}/interview")
def interview(uuid: UUID, body: InterviewIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    x = _exit(db, user, uuid)
    iv = db.query(ExitInterview).filter(ExitInterview.exit_id == x.id).first()
    if iv is None:
        emp = c.employee(db, x.company_id, x.employee_id)
        service._make_interview(db, x, emp)
        iv = db.query(ExitInterview).filter(ExitInterview.exit_id == x.id).first()
        if iv is None:
            raise BadRequest("This kind of exit has no interview.")
    service.submit_interview(db, user, iv, body.model_dump(exclude_unset=True))
    db.commit()
    return detail(uuid, db, user)


# ── Public alumni portal ────────────────────────────────────────────

def _alumni(db: Session, token: str) -> Exit:
    with bypass_rls(db):
        row = db.query(Exit.company_id).filter(Exit.alumni_token == token).first()
    if row is None or not token:
        raise NotFound("Alumni link")
    set_tenant(db, row.company_id)
    return service.alumni_by_token(db, token)


@public_router.get("/{token}")
def alumni(token: str, db: Session = Depends(get_db)):
    x = _alumni(db, token)
    x.alumni_last_seen_at = c.now()
    out = service.alumni_out(db, x)
    db.commit()
    return out


@public_router.get("/{token}/payslips/{uuid}.pdf")
def alumni_payslip(token: str, uuid: UUID, db: Session = Depends(get_db)):
    x = _alumni(db, token)
    try:
        from modules.payroll.api_runs import payslip_pdf_bytes
        from modules.payroll.models import Payslip
    except ImportError:
        raise NotFound("Payslip")
    p = db.query(Payslip).filter(Payslip.company_id == x.company_id, Payslip.employee_id == x.employee_id,
                                 Payslip.uuid == uuid, Payslip.published.is_(True)).first()
    if p is None:
        raise NotFound("Payslip")
    data = payslip_pdf_bytes(db, x.company_id, p, with_password=False)
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="payslip-{p.period_start:%Y-%m}.pdf"'})


@public_router.get("/{token}/tax/{fy}.pdf")
def alumni_form16(token: str, fy: str, db: Session = Depends(get_db)):
    x = _alumni(db, token)
    emp = c.employee(db, x.company_id, x.employee_id)
    try:
        from modules.payroll.models import PayrollFile
        f = (db.query(PayrollFile).filter(PayrollFile.company_id == x.company_id, PayrollFile.employee_id == emp.id,
                                          PayrollFile.fy == fy, PayrollFile.kind.like("form16%")).first())
        if f is not None:
            return Response(f.content, media_type=f.content_type,
                            headers={"Content-Disposition": f'attachment; filename="{f.filename}"'})
        from modules.payroll import taxsvc
        content = taxsvc.form16_pdf(db, x.company_id, emp, fy)
    except ImportError:
        raise NotFound("Tax certificate")
    except BadRequest:
        raise
    except Exception:
        raise BadRequest("The tax certificate for this year is not ready yet. Please contact HR.")
    return Response(content, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="Form16-{fy}.pdf"'})


extra_routers = [public_router]
