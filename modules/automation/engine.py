"""Running automation rules.

Record triggers (created, updated, field changed, approved, rejected)
- A `before_flush` listener notes new and changed records of the watched types (and
  requests whose status turns approved or rejected) in `session.info`.
- A `before_commit` listener runs the matching rules for those records in the same
  transaction, each rule inside its own savepoint, so a failing rule is logged and never
  stops the save. Changes made by actions can trigger further rules up to MAX_DEPTH
  rounds; a rule never fires twice for the same record and trigger in one save.

Date triggers run from the periodic job (`run_date_rules`): N days before or after a date
field, optionally every year (birthdays, work anniversaries). The run log's occurrence key
(record and date) makes each occurrence run once.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

from sqlalchemy import event as sa_event, extract, inspect, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core import expressions
from modules.automation import entities as ents
from modules.automation.models import AutomationRule, AutomationRun, AutomationWebhook

logger = logging.getLogger("hrms")

MAX_DEPTH = 3
_PENDING = "automation_pending"
_FIRED = "automation_fired"
_BUSY = "automation_busy"

# Employee fields an "update a field" action may set, and how the value is checked.
UPDATE_FIELDS = {
    "work_mode": ("Work mode", "choice"),
    "notice_period_days": ("Notice period (days)", "int"),
    "probation_end_date": ("Probation end date", "date"),
    "confirmation_date": ("Confirmation date", "date"),
    "timezone": ("Timezone", "timezone"),
    "employee_status_id": ("Sub-status (from the list)", "master:employee_status"),
}
AUDIENCES = {"employee": "The person", "manager": "Their reporting manager", "owner": "Company owner"}
ACTION_TYPES = {
    "notify": "Send a notification",
    "email": "Send an email",
    "task": "Create a task",
    "update_field": "Update a field",
    "workflow": "Start an approval",
    "webhook": "Call a webhook",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Collecting changes ──────────────────────────────────────────────

_MODEL_MAP: dict | None = None


def _model_map() -> dict:
    global _MODEL_MAP
    if _MODEL_MAP is None:
        m = {}
        for e in ents.available().values():
            if e.key != "request":
                m[e.model()] = e
        _MODEL_MAP = m
    return _MODEL_MAP


def _changes(obj, ent) -> dict:
    out = {}
    state = inspect(obj)
    for key in ent.fields:
        if key not in state.attrs:
            continue
        hist = state.attrs[key].history
        if not hist.has_changes():
            continue
        old = hist.deleted[0] if hist.deleted else None
        new = hist.added[0] if hist.added else getattr(obj, key, None)
        if old != new:
            out[key] = {"old": ents.plain(old), "new": ents.plain(new)}
    return out


def _request_status_change(obj) -> str | None:
    hist = inspect(obj).attrs["status"].history
    if not hist.has_changes():
        return None
    new = hist.added[0] if hist.added else None
    return new if new in ("approved", "rejected") else None


@sa_event.listens_for(Session, "before_flush")
def _collect(session: Session, _ctx, _instances):
    if session.info.get("automation_off"):
        return
    try:
        from core.workflow.models import WorkflowInstance
        mm = _model_map()
        pend = session.info.setdefault(_PENDING, [])
        for obj in session.new:
            if isinstance(obj, WorkflowInstance):
                if obj.status in ("approved", "rejected"):
                    pend.append(("request", obj, obj.status, {}))
                continue
            ent = mm.get(type(obj))
            if ent is not None:
                pend.append((ent.key, obj, "created", {}))
        for obj in session.dirty:
            if isinstance(obj, WorkflowInstance):
                kind = _request_status_change(obj)
                if kind:
                    pend.append(("request", obj, kind, {}))
                continue
            ent = mm.get(type(obj))
            if ent is None or not session.is_modified(obj, include_collections=False):
                continue
            ch = _changes(obj, ent)
            if ch:
                pend.append((ent.key, obj, "updated", ch))
        if not pend:
            session.info.pop(_PENDING, None)
    except Exception as exc:  # pragma: no cover - never break a save
        logger.warning("Automation could not read changes: %s", exc)


@sa_event.listens_for(Session, "before_commit")
def _run_pending(session: Session):
    if session.info.get(_BUSY):
        return
    # commit() flushes only after this hook, so flush now to collect the last changes
    session.flush()
    if not session.info.get(_PENDING):
        return
    session.info[_BUSY] = True
    try:
        for _depth in range(MAX_DEPTH):
            session.flush()
            pend = session.info.pop(_PENDING, None)
            if not pend:
                break
            try:
                process(session, pend)
            except Exception as exc:  # pragma: no cover
                logger.warning("Automation run failed: %s", exc)
        session.info.pop(_PENDING, None)
    finally:
        session.info[_BUSY] = False
        session.info.pop(_FIRED, None)


@sa_event.listens_for(Session, "after_rollback")
def _forget(session: Session):
    session.info.pop(_PENDING, None)
    session.info.pop(_FIRED, None)


def _rules(db: Session, company_id: int, entity: str, triggers: list[str]) -> list[AutomationRule]:
    return (db.query(AutomationRule)
            .filter(AutomationRule.company_id == company_id, AutomationRule.entity == entity,
                    AutomationRule.trigger.in_(triggers), AutomationRule.active.is_(True),
                    AutomationRule.archived_at.is_(None))
            .order_by(AutomationRule.id).all())


def process(db: Session, pending: list) -> int:
    fired = db.info.setdefault(_FIRED, set())
    cache: dict = {}
    n = 0
    for ent_key, obj, kind, changes in pending:
        state = inspect(obj)
        if not state.persistent or obj.company_id is None:
            continue
        triggers = {"created": ["created"], "updated": ["updated", "field_changed"]}.get(kind, [kind])
        ck = (obj.company_id, ent_key, tuple(triggers))
        if ck not in cache:
            cache[ck] = _rules(db, obj.company_id, ent_key, triggers)
        ent = ents.ENTITIES[ent_key]
        for rule in cache[ck]:
            if rule.trigger == "field_changed" and rule.fields and not set(rule.fields) & set(changes):
                continue
            if ent_key == "request" and rule.request_type and rule.request_type != obj.request_type:
                continue
            key = (rule.id, ent_key, obj.id, rule.trigger)
            if key in fired:
                continue
            fired.add(key)
            run_rule(db, rule, ent, obj, trigger=rule.trigger, changes=changes)
            n += 1
    return n


# ── One rule run ────────────────────────────────────────────────────

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")


def _lookup(values: dict, path: str):
    cur = values
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return ""
    return "" if cur is None else cur


def render(text: str | None, values: dict) -> str:
    return _PLACEHOLDER.sub(lambda m: str(_lookup(values, m.group(1))), text or "")


def _today(db: Session, company_id: int) -> date:
    from core.timeutil import today_for_company_id
    return today_for_company_id(db, company_id)


def run_rule(db: Session, rule, ent, obj, *, trigger: str, changes: dict | None = None,
             occurrence_key: str | None = None, dry: bool = False) -> dict:
    """Check the condition and do (or, with dry=True, describe) each action. Writes the
    run log unless dry. Returns {matched, status, results, error}."""
    company_id = obj.company_id
    values = ents.variables(db, ent, obj, trigger=trigger, changes=changes)
    values["rule"] = {"name": rule.name}
    label = ent.label_of(obj)
    error = None
    try:
        matched = expressions.check_condition(rule.condition, values, today=_today(db, company_id))
    except expressions.ExpressionError as exc:
        matched, error = False, f"Condition: {exc}"
    results = []
    if matched:
        for action in rule.actions or []:
            try:
                if dry:
                    results.append({"type": action.get("type"), "ok": True,
                                    "detail": describe(db, company_id, action, values)})
                else:
                    with db.begin_nested():
                        detail = perform(db, company_id, rule, action, values, ent, obj)
                    results.append({"type": action.get("type"), "ok": True, "detail": detail})
            except Exception as exc:
                msg = getattr(exc, "detail", None) or str(exc) or exc.__class__.__name__
                results.append({"type": action.get("type"), "ok": False, "detail": str(msg)[:300]})
    if error:
        status = "failed"
    elif not matched:
        status = "skipped"
    elif results and all(r["ok"] for r in results):
        status = "done"
    elif results and not any(r["ok"] for r in results):
        status = "failed"
    elif results:
        status = "partial"
    else:
        status = "done"
    out = {"matched": matched, "status": status, "results": results, "error": error, "record_label": label}
    if dry:
        out["values"] = values
        return out
    db.add(AutomationRun(company_id=company_id, rule_id=rule.id, trigger=trigger, entity=ent.key, record_id=obj.id,
                         record_label=(label or "")[:250], occurrence_key=occurrence_key, matched=matched,
                         status=status, results=results, error=error))
    if matched:
        db.execute(update(AutomationRule).where(AutomationRule.id == rule.id)
                   .values(run_count=AutomationRule.run_count + 1, last_run_at=_now())
                   .execution_options(synchronize_session=False))
    db.flush()
    return out


# ── Recipients ──────────────────────────────────────────────────────

def recipients(db: Session, company_id: int, to: list[str], values: dict) -> list:
    """Employee ids (None = company owner) for a list like ["employee", "manager", "role:hr", "person:12"]."""
    from modules.employees.models import Employee
    out: list = []
    emp = values.get("employee") or {}
    for item in to or []:
        if item == "employee" and emp.get("id"):
            out.append(emp["id"])
        elif item == "manager" and emp.get("reporting_manager_id"):
            out.append(emp["reporting_manager_id"])
        elif item == "owner":
            out.append(None)
        elif item.startswith("role:"):
            ids = [i for (i,) in db.query(Employee.id).filter(Employee.company_id == company_id,
                                                             Employee.role == item[5:], Employee.active.is_(True))
                   .order_by(Employee.id).limit(100).all()]
            out.extend(ids)
        elif item.startswith("person:"):
            try:
                out.append(int(item[7:]))
            except ValueError:
                pass
    return list(dict.fromkeys(out))


def _emails(db: Session, company_id: int, ids: list) -> list[str]:
    from modules.company.models import Company
    from modules.employees.models import Employee
    out = []
    real = [i for i in ids if i]
    if real:
        out += [e for (e,) in db.query(Employee.email).filter(Employee.company_id == company_id,
                                                             Employee.id.in_(real), Employee.active.is_(True)).all() if e]
    if None in ids:
        owner = db.query(Company.email).filter(Company.id == company_id).scalar()
        if owner:
            out.append(owner)
    return out


def _who(to: list[str], addresses: list[str] | None = None) -> str:
    names = []
    for t in to or []:
        if t in AUDIENCES:
            names.append(AUDIENCES[t].lower())
        elif t.startswith("role:"):
            names.append(f"everyone with the {t[5:]} role")
        elif t.startswith("person:"):
            names.append("a chosen person")
    names += addresses or []
    return ", ".join(names) or "nobody"


# ── Actions ─────────────────────────────────────────────────────────

def describe(db: Session, company_id: int, action: dict, values: dict) -> str:
    t = action.get("type")
    if t == "notify":
        ids = recipients(db, company_id, action.get("to") or [], values)
        return f"Notify {_who(action.get('to'))} ({len(ids)} people): “{render(action.get('title'), values)}”"
    if t == "email":
        ids = recipients(db, company_id, action.get("to") or [], values)
        addrs = _emails(db, company_id, ids) + list(action.get("addresses") or [])
        return f"Email {', '.join(addrs) or 'nobody'}: “{render(action.get('subject'), values)}”"
    if t == "task":
        ids = recipients(db, company_id, [action.get("assignee") or "employee"], values)
        return (f"Create the task “{render(action.get('title'), values)}” for "
                f"{_who([action.get('assignee') or 'employee'])}" + (" (nobody found)" if not ids else "")
                + (f", due in {action.get('due_in_days')} days" if action.get("due_in_days") not in (None, "") else ""))
    if t == "update_field":
        value = _field_value(action, values, company_id, db)
        label = UPDATE_FIELDS.get(action.get("field"), (action.get("field"),))[0]
        return f"Set {label} of {values.get('employee', {}).get('name') or 'the person'} to {value}"
    if t == "workflow":
        return f"Start an approval: “{render(action.get('title'), values) or 'Automatic request'}”"
    if t == "webhook":
        hook = _webhook(db, company_id, action.get("webhook_uuid"))
        return f"Call the webhook {hook.name} ({hook.url})" if hook else "Call a webhook that no longer exists"
    return "Unknown action"


def _webhook(db: Session, company_id: int, uuid) -> AutomationWebhook | None:
    import uuid as uuid_mod
    try:
        uuid = uuid_mod.UUID(str(uuid))
    except (TypeError, ValueError):
        return None
    return db.query(AutomationWebhook).filter(AutomationWebhook.company_id == company_id,
                                              AutomationWebhook.uuid == uuid,
                                              AutomationWebhook.archived_at.is_(None)).first()


def _field_value(action: dict, values: dict, company_id: int, db: Session):
    if action.get("formula"):
        v = expressions.evaluate(action["formula"], values, today=_today(db, company_id))
        return expressions.to_json(v)
    return action.get("value")


def _clean_field_value(db: Session, company_id: int, field: str, value):
    from modules.employees.service import WORK_MODES
    if field not in UPDATE_FIELDS:
        raise ValueError("This field can't be changed by a rule.")
    kind = UPDATE_FIELDS[field][1]
    if value in (None, ""):
        return None
    if kind == "choice":
        if value not in WORK_MODES:
            raise ValueError(f"Work mode must be one of {', '.join(WORK_MODES)}.")
        return value
    if kind == "int":
        try:
            n = int(Decimal(str(value)))
        except (InvalidOperation, ValueError):
            raise ValueError("Enter a whole number.")
        if n < 0 or n > 365:
            raise ValueError("Enter a number from 0 to 365.")
        return n
    if kind == "date":
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            raise ValueError("Enter a date like 2026-12-31.")
    if kind == "timezone":
        from core.timeutil import is_valid_timezone
        if not is_valid_timezone(str(value)):
            raise ValueError("Unknown timezone.")
        return str(value)
    if kind.startswith("master:"):
        from core.masters import MasterValue
        try:
            vid = int(value)
        except (TypeError, ValueError):
            raise ValueError("Pick a value from the list.")
        ok = db.query(MasterValue.id).filter(MasterValue.company_id == company_id, MasterValue.id == vid).first()
        if not ok:
            raise ValueError("Pick a value from the list.")
        return vid
    return value


def perform(db: Session, company_id: int, rule, action: dict, values: dict, ent, obj) -> str:
    t = action.get("type")
    if t == "notify":
        from modules.notifications.service import notify
        ids = recipients(db, company_id, action.get("to") or [], values)
        title = render(action.get("title"), values) or rule.name
        n = notify(db, company_id, ids, kind="automation", title=title, body=render(action.get("body"), values),
                   link=action.get("link") or None, actor_name=f"Automation: {rule.name}")
        return f"Notified {n} people"
    if t == "email":
        from modules.notifications.channels import send_email
        ids = recipients(db, company_id, action.get("to") or [], values)
        addrs = _emails(db, company_id, ids) + list(action.get("addresses") or [])
        if not addrs:
            return "Nobody to email"
        n = send_email(db, company_id, addrs, render(action.get("subject"), values) or rule.name,
                       render(action.get("body"), values), category="automation", source="automation")
        return f"Emailed {n} address(es)"
    if t == "task":
        from modules.tasks import service as tasks
        ids = [i for i in recipients(db, company_id, [action.get("assignee") or "employee"], values) if i]
        due = None
        if action.get("due_in_days") not in (None, ""):
            due = _today(db, company_id) + timedelta(days=int(action["due_in_days"]))
        task = tasks.create_task(db, company_id, title=render(action.get("title"), values) or rule.name,
                                 description=render(action.get("description"), values) or None,
                                 assignee_employee_id=ids[0] if ids else None, due_date=due,
                                 priority=action.get("priority") or "medium", source_module="automation",
                                 source_ref=str(rule.uuid), created_by_name=f"Automation: {rule.name}")
        return f"Task created: {task.title}"
    if t == "update_field":
        from modules.employees.models import Employee
        emp_id = (values.get("employee") or {}).get("id")
        emp = db.query(Employee).filter(Employee.company_id == company_id, Employee.id == emp_id).first() if emp_id else None
        if emp is None:
            raise ValueError("No person to update.")
        field = action.get("field")
        value = _clean_field_value(db, company_id, field, _field_value(action, values, company_id, db))
        setattr(emp, field, value)
        db.flush()
        return f"{UPDATE_FIELDS[field][0]} set to {ents.plain(value)}"
    if t == "workflow":
        from core.workflow import service as wf
        title = render(action.get("title"), values) or f"{rule.name}: {ent.label_of(obj)}"
        payload = {"rule": rule.name, "record_type": ent.label, "record": ent.label_of(obj),
                   "note": render(action.get("note"), values) or None,
                   "employee_id": (values.get("employee") or {}).get("id")}
        inst = wf.submit(db, company_id, "automation.approval", payload, title, subject_type=ent.key, subject_id=obj.id)
        return f"Approval started ({inst.reference_no or inst.status})"
    if t == "webhook":
        from modules.automation import webhooks
        hook = _webhook(db, company_id, action.get("webhook_uuid"))
        if hook is None or not hook.active:
            raise ValueError("The webhook is missing or switched off.")
        payload = {"event": "automation.rule", "rule": {"uuid": str(rule.uuid), "name": rule.name},
                   "trigger": values.get("trigger"), "record_type": ent.key, "record": values.get("record"),
                   "employee": {k: values.get("employee", {}).get(k) for k in
                                ("id", "uuid", "name", "email", "employee_code") if values.get("employee")},
                   "changes": values.get("changes"), "sent_at": _now().isoformat()}
        d = webhooks.send(db, hook, "automation.rule", payload, rule_id=rule.id)
        if d.status != "sent":
            return f"Webhook will be tried again ({d.error})"
        return f"Webhook answered {d.response_status}"
    raise ValueError("Unknown action.")


# ── Date rules ──────────────────────────────────────────────────────

def _targets(today: date, offset: int) -> list[date]:
    """Record dates that are due today (and yesterday, in case a run was missed)."""
    return [today - timedelta(days=offset), today - timedelta(days=offset + 1)]


def _yearly_filter(col, d: date):
    conds = [(extract("month", col) == d.month) & (extract("day", col) == d.day)]
    leap = d.year % 4 == 0 and (d.year % 100 != 0 or d.year % 400 == 0)
    if d.month == 2 and d.day == 28 and not leap:
        conds.append((extract("month", col) == 2) & (extract("day", col) == 29))
    return or_(*conds)


def run_date_rule(db: Session, rule: AutomationRule, today: date | None = None) -> int:
    ent = ents.ENTITIES.get(rule.entity)
    if ent is None or not rule.date_field or rule.date_field not in ent.date_fields:
        return 0
    Model = ent.model()
    col = getattr(Model, rule.date_field)
    today = today or _today(db, rule.company_id)
    n = 0
    for target in _targets(today, rule.offset_days or 0):
        q = db.query(Model).filter(Model.company_id == rule.company_id)
        if ent.active_filter:
            q = ent.active_filter(q, Model)
        if rule.repeat_yearly:
            q = q.filter(col.isnot(None), col <= target, _yearly_filter(col, target))
        else:
            q = q.filter(col == target)
        for obj in q.order_by(Model.id).limit(1000).all():
            key = f"{obj.id}:{target.isoformat()}"
            seen = db.query(AutomationRun.id).filter(AutomationRun.rule_id == rule.id,
                                                    AutomationRun.occurrence_key == key).first()
            if seen:
                continue
            try:
                with db.begin_nested():
                    run_rule(db, rule, ent, obj, trigger="date", occurrence_key=key)
                n += 1
            except IntegrityError:
                continue
    return n


def run_date_rules(db: Session, company_id: int | None = None, today: date | None = None) -> int:
    """Periodic job (every company when run with the RLS bypass)."""
    q = db.query(AutomationRule).filter(AutomationRule.trigger == "date", AutomationRule.active.is_(True),
                                        AutomationRule.archived_at.is_(None))
    if company_id is not None:
        q = q.filter(AutomationRule.company_id == company_id)
    total = 0
    for rule in q.order_by(AutomationRule.company_id, AutomationRule.id).all():
        try:
            with db.begin_nested():
                total += run_date_rule(db, rule, today)
        except Exception as exc:
            logger.warning("Date rule %s failed: %s", rule.id, exc)
    return total


# ── Preview ─────────────────────────────────────────────────────────

def preview(db: Session, company_id: int, draft: dict, record) -> dict:
    ent = ents.ENTITIES[draft["entity"]]
    rule = SimpleNamespace(id=0, uuid="preview", name=draft.get("name") or "Draft rule",
                           condition=draft.get("condition"), actions=draft.get("actions") or [])
    trigger = draft.get("trigger") or "updated"
    out = run_rule(db, rule, ent, record, trigger=trigger, changes={}, dry=True)
    return out
