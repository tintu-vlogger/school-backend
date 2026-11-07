"""Onboarding API: dashboard and board, flows and settings, one onboarding's details,
review, documents, assets and accounts, day one, check-ins; plus the public pre-joining
portal opened with a personal link."""
from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from core.db import bypass_rls, get_db, set_tenant
from core.errors import BadRequest, Forbidden, NotFound
from core.pagination import PageParams, paginate
from core.security import get_current_user
from modules.onboarding import forms as sforms, service, settings as osettings
from modules.onboarding.models import Onboarding, OnboardingCheckin, OnboardingFlow
from modules.recruitment import common as c

router = APIRouter(prefix="/onboarding", tags=["Onboarding"])
public_router = APIRouter(prefix="/public/joining", tags=["Onboarding (public)"])


class _In(BaseModel):
    model_config = ConfigDict(extra="ignore")


class StartIn(_In):
    employee_id: int
    flow_uuid: UUID | None = None
    joining_date: date | None = None
    buddy_employee_id: int | None = None
    hr_owner_employee_id: int | None = None
    send_invite: bool = True


class UpdateIn(_In):
    buddy_employee_id: int | None = None
    hr_owner_employee_id: int | None = None
    joining_date: date | None = None
    flow_uuid: UUID | None = None


class ReviewIn(_In):
    items: list[dict]


class FormsIn(_In):
    kinds: list[str] | None = None


class AssetIn(_In):
    asset_uuid: UUID


class AccountIn(_In):
    kind: str
    note: str | None = None


class ReasonIn(_In):
    reason: str | None = None


class CompleteIn(_In):
    force: bool = False


class PortalIn(_In):
    enabled: bool
    extend_days: int | None = None


class CheckinIn(_In):
    status: str = "done"
    rating: int | None = None
    went_well: str | None = None
    concerns: str | None = None
    support_needed: str | None = None


def _manage(db: Session, user: dict) -> None:
    service.require_manage(db, user)


# ── Meta, dashboard, settings, flows ────────────────────────────────

@router.get("/meta")
def meta(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    return {"can_manage": service.can_manage(db, user), "statuses": service.STATUSES, "sections": osettings.SECTIONS,
            "owners": osettings.OWNERS, "forms": sforms.FORMS,
            "account_kinds": {k: v[0] for k, v in service.ACCOUNT_KINDS.items()}}


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _manage(db, user)
    return service.dashboard(db, user["company_id"])


@router.get("/settings")
def get_settings_(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _manage(db, user)
    return osettings.get(db, user["company_id"])


@router.put("/settings")
def put_settings(body: dict, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _manage(db, user)
    out = osettings.save(db, user["company_id"], body)
    db.commit()
    return out


@router.get("/flows")
def list_flows(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _manage(db, user)
    rows = (db.query(OnboardingFlow).filter(OnboardingFlow.company_id == user["company_id"])
            .order_by(OnboardingFlow.priority, OnboardingFlow.id).all())
    counts = dict(db.query(Onboarding.flow_id, func.count(Onboarding.id))
                  .filter(Onboarding.company_id == user["company_id"],
                          Onboarding.status.in_(("pre_joining", "in_progress"))).group_by(Onboarding.flow_id).all())
    return {"items": [service.flow_out(db, f, counts) for f in rows]}


@router.post("/flows", status_code=status.HTTP_201_CREATED)
def create_flow(body: dict, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _manage(db, user)
    f = service.save_flow(db, user["company_id"], body)
    db.commit()
    return service.flow_out(db, f)


@router.put("/flows/{uuid}")
def update_flow(uuid: UUID, body: dict, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _manage(db, user)
    f = service.save_flow(db, user["company_id"], body, service.get_flow(db, user["company_id"], uuid))
    db.commit()
    return service.flow_out(db, f)


@router.get("/flows/preview")
def preview_flow(employee_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _manage(db, user)
    emp = c.employee(db, user["company_id"], employee_id)
    if emp is None:
        raise NotFound("Employee")
    f = service.pick_flow(db, user["company_id"], emp)
    return {"flow": service.flow_out(db, f) if f else None}


# ── Onboardings ─────────────────────────────────────────────────────

@router.get("/onboardings")
def list_onboardings(status_: str | None = Query(None, alias="status"), q: str | None = None,
                     review_status: str | None = None, params: PageParams = Depends(),
                     db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _manage(db, user)
    E = c.Employee()
    cid = user["company_id"]
    qr = (db.query(Onboarding).options(joinedload(Onboarding.flow))
          .join(E, E.id == Onboarding.employee_id).filter(Onboarding.company_id == cid))
    if status_:
        qr = qr.filter(Onboarding.status.in_(status_.split(",")))
    if review_status:
        qr = qr.filter(Onboarding.review_status == review_status)
    if q:
        qr = qr.filter(or_(E.first_name.ilike(f"%{q}%"), E.last_name.ilike(f"%{q}%"), E.email.ilike(f"%{q}%")))
    page = paginate(qr.order_by(Onboarding.joining_date.desc()), params)
    rows = page["items"]
    emps = {e.id: e for e in db.query(E).filter(E.company_id == cid, E.id.in_([o.employee_id for o in rows] or [0]))}
    names = c.names_for(db, cid, [x for o in rows for x in (o.buddy_employee_id, o.hr_owner_employee_id,
                                                            emps[o.employee_id].reporting_manager_id)])
    page["items"] = [service.out(db, o, emp=emps[o.employee_id], names=names, progress=service.task_progress(db, o))
                     for o in rows]
    return page


@router.post("/onboardings", status_code=status.HTTP_201_CREATED)
def start(body: StartIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _manage(db, user)
    cid = user["company_id"]
    emp = c.employee(db, cid, body.employee_id)
    if emp is None:
        raise NotFound("Employee")
    if emp.status in ("exited", "archived"):
        raise BadRequest("This person has left.")
    flow = service.get_flow(db, cid, body.flow_uuid) if body.flow_uuid else None
    o = service.start(db, cid, emp, flow=flow, joining_date=body.joining_date, buddy_id=body.buddy_employee_id,
                      hr_owner_id=body.hr_owner_employee_id, send_invite=body.send_invite)
    db.commit()
    return service.out(db, o, full=True)


@router.get("/onboardings/{uuid}")
def detail(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _manage(db, user)
    return service.out(db, service.get(db, user["company_id"], uuid), full=True)


@router.put("/onboardings/{uuid}")
def update(uuid: UUID, body: UpdateIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _manage(db, user)
    cid = user["company_id"]
    o = service.get(db, cid, uuid)
    data = body.model_dump(exclude_unset=True)
    for k in ("buddy_employee_id", "hr_owner_employee_id"):
        if k in data:
            if data[k] and c.employee(db, cid, data[k]) is None:
                raise BadRequest("The person was not found.")
            setattr(o, k, data[k])
    if data.get("joining_date") and data["joining_date"] != o.joining_date:
        if o.day_one_started_at:
            raise BadRequest("The person has already joined.")
        from modules.employees import service as people
        emp = c.employee(db, cid, o.employee_id)
        o.joining_date = data["joining_date"]
        people.update(db, cid, emp, {"date_of_joining": data["joining_date"]}, reason="Joining date changed")
        service._log(o, f"Joining date changed to {o.joining_date.isoformat()}")
    if data.get("flow_uuid"):
        o.flow = service.get_flow(db, cid, data["flow_uuid"])
        service._log(o, f"Flow changed to “{o.flow.name}”")
        emp = c.employee(db, cid, o.employee_id)
        service._create_tasks(db, o, emp, "pre_joining")
        if o.day_one_started_at:
            service._create_tasks(db, o, emp, "day_one")
    db.commit()
    return detail(uuid, db, user)


@router.post("/onboardings/{uuid}/invite")
def invite(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _manage(db, user)
    o = service.get(db, user["company_id"], uuid)
    from datetime import timedelta
    conf = osettings.get(db, user["company_id"])
    o.portal_enabled = True
    o.portal_expires_at = c.now() + timedelta(days=int(conf["portal_link_days"]))
    service.send_portal_invite(db, o)
    db.commit()
    return detail(uuid, db, user)


@router.post("/onboardings/{uuid}/portal")
def portal_switch(uuid: UUID, body: PortalIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _manage(db, user)
    from datetime import timedelta
    o = service.get(db, user["company_id"], uuid)
    o.portal_enabled = body.enabled
    if body.extend_days:
        o.portal_expires_at = c.now() + timedelta(days=max(1, min(body.extend_days, 90)))
    service._log(o, "Joining link switched " + ("on" if body.enabled else "off"), "portal")
    db.commit()
    return detail(uuid, db, user)


@router.post("/onboardings/{uuid}/review")
def review(uuid: UUID, body: ReviewIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _manage(db, user)
    service.review(db, service.get(db, user["company_id"], uuid), body.items)
    db.commit()
    return detail(uuid, db, user)


@router.post("/onboardings/{uuid}/accept-all")
def accept_all(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _manage(db, user)
    o = service.get(db, user["company_id"], uuid)
    service.accept_all(db, o)
    service.apply_to_record(db, o)
    db.commit()
    return detail(uuid, db, user)


@router.post("/onboardings/{uuid}/apply")
def apply(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _manage(db, user)
    o = service.get(db, user["company_id"], uuid)
    res = service.apply_to_record(db, o)
    db.commit()
    return {**detail(uuid, db, user), "written": res["written"]}


@router.post("/onboardings/{uuid}/appointment-letter")
def appointment_letter(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _manage(db, user)
    service.generate_appointment_letter(db, service.get(db, user["company_id"], uuid))
    db.commit()
    return detail(uuid, db, user)


@router.post("/onboardings/{uuid}/forms")
def statutory_forms(uuid: UUID, body: FormsIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _manage(db, user)
    service.generate_forms(db, service.get(db, user["company_id"], uuid), body.kinds)
    db.commit()
    return detail(uuid, db, user)


@router.post("/onboardings/{uuid}/assets")
def assign_asset(uuid: UUID, body: AssetIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _manage(db, user)
    service.assign_asset(db, service.get(db, user["company_id"], uuid), body.asset_uuid)
    db.commit()
    return detail(uuid, db, user)


@router.post("/onboardings/{uuid}/accounts")
def request_account(uuid: UUID, body: AccountIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _manage(db, user)
    service.request_account(db, service.get(db, user["company_id"], uuid), body.kind, body.note)
    db.commit()
    return detail(uuid, db, user)


@router.post("/onboardings/{uuid}/day-one")
def day_one(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _manage(db, user)
    o = service.get(db, user["company_id"], uuid)
    if o.joining_date > c.today(db, user["company_id"]):
        raise BadRequest("The joining date has not come yet. Change the joining date first if they joined early.")
    service.start_day_one(db, o)
    db.commit()
    return detail(uuid, db, user)


@router.post("/onboardings/{uuid}/complete")
def complete(uuid: UUID, body: CompleteIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _manage(db, user)
    service.complete(db, service.get(db, user["company_id"], uuid), body.force)
    db.commit()
    return detail(uuid, db, user)


@router.post("/onboardings/{uuid}/cancel")
def cancel(uuid: UUID, body: ReasonIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _manage(db, user)
    service.cancel(db, service.get(db, user["company_id"], uuid), body.reason)
    db.commit()
    return detail(uuid, db, user)


# ── Check-ins ───────────────────────────────────────────────────────

@router.get("/checkins")
def list_checkins(scope: str = "mine", status_: str | None = Query("pending", alias="status"),
                  db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    cid, eid = user["company_id"], user.get("employee_id")
    qr = db.query(OnboardingCheckin).options(joinedload(OnboardingCheckin.onboarding)).filter(OnboardingCheckin.company_id == cid)
    if scope == "all":
        _manage(db, user)
    else:
        qr = qr.filter(OnboardingCheckin.manager_employee_id == (eid or -1))
    if status_:
        qr = qr.filter(OnboardingCheckin.status.in_(status_.split(",")))
    rows = qr.order_by(OnboardingCheckin.due_on).limit(300).all()
    names = c.names_for(db, cid, [x for r in rows for x in (r.employee_id, r.manager_employee_id)])
    return {"items": [{**service.checkin_out(r, names), "employee_name": names.get(r.employee_id),
                       "onboarding_uuid": str(r.onboarding.uuid)} for r in rows]}


@router.post("/checkins/{uuid}")
def save_checkin(uuid: UUID, body: CheckinIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    ch = (db.query(OnboardingCheckin).options(joinedload(OnboardingCheckin.onboarding))
          .filter(OnboardingCheckin.company_id == user["company_id"], OnboardingCheckin.uuid == uuid).first())
    if ch is None:
        raise NotFound("Check-in")
    service.save_checkin(db, user, ch, body.model_dump(exclude_unset=True))
    db.commit()
    return service.checkin_out(ch)


@router.get("/me")
def my_onboarding(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """The signed-in employee's own onboarding: checklist, induction and check-ins."""
    eid = user.get("employee_id")
    if not eid:
        return {"onboarding": None}
    o = (db.query(Onboarding).options(joinedload(Onboarding.flow), joinedload(Onboarding.checkins))
         .filter(Onboarding.company_id == user["company_id"], Onboarding.employee_id == eid,
                 Onboarding.status != "cancelled").order_by(Onboarding.id.desc()).first())
    if o is None:
        return {"onboarding": None}
    full = service.out(db, o, full=True)
    keep = ("uuid", "name", "joining_date", "status", "status_label", "buddy_name", "manager_name", "hr_owner_name",
            "induction", "checkins", "tasks", "flow_name", "days_to_join")
    return {"onboarding": {k: full.get(k) for k in keep}}


# ── Public pre-joining portal ───────────────────────────────────────

def _portal(db: Session, token: str) -> Onboarding:
    with bypass_rls(db):
        row = db.query(Onboarding.company_id).filter(Onboarding.portal_token == token).first()
    if row is None or not token:
        raise NotFound("Joining link")
    set_tenant(db, row.company_id)
    o = service.by_token(db, token)
    service.check_portal(o)
    return o


def _portal_out(db: Session, o: Onboarding) -> dict:
    emp = c.employee(db, o.company_id, o.employee_id)
    comp = c.company(db, o.company_id)
    labels = c.org_labels(db, o.company_id, departments=[emp.department_id], designations=[emp.designation_id])
    rv = {k: v for k, v in (o.review or {}).items() if k != "_applied"}
    from modules.documents import service as documents
    logo = comp.logo_url if comp and comp.logo_url and (comp.logo_url.startswith("data:image/") or comp.logo_url.startswith("http")) else None
    return {"company": {"name": comp.company_name if comp else None, "logo_url": logo},
            "joiner": {"first_name": emp.first_name, "last_name": emp.last_name,
                       "designation": labels["designation"].get(emp.designation_id),
                       "department": labels["department"].get(emp.department_id)},
            "joining_date": o.joining_date, "expires_at": o.portal_expires_at,
            "sections": [{"key": s, "label": osettings.SECTIONS[s]} for s in service.sections_for(o)],
            "data": service._masked(o.portal_data or {}), "review": rv, "review_status": o.review_status,
            "submitted_at": o.portal_submitted_at, "completeness": service.completeness(o),
            "required_documents": (o.flow.documents if o.flow else []) or [],
            "uploads": [{**u, "file": documents.file_link(db, o.company_id, u["file_id"])} for u in o.uploads or []],
            "locked": o.review_status == "accepted"}


@public_router.get("/{token}")
def portal(token: str, db: Session = Depends(get_db)):
    o = _portal(db, token)
    if o.portal_opened_at is None:
        o.portal_opened_at = c.now()
        service._log(o, "Joiner opened the joining link", "portal")
        db.commit()
    return _portal_out(db, o)


@public_router.get("/{token}/options")
def portal_options(token: str, db: Session = Depends(get_db)):
    o = _portal(db, token)
    from core.masters import get_type, values_query
    out = {}
    for key in ("gender", "marital_status", "blood_group", "nationality", "relation", "bank", "state", "education_level"):
        try:
            t = get_type(db, o.company_id, key)
            out[key] = [{"value": v.id, "label": v.label} for v in values_query(db, o.company_id, t, active=True).all()]
        except Exception:
            out[key] = []
    return out


@public_router.put("/{token}/sections/{section}")
def portal_save(token: str, section: str, body: dict, db: Session = Depends(get_db)):
    o = _portal(db, token)
    data = body.get("data") if isinstance(body, dict) and "data" in body else body
    # masked values sent back mean "unchanged"
    if section in ("statutory", "bank") and isinstance(data, dict):
        old = (o.portal_data or {}).get(section) or {}
        for k in ("aadhaar", "account_number"):
            if isinstance(data.get(k), str) and ("X" in data[k] or "•" in data[k]) and old.get(k):
                data[k] = old[k]
    service.save_section(db, o, section, data)
    db.commit()
    return _portal_out(db, o)


@public_router.post("/{token}/uploads")
async def portal_upload(token: str, label: str = Form(...), file: UploadFile = File(...), db: Session = Depends(get_db)):
    o = _portal(db, token)
    if o.review_status == "accepted":
        raise BadRequest("HR has already accepted your details.")
    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        raise BadRequest("Files should be smaller than 5 MB.")
    ext = (file.filename or "").lower().rsplit(".", 1)[-1]
    if ext not in ("pdf", "jpg", "jpeg", "png", "webp", "heic", "doc", "docx"):
        raise BadRequest("Upload a PDF or a photo (JPG or PNG).")
    from modules.documents import storage
    blob = storage.store(db, o.company_id, file.filename or "document", raw, file.content_type, "onboarding")
    service.add_upload(db, o, label, blob)
    rv = dict(o.review or {})
    key = f"doc:{label.strip()}"
    if rv.get(key, {}).get("status") == "sent_back":
        rv[key] = {**rv[key], "status": "updated"}
        o.review = rv
    db.commit()
    return _portal_out(db, o)


@public_router.post("/{token}/submit")
def portal_submit(token: str, db: Session = Depends(get_db)):
    o = _portal(db, token)
    service.submit_portal(db, o)
    db.commit()
    return _portal_out(db, o)


extra_routers = [public_router]
