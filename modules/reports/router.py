"""Reports API: data sources, saved and standard reports, previews, exports (recorded) and
email schedules."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, Field as PField
from sqlalchemy.orm import Session

from core.db import get_db
from core.errors import BadRequest, Forbidden, NotFound
from core.pagination import PageParams, paginate
from modules.reports import engine, registry, render, service
from modules.reports.models import Report, ReportSchedule
from modules.reports.registry import Principal, get_principal

router = APIRouter(prefix="/reports", tags=["Reports"])


class RunBody(BaseModel):
    filters: list[dict] | None = None
    replace_filters: list[dict] | None = None
    params: dict | None = None
    sort: list[dict] | None = None
    page: int = 1
    size: int = PField(100, ge=1, le=1000)


class PreviewBody(BaseModel):
    source: str
    config: dict = {}
    scope: str | None = None
    page: int = 1
    size: int = PField(100, ge=1, le=1000)


class ReportBody(BaseModel):
    name: str | None = None
    description: str | None = None
    source: str | None = None
    config: dict | None = None
    chart: dict | None = None
    scope: str | None = None
    shared_roles: list[str] | None = None
    shared_employee_ids: list[int] | None = None


class ExportBody(BaseModel):
    format: str = "xlsx"
    protect: bool = False
    filters: list[dict] | None = None
    replace_filters: list[dict] | None = None
    params: dict | None = None
    sort: list[dict] | None = None
    # ad hoc export (no saved report)
    source: str | None = None
    config: dict | None = None
    scope: str | None = None
    title: str | None = None


class ScheduleBody(BaseModel):
    report_uuid: str | None = None
    name: str | None = None
    frequency: str | None = None
    weekday: int | None = None
    day_of_month: int | None = None
    hour: int | None = None
    format: str | None = None
    recipient_employee_ids: list[int] | None = None
    recipient_emails: list[str] | None = None
    active: bool | None = None


def _data(body: BaseModel) -> dict[str, Any]:
    return body.model_dump(exclude_unset=True)


@router.get("/meta")
def meta(p: Principal = Depends(get_principal)):
    return {
        "categories": [{"value": k, "label": v} for k, v in registry.CATEGORIES.items()],
        "ops": [{"value": k, "label": v} for k, v in engine.OPS.items()],
        "date_ops": sorted(engine.DATE_OPS),
        "aggs": [{"value": k, "label": v} for k, v in engine.AGGS.items()],
        "grains": list(engine.GRAINS),
        "scopes": [{"value": k, "label": v} for k, v in registry.SCOPE_LABELS.items()],
        "formats": [{"value": k, "label": v} for k, v in render.FORMATS.items()],
        "frequencies": [{"value": k, "label": v} for k, v in service.FREQUENCIES.items()],
        "can_share": service.can_share(p), "can_build": p.can(service.VIEW) or service.can_share(p),
        "can_see_all_exports": p.can("insights.audit.view") or service.can_share(p),
    }


@router.get("/sources")
def sources(db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    out = []
    for s in registry.all_sources():
        scopes = registry.allowed_scopes(db, p, s)
        if scopes:
            out.append({**s.out(include_fields=False), "scopes": scopes})
    return out


@router.get("/sources/{key}")
def source(key: str, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    s = registry.get(key)
    if s is None:
        raise NotFound("Data source")
    scopes = registry.allowed_scopes(db, p, s)
    if not scopes:
        raise Forbidden(f"You don't have access to {s.label.lower()}.")
    return {**s.out(), "scopes": scopes}


@router.get("/share-options")
def share_options(db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    from modules.roles.models import Role
    roles = db.query(Role).filter(Role.company_id == p.company_id).order_by(Role.display_name).all()
    return {"roles": [{"value": r.role_name, "label": r.display_name} for r in roles]}


@router.get("/")
def list_reports(category: str | None = None, q: str | None = None, which: str | None = None,
                 db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    return service.list_reports(db, p, category=category, q=q, which=which)


@router.post("/", status_code=201)
def create(body: ReportBody, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    if not (p.can(service.VIEW) or service.can_share(p)):
        raise Forbidden("You can't build reports.")
    r = service.save(db, p, _data(body))
    db.commit()
    return service.report_out(db, p, r)


@router.post("/preview")
def preview(body: PreviewBody, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    res = service.run_adhoc(db, p, body.source, body.config, body.scope)
    db.commit()   # function sources may bring attendance up to date
    return engine.to_json(res, body.page, body.size)


@router.post("/export")
def export_adhoc(body: ExportBody, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    if not body.source:
        raise BadRequest("Choose what to export.")
    res = service.run_adhoc(db, p, body.source, body.config or {}, body.scope)
    src = registry.get(body.source)
    log, pw = service.export(db, p, res, title=(body.title or src.label), fmt=body.format, protect=body.protect)
    db.commit()
    return {"uuid": str(log.uuid), "file_name": log.file_name, "password": pw, "rows": log.row_count,
            "sensitive": log.sensitive}


@router.get("/exports")
def exports(params: PageParams = Depends(), everyone: bool = False, channel: str | None = None,
            db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    allowed = p.can("insights.audit.view") or service.can_share(p)
    if everyone and not allowed:
        raise Forbidden("Only HR can see everyone's exports.")
    return paginate(service.export_history(db, p, everyone=everyone, channel=channel), params, service.export_out)


@router.get("/exports/{uuid}/download")
def download(uuid: UUID, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    log = service.download(db, p, uuid)
    db.commit()
    return Response(content=log.content, media_type=log.content_type,
                    headers={"Content-Disposition": f'attachment; filename="{log.file_name}"',
                             "Cache-Control": "no-store"})


# ── Schedules ──────────────────────────────────────────────────────

@router.get("/schedules")
def schedules(db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    rows = db.query(ReportSchedule, Report).join(Report, Report.id == ReportSchedule.report_id).filter(
        ReportSchedule.company_id == p.company_id).order_by(ReportSchedule.name).all()
    return [service.schedule_out(db, s, r) for s, r in rows if service.can_view(db, p, r)]


@router.post("/schedules", status_code=201)
def create_schedule(body: ScheduleBody, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    s = service.save_schedule(db, p, _data(body))
    db.commit()
    return service.schedule_out(db, s, db.get(Report, s.report_id))


def _schedule(db: Session, p: Principal, uuid) -> ReportSchedule:
    s = db.query(ReportSchedule).filter(ReportSchedule.company_id == p.company_id, ReportSchedule.uuid == uuid).first()
    if s is None:
        raise NotFound("Schedule")
    return s


@router.put("/schedules/{uuid}")
def update_schedule(uuid: UUID, body: ScheduleBody, db: Session = Depends(get_db),
                    p: Principal = Depends(get_principal)):
    s = service.save_schedule(db, p, _data(body), _schedule(db, p, uuid))
    db.commit()
    return service.schedule_out(db, s, db.get(Report, s.report_id))


@router.delete("/schedules/{uuid}", status_code=204)
def delete_schedule(uuid: UUID, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    if not service.can_share(p):
        raise Forbidden("Only HR can change schedules.")
    db.delete(_schedule(db, p, uuid))
    db.commit()


@router.post("/schedules/{uuid}/send-now")
def send_now(uuid: UUID, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    if not service.can_share(p):
        raise Forbidden("Only HR can send schedules.")
    s = _schedule(db, p, uuid)
    service.run_schedule(db, s)
    db.commit()
    return service.schedule_out(db, s, db.get(Report, s.report_id))


# ── One report ─────────────────────────────────────────────────────

@router.get("/{uuid}")
def get_report(uuid: UUID, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    r = service.get(db, p, uuid)
    src = registry.get(r.source)
    return {**service.report_out(db, p, r), "scopes": registry.allowed_scopes(db, p, src),
            "source_def": src.out()}


@router.put("/{uuid}")
def update(uuid: UUID, body: ReportBody, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    r = service.get(db, p, uuid)
    data = _data(body)
    if r.is_standard and set(data) <= {"shared_roles", "shared_employee_ids"}:
        r = service.share(db, p, r, data)
    else:
        r = service.save(db, p, data, r)
    db.commit()
    return service.report_out(db, p, r)


@router.post("/{uuid}/share")
def share(uuid: UUID, body: ReportBody, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    r = service.share(db, p, service.get(db, p, uuid), _data(body))
    db.commit()
    return service.report_out(db, p, r)


@router.post("/{uuid}/clone", status_code=201)
def clone(uuid: UUID, body: ReportBody | None = None, db: Session = Depends(get_db),
          p: Principal = Depends(get_principal)):
    r = service.clone(db, p, service.get(db, p, uuid), body.name if body else None)
    db.commit()
    return service.report_out(db, p, r)


@router.delete("/{uuid}", status_code=204)
def remove(uuid: UUID, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    service.archive(db, p, service.get(db, p, uuid))
    db.commit()


@router.post("/{uuid}/run")
def run(uuid: UUID, body: RunBody, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    r = service.get(db, p, uuid)
    res = service.run_report(db, p, r, _data(body))
    db.commit()
    return engine.to_json(res, body.page, body.size)


@router.post("/{uuid}/export")
def export(uuid: UUID, body: ExportBody, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    r = service.get(db, p, uuid)
    res = service.run_report(db, p, r, _data(body))
    log, pw = service.export(db, p, res, title=r.name, fmt=body.format, protect=body.protect, report=r)
    db.commit()
    return {"uuid": str(log.uuid), "file_name": log.file_name, "password": pw, "rows": log.row_count,
            "sensitive": log.sensitive}
