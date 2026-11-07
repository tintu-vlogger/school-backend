"""Automation rules and webhooks: checking what is saved, and how it is shown."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from core import context, expressions
from core.errors import BadRequest, NotFound
from core.timeutil import iso_utc
from modules.automation import entities as ents
from modules.automation import webhooks
from modules.automation.engine import ACTION_TYPES, AUDIENCES, UPDATE_FIELDS
from modules.automation.models import AutomationRule, AutomationRun, AutomationWebhook, AutomationWebhookDelivery

TRIGGERS = {
    "created": "A record is added",
    "updated": "A record is changed",
    "field_changed": "Chosen fields change",
    "approved": "A request is approved",
    "rejected": "A request is rejected",
    "date": "A date comes up",
}
PRIORITIES = ("low", "medium", "high", "urgent")


def _clean_to(to) -> list[str]:
    out = []
    for item in to or []:
        item = str(item).strip()
        if item in AUDIENCES or (item.startswith("role:") and len(item) > 5) or \
                (item.startswith("person:") and item[7:].isdigit()):
            out.append(item)
        elif item:
            raise BadRequest(f"Unknown recipient “{item}”.")
    return list(dict.fromkeys(out))


def _clean_addresses(addrs) -> list[str]:
    out = []
    for a in addrs or []:
        a = str(a).strip()
        if not a:
            continue
        if "@" not in a or " " in a or len(a) > 254:
            raise BadRequest(f"“{a}” is not an email address.")
        out.append(a)
    return out[:20]


def _check_text(text: str | None, limit: int, what: str) -> str | None:
    text = (text or "").strip() or None
    if text and len(text) > limit:
        raise BadRequest(f"The {what} can be at most {limit} characters.")
    return text


def clean_action(db: Session, company_id: int, a: dict, i: int) -> dict:
    t = a.get("type")
    if t not in ACTION_TYPES:
        raise BadRequest(f"Action {i}: pick what to do.")
    if t == "notify":
        to = _clean_to(a.get("to"))
        if not to:
            raise BadRequest(f"Action {i}: say who gets the notification.")
        title = _check_text(a.get("title"), 200, "title")
        if not title:
            raise BadRequest(f"Action {i}: give the notification a title.")
        return {"type": t, "to": to, "title": title, "body": _check_text(a.get("body"), 2000, "message"),
                "link": _check_text(a.get("link"), 300, "link")}
    if t == "email":
        to, addrs = _clean_to(a.get("to")), _clean_addresses(a.get("addresses"))
        if not to and not addrs:
            raise BadRequest(f"Action {i}: say who gets the email.")
        subject = _check_text(a.get("subject"), 200, "subject")
        if not subject:
            raise BadRequest(f"Action {i}: give the email a subject.")
        return {"type": t, "to": to, "addresses": addrs, "subject": subject,
                "body": _check_text(a.get("body"), 5000, "email text") or ""}
    if t == "task":
        title = _check_text(a.get("title"), 200, "task title")
        if not title:
            raise BadRequest(f"Action {i}: give the task a title.")
        assignee = (_clean_to([a.get("assignee") or "employee"]) or ["employee"])[0]
        if assignee == "owner":
            raise BadRequest(f"Action {i}: pick a person to do the task.")
        due = a.get("due_in_days")
        if due not in (None, ""):
            try:
                due = int(due)
            except (TypeError, ValueError):
                raise BadRequest(f"Action {i}: the due date must be a number of days.")
            if not 0 <= due <= 365:
                raise BadRequest(f"Action {i}: the task can be due in 0 to 365 days.")
        else:
            due = None
        priority = a.get("priority") or "medium"
        if priority not in PRIORITIES:
            raise BadRequest(f"Action {i}: unknown priority.")
        return {"type": t, "title": title, "description": _check_text(a.get("description"), 2000, "description"),
                "assignee": assignee, "due_in_days": due, "priority": priority}
    if t == "update_field":
        field = a.get("field")
        if field not in UPDATE_FIELDS:
            raise BadRequest(f"Action {i}: pick a field that rules may change.")
        formula = _check_text(a.get("formula"), 500, "formula")
        if formula:
            try:
                expressions.parse(formula)
            except expressions.ExpressionError as e:
                raise BadRequest(f"Action {i} formula: {e}")
            return {"type": t, "field": field, "formula": formula, "value": None}
        value = a.get("value")
        if value not in (None, ""):
            from modules.automation.engine import _clean_field_value
            try:
                _clean_field_value(db, company_id, field, value)
            except ValueError as e:
                raise BadRequest(f"Action {i}: {e}")
        return {"type": t, "field": field, "value": value, "formula": None}
    if t == "workflow":
        return {"type": t, "title": _check_text(a.get("title"), 200, "title"),
                "note": _check_text(a.get("note"), 1000, "note")}
    if t == "webhook":
        uuid = a.get("webhook_uuid")
        try:
            hook = get_webhook(db, company_id, uuid)
        except NotFound:
            hook = None
        if hook is None:
            raise BadRequest(f"Action {i}: pick a webhook.")
        return {"type": t, "webhook_uuid": str(hook.uuid)}
    raise BadRequest(f"Action {i}: pick what to do.")


def clean_rule(db: Session, company_id: int, data: dict) -> dict:
    available = ents.available()
    ent = available.get(data.get("entity"))
    if ent is None:
        raise BadRequest("Pick what kind of record the rule watches.")
    trigger = data.get("trigger")
    if trigger not in TRIGGERS or trigger not in ent.triggers:
        raise BadRequest(f"Pick when the rule runs for a {ent.label.lower()}.")
    name = (data.get("name") or "").strip()
    if not name:
        raise BadRequest("Give the rule a name.")
    out = {"name": name[:150], "description": _check_text(data.get("description"), 1000, "description"),
           "entity": ent.key, "trigger": trigger, "fields": [], "request_type": None, "date_field": None,
           "offset_days": 0, "repeat_yearly": False, "active": bool(data.get("active", True))}
    if trigger == "field_changed":
        fields = [f for f in dict.fromkeys(data.get("fields") or [])]
        if not fields:
            raise BadRequest("Pick at least one field to watch.")
        bad = [f for f in fields if f not in ent.fields]
        if bad:
            raise BadRequest(f"“{bad[0]}” can't be watched.")
        out["fields"] = fields
    if trigger in ("approved", "rejected") and data.get("request_type"):
        from core.workflow import registry
        if not registry.get(data["request_type"]):
            raise BadRequest("Unknown kind of request.")
        out["request_type"] = data["request_type"]
    if trigger == "date":
        if data.get("date_field") not in ent.date_fields:
            raise BadRequest("Pick the date the rule counts from.")
        try:
            offset = int(data.get("offset_days") or 0)
        except (TypeError, ValueError):
            raise BadRequest("The number of days must be a whole number.")
        if not -365 <= offset <= 365:
            raise BadRequest("Pick up to 365 days before or after.")
        out.update(date_field=data["date_field"], offset_days=offset, repeat_yearly=bool(data.get("repeat_yearly")))
    cond = _check_text(data.get("condition"), 2000, "condition")
    if cond:
        try:
            expressions.validate(cond, {"record", "employee", "changes", "request", "trigger", ent.key})
        except expressions.ExpressionError as e:
            raise BadRequest(f"Condition: {e}")
    out["condition"] = cond
    actions = data.get("actions") or []
    if not actions:
        raise BadRequest("Add at least one action.")
    if len(actions) > 10:
        raise BadRequest("A rule can have at most 10 actions.")
    out["actions"] = [clean_action(db, company_id, a or {}, i) for i, a in enumerate(actions, start=1)]
    return out


def parse_uuid(value, what: str = "Record"):
    import uuid as uuid_mod
    try:
        return uuid_mod.UUID(str(value))
    except (TypeError, ValueError):
        raise NotFound(what)


def get_rule(db: Session, company_id: int, uuid) -> AutomationRule:
    uuid = parse_uuid(uuid, "Rule")
    row = db.query(AutomationRule).filter(AutomationRule.company_id == company_id, AutomationRule.uuid == uuid,
                                          AutomationRule.archived_at.is_(None)).first()
    if row is None:
        raise NotFound("Rule")
    return row


def save_rule(db: Session, company_id: int, data: dict, row: AutomationRule | None = None) -> AutomationRule:
    clean = clean_rule(db, company_id, data)
    if row is None:
        row = AutomationRule(company_id=company_id, created_by_name=context.current().actor_name)
        db.add(row)
    for k, v in clean.items():
        setattr(row, k, v)
    db.flush()
    return row


def archive_rule(db: Session, row: AutomationRule) -> None:
    row.archived_at = datetime.now(timezone.utc)
    row.active = False


def rule_out(r: AutomationRule) -> dict:
    ent = ents.ENTITIES.get(r.entity)
    return {"uuid": str(r.uuid), "name": r.name, "description": r.description, "entity": r.entity,
            "entity_label": ent.label if ent else r.entity, "trigger": r.trigger,
            "trigger_label": TRIGGERS.get(r.trigger, r.trigger), "fields": r.fields or [],
            "request_type": r.request_type, "date_field": r.date_field, "offset_days": r.offset_days,
            "repeat_yearly": r.repeat_yearly, "condition": r.condition, "actions": r.actions or [],
            "active": r.active, "run_count": r.run_count, "last_run_at": iso_utc(r.last_run_at),
            "created_by_name": r.created_by_name, "created_at": iso_utc(r.created_at),
            "summary": summary(r)}


def summary(r: AutomationRule) -> str:
    ent = ents.ENTITIES.get(r.entity)
    label = (ent.label if ent else r.entity).lower()
    if r.trigger == "date" and ent:
        d = ent.date_fields.get(r.date_field, r.date_field or "").lower()
        when = "on" if not r.offset_days else (f"{abs(r.offset_days)} days {'before' if r.offset_days < 0 else 'after'}")
        return f"{when} the {d}" + (" every year" if r.repeat_yearly else "")
    if r.trigger == "field_changed" and ent:
        return "when " + ", ".join(ent.fields.get(f, f).lower() for f in r.fields or []) + " changes"
    return {"created": f"when a {label} is added", "updated": f"when a {label} is changed",
            "approved": "when a request is approved", "rejected": "when a request is rejected"}.get(r.trigger, r.trigger)


def run_out(r: AutomationRun, rule_names: dict) -> dict:
    return {"uuid": str(r.uuid), "rule_name": rule_names.get(r.rule_id), "trigger": r.trigger,
            "trigger_label": TRIGGERS.get(r.trigger, r.trigger), "entity": r.entity, "record_id": r.record_id,
            "record_label": r.record_label, "matched": r.matched, "status": r.status, "results": r.results or [],
            "error": r.error, "created_at": iso_utc(r.created_at)}


# ── Webhooks ────────────────────────────────────────────────────────

def mask(secret: str) -> str:
    return f"{secret[:6]}…{secret[-4:]}" if secret and len(secret) > 12 else "…"


def get_webhook(db: Session, company_id: int, uuid) -> AutomationWebhook:
    uuid = parse_uuid(uuid, "Webhook")
    row = db.query(AutomationWebhook).filter(AutomationWebhook.company_id == company_id,
                                             AutomationWebhook.uuid == uuid,
                                             AutomationWebhook.archived_at.is_(None)).first()
    if row is None:
        raise NotFound("Webhook")
    return row


def save_webhook(db: Session, company_id: int, data: dict, row: AutomationWebhook | None = None) -> AutomationWebhook:
    name = (data.get("name") if data.get("name") is not None else (row.name if row else "")).strip()
    if not name:
        raise BadRequest("Give the webhook a name.")
    url = webhooks.check_url(data["url"]) if data.get("url") is not None else (row.url if row else None)
    if not url:
        raise BadRequest("Enter the address to call.")
    if row is None:
        row = AutomationWebhook(company_id=company_id, secret=webhooks.new_secret())
        db.add(row)
    row.name, row.url = name[:100], url
    if data.get("headers") is not None:
        row.headers = webhooks.clean_headers(data["headers"])
    if data.get("active") is not None:
        row.active = bool(data["active"])
    db.flush()
    return row


def webhook_out(w: AutomationWebhook, show_secret: bool = False, stats: dict | None = None) -> dict:
    return {"uuid": str(w.uuid), "name": w.name, "url": w.url, "headers": w.headers or {}, "active": w.active,
            "secret": w.secret if show_secret else None, "secret_hint": mask(w.secret),
            "created_at": iso_utc(w.created_at), **(stats or {})}


def delivery_out(d: AutomationWebhookDelivery, names: dict) -> dict:
    return {"uuid": str(d.uuid), "webhook_name": names.get(d.webhook_id), "event": d.event, "status": d.status,
            "attempts": d.attempts, "response_status": d.response_status, "response_body": d.response_body,
            "error": d.error, "next_attempt_at": iso_utc(d.next_attempt_at), "sent_at": iso_utc(d.sent_at),
            "created_at": iso_utc(d.created_at), "payload": d.payload}
