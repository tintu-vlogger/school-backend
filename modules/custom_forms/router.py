"""Custom services API: services and their forms (designer), templates, the menu, records
(submit, edit, statuses, comments, withdraw), lists with saved views, exports and lookups."""
from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from core.db import get_db
from core.errors import BadRequest, Forbidden, NotFound
from core.pagination import PageParams, paginate
from core.security import get_current_user
from core.timeutil import iso_utc
from modules.custom_forms import fields as F
from modules.custom_forms import schemas as sc
from modules.custom_forms import service as svc
from modules.custom_forms import templates as T
from modules.custom_forms.models import CustomFormRecord, CustomFormView, CustomService

router = APIRouter(prefix="/custom-forms", tags=["Custom services"])


def _viewer(db: Session = Depends(get_db), user: dict = Depends(get_current_user)) -> svc.Viewer:
    return svc.Viewer(db, user)


def _designer(v: svc.Viewer = Depends(_viewer)) -> svc.Viewer:
    if not v.can_design:
        raise Forbidden("Permission denied: settings.services.manage")
    return v


# ── Setup ───────────────────────────────────────────────────────────

@router.get("/meta")
def meta(db: Session = Depends(get_db), v: svc.Viewer = Depends(_viewer)):
    from modules.roles.models import Role
    roles = [{"value": r.role_name, "label": r.display_name}
             for r in db.query(Role).filter(Role.company_id == v.company_id).order_by(Role.id).all()]
    installed = {k for (k,) in db.query(CustomService.template_key).filter(
        CustomService.company_id == v.company_id, CustomService.template_key.isnot(None)).all()}
    return {
        "field_types": [{"value": k, "label": l} for k, l in F.FIELD_TYPES.items()],
        "field_groups": [{"label": g, "types": ts} for g, ts in F.GROUPS],
        "table_column_types": sorted(F.TABLE_COLUMN_TYPES),
        "unique_types": sorted(F.UNIQUE_TYPES),
        "formula_results": [{"value": k, "label": l} for k, l in F.FORMULA_RESULTS.items()],
        "requester_variables": [{"name": k, "label": l} for k, l in F.REQUESTER_VARS.items()],
        "icons": svc.ICONS,
        "nav_sections": [{"value": k, "label": l} for k, l in svc.NAV_SECTIONS.items()],
        "tones": list(svc.TONES),
        "roles": roles,
        "templates": [{"key": t["key"], "name": t["name"], "description": t["description"], "icon": t["icon"],
                       "needs_approval": t["needs_approval"], "fields": len(F.input_fields({"fields": t["fields"]})),
                       "installed": t["key"] in installed} for t in T.TEMPLATES],
        "can_design": v.can_design,
        "handles_all": v.handles_all,
    }


def _service_brief(s: CustomService, counts: dict | None = None) -> dict:
    c = (counts or {}).get(s.id, {})
    return {"uuid": str(s.uuid), "code": s.code, "name": s.name, "description": s.description, "icon": s.icon,
            "nav_section": s.nav_section, "active": s.active, "archived": s.archived_at is not None,
            "needs_approval": s.needs_approval, "template_key": s.template_key, "version": s.current_version,
            "records": c.get("total", 0), "pending": c.get("pending", 0), "updated_at": iso_utc(s.updated_at)}


def _service_full(db: Session, v: svc.Viewer, s: CustomService) -> dict:
    ver = svc.current_version(db, s)
    out = _service_brief(s, svc.record_counts(db, v.company_id))
    names = svc.resolve_labels(db, v.company_id, [({"fields": [{"key": "m", "type": "employee", "label": "m",
                                                                 "multiple": True}]},
                                                    {"m": list(s.manager_employee_ids or [])})])
    out.update({
        "allow_drafts": s.allow_drafts, "allow_cancel": s.allow_cancel, "submit_access": s.submit_access,
        "manager_roles": s.manager_roles or [], "manager_employee_ids": s.manager_employee_ids or [],
        "managers": [{"value": i, "label": names.get(f"employee:{i}", f"#{i}")} for i in s.manager_employee_ids or []],
        "statuses": s.statuses or [], "all_statuses": svc.all_statuses(s), "notify": s.notify or {},
        "title_field": s.title_field, "definition": ver.definition if ver else {"fields": []},
        "request_type": svc.request_type_key(s.company_id, s.code), "flows": svc.flow_count(db, s),
        "series": svc.series_info(db, s), "created_by_name": s.created_by_name, "created_at": iso_utc(s.created_at),
        "archived_by_name": s.archived_by_name,
    })
    return out


@router.get("/services")
def list_services(include_archived: bool = False, db: Session = Depends(get_db), v: svc.Viewer = Depends(_designer)):
    q = db.query(CustomService).filter(CustomService.company_id == v.company_id)
    if not include_archived:
        q = q.filter(CustomService.archived_at.is_(None))
    counts = svc.record_counts(db, v.company_id)
    return [_service_brief(s, counts) for s in q.order_by(CustomService.sort_order, CustomService.name).all()]


@router.post("/services", status_code=201)
def create_service(body: sc.ServiceCreate, db: Session = Depends(get_db), v: svc.Viewer = Depends(_designer)):
    data = body.model_dump(exclude_unset=True)
    fields = data.pop("fields", None)
    s = svc.create_service(db, v.company_id, data, definition={"fields": fields} if fields else None)
    db.commit()
    return _service_full(db, v, s)


@router.get("/services/{code}")
def get_service(code: str, db: Session = Depends(get_db), v: svc.Viewer = Depends(_designer)):
    return _service_full(db, v, svc.get_service(db, v.company_id, code))


@router.put("/services/{code}")
def update_service(code: str, body: sc.ServiceIn, db: Session = Depends(get_db), v: svc.Viewer = Depends(_designer)):
    s = svc.get_service(db, v.company_id, code)
    svc.update_service(db, s, body.model_dump(exclude_unset=True))
    db.commit()
    return _service_full(db, v, s)


@router.put("/services/{code}/form")
def save_form(code: str, body: sc.FormIn, db: Session = Depends(get_db), v: svc.Viewer = Depends(_designer)):
    s = svc.get_service(db, v.company_id, code)
    svc.save_form(db, s, {"fields": body.fields}, body.change_note)
    db.commit()
    return _service_full(db, v, s)


@router.get("/services/{code}/versions")
def versions(code: str, db: Session = Depends(get_db), v: svc.Viewer = Depends(_designer)):
    from sqlalchemy import func
    from modules.custom_forms.models import CustomFormVersion
    s = svc.get_service(db, v.company_id, code)
    used = dict(db.query(CustomFormRecord.version_id, func.count(CustomFormRecord.id)).filter(
        CustomFormRecord.company_id == v.company_id, CustomFormRecord.service_id == s.id)
        .group_by(CustomFormRecord.version_id).all())
    rows = db.query(CustomFormVersion).filter(CustomFormVersion.company_id == v.company_id,
                                              CustomFormVersion.service_id == s.id).order_by(
        CustomFormVersion.version.desc()).all()
    return [{"uuid": str(r.uuid), "version": r.version, "current": r.version == s.current_version,
             "fields": len(F.input_fields(r.definition)), "records": used.get(r.id, 0), "change_note": r.change_note,
             "created_by_name": r.created_by_name, "created_at": iso_utc(r.created_at), "definition": r.definition}
            for r in rows]


@router.post("/services/{code}/copy", status_code=201)
def copy_service(code: str, body: sc.CopyIn, db: Session = Depends(get_db), v: svc.Viewer = Depends(_designer)):
    s = svc.copy_service(db, svc.get_service(db, v.company_id, code), body.name)
    db.commit()
    return _service_full(db, v, s)


@router.post("/services/{code}/archive")
def archive(code: str, db: Session = Depends(get_db), v: svc.Viewer = Depends(_designer)):
    s = svc.archive_service(db, svc.get_service(db, v.company_id, code), True)
    db.commit()
    return _service_brief(s)


@router.post("/services/{code}/restore")
def restore(code: str, db: Session = Depends(get_db), v: svc.Viewer = Depends(_designer)):
    s = svc.archive_service(db, svc.get_service(db, v.company_id, code), False)
    db.commit()
    return _service_brief(s)


@router.post("/templates/{key}/install", status_code=201)
def install_template(key: str, db: Session = Depends(get_db), v: svc.Viewer = Depends(_designer)):
    s = svc.install_template(db, v.company_id, key)
    db.commit()
    return _service_full(db, v, s)


@router.post("/check-expression")
def check_expression(body: dict, v: svc.Viewer = Depends(_designer)):
    """Check a show-if condition or formula in the designer: {expression, fields: [keys]}."""
    from core import expressions
    allowed = {str(k) for k in body.get("fields") or []} | set(F.REQUESTER_VARS)
    try:
        used = expressions.validate(str(body.get("expression") or ""), allowed)
    except expressions.ExpressionError as e:
        return {"ok": False, "message": str(e)}
    return {"ok": True, "fields": used}


# ── Menu and catalogue ──────────────────────────────────────────────

@router.get("/nav")
def nav(db: Session = Depends(get_db), v: svc.Viewer = Depends(_viewer)):
    """Services this person may use, for the menu."""
    return [{"code": s.code, "name": s.name, "icon": s.icon, "nav_section": s.nav_section, "can_submit": sub,
             "can_handle": handle} for s, sub, handle in svc.usable_services(db, v)]


@router.get("/catalog")
def catalog(db: Session = Depends(get_db), v: svc.Viewer = Depends(_viewer)):
    from sqlalchemy import func
    usable = svc.usable_services(db, v)
    mine = db.query(CustomFormRecord.service_id, CustomFormRecord.status, func.count(CustomFormRecord.id)).filter(
        CustomFormRecord.company_id == v.company_id,
        (CustomFormRecord.requester_employee_id == v.employee_id) if v.employee_id
        else CustomFormRecord.requester_employee_id.is_(None)).group_by(
        CustomFormRecord.service_id, CustomFormRecord.status).all()
    counts = svc.record_counts(db, v.company_id)
    open_mine: dict[int, int] = {}
    drafts: dict[int, int] = {}
    by_id = {s.id: s for s, _, _ in usable}
    for sid, st, n in mine:
        s = by_id.get(sid)
        if s is None:
            continue
        if st == "draft":
            drafts[sid] = drafts.get(sid, 0) + n
        elif not svc.status_info(s, st)["closed"]:
            open_mine[sid] = open_mine.get(sid, 0) + n
    return [{**_service_brief(s, counts if handle else None), "can_submit": sub, "can_handle": handle,
             "my_open": open_mine.get(s.id, 0), "my_drafts": drafts.get(s.id, 0)} for s, sub, handle in usable]


@router.get("/services/{code}/definition")
def definition(code: str, db: Session = Depends(get_db), v: svc.Viewer = Depends(_viewer)):
    """The current form as this person sees it, with starting values, for the submit modal and list page."""
    s = svc.get_service(db, v.company_id, code)
    sub, handle = v.can_submit(s), v.can_handle(s)
    if not (sub or handle or v.can_design):
        raise NotFound("Service")
    ver = svc.current_version(db, s)
    from core.timeutil import today_for_company_id
    today = today_for_company_id(db, v.company_id)
    defaults = F.defaults_for(ver.definition, v.requester_vars(today))
    visible = F.visible_definition(ver.definition, v.roles, full=v.field_full())
    defaults = {k: val for k, val in defaults.items()
                if any(f.get("key") == k and f.get("access") == "edit" for f in visible["fields"])}
    labels = svc.resolve_labels(db, v.company_id, [(ver.definition, defaults)])
    return {"uuid": str(s.uuid), "code": s.code, "name": s.name, "description": s.description, "icon": s.icon,
            "nav_section": s.nav_section, "needs_approval": s.needs_approval, "allow_drafts": s.allow_drafts,
            "version": ver.version, "definition": visible, "defaults": defaults, "labels": labels,
            "statuses": svc.all_statuses(s), "work_statuses": s.statuses or [], "can_submit": sub, "can_handle": handle,
            "can_design": v.can_design, "active": s.active and s.archived_at is None}


@router.post("/services/{code}/evaluate")
def evaluate(code: str, body: sc.EvaluateIn, db: Session = Depends(get_db), v: svc.Viewer = Depends(_viewer)):
    s = svc.get_service(db, v.company_id, code)
    r = None
    if body.record_uuid:
        r = svc.get_record(db, v.company_id, body.record_uuid)
        svc.require_view(db, v, r)
    elif not (v.can_submit(s) or v.can_handle(s) or v.can_design):
        raise NotFound("Service")
    return svc.evaluate(db, v, s, body.values, r)


# ── Records ─────────────────────────────────────────────────────────

def _list_args(status: str | None, date_from: date | None, date_to: date | None, request: Request):
    statuses = [x for x in (status or "").split(",") if x]
    ff = {k[2:]: val for k, val in request.query_params.items() if k.startswith("f_")}
    return statuses, ff


@router.post("/services/{code}/records", status_code=201)
def create_record(code: str, body: sc.RecordIn, db: Session = Depends(get_db), v: svc.Viewer = Depends(_viewer)):
    s = svc.get_service(db, v.company_id, code)
    r = svc.create_record(db, v, s, body.values, draft=body.draft)
    db.commit()
    return svc.record_detail(db, v, r)


@router.get("/services/{code}/records")
def list_records(code: str, request: Request, scope: str = Query("mine", pattern="^(mine|all)$"),
                 status: str | None = None, q: str | None = None, date_from: date | None = None,
                 date_to: date | None = None, sort: str | None = None, page: int = Query(1, ge=1),
                 size: int = Query(25, ge=1, le=200), db: Session = Depends(get_db),
                 v: svc.Viewer = Depends(_viewer)):
    s = svc.get_service(db, v.company_id, code)
    if scope == "mine" and not (v.can_submit(s) or v.can_handle(s)):
        raise NotFound("Service")
    statuses, ff = _list_args(status, date_from, date_to, request)
    query = svc.records_query(db, v, s, scope=scope, status=statuses, q=q, date_from=date_from, date_to=date_to,
                              field_filters=ff)
    result = paginate(svc.sort_query(query, sort), PageParams(page, size))
    rows = result["items"]
    labels = svc.resolve_labels(db, v.company_id, [(r.version.definition, r.values or {}) for r in rows])
    result["items"] = [svc.record_row(v, r, labels) for r in rows]
    return result


@router.get("/services/{code}/export")
def export(code: str, request: Request, format: str = Query("csv", pattern="^(csv|xlsx)$"),
           scope: str = Query("all", pattern="^(mine|all)$"), status: str | None = None, q: str | None = None,
           date_from: date | None = None, date_to: date | None = None, sort: str | None = None,
           db: Session = Depends(get_db), v: svc.Viewer = Depends(_viewer)):
    s = svc.get_service(db, v.company_id, code)
    statuses, ff = _list_args(status, date_from, date_to, request)
    query = svc.records_query(db, v, s, scope=scope, status=statuses, q=q, date_from=date_from, date_to=date_to,
                              field_filters=ff)
    rows = svc.sort_query(query, sort).limit(20000).all()
    content, name, ctype = svc.export(db, v, s, rows, format)
    db.commit()
    return Response(content=content, media_type=ctype,
                    headers={"Content-Disposition": f"attachment; filename=\"{name}\"; filename*=UTF-8''{quote(name)}"})


@router.get("/my-requests")
def my_requests(status: str | None = None, service: str | None = None, q: str | None = None,
                page: int = Query(1, ge=1), size: int = Query(25, ge=1, le=200), db: Session = Depends(get_db),
                v: svc.Viewer = Depends(_viewer)):
    s = svc.get_service(db, v.company_id, service) if service else None
    query = svc.records_query(db, v, s, scope="mine", status=None, q=q)
    if status == "open":
        query = query.filter(CustomFormRecord.closed_at.is_(None), CustomFormRecord.status.notin_(("rejected", "cancelled")))
    elif status == "closed":
        query = query.filter(CustomFormRecord.closed_at.isnot(None))
    elif status:
        query = query.filter(CustomFormRecord.status.in_(status.split(",")))
    result = paginate(svc.sort_query(query, None), PageParams(page, size))
    rows = result["items"]
    labels = svc.resolve_labels(db, v.company_id, [(r.version.definition, r.values or {}) for r in rows])
    result["items"] = [svc.record_row(v, r, labels) for r in rows]
    return result


def _record(db: Session, v: svc.Viewer, uuid: str) -> CustomFormRecord:
    r = svc.get_record(db, v.company_id, uuid)
    svc.require_view(db, v, r)
    return r


@router.get("/records/{uuid}")
def get_record(uuid: str, db: Session = Depends(get_db), v: svc.Viewer = Depends(_viewer)):
    return svc.record_detail(db, v, _record(db, v, uuid))


@router.put("/records/{uuid}")
def update_record(uuid: str, body: sc.RecordUpdate, db: Session = Depends(get_db), v: svc.Viewer = Depends(_viewer)):
    r = svc.update_record(db, v, _record(db, v, uuid), body.values)
    db.commit()
    return svc.record_detail(db, v, r)


@router.post("/records/{uuid}/submit")
def submit_record(uuid: str, db: Session = Depends(get_db), v: svc.Viewer = Depends(_viewer)):
    r = svc.submit_record(db, v, _record(db, v, uuid))
    db.commit()
    return svc.record_detail(db, v, r)


@router.post("/records/{uuid}/status")
def change_status(uuid: str, body: sc.StatusIn, db: Session = Depends(get_db), v: svc.Viewer = Depends(_viewer)):
    r = svc.change_status(db, v, _record(db, v, uuid), body.status, body.note)
    db.commit()
    return svc.record_detail(db, v, r)


@router.post("/records/{uuid}/cancel")
def cancel_record(uuid: str, body: sc.CancelIn, db: Session = Depends(get_db), v: svc.Viewer = Depends(_viewer)):
    r = svc.cancel_record(db, v, _record(db, v, uuid), body.reason)
    db.commit()
    return svc.record_detail(db, v, r)


@router.delete("/records/{uuid}", status_code=204)
def delete_draft(uuid: str, db: Session = Depends(get_db), v: svc.Viewer = Depends(_viewer)):
    svc.delete_draft(db, v, _record(db, v, uuid))
    db.commit()
    return Response(status_code=204)


@router.post("/records/{uuid}/comments", status_code=201)
def add_comment(uuid: str, body: sc.CommentIn, db: Session = Depends(get_db), v: svc.Viewer = Depends(_viewer)):
    r = _record(db, v, uuid)
    if r.status == "draft":
        raise BadRequest("Submit the form before adding comments.")
    svc.add_comment(db, v, r, body.body, body.attachment_uuids)
    db.commit()
    return svc.activity(db, r)


# ── Saved views ─────────────────────────────────────────────────────

def _view_out(row: CustomFormView, v: svc.Viewer) -> dict:
    return {"uuid": str(row.uuid), "name": row.name, "filters": row.filters, "columns": row.columns,
            "shared": row.shared, "mine": row.owner_key == v.owner_key}


@router.get("/services/{code}/views")
def list_views(code: str, db: Session = Depends(get_db), v: svc.Viewer = Depends(_viewer)):
    s = svc.get_service(db, v.company_id, code)
    return [_view_out(r, v) for r in svc.views(db, v, s)]


@router.post("/services/{code}/views", status_code=201)
def create_view(code: str, body: sc.ViewIn, db: Session = Depends(get_db), v: svc.Viewer = Depends(_viewer)):
    s = svc.get_service(db, v.company_id, code)
    if not (v.can_submit(s) or v.can_handle(s)):
        raise NotFound("Service")
    row = svc.save_view(db, v, s, body.model_dump())
    db.commit()
    return _view_out(row, v)


def _view(db: Session, v: svc.Viewer, uuid: str) -> CustomFormView:
    row = db.query(CustomFormView).filter(CustomFormView.company_id == v.company_id,
                                          CustomFormView.uuid == uuid).first()
    if row is None or (row.owner_key != v.owner_key and not row.shared):
        raise NotFound("View")
    return row


@router.put("/views/{uuid}")
def update_view(uuid: str, body: sc.ViewIn, db: Session = Depends(get_db), v: svc.Viewer = Depends(_viewer)):
    row = _view(db, v, uuid)
    s = db.query(CustomService).filter(CustomService.id == row.service_id).first()
    row = svc.save_view(db, v, s, body.model_dump(), row)
    db.commit()
    return _view_out(row, v)


@router.delete("/views/{uuid}", status_code=204)
def delete_view(uuid: str, db: Session = Depends(get_db), v: svc.Viewer = Depends(_viewer)):
    """A saved view is a personal setting, not a business record, so it can be removed."""
    row = _view(db, v, uuid)
    if row.owner_key != v.owner_key:
        raise Forbidden("Only the person who saved this view can remove it.")
    db.delete(row)
    db.commit()
    return Response(status_code=204)


# ── Lookups for pickers ─────────────────────────────────────────────

@router.get("/lookups/{kind}")
def lookups(kind: str, q: str | None = None, ids: str | None = None, db: Session = Depends(get_db),
            v: svc.Viewer = Depends(_viewer)):
    id_list = [int(x) for x in (ids or "").split(",") if x.strip().isdigit()][:50] or None
    return svc.lookup_options(db, v.company_id, kind, q, id_list)
