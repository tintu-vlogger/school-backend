"""Company files, folders, personal lockers and policies (with acknowledgements).
Letters, templates and e-signatures are in letters_router.py."""
import io
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session, undefer

from core.db import bypass_rls, get_db, set_tenant
from core.errors import BadRequest, Forbidden, NotFound
from core.pagination import PageParams, paginate
from core.security import get_current_user, require_permission
from modules.documents import schemas, service, storage
from modules.documents import settings as doc_settings
from modules.documents.models import DocFile, DocFileVersion, DocFolder, FileBlob, Policy, PolicyVersion

router = APIRouter(prefix="/documents", tags=["Files and policies"])
MANAGE = "workplace.files.manage"


def _manager(db: Session, user: dict) -> bool:
    return service.has_perm(db, user, MANAGE)


def _require_manager(db: Session, user: dict) -> None:
    if not _manager(db, user):
        raise Forbidden("Only people who manage company files can do this.")


# ── Uploads and file bytes ──────────────────────────────────────────

@router.post("/uploads", status_code=status.HTTP_201_CREATED)
async def upload(file: UploadFile = File(...), db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Upload a file (up to 10 MB). Returns its id to attach to a case, task, policy or folder."""
    data = await file.read()
    blob = storage.store(db, user["company_id"], file.filename or "file", data, file.content_type, "upload")
    db.commit()
    return storage.describe(blob)


@router.get("/blob/{company_id}/{uuid}")
def blob(company_id: int, uuid: UUID, exp: int, sig: str, dl: int = 0, db: Session = Depends(get_db)):
    """File bytes behind a signed, short-lived link (no sign-in token needed)."""
    if not storage.check_signature(company_id, uuid, exp, sig):
        raise Forbidden("This link has expired. Open the file again from the app.")
    set_tenant(db, company_id)
    row = storage.get(db, company_id, uuid, with_data=True)
    disposition = "attachment" if dl else "inline"
    safe = row.filename.replace('"', "")
    return Response(content=row.data, media_type=row.content_type,
                    headers={"Content-Disposition": f'{disposition}; filename="{safe}"',
                             "Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"})


# ── Settings ────────────────────────────────────────────────────────

@router.get("/settings")
def get_settings_(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    return doc_settings.get(db, user["company_id"])


@router.put("/settings")
def put_settings(data: dict, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    out = doc_settings.update(db, user["company_id"], data)
    db.commit()
    return out


# ── Folders ─────────────────────────────────────────────────────────

def _folder_out(db: Session, f: DocFolder, counts: dict, dept_names: dict) -> dict:
    return {"uuid": str(f.uuid), "id": f.id, "name": f.name, "area": f.area, "area_label": service.AREAS.get(f.area),
            "description": f.description, "department_id": f.department_id,
            "department_name": dept_names.get(f.department_id), "is_system": f.is_system,
            "file_count": counts.get(f.id, 0)}


@router.get("/folders")
def list_folders(area: str | None = None, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    manager = _manager(db, user)
    folders = service.list_folders(db, user, manager, area)
    counts = dict(db.query(DocFile.folder_id, func.count(DocFile.id))
                  .filter(DocFile.company_id == user["company_id"], DocFile.archived_at.is_(None),
                          DocFile.folder_id.in_([f.id for f in folders] or [0]))
                  .group_by(DocFile.folder_id).all())
    from modules.departments.models import Department
    dept_ids = [f.department_id for f in folders if f.department_id]
    dept_names = {d.id: d.department_name for d in db.query(Department).filter(Department.id.in_(dept_ids or [0]))}
    db.commit()
    return {"items": [_folder_out(db, f, counts, dept_names) for f in folders], "can_manage": manager,
            "areas": service.AREAS}


@router.post("/folders", status_code=status.HTTP_201_CREATED)
def create_folder(body: schemas.FolderIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    manager = _manager(db, user)
    if not manager:
        me = service.employee(db, user["company_id"], user.get("employee_id"))
        if not (body.area == "department" and service._is_dept_head(db, user["company_id"], me, body.department_id)
                and doc_settings.get(db, user["company_id"]).get("department_heads_can_share", True)):
            raise Forbidden("Only HR or the department head can add folders.")
    f = service.save_folder(db, user, body.model_dump())
    db.commit()
    return _folder_out(db, f, {}, {})


@router.put("/folders/{uuid}")
def update_folder(uuid: UUID, body: schemas.FolderIn, db: Session = Depends(get_db),
                  user: dict = Depends(require_permission(MANAGE))):
    f = service.get_folder(db, user["company_id"], uuid)
    if f.area == "personal":
        raise Forbidden("Personal lockers can't be changed.")
    service.save_folder(db, user, body.model_dump(), f)
    db.commit()
    return _folder_out(db, f, {}, {})


@router.delete("/folders/{uuid}")
def remove_folder(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    f = service.get_folder(db, user["company_id"], uuid)
    service.archive_folder(db, f)
    db.commit()
    return {"detail": "Folder removed"}


# ── Files ───────────────────────────────────────────────────────────

def _file_out(db: Session, f: DocFile, names: dict | None = None, detail: bool = False) -> dict:
    names = names or {}
    out = {"uuid": str(f.uuid), "title": f.title, "description": f.description, "version": f.version,
           "folder_uuid": str(f.folder.uuid), "folder_name": f.folder.name, "area": f.folder.area,
           "employee_id": f.employee_id, "employee_name": names.get(f.employee_id), "audience": f.audience,
           "visible_to_employee": f.visible_to_employee, "allow_download": f.allow_download,
           "expires_on": f.expires_on, "source": f.source, "tags": f.tags or [],
           "uploaded_by_name": f.uploaded_by_name, "archived": f.archived_at is not None,
           "created_at": f.created_at, "updated_at": f.updated_at,
           "file": storage.describe(f.stored) if f.stored else None}
    if out["file"] and not f.allow_download:
        out["file"]["download_url"] = None
    if detail:
        versions = (db.query(DocFileVersion).filter(DocFileVersion.doc_file_id == f.id)
                    .order_by(DocFileVersion.version.desc()).all())
        out["versions"] = [{"version": v.version, "note": v.note, "created_by_name": v.created_by_name,
                            "created_at": v.created_at, "file": storage.describe(v.stored)} for v in versions]
    return out


@router.get("/files")
def list_files(folder: UUID | None = None, q: str | None = None, employee_id: int | None = None,
               archived: bool = False, expiring_days: int | None = Query(None, ge=0, le=365),
               params: PageParams = Depends(), db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    cid = user["company_id"]
    manager = _manager(db, user)
    folder_row = service.get_folder(db, cid, folder) if folder else None
    if folder_row is not None and not service.can_see_folder(db, user, folder_row, manager):
        raise Forbidden("You don't have access to this folder.")
    if folder_row is None and not manager:
        raise Forbidden("Pick a folder.")
    query = service.files_query(db, user, manager, folder=folder_row, q=q, employee_id=employee_id, archived=archived,
                                expiring_days=expiring_days)
    if folder_row is not None and not manager and folder_row.area in ("company", "department"):
        visible = [f for f in query.all() if service.can_see_file(db, user, f, False)]
        page = {"items": visible[params.offset:params.offset + params.size], "total": len(visible),
                "page": params.page, "size": params.size}
    else:
        page = paginate(query, params)
    names = service.names_for(db, cid, [f.employee_id for f in page["items"]])
    page["items"] = [_file_out(db, f, names) for f in page["items"]]
    page["can_add"] = bool(folder_row and service.can_add_to_folder(db, user, folder_row, manager))
    return page


@router.post("/files", status_code=status.HTTP_201_CREATED)
def add_file(body: schemas.FileIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    cid = user["company_id"]
    folder = service.get_folder(db, cid, body.folder_uuid)
    blob = storage.get(db, cid, body.upload_uuid)
    data = body.model_dump()
    if body.audience is not None:
        data["audience"] = body.audience.model_dump()
    f = service.add_file(db, user, folder, blob, data, _manager(db, user))
    db.commit()
    return _file_out(db, service.get_file(db, cid, f.uuid))


def _visible_file(db: Session, user: dict, uuid: UUID) -> DocFile:
    f = service.get_file(db, user["company_id"], uuid)
    if not service.can_see_file(db, user, f, _manager(db, user)):
        raise NotFound("File")
    return f


@router.get("/files/{uuid}")
def get_file(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    f = _visible_file(db, user, uuid)
    names = service.names_for(db, user["company_id"], [f.employee_id])
    out = _file_out(db, f, names, detail=True)
    out["can_edit"] = service.can_add_to_folder(db, user, f.folder, _manager(db, user))
    return out


def _editable_file(db: Session, user: dict, uuid: UUID) -> DocFile:
    f = _visible_file(db, user, uuid)
    if not service.can_add_to_folder(db, user, f.folder, _manager(db, user)):
        raise Forbidden("You can't change this file.")
    return f


@router.put("/files/{uuid}")
def update_file(uuid: UUID, body: schemas.FileUpdate, db: Session = Depends(get_db),
                user: dict = Depends(get_current_user)):
    f = _editable_file(db, user, uuid)
    data = body.model_dump(exclude_unset=True)
    if body.audience is not None:
        data["audience"] = body.audience.model_dump()
    service.update_file(db, user, f, data)
    db.commit()
    return _file_out(db, service.get_file(db, user["company_id"], uuid), detail=True)


@router.post("/files/{uuid}/versions")
def add_version(uuid: UUID, body: schemas.VersionIn, db: Session = Depends(get_db),
                user: dict = Depends(get_current_user)):
    f = _editable_file(db, user, uuid)
    service.new_version(db, f, storage.get(db, user["company_id"], body.upload_uuid), body.note)
    db.commit()
    return _file_out(db, service.get_file(db, user["company_id"], uuid), detail=True)


@router.post("/files/{uuid}/restore")
def restore(uuid: UUID, body: schemas.RestoreIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    f = _editable_file(db, user, uuid)
    service.restore_version(db, f, body.version)
    db.commit()
    return _file_out(db, service.get_file(db, user["company_id"], uuid), detail=True)


@router.post("/files/{uuid}/archive")
def archive_file(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    f = _editable_file(db, user, uuid)
    f.archived_at = service._now()
    db.commit()
    return {"detail": "File archived"}


@router.post("/files/{uuid}/unarchive")
def unarchive_file(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    f = _editable_file(db, user, uuid)
    f.archived_at = None
    db.commit()
    return {"detail": "File restored"}


@router.get("/lockers")
def lockers(db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    """How many files and how much space each personal locker uses (never the files)."""
    return {"items": service.locker_usage(db, user["company_id"]),
            "limit": doc_settings.get(db, user["company_id"])["personal_file_limit"]}


@router.get("/me/files")
def my_files(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """The signed-in employee's own files from HR (letters, signed documents, records)."""
    cid = user["company_id"]
    me = service.employee(db, cid, user.get("employee_id"))
    if me is None:
        return {"items": []}
    rows = (service.files_query(db, user, True, employee_id=me.id)
            .filter(DocFolder.area == "hr", DocFile.visible_to_employee.is_(True)).limit(200).all())
    return {"items": [_file_out(db, f) for f in rows]}


# ── Policies ────────────────────────────────────────────────────────

def _policy_out(db: Session, p: Policy, counts: dict | None = None, labels: dict | None = None) -> dict:
    v = p.current_version
    return {"uuid": str(p.uuid), "title": p.title, "category_id": p.category_id,
            "category_name": (labels or {}).get(p.category_id), "description": p.description,
            "audience": p.audience, "requires_ack": p.requires_ack, "ack_due_days": p.ack_due_days,
            "reminder_every_days": p.reminder_every_days, "is_posh": p.is_posh, "review_on": p.review_on,
            "owner_name": p.owner_name, "active": p.active, "created_at": p.created_at, "updated_at": p.updated_at,
            "current_version": _version_out(v) if v else None,
            "stats": (counts or {}).get(p.current_version_id) or {"assigned": 0, "acknowledged": 0, "pending": 0, "overdue": 0}}


def _version_out(v: PolicyVersion) -> dict:
    return {"uuid": str(v.uuid), "version": v.version, "change_note": v.change_note, "effective_from": v.effective_from,
            "ack_due_on": v.ack_due_on, "published_at": v.published_at, "published_by_name": v.published_by_name,
            "body_html": v.body_html, "file": storage.describe(v.stored) if v.stored else None}


@router.get("/policies")
def list_policies(q: str | None = None, active: bool | None = None, db: Session = Depends(get_db),
                  user: dict = Depends(require_permission(MANAGE))):
    cid = user["company_id"]
    query = db.query(Policy).filter(Policy.company_id == cid)
    if q:
        query = query.filter(Policy.title.ilike(f"%{q.strip()}%"))
    if active is not None:
        query = query.filter(Policy.active.is_(active))
    rows = query.order_by(Policy.title).all()
    counts = service.ack_counts(db, cid, [p.current_version_id for p in rows if p.current_version_id])
    from core.masters import labels_for
    labels = labels_for(db, cid, [p.category_id for p in rows if p.category_id])
    return {"items": [_policy_out(db, p, counts, labels) for p in rows]}


@router.get("/policies/summary")
def policy_summary(posh_only: bool = False, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    return service.policy_ack_summary(db, user["company_id"], posh_only)


@router.post("/policies", status_code=status.HTTP_201_CREATED)
def create_policy(body: schemas.PolicyIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    data = body.model_dump()
    conf = doc_settings.get(db, user["company_id"])
    data["ack_due_days"] = data.get("ack_due_days") or conf["default_ack_days"]
    data["reminder_every_days"] = data.get("reminder_every_days") or conf["default_reminder_days"]
    data["audience"] = body.audience.model_dump() if body.audience else {"all": True}
    p = service.save_policy(db, user["company_id"], data)
    db.commit()
    return _policy_out(db, service.get_policy(db, user["company_id"], p.uuid))


@router.get("/policies/{uuid}")
def get_policy(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    cid = user["company_id"]
    p = service.get_policy(db, cid, uuid)
    manager = _manager(db, user)
    me = service.employee(db, cid, user.get("employee_id"))
    if not manager and (not p.active or not service._audience_match(p.audience, me)):
        raise NotFound("Policy")
    counts = service.ack_counts(db, cid, [p.current_version_id]) if manager and p.current_version_id else {}
    out = _policy_out(db, p, counts)
    if manager:
        out["versions"] = [_version_out(v) for v in db.query(PolicyVersion).filter(PolicyVersion.policy_id == p.id)
                           .order_by(PolicyVersion.version.desc()).all()]
    return out


@router.put("/policies/{uuid}")
def update_policy(uuid: UUID, body: schemas.PolicyIn, db: Session = Depends(get_db),
                  user: dict = Depends(require_permission(MANAGE))):
    p = service.get_policy(db, user["company_id"], uuid)
    data = body.model_dump(exclude_unset=True)
    if body.audience is not None:
        data["audience"] = body.audience.model_dump()
    service.save_policy(db, user["company_id"], data, p)
    if p.current_version_id and p.requires_ack and p.active:
        service.assign_acknowledgements(db, user["company_id"], p, p.current_version)
    db.commit()
    return _policy_out(db, service.get_policy(db, user["company_id"], uuid))


@router.post("/policies/{uuid}/versions", status_code=status.HTTP_201_CREATED)
def publish(uuid: UUID, body: schemas.PolicyVersionIn, db: Session = Depends(get_db),
            user: dict = Depends(require_permission(MANAGE))):
    cid = user["company_id"]
    p = service.get_policy(db, cid, uuid)
    blob = storage.get(db, cid, body.upload_uuid) if body.upload_uuid else None
    v = service.publish_version(db, cid, p, blob=blob, body_html=body.body_html, effective_from=body.effective_from,
                                change_note=body.change_note, ask_again=body.ask_again)
    db.commit()
    return _version_out(v)


@router.get("/policies/{uuid}/acknowledgements")
def acknowledgements(uuid: UUID, status_: str | None = Query(None, alias="status"), format: str | None = None,
                     db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = user["company_id"]
    p = service.get_policy(db, cid, uuid)
    rows = service.ack_report(db, cid, p, status_)
    if format == "csv":
        from core import audit
        from core import csv_safe
        buf = io.StringIO()
        w = csv_safe.writer(buf)
        w.writerow(["Employee ID", "Name", "Status", "Due on", "Acknowledged at (UTC)", "Reminders"])
        for r in rows:
            w.writerow([r["employee_code"] or "", r["employee_name"], r["status"], r["due_on"] or "",
                        r["acknowledged_at"].strftime("%Y-%m-%d %H:%M") if r["acknowledged_at"] else "", r["reminder_count"]])
        audit.event(db, "export.downloaded", "policies", p.id, {"report": "acknowledgements"}, entity_uuid=str(p.uuid),
                    entity_label=p.title)
        db.commit()
        return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="acknowledgements.csv"'})
    return {"items": rows, "policy": {"uuid": str(p.uuid), "title": p.title}}


@router.post("/policies/{uuid}/remind")
def remind(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    p = service.get_policy(db, user["company_id"], uuid)
    n = service.remind_pending(db, user["company_id"], p)
    db.commit()
    return {"reminded": n}


@router.get("/me/policies")
def my_policies(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    rows = service.my_policies(db, user)
    db.commit()
    return {"items": [{**_policy_out(db, r["policy"]), "stats": None, "my_status": r["status"],
                       "due_on": r["ack"].due_on if r["ack"] else None,
                       "acknowledged_at": r["ack"].acknowledged_at if r["ack"] else None} for r in rows]}


@router.get("/me/pending-count")
def my_pending(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    if not user.get("employee_id"):
        return {"policies": 0, "signatures": 0}
    from modules.documents.models import EsignSigner
    sig = db.query(func.count(EsignSigner.id)).filter(EsignSigner.company_id == user["company_id"],
                                                      EsignSigner.employee_id == user["employee_id"],
                                                      EsignSigner.status == "pending").scalar()
    return {"policies": service.pending_acknowledgements(db, user["company_id"], user["employee_id"]), "signatures": sig}


@router.post("/policies/{uuid}/acknowledge")
def acknowledge(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    p = service.get_policy(db, user["company_id"], uuid)
    a = service.acknowledge(db, user, p)
    db.commit()
    return {"acknowledged_at": a.acknowledged_at}


from modules.documents.letters_router import public_router, router as letters_router  # noqa: E402

extra_routers = [letters_router, public_router]
