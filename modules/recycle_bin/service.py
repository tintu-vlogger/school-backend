"""Recycle bin: list, view, restore and purge soft-deleted records of every registered kind.

Who deleted a record and when comes from the record itself (archived_at, archived_by) or
else from the audit log (the last change that switched the flag), so no module needs
extra columns. Restores and purges are business events in the audit log; a purge also
keeps a masked copy of the row there.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core import audit
from core import recycle_bin as rb
from core.errors import BadRequest, NotFound
from modules.recycle_bin.models import RecycleBinSetting

DEFAULT_RETENTION = 90
MIN_RETENTION = 30
PER_KIND_LIMIT = 2000
_discovered = False


def ensure_registry() -> None:
    """Register the catalogue once (hooks import it) and pick up any model that has not
    declared itself."""
    global _discovered
    if _discovered:
        return
    from modules import import_all_models
    from modules.recycle_bin import catalogue
    import_all_models()
    if not rb.get("department"):
        catalogue.register_all()
    catalogue.discover()
    _discovered = True


def now() -> datetime:
    return datetime.now(timezone.utc)


# ── Settings ────────────────────────────────────────────────────────

def settings(db: Session, company_id: int) -> RecycleBinSetting:
    s = db.query(RecycleBinSetting).filter(RecycleBinSetting.company_id == company_id).first()
    if s is None:
        # two first requests at once must not clash on the one-row-per-company rule
        from sqlalchemy.dialects.postgresql import insert
        db.execute(insert(RecycleBinSetting).values(company_id=company_id, retention_days=DEFAULT_RETENTION, auto_purge=False)
                   .on_conflict_do_nothing(constraint="uq_recycle_bin_settings_company"))
        s = db.query(RecycleBinSetting).filter(RecycleBinSetting.company_id == company_id).first()
    return s


def save_settings(db: Session, company_id: int, data: dict) -> RecycleBinSetting:
    s = settings(db, company_id)
    if "retention_days" in data and data["retention_days"] is not None:
        days = int(data["retention_days"])
        if days < MIN_RETENTION or days > 3650:
            raise BadRequest(f"Keep deleted records for between {MIN_RETENTION} and 3650 days.")
        s.retention_days = days
    if "auto_purge" in data and data["auto_purge"] is not None:
        s.auto_purge = bool(data["auto_purge"])
    db.flush()
    return s


def settings_out(s: RecycleBinSetting) -> dict:
    return {"retention_days": s.retention_days, "auto_purge": s.auto_purge, "min_retention_days": MIN_RETENTION}


# ── Reading the bin ─────────────────────────────────────────────────

def _query(db: Session, company_id: int, kind: rb.BinKind):
    m = kind.model
    q = db.query(m).filter(m.company_id == company_id, rb.in_bin_clause(kind))
    if kind.where:
        q = kind.where(q)
    return q


def _deletion_info(db: Session, company_id: int, kind: rb.BinKind, rows: list) -> dict[int, dict]:
    """{row id: {"at": datetime, "by": name}} from the row or the audit log."""
    out: dict[int, dict] = {}
    need = []
    for r in rows:
        at = getattr(r, kind.flag, None) if kind.flag in ("archived_at", "deleted_at") else None
        by = getattr(r, "archived_by", None) if kind.flag == "archived_at" else None
        out[r.id] = {"at": at, "by": by}
        if at is None or by is None:
            need.append(r.id)
    if need:
        field = kind.flag
        logs = db.execute(text(
            "SELECT entity_id, actor_name, created_at, changes, action FROM audit_logs "
            "WHERE company_id = :cid AND entity_type = :t AND entity_id = ANY(:ids) "
            "AND action IN ('updated', 'created') ORDER BY id DESC"),
            {"cid": company_id, "t": kind.table, "ids": need}).fetchall()
        seen: set = set()
        for eid, actor, created, changes, action in logs:
            if eid in seen:
                continue
            if action == "updated" and field not in (changes or {}):
                continue
            seen.add(eid)
            info = out[eid]
            if action == "created" and kind.flag in ("active", "status"):
                info["created_off"] = True  # made switched off (e.g. a sample), never deleted
            info["at"] = info["at"] or created
            info["by"] = info["by"] or actor
    for r in rows:
        info = out[r.id]
        info["at"] = info["at"] or getattr(r, "updated_at", None) or getattr(r, "created_at", None)
    return out


def _purge_after(at: datetime | None, retention: int) -> datetime | None:
    return at + timedelta(days=retention) if at else None


def _blocked_by_system(row) -> bool:
    return bool(getattr(row, "is_system", False))


def item_out(kind: rb.BinKind, row, info: dict, retention: int) -> dict:
    at = info.get("at")
    after = _purge_after(at, retention)
    purge_reason = None
    if not kind.purgeable:
        purge_reason = kind.keep_reason or "Kept for good"
    elif _blocked_by_system(row):
        purge_reason = "Built into the product; it can be restored but not removed"
    sub = None
    if kind.subtitle:
        try:
            sub = kind.subtitle(row)
        except Exception:
            sub = None
    return {"kind": kind.key, "kind_label": kind.label, "group": kind.group, "uuid": str(row.uuid),
            "title": rb.title_of(kind, row), "subtitle": sub,
            "state": "Switched off" if kind.flag == "active" else ("Archived" if kind.flag != "deleted_at" else "Deleted"),
            "deleted_at": at.isoformat() if at else None, "deleted_by": info.get("by"),
            "purgeable": purge_reason is None, "keep_reason": purge_reason,
            "purge_after": after.isoformat() if after and purge_reason is None else None,
            "can_purge_now": purge_reason is None and after is not None and after <= now()}


def list_items(db: Session, company_id: int, *, kind: str | None = None, group: str | None = None,
               who: str | None = None, start: date | None = None, end: date | None = None, q: str | None = None,
               purge: str | None = None) -> list[dict]:
    ensure_registry()
    retention = settings(db, company_id).retention_days
    items = []
    for k in rb.kinds():
        if kind and k.key not in kind.split(","):
            continue
        if group and k.group != group:
            continue
        try:
            with db.begin_nested():
                rows = _query(db, company_id, k).limit(PER_KIND_LIMIT).all()
        except Exception:  # a module's table missing or changed must not break the bin
            continue
        if not rows:
            continue
        infos = _deletion_info(db, company_id, k, rows)
        for r in rows:
            if infos[r.id].get("created_off"):
                continue
            items.append(item_out(k, r, infos[r.id], retention))
    if who:
        items = [i for i in items if (i["deleted_by"] or "") == who]
    if start:
        items = [i for i in items if i["deleted_at"] and i["deleted_at"][:10] >= start.isoformat()]
    if end:
        items = [i for i in items if i["deleted_at"] and i["deleted_at"][:10] <= end.isoformat()]
    if q:
        ql = q.lower()
        items = [i for i in items if ql in (i["title"] or "").lower() or ql in (i["subtitle"] or "").lower()
                 or ql in i["kind_label"].lower()]
    if purge == "due":
        items = [i for i in items if i["can_purge_now"]]
    elif purge == "kept":
        items = [i for i in items if not i["purgeable"]]
    items.sort(key=lambda i: i["deleted_at"] or "", reverse=True)
    return items


def counts(items: list[dict]) -> dict:
    by_kind: dict = {}
    for i in items:
        e = by_kind.setdefault(i["kind"], {"kind": i["kind"], "label": i["kind_label"], "group": i["group"], "count": 0})
        e["count"] += 1
    who = sorted({i["deleted_by"] for i in items if i["deleted_by"]})
    return {"kinds": sorted(by_kind.values(), key=lambda x: (x["group"], x["label"])), "people": who,
            "due": sum(1 for i in items if i["can_purge_now"])}


def _kind(key: str) -> rb.BinKind:
    ensure_registry()
    k = rb.get(key)
    if not k:
        raise NotFound("Kind of record")
    return k


def get_row(db: Session, company_id: int, kind: rb.BinKind, uuid):
    row = _query(db, company_id, kind).filter(kind.model.uuid == uuid).first()
    if row is None:
        raise NotFound("Record in the recycle bin")
    return row


def detail(db: Session, company_id: int, kind_key: str, uuid) -> dict:
    kind = _kind(kind_key)
    row = get_row(db, company_id, kind, uuid)
    retention = settings(db, company_id).retention_days
    out = item_out(kind, row, _deletion_info(db, company_id, kind, [row])[row.id], retention)
    fields = []
    hidden = set(kind.hidden_fields) | {"id", "uuid", "company_id"}
    for c in kind.model.__table__.columns:
        if c.name in hidden:
            continue
        v = getattr(row, c.key, None)
        if v is None or v == "" or v == [] or v == {}:
            continue
        fields.append({"field": c.name, "label": c.name.replace("_", " ").capitalize(), "value": audit._mask_value(c.name, v)})
    out["fields"] = fields
    out["problems"] = restore_problems(db, company_id, kind, row)
    out["references"] = references(db, kind, row) if out["purgeable"] else []
    history = db.execute(text(
        "SELECT action, actor_name, created_at FROM audit_logs WHERE company_id = :cid AND entity_type = :t "
        "AND entity_id = :id ORDER BY id DESC LIMIT 8"), {"cid": company_id, "t": kind.table, "id": row.id}).fetchall()
    out["history"] = [{"action": a, "by": b, "at": c.isoformat() if c else None} for a, b, c in history]
    return out


# ── Restore ─────────────────────────────────────────────────────────

def restore_problems(db: Session, company_id: int, kind: rb.BinKind, row) -> list[str]:
    """Why the record cannot come back yet: something it points to is still in the bin,
    or a live record already uses its code."""
    problems: list[str] = []
    tables = rb.by_table()
    for col in kind.model.__table__.columns:
        if col.name == "company_id" or not col.foreign_keys:
            continue
        value = getattr(row, col.key, None)
        if value is None:
            continue
        target_table = next(iter(col.foreign_keys)).column.table.name
        tk = tables.get(target_table)
        if tk is None:
            continue
        parent = db.query(tk.model).filter(tk.model.id == value).first()
        if parent is not None and rb.is_in_bin(tk, parent):
            what = "its parent" if target_table == kind.table else f"its {tk.label.lower()}"
            problems.append(f"Restore {what} “{rb.title_of(tk, parent)}” first.")
    m = kind.model
    for f in kind.unique_fields:
        if not hasattr(m, f):
            continue
        v = getattr(row, f, None)
        if v in (None, ""):
            continue
        live = db.query(m).filter(m.company_id == company_id, getattr(m, f) == v, m.id != row.id,
                                  ~rb.in_bin_clause(kind)).first()
        if live is not None:
            problems.append(f"Another {kind.label.lower()} already uses “{v}”. Rename or remove that one first.")
    if kind.restore_check:
        problems.extend(kind.restore_check(db, row) or [])
    return problems


def restore(db: Session, company_id: int, kind_key: str, uuid) -> dict:
    kind = _kind(kind_key)
    row = get_row(db, company_id, kind, uuid)
    problems = restore_problems(db, company_id, kind, row)
    if problems:
        raise BadRequest(" ".join(problems))
    title = rb.title_of(kind, row)
    rb.clear_flag(kind, row)
    if kind.on_restore:
        kind.on_restore(db, row)
    db.flush()
    audit.event(db, "recycle_bin.restored", kind.table, row.id, {"kind": kind.key, "kind_label": kind.label},
                company_id=company_id, entity_uuid=str(row.uuid), entity_label=title)
    return {"kind": kind.key, "uuid": str(row.uuid), "title": title}


# ── Purge ───────────────────────────────────────────────────────────

def references(db: Session, kind: rb.BinKind, row) -> list[dict]:
    """Other records that still point to this one (each blocks a purge)."""
    from core.db import Base
    out = []
    for t in Base.metadata.tables.values():
        for fk in t.foreign_keys:
            if fk.column.table.name != kind.table or fk.column.name != "id":
                continue
            col = fk.parent
            if t.name == kind.table and col.name == "id":
                continue
            try:
                with db.begin_nested():
                    n = db.execute(select(col).where(col == row.id).limit(51)).fetchall()
            except Exception:
                continue
            if n:
                tk = rb.by_table().get(t.name)
                label = tk.label if tk else t.name.replace("_", " ")
                out.append({"table": t.name, "label": label, "count": len(n) if len(n) <= 50 else "50+"})
    return out


def purge_check(db: Session, company_id: int, kind: rb.BinKind, row, *, ignore_retention: bool = False) -> None:
    if not kind.purgeable:
        raise BadRequest(f"{kind.label} records cannot be removed for good: {(kind.keep_reason or 'kept for good').lower()}.")
    if _blocked_by_system(row):
        raise BadRequest("This record is built into the product. It can be restored but not removed.")
    s = settings(db, company_id)
    info = _deletion_info(db, company_id, kind, [row])[row.id]
    after = _purge_after(info.get("at"), s.retention_days)
    if not ignore_retention and (after is None or after > now()):
        raise BadRequest(f"Deleted records are kept for {s.retention_days} days. This one can be removed from "
                         f"{after.date().strftime('%d %b %Y') if after else 'later'}.")
    refs = references(db, kind, row)
    if refs:
        what = ", ".join(f"{r['count']} {r['label'].lower()}" for r in refs[:4])
        raise BadRequest(f"Other records still use this {kind.label.lower()} ({what}). Restore it or leave it in the bin.")


def purge(db: Session, company_id: int, kind_key: str, uuid, *, system: bool = False) -> dict:
    kind = _kind(kind_key)
    row = get_row(db, company_id, kind, uuid)
    purge_check(db, company_id, kind, row)
    title = rb.title_of(kind, row)
    snapshot = {k: (v.get("new") if isinstance(v, dict) and "new" in v else v) for k, v in audit._snapshot(row).items()}
    for f in kind.hidden_fields:
        snapshot.pop(f, None)
    rid, ruuid = row.id, str(row.uuid)
    db.expunge(row)
    try:
        with db.begin_nested():
            # A plain DELETE: the database refuses it when anything still points here
            # (no ORM cascades that could quietly unlink other records).
            db.execute(delete(kind.model.__table__).where(kind.model.__table__.c.id == rid))
    except IntegrityError:
        raise BadRequest(f"Other records still use this {kind.label.lower()}. Restore it or leave it in the bin.")
    audit.event(db, "recycle_bin.purged", kind.table, rid,
                {"kind": kind.key, "kind_label": kind.label, "record": snapshot, "automatic": system},
                company_id=company_id, entity_uuid=ruuid, entity_label=title)
    return {"kind": kind.key, "uuid": ruuid, "title": title}


def auto_purge(db: Session) -> int:
    """Periodic job (system session): for companies that switched it on, remove records
    whose retention period has passed and that nothing uses."""
    ensure_registry()
    n = 0
    for s in db.query(RecycleBinSetting).filter(RecycleBinSetting.auto_purge.is_(True)).all():
        for item in list_items(db, s.company_id, purge="due"):
            try:
                with db.begin_nested():
                    purge(db, s.company_id, item["kind"], item["uuid"], system=True)
                n += 1
            except Exception:
                continue
    return n
