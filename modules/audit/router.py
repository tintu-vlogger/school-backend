"""Audit log screen: field-level changes and events, filtered by record type, person and dates."""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from core.audit import AuditLog
from core.db import get_db
from core.pagination import PageParams, paginate
from core.security import require_permission
from core.timeutil import DEFAULT_TZ, iso_utc

router = APIRouter(prefix="/audit", tags=["Audit"])
VIEW = require_permission("insights.audit.view")


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def _out(e: AuditLog) -> dict:
    return {
        "id": e.id,
        "created_at": iso_utc(e.created_at),
        "actor_type": e.actor_type,
        "actor_id": e.actor_id,
        "actor_name": e.actor_name,
        "action": e.action,
        "entity_type": e.entity_type,
        "entity_id": e.entity_id,
        "entity_uuid": e.entity_uuid,
        "entity_label": e.entity_label,
        "changes": e.changes,
        "details": e.details,
        "ip_address": e.ip_address,
        "request_id": e.request_id,
    }


@router.get("/")
def list_audit_entries(
    entity_type: str | None = None,
    action: str | None = None,
    actor: str | None = Query(None, description="owner, employee:<id> or platform:<id>"),
    entity_uuid: str | None = None,
    q: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    tz: str | None = Query(None, description="Timezone the dates are in (the viewer's)"),
    pages: PageParams = Depends(),
    current_user: dict = Depends(VIEW),
    db: Session = Depends(get_db),
):
    query = db.query(AuditLog).filter(AuditLog.company_id == current_user["company_id"])
    if entity_type:
        query = query.filter(AuditLog.entity_type == entity_type)
    if action:
        query = query.filter(or_(AuditLog.action == action, AuditLog.action.like(f"%.{action}")))
    if entity_uuid:
        query = query.filter(AuditLog.entity_uuid == entity_uuid)
    if actor:
        kind, _, ident = actor.partition(":")
        query = query.filter(AuditLog.actor_type == kind)
        if ident.isdigit():
            col = AuditLog.platform_user_id if kind == "platform" else AuditLog.actor_id
            query = query.filter(col == int(ident))
    if q:
        term = f"%{q.strip()}%"
        query = query.filter(or_(AuditLog.entity_label.ilike(term), AuditLog.actor_name.ilike(term), AuditLog.action.ilike(term)))
    zone = _zone(tz)
    if date_from:
        query = query.filter(AuditLog.created_at >= datetime.combine(date_from, time.min, zone))
    if date_to:
        query = query.filter(AuditLog.created_at < datetime.combine(date_to + timedelta(days=1), time.min, zone))
    query = query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    return paginate(query, pages, _out)


@router.get("/filters")
def audit_filters(current_user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    """Choices for the filter bar: record types and people that appear in the log."""
    cid = current_user["company_id"]
    types = (db.query(AuditLog.entity_type, func.count(AuditLog.id))
             .filter(AuditLog.company_id == cid).group_by(AuditLog.entity_type).order_by(AuditLog.entity_type).all())
    actors = (db.query(AuditLog.actor_type, AuditLog.actor_id, AuditLog.platform_user_id, func.max(AuditLog.actor_name))
              .filter(AuditLog.company_id == cid)
              .group_by(AuditLog.actor_type, AuditLog.actor_id, AuditLog.platform_user_id).limit(500).all())
    out_actors = []
    for kind, emp_id, pid, name in actors:
        key = kind if kind in ("owner", "system", "anonymous") else f"{kind}:{emp_id if kind == 'employee' else pid}"
        label = name or {"owner": "Company owner", "system": "System", "platform": "Platform support"}.get(kind, kind)
        out_actors.append({"value": key, "label": label, "type": kind})
    out_actors.sort(key=lambda a: a["label"].lower())
    return {"entity_types": [{"value": t, "count": c} for t, c in types], "actors": out_actors}
