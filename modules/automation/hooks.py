"""Automation: its permission, the generic approval request rules can start, the periodic
job for date rules and webhook retries, and the change listener (imported here so it is
active in every process)."""
from core import periodic
from core.workflow.registry import RequestType, register
from modules.automation import engine  # noqa: F401  (registers the session listeners)
from modules.roles.models import ALL_PERMISSIONS, DEFAULT_ROLE_PERMISSIONS

ALL_PERMISSIONS.setdefault("settings.automation.manage", "Set up automation rules and webhooks")
for _role in ("admin", "hr"):
    _lst = DEFAULT_ROLE_PERMISSIONS.setdefault(_role, [])
    if "settings.automation.manage" not in _lst:
        _lst.append("settings.automation.manage")


def _summ(instance) -> list[str]:
    p = instance.payload or {}
    out = [f"Raised by the rule “{p.get('rule')}”" if p.get("rule") else "Raised by an automation rule"]
    if p.get("record"):
        out.append(f"{p.get('record_type') or 'Record'}: {p['record']}")
    if p.get("note"):
        out.append(p["note"])
    return out


register(RequestType(
    key="automation.approval",
    label="Approval started by a rule",
    description="An automation rule asks someone to look at a record and approve it.",
    summarize=_summ,
    variables={"request.rule": "Rule name", "request.record_type": "Kind of record"},
    default_steps=[{"name": "HR review", "approver_type": "role", "approver_value": "hr", "mode": "any"}],
))


def _job(db) -> int:
    from modules.automation import webhooks
    return engine.run_date_rules(db) + webhooks.retry_due(db)


periodic.register("automation.rules", _job)
