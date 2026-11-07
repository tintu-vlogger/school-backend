"""Admissions rules: enquiry pipeline, application statuses, seats and merit list, offers,
conversion to a student, and certificates.

Functions other modules (and the public form) use:
    settings(db, company_id)                      -> dict with defaults filled in
    seats_summary(db, company_id, offering)       -> {seats, taken, available, ...}
    change_stage(db, company_id, enquiry, ...)    -> pipeline move with the rules below
    change_status(db, company_id, application, ...)
    offer(db, company_id, application, ...)       -> starts the "admissions.offer" approval
    enrol(db, company_id, application, ...)       -> creates the student, guardians and enrolment
    issue_certificate(db, company_id, ...)        -> numbered certificate in the register

Enquiry stages: new -> contacted -> visit_scheduled -> applied -> converted, or lost.
  - "converted" is set only when the applicant is enrolled (never by hand) and is final.
  - "lost" needs a reason; a lost enquiry can only be reopened to new or contacted.
  - "visit_scheduled" needs the visit date and time.
Application statuses follow ALLOWED below; "offered" goes through `offer`, "enrolled" through `enrol`.
"""
from __future__ import annotations

import copy
import html as html_mod
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.orm.attributes import flag_modified

from core import audit, context
from core.errors import BadRequest, NotFound
from modules.academics import service as academics
from modules.academics.models import AcademicSession, Program, Section
from modules.admissions.models import (AdmissionActivity, AdmissionOffering, AdmissionSettings, Application,
                                       Certificate, CertificateTemplate, Enquiry)

# ── Words ────────────────────────────────────────────────────────────

STAGES = ["new", "contacted", "visit_scheduled", "applied", "converted", "lost"]
STAGE_LABELS = {"new": "New", "contacted": "Contacted", "visit_scheduled": "Visit scheduled", "applied": "Applied",
                "converted": "Converted", "lost": "Lost"}
OPEN_STAGES = ("new", "contacted", "visit_scheduled", "applied")

STATUSES = ["draft", "submitted", "under_review", "test_scheduled", "interview", "shortlisted", "offered", "accepted",
            "enrolled", "rejected", "waitlisted", "withdrawn"]
STATUS_LABELS = {"draft": "Draft", "submitted": "Submitted", "under_review": "Under review",
                 "test_scheduled": "Test scheduled", "interview": "Interview", "shortlisted": "Shortlisted",
                 "offered": "Offered", "accepted": "Accepted", "enrolled": "Enrolled", "rejected": "Rejected",
                 "waitlisted": "Waitlisted", "withdrawn": "Withdrawn"}
_REVIEW = ["under_review", "test_scheduled", "interview", "shortlisted", "waitlisted", "rejected", "withdrawn"]
# Where an application may move by hand. "offered" only through offer(), "enrolled" only through enrol().
ALLOWED = {
    "draft": ["submitted", "withdrawn"],
    "submitted": _REVIEW,
    "under_review": _REVIEW,
    "test_scheduled": _REVIEW,
    "interview": _REVIEW,
    "shortlisted": _REVIEW,
    "waitlisted": ["under_review", "shortlisted", "rejected", "withdrawn"],
    "offered": ["accepted", "withdrawn", "rejected", "waitlisted"],
    "accepted": ["withdrawn"],
    "enrolled": [],
    "rejected": ["under_review"],
    "withdrawn": ["under_review"],
}
OFFERABLE = ("submitted", "under_review", "test_scheduled", "interview", "shortlisted", "waitlisted")
RANKABLE = OFFERABLE
SEAT_HOLDING = ("offered", "accepted", "enrolled")
IN_PROGRESS = ("submitted", "under_review", "test_scheduled", "interview", "shortlisted", "waitlisted", "offered",
               "accepted")

CERT_KINDS = {"transfer": "Transfer certificate", "leaving": "School leaving certificate", "bonafide": "Bonafide certificate",
              "character": "Character certificate", "conduct": "Conduct certificate", "study": "Study certificate",
              "migration": "Migration certificate", "custom": "Other certificate"}

D2 = Decimal("0.01")


def _money(v) -> str:
    return str(Decimal(v or 0).quantize(D2))


# ── Settings ─────────────────────────────────────────────────────────

DEFAULT_SETTINGS = {
    "online_enabled": True,
    "form_title": "Apply for admission",
    "form_intro": "Fill in the form below and upload the documents asked for. You will get an application number "
                  "to check the status of the application later.",
    "privacy_notice": "We use these details only to process this admission application.",
    "required_documents": [
        {"key": "birth_certificate", "label": "Birth certificate", "required": True},
        {"key": "photo", "label": "Recent passport-size photograph", "required": True},
        {"key": "address_proof", "label": "Address proof", "required": False},
        {"key": "previous_report", "label": "Last report card or transfer certificate", "required": False},
    ],
    "ack_email_subject": "We received your application {{application.number}}",
    "ack_email_body": (
        "Dear {{guardian.name}},\n\nThank you for applying to {{institution.name}} for {{application.program}} "
        "({{application.session}}). Your application number is {{application.number}}.\n\nYou can check the status "
        "of the application on our website with this number and your phone number or email.\n\nAdmissions office\n"
        "{{institution.name}}"),
    "offer_email_body": (
        "Dear {{guardian.name}},\n\nWe are happy to offer {{applicant.name}} a place in {{application.program}} for "
        "{{application.session}}. Please confirm by {{offer.expires_on}}.\n\nAdmissions office\n{{institution.name}}"),
    "offer_letter_html": (
        "<p class=\"right\">{{offer.date}}</p>"
        "<p>Ref: {{application.number}}</p>"
        "<p>Dear {{guardian.name}},</p>"
        "<h2>Offer of admission</h2>"
        "<p>We are pleased to offer <b>{{applicant.name}}</b> admission to <b>{{application.program}}</b> for the "
        "{{application.session}} session at {{institution.name}}.</p>"
        "<p>Please confirm acceptance and complete the admission formalities on or before <b>{{offer.expires_on}}</b>. "
        "The offer lapses after that date.</p>"
        "<p>{{offer.note}}</p>"
        "<p>We look forward to welcoming {{applicant.first_name}}.</p>"
        "<p style=\"margin-top:3em\">Principal<br/>{{institution.name}}</p>"),
    "offer_valid_days": 14,
    "test_weight": 70,
    "interview_weight": 30,
}


def _settings_row(db: Session, company_id: int) -> AdmissionSettings:
    row = db.query(AdmissionSettings).filter(AdmissionSettings.company_id == company_id).first()
    if row is None:
        row = AdmissionSettings(company_id=company_id, config={})
        db.add(row)
        db.flush()
    return row


def settings(db: Session, company_id: int) -> dict:
    row = db.query(AdmissionSettings).filter(AdmissionSettings.company_id == company_id).first()
    conf = copy.deepcopy(DEFAULT_SETTINGS)
    if row and isinstance(row.config, dict):
        conf.update({k: v for k, v in row.config.items() if k in DEFAULT_SETTINGS})
    return conf


def save_settings(db: Session, company_id: int, data: dict) -> dict:
    row = _settings_row(db, company_id)
    conf = dict(row.config or {})
    for k, v in data.items():
        if k in DEFAULT_SETTINGS and v is not None:
            conf[k] = v
    docs = conf.get("required_documents")
    if isinstance(docs, list):
        seen, clean = set(), []
        for d in docs:
            label = str((d or {}).get("label") or "").strip()[:120]
            if not label:
                continue
            key = re.sub(r"[^a-z0-9]+", "_", str(d.get("key") or label).lower()).strip("_")[:40] or "doc"
            while key in seen:
                key += "_2"
            seen.add(key)
            clean.append({"key": key, "label": label, "required": bool(d.get("required"))})
        conf["required_documents"] = clean[:20]
    tw, iw = conf.get("test_weight", 70), conf.get("interview_weight", 30)
    if not (0 <= int(tw) <= 100 and 0 <= int(iw) <= 100) or int(tw) + int(iw) == 0:
        raise BadRequest("Give the test and interview weights between 0 and 100 (not both 0).")
    row.config = conf
    flag_modified(row, "config")
    db.flush()
    return settings(db, company_id)


# ── Small helpers ────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _actor() -> tuple[int | None, str]:
    ctx = context.current()
    return ctx.employee_id, (ctx.actor_name or "Admissions office")


def log(db: Session, company_id: int, *, enquiry: Enquiry | None = None, application: Application | None = None,
        kind: str = "note", body: str | None = None, from_value: str | None = None,
        to_value: str | None = None, actor_name: str | None = None) -> AdmissionActivity:
    emp_id, name = _actor()
    row = AdmissionActivity(company_id=company_id, enquiry_id=enquiry.id if enquiry else None,
                            application_id=application.id if application else None, kind=kind, body=body,
                            from_value=from_value, to_value=to_value, actor_employee_id=emp_id,
                            actor_name=actor_name or name)
    db.add(row)
    return row


def activities(db: Session, company_id: int, *, enquiry_id: int | None = None,
               application_id: int | None = None) -> list[dict]:
    q = db.query(AdmissionActivity).filter(AdmissionActivity.company_id == company_id)
    conds = []
    if enquiry_id:
        conds.append(AdmissionActivity.enquiry_id == enquiry_id)
    if application_id:
        conds.append(AdmissionActivity.application_id == application_id)
    if not conds:
        return []
    rows = q.filter(or_(*conds)).order_by(AdmissionActivity.created_at.desc(), AdmissionActivity.id.desc()).limit(200).all()
    return [{"id": a.id, "uuid": str(a.uuid), "kind": a.kind, "body": a.body, "from_value": a.from_value,
             "to_value": a.to_value, "actor_name": a.actor_name, "created_at": a.created_at,
             "on": "application" if a.application_id else "enquiry"} for a in rows]


def _number(db: Session, company_id: int, key: str, model, column, prefix: str) -> str:
    from core.numbering import next_number
    num = next_number(db, company_id, key)
    if num:
        return num
    n = db.query(func.count(model.id)).filter(model.company_id == company_id).scalar() or 0
    return f"{prefix}{n + 1:05d}"


def _check_session_program(db: Session, company_id: int, session_id: int | None, program_id: int | None):
    sess = prog = None
    if session_id is not None:
        sess = db.query(AcademicSession).filter(AcademicSession.company_id == company_id,
                                                AcademicSession.id == session_id).first()
        if sess is None:
            raise BadRequest("The session was not found.")
    if program_id is not None:
        prog = db.query(Program).filter(Program.company_id == company_id, Program.id == program_id).first()
        if prog is None:
            raise BadRequest("The program was not found.")
    return sess, prog


def _check_employee(db: Session, company_id: int, employee_id: int | None):
    if employee_id is None:
        return None
    from modules.employees.models import Employee
    emp = db.query(Employee).filter(Employee.company_id == company_id, Employee.id == employee_id).first()
    if emp is None:
        raise BadRequest("The staff member was not found.")
    return emp


def _check_source(db: Session, company_id: int, source_id: int | None) -> None:
    if source_id is None:
        return
    from core.masters import validate_value_ids
    validate_value_ids(db, company_id, "enquiry_source", [source_id])


def source_id_by_code(db: Session, company_id: int, code: str) -> int | None:
    from core.masters import MasterType, MasterValue
    row = (db.query(MasterValue.id).join(MasterType, MasterType.id == MasterValue.type_id)
           .filter(MasterValue.company_id == company_id, MasterType.key == "enquiry_source", MasterValue.code == code)
           .first())
    return row[0] if row else None


def _emp_name(emp) -> str | None:
    return f"{emp.first_name} {emp.last_name or ''}".strip() if emp else None


def default_session_id(db: Session, company_id: int) -> int | None:
    """The session admissions are mostly for: the latest session with an open offering, else the current one."""
    row = (db.query(AdmissionOffering.session_id).join(AcademicSession, AcademicSession.id == AdmissionOffering.session_id)
           .filter(AdmissionOffering.company_id == company_id, AdmissionOffering.active.is_(True))
           .order_by(AcademicSession.start_date.desc()).first())
    if row:
        return row[0]
    cur = academics.current_session(db, company_id)
    return cur.id if cur else None


# ── Offerings (programs open for admission, seats) ───────────────────

def get_offering(db: Session, company_id: int, session_id: int, program_id: int) -> AdmissionOffering | None:
    return db.query(AdmissionOffering).filter(AdmissionOffering.company_id == company_id,
                                              AdmissionOffering.session_id == session_id,
                                              AdmissionOffering.program_id == program_id).first()


def seats_taken(db: Session, company_id: int, session_id: int, program_id: int, exclude_id: int | None = None) -> int:
    q = db.query(func.count(Application.id)).filter(
        Application.company_id == company_id, Application.session_id == session_id,
        Application.program_id == program_id, Application.archived_at.is_(None),
        or_(Application.status.in_(SEAT_HOLDING), Application.offer_state == "pending"))
    if exclude_id:
        q = q.filter(Application.id != exclude_id)
    return q.scalar() or 0


def offering_counts(db: Session, company_id: int, session_id: int | None = None) -> dict[tuple[int, int], dict]:
    q = db.query(Application.session_id, Application.program_id, Application.status, Application.offer_state,
                 func.count(Application.id)).filter(Application.company_id == company_id,
                                                    Application.archived_at.is_(None))
    if session_id:
        q = q.filter(Application.session_id == session_id)
    out: dict[tuple[int, int], dict] = {}
    for s_id, p_id, status, offer_state, n in q.group_by(Application.session_id, Application.program_id,
                                                         Application.status, Application.offer_state).all():
        c = out.setdefault((s_id, p_id), {"applied": 0, "offered": 0, "accepted": 0, "enrolled": 0, "pending_offers": 0,
                                          "in_progress": 0})
        if status != "draft":
            c["applied"] += n
        if status in ("offered", "accepted", "enrolled"):
            c[status] += n
        if offer_state == "pending" and status not in SEAT_HOLDING:
            c["pending_offers"] += n
        if status in IN_PROGRESS:
            c["in_progress"] += n
    return out


def offering_out(o: AdmissionOffering, counts: dict | None = None) -> dict:
    c = counts or {"applied": 0, "offered": 0, "accepted": 0, "enrolled": 0, "pending_offers": 0, "in_progress": 0}
    taken = c["offered"] + c["accepted"] + c["enrolled"] + c["pending_offers"]
    return {
        "id": o.id, "uuid": str(o.uuid), "session_id": o.session_id, "session_name": o.session.name if o.session else None,
        "program_id": o.program_id, "program_name": o.program.name if o.program else None,
        "seats": o.seats, "application_fee": _money(o.application_fee), "fee_required": o.fee_required,
        "open_online": o.open_online, "opens_on": o.opens_on, "closes_on": o.closes_on,
        "test_required": o.test_required, "interview_required": o.interview_required,
        "min_age_years": o.min_age_years, "max_age_years": o.max_age_years, "age_as_on": o.age_as_on,
        "notes": o.notes, "active": o.active,
        **c, "taken": taken, "available": max(0, (o.seats or 0) - taken),
    }


def list_offerings(db: Session, company_id: int, session_id: int | None = None, active_only: bool = False) -> list[dict]:
    q = (db.query(AdmissionOffering).options(joinedload(AdmissionOffering.program), joinedload(AdmissionOffering.session))
         .filter(AdmissionOffering.company_id == company_id))
    if session_id:
        q = q.filter(AdmissionOffering.session_id == session_id)
    if active_only:
        q = q.filter(AdmissionOffering.active.is_(True))
    rows = q.all()
    rows.sort(key=lambda o: (-(o.session.start_date.toordinal() if o.session else 0), o.program.sort_order if o.program else 0,
                             o.program.name if o.program else ""))
    counts = offering_counts(db, company_id, session_id)
    return [offering_out(o, counts.get((o.session_id, o.program_id))) for o in rows]


def save_offering(db: Session, company_id: int, data: dict, row: AdmissionOffering | None = None) -> AdmissionOffering:
    session_id = data.get("session_id", row.session_id if row else None)
    program_id = data.get("program_id", row.program_id if row else None)
    if not session_id or not program_id:
        raise BadRequest("Pick the session and the program.")
    _check_session_program(db, company_id, session_id, program_id)
    other = get_offering(db, company_id, session_id, program_id)
    if other is not None and (row is None or other.id != row.id):
        raise HTTPException(status_code=409, detail="This program is already open for that session.")
    if data.get("seats") is not None and data["seats"] < 0:
        raise BadRequest("Seats cannot be negative.")
    if data.get("opens_on") and data.get("closes_on") and data["closes_on"] < data["opens_on"]:
        raise BadRequest("The closing date is before the opening date.")
    if row is None:
        row = AdmissionOffering(company_id=company_id)
        db.add(row)
    for k, v in data.items():
        setattr(row, k, v)
    db.flush()
    return row


def offering_open_now(o: AdmissionOffering, today: date) -> bool:
    return bool(o.active and o.open_online and (not o.opens_on or o.opens_on <= today)
                and (not o.closes_on or o.closes_on >= today))


def seats_summary(db: Session, company_id: int, session_id: int, program_id: int) -> dict:
    o = get_offering(db, company_id, session_id, program_id)
    taken = seats_taken(db, company_id, session_id, program_id)
    seats = o.seats if o else 0
    return {"seats": seats, "taken": taken, "available": max(0, seats - taken), "has_offering": o is not None}


# ── Enquiries ────────────────────────────────────────────────────────

def enquiry_out(e: Enquiry, full: bool = False) -> dict:
    out = {
        "id": e.id, "uuid": str(e.uuid), "number": e.number, "child_name": e.child_name,
        "child_first_name": e.child_first_name, "child_last_name": e.child_last_name, "child_dob": e.child_dob,
        "child_gender": e.child_gender, "parent_name": e.parent_name, "parent_relation": e.parent_relation,
        "phone": e.phone, "email": e.email, "stage": e.stage, "stage_label": STAGE_LABELS.get(e.stage, e.stage),
        "source_id": e.source_id, "source_label": e.source.label if e.source else None,
        "source_detail": e.source_detail, "session_id": e.session_id,
        "session_name": e.session.name if e.session else None, "program_id": e.program_id,
        "program_name": e.program.name if e.program else None, "next_follow_up": e.next_follow_up,
        "visit_at": e.visit_at, "counsellor_id": e.counsellor_id, "counsellor_name": _emp_name(e.counsellor),
        "lost_reason": e.lost_reason, "application_id": e.application_id, "created_at": e.created_at,
        "stage_changed_at": e.stage_changed_at, "archived": e.archived_at is not None,
    }
    if full:
        out.update({"current_institution": e.current_institution, "city": e.city, "address": e.address,
                    "notes": e.notes})
    return out


def enquiry_query(db: Session, company_id: int, *, q: str | None = None, stage: str | None = None,
                  source_id: int | None = None, program_id: int | None = None, session_id: int | None = None,
                  counsellor_id: int | None = None, follow_up: str | None = None, today: date | None = None,
                  archived: bool = False):
    query = (db.query(Enquiry).options(joinedload(Enquiry.program), joinedload(Enquiry.session),
                                       joinedload(Enquiry.counsellor), joinedload(Enquiry.source))
             .filter(Enquiry.company_id == company_id))
    query = query.filter(Enquiry.archived_at.isnot(None) if archived else Enquiry.archived_at.is_(None))
    if stage:
        stages = [s for s in stage.split(",") if s in STAGES]
        if stages:
            query = query.filter(Enquiry.stage.in_(stages))
    if source_id:
        query = query.filter(Enquiry.source_id == source_id)
    if program_id:
        query = query.filter(Enquiry.program_id == program_id)
    if session_id:
        query = query.filter(Enquiry.session_id == session_id)
    if counsellor_id:
        query = query.filter(Enquiry.counsellor_id == counsellor_id)
    if follow_up and today:
        query = query.filter(Enquiry.stage.in_(OPEN_STAGES))
        if follow_up == "overdue":
            query = query.filter(Enquiry.next_follow_up < today)
        elif follow_up == "today":
            query = query.filter(Enquiry.next_follow_up == today)
        elif follow_up == "due":
            query = query.filter(Enquiry.next_follow_up <= today)
        elif follow_up == "none":
            query = query.filter(Enquiry.next_follow_up.is_(None))
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Enquiry.child_first_name.ilike(like), Enquiry.child_last_name.ilike(like),
                                 Enquiry.parent_name.ilike(like), Enquiry.phone.ilike(like), Enquiry.email.ilike(like),
                                 Enquiry.number.ilike(like),
                                 func.concat(Enquiry.child_first_name, " ", Enquiry.child_last_name).ilike(like)))
    return query


ENQUIRY_FIELDS = ("source_id", "source_detail", "session_id", "program_id", "child_first_name", "child_last_name",
                  "child_dob", "child_gender", "current_institution", "parent_name", "parent_relation", "phone",
                  "email", "city", "address", "next_follow_up", "counsellor_id", "notes")


def create_enquiry(db: Session, company_id: int, data: dict, *, actor_name: str | None = None) -> Enquiry:
    data = {k: v for k, v in data.items() if k in ENQUIRY_FIELDS}
    if not (data.get("phone") or data.get("email")):
        raise BadRequest("Add a phone number or an email so the family can be contacted.")
    _check_session_program(db, company_id, data.get("session_id"), data.get("program_id"))
    _check_employee(db, company_id, data.get("counsellor_id"))
    _check_source(db, company_id, data.get("source_id"))
    if not data.get("session_id"):
        data["session_id"] = default_session_id(db, company_id)
    e = Enquiry(company_id=company_id, stage="new", **data)
    e.number = _number(db, company_id, "enquiry", Enquiry, Enquiry.number, "ENQ")
    db.add(e)
    db.flush()
    log(db, company_id, enquiry=e, kind="system", body="Enquiry added", to_value="new", actor_name=actor_name)
    if e.counsellor_id:
        _notify_counsellor(db, company_id, e)
    return e


def update_enquiry(db: Session, company_id: int, e: Enquiry, data: dict) -> Enquiry:
    data = {k: v for k, v in data.items() if k in ENQUIRY_FIELDS}
    _check_session_program(db, company_id, data.get("session_id"), data.get("program_id"))
    _check_source(db, company_id, data.get("source_id"))
    if "counsellor_id" in data and data["counsellor_id"] != e.counsellor_id:
        assign(db, company_id, e, data.pop("counsellor_id"))
    if "next_follow_up" in data and data["next_follow_up"] != e.next_follow_up:
        log(db, company_id, enquiry=e, kind="follow_up",
            body=f"Next follow-up {data['next_follow_up'].isoformat() if data['next_follow_up'] else 'cleared'}")
    for k, v in data.items():
        setattr(e, k, v)
    if not (e.phone or e.email):
        raise BadRequest("Keep a phone number or an email so the family can be contacted.")
    db.flush()
    return e


def assign(db: Session, company_id: int, e: Enquiry, counsellor_id: int | None) -> Enquiry:
    emp = _check_employee(db, company_id, counsellor_id)
    before = _emp_name(e.counsellor)
    e.counsellor_id = counsellor_id
    db.flush()
    db.refresh(e)
    log(db, company_id, enquiry=e, kind="assign", body=f"Assigned to {_emp_name(emp)}" if emp else "Unassigned",
        from_value=(before or "")[:30] or None, to_value=(_emp_name(emp) or "")[:30] or None)
    if emp:
        _notify_counsellor(db, company_id, e)
    return e


def _notify_counsellor(db: Session, company_id: int, e: Enquiry) -> None:
    if not e.counsellor_id:
        return
    try:
        from modules.notifications.service import notify
        emp_id, _ = _actor()
        notify(db, company_id, [e.counsellor_id], kind="admissions", title=f"Admission enquiry: {e.child_name}",
               body=f"{e.parent_name or 'A family'} enquired{' about ' + e.program.name if e.program else ''}. "
                    f"{e.phone or e.email or ''}".strip(),
               link=f"/dashboard/admissions/enquiries?view={e.uuid}", skip={emp_id} if emp_id else None)
    except Exception:  # notifications never block the enquiry
        pass


def check_stage_move(e: Enquiry, to: str, *, lost_reason: str | None = None, visit_at: datetime | None = None,
                     system: bool = False) -> None:
    """The pipeline rules (see the module docstring). Raises BadRequest with a plain reason."""
    if to not in STAGES:
        raise BadRequest("Unknown stage.")
    if to == e.stage:
        return
    if e.stage == "converted":
        raise BadRequest("This enquiry has been converted into an admission and cannot move.")
    if to == "converted" and not system:
        raise BadRequest("An enquiry is converted automatically when the applicant is enrolled.")
    if e.stage == "lost" and to not in ("new", "contacted"):
        raise BadRequest("Reopen a lost enquiry as New or Contacted first.")
    if to == "lost" and not (lost_reason or "").strip():
        raise BadRequest("Say why the enquiry was lost.")
    if to == "visit_scheduled" and not (visit_at or e.visit_at):
        raise BadRequest("Pick the date and time of the visit.")
    if to == "applied" and not system and not e.application_id:
        raise BadRequest("Start an application from this enquiry to move it to Applied.")


def change_stage(db: Session, company_id: int, e: Enquiry, to: str, *, note: str | None = None,
                 lost_reason: str | None = None, visit_at: datetime | None = None,
                 next_follow_up: date | None = None, system: bool = False) -> Enquiry:
    check_stage_move(e, to, lost_reason=lost_reason, visit_at=visit_at, system=system)
    before = e.stage
    if visit_at:
        e.visit_at = visit_at
    if next_follow_up is not None:
        e.next_follow_up = next_follow_up
    if to == before:
        if note:
            log(db, company_id, enquiry=e, kind="note", body=note)
        db.flush()
        return e
    e.stage = to
    e.stage_changed_at = _now()
    if to == "lost":
        e.lost_reason = lost_reason.strip()[:300]
        e.next_follow_up = None
    elif before == "lost":
        e.lost_reason = None
    if to in ("converted",):
        e.next_follow_up = None
    body = note or (f"Lost: {e.lost_reason}" if to == "lost" else None)
    if to == "visit_scheduled" and e.visit_at:
        body = (body + " · " if body else "") + f"Visit on {e.visit_at.isoformat()}"
    log(db, company_id, enquiry=e, kind="stage", body=body, from_value=before, to_value=to)
    db.flush()
    return e


def add_activity(db: Session, company_id: int, e: Enquiry, kind: str, body: str,
                 next_follow_up: date | None = None) -> AdmissionActivity:
    if kind not in ("note", "call", "email", "visit", "sms", "whatsapp", "meeting"):
        raise BadRequest("Unknown kind of activity.")
    if not (body or "").strip():
        raise BadRequest("Write what happened.")
    row = log(db, company_id, enquiry=e, kind=kind, body=body.strip())
    if next_follow_up is not None:
        e.next_follow_up = next_follow_up
    # The first call or meeting moves a new enquiry to Contacted.
    if e.stage == "new" and kind in ("call", "email", "visit", "sms", "whatsapp", "meeting"):
        change_stage(db, company_id, e, "contacted")
    db.flush()
    return row


def board(db: Session, company_id: int, per_stage: int = 60, **filters) -> list[dict]:
    out = []
    for st in STAGES:
        q = enquiry_query(db, company_id, **{**filters, "stage": st})
        total = q.order_by(None).count()
        rows = q.order_by(Enquiry.next_follow_up.asc().nullslast(), Enquiry.created_at.desc()).limit(per_stage).all()
        out.append({"key": st, "label": STAGE_LABELS[st], "total": total, "items": [enquiry_out(e) for e in rows]})
    return out


# ── Applications ─────────────────────────────────────────────────────

APPLICATION_FIELDS = ("session_id", "program_id", "first_name", "middle_name", "last_name", "gender", "date_of_birth",
                      "blood_group", "nationality", "religion", "category", "mother_tongue", "national_id",
                      "previous_institution", "previous_program", "previous_marks", "address", "city", "state",
                      "pincode", "contact_phone", "contact_email", "remarks")


def _clean_guardians(items: list[dict] | None) -> list[dict]:
    out = []
    for g in (items or [])[:4]:
        first = str(g.get("first_name") or "").strip()
        if not first:
            continue
        out.append({
            "relation": str(g.get("relation") or "guardian")[:30],
            "first_name": first[:100], "last_name": (str(g.get("last_name") or "").strip()[:100] or None),
            "phone": (str(g.get("phone") or "").strip()[:20] or None),
            "email": (str(g.get("email") or "").strip().lower()[:255] or None),
            "occupation": (str(g.get("occupation") or "").strip()[:120] or None),
            "is_primary": bool(g.get("is_primary")),
        })
    if out and not any(g["is_primary"] for g in out):
        out[0]["is_primary"] = True
    return out


def application_out(db: Session, a: Application, full: bool = False) -> dict:
    out = {
        "id": a.id, "uuid": str(a.uuid), "application_no": a.application_no, "name": a.full_name,
        "first_name": a.first_name, "last_name": a.last_name, "gender": a.gender, "date_of_birth": a.date_of_birth,
        "status": a.status, "status_label": STATUS_LABELS.get(a.status, a.status), "channel": a.channel,
        "session_id": a.session_id, "session_name": a.session.name if a.session else None,
        "program_id": a.program_id, "program_name": a.program.name if a.program else None,
        "contact_phone": a.contact_phone, "contact_email": a.contact_email,
        "fee_amount": _money(a.fee_amount), "fee_paid": a.fee_paid, "test_at": a.test_at, "interview_at": a.interview_at,
        "test_score": a.test_score, "test_max": a.test_max, "interview_score": a.interview_score,
        "interview_max": a.interview_max, "merit_score": a.merit_score, "merit_rank": a.merit_rank,
        "offer_state": a.offer_state, "offered_at": a.offered_at, "offer_expires_on": a.offer_expires_on,
        "submitted_at": a.submitted_at, "created_at": a.created_at, "student_id": a.student_id,
        "student_uuid": str(a.student.uuid) if a.student else None,
        "primary_guardian": next((f"{g['first_name']} {g.get('last_name') or ''}".strip()
                                  for g in (a.guardians or []) if g.get("is_primary")), None),
        "documents_pending": sum(1 for d in (a.documents or []) if not d.get("uuid")),
    }
    if full:
        from modules.documents import storage
        out.update({k: getattr(a, k) for k in (
            "middle_name", "blood_group", "nationality", "religion", "category", "mother_tongue",
            "previous_institution", "previous_program", "previous_marks", "address", "city", "state", "pincode",
            "fee_paid_on", "fee_reference", "test_venue", "interviewer_id", "interview_notes", "offer_note",
            "accepted_at", "decision_reason", "section_id", "enrolled_at", "remarks", "enquiry_id")})
        out["national_id"] = ("•" * max(0, len(a.national_id) - 4) + a.national_id[-4:]) if a.national_id else None
        out["interviewer_name"] = _emp_name(a.interviewer)
        out["section_label"] = academics.section_label(a.section) if a.section else None
        out["guardians"] = a.guardians or []
        out["custom"] = a.custom or {}
        links = {d["uuid"]: d for d in storage.with_links(db, a.company_id, [d for d in (a.documents or []) if d.get("uuid")])}
        out["documents"] = [{**d, "url": (links.get(d.get("uuid")) or {}).get("url"),
                             "download_url": (links.get(d.get("uuid")) or {}).get("download_url")}
                            for d in (a.documents or [])]
        out["enquiry_uuid"] = str(a.enquiry.uuid) if a.enquiry else None
        out["allowed"] = ALLOWED.get(a.status, [])
        out["can_offer"] = a.status in OFFERABLE and a.offer_state != "pending"
        out["seats"] = seats_summary(db, a.company_id, a.session_id, a.program_id)
    return out


def application_query(db: Session, company_id: int, *, q: str | None = None, status: str | None = None,
                      program_id: int | None = None, session_id: int | None = None, channel: str | None = None,
                      fee_paid: bool | None = None, archived: bool = False):
    query = (db.query(Application).options(joinedload(Application.program), joinedload(Application.session),
                                           joinedload(Application.student))
             .filter(Application.company_id == company_id))
    query = query.filter(Application.archived_at.isnot(None) if archived else Application.archived_at.is_(None))
    if status:
        sts = [s for s in status.split(",") if s in STATUSES]
        if sts:
            query = query.filter(Application.status.in_(sts))
    if program_id:
        query = query.filter(Application.program_id == program_id)
    if session_id:
        query = query.filter(Application.session_id == session_id)
    if channel:
        query = query.filter(Application.channel == channel)
    if fee_paid is not None:
        query = query.filter(Application.fee_paid.is_(fee_paid))
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Application.first_name.ilike(like), Application.last_name.ilike(like),
                                 Application.application_no.ilike(like), Application.contact_phone.ilike(like),
                                 Application.contact_email.ilike(like),
                                 func.concat(Application.first_name, " ", Application.last_name).ilike(like)))
    return query


def _documents_from_settings(conf: dict, existing: list[dict] | None = None) -> list[dict]:
    """The application's document list: every configured document (keeping uploads already made)."""
    have = {d.get("key"): d for d in (existing or [])}
    out = []
    for d in conf.get("required_documents") or []:
        cur = have.pop(d["key"], {})
        out.append({**cur, "key": d["key"], "label": d["label"], "required": bool(d.get("required"))})
    out.extend(v for v in have.values() if v.get("uuid"))  # uploads for documents no longer asked for
    return out


def _age_years(dob: date, on: date) -> Decimal:
    years = on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))
    last = date(dob.year + years, dob.month, min(dob.day, 28)) if dob.month == 2 and dob.day == 29 else dob.replace(year=dob.year + years)
    months = (on.year - last.year) * 12 + on.month - last.month - (1 if on.day < last.day else 0)
    return (Decimal(years) + Decimal(max(0, months)) / Decimal(12)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def check_age(o: AdmissionOffering | None, dob: date | None, sess: AcademicSession | None) -> None:
    if not o or not dob or (o.min_age_years is None and o.max_age_years is None):
        return
    on = o.age_as_on or (sess.start_date if sess else date.today())
    age = _age_years(dob, on)
    if o.min_age_years is not None and age < o.min_age_years:
        raise BadRequest(f"The minimum age for {o.program.name if o.program else 'this program'} is "
                         f"{o.min_age_years:g} years on {on.strftime('%d %b %Y')}.")
    if o.max_age_years is not None and age > o.max_age_years:
        raise BadRequest(f"The maximum age for {o.program.name if o.program else 'this program'} is "
                         f"{o.max_age_years:g} years on {on.strftime('%d %b %Y')}.")


def create_application(db: Session, company_id: int, data: dict, *, guardians: list[dict] | None = None,
                       custom: dict | None = None, channel: str = "office", status: str = "submitted",
                       enquiry: Enquiry | None = None, actor_name: str | None = None) -> Application:
    data = {k: v for k, v in data.items() if k in APPLICATION_FIELDS}
    if not data.get("first_name"):
        raise BadRequest("Enter the applicant's first name.")
    if not data.get("session_id") or not data.get("program_id"):
        raise BadRequest("Pick the session and the program applied for.")
    sess, prog = _check_session_program(db, company_id, data["session_id"], data["program_id"])
    o = get_offering(db, company_id, data["session_id"], data["program_id"])
    check_age(o, data.get("date_of_birth"), sess)
    guardians = _clean_guardians(guardians)
    if not data.get("contact_phone") and guardians:
        data["contact_phone"] = next((g["phone"] for g in guardians if g.get("phone")), None)
    if not data.get("contact_email") and guardians:
        data["contact_email"] = next((g["email"] for g in guardians if g.get("email")), None)
    if not (data.get("contact_phone") or data.get("contact_email")):
        raise BadRequest("Add a phone number or an email for the family.")
    if data.get("contact_email"):
        data["contact_email"] = data["contact_email"].strip().lower()
    from core import custom_fields
    cust = custom_fields.merge_values(db, company_id, "admission", custom or {}, partial=channel == "office") if (
        custom or channel != "office") else {}
    if status not in ("draft", "submitted"):
        status = "submitted"
    a = Application(company_id=company_id, status=status, channel=channel, guardians=guardians, custom=cust,
                    enquiry_id=enquiry.id if enquiry else None,
                    fee_amount=o.application_fee if o else Decimal("0"), **data)
    a.documents = _documents_from_settings(settings(db, company_id))
    a.application_no = _number(db, company_id, "admission_application", Application, Application.application_no, "APP")
    if status == "submitted":
        a.submitted_at = _now()
    db.add(a)
    db.flush()
    log(db, company_id, application=a, kind="status", to_value=status,
        body="Application received online" if channel == "online" else "Application added", actor_name=actor_name)
    if enquiry is not None:
        enquiry.application_id = a.id
        if enquiry.stage not in ("converted",):
            if enquiry.stage == "lost":
                change_stage(db, company_id, enquiry, "contacted", note="Reopened: application received", system=True)
            change_stage(db, company_id, enquiry, "applied", note=f"Application {a.application_no}", system=True)
    return a


def update_application(db: Session, company_id: int, a: Application, data: dict, *, guardians: list[dict] | None = None,
                       custom: dict | None = None) -> Application:
    if a.status == "enrolled":
        raise BadRequest("This applicant is already enrolled; edit the student record instead.")
    data = {k: v for k, v in data.items() if k in APPLICATION_FIELDS}
    if "session_id" in data or "program_id" in data:
        new_s, new_p = data.get("session_id", a.session_id), data.get("program_id", a.program_id)
        _check_session_program(db, company_id, new_s, new_p)
        if (new_s, new_p) != (a.session_id, a.program_id) and (a.status in SEAT_HOLDING or a.offer_state == "pending"):
            raise BadRequest("Withdraw the offer before moving the application to another program.")
        if (new_s, new_p) != (a.session_id, a.program_id):
            a.merit_rank = None
    for k, v in data.items():
        setattr(a, k, v)
    if guardians is not None:
        a.guardians = _clean_guardians(guardians)
    if custom is not None:
        from core import custom_fields
        a.custom = custom_fields.merge_values(db, company_id, "admission", custom, a.custom, partial=True)
    if not (a.contact_phone or a.contact_email):
        raise BadRequest("Keep a phone number or an email for the family.")
    db.flush()
    return a


def change_status(db: Session, company_id: int, a: Application, to: str, *, note: str | None = None,
                  reason: str | None = None, system: bool = False) -> Application:
    if to not in STATUSES:
        raise BadRequest("Unknown status.")
    if to == a.status:
        return a
    if not system:
        if to == "offered":
            raise BadRequest("Use Make offer to offer a seat.")
        if to == "enrolled":
            raise BadRequest("Use Enrol to turn the applicant into a student.")
        if to not in ALLOWED.get(a.status, []):
            raise BadRequest(f"An application that is {STATUS_LABELS[a.status].lower()} cannot be moved to "
                             f"{STATUS_LABELS[to].lower()}.")
        if to in ("rejected", "withdrawn") and not (reason or "").strip():
            raise BadRequest("Give a reason.")
    before = a.status
    a.status = to
    if to == "submitted" and not a.submitted_at:
        a.submitted_at = _now()
    if to in ("rejected", "withdrawn"):
        a.decision_reason = (reason or "").strip()[:300] or None
        if a.offer_state == "pending" and a.offer_workflow_id:
            _cancel_offer_request(db, company_id, a)
    if to == "accepted":
        a.accepted_at = _now()
    if before == "offered" and to in ("withdrawn", "rejected", "waitlisted"):
        a.offer_state = "none"
    log(db, company_id, application=a, kind="status", from_value=before, to_value=to, body=note or reason)
    db.flush()
    return a


def schedule(db: Session, company_id: int, a: Application, data: dict) -> Application:
    if a.status in ("enrolled", "rejected", "withdrawn"):
        raise BadRequest("This application is closed.")
    _check_employee(db, company_id, data.get("interviewer_id"))
    parts = []
    if "test_at" in data:
        a.test_at = data["test_at"]
        a.test_venue = data.get("test_venue", a.test_venue)
        if a.test_at:
            parts.append(f"Entrance test on {a.test_at.isoformat()}" + (f" at {a.test_venue}" if a.test_venue else ""))
    if "interview_at" in data:
        a.interview_at = data["interview_at"]
        if "interviewer_id" in data:
            a.interviewer_id = data["interviewer_id"]
        if a.interview_at:
            parts.append(f"Interview on {a.interview_at.isoformat()}")
    target = None
    if data.get("interview_at") and a.status in ("submitted", "under_review", "test_scheduled"):
        target = "interview"
    elif data.get("test_at") and a.status in ("submitted", "under_review"):
        target = "test_scheduled"
    if target:
        change_status(db, company_id, a, target, note="; ".join(parts) or None, system=True)
    elif parts:
        log(db, company_id, application=a, kind="system", body="; ".join(parts))
    db.flush()
    return a


def compute_merit(a: Application, conf: dict) -> Decimal | None:
    tw, iw = Decimal(str(conf.get("test_weight", 70))), Decimal(str(conf.get("interview_weight", 30)))
    parts = []
    if a.test_score is not None and a.test_max:
        parts.append((Decimal(a.test_score) / Decimal(a.test_max) * 100, tw))
    if a.interview_score is not None and a.interview_max:
        parts.append((Decimal(a.interview_score) / Decimal(a.interview_max) * 100, iw))
    weight = sum(w for _, w in parts)
    if not parts or weight == 0:
        return None
    return (sum(p * w for p, w in parts) / weight).quantize(D2, rounding=ROUND_HALF_UP)


def save_scores(db: Session, company_id: int, a: Application, data: dict) -> Application:
    for key in ("test_score", "test_max", "interview_score", "interview_max"):
        if key in data and data[key] is not None and data[key] < 0:
            raise BadRequest("Scores cannot be negative.")
    for k in ("test_score", "test_max", "interview_score", "interview_max", "interview_notes"):
        if k in data:
            setattr(a, k, data[k])
    if a.test_score is not None and a.test_max and a.test_score > a.test_max:
        raise BadRequest("The test score is more than the maximum.")
    if a.interview_score is not None and a.interview_max and a.interview_score > a.interview_max:
        raise BadRequest("The interview score is more than the maximum.")
    a.merit_score = compute_merit(a, settings(db, company_id))
    log(db, company_id, application=a, kind="system",
        body=f"Scores saved" + (f": merit score {a.merit_score}" if a.merit_score is not None else ""))
    db.flush()
    return a


def set_fee(db: Session, company_id: int, a: Application, paid: bool, paid_on: date | None, reference: str | None,
            amount: Decimal | None = None) -> Application:
    if amount is not None:
        if amount < 0:
            raise BadRequest("The fee cannot be negative.")
        a.fee_amount = amount.quantize(D2)
    a.fee_paid = paid
    a.fee_paid_on = (paid_on or date.today()) if paid else None
    a.fee_reference = (reference or None) if paid else None
    log(db, company_id, application=a, kind="system",
        body=f"Application fee {'received' if paid else 'marked unpaid'}" + (f" ({reference})" if paid and reference else ""))
    db.flush()
    return a


def attach_document(db: Session, company_id: int, a: Application, key: str, blob) -> Application:
    docs = _documents_from_settings(settings(db, company_id), a.documents)
    for d in docs:
        if d["key"] == key:
            d.update({"uuid": str(blob.uuid), "name": blob.filename, "size": blob.size, "content_type": blob.content_type,
                      "verified": False})
            break
    else:
        raise BadRequest("That document is not on the list.")
    a.documents = docs
    flag_modified(a, "documents")
    db.flush()
    return a


def verify_document(db: Session, company_id: int, a: Application, key: str, verified: bool) -> Application:
    docs = [dict(d) for d in (a.documents or [])]
    for d in docs:
        if d.get("key") == key:
            if not d.get("uuid"):
                raise BadRequest("Nothing has been uploaded for this document yet.")
            d["verified"] = verified
            break
    else:
        raise NotFound("Document")
    a.documents = docs
    flag_modified(a, "documents")
    log(db, company_id, application=a, kind="system",
        body=f"{next(d['label'] for d in docs if d.get('key') == key)} {'verified' if verified else 'marked not verified'}")
    db.flush()
    return a


# ── Merit list and seats ─────────────────────────────────────────────

def _pool(db: Session, company_id: int, session_id: int, program_id: int) -> list[Application]:
    rows = db.query(Application).filter(Application.company_id == company_id, Application.session_id == session_id,
                                        Application.program_id == program_id, Application.archived_at.is_(None),
                                        Application.status.in_(RANKABLE)).all()
    rows = [r for r in rows if r.offer_state != "pending"]
    far = datetime.max.replace(tzinfo=timezone.utc)
    rows.sort(key=lambda a: (a.merit_score is None, -(a.merit_score or 0),
                             -(Decimal(a.test_score) / Decimal(a.test_max) if a.test_score is not None and a.test_max else 0),
                             a.submitted_at or far, a.id))
    return rows


def rank(db: Session, company_id: int, session_id: int, program_id: int) -> list[Application]:
    """Give every application still in the running a merit rank (1 = best) for this program."""
    _check_session_program(db, company_id, session_id, program_id)
    pool = _pool(db, company_id, session_id, program_id)
    conf = settings(db, company_id)
    for a in pool:
        a.merit_score = compute_merit(a, conf)
    pool = _pool(db, company_id, session_id, program_id)
    for i, a in enumerate(pool, 1):
        a.merit_rank = i
    audit.event(db, "admissions.merit_ranked", "admission_applications", None,
                {"session_id": session_id, "program_id": program_id, "ranked": len(pool)})
    db.flush()
    return pool


def merit_list(db: Session, company_id: int, session_id: int, program_id: int) -> dict:
    _check_session_program(db, company_id, session_id, program_id)
    rows = (application_query(db, company_id, session_id=session_id, program_id=program_id)
            .filter(Application.status.notin_(("draft",))).all())
    rows.sort(key=lambda a: (a.merit_rank is None, a.merit_rank or 0, -(a.merit_score or 0), a.id))
    return {"seats": seats_summary(db, company_id, session_id, program_id),
            "items": [application_out(db, a) for a in rows]}


def offer_top(db: Session, company_id: int, session_id: int, program_id: int, count: int | None = None,
              waitlist_rest: bool = False, expires_on: date | None = None) -> dict:
    pool = rank(db, company_id, session_id, program_id)
    seats = seats_summary(db, company_id, session_id, program_id)
    if not seats["has_offering"]:
        raise BadRequest("Open this program for admission (with seats) in Admission settings first.")
    room = seats["available"] if count is None else min(count, seats["available"])
    offered, skipped = [], []
    for a in pool:
        if len(offered) >= room:
            break
        try:
            offer(db, company_id, a, expires_on=expires_on)
            offered.append(a)
        except HTTPException as exc:
            skipped.append({"application_no": a.application_no, "reason": exc.detail})
    waitlisted = 0
    if waitlist_rest:
        for a in pool:
            if a in offered or a.status in ("waitlisted",) or a.offer_state == "pending":
                continue
            change_status(db, company_id, a, "waitlisted", note="Waitlisted from the merit list", system=True)
            waitlisted += 1
    db.flush()
    return {"offered": len(offered), "waitlisted": waitlisted, "skipped": skipped,
            "seats": seats_summary(db, company_id, session_id, program_id)}


# ── Offers (approval through the workflow engine) ────────────────────

def offer(db: Session, company_id: int, a: Application, *, expires_on: date | None = None,
          note: str | None = None) -> Application:
    if a.status not in OFFERABLE:
        raise BadRequest(f"An application that is {STATUS_LABELS.get(a.status, a.status).lower()} cannot get an offer.")
    if a.offer_state == "pending":
        raise BadRequest("An offer for this application is already waiting for approval.")
    o = get_offering(db, company_id, a.session_id, a.program_id)
    if o is None:
        raise BadRequest("Open this program for admission (with seats) in Admission settings first.")
    if o.fee_required and not a.fee_paid and a.fee_amount and a.fee_amount > 0:
        raise BadRequest("Mark the application fee as paid first.")
    taken = seats_taken(db, company_id, a.session_id, a.program_id, exclude_id=a.id)
    if taken >= (o.seats or 0):
        raise BadRequest(f"No seats left in {o.program.name if o.program else 'this program'} "
                         f"({taken} of {o.seats} taken).")
    conf = settings(db, company_id)
    a.offer_expires_on = expires_on or (date.today() + timedelta(days=int(conf.get("offer_valid_days") or 14)))
    a.offer_note = note
    a.offer_state = "pending"
    db.flush()
    from core.workflow import service as workflow
    payload = {"application_id": a.id, "application_no": a.application_no, "applicant": a.full_name,
               "program": a.program.name if a.program else None, "program_id": a.program_id,
               "session": a.session.name if a.session else None, "merit_rank": a.merit_rank,
               "merit_score": float(a.merit_score) if a.merit_score is not None else None,
               "seats": o.seats, "seats_taken": taken, "fee": float(a.fee_amount or 0), "note": note}
    inst = workflow.submit(db, company_id, "admissions.offer", payload,
                           f"Admission offer: {a.full_name} ({a.application_no})",
                           subject_type="admission_application", subject_id=a.id)
    a.offer_workflow_id = inst.id
    if a.offer_state == "pending":
        log(db, company_id, application=a, kind="system", body="Offer sent for approval")
    db.flush()
    return a


def _app_for_instance(db: Session, instance) -> Application | None:
    if instance.subject_type != "admission_application" or not instance.subject_id:
        return None
    return db.query(Application).filter(Application.company_id == instance.company_id,
                                        Application.id == instance.subject_id).first()


def on_offer_approved(db: Session, instance) -> None:
    a = _app_for_instance(db, instance)
    if a is None or a.status not in OFFERABLE:
        return
    o = get_offering(db, a.company_id, a.session_id, a.program_id)
    taken = seats_taken(db, a.company_id, a.session_id, a.program_id, exclude_id=a.id)
    if o is not None and taken >= (o.seats or 0):
        raise BadRequest(f"No seats left in {o.program.name if o.program else 'this program'} to approve this offer.")
    a.offer_state = "approved"
    change_status(db, a.company_id, a, "offered", note=a.offer_note or "Offer made", system=True)
    a.offered_at = _now()
    _send_offer_email(db, a)
    db.flush()


def on_offer_closed(db: Session, instance, outcome: str) -> None:
    a = _app_for_instance(db, instance)
    if a is None or a.offer_state != "pending":
        return
    a.offer_state = "rejected" if outcome == "rejected" else "none"
    log(db, a.company_id, application=a, kind="system",
        body="Offer not approved" if outcome == "rejected" else "Offer request withdrawn")
    db.flush()


def _cancel_offer_request(db: Session, company_id: int, a: Application) -> None:
    from core.workflow import service as workflow
    from core.workflow.models import WorkflowInstance
    inst = db.query(WorkflowInstance).filter(WorkflowInstance.company_id == company_id,
                                             WorkflowInstance.id == a.offer_workflow_id).first()
    a.offer_state = "none"
    if inst is not None and inst.status == "pending":
        try:
            workflow.cancel(db, company_id, inst, "Application closed")
        except HTTPException:
            pass


def accept(db: Session, company_id: int, a: Application, note: str | None = None) -> Application:
    if a.status != "offered":
        raise BadRequest("Only an offered application can be accepted.")
    if a.offer_expires_on and a.offer_expires_on < date.today():
        raise BadRequest("The offer has lapsed. Make a new offer first.")
    return change_status(db, company_id, a, "accepted", note=note or "Offer accepted")


def _merge_values_for_application(db: Session, company_id: int, a: Application) -> dict:
    from modules.company.models import Company
    from modules.documents.merge import format_date
    comp = db.query(Company).filter(Company.id == company_id).first()
    g = next((x for x in (a.guardians or []) if x.get("is_primary")), (a.guardians or [None])[0] if a.guardians else None)
    gname = f"{g['first_name']} {g.get('last_name') or ''}".strip() if g else "Parent"
    return {
        "institution.name": comp.company_name if comp else "", "institution.city": comp.city if comp else "",
        "applicant.name": a.full_name, "applicant.first_name": a.first_name,
        "application.number": a.application_no, "application.program": a.program.name if a.program else "",
        "application.session": a.session.name if a.session else "",
        "guardian.name": gname, "offer.expires_on": format_date(a.offer_expires_on) if a.offer_expires_on else "",
        "offer.date": format_date(a.offered_at.date() if a.offered_at else date.today()),
        "offer.note": a.offer_note or "", "application.merit_rank": str(a.merit_rank or ""),
    }


def _send_text(to: str | None, subject: str, body: str) -> None:
    if not to:
        return
    try:
        from core.email import send_email
        send_email(to, subject, body)
    except Exception:
        pass


def _send_offer_email(db: Session, a: Application) -> None:
    from modules.documents.merge import render
    conf = settings(db, a.company_id)
    values = _merge_values_for_application(db, a.company_id, a)
    body, _ = render(conf["offer_email_body"], values, escape=False)
    _send_text(a.contact_email, f"Offer of admission: {a.full_name}", body)


def send_acknowledgement(db: Session, a: Application) -> None:
    from modules.documents.merge import render
    conf = settings(db, a.company_id)
    values = _merge_values_for_application(db, a.company_id, a)
    subject, _ = render(conf["ack_email_subject"], values, escape=False)
    body, _ = render(conf["ack_email_body"], values, escape=False)
    _send_text(a.contact_email, subject, body)


def offer_letter_pdf(db: Session, a: Application) -> bytes:
    from modules.documents import pdf
    from modules.documents.merge import render
    if a.status not in ("offered", "accepted", "enrolled"):
        raise BadRequest("There is no offer on this application yet.")
    conf = settings(db, a.company_id)
    body, _ = render(conf["offer_letter_html"], _merge_values_for_application(db, a.company_id, a))
    header = letterhead_html(db, a.company_id)
    return pdf.render_pdf(pdf.pdf_html(body, header=header, footer=None, header_mm=28, footer_mm=0,
                                       font_family="Georgia", title=f"Offer {a.application_no}"))


# ── Convert to a student ─────────────────────────────────────────────

def enrol(db: Session, company_id: int, a: Application, *, section_id: int, roll_no: str | None = None,
          admission_no: str | None = None, create_portal_accounts: bool = False,
          admission_date: date | None = None) -> tuple[object, list[dict]]:
    """Create the student with guardians from the application, enrol them in the section and
    (optionally) create parent and student sign-ins."""
    if a.status not in ("offered", "accepted"):
        raise BadRequest("Only an applicant who was offered a seat (or accepted it) can be enrolled.")
    if a.student_id:
        raise BadRequest("This applicant is already a student.")
    section = (db.query(Section).options(joinedload(Section.program))
               .filter(Section.company_id == company_id, Section.id == section_id).first())
    if section is None:
        raise BadRequest("The section was not found.")
    if section.program_id != a.program_id or section.session_id != a.session_id:
        raise BadRequest("Pick a section of the program and session applied for.")
    from modules.students.router import GuardianIn, StudentCreate, create_student
    guardians = []
    for g in a.guardians or []:
        guardians.append(GuardianIn(first_name=g["first_name"], last_name=g.get("last_name"), email=g.get("email") or None,
                                    phone=g.get("phone"), occupation=g.get("occupation"),
                                    relation=g.get("relation") or "guardian", is_primary=bool(g.get("is_primary"))))
    payload = StudentCreate(
        first_name=a.first_name, middle_name=a.middle_name, last_name=a.last_name, gender=a.gender,
        date_of_birth=a.date_of_birth, blood_group=a.blood_group, nationality=a.nationality, religion=a.religion,
        category=a.category, mother_tongue=a.mother_tongue, national_id=a.national_id,
        previous_institution=a.previous_institution, address=a.address, city=a.city, state=a.state,
        pincode=a.pincode, admission_date=admission_date or date.today(), admission_no=admission_no or None,
        section_id=section.id, roll_no=roll_no or None, guardians=guardians,
        create_portal_accounts=create_portal_accounts,
    )
    student, logins = create_student(db, company_id, payload)
    _copy_documents(db, company_id, a, student)
    before = a.status
    a.status = "enrolled"
    a.student_id = student.id
    a.section_id = section.id
    a.enrolled_at = _now()
    if not a.accepted_at:
        a.accepted_at = _now()
    log(db, company_id, application=a, kind="status", from_value=before, to_value="enrolled",
        body=f"Enrolled as {student.admission_no} in {academics.section_label(section)}")
    if a.enquiry_id:
        e = db.query(Enquiry).filter(Enquiry.company_id == company_id, Enquiry.id == a.enquiry_id).first()
        if e is not None and e.stage != "converted":
            change_stage(db, company_id, e, "converted", note=f"Enrolled as {student.admission_no}", system=True)
    audit.event(db, "admissions.enrolled", "admission_applications", a.id,
                {"application_no": a.application_no, "student_id": student.id, "admission_no": student.admission_no,
                 "section_id": section.id}, entity_uuid=str(a.uuid), entity_label=a.full_name)
    db.flush()
    return student, logins


def _copy_documents(db: Session, company_id: int, a: Application, student) -> None:
    """Keep the uploaded admission documents on the student record (links signed for five years)."""
    from modules.documents import storage
    from modules.documents.models import FileBlob
    from modules.students.models import StudentDocument
    docs = [d for d in (a.documents or []) if d.get("uuid")]
    if not docs:
        return
    blobs = {str(b.uuid): b for b in db.query(FileBlob).filter(FileBlob.company_id == company_id,
                                                                FileBlob.uuid.in_([d["uuid"] for d in docs])).all()}
    for d in docs:
        b = blobs.get(d["uuid"])
        if b is None:
            continue
        db.add(StudentDocument(company_id=company_id, student_id=student.id, title=d.get("label") or b.filename,
                               doc_type=d.get("key"), file_url=storage.signed_url(b, seconds=5 * 365 * 86400),
                               verified=bool(d.get("verified")), uploaded_by="Admissions"))


# ── Dashboard ────────────────────────────────────────────────────────

def dashboard(db: Session, company_id: int, session_id: int | None, today: date) -> dict:
    eq = db.query(Enquiry.stage, func.count(Enquiry.id)).filter(Enquiry.company_id == company_id,
                                                                Enquiry.archived_at.is_(None))
    aq = db.query(Application.status, func.count(Application.id)).filter(Application.company_id == company_id,
                                                                         Application.archived_at.is_(None))
    if session_id:
        eq = eq.filter(Enquiry.session_id == session_id)
        aq = aq.filter(Application.session_id == session_id)
    by_stage = dict(eq.group_by(Enquiry.stage).all())
    by_status = dict(aq.group_by(Application.status).all())
    total_enq = sum(by_stage.values())
    converted = by_stage.get("converted", 0)
    applied_total = sum(v for k, v in by_status.items() if k != "draft")
    enrolled = by_status.get("enrolled", 0)
    from core.masters import MasterValue
    src_q = (db.query(MasterValue.label, func.count(Enquiry.id)).outerjoin(MasterValue, MasterValue.id == Enquiry.source_id)
             .filter(Enquiry.company_id == company_id, Enquiry.archived_at.is_(None)))
    if session_id:
        src_q = src_q.filter(Enquiry.session_id == session_id)
    by_source = [{"label": label or "Not recorded", "count": n}
                 for label, n in src_q.group_by(MasterValue.label).order_by(func.count(Enquiry.id).desc()).all()]
    follow = enquiry_query(db, company_id, follow_up="due", today=today)
    due = follow.order_by(Enquiry.next_follow_up).limit(8).all()
    upcoming = (db.query(Application).options(joinedload(Application.program))
                .filter(Application.company_id == company_id, Application.archived_at.is_(None),
                        Application.status.in_(("test_scheduled", "interview")),
                        or_(Application.test_at >= _now() - timedelta(hours=12),
                            Application.interview_at >= _now() - timedelta(hours=12)))
                .order_by(func.coalesce(Application.interview_at, Application.test_at)).limit(8).all())
    return {
        "session_id": session_id,
        "stages": [{"key": s, "label": STAGE_LABELS[s], "count": by_stage.get(s, 0)} for s in STAGES],
        "statuses": [{"key": s, "label": STATUS_LABELS[s], "count": by_status.get(s, 0)} for s in STATUSES],
        "by_source": by_source,
        "offerings": list_offerings(db, company_id, session_id, active_only=True),
        "totals": {
            "enquiries": total_enq, "open_enquiries": sum(by_stage.get(s, 0) for s in OPEN_STAGES),
            "applications": applied_total, "enrolled": enrolled, "offered": by_status.get("offered", 0),
            "accepted": by_status.get("accepted", 0),
            "enquiry_conversion": round(converted * 100 / total_enq, 1) if total_enq else 0,
            "application_conversion": round(enrolled * 100 / applied_total, 1) if applied_total else 0,
            "follow_ups_due": follow.order_by(None).count(),
        },
        "follow_ups": [enquiry_out(e) for e in due],
        "upcoming": [application_out(db, a) for a in upcoming],
    }


def summary(db: Session, company_id: int, today: date) -> dict:
    month_start = today.replace(day=1)
    open_enq = db.query(func.count(Enquiry.id)).filter(Enquiry.company_id == company_id, Enquiry.archived_at.is_(None),
                                                       Enquiry.stage.in_(OPEN_STAGES)).scalar() or 0
    due = db.query(func.count(Enquiry.id)).filter(Enquiry.company_id == company_id, Enquiry.archived_at.is_(None),
                                                  Enquiry.stage.in_(OPEN_STAGES),
                                                  Enquiry.next_follow_up <= today).scalar() or 0
    month_start_dt = datetime(month_start.year, month_start.month, 1, tzinfo=timezone.utc)
    apps_month = db.query(func.count(Application.id)).filter(
        Application.company_id == company_id, Application.archived_at.is_(None), Application.status != "draft",
        func.coalesce(Application.submitted_at, Application.created_at) >= month_start_dt).scalar() or 0
    total = db.query(func.count(Enquiry.id)).filter(Enquiry.company_id == company_id,
                                                    Enquiry.archived_at.is_(None)).scalar() or 0
    converted = db.query(func.count(Enquiry.id)).filter(Enquiry.company_id == company_id, Enquiry.archived_at.is_(None),
                                                        Enquiry.stage == "converted").scalar() or 0
    return {"open_enquiries": open_enq, "follow_ups_due": due, "applications_this_month": apps_month,
            "conversion_rate": round(converted * 100 / total, 1) if total else 0, "enquiries": total,
            "converted": converted}


# ── Certificates ─────────────────────────────────────────────────────

FIELD_CATALOGUE = [
    {"group": "student", "label": "Student", "fields": [
        ("student.full_name", "Full name"), ("student.first_name", "First name"), ("student.last_name", "Last name"),
        ("student.admission_no", "Admission number"), ("student.date_of_birth", "Date of birth"),
        ("student.date_of_birth_words", "Date of birth in words"), ("student.gender", "Gender"),
        ("student.he_she", "he / she"), ("student.his_her", "his / her"), ("student.son_daughter", "son / daughter"),
        ("student.father_name", "Father's name"), ("student.mother_name", "Mother's name"),
        ("student.guardian_name", "Parent or guardian's name"), ("student.nationality", "Nationality"),
        ("student.religion", "Religion"), ("student.category", "Category"), ("student.mother_tongue", "Mother tongue"),
        ("student.admission_date", "Date of admission"), ("student.address", "Address"),
        ("student.reg_id", "Board / university ID"), ("student.left_on", "Date of leaving"),
        ("student.leaving_reason", "Reason for leaving"), ("student.previous_institution", "Previous institution"),
    ]},
    {"group": "enrolment", "label": "Enrolment", "fields": [
        ("enrolment.program", "Program (class / course)"), ("enrolment.section", "Section / batch"),
        ("enrolment.class_label", "Program and section"), ("enrolment.roll_no", "Roll number"),
        ("enrolment.session", "Session"), ("enrolment.enrolled_on", "Enrolled on"),
    ]},
    {"group": "institution", "label": "Institution", "fields": [
        ("institution.name", "Name"), ("institution.address", "Address"), ("institution.city", "City"),
        ("institution.phone", "Phone"), ("institution.email", "Email"), ("institution.board", "Board / university"),
        ("institution.affiliation_no", "Affiliation number"), ("institution.principal_name", "Head of institution"),
    ]},
    {"group": "certificate", "label": "Certificate", "fields": [
        ("certificate.number", "Certificate number"), ("certificate.date", "Date of issue"),
        ("certificate.title", "Title"),
    ]},
]
DATE_KEYS = {"student.date_of_birth", "student.admission_date", "student.left_on", "enrolment.enrolled_on",
             "certificate.date"}


def catalogue() -> list[dict]:
    return [{"group": g["group"], "label": g["label"], "fields": [{"key": k, "label": l} for k, l in g["fields"]]}
            for g in FIELD_CATALOGUE]


def _date_words(d: date) -> str:
    from modules.documents.merge import number_in_words
    return f"{number_in_words(d.day)} {d.strftime('%B')} {number_in_words(d.year)}".replace("  ", " ").title()


def student_values(db: Session, company_id: int, student) -> dict:
    from modules.company.models import Company
    from modules.documents.merge import format_date
    from modules.students import service as students
    from modules.students.models import StudentGuardian
    comp = db.query(Company).filter(Company.id == company_id).first()
    prof = academics.profile(comp) if comp else {}
    e = students.current_enrollment(db, company_id, student.id)
    links = (db.query(StudentGuardian).options(joinedload(StudentGuardian.guardian))
             .filter(StudentGuardian.company_id == company_id, StudentGuardian.student_id == student.id).all())

    def rel(name):
        l = next((x for x in links if x.relation == name), None)
        return l.guardian.full_name if l else ""
    primary = next((l for l in links if l.is_primary), links[0] if links else None)
    male, female = student.gender == "male", student.gender == "female"
    addr = ", ".join(p for p in (comp.address if comp else None, comp.city if comp else None,
                                 comp.state if comp else None, comp.pincode if comp else None) if p)
    vals = {
        "student.full_name": student.full_name, "student.first_name": student.first_name,
        "student.last_name": student.last_name or "", "student.admission_no": student.admission_no,
        "student.date_of_birth": format_date(student.date_of_birth) if student.date_of_birth else "",
        "student.date_of_birth_words": _date_words(student.date_of_birth) if student.date_of_birth else "",
        "student.gender": (student.gender or "").title(),
        "student.he_she": "he" if male else "she" if female else "they",
        "student.his_her": "his" if male else "her" if female else "their",
        "student.son_daughter": "son" if male else "daughter" if female else "ward",
        "student.father_name": rel("father"), "student.mother_name": rel("mother"),
        "student.guardian_name": primary.guardian.full_name if primary else "",
        "student.nationality": student.nationality or "", "student.religion": student.religion or "",
        "student.category": student.category or "", "student.mother_tongue": student.mother_tongue or "",
        "student.admission_date": format_date(student.admission_date) if student.admission_date else "",
        "student.address": ", ".join(p for p in (student.address, student.city, student.state, student.pincode) if p),
        "student.reg_id": student.student_reg_id or "",
        "student.left_on": format_date(student.left_on) if student.left_on else "",
        "student.leaving_reason": student.leaving_reason or "",
        "student.previous_institution": student.previous_institution or "",
        "enrolment.program": e.program.name if e and e.program else "",
        "enrolment.section": e.section.name if e and e.section else "",
        "enrolment.class_label": academics.section_label(e.section) if e and e.section else (e.program.name if e and e.program else ""),
        "enrolment.roll_no": e.roll_no or "" if e else "",
        "enrolment.session": e.session.name if e and e.session else "",
        "enrolment.enrolled_on": format_date(e.enrolled_on) if e and e.enrolled_on else "",
        "institution.name": comp.company_name if comp else "", "institution.address": addr,
        "institution.city": comp.city or "" if comp else "", "institution.phone": comp.phone or "" if comp else "",
        "institution.email": comp.email or "" if comp else "", "institution.board": prof.get("board", ""),
        "institution.affiliation_no": prof.get("affiliation_no", ""),
        "institution.principal_name": prof.get("principal_name", ""),
    }
    return vals


def letterhead_html(db: Session, company_id: int) -> str:
    from modules.company.models import Company
    comp = db.query(Company).filter(Company.id == company_id).first()
    if comp is None:
        return ""
    prof = academics.profile(comp)
    esc = html_mod.escape
    logo = ""
    if comp.logo_url and (comp.logo_url.startswith("data:image/") or comp.logo_url.startswith("http")):
        logo = f'<img src="{esc(comp.logo_url)}" style="height:52px;max-width:120px;object-fit:contain" />'
    addr = ", ".join(p for p in (comp.address, comp.city, comp.state, comp.pincode) if p)
    line2 = " · ".join(p for p in (addr, comp.phone, comp.email) if p)
    line3 = " · ".join(p for p in ((f"Affiliated to {prof['board']}" if prof.get("board") else ""),
                                   (f"Affiliation no. {prof['affiliation_no']}" if prof.get("affiliation_no") else "")) if p)
    return (f'<table class="plain" style="width:100%;border-bottom:0.8pt solid #9aa0ad;margin:0"><tr>'
            f'<td style="width:130px;vertical-align:middle">{logo}</td>'
            f'<td style="text-align:center;vertical-align:middle"><div style="font-size:17pt;letter-spacing:0.02em">'
            f'{esc(comp.company_name)}</div><div class="muted" style="font-size:9pt">{esc(line2)}</div>'
            f'<div class="muted" style="font-size:9pt">{esc(line3)}</div></td><td style="width:130px"></td></tr></table>')


def template_out(t: CertificateTemplate, issued: int = 0) -> dict:
    return {"id": t.id, "uuid": str(t.uuid), "name": t.name, "kind": t.kind, "kind_label": CERT_KINDS.get(t.kind, t.kind),
            "title": t.title, "body_html": t.body_html, "fields": t.fields or [], "page_size": t.page_size,
            "orientation": t.orientation, "font_family": t.font_family, "show_letterhead": t.show_letterhead,
            "marks_student": t.marks_student, "active": t.active, "issued": issued, "updated_at": t.updated_at}


def clean_template_fields(items: list[dict] | None) -> list[dict]:
    out, seen = [], set()
    for f in (items or [])[:15]:
        label = str((f or {}).get("label") or "").strip()[:80]
        if not label:
            continue
        key = re.sub(r"[^a-z0-9]+", "_", str(f.get("key") or label).lower()).strip("_")[:40] or "field"
        if key in seen:
            continue
        seen.add(key)
        kind = f.get("type") if f.get("type") in ("text", "date", "textarea") else "text"
        out.append({"key": key, "label": label, "type": kind, "required": bool(f.get("required")),
                    "default": str(f.get("default") or "")[:200]})
    return out


def render_certificate(db: Session, company_id: int, t: CertificateTemplate, student, values: dict,
                       number: str | None, issued_on: date) -> tuple[str, list[str]]:
    from modules.documents.merge import format_date, render
    vals = student_values(db, company_id, student)
    vals.update({"certificate.number": number or "", "certificate.date": format_date(issued_on),
                 "certificate.title": t.title or t.name})
    for f in t.fields or []:
        v = values.get(f["key"], f.get("default") or "")
        if f.get("type") == "date" and v:
            try:
                v = format_date(date.fromisoformat(str(v)))
            except ValueError:
                pass
        vals[f"certificate.{f['key']}"] = v
    body, missing = render(t.body_html, vals)
    return body, missing


def certificate_document(db: Session, company_id: int, t: CertificateTemplate | None, body: str, *,
                         title: str = "Certificate", pdf_mode: bool = True) -> str:
    from modules.documents import pdf
    header = letterhead_html(db, company_id) if (t is None or t.show_letterhead) else None
    opts = dict(header=header, footer=None, header_mm=30 if header else 0, footer_mm=0,
                page_size=t.page_size if t else "A4", orientation=t.orientation if t else "portrait",
                font_family=t.font_family if t else "Georgia", font_size=12)
    framed = f'<div style="padding:4mm 2mm">{body}</div>'
    if pdf_mode:
        return pdf.pdf_html(framed, title=title, **opts)
    return pdf.preview_html(framed, **opts)


def _mark_student(db: Session, company_id: int, student, status: str, on: date, reason: str | None) -> None:
    """Same changes the students module makes when status is set to transferred / left."""
    from modules.students import service as students
    from modules.students.models import PortalAccount
    before = student.status
    student.status = status
    student.left_on = on
    student.leaving_reason = reason or student.leaving_reason
    e = students.current_enrollment(db, company_id, student.id)
    if e and e.status == "enrolled":
        e.status, e.left_on = ("transferred" if status == "transferred" else "dropped"), on
    db.query(PortalAccount).filter(PortalAccount.company_id == company_id,
                                   PortalAccount.student_id == student.id).update({"active": False})
    audit.event(db, "students.status_changed", "students", student.id,
                {"status": status, "from": before, "reason": reason, "via": "certificate"},
                entity_label=student.full_name)


def issue_certificate(db: Session, company_id: int, t: CertificateTemplate, student, *, values: dict,
                      issued_on: date | None = None, mark_student: bool = False) -> Certificate:
    if not t.active:
        raise BadRequest("This certificate template is switched off.")
    if student.archived_at is not None:
        raise BadRequest("This student record is archived.")
    values = {k: str(v).strip()[:500] for k, v in (values or {}).items() if v not in (None, "")}
    for f in t.fields or []:
        if f.get("required") and not (values.get(f["key"]) or f.get("default")):
            raise BadRequest(f"Fill in: {f['label']}.")
    issued_on = issued_on or date.today()
    if mark_student and t.marks_student in ("transferred", "left"):
        on = issued_on
        for key in ("date_of_leaving", "leaving_date", "left_on"):
            if values.get(key):
                try:
                    on = date.fromisoformat(values[key])
                except ValueError:
                    pass
        reason = values.get("reason") or values.get("reason_for_leaving") or values.get("leaving_reason")
        _mark_student(db, company_id, student, t.marks_student, on, reason)
    from core.numbering import next_number
    number = next_number(db, company_id, "certificate")
    body, _ = render_certificate(db, company_id, t, student, values, number, issued_on)
    _, name = _actor()
    c = Certificate(company_id=company_id, number=number, template_id=t.id, student_id=student.id, kind=t.kind,
                    title=t.title or t.name, issued_on=issued_on, values=values, body_html=body, status="issued",
                    marked_student=t.marks_student if mark_student and t.marks_student else None, issued_by_name=name)
    db.add(c)
    db.flush()
    audit.event(db, "certificates.issued", "certificates", c.id,
                {"number": number, "kind": t.kind, "student_id": student.id,
                 "marked_student": c.marked_student}, entity_uuid=str(c.uuid), entity_label=student.full_name)
    return c


def cancel_certificate(db: Session, company_id: int, c: Certificate, reason: str) -> Certificate:
    if c.status == "cancelled":
        raise BadRequest("This certificate is already cancelled.")
    if not (reason or "").strip():
        raise BadRequest("Give the reason for cancelling.")
    _, name = _actor()
    c.status = "cancelled"
    c.cancelled_at = _now()
    c.cancel_reason = reason.strip()[:300]
    c.cancelled_by_name = name
    audit.event(db, "certificates.cancelled", "certificates", c.id, {"number": c.number, "reason": c.cancel_reason},
                entity_uuid=str(c.uuid))
    db.flush()
    return c


def certificate_out(c: Certificate, full: bool = False) -> dict:
    s = c.student
    out = {"id": c.id, "uuid": str(c.uuid), "number": c.number, "kind": c.kind, "kind_label": CERT_KINDS.get(c.kind, c.kind),
           "title": c.title, "issued_on": c.issued_on, "status": c.status, "student_id": c.student_id,
           "student_uuid": str(s.uuid) if s else None, "student_name": s.full_name if s else None,
           "admission_no": s.admission_no if s else None, "template_id": c.template_id,
           "template_name": c.template.name if c.template else None, "marked_student": c.marked_student,
           "issued_by_name": c.issued_by_name, "cancelled_at": c.cancelled_at, "cancel_reason": c.cancel_reason,
           "cancelled_by_name": c.cancelled_by_name, "created_at": c.created_at}
    if full:
        out["values"] = c.values or {}
    return out
