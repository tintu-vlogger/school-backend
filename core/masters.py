"""Masters: every list used in a dropdown (blood group, bank, state, document type …).

Each company has its own copy of the system lists (seeded by `ensure_system_masters`),
so it can add values, rename them, reorder them and switch them off. System types and
values can't be removed. Values may have a parent (sub-values) and extra attributes.
"""
from __future__ import annotations

import re

from sqlalchemy import (Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func,
                        or_, text)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Session, joinedload, relationship

from core.db import Base
from core.errors import BadRequest, NotFound


class MasterType(Base):
    __tablename__ = "master_types"
    __table_args__ = (
        UniqueConstraint("company_id", "key", name="uq_master_types_company_key"),
        Index("ix_master_types_company_sort", "company_id", "sort_order"),
    )

    id = Column(Integer, primary_key=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    key = Column(String(60), nullable=False)
    name = Column(String(120), nullable=False)
    description = Column(Text, nullable=True)
    hierarchical = Column(Boolean, nullable=False, server_default=text("false"))
    is_system = Column(Boolean, nullable=False, server_default=text("false"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    sort_order = Column(Integer, nullable=False, server_default=text("0"))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class MasterValue(Base):
    __tablename__ = "master_values"
    __table_args__ = (
        Index("ix_master_values_company_type", "company_id", "type_id", "sort_order"),
        Index("ix_master_values_company_parent", "company_id", "parent_id"),
        Index("uq_master_values_type_code", "type_id", "code", unique=True, postgresql_where=text("code IS NOT NULL")),
    )

    id = Column(Integer, primary_key=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    type_id = Column(Integer, ForeignKey("master_types.id"), nullable=False)
    parent_id = Column(Integer, ForeignKey("master_values.id"), nullable=True)
    code = Column(String(60), nullable=True)
    label = Column(String(200), nullable=False)
    attributes = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    is_system = Column(Boolean, nullable=False, server_default=text("false"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    sort_order = Column(Integer, nullable=False, server_default=text("0"))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    type = relationship("MasterType")
    parent = relationship("MasterValue", remote_side=[id])


# ── Seeding ─────────────────────────────────────────────────────────

def _norm_value(v):
    if isinstance(v, str):
        return v, None, None, None
    v = tuple(v) + (None,) * (4 - len(v))
    return v[0], v[1], v[2], v[3]


def slug_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")[:60]


def ensure_system_masters(db: Session, company_id: int) -> int:
    """Create missing system types and values for a company (idempotent). Returns how
    many rows were added. Existing values are left as the company changed them."""
    from core.master_seed import MASTERS

    added = 0
    types = {t.key: t for t in db.query(MasterType).filter(MasterType.company_id == company_id).all()}
    existing_values: dict[int, dict[str, MasterValue]] = {}
    for tv in db.query(MasterValue).filter(MasterValue.company_id == company_id).all():
        existing_values.setdefault(tv.type_id, {})[(tv.code or tv.label).lower()] = tv

    for order, (key, (name, description, hierarchical, values)) in enumerate(MASTERS.items()):
        mtype = types.get(key)
        if mtype is None:
            mtype = MasterType(company_id=company_id, key=key, name=name, description=description,
                               hierarchical=hierarchical, is_system=True, sort_order=order)
            db.add(mtype)
            db.flush()
            added += 1
        known = existing_values.setdefault(mtype.id, {})

        def add(items, parent_id=None):
            nonlocal added
            for i, raw in enumerate(items):
                label, code, attrs, children = _norm_value(raw)
                code = code or slug_key(label)
                row = known.get(code.lower()) or known.get(label.lower())
                if row is None:
                    row = MasterValue(company_id=company_id, type_id=mtype.id, parent_id=parent_id, code=code,
                                      label=label, attributes=attrs or {}, is_system=True, sort_order=i)
                    db.add(row)
                    db.flush()
                    known[code.lower()] = row
                    added += 1
                if children:
                    add(children, row.id)

        add(values)
    return added


# ── Queries ─────────────────────────────────────────────────────────

def get_type(db: Session, company_id: int, key_or_uuid: str) -> MasterType:
    q = db.query(MasterType).filter(MasterType.company_id == company_id)
    if re.fullmatch(r"[0-9a-f-]{36}", key_or_uuid or ""):
        row = q.filter(MasterType.uuid == key_or_uuid).first()
    else:
        row = q.filter(MasterType.key == key_or_uuid).first()
    if not row:
        raise NotFound("List")
    return row


def get_value(db: Session, company_id: int, uuid) -> MasterValue:
    row = db.query(MasterValue).filter(MasterValue.company_id == company_id, MasterValue.uuid == uuid).first()
    if not row:
        raise NotFound("Value")
    return row


def list_types(db: Session, company_id: int, q: str | None = None, include_inactive: bool = True):
    counts = dict(
        db.query(MasterValue.type_id, func.count(MasterValue.id))
        .filter(MasterValue.company_id == company_id).group_by(MasterValue.type_id).all()
    )
    query = db.query(MasterType).filter(MasterType.company_id == company_id)
    if not include_inactive:
        query = query.filter(MasterType.active.is_(True))
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(MasterType.name.ilike(like), MasterType.key.ilike(like), MasterType.description.ilike(like)))
    rows = query.order_by(MasterType.sort_order, MasterType.name).all()
    return [(t, counts.get(t.id, 0)) for t in rows]


def values_query(db: Session, company_id: int, mtype: MasterType, q: str | None = None, parent_id: int | None = None,
                 top_level_only: bool = False, active: bool | None = None):
    query = (db.query(MasterValue).options(joinedload(MasterValue.parent))
             .filter(MasterValue.company_id == company_id, MasterValue.type_id == mtype.id))
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(MasterValue.label.ilike(like), MasterValue.code.ilike(like)))
    if parent_id is not None:
        query = query.filter(MasterValue.parent_id == parent_id)
    elif top_level_only and not q:
        query = query.filter(MasterValue.parent_id.is_(None))
    if active is not None:
        query = query.filter(MasterValue.active.is_(active))
    return query.order_by(MasterValue.sort_order, MasterValue.label, MasterValue.id)


def child_counts(db: Session, ids: list[int]) -> dict[int, int]:
    if not ids:
        return {}
    return dict(db.query(MasterValue.parent_id, func.count(MasterValue.id))
                .filter(MasterValue.parent_id.in_(ids)).group_by(MasterValue.parent_id).all())


def labels_for(db: Session, company_id: int, ids) -> dict[int, str]:
    ids = [int(i) for i in ids if str(i).isdigit()]
    if not ids:
        return {}
    return dict(db.query(MasterValue.id, MasterValue.label)
                .filter(MasterValue.company_id == company_id, MasterValue.id.in_(ids)).all())


# ── Changes ─────────────────────────────────────────────────────────

def create_type(db: Session, company_id: int, name: str, key: str | None, description: str | None,
                hierarchical: bool) -> MasterType:
    key = slug_key(key or name)
    if not key:
        raise BadRequest("Give the list a name.")
    if db.query(MasterType.id).filter(MasterType.company_id == company_id, MasterType.key == key).first():
        raise BadRequest("A list with this name already exists.")
    order = (db.query(func.max(MasterType.sort_order)).filter(MasterType.company_id == company_id).scalar() or 0) + 1
    row = MasterType(company_id=company_id, key=key, name=name.strip(), description=description,
                     hierarchical=hierarchical, is_system=False, sort_order=order)
    db.add(row)
    db.flush()
    return row


def update_type(mtype: MasterType, data: dict) -> MasterType:
    if mtype.is_system and data.get("active") is False:
        raise BadRequest("Built-in lists can't be switched off. Switch off individual values instead.")
    for field in ("name", "description", "hierarchical", "active", "sort_order"):
        if field in data and data[field] is not None:
            setattr(mtype, field, data[field].strip() if isinstance(data[field], str) else data[field])
    return mtype


def _check_parent(db: Session, company_id: int, mtype: MasterType, parent_id: int | None, value: MasterValue | None):
    if parent_id is None:
        return
    if not mtype.hierarchical:
        raise BadRequest("This list has no sub-values. Turn on sub-values for the list first.")
    parent = db.query(MasterValue).filter(MasterValue.company_id == company_id, MasterValue.id == parent_id).first()
    if not parent or parent.type_id != mtype.id:
        raise BadRequest("The parent value was not found in this list.")
    cursor = parent
    while cursor is not None:
        if value is not None and cursor.id == value.id:
            raise BadRequest("A value can't be placed under itself.")
        cursor = cursor.parent


def create_value(db: Session, company_id: int, mtype: MasterType, data: dict) -> MasterValue:
    label = (data.get("label") or "").strip()
    if not label:
        raise BadRequest("Enter a name for the value.")
    parent_id = data.get("parent_id")
    _check_parent(db, company_id, mtype, parent_id, None)
    dup = values_query(db, company_id, mtype).filter(func.lower(MasterValue.label) == label.lower(),
                                                      MasterValue.parent_id.is_(None) if parent_id is None
                                                      else MasterValue.parent_id == parent_id).first()
    if dup:
        raise BadRequest(f"“{label}” is already in this list.")
    code = (data.get("code") or "").strip() or slug_key(label)
    if db.query(MasterValue.id).filter(MasterValue.type_id == mtype.id, MasterValue.code == code).first():
        code = f"{code}_{db.query(func.count(MasterValue.id)).filter(MasterValue.type_id == mtype.id).scalar() + 1}"
    order = data.get("sort_order")
    if order is None:
        order = (db.query(func.max(MasterValue.sort_order)).filter(MasterValue.type_id == mtype.id).scalar() or 0) + 1
    row = MasterValue(company_id=company_id, type_id=mtype.id, parent_id=parent_id, code=code, label=label,
                      attributes=data.get("attributes") or {}, sort_order=order, active=data.get("active", True))
    db.add(row)
    db.flush()
    return row


def update_value(db: Session, company_id: int, value: MasterValue, data: dict) -> MasterValue:
    if "parent_id" in data:
        _check_parent(db, company_id, value.type, data["parent_id"], value)
        value.parent_id = data["parent_id"]
    if "label" in data and data["label"] is not None:
        label = data["label"].strip()
        if not label:
            raise BadRequest("Enter a name for the value.")
        value.label = label
    if "code" in data and data["code"] is not None:
        if value.is_system and data["code"] != value.code:
            raise BadRequest("The code of a built-in value can't be changed.")
        value.code = data["code"].strip() or value.code
    for field in ("active", "sort_order", "attributes"):
        if field in data and data[field] is not None:
            setattr(value, field, data[field])
    return value


def reorder_values(db: Session, company_id: int, mtype: MasterType, ids: list[int]) -> None:
    rows = {v.id: v for v in db.query(MasterValue).filter(MasterValue.company_id == company_id,
                                                           MasterValue.type_id == mtype.id,
                                                           MasterValue.id.in_(ids)).all()}
    for i, vid in enumerate(ids):
        if vid in rows and rows[vid].sort_order != i:
            rows[vid].sort_order = i


def validate_value_ids(db: Session, company_id: int, type_key: str, ids: list[int]) -> None:
    """Raise when any id is not an active value of the given list (used by custom fields)."""
    if not ids:
        return
    mtype = get_type(db, company_id, type_key)
    found = {i for (i,) in db.query(MasterValue.id).filter(MasterValue.company_id == company_id,
                                                           MasterValue.type_id == mtype.id,
                                                           MasterValue.id.in_(ids)).all()}
    missing = [i for i in ids if i not in found]
    if missing:
        raise BadRequest(f"Pick a value from the {mtype.name} list.")
