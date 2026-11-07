"""Compliance dashboard API."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.db import get_db
from core.errors import Forbidden
from modules.compliance import service
from modules.reports.registry import Principal, get_principal

router = APIRouter(prefix="/compliance", tags=["Compliance"])


class MarkBody(BaseModel):
    key: str
    status: str = "done"
    done_on: str | None = None
    reference: str | None = None
    notes: str | None = None


def _require(p: Principal) -> None:
    if not (p.can(service.VIEW) or p.can("payroll.view")):
        raise Forbidden("Only HR and payroll can see the compliance dashboard.")


@router.get("/dashboard")
def dashboard(entity_id: int | None = None, state: str | None = None, db: Session = Depends(get_db),
              p: Principal = Depends(get_principal)):
    _require(p)
    res = service.dashboard(db, p, entity_id=entity_id, state=state)
    db.commit()
    return res


@router.post("/records")
def mark(body: MarkBody, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    _require(p)
    service.mark(db, p, body.key, body.model_dump())
    db.commit()
    return {"ok": True}
