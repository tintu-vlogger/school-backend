"""Company-wide approval settings (one row per company in `workflow_settings`).

    no_approver   who decides when a step finds nobody: owner | role | employee | auto_approve
    on_exit       who takes over the pending approvals of someone who leaves:
                  manager (their own manager, then the fallback) | fallback
    fallback      the person or role used by both rules above when the first choice is empty
    nudge_cooldown_hours   how often a requester may remind the approvers
    email_actions          one-click approve / reject links in approval emails
    email_link_hours       how long those links work
    revoke        {request type: off | direct | approval} for cancelling an approved request
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from core.errors import BadRequest
from core.workflow import registry
from core.workflow.models import WorkflowSettings

NO_APPROVER = {
    "owner": "The company owner decides",
    "role": "Everyone with a role decides",
    "employee": "A named person decides",
    "auto_approve": "Approve that step automatically",
}
ON_EXIT = {
    "manager": "Their own manager, otherwise the fallback below",
    "fallback": "Always the fallback below",
}
REVOKE_MODES = {
    "off": "Not allowed",
    "direct": "Allowed without approval",
    "approval": "Allowed after approval",
}
DEFAULTS = {
    "no_approver": {"action": "owner", "value": None},
    "on_exit": {"action": "manager"},
    "fallback": {"approver_type": "owner", "approver_value": None},
    "nudge_cooldown_hours": 24,
    "email_actions": True,
    "email_link_hours": 72,
    "revoke": {},
}


def revocable_types() -> list:
    return [rt for rt in registry.all_types() if rt.on_revoked is not None]


def get(db: Session, company_id: int) -> dict:
    row = db.query(WorkflowSettings).filter(WorkflowSettings.company_id == company_id).first()
    cfg = dict((row.config if row else None) or {})
    out = {**DEFAULTS, **cfg}
    revoke = {rt.key: "approval" for rt in revocable_types()}
    revoke.update({k: v for k, v in (cfg.get("revoke") or {}).items() if k in revoke and v in REVOKE_MODES})
    out["revoke"] = revoke
    return out


def save(db: Session, company_id: int, data: dict, actor: str | None) -> dict:
    current = get(db, company_id)
    cfg = {k: current[k] for k in DEFAULTS}
    if "no_approver" in data:
        na = data["no_approver"] or {}
        if na.get("action") not in NO_APPROVER:
            raise BadRequest("Pick who decides when no approver is found.")
        if na["action"] in ("role", "employee") and not na.get("value"):
            raise BadRequest("Say which role or person decides when no approver is found.")
        cfg["no_approver"] = {"action": na["action"], "value": na.get("value")}
    if "on_exit" in data:
        oe = data["on_exit"] or {}
        if oe.get("action") not in ON_EXIT:
            raise BadRequest("Pick who takes over when an approver leaves.")
        cfg["on_exit"] = {"action": oe["action"]}
    if "fallback" in data:
        fb = data["fallback"] or {}
        if fb.get("approver_type") not in ("owner", "role", "employee"):
            raise BadRequest("The fallback must be the owner, a role or a named person.")
        if fb["approver_type"] != "owner" and not fb.get("approver_value"):
            raise BadRequest("Say which role or person is the fallback.")
        cfg["fallback"] = {"approver_type": fb["approver_type"], "approver_value": fb.get("approver_value")}
    if "nudge_cooldown_hours" in data:
        try:
            n = int(data["nudge_cooldown_hours"])
        except (TypeError, ValueError):
            raise BadRequest("The reminder gap must be a whole number of hours.")
        if n < 1 or n > 24 * 14:
            raise BadRequest("The reminder gap must be between 1 hour and 14 days.")
        cfg["nudge_cooldown_hours"] = n
    if "email_actions" in data:
        cfg["email_actions"] = bool(data["email_actions"])
    if "email_link_hours" in data:
        try:
            n = int(data["email_link_hours"])
        except (TypeError, ValueError):
            raise BadRequest("Link life must be a whole number of hours.")
        if n < 1 or n > 24 * 14:
            raise BadRequest("Email links can work for 1 hour up to 14 days.")
        cfg["email_link_hours"] = n
    if "revoke" in data:
        allowed = {rt.key for rt in revocable_types()}
        rv = dict(current["revoke"])
        for k, v in (data["revoke"] or {}).items():
            if k not in allowed:
                raise BadRequest("That kind of request can't be cancelled after approval.")
            if v not in REVOKE_MODES:
                raise BadRequest("Pick whether cancelling after approval is allowed.")
            rv[k] = v
        cfg["revoke"] = rv
    row = db.query(WorkflowSettings).filter(WorkflowSettings.company_id == company_id).first()
    if row is None:
        row = WorkflowSettings(company_id=company_id, config=cfg)
        db.add(row)
    else:
        row.config = cfg
    row.updated_by_name = actor
    db.flush()
    return get(db, company_id)


def fallback_approvers(db: Session, company_id: int, requester, rule: str):
    """Approvers when the normal ones are missing. rule is "no_approver" or "on_exit".
    Returns None to mean "approve the step automatically"."""
    from core.workflow.service import resolve_approvers
    cfg = get(db, company_id)
    if rule == "no_approver":
        na = cfg["no_approver"]
        if na["action"] == "auto_approve":
            return None
        if na["action"] in ("role", "employee"):
            found = resolve_approvers(db, company_id, na["action"], na.get("value"), requester, {})
            if found:
                return found
            return ["owner"]
        return ["owner"]
    fb = cfg["fallback"]
    found = resolve_approvers(db, company_id, fb["approver_type"], fb.get("approver_value"), requester, {})
    return found or ["owner"]
