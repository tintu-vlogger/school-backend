"""Custom fields API: the layout (sections and fields) for a record type, and its setup."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core import custom_fields as cf
from core.db import get_db
from core.errors import NotFound
from core.security import get_current_user, require_permission
from modules.custom_fields.schemas import FieldIn, FieldOut, FieldUpdate, Reorder, SectionIn, SectionOut, SectionUpdate

router = APIRouter(prefix="/custom-fields", tags=["Custom fields"])
MANAGE = require_permission("settings.custom_fields.manage")


def _field_out(d) -> FieldOut:
    return FieldOut.model_validate(d, from_attributes=True)


def _section_out(s) -> SectionOut:
    return SectionOut.model_validate(s, from_attributes=True)


@router.get("/meta")
def meta(_: dict = Depends(get_current_user)):
    return {"entities": [{"value": k, "label": v} for k, v in cf.ENTITIES.items()],
            "field_types": [{"value": k, "label": v} for k, v in cf.FIELD_TYPES.items()]}


@router.get("/{entity}")
def layout(entity: str, include_inactive: bool = Query(False), current_user: dict = Depends(get_current_user),
           db: Session = Depends(get_db)):
    """Sections with their fields, in order. Forms use this to render custom fields."""
    cf.check_entity(entity)
    cid = current_user["company_id"]
    secs = cf.sections(db, cid, entity, active_only=not include_inactive)
    defs = cf.definitions(db, cid, entity, active_only=not include_inactive)
    by_section: dict = {}
    for d in defs:
        by_section.setdefault(d.section_id, []).append(_field_out(d))
    out = [{**_section_out(s).model_dump(mode="json"), "fields": by_section.get(s.id, [])} for s in secs]
    loose = by_section.get(None, [])
    orphan = [f for sid, fs in by_section.items() if sid is not None and sid not in {s.id for s in secs} for f in fs]
    return {"entity": entity, "sections": out, "unsectioned": loose + orphan}


@router.post("/{entity}/sections", response_model=SectionOut, status_code=201)
def create_section(entity: str, req: SectionIn, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cf.check_entity(entity)
    cid = current_user["company_id"]
    order = len(cf.sections(db, cid, entity))
    row = cf.CustomFieldSection(company_id=cid, entity=entity, name=req.name.strip(), description=req.description,
                                sort_order=order)
    db.add(row)
    db.commit()
    return _section_out(row)


def _section(db, cid, uuid):
    row = db.query(cf.CustomFieldSection).filter(cf.CustomFieldSection.company_id == cid,
                                                 cf.CustomFieldSection.uuid == uuid).first()
    if not row:
        raise NotFound("Section")
    return row


@router.put("/sections/{uuid}", response_model=SectionOut)
def update_section(uuid: str, req: SectionUpdate, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    row = _section(db, current_user["company_id"], uuid)
    for k, v in req.model_dump(exclude_unset=True).items():
        if v is not None:
            setattr(row, k, v.strip() if isinstance(v, str) else v)
    db.commit()
    return _section_out(row)


@router.post("/{entity}/sections/reorder")
def reorder_sections(entity: str, req: Reorder, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    rows = {s.id: s for s in cf.sections(db, current_user["company_id"], entity)}
    for i, sid in enumerate(req.ids):
        if sid in rows:
            rows[sid].sort_order = i
    db.commit()
    return {"detail": "Order saved"}


@router.post("/{entity}/fields", response_model=FieldOut, status_code=201)
def create_field(entity: str, req: FieldIn, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    data = req.model_dump(exclude_unset=False)
    data["options"] = data.get("options") or []
    data["validation"] = data.get("validation") or {}
    row = cf.create_definition(db, current_user["company_id"], entity, data)
    db.commit()
    return _field_out(row)


def _field(db, cid, uuid):
    row = db.query(cf.CustomFieldDef).filter(cf.CustomFieldDef.company_id == cid, cf.CustomFieldDef.uuid == uuid).first()
    if not row:
        raise NotFound("Field")
    return row


@router.put("/fields/{uuid}", response_model=FieldOut)
def update_field(uuid: str, req: FieldUpdate, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    row = cf.update_definition(db, cid, _field(db, cid, uuid), req.model_dump(exclude_unset=True))
    db.commit()
    return _field_out(row)


@router.post("/{entity}/fields/reorder")
def reorder_fields(entity: str, req: Reorder, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    rows = {d.id: d for d in cf.definitions(db, current_user["company_id"], entity, active_only=False)}
    for i, fid in enumerate(req.ids):
        if fid in rows:
            rows[fid].sort_order = i
    db.commit()
    return {"detail": "Order saved"}
