"""Assets API: register, assignment and return, maintenance, requests, recoveries, reports."""
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from core.db import get_db
from core.errors import BadRequest, Forbidden, NotFound
from core.pagination import PageParams, paginate
from core.security import get_current_user, require_permission
from modules.assets import schemas, service
from modules.assets.models import Asset, AssetAssignment, AssetMaintenance, AssetRequest

router = APIRouter(prefix="/assets", tags=["Assets"])
MANAGE = "workplace.assets.manage"


def _money(obj):
    """Amounts go out as strings with 2 decimals (never floats)."""
    from decimal import Decimal
    if isinstance(obj, Decimal):
        return f"{obj:.2f}"
    if isinstance(obj, dict):
        return {k: _money(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_money(v) for v in obj]
    return obj


def _x_out(x: AssetAssignment, names: dict) -> dict:
    return _money({"uuid": str(x.uuid), "employee_id": x.employee_id, "employee_name": names.get(x.employee_id),
            "status": x.status, "assigned_on": x.assigned_on, "expected_return_on": x.expected_return_on,
            "assigned_by_name": x.assigned_by_name, "condition_out": x.condition_out, "notes": x.notes,
            "acknowledged_at": x.acknowledged_at, "returned_on": x.returned_on, "condition_in": x.condition_in,
            "return_notes": x.return_notes, "received_by_name": x.received_by_name,
            "recovery_amount": x.recovery_amount, "recovery_reason": x.recovery_reason,
            "recovery_status": x.recovery_status})


def _out(db: Session, a: Asset, names: dict, labels: dict, locs: dict, detail: bool = False) -> dict:
    cur = service.current_assignment(a)
    out = {"uuid": str(a.uuid), "asset_tag": a.asset_tag, "name": a.name, "category_id": a.category_id,
           "category_name": labels.get(a.category_id), "brand": a.brand, "model": a.model,
           "serial_number": a.serial_number, "purchase_date": a.purchase_date, "purchase_value": a.purchase_value,
           "vendor": a.vendor, "invoice_no": a.invoice_no, "warranty_until": a.warranty_until,
           "location_id": a.location_id, "location_name": locs.get(a.location_id), "location_note": a.location_note,
           "condition": a.condition, "status": a.status, "status_label": service.STATUSES.get(a.status),
           "notes": a.notes, "retired_on": a.retired_on, "created_at": a.created_at, "updated_at": a.updated_at,
           "custom": a.custom or {},
           "assigned_to": {"employee_id": cur.employee_id, "name": names.get(cur.employee_id),
                           "assignment_uuid": str(cur.uuid), "since": cur.assigned_on,
                           "acknowledged": cur.acknowledged_at is not None} if cur else None}
    if detail:
        out["history"] = [_x_out(x, names) for x in a.assignments]
        out["maintenance"] = [{"uuid": str(m.uuid), "kind": m.kind, "performed_on": m.performed_on, "vendor": m.vendor,
                               "cost": m.cost, "description": m.description, "status": m.status,
                               "next_due_on": m.next_due_on, "created_by_name": m.created_by_name}
                              for m in db.query(AssetMaintenance).filter(AssetMaintenance.asset_id == a.id)
                              .order_by(AssetMaintenance.performed_on.desc()).all()]
    return _money(out)


def _context(db: Session, company_id: int, assets: list[Asset]) -> tuple[dict, dict, dict]:
    from core.masters import labels_for
    from modules.helpdesk.service import location_names
    ids = [x.employee_id for a in assets for x in a.assignments]
    return (service.names_for(db, company_id, ids), labels_for(db, company_id, [a.category_id for a in assets if a.category_id]),
            location_names(db, company_id, [a.location_id for a in assets]))


@router.get("/meta")
def meta(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    return {"conditions": service.CONDITIONS, "statuses": service.STATUSES, "maintenance_kinds": service.MAINT_KINDS,
            "can_manage": service_has(db, user)}


def service_has(db: Session, user: dict) -> bool:
    from core.security import _get_role_permissions
    return MANAGE in _get_role_permissions(user["company_id"], user["role"], db)


@router.get("")
def list_assets(q: str | None = None, status_: str | None = Query(None, alias="status"), category_id: int | None = None,
                condition: str | None = None, employee_id: int | None = None, warranty_days: int | None = None,
                params: PageParams = Depends(), db: Session = Depends(get_db),
                user: dict = Depends(require_permission(MANAGE))):
    cid = user["company_id"]
    query = db.query(Asset).options(joinedload(Asset.assignments)).filter(Asset.company_id == cid)
    if status_:
        query = query.filter(Asset.status.in_(status_.split(",")))
    if category_id:
        from core.masters import MasterValue
        children = [i for (i,) in db.query(MasterValue.id).filter(MasterValue.company_id == cid,
                                                                  MasterValue.parent_id == category_id)]
        query = query.filter(Asset.category_id.in_([category_id, *children]))
    if condition:
        query = query.filter(Asset.condition == condition)
    if employee_id:
        query = query.filter(Asset.assignments.any((AssetAssignment.employee_id == employee_id) &
                                                   AssetAssignment.status.in_(("pending_ack", "active"))))
    if warranty_days is not None:
        from datetime import timedelta
        today = service._today(db, cid)
        query = query.filter(Asset.warranty_until >= today, Asset.warranty_until <= today + timedelta(days=warranty_days))
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Asset.name.ilike(like), Asset.asset_tag.ilike(like), Asset.serial_number.ilike(like),
                                 Asset.model.ilike(like), Asset.brand.ilike(like)))
    total = query.order_by(None).count()
    rows = query.order_by(Asset.asset_tag).offset(params.offset).limit(params.size).all()
    names, labels, locs = _context(db, cid, rows)
    return {"items": [_out(db, a, names, labels, locs) for a in rows], "total": total, "page": params.page,
            "size": params.size}


@router.post("", status_code=status.HTTP_201_CREATED)
def create(body: schemas.AssetIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    a = service.save(db, user["company_id"], body.model_dump())
    db.commit()
    return detail(a.uuid, db, user)


@router.get("/report")
def report(db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    return _money(service.report(db, user["company_id"]))


@router.get("/recoveries")
def recoveries(employee_id: int | None = None, status_: str | None = Query("pending", alias="status"),
               db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    """Amounts to recover for damaged or lost assets (final settlement reads this)."""
    return _money(service.recoveries(db, user["company_id"], employee_id, status_ or None))


@router.get("/outstanding/{employee_id}")
def outstanding(employee_id: int, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    """Assets an employee still holds (exit clearance)."""
    rec = service.recoveries(db, user["company_id"], employee_id)
    return _money({"items": service.outstanding_assets(db, user["company_id"], employee_id),
                   "recoveries": rec["items"], "total_pending": rec["total_pending"]})


@router.get("/me")
def mine(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    cid = user["company_id"]
    if not user.get("employee_id"):
        return {"items": [], "requests": []}
    xs = (db.query(AssetAssignment).options(joinedload(AssetAssignment.asset))
          .filter(AssetAssignment.company_id == cid, AssetAssignment.employee_id == user["employee_id"])
          .order_by(AssetAssignment.status != "pending_ack", AssetAssignment.returned_on.desc().nullsfirst()).all())
    from core.masters import labels_for
    labels = labels_for(db, cid, [x.asset.category_id for x in xs if x.asset.category_id])
    reqs = (db.query(AssetRequest).filter(AssetRequest.company_id == cid, AssetRequest.employee_id == user["employee_id"])
            .order_by(AssetRequest.created_at.desc()).limit(30).all())
    rlabels = labels_for(db, cid, [r.category_id for r in reqs if r.category_id])
    return {"items": [{**_x_out(x, {}), "asset": {"uuid": str(x.asset.uuid), "asset_tag": x.asset.asset_tag,
                                                  "name": x.asset.name, "brand": x.asset.brand, "model": x.asset.model,
                                                  "serial_number": x.asset.serial_number,
                                                  "category_name": labels.get(x.asset.category_id)}} for x in xs],
            "requests": [_req_out(r, rlabels, {}) for r in reqs]}


def _req_out(r: AssetRequest, labels: dict, names: dict) -> dict:
    return {"uuid": str(r.uuid), "employee_id": r.employee_id, "employee_name": names.get(r.employee_id),
            "category_id": r.category_id, "category_name": labels.get(r.category_id), "details": r.details,
            "needed_by": r.needed_by, "status": r.status, "created_at": r.created_at, "fulfilled_at": r.fulfilled_at}


@router.post("/requests", status_code=status.HTTP_201_CREATED)
def request_asset(body: schemas.RequestIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    r = service.submit_request(db, user, body.model_dump())
    db.commit()
    return _req_out(r, {}, {})


@router.get("/requests")
def list_requests(status_: str | None = Query(None, alias="status"), params: PageParams = Depends(),
                  db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = user["company_id"]
    q = db.query(AssetRequest).filter(AssetRequest.company_id == cid)
    if status_:
        q = q.filter(AssetRequest.status.in_(status_.split(",")))
    page = paginate(q.order_by(AssetRequest.created_at.desc()), params)
    from core.masters import labels_for
    labels = labels_for(db, cid, [r.category_id for r in page["items"] if r.category_id])
    names = service.names_for(db, cid, [r.employee_id for r in page["items"]])
    page["items"] = [_req_out(r, labels, names) for r in page["items"]]
    return page


@router.post("/assignments/{uuid}/acknowledge")
def acknowledge(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    x = service.get_assignment(db, user["company_id"], uuid)
    service.acknowledge(db, user, x)
    db.commit()
    return _x_out(x, {})


@router.post("/assignments/{uuid}/return")
def return_asset(uuid: UUID, body: schemas.ReturnIn, db: Session = Depends(get_db),
                 user: dict = Depends(require_permission(MANAGE))):
    x = service.get_assignment(db, user["company_id"], uuid)
    service.return_asset(db, user["company_id"], x, **body.model_dump())
    db.commit()
    return _x_out(x, {})


@router.post("/assignments/{uuid}/recovery")
def recovery(uuid: UUID, body: schemas.RecoveryIn, db: Session = Depends(get_db),
             user: dict = Depends(require_permission(MANAGE))):
    x = service.get_assignment(db, user["company_id"], uuid)
    service.set_recovery(db, x, body.status, body.note)
    db.commit()
    return _x_out(x, {})


@router.get("/{uuid}")
def detail(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    a = service.get(db, user["company_id"], uuid)
    names, labels, locs = _context(db, user["company_id"], [a])
    return _out(db, a, names, labels, locs, detail=True)


@router.put("/{uuid}")
def update(uuid: UUID, body: schemas.AssetIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    a = service.get(db, user["company_id"], uuid)
    service.save(db, user["company_id"], body.model_dump(exclude_unset=True), a)
    db.commit()
    return detail(uuid, db, user)


@router.post("/{uuid}/status")
def set_status(uuid: UUID, body: schemas.StatusIn, db: Session = Depends(get_db),
               user: dict = Depends(require_permission(MANAGE))):
    a = service.get(db, user["company_id"], uuid)
    service.set_status(db, a, body.status, body.note)
    db.commit()
    return detail(uuid, db, user)


@router.post("/{uuid}/assign")
def assign(uuid: UUID, body: schemas.AssignIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = user["company_id"]
    a = service.get(db, cid, uuid)
    req = service.get_request(db, cid, body.request_uuid) if body.request_uuid else None
    if req is not None and req.status != "approved":
        raise BadRequest("Only approved requests can be fulfilled.")
    if req is not None and req.employee_id != body.employee_id:
        raise BadRequest("Give the asset to the person who asked for it.")
    service.assign(db, cid, a, body.employee_id, assigned_on=body.assigned_on, expected_return_on=body.expected_return_on,
                   condition=body.condition, notes=body.notes, request=req)
    db.commit()
    return detail(uuid, db, user)


@router.post("/{uuid}/maintenance", status_code=status.HTTP_201_CREATED)
def add_maintenance(uuid: UUID, body: schemas.MaintenanceIn, db: Session = Depends(get_db),
                    user: dict = Depends(require_permission(MANAGE))):
    a = service.get(db, user["company_id"], uuid)
    service.add_maintenance(db, user["company_id"], a, body.model_dump())
    db.commit()
    return detail(uuid, db, user)


@router.put("/{uuid}/maintenance/{mid}")
def update_maintenance(uuid: UUID, mid: UUID, body: schemas.MaintenanceIn, db: Session = Depends(get_db),
                       user: dict = Depends(require_permission(MANAGE))):
    a = service.get(db, user["company_id"], uuid)
    m = db.query(AssetMaintenance).filter(AssetMaintenance.company_id == user["company_id"],
                                          AssetMaintenance.asset_id == a.id, AssetMaintenance.uuid == mid).first()
    if m is None:
        raise NotFound("Maintenance entry")
    service.add_maintenance(db, user["company_id"], a, body.model_dump(), m)
    db.commit()
    return detail(uuid, db, user)
