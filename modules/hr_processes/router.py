"""HR processes: process types (set up on screen), change requests and one-click revoke."""
from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.db import get_db
from core.errors import BadRequest
from core.pagination import PageParams, paginate
from core.security import get_current_user, require_permission
from core.timeutil import iso_utc
from modules.hr_processes import service as svc
from modules.hr_processes.models import HrProcessRequest, HrProcessType

router = APIRouter(prefix="/hr-processes", tags=["HR processes"])
MANAGE = require_permission("people.processes.manage")


class TypeIn(BaseModel):
    code: Optional[str] = Field(None, max_length=10)
    name: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = Field(None, max_length=1000)
    change_type: Optional[str] = None
    fields: Optional[list[str]] = None
    key_field: Optional[str] = None
    target_status: Optional[str] = None
    letter_template_uuids: Optional[list[str]] = None
    revocable: Optional[bool] = None
    needs_approval: Optional[bool] = None
    active: Optional[bool] = None
    sort_order: Optional[int] = Field(None, ge=0, le=10000)


class RequestIn(BaseModel):
    process_type_uuid: str
    employee_ids: list[int] = Field(..., min_length=1, max_length=200)
    effective_date: date
    values: dict[str, Any] = {}
    reason: Optional[str] = Field(None, max_length=300)
    remarks: Optional[str] = Field(None, max_length=2000)


class RevokeIn(BaseModel):
    reason: Optional[str] = Field(None, max_length=300)


def _type_out(t: HrProcessType, catalog: dict, counts: dict | None = None) -> dict:
    return {"uuid": str(t.uuid), "code": t.code, "name": t.name, "description": t.description,
            "change_type": t.change_type, "fields": t.fields or [],
            "field_labels": [catalog.get(f, {}).get("label", f) for f in t.fields or []],
            "key_field": t.key_field, "target_status": t.target_status,
            "letter_template_uuids": t.letter_template_uuids or [], "revocable": t.revocable,
            "needs_approval": t.needs_approval, "is_system": t.is_system, "active": t.active,
            "sort_order": t.sort_order, "requests": (counts or {}).get(t.id, 0)}


@router.get("/meta")
def meta(current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    from modules.employees import service as es
    cid = current_user["company_id"]
    catalog = svc.field_catalog(db, cid)
    letters = []
    try:
        from modules.documents.models import LetterTemplate
        letters = [{"value": str(t.uuid), "label": t.name, "description": t.letter_type}
                   for t in db.query(LetterTemplate).filter(LetterTemplate.company_id == cid,
                                                            LetterTemplate.active.is_(True))
                   .order_by(LetterTemplate.name).all()]
    except ImportError:  # pragma: no cover
        pass
    return {
        "fields": [{"value": k, **v} for k, v in catalog.items()],
        "change_types": [{"value": k, "label": v} for k, v in es.CHANGE_TYPES.items()
                         if k not in ("joining", "rehire", "exit", "status", "revoke")],
        "target_statuses": [{"value": k, "label": v} for k, v in svc.TARGET_STATUSES.items()],
        "letter_templates": letters,
        "statuses": [{"value": k, "label": v} for k, v in svc.STATUS_LABELS.items()],
        "work_modes": [{"value": k, "label": v} for k, v in es.WORK_MODES.items()],
    }


@router.get("/types")
def list_types(active: bool = False, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    from sqlalchemy import func
    cid = current_user["company_id"]
    counts = dict(db.query(HrProcessRequest.process_type_id, func.count(HrProcessRequest.id))
                  .filter(HrProcessRequest.company_id == cid).group_by(HrProcessRequest.process_type_id).all())
    catalog = svc.field_catalog(db, cid)
    return [_type_out(t, catalog, counts) for t in svc.list_types(db, cid, active_only=active)]


@router.post("/types", status_code=201)
def create_type(req: TypeIn, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    row = svc.save_type(db, cid, req.model_dump(exclude_unset=True))
    db.commit()
    return _type_out(row, svc.field_catalog(db, cid))


@router.get("/types/{uuid}")
def get_type(uuid: str, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    return _type_out(svc.get_type(db, cid, uuid), svc.field_catalog(db, cid))


@router.put("/types/{uuid}")
def update_type(uuid: str, req: TypeIn, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    row = svc.save_type(db, cid, req.model_dump(exclude_unset=True), svc.get_type(db, cid, uuid))
    db.commit()
    return _type_out(row, svc.field_catalog(db, cid))


def _request_out(db: Session, r: HrProcessRequest, names: dict, catalog: dict, detail: bool = False) -> dict:
    emp = names.get(r.employee_id) or {}
    out = {"uuid": str(r.uuid), "process": {"uuid": str(r.process_type.uuid), "code": r.process_type.code,
                                             "name": r.process_type.name, "revocable": r.process_type.revocable},
           "employee": emp, "effective_date": r.effective_date.isoformat(), "status": r.status,
           "status_label": svc.STATUS_LABELS.get(r.status, r.status), "reason": r.reason,
           "fields": [catalog.get(f, {}).get("label", f) for f in (r.new_values or {})],
           "batch_id": str(r.batch_id) if r.batch_id else None, "created_by_name": r.created_by_name,
           "created_at": iso_utc(r.created_at), "applied_at": iso_utc(r.applied_at),
           "revoked_at": iso_utc(r.revoked_at), "can_revoke": r.process_type.revocable and r.status in ("scheduled", "applied")}
    if detail:
        from core.workflow.models import WorkflowInstance
        from modules.employees import service as es
        new = dict(r.new_values or {})
        if r.status == "applied" or (r.status == "revoked" and r.old_values):
            before = dict(r.old_values or {})
        else:
            e = es.get_by_id(db, r.company_id, r.employee_id)
            before = svc.current_values(e, list(new)) if e else {}
        old_r, new_r = svc.readable(db, r.company_id, [before, new])
        out["changes"] = [{"field": f, "label": catalog.get(f, {}).get("label", f), "old": old_r.get(f),
                           "new": new_r.get(f)} for f in new]
        if r.process_type.target_status:
            out["changes"].append({"field": "status", "label": "Stage", "old": None,
                                   "new": svc.TARGET_STATUSES.get(r.process_type.target_status)})
        inst = db.query(WorkflowInstance).filter(WorkflowInstance.id == r.workflow_instance_id).first() \
            if r.workflow_instance_id else None
        out.update({"remarks": r.remarks, "note": r.note, "revoked_by_name": r.revoked_by_name,
                    "revoke_reason": r.revoke_reason, "letters": r.letter_uuids or [],
                    "workflow": {"uuid": str(inst.uuid), "reference_no": inst.reference_no, "status": inst.status}
                    if inst else None})
    return out


def _names(db: Session, cid: int, ids) -> dict:
    from modules.employees.models import Employee
    ids = set(ids)
    if not ids:
        return {}
    return {e.id: {"id": e.id, "uuid": str(e.uuid), "name": f"{e.first_name} {e.last_name}".strip(),
                   "code": e.employee_code, "photo": e.profile_image_url}
            for e in db.query(Employee).filter(Employee.company_id == cid, Employee.id.in_(ids)).all()}


@router.get("/requests")
def list_requests(status: Optional[str] = None, process: Optional[str] = None, q: Optional[str] = None,
                  employee_id: Optional[int] = None, pages: PageParams = Depends(),
                  current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    if svc.apply_due(db, cid):
        db.commit()
    query = svc.list_query(db, cid, status=status, type_uuid=process, q=q, employee_id=employee_id)
    catalog = svc.field_catalog(db, cid)
    total = query.count()
    rows = query.offset((pages.page - 1) * pages.size).limit(pages.size).all()
    names = _names(db, cid, [r.employee_id for r in rows])
    return {"items": [_request_out(db, r, names, catalog) for r in rows], "total": total, "page": pages.page,
            "size": pages.size}


@router.post("/requests", status_code=201)
def create_requests(req: RequestIn, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    ptype = svc.get_type(db, cid, req.process_type_uuid)
    rows = svc.create_requests(db, cid, ptype, req.employee_ids, req.effective_date, req.values, req.reason, req.remarks)
    db.commit()
    names = _names(db, cid, [r.employee_id for r in rows])
    catalog = svc.field_catalog(db, cid)
    return {"items": [_request_out(db, r, names, catalog) for r in rows]}


@router.get("/requests/{uuid}")
def get_request(uuid: str, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    r = svc.get_request(db, cid, uuid)
    return _request_out(db, r, _names(db, cid, [r.employee_id]), svc.field_catalog(db, cid), detail=True)


@router.post("/requests/{uuid}/revoke")
def revoke(uuid: str, req: RevokeIn, current_user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = current_user["company_id"]
    r = svc.revoke(db, svc.get_request(db, cid, uuid), req.reason)
    db.commit()
    return _request_out(db, r, _names(db, cid, [r.employee_id]), svc.field_catalog(db, cid), detail=True)


@router.get("/current")
def current(employee_id: int = Query(...), process: str = Query(...), current_user: dict = Depends(MANAGE),
            db: Session = Depends(get_db)):
    """An employee's present values of the fields a process changes (shown as "Now: …")."""
    from modules.employees import service as es
    cid = current_user["company_id"]
    ptype = svc.get_type(db, cid, process)
    emp = es.get_by_id(db, cid, employee_id)
    if emp is None:
        raise BadRequest("Employee not found.")
    vals = svc.current_values(emp, list(ptype.fields or []))
    labels = svc.readable(db, cid, [vals])[0]
    return {"values": vals, "labels": labels, "status": emp.status, "date_of_joining": _iso(emp.date_of_joining)}


def _iso(d):
    return d.isoformat() if d else None
