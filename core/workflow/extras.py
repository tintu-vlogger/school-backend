"""More approval actions on top of the engine in service.py.

add_comment()        comments and attachments on a request
nudge()              the requester reminds the approvers (with a cooldown)
send_reminders()     periodic: "remind every N hours" on steps that ask for it
bulk_reassign()      forward many pending approvals at once (up to 200)
reassign_departed()  pending approvals of people who left go to their manager or the fallback
revoke()             cancel an already-approved request, directly or through its own approval
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session, joinedload

from core import audit, context
from core.errors import BadRequest, Forbidden
from core.workflow import registry
from core.workflow import service as wf
from core.workflow import settings as wf_settings
from core.workflow.models import WorkflowComment, WorkflowInstance, WorkflowTask

REVOKE_TYPE = "workflow.revoke"
MAX_BULK = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Comments and attachments ────────────────────────────────────────

def comments(db: Session, company_id: int, instance: WorkflowInstance) -> list[WorkflowComment]:
    return (db.query(WorkflowComment).filter(WorkflowComment.company_id == company_id,
                                             WorkflowComment.instance_id == instance.id)
            .order_by(WorkflowComment.created_at, WorkflowComment.id).all())


def add_comment(db: Session, company_id: int, instance: WorkflowInstance, body: str | None,
                attachment_uuids: list | None = None) -> WorkflowComment:
    body = (body or "").strip() or None
    files = []
    if attachment_uuids:
        from modules.documents import service as docs
        files = docs.attachments_from(db, company_id, attachment_uuids, limit=10)
        if len(files) != len(set(str(u) for u in attachment_uuids)):
            raise BadRequest("One of the files was not found. Upload it again.")
    if not body and not files:
        raise BadRequest("Write a comment or attach a file.")
    if body and len(body) > 4000:
        raise BadRequest("Keep the comment under 4,000 characters.")
    ctx = context.current()
    row = WorkflowComment(company_id=company_id, instance_id=instance.id, author_employee_id=ctx.employee_id,
                          author_name=ctx.actor_name, body=body, attachments=files)
    db.add(row)
    db.flush()
    # Tell the others involved: the requester and the people with a pending task.
    targets: set = set()
    if instance.requester_employee_id != ctx.employee_id or ctx.actor_type == "owner":
        targets.add(("e", instance.requester_employee_id))
    for t in instance.tasks:
        if t.status == wf.OPEN:
            if t.approver_kind == "owner" and ctx.actor_type != "owner":
                targets.add(("o", None))
            elif t.approver_employee_id and t.approver_employee_id != ctx.employee_id:
                targets.add(("e", t.approver_employee_id))
    what = "attached a file to" if files and not body else "commented on"
    for kind, eid in targets:
        if kind == "e" and eid is None and ctx.actor_type == "owner":
            continue
        wf._send(db, instance, eid, f"{ctx.actor_name or 'Someone'} {what} {instance.title}.",
                 kind="approval.comment", owner=(kind == "o" or eid is None))
    return row


# ── Nudge and reminders ─────────────────────────────────────────────

def nudge(db: Session, company_id: int, instance: WorkflowInstance) -> dict:
    """The requester reminds the people who still have to decide."""
    ctx = context.current()
    mine = (instance.requester_employee_id is not None and instance.requester_employee_id == ctx.employee_id) or \
           (instance.requester_employee_id is None and ctx.actor_type == "owner")
    if not mine:
        raise Forbidden("Only the person who asked can send a reminder.")
    if instance.status != wf.OPEN:
        raise BadRequest("This request is no longer waiting for a decision.")
    cooldown = int(wf_settings.get(db, company_id)["nudge_cooldown_hours"])
    if instance.last_nudged_at and instance.last_nudged_at > _now() - timedelta(hours=cooldown):
        wait = instance.last_nudged_at + timedelta(hours=cooldown) - _now()
        hours = max(1, int(wait.total_seconds() // 3600) + (1 if wait.total_seconds() % 3600 else 0))
        raise BadRequest(f"You reminded them recently. You can send another reminder in about {hours} hour(s).")
    pending = [t for t in instance.tasks if t.status == wf.OPEN]
    if not pending:
        raise BadRequest("Nobody is waiting to decide on this request.")
    for t in pending:
        wf.notify_task(db, t, f"Reminder from {ctx.actor_name or 'the requester'}: {instance.title} is waiting "
                              f"for your approval.", kind="approval.nudge")
    instance.last_nudged_at = _now()
    instance.nudge_count = (instance.nudge_count or 0) + 1
    audit.event(db, "workflow.nudged", "workflow_instances", instance.id,
                {"approvers": [t.approver_name for t in pending]}, entity_uuid=str(instance.uuid),
                entity_label=instance.title)
    db.flush()
    return {"reminded": len(pending), "next_allowed_at": instance.last_nudged_at + timedelta(hours=cooldown)}


def send_reminders(db: Session, now: datetime | None = None) -> int:
    """Periodic job: remind approvers on steps set to "remind every N hours"."""
    now = now or _now()
    rows = (db.query(WorkflowTask).options(joinedload(WorkflowTask.instance).joinedload(WorkflowInstance.definition))
            .join(WorkflowInstance, WorkflowTask.instance_id == WorkflowInstance.id)
            .filter(WorkflowTask.status == wf.OPEN, WorkflowInstance.status == wf.OPEN,
                    WorkflowInstance.definition_id.isnot(None))
            .limit(2000).all())
    n = 0
    for t in rows:
        steps = t.instance.definition.steps if t.instance.definition else []
        step = steps[t.step_index] if t.step_index < len(steps) else {}
        every = step.get("remind_every_hours")
        if not every:
            continue
        last = t.last_reminded_at or t.created_at
        if last and last > now - timedelta(hours=int(every)):
            continue
        wf.notify_task(db, t, f"Reminder: {t.instance.title} is still waiting for your approval.",
                       kind="approval.reminder")
        t.last_reminded_at = now
        t.reminder_count = (t.reminder_count or 0) + 1
        n += 1
    db.flush()
    return n


# ── Forwarding many at once and approvers who leave ─────────────────

def _move(db: Session, task: WorkflowTask, target, note: str, by: str | None) -> WorkflowTask:
    task.status = "reassigned"
    task.comment = note
    task.decided_at = _now()
    task.decided_by_name = by
    task.decided_via = "system" if by and by.startswith("System") else "bulk"
    new = wf._make_task(db, task.instance, task.step_index, {"name": task.step_name}, target, task.due_at)
    audit.event(db, "workflow.reassigned", "workflow_instances", task.instance_id,
                {"from": task.approver_name, "to": new.approver_name, "comment": note},
                company_id=task.company_id, entity_uuid=str(task.instance.uuid), entity_label=task.instance.title)
    return new


def bulk_reassign(db: Session, company_id: int, to_employee_id: int, *, task_uuids: list | None = None,
                  from_employee_id: int | None = None, comment: str | None = None, can_manage: bool = False) -> dict:
    """Forward pending approvals to someone else: the chosen ones, or every pending one of
    a person (admins only)."""
    target = wf._active_employee(db, company_id, to_employee_id)
    if target is None:
        raise BadRequest("Pick an active employee to pass the approvals to.")
    q = (db.query(WorkflowTask).options(joinedload(WorkflowTask.instance))
         .join(WorkflowInstance, WorkflowTask.instance_id == WorkflowInstance.id)
         .filter(WorkflowTask.company_id == company_id, WorkflowTask.status == wf.OPEN,
                 WorkflowInstance.status == wf.OPEN))
    if task_uuids:
        if len(task_uuids) > MAX_BULK:
            raise BadRequest(f"Pass on up to {MAX_BULK} approvals at a time.")
        q = q.filter(WorkflowTask.uuid.in_([str(u) for u in task_uuids]))
    elif from_employee_id:
        if not can_manage:
            raise Forbidden("Only an admin can pass on someone else's approvals.")
        q = q.filter(WorkflowTask.approver_employee_id == from_employee_id)
    else:
        raise BadRequest("Pick the approvals to pass on.")
    tasks = q.order_by(WorkflowTask.id).limit(MAX_BULK + 1).all()
    if len(tasks) > MAX_BULK:
        raise BadRequest(f"There are more than {MAX_BULK}. Pass them on in smaller groups.")
    ctx = context.current()
    moved, skipped = 0, []
    for t in tasks:
        if not (wf.is_my_task(t) or can_manage):
            skipped.append({"task_uuid": str(t.uuid), "detail": "Not assigned to you."})
            continue
        if t.approver_employee_id == target.id:
            skipped.append({"task_uuid": str(t.uuid), "detail": "Already with that person."})
            continue
        if t.instance.requester_employee_id == target.id:
            skipped.append({"task_uuid": str(t.uuid), "detail": "That person asked for it."})
            continue
        _move(db, t, target, (comment or "").strip() or "Passed on", ctx.actor_name)
        moved += 1
    db.flush()
    return {"moved": moved, "skipped": skipped}


def reassign_departed(db: Session, company_id: int | None = None, employee_ids: list[int] | None = None,
                      leaving: bool = False) -> int:
    """Pending approvals held by people who are no longer active go to their manager or
    the company's fallback. Runs periodically, and right before someone's exit is saved
    (`leaving=True` with their ids: they are still active in the database then)."""
    from modules.employees.models import Employee
    q = (db.query(WorkflowTask).options(joinedload(WorkflowTask.instance))
         .join(WorkflowInstance, WorkflowTask.instance_id == WorkflowInstance.id)
         .join(Employee, Employee.id == WorkflowTask.approver_employee_id)
         .filter(WorkflowTask.status == wf.OPEN, WorkflowInstance.status == wf.OPEN,
                 WorkflowTask.approver_kind == "employee"))
    if not leaving:
        q = q.filter(Employee.active.is_(False))
    if company_id is not None:
        q = q.filter(WorkflowTask.company_id == company_id)
    if employee_ids:
        q = q.filter(WorkflowTask.approver_employee_id.in_(employee_ids))
    elif leaving:
        return 0
    count = 0
    for t in q.limit(500).all():
        cid = t.company_id
        cfg = wf_settings.get(db, cid)
        leaver = db.query(Employee).filter(Employee.id == t.approver_employee_id).first()
        requester = wf._employee_by_id(db, cid, t.instance.requester_employee_id)
        targets = []
        if cfg["on_exit"]["action"] == "manager" and leaver is not None:
            mgr = wf._active_employee(db, cid, leaver.reporting_manager_id)
            if mgr is not None:
                targets = [mgr]
        if not targets:
            targets = wf_settings.fallback_approvers(db, cid, requester, "on_exit") or ["owner"]
        targets = [x for x in targets if x == "owner" or requester is None or x.id != requester.id] or ["owner"]
        _move(db, t, targets[0], f"{t.approver_name} has left", "System (approver left)")
        count += 1
    if not leaving:  # called from inside a save otherwise
        db.flush()
    return count


# ── Cancel after approval ───────────────────────────────────────────

def revoke_mode(db: Session, company_id: int, request_type: str) -> str:
    rt = registry.get(request_type)
    if rt is None or rt.on_revoked is None:
        return "off"
    return wf_settings.get(db, company_id)["revoke"].get(request_type, "off")


def pending_revoke(db: Session, company_id: int, instance: WorkflowInstance) -> WorkflowInstance | None:
    return (db.query(WorkflowInstance)
            .filter(WorkflowInstance.company_id == company_id, WorkflowInstance.request_type == REVOKE_TYPE,
                    WorkflowInstance.status == wf.OPEN, WorkflowInstance.subject_type == "workflow_instance",
                    WorkflowInstance.subject_id == instance.id).first())


def revoke(db: Session, company_id: int, instance: WorkflowInstance, reason: str | None,
           can_manage: bool = False) -> dict:
    """Cancel an approved request. Depending on the company's setting for its kind this
    happens at once or after its own approval."""
    ctx = context.current()
    reason = (reason or "").strip()
    if not reason:
        raise BadRequest("Say why the approved request should be cancelled.")
    if instance.status != "approved":
        raise BadRequest("Only approved requests can be cancelled this way.")
    mine = (instance.requester_employee_id is not None and instance.requester_employee_id == ctx.employee_id) or \
           (instance.requester_employee_id is None and ctx.actor_type == "owner")
    if not (mine or can_manage):
        raise Forbidden("Only the person who asked, or an admin, can cancel this request.")
    mode = revoke_mode(db, company_id, instance.request_type)
    if mode == "off" and not can_manage:
        raise BadRequest("This kind of request can't be cancelled after approval.")
    if registry.get(instance.request_type) is None or registry.get(instance.request_type).on_revoked is None:
        raise BadRequest("This kind of request can't be cancelled after approval.")
    if pending_revoke(db, company_id, instance):
        raise BadRequest("A cancellation of this request is already waiting for approval.")
    if mode == "direct" or (can_manage and not mine):
        do_revoke(db, instance, reason)
        return {"status": "revoked", "request_uuid": str(instance.uuid)}
    req = wf.submit(db, company_id, REVOKE_TYPE,
                    {"instance_uuid": str(instance.uuid), "request_type": instance.request_type,
                     "reference_no": instance.reference_no, "title": instance.title, "reason": reason},
                    f"Cancel {instance.title}", subject_type="workflow_instance", subject_id=instance.id)
    return {"status": req.status, "request_uuid": str(req.uuid), "pending": req.status == wf.OPEN,
            "revoked": instance.status == "revoked"}


def do_revoke(db: Session, instance: WorkflowInstance, reason: str | None) -> None:
    if instance.status != "approved":
        raise BadRequest("Only approved requests can be cancelled.")
    rt = registry.get(instance.request_type)
    if rt is None or rt.on_revoked is None:
        raise BadRequest("This kind of request can't be cancelled after approval.")
    rt.on_revoked(db, instance)
    instance.status = "revoked"
    instance.revoked_at = _now()
    instance.revoke_reason = reason
    audit.event(db, "workflow.revoked", "workflow_instances", instance.id, {"reason": reason},
                company_id=instance.company_id, entity_uuid=str(instance.uuid), entity_label=instance.title)
    wf._notify(db, instance, instance.requester_employee_id, f"{instance.title} was cancelled after approval.")
    db.flush()
    wf.emit_event(db, instance, "revoked")


def _revoke_target(db: Session, req: WorkflowInstance) -> WorkflowInstance | None:
    if req.subject_type != "workflow_instance" or not req.subject_id:
        return None
    return db.query(WorkflowInstance).filter(WorkflowInstance.company_id == req.company_id,
                                             WorkflowInstance.id == req.subject_id).first()


def _on_revoke_submit(db: Session, req: WorkflowInstance) -> None:
    target = _revoke_target(db, req)
    if target is None or target.status != "approved":
        raise BadRequest("Only approved requests can be cancelled.")


def _on_revoke_approved(db: Session, req: WorkflowInstance) -> None:
    target = _revoke_target(db, req)
    if target is not None and target.status == "approved":
        do_revoke(db, target, (req.payload or {}).get("reason"))


def _summ_revoke(i: WorkflowInstance) -> list[str]:
    p = i.payload or {}
    rt = registry.get(p.get("request_type") or "")
    return [f"{rt.label if rt else 'Request'} {p.get('reference_no') or ''}".strip(),
            f"Reason: {p.get('reason')}" if p.get("reason") else ""]


registry.register(registry.RequestType(
    key=REVOKE_TYPE,
    label="Cancel an approved request",
    description="Someone asks to cancel a request that was already approved.",
    on_submit=_on_revoke_submit,
    on_approved=_on_revoke_approved,
    summarize=_summ_revoke,
    variables={"request.request_type": "Kind of request being cancelled", "request.reason": "Reason"},
    default_steps=[{"name": "Reporting manager", "approver_type": "reporting_manager", "mode": "any"}],
))
