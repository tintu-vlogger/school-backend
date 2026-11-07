"""Recycle bin API under /api/v1/recycle-bin.

GET  /recycle-bin/items?kind=&group=&who=&start=&end=&q=&purge=due|kept&page=&size=
GET  /recycle-bin/items/{kind}/{uuid}            details, restore problems, what still uses it
POST /recycle-bin/items/{kind}/{uuid}/restore
POST /recycle-bin/items/{kind}/{uuid}/purge      (after the retention period, where allowed)
POST /recycle-bin/restore                        several at once: {items: [{kind, uuid}]}
GET  /recycle-bin/kinds                          every kind of record the bin knows
GET/PUT /recycle-bin/settings                    retention days, automatic purge
"""
from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core import recycle_bin as rb
from core.db import get_db
from core.errors import BadRequest
from core.security import _get_role_permissions, require_permission
from modules.recycle_bin import service

router = APIRouter(prefix="/recycle-bin", tags=["Recycle bin"])
VIEW = require_permission("settings.recycle_bin.manage")
PURGE = require_permission("settings.recycle_bin.manage", "settings.recycle_bin.purge")


@router.get("/kinds")
def kinds(user: dict = Depends(VIEW)):
    service.ensure_registry()
    return [{"key": k.key, "label": k.label, "group": k.group, "purgeable": k.purgeable, "keep_reason": k.keep_reason}
            for k in rb.kinds()]


@router.get("/items")
def items(kind: str | None = None, group: str | None = None, who: str | None = None, start: date | None = None,
          end: date | None = None, q: str | None = None, purge: str | None = None, page: int = 1,
          size: int = Query(25, le=200), user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    cid = user["company_id"]
    all_rows = service.list_items(db, cid, kind=kind, group=group, who=who, start=start, end=end, q=q, purge=purge)
    facets = service.counts(service.list_items(db, cid) if (kind or group or who or start or end or q or purge) else all_rows)
    s = service.settings(db, cid)
    db.commit()
    perms = _get_role_permissions(cid, user["role"], db)
    return {"items": all_rows[(page - 1) * size: page * size], "total": len(all_rows), "page": page, "size": size,
            "facets": facets, "settings": service.settings_out(s), "can_purge": "settings.recycle_bin.purge" in perms}


@router.get("/items/{kind}/{uuid}")
def item(kind: str, uuid: UUID, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    out = service.detail(db, user["company_id"], kind, uuid)
    db.commit()
    return out


@router.post("/items/{kind}/{uuid}/restore")
def restore(kind: str, uuid: UUID, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    out = service.restore(db, user["company_id"], kind, uuid)
    db.commit()
    return out


@router.post("/items/{kind}/{uuid}/purge")
def purge(kind: str, uuid: UUID, user: dict = Depends(PURGE), db: Session = Depends(get_db)):
    out = service.purge(db, user["company_id"], kind, uuid)
    db.commit()
    return out


class ItemRef(BaseModel):
    kind: str
    uuid: UUID


class ManyIn(BaseModel):
    items: list[ItemRef]


@router.post("/restore")
def restore_many(body: ManyIn, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    """Restore several records; each one that cannot come back is reported, the rest are restored."""
    if not body.items or len(body.items) > 200:
        raise BadRequest("Choose between 1 and 200 records.")
    done, failed = [], []
    for it in body.items:
        try:
            with db.begin_nested():
                done.append(service.restore(db, user["company_id"], it.kind, it.uuid))
        except Exception as exc:  # report and carry on
            failed.append({"kind": it.kind, "uuid": str(it.uuid), "detail": getattr(exc, "detail", None) or str(exc)})
    db.commit()
    return {"restored": done, "failed": failed}


class SettingsIn(BaseModel):
    retention_days: int | None = None
    auto_purge: bool | None = None


@router.get("/settings")
def get_settings(user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    s = service.settings(db, user["company_id"])
    db.commit()
    return service.settings_out(s)


@router.put("/settings")
def put_settings(body: SettingsIn, user: dict = Depends(PURGE), db: Session = Depends(get_db)):
    s = service.save_settings(db, user["company_id"], body.model_dump(exclude_unset=True))
    db.commit()
    return service.settings_out(s)
