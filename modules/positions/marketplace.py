"""Internal job marketplace: postings for current employees (jobs, often from a hiring
request or an open position, and gigs with a duration and hours a week), applications
with a skills match score, the current manager's consent through the workflow engine and
moving the chosen person with a job change.

Rules
- Employees see open postings; HR (talent.marketplace.manage) and the posting's hiring
  manager see its applications and decide.
- Before applying for a job a person must have been in their role for min_months_in_role
  (from their joining date or last move); gigs have no such wait. One open application
  per posting and at most max_open_applications at a time. Hiring managers can't apply to
  their own posting.
- When the posting needs consent and the person has a manager, the application waits for
  that manager in the approvals inbox (positions.internal_application); a decline ends it.
- Match score = average over the posting's skills of min(person's level / level asked, 1),
  as a percentage; a missing skill counts 0.
- Moving someone for a job: into the posting's position when it has one (seat rules
  apply), otherwise a job change to the posting's department, designation, location and
  hiring manager. Gigs are assigned without a job change.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core import audit
from core.errors import BadRequest, Forbidden, NotFound
from modules.positions import common as c
from modules.positions.models import InternalApplication, InternalPosting, Position

KINDS = {"job": "Job", "gig": "Gig or project"}
POSTING_STATUSES = {"draft": "Draft", "open": "Open", "closed": "Closed", "filled": "Filled", "cancelled": "Cancelled"}
APP_STATUSES = {
    "manager_review": "Waiting for manager", "submitted": "Applied", "shortlisted": "Shortlisted",
    "selected": "Selected", "moved": "Moved", "assigned": "Assigned", "manager_declined": "Manager declined",
    "rejected": "Not selected", "withdrawn": "Withdrawn",
}
ACTIVE_APPS = ("manager_review", "submitted", "shortlisted", "selected")
WORK_MODES = {"office": "Office", "hybrid": "Hybrid", "remote": "Remote"}


def match_score(required: list[dict], have: dict[int, int]) -> tuple[float | None, list[dict]]:
    """required: [{"skill_id", "level"}]; have: {skill_id: level}. Returns (score %, detail)."""
    if not required:
        return None, []
    total, detail = 0.0, []
    for r in required:
        need = max(1, int(r.get("level") or 1))
        got = int(have.get(int(r["skill_id"]), 0))
        credit = min(got / need, 1.0) if got else 0.0
        total += credit
        detail.append({"skill_id": int(r["skill_id"]), "required": need, "have": got,
                       "met": got >= need, "credit": round(credit * 100)})
    return round(100 * total / len(required), 1), detail


def skill_levels(db: Session, company_id: int, employee_id: int) -> dict[int, int]:
    from modules.organization.models import EmployeeSkill
    return {sid: lvl for sid, lvl in db.query(EmployeeSkill.skill_id, EmployeeSkill.level)
            .filter(EmployeeSkill.company_id == company_id, EmployeeSkill.employee_id == employee_id)}


def months_in_role(db: Session, company_id: int, emp) -> int:
    from modules.employees.models import JobChange
    last = (db.query(func.max(JobChange.effective_date))
            .filter(JobChange.company_id == company_id, JobChange.employee_id == emp.id, JobChange.status == "applied",
                    JobChange.change_type.in_(("transfer", "promotion", "redesignation", "department_change")))
            .scalar())
    dates = [d for d in (last, emp.date_of_joining) if d]
    if not dates:
        return 999
    start = max(dates)
    t = c.today(db, company_id)
    return (t.year - start.year) * 12 + (t.month - start.month) - (1 if t.day < start.day else 0)


# ── Postings ────────────────────────────────────────────────────────

def get_posting(db: Session, company_id: int, uuid) -> InternalPosting:
    p = db.query(InternalPosting).filter(InternalPosting.company_id == company_id, InternalPosting.uuid == uuid).first()
    if p is None:
        raise NotFound("Internal posting")
    return p


def can_manage(v: c.Viewer, p: InternalPosting) -> bool:
    return v.marketplace or (v.employee_id is not None and p.hiring_manager_id == v.employee_id)


def posting_outputs(db: Session, v: c.Viewer, rows: list[InternalPosting], detail: bool = False) -> list[dict]:
    cid = v.company_id
    labels = c.org_labels(db, cid, departments=[r.department_id for r in rows], designations=[r.designation_id for r in rows],
                          locations=[r.location_id for r in rows])
    names = c.people(db, cid, [r.hiring_manager_id for r in rows])
    skills = c.skill_names(db, cid, {s.get("skill_id") for r in rows for s in (r.skills or [])})
    pos = {p.id: p for p in db.query(Position).filter(Position.company_id == cid,
                                                      Position.id.in_({r.position_id for r in rows if r.position_id} or {0}))}
    counts = dict(db.query(InternalApplication.posting_id, func.count(InternalApplication.id))
                  .filter(InternalApplication.company_id == cid, InternalApplication.posting_id.in_([r.id for r in rows] or [0]),
                          InternalApplication.status.notin_(("withdrawn",)))
                  .group_by(InternalApplication.posting_id).all())
    mine = {}
    have = {}
    if v.employee_id:
        mine = {a.posting_id: a for a in db.query(InternalApplication).filter(
            InternalApplication.company_id == cid, InternalApplication.employee_id == v.employee_id,
            InternalApplication.posting_id.in_([r.id for r in rows] or [0])).order_by(InternalApplication.id)}
        have = skill_levels(db, cid, v.employee_id)
    req_numbers = {}
    if any(r.requisition_id for r in rows):
        from modules.recruitment.models import Requisition
        req_numbers = {r.id: (r.number, str(r.uuid)) for r in db.query(Requisition).filter(
            Requisition.company_id == cid, Requisition.id.in_({r.requisition_id for r in rows if r.requisition_id}))}
    t = c.today(db, cid)
    out = []
    for r in rows:
        p = pos.get(r.position_id)
        score, detail_ = match_score(r.skills or [], have) if v.employee_id else (None, [])
        a = mine.get(r.id)
        d = {"uuid": str(r.uuid), "number": r.number, "kind": r.kind, "kind_label": KINDS.get(r.kind), "title": r.title,
             "description": r.description, "status": r.status, "status_label": POSTING_STATUSES.get(r.status),
             "department_id": r.department_id, "department": labels["department"].get(r.department_id),
             "designation_id": r.designation_id, "designation": labels["designation"].get(r.designation_id),
             "location_id": r.location_id, "location": labels["location"].get(r.location_id),
             "position": {"uuid": str(p.uuid), "code": p.code, "title": p.title} if p else None,
             "requisition": {"number": req_numbers[r.requisition_id][0], "uuid": req_numbers[r.requisition_id][1]}
             if r.requisition_id in req_numbers else None,
             "skills": [{**s, "name": skills.get(s.get("skill_id"))} for s in (r.skills or [])],
             "openings": r.openings, "duration_weeks": r.duration_weeks, "hours_per_week": r.hours_per_week,
             "work_mode": r.work_mode, "work_mode_label": WORK_MODES.get(r.work_mode or ""),
             "starts_on": r.starts_on.isoformat() if r.starts_on else None,
             "closes_on": r.closes_on.isoformat() if r.closes_on else None,
             "closing_soon": bool(r.closes_on and r.status == "open" and 0 <= (r.closes_on - t).days <= 7),
             "hiring_manager": c.person_ref(names.get(r.hiring_manager_id)), "hiring_manager_id": r.hiring_manager_id,
             "needs_manager_consent": r.needs_manager_consent, "applications": counts.get(r.id, 0),
             "published_at": r.published_at, "created_at": r.created_at,
             "can_manage": can_manage(v, r),
             "my_match": score, "my_match_detail": [{**x, "name": skills.get(x["skill_id"])} for x in detail_],
             "my_application": {"uuid": str(a.uuid), "status": a.status, "status_label": APP_STATUSES.get(a.status)} if a else None}
        out.append(d)
    return out


def list_postings(db: Session, v: c.Viewer, *, scope: str = "open", q: str | None = None, kind: str | None = None,
                  status: str | None = None):
    query = db.query(InternalPosting).filter(InternalPosting.company_id == v.company_id)
    if scope == "manage":
        if not v.marketplace:
            query = query.filter(InternalPosting.hiring_manager_id == (v.employee_id or 0))
        if status:
            query = query.filter(InternalPosting.status.in_(status.split(",")))
    else:
        t = c.today(db, v.company_id)
        query = query.filter(InternalPosting.status == "open",
                             or_(InternalPosting.closes_on.is_(None), InternalPosting.closes_on >= t))
    if kind:
        query = query.filter(InternalPosting.kind == kind)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(InternalPosting.title.ilike(like), InternalPosting.description.ilike(like)))
    return query.order_by(InternalPosting.published_at.desc().nullslast(), InternalPosting.id.desc())


def save_posting(db: Session, v: c.Viewer, data: dict, row: InternalPosting | None = None) -> InternalPosting:
    from modules.positions import service as pos
    cid = v.company_id
    if row is not None and not can_manage(v, row):
        raise Forbidden("Only HR or the hiring manager can change this posting.")
    if row is None and not v.marketplace:
        raise Forbidden("Only HR can post internal jobs and gigs.")
    if row is not None and row.status in ("filled", "cancelled"):
        raise BadRequest("This posting is finished.")
    kind = data.get("kind") or (row.kind if row else "job")
    if kind not in KINDS:
        raise BadRequest("A posting is a job or a gig.")
    title = (data.get("title") if "title" in data else (row.title if row else "")) or ""
    position = None
    if data.get("position_uuid"):
        position = pos.get(db, cid, data["position_uuid"])
        if position.status != "approved":
            raise BadRequest("Only approved positions can be posted.")
    if data.get("requisition_uuid") and row is None:
        from modules.recruitment.models import Requisition
        req = db.query(Requisition).filter(Requisition.company_id == cid, Requisition.uuid == data["requisition_uuid"]).first()
        if req is None:
            raise BadRequest("That hiring request doesn't exist.")
        if req.status not in ("approved", "pending"):
            raise BadRequest("Only approved hiring requests can be posted inside the company.")
        if db.query(InternalPosting.id).filter(InternalPosting.company_id == cid, InternalPosting.requisition_id == req.id,
                                               InternalPosting.status.in_(("draft", "open"))).first():
            raise BadRequest("This hiring request is already posted inside the company.")
        data = {"title": req.title, "department_id": req.department_id, "designation_id": req.designation_id,
                "location_id": req.location_id, "openings": req.openings, "hiring_manager_id": req.hiring_manager_id,
                "description": req.justification, **{k: v_ for k, v_ in data.items() if v_ not in (None, "")}}
        title = data.get("title") or title
        if position is None:
            position = db.query(Position).filter(Position.company_id == cid,
                                                 Position.requisition_ids.contains([req.id])).first()
        data["_requisition_id"] = req.id
    if position is not None:
        for k in ("department_id", "designation_id", "location_id"):
            data.setdefault(k, getattr(position, k))
        title = title or position.title
        if not data.get("skills") and position.skills:
            data["skills"] = position.skills
    title = title.strip()
    if not title:
        raise BadRequest("Give the posting a title.")
    if row is None:
        from core import numbering
        row = InternalPosting(company_id=cid, kind=kind, title=title, status="draft", created_by_name=c.actor_name(),
                              number=numbering.next_number(db, cid, "internal_posting", c.today(db, cid)))
        db.add(row)
    row.kind, row.title = kind, title[:200]
    if position is not None:
        row.position_id = position.id
    if data.get("_requisition_id"):
        row.requisition_id = data["_requisition_id"]
    if "description" in data:
        row.description = (data.get("description") or "").strip() or None
    for k in ("department_id", "designation_id", "location_id"):
        if k in data:
            setattr(row, k, data.get(k) or None)
    pos._check_refs(db, cid, {k: getattr(row, k) for k in ("department_id", "designation_id", "location_id")})
    if "skills" in data:
        row.skills = c.clean_skills(db, cid, data.get("skills") or [])
        flag_modified(row, "skills")
    if "openings" in data:
        n = int(data.get("openings") or 1)
        if not 1 <= n <= 100:
            raise BadRequest("Openings should be between 1 and 100.")
        row.openings = n
    if kind == "gig":
        for k, label, hi in (("duration_weeks", "Duration", 104), ("hours_per_week", "Hours a week", 40)):
            if k in data:
                val = data.get(k)
                if val in (None, ""):
                    setattr(row, k, None)
                    continue
                n = int(val)
                if not 1 <= n <= hi:
                    raise BadRequest(f"{label} should be between 1 and {hi}.")
                setattr(row, k, n)
        if not row.duration_weeks or not row.hours_per_week:
            raise BadRequest("Say how many weeks the gig runs and how many hours a week it needs.")
    else:
        row.duration_weeks = row.hours_per_week = None
    if "work_mode" in data:
        wm = data.get("work_mode") or None
        if wm and wm not in WORK_MODES:
            raise BadRequest("Work mode is office, hybrid or remote.")
        row.work_mode = wm
    for k in ("starts_on", "closes_on"):
        if k in data:
            setattr(row, k, data.get(k) or None)
    if "hiring_manager_id" in data:
        if data.get("hiring_manager_id"):
            c.require_employee(db, cid, data["hiring_manager_id"], "hiring manager")
        row.hiring_manager_id = data.get("hiring_manager_id") or None
    if "needs_manager_consent" in data:
        row.needs_manager_consent = bool(data["needs_manager_consent"])
    elif row.id is None:
        row.needs_manager_consent = bool(c.get_settings(db, cid)["manager_consent_default"])
    db.flush()
    return row


def set_posting_status(db: Session, v: c.Viewer, row: InternalPosting, action: str) -> InternalPosting:
    if not can_manage(v, row):
        raise Forbidden("Only HR or the hiring manager can do this.")
    t = c.today(db, v.company_id)
    if action == "publish":
        if row.status not in ("draft", "closed"):
            raise BadRequest("Only drafts and closed postings can be opened.")
        if not c.get_settings(db, v.company_id)["marketplace_enabled"]:
            raise BadRequest("The internal job marketplace is switched off in settings.")
        if row.closes_on and row.closes_on < t:
            raise BadRequest("The closing date has passed. Move it first.")
        row.status = "open"
        row.published_at = row.published_at or c.now()
    elif action == "close":
        if row.status != "open":
            raise BadRequest("Only open postings can be closed.")
        row.status = "closed"
    elif action == "cancel":
        if row.status in ("filled", "cancelled"):
            raise BadRequest("This posting is already finished.")
        row.status = "cancelled"
        for a in db.query(InternalApplication).filter(InternalApplication.company_id == v.company_id,
                                                      InternalApplication.posting_id == row.id,
                                                      InternalApplication.status.in_(ACTIVE_APPS)):
            _end_application(db, a, "rejected", "The posting was cancelled.")
    else:
        raise BadRequest("Unknown action.")
    db.flush()
    return row


# ── Applications ────────────────────────────────────────────────────

def get_application(db: Session, company_id: int, uuid) -> InternalApplication:
    a = db.query(InternalApplication).filter(InternalApplication.company_id == company_id,
                                             InternalApplication.uuid == uuid).first()
    if a is None:
        raise NotFound("Application")
    return a


def application_outputs(db: Session, company_id: int, rows: list[InternalApplication], *, for_manager: bool = True) -> list[dict]:
    names = c.people(db, company_id, [a.employee_id for a in rows] + [a.manager_employee_id for a in rows])
    labels = c.org_labels(db, company_id, departments=[n["department_id"] for n in names.values()],
                          designations=[n["designation_id"] for n in names.values()])
    posts = {p.id: p for p in db.query(InternalPosting).filter(InternalPosting.company_id == company_id,
                                                              InternalPosting.id.in_({a.posting_id for a in rows} or {0}))}
    skills = c.skill_names(db, company_id, {x.get("skill_id") for a in rows for x in (a.match_detail or [])})
    talent = {}
    if for_manager and rows:
        from modules.positions.succession import talent_snapshot
        talent = talent_snapshot(db, company_id, [a.employee_id for a in rows])
    out = []
    for a in rows:
        p = posts.get(a.posting_id)
        e = names.get(a.employee_id) or {}
        d = {"uuid": str(a.uuid), "status": a.status, "status_label": APP_STATUSES.get(a.status),
             "posting": {"uuid": str(p.uuid), "title": p.title, "kind": p.kind, "kind_label": KINDS.get(p.kind),
                         "status": p.status, "number": p.number} if p else None,
             "employee": c.person_ref(e) if e else None, "employee_id": a.employee_id,
             "designation": labels["designation"].get(e.get("designation_id")),
             "department": labels["department"].get(e.get("department_id")),
             "manager": c.person_ref(names.get(a.manager_employee_id)),
             "note": a.note, "match_score": float(a.match_score) if a.match_score is not None else None,
             "match_detail": [{**x, "name": skills.get(x.get("skill_id"))} for x in (a.match_detail or [])],
             "manager_note": a.manager_note, "decision_note": a.decision_note,
             "effective_date": a.effective_date.isoformat() if a.effective_date else None,
             "created_at": a.created_at, "decided_at": a.decided_at}
        if for_manager:
            d["talent"] = talent.get(a.employee_id)
        out.append(d)
    return out


def apply(db: Session, v: c.Viewer, p: InternalPosting, note: str | None) -> InternalApplication:
    cid = v.company_id
    settings = c.get_settings(db, cid)
    if not v.employee_id:
        raise BadRequest("Only employees can apply.")
    if not settings["marketplace_enabled"]:
        raise BadRequest("The internal job marketplace is switched off.")
    t = c.today(db, cid)
    if p.status != "open" or (p.closes_on and p.closes_on < t):
        raise BadRequest("This posting is not open for applications.")
    emp = c.require_employee(db, cid, v.employee_id)
    if p.hiring_manager_id == emp.id:
        raise BadRequest("You are the hiring manager for this posting.")
    active = (db.query(InternalApplication).filter(InternalApplication.company_id == cid,
                                                   InternalApplication.employee_id == emp.id,
                                                   InternalApplication.status.in_(ACTIVE_APPS)).all())
    if any(a.posting_id == p.id for a in active):
        raise BadRequest("You have already applied for this.")
    if len(active) >= settings["max_open_applications"]:
        raise BadRequest(f"You can have up to {settings['max_open_applications']} open applications at a time. "
                         "Withdraw one to apply here.")
    if p.kind == "job" and settings["min_months_in_role"]:
        months = months_in_role(db, cid, emp)
        if months < settings["min_months_in_role"]:
            raise BadRequest(f"You can apply for internal jobs after {settings['min_months_in_role']} months in your "
                             f"current role (you have {months}).")
    if p.position_id:
        from modules.positions import service as pos
        seat = pos.current_seat(db, cid, emp.id)
        if seat is not None and seat.position_id == p.position_id:
            raise BadRequest("You already hold this position.")
    score, detail = match_score(p.skills or [], skill_levels(db, cid, emp.id))
    a = InternalApplication(company_id=cid, posting_id=p.id, employee_id=emp.id, note=(note or "").strip()[:2000] or None,
                            match_score=score, match_detail=detail, manager_employee_id=emp.reporting_manager_id,
                            status="submitted")
    db.add(a)
    db.flush()
    if p.needs_manager_consent and emp.reporting_manager_id and emp.reporting_manager_id != p.hiring_manager_id:
        a.status = "manager_review"
        from core.workflow import service as wf
        payload = {"application_id": a.id, "manager_id": emp.reporting_manager_id, "employee_id": emp.id,
                   "employee": c.display_name(emp), "posting": p.title, "kind": p.kind, "kind_label": KINDS.get(p.kind),
                   "match_score": float(score) if score is not None else None,
                   "duration_weeks": p.duration_weeks, "hours_per_week": p.hours_per_week}
        inst = wf.submit(db, cid, "positions.internal_application", payload,
                         f"Internal application: {c.display_name(emp)} for {p.title}",
                         subject_type="internal_application", subject_id=a.id)
        a.workflow_instance_id = inst.id
    else:
        _notify_hiring(db, p, a, emp)
    audit.event(db, "marketplace.applied", "internal_applications", a.id, {"posting": p.title, "score": score},
                entity_uuid=str(a.uuid), entity_label=p.title)
    db.flush()
    return a


def _notify_hiring(db: Session, p: InternalPosting, a: InternalApplication, emp) -> None:
    if p.hiring_manager_id:
        c.notify(db, p.company_id, p.hiring_manager_id,
                 f"{c.display_name(emp)} applied for “{p.title}”" + (f" ({float(a.match_score):g}% skills match)." if a.match_score is not None else "."),
                 "marketplace")


def on_consent(db: Session, instance, status: str) -> None:
    a = db.query(InternalApplication).filter(InternalApplication.company_id == instance.company_id,
                                             InternalApplication.id == (instance.payload or {}).get("application_id")).first()
    if a is None or a.status != "manager_review":
        return
    if status == "approved":
        a.status = "submitted"
        a.manager_note = (instance.result_note or "")[:500] or None
        p = db.get(InternalPosting, a.posting_id)
        emp = c.employee(db, a.company_id, a.employee_id)
        _notify_hiring(db, p, a, emp)
        c.notify(db, a.company_id, a.employee_id, f"Your manager agreed to your application for “{p.title}”.", "marketplace")
    elif status == "rejected":
        a.status = "manager_declined"
        a.decided_at = c.now()
        a.manager_note = (instance.result_note or "")[:500] or None
        p = db.get(InternalPosting, a.posting_id)
        c.notify(db, a.company_id, a.employee_id, f"Your manager did not agree to your application for “{p.title}”.", "marketplace")
    else:
        a.status = "withdrawn"
    db.flush()


def withdraw(db: Session, v: c.Viewer, a: InternalApplication) -> InternalApplication:
    if a.employee_id != v.employee_id:
        raise Forbidden("You can withdraw only your own applications.")
    if a.status not in ACTIVE_APPS:
        raise BadRequest("This application is already closed.")
    if a.status == "manager_review" and a.workflow_instance_id:
        from core.workflow import service as wf
        from core.workflow.models import WorkflowInstance
        inst = db.query(WorkflowInstance).filter(WorkflowInstance.company_id == v.company_id,
                                                 WorkflowInstance.id == a.workflow_instance_id).first()
        if inst is not None and inst.status == "pending":
            wf.cancel(db, v.company_id, inst, "Application withdrawn")
    a.status = "withdrawn"
    a.decided_at = c.now()
    db.flush()
    return a


def _end_application(db: Session, a: InternalApplication, status: str, note: str | None) -> None:
    a.status = status
    a.decision_note = (note or "").strip()[:500] or None
    a.decided_at = c.now()
    if a.workflow_instance_id:
        from core.workflow.models import WorkflowInstance
        inst = db.query(WorkflowInstance).filter(WorkflowInstance.company_id == a.company_id,
                                                 WorkflowInstance.id == a.workflow_instance_id).first()
        if inst is not None and inst.status == "pending":
            from core.workflow import service as wf
            wf._finish(db, inst, "cancelled", "The application was closed")


def decide(db: Session, v: c.Viewer, a: InternalApplication, action: str, note: str | None = None,
           effective_date: date | None = None) -> InternalApplication:
    p = db.get(InternalPosting, a.posting_id)
    if not can_manage(v, p):
        raise Forbidden("Only HR or the hiring manager can decide on applications.")
    cid = v.company_id
    title = p.title
    if action == "shortlist":
        if a.status != "submitted":
            raise BadRequest("Only new applications can be shortlisted.")
        a.status = "shortlisted"
        c.notify(db, cid, a.employee_id, f"You have been shortlisted for “{title}”.", "marketplace")
    elif action == "reject":
        if a.status not in ("submitted", "shortlisted", "selected"):
            raise BadRequest("This application can't be turned down now.")
        _end_application(db, a, "rejected", note)
        c.notify(db, cid, a.employee_id, f"Your application for “{title}” was not taken forward.", "marketplace")
    elif action == "reconsider":
        if a.status != "rejected":
            raise BadRequest("Only turned-down applications can be reconsidered.")
        a.status, a.decided_at = "submitted", None
    elif action == "select":
        if a.status not in ("submitted", "shortlisted"):
            raise BadRequest("Shortlist the application first.")
        taken = (db.query(func.count(InternalApplication.id))
                 .filter(InternalApplication.company_id == cid, InternalApplication.posting_id == p.id,
                         InternalApplication.status.in_(("selected", "moved", "assigned"))).scalar())
        if taken >= p.openings:
            raise BadRequest(f"All {p.openings} opening(s) already have someone selected.")
        a.status = "selected"
        a.decision_note = (note or "").strip()[:500] or a.decision_note
        c.notify(db, cid, a.employee_id, f"You have been selected for “{title}”.", "marketplace")
    elif action in ("move", "assign"):
        if a.status != "selected":
            raise BadRequest("Select the person first.")
        emp = c.require_employee(db, cid, a.employee_id)
        eff = effective_date or c.today(db, cid)
        if p.kind == "gig":
            a.status = "assigned"
        else:
            from modules.positions import service as pos
            if p.position_id:
                position = pos.by_id(db, cid, p.position_id)
                seat = pos.assign(db, cid, position, emp.id, eff, reason=f"Internal move: {title}",
                                  change_type="transfer", source="marketplace", sync_job=True)
                a.job_change_id = seat.job_change_id
            else:
                vals = {k: getattr(p, k) for k in ("department_id", "designation_id", "location_id") if getattr(p, k)}
                if p.hiring_manager_id and p.hiring_manager_id != emp.id:
                    vals["reporting_manager_id"] = p.hiring_manager_id
                diff = {k: val for k, val in vals.items() if getattr(emp, k) != val}
                if diff:
                    from modules.employees import service as emp_service
                    jc = emp_service.record_job_change(db, cid, emp, "transfer", eff, diff, f"Internal move: {title}")
                    a.job_change_id = jc.id
            a.status = "moved"
        a.effective_date = eff
        a.decided_at = c.now()
        db.flush()
        done = (db.query(func.count(InternalApplication.id))
                .filter(InternalApplication.company_id == cid, InternalApplication.posting_id == p.id,
                        InternalApplication.status.in_(("moved", "assigned"))).scalar())
        if done >= p.openings and p.status in ("open", "closed"):
            p.status = "filled"
        c.notify(db, cid, a.employee_id,
                 f"Your move to “{title}” takes effect on {eff.strftime('%d %b %Y')}." if p.kind == "job"
                 else f"You are on the gig “{title}” from {eff.strftime('%d %b %Y')}.", "marketplace")
        if a.manager_employee_id:
            c.notify(db, cid, a.manager_employee_id,
                     f"{c.display_name(emp)} moves to “{title}” from {eff.strftime('%d %b %Y')}.", "marketplace")
        audit.event(db, "marketplace.moved", "internal_applications", a.id,
                    {"posting": title, "effective_date": eff.isoformat(), "kind": p.kind},
                    entity_uuid=str(a.uuid), entity_label=title)
    else:
        raise BadRequest("Unknown action.")
    db.flush()
    return a


def list_applications(db: Session, v: c.Viewer, *, posting: InternalPosting | None = None, mine: bool = False,
                      status: str | None = None):
    q = db.query(InternalApplication).filter(InternalApplication.company_id == v.company_id)
    if mine:
        q = q.filter(InternalApplication.employee_id == (v.employee_id or 0))
    elif posting is not None:
        if not can_manage(v, posting):
            raise Forbidden("Only HR or the hiring manager can see applications.")
        q = q.filter(InternalApplication.posting_id == posting.id)
    elif not v.marketplace:
        managed = [i for (i,) in db.query(InternalPosting.id).filter(InternalPosting.company_id == v.company_id,
                                                                    InternalPosting.hiring_manager_id == (v.employee_id or 0))]
        q = q.filter(InternalApplication.posting_id.in_(managed or [0]))
    if status:
        q = q.filter(InternalApplication.status.in_(status.split(",")))
    return q.order_by(InternalApplication.match_score.desc().nullslast(), InternalApplication.id.desc()) if not mine \
        else q.order_by(InternalApplication.id.desc())


def suggested_people(db: Session, v: c.Viewer, p: InternalPosting, limit: int = 10) -> list[dict]:
    """Employees with the best skills match for a posting who have not applied."""
    from modules.organization.models import EmployeeSkill
    req = p.skills or []
    if not req:
        return []
    ids = {r["skill_id"] for r in req}
    rows = (db.query(EmployeeSkill.employee_id, EmployeeSkill.skill_id, EmployeeSkill.level)
            .filter(EmployeeSkill.company_id == v.company_id, EmployeeSkill.skill_id.in_(ids)).all())
    by: dict[int, dict] = {}
    for eid, sid, lvl in rows:
        by.setdefault(eid, {})[sid] = lvl
    applied = {i for (i,) in db.query(InternalApplication.employee_id).filter(
        InternalApplication.company_id == v.company_id, InternalApplication.posting_id == p.id)}
    scored = sorted(((match_score(req, have)[0] or 0, eid) for eid, have in by.items() if eid not in applied), reverse=True)
    names = c.people(db, v.company_id, [e for _, e in scored[: limit * 2]])
    return [{**c.person_ref(names[e]), "match_score": s} for s, e in scored[: limit * 2]
            if e in names and names[e]["active"] and e != p.hiring_manager_id][:limit]
