"""Admissions API: settings and programs open, enquiries (pipeline), applications (tests,
interviews, merit list, offers, enrolment), dashboard; certificates (templates and register);
and the public online application form on the institution's own address."""
import json
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from core import audit
from core.crud import get_or_404
from core.db import bypass_rls, get_db, set_tenant
from core.errors import BadRequest, NotFound
from core.pagination import PageParams, paginate
from core.security import require_permission
from core.timeutil import today_for_company_id
from modules.admissions import service
from modules.admissions.models import (AdmissionOffering, Application, Certificate, CertificateTemplate, Enquiry)

router = APIRouter(prefix="/admissions", tags=["Admissions"])
cert_router = APIRouter(prefix="/certificates", tags=["Certificates"])
public_router = APIRouter(prefix="/public/admissions", tags=["Admissions (public)"])
extra_routers = [cert_router, public_router]

VIEW, MANAGE, ENROL, SETTINGS = "admissions.view", "admissions.manage", "admissions.enrol", "admissions.settings.manage"
CERT_VIEW, CERT_MANAGE = "admissions.certificates.view", "admissions.certificates.manage"


def _cid(user: dict) -> int:
    return user["company_id"]


# ── Settings and programs open for admission ─────────────────────────

class DocIn(BaseModel):
    key: str | None = Field(None, max_length=40)
    label: str = Field(..., max_length=120)
    required: bool = False


class SettingsIn(BaseModel):
    online_enabled: bool | None = None
    form_title: str | None = Field(None, max_length=120)
    form_intro: str | None = Field(None, max_length=2000)
    privacy_notice: str | None = Field(None, max_length=2000)
    required_documents: list[DocIn] | None = Field(None, max_length=20)
    ack_email_subject: str | None = Field(None, max_length=200)
    ack_email_body: str | None = Field(None, max_length=5000)
    offer_email_body: str | None = Field(None, max_length=5000)
    offer_letter_html: str | None = Field(None, max_length=50000)
    offer_valid_days: int | None = Field(None, ge=1, le=365)
    test_weight: int | None = Field(None, ge=0, le=100)
    interview_weight: int | None = Field(None, ge=0, le=100)


@router.get("/settings")
def get_settings(db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    return service.settings(db, _cid(user))


@router.put("/settings")
def put_settings(body: SettingsIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(SETTINGS))):
    data = body.model_dump(exclude_unset=True)
    if "required_documents" in data and data["required_documents"] is not None:
        data["required_documents"] = [d for d in data["required_documents"]]
    out = service.save_settings(db, _cid(user), data)
    db.commit()
    return out


class OfferingIn(BaseModel):
    session_id: int
    program_id: int
    seats: int = Field(0, ge=0, le=100000)
    application_fee: Decimal = Field(Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    fee_required: bool = False
    open_online: bool = True
    opens_on: date | None = None
    closes_on: date | None = None
    test_required: bool = False
    interview_required: bool = False
    min_age_years: Decimal | None = Field(None, ge=0, le=99, max_digits=4, decimal_places=1)
    max_age_years: Decimal | None = Field(None, ge=0, le=99, max_digits=4, decimal_places=1)
    age_as_on: date | None = None
    notes: str | None = Field(None, max_length=1000)
    active: bool = True


class OfferingPatch(BaseModel):
    seats: int | None = Field(None, ge=0, le=100000)
    application_fee: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=2)
    fee_required: bool | None = None
    open_online: bool | None = None
    opens_on: date | None = None
    closes_on: date | None = None
    test_required: bool | None = None
    interview_required: bool | None = None
    min_age_years: Decimal | None = Field(None, ge=0, le=99, max_digits=4, decimal_places=1)
    max_age_years: Decimal | None = Field(None, ge=0, le=99, max_digits=4, decimal_places=1)
    age_as_on: date | None = None
    notes: str | None = Field(None, max_length=1000)
    active: bool | None = None


@router.get("/offerings")
def offerings(session_id: int | None = None, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    return service.list_offerings(db, _cid(user), session_id)


@router.post("/offerings", status_code=201)
def add_offering(body: OfferingIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(SETTINGS))):
    row = service.save_offering(db, _cid(user), body.model_dump())
    db.commit()
    return next(o for o in service.list_offerings(db, _cid(user), row.session_id) if o["id"] == row.id)


@router.put("/offerings/{uuid}")
def edit_offering(uuid: UUID, body: OfferingPatch, db: Session = Depends(get_db),
                  user: dict = Depends(require_permission(SETTINGS))):
    row = get_or_404(db, AdmissionOffering, _cid(user), uuid, "Program")
    service.save_offering(db, _cid(user), body.model_dump(exclude_unset=True), row)
    db.commit()
    return next(o for o in service.list_offerings(db, _cid(user), row.session_id) if o["id"] == row.id)


@router.delete("/offerings/{uuid}")
def close_offering(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(SETTINGS))):
    row = get_or_404(db, AdmissionOffering, _cid(user), uuid, "Program")
    row.active = False
    row.open_online = False
    db.commit()
    return {"detail": "Closed for admission"}


@router.get("/meta")
def meta(db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    """Words and choices used by the admissions pages."""
    from modules.academics.models import AcademicSession
    cid = _cid(user)
    sessions = (db.query(AcademicSession).filter(AcademicSession.company_id == cid, AcademicSession.active.is_(True))
                .order_by(AcademicSession.start_date.desc()).all())
    return {
        "stages": [{"key": k, "label": service.STAGE_LABELS[k]} for k in service.STAGES],
        "statuses": [{"key": k, "label": service.STATUS_LABELS[k]} for k in service.STATUSES],
        "allowed": service.ALLOWED,
        "certificate_kinds": [{"key": k, "label": v} for k, v in service.CERT_KINDS.items()],
        "sessions": [{"id": s.id, "name": s.name, "is_current": s.is_current} for s in sessions],
        "default_session_id": service.default_session_id(db, cid),
    }


# ── Dashboard and home card ──────────────────────────────────────────

@router.get("/dashboard")
def dashboard(session_id: int | None = None, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    cid = _cid(user)
    sid = session_id or service.default_session_id(db, cid)
    return service.dashboard(db, cid, sid, today_for_company_id(db, cid))


@router.get("/summary")
def summary(db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    cid = _cid(user)
    return service.summary(db, cid, today_for_company_id(db, cid))


# ── Enquiries ────────────────────────────────────────────────────────

class EnquiryIn(BaseModel):
    child_first_name: str = Field(..., min_length=1, max_length=100)
    child_last_name: str | None = Field(None, max_length=100)
    child_dob: date | None = None
    child_gender: str | None = Field(None, max_length=20)
    current_institution: str | None = Field(None, max_length=200)
    parent_name: str | None = Field(None, max_length=200)
    parent_relation: str | None = Field(None, max_length=30)
    phone: str | None = Field(None, max_length=20)
    email: EmailStr | None = None
    city: str | None = Field(None, max_length=100)
    address: str | None = Field(None, max_length=1000)
    source_id: int | None = None
    source_detail: str | None = Field(None, max_length=200)
    session_id: int | None = None
    program_id: int | None = None
    next_follow_up: date | None = None
    counsellor_id: int | None = None
    notes: str | None = Field(None, max_length=4000)


class EnquiryPatch(BaseModel):
    child_first_name: str | None = Field(None, min_length=1, max_length=100)
    child_last_name: str | None = Field(None, max_length=100)
    child_dob: date | None = None
    child_gender: str | None = Field(None, max_length=20)
    current_institution: str | None = Field(None, max_length=200)
    parent_name: str | None = Field(None, max_length=200)
    parent_relation: str | None = Field(None, max_length=30)
    phone: str | None = Field(None, max_length=20)
    email: EmailStr | None = None
    city: str | None = Field(None, max_length=100)
    address: str | None = Field(None, max_length=1000)
    source_id: int | None = None
    source_detail: str | None = Field(None, max_length=200)
    session_id: int | None = None
    program_id: int | None = None
    next_follow_up: date | None = None
    counsellor_id: int | None = None
    notes: str | None = Field(None, max_length=4000)


class StageIn(BaseModel):
    stage: str
    note: str | None = Field(None, max_length=2000)
    lost_reason: str | None = Field(None, max_length=300)
    visit_at: datetime | None = None
    next_follow_up: date | None = None


class AssignIn(BaseModel):
    counsellor_id: int | None = None


class ActivityIn(BaseModel):
    kind: str = Field("note", max_length=20)
    body: str = Field(..., min_length=1, max_length=4000)
    next_follow_up: date | None = None


def _enquiry(db: Session, user: dict, uuid: UUID) -> Enquiry:
    return get_or_404(db, Enquiry, _cid(user), uuid, "Enquiry")


def _enquiry_filters(q, stage, source_id, program_id, session_id, counsellor_id, follow_up, mine, user, db):
    return dict(q=q, stage=stage, source_id=source_id, program_id=program_id, session_id=session_id,
                counsellor_id=user.get("employee_id") if mine else counsellor_id, follow_up=follow_up,
                today=today_for_company_id(db, _cid(user)))


@router.get("/enquiries")
def list_enquiries(q: str | None = None, stage: str | None = None, source_id: int | None = None,
                   program_id: int | None = None, session_id: int | None = None, counsellor_id: int | None = None,
                   follow_up: str | None = None, mine: bool = False, archived: bool = False,
                   params: PageParams = Depends(), db: Session = Depends(get_db),
                   user: dict = Depends(require_permission(VIEW))):
    f = _enquiry_filters(q, stage, source_id, program_id, session_id, counsellor_id, follow_up, mine, user, db)
    query = service.enquiry_query(db, _cid(user), archived=archived, **f).order_by(Enquiry.created_at.desc())
    return paginate(query, params, service.enquiry_out)


@router.get("/enquiries/board")
def enquiry_board(q: str | None = None, source_id: int | None = None, program_id: int | None = None,
                  session_id: int | None = None, counsellor_id: int | None = None, follow_up: str | None = None,
                  mine: bool = False, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    f = _enquiry_filters(q, None, source_id, program_id, session_id, counsellor_id, follow_up, mine, user, db)
    f.pop("stage")
    return {"stages": service.board(db, _cid(user), **f)}


@router.post("/enquiries", status_code=201)
def add_enquiry(body: EnquiryIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    e = service.create_enquiry(db, _cid(user), body.model_dump())
    db.commit()
    db.refresh(e)
    return service.enquiry_out(e, full=True)


@router.get("/enquiries/{uuid}")
def get_enquiry(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    e = _enquiry(db, user, uuid)
    out = service.enquiry_out(e, full=True)
    out["activities"] = service.activities(db, _cid(user), enquiry_id=e.id, application_id=e.application_id)
    if e.application_id:
        a = db.query(Application).filter(Application.company_id == _cid(user), Application.id == e.application_id).first()
        out["application"] = service.application_out(db, a) if a else None
    return out


@router.put("/enquiries/{uuid}")
def edit_enquiry(uuid: UUID, body: EnquiryPatch, db: Session = Depends(get_db),
                 user: dict = Depends(require_permission(MANAGE))):
    e = _enquiry(db, user, uuid)
    service.update_enquiry(db, _cid(user), e, body.model_dump(exclude_unset=True))
    db.commit()
    db.refresh(e)
    return service.enquiry_out(e, full=True)


@router.post("/enquiries/{uuid}/stage")
def enquiry_stage(uuid: UUID, body: StageIn, db: Session = Depends(get_db),
                  user: dict = Depends(require_permission(MANAGE))):
    e = _enquiry(db, user, uuid)
    service.change_stage(db, _cid(user), e, body.stage, note=body.note, lost_reason=body.lost_reason,
                         visit_at=body.visit_at, next_follow_up=body.next_follow_up)
    db.commit()
    db.refresh(e)
    return service.enquiry_out(e, full=True)


@router.post("/enquiries/{uuid}/assign")
def enquiry_assign(uuid: UUID, body: AssignIn, db: Session = Depends(get_db),
                   user: dict = Depends(require_permission(MANAGE))):
    e = _enquiry(db, user, uuid)
    service.assign(db, _cid(user), e, body.counsellor_id)
    db.commit()
    db.refresh(e)
    return service.enquiry_out(e, full=True)


class BulkAssignIn(BaseModel):
    uuids: list[UUID] = Field(..., max_length=500)
    counsellor_id: int | None = None


@router.post("/enquiries/bulk-assign")
def enquiry_bulk_assign(body: BulkAssignIn, db: Session = Depends(get_db),
                        user: dict = Depends(require_permission(MANAGE))):
    rows = db.query(Enquiry).filter(Enquiry.company_id == _cid(user), Enquiry.uuid.in_(body.uuids)).all()
    for e in rows:
        service.assign(db, _cid(user), e, body.counsellor_id)
    db.commit()
    return {"updated": len(rows)}


@router.post("/enquiries/{uuid}/activities", status_code=201)
def enquiry_activity(uuid: UUID, body: ActivityIn, db: Session = Depends(get_db),
                     user: dict = Depends(require_permission(MANAGE))):
    e = _enquiry(db, user, uuid)
    service.add_activity(db, _cid(user), e, body.kind, body.body, body.next_follow_up)
    db.commit()
    return {"activities": service.activities(db, _cid(user), enquiry_id=e.id, application_id=e.application_id),
            "enquiry": service.enquiry_out(e, full=True)}


@router.delete("/enquiries/{uuid}")
def archive_enquiry(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    e = _enquiry(db, user, uuid)
    from datetime import timezone
    e.archived_at = datetime.now(timezone.utc)
    service.log(db, _cid(user), enquiry=e, kind="system", body="Archived")
    db.commit()
    return {"detail": "Archived"}


@router.post("/enquiries/{uuid}/restore")
def restore_enquiry(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    e = _enquiry(db, user, uuid)
    e.archived_at = None
    service.log(db, _cid(user), enquiry=e, kind="system", body="Restored")
    db.commit()
    return {"detail": "Restored"}


@router.post("/enquiries/{uuid}/application", status_code=201)
def enquiry_to_application(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    """Start an application from the enquiry (child and parent details copied)."""
    cid = _cid(user)
    e = _enquiry(db, user, uuid)
    if e.application_id:
        raise BadRequest("This enquiry already has an application.")
    if not e.program_id or not e.session_id:
        raise BadRequest("Pick the program and session on the enquiry first.")
    parts = (e.parent_name or "").strip().split(" ", 1)
    guardians = [{"relation": e.parent_relation or "guardian", "first_name": parts[0] or "Parent",
                  "last_name": parts[1] if len(parts) > 1 else None, "phone": e.phone, "email": e.email,
                  "is_primary": True}] if (e.parent_name or e.phone or e.email) else []
    a = service.create_application(db, cid, {
        "session_id": e.session_id, "program_id": e.program_id, "first_name": e.child_first_name,
        "last_name": e.child_last_name, "date_of_birth": e.child_dob, "gender": e.child_gender,
        "previous_institution": e.current_institution, "city": e.city, "address": e.address,
        "contact_phone": e.phone, "contact_email": e.email}, guardians=guardians, enquiry=e)
    db.commit()
    db.refresh(a)
    return service.application_out(db, a, full=True)


# ── Applications ─────────────────────────────────────────────────────

class GuardianIn(BaseModel):
    relation: str = Field("guardian", max_length=30)
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str | None = Field(None, max_length=100)
    phone: str | None = Field(None, max_length=20)
    email: EmailStr | None = None
    occupation: str | None = Field(None, max_length=120)
    is_primary: bool = False


class ApplicationBase(BaseModel):
    first_name: str | None = Field(None, min_length=1, max_length=100)
    middle_name: str | None = Field(None, max_length=100)
    last_name: str | None = Field(None, max_length=100)
    gender: str | None = Field(None, max_length=20)
    date_of_birth: date | None = None
    blood_group: str | None = Field(None, max_length=10)
    nationality: str | None = Field(None, max_length=60)
    religion: str | None = Field(None, max_length=60)
    category: str | None = Field(None, max_length=40)
    mother_tongue: str | None = Field(None, max_length=60)
    national_id: str | None = Field(None, max_length=40)
    previous_institution: str | None = Field(None, max_length=200)
    previous_program: str | None = Field(None, max_length=120)
    previous_marks: str | None = Field(None, max_length=60)
    address: str | None = Field(None, max_length=1000)
    city: str | None = Field(None, max_length=100)
    state: str | None = Field(None, max_length=100)
    pincode: str | None = Field(None, max_length=12)
    contact_phone: str | None = Field(None, max_length=20)
    contact_email: EmailStr | None = None
    remarks: str | None = Field(None, max_length=4000)
    session_id: int | None = None
    program_id: int | None = None
    guardians: list[GuardianIn] | None = Field(None, max_length=4)
    custom: dict | None = None


class ApplicationIn(ApplicationBase):
    first_name: str = Field(..., min_length=1, max_length=100)
    session_id: int
    program_id: int
    status: str = Field("submitted", pattern="^(draft|submitted)$")


class StatusIn(BaseModel):
    status: str
    note: str | None = Field(None, max_length=2000)
    reason: str | None = Field(None, max_length=300)


class ScheduleIn(BaseModel):
    test_at: datetime | None = None
    test_venue: str | None = Field(None, max_length=200)
    interview_at: datetime | None = None
    interviewer_id: int | None = None


class ScoresIn(BaseModel):
    test_score: Decimal | None = Field(None, max_digits=7, decimal_places=2)
    test_max: Decimal | None = Field(None, max_digits=7, decimal_places=2)
    interview_score: Decimal | None = Field(None, max_digits=7, decimal_places=2)
    interview_max: Decimal | None = Field(None, max_digits=7, decimal_places=2)
    interview_notes: str | None = Field(None, max_length=4000)


class FeeIn(BaseModel):
    paid: bool
    paid_on: date | None = None
    reference: str | None = Field(None, max_length=80)
    amount: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=2)


class OfferIn(BaseModel):
    expires_on: date | None = None
    note: str | None = Field(None, max_length=2000)


class EnrolIn(BaseModel):
    section_id: int
    roll_no: str | None = Field(None, max_length=20)
    admission_no: str | None = Field(None, max_length=40)
    admission_date: date | None = None
    create_portal_accounts: bool = False


class DocAttachIn(BaseModel):
    upload_uuid: UUID


class VerifyIn(BaseModel):
    verified: bool = True


class NoteIn(BaseModel):
    body: str = Field(..., min_length=1, max_length=4000)


def _application(db: Session, user: dict, uuid: UUID) -> Application:
    return get_or_404(db, Application, _cid(user), uuid, "Application")


def _app_full(db: Session, user: dict, a: Application) -> dict:
    db.refresh(a)
    out = service.application_out(db, a, full=True)
    out["activities"] = service.activities(db, _cid(user), application_id=a.id, enquiry_id=a.enquiry_id)
    return out


@router.get("/applications")
def list_applications(q: str | None = None, status: str | None = None, program_id: int | None = None,
                      session_id: int | None = None, channel: str | None = None, fee_paid: bool | None = None,
                      archived: bool = False, sort: str = "recent", params: PageParams = Depends(),
                      db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    query = service.application_query(db, _cid(user), q=q, status=status, program_id=program_id, session_id=session_id,
                                      channel=channel, fee_paid=fee_paid, archived=archived)
    if sort == "merit":
        query = query.order_by(Application.merit_rank.asc().nullslast(), Application.merit_score.desc().nullslast())
    else:
        query = query.order_by(func.coalesce(Application.submitted_at, Application.created_at).desc())
    return paginate(query, params, lambda a: service.application_out(db, a))


@router.post("/applications", status_code=201)
def add_application(body: ApplicationIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    data = body.model_dump(exclude={"guardians", "custom", "status"})
    a = service.create_application(db, _cid(user), data, guardians=[g.model_dump() for g in body.guardians or []],
                                   custom=body.custom, status=body.status)
    db.commit()
    return _app_full(db, user, a)


@router.get("/applications/{uuid}")
def get_application(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    return _app_full(db, user, _application(db, user, uuid))


@router.put("/applications/{uuid}")
def edit_application(uuid: UUID, body: ApplicationBase, db: Session = Depends(get_db),
                     user: dict = Depends(require_permission(MANAGE))):
    a = _application(db, user, uuid)
    data = body.model_dump(exclude_unset=True, exclude={"guardians", "custom"})
    service.update_application(db, _cid(user), a, data,
                               guardians=[g.model_dump() for g in body.guardians] if body.guardians is not None else None,
                               custom=body.custom)
    db.commit()
    return _app_full(db, user, a)


@router.post("/applications/{uuid}/status")
def application_status(uuid: UUID, body: StatusIn, db: Session = Depends(get_db),
                       user: dict = Depends(require_permission(MANAGE))):
    a = _application(db, user, uuid)
    service.change_status(db, _cid(user), a, body.status, note=body.note, reason=body.reason)
    db.commit()
    return _app_full(db, user, a)


@router.post("/applications/{uuid}/notes", status_code=201)
def application_note(uuid: UUID, body: NoteIn, db: Session = Depends(get_db),
                     user: dict = Depends(require_permission(MANAGE))):
    a = _application(db, user, uuid)
    service.log(db, _cid(user), application=a, kind="note", body=body.body.strip())
    db.commit()
    return _app_full(db, user, a)


@router.post("/applications/{uuid}/schedule")
def application_schedule(uuid: UUID, body: ScheduleIn, db: Session = Depends(get_db),
                         user: dict = Depends(require_permission(MANAGE))):
    a = _application(db, user, uuid)
    service.schedule(db, _cid(user), a, body.model_dump(exclude_unset=True))
    db.commit()
    return _app_full(db, user, a)


@router.post("/applications/{uuid}/scores")
def application_scores(uuid: UUID, body: ScoresIn, db: Session = Depends(get_db),
                       user: dict = Depends(require_permission(MANAGE))):
    a = _application(db, user, uuid)
    service.save_scores(db, _cid(user), a, body.model_dump(exclude_unset=True))
    db.commit()
    return _app_full(db, user, a)


@router.post("/applications/{uuid}/fee")
def application_fee(uuid: UUID, body: FeeIn, db: Session = Depends(get_db),
                    user: dict = Depends(require_permission(MANAGE))):
    a = _application(db, user, uuid)
    service.set_fee(db, _cid(user), a, body.paid, body.paid_on, body.reference, body.amount)
    db.commit()
    return _app_full(db, user, a)


@router.post("/applications/{uuid}/documents/{key}")
def application_document(uuid: UUID, key: str, body: DocAttachIn, db: Session = Depends(get_db),
                         user: dict = Depends(require_permission(MANAGE))):
    from modules.documents import storage
    a = _application(db, user, uuid)
    blob = storage.get(db, _cid(user), body.upload_uuid)
    service.attach_document(db, _cid(user), a, key, blob)
    service.log(db, _cid(user), application=a, kind="system", body=f"Document uploaded: {blob.filename}")
    db.commit()
    return _app_full(db, user, a)


@router.post("/applications/{uuid}/documents/{key}/verify")
def application_document_verify(uuid: UUID, key: str, body: VerifyIn, db: Session = Depends(get_db),
                                user: dict = Depends(require_permission(MANAGE))):
    a = _application(db, user, uuid)
    service.verify_document(db, _cid(user), a, key, body.verified)
    db.commit()
    return _app_full(db, user, a)


@router.post("/applications/{uuid}/offer")
def application_offer(uuid: UUID, body: OfferIn, db: Session = Depends(get_db),
                      user: dict = Depends(require_permission(MANAGE))):
    a = _application(db, user, uuid)
    service.offer(db, _cid(user), a, expires_on=body.expires_on, note=body.note)
    db.commit()
    return _app_full(db, user, a)


@router.post("/applications/{uuid}/accept")
def application_accept(uuid: UUID, body: NoteIn | None = None, db: Session = Depends(get_db),
                       user: dict = Depends(require_permission(MANAGE))):
    a = _application(db, user, uuid)
    service.accept(db, _cid(user), a, body.body if body else None)
    db.commit()
    return _app_full(db, user, a)


@router.post("/applications/{uuid}/enrol")
def application_enrol(uuid: UUID, body: EnrolIn, db: Session = Depends(get_db),
                      user: dict = Depends(require_permission(ENROL))):
    a = _application(db, user, uuid)
    student, logins = service.enrol(db, _cid(user), a, section_id=body.section_id, roll_no=body.roll_no,
                                    admission_no=body.admission_no, admission_date=body.admission_date,
                                    create_portal_accounts=body.create_portal_accounts)
    db.commit()
    out = _app_full(db, user, a)
    out["student"] = {"id": student.id, "uuid": str(student.uuid), "admission_no": student.admission_no,
                      "name": student.full_name}
    out["new_logins"] = logins
    return out


@router.get("/applications/{uuid}/offer-letter.pdf")
def application_offer_letter(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    a = _application(db, user, uuid)
    data = service.offer_letter_pdf(db, a)
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="offer-{a.application_no}.pdf"'})


@router.delete("/applications/{uuid}")
def archive_application(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    a = _application(db, user, uuid)
    if a.status in ("offered", "accepted", "enrolled") or a.offer_state == "pending":
        raise BadRequest("Withdraw the offer before archiving this application.")
    from datetime import timezone
    a.archived_at = datetime.now(timezone.utc)
    service.log(db, _cid(user), application=a, kind="system", body="Archived")
    db.commit()
    return {"detail": "Archived"}


# ── Merit list ───────────────────────────────────────────────────────

class MeritIn(BaseModel):
    session_id: int
    program_id: int


class MeritOfferIn(MeritIn):
    count: int | None = Field(None, ge=1, le=10000)
    waitlist_rest: bool = False
    expires_on: date | None = None


@router.get("/merit")
def merit(session_id: int, program_id: int, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    return service.merit_list(db, _cid(user), session_id, program_id)


@router.post("/merit/rank")
def merit_rank(body: MeritIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    pool = service.rank(db, _cid(user), body.session_id, body.program_id)
    db.commit()
    out = service.merit_list(db, _cid(user), body.session_id, body.program_id)
    out["ranked"] = len(pool)
    return out


@router.post("/merit/offer")
def merit_offer(body: MeritOfferIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    res = service.offer_top(db, _cid(user), body.session_id, body.program_id, body.count, body.waitlist_rest,
                            body.expires_on)
    db.commit()
    return res


# ── Certificates ─────────────────────────────────────────────────────

class TemplateField(BaseModel):
    key: str | None = Field(None, max_length=40)
    label: str = Field(..., max_length=80)
    type: str = Field("text", pattern="^(text|date|textarea)$")
    required: bool = False
    default: str | None = Field(None, max_length=200)


class TemplateIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    kind: str = Field("custom", pattern="^(transfer|leaving|bonafide|character|conduct|study|migration|custom)$")
    title: str | None = Field(None, max_length=200)
    body_html: str = Field("", max_length=100000)
    fields: list[TemplateField] = Field([], max_length=15)
    page_size: str = Field("A4", pattern="^(A4|A5|Letter|Legal)$")
    orientation: str = Field("portrait", pattern="^(portrait|landscape)$")
    font_family: str = Field("Georgia", max_length=40)
    show_letterhead: bool = True
    marks_student: str | None = Field(None, pattern="^(transferred|left)$")
    active: bool = True


class TemplatePatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    kind: str | None = Field(None, pattern="^(transfer|leaving|bonafide|character|conduct|study|migration|custom)$")
    title: str | None = Field(None, max_length=200)
    body_html: str | None = Field(None, max_length=100000)
    fields: list[TemplateField] | None = Field(None, max_length=15)
    page_size: str | None = Field(None, pattern="^(A4|A5|Letter|Legal)$")
    orientation: str | None = Field(None, pattern="^(portrait|landscape)$")
    font_family: str | None = Field(None, max_length=40)
    show_letterhead: bool | None = None
    marks_student: str | None = Field(None, pattern="^(transferred|left)$")
    active: bool | None = None


class PreviewIn(BaseModel):
    student_id: int | None = None
    values: dict = {}
    body_html: str | None = Field(None, max_length=100000)
    template: TemplatePatch | None = None


class IssueIn(BaseModel):
    template_id: int
    student_id: int
    issued_on: date | None = None
    values: dict = {}
    mark_student: bool = False


class CancelIn(BaseModel):
    reason: str = Field(..., min_length=2, max_length=300)


def _template_counts(db: Session, cid: int) -> dict[int, int]:
    return dict(db.query(Certificate.template_id, func.count(Certificate.id))
                .filter(Certificate.company_id == cid).group_by(Certificate.template_id).all())


def _student_by_id(db: Session, cid: int, student_id: int):
    from modules.students.models import Student
    s = db.query(Student).filter(Student.company_id == cid, Student.id == student_id).first()
    if s is None:
        raise BadRequest("The student was not found.")
    return s


@cert_router.get("/fields")
def cert_fields(user: dict = Depends(require_permission(CERT_VIEW))):
    return service.catalogue()


@cert_router.get("/templates")
def cert_templates(active: bool | None = None, db: Session = Depends(get_db),
                   user: dict = Depends(require_permission(CERT_VIEW))):
    cid = _cid(user)
    q = db.query(CertificateTemplate).filter(CertificateTemplate.company_id == cid)
    if active is not None:
        q = q.filter(CertificateTemplate.active.is_(active))
    counts = _template_counts(db, cid)
    return [service.template_out(t, counts.get(t.id, 0)) for t in q.order_by(CertificateTemplate.name).all()]


def _save_template(db: Session, cid: int, data: dict, row: CertificateTemplate | None = None) -> CertificateTemplate:
    from modules.documents.pdf import sanitize
    if "fields" in data and data["fields"] is not None:
        data["fields"] = service.clean_template_fields(data["fields"])
    if data.get("body_html") is not None:
        data["body_html"] = sanitize(data["body_html"])
    name = data.get("name")
    if name:
        clash = db.query(CertificateTemplate.id).filter(CertificateTemplate.company_id == cid,
                                                        func.lower(CertificateTemplate.name) == name.strip().lower())
        if row is not None:
            clash = clash.filter(CertificateTemplate.id != row.id)
        if clash.first():
            raise HTTPException(status_code=409, detail="A template with this name already exists.")
        data["name"] = name.strip()
    if row is None:
        row = CertificateTemplate(company_id=cid)
        db.add(row)
    for k, v in data.items():
        setattr(row, k, v)
    db.flush()
    return row


@cert_router.post("/templates", status_code=201)
def add_cert_template(body: TemplateIn, db: Session = Depends(get_db),
                      user: dict = Depends(require_permission(CERT_MANAGE))):
    data = body.model_dump()
    data["fields"] = [f for f in data["fields"]]
    t = _save_template(db, _cid(user), data)
    db.commit()
    return service.template_out(t)


@cert_router.get("/templates/{uuid}")
def get_cert_template(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(CERT_VIEW))):
    t = get_or_404(db, CertificateTemplate, _cid(user), uuid, "Template")
    return service.template_out(t, _template_counts(db, _cid(user)).get(t.id, 0))


@cert_router.put("/templates/{uuid}")
def edit_cert_template(uuid: UUID, body: TemplatePatch, db: Session = Depends(get_db),
                       user: dict = Depends(require_permission(CERT_MANAGE))):
    t = get_or_404(db, CertificateTemplate, _cid(user), uuid, "Template")
    data = body.model_dump(exclude_unset=True)
    if "marks_student" not in body.model_fields_set:
        data.pop("marks_student", None)
    _save_template(db, _cid(user), data, t)
    db.commit()
    db.refresh(t)
    return service.template_out(t, _template_counts(db, _cid(user)).get(t.id, 0))


@cert_router.delete("/templates/{uuid}")
def archive_cert_template(uuid: UUID, db: Session = Depends(get_db),
                          user: dict = Depends(require_permission(CERT_MANAGE))):
    t = get_or_404(db, CertificateTemplate, _cid(user), uuid, "Template")
    t.active = False
    db.commit()
    return {"detail": "Switched off"}


@cert_router.post("/templates/{uuid}/preview")
def preview_cert_template(uuid: UUID, body: PreviewIn, db: Session = Depends(get_db),
                          user: dict = Depends(require_permission(CERT_VIEW))):
    """A one-page HTML preview for a student (or the first active student) with typed values."""
    cid = _cid(user)
    t = get_or_404(db, CertificateTemplate, cid, uuid, "Template")
    return _preview(db, cid, t, body)


@cert_router.post("/preview")
def preview_cert_draft(body: PreviewIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(CERT_VIEW))):
    """Preview of a template being edited (not saved yet)."""
    cid = _cid(user)
    data = (body.template.model_dump(exclude_unset=True) if body.template else {})
    t = CertificateTemplate(company_id=cid, name=data.get("name") or "Certificate", kind=data.get("kind") or "custom",
                            title=data.get("title"), body_html=data.get("body_html") or body.body_html or "",
                            fields=service.clean_template_fields(data.get("fields") or []),
                            page_size=data.get("page_size") or "A4", orientation=data.get("orientation") or "portrait",
                            font_family=data.get("font_family") or "Georgia",
                            show_letterhead=data.get("show_letterhead", True))
    return _preview(db, cid, t, body)


def _preview(db: Session, cid: int, t: CertificateTemplate, body: PreviewIn) -> dict:
    from modules.students.models import Student
    if body.student_id:
        s = _student_by_id(db, cid, body.student_id)
    else:
        s = (db.query(Student).filter(Student.company_id == cid, Student.status == "active")
             .order_by(Student.id).first())
    if s is None:
        raise BadRequest("Add a student first to see a preview.")
    html_body, missing = service.render_certificate(db, cid, t, s, {k: str(v) for k, v in (body.values or {}).items()},
                                                    "CERT/0000/0001", date.today())
    return {"html": service.certificate_document(db, cid, t, html_body, pdf_mode=False), "missing": missing,
            "student": {"id": s.id, "name": s.full_name, "admission_no": s.admission_no}}


@cert_router.get("/issued")
def cert_register(q: str | None = None, kind: str | None = None, status: str | None = None, student_id: int | None = None,
                  date_from: date | None = None, date_to: date | None = None, params: PageParams = Depends(),
                  db: Session = Depends(get_db), user: dict = Depends(require_permission(CERT_VIEW))):
    from modules.students.models import Student
    cid = _cid(user)
    query = (db.query(Certificate).join(Student, Student.id == Certificate.student_id)
             .options(joinedload(Certificate.student), joinedload(Certificate.template))
             .filter(Certificate.company_id == cid))
    if kind:
        query = query.filter(Certificate.kind == kind)
    if status:
        query = query.filter(Certificate.status == status)
    if student_id:
        query = query.filter(Certificate.student_id == student_id)
    if date_from:
        query = query.filter(Certificate.issued_on >= date_from)
    if date_to:
        query = query.filter(Certificate.issued_on <= date_to)
    if q:
        like = f"%{q.strip()}%"
        from sqlalchemy import or_
        query = query.filter(or_(Certificate.number.ilike(like), Student.first_name.ilike(like),
                                 Student.last_name.ilike(like), Student.admission_no.ilike(like),
                                 func.concat(Student.first_name, " ", Student.last_name).ilike(like)))
    query = query.order_by(Certificate.issued_on.desc(), Certificate.id.desc())
    return paginate(query, params, service.certificate_out)


@cert_router.get("/issued/summary")
def cert_summary(db: Session = Depends(get_db), user: dict = Depends(require_permission(CERT_VIEW))):
    cid = _cid(user)
    today = today_for_company_id(db, cid)
    rows = dict(db.query(Certificate.kind, func.count(Certificate.id)).filter(
        Certificate.company_id == cid, Certificate.status == "issued").group_by(Certificate.kind).all())
    month = db.query(func.count(Certificate.id)).filter(Certificate.company_id == cid, Certificate.status == "issued",
                                                        Certificate.issued_on >= today.replace(day=1)).scalar() or 0
    cancelled = db.query(func.count(Certificate.id)).filter(Certificate.company_id == cid,
                                                            Certificate.status == "cancelled").scalar() or 0
    return {"by_kind": rows, "issued": sum(rows.values()), "this_month": month, "cancelled": cancelled}


@cert_router.post("/issued", status_code=201)
def issue_cert(body: IssueIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(CERT_MANAGE))):
    cid = _cid(user)
    t = db.query(CertificateTemplate).filter(CertificateTemplate.company_id == cid,
                                             CertificateTemplate.id == body.template_id).first()
    if t is None:
        raise BadRequest("The certificate template was not found.")
    s = _student_by_id(db, cid, body.student_id)
    c = service.issue_certificate(db, cid, t, s, values=body.values, issued_on=body.issued_on,
                                  mark_student=body.mark_student)
    db.commit()
    db.refresh(c)
    return service.certificate_out(c, full=True)


@cert_router.get("/issued/{uuid}")
def get_cert(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(CERT_VIEW))):
    c = get_or_404(db, Certificate, _cid(user), uuid, "Certificate")
    out = service.certificate_out(c, full=True)
    out["html"] = service.certificate_document(db, _cid(user), c.template, c.body_html, pdf_mode=False)
    return out


@cert_router.get("/issued/{uuid}/pdf")
def cert_pdf(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(CERT_VIEW))):
    from modules.documents import pdf
    cid = _cid(user)
    c = get_or_404(db, Certificate, cid, uuid, "Certificate")
    body = c.body_html
    if c.status == "cancelled":
        body = ('<div style="border:1pt solid #c0392b;color:#c0392b;padding:3mm;margin-bottom:4mm;text-align:center">'
                f'CANCELLED on {c.cancelled_at.date().isoformat() if c.cancelled_at else ""}</div>') + body
    data = pdf.render_pdf(service.certificate_document(db, cid, c.template, body, title=c.number or c.title))
    audit.event(db, "certificates.downloaded", "certificates", c.id, {"number": c.number}, entity_uuid=str(c.uuid))
    db.commit()
    safe = (c.number or "certificate").replace("/", "-")
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{safe}.pdf"'})


@cert_router.post("/issued/{uuid}/cancel")
def cancel_cert(uuid: UUID, body: CancelIn, db: Session = Depends(get_db),
                user: dict = Depends(require_permission(CERT_MANAGE))):
    c = get_or_404(db, Certificate, _cid(user), uuid, "Certificate")
    service.cancel_certificate(db, _cid(user), c, body.reason)
    db.commit()
    db.refresh(c)
    return service.certificate_out(c, full=True)


# ── Public: online application on the institution's own address ─────

PUBLIC_DOC_EXT = {"pdf", "jpg", "jpeg", "png", "webp", "heic"}
PUBLIC_DOC_MAX = 5 * 1024 * 1024


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "?")


def _rate_limit(key: str, limit: int, seconds: int) -> None:
    """Count hits in Redis; refuse once over the limit. Steps aside when Redis is down."""
    try:
        from core.cache import get_redis
        r = get_redis()
        n = r.incr(key)
        if n == 1:
            r.expire(key, seconds)
    except Exception:
        return
    if n > limit:
        raise HTTPException(status_code=429, detail="Too many attempts. Please wait a while and try again.")


def _company_by_slug(db: Session, slug: str):
    from modules.company.models import Company
    with bypass_rls(db):
        comp = db.query(Company).filter(Company.slug == slug, Company.active.is_(True)).first()
    if comp is None:
        raise NotFound("Institution")
    set_tenant(db, comp.id)
    return comp


def _public_offerings(db: Session, cid: int) -> list[AdmissionOffering]:
    today = today_for_company_id(db, cid)
    rows = (db.query(AdmissionOffering).options(joinedload(AdmissionOffering.program), joinedload(AdmissionOffering.session))
            .filter(AdmissionOffering.company_id == cid).all())
    rows = [o for o in rows if service.offering_open_now(o, today)]
    rows.sort(key=lambda o: (o.session.start_date if o.session else date.min, o.program.sort_order if o.program else 0))
    return rows


def _public_custom_fields(db: Session, cid: int) -> list[dict]:
    from core import custom_fields
    from core.masters import MasterType, MasterValue
    out = []
    for d in custom_fields.definitions(db, cid, "admission"):
        options = [{"value": o, "label": o} if isinstance(o, str) else o for o in (d.options or [])]
        if d.master_type_key:
            vals = (db.query(MasterValue).join(MasterType, MasterType.id == MasterValue.type_id)
                    .filter(MasterValue.company_id == cid, MasterType.key == d.master_type_key,
                            MasterValue.active.is_(True)).order_by(MasterValue.sort_order).all())
            options = [{"value": v.id, "label": v.label} for v in vals]
        if d.field_type == "file":
            continue  # files go in the documents list
        out.append({"key": d.key, "label": d.label, "type": d.field_type, "required": d.required,
                    "help_text": d.help_text, "placeholder": d.placeholder, "options": options})
    return out


@public_router.get("/{slug}/form")
def public_form(slug: str, db: Session = Depends(get_db)):
    """What the online application page needs: institution, programs open, documents, extra fields."""
    from modules.academics.service import profile
    comp = _company_by_slug(db, slug)
    conf = service.settings(db, comp.id)
    prof = profile(comp)
    if not prof["modules"].get("admissions", True):
        raise NotFound("Online admissions")
    logo = comp.logo_url if comp.logo_url and (comp.logo_url.startswith("data:image/") or comp.logo_url.startswith("http")) else None
    offerings = _public_offerings(db, comp.id) if conf["online_enabled"] else []
    return {
        "institution": {"name": comp.company_name, "logo_url": logo, "city": comp.city, "type": prof["type"]},
        "terms": prof["terms"],
        "open": bool(conf["online_enabled"] and offerings),
        "title": conf["form_title"], "intro": conf["form_intro"], "privacy_notice": conf["privacy_notice"],
        "documents": conf["required_documents"],
        "programs": [{"session_id": o.session_id, "session_name": o.session.name, "program_id": o.program_id,
                      "program_name": o.program.name, "application_fee": service._money(o.application_fee),
                      "fee_required": o.fee_required, "closes_on": o.closes_on,
                      "min_age_years": o.min_age_years, "max_age_years": o.max_age_years,
                      "age_as_on": o.age_as_on, "test_required": o.test_required,
                      "interview_required": o.interview_required} for o in offerings],
        "custom_fields": _public_custom_fields(db, comp.id),
    }


class PublicApplication(BaseModel):
    session_id: int
    program_id: int
    first_name: str = Field(..., min_length=1, max_length=100)
    middle_name: str | None = Field(None, max_length=100)
    last_name: str | None = Field(None, max_length=100)
    gender: str | None = Field(None, pattern="^(male|female|other)$")
    date_of_birth: date
    blood_group: str | None = Field(None, max_length=10)
    nationality: str | None = Field(None, max_length=60)
    religion: str | None = Field(None, max_length=60)
    category: str | None = Field(None, max_length=40)
    mother_tongue: str | None = Field(None, max_length=60)
    previous_institution: str | None = Field(None, max_length=200)
    previous_program: str | None = Field(None, max_length=120)
    previous_marks: str | None = Field(None, max_length=60)
    address: str | None = Field(None, max_length=1000)
    city: str | None = Field(None, max_length=100)
    state: str | None = Field(None, max_length=100)
    pincode: str | None = Field(None, max_length=12)
    contact_phone: str = Field(..., min_length=6, max_length=20)
    contact_email: EmailStr | None = None
    guardians: list[GuardianIn] = Field(..., min_length=1, max_length=3)
    custom: dict = {}
    consent: bool = False
    website: str | None = None  # honeypot: people never fill it


@public_router.post("/{slug}/apply", status_code=201)
async def public_apply(slug: str, request: Request, db: Session = Depends(get_db)):
    """Multipart form: `data` (JSON of PublicApplication) and one file per document (`doc_<key>`)."""
    ip = _client_ip(request)
    form = await request.form()
    try:
        raw = json.loads(str(form.get("data") or "{}"))
    except ValueError:
        raise BadRequest("The form could not be read. Please try again.")
    try:
        body = PublicApplication.model_validate(raw)
    except Exception as exc:
        from pydantic import ValidationError
        if isinstance(exc, ValidationError):
            err = exc.errors()[0]
            field = " ".join(str(p) for p in err.get("loc", []) if not isinstance(p, int)).replace("_", " ")
            raise BadRequest(f"Check {field}: {err.get('msg', 'invalid value')}.")
        raise
    if body.website:
        return {"application_no": None, "status": "received"}
    if not body.consent:
        raise BadRequest("Please agree to the privacy notice to send the application.")
    comp = _company_by_slug(db, slug)
    _rate_limit(f"adm:apply:{comp.slug}:ip:{ip}", 10, 3600)
    _rate_limit(f"adm:apply:{comp.slug}:ph:{body.contact_phone.strip()}", 5, 86400)
    conf = service.settings(db, comp.id)
    offering = next((o for o in _public_offerings(db, comp.id)
                     if o.session_id == body.session_id and o.program_id == body.program_id), None)
    if not conf["online_enabled"] or offering is None:
        raise BadRequest("Applications for this program are not open right now.")
    # Files first, so a bad file stops the application before anything is saved.
    from modules.documents import storage
    files: dict[str, tuple[str, bytes, str | None]] = {}
    for d in conf["required_documents"]:
        up = form.get(f"doc_{d['key']}")
        if up is None or not getattr(up, "filename", None):
            if d.get("required"):
                raise BadRequest(f"Upload: {d['label']}.")
            continue
        data = await up.read()
        ext = up.filename.lower().rsplit(".", 1)[-1] if "." in up.filename else ""
        if ext not in PUBLIC_DOC_EXT:
            raise BadRequest(f"{d['label']}: upload a PDF or a photo (JPG, PNG).")
        if len(data) > PUBLIC_DOC_MAX:
            raise BadRequest(f"{d['label']}: the file should be smaller than 5 MB.")
        files[d["key"]] = (up.filename, data, up.content_type)
    # Same phone already applied for the same program and session: refuse a duplicate.
    dup = db.query(Application.id).filter(
        Application.company_id == comp.id, Application.session_id == body.session_id,
        Application.program_id == body.program_id, Application.contact_phone == body.contact_phone.strip(),
        func.lower(Application.first_name) == body.first_name.strip().lower(),
        Application.status.notin_(("withdrawn", "rejected")), Application.archived_at.is_(None)).first()
    if dup:
        raise BadRequest("An application for this child and program has already been received. "
                         "Use the status check with your application number.")
    data = body.model_dump(exclude={"guardians", "custom", "consent", "website"})
    data["contact_phone"] = data["contact_phone"].strip()
    # Link to an open enquiry from the same phone (website enquiries, walk-ins).
    enquiry = (db.query(Enquiry).filter(Enquiry.company_id == comp.id, Enquiry.phone == data["contact_phone"],
                                        Enquiry.application_id.is_(None), Enquiry.archived_at.is_(None),
                                        Enquiry.stage.in_(("new", "contacted", "visit_scheduled", "lost")))
               .order_by(Enquiry.created_at.desc()).first())
    a = service.create_application(db, comp.id, data, guardians=[g.model_dump() for g in body.guardians],
                                   custom=body.custom, channel="online", enquiry=enquiry,
                                   actor_name=f"{body.guardians[0].first_name} (online)")
    for key, (name, raw_bytes, ctype) in files.items():
        blob = storage.store(db, comp.id, name, raw_bytes, ctype, "admission")
        service.attach_document(db, comp.id, a, key, blob)
    audit.event(db, "admissions.applied_online", "admission_applications", a.id,
                {"application_no": a.application_no, "ip": ip}, company_id=comp.id, entity_uuid=str(a.uuid),
                entity_label=a.full_name)
    try:
        from modules.notifications.service import notify
        notify(db, comp.id, [enquiry.counsellor_id if enquiry and enquiry.counsellor_id else None], kind="admissions",
               title=f"New online application: {a.full_name}",
               body=f"{a.program.name if a.program else ''} · {a.application_no}",
               link=f"/dashboard/admissions/applications?view={a.uuid}")
    except Exception:
        pass
    service.send_acknowledgement(db, a)
    db.commit()
    return {"application_no": a.application_no, "status": "submitted",
            "program": offering.program.name, "session": offering.session.name,
            "fee_amount": service._money(a.fee_amount) if offering.fee_required else None}


class PublicEnquiry(BaseModel):
    child_first_name: str = Field(..., min_length=1, max_length=100)
    child_last_name: str | None = Field(None, max_length=100)
    child_dob: date | None = None
    parent_name: str = Field(..., min_length=1, max_length=200)
    phone: str = Field(..., min_length=6, max_length=20)
    email: EmailStr | None = None
    program_id: int | None = None
    session_id: int | None = None
    message: str | None = Field(None, max_length=2000)
    website: str | None = None


@public_router.post("/{slug}/enquiry", status_code=201)
def public_enquiry(slug: str, body: PublicEnquiry, request: Request, db: Session = Depends(get_db)):
    """A short 'ask us' form on the website: becomes an enquiry with source Website."""
    if body.website:
        return {"status": "received"}
    comp = _company_by_slug(db, slug)
    _rate_limit(f"adm:enq:{comp.slug}:ip:{_client_ip(request)}", 10, 3600)
    open_ids = {(o.session_id, o.program_id) for o in _public_offerings(db, comp.id)}
    prog_ok = body.program_id is None or any(p == body.program_id for _, p in open_ids)
    service.create_enquiry(db, comp.id, {
        "child_first_name": body.child_first_name, "child_last_name": body.child_last_name, "child_dob": body.child_dob,
        "parent_name": body.parent_name, "phone": body.phone.strip(), "email": body.email,
        "program_id": body.program_id if prog_ok else None,
        "session_id": body.session_id if body.session_id and any(s == body.session_id for s, _ in open_ids) else None,
        "source_id": service.source_id_by_code(db, comp.id, "website"), "notes": body.message,
    }, actor_name=f"{body.parent_name} (website)")
    db.commit()
    return {"status": "received"}


class StatusCheckIn(BaseModel):
    application_no: str = Field(..., min_length=3, max_length=40)
    contact: str = Field(..., min_length=4, max_length=255)  # phone or email used on the form


PUBLIC_STEPS = {
    "draft": "Not sent yet", "submitted": "Received", "under_review": "Being reviewed",
    "test_scheduled": "Entrance test scheduled", "interview": "Interview scheduled", "shortlisted": "Shortlisted",
    "offered": "Seat offered", "accepted": "Offer accepted", "enrolled": "Admission confirmed",
    "rejected": "Not selected", "waitlisted": "On the waiting list", "withdrawn": "Withdrawn",
}


def _digits(v: str | None) -> str:
    return "".join(ch for ch in (v or "") if ch.isdigit())


@public_router.post("/{slug}/status")
def public_status(slug: str, body: StatusCheckIn, request: Request, db: Session = Depends(get_db)):
    """Status of an application for the family: needs the application number and the same phone
    or email. A wrong pair gets the same answer as an unknown number (nothing leaks)."""
    comp = _company_by_slug(db, slug)
    _rate_limit(f"adm:status:{comp.slug}:ip:{_client_ip(request)}", 20, 900)
    a = (db.query(Application).options(joinedload(Application.program), joinedload(Application.session))
         .filter(Application.company_id == comp.id, Application.archived_at.is_(None),
                 func.upper(Application.application_no) == body.application_no.strip().upper()).first())
    contact = body.contact.strip().lower()
    ok = a is not None and (
        (a.contact_email and contact == a.contact_email.lower()) or
        (len(_digits(contact)) >= 6 and a.contact_phone and _digits(a.contact_phone)[-10:] == _digits(contact)[-10:]))
    if not ok:
        raise NotFound("No application matches these details")
    pending_docs = [d["label"] for d in (a.documents or []) if d.get("required") and not d.get("uuid")]
    return {
        "application_no": a.application_no, "applicant": a.first_name, "program": a.program.name if a.program else None,
        "session": a.session.name if a.session else None, "status": a.status,
        "status_label": PUBLIC_STEPS.get(a.status, a.status), "submitted_at": a.submitted_at,
        "test_at": a.test_at if a.status == "test_scheduled" else None, "test_venue": a.test_venue if a.status == "test_scheduled" else None,
        "interview_at": a.interview_at if a.status == "interview" else None,
        "offer_expires_on": a.offer_expires_on if a.status == "offered" else None,
        "fee_amount": service._money(a.fee_amount) if a.fee_amount and a.fee_amount > 0 else None, "fee_paid": a.fee_paid,
        "documents_pending": pending_docs,
    }
