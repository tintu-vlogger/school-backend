"""Import and export API (Settings → Import and export)."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.db import get_db
from core.pagination import PageParams, paginate
from modules.dataio import entities, service
from modules.reports.registry import Principal, get_principal

router = APIRouter(prefix="/data", tags=["Import and export"])


class ConfigureBody(BaseModel):
    mapping: dict | None = None
    mode: str | None = None
    match_key: str | None = None


class ExportBody(BaseModel):
    entity: str
    format: str = "xlsx"
    protect: bool = False


@router.get("/entities")
def list_entities(p: Principal = Depends(get_principal)):
    return {"entities": service.entities_for(p),
            "modes": [{"value": k, "label": v} for k, v in service.MODES.items()],
            "max_rows": service.MAX_ROWS, "max_mb": service.MAX_BYTES // (1024 * 1024)}


@router.get("/entities/{key}/template")
def template(key: str, format: str = Query("xlsx", pattern="^(xlsx|csv)$"), p: Principal = Depends(get_principal)):
    e = entities.get(key)
    service.check(p, e.key)
    data, ctype, name = service.template(e, format)
    return Response(content=data, media_type=ctype, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/imports", status_code=201)
async def upload(entity: str = Form(...), file: UploadFile = File(...), db: Session = Depends(get_db),
                 p: Principal = Depends(get_principal)):
    content = await file.read()
    j = service.create_import(db, p, entity, file.filename or "upload.csv", content)
    db.commit()
    return service.job_out(j, full=True)


@router.get("/jobs")
def jobs(params: PageParams = Depends(), kind: str | None = None, db: Session = Depends(get_db),
         p: Principal = Depends(get_principal)):
    return paginate(service.history(db, p, kind), params, service.job_out)


@router.get("/imports/{uuid}")
def get_import(uuid: UUID, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    return service.job_out(service.get_job(db, p, uuid), full=True)


@router.put("/imports/{uuid}")
def configure(uuid: UUID, body: ConfigureBody, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    j = service.configure(db, p, service.get_job(db, p, uuid), body.model_dump(exclude_unset=True))
    db.commit()
    return service.job_out(j, full=True)


@router.post("/imports/{uuid}/validate")
def validate(uuid: UUID, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    j = service.validate(db, p, service.get_job(db, p, uuid))
    db.commit()
    return service.job_out(j, full=True)


@router.post("/imports/{uuid}/run")
def run(uuid: UUID, background: BackgroundTasks, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    j = service.queue(db, p, service.get_job(db, p, uuid))
    db.commit()
    background.add_task(service.run_job, p.company_id, j.id, p.employee_id, p.name or "Company owner")
    return service.job_out(j, full=True)


@router.get("/imports/{uuid}/errors")
def errors(uuid: UUID, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    j = service.get_job(db, p, uuid)
    return Response(content=service.error_file(j),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{j.entity}-rows-to-fix.xlsx"'})


@router.post("/exports")
def export(body: ExportBody, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    j, pw = service.export(db, p, body.entity, body.format, body.protect)
    db.commit()
    from modules.reports.models import ExportLog
    log = db.get(ExportLog, j.export_log_id)
    return {**service.job_out(j), "export_uuid": str(log.uuid), "password": pw, "file_name": log.file_name}
