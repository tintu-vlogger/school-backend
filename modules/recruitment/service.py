"""Hiring rules: requisitions, jobs, candidates, pipelines, screening, interviews and
scorecards, reference checks, offers, referrals, privacy purge and reports.

Functions other modules may call:
    offer_for_employee(db, company_id, employee_id) -> Offer | None
    candidate_summary(db, company_id, candidate_id) -> dict
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.orm.attributes import flag_modified

from core import audit, context
from core.errors import BadRequest, Forbidden, NotFound
from modules.recruitment import common as c
from modules.recruitment import screening, settings as rsettings
from modules.recruitment.models import (Application, Candidate, Interview, InterviewKit, Job, Offer, ReferenceCheck,
                                        Referral, Requisition, Scorecard)

MANAGE = "talent.hiring.manage"
OFFERS = "talent.offers.manage"
WORK_MODES = {"office": "Office", "remote": "Remote", "hybrid": "Hybrid"}
JOB_STATUSES = {"draft": "Draft", "open": "Open", "on_hold": "On hold", "closed": "Closed", "filled": "Filled"}
REQ_STATUSES = {"draft": "Draft", "pending": "Waiting for approval", "approved": "Approved", "rejected": "Rejected",
                "cancelled": "Cancelled", "filled": "Filled"}
OFFER_STATUSES = {"draft": "Draft", "pending_approval": "Waiting for approval", "approved": "Approved",
                  "rejected": "Not approved", "sent": "Sent", "accepted": "Accepted", "declined": "Declined",
                  "expired": "Expired", "withdrawn": "Withdrawn"}
RECOMMENDATIONS = {"strong_yes": "Strong yes", "yes": "Yes", "no": "No", "strong_no": "Strong no"}
BGV_STATUSES = {"not_started": "Not started", "consent_pending": "Waiting for consent", "in_progress": "In progress",
                "clear": "Clear", "discrepancy": "Discrepancy found", "failed": "Failed"}
MODES = {"video": "Video call", "phone": "Phone", "in_person": "In person"}


# ── Viewer ──────────────────────────────────────────────────────────

class Viewer:
    def __init__(self, db: Session, user: dict):
        self.db, self.user = db, user
        self.company_id = user["company_id"]
        self.employee_id = user.get("employee_id")
        self.admin = c.has_perm(db, user, MANAGE)
        self.offers = self.admin or c.has_perm(db, user, OFFERS)

    def require_admin(self):
        if not self.admin:
            raise Forbidden("Only the hiring team can do this.")

    def on_team(self, job: Job) -> bool:
        if self.admin:
            return True
        if not self.employee_id:
            return False
        return job.hiring_manager_id == self.employee_id or any(
            int(m.get("employee_id") or 0) == self.employee_id for m in (job.team or []))


# ── Requisitions ────────────────────────────────────────────────────

def get_requisition(db: Session, company_id: int, uuid) -> Requisition:
    row = db.query(Requisition).filter(Requisition.company_id == company_id, Requisition.uuid == uuid).first()
    if row is None:
        raise NotFound("Hiring request")
    return row


def save_requisition(db: Session, v: Viewer, data: dict, row: Requisition | None = None) -> Requisition:
    if row is not None and row.status not in ("draft", "rejected"):
        raise BadRequest("Only drafts and rejected requests can be changed.")
    if row is not None and not v.admin and row.requested_by_employee_id != v.employee_id:
        raise Forbidden("You can change only your own hiring requests.")
    title = (data.get("title") or (row.title if row else "")).strip()
    if not title:
        raise BadRequest("Give the position a title.")
    openings = int(data.get("openings") or (row.openings if row else 1) or 1)
    if not 1 <= openings <= 500:
        raise BadRequest("Openings should be between 1 and 500.")
    bmin = c.money(data.get("budget_min"), "Minimum CTC") if "budget_min" in data else (row.budget_min if row else None)
    bmax = c.money(data.get("budget_max"), "Maximum CTC") if "budget_max" in data else (row.budget_max if row else None)
    if bmin and bmax and bmin > bmax:
        raise BadRequest("The minimum CTC can't be more than the maximum.")
    kind = data.get("kind") or (row.kind if row else "new")
    if kind not in ("new", "replacement"):
        raise BadRequest("A request is for a new position or a replacement.")
    if kind == "replacement" and not (data.get("replacement_for_id") or (row.replacement_for_id if row else None)):
        raise BadRequest("Pick the person being replaced.")
    if row is None:
        emp = c.employee(db, v.company_id, v.employee_id)
        row = Requisition(company_id=v.company_id, status="draft", requested_by_employee_id=v.employee_id,
                          requested_by_name=c.display_name(emp) or c.actor_name())
        db.add(row)
    row.title, row.openings, row.budget_min, row.budget_max, row.kind = title[:200], openings, bmin, bmax, kind
    for k in ("department_id", "designation_id", "location_id", "employment_type_id", "replacement_for_id",
              "hiring_manager_id", "needed_by", "min_experience", "max_experience"):
        if k in data:
            setattr(row, k, data[k] or None)
    if "justification" in data:
        row.justification = (data.get("justification") or "").strip() or None
    if not row.justification:
        raise BadRequest("Say why this position is needed.")
    if row.status == "rejected":
        row.status = "draft"
    db.flush()
    return row


def submit_requisition(db: Session, v: Viewer, row: Requisition) -> Requisition:
    if row.status not in ("draft",):
        raise BadRequest("This request has already been sent for approval.")
    from core.workflow import service as wf
    if not row.number:
        from core import numbering
        row.number = numbering.next_number(db, v.company_id, "requisition", c.today(db, v.company_id))
    labels = c.org_labels(db, v.company_id, departments=[row.department_id], locations=[row.location_id])
    payload = {"requisition_id": row.id, "title": row.title, "openings": row.openings,
               "budget_min": str(row.budget_min) if row.budget_min else None,
               "budget_max": str(row.budget_max) if row.budget_max else None, "kind": row.kind,
               "department": labels["department"].get(row.department_id),
               "department_id": row.department_id, "justification": row.justification}
    row.status = "pending"
    inst = wf.submit(db, v.company_id, "recruitment.requisition", payload,
                     f"Hiring request: {row.title} ({row.openings})", subject_type="requisition", subject_id=row.id)
    row.workflow_instance_id = inst.id
    db.flush()
    return row


def on_requisition_decided(db: Session, instance, status: str) -> None:
    row = db.query(Requisition).filter(Requisition.company_id == instance.company_id,
                                       Requisition.id == (instance.payload or {}).get("requisition_id")).first()
    if row is None or row.status != "pending":
        return
    row.status = {"approved": "approved", "rejected": "rejected", "cancelled": "draft"}[status]
    row.decided_at = c.now()
    row.decision_note = instance.result_note
    if status == "approved":
        c.notify_hr(db, row.company_id, f"Hiring request approved: {row.title}. Create the job opening.", "hiring")


def cancel_requisition(db: Session, v: Viewer, row: Requisition) -> None:
    if row.status in ("filled", "cancelled"):
        raise BadRequest("This request is already closed.")
    if not v.admin and row.requested_by_employee_id != v.employee_id:
        raise Forbidden("You can cancel only your own hiring requests.")
    if row.status == "pending" and row.workflow_instance_id:
        from core.workflow import service as wf
        from core.workflow.models import WorkflowInstance
        inst = db.get(WorkflowInstance, row.workflow_instance_id)
        if inst is not None and inst.status == "pending":
            c.cancel_instance(db, v.company_id, inst, "Hiring request cancelled")
    row.status = "cancelled"


def requisition_out(db: Session, r: Requisition, labels: dict | None = None, names: dict | None = None,
                    jobs: dict | None = None) -> dict:
    labels = labels or c.org_labels(db, r.company_id, departments=[r.department_id], designations=[r.designation_id],
                                    locations=[r.location_id], masters=[r.employment_type_id])
    names = names if names is not None else c.names_for(db, r.company_id, [r.hiring_manager_id, r.replacement_for_id])
    loc = labels["location"].get(r.location_id)
    return {"uuid": str(r.uuid), "id": r.id, "number": r.number, "title": r.title, "openings": r.openings,
            "kind": r.kind, "department_id": r.department_id, "department": labels["department"].get(r.department_id),
            "designation_id": r.designation_id, "designation": labels["designation"].get(r.designation_id),
            "location_id": r.location_id, "location": loc[0] if loc else None,
            "employment_type_id": r.employment_type_id, "employment_type": labels["master"].get(r.employment_type_id),
            "replacement_for_id": r.replacement_for_id, "replacement_for": names.get(r.replacement_for_id),
            "hiring_manager_id": r.hiring_manager_id, "hiring_manager": names.get(r.hiring_manager_id),
            "budget_min": r.budget_min, "budget_max": r.budget_max, "min_experience": r.min_experience,
            "max_experience": r.max_experience, "needed_by": r.needed_by, "justification": r.justification,
            "requested_by_name": r.requested_by_name, "requested_by_employee_id": r.requested_by_employee_id,
            "status": r.status, "status_label": REQ_STATUSES.get(r.status), "decision_note": r.decision_note,
            "decided_at": r.decided_at, "created_at": r.created_at,
            "jobs": (jobs or {}).get(r.id, [])}


# ── Jobs ────────────────────────────────────────────────────────────

def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s or "job")[:80]


def _unique_slug(db: Session, company_id: int, title: str, exclude_id: int | None = None) -> str:
    base = _slugify(title)
    slug, n = base, 1
    while True:
        q = db.query(Job.id).filter(Job.company_id == company_id, Job.slug == slug)
        if exclude_id:
            q = q.filter(Job.id != exclude_id)
        if not q.first():
            return slug
        n += 1
        slug = f"{base}-{n}"


def get_job(db: Session, company_id: int, uuid) -> Job:
    row = db.query(Job).filter(Job.company_id == company_id, Job.uuid == uuid).first()
    if row is None:
        raise NotFound("Job")
    return row


def _clean_list(values, limit=40, size=60) -> list[str]:
    out = []
    for x in values or []:
        s = str(x).strip()[:size]
        if s and s.lower() not in {o.lower() for o in out}:
            out.append(s)
    return out[:limit]


def _clean_questions(items) -> list[dict]:
    out = []
    for i, q in enumerate(items or []):
        label = (q.get("label") or "").strip()
        if not label:
            continue
        typ = q.get("type") or "text"
        if typ not in ("text", "long_text", "yesno", "number", "choice"):
            raise BadRequest("A question is text, long text, yes/no, number or a choice.")
        opts = _clean_list(q.get("options"), 20, 80) if typ == "choice" else []
        if typ == "choice" and len(opts) < 2:
            raise BadRequest(f"Give at least two choices for “{label}”.")
        out.append({"key": q.get("key") or f"q{i + 1}", "label": label[:200], "type": typ,
                    "required": bool(q.get("required")), "options": opts})
    return out[:20]


def _clean_team(db: Session, company_id: int, team) -> list[dict]:
    out, seen = [], set()
    for m in team or []:
        eid = int(m.get("employee_id") or 0)
        if not eid or eid in seen:
            continue
        if c.employee(db, company_id, eid) is None:
            raise BadRequest("A hiring team member was not found.")
        role = m.get("role") or "interviewer"
        if role not in ("recruiter", "interviewer", "coordinator"):
            raise BadRequest("A team member is a recruiter, interviewer or coordinator.")
        seen.add(eid)
        out.append({"employee_id": eid, "role": role})
    return out


def save_job(db: Session, v: Viewer, data: dict, row: Job | None = None) -> Job:
    v.require_admin()
    conf = rsettings.get(db, v.company_id)
    new = row is None
    if new:
        row = Job(company_id=v.company_id, status="draft", created_by_name=c.actor_name(),
                  stages=conf["default_stages"])
        req_uuid = data.get("requisition_uuid")
        if req_uuid:
            req = get_requisition(db, v.company_id, req_uuid)
            if req.status not in ("approved", "filled"):
                raise BadRequest("Only approved hiring requests can become job openings.")
            row.requisition_id = req.id
            for k in ("department_id", "designation_id", "location_id", "employment_type_id", "hiring_manager_id",
                      "min_experience", "max_experience"):
                if data.get(k) in (None, "") and getattr(req, k) is not None:
                    data[k] = getattr(req, k)
            data.setdefault("openings", req.openings)
            data.setdefault("title", req.title)
            if data.get("salary_min") in (None, ""):
                data["salary_min"] = req.budget_min
            if data.get("salary_max") in (None, ""):
                data["salary_max"] = req.budget_max
        db.add(row)
    title = (data.get("title") if "title" in data else row.title) or ""
    if not title.strip():
        raise BadRequest("Give the job a title.")
    row.title = title.strip()[:200]
    if new or data.get("slug"):
        row.slug = _unique_slug(db, v.company_id, data.get("slug") or row.title, row.id)
    for k in ("department_id", "designation_id", "location_id", "employment_type_id", "hiring_manager_id"):
        if k in data:
            setattr(row, k, data[k] or None)
    for k in ("location_text", "summary", "description"):
        if k in data:
            setattr(row, k, (data[k] or "").strip() or None)
    if "work_mode" in data:
        if data["work_mode"] not in WORK_MODES:
            raise BadRequest("Work mode is office, remote or hybrid.")
        row.work_mode = data["work_mode"]
    if "skills" in data:
        row.skills = _clean_list(data["skills"])
    if "nice_skills" in data:
        row.nice_skills = _clean_list(data["nice_skills"])
    for k in ("min_experience", "max_experience"):
        if k in data:
            val = data[k]
            setattr(row, k, float(val) if val not in (None, "") else None)
    if row.min_experience is not None and row.max_experience is not None and row.min_experience > row.max_experience:
        raise BadRequest("Minimum experience can't be more than the maximum.")
    for k in ("salary_min", "salary_max", "referral_bonus"):
        if k in data:
            setattr(row, k, c.money(data[k], "Amount"))
    if row.salary_min and row.salary_max and row.salary_min > row.salary_max:
        raise BadRequest("The minimum salary can't be more than the maximum.")
    if "show_salary" in data:
        row.show_salary = bool(data["show_salary"])
    if "openings" in data:
        row.openings = max(1, min(int(data["openings"] or 1), 500))
    if "closes_on" in data:
        row.closes_on = data["closes_on"] or None
    if "team" in data:
        row.team = _clean_team(db, v.company_id, data["team"])
    if "questions" in data:
        row.questions = _clean_questions(data["questions"])
    if "stages" in data and data["stages"]:
        stages = rsettings.clean_stages(data["stages"])
        if not new:
            keys = {s["key"] for s in stages}
            used = {s for (s,) in db.query(Application.stage).filter(Application.job_id == row.id,
                                                                     Application.status == "active").distinct()}
            if used - keys:
                raise BadRequest("Move candidates out of a stage before removing it.")
        row.stages = stages
    if "screening" in data:
        sc = data["screening"] or {}
        row.screening = {k: (int(sc[k]) if sc.get(k) not in (None, "") else None)
                         for k in ("auto_reject_below", "auto_shortlist_above")}
    db.flush()
    if not row.code:
        from core import numbering
        row.code = numbering.next_number(db, v.company_id, "job", c.today(db, v.company_id))
    return row


def set_job_status(db: Session, v: Viewer, job: Job, action: str) -> Job:
    v.require_admin()
    if action == "publish":
        if job.status in ("closed", "filled"):
            raise BadRequest("Reopen the job before publishing it.")
        if not (job.description or job.summary):
            raise BadRequest("Add a description before publishing.")
        job.status, job.published = "open", True
        job.published_at = job.published_at or c.now()
    elif action == "unpublish":
        job.published = False
    elif action == "hold":
        job.status, job.published = "on_hold", False
    elif action == "open":
        job.status = "open"
    elif action == "close":
        job.status, job.published, job.closed_at = "closed", False, c.now()
    elif action == "reopen":
        job.status, job.closed_at = "open", None
    else:
        raise BadRequest("Unknown action.")
    audit.event(db, f"job.{action}", "jobs", job.id, {"title": job.title}, entity_uuid=str(job.uuid),
                entity_label=job.title)
    return job


def stage_map(job: Job) -> dict[str, dict]:
    return {s["key"]: {**s, "index": i} for i, s in enumerate(job.stages or [])}


def first_stage(job: Job, kind: str = "applied") -> str:
    for s in job.stages or []:
        if s["kind"] == kind:
            return s["key"]
    return (job.stages or [{"key": "applied"}])[0]["key"]


def job_counts(db: Session, company_id: int, job_ids: list[int]) -> dict[int, dict]:
    if not job_ids:
        return {}
    out: dict[int, dict] = {}
    rows = (db.query(Application.job_id, Application.stage, Application.status, func.count(Application.id))
            .filter(Application.company_id == company_id, Application.job_id.in_(job_ids))
            .group_by(Application.job_id, Application.stage, Application.status).all())
    for jid, stage, status, n in rows:
        d = out.setdefault(jid, {"total": 0, "active": 0, "hired": 0, "rejected": 0, "by_stage": {}})
        d["total"] += n
        if status == "active":
            d["active"] += n
            d["by_stage"][stage] = d["by_stage"].get(stage, 0) + n
        elif status == "hired":
            d["hired"] += n
        elif status in ("rejected", "withdrawn"):
            d["rejected"] += n
    return out


def job_out(db: Session, j: Job, labels: dict | None = None, counts: dict | None = None, names: dict | None = None,
            public: bool = False) -> dict:
    labels = labels or c.org_labels(db, j.company_id, departments=[j.department_id], designations=[j.designation_id],
                                    locations=[j.location_id], masters=[j.employment_type_id])
    loc = labels["location"].get(j.location_id)
    location = j.location_text or (loc[1] or loc[0] if loc else None)
    out = {"uuid": str(j.uuid), "code": j.code, "title": j.title, "slug": j.slug, "summary": j.summary,
           "description": j.description, "department": labels["department"].get(j.department_id),
           "department_id": j.department_id, "location_id": j.location_id, "location": location,
           "location_text": j.location_text, "work_mode": j.work_mode, "work_mode_label": WORK_MODES.get(j.work_mode),
           "employment_type_id": j.employment_type_id, "employment_type": labels["master"].get(j.employment_type_id),
           "skills": j.skills or [], "nice_skills": j.nice_skills or [], "min_experience": j.min_experience,
           "max_experience": j.max_experience, "openings": j.openings, "published_at": j.published_at,
           "closes_on": j.closes_on, "questions": j.questions or []}
    if public:
        if j.show_salary:
            out.update(salary_min=j.salary_min, salary_max=j.salary_max)
        return out
    names = names if names is not None else c.names_for(db, j.company_id, [j.hiring_manager_id,
                                                                            *[m["employee_id"] for m in j.team or []]])
    req = db.get(Requisition, j.requisition_id) if j.requisition_id else None
    out.update({
        "designation_id": j.designation_id, "designation": labels["designation"].get(j.designation_id),
        "salary_min": j.salary_min, "salary_max": j.salary_max, "show_salary": j.show_salary,
        "referral_bonus": j.referral_bonus, "hiring_manager_id": j.hiring_manager_id,
        "hiring_manager": names.get(j.hiring_manager_id),
        "team": [{**m, "name": names.get(m["employee_id"])} for m in j.team or []],
        "stages": j.stages or [], "screening": j.screening or {}, "postings": j.postings or {},
        "status": j.status, "status_label": JOB_STATUSES.get(j.status), "published": j.published,
        "requisition_uuid": str(req.uuid) if req else None, "requisition_number": req.number if req else None,
        "created_at": j.created_at, "counts": (counts or {}).get(j.id, {"total": 0, "active": 0, "hired": 0,
                                                                         "rejected": 0, "by_stage": {}}),
    })
    return out


# ── Candidates ──────────────────────────────────────────────────────

def get_candidate(db: Session, company_id: int, uuid) -> Candidate:
    row = db.query(Candidate).filter(Candidate.company_id == company_id, Candidate.uuid == uuid).first()
    if row is None or row.status == "purged":
        raise NotFound("Candidate")
    return row


def _norm_phone(p: str | None) -> str | None:
    if not p:
        return None
    digits = re.sub(r"\D", "", p)
    if len(digits) > 10 and digits.startswith("91"):
        digits = digits[2:]
    digits = digits.lstrip("0")
    return digits[-10:] if len(digits) >= 10 else (digits or None)


def find_duplicates(db: Session, company_id: int, email: str | None, phone: str | None,
                    exclude_id: int | None = None) -> list[Candidate]:
    conds = []
    if email:
        conds.append(func.lower(Candidate.email) == email.strip().lower())
    ph = _norm_phone(phone)
    if ph:
        conds.append(Candidate.phone == ph)
    if not conds:
        return []
    q = db.query(Candidate).filter(Candidate.company_id == company_id, or_(*conds),
                                   Candidate.status.notin_(("merged", "purged")))
    if exclude_id:
        q = q.filter(Candidate.id != exclude_id)
    return q.order_by(Candidate.id).all()


CAND_FIELDS = ("first_name", "last_name", "email", "phone", "location", "current_company", "current_title",
               "total_experience", "current_ctc", "expected_ctc", "notice_days", "education", "linkedin_url",
               "source", "source_detail", "referred_by_employee_id", "notes", "owner_employee_id", "follow_up_on",
               "follow_up_note", "talent_pool")


def _apply_candidate_fields(row: Candidate, data: dict) -> None:
    for k in CAND_FIELDS:
        if k not in data:
            continue
        val = data[k]
        if k in ("current_ctc", "expected_ctc"):
            val = c.money(val, "CTC")
        elif k == "total_experience":
            val = float(val) if val not in (None, "") else None
            if val is not None and not 0 <= val <= 60:
                raise BadRequest("Experience should be between 0 and 60 years.")
        elif k == "notice_days":
            val = int(val) if val not in (None, "") else None
        elif k == "phone":
            val = _norm_phone(val)
        elif k == "email":
            val = (val or "").strip().lower() or None
            if val and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", val):
                raise BadRequest("The email address doesn't look right.")
        elif k == "source":
            if val not in rsettings.SOURCES:
                raise BadRequest("Unknown source.")
        elif k == "talent_pool":
            val = bool(val)
        elif isinstance(val, str):
            val = val.strip() or None
        setattr(row, k, val)
    if "skills" in data:
        row.skills = _clean_list(data["skills"])
    if "tags" in data:
        row.tags = _clean_list(data["tags"], 12, 30)
    if not (row.first_name or "").strip():
        raise BadRequest("Enter the candidate's first name.")
    row.last_name = row.last_name or ""


def _retention_date(db: Session, company_id: int, row: Candidate) -> date:
    conf = rsettings.get(db, company_id)["privacy"]
    days = conf["talent_pool_retention_days"] if row.talent_pool else conf["retention_days"]
    return c.today(db, company_id) + timedelta(days=int(days))


def save_candidate(db: Session, company_id: int, data: dict, row: Candidate | None = None, *,
                   resume_file_id: int | None = None, resume_text: str | None = None,
                   consent: dict | None = None, allow_duplicate: bool = False) -> Candidate:
    new = row is None
    if new:
        dups = find_duplicates(db, company_id, data.get("email"), data.get("phone"))
        if dups and not allow_duplicate:
            d = dups[0]
            raise BadRequest(f"A candidate with this email or phone already exists: {d.first_name} {d.last_name}. "
                             f"Open them instead, or merge later.")
        row = Candidate(company_id=company_id, status="active", source=data.get("source") or "direct")
        db.add(row)
    _apply_candidate_fields(row, data)
    if data.get("custom") is not None:
        # Candidates also arrive from the careers page and job feeds, so required custom
        # fields are not enforced here; recruiters fill them in.
        from core import custom_fields
        row.custom = custom_fields.merge_values(db, company_id, "candidate", data["custom"], row.custom, partial=True)
    if resume_file_id:
        row.resume_file_id = resume_file_id
    if resume_text is not None:
        row.resume_text = resume_text[:60000] or None
    if consent:
        row.consent_at = c.now()
        row.consent_text = consent.get("text")
        row.consent_ip = (consent.get("ip") or "")[:64] or None
    row.purge_after = _retention_date(db, company_id, row)
    db.flush()
    return row


def parse_resume_blob(db: Session, company_id: int, blob, job: Job | None = None) -> dict:
    from modules.documents import storage
    from modules.recruitment import resume
    full = storage.by_id(db, company_id, blob.id, with_data=True)
    text = resume.extract_text(full.filename, full.data)
    fields = resume.parse(text, (job.skills or []) + (job.nice_skills or []) if job else None)
    return {"text": text, "fields": fields}


def merge_candidates(db: Session, company_id: int, keep: Candidate, other: Candidate) -> Candidate:
    """Move the other candidate's applications, interviews and referrals to `keep`, fill
    empty fields from it and mark it merged (never deleted)."""
    if keep.id == other.id:
        raise BadRequest("Pick two different candidates.")
    keep_jobs = {a.job_id for a in keep.applications}
    for a in list(other.applications):
        if a.job_id in keep_jobs:
            a.status = "withdrawn"
            a.rejection_reason = "Merged duplicate"
            _log(a, "merge", f"Merged into {keep.first_name} {keep.last_name}")
        else:
            a.candidate_id = keep.id
    for k in CAND_FIELDS + ("resume_file_id", "resume_text"):
        if getattr(keep, k, None) in (None, "") and getattr(other, k, None) not in (None, ""):
            setattr(keep, k, getattr(other, k))
    keep.skills = _clean_list((keep.skills or []) + (other.skills or []))
    keep.tags = _clean_list((keep.tags or []) + (other.tags or []), 12, 30)
    db.query(Referral).filter(Referral.company_id == company_id, Referral.candidate_id == other.id).update(
        {Referral.candidate_id: keep.id})
    db.query(Offer).filter(Offer.company_id == company_id, Offer.candidate_id == other.id).update(
        {Offer.candidate_id: keep.id})
    other.status, other.merged_into_id = "merged", keep.id
    audit.event(db, "candidate.merged", "candidates", keep.id, {"merged": other.id}, entity_uuid=str(keep.uuid),
                entity_label=f"{keep.first_name} {keep.last_name}")
    db.flush()
    return keep


def candidate_out(db: Session, cand: Candidate, *, detail: bool = False, names: dict | None = None) -> dict:
    out = {"uuid": str(cand.uuid), "id": cand.id, "first_name": cand.first_name, "last_name": cand.last_name,
           "name": f"{cand.first_name} {cand.last_name}".strip(), "email": cand.email, "phone": cand.phone,
           "location": cand.location, "current_company": cand.current_company, "current_title": cand.current_title,
           "total_experience": cand.total_experience, "current_ctc": cand.current_ctc,
           "expected_ctc": cand.expected_ctc, "notice_days": cand.notice_days, "skills": cand.skills or [],
           "education": cand.education, "linkedin_url": cand.linkedin_url, "source": cand.source,
           "source_label": rsettings.SOURCES.get(cand.source), "source_detail": cand.source_detail,
           "tags": cand.tags or [], "talent_pool": cand.talent_pool, "status": cand.status,
           "follow_up_on": cand.follow_up_on, "follow_up_note": cand.follow_up_note,
           "owner_employee_id": cand.owner_employee_id, "referred_by_employee_id": cand.referred_by_employee_id,
           "has_resume": bool(cand.resume_file_id), "consent_at": cand.consent_at, "purge_after": cand.purge_after,
           "created_at": cand.created_at, "custom": cand.custom or {}}
    if detail:
        names = names if names is not None else c.names_for(db, cand.company_id,
                                                            [cand.owner_employee_id, cand.referred_by_employee_id])
        out["owner_name"] = names.get(cand.owner_employee_id)
        out["referred_by_name"] = names.get(cand.referred_by_employee_id)
        out["notes"] = cand.notes
        out["parsed"] = cand.parsed or {}
        out["consent_text"] = cand.consent_text
        if cand.resume_file_id:
            from modules.documents import service as documents
            out["resume"] = documents.file_link(db, cand.company_id, cand.resume_file_id)
        out["resume_text"] = (cand.resume_text or "")[:4000] or None
        out["duplicates"] = [{"uuid": str(d.uuid), "name": f"{d.first_name} {d.last_name}".strip(), "email": d.email,
                              "phone": d.phone}
                             for d in find_duplicates(db, cand.company_id, cand.email, cand.phone, cand.id)]
    return out


# ── Applications and the pipeline ───────────────────────────────────

def get_application(db: Session, company_id: int, uuid) -> Application:
    row = (db.query(Application).options(joinedload(Application.candidate), joinedload(Application.job))
           .filter(Application.company_id == company_id, Application.uuid == uuid).first())
    if row is None or row.candidate.status == "purged":
        raise NotFound("Application")
    return row


def _log(app: Application, kind: str, text: str, **extra) -> None:
    hist = list(app.history or [])
    hist.append(c.event(kind=kind, text=text, **extra))
    app.history = hist[-200:]
    flag_modified(app, "history")


def _job_dict(db: Session, job: Job) -> dict:
    labels = c.org_labels(db, job.company_id, locations=[job.location_id])
    loc = labels["location"].get(job.location_id)
    return {"skills": job.skills or [], "nice_skills": job.nice_skills or [], "min_experience": job.min_experience,
            "max_experience": job.max_experience, "work_mode": job.work_mode,
            "location": job.location_text or (loc[1] or loc[0] if loc else None)}


def _cand_dict(cand: Candidate) -> dict:
    return {"skills": cand.skills or [], "total_experience": cand.total_experience, "location": cand.location,
            "resume_text": cand.resume_text or ""}


def rescore(db: Session, app: Application) -> dict:
    conf = rsettings.get(db, app.company_id)["screening"]
    res = screening.score(_job_dict(db, app.job), _cand_dict(app.candidate), conf.get("weights"),
                          conf.get("scorer") or "rules")
    app.score = res["score"]
    app.score_detail = res
    flag_modified(app, "score_detail")
    return res


def apply(db: Session, company_id: int, job: Job, cand: Candidate, *, source: str | None = None,
          answers: dict | None = None, auto_rules: bool = True) -> Application:
    have = db.query(Application).filter(Application.company_id == company_id, Application.job_id == job.id,
                                        Application.candidate_id == cand.id).first()
    if have is not None:
        if have.status in ("rejected", "withdrawn"):
            raise BadRequest("This candidate has already applied for this job.")
        return have
    if job.status in ("closed", "filled"):
        raise BadRequest("This job is closed.")
    for q in job.questions or []:
        if q.get("required") and (answers or {}).get(q["key"]) in (None, ""):
            raise BadRequest(f"Answer: {q['label']}")
    pos = (db.query(func.coalesce(func.max(Application.position), 0)).filter(Application.job_id == job.id).scalar()
           or 0) + 1
    app = Application(company_id=company_id, job_id=job.id, candidate_id=cand.id, stage=first_stage(job),
                      position=pos, status="active", source=source or cand.source,
                      answers={k: v for k, v in (answers or {}).items() if v not in (None, "")})
    app.job, app.candidate = job, cand
    db.add(app)
    db.flush()
    res = rescore(db, app)
    _log(app, "applied", f"Applied ({rsettings.SOURCES.get(app.source, app.source)}). Screening score {res['score']}.")
    rules = job.screening or {}
    if auto_rules:
        below, above = rules.get("auto_reject_below"), rules.get("auto_shortlist_above")
        if below is not None and res["score"] < below:
            app.status, app.rejection_reason = "rejected", f"Screening score {res['score']} below {below}"
            _log(app, "rejected", app.rejection_reason)
        elif above is not None and res["score"] >= above:
            nxt = next((s["key"] for s in job.stages if s["kind"] == "screening"), None)
            if nxt:
                app.stage = nxt
                _log(app, "moved", f"Shortlisted automatically (score {res['score']} ≥ {above})", to=nxt)
    cand.purge_after = _retention_date(db, company_id, cand)
    db.flush()
    return app


def _reposition(db: Session, app: Application, before_uuid=None, after_uuid=None) -> None:
    """Place a card between two others (fractional positions, like the task board)."""
    def pos_of(u):
        if not u:
            return None
        r = db.query(Application.position).filter(Application.job_id == app.job_id, Application.uuid == u).first()
        return r[0] if r else None
    above, below = pos_of(after_uuid), pos_of(before_uuid)
    if above is not None and below is not None:
        app.position = (above + below) / 2
    elif above is not None:
        app.position = above + 1
    elif below is not None:
        app.position = below - 1
    else:
        app.position = (db.query(func.coalesce(func.max(Application.position), 0))
                        .filter(Application.job_id == app.job_id).scalar() or 0) + 1


def move_stage(db: Session, v: Viewer, app: Application, stage: str, before_uuid=None, after_uuid=None) -> Application:
    if not v.on_team(app.job):
        raise Forbidden("Only the hiring team can move candidates.")
    if app.status != "active":
        raise BadRequest("Only candidates still in the running can be moved. Reopen them first.")
    stages = stage_map(app.job)
    if stage not in stages:
        raise BadRequest("Unknown stage for this job.")
    target = stages[stage]
    if target["kind"] == "hired":
        offer = (db.query(Offer).filter(Offer.application_id == app.id, Offer.status == "accepted").first())
        if offer is None:
            raise BadRequest("Hire through an accepted offer: create and send the offer first.")
    if stage != app.stage:
        old = stages.get(app.stage, {}).get("label", app.stage)
        app.stage = stage
        app.stage_entered_at = c.now()
        _log(app, "moved", f"Moved from {old} to {target['label']}", **{"from": old, "to": target["label"]})
        cand = app.candidate
        cand.purge_after = _retention_date(db, app.company_id, cand)
    _reposition(db, app, before_uuid, after_uuid)
    db.flush()
    return app


def reject(db: Session, v: Viewer, app: Application, reason: str | None, notify_candidate: bool = False) -> Application:
    if not v.on_team(app.job):
        raise Forbidden("Only the hiring team can reject candidates.")
    if app.status != "active":
        raise BadRequest("This candidate is not in the running.")
    if not (reason or "").strip():
        raise BadRequest("Say why the candidate is not moving ahead.")
    app.status, app.rejection_reason = "rejected", reason.strip()[:300]
    _log(app, "rejected", f"Not moving ahead: {app.rejection_reason}")
    if notify_candidate and app.candidate.email:
        from core.email import send_email
        comp = c.company(db, app.company_id)
        send_email(app.candidate.email, f"Your application for {app.job.title}",
                   f"Dear {app.candidate.first_name},\n\nThank you for your interest in {app.job.title} at "
                   f"{comp.company_name if comp else 'our company'}. After careful review we will not be moving ahead "
                   f"with your application at this time. We will keep your details for future roles for the period "
                   f"stated in our privacy notice.\n\nBest wishes,\nHiring team")
    return app


def reopen(db: Session, v: Viewer, app: Application) -> Application:
    if not v.on_team(app.job):
        raise Forbidden("Only the hiring team can do this.")
    if app.status not in ("rejected", "withdrawn"):
        raise BadRequest("Only rejected or withdrawn candidates can be brought back.")
    app.status, app.rejection_reason = "active", None
    _log(app, "reopened", "Back in the running")
    return app


def add_note(db: Session, app: Application, text: str) -> None:
    if not (text or "").strip():
        raise BadRequest("Write a note.")
    _log(app, "note", text.strip()[:2000])


# ── Evaluation from scorecards ──────────────────────────────────────

REC_POINTS = {"strong_yes": 2, "yes": 1, "no": -1, "strong_no": -2}


def evaluate(db: Session, app: Application) -> dict:
    """Summary of every submitted scorecard: average overall rating, average per
    competency, recommendation counts and a computed recommendation."""
    cards = (db.query(Scorecard).filter(Scorecard.company_id == app.company_id, Scorecard.application_id == app.id,
                                        Scorecard.submitted_at.isnot(None)).all())
    if not cards:
        out = {"count": 0, "average": None, "competencies": {}, "recommendations": {}, "recommendation": None,
               "summary": "No scorecards yet."}
    else:
        overall = [s.overall for s in cards if s.overall]
        comp: dict[str, list[int]] = {}
        for s in cards:
            for k, val in (s.ratings or {}).items():
                if isinstance(val, (int, float)) and 1 <= val <= 5:
                    comp.setdefault(k, []).append(val)
        recs: dict[str, int] = {}
        for s in cards:
            if s.recommendation:
                recs[s.recommendation] = recs.get(s.recommendation, 0) + 1
        avg = round(sum(overall) / len(overall), 2) if overall else None
        net = sum(REC_POINTS.get(r, 0) * n for r, n in recs.items())
        vetoed = recs.get("strong_no", 0) > 0
        if avg is None:
            rec = "hire" if net > 0 and not vetoed else "hold"
        elif avg >= 4 and net > 0 and not vetoed:
            rec = "hire"
        elif avg >= 3 and net >= 0 and not vetoed:
            rec = "consider"
        else:
            rec = "reject"
        comps = {k: round(sum(vals) / len(vals), 2) for k, vals in comp.items()}
        best = max(comps, key=comps.get) if comps else None
        worst = min(comps, key=comps.get) if comps else None
        words = {"hire": "Recommend hiring", "consider": "Worth considering", "reject": "Not recommended",
                 "hold": "Needs more input"}
        summary = f"{words[rec]}: {len(cards)} scorecard{'s' if len(cards) != 1 else ''}"
        if avg is not None:
            summary += f", average {avg}/5"
        if best and worst and best != worst:
            summary += f"; strongest in {best.replace('_', ' ')}, weakest in {worst.replace('_', ' ')}"
        if vetoed:
            summary += "; at least one interviewer said strong no"
        out = {"count": len(cards), "average": avg, "competencies": comps, "recommendations": recs,
               "recommendation": rec, "summary": summary + "."}
    app.evaluation = out
    flag_modified(app, "evaluation")
    return out


def override_evaluation(db: Session, v: Viewer, app: Application, recommendation: str | None, note: str | None) -> None:
    v.require_admin()
    if recommendation and recommendation not in ("hire", "consider", "reject", "hold"):
        raise BadRequest("Pick hire, consider, hold or reject.")
    if recommendation and not (note or "").strip():
        raise BadRequest("Say why you are overriding the evaluation.")
    app.evaluation_override = ({"recommendation": recommendation, "note": note.strip()[:500], "by": c.actor_name(),
                                "at": c.now().isoformat()} if recommendation else {})
    _log(app, "evaluation", f"Evaluation set to {recommendation}: {note}" if recommendation else "Override removed")


# ── Interviews ──────────────────────────────────────────────────────

def get_interview(db: Session, company_id: int, uuid) -> Interview:
    row = (db.query(Interview).options(joinedload(Interview.application).joinedload(Application.candidate),
                                       joinedload(Interview.application).joinedload(Application.job))
           .filter(Interview.company_id == company_id, Interview.uuid == uuid).first())
    if row is None:
        raise NotFound("Interview")
    return row


def _aware(dt) -> datetime:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _send_invites(db: Session, iv: Interview, cancel: bool = False) -> int:
    from modules.recruitment import mailer
    conf = rsettings.get(db, iv.company_id)["interviews"]
    if not conf.get("send_invites", True):
        return 0
    app, comp = iv.application, c.company(db, iv.company_id)
    cname = comp.company_name if comp else "Company"
    panel = [c.employee(db, iv.company_id, eid) for eid in iv.panel or []]
    panel = [p for p in panel if p is not None]
    where = iv.meeting_link or iv.location or MODES.get(iv.mode, "")
    summary = f"Interview: {app.candidate.first_name} {app.candidate.last_name} for {app.job.title} ({iv.round_name})"
    attendees = [(f"{app.candidate.first_name} {app.candidate.last_name}", app.candidate.email or "")] + \
                [(c.display_name(p), p.email) for p in panel]
    text = mailer.ics(uid=f"{iv.uuid}@campusly", start=iv.starts_at, end=iv.ends_at, summary=summary,
                      description=f"{iv.round_name} · {MODES.get(iv.mode, '')}\n{iv.meeting_link or ''}\n{iv.notes or ''}",
                      location=where, organizer=(comp.email if comp else ""), attendees=attendees,
                      sequence=iv.sequence, cancel=cancel)
    when = iv.starts_at.astimezone(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    sent = 0
    verb = "cancelled" if cancel else ("updated" if iv.sequence else "scheduled")
    if app.candidate.email:
        mailer.send_with_invite(app.candidate.email, f"{'Cancelled: ' if cancel else ''}Interview with {cname}",
                                f"Dear {app.candidate.first_name},\n\nYour {iv.round_name} for {app.job.title} at {cname} "
                                f"is {verb} for {when}.\n{('Join: ' + iv.meeting_link) if iv.meeting_link else ('Where: ' + where)}\n\n"
                                f"The calendar invite is attached.\n\nHiring team", text)
        sent += 1
    for p in panel:
        mailer.send_with_invite(p.email, f"{'Cancelled: ' if cancel else ''}{summary}",
                                f"Hello {p.first_name},\n\nYou are on the panel for {summary} at {when}. "
                                f"Open Hiring → Interviews in the app for the interview kit and your scorecard.", text)
        c.notify(db, iv.company_id, p.id, f"Interview {verb}: {app.candidate.first_name} {app.candidate.last_name} "
                                          f"({app.job.title}), {when}.", "hiring")
        sent += 1
    return sent


def schedule_interview(db: Session, v: Viewer, app: Application, data: dict, iv: Interview | None = None) -> Interview:
    if not v.on_team(app.job):
        raise Forbidden("Only the hiring team can schedule interviews.")
    if app.status != "active":
        raise BadRequest("This candidate is not in the running.")
    start = _aware(data.get("starts_at") or (iv.starts_at if iv else None))
    if start is None:
        raise BadRequest("Pick the interview date and time.")
    minutes = int(data.get("duration_minutes") or 0) or (
        int((iv.ends_at - iv.starts_at).total_seconds() // 60) if iv else rsettings.get(db, v.company_id)["interviews"]["default_duration_minutes"])
    if not 10 <= minutes <= 480:
        raise BadRequest("An interview lasts between 10 minutes and 8 hours.")
    panel = [int(x) for x in (data.get("panel") if "panel" in data else (iv.panel if iv else [])) or []]
    if not panel:
        raise BadRequest("Add at least one interviewer.")
    for eid in panel:
        if c.employee(db, v.company_id, eid) is None:
            raise BadRequest("An interviewer was not found.")
    mode = data.get("mode") or (iv.mode if iv else "video")
    if mode not in MODES:
        raise BadRequest("Interview mode is video, phone or in person.")
    new = iv is None
    if new:
        if start < c.now() - timedelta(minutes=5):
            raise BadRequest("Pick a time in the future.")
        iv = Interview(company_id=v.company_id, application_id=app.id, created_by_name=c.actor_name(), status="scheduled")
        iv.application = app
        db.add(iv)
    else:
        if iv.status != "scheduled":
            raise BadRequest("Only scheduled interviews can be changed.")
        iv.sequence = (iv.sequence or 0) + 1
        iv.reminded_at = None
    iv.round_name = (data.get("round_name") or (iv.round_name if not new else "") or "Interview").strip()[:120]
    iv.mode, iv.starts_at, iv.ends_at, iv.panel = mode, start, start + timedelta(minutes=minutes), panel
    for k in ("location", "meeting_link", "notes"):
        if k in data:
            setattr(iv, k, (data[k] or "").strip() or None)
    if "kit_uuid" in data:
        kit = (db.query(InterviewKit).filter(InterviewKit.company_id == v.company_id, InterviewKit.uuid == data["kit_uuid"])
               .first() if data.get("kit_uuid") else None)
        iv.kit_id = kit.id if kit else None
    db.flush()
    # one scorecard per panel member
    have = {s.interviewer_employee_id for s in db.query(Scorecard).filter(Scorecard.interview_id == iv.id)}
    names = c.names_for(db, v.company_id, panel)
    for eid in panel:
        if eid not in have:
            db.add(Scorecard(company_id=v.company_id, interview_id=iv.id, application_id=app.id,
                             interviewer_employee_id=eid, interviewer_name=names.get(eid)))
    # move into the interview stage when still earlier
    stages = stage_map(app.job)
    cur = stages.get(app.stage, {"index": 0})
    istage = next((s for s in app.job.stages if s["kind"] == "interview"), None)
    if istage and cur["index"] < stages[istage["key"]]["index"]:
        app.stage, app.stage_entered_at = istage["key"], c.now()
    _log(app, "interview", f"{iv.round_name} {'scheduled' if new else 'rescheduled'} for "
                           f"{iv.starts_at.strftime('%d %b %Y %H:%M UTC')}")
    db.flush()
    db.refresh(iv)
    _send_invites(db, iv)
    return iv


def cancel_interview(db: Session, v: Viewer, iv: Interview, reason: str | None) -> None:
    if not v.on_team(iv.application.job):
        raise Forbidden("Only the hiring team can cancel interviews.")
    if iv.status != "scheduled":
        raise BadRequest("Only scheduled interviews can be cancelled.")
    iv.status = "cancelled"
    iv.sequence = (iv.sequence or 0) + 1
    iv.notes = ((iv.notes or "") + f"\nCancelled: {reason or ''}").strip()
    _log(iv.application, "interview", f"{iv.round_name} cancelled" + (f": {reason}" if reason else ""))
    _send_invites(db, iv, cancel=True)


def set_interview_status(db: Session, v: Viewer, iv: Interview, status: str) -> None:
    if not v.on_team(iv.application.job):
        raise Forbidden("Only the hiring team can do this.")
    if status not in ("completed", "no_show", "scheduled"):
        raise BadRequest("Mark the interview completed or as a no-show.")
    iv.status = status
    _log(iv.application, "interview", f"{iv.round_name} marked {status.replace('_', ' ')}")


def kit_for(db: Session, iv: Interview) -> dict:
    conf = rsettings.get(db, iv.company_id)["interviews"]
    kit = iv.kit
    return {"name": kit.name if kit else "Standard kit",
            "competencies": (kit.competencies if kit and kit.competencies else conf["competencies"]),
            "questions": kit.questions if kit else []}


def submit_scorecard(db: Session, v: Viewer, iv: Interview, data: dict) -> Scorecard:
    if v.employee_id is None and not v.admin:
        raise Forbidden("Only interviewers can fill in scorecards.")
    card = (db.query(Scorecard).filter(Scorecard.interview_id == iv.id,
                                       Scorecard.interviewer_employee_id == v.employee_id).first()
            if v.employee_id else None)
    if card is None:
        if not v.admin:
            raise Forbidden("You are not on this interview panel.")
        card = Scorecard(company_id=v.company_id, interview_id=iv.id, application_id=iv.application_id,
                         interviewer_employee_id=v.employee_id, interviewer_name=c.actor_name())
        db.add(card)
    if iv.status == "cancelled":
        raise BadRequest("This interview was cancelled.")
    kit = kit_for(db, iv)
    keys = {k["key"] for k in kit["competencies"]}
    ratings = {}
    for k, val in (data.get("ratings") or {}).items():
        if k not in keys:
            continue
        if val in (None, ""):
            continue
        val = int(val)
        if not 1 <= val <= 5:
            raise BadRequest("Ratings go from 1 to 5.")
        ratings[k] = val
    overall = data.get("overall")
    if overall in (None, ""):
        raise BadRequest("Give an overall rating.")
    overall = int(overall)
    if not 1 <= overall <= 5:
        raise BadRequest("Ratings go from 1 to 5.")
    rec = data.get("recommendation")
    if rec not in RECOMMENDATIONS:
        raise BadRequest("Say whether you recommend hiring.")
    card.ratings, card.overall, card.recommendation = ratings, overall, rec
    for k in ("strengths", "concerns", "notes"):
        if k in data:
            setattr(card, k, (data[k] or "").strip() or None)
    card.submitted_at = c.now()
    if iv.status == "scheduled" and iv.starts_at <= c.now():
        iv.status = "completed"
    db.flush()
    evaluate(db, iv.application)
    _log(iv.application, "scorecard", f"{card.interviewer_name} submitted a scorecard: {RECOMMENDATIONS[rec]}, {overall}/5")
    return card


def scorecard_out(s: Scorecard) -> dict:
    return {"uuid": str(s.uuid), "interviewer_employee_id": s.interviewer_employee_id,
            "interviewer_name": s.interviewer_name, "ratings": s.ratings or {}, "overall": s.overall,
            "recommendation": s.recommendation, "recommendation_label": RECOMMENDATIONS.get(s.recommendation),
            "strengths": s.strengths, "concerns": s.concerns, "notes": s.notes, "submitted_at": s.submitted_at}


def interview_out(db: Session, iv: Interview, names: dict | None = None, full: bool = False, viewer_id=None) -> dict:
    names = names if names is not None else c.names_for(db, iv.company_id, iv.panel or [])
    app = iv.application
    out = {"uuid": str(iv.uuid), "round_name": iv.round_name, "mode": iv.mode, "mode_label": MODES.get(iv.mode),
           "starts_at": iv.starts_at, "ends_at": iv.ends_at, "location": iv.location, "meeting_link": iv.meeting_link,
           "notes": iv.notes, "status": iv.status,
           "panel": [{"employee_id": e, "name": names.get(e)} for e in iv.panel or []],
           "application_uuid": str(app.uuid), "candidate_uuid": str(app.candidate.uuid),
           "candidate_name": f"{app.candidate.first_name} {app.candidate.last_name}".strip(),
           "job_uuid": str(app.job.uuid), "job_title": app.job.title,
           "scorecards_done": sum(1 for s in iv.scorecards if s.submitted_at),
           "scorecards_total": len(iv.scorecards)}
    if viewer_id:
        mine = next((s for s in iv.scorecards if s.interviewer_employee_id == viewer_id), None)
        out["my_scorecard"] = scorecard_out(mine) if mine else None
    if full:
        out["kit"] = kit_for(db, iv)
        out["kit_uuid"] = str(iv.kit.uuid) if iv.kit else None
        out["scorecards"] = [scorecard_out(s) for s in iv.scorecards]
    return out


def run_interview_reminders(db: Session) -> int:
    """Remind candidates and panels a set number of hours before an interview."""
    n = 0
    now = c.now()
    rows = (db.query(Interview).filter(Interview.status == "scheduled", Interview.reminded_at.is_(None),
                                       Interview.starts_at > now, Interview.starts_at <= now + timedelta(hours=48))
            .all())
    from core.email import send_email
    for iv in rows:
        db.info["company_id"] = iv.company_id
        hours = rsettings.get(db, iv.company_id)["interviews"].get("reminder_hours") or 24
        if iv.starts_at > now + timedelta(hours=int(hours)):
            continue
        app = iv.application
        when = iv.starts_at.strftime("%d %b %Y %H:%M UTC")
        if app.candidate.email:
            send_email(app.candidate.email, f"Reminder: interview for {app.job.title}",
                       f"Dear {app.candidate.first_name},\n\nA reminder of your {iv.round_name} at {when}.\n"
                       + (f"Join: {iv.meeting_link}\n" if iv.meeting_link else (f"Where: {iv.location}\n" if iv.location else "")))
        for eid in iv.panel or []:
            c.notify(db, iv.company_id, eid, f"Reminder: you interview {app.candidate.first_name} {app.candidate.last_name} "
                                             f"for {app.job.title} at {when}.", "hiring")
        iv.reminded_at = now
        n += 1
    return n


# ── Reference checks and background verification ────────────────────

REFERENCE_QUESTIONS = [
    {"key": "capacity", "label": "In what capacity and for how long did you work with the candidate?", "type": "long_text"},
    {"key": "strengths", "label": "What are their main strengths?", "type": "long_text"},
    {"key": "improve", "label": "Where could they improve?", "type": "long_text"},
    {"key": "performance", "label": "How would you rate their overall performance (1-5)?", "type": "rating"},
    {"key": "rehire", "label": "Would you work with them or hire them again?", "type": "yesno"},
    {"key": "other", "label": "Anything else we should know?", "type": "long_text"},
]


def add_reference(db: Session, v: Viewer, app: Application, data: dict, send: bool = True) -> ReferenceCheck:
    if not v.on_team(app.job):
        raise Forbidden("Only the hiring team can do this.")
    name = (data.get("referee_name") or "").strip()
    if not name:
        raise BadRequest("Enter the referee's name.")
    email = (data.get("referee_email") or "").strip().lower() or None
    if send and not email:
        raise BadRequest("Enter the referee's email so the form can be sent.")
    r = ReferenceCheck(company_id=v.company_id, application_id=app.id, referee_name=name[:200], referee_email=email,
                       referee_phone=(data.get("referee_phone") or "").strip() or None,
                       relationship_=(data.get("relationship") or "").strip() or None,
                       referee_company=(data.get("referee_company") or "").strip() or None, status="draft")
    db.add(r)
    db.flush()
    _log(app, "reference", f"Reference added: {name}")
    if send:
        send_reference(db, r, app)
    return r


def send_reference(db: Session, r: ReferenceCheck, app: Application) -> None:
    from core.email import send_email
    if not r.referee_email:
        raise BadRequest("This referee has no email address.")
    r.access_token = r.access_token or c.token()
    r.status, r.sent_at = "sent", c.now()
    r.expires_at = c.now() + timedelta(days=14)
    link = f"{c.frontend_url(db, r.company_id)}/reference/{r.access_token}"
    comp = c.company(db, r.company_id)
    send_email(r.referee_email, f"Reference request for {app.candidate.first_name} {app.candidate.last_name}",
               f"Dear {r.referee_name},\n\n{app.candidate.first_name} {app.candidate.last_name} has applied for "
               f"{app.job.title} at {comp.company_name if comp else 'our company'} and named you as a referee. "
               f"Please share your views using this short form (about 5 minutes): {link}\n\nThank you.")


def reference_by_token(db: Session, token: str) -> ReferenceCheck:
    r = db.query(ReferenceCheck).filter(ReferenceCheck.access_token == token).first()
    if r is None:
        raise NotFound("Reference request")
    return r


def submit_reference(db: Session, r: ReferenceCheck, answers: dict, decline: bool = False) -> None:
    if r.status != "sent":
        raise BadRequest("This form has already been answered or was withdrawn.")
    if r.expires_at and r.expires_at < c.now():
        r.status = "expired"
        raise BadRequest("This link has expired.")
    if decline:
        r.status, r.responded_at = "declined", c.now()
        return
    clean = {}
    for q in REFERENCE_QUESTIONS:
        val = answers.get(q["key"])
        if val in (None, ""):
            continue
        if q["type"] == "rating":
            val = int(val)
            if not 1 <= val <= 5:
                raise BadRequest("Rate from 1 to 5.")
        clean[q["key"]] = str(val)[:3000] if q["type"] != "rating" else val
    if len(clean) < 2:
        raise BadRequest("Please answer at least a couple of questions.")
    r.responses, r.status, r.responded_at = clean, "received", c.now()
    app = db.get(Application, r.application_id)
    if app is not None:
        _log(app, "reference", f"Reference received from {r.referee_name}")
        for eid in c.role_ids(db, r.company_id):
            c.notify(db, r.company_id, eid, f"Reference received for {app.candidate.first_name} "
                                            f"{app.candidate.last_name} from {r.referee_name}.", "hiring")


def reference_out(r: ReferenceCheck) -> dict:
    return {"uuid": str(r.uuid), "referee_name": r.referee_name, "referee_email": r.referee_email,
            "referee_phone": r.referee_phone, "relationship": r.relationship_, "referee_company": r.referee_company,
            "status": r.status, "sent_at": r.sent_at, "responded_at": r.responded_at, "responses": r.responses or {},
            "questions": REFERENCE_QUESTIONS}


def set_bgv(db: Session, v: Viewer, app: Application, data: dict) -> None:
    v.require_admin()
    status = data.get("bgv_status")
    if status not in BGV_STATUSES:
        raise BadRequest("Unknown verification status.")
    app.bgv_status = status
    if "bgv_vendor" in data:
        app.bgv_vendor = (data["bgv_vendor"] or "").strip() or None
    if "bgv_notes" in data:
        app.bgv_notes = (data["bgv_notes"] or "").strip() or None
    if data.get("consent_received"):
        app.bgv_consent_at = c.now()
    _log(app, "bgv", f"Background verification: {BGV_STATUSES[status]}")


# ── Offers ──────────────────────────────────────────────────────────

def get_offer(db: Session, company_id: int, uuid) -> Offer:
    row = (db.query(Offer).options(joinedload(Offer.candidate), joinedload(Offer.job), joinedload(Offer.application))
           .filter(Offer.company_id == company_id, Offer.uuid == uuid).first())
    if row is None:
        raise NotFound("Offer")
    return row


def _offer_log(o: Offer, text: str) -> None:
    hist = list(o.history or [])
    hist.append(c.event(text=text))
    o.history = hist
    flag_modified(o, "history")


def _band(db: Session, job: Job) -> tuple[Decimal | None, Decimal | None]:
    lo, hi = job.salary_min, job.salary_max
    if (lo is None or hi is None) and job.requisition_id:
        req = db.get(Requisition, job.requisition_id)
        if req is not None:
            lo = lo if lo is not None else req.budget_min
            hi = hi if hi is not None else req.budget_max
    return lo, hi


def save_offer(db: Session, v: Viewer, app: Application, data: dict, o: Offer | None = None) -> Offer:
    from modules.recruitment import salary
    if not v.offers:
        raise Forbidden("Only offer administrators can prepare offers.")
    if o is not None and o.status not in ("draft", "rejected", "approved"):
        raise BadRequest("Withdraw the offer before changing it.")
    if o is None:
        if app.status != "active":
            raise BadRequest("Only candidates still in the running can get an offer.")
        live = db.query(Offer).filter(Offer.application_id == app.id,
                                      Offer.status.in_(("draft", "pending_approval", "approved", "sent", "accepted"))).first()
        if live is not None:
            raise BadRequest("This candidate already has an open offer.")
        conf = rsettings.get(db, v.company_id)["offers"]
        job = app.job
        o = Offer(company_id=v.company_id, application_id=app.id, candidate_id=app.candidate_id, job_id=job.id,
                  status="draft", created_by_name=c.actor_name(), designation_id=job.designation_id,
                  department_id=job.department_id, location_id=job.location_id,
                  employment_type_id=job.employment_type_id, reporting_manager_id=job.hiring_manager_id,
                  probation_months=conf["default_probation_months"], notice_days=conf["default_notice_days"])
        o.application, o.job, o.candidate = app, job, app.candidate
        db.add(o)
    for k in ("designation_id", "department_id", "location_id", "employment_type_id", "reporting_manager_id",
              "joining_date", "probation_months", "notice_days"):
        if k in data:
            setattr(o, k, data[k] if data[k] not in ("",) else None)
    if "annual_ctc" in data:
        o.annual_ctc = c.money(data["annual_ctc"], "Annual CTC", allow_none=False)
    if "joining_bonus" in data:
        o.joining_bonus = c.money(data["joining_bonus"], "Joining bonus")
    if "structure_id" in data:
        o.structure_id = data["structure_id"] or None
    if "notes" in data:
        o.notes = (data["notes"] or "").strip() or None
    if not o.joining_date:
        raise BadRequest("Pick the joining date.")
    if not o.annual_ctc:
        raise BadRequest("Enter the annual CTC.")
    if o.joining_date < c.today(db, v.company_id):
        raise BadRequest("The joining date can't be in the past.")
    if o.probation_months is not None and not 0 <= int(o.probation_months) <= 24:
        raise BadRequest("Probation should be between 0 and 24 months.")
    b = salary.breakup(db, v.company_id, o.annual_ctc, o.structure_id, o.joining_date)
    o.breakup = b["lines"]
    o.band_min, o.band_max = _band(db, o.job)
    o.outside_band = bool((o.band_min is not None and o.annual_ctc < o.band_min)
                          or (o.band_max is not None and o.annual_ctc > o.band_max))
    if o.status in ("rejected", "approved"):
        o.status = "draft"
    db.flush()
    if not o.number:
        from core import numbering
        o.number = numbering.next_number(db, v.company_id, "offer", c.today(db, v.company_id))
    _offer_log(o, f"Offer prepared: ₹{o.annual_ctc} a year, joining {o.joining_date.isoformat()}"
               + (" (outside the salary band)" if o.outside_band else ""))
    return o


def submit_offer(db: Session, v: Viewer, o: Offer) -> Offer:
    """Approved straight away inside the band; outside the band it goes for approval."""
    if not v.offers:
        raise Forbidden("Only offer administrators can do this.")
    if o.status != "draft":
        raise BadRequest("Only draft offers can be sent for approval.")
    conf = rsettings.get(db, v.company_id)["offers"]
    if o.outside_band and conf.get("approval_outside_band", True):
        from core.workflow import service as wf
        o.status = "pending_approval"
        payload = {"offer_id": o.id, "candidate": f"{o.candidate.first_name} {o.candidate.last_name}",
                   "job": o.job.title, "annual_ctc": str(o.annual_ctc),
                   "band_min": str(o.band_min) if o.band_min is not None else None,
                   "band_max": str(o.band_max) if o.band_max is not None else None,
                   "joining_date": o.joining_date.isoformat(), "outside_band": True}
        inst = wf.submit(db, v.company_id, "recruitment.offer", payload,
                         f"Offer outside band: {payload['candidate']} for {o.job.title}", subject_type="offer",
                         subject_id=o.id)
        o.workflow_instance_id = inst.id
        _offer_log(o, "Sent for approval (outside the salary band)")
    else:
        o.status = "approved"
        _offer_log(o, "Approved (within the salary band)")
    db.flush()
    return o


def on_offer_decided(db: Session, instance, status: str) -> None:
    o = db.query(Offer).filter(Offer.company_id == instance.company_id,
                               Offer.id == (instance.payload or {}).get("offer_id")).first()
    if o is None or o.status != "pending_approval":
        return
    o.status = {"approved": "approved", "rejected": "rejected", "cancelled": "draft"}[status]
    _offer_log(o, {"approved": "Approved", "rejected": f"Not approved: {instance.result_note or ''}",
                   "cancelled": "Approval withdrawn"}[status])
    c.notify_hr(db, o.company_id, f"Offer for {o.candidate.first_name} {o.candidate.last_name} "
                                  f"{'approved' if status == 'approved' else 'was not approved' if status == 'rejected' else 'is back in draft'}.",
                "hiring")


def offer_values(db: Session, o: Offer) -> dict:
    from modules.recruitment import salary
    labels = c.org_labels(db, o.company_id, departments=[o.department_id], designations=[o.designation_id],
                          locations=[o.location_id], masters=[o.employment_type_id])
    names = c.names_for(db, o.company_id, [o.reporting_manager_id])
    loc = labels["location"].get(o.location_id)
    cand = o.candidate
    vals = {"employee.full_name": f"{cand.first_name} {cand.last_name}".strip(), "employee.first_name": cand.first_name,
            "employee.last_name": cand.last_name, "employee.email": cand.email, "employee.phone": cand.phone,
            "employee.address": cand.location,
            "job.designation": labels["designation"].get(o.designation_id) or o.job.title,
            "job.department": labels["department"].get(o.department_id),
            "job.location": (loc[1] or loc[0]) if loc else (o.job.location_text or ""),
            "job.employment_type": labels["master"].get(o.employment_type_id),
            "job.manager": names.get(o.reporting_manager_id), "job.joining_date": o.joining_date.isoformat(),
            "job.probation_months": str(o.probation_months or 0), "job.notice_period_days": str(o.notice_days or ""),
            "salary.ctc": str(o.annual_ctc)}
    vals.update(salary.letter_values({"lines": o.breakup or [], "monthly_gross": _gross(o)}))
    return {k: v for k, v in vals.items() if v not in (None, "")}


def _gross(o: Offer) -> str | None:
    total = sum((Decimal(str(ln["monthly"])) for ln in (o.breakup or []) if ln.get("kind") in ("earning", "reimbursement")),
                Decimal("0"))
    return str(total) if total else None


def send_offer(db: Session, v: Viewer, o: Offer, message: str | None = None) -> Offer:
    """Generate the offer letter from the template, start e-sign with the candidate and
    email the candidate a link to view, sign and accept or decline."""
    from core.email import send_email
    from modules.documents import service as documents
    if not v.offers:
        raise Forbidden("Only offer administrators can send offers.")
    if o.status != "approved":
        raise BadRequest("Only approved offers can be sent." if o.status != "pending_approval"
                         else "This offer is still waiting for approval.")
    if not o.candidate.email:
        raise BadRequest("Add the candidate's email address first.")
    conf = rsettings.get(db, v.company_id)["offers"]
    template = documents.default_template(db, v.company_id, "offer")
    if template is None:
        raise BadRequest("Add an offer letter template under Letters first.")
    values = offer_values(db, o)
    letter = documents.generate_letter(db, v.company_id, template, None, values, file_to_employee=False)
    o.letter_id = letter.id
    if conf.get("esign", True):
        name = f"{o.candidate.first_name} {o.candidate.last_name}".strip()
        req = documents.send_for_signature(db, v.company_id, letter,
                                           [{"kind": "external", "name": name, "email": o.candidate.email,
                                             "label": "Candidate"}], True, expires_days=int(conf["expiry_days"]) + 7)
        o.esign_request_id = req.id
    o.access_token = c.token()
    o.expires_on = c.today(db, v.company_id) + timedelta(days=int(conf["expiry_days"]))
    o.status, o.sent_at = "sent", c.now()
    comp = c.company(db, v.company_id)
    link = f"{c.frontend_url(db, v.company_id)}/offer/{o.access_token}"
    send_email(o.candidate.email, f"Your offer from {comp.company_name if comp else 'us'}",
               f"Dear {o.candidate.first_name},\n\nWe are delighted to offer you the role of {o.job.title}. "
               f"View your offer letter, sign it and let us know your decision here: {link}\n"
               + (f"\n{message}\n" if message else "")
               + f"\nThis offer is open until {o.expires_on.strftime('%d %b %Y')}.\n\nWarm regards,\nHiring team")
    _offer_log(o, f"Sent to {o.candidate.email}; open until {o.expires_on.isoformat()}")
    _log(o.application, "offer", f"Offer sent ({o.number or ''})")
    stages = stage_map(o.job)
    ostage = next((s for s in o.job.stages if s["kind"] == "offer"), None)
    if ostage and stages.get(o.application.stage, {"index": 0})["index"] < stages[ostage["key"]]["index"]:
        o.application.stage, o.application.stage_entered_at = ostage["key"], c.now()
    db.flush()
    return o


def withdraw_offer(db: Session, v: Viewer, o: Offer, reason: str | None) -> Offer:
    if not v.offers:
        raise Forbidden("Only offer administrators can do this.")
    if o.status in ("accepted", "declined", "expired", "withdrawn"):
        raise BadRequest("This offer is already closed.")
    if not (reason or "").strip():
        raise BadRequest("Say why the offer is withdrawn.")
    _close_letter(db, o)
    if o.status == "pending_approval" and o.workflow_instance_id:
        from core.workflow import service as wf
        from core.workflow.models import WorkflowInstance
        inst = db.get(WorkflowInstance, o.workflow_instance_id)
        if inst is not None and inst.status == "pending":
            c.cancel_instance(db, v.company_id, inst, "Offer withdrawn")
    o.status = "withdrawn"
    _offer_log(o, f"Withdrawn: {reason.strip()}")
    return o


def _close_letter(db: Session, o: Offer) -> None:
    from modules.documents import service as documents
    from modules.documents.models import EsignRequest, Letter
    if o.esign_request_id:
        req = db.get(EsignRequest, o.esign_request_id)
        if req is not None and req.status == "out_for_signature":
            documents.cancel_signature(db, req, "Offer closed")
    if o.letter_id:
        letter = db.get(Letter, o.letter_id)
        if letter is not None and letter.status not in ("signed", "cancelled"):
            letter.status = "cancelled"


def esign_state(db: Session, o: Offer) -> dict:
    from modules.documents.models import EsignRequest
    if not o.esign_request_id:
        return {"required": False, "status": None, "signer_token": None}
    req = db.get(EsignRequest, o.esign_request_id)
    if req is None:
        return {"required": False, "status": None, "signer_token": None}
    signer = next((s for s in req.signers if s.label == "Candidate" or s.employee_id is None), None)
    return {"required": True, "status": req.status, "signed": req.status == "completed",
            "signer_token": signer.access_token if signer else None}


def offer_by_token(db: Session, token: str) -> Offer:
    o = (db.query(Offer).options(joinedload(Offer.candidate), joinedload(Offer.job))
         .filter(Offer.access_token == token).first())
    if o is None:
        raise NotFound("Offer")
    return o


def _check_open_offer(db: Session, o: Offer) -> None:
    if o.status == "sent" and o.expires_on and o.expires_on < c.today(db, o.company_id):
        o.status = "expired"
        _offer_log(o, "Expired")
        db.flush()
    if o.status != "sent":
        raise BadRequest({"accepted": "You have already accepted this offer.", "declined": "You declined this offer.",
                          "expired": "This offer has expired. Please contact us.",
                          "withdrawn": "This offer is no longer available."}.get(o.status, "This offer can't be answered now."))


def accept_offer(db: Session, o: Offer, typed_name: str, ip: str | None = None) -> Offer:
    _check_open_offer(db, o)
    es = esign_state(db, o)
    if es["required"] and not es.get("signed"):
        raise BadRequest("Sign the offer letter first.")
    if not (typed_name or "").strip():
        raise BadRequest("Type your full name to accept.")
    o.status, o.responded_at = "accepted", c.now()
    o.accepted_name, o.accepted_ip = typed_name.strip()[:200], (ip or "")[:64] or None
    _offer_log(o, f"Accepted by {o.accepted_name}")
    _log(o.application, "offer", "Offer accepted")
    audit.event(db, "offer.accepted", "offers", o.id, {"candidate": o.accepted_name}, company_id=o.company_id,
                entity_uuid=str(o.uuid), entity_label=o.number)
    convert_to_employee(db, o)
    c.notify_hr(db, o.company_id, f"{o.candidate.first_name} {o.candidate.last_name} accepted the offer for "
                                  f"{o.job.title}. Pre-boarding has started.", "hiring")
    return o


def decline_offer(db: Session, o: Offer, reason: str | None) -> Offer:
    _check_open_offer(db, o)
    if not (reason or "").strip():
        raise BadRequest("Tell us briefly why you are declining.")
    _close_letter(db, o)
    o.status, o.responded_at, o.decline_reason = "declined", c.now(), reason.strip()[:500]
    _offer_log(o, f"Declined: {o.decline_reason}")
    _log(o.application, "offer", f"Offer declined: {o.decline_reason}")
    c.notify_hr(db, o.company_id, f"{o.candidate.first_name} {o.candidate.last_name} declined the offer for "
                                  f"{o.job.title}: {o.decline_reason}", "hiring")
    return o


def convert_to_employee(db: Session, o: Offer):
    """Accepted offer → pre-boarding employee (no retyping) → onboarding started."""
    from modules.employees import service as people
    if o.employee_id:
        return c.employee(db, o.company_id, o.employee_id)
    cand, app = o.candidate, o.application
    email = (cand.email or "").strip().lower()
    E = c.Employee()
    existing = db.query(E).filter(E.company_id == o.company_id, func.lower(E.email) == email).first() if email else None
    if existing is not None and existing.status in ("exited", "archived"):
        emp = people.rehire(db, o.company_id, existing, {"date_of_joining": o.joining_date, "status": "pre_joining",
                                                          "designation_id": o.designation_id,
                                                          "department_id": o.department_id,
                                                          "location_id": o.location_id,
                                                          "reporting_manager_id": o.reporting_manager_id,
                                                          "reason": f"Rehired through offer {o.number or ''}".strip()})
    elif existing is not None:
        raise BadRequest("An active employee already uses this email address.")
    else:
        data = {"first_name": cand.first_name, "last_name": cand.last_name or "-", "email": email,
                "phone": cand.phone, "status": "pre_joining", "date_of_joining": o.joining_date,
                "designation_id": o.designation_id, "department_id": o.department_id, "location_id": o.location_id,
                "employment_type_id": o.employment_type_id, "reporting_manager_id": o.reporting_manager_id,
                "notice_period_days": o.notice_days, "role": "employee"}
        if o.probation_months:
            data["probation_end_date"] = c.add_months(o.joining_date, int(o.probation_months)) - timedelta(days=1)
        if o.job.work_mode in ("office", "remote", "hybrid"):
            data["work_mode"] = o.job.work_mode
        emp = people.create(db, o.company_id, {k: val for k, val in data.items() if val not in (None, "")})
    o.employee_id = emp.id
    app.status, app.hired_at = "hired", c.now()
    hired_key = next((s["key"] for s in o.job.stages if s["kind"] == "hired"), app.stage)
    app.stage, app.stage_entered_at = hired_key, c.now()
    cand.status = "hired"
    cand.purge_after = None
    _log(app, "hired", f"Hired; pre-boarding record created for {emp.first_name} {emp.last_name}")
    # resume and the signed offer go to the new employee's documents
    try:
        from modules.documents import service as documents
        from modules.documents import storage
        from modules.documents.models import EsignRequest, Letter
        if cand.resume_file_id:
            blob = storage.by_id(db, o.company_id, cand.resume_file_id)
            if blob is not None:
                documents.file_into_employee_documents(db, o.company_id, emp.id, blob, "Resume")
        if o.letter_id:
            letter = db.get(Letter, o.letter_id)
            if letter is not None:
                letter.employee_id = emp.id
                req = db.get(EsignRequest, o.esign_request_id) if o.esign_request_id else None
                blob_id = req.signed_file_id if req is not None and req.signed_file_id else letter.stored_file_id
                blob = storage.by_id(db, o.company_id, blob_id) if blob_id else None
                if blob is not None:
                    documents.file_into_employee_documents(db, o.company_id, emp.id, blob, "Offer letter (signed)"
                                                           if req is not None and req.signed_file_id else "Offer letter")
    except Exception:  # documents are a convenience; never block the hire
        pass
    # referral bonus clock starts at joining
    conf = rsettings.get(db, o.company_id)["referrals"]
    for ref in db.query(Referral).filter(Referral.company_id == o.company_id, Referral.candidate_id == cand.id,
                                         Referral.bonus_status == "in_process").all():
        ref.bonus_status = "pending"
        ref.eligible_on = o.joining_date + timedelta(days=int(conf["qualifying_days"]))
        c.notify(db, o.company_id, ref.referrer_employee_id,
                 f"{cand.first_name} {cand.last_name}, whom you referred, accepted an offer. Your referral bonus "
                 f"becomes due on {ref.eligible_on.strftime('%d %b %Y')} if they are still with us.", "hiring")
    # job filled?
    job = o.job
    hired = db.query(func.count(Application.id)).filter(Application.job_id == job.id, Application.status == "hired").scalar()
    if hired >= (job.openings or 1):
        job.status, job.published, job.closed_at = "filled", False, c.now()
        if job.requisition_id:
            req = db.get(Requisition, job.requisition_id)
            if req is not None and req.status == "approved":
                req.status = "filled"
    db.flush()
    try:
        from modules.onboarding import service as onboarding
        onboarding.start_from_offer(db, o.company_id, emp, o)
    except ImportError:
        pass
    return emp


def offer_out(db: Session, o: Offer, full: bool = False) -> dict:
    labels = c.org_labels(db, o.company_id, departments=[o.department_id], designations=[o.designation_id],
                          locations=[o.location_id], masters=[o.employment_type_id])
    names = c.names_for(db, o.company_id, [o.reporting_manager_id])
    loc = labels["location"].get(o.location_id)
    t = c.today(db, o.company_id)
    out = {"uuid": str(o.uuid), "number": o.number, "status": o.status, "status_label": OFFER_STATUSES.get(o.status),
           "candidate_uuid": str(o.candidate.uuid), "candidate_name": f"{o.candidate.first_name} {o.candidate.last_name}".strip(),
           "candidate_email": o.candidate.email, "application_uuid": str(o.application.uuid),
           "job_uuid": str(o.job.uuid), "job_title": o.job.title,
           "designation_id": o.designation_id, "designation": labels["designation"].get(o.designation_id),
           "department_id": o.department_id, "department": labels["department"].get(o.department_id),
           "location_id": o.location_id, "location": loc[0] if loc else None,
           "employment_type_id": o.employment_type_id, "employment_type": labels["master"].get(o.employment_type_id),
           "reporting_manager_id": o.reporting_manager_id, "reporting_manager": names.get(o.reporting_manager_id),
           "joining_date": o.joining_date, "annual_ctc": o.annual_ctc, "joining_bonus": o.joining_bonus,
           "structure_id": o.structure_id, "breakup": o.breakup or [], "monthly_gross": _gross(o),
           "band_min": o.band_min, "band_max": o.band_max, "outside_band": o.outside_band,
           "probation_months": o.probation_months, "notice_days": o.notice_days, "notes": o.notes,
           "expires_on": o.expires_on, "days_left": (o.expires_on - t).days if o.expires_on and o.status == "sent" else None,
           "sent_at": o.sent_at, "viewed_at": o.viewed_at, "responded_at": o.responded_at,
           "decline_reason": o.decline_reason, "accepted_name": o.accepted_name,
           "employee_id": o.employee_id, "created_at": o.created_at}
    if full:
        out["history"] = o.history or []
        out["esign"] = {k: val for k, val in esign_state(db, o).items() if k != "signer_token"}
        if o.letter_id:
            from modules.documents import service as documents
            from modules.documents.models import Letter
            letter = db.get(Letter, o.letter_id)
            if letter is not None:
                out["letter"] = {"uuid": str(letter.uuid), "status": letter.status,
                                 "file": documents.file_link(db, o.company_id, letter.stored_file_id)}
        if o.access_token and o.status == "sent":
            out["portal_link"] = f"{c.frontend_url(db, o.company_id)}/offer/{o.access_token}"
    return out


def expire_offers(db: Session) -> int:
    n = 0
    for o in db.query(Offer).filter(Offer.status == "sent", Offer.expires_on.isnot(None)).all():
        db.info["company_id"] = o.company_id
        if o.expires_on < c.today(db, o.company_id):
            o.status = "expired"
            _offer_log(o, "Expired without an answer")
            _close_letter(db, o)
            c.notify_hr(db, o.company_id, f"The offer to {o.candidate.first_name} {o.candidate.last_name} expired.", "hiring")
            n += 1
    return n


def offer_for_employee(db: Session, company_id: int, employee_id: int) -> Offer | None:
    return (db.query(Offer).filter(Offer.company_id == company_id, Offer.employee_id == employee_id)
            .order_by(Offer.id.desc()).first())


# ── Referrals ───────────────────────────────────────────────────────

REFERRAL_STATUSES = {"in_process": "In process", "not_eligible": "Not eligible", "pending": "Waiting for joining period",
                     "eligible": "Bonus due", "approved": "Approved for payroll", "paid": "Paid", "cancelled": "Cancelled"}


def refer(db: Session, v: Viewer, job: Job, data: dict, resume_blob=None) -> Referral:
    if not v.employee_id:
        raise BadRequest("Only employees can refer candidates.")
    if job.status != "open":
        raise BadRequest("This job is not open for referrals.")
    data = {**data, "source": "referral", "referred_by_employee_id": v.employee_id}
    dups = find_duplicates(db, v.company_id, data.get("email"), data.get("phone"))
    if dups:
        cand = dups[0]
        if db.query(Referral.id).filter(Referral.company_id == v.company_id, Referral.candidate_id == cand.id).first():
            raise BadRequest("This person has already been referred.")
        if cand.status == "hired":
            raise BadRequest("This person already works here.")
    else:
        text = None
        if resume_blob is not None:
            parsed = parse_resume_blob(db, v.company_id, resume_blob, job)
            text = parsed["text"]
            for k, val in parsed["fields"].items():
                if data.get(k) in (None, "", []):
                    data[k] = val
        cand = save_candidate(db, v.company_id, data, resume_file_id=resume_blob.id if resume_blob else None,
                              resume_text=text)
    app = apply(db, v.company_id, job, cand, source="referral", auto_rules=False)
    conf = rsettings.get(db, v.company_id)["referrals"]
    bonus = job.referral_bonus if job.referral_bonus is not None else c.money(conf.get("default_bonus"))
    ref = Referral(company_id=v.company_id, referrer_employee_id=v.employee_id, candidate_id=cand.id,
                   application_id=app.id, job_id=job.id, note=(data.get("note") or "").strip() or None,
                   bonus_amount=bonus, bonus_status="in_process")
    db.add(ref)
    _log(app, "referral", f"Referred by {c.actor_name()}")
    db.flush()
    c.notify_hr(db, v.company_id, f"New referral for {job.title}: {cand.first_name} {cand.last_name}.", "hiring")
    return ref


def referral_out(db: Session, r: Referral, names: dict | None = None) -> dict:
    cand = db.get(Candidate, r.candidate_id)
    app = db.get(Application, r.application_id) if r.application_id else None
    job = db.get(Job, r.job_id) if r.job_id else None
    names = names if names is not None else c.names_for(db, r.company_id, [r.referrer_employee_id])
    stage = None
    if app is not None:
        st = stage_map(app.job).get(app.stage)
        stage = "Not moving ahead" if app.status in ("rejected", "withdrawn") else ("Hired" if app.status == "hired" else (st or {}).get("label"))
    return {"uuid": str(r.uuid), "referrer_employee_id": r.referrer_employee_id,
            "referrer_name": names.get(r.referrer_employee_id),
            "candidate_name": f"{cand.first_name} {cand.last_name}".strip() if cand and cand.status != "purged" else "Removed",
            "candidate_uuid": str(cand.uuid) if cand else None, "job_title": job.title if job else None,
            "stage": stage, "bonus_amount": r.bonus_amount, "bonus_status": r.bonus_status,
            "bonus_status_label": REFERRAL_STATUSES.get(r.bonus_status), "eligible_on": r.eligible_on,
            "paid_on": r.paid_on, "payment_ref": r.payment_ref, "note": r.note, "created_at": r.created_at}


def set_referral_status(db: Session, v: Viewer, r: Referral, status: str, payment_ref: str | None = None) -> Referral:
    v.require_admin()
    allowed = {"eligible": {"approved", "cancelled", "not_eligible"}, "approved": {"paid", "cancelled"},
               "pending": {"eligible", "cancelled", "not_eligible"}, "in_process": {"cancelled", "not_eligible"}}
    if status not in allowed.get(r.bonus_status, set()):
        raise BadRequest("This change is not possible for the referral now.")
    r.bonus_status = status
    if status == "paid":
        r.paid_on = c.today(db, v.company_id)
        r.payment_ref = (payment_ref or "").strip() or None
    if status in ("approved", "paid"):
        c.notify(db, v.company_id, r.referrer_employee_id,
                 f"Your referral bonus of ₹{r.bonus_amount} is {'approved and will be paid with salary' if status == 'approved' else 'paid'}.",
                 "hiring")
    return r


def run_referral_eligibility(db: Session) -> int:
    n = 0
    for r in db.query(Referral).filter(Referral.bonus_status == "pending", Referral.eligible_on.isnot(None)).all():
        db.info["company_id"] = r.company_id
        if r.eligible_on > c.today(db, r.company_id):
            continue
        cand = db.get(Candidate, r.candidate_id)
        offer = db.query(Offer).filter(Offer.candidate_id == r.candidate_id, Offer.status == "accepted").first()
        emp = c.employee(db, r.company_id, offer.employee_id) if offer and offer.employee_id else None
        if emp is not None and emp.active and emp.status not in ("exited", "archived", "notice"):
            r.bonus_status = "eligible"
            c.notify_hr(db, r.company_id, f"Referral bonus due for referring {cand.first_name} {cand.last_name}.", "hiring")
        else:
            r.bonus_status = "not_eligible"
        n += 1
    return n


# ── Follow-ups and privacy ──────────────────────────────────────────

def run_follow_ups(db: Session) -> int:
    n = 0
    for cand in db.query(Candidate).filter(Candidate.follow_up_on.isnot(None), Candidate.status == "active").all():
        db.info["company_id"] = cand.company_id
        t = c.today(db, cand.company_id)
        if cand.follow_up_on > t or cand.follow_up_sent_on == cand.follow_up_on:
            continue
        msg = f"Follow up with {cand.first_name} {cand.last_name}" + (f": {cand.follow_up_note}" if cand.follow_up_note else ".")
        if cand.owner_employee_id:
            c.notify(db, cand.company_id, cand.owner_employee_id, msg, "hiring")
        else:
            c.notify_hr(db, cand.company_id, msg, "hiring")
        cand.follow_up_sent_on = cand.follow_up_on
        n += 1
    return n


def purge_candidate(db: Session, cand: Candidate, reason: str = "Retention period ended") -> None:
    """Delete personal data (DPDP): names, contact details, CV, answers and notes go;
    the anonymous row stays so hiring reports keep their counts."""
    from modules.documents.models import FileBlob
    blob_id = cand.resume_file_id
    cand.first_name, cand.last_name = "Deleted", "candidate"
    for k in ("email", "phone", "location", "current_company", "current_title", "current_ctc", "expected_ctc",
              "education", "linkedin_url", "notes", "resume_text", "follow_up_note", "consent_ip", "source_detail"):
        setattr(cand, k, None)
    cand.skills, cand.tags, cand.parsed, cand.custom = [], [], {}, {}
    cand.resume_file_id = None
    cand.status, cand.purged_at, cand.purge_after, cand.follow_up_on = "purged", c.now(), None, None
    for app in db.query(Application).filter(Application.candidate_id == cand.id).all():
        app.answers, app.history, app.bgv_notes = {}, [c.event(kind="purged", text=reason)], None
        app.score_detail = {"score": app.score, "scorer": (app.score_detail or {}).get("scorer"), "parts": [],
                            "explanation": "Removed"}
    for s in db.query(Scorecard).filter(Scorecard.application_id.in_(
            db.query(Application.id).filter(Application.candidate_id == cand.id))).all():
        s.strengths = s.concerns = s.notes = None
    for r in db.query(ReferenceCheck).filter(ReferenceCheck.application_id.in_(
            db.query(Application.id).filter(Application.candidate_id == cand.id))).all():
        r.referee_name, r.referee_email, r.referee_phone, r.responses = "Removed", None, None, {}
    db.flush()
    if blob_id:
        from modules.documents.models import DocFile
        in_use = db.query(DocFile.id).filter(DocFile.stored_file_id == blob_id).first()
        blob = db.get(FileBlob, blob_id)
        if blob is not None and not in_use:
            try:
                with db.begin_nested():
                    db.delete(blob)
            except Exception:  # still referenced somewhere: keep the bytes, the link is gone
                pass
    audit.event(db, "candidate.purged", "candidates", cand.id, {"reason": reason}, company_id=cand.company_id,
                entity_uuid=str(cand.uuid), entity_label="Deleted candidate")


def purge_due(db: Session, company_id: int | None = None) -> int:
    """Candidates past their retention date with nothing open (no active application and
    no open offer) are purged. People who joined are never purged here."""
    q = db.query(Candidate).filter(Candidate.status.in_(("active", "archived")), Candidate.purge_after.isnot(None))
    if company_id:
        q = q.filter(Candidate.company_id == company_id)
    n = 0
    for cand in q.all():
        db.info["company_id"] = cand.company_id
        if cand.purge_after > c.today(db, cand.company_id):
            continue
        busy = db.query(Application.id).filter(Application.candidate_id == cand.id, Application.status == "active").first()
        open_offer = db.query(Offer.id).filter(Offer.candidate_id == cand.id,
                                               Offer.status.in_(("draft", "pending_approval", "approved", "sent"))).first()
        if busy or open_offer:
            continue
        purge_candidate(db, cand)
        n += 1
    return n


# ── Reports ─────────────────────────────────────────────────────────

def reports(db: Session, company_id: int, job_uuid=None, start: date | None = None, end: date | None = None) -> dict:
    q = db.query(Application).options(joinedload(Application.job)).filter(Application.company_id == company_id)
    if job_uuid:
        q = q.join(Job, Job.id == Application.job_id).filter(Job.uuid == job_uuid)
    if start:
        q = q.filter(Application.applied_at >= datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc))
    if end:
        q = q.filter(Application.applied_at < datetime.combine(end + timedelta(days=1), datetime.min.time(),
                                                               tzinfo=timezone.utc))
    apps = q.all()
    kinds = ["applied", "screening", "interview", "offer", "hired"]
    funnel = {k: 0 for k in kinds}
    sources: dict[str, dict] = {}
    reached_kind = {}
    interviewed = {i for (i,) in db.query(Interview.application_id).filter(Interview.company_id == company_id,
                                                                          Interview.status != "cancelled").distinct()}
    offered = {i for (i,) in db.query(Offer.application_id).filter(
        Offer.company_id == company_id, Offer.status.in_(("sent", "accepted", "declined", "expired"))).distinct()}
    for a in apps:
        stages = stage_map(a.job)
        idx = stages.get(a.stage, {"index": 0})["index"]
        reached = set()
        for s in a.job.stages[: idx + 1]:
            reached.add(s["kind"])
        if a.id in interviewed:
            reached |= {"screening", "interview"}
        if a.id in offered:
            reached |= {"screening", "interview", "offer"}
        if a.status == "hired":
            reached |= set(kinds)
        reached.add("applied")
        reached_kind[a.id] = reached
        for k in reached:
            if k in funnel:
                funnel[k] += 1
        src = a.source or "direct"
        s = sources.setdefault(src, {"source": src, "label": rsettings.SOURCES.get(src, src), "applicants": 0,
                                     "interviewed": 0, "offered": 0, "hired": 0})
        s["applicants"] += 1
        s["interviewed"] += 1 if "interview" in reached else 0
        s["offered"] += 1 if "offer" in reached else 0
        s["hired"] += 1 if a.status == "hired" else 0
    for s in sources.values():
        s["conversion"] = round(100 * s["hired"] / s["applicants"], 1) if s["applicants"] else 0
        s["interview_rate"] = round(100 * s["interviewed"] / s["applicants"], 1) if s["applicants"] else 0
    hires = [a for a in apps if a.status == "hired" and a.hired_at]
    tth = [(a.hired_at - a.applied_at).days for a in hires]
    oq = db.query(Offer).filter(Offer.company_id == company_id, Offer.status.in_(("sent", "accepted", "declined", "expired")))
    if job_uuid:
        oq = oq.join(Job, Job.id == Offer.job_id).filter(Job.uuid == job_uuid)
    offers = oq.all()
    acc = sum(1 for o in offers if o.status == "accepted")
    dec = sum(1 for o in offers if o.status == "declined")
    exp = sum(1 for o in offers if o.status == "expired")
    decided = acc + dec + exp
    jobs = db.query(Job).filter(Job.company_id == company_id, Job.published_at.isnot(None)).all()
    open_days = []
    for j in jobs:
        end_at = j.closed_at or c.now()
        open_days.append({"uuid": str(j.uuid), "title": j.title, "status": j.status,
                          "days_open": max(0, (end_at - j.published_at).days)})
    decline_reasons: dict[str, int] = {}
    for o in offers:
        if o.status == "declined" and o.decline_reason:
            key = o.decline_reason.strip()[:60]
            decline_reasons[key] = decline_reasons.get(key, 0) + 1
    return {
        "funnel": [{"kind": k, "label": rsettings.STAGE_KINDS[k], "count": funnel[k],
                    "rate": round(100 * funnel[k] / funnel["applied"], 1) if funnel["applied"] else 0} for k in kinds],
        "sources": sorted(sources.values(), key=lambda s: -s["applicants"]),
        "time_to_hire": {"hires": len(hires), "average_days": round(sum(tth) / len(tth), 1) if tth else None,
                         "fastest_days": min(tth) if tth else None, "slowest_days": max(tth) if tth else None},
        "offers": {"sent": len(offers), "accepted": acc, "declined": dec, "expired": exp,
                   "pending": sum(1 for o in offers if o.status == "sent"),
                   "acceptance_rate": round(100 * acc / decided, 1) if decided else None,
                   "decline_reasons": [{"reason": k, "count": v} for k, v in sorted(decline_reasons.items(), key=lambda x: -x[1])]},
        "jobs_open": sorted(open_days, key=lambda x: -x["days_open"])[:20],
        "totals": {"applications": len(apps), "candidates": len({a.candidate_id for a in apps}),
                   "open_jobs": db.query(func.count(Job.id)).filter(Job.company_id == company_id, Job.status == "open").scalar()},
    }


def summary(db: Session, v: Viewer) -> dict:
    cid = v.company_id
    t = c.today(db, cid)
    start = datetime.combine(t, datetime.min.time(), tzinfo=timezone.utc)
    return {
        "open_jobs": db.query(func.count(Job.id)).filter(Job.company_id == cid, Job.status == "open").scalar(),
        "active_candidates": db.query(func.count(Application.id)).filter(Application.company_id == cid,
                                                                         Application.status == "active").scalar(),
        "interviews_this_week": db.query(func.count(Interview.id)).filter(
            Interview.company_id == cid, Interview.status == "scheduled", Interview.starts_at >= start,
            Interview.starts_at < start + timedelta(days=7)).scalar(),
        "offers_out": db.query(func.count(Offer.id)).filter(Offer.company_id == cid, Offer.status == "sent").scalar(),
        "offers_pending_approval": db.query(func.count(Offer.id)).filter(Offer.company_id == cid,
                                                                         Offer.status == "pending_approval").scalar(),
        "requisitions_pending": db.query(func.count(Requisition.id)).filter(Requisition.company_id == cid,
                                                                            Requisition.status == "pending").scalar(),
        "follow_ups_due": db.query(func.count(Candidate.id)).filter(Candidate.company_id == cid,
                                                                    Candidate.status == "active",
                                                                    Candidate.follow_up_on <= t).scalar(),
    }


def candidate_summary(db: Session, company_id: int, candidate_id: int) -> dict:
    cand = db.get(Candidate, candidate_id)
    return candidate_out(db, cand) if cand and cand.company_id == company_id else {}

