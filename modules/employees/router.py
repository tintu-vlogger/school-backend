"""Employees API: the directory, the full record and its sections, lifecycle actions,
job history, documents, skills, tags, exports, saved views and self service (/me).

Static paths are declared before /{uuid} paths so they are not read as an id."""
import io
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core import audit
from core.csv_safe import _clean as safe_cell
from core.csv_safe import writer as safe_csv_writer
from core.db import get_db
from core.errors import BadRequest, Forbidden, NotFound
from core.images import MAX_UPLOAD_BYTES, to_square_data_url
from core.masters import labels_for
from core.pagination import PageParams, paginate
from core.security import _get_role_permissions, get_current_user, require_permission
from modules.employees import details, documents, profile, sensitive, service
from modules.employees import schemas as sc
from modules.employees.models import Employee, EmployeeDocument, EmployeeSettings, JobChange, SavedView
from modules.employees.schemas import (EmployeeCreate, EmployeeDetailResponse, EmployeeResponse, EmployeeUpdate,
                                       WorkArrangementUpdate)
from modules.employees.work_arrangement import DEFAULT_WORK_ARRANGEMENT, normalize_work_arrangement

router = APIRouter(prefix="/employees", tags=["Employees"])

VIEW = require_permission("people.employees.view")
MANAGE = require_permission("people.employees.manage")
EXPORT = require_permission("people.employees.export")
VERIFY = require_permission("people.documents.verify")


def perms(db: Session, user: dict) -> list[str]:
    return _get_role_permissions(user["company_id"], user["role"], db)


def _response(emp: Employee, cls=EmployeeResponse, tags: dict | None = None, labels: dict | None = None, **extra):
    labels = labels or {}
    return cls(
        id=emp.id, uuid=emp.uuid, employee_code=emp.employee_code, greeting=emp.greeting,
        first_name=emp.first_name, last_name=emp.last_name, preferred_name=emp.preferred_name, email=emp.email,
        phone=emp.phone, role=emp.role, active=emp.active, status=emp.status,
        status_label=service.STATUSES.get(emp.status), designation_id=emp.designation_id,
        designation_name=emp.designation.designation_name if emp.designation else None,
        department_id=emp.department_id, department_name=emp.department.department_name if emp.department else None,
        location_id=emp.location_id, location_name=emp.location.name if emp.location else None,
        legal_entity_id=emp.legal_entity_id, legal_entity_name=emp.legal_entity.name if emp.legal_entity else None,
        grade_id=emp.grade_id, grade_name=emp.grade.name if emp.grade else None,
        employment_type_id=emp.employment_type_id, employment_type_name=labels.get(emp.employment_type_id),
        date_of_joining=emp.date_of_joining, work_mode=emp.work_mode,
        reporting_manager_id=emp.reporting_manager_id,
        reporting_manager_uuid=emp.reporting_manager.uuid if emp.reporting_manager else None,
        reporting_manager_name=service.display_name(emp.reporting_manager), timezone=emp.timezone,
        custom=emp.custom or {}, tags=(tags or {}).get(emp.id, []), profile_image_url=emp.profile_image_url,
        created_at=emp.created_at, updated_at=emp.updated_at, **extra,
    )


def _responses(db: Session, company_id: int, rows: list[Employee]) -> list:
    from modules.organization import service as org
    tags = org.tags_of(db, company_id, "employees", [e.id for e in rows])
    labels = labels_for(db, company_id, [e.employment_type_id for e in rows if e.employment_type_id])
    return [_response(e, tags=tags, labels=labels) for e in rows]


def _get(db: Session, company_id: int, uuid: UUID) -> Employee:
    return service.get(db, company_id, uuid)


def _ids(value: str | None) -> list[int]:
    return [int(x) for x in (value or "").split(",") if x.strip().isdigit()]


def _filtered(db: Session, company_id: int, *, q=None, status_filter=None, designation_id=None, department_id=None,
              location_id=None, legal_entity_id=None, employment_type_id=None, grade_id=None, team_id=None, tag_id=None,
              reporting_manager_id=None, work_mode=None):
    query = service.search(service.base_query(db, company_id), q)
    for col, val in ((Employee.designation_id, designation_id), (Employee.department_id, department_id),
                     (Employee.location_id, location_id), (Employee.legal_entity_id, legal_entity_id),
                     (Employee.employment_type_id, employment_type_id), (Employee.grade_id, grade_id)):
        ids = _ids(val)
        if ids:
            query = query.filter(col.in_(ids))
    if reporting_manager_id is not None:
        query = query.filter(Employee.reporting_manager_id == reporting_manager_id)
    if work_mode:
        query = query.filter(Employee.work_mode.in_(work_mode.split(",")))
    statuses = [s for s in (status_filter or "").split(",") if s]
    if statuses:
        conds = []
        for s in statuses:
            if s == "active":
                conds.append(Employee.active.is_(True))
            elif s == "inactive":
                conds.append(Employee.active.is_(False))
            elif s in service.STATUSES:
                conds.append(Employee.status == s)
        if conds:
            query = query.filter(or_(*conds))
    else:
        query = query.filter(Employee.status != "archived")
    if _ids(team_id):
        from modules.organization.models import TeamMember
        query = query.filter(Employee.id.in_(db.query(TeamMember.employee_id).filter(
            TeamMember.company_id == company_id, TeamMember.team_id.in_(_ids(team_id)))))
    if _ids(tag_id):
        from modules.organization import service as org
        from modules.organization.models import TagLink
        all_tags = org.tag_descendants(db, company_id, _ids(tag_id))
        query = query.filter(Employee.id.in_(db.query(TagLink.entity_id).filter(
            TagLink.company_id == company_id, TagLink.entity_type == "employees", TagLink.tag_id.in_(all_tags))))
    return query


SORTS = {
    "name": (Employee.first_name, Employee.last_name),
    "code": (Employee.employee_code,),
    "joining": (Employee.date_of_joining,),
    "recent": (Employee.created_at.desc(),),
}


@router.get("/")
def list_employees(
    search: str | None = Query(None), q: str | None = Query(None),
    designation_id: str | None = None, department_id: str | None = None, location_id: str | None = None,
    legal_entity_id: str | None = None, employment_type_id: str | None = None, grade_id: str | None = None,
    team_id: str | None = None, tag_id: str | None = None, reporting_manager_id: int | None = None,
    work_mode: str | None = None, status_filter: str | None = Query(None, alias="status"),
    sort: str = "name", direction: str = "asc",
    page: int | None = Query(None, ge=1), size: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db), current_user: dict = Depends(VIEW),
):
    """Employees of the company (archived records only when asked for by status). With
    `page` the answer is paginated ({items, total, page, size}); without it, the full list."""
    cid = current_user["company_id"]
    service.apply_due_changes(db, cid)
    query = _filtered(db, cid, q=q or search, status_filter=status_filter, designation_id=designation_id,
                      department_id=department_id, location_id=location_id, legal_entity_id=legal_entity_id,
                      employment_type_id=employment_type_id, grade_id=grade_id, team_id=team_id, tag_id=tag_id,
                      reporting_manager_id=reporting_manager_id, work_mode=work_mode)
    # Data scope of "See employees" (Roles & access): own team, department, location ...
    from modules.roles.access import permission_scope, scope_employee_ids
    allowed = scope_employee_ids(db, current_user, permission_scope(db, current_user, "people.employees.view"))
    if allowed is not None:
        query = query.filter(Employee.id.in_(allowed))
    cols = SORTS.get(sort, SORTS["name"])
    if direction == "desc" and sort != "recent":
        cols = tuple(c.desc() for c in cols)
    query = query.order_by(*cols, Employee.id)
    if page is None:
        rows = query.all()
        db.commit()
        return _responses(db, cid, rows)
    result = paginate(query, PageParams(page, size))
    result["items"] = _responses(db, cid, result["items"])
    db.commit()
    return result


@router.get("/lookup")
def lookup_employees(
    q: str | None = Query(None), ids: str | None = Query(None, description="comma separated ids to resolve labels"),
    active_only: bool = True, limit: int = Query(20, ge=1, le=50),
    db: Session = Depends(get_db), current_user: dict = Depends(get_current_user),
):
    """Small option list for searchable pickers: [{value, label, hint, uuid}]."""
    query = db.query(Employee).filter(Employee.company_id == current_user["company_id"])
    if ids:
        query = query.filter(Employee.id.in_(_ids(ids)[:50]))
    else:
        query = service.search(query, q)
        if active_only:
            query = query.filter(Employee.active.is_(True))
    rows = query.order_by(Employee.first_name, Employee.last_name).limit(limit).all()
    return [{"value": e.id, "uuid": str(e.uuid), "label": service.display_name(e), "photo": e.profile_image_url,
             "hint": " · ".join(x for x in (e.employee_code, e.email) if x)} for e in rows]


@router.get("/stats")
def directory_stats(db: Session = Depends(get_db), current_user: dict = Depends(VIEW)):
    """Head count by stage, plus joiners this month and documents waiting for a check."""
    from sqlalchemy import func
    cid = current_user["company_id"]
    by_status = dict(db.query(Employee.status, func.count(Employee.id)).filter(Employee.company_id == cid)
                     .group_by(Employee.status).all())
    t = service.today()
    joiners = db.query(func.count(Employee.id)).filter(Employee.company_id == cid, Employee.date_of_joining >= t.replace(day=1),
                                                       Employee.status.notin_(("exited", "archived"))).scalar()
    pending_docs = documents.company_query(db, cid).filter(EmployeeDocument.verification_status == "pending").count()
    return {"by_status": by_status, "active": sum(v for k, v in by_status.items() if k not in ("exited", "archived")),
            "joiners_this_month": joiners, "pending_documents": pending_docs}


@router.post("/duplicates")
def check_duplicates(body: sc.DuplicateCheck, db: Session = Depends(get_db), current_user: dict = Depends(MANAGE)):
    """Others holding the same email, phone, PAN, Aadhaar or UAN (shown as warnings while typing)."""
    from modules.employees import validators as v
    cid = current_user["company_id"]
    exclude = None
    if body.exclude_uuid:
        exclude = _get(db, cid, body.exclude_uuid).id

    def safe(fn, x):
        try:
            return fn(x)
        except HTTPException:
            return None
    return service.find_duplicates(db, cid, email=body.email, phone=body.phone, pan=safe(v.clean_pan, body.pan),
                                   aadhaar=safe(v.clean_aadhaar, body.aadhaar), uan=safe(v.clean_uan, body.uan),
                                   exclude_id=exclude)


@router.post("/", response_model=EmployeeResponse, status_code=status.HTTP_201_CREATED)
def create_employee(payload: EmployeeCreate, db: Session = Depends(get_db), current_user: dict = Depends(MANAGE)):
    emp = service.create(db, current_user["company_id"], payload.model_dump(exclude_unset=True))
    db.commit()
    return _responses(db, current_user["company_id"], [service.get(db, current_user["company_id"], emp.uuid)])[0]


# ── Bulk actions, export and saved views ─────────────────────────────

@router.post("/bulk/manager")
def bulk_change_manager(body: sc.BulkManager, db: Session = Depends(get_db), current_user: dict = Depends(MANAGE)):
    cid = current_user["company_id"]
    rows = db.query(Employee).filter(Employee.company_id == cid, Employee.uuid.in_(body.employee_uuids)).all()
    if len(rows) != len(set(body.employee_uuids)):
        raise BadRequest("Some of the people picked were not found.")
    changed = 0
    for emp in rows:
        if emp.reporting_manager_id == body.manager_id or emp.status in service.INACTIVE_STATUSES:
            continue
        if body.effective_date and body.effective_date > service.today():
            service.record_job_change(db, cid, emp, "manager_change", body.effective_date,
                                      {"reporting_manager_id": body.manager_id}, body.reason)
        else:
            service.update(db, cid, emp, {"reporting_manager_id": body.manager_id}, change_type="manager_change",
                           reason=body.reason, effective_date=body.effective_date)
        changed += 1
    db.commit()
    return {"changed": changed}


EXPORT_COLUMNS = [
    ("employee_code", "Employee ID"), ("name", "Name"), ("email", "Work email"), ("phone", "Mobile"),
    ("status", "Stage"), ("designation", "Designation"), ("department", "Department"), ("location", "Location"),
    ("legal_entity", "Legal entity"), ("grade", "Grade"), ("employment_type", "Employment type"),
    ("manager", "Reporting manager"), ("date_of_joining", "Joining date"), ("work_mode", "Work mode"), ("tags", "Tags"),
]


@router.get("/export")
def export_employees(
    format: str = Query("csv", pattern="^(csv|xlsx)$"), q: str | None = None, status_filter: str | None = Query(None, alias="status"),
    designation_id: str | None = None, department_id: str | None = None, location_id: str | None = None,
    legal_entity_id: str | None = None, employment_type_id: str | None = None, grade_id: str | None = None,
    team_id: str | None = None, tag_id: str | None = None, uuids: str | None = None, columns: str | None = None,
    db: Session = Depends(get_db), current_user: dict = Depends(EXPORT),
):
    """The filtered list (or the picked people) as CSV or Excel. Logged in the audit log."""
    cid = current_user["company_id"]
    query = _filtered(db, cid, q=q, status_filter=status_filter, designation_id=designation_id,
                      department_id=department_id, location_id=location_id, legal_entity_id=legal_entity_id,
                      employment_type_id=employment_type_id, grade_id=grade_id, team_id=team_id, tag_id=tag_id)
    if uuids:
        query = query.filter(Employee.uuid.in_([u for u in uuids.split(",") if u]))
    rows = query.order_by(Employee.first_name, Employee.last_name).limit(10000).all()
    from modules.organization import service as org
    tags = org.tags_of(db, cid, "employees", [e.id for e in rows])
    types = labels_for(db, cid, [e.employment_type_id for e in rows if e.employment_type_id])
    wanted = [c for c in EXPORT_COLUMNS if not columns or c[0] in columns.split(",")]

    def value(e: Employee, key: str):
        return {
            "employee_code": e.employee_code, "name": service.display_name(e), "email": e.email, "phone": e.phone,
            "status": service.STATUSES.get(e.status), "designation": e.designation.designation_name if e.designation else None,
            "department": e.department.department_name if e.department else None,
            "location": e.location.name if e.location else None,
            "legal_entity": e.legal_entity.name if e.legal_entity else None, "grade": e.grade.name if e.grade else None,
            "employment_type": types.get(e.employment_type_id), "manager": service.display_name(e.reporting_manager),
            "date_of_joining": e.date_of_joining.isoformat() if e.date_of_joining else None,
            "work_mode": service.WORK_MODES.get(e.work_mode) if e.work_mode else None,
            "tags": ", ".join(t["name"] for t in tags.get(e.id, [])),
        }[key]

    audit.event(db, "export.downloaded", "employees", None, {"format": format, "rows": len(rows)})
    db.commit()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    if format == "xlsx":
        from openpyxl import Workbook
        from openpyxl.styles import Font
        wb = Workbook()
        ws = wb.active
        ws.title = "Employees"
        ws.append([label for _, label in wanted])
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for e in rows:
            ws.append([safe_cell(value(e, k)) for k, _ in wanted])
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width = min(40, max(12, *(len(str(c.value or "")) + 2 for c in col)))
        buf = io.BytesIO()
        wb.save(buf)
        return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="employees-{stamp}.xlsx"'})
    buf = io.StringIO()
    w = safe_csv_writer(buf)
    w.writerow([label for _, label in wanted])
    for e in rows:
        w.writerow([safe_cell(value(e, k)) for k, _ in wanted])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="employees-{stamp}.csv"'})


def _owner_key(user: dict) -> str:
    return f"employee:{user['employee_id']}" if user.get("employee_id") else "owner"


def _view_out(r: SavedView, user: dict) -> dict:
    return {"uuid": str(r.uuid), "name": r.name, "filters": r.filters, "columns": r.columns, "shared": r.shared,
            "mine": r.owner_key == _owner_key(user)}


@router.get("/saved-views")
def list_saved_views(db: Session = Depends(get_db), current_user: dict = Depends(VIEW)):
    rows = (db.query(SavedView).filter(SavedView.company_id == current_user["company_id"], SavedView.entity == "employees",
                                       or_(SavedView.owner_key == _owner_key(current_user), SavedView.shared.is_(True)))
            .order_by(SavedView.name).all())
    return [_view_out(r, current_user) for r in rows]


@router.post("/saved-views", status_code=201)
def create_saved_view(body: sc.SavedViewIn, db: Session = Depends(get_db), current_user: dict = Depends(VIEW)):
    cid = current_user["company_id"]
    if db.query(SavedView.id).filter(SavedView.company_id == cid, SavedView.entity == "employees",
                                     SavedView.owner_key == _owner_key(current_user),
                                     SavedView.name.ilike(body.name.strip())).first():
        raise BadRequest("You already have a view with this name.")
    row = SavedView(company_id=cid, entity="employees", owner_key=_owner_key(current_user), name=body.name.strip(),
                    filters=body.filters, columns=body.columns, shared=body.shared)
    db.add(row)
    db.commit()
    return _view_out(row, current_user)


@router.put("/saved-views/{uuid}")
def update_saved_view(uuid: UUID, body: sc.SavedViewIn, db: Session = Depends(get_db), current_user: dict = Depends(VIEW)):
    row = db.query(SavedView).filter(SavedView.company_id == current_user["company_id"], SavedView.uuid == uuid,
                                     SavedView.owner_key == _owner_key(current_user)).first()
    if row is None:
        raise NotFound("View")
    row.name, row.filters, row.columns, row.shared = body.name.strip(), body.filters, body.columns, body.shared
    db.commit()
    return _view_out(row, current_user)


@router.delete("/saved-views/{uuid}")
def delete_saved_view(uuid: UUID, db: Session = Depends(get_db), current_user: dict = Depends(VIEW)):
    """A saved filter is a personal setting, not a business record, so it can be removed."""
    row = db.query(SavedView).filter(SavedView.company_id == current_user["company_id"], SavedView.uuid == uuid,
                                     SavedView.owner_key == _owner_key(current_user)).first()
    if row is None:
        raise NotFound("View")
    db.delete(row)
    db.commit()
    return {"detail": "View removed"}


# ── Documents across the company ─────────────────────────────────────

@router.get("/documents")
def company_documents(
    status_filter: str | None = Query(None, alias="status"), expiry: str | None = None, type_id: int | None = None,
    q: str | None = None, page: int = Query(1, ge=1), size: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db), current_user: dict = Depends(VIEW),
):
    """Every employee document, to verify and to watch expiry (expiry=expired|expiring|30|60|90)."""
    from datetime import timedelta
    cid = current_user["company_id"]
    documents.send_expiry_reminders(db, cid)
    query = documents.company_query(db, cid).filter(Employee.status != "archived")
    if status_filter:
        query = query.filter(EmployeeDocument.verification_status.in_(status_filter.split(",")))
    if type_id:
        from core.masters import MasterValue
        subs = [i for (i,) in db.query(MasterValue.id).filter(MasterValue.parent_id == type_id)]
        query = query.filter(EmployeeDocument.document_type_id.in_([type_id, *subs]))
    t = service.today()
    if expiry == "expired":
        query = query.filter(EmployeeDocument.expiry_date < t)
    elif expiry and expiry.isdigit():
        query = query.filter(EmployeeDocument.expiry_date >= t, EmployeeDocument.expiry_date <= t + timedelta(days=int(expiry)))
    elif expiry == "expiring":
        query = query.filter(EmployeeDocument.expiry_date >= t, EmployeeDocument.expiry_date <= t + timedelta(days=60))
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Employee.first_name.ilike(like), Employee.last_name.ilike(like),
                                 Employee.employee_code.ilike(like), EmployeeDocument.document_number.ilike(like),
                                 EmployeeDocument.title.ilike(like)))
    query = query.order_by(EmployeeDocument.expiry_date.asc().nulls_last() if expiry else EmployeeDocument.created_at.desc(),
                           EmployeeDocument.id)
    result = paginate(query, PageParams(page, size))
    rows = result["items"]
    emps = {e.id: {"uuid": str(e.uuid), "name": service.display_name(e), "code": e.employee_code,
                   "photo": e.profile_image_url} for e in
            db.query(Employee).filter(Employee.id.in_([r.employee_id for r in rows] or [0]))}
    result["items"] = documents.out(db, cid, rows, emps)
    db.commit()
    return result


# ── One employee ─────────────────────────────────────────────────────

@router.get("/{uuid}", response_model=EmployeeDetailResponse)
def get_employee(uuid: UUID, db: Session = Depends(get_db), current_user: dict = Depends(VIEW)):
    emp = _get(db, current_user["company_id"], uuid)
    return _response(emp, EmployeeDetailResponse, config=emp.settings.config if emp.settings else None)


def _profile(db: Session, user: dict, emp: Employee) -> dict:
    p = perms(db, user)
    is_self = user.get("employee_id") == emp.id
    data = profile.build(db, user["company_id"], emp, sensitive="people.sensitive.view" in p,
                         can_edit="people.employees.manage" in p, can_verify="people.documents.verify" in p,
                         is_self=is_self)
    if not is_self:
        _apply_field_access(db, user, emp, data)
    db.commit()
    return data


def _apply_field_access(db: Session, user: dict, emp: Employee, data: dict) -> None:
    """Roles can narrow what a viewer sees of other people's PAN, Aadhaar and bank details
    (Roles & access, sensitive fields). It never widens what people.sensitive.view allows."""
    from modules.employees import validators as v_
    from modules.roles.access import field_level
    pan = field_level(db, user, "pan")
    if pan == "hidden":
        data["pan"] = None
    elif pan == "masked" and emp.pan:
        data["pan"] = v_.mask(emp.pan)
    if field_level(db, user, "aadhaar") == "hidden":
        data["aadhaar_masked"] = None
    bank = field_level(db, user, "bank")
    if bank == "hidden":
        data["bank_accounts"] = []
    elif bank == "masked" and data.get("permissions", {}).get("sensitive"):
        data["bank_accounts"] = details.out_rows(db, user["company_id"], "bank-accounts",
                                                 details.list_rows(db, user["company_id"], emp.id, "bank-accounts"),
                                                 full_bank=False)


@router.get("/{uuid}/profile")
def get_profile(uuid: UUID, db: Session = Depends(get_db), current_user: dict = Depends(VIEW)):
    return _profile(db, current_user, _get(db, current_user["company_id"], uuid))


@router.put("/{uuid}", response_model=EmployeeResponse)
def update_employee(uuid: UUID, payload: EmployeeUpdate, db: Session = Depends(get_db), current_user: dict = Depends(MANAGE)):
    cid = current_user["company_id"]
    emp = _get(db, cid, uuid)
    service.update(db, cid, emp, payload.model_dump(exclude_unset=True))
    db.commit()
    return _responses(db, cid, [service.get(db, cid, uuid)])[0]


@router.delete("/{uuid}")
def archive_employee(uuid: UUID, db: Session = Depends(get_db), current_user: dict = Depends(MANAGE)):
    """Employees are never deleted. Someone still working is marked as exited; someone who
    has left (or never joined) is archived."""
    cid = current_user["company_id"]
    emp = _get(db, cid, uuid)
    if emp.status in ("exited", "pre_joining"):
        service.archive(db, cid, emp)
        detail = "Employee archived"
    elif emp.status == "archived":
        raise BadRequest("This record is already archived.")
    else:
        service.set_status(db, cid, emp, "exited", reason="Deactivated")
        detail = "Employee deactivated"
    db.commit()
    return {"detail": detail}


@router.post("/{uuid}/archive")
def archive(uuid: UUID, body: sc.ArchiveIn, db: Session = Depends(get_db), current_user: dict = Depends(MANAGE)):
    cid = current_user["company_id"]
    service.archive(db, cid, _get(db, cid, uuid), body.reason)
    db.commit()
    return {"detail": "Employee archived"}


@router.post("/{uuid}/status")
def change_status(uuid: UUID, body: sc.StatusChange, db: Session = Depends(get_db), current_user: dict = Depends(MANAGE)):
    cid = current_user["company_id"]
    emp = _get(db, cid, uuid)
    service.set_status(db, cid, emp, body.status, reason=body.reason, effective=body.effective_date,
                       extra=body.model_dump(exclude_unset=True))
    db.commit()
    return _profile(db, current_user, _get(db, cid, uuid))


@router.post("/{uuid}/rehire")
def rehire(uuid: UUID, body: sc.RehireIn, db: Session = Depends(get_db), current_user: dict = Depends(MANAGE)):
    cid = current_user["company_id"]
    emp = _get(db, cid, uuid)
    service.rehire(db, cid, emp, body.model_dump(exclude_unset=True))
    db.commit()
    return _profile(db, current_user, _get(db, cid, uuid))


@router.get("/{uuid}/history")
def job_history(uuid: UUID, db: Session = Depends(get_db), current_user: dict = Depends(VIEW)):
    cid = current_user["company_id"]
    return profile.history(db, cid, _get(db, cid, uuid).id)


@router.post("/{uuid}/job-changes", status_code=201)
def add_job_change(uuid: UUID, body: sc.JobChangeIn, db: Session = Depends(get_db), current_user: dict = Depends(MANAGE)):
    cid = current_user["company_id"]
    emp = _get(db, cid, uuid)
    row = service.record_job_change(db, cid, emp, body.change_type, body.effective_date, body.changes, body.reason,
                                    body.remarks)
    db.commit()
    return {"uuid": str(row.uuid), "status": row.status}


@router.post("/{uuid}/job-changes/{change_uuid}/cancel")
def cancel_job_change(uuid: UUID, change_uuid: UUID, db: Session = Depends(get_db), current_user: dict = Depends(MANAGE)):
    cid = current_user["company_id"]
    emp = _get(db, cid, uuid)
    row = db.query(JobChange).filter(JobChange.company_id == cid, JobChange.employee_id == emp.id,
                                     JobChange.uuid == change_uuid).first()
    if row is None:
        raise NotFound("Change")
    service.cancel_job_change(db, cid, row)
    db.commit()
    return {"detail": "Change cancelled"}


@router.get("/{uuid}/aadhaar")
def reveal_aadhaar(uuid: UUID, db: Session = Depends(get_db),
                   current_user: dict = Depends(require_permission("people.sensitive.view"))):
    """The full Aadhaar number, for people allowed to see it. Every look is logged."""
    cid = current_user["company_id"]
    emp = _get(db, cid, uuid)
    from modules.roles.access import field_level
    if emp.id != current_user.get("employee_id") and field_level(db, current_user, "aadhaar") != "full":
        raise Forbidden("Your role can't see full Aadhaar numbers.")
    number = sensitive.decrypt(emp.aadhaar_encrypted)
    if not number:
        raise NotFound("Aadhaar number")
    audit.event(db, "employee.aadhaar_viewed", "employees", emp.id, entity_uuid=str(emp.uuid),
                entity_label=service.display_name(emp))
    db.commit()
    return {"aadhaar": f"{number[:4]} {number[4:8]} {number[8:]}"}


class TagsIn(BaseModel):
    tag_ids: list[int] = Field(default_factory=list)


@router.put("/{uuid}/tags")
def set_employee_tags(uuid: UUID, body: TagsIn, db: Session = Depends(get_db),
                      current_user: dict = Depends(require_permission("people.tags.manage"))):
    from modules.organization import service as org
    cid = current_user["company_id"]
    emp = _get(db, cid, uuid)
    org.set_tags(db, cid, "employees", emp.id, body.tag_ids)
    db.commit()
    return org.tags_of(db, cid, "employees", [emp.id]).get(emp.id, [])


# ── Sections (contacts, family, nominees, bank, education, experience) ─

def _section_access(db: Session, user: dict, emp: Employee, kind: str, write: bool) -> bool:
    """Returns whether the viewer may see full bank numbers. HR edits everything; an
    employee edits their own self-service sections."""
    p = perms(db, user)
    if "people.employees.manage" in p:
        if user.get("employee_id") != emp.id and kind == "bank-accounts":
            from modules.roles.access import field_level
            level = field_level(db, user, "bank")
            if level == "hidden" and not write:
                raise Forbidden("Your role can't see bank details.")
            return "people.sensitive.view" in p and level == "full"
        return "people.sensitive.view" in p
    if user.get("employee_id") == emp.id and (not write or kind in details.SELF_SERVICE_KINDS):
        return False
    if not write and "people.employees.view" in p and kind not in ("bank-accounts", "nominees"):
        return False
    raise Forbidden("You can't change this section. Ask HR, or send a change request.")


@router.get("/{uuid}/details/{kind}")
def list_section(uuid: UUID, kind: str, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    cid = current_user["company_id"]
    emp = _get(db, cid, uuid)
    full = _section_access(db, current_user, emp, kind, write=False)
    return details.out_rows(db, cid, kind, details.list_rows(db, cid, emp.id, kind), full_bank=full)


@router.post("/{uuid}/details/{kind}", status_code=201)
def add_section_row(uuid: UUID, kind: str, body: dict, db: Session = Depends(get_db),
                    current_user: dict = Depends(get_current_user)):
    cid = current_user["company_id"]
    emp = _get(db, cid, uuid)
    full = _section_access(db, current_user, emp, kind, write=True)
    row = details.save_row(db, cid, emp.id, kind, _section_body(kind, body), can_verify=full)
    db.commit()
    return details.out_rows(db, cid, kind, [row], full_bank=full)[0]


@router.put("/{uuid}/details/{kind}/{item}")
def update_section_row(uuid: UUID, kind: str, item: UUID, body: dict, db: Session = Depends(get_db),
                       current_user: dict = Depends(get_current_user)):
    cid = current_user["company_id"]
    emp = _get(db, cid, uuid)
    full = _section_access(db, current_user, emp, kind, write=True)
    row = details.get_row(db, cid, emp.id, kind, item)
    details.save_row(db, cid, emp.id, kind, _section_body(kind, body, partial=True), row, can_verify=full)
    db.commit()
    return details.out_rows(db, cid, kind, [row], full_bank=full)[0]


@router.delete("/{uuid}/details/{kind}/{item}")
def remove_section_row(uuid: UUID, kind: str, item: UUID, db: Session = Depends(get_db),
                       current_user: dict = Depends(get_current_user)):
    cid = current_user["company_id"]
    emp = _get(db, cid, uuid)
    _section_access(db, current_user, emp, kind, write=True)
    row = details.get_row(db, cid, emp.id, kind, item)
    details.remove_row(db, cid, emp.id, kind, row)
    db.commit()
    return {"detail": "Removed"}


SECTION_SCHEMAS = {"emergency-contacts": sc.EmergencyContactIn, "family": sc.FamilyMemberIn, "nominees": sc.NomineeIn,
                   "bank-accounts": sc.BankAccountIn, "education": sc.EducationIn, "experience": sc.PreviousEmploymentIn}


def _section_body(kind: str, body: dict, partial: bool = False) -> dict:
    details.kind_or_404(kind)
    model = SECTION_SCHEMAS[kind].model_validate(body or {})
    return model.model_dump(exclude_unset=True)


# ── Documents of one employee ────────────────────────────────────────

def _doc_access(db: Session, user: dict, emp: Employee, write: bool = False) -> None:
    p = perms(db, user)
    if "people.employees.manage" in p or (not write and "people.employees.view" in p):
        return
    if user.get("employee_id") == emp.id:
        return
    raise Forbidden()


@router.get("/{uuid}/documents")
def employee_documents(uuid: UUID, include_archived: bool = False, db: Session = Depends(get_db),
                       current_user: dict = Depends(get_current_user)):
    cid = current_user["company_id"]
    emp = _get(db, cid, uuid)
    _doc_access(db, current_user, emp)
    q = db.query(EmployeeDocument).filter(EmployeeDocument.company_id == cid, EmployeeDocument.employee_id == emp.id)
    if not include_archived:
        q = q.filter(EmployeeDocument.archived_at.is_(None))
    if current_user.get("employee_id") == emp.id and "people.employees.manage" not in perms(db, current_user):
        q = q.filter(EmployeeDocument.visible_to_employee.is_(True))
    return documents.out(db, cid, q.order_by(EmployeeDocument.created_at.desc()).all())


@router.post("/{uuid}/documents", status_code=201)
async def add_document(uuid: UUID, document_type_id: int = Form(...), title: str | None = Form(None),
                       document_number: str | None = Form(None), issue_date: str | None = Form(None),
                       expiry_date: str | None = Form(None), issued_by: str | None = Form(None),
                       remind_days_before: int | None = Form(None), visible_to_employee: bool = Form(True),
                       file: UploadFile | None = File(None), db: Session = Depends(get_db),
                       current_user: dict = Depends(get_current_user)):
    """Add a document, with or without its file (multipart form). Employees can add their own."""
    from datetime import date as _date
    cid = current_user["company_id"]
    emp = _get(db, cid, uuid)
    _doc_access(db, current_user, emp, write=True)
    is_hr = "people.employees.manage" in perms(db, current_user)

    def d(x):
        try:
            return _date.fromisoformat(x) if x else None
        except ValueError:
            raise BadRequest("Dates should be in the form YYYY-MM-DD.")
    data = sc.DocumentIn(document_type_id=document_type_id, title=title, document_number=document_number,
                         issue_date=d(issue_date), expiry_date=d(expiry_date), issued_by=issued_by,
                         remind_days_before=remind_days_before,
                         visible_to_employee=visible_to_employee if is_hr else True).model_dump(exclude_unset=True)
    row = documents.save(db, cid, emp.id, data)
    if file is not None and file.filename:
        stored = documents.store_file(db, cid, emp.id, file.filename, file.content_type, await file.read())
        documents.attach_file(db, row, stored)
    db.commit()
    db.refresh(row)
    return documents.out(db, cid, [row])[0]


@router.put("/{uuid}/documents/{doc}")
def update_document(uuid: UUID, doc: UUID, body: sc.DocumentIn, db: Session = Depends(get_db),
                    current_user: dict = Depends(get_current_user)):
    cid = current_user["company_id"]
    emp = _get(db, cid, uuid)
    _doc_access(db, current_user, emp, write=True)
    row = documents.get(db, cid, emp.id, doc)
    is_hr = "people.employees.manage" in perms(db, current_user)
    if not is_hr and row.verification_status == "verified":
        raise BadRequest("This document is verified. Ask HR to change it.")
    data = body.model_dump(exclude_unset=True)
    if not is_hr:
        data.pop("visible_to_employee", None)
    documents.save(db, cid, emp.id, data, row)
    db.commit()
    return documents.out(db, cid, [row])[0]


@router.post("/{uuid}/documents/{doc}/file")
async def upload_document_file(uuid: UUID, doc: UUID, file: UploadFile = File(...), db: Session = Depends(get_db),
                               current_user: dict = Depends(get_current_user)):
    cid = current_user["company_id"]
    emp = _get(db, cid, uuid)
    _doc_access(db, current_user, emp, write=True)
    row = documents.get(db, cid, emp.id, doc)
    stored = documents.store_file(db, cid, emp.id, file.filename, file.content_type, await file.read())
    documents.attach_file(db, row, stored)
    db.commit()
    db.refresh(row)
    return documents.out(db, cid, [row])[0]


@router.get("/{uuid}/documents/{doc}/file")
def download_document_file(uuid: UUID, doc: UUID, db: Session = Depends(get_db),
                           current_user: dict = Depends(get_current_user)):
    cid = current_user["company_id"]
    emp = _get(db, cid, uuid)
    _doc_access(db, current_user, emp)
    row = documents.get(db, cid, emp.id, doc)
    if row.file is None:
        raise NotFound("File")
    audit.event(db, "document.downloaded", "employee_documents", row.id, entity_uuid=str(row.uuid),
                entity_label=row.file.filename)
    db.commit()
    return StreamingResponse(io.BytesIO(row.file.data), media_type=row.file.content_type,
                             headers={"Content-Disposition": f'inline; filename="{row.file.filename}"'})


@router.post("/{uuid}/documents/{doc}/verify")
def verify_document(uuid: UUID, doc: UUID, body: sc.DocumentVerify, db: Session = Depends(get_db),
                    current_user: dict = Depends(VERIFY)):
    cid = current_user["company_id"]
    emp = _get(db, cid, uuid)
    row = documents.get(db, cid, emp.id, doc)
    documents.verify(db, row, body.status, body.remarks)
    db.commit()
    return documents.out(db, cid, [row])[0]


@router.post("/{uuid}/documents/{doc}/archive")
def archive_document(uuid: UUID, doc: UUID, db: Session = Depends(get_db), current_user: dict = Depends(MANAGE)):
    cid = current_user["company_id"]
    emp = _get(db, cid, uuid)
    documents.archive(documents.get(db, cid, emp.id, doc))
    db.commit()
    return {"detail": "Document archived"}


# ── Skills of one employee ───────────────────────────────────────────

def _skill_access(db: Session, user: dict, emp: Employee) -> bool:
    """Returns whether the viewer may verify skills."""
    p = perms(db, user)
    if "people.employees.manage" in p or "people.skills.manage" in p:
        return True
    if user.get("employee_id") == emp.id:
        return False
    if emp.reporting_manager_id and user.get("employee_id") == emp.reporting_manager_id:
        return True  # managers confirm their team's skills
    raise Forbidden()


@router.get("/{uuid}/skills")
def employee_skills(uuid: UUID, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    from modules.organization import service as org
    cid = current_user["company_id"]
    return org.skills_of(db, cid, _get(db, cid, uuid).id)


@router.post("/{uuid}/skills", status_code=201)
def add_employee_skill(uuid: UUID, body: dict, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    from modules.organization import service as org
    from modules.organization.schemas import EmployeeSkillIn
    cid = current_user["company_id"]
    emp = _get(db, cid, uuid)
    can_verify = _skill_access(db, current_user, emp)
    org.save_employee_skill(db, cid, emp.id, EmployeeSkillIn.model_validate(body).model_dump(exclude_unset=True),
                            can_verify=can_verify)
    db.commit()
    return org.skills_of(db, cid, emp.id)


@router.put("/{uuid}/skills/{item}")
def update_employee_skill(uuid: UUID, item: UUID, body: dict, db: Session = Depends(get_db),
                          current_user: dict = Depends(get_current_user)):
    from modules.organization import service as org
    from modules.organization.models import EmployeeSkill
    from modules.organization.schemas import EmployeeSkillIn
    cid = current_user["company_id"]
    emp = _get(db, cid, uuid)
    can_verify = _skill_access(db, current_user, emp)
    row = db.query(EmployeeSkill).filter(EmployeeSkill.company_id == cid, EmployeeSkill.employee_id == emp.id,
                                         EmployeeSkill.uuid == item).first()
    if row is None:
        raise NotFound("Skill")
    org.save_employee_skill(db, cid, emp.id, EmployeeSkillIn.model_validate(body).model_dump(exclude_unset=True), row,
                            can_verify=can_verify)
    db.commit()
    return org.skills_of(db, cid, emp.id)


@router.delete("/{uuid}/skills/{item}")
def remove_employee_skill(uuid: UUID, item: UUID, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    from modules.organization import service as org
    from modules.organization.models import EmployeeSkill
    cid = current_user["company_id"]
    emp = _get(db, cid, uuid)
    _skill_access(db, current_user, emp)
    row = db.query(EmployeeSkill).filter(EmployeeSkill.company_id == cid, EmployeeSkill.employee_id == emp.id,
                                         EmployeeSkill.uuid == item).first()
    if row is None:
        raise NotFound("Skill")
    db.delete(row)
    db.commit()
    return org.skills_of(db, cid, emp.id)


# ── Photo and work arrangement ───────────────────────────────────────

@router.post("/{uuid}/photo")
async def upload_photo(uuid: UUID, file: UploadFile = File(...), db: Session = Depends(get_db),
                       current_user: dict = Depends(MANAGE)):
    """Profile photo, stored as a small square JPEG data URL."""
    emp = _get(db, current_user["company_id"], uuid)
    emp.profile_image_url = await _photo(file)
    db.commit()
    return {"profile_image_url": emp.profile_image_url}


async def _photo(file: UploadFile) -> str:
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image files are allowed")
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="Image must be under 5MB")
    try:
        return to_square_data_url(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="Could not read that image")


@router.delete("/{uuid}/photo")
def remove_photo(uuid: UUID, db: Session = Depends(get_db), current_user: dict = Depends(MANAGE)):
    emp = _get(db, current_user["company_id"], uuid)
    emp.profile_image_url = None
    db.commit()
    return {"detail": "Photo removed"}


@router.get("/{uuid}/work-arrangement")
def get_work_arrangement(uuid: UUID, db: Session = Depends(get_db), current_user: dict = Depends(VIEW)):
    emp = _get(db, current_user["company_id"], uuid)
    return {"config": emp.settings.config if emp.settings else DEFAULT_WORK_ARRANGEMENT}


@router.put("/{uuid}/work-arrangement")
def update_work_arrangement(uuid: UUID, payload: WorkArrangementUpdate, db: Session = Depends(get_db),
                            current_user: dict = Depends(MANAGE)):
    """Working days and usual shift timings."""
    emp = _get(db, current_user["company_id"], uuid)
    existing = emp.settings.config if emp.settings and emp.settings.config else {}
    config = normalize_work_arrangement(payload.config, existing)
    if emp.settings:
        emp.settings.config = config
        flag_modified(emp.settings, "config")
    else:
        db.add(EmployeeSettings(company_id=emp.company_id, employee_id=emp.id, config=config))
    db.commit()
    return {"detail": "Work arrangement updated", "config": config}


# ── Self service ─────────────────────────────────────────────────────

me_router = APIRouter(prefix="/me", tags=["Self service"])


class ProfileChangeRequest(BaseModel):
    changes: dict = Field(..., description="Fields to change (see hooks.PROFILE_FIELDS), custom, bank_account")
    reason: str | None = Field(None, max_length=1000)


def _me(db: Session, user: dict) -> Employee:
    if not user.get("employee_id"):
        raise HTTPException(status_code=404, detail="The company owner has no employee record.")
    emp = service.get_by_id(db, user["company_id"], user["employee_id"])
    if emp is None:
        raise NotFound("Employee")
    return service.get(db, user["company_id"], emp.uuid)


@me_router.get("/profile")
def my_profile(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    """The signed-in employee's own record (no people.employees.view permission needed)."""
    return _response(_me(db, current_user))


@me_router.get("/profile/full")
def my_full_profile(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    return _profile(db, current_user, _me(db, current_user))


@me_router.put("/profile/personal")
def update_my_details(body: sc.SelfServiceUpdate, db: Session = Depends(get_db),
                      current_user: dict = Depends(get_current_user)):
    """Details an employee may change without approval (preferred name, personal email,
    alternate phone, blood group, addresses). Recorded in the audit log."""
    emp = _me(db, current_user)
    service.update(db, current_user["company_id"], emp, body.model_dump(exclude_unset=True))
    db.commit()
    return _profile(db, current_user, _me(db, current_user))


@me_router.post("/profile/photo")
async def upload_my_photo(file: UploadFile = File(...), db: Session = Depends(get_db),
                          current_user: dict = Depends(get_current_user)):
    emp = _me(db, current_user)
    emp.profile_image_url = await _photo(file)
    db.commit()
    return {"profile_image_url": emp.profile_image_url}


@me_router.post("/profile-change", status_code=status.HTTP_201_CREATED)
def request_profile_change(req: ProfileChangeRequest, db: Session = Depends(get_db),
                           current_user: dict = Depends(get_current_user)):
    """An employee asks to change sensitive details; HR approves before it applies."""
    from core.workflow import service as workflow
    if not current_user.get("employee_id"):
        raise HTTPException(status_code=400, detail="Only employees can send a change request. Edit the details directly.")
    emp = service.get_by_id(db, current_user["company_id"], current_user["employee_id"])
    instance = workflow.submit(
        db, current_user["company_id"], "employee.profile_change",
        {"employee_id": emp.id, "changes": req.changes, "reason": (req.reason or "").strip() or None},
        f"Change of personal details: {service.display_name(emp)}",
    )
    db.commit()
    return {"uuid": str(instance.uuid), "reference_no": instance.reference_no, "status": instance.status}


extra_routers = [me_router]
