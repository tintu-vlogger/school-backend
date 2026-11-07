"""Number series settings: formats and counters for every numbered record."""
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core import numbering
from core.db import get_db
from core.errors import BadRequest
from core.security import get_current_user, require_permission
from core.timeutil import today_for_company_id

router = APIRouter(prefix="/number-series", tags=["Number series"])
MANAGE = require_permission("settings.numbering.manage")


class SeriesIn(BaseModel):
    key: str = Field(..., min_length=2, max_length=60)
    name: str = Field(..., min_length=1, max_length=120)
    description: Optional[str] = Field(None, max_length=500)
    prefix: str = Field("", max_length=30)
    pattern: str = Field(..., max_length=120)
    reset: str = "never"
    start_number: int = Field(1, ge=0, le=1_000_000_000)
    allow_manual: bool = True
    active: bool = True
    counter_scope: str = "shared"


class SeriesUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    description: Optional[str] = Field(None, max_length=500)
    prefix: Optional[str] = Field(None, max_length=30)
    pattern: Optional[str] = Field(None, max_length=120)
    reset: Optional[str] = None
    start_number: Optional[int] = Field(None, ge=0, le=1_000_000_000)
    allow_manual: Optional[bool] = None
    active: Optional[bool] = None
    counter_scope: Optional[str] = None


class PreviewIn(BaseModel):
    prefix: str = Field("", max_length=30)
    pattern: str = Field(..., max_length=120)
    reset: str = "never"
    start_number: int = Field(1, ge=0)
    counter_scope: str = "shared"
    legal_entity_id: Optional[int] = None


def _check(data: dict, row=None) -> None:
    if "pattern" in data and data["pattern"] is not None:
        numbering.validate_pattern(data["pattern"])
    if data.get("reset") is not None and data["reset"] not in numbering.RESETS:
        raise BadRequest("Pick when the counter starts again.")
    if data.get("counter_scope") is not None and data["counter_scope"] not in numbering.COUNTER_SCOPES:
        raise BadRequest("Pick whether legal entities share one counter or each has its own.")
    scope = data.get("counter_scope") or (row.counter_scope if row is not None else "shared")
    pattern = data.get("pattern") or (row.pattern if row is not None else "")
    if scope == "per_entity" and "{ENTITY}" not in pattern:
        raise BadRequest("With a separate counter per legal entity, put {ENTITY} in the format so that "
                         "numbers stay unique.")


def _entities(db, cid) -> list:
    try:
        from modules.organization.models import LegalEntity
    except Exception:  # pragma: no cover
        return []
    return (db.query(LegalEntity).filter(LegalEntity.company_id == cid, LegalEntity.active.is_(True))
            .order_by(LegalEntity.is_primary.desc(), LegalEntity.name).all())


def _out(db, s, today, entities: list | None = None):
    uses_entity = "{ENTITY}" in (s.pattern or "") or s.counter_scope == "per_entity"
    by_entity = [{"legal_entity_id": e.id, "name": e.name, "code": numbering.entity_code(e),
                  "next": numbering.preview(db, s, today, e.id)} for e in (entities or [])] if uses_entity else []
    return {
        "uuid": str(s.uuid), "key": s.key, "name": s.name, "description": s.description, "prefix": s.prefix,
        "pattern": s.pattern, "reset": s.reset, "start_number": s.start_number, "allow_manual": s.allow_manual,
        "is_system": s.is_system, "active": s.active, "counter_scope": s.counter_scope,
        "next": numbering.preview(db, s, today), "next_by_entity": by_entity,
    }


@router.get("/")
def list_series(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    today = today_for_company_id(db, cid)
    rows = db.query(numbering.NumberSeries).filter(numbering.NumberSeries.company_id == cid) \
        .order_by(numbering.NumberSeries.is_system.desc(), numbering.NumberSeries.name).all()
    entities = _entities(db, cid)
    return {"items": [_out(db, s, today, entities) for s in rows],
            "resets": [{"value": k, "label": v} for k, v in numbering.RESETS.items()],
            "counter_scopes": [{"value": k, "label": v} for k, v in numbering.COUNTER_SCOPES.items()],
            "legal_entities": [{"id": e.id, "name": e.name, "code": numbering.entity_code(e)} for e in entities]}


@router.post("/", status_code=201)
def create_series(req: SeriesIn, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    data = req.model_dump()
    _check(data)
    key = data["key"].strip().lower().replace(" ", "_")
    if numbering.get_series(db, cid, key):
        raise BadRequest("A number series for this already exists.")
    row = numbering.NumberSeries(company_id=cid, **{**data, "key": key})
    db.add(row)
    db.commit()
    return _out(db, row, today_for_company_id(db, cid), _entities(db, cid))


@router.put("/{uuid}")
def update_series(uuid: str, req: SeriesUpdate, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    row = numbering.require(db, cid, uuid)
    data = req.model_dump(exclude_unset=True)
    _check(data, row)
    for k, v in data.items():
        if v is not None:
            setattr(row, k, v)
    db.commit()
    return _out(db, row, today_for_company_id(db, cid), _entities(db, cid))


@router.post("/preview")
def preview(req: PreviewIn, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """What the next few numbers would look like with this format (nothing is used up)."""
    _check(req.model_dump())
    cid = current_user["company_id"]
    today = today_for_company_id(db, cid)
    entities = _entities(db, cid)
    chosen = next((e for e in entities if e.id == req.legal_entity_id), None) or (entities[0] if entities else None)
    code = numbering.entity_code(chosen)
    examples = [numbering.render(req.pattern, req.prefix, req.start_number + i, today, code) for i in range(3)]
    by_entity = []
    if "{ENTITY}" in req.pattern:
        by_entity = [{"name": e.name, "example": numbering.render(req.pattern, req.prefix, req.start_number, today,
                                                                   numbering.entity_code(e))} for e in entities[:6]]
    return {"examples": examples, "by_entity": by_entity}
