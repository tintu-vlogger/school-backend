"""Approvals: flow setup, the approvals inbox, requests, delegation and escalation,
comments and attachments, nudges, cancelling approved requests, approval settings,
one-click email links and the approvals history report."""
import io
from datetime import date

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from core import context
from core.db import get_db
from core.errors import BadRequest, Forbidden, NotFound
from core.pagination import PageParams, paginate
from core.security import _get_role_permissions, get_current_user, require_permission
from core.timeutil import iso_utc
from core.workflow import registry
from core.workflow import service as wf
from core.workflow.models import WorkflowDefinition, WorkflowDelegation, WorkflowInstance, WorkflowTask
from core.workflow import email_actions, extras, reports
from core.workflow import settings as wf_settings
from modules.workflow.schemas import (BulkDecisionIn, BulkReassignIn, CancelIn, CommentIn, DecisionIn, DefinitionIn,
                                      DefinitionUpdate, DelegationIn, EmailActionIn, ReassignIn, RevokeIn,
                                      SettingsIn)

router = APIRouter(prefix="/workflow", tags=["Approvals"])
MANAGE = require_permission("settings.workflows.manage")


def _can_manage(db: Session, current_user: dict) -> bool:
    return "settings.workflows.manage" in _get_role_permissions(current_user["company_id"], current_user["role"], db)


def _type_label(key: str) -> str:
    rt = registry.get(key)
    return rt.label if rt else key


# ── Setup ───────────────────────────────────────────────────────────

@router.get("/meta")
def meta(current_user: dict = Depends(get_current_user)):
    return {
        "approver_types": [{"value": k, "label": v} for k, v in wf.APPROVER_TYPES.items()],
        "modes": [{"value": k, "label": v} for k, v in wf.MODES.items()],
        "timeout_actions": [{"value": k, "label": v} for k, v in wf.TIMEOUT_ACTIONS.items()],
        "roles": [{"value": r, "label": label} for r, label in
                  (("admin", "HR admin"), ("hr", "HR"), ("manager", "Manager"), ("employee", "Employee"))],
    }


@router.get("/request-types")
def request_types(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    defs = wf.current_definitions(db, current_user["company_id"])
    counts: dict = {}
    for d in defs:
        counts[d.request_type] = counts.get(d.request_type, 0) + 1
    return [{"key": rt.key, "label": rt.label, "description": rt.description, "flows": counts.get(rt.key, 0),
             "variables": [{"name": k, "label": v} for k, v in {**rt.variables, **registry.COMMON_VARIABLES}.items()]}
            for rt in registry.all_types()]


def _def_out(d: WorkflowDefinition) -> dict:
    return {"uuid": str(d.uuid), "lineage_id": str(d.lineage_id), "version": d.version, "is_current": d.is_current,
            "request_type": d.request_type, "request_type_label": _type_label(d.request_type), "name": d.name,
            "description": d.description, "applies_when": d.applies_when, "priority": d.priority, "steps": d.steps,
            "active": d.active, "change_note": d.change_note, "created_by_name": d.created_by_name,
            "created_at": iso_utc(d.created_at)}


@router.get("/definitions")
def list_definitions(request_type: str | None = None, current_user: dict = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    return [_def_out(d) for d in wf.current_definitions(db, current_user["company_id"], request_type)]


def _definition(db, cid, uuid) -> WorkflowDefinition:
    row = db.query(WorkflowDefinition).filter(WorkflowDefinition.company_id == cid, WorkflowDefinition.uuid == uuid).first()
    if not row:
        raise NotFound("Approval flow")
    return row


@router.get("/definitions/{uuid}")
def get_definition(uuid: str, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    d = _definition(db, cid, uuid)
    versions = (db.query(WorkflowDefinition).filter(WorkflowDefinition.company_id == cid,
                                                    WorkflowDefinition.lineage_id == d.lineage_id)
                .order_by(WorkflowDefinition.version.desc()).all())
    return {**_def_out(d), "versions": [_def_out(v) for v in versions]}


@router.post("/definitions", status_code=201)
def create_definition(req: DefinitionIn, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    data = req.model_dump()
    row = wf.create_definition(db, current_user["company_id"], data)
    db.commit()
    return _def_out(row)


@router.put("/definitions/{uuid}")
def update_definition(uuid: str, req: DefinitionUpdate, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    data = req.model_dump(exclude_unset=True)
    row = wf.new_version(db, cid, _definition(db, cid, uuid), data)
    db.commit()
    return _def_out(row)


# ── Inbox ───────────────────────────────────────────────────────────

def _mine_filter(q):
    ctx = context.current()
    if ctx.actor_type == "owner":
        # The owner approves what goes to "the company owner" and what goes to their own
        # employee record (for example as someone's manager).
        if ctx.employee_id:
            return q.filter(or_(WorkflowTask.approver_kind == "owner", WorkflowTask.approver_employee_id == ctx.employee_id))
        return q.filter(WorkflowTask.approver_kind == "owner")
    return q.filter(WorkflowTask.approver_employee_id == ctx.employee_id)


def _instance_brief(i: WorkflowInstance) -> dict:
    rt = registry.get(i.request_type)
    return {"uuid": str(i.uuid), "reference_no": i.reference_no, "title": i.title, "request_type": i.request_type,
            "request_type_label": _type_label(i.request_type), "requester_name": i.requester_name, "status": i.status,
            "created_at": iso_utc(i.created_at), "completed_at": iso_utc(i.completed_at),
            "summary": rt.summarize(i) if rt and rt.summarize else []}


def _task_out(t: WorkflowTask) -> dict:
    return {"uuid": str(t.uuid), "step_name": t.step_name, "step_index": t.step_index, "status": t.status,
            "approver_name": t.approver_name, "approver_kind": t.approver_kind, "comment": t.comment,
            "decided_by_name": t.decided_by_name, "decided_at": iso_utc(t.decided_at), "due_at": iso_utc(t.due_at),
            "escalated": t.escalated, "delegated": t.delegated_from_employee_id is not None,
            "decided_via": t.decided_via, "reminder_count": t.reminder_count or 0,
            "created_at": iso_utc(t.created_at)}


@router.get("/inbox")
def inbox(status: str = Query("pending", pattern="^(pending|done)$"), q: str | None = None,
          request_type: str | None = None, task: str | None = None, pages: PageParams = Depends(),
          current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Approvals assigned to the signed-in person: waiting ones, or ones already decided."""
    query = (_mine_filter(db.query(WorkflowTask)).options(joinedload(WorkflowTask.instance))
             .join(WorkflowInstance, WorkflowTask.instance_id == WorkflowInstance.id)
             .filter(WorkflowTask.company_id == current_user["company_id"]))
    if status == "pending":
        query = query.filter(WorkflowTask.status == "pending", WorkflowInstance.status == "pending") \
            .order_by(WorkflowTask.due_at.asc().nullslast(), WorkflowTask.created_at.asc())
    else:
        query = query.filter(WorkflowTask.status.in_(["approved", "rejected", "reassigned", "escalated"])) \
            .order_by(WorkflowTask.decided_at.desc().nullslast())
    if request_type:
        query = query.filter(WorkflowInstance.request_type == request_type)
    if task:
        query = query.filter(WorkflowTask.uuid == task)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(WorkflowInstance.title.ilike(like), WorkflowInstance.reference_no.ilike(like),
                                 WorkflowInstance.requester_name.ilike(like)))
    return paginate(query, pages, lambda t: {**_task_out(t), "request": _instance_brief(t.instance)})


@router.get("/inbox/count")
def inbox_count(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    n = (_mine_filter(db.query(WorkflowTask.id)).filter(WorkflowTask.company_id == current_user["company_id"],
                                                        WorkflowTask.status == "pending").count())
    return {"count": n}


@router.post("/tasks/{uuid}/decision")
def decide(uuid: str, req: DecisionIn, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    inst = wf.decide(db, cid, wf.get_task(db, cid, uuid), req.decision, req.comment)
    db.commit()
    return _instance_brief(inst)


@router.post("/tasks/bulk-decision")
def bulk_decide(req: BulkDecisionIn, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Approve or reject many at once. Each one is saved on its own, so one problem does
    not stop the rest; the answer says what happened to each."""
    cid = current_user["company_id"]
    results = []
    for uuid in dict.fromkeys(req.task_uuids):
        try:
            inst = wf.decide(db, cid, wf.get_task(db, cid, uuid), req.decision, req.comment, via="bulk")
            db.commit()
            results.append({"task_uuid": uuid, "ok": True, "status": inst.status})
        except (BadRequest, Forbidden, NotFound) as e:
            db.rollback()
            results.append({"task_uuid": uuid, "ok": False, "detail": e.detail})
    done = sum(1 for r in results if r["ok"])
    return {"done": done, "failed": len(results) - done, "results": results}


@router.post("/tasks/{uuid}/reassign")
def reassign(uuid: str, req: ReassignIn, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    task = wf.reassign(db, cid, wf.get_task(db, cid, uuid), req.to_employee_id, req.comment,
                       _can_manage(db, current_user))
    db.commit()
    return _task_out(task)


# ── Requests ────────────────────────────────────────────────────────

@router.get("/requests")
def list_requests(scope: str = Query("mine", pattern="^(mine|all)$"), status: str | None = None,
                  request_type: str | None = None, q: str | None = None, pages: PageParams = Depends(),
                  current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    query = db.query(WorkflowInstance).filter(WorkflowInstance.company_id == current_user["company_id"])
    if scope == "mine":
        ctx = context.current()
        if ctx.actor_type == "owner" and ctx.employee_id:
            query = query.filter(or_(WorkflowInstance.requester_employee_id == ctx.employee_id,
                                     WorkflowInstance.requester_employee_id.is_(None)))
        elif ctx.employee_id:
            query = query.filter(WorkflowInstance.requester_employee_id == ctx.employee_id)
        else:
            query = query.filter(WorkflowInstance.requester_employee_id.is_(None))
    elif not _can_manage(db, current_user):
        raise Forbidden("You can only see your own requests.")
    if status:
        query = query.filter(WorkflowInstance.status == status)
    if request_type:
        query = query.filter(WorkflowInstance.request_type == request_type)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(WorkflowInstance.title.ilike(like), WorkflowInstance.reference_no.ilike(like),
                                 WorkflowInstance.requester_name.ilike(like)))
    return paginate(query.order_by(WorkflowInstance.created_at.desc()), pages, _instance_brief)


@router.get("/requests/{uuid}")
def get_request(uuid: str, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    inst = wf.get_instance(db, cid, uuid)
    ctx = context.current()
    is_owner = ctx.actor_type == "owner"
    asked = (ctx.employee_id is not None and inst.requester_employee_id == ctx.employee_id) \
        or (is_owner and inst.requester_employee_id is None)
    approver = any((t.approver_kind == "owner" and is_owner)
                   or (ctx.employee_id is not None and t.approver_employee_id == ctx.employee_id) for t in inst.tasks)
    involved = asked or approver
    if not involved and not _can_manage(db, current_user):
        raise NotFound("Request")
    steps = inst.definition.steps if inst.definition else []
    cfg = wf_settings.get(db, cid)
    from datetime import timedelta
    next_nudge = (inst.last_nudged_at + timedelta(hours=int(cfg["nudge_cooldown_hours"]))) if inst.last_nudged_at else None
    revoke_mode = extras.revoke_mode(db, cid, inst.request_type)
    manage = _can_manage(db, current_user)
    revoke_req = extras.pending_revoke(db, cid, inst) if inst.status == "approved" else None
    return {**_instance_brief(inst), "payload": inst.payload, "result_note": inst.result_note,
            "can_nudge": inst.status == "pending" and asked,
            "next_nudge_at": iso_utc(next_nudge), "nudge_count": inst.nudge_count or 0,
            "last_nudged_at": iso_utc(inst.last_nudged_at),
            "revoke_mode": revoke_mode,
            "can_revoke": inst.status == "approved" and revoke_req is None and
            ((asked and revoke_mode != "off") or (manage and revoke_mode != "off")),
            "revoke_pending": {"uuid": str(revoke_req.uuid), "reference_no": revoke_req.reference_no}
            if revoke_req else None,
            "revoked_at": iso_utc(inst.revoked_at), "revoke_reason": inst.revoke_reason,
            "comments": [_comment_out(db, cid, c) for c in extras.comments(db, cid, inst)],
            "can_withdraw": inst.status == "pending" and asked,
            "current_step": inst.current_step, "flow_name": inst.definition.name if inst.definition else None,
            "flow_version": inst.definition.version if inst.definition else None,
            "steps": [{"name": s["name"], "approver_type": s["approver_type"], "mode": s.get("mode", "any")} for s in steps],
            "tasks": [{**_task_out(t), "mine": wf.is_my_task(t) and t.status == "pending"} for t in inst.tasks]}


@router.post("/requests/{uuid}/cancel")
def cancel_request(uuid: str, req: CancelIn, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    inst = wf.cancel(db, cid, wf.get_instance(db, cid, uuid), req.reason)
    db.commit()
    return _instance_brief(inst)


# ── Delegation and escalation ───────────────────────────────────────

def _delegation_out(d: WorkflowDelegation, names: dict) -> dict:
    return {"uuid": str(d.uuid), "from_employee_id": d.from_employee_id, "from_name": names.get(d.from_employee_id),
            "to_employee_id": d.to_employee_id, "to_name": names.get(d.to_employee_id),
            "start_date": d.start_date.isoformat(), "end_date": d.end_date.isoformat(),
            "request_type": d.request_type, "request_type_label": _type_label(d.request_type) if d.request_type else "All requests",
            "note": d.note, "active": d.active}


@router.get("/delegations")
def list_delegations(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    from modules.employees.models import Employee
    cid = current_user["company_id"]
    q = db.query(WorkflowDelegation).filter(WorkflowDelegation.company_id == cid)
    ctx = context.current()
    if not _can_manage(db, current_user):
        q = q.filter(or_(WorkflowDelegation.from_employee_id == ctx.employee_id,
                         WorkflowDelegation.to_employee_id == ctx.employee_id))
    rows = q.order_by(WorkflowDelegation.start_date.desc()).limit(200).all()
    ids = {r.from_employee_id for r in rows} | {r.to_employee_id for r in rows}
    names = {e.id: f"{e.first_name} {e.last_name}".strip()
             for e in db.query(Employee).filter(Employee.company_id == cid, Employee.id.in_(ids)).all()} if ids else {}
    return [_delegation_out(r, names) for r in rows]


@router.post("/delegations", status_code=201)
def add_delegation(req: DelegationIn, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    ctx = context.current()
    from_id = req.from_employee_id or ctx.employee_id
    if from_id is None:
        raise BadRequest("Pick whose approvals are handed over.")
    if from_id != ctx.employee_id and not _can_manage(db, current_user):
        raise Forbidden("You can only hand over your own approvals.")
    row = wf.add_delegation(db, current_user["company_id"], from_id, req.to_employee_id, req.start_date, req.end_date,
                            req.request_type, req.note)
    db.commit()
    return {"uuid": str(row.uuid)}


@router.post("/delegations/{uuid}/stop")
def stop_delegation(uuid: str, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    ctx = context.current()
    row = db.query(WorkflowDelegation).filter(WorkflowDelegation.company_id == current_user["company_id"],
                                              WorkflowDelegation.uuid == uuid).first()
    if not row:
        raise NotFound("Delegation")
    if row.from_employee_id != ctx.employee_id and not _can_manage(db, current_user):
        raise Forbidden("You can only stop your own delegation.")
    row.active = False
    db.commit()
    return {"detail": "Delegation stopped"}


@router.post("/escalations/run")
def run_escalations(current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    """Check for late approvals now (also meant to be called by a scheduler)."""
    n = wf.escalate_overdue(db, current_user["company_id"])
    db.commit()
    return {"escalated": n}


# ── Comments, attachments, nudges and cancelling approved requests ──

def _involved(db: Session, current_user: dict, inst: WorkflowInstance) -> bool:
    ctx = context.current()
    is_owner = ctx.actor_type == "owner"
    asked = (ctx.employee_id is not None and inst.requester_employee_id == ctx.employee_id) \
        or (is_owner and inst.requester_employee_id is None)
    approver = any((t.approver_kind == "owner" and is_owner)
                   or (ctx.employee_id is not None and t.approver_employee_id == ctx.employee_id) for t in inst.tasks)
    return asked or approver or _can_manage(db, current_user)


def _comment_out(db: Session, cid: int, c) -> dict:
    files = c.attachments or []
    if files:
        try:
            from modules.documents import service as docs
            files = docs.attachments_with_links(db, cid, files)
        except Exception:
            pass
    ctx = context.current()
    return {"uuid": str(c.uuid), "author_name": c.author_name, "body": c.body, "attachments": files,
            "mine": (c.author_employee_id is not None and c.author_employee_id == ctx.employee_id),
            "created_at": iso_utc(c.created_at)}


@router.get("/requests/{uuid}/comments")
def list_comments(uuid: str, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    inst = wf.get_instance(db, cid, uuid)
    if not _involved(db, current_user, inst):
        raise NotFound("Request")
    return [_comment_out(db, cid, c) for c in extras.comments(db, cid, inst)]


@router.post("/requests/{uuid}/comments", status_code=201)
def add_comment(uuid: str, req: CommentIn, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """A comment and/or files (upload them first with POST /documents/uploads)."""
    cid = current_user["company_id"]
    inst = wf.get_instance(db, cid, uuid)
    if not _involved(db, current_user, inst):
        raise NotFound("Request")
    row = extras.add_comment(db, cid, inst, req.body, req.attachment_uuids)
    db.commit()
    return _comment_out(db, cid, row)


@router.post("/requests/{uuid}/nudge")
def nudge(uuid: str, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    out = extras.nudge(db, cid, wf.get_instance(db, cid, uuid))
    db.commit()
    return {"reminded": out["reminded"], "next_allowed_at": iso_utc(out["next_allowed_at"])}


@router.post("/requests/{uuid}/revoke")
def revoke(uuid: str, req: RevokeIn, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Cancel an approved request: at once or through its own approval, per kind of request."""
    cid = current_user["company_id"]
    out = extras.revoke(db, cid, wf.get_instance(db, cid, uuid), req.reason, _can_manage(db, current_user))
    db.commit()
    return out


@router.post("/tasks/bulk-reassign")
def bulk_reassign(req: BulkReassignIn, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Forward several pending approvals (yours, or as an admin everyone's of one person)."""
    cid = current_user["company_id"]
    out = extras.bulk_reassign(db, cid, req.to_employee_id, task_uuids=req.task_uuids,
                               from_employee_id=req.from_employee_id, comment=req.comment,
                               can_manage=_can_manage(db, current_user))
    db.commit()
    return out


@router.get("/pending-by-approver")
def pending_by_approver(current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    """Who holds how many pending approvals (for passing them on in bulk)."""
    from sqlalchemy import func
    cid = current_user["company_id"]
    rows = (db.query(WorkflowTask.approver_employee_id, WorkflowTask.approver_name, WorkflowTask.approver_kind,
                     func.count(WorkflowTask.id))
            .join(WorkflowInstance, WorkflowTask.instance_id == WorkflowInstance.id)
            .filter(WorkflowTask.company_id == cid, WorkflowTask.status == "pending",
                    WorkflowInstance.status == "pending")
            .group_by(WorkflowTask.approver_employee_id, WorkflowTask.approver_name, WorkflowTask.approver_kind)
            .order_by(func.count(WorkflowTask.id).desc()).limit(200).all())
    return [{"employee_id": e, "name": n, "kind": k, "pending": c} for e, n, k, c in rows]


# ── Approval settings ───────────────────────────────────────────────

@router.get("/settings")
def get_settings_(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cfg = wf_settings.get(db, current_user["company_id"])
    return {**cfg,
            "options": {
                "no_approver": [{"value": k, "label": v} for k, v in wf_settings.NO_APPROVER.items()],
                "on_exit": [{"value": k, "label": v} for k, v in wf_settings.ON_EXIT.items()],
                "revoke_modes": [{"value": k, "label": v} for k, v in wf_settings.REVOKE_MODES.items()],
                "revocable_types": [{"key": rt.key, "label": rt.label} for rt in wf_settings.revocable_types()],
            }}


@router.put("/settings")
def save_settings(req: SettingsIn, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    out = wf_settings.save(db, current_user["company_id"], req.model_dump(exclude_unset=True),
                           context.current().actor_name)
    db.commit()
    return out


@router.post("/approvers/hand-over")
def hand_over_now(current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    """Hand over now the pending approvals of everyone who has left."""
    n = extras.reassign_departed(db, current_user["company_id"])
    db.commit()
    return {"moved": n}


# ── Approve or reject from an email (no sign-in; the link is the key) ──

@router.get("/email-actions/{token}")
def email_action_info(token: str, db: Session = Depends(get_db)):
    out = email_actions.describe(db, token)
    return {**out, "created_at": iso_utc(out["created_at"]), "expires_at": iso_utc(out["expires_at"])}


@router.post("/email-actions/{token}")
def email_action_use(token: str, req: EmailActionIn, request: Request, db: Session = Depends(get_db)):
    ip = request.client.host if request.client else None
    out = email_actions.use(db, token, req.comment, ip, request.headers.get("user-agent"))
    db.commit()
    return out


# ── Approvals history report ────────────────────────────────────────

def _report_query(db, cid, date_from, date_to, request_type, approver_employee_id, status, q):
    from core.timeutil import tz_for
    from modules.company.models import Company
    tz = tz_for(db.query(Company).filter(Company.id == cid).first())
    return reports.base_query(db, cid, date_from=date_from, date_to=date_to, request_type=request_type,
                              approver_employee_id=approver_employee_id, status=status, q=q, tz=tz)


def _report_row_out(r: dict) -> dict:
    return {**r, "created_at": iso_utc(r["created_at"]), "decided_at": iso_utc(r["decided_at"]),
            "due_at": iso_utc(r["due_at"])}


@router.get("/reports/history")
def report_history(date_from: date | None = None, date_to: date | None = None, request_type: str | None = None,
                   approver_employee_id: int | None = None, status: str | None = None, q: str | None = None,
                   pages: PageParams = Depends(), current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    query = _report_query(db, current_user["company_id"], date_from, date_to, request_type, approver_employee_id,
                          status, q).order_by(WorkflowTask.created_at.desc())
    return paginate(query, pages, lambda t: _report_row_out(reports.row(t)))


@router.get("/reports/summary")
def report_summary(date_from: date | None = None, date_to: date | None = None, request_type: str | None = None,
                   approver_employee_id: int | None = None, current_user: dict = Depends(MANAGE),
                   db: Session = Depends(get_db)):
    rows = _report_query(db, current_user["company_id"], date_from, date_to, request_type, approver_employee_id,
                         None, None).limit(20000).all()
    return reports.summary(rows)


@router.get("/reports/history.csv")
def report_csv(date_from: date | None = None, date_to: date | None = None, request_type: str | None = None,
               approver_employee_id: int | None = None, status: str | None = None, q: str | None = None,
               current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    from core import audit
    from core import csv_safe
    rows = _report_query(db, current_user["company_id"], date_from, date_to, request_type, approver_employee_id,
                         status, q).order_by(WorkflowTask.created_at.desc()).limit(20000).all()
    buf = io.StringIO()
    w = csv_safe.writer(buf)
    w.writerow(["Reference", "Request", "Kind", "Requested by", "Step", "Approver", "Status", "Decided by", "How",
                "Asked at (UTC)", "Decided at (UTC)", "Due at (UTC)", "Hours taken", "Within time limit"])
    for t in rows:
        r = reports.row(t)
        w.writerow([x for x in (
            r["reference_no"], r["title"], r["request_type_label"], r["requester_name"], r["step_name"],
            r["approver_name"], r["status"], r["decided_by_name"], r["decided_via"], iso_utc(r["created_at"]),
            iso_utc(r["decided_at"]), iso_utc(r["due_at"]), r["hours_taken"] if r["hours_taken"] is not None else "",
            "" if r["within_sla"] is None else ("yes" if r["within_sla"] else "no"))])
    audit.event(db, "export.downloaded", "workflow_tasks", None, {"report": "approvals history", "rows": len(rows)})
    db.commit()
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="approvals-history.csv"'})
