"""Approve or reject from an email, through one-time links.

A link looks like  <app>/approve/<secret>.<signature>
- the secret is random; only its SHA-256 hash is stored (`workflow_action_tokens`);
- the signature is an HMAC (SECRET_KEY) over the secret, the task and the action, so a
  link can't be altered to point at another task or the other decision;
- a link works once, stops when it expires (company setting, 72 hours by default) and
  stops as soon as the task is decided some other way;
- opening and using a link are recorded with audit.event, with the IP address.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session, joinedload

from core import audit, context
from core.config import get_settings
from core.db import bypass_rls, set_tenant
from core.errors import BadRequest, NotFound
from core.workflow import registry
from core.workflow import service as wf
from core.workflow import settings as wf_settings
from core.workflow.models import WorkflowActionToken, WorkflowInstance, WorkflowTask

ACTIONS = ("approve", "reject")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _sign(secret: str, task_uuid: str, action: str) -> str:
    key = (get_settings().SECRET_KEY or "dev").encode()
    mac = hmac.new(key, f"wf-email:{secret}:{task_uuid}:{action}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac[:18]).decode().rstrip("=")


def company_url(db: Session, company_id: int) -> str:
    """The company's own web address (its subdomain) for links in emails."""
    base = (get_settings().FRONTEND_URL or "http://localhost:3100").rstrip("/")
    from modules.company.models import Company
    slug = db.query(Company.slug).filter(Company.id == company_id).scalar()
    proto, _, host = base.partition("://")
    if not slug or not host or host.startswith(f"{slug}."):
        return base
    return f"{proto}://{slug}.{host}"


def issue(db: Session, task: WorkflowTask, action: str, hours: int) -> str:
    secret = secrets.token_urlsafe(24)
    db.add(WorkflowActionToken(company_id=task.company_id, task_id=task.id, action=action, token_hash=_hash(secret),
                               expires_at=_now() + timedelta(hours=hours)))
    return f"{secret}.{_sign(secret, str(task.uuid), action)}"


def links_for_task(db: Session, company_id: int, task_uuid: str) -> dict | None:
    """Approve, reject and view links for an approval email, or None when the task is
    no longer waiting or the company has switched email links off."""
    cfg = wf_settings.get(db, company_id)
    task = (db.query(WorkflowTask).options(joinedload(WorkflowTask.instance))
            .filter(WorkflowTask.company_id == company_id, WorkflowTask.uuid == task_uuid).first())
    if task is None or task.status != wf.OPEN or task.instance.status != wf.OPEN:
        return None
    base = company_url(db, company_id)
    out = {"view_url": f"{base}/dashboard/inbox?task={task.uuid}", "task": task,
           "link_hours": int(cfg["email_link_hours"])}
    if cfg.get("email_actions", True):
        hours = int(cfg["email_link_hours"])
        out["approve_url"] = f"{base}/approve/{issue(db, task, 'approve', hours)}"
        out["reject_url"] = f"{base}/approve/{issue(db, task, 'reject', hours)}"
    db.flush()
    return out


def _find(db: Session, token: str) -> tuple[WorkflowActionToken, WorkflowTask]:
    secret, _, sig = (token or "").partition(".")
    if not secret or not sig or len(token) > 200:
        raise NotFound("Approval link")
    with bypass_rls(db):
        row = db.query(WorkflowActionToken).filter(WorkflowActionToken.token_hash == _hash(secret)).first()
        task = None
        if row is not None:
            task = (db.query(WorkflowTask).options(joinedload(WorkflowTask.instance))
                    .filter(WorkflowTask.id == row.task_id).first())
    if row is None or task is None or not hmac.compare_digest(sig, _sign(secret, str(task.uuid), row.action)):
        raise NotFound("Approval link")
    set_tenant(db, row.company_id)
    return row, task


def _state(row: WorkflowActionToken, task: WorkflowTask) -> str:
    if row.used_at is not None:
        return "used"
    if task.status != wf.OPEN or task.instance.status != wf.OPEN:
        return "decided"
    if row.expires_at < _now():
        return "expired"
    return "valid"


def describe(db: Session, token: str) -> dict:
    """What the link page shows before the person confirms."""
    row, task = _find(db, token)
    inst: WorkflowInstance = task.instance
    rt = registry.get(inst.request_type)
    state = _state(row, task)
    from modules.company.models import Company
    company = db.query(Company.company_name).filter(Company.id == inst.company_id).scalar()
    return {"action": row.action, "state": state, "company_name": company, "approver_name": task.approver_name,
            "title": inst.title, "reference_no": inst.reference_no, "requester_name": inst.requester_name,
            "request_type_label": rt.label if rt else inst.request_type, "step_name": task.step_name,
            "summary": rt.summarize(inst) if rt and rt.summarize else [],
            "created_at": inst.created_at, "expires_at": row.expires_at,
            "outcome": task.status if state == "decided" else None,
            "comment_required": row.action == "reject"}


def use(db: Session, token: str, comment: str | None, ip: str | None, user_agent: str | None) -> dict:
    """Make the decision the link stands for, as the approver it was sent to."""
    row, task = _find(db, token)
    state = _state(row, task)
    if state == "used":
        raise BadRequest("This link has already been used.")
    if state == "decided":
        raise BadRequest("This request has already been decided.")
    if state == "expired":
        raise BadRequest("This link has expired. Open the request in the app instead.")
    if row.action == "reject" and not (comment or "").strip():
        raise BadRequest("Add a comment saying why the request is rejected.")
    ctx = context.current()
    ctx.company_id = task.company_id
    ctx.employee_id = task.approver_employee_id
    ctx.actor_type = "owner" if task.approver_kind == "owner" else "employee"
    ctx.actor_name = f"{task.approver_name} (by email)"
    row.used_at = _now()
    row.used_ip = (ip or "")[:64] or None
    row.used_user_agent = (user_agent or "")[:300] or None
    # Every other link for this task stops working too.
    db.query(WorkflowActionToken).filter(WorkflowActionToken.task_id == task.id, WorkflowActionToken.id != row.id,
                                         WorkflowActionToken.used_at.is_(None)) \
        .update({WorkflowActionToken.expires_at: _now()}, synchronize_session=False)
    audit.event(db, "workflow.email_link_used", "workflow_instances", task.instance_id,
                {"action": row.action, "ip": row.used_ip, "approver": task.approver_name},
                company_id=task.company_id, entity_uuid=str(task.instance.uuid), entity_label=task.instance.title)
    inst = wf.apply_decision(db, task.company_id, task, row.action, comment, decided_by=ctx.actor_name, via="email")
    db.flush()
    return {"status": inst.status, "decision": row.action, "title": inst.title, "reference_no": inst.reference_no}
