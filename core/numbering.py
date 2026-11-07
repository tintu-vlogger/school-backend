"""Number series: formats and counters for employee IDs, requests, letters, assets …

A series has a pattern made of text and tokens:

    {PREFIX}  the series prefix          {SEQ} / {SEQ:5}  the counter (padded to 5)
    {YYYY} {YY} {MM} {DD}  today's date in the company's timezone
    {FY}      financial year, e.g. 2627 for April 2026 – March 2027
    {ENTITY}  the legal entity's code (e.g. KA01), for groups with several companies

The counter can reset never, every year, every financial year or every month. Each
period has its own row in `number_series_counters`, bumped with one atomic
INSERT … ON CONFLICT … RETURNING, so two people saving at once never get the same number.

Counters are shared by every legal entity, or (counter_scope = per_entity) each legal
entity counts on its own: the counter row then carries the entity id (0 = shared).
"""
from __future__ import annotations

import re
from datetime import date

from sqlalchemy import (Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func,
                        text)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Session

from core.db import Base
from core.errors import BadRequest, NotFound

RESETS = {"never": "Never", "yearly": "Every calendar year", "financial_year": "Every financial year (April)",
          "monthly": "Every month"}
TOKEN_RE = re.compile(r"\{(PREFIX|YYYY|YY|MM|DD|FY|ENTITY|SEQ(?::(\d{1,2}))?)\}")
COUNTER_SCOPES = {"shared": "One counter for all legal entities", "per_entity": "A separate counter per legal entity"}


class NumberSeries(Base):
    __tablename__ = "number_series"
    __table_args__ = (UniqueConstraint("company_id", "key", name="uq_number_series_company_key"),)

    id = Column(Integer, primary_key=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    key = Column(String(60), nullable=False)  # what it numbers: employee, request, letter, asset …
    name = Column(String(120), nullable=False)
    description = Column(Text, nullable=True)
    prefix = Column(String(30), nullable=False, server_default=text("''"))
    pattern = Column(String(120), nullable=False)
    reset = Column(String(20), nullable=False, server_default=text("'never'"))
    start_number = Column(Integer, nullable=False, server_default=text("1"))
    allow_manual = Column(Boolean, nullable=False, server_default=text("true"))  # may a person type their own?
    counter_scope = Column(String(20), nullable=False, server_default=text("'shared'"))  # shared | per_entity
    is_system = Column(Boolean, nullable=False, server_default=text("false"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class NumberSeriesCounter(Base):
    __tablename__ = "number_series_counters"
    __audit__ = False  # bumped on every number; the numbered record itself is audited
    __table_args__ = (
        UniqueConstraint("series_id", "period", "legal_entity_id", name="uq_number_series_counters_series_period_entity"),
        Index("ix_number_series_counters_company", "company_id", "series_id"),
    )

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    series_id = Column(Integer, ForeignKey("number_series.id"), nullable=False)
    period = Column(String(20), nullable=False)
    # 0 = the shared counter; otherwise the legal entity that counts on its own (no FK, as 0 is not an entity)
    legal_entity_id = Column(Integer, nullable=False, server_default=text("0"))
    last_value = Column(Integer, nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


DEFAULT_SERIES = [
    # key, name, prefix, pattern, reset, active
    ("employee", "Employee ID", "EMP", "{PREFIX}{SEQ:4}", "never", False,
     "Given to new employees when switched on. People can still type an ID by hand if allowed."),
    ("request", "Requests", "REQ", "{PREFIX}-{YY}{MM}-{SEQ:4}", "monthly", True,
     "Numbers for leave, change and other requests that go through approval."),
    ("letter", "Letters", "LTR", "{PREFIX}/{FY}/{SEQ:4}", "financial_year", True, "Reference numbers on letters."),
    ("asset", "Assets", "AST", "{PREFIX}{SEQ:5}", "never", True, "Asset tags."),
    ("ticket", "Help desk tickets", "TKT", "{PREFIX}{YY}{SEQ:5}", "yearly", True, "Help desk ticket numbers."),
    ("student", "Admission number", "ADM", "{PREFIX}{YYYY}{SEQ:4}", "never", True,
     "Given to every new student or learner on admission."),
    ("certificate", "Certificates", "CERT", "{PREFIX}/{YYYY}/{SEQ:4}", "yearly", True,
     "Transfer, bonafide, character and other certificates."),
]


def register_series(key: str, name: str, prefix: str, pattern: str, reset: str = "yearly", active: bool = True,
                    description: str = "") -> None:
    """Modules add their own number series at import time (from hooks.py); every company
    gets it at the next start-up or sign-up."""
    if not any(row[0] == key for row in DEFAULT_SERIES):
        DEFAULT_SERIES.append((key, name, prefix, pattern, reset, active, description))


def ensure_default_series(db: Session, company_id: int) -> None:
    have = {k for (k,) in db.query(NumberSeries.key).filter(NumberSeries.company_id == company_id).all()}
    for key, name, prefix, pattern, reset, active, desc in DEFAULT_SERIES:
        if key not in have:
            db.add(NumberSeries(company_id=company_id, key=key, name=name, prefix=prefix, pattern=pattern, reset=reset,
                                active=active, description=desc, is_system=True))
    db.flush()


# ── Formatting ──────────────────────────────────────────────────────

def validate_pattern(pattern: str) -> None:
    if not pattern or len(pattern) > 120:
        raise BadRequest("Enter a format of up to 120 characters.")
    seqs = re.findall(r"\{SEQ(?::\d{1,2})?\}", pattern)
    if len(seqs) != 1:
        raise BadRequest("The format must contain the counter exactly once, e.g. {SEQ:4}.")
    leftover = TOKEN_RE.sub("", pattern)
    if "{" in leftover or "}" in leftover:
        bad = re.search(r"\{[^}]*\}?", leftover)
        raise BadRequest(f"Unknown part {bad.group(0) if bad else '{'} in the format.")


def period_key(reset: str, today: date) -> str:
    if reset == "yearly":
        return f"{today.year}"
    if reset == "monthly":
        return f"{today.year}-{today.month:02d}"
    if reset == "financial_year":
        start = today.year if today.month >= 4 else today.year - 1
        return f"FY{start}"
    return "all"


def financial_year(today: date) -> str:
    start = today.year if today.month >= 4 else today.year - 1
    return f"{start % 100:02d}{(start + 1) % 100:02d}"


def render(pattern: str, prefix: str, seq: int, today: date, entity_code: str | None = None) -> str:
    def sub(m: re.Match) -> str:
        token = m.group(1)
        if token == "PREFIX":
            return prefix or ""
        if token == "ENTITY":
            return entity_code or ""
        if token == "YYYY":
            return f"{today.year}"
        if token == "YY":
            return f"{today.year % 100:02d}"
        if token == "MM":
            return f"{today.month:02d}"
        if token == "DD":
            return f"{today.day:02d}"
        if token == "FY":
            return financial_year(today)
        width = int(m.group(2) or 1)
        return str(seq).zfill(width)
    return TOKEN_RE.sub(sub, pattern)


# ── Using a series ──────────────────────────────────────────────────

def get_series(db: Session, company_id: int, key: str) -> NumberSeries | None:
    return db.query(NumberSeries).filter(NumberSeries.company_id == company_id, NumberSeries.key == key).first()


def entity_code(entity) -> str:
    """The text {ENTITY} stands for: the entity's code, or the initials of its name."""
    if entity is None:
        return ""
    if entity.code:
        return re.sub(r"[^A-Za-z0-9-]", "", entity.code).upper()[:12]
    words = re.findall(r"[A-Za-z0-9]+", entity.name or "")
    stop = {"pvt", "private", "ltd", "limited", "llp", "and", "of", "the", "india"}
    initials = "".join(w[0] for w in words if w.lower() not in stop) or "".join(w[0] for w in words)
    return initials.upper()[:6]


def _entity(db: Session, company_id: int, legal_entity_id: int | None):
    """The legal entity a number is for; without one, the company's main entity."""
    try:
        from modules.organization.models import LegalEntity
    except Exception:  # pragma: no cover - organisation module not installed
        return None
    q = db.query(LegalEntity).filter(LegalEntity.company_id == company_id)
    if legal_entity_id:
        row = q.filter(LegalEntity.id == legal_entity_id).first()
        if row is not None:
            return row
    return q.filter(LegalEntity.is_primary.is_(True)).order_by(LegalEntity.id).first()


def _uses_entity(series: NumberSeries) -> bool:
    return series.counter_scope == "per_entity" or "{ENTITY}" in (series.pattern or "")


def _counter_entity_id(series: NumberSeries, entity) -> int:
    return entity.id if (series.counter_scope == "per_entity" and entity is not None) else 0


def preview(db: Session, series: NumberSeries, today: date, legal_entity_id: int | None = None) -> str:
    entity = _entity(db, series.company_id, legal_entity_id) if _uses_entity(series) else None
    current = db.query(NumberSeriesCounter.last_value).filter(
        NumberSeriesCounter.series_id == series.id, NumberSeriesCounter.period == period_key(series.reset, today),
        NumberSeriesCounter.legal_entity_id == _counter_entity_id(series, entity),
    ).scalar()
    nxt = (current + 1) if current is not None else series.start_number
    return render(series.pattern, series.prefix, nxt, today, entity_code(entity))


def next_number(db: Session, company_id: int, key: str, today: date | None = None,
                legal_entity_id: int | None = None) -> str | None:
    """Take the next number of an active series, or None when the series is off. Pass the
    record's legal entity for series that use {ENTITY} or count per entity (without one,
    the company's main entity is used)."""
    series = get_series(db, company_id, key)
    if series is None or not series.active:
        return None
    if today is None:
        from core.timeutil import today_for_company_id
        today = today_for_company_id(db, company_id)
    entity = _entity(db, company_id, legal_entity_id) if _uses_entity(series) else None
    period = period_key(series.reset, today)
    value = db.execute(text("""
        INSERT INTO number_series_counters (company_id, series_id, period, legal_entity_id, last_value)
        VALUES (:company_id, :series_id, :period, :entity_id, :start)
        ON CONFLICT (series_id, period, legal_entity_id)
        DO UPDATE SET last_value = number_series_counters.last_value + 1, updated_at = now()
        RETURNING last_value
    """), {"company_id": company_id, "series_id": series.id, "period": period,
           "entity_id": _counter_entity_id(series, entity), "start": series.start_number}).scalar()
    return render(series.pattern, series.prefix, value, today, entity_code(entity))


def require(db: Session, company_id: int, uuid) -> NumberSeries:
    row = db.query(NumberSeries).filter(NumberSeries.company_id == company_id, NumberSeries.uuid == uuid).first()
    if not row:
        raise NotFound("Number series")
    return row
