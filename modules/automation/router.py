"""Automation rules, their run log and webhooks (Settings > Automation)."""
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from core.db import get_db
from core.errors import BadRequest, NotFound
from core.pagination import PageParams, paginate
from core.security import require_permission
from core.workflow import registry
from modules.automation import engine, entities as ents, service, webhooks
from modules.automation.models import AutomationRule, AutomationRun, AutomationWebhook, AutomationWebhookDelivery

router = APIRouter(prefix="/automation", tags=["Automation"])
MANAGE = require_permission("settings.automation.manage")


class RuleIn(BaseModel):
    name: str = Field(..., max_length=150)
    description: Optional[str] = Field(None, max_length=1000)
    entity: str
    trigger: str
    fields: list[str] = []
    request_type: Optional[str] = None
    date_field: Optional[str] = None
    offset_days: int = 0
    repeat_yearly: bool = False
    condition: Optional[str] = Field(None, max_length=2000)
    actions: list[dict[str, Any]] = []
    active: bool = True


class ActiveIn(BaseModel):
    active: bool


class PreviewIn(RuleIn):
    name: str = Field("Draft rule", max_length=150)
    record_uuid: Optional[str] = None


class WebhookIn(BaseModel):
    name: Optional[str] = Field(None, max_length=100)
    url: Optional[str] = Field(None, max_length=500)
    headers: Optional[dict[str, str]] = None
    active: Optional[bool] = None


# ── Meta ────────────────────────────────────────────────────────────

@router.get("/meta")
def meta(current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    from modules.employees.service import WORK_MODES
    hooks = (db.query(AutomationWebhook).filter(AutomationWebhook.company_id == current_user["company_id"],
                                                AutomationWebhook.archived_at.is_(None))
             .order_by(AutomationWebhook.name).all())
    return {
        "entities": [{"key": e.key, "label": e.label, "triggers": list(e.triggers),
                      "fields": [{"value": k, "label": v} for k, v in e.fields.items()],
                      "date_fields": [{"value": k, "label": v} for k, v in e.date_fields.items()],
                      "condition_fields": ents.condition_fields(e)}
                     for e in ents.available().values()],
        "triggers": [{"value": k, "label": v} for k, v in service.TRIGGERS.items()],
        "action_types": [{"value": k, "label": v} for k, v in engine.ACTION_TYPES.items()],
        "audiences": [{"value": k, "label": v} for k, v in engine.AUDIENCES.items()],
        "roles": [{"value": r, "label": label} for r, label in
                  (("hr", "HR"), ("manager", "Manager"), ("admin", "HR admin"), ("employee", "Employee"))],
        "update_fields": [{"value": k, "label": v[0], "kind": v[1]} for k, v in engine.UPDATE_FIELDS.items()],
        "work_modes": [{"value": k, "label": v} for k, v in WORK_MODES.items()],
        "request_types": [{"value": rt.key, "label": rt.label} for rt in registry.all_types()],
        "webhooks": [{"value": str(w.uuid), "label": w.name, "active": w.active} for w in hooks],
        "priorities": [{"value": p, "label": p.capitalize()} for p in service.PRIORITIES],
    }


# ── Rules ───────────────────────────────────────────────────────────

@router.get("/rules")
def list_rules(q: Optional[str] = None, entity: Optional[str] = None, trigger: Optional[str] = None,
               active: Optional[bool] = None, pages: PageParams = Depends(),
               current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    query = db.query(AutomationRule).filter(AutomationRule.company_id == current_user["company_id"],
                                            AutomationRule.archived_at.is_(None))
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(AutomationRule.name.ilike(like), AutomationRule.description.ilike(like)))
    if entity:
        query = query.filter(AutomationRule.entity.in_(entity.split(",")))
    if trigger:
        query = query.filter(AutomationRule.trigger.in_(trigger.split(",")))
    if active is not None:
        query = query.filter(AutomationRule.active.is_(active))
    return paginate(query.order_by(AutomationRule.active.desc(), AutomationRule.name), pages, service.rule_out)


@router.post("/rules", status_code=201)
def create_rule(req: RuleIn, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    row = service.save_rule(db, current_user["company_id"], req.model_dump())
    db.commit()
    return service.rule_out(row)


@router.get("/rules/{uuid}")
def get_rule(uuid: str, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    return service.rule_out(service.get_rule(db, current_user["company_id"], uuid))


@router.put("/rules/{uuid}")
def update_rule(uuid: str, req: RuleIn, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    row = service.save_rule(db, cid, req.model_dump(), service.get_rule(db, cid, uuid))
    db.commit()
    return service.rule_out(row)


@router.post("/rules/{uuid}/active")
def set_active(uuid: str, req: ActiveIn, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    row = service.get_rule(db, current_user["company_id"], uuid)
    row.active = req.active
    db.commit()
    return service.rule_out(row)


@router.post("/rules/{uuid}/archive")
def archive_rule(uuid: str, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    service.archive_rule(db, service.get_rule(db, current_user["company_id"], uuid))
    db.commit()
    return {"detail": "Rule removed"}


def _sample_record(db: Session, company_id: int, entity: str, uuid: Optional[str]):
    ent = ents.available().get(entity)
    if ent is None:
        raise BadRequest("Pick what kind of record the rule watches.")
    Model = ent.model()
    q = db.query(Model).filter(Model.company_id == company_id)
    if uuid:
        row = q.filter(Model.uuid == service.parse_uuid(uuid, "Sample record")).first()
    else:
        row = q.order_by(Model.id.desc()).first()
    if row is None:
        raise NotFound("Sample record")
    return row


@router.post("/preview")
def preview(req: PreviewIn, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    """Test a rule (saved or not) against one record without doing anything."""
    cid = current_user["company_id"]
    draft = service.clean_rule(db, cid, req.model_dump())
    record = _sample_record(db, cid, draft["entity"], req.record_uuid)
    out = engine.preview(db, cid, draft, record)
    db.rollback()
    values = out.pop("values", {})
    return {**out, "values": {k: values.get(k) for k in ("record", "employee")}}


@router.get("/records")
def sample_records(entity: str, q: Optional[str] = None, current_user: dict = Depends(MANAGE),
                   db: Session = Depends(get_db)):
    """Records to test a rule against."""
    ent = ents.available().get(entity)
    if ent is None:
        raise BadRequest("Unknown kind of record.")
    Model = ent.model()
    query = db.query(Model).filter(Model.company_id == current_user["company_id"])
    if q and entity == "employee":
        like = f"%{q.strip()}%"
        query = query.filter(or_(Model.first_name.ilike(like), Model.last_name.ilike(like), Model.email.ilike(like)))
    rows = query.order_by(Model.id.desc()).limit(25).all()
    return [{"value": str(r.uuid), "label": ent.label_of(r)} for r in rows]


# ── Run log ─────────────────────────────────────────────────────────

@router.get("/runs")
def list_runs(rule: Optional[str] = None, status: Optional[str] = None, pages: PageParams = Depends(),
              current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    query = db.query(AutomationRun).filter(AutomationRun.company_id == cid)
    if rule:
        query = query.filter(AutomationRun.rule_id == service.get_rule(db, cid, rule).id)
    if status:
        query = query.filter(AutomationRun.status.in_(status.split(",")))
    names = dict(db.query(AutomationRule.id, AutomationRule.name).filter(AutomationRule.company_id == cid).all())
    return paginate(query.order_by(AutomationRun.created_at.desc(), AutomationRun.id.desc()), pages,
                    lambda r: service.run_out(r, names))


@router.post("/run-date-rules")
def run_date_rules(current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    """Check date rules now (they also run by themselves every 15 minutes)."""
    n = engine.run_date_rules(db, current_user["company_id"])
    db.commit()
    return {"ran": n}


# ── Webhooks ────────────────────────────────────────────────────────

@router.get("/webhooks")
def list_webhooks(current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    rows = (db.query(AutomationWebhook).filter(AutomationWebhook.company_id == cid,
                                               AutomationWebhook.archived_at.is_(None))
            .order_by(AutomationWebhook.name).all())
    stats = {}
    for wid, status, n in (db.query(AutomationWebhookDelivery.webhook_id, AutomationWebhookDelivery.status,
                                    func.count()).filter(AutomationWebhookDelivery.company_id == cid)
                           .group_by(AutomationWebhookDelivery.webhook_id, AutomationWebhookDelivery.status).all()):
        stats.setdefault(wid, {"sent": 0, "queued": 0, "failed": 0})[status] = n
    return [service.webhook_out(w, stats={"deliveries": stats.get(w.id, {"sent": 0, "queued": 0, "failed": 0})})
            for w in rows]


@router.post("/webhooks", status_code=201)
def create_webhook(req: WebhookIn, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    row = service.save_webhook(db, current_user["company_id"], req.model_dump())
    db.commit()
    return service.webhook_out(row, show_secret=True)


@router.put("/webhooks/{uuid}")
def update_webhook(uuid: str, req: WebhookIn, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    row = service.save_webhook(db, cid, req.model_dump(), service.get_webhook(db, cid, uuid))
    db.commit()
    return service.webhook_out(row)


@router.post("/webhooks/{uuid}/secret")
def rotate_secret(uuid: str, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    row = service.get_webhook(db, current_user["company_id"], uuid)
    row.secret = webhooks.new_secret()
    db.commit()
    return service.webhook_out(row, show_secret=True)


@router.post("/webhooks/{uuid}/archive")
def archive_webhook(uuid: str, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    from datetime import datetime, timezone
    row = service.get_webhook(db, current_user["company_id"], uuid)
    row.archived_at = datetime.now(timezone.utc)
    row.active = False
    db.commit()
    return {"detail": "Webhook removed"}


@router.post("/webhooks/{uuid}/test")
def test_webhook(uuid: str, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    from datetime import datetime, timezone
    row = service.get_webhook(db, current_user["company_id"], uuid)
    d = webhooks.send(db, row, "automation.test", {"event": "automation.test", "message": "This is a test call.",
                                                   "sent_at": datetime.now(timezone.utc).isoformat()})
    db.commit()
    return service.delivery_out(d, {row.id: row.name})


@router.get("/webhook-deliveries")
def list_deliveries(webhook: Optional[str] = None, status: Optional[str] = None, pages: PageParams = Depends(),
                    current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    query = db.query(AutomationWebhookDelivery).filter(AutomationWebhookDelivery.company_id == cid)
    if webhook:
        query = query.filter(AutomationWebhookDelivery.webhook_id == service.get_webhook(db, cid, webhook).id)
    if status:
        query = query.filter(AutomationWebhookDelivery.status.in_(status.split(",")))
    names = dict(db.query(AutomationWebhook.id, AutomationWebhook.name).filter(AutomationWebhook.company_id == cid).all())
    return paginate(query.order_by(AutomationWebhookDelivery.created_at.desc(), AutomationWebhookDelivery.id.desc()),
                    pages, lambda d: service.delivery_out(d, names))


@router.post("/webhook-deliveries/{uuid}/retry")
def retry_delivery(uuid: str, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    d = db.query(AutomationWebhookDelivery).filter(AutomationWebhookDelivery.company_id == cid,
                                                   AutomationWebhookDelivery.uuid == service.parse_uuid(uuid)).first()
    if d is None:
        raise NotFound("Delivery")
    if d.status == "sent":
        raise BadRequest("This call already went through.")
    webhooks.retry_now(db, d)
    db.commit()
    names = dict(db.query(AutomationWebhook.id, AutomationWebhook.name).filter(AutomationWebhook.company_id == cid).all())
    return service.delivery_out(d, names)
