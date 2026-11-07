"""Grievances and POSH complaints.

Who sees what
- POSH complaints: only active members of the active Internal Committee (and the company
  owner when the company switches that on). Not HR, not admins, not managers.
- Other grievances: people with workplace.grievances.manage and members of the grievance
  committee.
- The complainant (when named) sees their own case without the committee's notes.
- Anonymous complainants keep no link to their record. They follow up with the tracking
  code (shown once, stored only as a hash), even without signing in.

POSH deadlines (configurable, defaults from the POSH Act, 2013 and its Rules)
- complaint within 3 months of the incident (last incident), extendable by 3 months
  when the committee records the reason for the delay
- inquiry completed within 90 days of the complaint
- report to the employer within 10 days of completing the inquiry
- employer acts on the report within 60 days
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core import audit, context
from core.config import get_settings as app_settings
from core.errors import BadRequest, Forbidden, NotFound
from core.timeutil import today_for_company_id
from modules.grievances.models import (Grievance, GrievanceCommittee, GrievanceCommitteeMember, GrievanceMessage,
                                       GrievanceSettings, PoshProgramme)

STATUSES = {"submitted": "Submitted", "under_review": "Under review", "inquiry": "Inquiry in progress",
            "report_submitted": "Report submitted", "resolved": "Resolved", "closed": "Closed",
            "rejected": "Not taken up", "withdrawn": "Withdrawn"}
OPEN = ("submitted", "under_review", "inquiry", "report_submitted")
OUTCOMES = {"upheld": "Complaint upheld", "partly_upheld": "Partly upheld", "not_upheld": "Not upheld",
            "settled": "Settled by conciliation", "withdrawn": "Withdrawn", "false_complaint": "False or malicious"}
ROLES = {"presiding_officer": "Presiding officer", "chairperson": "Chairperson", "member": "Member",
         "external_member": "External member (NGO / legal)", "secretary": "Secretary"}

DEFAULT_SETTINGS = {
    "allow_anonymous": True,
    "posh_enabled": True,
    "owner_sees_posh": False,
    "posh_filing_months": 3,
    "posh_extension_months": 3,
    "posh_acknowledge_days": 7,
    "posh_inquiry_days": 90,
    "posh_report_days": 10,
    "posh_action_days": 60,
    "grievance_resolution_days": 30,
    "notify_committee": True,
}
CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today(db: Session, company_id: int) -> date:
    return today_for_company_id(db, company_id)


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    import calendar
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _employee_model():
    from modules.employees.models import Employee
    return Employee


def employee(db: Session, company_id: int, eid):
    if not eid:
        return None
    E = _employee_model()
    return db.query(E).filter(E.company_id == company_id, E.id == eid).first()


def display_name(emp) -> str | None:
    return f"{emp.first_name} {emp.last_name}".strip() if emp else None


def names_for(db: Session, company_id: int, ids) -> dict[int, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    E = _employee_model()
    return {r.id: f"{r.first_name} {r.last_name}".strip()
            for r in db.query(E.id, E.first_name, E.last_name).filter(E.company_id == company_id, E.id.in_(ids))}


def notify(db: Session, company_id: int, employee_id, message: str) -> None:
    if not employee_id:
        return
    from modules.notifications.models import Notification
    db.add(Notification(company_id=company_id, employee_id=employee_id, channel="in_app", message_type="grievance",
                        message_content=message[:1000], status="sent", sent_at=_now()))


def has_perm(db: Session, user: dict, perm: str) -> bool:
    from core.security import _get_role_permissions
    return perm in _get_role_permissions(user["company_id"], user["role"], db)


# ── Settings ────────────────────────────────────────────────────────

def get_settings(db: Session, company_id: int) -> dict:
    row = db.query(GrievanceSettings).filter(GrievanceSettings.company_id == company_id).first()
    return {**DEFAULT_SETTINGS, **((row.config if row else None) or {})}


LIMITS = {"posh_filing_months": (1, 12), "posh_extension_months": (0, 12), "posh_acknowledge_days": (1, 30),
          "posh_inquiry_days": (1, 180), "posh_report_days": (1, 60), "posh_action_days": (1, 180),
          "grievance_resolution_days": (1, 180)}


def update_settings(db: Session, company_id: int, data: dict) -> dict:
    row = db.query(GrievanceSettings).filter(GrievanceSettings.company_id == company_id).first()
    if row is None:
        row = GrievanceSettings(company_id=company_id, config={})
        db.add(row)
    config = {**DEFAULT_SETTINGS, **(row.config or {})}
    for k, v in data.items():
        if k not in DEFAULT_SETTINGS:
            continue
        if isinstance(DEFAULT_SETTINGS[k], bool):
            config[k] = bool(v)
        else:
            lo, hi = LIMITS[k]
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise BadRequest("Use whole numbers for time limits.")
            if not lo <= v <= hi:
                raise BadRequest(f"Keep this between {lo} and {hi}.")
            config[k] = v
    row.config = config
    flag_modified(row, "config")
    db.flush()
    return config


# ── Committees ──────────────────────────────────────────────────────

def committee_member_ids(db: Session, company_id: int, kind: str) -> set[int]:
    rows = (db.query(GrievanceCommitteeMember.employee_id)
            .join(GrievanceCommittee, GrievanceCommittee.id == GrievanceCommitteeMember.committee_id)
            .filter(GrievanceCommittee.company_id == company_id, GrievanceCommittee.kind == kind,
                    GrievanceCommittee.active.is_(True), GrievanceCommitteeMember.active.is_(True),
                    GrievanceCommitteeMember.employee_id.isnot(None)).all())
    return {r[0] for r in rows}


def active_committee(db: Session, company_id: int, kind: str) -> GrievanceCommittee | None:
    return (db.query(GrievanceCommittee).filter(GrievanceCommittee.company_id == company_id,
                                                GrievanceCommittee.kind == kind, GrievanceCommittee.active.is_(True))
            .order_by(GrievanceCommittee.id.desc()).first())


def get_committee(db: Session, company_id: int, uuid) -> GrievanceCommittee:
    row = db.query(GrievanceCommittee).filter(GrievanceCommittee.company_id == company_id,
                                              GrievanceCommittee.uuid == uuid).first()
    if row is None:
        raise NotFound("Committee")
    return row


def save_committee(db: Session, company_id: int, data: dict, row: GrievanceCommittee | None = None) -> GrievanceCommittee:
    kind = data.get("kind") or (row.kind if row else None)
    if kind not in ("grievance", "posh_ic"):
        raise BadRequest("A committee is a grievance committee or a POSH Internal Committee.")
    name = (data.get("name") or (row.name if row else "")).strip()
    if not name:
        raise BadRequest("Give the committee a name.")
    if row is None:
        row = GrievanceCommittee(company_id=company_id, kind=kind, name=name)
        db.add(row)
    row.name, row.kind = name[:150], kind
    for k in ("description", "constituted_on", "valid_until", "active"):
        if k in data and data[k] is not None:
            setattr(row, k, data[k])
    db.flush()
    if data.get("active", row.active):
        db.query(GrievanceCommittee).filter(GrievanceCommittee.company_id == company_id, GrievanceCommittee.kind == kind,
                                            GrievanceCommittee.id != row.id).update({"active": False})
    if "members" in data and data["members"] is not None:
        set_members(db, company_id, row, data["members"])
    return row


def set_members(db: Session, company_id: int, committee: GrievanceCommittee, members: list[dict]) -> None:
    seen = set()
    keep = []
    for m in members:
        role = m.get("role") or "member"
        if role not in ROLES:
            raise BadRequest("Unknown committee role.")
        if m.get("employee_id"):
            if employee(db, company_id, m["employee_id"]) is None:
                raise BadRequest("A committee member was not found.")
            key = ("e", int(m["employee_id"]))
        else:
            if not (m.get("external_name") or "").strip():
                raise BadRequest("Give each outside member a name.")
            key = ("x", m["external_name"].strip().lower())
        if key in seen:
            continue
        seen.add(key)
        keep.append((key, role, m))
    if committee.kind == "posh_ic" and keep:
        if not any(r == "presiding_officer" for _, r, _ in keep):
            raise BadRequest("The Internal Committee needs a presiding officer (a senior woman employee).")
        if not any(r == "external_member" for _, r, _ in keep):
            raise BadRequest("The Internal Committee needs an external member from an NGO or with legal knowledge.")
    existing = {(("e", x.employee_id) if x.employee_id else ("x", (x.external_name or "").lower())): x
                for x in committee.members}
    for key, x in existing.items():
        if key not in {k for k, _, _ in keep}:
            x.active = False
    for key, role, m in keep:
        x = existing.get(key)
        if x is None:
            x = GrievanceCommitteeMember(company_id=company_id, committee_id=committee.id)
            db.add(x)
        x.employee_id = key[1] if key[0] == "e" else None
        x.external_name = None if key[0] == "e" else m["external_name"].strip()[:200]
        x.external_email = (m.get("external_email") or "").strip()[:255] or None
        x.external_org = (m.get("external_org") or "").strip()[:200] or None
        x.role = role
        x.active = True
    db.flush()


# ── Access ──────────────────────────────────────────────────────────

class Viewer:
    def __init__(self, db: Session, user: dict):
        self.company_id = user["company_id"]
        self.employee_id = user.get("employee_id")
        self.is_owner = (user.get("is_owner") or self.employee_id is None) and user.get("role") == "admin"
        self.settings = get_settings(db, self.company_id)
        self.manager = has_perm(db, user, "workplace.grievances.manage")
        self.ic_member = self.employee_id in committee_member_ids(db, self.company_id, "posh_ic") if self.employee_id else False
        self.grc_member = self.employee_id in committee_member_ids(db, self.company_id, "grievance") if self.employee_id else False

    @property
    def sees_posh(self) -> bool:
        return self.ic_member or (self.is_owner and self.settings["owner_sees_posh"])

    @property
    def sees_grievances(self) -> bool:
        return self.manager or self.grc_member

    def handles(self, g: Grievance) -> bool:
        return self.sees_posh if g.kind == "posh" else self.sees_grievances

    def is_complainant(self, g: Grievance) -> bool:
        return self.employee_id is not None and g.complainant_employee_id == self.employee_id

    def kinds(self) -> list[str]:
        return (["posh"] if self.sees_posh else []) + (["grievance"] if self.sees_grievances else [])


def get(db: Session, company_id: int, uuid) -> Grievance:
    row = db.query(Grievance).filter(Grievance.company_id == company_id, Grievance.uuid == uuid).first()
    if row is None:
        raise NotFound("Grievance")
    return row


def visible(db: Session, v: Viewer, uuid) -> Grievance:
    g = get(db, v.company_id, uuid)
    if not (v.handles(g) or v.is_complainant(g)):
        raise NotFound("Grievance")  # never reveal that a POSH case exists
    return g


def list_query(db: Session, v: Viewer, scope: str):
    q = db.query(Grievance).filter(Grievance.company_id == v.company_id)
    if scope == "mine":
        return q.filter(Grievance.complainant_employee_id == (v.employee_id or 0))
    kinds = v.kinds()
    if scope in ("posh", "grievance"):
        kinds = [k for k in kinds if k == scope]
    if not kinds:
        raise Forbidden("You don't handle grievances.")
    return q.filter(Grievance.kind.in_(kinds))


# ── Tracking codes ──────────────────────────────────────────────────

def new_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def normalize_code(code: str) -> str:
    c = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    return f"{c[:4]}-{c[4:8]}-{c[8:12]}" if len(c) == 12 else c


def code_hash(company_id: int, code: str) -> str:
    key = app_settings().SECRET_KEY.encode()
    return hmac.new(key, f"{company_id}:{normalize_code(code)}".encode(), hashlib.sha256).hexdigest()


def by_code(db: Session, company_id: int, code: str) -> Grievance:
    row = db.query(Grievance).filter(Grievance.company_id == company_id,
                                     Grievance.tracking_hash == code_hash(company_id, code)).first()
    if row is None:
        raise NotFound("No grievance matches this tracking code")
    return row


@contextmanager
def anonymous_actor():
    """Nothing recorded while this block runs (audit log, notifications) carries the
    identity, IP address or browser of the person who submitted anonymously."""
    ctx = context.current()
    saved = (ctx.employee_id, ctx.actor_type, ctx.actor_name, ctx.ip_address, ctx.user_agent)
    ctx.employee_id, ctx.actor_type, ctx.actor_name, ctx.ip_address, ctx.user_agent = (
        None, "anonymous", "Anonymous complainant", None, None)
    try:
        yield
    finally:
        ctx.employee_id, ctx.actor_type, ctx.actor_name, ctx.ip_address, ctx.user_agent = saved


# ── Deadlines ───────────────────────────────────────────────────────

def posh_deadlines(conf: dict, received: date, incident: date | None, inquiry_completed: date | None = None,
                   report_submitted: date | None = None) -> dict:
    """Pure: the legal dates for a POSH complaint received on `received`."""
    filing = add_months(incident, conf["posh_filing_months"]) if incident else None
    extended = add_months(filing, conf["posh_extension_months"]) if filing else None
    return {
        "filing_deadline": filing,
        "filing_extended_deadline": extended,
        "late_filing": bool(filing and received > filing),
        "beyond_extension": bool(extended and received > extended),
        "acknowledge_due_on": received + timedelta(days=conf["posh_acknowledge_days"]),
        "inquiry_due_on": received + timedelta(days=conf["posh_inquiry_days"]),
        "report_due_on": (inquiry_completed + timedelta(days=conf["posh_report_days"])) if inquiry_completed else None,
        "action_due_on": (report_submitted + timedelta(days=conf["posh_action_days"])) if report_submitted else None,
    }


def timeline(g: Grievance, today: date) -> list[dict]:
    """Milestones with their due date, when they were done and whether they are late."""
    def item(key, label, due, done, note=None):
        state = "done" if done else ("overdue" if due and due < today else ("due_soon" if due and (due - today).days <= 7 else "upcoming"))
        if not due and not done:
            state = "waiting"
        return {"key": key, "label": label, "due_on": due, "done_on": done, "state": state, "note": note}
    if g.kind != "posh":
        return [item("received", "Received", None, g.received_on),
                item("review", "Taken up", g.acknowledge_due_on, g.acknowledged_on),
                item("resolve", "Resolved", g.inquiry_due_on, g.inquiry_completed_on or (g.closed_at.date() if g.closed_at else None))]
    filing_note = None
    if g.late_filing:
        filing_note = "Filed after the time limit" + (" (delay accepted by the committee)" if g.delay_condoned else "")
    filing = item("filing", "Complaint within the time limit", g.filing_deadline, g.received_on, filing_note) \
        if g.filing_deadline else item("filing", "Complaint received", None, g.received_on)
    if g.late_filing:  # a past fact, not an open deadline
        filing["state"] = "late"
    return [
        filing,
        item("acknowledge", "Acknowledged by the committee", g.acknowledge_due_on, g.acknowledged_on),
        item("inquiry_start", "Inquiry started", None, g.inquiry_started_on),
        item("inquiry", "Inquiry completed", g.inquiry_due_on, g.inquiry_completed_on),
        item("report", "Report to the employer", g.report_due_on, g.report_submitted_on),
        item("action", "Action on the report", g.action_due_on, g.action_completed_on),
    ]


# ── Submission and follow-up ────────────────────────────────────────

def submit(db: Session, company_id: int, data: dict, complainant_id: int | None) -> tuple[Grievance, str]:
    """Create a grievance or POSH complaint. Returns it with the tracking code (shown once)."""
    conf = get_settings(db, company_id)
    kind = data.get("kind") or "grievance"
    if kind not in ("grievance", "posh"):
        raise BadRequest("Pick grievance or POSH complaint.")
    if kind == "posh" and not conf["posh_enabled"]:
        raise BadRequest("POSH complaints are not set up yet. Contact HR.")
    anonymous = bool(data.get("anonymous"))
    if anonymous and not conf["allow_anonymous"]:
        raise BadRequest("Anonymous grievances are switched off.")
    if kind == "posh" and anonymous:
        raise BadRequest("A POSH complaint must be in writing with the complainant's name so the committee can inquire."
                         " Your identity is kept confidential.")
    subject, description = (data.get("subject") or "").strip(), (data.get("description") or "").strip()
    if not subject or not description:
        raise BadRequest("Write a subject and describe what happened.")
    if not anonymous and not complainant_id:
        raise BadRequest("Sign in as an employee, or submit anonymously.")
    today = _today(db, company_id)
    incident = data.get("incident_date")
    if incident and incident > today:
        raise BadRequest("The incident date can't be in the future.")
    code = new_code()
    from core import numbering
    g = Grievance(company_id=company_id, kind=kind, tracking_hash=code_hash(company_id, code),
                  reference_no=numbering.next_number(db, company_id, "grievance") or None,
                  category_id=data.get("category_id"), subject=subject[:250], description=description,
                  anonymous=anonymous, complainant_employee_id=None if anonymous else complainant_id,
                  respondent_employee_id=data.get("respondent_employee_id"),
                  respondent_name=(data.get("respondent_name") or "").strip()[:200] or None,
                  incident_date=incident, incident_location=(data.get("incident_location") or "").strip()[:250] or None,
                  witnesses=(data.get("witnesses") or "").strip()[:500] or None, received_on=today, status="submitted",
                  attachments=data.get("attachments") or [])
    if g.respondent_employee_id and not g.respondent_name:
        g.respondent_name = display_name(employee(db, company_id, g.respondent_employee_id))
    if kind == "posh":
        d = posh_deadlines(conf, today, incident)
        g.filing_deadline, g.late_filing = d["filing_deadline"], d["late_filing"]
        g.acknowledge_due_on, g.inquiry_due_on = d["acknowledge_due_on"], d["inquiry_due_on"]
        ic = active_committee(db, company_id, "posh_ic")
        g.committee_id = ic.id if ic else None
    else:
        g.acknowledge_due_on = today + timedelta(days=7)
        g.inquiry_due_on = today + timedelta(days=conf["grievance_resolution_days"])
        c = active_committee(db, company_id, "grievance")
        g.committee_id = c.id if c else None
    db.add(g)
    db.flush()
    db.add(GrievanceMessage(company_id=company_id, grievance_id=g.id, kind="event", author_label="System",
                            body="Received." + (" Filed after the 3-month limit; the committee may accept the delay with reasons."
                                                if g.late_filing else "")))
    if conf["notify_committee"]:
        members = committee_member_ids(db, company_id, "posh_ic" if kind == "posh" else "grievance")
        for m in members:
            notify(db, company_id, m, f"A new {'POSH complaint' if kind == 'posh' else 'grievance'} was received ({g.reference_no or 'no number'}).")
    return g, code


def post_message(db: Session, g: Grievance, body: str, *, kind: str, from_complainant: bool, author_employee_id=None,
                 author_label: str | None = None, attachments: list | None = None) -> GrievanceMessage:
    body = (body or "").strip()
    if not body:
        raise BadRequest("Write a message.")
    if kind not in ("reply", "note"):
        raise BadRequest("Unknown message type.")
    if g.status in ("closed", "withdrawn", "rejected") and from_complainant:
        raise BadRequest("This case is closed.")
    m = GrievanceMessage(company_id=g.company_id, grievance_id=g.id, kind=kind, body=body,
                         from_complainant=from_complainant, author_employee_id=author_employee_id,
                         author_label=author_label, attachments=attachments or [])
    db.add(m)
    if not from_complainant and kind == "reply":
        notify(db, g.company_id, g.complainant_employee_id, f"The committee replied on {g.reference_no or 'your grievance'}.")
    if from_complainant:
        members = committee_member_ids(db, g.company_id, "posh_ic" if g.kind == "posh" else "grievance")
        for mid in members:
            notify(db, g.company_id, mid, f"New message from the complainant on {g.reference_no or 'a grievance'}.")
    db.flush()
    return m


def _event(db: Session, g: Grievance, text: str) -> None:
    db.add(GrievanceMessage(company_id=g.company_id, grievance_id=g.id, kind="event", body=text,
                            author_label=context.current().actor_name or "System"))


def update(db: Session, v: Viewer, g: Grievance, data: dict) -> Grievance:
    """Committee actions: acknowledge, start and complete the inquiry, submit the report,
    record the outcome and the action taken, accept a late filing, close."""
    if not v.handles(g):
        raise Forbidden("Only the committee can change this case.")
    conf = get_settings(db, g.company_id)
    today = _today(db, g.company_id)
    action = data.get("action")
    on = data.get("on") or today
    if on > today:
        raise BadRequest("The date can't be in the future.")
    if action == "acknowledge":
        g.acknowledged_on = on
        g.status = "under_review"
        _event(db, g, "Acknowledged by the committee.")
        notify(db, g.company_id, g.complainant_employee_id, f"Your {g.reference_no or 'case'} is under review.")
    elif action == "condone_delay":
        if not (data.get("reason") or "").strip():
            raise BadRequest("Record why the delay is accepted.")
        if g.kind == "posh" and g.filing_deadline:
            d = posh_deadlines(conf, g.received_on, g.incident_date)
            if d["beyond_extension"]:
                raise BadRequest("The complaint came after the extended time limit; the delay can't be accepted.")
        g.delay_condoned, g.delay_reason = True, data["reason"].strip()[:500]
        _event(db, g, f"Delay in filing accepted: {g.delay_reason}")
    elif action == "start_inquiry":
        g.inquiry_started_on = on
        g.status = "inquiry"
        g.acknowledged_on = g.acknowledged_on or on
        if data.get("interim_relief"):
            g.interim_relief = data["interim_relief"].strip()[:500]
        _event(db, g, "Inquiry started." + (f" Interim relief: {g.interim_relief}" if g.interim_relief else ""))
    elif action == "conciliation":
        g.conciliation = True
        g.outcome = "settled"
        g.outcome_summary = (data.get("summary") or "").strip() or "Settled through conciliation at the complainant's request."
        g.inquiry_completed_on = g.inquiry_completed_on or on
        g.status = "resolved"
        _event(db, g, "Settled through conciliation.")
    elif action == "complete_inquiry":
        if g.status not in ("inquiry", "under_review"):
            raise BadRequest("Start the inquiry first.")
        g.inquiry_completed_on = on
        g.report_due_on = on + timedelta(days=conf["posh_report_days"]) if g.kind == "posh" else None
        _event(db, g, "Inquiry completed.")
    elif action == "submit_report":
        if not g.inquiry_completed_on:
            raise BadRequest("Complete the inquiry before the report.")
        if data.get("outcome") not in OUTCOMES:
            raise BadRequest("Pick the finding.")
        g.report_submitted_on = on
        g.outcome = data["outcome"]
        g.outcome_summary = (data.get("summary") or "").strip() or None
        g.action_due_on = on + timedelta(days=conf["posh_action_days"]) if g.kind == "posh" else None
        g.status = "report_submitted"
        _event(db, g, f"Report submitted: {OUTCOMES[g.outcome]}.")
    elif action == "record_action":
        if not (data.get("action_taken") or "").strip():
            raise BadRequest("Describe the action taken.")
        g.action_taken = data["action_taken"].strip()
        g.action_completed_on = on
        g.status = "resolved"
        _event(db, g, "Action on the report recorded.")
    elif action == "resolve":
        if g.kind == "posh":
            raise BadRequest("Complete the inquiry and the report for POSH complaints.")
        g.outcome = data.get("outcome") if data.get("outcome") in OUTCOMES else (g.outcome or "upheld")
        g.outcome_summary = (data.get("summary") or "").strip() or g.outcome_summary
        g.action_taken = (data.get("action_taken") or "").strip() or g.action_taken
        g.inquiry_completed_on = on
        g.status = "resolved"
        _event(db, g, "Resolved.")
    elif action == "close":
        if g.status not in ("resolved", "report_submitted", "rejected") and not g.outcome:
            raise BadRequest("Record the outcome before closing.")
        g.status, g.closed_at = "closed", _now()
        _event(db, g, "Closed.")
    elif action == "reject":
        if not (data.get("reason") or "").strip():
            raise BadRequest("Say why the case is not taken up.")
        g.status, g.closed_at, g.outcome_summary = "rejected", _now(), data["reason"].strip()
        _event(db, g, f"Not taken up: {g.outcome_summary}")
    elif action == "assign":
        target = employee(db, g.company_id, data.get("employee_id"))
        if target is None:
            raise BadRequest("Pick a committee member.")
        kind = "posh_ic" if g.kind == "posh" else "grievance"
        if g.kind == "posh" and target.id not in committee_member_ids(db, g.company_id, kind):
            raise BadRequest("POSH complaints can only be assigned to Internal Committee members.")
        g.assigned_to_employee_id = target.id
        _event(db, g, f"Assigned to {display_name(target)}.")
        notify(db, g.company_id, target.id, f"{g.reference_no or 'A case'} was assigned to you.")
    else:
        raise BadRequest("Unknown action.")
    if g.status in ("resolved", "report_submitted", "closed", "rejected") and g.complainant_employee_id and \
            action in ("submit_report", "record_action", "resolve", "close", "reject", "conciliation"):
        notify(db, g.company_id, g.complainant_employee_id,
               f"There is an update on {g.reference_no or 'your case'}: {STATUSES[g.status].lower()}.")
    audit.event(db, f"grievance.{action}", "grievances", g.id, {"status": g.status}, entity_uuid=str(g.uuid),
                entity_label=g.reference_no)
    db.flush()
    return g


def withdraw(db: Session, g: Grievance, reason: str | None) -> Grievance:
    if g.status not in OPEN:
        raise BadRequest("Only open cases can be withdrawn.")
    g.status, g.closed_at = "withdrawn", _now()
    g.outcome = "withdrawn"
    db.add(GrievanceMessage(company_id=g.company_id, grievance_id=g.id, kind="event", author_label="Complainant",
                            body="Withdrawn by the complainant." + (f" {reason.strip()}" if (reason or "").strip() else "")))
    return g


# ── Deadline alerts (background job) ───────────────────────────────

def deadline_alerts(db: Session) -> int:
    """Tell committee members once when a POSH deadline is 7 days away and once when it passes."""
    n = 0
    rows = db.query(Grievance).filter(Grievance.kind == "posh", Grievance.status.in_(OPEN)).limit(1000).all()
    for g in rows:
        today = _today(db, g.company_id)
        sent = dict(g.deadline_alerts or {})
        for key, label, due, done in (("acknowledge", "acknowledge", g.acknowledge_due_on, g.acknowledged_on),
                                      ("inquiry", "complete the inquiry", g.inquiry_due_on, g.inquiry_completed_on),
                                      ("report", "submit the report", g.report_due_on, g.report_submitted_on),
                                      ("action", "act on the report", g.action_due_on, g.action_completed_on)):
            if not due or done:
                continue
            stage = "overdue" if due < today else ("soon" if (due - today).days <= 7 else None)
            if stage and sent.get(key) != stage:
                for m in committee_member_ids(db, g.company_id, "posh_ic"):
                    notify(db, g.company_id, m, f"POSH {g.reference_no}: the deadline to {label} "
                           + ("has passed." if stage == "overdue" else f"is {due.strftime('%d %b %Y')}."))
                sent[key] = stage
                n += 1
        if sent != (g.deadline_alerts or {}):
            g.deadline_alerts = sent
    return n


# ── Annual POSH report ──────────────────────────────────────────────

def annual_report(db: Session, company_id: int, year: int) -> dict:
    """Numbers for the annual report the Internal Committee gives the employer and the
    District Officer (POSH Rules, rule 14), for a calendar year."""
    start, end = date(year, 1, 1), date(year, 12, 31)
    posh = db.query(Grievance).filter(Grievance.company_id == company_id, Grievance.kind == "posh")
    received = posh.filter(Grievance.received_on >= start, Grievance.received_on <= end).all()
    disposed = [g for g in posh.filter(Grievance.status.in_(("resolved", "closed", "rejected", "withdrawn"))).all()
                if (g.action_completed_on or g.report_submitted_on or (g.closed_at.date() if g.closed_at else None)
                    or end) <= end and (g.action_completed_on or g.report_submitted_on or
                                        (g.closed_at.date() if g.closed_at else start)) >= start]
    pending_end = [g for g in posh.filter(Grievance.received_on <= end).all()
                   if g.status in OPEN or (g.closed_at and g.closed_at.date() > end)]
    as_of = min(end, _today(db, company_id))  # the current year counts up to today, not 31 December
    over_90 = [g for g in pending_end if (as_of - g.received_on).days > 90]
    outcomes: dict[str, int] = {}
    for g in disposed:
        if g.outcome:
            outcomes[OUTCOMES.get(g.outcome, g.outcome)] = outcomes.get(OUTCOMES.get(g.outcome, g.outcome), 0) + 1
    progs = (db.query(PoshProgramme).filter(PoshProgramme.company_id == company_id, PoshProgramme.held_on >= start,
                                            PoshProgramme.held_on <= end).order_by(PoshProgramme.held_on).all())
    ic = active_committee(db, company_id, "posh_ic")
    names = names_for(db, company_id, [m.employee_id for m in (ic.members if ic else [])])
    from modules.company.models import Company
    company = db.query(Company).filter(Company.id == company_id).first()
    from modules.employees.models import Employee
    headcount = db.query(func.count(Employee.id)).filter(Employee.company_id == company_id,
                                                        Employee.active.is_(True)).scalar()
    return {
        "year": year, "company_name": company.company_name if company else None, "employees": headcount,
        "complaints_received": len(received), "complaints_disposed": len(disposed),
        "pending_at_year_end": len(pending_end), "pending_over_90_days": len(over_90),
        "conciliations": sum(1 for g in received if g.conciliation),
        "late_filings": sum(1 for g in received if g.late_filing),
        "outcomes": outcomes,
        "actions_taken": [{"reference_no": g.reference_no, "outcome": OUTCOMES.get(g.outcome or "", g.outcome),
                           "action": g.action_taken} for g in disposed if g.action_taken],
        "programmes": [{"held_on": p.held_on, "title": p.title, "kind": p.kind, "participants": p.participants}
                       for p in progs],
        "workshops": len(progs), "participants": sum(p.participants for p in progs),
        "committee": {"name": ic.name, "constituted_on": ic.constituted_on, "valid_until": ic.valid_until,
                      "members": [{"name": names.get(m.employee_id) or m.external_name, "role": ROLES.get(m.role),
                                   "organisation": m.external_org} for m in ic.members if m.active]} if ic else None,
    }
