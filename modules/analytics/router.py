"""Dashboards API: list, build, arrange and share dashboards, and the data for each widget."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.db import get_db
from core.errors import NotFound
from modules.analytics import service
from modules.reports.registry import Principal, get_principal

router = APIRouter(prefix="/dashboards", tags=["Dashboards"])


class DashboardBody(BaseModel):
    name: str | None = None
    description: str | None = None
    icon: str | None = None
    widgets: list[dict] | None = None
    shared_roles: list[str] | None = None
    shared_employee_ids: list[int] | None = None


class WidgetBody(BaseModel):
    widget: dict


@router.get("/meta")
def meta(p: Principal = Depends(get_principal)):
    return {"types": [{"value": k, "label": v} for k, v in service.TYPES.items()],
            "can_manage": service.can_manage(p), "can_build": service.can_manage(p) or p.can(service.VIEW)}


@router.get("/")
def list_dashboards(db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    return service.list_dashboards(db, p)


@router.post("/", status_code=201)
def create(body: DashboardBody, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    d = service.save(db, p, body.model_dump(exclude_unset=True))
    db.commit()
    return service.out(p, d, full=True)


@router.post("/widget-preview")
def widget_preview(body: WidgetBody, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    w = service.clean_widget(db, p, body.widget)
    res = service.widget_data(db, p, w)
    db.commit()
    return res


@router.get("/{uuid}")
def get_dashboard(uuid: UUID, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    return service.out(p, service.get(db, p, uuid), full=True)


@router.put("/{uuid}")
def update(uuid: UUID, body: DashboardBody, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    d = service.save(db, p, body.model_dump(exclude_unset=True), service.get(db, p, uuid))
    db.commit()
    return service.out(p, d, full=True)


@router.post("/{uuid}/clone", status_code=201)
def clone(uuid: UUID, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    d = service.clone(db, p, service.get(db, p, uuid))
    db.commit()
    return service.out(p, d, full=True)


@router.delete("/{uuid}", status_code=204)
def remove(uuid: UUID, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    service.archive(db, p, service.get(db, p, uuid))
    db.commit()


@router.get("/{uuid}/widgets/{wid}/data")
def widget(uuid: UUID, wid: str, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    d = service.get(db, p, uuid)
    w = next((x for x in d.widgets or [] if x.get("id") == wid), None)
    if w is None:
        raise NotFound("Widget")
    res = service.widget_data(db, p, w)
    db.commit()
    return res
