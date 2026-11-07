"""Letter templates (with header and footer designs), letter generation (single and bulk),
self-service letter requests and e-signatures (in the app, and by link for outsiders)."""
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import Response
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from core.db import bypass_rls, get_db, set_tenant
from core.errors import BadRequest, Forbidden, NotFound
from core.pagination import PageParams, paginate
from core.security import get_current_user, require_permission
from modules.documents import esign_providers, merge, schemas, service, storage
from modules.documents.models import EsignRequest, EsignSigner, Letter, Letterhead, LetterTemplate

router = APIRouter(prefix="/letters", tags=["Letters and e-sign"])
public_router = APIRouter(prefix="/public/esign", tags=["Letters and e-sign"])
MANAGE = "workplace.letters.manage"
TEMPLATES = "workplace.letters.manage"


# ── Reference data ──────────────────────────────────────────────────

@router.get("/meta")
def meta(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    from modules.documents.pdf import FONTS, PAGE_SIZES_MM
    return {"letter_types": [{"value": k, "label": v} for k, v in service.LETTER_TYPES.items()],
            "page_sizes": list(PAGE_SIZES_MM), "fonts": list(FONTS), "merge_fields": merge.catalogue(),
            "providers": esign_providers.available()}


# ── Header and footer designs ───────────────────────────────────────

def _lh_out(r: Letterhead) -> dict:
    return {"id": r.id, "uuid": str(r.uuid), "kind": r.kind, "name": r.name, "html": r.html, "height_mm": r.height_mm,
            "is_default": r.is_default, "active": r.active, "updated_at": r.updated_at}


@router.get("/letterheads")
def list_letterheads(kind: str | None = None, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    q = db.query(Letterhead).filter(Letterhead.company_id == user["company_id"])
    if kind:
        q = q.filter(Letterhead.kind == kind)
    return {"items": [_lh_out(r) for r in q.order_by(Letterhead.kind, Letterhead.is_default.desc(), Letterhead.name)]}


@router.post("/letterheads", status_code=status.HTTP_201_CREATED)
def create_letterhead(body: schemas.LetterheadIn, db: Session = Depends(get_db),
                      user: dict = Depends(require_permission(TEMPLATES))):
    r = service.save_letterhead(db, user["company_id"], body.model_dump())
    db.commit()
    return _lh_out(r)


@router.put("/letterheads/{uuid}")
def update_letterhead(uuid: UUID, body: schemas.LetterheadIn, db: Session = Depends(get_db),
                      user: dict = Depends(require_permission(TEMPLATES))):
    r = service.get_letterhead(db, user["company_id"], uuid)
    service.save_letterhead(db, user["company_id"], body.model_dump(exclude_unset=True), r)
    db.commit()
    return _lh_out(r)


@router.post("/letterheads/{uuid}/preview")
def preview_letterhead(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    r = service.get_letterhead(db, user["company_id"], uuid)
    values = {**merge.sample_values(), **service.company_values(db, user["company_id"])}
    html, _ = merge.render(r.html.replace("{{page.number}}", "\x00PN\x00").replace("{{page.total}}", "\x00PT\x00"), values)
    html = html.replace("\x00PN\x00", "{{page.number}}").replace("\x00PT\x00", "{{page.total}}")
    from modules.documents import pdf
    body = '<p class="muted">The letter text goes here.</p>' * 3
    return {"html": pdf.preview_html(body, header=html if r.kind == "header" else None,
                                     footer=html if r.kind == "footer" else None,
                                     header_mm=r.height_mm, footer_mm=r.height_mm)}


# ── Templates ───────────────────────────────────────────────────────

def _tpl_out(t: LetterTemplate, full: bool = False) -> dict:
    out = {"id": t.id, "uuid": str(t.uuid), "name": t.name, "letter_type": t.letter_type,
           "letter_type_label": service.LETTER_TYPES.get(t.letter_type), "description": t.description,
           "subject": t.subject, "header_id": t.header_id, "footer_id": t.footer_id, "page_size": t.page_size,
           "orientation": t.orientation, "margin_top": t.margin_top, "margin_right": t.margin_right,
           "margin_bottom": t.margin_bottom, "margin_left": t.margin_left, "font_family": t.font_family,
           "font_size": t.font_size, "extra_fields": t.extra_fields or [], "self_service": t.self_service,
           "esign_required": t.esign_required, "signers": t.signers or [], "signing_order": t.signing_order,
           "is_default": t.is_default, "active": t.active, "version": t.version, "updated_at": t.updated_at}
    if full:
        out["body_html"] = t.body_html
        out["fields_used"] = merge.fields_used(t.body_html)
    return out


@router.get("/templates")
def list_templates(letter_type: str | None = None, q: str | None = None, active: bool | None = None,
                   db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    query = db.query(LetterTemplate).filter(LetterTemplate.company_id == user["company_id"])
    if letter_type:
        query = query.filter(LetterTemplate.letter_type == letter_type)
    if active is not None:
        query = query.filter(LetterTemplate.active.is_(active))
    if q:
        query = query.filter(LetterTemplate.name.ilike(f"%{q.strip()}%"))
    return {"items": [_tpl_out(t) for t in query.order_by(LetterTemplate.letter_type, LetterTemplate.name)]}


@router.post("/templates", status_code=status.HTTP_201_CREATED)
def create_template(body: schemas.TemplateIn, db: Session = Depends(get_db),
                    user: dict = Depends(require_permission(TEMPLATES))):
    t = service.save_template(db, user["company_id"], body.model_dump(exclude_unset=True))
    db.commit()
    return _tpl_out(t, True)


@router.get("/templates/{uuid}")
def get_template(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    return _tpl_out(service.get_template(db, user["company_id"], uuid), True)


@router.put("/templates/{uuid}")
def update_template(uuid: UUID, body: schemas.TemplateIn, db: Session = Depends(get_db),
                    user: dict = Depends(require_permission(TEMPLATES))):
    t = service.get_template(db, user["company_id"], uuid)
    service.save_template(db, user["company_id"], body.model_dump(exclude_unset=True), t)
    db.commit()
    return _tpl_out(t, True)


@router.post("/templates/{uuid}/duplicate", status_code=status.HTTP_201_CREATED)
def duplicate_template(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(TEMPLATES))):
    t = service.get_template(db, user["company_id"], uuid)
    data = _tpl_out(t, True)
    for k in ("id", "uuid", "letter_type_label", "version", "updated_at", "fields_used"):
        data.pop(k, None)
    data.update({"name": f"{t.name} (copy)"[:150], "is_default": False})
    copy = service.save_template(db, user["company_id"], data)
    db.commit()
    return _tpl_out(copy, True)


def _template_for_preview(db: Session, company_id: int, body: schemas.PreviewIn) -> LetterTemplate:
    if body.template is not None:  # unsaved designer state: build a throwaway template
        base = service.get_template(db, company_id, body.template_uuid) if body.template_uuid else None
        t = LetterTemplate(company_id=company_id, name="Preview", letter_type="other", body_html="", page_size="A4",
                           orientation="portrait", margin_top=20, margin_right=20, margin_bottom=20, margin_left=20,
                           font_family="Helvetica", font_size=11, extra_fields=[], signers=[], active=True)
        if base is not None:
            for col in LetterTemplate.__table__.columns.keys():
                if col not in ("id", "uuid"):
                    setattr(t, col, getattr(base, col))
        data = body.template.model_dump(exclude_unset=True)
        from modules.documents import pdf
        for k, v in data.items():
            if v is None or k in ("signers", "extra_fields", "is_default", "self_service", "esign_required"):
                continue
            if k == "body_html":
                v = pdf.sanitize(v)
            setattr(t, k, v)
        t.header = service.letterhead_by_id(db, company_id, t.header_id)
        t.footer = service.letterhead_by_id(db, company_id, t.footer_id)
        db.expunge(t) if t in db else None
        return t
    if body.template_uuid is None:
        raise BadRequest("Pick a template.")
    return service.get_template(db, company_id, body.template_uuid)


@router.post("/preview")
def preview(body: schemas.PreviewIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = user["company_id"]
    t = _template_for_preview(db, cid, body)
    emp = service.employee(db, cid, body.employee_id)
    out = service.preview(db, cid, t, emp, body.values, body.header_id, body.footer_id)
    db.rollback()
    return out


@router.post("/preview.pdf")
def preview_pdf(body: schemas.PreviewIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = user["company_id"]
    t = _template_for_preview(db, cid, body)
    emp = service.employee(db, cid, body.employee_id)
    data = service.preview(db, cid, t, emp, body.values, body.header_id, body.footer_id, as_pdf=True)
    db.rollback()
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": 'inline; filename="preview.pdf"'})


# ── Letters ─────────────────────────────────────────────────────────

def _esign_out(db: Session, r: EsignRequest | None, me_id: int | None = None) -> dict | None:
    if r is None:
        return None
    return {"uuid": str(r.uuid), "status": r.status, "provider": r.provider, "signing_order": r.signing_order,
            "expires_at": r.expires_at, "completed_at": r.completed_at, "message": r.message,
            "signed_file": storage.describe(storage.by_id(db, r.company_id, r.signed_file_id)) if r.signed_file_id else None,
            "signers": [{"uuid": str(s.uuid), "position": s.position, "name": s.name, "email": s.email, "label": s.label,
                         "status": s.status, "signed_at": s.signed_at, "declined_at": s.declined_at,
                         "decline_reason": s.decline_reason, "is_me": bool(me_id and s.employee_id == me_id)}
                        for s in sorted(r.signers, key=lambda x: x.position)]}


def _latest_esign(db: Session, letter: Letter) -> EsignRequest | None:
    return (db.query(EsignRequest).options(joinedload(EsignRequest.signers))
            .filter(EsignRequest.letter_id == letter.id).order_by(EsignRequest.id.desc()).first())


def _letter_out(db: Session, l: Letter, names: dict | None = None, detail: bool = False, me_id: int | None = None) -> dict:
    names = names if names is not None else service.names_for(db, l.company_id, [l.employee_id])
    out = {"uuid": str(l.uuid), "reference_no": l.reference_no, "title": l.title, "letter_type": l.letter_type,
           "letter_type_label": service.LETTER_TYPES.get(l.letter_type), "employee_id": l.employee_id,
           "employee_name": names.get(l.employee_id), "status": l.status, "template_name": l.template.name if l.template else None,
           "generated_by_name": l.generated_by_name, "created_at": l.created_at,
           "file": service.file_link(db, l.company_id, l.stored_file_id) if l.stored_file_id else None}
    if detail:
        out["values"] = l.values
        out["esign"] = _esign_out(db, _latest_esign(db, l), me_id)
    return out


@router.get("")
def list_letters(q: str | None = None, letter_type: str | None = None, status_: str | None = Query(None, alias="status"),
                 employee_id: int | None = None, params: PageParams = Depends(), db: Session = Depends(get_db),
                 user: dict = Depends(require_permission(MANAGE))):
    cid = user["company_id"]
    query = db.query(Letter).options(joinedload(Letter.template)).filter(Letter.company_id == cid)
    if letter_type:
        query = query.filter(Letter.letter_type == letter_type)
    if status_:
        query = query.filter(Letter.status == status_)
    if employee_id:
        query = query.filter(Letter.employee_id == employee_id)
    if q:
        from modules.employees.models import Employee
        like = f"%{q.strip()}%"
        query = query.outerjoin(Employee, Employee.id == Letter.employee_id).filter(or_(
            Letter.title.ilike(like), Letter.reference_no.ilike(like), Employee.first_name.ilike(like),
            Employee.last_name.ilike(like), Employee.employee_code.ilike(like)))
    page = paginate(query.order_by(Letter.created_at.desc()), params)
    names = service.names_for(db, cid, [l.employee_id for l in page["items"]])
    page["items"] = [_letter_out(db, l, names) for l in page["items"]]
    return page


@router.post("/generate", status_code=status.HTTP_201_CREATED)
def generate(body: schemas.GenerateIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = user["company_id"]
    t = service.get_template(db, cid, body.template_uuid)
    emp = service.employee(db, cid, body.employee_id)
    if emp is None:
        raise BadRequest("Pick the employee.")
    letter = service.generate_letter(db, cid, t, emp, body.values, header_id=body.header_id, footer_id=body.footer_id,
                                     use_header=body.use_header, use_footer=body.use_footer)
    db.commit()
    return _letter_out(db, service.get_letter(db, cid, letter.uuid), detail=True)


@router.post("/bulk", status_code=status.HTTP_201_CREATED)
def bulk(body: schemas.BulkIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = user["company_id"]
    t = service.get_template(db, cid, body.template_uuid)
    out = service.bulk_generate(db, cid, t, body.employee_ids, body.values, body.header_id, body.footer_id)
    db.commit()
    names = service.names_for(db, cid, [f["employee_id"] for f in out["failed"]])
    return {"generated": len(out["letters"]),
            "failed": [{**f, "employee_name": names.get(f["employee_id"])} for f in out["failed"]]}


@router.get("/me")
def my_letters(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    cid = user["company_id"]
    if not user.get("employee_id"):
        return {"items": []}
    rows = (db.query(Letter).options(joinedload(Letter.template))
            .filter(Letter.company_id == cid, Letter.employee_id == user["employee_id"],
                    Letter.status.in_(["issued", "signed", "out_for_signature"]))
            .order_by(Letter.created_at.desc()).limit(100).all())
    return {"items": [_letter_out(db, l, detail=True, me_id=user["employee_id"]) for l in rows]}


@router.get("/me/requestable")
def requestable(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    rows = (db.query(LetterTemplate).filter(LetterTemplate.company_id == user["company_id"],
                                            LetterTemplate.active.is_(True), LetterTemplate.self_service.is_(True))
            .order_by(LetterTemplate.name).all())
    return {"items": [{"uuid": str(t.uuid), "name": t.name, "letter_type": t.letter_type,
                       "description": t.description} for t in rows]}


@router.post("/me/requests", status_code=status.HTTP_201_CREATED)
def request_letter(body: schemas.LetterRequestIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    from core.workflow import service as workflow
    cid = user["company_id"]
    if not user.get("employee_id"):
        raise BadRequest("Only employees can request letters.")
    t = service.get_template(db, cid, body.template_uuid)
    emp = service.employee(db, cid, user["employee_id"])
    payload = {"employee_id": emp.id, "template_id": t.id, "letter_type": t.letter_type, "reason_id": body.reason_id,
               "reason_text": (body.reason_text or "").strip() or None, "purpose": (body.purpose or "").strip() or None,
               "details": (body.details or "").strip() or None}
    inst = workflow.submit(db, cid, "documents.letter_request", payload, f"{t.name}: {service.display_name(emp)}")
    db.commit()
    return {"uuid": str(inst.uuid), "reference_no": inst.reference_no, "status": inst.status,
            "letter_uuid": (inst.payload or {}).get("letter_uuid")}


@router.get("/me/requests")
def my_requests(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    from core.workflow.models import WorkflowInstance
    if not user.get("employee_id"):
        return {"items": []}
    rows = (db.query(WorkflowInstance).filter(WorkflowInstance.company_id == user["company_id"],
                                              WorkflowInstance.request_type == "documents.letter_request",
                                              WorkflowInstance.requester_employee_id == user["employee_id"])
            .order_by(WorkflowInstance.created_at.desc()).limit(50).all())
    return {"items": [{"uuid": str(r.uuid), "reference_no": r.reference_no, "title": r.title, "status": r.status,
                       "created_at": r.created_at, "completed_at": r.completed_at, "result_note": r.result_note,
                       "letter_uuid": (r.payload or {}).get("letter_uuid"),
                       "template_name": (r.payload or {}).get("template_name")} for r in rows]}


@router.get("/me/signatures")
def my_signatures(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    if not user.get("employee_id"):
        return {"items": []}
    rows = service.my_signatures(db, user["company_id"], user["employee_id"])
    return {"items": [{"uuid": str(s.uuid), "status": s.status, "label": s.label, "signed_at": s.signed_at,
                       "request_status": s.request.status, "title": s.request.title, "message": s.request.message,
                       "expires_at": s.request.expires_at, "created_at": s.created_at,
                       "letter_uuid": str(s.request.letter.uuid),
                       "file": service.file_link(db, s.company_id, s.request.letter.stored_file_id)} for s in rows]}


def _my_signer(db: Session, user: dict, uuid: UUID) -> EsignSigner:
    s = (db.query(EsignSigner).options(joinedload(EsignSigner.request).joinedload(EsignRequest.letter))
         .filter(EsignSigner.company_id == user["company_id"], EsignSigner.uuid == uuid).first())
    if s is None or not user.get("employee_id") or s.employee_id != user["employee_id"]:
        raise NotFound("Signing request")
    return s


@router.post("/sign/{uuid}/otp")
def my_otp(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    out = service.send_otp(db, _my_signer(db, user, uuid))
    db.commit()
    return out


@router.post("/sign/{uuid}")
def my_sign(uuid: UUID, body: schemas.SignIn, request: Request, db: Session = Depends(get_db),
            user: dict = Depends(get_current_user)):
    s = _my_signer(db, user, uuid)
    try:
        req = service.sign(db, s, body.code, body.signature_image, body.typed_name,
                           request.client.host if request.client else None, request.headers.get("user-agent"))
    except BadRequest:
        db.commit()  # keep the wrong-code count
        raise
    db.commit()
    return {"status": req.status}


@router.post("/sign/{uuid}/decline")
def my_decline(uuid: UUID, body: schemas.DeclineIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    req = service.decline(db, _my_signer(db, user, uuid), body.reason)
    db.commit()
    return {"status": req.status}


@router.get("/{uuid}")
def get_letter(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    cid = user["company_id"]
    l = service.get_letter(db, cid, uuid)
    if not service.has_perm(db, user, MANAGE) and not (user.get("employee_id") and l.employee_id == user["employee_id"]):
        raise NotFound("Letter")
    return _letter_out(db, l, detail=True, me_id=user.get("employee_id"))


@router.post("/{uuid}/cancel")
def cancel_letter(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    l = service.get_letter(db, user["company_id"], uuid)
    service.cancel_letter(db, l)
    db.commit()
    return _letter_out(db, l, detail=True)


@router.post("/{uuid}/esign", status_code=status.HTTP_201_CREATED)
def send_esign(uuid: UUID, body: schemas.EsignIn, db: Session = Depends(get_db),
               user: dict = Depends(require_permission(MANAGE))):
    cid = user["company_id"]
    l = service.get_letter(db, cid, uuid)
    if l.status == "cancelled":
        raise BadRequest("This letter was withdrawn.")
    service.send_for_signature(db, cid, l, [s.model_dump() for s in body.signers] or None, body.signing_order,
                               body.expires_days, body.message, body.provider)
    db.commit()
    return _letter_out(db, service.get_letter(db, cid, uuid), detail=True)


@router.post("/esign/{uuid}/remind")
def esign_remind(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    n = service.remind_signers(db, service.get_esign(db, user["company_id"], uuid))
    db.commit()
    return {"reminded": n}


@router.post("/esign/{uuid}/recall")
def esign_recall(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    service.cancel_signature(db, service.get_esign(db, user["company_id"], uuid), "Recalled by HR")
    db.commit()
    return {"status": "cancelled"}


# ── Signing by link (people without a login, e.g. candidates) ───────

def _by_token(db: Session, token: str) -> EsignSigner:
    with bypass_rls(db):
        s = db.query(EsignSigner.company_id).filter(EsignSigner.access_token == token).first()
    if s is None:
        raise NotFound("Signing request")
    set_tenant(db, s.company_id)
    return service.get_signer_by_token(db, token)


@public_router.get("/{token}")
def public_view(token: str, db: Session = Depends(get_db)):
    s = _by_token(db, token)
    from modules.company.models import Company
    company = db.query(Company.company_name).filter(Company.id == s.company_id).scalar()
    return {"company_name": company, "title": s.request.title, "message": s.request.message, "name": s.name,
            "status": s.status, "request_status": s.request.status, "expires_at": s.request.expires_at,
            "file": service.file_link(db, s.company_id, s.request.letter.stored_file_id)}


@public_router.post("/{token}/otp")
def public_otp(token: str, db: Session = Depends(get_db)):
    out = service.send_otp(db, _by_token(db, token))
    db.commit()
    return out


@public_router.post("/{token}/sign")
def public_sign(token: str, body: schemas.SignIn, request: Request, db: Session = Depends(get_db)):
    s = _by_token(db, token)
    try:
        req = service.sign(db, s, body.code, body.signature_image, body.typed_name,
                           request.client.host if request.client else None, request.headers.get("user-agent"))
    except BadRequest:
        db.commit()
        raise
    db.commit()
    return {"status": req.status}


@public_router.post("/{token}/decline")
def public_decline(token: str, body: schemas.DeclineIn, db: Session = Depends(get_db)):
    req = service.decline(db, _by_token(db, token), body.reason)
    db.commit()
    return {"status": req.status}
