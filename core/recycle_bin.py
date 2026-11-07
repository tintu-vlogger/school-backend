"""Recycle bin registry: models declare that their soft-deleted rows can be listed,
restored and (where the law allows) removed for good.

Records are never hard-deleted in normal use (platform.md §6). A module marks a row as
deleted in one of four ways, and registers the model here with the matching `flag`:

    "archived_at"  a timestamp column is set (also clears `active` when the model has it)
    "deleted_at"   same, with a deleted_at column
    "active"       a boolean switched off
    "status"       a status column takes one of `status_values` (e.g. "archived")

Register in the module's hooks.py (or in modules/recycle_bin/catalogue.py for models that
existed before the bin):

    from core import recycle_bin as bin
    bin.register(bin.BinKind(key="department", label="Department", group="Organisation",
                             model=Department, flag="active", title=lambda d: d.department_name))

Rules the bin applies to every kind:
  - restore is refused while a record it points to (a parent, a folder, a list) is itself
    in the bin, and when another live record already uses the same code;
  - purge only for kinds with `purgeable=True`, only after the company's retention
    period, and never when other records still point to the row (the database refuses);
  - every restore and purge is written to the audit log.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

FLAGS = ("archived_at", "deleted_at", "active", "status")


@dataclass
class BinKind:
    key: str                                  # stable id used in URLs, e.g. "department"
    label: str                                # "Department"
    group: str                                # "Organisation", "Lists and fields", ...
    model: Any                                # SQLAlchemy model class (company_id + uuid columns)
    flag: str                                 # one of FLAGS
    status_values: tuple = ()                 # flag="status": values that mean "in the bin"
    restore_status: str | None = None         # flag="status": value set on restore
    title: Callable[[Any], str] | None = None  # display name; default name/title/label/code
    subtitle: Callable[[Any], str | None] | None = None
    purgeable: bool = True                    # False: kept for good (records the law needs)
    keep_reason: str | None = None            # shown when purgeable is False
    where: Callable[[Any], Any] | None = None  # extra filter: fn(query) -> query
    restore_check: Callable[[Any, Any], list[str]] | None = None  # fn(db, row) -> problems
    on_restore: Callable[[Any, Any], None] | None = None          # fn(db, row) after the flag is cleared
    unique_fields: tuple = ("code",)          # live rows must not share these values on restore
    hidden_fields: tuple = ()                 # never shown in the details view
    extra: dict = field(default_factory=dict)

    @property
    def table(self) -> str:
        return self.model.__tablename__


_REGISTRY: dict[str, BinKind] = {}


def register(kind: BinKind) -> BinKind:
    if kind.flag not in FLAGS:
        raise ValueError(f"Unknown recycle bin flag {kind.flag}")
    if kind.flag == "status" and not (kind.status_values and kind.restore_status):
        raise ValueError("A status-based kind needs status_values and restore_status")
    _REGISTRY[kind.key] = kind
    return kind


def get(key: str) -> BinKind | None:
    return _REGISTRY.get(key)


def kinds() -> list[BinKind]:
    return sorted(_REGISTRY.values(), key=lambda k: (k.group, k.label))


def by_table() -> dict[str, BinKind]:
    out: dict[str, BinKind] = {}
    for k in _REGISTRY.values():
        out.setdefault(k.table, k)
    return out


def in_bin_clause(kind: BinKind):
    """SQL condition that is true for rows of this kind that are in the bin."""
    m = kind.model
    if kind.flag in ("archived_at", "deleted_at"):
        return getattr(m, kind.flag).isnot(None)
    if kind.flag == "active":
        return m.active.is_(False)
    return m.status.in_(kind.status_values)


def is_in_bin(kind: BinKind, row) -> bool:
    if kind.flag in ("archived_at", "deleted_at"):
        return getattr(row, kind.flag) is not None
    if kind.flag == "active":
        return row.active is False
    return row.status in kind.status_values


def clear_flag(kind: BinKind, row) -> None:
    if kind.flag in ("archived_at", "deleted_at"):
        setattr(row, kind.flag, None)
        if hasattr(row, "archived_by") and kind.flag == "archived_at":
            row.archived_by = None
        if hasattr(row, "active") and row.active is False:
            row.active = True
    elif kind.flag == "active":
        row.active = True
        if hasattr(row, "archived_at") and getattr(row, "archived_at", None) is not None:
            row.archived_at = None
    else:
        row.status = kind.restore_status


def title_of(kind: BinKind, row) -> str:
    if kind.title:
        try:
            return str(kind.title(row) or "")[:200]
        except Exception:
            pass
    for f in ("name", "title", "label", "department_name", "code", "key"):
        v = getattr(row, f, None)
        if v:
            return str(v)[:200]
    return f"{kind.label} {row.id}"
