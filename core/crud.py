"""Generic list / create / read / update / archive endpoints for simple company-owned tables.

Most institution set-up lists (sessions, rooms, houses, fee heads, book categories, routes …)
are plain records: a few columns, unique within the company, never hard-deleted. Instead of
hand-writing five endpoints for each, a module calls `crud_router(...)`:

    router.include_router(crud_router(
        model=Room, prefix="/rooms", tag="Academics",
        create_schema=RoomIn, update_schema=RoomPatch, out=lambda r: {...},
        view_perm="academics.view", manage_perm="academics.manage",
        search_fields=("name", "code"), order_by=("name",),
    ))

Rules it follows (platform.md): records are addressed by uuid, rows carry `company_id`
(filled from the signed-in person and also filtered on, on top of row-level security),
lists accept `q`, `active` and paging and return {items, total, page, size}, and DELETE
switches a record off (`active = false`) when the table has that column. Hard delete only
happens for tables without an `active` column.
"""
from typing import Any, Callable, Iterable
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.db import get_db
from core.pagination import PageParams, paginate
from core.security import require_permission


def get_or_404(db: Session, model, company_id: int, uuid: UUID, label: str = "Record"):
    row = db.query(model).filter(model.company_id == company_id, model.uuid == uuid).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"{label} not found")
    return row


def by_id_or_400(db: Session, model, company_id: int, id_: int | None, label: str = "Record"):
    """Check a submitted foreign id belongs to this company (returns None for None)."""
    if id_ is None:
        return None
    row = db.query(model).filter(model.company_id == company_id, model.id == id_).first()
    if row is None:
        raise HTTPException(status_code=400, detail=f"{label} was not found.")
    return row


def default_out(row) -> dict:
    out = {}
    for col in row.__table__.columns:
        if col.name in ("company_id", "password"):
            continue
        val = getattr(row, col.name)
        out[col.name] = str(val) if isinstance(val, UUID) else val
    return out


def crud_router(
    *,
    model,
    prefix: str,
    tag: str,
    create_schema: type[BaseModel],
    update_schema: type[BaseModel],
    view_perm: str,
    manage_perm: str,
    out: Callable[[Any], dict] = default_out,
    search_fields: Iterable[str] = ("name",),
    order_by: Iterable[str] = ("name",),
    label: str = "Record",
    filters: Iterable[str] = (),
    validate: Callable[[Session, int, dict, Any], None] | None = None,
    after_change: Callable[[Session, int, Any], None] | None = None,
) -> APIRouter:
    """Build the five endpoints. `filters` names integer/string columns accepted as query
    parameters (e.g. "program_id"); `validate(db, company_id, data, row_or_None)` may raise
    HTTPException to refuse bad links."""
    r = APIRouter(prefix=prefix, tags=[tag])
    has_active = "active" in model.__table__.columns
    filter_cols = list(filters)

    @r.get("")
    def list_rows(
        request: Request,
        q: str | None = None,
        active: bool | None = Query(None),
        all: bool = Query(False),
        params: PageParams = Depends(),
        db: Session = Depends(get_db),
        user: dict = Depends(require_permission(view_perm)),
    ):
        query = db.query(model).filter(model.company_id == user["company_id"])
        if has_active and active is not None:
            query = query.filter(model.active.is_(active))
        if q:
            like = f"%{q.strip()}%"
            query = query.filter(or_(*[getattr(model, f).ilike(like) for f in search_fields]))
        for name in filter_cols:
            raw = request.query_params.get(name)
            if raw not in (None, ""):
                col = getattr(model, name)
                try:
                    value: Any = int(raw) if col.type.python_type is int else raw
                except (NotImplementedError, ValueError):
                    value = raw
                query = query.filter(col == value)
        query = query.order_by(*[getattr(model, c) for c in order_by])
        if all:
            rows = query.limit(2000).all()
            return {"items": [out(x) for x in rows], "total": len(rows), "page": 1, "size": len(rows)}
        return paginate(query, params, out)

    @r.post("", status_code=201)
    def create_row(payload: create_schema, db: Session = Depends(get_db),  # type: ignore[valid-type]
                   user: dict = Depends(require_permission(manage_perm))):
        data = payload.model_dump()
        if validate:
            validate(db, user["company_id"], data, None)
        row = model(company_id=user["company_id"], **data)
        db.add(row)
        _commit(db, label)
        db.refresh(row)
        if after_change:
            after_change(db, user["company_id"], row)
            db.commit()
        return out(row)

    @r.get("/{uuid}")
    def get_row(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(view_perm))):
        return out(get_or_404(db, model, user["company_id"], uuid, label))

    @r.put("/{uuid}")
    def update_row(uuid: UUID, payload: update_schema, db: Session = Depends(get_db),  # type: ignore[valid-type]
                   user: dict = Depends(require_permission(manage_perm))):
        row = get_or_404(db, model, user["company_id"], uuid, label)
        data = payload.model_dump(exclude_unset=True)
        if validate:
            validate(db, user["company_id"], data, row)
        for k, v in data.items():
            setattr(row, k, v)
        _commit(db, label)
        db.refresh(row)
        if after_change:
            after_change(db, user["company_id"], row)
            db.commit()
        return out(row)

    @r.delete("/{uuid}")
    def archive_row(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(manage_perm))):
        row = get_or_404(db, model, user["company_id"], uuid, label)
        if has_active:
            row.active = False
        else:
            db.delete(row)
        _commit(db, label)
        return {"detail": "Switched off" if has_active else "Removed"}

    return r


def _commit(db: Session, label: str) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        msg = str(exc.orig).lower() if exc.orig else ""
        if "unique" in msg or "duplicate" in msg:
            raise HTTPException(status_code=409, detail=f"A {label.lower()} with this name or code already exists.")
        if "foreign key" in msg:
            raise HTTPException(status_code=400, detail=f"This {label.lower()} is linked to records that are missing or still in use.")
        raise
