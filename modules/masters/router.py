"""Masters API: the lists behind every dropdown. Everyone signed in can read them (forms
need them); changing them needs settings.masters.manage."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core import masters
from core.db import get_db
from core.pagination import PageParams, paginate
from core.security import get_current_user, require_permission
from modules.masters.schemas import (MasterTypeCreate, MasterTypeOut, MasterTypeUpdate, MasterValueCreate,
                                     MasterValueOut, MasterValueUpdate, ReorderRequest)

router = APIRouter(prefix="/masters", tags=["Masters"])
MANAGE = require_permission("settings.masters.manage")


def _type_out(t, count=0) -> MasterTypeOut:
    return MasterTypeOut(uuid=t.uuid, key=t.key, name=t.name, description=t.description, hierarchical=t.hierarchical,
                         is_system=t.is_system, active=t.active, sort_order=t.sort_order, value_count=count)


def _value_out(v, children: dict | None = None) -> MasterValueOut:
    return MasterValueOut(id=v.id, uuid=v.uuid, code=v.code, label=v.label, parent_id=v.parent_id,
                          parent_label=v.parent.label if v.parent_id and v.parent else None,
                          attributes=v.attributes or {}, is_system=v.is_system, active=v.active,
                          sort_order=v.sort_order, child_count=(children or {}).get(v.id, 0), updated_at=v.updated_at)


@router.get("/types", response_model=list[MasterTypeOut])
def list_types(q: str | None = None, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    return [_type_out(t, n) for t, n in masters.list_types(db, current_user["company_id"], q)]


@router.post("/types", response_model=MasterTypeOut, status_code=201)
def create_type(req: MasterTypeCreate, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    row = masters.create_type(db, current_user["company_id"], req.name, req.key, req.description, req.hierarchical)
    db.commit()
    return _type_out(row)


@router.get("/types/{key}", response_model=MasterTypeOut)
def get_type(key: str, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    return _type_out(masters.get_type(db, current_user["company_id"], key))


@router.put("/types/{key}", response_model=MasterTypeOut)
def update_type(key: str, req: MasterTypeUpdate, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    row = masters.update_type(masters.get_type(db, current_user["company_id"], key), req.model_dump(exclude_unset=True))
    db.commit()
    return _type_out(row)


@router.get("/types/{key}/values")
def list_values(
    key: str,
    q: str | None = None,
    parent_id: int | None = None,
    top_level: bool = Query(False, description="Only values without a parent"),
    active: bool | None = None,
    pages: PageParams = Depends(),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cid = current_user["company_id"]
    mtype = masters.get_type(db, cid, key)
    query = masters.values_query(db, cid, mtype, q, parent_id, top_level, active)
    page = paginate(query, pages)
    counts = masters.child_counts(db, [v.id for v in page["items"]])
    page["items"] = [_value_out(v, counts) for v in page["items"]]
    return page


@router.post("/types/{key}/values", response_model=MasterValueOut, status_code=201)
def create_value(key: str, req: MasterValueCreate, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    row = masters.create_value(db, cid, masters.get_type(db, cid, key), req.model_dump())
    db.commit()
    return _value_out(row)


@router.post("/types/{key}/values/reorder")
def reorder_values(key: str, req: ReorderRequest, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    masters.reorder_values(db, cid, masters.get_type(db, cid, key), req.ids)
    db.commit()
    return {"detail": "Order saved"}


@router.put("/values/{uuid}", response_model=MasterValueOut)
def update_value(uuid: str, req: MasterValueUpdate, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    row = masters.update_value(db, cid, masters.get_value(db, cid, uuid), req.model_dump(exclude_unset=True))
    db.commit()
    return _value_out(row)


@router.get("/options/{key}")
def options(
    key: str,
    q: str | None = None,
    parent_id: int | None = None,
    ids: str | None = Query(None, description="comma separated ids to resolve (e.g. the saved value)"),
    include_inactive: bool = False,
    limit: int = Query(30, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Options for a MasterSelect: [{value, label, code, parent_id, parent_label}]. Searches
    labels and codes; sub-values show their parent."""
    cid = current_user["company_id"]
    mtype = masters.get_type(db, cid, key)
    query = masters.values_query(db, cid, mtype, q, parent_id, False, None if include_inactive else True)
    if ids:
        wanted = [int(x) for x in ids.split(",") if x.strip().isdigit()][:100]
        query = masters.values_query(db, cid, mtype).filter(masters.MasterValue.id.in_(wanted))
    rows = query.limit(limit).all()
    return {
        "type": {"key": mtype.key, "name": mtype.name, "hierarchical": mtype.hierarchical},
        "items": [{"value": v.id, "label": v.label, "code": v.code, "parent_id": v.parent_id,
                   "parent_label": v.parent.label if v.parent_id and v.parent else None, "active": v.active}
                  for v in rows],
    }
