"""Audit: every change to a business record, and business events, in `audit_logs`.

Row changes are recorded automatically. A `before_flush` hook reads what is about to be
inserted, updated or deleted (old and new values per field), and `after_flush` writes
the audit rows in the same transaction, so a change and its audit entry are committed
or rolled back together. Who and where come from the request context.

Models opt out with `__audit__ = False`. Secrets are masked; very long values (images
stored as data URLs) are replaced by their length.

Business events that are not plain row changes (sign in, export, approval decision) go
through `event(...)`.

`audit_logs` is append-only: a database trigger rejects UPDATE and DELETE.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from uuid import UUID

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, event as sa_event, inspect, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session, relationship
from sqlalchemy.sql import func

from core import context
from core.db import Base


class AuditLog(Base):
    """Who changed what and when. Row changes have action created/updated/deleted and the
    table name as entity_type; events have a dotted action such as auth.signed_in."""
    __tablename__ = "audit_logs"
    __audit__ = False
    __table_args__ = (
        Index("ix_audit_logs_company_created", "company_id", text("created_at DESC")),
        Index("ix_audit_logs_company_entity", "company_id", "entity_type", "entity_id"),
        Index("ix_audit_logs_company_actor", "company_id", "actor_type", "actor_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    # NULL only for platform-level entries (platform users and settings).
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)
    actor_type = Column(String(20), nullable=False)  # owner | employee | platform | system | anonymous
    actor_id = Column(Integer, nullable=True)  # employee id for employees
    platform_user_id = Column(Integer, nullable=True)
    actor_name = Column(String(200), nullable=True)
    action = Column(String(100), nullable=False)
    entity_type = Column(String(50), nullable=False)
    entity_id = Column(Integer, nullable=True)
    entity_uuid = Column(String(64), nullable=True)
    entity_label = Column(String(200), nullable=True)
    changes = Column(JSONB, nullable=True)  # {field: {"old": ..., "new": ...}}
    details = Column(JSONB, nullable=True)
    ip_address = Column(String(64), nullable=True)
    user_agent = Column(String(400), nullable=True)
    request_id = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    company = relationship("Company", back_populates="audit_logs")


# ── Value cleaning ───────────────────────────────────────────────────

MASK = "••••••"
_SECRET_RE = re.compile(r"(password|passwd|secret|token|api_key|apikey|otp|pin_code|private_key|reset)", re.I)
_LAST4_RE = re.compile(r"(aadhaar|aadhar|account_number|account_no|bank_account|card_number|uan)", re.I)
_SKIP_FIELDS = {"updated_at", "created_at"}
_MAX_TEXT = 300


def _mask_value(key: str, value):
    if value is None:
        return None
    if _SECRET_RE.search(key):
        return MASK
    if _LAST4_RE.search(key):
        s = str(value)
        return MASK + s[-4:] if len(s) > 4 else MASK
    return _plain(value, key)


def _plain(value, key: str = ""):
    """JSON-safe, masked, short."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _mask_value(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v, key) for v in value]
    s = str(value)
    if s.startswith("data:") or len(s) > _MAX_TEXT:
        return f"({len(s)} characters)"
    return s


def _label(obj) -> str | None:
    for attrs in (("first_name", "last_name"), ("name",), ("company_name",), ("department_name",),
                  ("designation_name",), ("display_name",), ("label",), ("title",), ("code",)):
        vals = [getattr(obj, a, None) for a in attrs]
        if all(v is not None for v in vals):
            return " ".join(str(v) for v in vals)[:200]
    return None


def _company_of(obj) -> int | None:
    if obj.__table__.name == "companies":
        return getattr(obj, "id", None)
    cid = getattr(obj, "company_id", None)
    return cid if cid is not None else context.current().company_id


def _audited(obj) -> bool:
    return getattr(type(obj), "__audit__", True) and hasattr(type(obj), "__table__")


def _columns(obj):
    return [c for c in obj.__table__.columns if c.key not in _SKIP_FIELDS]


def _snapshot(obj) -> dict:
    state = inspect(obj)
    out = {}
    for col in _columns(obj):
        if col.key in state.unloaded:
            continue
        val = getattr(obj, col.key, None)
        if val is not None:
            out[col.key] = {"old": None, "new": _mask_value(col.key, val)}
    return out


def _diff(obj) -> dict:
    state = inspect(obj)
    out = {}
    for col in _columns(obj):
        attr = state.attrs.get(col.key)
        if attr is None:
            continue
        hist = attr.history
        if not hist.has_changes():
            continue
        old = hist.deleted[0] if hist.deleted else None
        new = hist.added[0] if hist.added else None
        old_p, new_p = _mask_value(col.key, old), _mask_value(col.key, new)
        if old_p == new_p and old_p != MASK:
            continue
        out[col.key] = {"old": old_p, "new": new_p}
    return out


def _actor_fields() -> dict:
    ctx = context.current()
    return {
        "actor_type": ctx.actor_type or "system",
        "actor_id": ctx.employee_id or ctx.portal_account_id,
        "platform_user_id": ctx.platform_user_id,
        "actor_name": (ctx.actor_name or None) and ctx.actor_name[:200],
        "ip_address": ctx.ip_address,
        "user_agent": (ctx.user_agent or "")[:400] or None,
        "request_id": ctx.request_id,
    }


# ── Flush hooks ──────────────────────────────────────────────────────

_PENDING = "_audit_pending"


@sa_event.listens_for(Session, "before_flush")
def _collect(session: Session, _flush_context, _instances):
    pending = session.info.setdefault(_PENDING, [])
    for obj in session.new:
        if _audited(obj):
            pending.append(("created", obj, None))
    for obj in session.dirty:
        if _audited(obj) and session.is_modified(obj, include_collections=False):
            changes = _diff(obj)
            if changes:
                pending.append(("updated", obj, changes))
    for obj in session.deleted:
        if _audited(obj):
            snap = {k: {"old": v["new"], "new": None} for k, v in _snapshot(obj).items()}
            pending.append(("deleted", obj, snap))


@sa_event.listens_for(Session, "after_flush")
def _write(session: Session, _flush_context):
    pending = session.info.pop(_PENDING, [])
    if not pending:
        return
    # Sign-up: a new company makes this session that company's (it has no tenant yet).
    if session.info.get("company_id") is None and not session.info.get("bypass_rls"):
        new_company = next((o for a, o, _ in pending if a == "created" and o.__table__.name == "companies"), None)
        if new_company is not None:
            from core.db import set_tenant
            set_tenant(session, new_company.id)
    actor = _actor_fields()
    rows = []
    for action, obj, changes in pending:
        if action == "created":
            changes = _snapshot(obj)
        uuid_val = obj.__dict__.get("uuid")
        rows.append({
            **actor,
            "company_id": _company_of(obj),
            "action": action,
            "entity_type": obj.__table__.name,
            "entity_id": obj.__dict__.get("id"),
            "entity_uuid": str(uuid_val) if uuid_val is not None else None,
            "entity_label": _label(obj),
            "changes": changes or None,
        })
    session.connection().execute(AuditLog.__table__.insert(), rows)


# ── Business events ──────────────────────────────────────────────────

def event(
    db: Session,
    action: str,
    entity_type: str = "event",
    entity_id: int | None = None,
    details: dict | None = None,
    *,
    company_id: int | None = None,
    entity_uuid: str | None = None,
    entity_label: str | None = None,
) -> None:
    """Record a business event (e.g. "auth.signed_in", "export.downloaded",
    "workflow.approved"). Written with the caller's transaction."""
    ctx = context.current()
    db.add(AuditLog(
        **_actor_fields(),
        company_id=company_id if company_id is not None else ctx.company_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_uuid=entity_uuid,
        entity_label=entity_label,
        details=_plain(details) if details else None,
    ))


def log_action(db: Session, company_id: int, actor_type: str, actor_id: int | None, action: str,
               entity_type: str, entity_id: int | None = None, details: dict | None = None):
    """Older helper kept for callers that record a named event."""
    event(db, action, entity_type, entity_id, details, company_id=company_id)
