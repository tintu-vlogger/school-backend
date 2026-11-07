"""The workflow engine.

submit()     find the matching flow, create the request and the first step's tasks
decide()     approve or reject a task; moves the request to the next step or finishes it
cancel()     the requester withdraws a pending request
reassign()   move a pending task to someone else
escalate_overdue()  periodic check: tasks past their SLA go to the escalation target

Approver types: reporting_manager, skip_level_manager, department_head, role, employee,
field (an employee id found on the request or the requester). When nobody can be found,
the task goes to the company owner so a request never gets stuck. Delegations are
applied when a task is created. Every decision is audited.
"""
from __future__ import annotations

import uuid as uuid_mod
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from core import audit, context, expressions
from core.errors import BadRequest, Forbidden, NotFound
from core.workflow import registry
from core.workflow.models import WorkflowDefinition, WorkflowDelegation, WorkflowInstance, WorkflowTask

APPROVER_TYPES = {
    "reporting_manager": "Reporting manager",
    "skip_level_manager": "Manager's manager",
    "department_head": "Department head",
    "role": "Everyone with a role",
    "employee": "A specific employee",
    "field": "Person named on the request",
    "owner": "Company owner",
}
MODES = {"any": "Any one approver", "all": "Every approver"}
# What happens when a step's time limit passes (no choice = escalate when a target is set,
# otherwise remind the approver once).
TIMEOUT_ACTIONS = {
    "escalate": "Send it to someone else",
    "approve": "Approve automatically",
    "reject": "Reject automatically",
    "remind": "Remind the approver",
}
OPEN = "pending"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _employee_model():
    from modules.employees.models import Employee
    return Employee


def _name(emp) -> str:
    return f"{emp.first_name} {emp.last_name}".strip() if emp else "Company owner"


# ── Definitions ─────────────────────────────────────────────────────

def clean_steps(steps: list[dict]) -> list[dict]:
    """Check and normalise the steps of a flow."""
    if not steps:
        raise BadRequest("Add at least one approval step.")
    if len(steps) > 15:
        raise BadRequest("A flow can have at most 15 steps.")
    out = []
    for i, s in enumerate(steps, start=1):
        kind = s.get("approver_type")
        if kind not in APPROVER_TYPES:
            raise BadRequest(f"Step {i}: pick who approves.")
        value = s.get("approver_value")
        if kind in ("role", "employee", "field") and value in (None, ""):
            raise BadRequest(f"Step {i}: say which {'role' if kind == 'role' else 'person' if kind == 'employee' else 'field'}.")
        mode = s.get("mode") or "any"
        if mode not in MODES:
            raise BadRequest(f"Step {i}: approval mode must be any or all.")
        cond = (s.get("condition") or "").strip() or None
        if cond:
            try:
                expressions.parse(cond)
            except expressions.ExpressionError as e:
                raise BadRequest(f"Step {i} condition: {e}")
        sla = s.get("sla_hours")
        if sla not in (None, ""):
            try:
                sla = int(sla)
            except (TypeError, ValueError):
                raise BadRequest(f"Step {i}: the time limit must be a whole number of hours.")
            if sla < 1 or sla > 24 * 90:
                raise BadRequest(f"Step {i}: the time limit must be between 1 hour and 90 days.")
        else:
            sla = None
        timeout_action = s.get("timeout_action") or None
        if timeout_action not in (None, *TIMEOUT_ACTIONS):
            raise BadRequest(f"Step {i}: pick what happens when the time limit passes.")
        remind = s.get("remind_every_hours")
        if remind not in (None, ""):
            try:
                remind = int(remind)
            except (TypeError, ValueError):
                raise BadRequest(f"Step {i}: reminders must be a whole number of hours.")
            if remind < 1 or remind > 24 * 30:
                raise BadRequest(f"Step {i}: remind every 1 hour to 30 days.")
        else:
            remind = None
        esc = s.get("escalate_to") or None
        if timeout_action == "escalate" and not esc:
            raise BadRequest(f"Step {i}: say who the request goes to when the time limit passes.")
        if timeout_action and not sla:
            raise BadRequest(f"Step {i}: set a time limit for the automatic action.")
        if esc:
            if esc.get("approver_type") not in APPROVER_TYPES:
                raise BadRequest(f"Step {i}: pick who the request goes to when it is late.")
            esc = {"approver_type": esc["approver_type"], "approver_value": esc.get("approver_value")}
        out.append({
            "key": s.get("key") or f"step{i}",
            "name": (s.get("name") or APPROVER_TYPES[kind]).strip()[:150],
            "approver_type": kind,
            "approver_value": value,
            "mode": mode,
            "condition": cond,
            "sla_hours": sla,
            "escalate_to": esc,
            "timeout_action": timeout_action,
            "remind_every_hours": remind,
            "skip_if_requester": bool(s.get("skip_if_requester", True)),
        })
    return out


def _check_condition_text(text: str | None) -> str | None:
    text = (text or "").strip() or None
    if text:
        try:
            expressions.parse(text)
        except expressions.ExpressionError as e:
            raise BadRequest(f"Condition: {e}")
    return text


def create_definition(db: Session, company_id: int, data: dict) -> WorkflowDefinition:
    if not registry.get(data["request_type"]):
        raise BadRequest("Unknown kind of request.")
    row = WorkflowDefinition(
        company_id=company_id, lineage_id=uuid_mod.uuid4(), version=1, is_current=True,
        request_type=data["request_type"], name=data["name"].strip(), description=data.get("description"),
        applies_when=_check_condition_text(data.get("applies_when")), priority=data.get("priority") or 100,
        steps=clean_steps(data.get("steps") or []), active=data.get("active", True),
        change_note=data.get("change_note"), created_by_name=context.current().actor_name,
    )
    db.add(row)
    db.flush()
    return row


def new_version(db: Session, company_id: int, current: WorkflowDefinition, data: dict) -> WorkflowDefinition:
    """Save changes as a new version; the old one stays for requests already running."""
    if not current.is_current:
        raise BadRequest("Only the latest version of a flow can be changed.")
    merged = {
        "name": current.name, "description": current.description, "applies_when": current.applies_when,
        "priority": current.priority, "steps": current.steps, "active": current.active,
        **{k: v for k, v in data.items() if v is not None or k in ("applies_when", "description")},
    }
    current.is_current = False
    row = WorkflowDefinition(
        company_id=company_id, lineage_id=current.lineage_id, version=current.version + 1, is_current=True,
        request_type=current.request_type, name=merged["name"].strip(), description=merged.get("description"),
        applies_when=_check_condition_text(merged.get("applies_when")), priority=merged["priority"],
        steps=clean_steps(merged["steps"]), active=merged["active"], change_note=data.get("change_note"),
        created_by_name=context.current().actor_name,
    )
    db.add(row)
    db.flush()
    return row


def ensure_default_definitions(db: Session, company_id: int) -> None:
    """Give a company the default flow of every registered request type it lacks."""
    have = {t for (t,) in db.query(WorkflowDefinition.request_type)
            .filter(WorkflowDefinition.company_id == company_id).distinct().all()}
    for rt in registry.all_types():
        if rt.key in have or not rt.default_steps:
            continue
        db.add(WorkflowDefinition(company_id=company_id, lineage_id=uuid_mod.uuid4(), version=1, is_current=True,
                                  request_type=rt.key, name=rt.label, steps=clean_steps(rt.default_steps),
                                  change_note="Default flow", created_by_name="System"))
    db.flush()


def current_definitions(db: Session, company_id: int, request_type: str | None = None):
    q = db.query(WorkflowDefinition).filter(WorkflowDefinition.company_id == company_id,
                                            WorkflowDefinition.is_current.is_(True))
    if request_type:
        q = q.filter(WorkflowDefinition.request_type == request_type)
    return q.order_by(WorkflowDefinition.request_type, WorkflowDefinition.priority, WorkflowDefinition.id).all()


# ── Variables for conditions ────────────────────────────────────────

def _requester_vars(emp) -> dict:
    if emp is None:
        return {"role": "admin", "is_owner": True}
    return {
        "id": emp.id, "role": emp.role, "department_id": emp.department_id, "designation_id": emp.designation_id,
        "employee_code": emp.employee_code, "reporting_manager_id": emp.reporting_manager_id,
        "custom": emp.custom or {}, "is_owner": False,
    }


def variables_for(instance: WorkflowInstance, requester) -> dict:
    return {"request": instance.payload or {}, "requester": _requester_vars(requester),
            "request_type": instance.request_type}


# ── Approver resolution ─────────────────────────────────────────────

def _active_employee(db: Session, company_id: int, employee_id) -> object | None:
    if employee_id in (None, ""):
        return None
    try:
        employee_id = int(employee_id)
    except (TypeError, ValueError):
        return None
    Employee = _employee_model()
    return db.query(Employee).filter(Employee.company_id == company_id, Employee.id == employee_id,
                                     Employee.active.is_(True)).first()


def _lookup_path(data: dict, path: str):
    cur = data
    for part in str(path).split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def resolve_approvers(db: Session, company_id: int, approver_type: str, value, requester, variables: dict):
    """Employees who approve a step. [] means none found. ["owner"] means the company owner."""
    Employee = _employee_model()
    if approver_type == "owner":
        return ["owner"]
    if approver_type == "reporting_manager":
        mgr = _active_employee(db, company_id, requester.reporting_manager_id) if requester else None
        return [mgr] if mgr else []
    if approver_type == "skip_level_manager":
        mgr = _active_employee(db, company_id, requester.reporting_manager_id) if requester else None
        top = _active_employee(db, company_id, mgr.reporting_manager_id) if mgr else None
        return [top] if top else []
    if approver_type == "department_head":
        if not requester or not requester.department_id:
            return []
        from modules.departments.models import Department
        dept = db.query(Department).filter(Department.id == requester.department_id).first()
        head = _active_employee(db, company_id, dept.head_employee_id) if dept else None
        return [head] if head else []
    if approver_type == "role":
        role = str(value or "").strip()
        rows = (db.query(Employee).filter(Employee.company_id == company_id, Employee.role == role,
                                          Employee.active.is_(True)).order_by(Employee.id).limit(50).all())
        if role == "admin":
            return ["owner", *rows]
        return rows
    if approver_type == "employee":
        emp = _active_employee(db, company_id, value)
        return [emp] if emp else []
    if approver_type == "field":
        found = _lookup_path(variables, str(value))
        emp = _active_employee(db, company_id, found)
        return [emp] if emp else []
    return []


def _delegate_for(db: Session, company_id: int, employee, request_type: str, today: date):
    d = (db.query(WorkflowDelegation)
         .filter(WorkflowDelegation.company_id == company_id, WorkflowDelegation.from_employee_id == employee.id,
                 WorkflowDelegation.active.is_(True), WorkflowDelegation.start_date <= today,
                 WorkflowDelegation.end_date >= today,
                 or_(WorkflowDelegation.request_type.is_(None), WorkflowDelegation.request_type == request_type))
         .order_by(WorkflowDelegation.id.desc()).first())
    if d is None:
        return None
    return _active_employee(db, company_id, d.to_employee_id)


def _today(db: Session, company_id: int) -> date:
    from core.timeutil import today_for_company_id
    return today_for_company_id(db, company_id)


def _make_task(db: Session, instance: WorkflowInstance, step_index: int, step: dict, approver, due_at,
               delegated_from=None) -> WorkflowTask:
    is_owner = approver == "owner"
    task = WorkflowTask(
        uuid=uuid_mod.uuid4(),
        company_id=instance.company_id, instance=instance, step_index=step_index, step_name=step["name"],
        approver_kind="owner" if is_owner else "employee",
        approver_employee_id=None if is_owner else approver.id,
        approver_name="Company owner" if is_owner else _name(approver),
        delegated_from_employee_id=delegated_from.id if delegated_from is not None else None,
        status=OPEN, due_at=due_at,
    )
    db.add(task)
    notify_task(db, task, f"{instance.title} is waiting for your approval.")
    return task


def notify_task(db: Session, task: WorkflowTask, title: str, kind: str = "approval") -> None:
    """Tell a task's approver (an employee or the company owner). The notification carries
    the task, so the email version can offer one-click approve and reject links."""
    instance = task.instance
    _send(db, instance, task.approver_employee_id, title, kind=kind, owner=task.approver_kind == "owner",
          link=f"/dashboard/inbox?task={task.uuid}", data={"task_uuid": str(task.uuid),
                                                             "instance_uuid": str(instance.uuid)})


def _send(db: Session, instance: WorkflowInstance, employee_id: int | None, title: str, *, kind: str = "approval",
          owner: bool = False, link: str | None = None, data: dict | None = None) -> None:
    if employee_id is None and not owner:
        return
    try:
        from modules.notifications.service import notify
    except Exception:  # pragma: no cover
        return
    ctx = context.current()
    notify(db, instance.company_id, [employee_id], kind=kind, title=title,
           body=instance.reference_no and f"{title} ({instance.reference_no})" or title,
           link=link or f"/dashboard/inbox?request={instance.uuid}", actor_name=ctx.actor_name,
           actor_employee_id=ctx.employee_id, data=data)


def _portal_requester(instance: WorkflowInstance) -> int | None:
    """Portal account (parent / student) that raised the request, if any."""
    return (instance.payload or {}).get("_portal_account_id")


def _notify(db: Session, instance: WorkflowInstance, employee_id: int | None, message: str) -> None:
    """Tell the requester (employee_id None = the owner asked) about their request. Requests
    raised by a parent or student go to their portal sign-in instead."""
    portal_id = _portal_requester(instance)
    if portal_id and employee_id is None:
        from modules.notifications.service import notify_portal
        notify_portal(db, instance.company_id, [portal_id], kind="approval.update", title=message[:200], body=message)
        return
    _send(db, instance, employee_id, message, kind="approval.update", owner=employee_id is None,
          link=f"/dashboard/inbox?request={instance.uuid}")


def _owner_employee_id(db: Session, company_id: int) -> int | None:
    from modules.company.owner import owner_employee_id
    return owner_employee_id(db, company_id)


def _start_step(db: Session, instance: WorkflowInstance, requester) -> bool:
    """Create tasks for the next step that applies, starting at instance.current_step.
    Returns False when no steps are left (the request is then approved)."""
    steps = instance.definition.steps if instance.definition else []
    variables = variables_for(instance, requester)
    # The company owner asking (with or without their own employee record): never ask the
    # owner to approve their own request.
    owner_asks = (requester is None and not _portal_requester(instance)) or \
        (requester is not None and requester.id == _owner_employee_id(db, instance.company_id))
    today = _today(db, instance.company_id)
    while instance.current_step < len(steps):
        idx = instance.current_step
        step = steps[idx]
        if step.get("condition"):
            try:
                applies = expressions.check_condition(step["condition"], variables, today=today)
            except expressions.ExpressionError:
                applies = True  # a broken condition never silently skips an approval
            if not applies:
                instance.current_step += 1
                continue
        approvers = resolve_approvers(db, instance.company_id, step["approver_type"], step.get("approver_value"),
                                      requester, variables)
        if step.get("skip_if_requester", True):
            if owner_asks:
                approvers = [a for a in approvers if a != "owner"]
            if requester is not None:
                approvers = [a for a in approvers if a == "owner" or a.id != requester.id]
        if not approvers:
            # Nobody to ask: follow the company's rule (by default the company owner decides,
            # unless the owner is the one asking).
            from core.workflow import settings as wf_settings
            approvers = wf_settings.fallback_approvers(db, instance.company_id, requester, "no_approver")
            if approvers is None:  # approve this step automatically
                instance.current_step += 1
                continue
            if owner_asks:
                approvers = [a for a in approvers if a != "owner"]
            if requester is not None:
                approvers = [a for a in approvers if a == "owner" or a.id != requester.id]
            if not approvers:
                if owner_asks:
                    instance.current_step += 1
                    continue
                approvers = ["owner"]
        due = _now() + timedelta(hours=step["sla_hours"]) if step.get("sla_hours") else None
        seen: set = set()
        for a in approvers:
            delegate = _delegate_for(db, instance.company_id, a, instance.request_type, today) if a != "owner" else None
            target = delegate or a
            key = "owner" if target == "owner" else target.id
            if key in seen:
                continue
            seen.add(key)
            _make_task(db, instance, idx, step, target, due, delegated_from=a if delegate else None)
        db.flush()
        return True
    return False


# ── Requests ────────────────────────────────────────────────────────

def _requester_from_context(db: Session, company_id: int):
    ctx = context.current()
    if ctx.employee_id:
        Employee = _employee_model()
        return db.query(Employee).filter(Employee.company_id == company_id, Employee.id == ctx.employee_id).first()
    return None


def submit(db: Session, company_id: int, request_type: str, payload: dict, title: str, *,
           subject_type: str | None = None, subject_id: int | None = None) -> WorkflowInstance:
    """Start a request. The matching flow is the first active current flow of this type
    (by priority) whose 'applies when' condition holds. Without a flow it is approved
    straight away."""
    rt = registry.get(request_type)
    if rt is None:
        raise BadRequest("Unknown kind of request.")
    requester = _requester_from_context(db, company_id)
    payload = dict(payload or {})
    if context.current().portal_account_id and requester is None:
        payload["_portal_account_id"] = context.current().portal_account_id
    instance = WorkflowInstance(
        company_id=company_id, request_type=request_type, title=title[:250], payload=payload,
        subject_type=subject_type, subject_id=subject_id,
        requester_employee_id=requester.id if requester else None,
        requester_name=_name(requester) if requester else (context.current().actor_name or "Company owner"),
        status=OPEN, current_step=0,
    )
    variables = variables_for(instance, requester)
    today = _today(db, company_id)
    for d in current_definitions(db, company_id, request_type):
        if not d.active:
            continue
        try:
            if expressions.check_condition(d.applies_when, variables, today=today):
                instance.definition = d
                break
        except expressions.ExpressionError:
            continue
    from core import numbering
    instance.reference_no = numbering.next_number(db, company_id, "request", today)
    if rt.on_submit:
        rt.on_submit(db, instance)
    db.add(instance)
    db.flush()
    audit.event(db, "workflow.submitted", "workflow_instances", instance.id,
                {"request_type": request_type, "reference": instance.reference_no}, entity_uuid=str(instance.uuid),
                entity_label=instance.title)
    if not _start_step(db, instance, requester):
        _finish(db, instance, "approved", "No approval needed")
    return instance


def _finish(db: Session, instance: WorkflowInstance, status: str, note: str | None = None) -> None:
    instance.status = status
    instance.completed_at = _now()
    instance.result_note = note
    for t in instance.tasks:
        if t.status == OPEN:
            t.status = "cancelled" if status == "cancelled" else "skipped"
    rt = registry.get(instance.request_type)
    handler = {"approved": rt and rt.on_approved, "rejected": rt and rt.on_rejected,
               "cancelled": rt and rt.on_cancelled}.get(status)
    if handler:
        handler(db, instance)
    words = {"approved": "approved", "rejected": "rejected", "cancelled": "withdrawn"}
    _notify(db, instance, instance.requester_employee_id, f"{instance.title} was {words.get(status, status)}.")
    db.flush()
    emit_event(db, instance, status)


def emit_event(db: Session, instance: WorkflowInstance, kind: str) -> None:
    """Announce a finished request to other modules (automation rules and others)."""
    from core import events
    events.emit(db, events.Event(kind=kind, entity="request", record_id=instance.id, company_id=instance.company_id,
                                 data={"request_type": instance.request_type, "reference_no": instance.reference_no,
                                       "title": instance.title, "requester_employee_id": instance.requester_employee_id,
                                       "payload": instance.payload or {}, "status": instance.status,
                                       "uuid": str(instance.uuid)}))


def get_instance(db: Session, company_id: int, uuid) -> WorkflowInstance:
    row = (db.query(WorkflowInstance).options(joinedload(WorkflowInstance.tasks), joinedload(WorkflowInstance.definition))
           .filter(WorkflowInstance.company_id == company_id, WorkflowInstance.uuid == uuid).first())
    if not row:
        raise NotFound("Request")
    return row


def get_task(db: Session, company_id: int, uuid) -> WorkflowTask:
    row = (db.query(WorkflowTask).options(joinedload(WorkflowTask.instance))
           .filter(WorkflowTask.company_id == company_id, WorkflowTask.uuid == uuid).first())
    if not row:
        raise NotFound("Approval")
    return row


def is_my_task(task: WorkflowTask) -> bool:
    ctx = context.current()
    if task.approver_kind == "owner":
        return ctx.actor_type == "owner"
    return ctx.employee_id is not None and task.approver_employee_id == ctx.employee_id


def decide(db: Session, company_id: int, task: WorkflowTask, decision: str, comment: str | None = None,
           via: str = "app") -> WorkflowInstance:
    if decision not in ("approve", "reject"):
        raise BadRequest("Choose approve or reject.")
    if task.status != OPEN or task.instance.status != OPEN:
        raise BadRequest("This request has already been decided.")
    if not is_my_task(task):
        raise Forbidden("This approval is not assigned to you.")
    if decision == "reject" and not (comment or "").strip():
        raise BadRequest("Add a comment saying why the request is rejected.")
    return apply_decision(db, company_id, task, decision, comment, decided_by=context.current().actor_name, via=via)


def apply_decision(db: Session, company_id: int, task: WorkflowTask, decision: str, comment: str | None, *,
                   decided_by: str | None, via: str) -> WorkflowInstance:
    """Record a decision (already checked) and move the request on."""
    instance = task.instance
    task.status = "approved" if decision == "approve" else "rejected"
    task.comment = (comment or "").strip() or None
    task.decided_at = _now()
    task.decided_by_name = decided_by
    task.decided_via = via
    audit.event(db, f"workflow.{task.status}", "workflow_instances", instance.id,
                {"step": task.step_name, "comment": task.comment, "reference": instance.reference_no, "via": via},
                company_id=instance.company_id, entity_uuid=str(instance.uuid), entity_label=instance.title)

    if task.status == "rejected":
        _finish(db, instance, "rejected", task.comment)
        return instance

    step = instance.definition.steps[task.step_index] if instance.definition else {"mode": "any"}
    siblings = [t for t in instance.tasks if t.step_index == task.step_index and t.id != task.id]
    if step.get("mode") == "all" and any(t.status == OPEN for t in siblings):
        db.flush()
        return instance  # wait for the others
    for t in siblings:
        if t.status == OPEN:
            t.status = "skipped"
    instance.current_step = task.step_index + 1
    requester = _employee_by_id(db, company_id, instance.requester_employee_id)
    if not _start_step(db, instance, requester):
        _finish(db, instance, "approved", task.comment)
    db.flush()
    return instance


def _employee_by_id(db: Session, company_id: int, employee_id):
    if employee_id is None:
        return None
    Employee = _employee_model()
    return db.query(Employee).filter(Employee.company_id == company_id, Employee.id == employee_id).first()


def cancel(db: Session, company_id: int, instance: WorkflowInstance, reason: str | None = None) -> WorkflowInstance:
    if instance.status != OPEN:
        raise BadRequest("Only pending requests can be withdrawn.")
    ctx = context.current()
    mine = (instance.requester_employee_id is not None and instance.requester_employee_id == ctx.employee_id) or \
           (instance.requester_employee_id is None and ctx.actor_type == "owner" and not _portal_requester(instance)) or \
           (ctx.portal_account_id is not None and _portal_requester(instance) == ctx.portal_account_id)
    if not mine and ctx.actor_type != "owner":
        raise Forbidden("Only the person who asked can withdraw this request.")
    audit.event(db, "workflow.cancelled", "workflow_instances", instance.id, {"reason": reason},
                entity_uuid=str(instance.uuid), entity_label=instance.title)
    _finish(db, instance, "cancelled", reason)
    return instance


def reassign(db: Session, company_id: int, task: WorkflowTask, to_employee_id: int, comment: str | None,
             can_manage: bool) -> WorkflowTask:
    if task.status != OPEN or task.instance.status != OPEN:
        raise BadRequest("Only pending approvals can be passed on.")
    if not (is_my_task(task) or can_manage):
        raise Forbidden("Only the approver or an admin can pass this approval on.")
    target = _active_employee(db, company_id, to_employee_id)
    if target is None:
        raise BadRequest("Pick an active employee to pass this approval to.")
    if task.approver_employee_id == target.id:
        raise BadRequest("This approval is already with that person.")
    task.status = "reassigned"
    task.comment = (comment or "").strip() or None
    task.decided_at = _now()
    task.decided_by_name = context.current().actor_name
    step = {"name": task.step_name}
    new = _make_task(db, task.instance, task.step_index, step, target, task.due_at)
    audit.event(db, "workflow.reassigned", "workflow_instances", task.instance_id,
                {"from": task.approver_name, "to": _name(target), "comment": task.comment},
                entity_uuid=str(task.instance.uuid), entity_label=task.instance.title)
    db.flush()
    return new


def escalate_overdue(db: Session, company_id: int | None = None, now: datetime | None = None) -> int:
    """Move late approvals to their escalation target. Call it periodically (any company
    when run with RLS bypass, or one company). Returns how many tasks were escalated."""
    now = now or _now()
    q = (db.query(WorkflowTask).options(joinedload(WorkflowTask.instance).joinedload(WorkflowInstance.definition))
         .filter(WorkflowTask.status == OPEN, WorkflowTask.escalated.is_(False), WorkflowTask.due_at.isnot(None),
                 WorkflowTask.due_at < now))
    if company_id is not None:
        q = q.filter(WorkflowTask.company_id == company_id)
    count = 0
    for task in q.limit(500).all():
        inst = task.instance
        if inst.status != OPEN:
            continue
        step = inst.definition.steps[task.step_index] if inst.definition and task.step_index < len(inst.definition.steps) else {}
        esc = step.get("escalate_to")
        action = step.get("timeout_action") or ("escalate" if esc else "remind")
        task.escalated = True
        if action in ("approve", "reject"):
            note = ("Approved automatically: the time limit passed." if action == "approve"
                    else "Rejected automatically: the time limit passed.")
            apply_decision(db, inst.company_id, task, action, note, decided_by="System (time limit passed)", via="auto")
            count += 1
            continue
        if action == "remind" or not esc:
            notify_task(db, task, f"{inst.title} is past its time limit.", kind="approval.reminder")
            count += 1
            continue
        requester = _employee_by_id(db, inst.company_id, inst.requester_employee_id)
        targets = resolve_approvers(db, inst.company_id, esc["approver_type"], esc.get("approver_value"), requester,
                                    variables_for(inst, requester)) or ["owner"]
        target = targets[0]
        if target != "owner" and task.approver_employee_id == target.id:
            continue
        task.status = "escalated"
        task.decided_at = now
        task.decided_by_name = "System (time limit passed)"
        _make_task(db, inst, task.step_index, {"name": task.step_name}, target, None)
        audit.event(db, "workflow.escalated", "workflow_instances", inst.id,
                    {"step": task.step_name, "from": task.approver_name,
                     "to": "Company owner" if target == "owner" else _name(target)},
                    company_id=inst.company_id, entity_uuid=str(inst.uuid), entity_label=inst.title)
        count += 1
    db.flush()
    return count


# ── Delegation ──────────────────────────────────────────────────────

def add_delegation(db: Session, company_id: int, from_employee_id: int, to_employee_id: int, start: date, end: date,
                   request_type: str | None, note: str | None) -> WorkflowDelegation:
    if end < start:
        raise BadRequest("The end date must be on or after the start date.")
    if from_employee_id == to_employee_id:
        raise BadRequest("Pick someone else to approve for you.")
    if _active_employee(db, company_id, to_employee_id) is None:
        raise BadRequest("Pick an active employee.")
    if request_type and not registry.get(request_type):
        raise BadRequest("Unknown kind of request.")
    row = WorkflowDelegation(company_id=company_id, from_employee_id=from_employee_id, to_employee_id=to_employee_id,
                             start_date=start, end_date=end, request_type=request_type, note=note)
    db.add(row)
    db.flush()
    return row
