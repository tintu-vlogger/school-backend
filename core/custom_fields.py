"""Custom fields: extra fields a company adds to employees (and later other records).

Definitions live in `custom_field_sections` and `custom_field_defs`; the values live in
the record's own `custom` JSONB column, keyed by the field key. `clean_values` checks and
normalises submitted values against the definitions (type, required, limits, master
values), so every module stores custom values the same way.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Session, relationship

from core.db import Base
from core.errors import BadRequest

ENTITIES = {
    "employee": "Employee",
    "department": "Department",
    "designation": "Designation",
    "location": "Location",
    "legal_entity": "Legal entity",
    "asset": "Asset",
    "helpdesk_case": "Help desk case",
    "candidate": "Candidate",
    "student": "Student",
    "guardian": "Parent / guardian",
    "admission": "Admission application",
}
FIELD_TYPES = {
    "text": "Short text", "textarea": "Long text", "number": "Number", "date": "Date", "boolean": "Yes / no",
    "single_choice": "Choose one", "multi_choice": "Choose many", "email": "Email", "phone": "Phone",
    "url": "Web link", "file": "File",
}
CHOICE_TYPES = {"single_choice", "multi_choice"}
MAX_FILE_CHARS = 2_800_000  # about 2 MB as a data URL


class CustomFieldSection(Base):
    __tablename__ = "custom_field_sections"
    __table_args__ = (Index("ix_custom_field_sections_company_entity", "company_id", "entity", "sort_order"),)

    id = Column(Integer, primary_key=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    entity = Column(String(40), nullable=False)
    name = Column(String(120), nullable=False)
    description = Column(Text, nullable=True)
    sort_order = Column(Integer, nullable=False, server_default=text("0"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class CustomFieldDef(Base):
    __tablename__ = "custom_field_defs"
    __table_args__ = (
        UniqueConstraint("company_id", "entity", "key", name="uq_custom_field_defs_company_entity_key"),
        Index("ix_custom_field_defs_company_entity", "company_id", "entity", "sort_order"),
        Index("ix_custom_field_defs_company_section", "company_id", "section_id"),
    )

    id = Column(Integer, primary_key=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    entity = Column(String(40), nullable=False)
    section_id = Column(Integer, ForeignKey("custom_field_sections.id"), nullable=True)
    key = Column(String(60), nullable=False)
    label = Column(String(120), nullable=False)
    field_type = Column(String(30), nullable=False)
    master_type_key = Column(String(60), nullable=True)  # choices from a master list
    options = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))  # or fixed choices
    required = Column(Boolean, nullable=False, server_default=text("false"))
    help_text = Column(String(300), nullable=True)
    placeholder = Column(String(120), nullable=True)
    validation = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))  # min, max, min_length, max_length, pattern
    visible_to_employee = Column(Boolean, nullable=False, server_default=text("true"))
    editable_by_employee = Column(Boolean, nullable=False, server_default=text("false"))
    sort_order = Column(Integer, nullable=False, server_default=text("0"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    section = relationship("CustomFieldSection")


def _key(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (label or "").lower()).strip("_")[:60]


def check_entity(entity: str) -> str:
    if entity not in ENTITIES:
        raise BadRequest("Custom fields are not available for this kind of record.")
    return entity


def definitions(db: Session, company_id: int, entity: str, active_only: bool = True) -> list[CustomFieldDef]:
    q = db.query(CustomFieldDef).filter(CustomFieldDef.company_id == company_id, CustomFieldDef.entity == entity)
    if active_only:
        q = q.filter(CustomFieldDef.active.is_(True))
    return q.order_by(CustomFieldDef.sort_order, CustomFieldDef.id).all()


def sections(db: Session, company_id: int, entity: str, active_only: bool = False) -> list[CustomFieldSection]:
    q = db.query(CustomFieldSection).filter(CustomFieldSection.company_id == company_id, CustomFieldSection.entity == entity)
    if active_only:
        q = q.filter(CustomFieldSection.active.is_(True))
    return q.order_by(CustomFieldSection.sort_order, CustomFieldSection.id).all()


# ── Definitions ─────────────────────────────────────────────────────

def _check_definition(db: Session, company_id: int, data: dict) -> None:
    ftype = data.get("field_type")
    if ftype is not None and ftype not in FIELD_TYPES:
        raise BadRequest("Pick a field type from the list.")
    if ftype in CHOICE_TYPES:
        if not data.get("master_type_key") and not data.get("options"):
            raise BadRequest("A choice field needs a list: pick a master list or type the choices.")
    if data.get("master_type_key"):
        from core.masters import get_type
        get_type(db, company_id, data["master_type_key"])
    if data.get("section_id") is not None:
        sec = db.query(CustomFieldSection.id).filter(CustomFieldSection.company_id == company_id,
                                                    CustomFieldSection.id == data["section_id"]).first()
        if not sec:
            raise BadRequest("The section was not found.")
    if data.get("validation", {}).get("pattern"):
        try:
            re.compile(data["validation"]["pattern"])
        except re.error:
            raise BadRequest("The pattern is not valid.")


def create_definition(db: Session, company_id: int, entity: str, data: dict) -> CustomFieldDef:
    check_entity(entity)
    _check_definition(db, company_id, data)
    key = _key(data.get("key") or data["label"])
    if not key:
        raise BadRequest("Give the field a name.")
    if db.query(CustomFieldDef.id).filter(CustomFieldDef.company_id == company_id, CustomFieldDef.entity == entity,
                                          CustomFieldDef.key == key).first():
        raise BadRequest("A field with this name already exists. Use a different name.")
    order = (db.query(func.max(CustomFieldDef.sort_order)).filter(CustomFieldDef.company_id == company_id,
                                                                  CustomFieldDef.entity == entity).scalar() or 0) + 1
    row = CustomFieldDef(company_id=company_id, entity=entity, key=key, sort_order=order,
                         **{k: v for k, v in data.items() if k not in ("key", "sort_order") and v is not None})
    db.add(row)
    db.flush()
    return row


def update_definition(db: Session, company_id: int, row: CustomFieldDef, data: dict) -> CustomFieldDef:
    merged = {"field_type": row.field_type, "master_type_key": row.master_type_key, "options": row.options, **data}
    _check_definition(db, company_id, merged)
    if "field_type" in data and data["field_type"] != row.field_type:
        raise BadRequest("The type of a field can't be changed after it is created. Add a new field instead.")
    for k, v in data.items():
        if k != "key":
            setattr(row, k, v)
    return row


# ── Values ──────────────────────────────────────────────────────────

def _empty(v) -> bool:
    return v is None or v == "" or v == [] or v == {}


def _one(db: Session, company_id: int, d: CustomFieldDef, value):
    t, rules, label = d.field_type, d.validation or {}, d.label
    if t in ("text", "textarea", "email", "phone", "url"):
        s = str(value).strip()
        if rules.get("min_length") and len(s) < int(rules["min_length"]):
            raise BadRequest(f"{label} must be at least {rules['min_length']} characters.")
        limit = int(rules.get("max_length") or (2000 if t == "textarea" else 255))
        if len(s) > limit:
            raise BadRequest(f"{label} can be at most {limit} characters.")
        if t == "email" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", s):
            raise BadRequest(f"{label} must be an email address.")
        if t == "phone" and not re.fullmatch(r"[+\d][\d\s-]{6,19}", s):
            raise BadRequest(f"{label} must be a phone number.")
        if t == "url" and not re.match(r"https?://", s):
            raise BadRequest(f"{label} must start with http:// or https://.")
        if rules.get("pattern") and not re.fullmatch(rules["pattern"], s):
            raise BadRequest(rules.get("pattern_message") or f"{label} is not in the right format.")
        return s
    if t == "number":
        try:
            n = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise BadRequest(f"{label} must be a number.")
        if rules.get("min") is not None and n < Decimal(str(rules["min"])):
            raise BadRequest(f"{label} must be at least {rules['min']}.")
        if rules.get("max") is not None and n > Decimal(str(rules["max"])):
            raise BadRequest(f"{label} must be at most {rules['max']}.")
        return int(n) if n == n.to_integral_value() else float(n)
    if t == "date":
        try:
            return date.fromisoformat(str(value)[:10]).isoformat()
        except ValueError:
            raise BadRequest(f"{label} must be a date.")
    if t == "boolean":
        if isinstance(value, bool):
            return value
        if str(value).lower() in ("true", "yes", "1"):
            return True
        if str(value).lower() in ("false", "no", "0"):
            return False
        raise BadRequest(f"{label} must be yes or no.")
    if t in CHOICE_TYPES:
        items = value if isinstance(value, list) else [value]
        if t == "single_choice" and len(items) != 1:
            raise BadRequest(f"Pick one value for {label}.")
        if d.master_type_key:
            ids = []
            for i in items:
                try:
                    ids.append(int(i))
                except (TypeError, ValueError):
                    raise BadRequest(f"Pick a value for {label} from the list.")
            from core.masters import validate_value_ids
            validate_value_ids(db, company_id, d.master_type_key, ids)
            return ids[0] if t == "single_choice" else ids
        allowed = {str(o.get("value") if isinstance(o, dict) else o) for o in (d.options or [])}
        clean = [str(i) for i in items]
        if any(c not in allowed for c in clean):
            raise BadRequest(f"Pick a value for {label} from the list.")
        return clean[0] if t == "single_choice" else clean
    if t == "file":
        if not isinstance(value, dict) or not value.get("name") or not value.get("url"):
            raise BadRequest(f"Upload a file for {label}.")
        if len(str(value["url"])) > MAX_FILE_CHARS:
            raise BadRequest(f"The file for {label} must be under 2 MB.")
        return {"name": str(value["name"])[:200], "url": value["url"], "size": value.get("size")}
    return value


def clean_values(db: Session, company_id: int, entity: str, values: dict | None, existing: dict | None = None,
                 partial: bool = False, only_employee_editable: bool = False) -> dict:
    """Check submitted custom values. Unknown keys are refused; required fields must have
    a value (on create, or when sent on update). Returns the cleaned values to store."""
    values = values or {}
    defs = {d.key: d for d in definitions(db, company_id, entity)}
    unknown = [k for k in values if k not in defs]
    if unknown:
        raise BadRequest(f"Unknown field: {unknown[0]}.")
    out: dict = {}
    for key, d in defs.items():
        if key not in values:
            if not partial and d.required and _empty((existing or {}).get(key)):
                raise BadRequest(f"{d.label} is required.")
            continue
        if only_employee_editable and not d.editable_by_employee:
            raise BadRequest(f"{d.label} can only be changed by HR.")
        v = values[key]
        if _empty(v):
            if d.required:
                raise BadRequest(f"{d.label} is required.")
            out[key] = None
            continue
        out[key] = _one(db, company_id, d, v)
    return out


def merge_values(db: Session, company_id: int, entity: str, values: dict | None, existing: dict | None = None,
                 partial: bool = False) -> dict:
    """Clean submitted values and merge them over the record's current ones; the result is
    what to store in the record's `custom` column (empty values are dropped). On create
    (`partial=False`) required fields must be present."""
    cleaned = clean_values(db, company_id, entity, values, existing, partial=partial)
    merged = {**(existing or {}), **cleaned}
    return {k: v for k, v in merged.items() if v is not None}
