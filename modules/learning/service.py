"""Learning rules: courses and lessons, paths, enrolment (self, assigned, by rule,
mandatory with due dates), progress and certificates, trainer-led batches with seats,
waitlist and attendance, the question bank and assessments with attempts, course
requests, and reports.

Admin rights come from `talent.learning.manage`; managers can assign courses to their
reportees; everyone learns for themselves.
"""
from __future__ import annotations

import random
import uuid as uuid_mod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import Depends
from sqlalchemy import Integer, func, or_
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy.orm.attributes import flag_modified

from core import audit, expressions
from core.db import get_db
from core.errors import BadRequest, Forbidden, NotFound
from core.security import _get_role_permissions, get_current_user
from modules.learning import calc
from modules.learning.models import (
    Assessment, Attempt, Batch, BatchSeat, Course, CourseRequest, Enrolment, EnrolmentRule, LearningPath,
    LearningSettings, Lesson, Question,
)

ADMIN = "talent.learning.manage"
OPEN = ("requested", "enrolled", "in_progress")
LESSON_KINDS = ("video", "file", "text", "assessment")
Q_KINDS = ("single", "multi", "true_false", "short")


# ── Who is asking ───────────────────────────────────────────────────

@dataclass
class Actor:
    company_id: int
    employee_id: int | None
    role: str
    name: str
    perms: set = field(default_factory=set)

    @property
    def is_admin(self) -> bool:
        return self.employee_id is None or ADMIN in self.perms

    def require(self) -> None:
        if not self.is_admin:
            raise Forbidden("Only learning administrators can do this.")


def get_actor(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> Actor:
    from core import context
    perms = set(_get_role_permissions(current_user["company_id"], current_user["role"], db))
    return Actor(current_user["company_id"], current_user.get("employee_id"), current_user["role"],
                 context.current().actor_name or "Company owner", perms)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today(db: Session, company_id: int) -> date:
    from core.workflow.service import _today as wf_today
    return wf_today(db, company_id)


def _E():
    from modules.employees.models import Employee
    return Employee


def _name(emp) -> str:
    return f"{emp.first_name} {emp.last_name}".strip() if emp else "Company owner"


def _person(emp) -> dict | None:
    if emp is None:
        return None
    return {"id": emp.id, "uuid": str(emp.uuid), "name": _name(emp), "photo": emp.profile_image_url,
            "department": emp.department.department_name if getattr(emp, "department", None) else None,
            "designation": emp.designation.designation_name if getattr(emp, "designation", None) else None}


def _employee(db: Session, company_id: int, employee_id: int):
    E = _E()
    emp = db.query(E).filter(E.company_id == company_id, E.id == employee_id).first()
    if emp is None:
        raise BadRequest("That employee was not found.")
    return emp


def _reportees(db: Session, company_id: int, manager_id: int | None) -> set[int]:
    if manager_id is None:
        return set()
    E = _E()
    out, frontier = set(), {manager_id}
    while frontier:
        rows = {i for (i,) in db.query(E.id).filter(E.company_id == company_id, E.active.is_(True),
                                                       E.reporting_manager_id.in_(frontier)).all()} - out - {manager_id}
        out |= rows
        frontier = rows
    return out


def _notify(db: Session, company_id: int, employee_id: int | None, message: str) -> None:
    if employee_id is None:
        return
    from modules.notifications.models import Notification
    db.add(Notification(company_id=company_id, employee_id=employee_id, channel="in_app", message_type="learning",
                        message_content=message[:1000], status="sent", sent_at=_now()))


def _get(db: Session, model, company_id: int, uuid, what: str):
    row = db.query(model).filter(model.company_id == company_id, model.uuid == uuid).first()
    if row is None:
        raise NotFound(what)
    return row


# ── Settings ────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "certificate_title": "Certificate of completion",
    "certificate_text": "This is to certify that {employee_name} has successfully completed {course_title} on {completed_on}.",
    "certificate_signatory": "Head of People",
    "reminder_days_before_due": 3,
    "categories_master": "course_category",
}


def config(db: Session, company_id: int) -> dict:
    row = db.query(LearningSettings).filter(LearningSettings.company_id == company_id).first()
    return {**DEFAULT_CONFIG, **((row.config if row else None) or {})}


def save_config(db: Session, actor: Actor, data: dict) -> dict:
    actor.require()
    row = db.query(LearningSettings).filter(LearningSettings.company_id == actor.company_id).first()
    if row is None:
        row = LearningSettings(company_id=actor.company_id, config={})
        db.add(row)
    row.config = {**(row.config or {}), **{k: v for k, v in data.items() if k in DEFAULT_CONFIG}}
    flag_modified(row, "config")
    db.flush()
    return config(db, actor.company_id)


def _category_names(db: Session, company_id: int) -> dict:
    from core.masters import MasterType, MasterValue
    return dict(db.query(MasterValue.id, MasterValue.label).join(MasterType, MasterType.id == MasterValue.type_id)
                .filter(MasterValue.company_id == company_id, MasterType.key == "course_category").all())


# ── Courses ─────────────────────────────────────────────────────────

def lesson_out(l: Lesson, *, locked: bool = False, done: bool = False, full: bool = True) -> dict:
    out = {"id": l.id, "uuid": str(l.uuid), "title": l.title, "kind": l.kind, "duration_minutes": l.duration_minutes,
           "sort_order": l.sort_order, "locked": locked, "done": done, "assessment_id": l.assessment_id}
    if full and not locked:
        out.update({"body": l.body, "url": l.url, "file_name": l.file_name})
    return out


def course_out(db: Session, c: Course, *, enrolment: Enrolment | None = None, stats: dict | None = None,
               categories: dict | None = None, lessons: bool = False) -> dict:
    out = {
        "id": c.id, "uuid": str(c.uuid), "title": c.title, "summary": c.summary, "description": c.description,
        "category_id": c.category_id, "category": (categories or {}).get(c.category_id), "level": c.level,
        "delivery": c.delivery, "cover": c.cover, "cover_image_url": c.cover_image_url,
        "duration_minutes": c.duration_minutes or sum(l.duration_minutes or 0 for l in c.lessons),
        "sequential": c.sequential, "self_enrol": c.self_enrol, "enrol_approval": c.enrol_approval,
        "mandatory": c.mandatory, "due_days": c.due_days, "recert_months": c.recert_months,
        "assessment_id": c.assessment_id, "certificate": c.certificate, "status": c.status, "tags": c.tags or [],
        "skill_ids": c.skill_ids or [], "lesson_count": len(c.lessons), "owner_name": c.owner_name,
        "enrolment": enrolment_brief(enrolment) if enrolment else None,
    }
    if stats is not None:
        out["stats"] = stats
    if lessons:
        done = set((enrolment.lessons_done if enrolment else []) or [])
        ids = [l.id for l in c.lessons]
        open_ids = calc.unlocked(ids, list(done), c.sequential) if enrolment else set(ids)
        out["lessons"] = [lesson_out(l, locked=l.id not in open_ids, done=l.id in done, full=True) for l in c.lessons]
    return out


def enrolment_brief(e: Enrolment) -> dict:
    return {"uuid": str(e.uuid), "status": e.status, "progress": float(e.progress), "due_date": e.due_date.isoformat() if e.due_date else None,
            "mandatory": e.mandatory, "source": e.source, "completed_at": e.completed_at.isoformat() if e.completed_at else None,
            "certificate_no": e.certificate_no, "score": float(e.score) if e.score is not None else None, "passed": e.passed,
            "last_lesson_id": e.last_lesson_id, "expires_on": e.expires_on.isoformat() if e.expires_on else None}


def _course_q(db: Session, company_id: int):
    return db.query(Course).options(selectinload(Course.lessons)).filter(Course.company_id == company_id)


def get_course(db: Session, company_id: int, uuid) -> Course:
    c = _course_q(db, company_id).filter(Course.uuid == uuid).first()
    if c is None:
        raise NotFound("Course")
    return c


def _my_enrolments(db: Session, actor: Actor) -> dict[int, Enrolment]:
    if actor.employee_id is None:
        return {}
    rows = (db.query(Enrolment).filter(Enrolment.company_id == actor.company_id, Enrolment.employee_id == actor.employee_id)
            .order_by(Enrolment.created_at).all())
    out = {}
    for e in rows:
        if e.status in ("cancelled", "rejected"):
            out.setdefault(e.course_id, e)
            continue
        out[e.course_id] = e
    return out


def catalogue(db: Session, actor: Actor, q: str | None = None, category_id: int | None = None, level: str | None = None,
              delivery: str | None = None) -> list[dict]:
    query = _course_q(db, actor.company_id).filter(Course.status == "published")
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Course.title.ilike(like), Course.summary.ilike(like)))
    if category_id:
        query = query.filter(Course.category_id == category_id)
    if level:
        query = query.filter(Course.level == level)
    if delivery:
        query = query.filter(Course.delivery == delivery)
    mine = _my_enrolments(db, actor)
    cats = _category_names(db, actor.company_id)
    counts = dict(db.query(Enrolment.course_id, func.count(Enrolment.id)).filter(
        Enrolment.company_id == actor.company_id, Enrolment.status.notin_(("cancelled", "rejected"))).group_by(Enrolment.course_id).all())
    rows = [c for c in query.order_by(Course.mandatory.desc(), Course.title).all() if c.self_enrol or c.id in mine or actor.is_admin]
    return [course_out(db, c, enrolment=mine.get(c.id), categories=cats, stats={"learners": counts.get(c.id, 0)}) for c in rows]


def admin_courses(db: Session, actor: Actor, status: str | None = None, q: str | None = None) -> list[dict]:
    actor.require()
    query = _course_q(db, actor.company_id)
    if status:
        query = query.filter(Course.status == status)
    if q:
        query = query.filter(Course.title.ilike(f"%{q.strip()}%"))
    stats = {}
    for cid, st, n, avg in (db.query(Enrolment.course_id, Enrolment.status, func.count(Enrolment.id), func.avg(Enrolment.progress))
                            .filter(Enrolment.company_id == actor.company_id).group_by(Enrolment.course_id, Enrolment.status).all()):
        s = stats.setdefault(cid, {"enrolled": 0, "completed": 0, "in_progress": 0, "requested": 0, "overdue": 0})
        if st in ("cancelled", "rejected", "expired"):
            continue
        s["enrolled"] += n
        if st == "completed":
            s["completed"] += n
        elif st == "in_progress":
            s["in_progress"] += n
        elif st == "requested":
            s["requested"] += n
    t = _today(db, actor.company_id)
    for cid, n in (db.query(Enrolment.course_id, func.count(Enrolment.id)).filter(
            Enrolment.company_id == actor.company_id, Enrolment.status.in_(("enrolled", "in_progress")),
            Enrolment.due_date < t).group_by(Enrolment.course_id).all()):
        stats.setdefault(cid, {"enrolled": 0, "completed": 0, "in_progress": 0, "requested": 0, "overdue": 0})["overdue"] = n
    cats = _category_names(db, actor.company_id)
    out = []
    for c in query.order_by(Course.created_at.desc()).all():
        s = stats.get(c.id, {"enrolled": 0, "completed": 0, "in_progress": 0, "requested": 0, "overdue": 0})
        s["completion_pct"] = round(s["completed"] / s["enrolled"] * 100) if s["enrolled"] else 0
        out.append(course_out(db, c, categories=cats, stats=s))
    return out


def course_detail(db: Session, actor: Actor, c: Course) -> dict:
    if c.status != "published" and not actor.is_admin:
        raise NotFound("Course")
    mine = _my_enrolments(db, actor).get(c.id)
    out = course_out(db, c, enrolment=mine, categories=_category_names(db, actor.company_id), lessons=True)
    if not mine or mine.status not in ("enrolled", "in_progress", "completed"):
        if not actor.is_admin:
            for l in out["lessons"]:
                l.pop("body", None), l.pop("url", None)
    out["batches"] = [batch_out(db, actor, b) for b in db.query(Batch).options(selectinload(Batch.seats)).filter(
        Batch.company_id == c.company_id, Batch.course_id == c.id, Batch.status == "scheduled").order_by(Batch.id).all()]
    if c.assessment_id:
        a = db.query(Assessment).filter(Assessment.company_id == c.company_id, Assessment.id == c.assessment_id).first()
        out["assessment"] = assessment_brief(db, actor, a) if a else None
    return out


def _clean_lessons(db: Session, company_id: int, items: list[dict]) -> list[dict]:
    out = []
    for i, it in enumerate(items or []):
        title = (it.get("title") or "").strip()
        if not title:
            raise BadRequest(f"Lesson {i + 1}: add a title.")
        kind = it.get("kind") or "text"
        if kind not in LESSON_KINDS:
            raise BadRequest(f"Lesson {i + 1}: pick video, file, text or quiz.")
        if kind == "video" and not (it.get("url") or "").strip():
            raise BadRequest(f"Lesson {i + 1}: add the video link.")
        if kind == "file" and not (it.get("url") or "").strip():
            raise BadRequest(f"Lesson {i + 1}: attach the file.")
        if kind == "text" and not (it.get("body") or "").strip():
            raise BadRequest(f"Lesson {i + 1}: write the lesson.")
        aid = it.get("assessment_id")
        if kind == "assessment":
            if not aid or not db.query(Assessment.id).filter(Assessment.company_id == company_id, Assessment.id == aid).first():
                raise BadRequest(f"Lesson {i + 1}: pick the quiz.")
        out.append({**it, "title": title[:200], "kind": kind, "assessment_id": aid if kind == "assessment" else None})
    return out


def save_course(db: Session, actor: Actor, data: dict, c: Course | None = None) -> Course:
    actor.require()
    title = (data.get("title") or "").strip()
    if not title:
        raise BadRequest("Give the course a title.")
    if data.get("level") and data["level"] not in ("beginner", "intermediate", "advanced"):
        raise BadRequest("Level is beginner, intermediate or advanced.")
    if data.get("delivery") and data["delivery"] not in ("online", "classroom", "blended"):
        raise BadRequest("Delivery is online, classroom or blended.")
    if c is None:
        c = Course(company_id=actor.company_id, owner_name=actor.name, status="draft")
        db.add(c)
    c.title = title[:200]
    for k in ("summary", "description", "category_id", "cover", "cover_image_url", "due_days", "recert_months", "assessment_id"):
        if k in data:
            setattr(c, k, data[k] if data[k] not in ("",) else None)
    for k in ("level", "delivery"):
        if data.get(k):
            setattr(c, k, data[k])
    for k in ("sequential", "self_enrol", "enrol_approval", "mandatory", "certificate"):
        if k in data and data[k] is not None:
            setattr(c, k, bool(data[k]))
    if "duration_minutes" in data:
        c.duration_minutes = int(data.get("duration_minutes") or 0)
    if "tags" in data:
        c.tags = [str(t).strip()[:40] for t in data.get("tags") or [] if str(t).strip()][:12]
    if "skill_ids" in data:
        c.skill_ids = [int(x) for x in data.get("skill_ids") or []]
    if c.assessment_id and not db.query(Assessment.id).filter(Assessment.company_id == actor.company_id, Assessment.id == c.assessment_id).first():
        raise BadRequest("The final assessment was not found.")
    if data.get("status") in ("draft", "published", "archived"):
        c.status = data["status"]
    db.flush()
    if "lessons" in data and data["lessons"] is not None:
        items = _clean_lessons(db, actor.company_id, data["lessons"])
        existing = {str(l.uuid): l for l in c.lessons}
        keep = set()
        for i, it in enumerate(items):
            l = existing.get(str(it.get("uuid") or ""))
            if l is None:
                l = Lesson(company_id=actor.company_id, course_id=c.id)
                c.lessons.append(l)
            else:
                keep.add(str(l.uuid))
            l.title, l.kind, l.sort_order = it["title"], it["kind"], i
            l.body, l.url, l.file_name = it.get("body"), (it.get("url") or None), it.get("file_name")
            l.assessment_id = it.get("assessment_id")
            l.duration_minutes = int(it.get("duration_minutes") or 5)
        for u, l in existing.items():
            if u not in keep:
                c.lessons.remove(l)
        db.flush()
        _refresh_course_progress(db, c)
    if c.status == "published" and not c.lessons and c.delivery == "online":
        raise BadRequest("Add at least one lesson before publishing an online course.")
    db.flush()
    return c


def _refresh_course_progress(db: Session, c: Course) -> None:
    ids = [l.id for l in c.lessons]
    for e in db.query(Enrolment).filter(Enrolment.company_id == c.company_id, Enrolment.course_id == c.id,
                                        Enrolment.status.in_(("enrolled", "in_progress"))).all():
        e.lessons_done = [i for i in (e.lessons_done or []) if i in ids]
        e.progress = Decimal(str(calc.course_progress(ids, e.lessons_done)))


# ── Enrolment ───────────────────────────────────────────────────────

def _open_enrolment(db: Session, company_id: int, course_id: int, employee_id: int) -> Enrolment | None:
    return db.query(Enrolment).filter(Enrolment.company_id == company_id, Enrolment.course_id == course_id,
                                      Enrolment.employee_id == employee_id,
                                      Enrolment.status.notin_(("cancelled", "expired", "rejected"))).first()


def enrol(db: Session, company_id: int, course: Course, employee_id: int, *, source: str, by: str | None = None,
          due_date: date | None = None, mandatory: bool | None = None, rule_id: int | None = None,
          path_id: int | None = None) -> Enrolment | None:
    """Enrol one person; returns None when they already have an open or valid enrolment."""
    have = _open_enrolment(db, company_id, course.id, employee_id)
    if have is not None:
        if have.status == "completed" and have.expires_on and have.expires_on <= _today(db, company_id):
            have.status = "expired"
            db.flush()
        else:
            if due_date and have.status != "completed" and (have.due_date is None or due_date < have.due_date):
                have.due_date = due_date
            if mandatory and not have.mandatory:
                have.mandatory = True
            return None
    t = _today(db, company_id)
    if due_date is None and course.due_days and (mandatory or course.mandatory or source in ("assigned", "rule", "manager")):
        due_date = t + timedelta(days=course.due_days)
    e = Enrolment(company_id=company_id, course_id=course.id, employee_id=employee_id, source=source, assigned_by_name=by,
                  due_date=due_date, mandatory=bool(course.mandatory if mandatory is None else mandatory),
                  rule_id=rule_id, path_id=path_id, status="enrolled")
    db.add(e)
    db.flush()
    if source != "self":
        _notify(db, company_id, employee_id, f"You have been enrolled in “{course.title}”"
                + (f", due {due_date.strftime('%d %b %Y')}." if due_date else "."))
    return e


def self_enrol(db: Session, actor: Actor, c: Course) -> Enrolment:
    if actor.employee_id is None:
        raise BadRequest("Only employees can enrol.")
    if c.status != "published":
        raise BadRequest("This course isn't open.")
    if not c.self_enrol:
        raise Forbidden("This course is assigned by HR.")
    if _open_enrolment(db, actor.company_id, c.id, actor.employee_id):
        raise BadRequest("You are already enrolled.")
    if c.enrol_approval:
        from core.workflow import service as workflow
        e = Enrolment(company_id=actor.company_id, course_id=c.id, employee_id=actor.employee_id, source="self", status="requested")
        db.add(e)
        db.flush()
        inst = workflow.submit(db, actor.company_id, "learning.enrolment", {"enrolment_id": e.id, "course": c.title,
                                                                            "course_id": c.id}, f"Enrolment: {c.title}",
                               subject_type="learning_enrolments", subject_id=e.id)
        e.workflow_instance_id = inst.id
        db.flush()
        return e
    return enrol(db, actor.company_id, c, actor.employee_id, source="self")


def on_enrolment_decision(db: Session, instance, status: str) -> None:
    e = db.query(Enrolment).filter(Enrolment.company_id == instance.company_id,
                                   Enrolment.id == (instance.payload or {}).get("enrolment_id")).first()
    if e is None or e.status != "requested":
        return
    e.status = {"approved": "enrolled", "rejected": "rejected", "cancelled": "cancelled"}[status]
    db.flush()


def assign(db: Session, actor: Actor, c: Course, employee_ids: list[int], due_date: date | None, mandatory: bool | None) -> int:
    if not actor.is_admin:
        mine = _reportees(db, actor.company_id, actor.employee_id)
        if not employee_ids or any(i not in mine for i in employee_ids):
            raise Forbidden("You can assign courses to your team only.")
    if c.status != "published":
        raise BadRequest("Publish the course first.")
    n = 0
    for eid in dict.fromkeys(employee_ids):
        _employee(db, actor.company_id, eid)
        if enrol(db, actor.company_id, c, eid, source="assigned" if actor.is_admin else "manager", by=actor.name,
                 due_date=due_date, mandatory=mandatory):
            n += 1
    audit.event(db, "learning.assigned", "learning_courses", c.id, {"people": n}, entity_uuid=str(c.uuid), entity_label=c.title)
    return n


def cancel_enrolment(db: Session, actor: Actor, e: Enrolment) -> None:
    if not (actor.is_admin or (actor.employee_id == e.employee_id and e.source == "self" and not e.mandatory)):
        raise Forbidden("Only HR can remove an assigned course.")
    if e.status == "completed":
        raise BadRequest("A completed course stays on the record.")
    e.status = "cancelled"
    for s in db.query(BatchSeat).filter(BatchSeat.company_id == e.company_id, BatchSeat.enrolment_id == e.id,
                                        BatchSeat.status != "cancelled").all():
        _cancel_seat(db, s)
    db.flush()


# ── Rules and paths ─────────────────────────────────────────────────

def _matches(emp, criteria: dict, t: date) -> bool:
    if criteria.get("all"):
        pass
    if criteria.get("department_ids") and emp.department_id not in criteria["department_ids"]:
        return False
    if criteria.get("designation_ids") and emp.designation_id not in criteria["designation_ids"]:
        return False
    cond = (criteria.get("condition") or "").strip()
    if cond:
        try:
            return bool(expressions.check_condition(cond, {"employee": {
                "id": emp.id, "role": emp.role, "department_id": emp.department_id, "designation_id": emp.designation_id,
                "custom": emp.custom or {}}}, today=t))
        except expressions.ExpressionError:
            return False
    return True


def rule_out(db: Session, r: EnrolmentRule) -> dict:
    target = None
    if r.course_id:
        c = db.get(Course, r.course_id)
        target = {"kind": "course", "id": r.course_id, "title": c.title if c else None}
    elif r.path_id:
        p = db.get(LearningPath, r.path_id)
        target = {"kind": "path", "id": r.path_id, "title": p.title if p else None}
    return {"uuid": str(r.uuid), "name": r.name, "course_id": r.course_id, "path_id": r.path_id, "target": target,
            "criteria": r.criteria or {}, "mandatory": r.mandatory, "due_days": r.due_days, "active": r.active,
            "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None, "last_run_count": r.last_run_count}


def save_rule(db: Session, actor: Actor, data: dict, r: EnrolmentRule | None = None) -> EnrolmentRule:
    actor.require()
    name = (data.get("name") or "").strip()
    if not name:
        raise BadRequest("Give the rule a name.")
    if bool(data.get("course_id")) == bool(data.get("path_id")):
        raise BadRequest("Pick a course or a learning path.")
    crit = data.get("criteria") or {}
    crit = {"all": bool(crit.get("all", not (crit.get("department_ids") or crit.get("designation_ids") or crit.get("condition")))),
            "department_ids": [int(x) for x in crit.get("department_ids") or []],
            "designation_ids": [int(x) for x in crit.get("designation_ids") or []],
            "condition": (crit.get("condition") or "").strip()}
    if crit["condition"]:
        try:
            expressions.parse(crit["condition"])
        except expressions.ExpressionError as e:
            raise BadRequest(f"Condition: {e}")
    if r is None:
        r = EnrolmentRule(company_id=actor.company_id)
        db.add(r)
    r.name, r.course_id, r.path_id, r.criteria = name[:200], data.get("course_id") or None, data.get("path_id") or None, crit
    r.mandatory, r.due_days = bool(data.get("mandatory", True)), data.get("due_days") or None
    r.active = bool(data.get("active", True))
    db.flush()
    return r


def run_rule(db: Session, company_id: int, r: EnrolmentRule, by: str = "Rule") -> int:
    if not r.active:
        return 0
    E = _E()
    t = _today(db, company_id)
    due = t + timedelta(days=r.due_days) if r.due_days else None
    emps = [e for e in db.query(E).filter(E.company_id == company_id, E.active.is_(True)).all() if _matches(e, r.criteria or {}, t)]
    courses: list[tuple[Course, int | None]] = []
    if r.course_id:
        c = _course_q(db, company_id).filter(Course.id == r.course_id, Course.status == "published").first()
        if c:
            courses.append((c, None))
    elif r.path_id:
        p = db.query(LearningPath).filter(LearningPath.company_id == company_id, LearningPath.id == r.path_id).first()
        if p:
            for it in p.items or []:
                c = _course_q(db, company_id).filter(Course.id == it.get("course_id"), Course.status == "published").first()
                if c:
                    courses.append((c, p.id))
    n = 0
    for emp in emps:
        for c, pid in courses:
            if enrol(db, company_id, c, emp.id, source="rule", by=by, due_date=due, mandatory=r.mandatory, rule_id=r.id, path_id=pid):
                n += 1
    r.last_run_at, r.last_run_count = _now(), n
    db.flush()
    return n


def run_all_rules(db: Session, company_id: int) -> int:
    return sum(run_rule(db, company_id, r) for r in db.query(EnrolmentRule).filter(
        EnrolmentRule.company_id == company_id, EnrolmentRule.active.is_(True)).all())


def path_out(db: Session, actor: Actor, p: LearningPath, mine: dict | None = None) -> dict:
    ids = [it.get("course_id") for it in p.items or []]
    courses = {c.id: c for c in _course_q(db, p.company_id).filter(Course.id.in_(ids or [0])).all()}
    mine = mine if mine is not None else _my_enrolments(db, actor)
    items = []
    for it in p.items or []:
        c = courses.get(it.get("course_id"))
        if c is None:
            continue
        e = mine.get(c.id)
        items.append({"course_id": c.id, "course_uuid": str(c.uuid), "title": c.title, "required": bool(it.get("required", True)),
                      "duration_minutes": c.duration_minutes or sum(l.duration_minutes or 0 for l in c.lessons),
                      "cover": c.cover, "status": e.status if e else None, "progress": float(e.progress) if e else 0.0})
    req = [i for i in items if i["required"]] or items
    done = sum(1 for i in req if i["status"] == "completed")
    return {"uuid": str(p.uuid), "id": p.id, "title": p.title, "description": p.description, "kind": p.kind,
            "sequential": p.sequential, "due_days": p.due_days, "status": p.status, "items": items,
            "progress": round(sum(i["progress"] for i in req) / len(req)) if req else 0, "completed": done, "required": len(req)}


def save_path(db: Session, actor: Actor, data: dict, p: LearningPath | None = None) -> LearningPath:
    actor.require()
    title = (data.get("title") or "").strip()
    if not title:
        raise BadRequest("Give the path a title.")
    items = []
    for it in data.get("items") or []:
        cid = it.get("course_id")
        if not db.query(Course.id).filter(Course.company_id == actor.company_id, Course.id == cid).first():
            raise BadRequest("A course in the path was not found.")
        items.append({"course_id": int(cid), "required": bool(it.get("required", True))})
    if not items:
        raise BadRequest("Add at least one course.")
    if p is None:
        p = LearningPath(company_id=actor.company_id)
        db.add(p)
    p.title, p.description, p.items = title[:200], data.get("description"), items
    p.kind = data.get("kind") if data.get("kind") in ("plan", "path") else "path"
    p.sequential, p.due_days = bool(data.get("sequential")), data.get("due_days") or None
    if data.get("status") in ("draft", "published", "archived"):
        p.status = data["status"]
    flag_modified(p, "items")
    db.flush()
    return p


def assign_path(db: Session, actor: Actor, p: LearningPath, employee_ids: list[int], due_date: date | None) -> int:
    if not actor.is_admin:
        mine = _reportees(db, actor.company_id, actor.employee_id)
        if any(i not in mine for i in employee_ids):
            raise Forbidden("You can assign learning to your team only.")
    t = _today(db, actor.company_id)
    due = due_date or (t + timedelta(days=p.due_days) if p.due_days else None)
    n = 0
    for it in p.items or []:
        c = _course_q(db, actor.company_id).filter(Course.id == it["course_id"], Course.status == "published").first()
        if c is None:
            continue
        for eid in employee_ids:
            if enrol(db, actor.company_id, c, eid, source="path", by=actor.name, due_date=due, mandatory=it.get("required"), path_id=p.id):
                n += 1
    return n


def self_join_path(db: Session, actor: Actor, p: LearningPath) -> int:
    if actor.employee_id is None:
        raise BadRequest("Only employees can join a path.")
    n = 0
    for it in p.items or []:
        c = _course_q(db, actor.company_id).filter(Course.id == it["course_id"], Course.status == "published").first()
        if c and enrol(db, actor.company_id, c, actor.employee_id, source="path", path_id=p.id, mandatory=False):
            n += 1
    return n


# ── Progress and certificates ───────────────────────────────────────

def my_learning(db: Session, actor: Actor) -> dict:
    if actor.employee_id is None:
        return {"enrolments": [], "paths": [], "stats": {}}
    rows = (db.query(Enrolment).options(joinedload(Enrolment.course).selectinload(Course.lessons))
            .filter(Enrolment.company_id == actor.company_id, Enrolment.employee_id == actor.employee_id,
                    Enrolment.status.notin_(("cancelled", "rejected"))).order_by(Enrolment.created_at.desc()).all())
    t = _today(db, actor.company_id)
    cats = _category_names(db, actor.company_id)
    out = []
    for e in rows:
        d = course_out(db, e.course, enrolment=e, categories=cats)
        d["overdue"] = bool(e.due_date and e.due_date < t and e.status in OPEN)
        d["days_left"] = (e.due_date - t).days if e.due_date else None
        out.append(d)
    path_ids = {e.path_id for e in rows if e.path_id}
    mine = {e.course_id: e for e in rows}
    paths = [path_out(db, actor, p, mine) for p in db.query(LearningPath).filter(LearningPath.company_id == actor.company_id,
                                                                                LearningPath.id.in_(path_ids or {0})).all()]
    minutes = sum((l.duration_minutes or 0) for e in rows if e.status == "completed" for l in e.course.lessons)
    return {"enrolments": out, "paths": paths, "stats": {
        "in_progress": sum(1 for e in rows if e.status in ("enrolled", "in_progress")),
        "completed": sum(1 for e in rows if e.status == "completed"),
        "overdue": sum(1 for d in out if d["overdue"]), "certificates": sum(1 for e in rows if e.certificate_no),
        "hours": round(minutes / 60, 1)}}


def get_enrolment(db: Session, actor: Actor, uuid) -> Enrolment:
    e = (db.query(Enrolment).options(joinedload(Enrolment.course).selectinload(Course.lessons), joinedload(Enrolment.employee))
         .filter(Enrolment.company_id == actor.company_id, Enrolment.uuid == uuid).first())
    if e is None or not (actor.is_admin or e.employee_id == actor.employee_id or e.employee_id in _reportees(db, actor.company_id, actor.employee_id)):
        raise NotFound("Enrolment")
    return e


def _maybe_complete(db: Session, e: Enrolment) -> None:
    c = e.course
    ids = [l.id for l in c.lessons]
    done = set(e.lessons_done or [])
    lessons_ok = set(ids) <= done if ids else c.delivery != "online"
    final_ok = True
    if c.assessment_id:
        final_ok = bool(e.passed)
    if lessons_ok and final_ok and e.status != "completed" and (ids or c.assessment_id):
        e.status, e.completed_at, e.progress = "completed", _now(), Decimal(100)
        t = _today(db, e.company_id)
        if c.recert_months:
            m = t.month - 1 + c.recert_months
            e.expires_on = date(t.year + m // 12, m % 12 + 1, min(t.day, 28))
        if c.certificate:
            from core import numbering
            e.certificate_no = numbering.next_number(db, e.company_id, "certificate", t) or f"CERT-{t.year}-{e.id:05d}"
            e.certificate_issued_at = _now()
        _notify(db, e.company_id, e.employee_id, f"You completed “{c.title}”." + (" Your certificate is ready." if c.certificate else ""))
        audit.event(db, "learning.completed", "learning_enrolments", e.id, {"course": c.title}, entity_uuid=str(e.uuid))


def complete_lesson(db: Session, actor: Actor, e: Enrolment, lesson_uuid) -> Enrolment:
    if e.employee_id != actor.employee_id:
        raise Forbidden("Only the learner can mark lessons done.")
    if e.status not in ("enrolled", "in_progress", "completed"):
        raise BadRequest("You are not enrolled in this course.")
    c = e.course
    lesson = next((l for l in c.lessons if str(l.uuid) == str(lesson_uuid)), None)
    if lesson is None:
        raise BadRequest("That lesson was not found.")
    ids = [l.id for l in c.lessons]
    done = list(e.lessons_done or [])
    if lesson.id not in calc.unlocked(ids, done, c.sequential):
        raise BadRequest("Finish the earlier lessons first.")
    if lesson.kind == "assessment":
        best = _best_attempt(db, e.company_id, lesson.assessment_id, e.employee_id)
        if not best or not best.passed:
            raise BadRequest("Pass the quiz to finish this lesson.")
    if lesson.id not in done:
        done.append(lesson.id)
    e.lessons_done = done
    flag_modified(e, "lessons_done")
    e.last_lesson_id = lesson.id
    if e.status == "enrolled":
        e.status, e.started_at = "in_progress", _now()
    if e.status != "completed":
        e.progress = Decimal(str(calc.course_progress(ids, done)))
    _maybe_complete(db, e)
    db.flush()
    return e


def open_lesson(db: Session, actor: Actor, e: Enrolment, lesson_uuid) -> None:
    """Remember where the learner is; starting a course moves it to in progress."""
    if e.employee_id != actor.employee_id:
        return
    lesson = next((l for l in e.course.lessons if str(l.uuid) == str(lesson_uuid)), None)
    if lesson is None:
        return
    e.last_lesson_id = lesson.id
    if e.status == "enrolled":
        e.status, e.started_at = "in_progress", _now()
    db.flush()


def certificate(db: Session, actor: Actor, e: Enrolment) -> dict:
    if not e.certificate_no:
        raise NotFound("Certificate")
    from modules.company.models import Company
    cfg = config(db, e.company_id)
    company = db.get(Company, e.company_id)
    done = e.completed_at.date() if e.completed_at else None
    text = cfg["certificate_text"]
    for k, v in {"employee_name": _name(e.employee), "course_title": e.course.title,
                 "completed_on": done.strftime("%d %B %Y") if done else ""}.items():
        text = text.replace("{" + k + "}", v)
    return {"certificate_no": e.certificate_no, "title": cfg["certificate_title"], "text": text,
            "employee": _name(e.employee), "course": e.course.title, "completed_on": done.isoformat() if done else None,
            "expires_on": e.expires_on.isoformat() if e.expires_on else None, "signatory": cfg["certificate_signatory"],
            "company": company.company_name if company else "", "score": float(e.score) if e.score is not None else None}


# ── Batches ─────────────────────────────────────────────────────────

def batch_out(db: Session, actor: Actor, b: Batch, roster: bool = False) -> dict:
    seats = [s for s in b.seats if s.status != "cancelled"]
    confirmed = [s for s in seats if s.status == "confirmed"]
    waiting = [s for s in seats if s.status == "waitlisted"]
    mine = next((s for s in seats if s.employee_id == actor.employee_id), None)
    trainer = db.get(_E(), b.trainer_employee_id) if b.trainer_employee_id else None
    out = {"uuid": str(b.uuid), "name": b.name, "course_id": b.course_id, "mode": b.mode, "venue": b.venue,
           "meeting_link": b.meeting_link if (mine or actor.is_admin) else None, "capacity": b.capacity,
           "sessions": b.sessions or [], "status": b.status, "trainer": _name(trainer) if trainer else b.trainer_name,
           "trainer_employee_id": b.trainer_employee_id, "trainer_name": b.trainer_name,
           "confirmed": len(confirmed), "waitlisted": len(waiting), "seats_left": max(b.capacity - len(confirmed), 0),
           "my_seat": {"uuid": str(mine.uuid), "status": mine.status,
                       "position": (waiting.index(mine) + 1) if mine in waiting else None} if mine else None}
    if roster:
        E = _E()
        emps = {e.id: e for e in db.query(E).options(joinedload(E.department)).filter(E.id.in_([s.employee_id for s in seats] or [0])).all()}
        out["seats"] = [{"uuid": str(s.uuid), "employee": _person(emps.get(s.employee_id)), "status": s.status,
                         "attendance": s.attendance or {}} for s in seats]
    return out


def get_batch(db: Session, company_id: int, uuid) -> Batch:
    b = db.query(Batch).options(selectinload(Batch.seats), joinedload(Batch.course)).filter(
        Batch.company_id == company_id, Batch.uuid == uuid).first()
    if b is None:
        raise NotFound("Batch")
    return b


def list_batches(db: Session, actor: Actor, course_id: int | None = None) -> list[dict]:
    q = db.query(Batch).options(selectinload(Batch.seats), joinedload(Batch.course)).filter(Batch.company_id == actor.company_id)
    if course_id:
        q = q.filter(Batch.course_id == course_id)
    rows = q.order_by(Batch.created_at.desc()).all()
    return [{**batch_out(db, actor, b), "course": {"uuid": str(b.course.uuid), "title": b.course.title}} for b in rows]


def save_batch(db: Session, actor: Actor, data: dict, b: Batch | None = None) -> Batch:
    actor.require()
    name = (data.get("name") or "").strip()
    if not name:
        raise BadRequest("Give the batch a name.")
    cap = int(data.get("capacity") or 0)
    if cap < 1 or cap > 1000:
        raise BadRequest("Capacity must be between 1 and 1000.")
    sessions = []
    for i, s in enumerate(data.get("sessions") or []):
        if not s.get("date"):
            raise BadRequest(f"Session {i + 1}: pick a date.")
        sessions.append({"id": s.get("id") or uuid_mod.uuid4().hex[:8], "date": str(s["date"]), "start": s.get("start") or "10:00",
                         "end": s.get("end") or "13:00", "topic": (s.get("topic") or "")[:200]})
    if not sessions:
        raise BadRequest("Add at least one session.")
    if b is None:
        c = db.query(Course).filter(Course.company_id == actor.company_id, Course.id == data.get("course_id")).first()
        if c is None:
            raise BadRequest("Pick the course.")
        b = Batch(company_id=actor.company_id, course_id=c.id)
        db.add(b)
    b.name, b.capacity, b.sessions = name[:200], cap, sorted(sessions, key=lambda s: (s["date"], s["start"]))
    b.mode = data.get("mode") if data.get("mode") in ("classroom", "virtual") else "classroom"
    b.venue, b.meeting_link = data.get("venue"), data.get("meeting_link")
    b.trainer_employee_id, b.trainer_name = data.get("trainer_employee_id") or None, data.get("trainer_name")
    flag_modified(b, "sessions")
    db.flush()
    _fill_from_waitlist(db, b)
    return b


def _fill_from_waitlist(db: Session, b: Batch) -> None:
    db.refresh(b)
    confirmed = [s for s in b.seats if s.status == "confirmed"]
    free = b.capacity - len(confirmed)
    for s in [s for s in b.seats if s.status == "waitlisted"][:max(free, 0)]:
        s.status = "confirmed"
        _notify(db, b.company_id, s.employee_id, f"A seat opened up: you are confirmed for {b.name}.")
    db.flush()


def book_seat(db: Session, actor: Actor, b: Batch, employee_id: int | None = None) -> BatchSeat:
    eid = employee_id or actor.employee_id
    if eid is None:
        raise BadRequest("Pick who the seat is for.")
    if eid != actor.employee_id and not actor.is_admin and eid not in _reportees(db, actor.company_id, actor.employee_id):
        raise Forbidden()
    if b.status != "scheduled":
        raise BadRequest("This batch is not open.")
    c = db.query(Course).options(selectinload(Course.lessons)).filter(Course.id == b.course_id).first()
    e = _open_enrolment(db, actor.company_id, c.id, eid)
    if e is None:
        if eid == actor.employee_id and not c.self_enrol and not actor.is_admin:
            raise Forbidden("This course is assigned by HR.")
        e = enrol(db, actor.company_id, c, eid, source="self" if eid == actor.employee_id else "assigned", by=actor.name)
    other = (db.query(BatchSeat).join(Batch, Batch.id == BatchSeat.batch_id)
             .filter(BatchSeat.company_id == actor.company_id, BatchSeat.employee_id == eid, Batch.course_id == c.id,
                     BatchSeat.status != "cancelled", Batch.status == "scheduled").first())
    if other:
        raise BadRequest("Already booked on a batch of this course. Cancel that seat first.")
    existing = next((s for s in b.seats if s.employee_id == eid), None)
    confirmed = sum(1 for s in b.seats if s.status == "confirmed")
    status = "confirmed" if confirmed < b.capacity else "waitlisted"
    if existing:
        existing.status, existing.enrolment_id = status, e.id if e else existing.enrolment_id
        seat = existing
    else:
        seat = BatchSeat(company_id=actor.company_id, batch_id=b.id, employee_id=eid, enrolment_id=e.id if e else None, status=status)
        b.seats.append(seat)
    db.flush()
    _notify(db, actor.company_id, eid, f"{'Seat confirmed' if status == 'confirmed' else 'You are on the waitlist'} for {b.name}.")
    return seat


def _cancel_seat(db: Session, s: BatchSeat) -> None:
    was = s.status
    s.status = "cancelled"
    db.flush()
    if was == "confirmed":
        _fill_from_waitlist(db, s.batch)


def cancel_seat(db: Session, actor: Actor, b: Batch, seat_uuid) -> None:
    s = next((x for x in b.seats if str(x.uuid) == str(seat_uuid)), None)
    if s is None:
        raise NotFound("Seat")
    if not (actor.is_admin or s.employee_id == actor.employee_id):
        raise Forbidden()
    _cancel_seat(db, s)


def mark_attendance(db: Session, actor: Actor, b: Batch, session_id: str, present_ids: list[str]) -> None:
    if not (actor.is_admin or (actor.employee_id and actor.employee_id == b.trainer_employee_id)):
        raise Forbidden("Only the trainer or HR marks attendance.")
    if session_id not in {s["id"] for s in b.sessions or []}:
        raise BadRequest("That session was not found.")
    present = set(present_ids)
    for s in b.seats:
        if s.status != "confirmed":
            continue
        att = dict(s.attendance or {})
        att[session_id] = "present" if str(s.uuid) in present else "absent"
        s.attendance = att
        flag_modified(s, "attendance")
    db.flush()


def complete_batch(db: Session, actor: Actor, b: Batch, min_attendance_pct: int = 100) -> int:
    """Close the batch; learners who attended enough sessions complete the course."""
    actor.require()
    sessions = [s["id"] for s in b.sessions or []]
    n = 0
    for s in b.seats:
        if s.status != "confirmed" or not s.enrolment_id:
            continue
        att = s.attendance or {}
        pct = (sum(1 for sid in sessions if att.get(sid) == "present") / len(sessions) * 100) if sessions else 0
        e = db.query(Enrolment).options(joinedload(Enrolment.course).selectinload(Course.lessons)).filter(Enrolment.id == s.enrolment_id).first()
        if e and pct >= min_attendance_pct and e.status in OPEN:
            e.lessons_done = [l.id for l in e.course.lessons]
            flag_modified(e, "lessons_done")
            e.status = "in_progress"
            _maybe_complete(db, e)
            if e.status != "completed" and not e.course.lessons and not e.course.assessment_id:
                e.status, e.completed_at, e.progress = "completed", _now(), Decimal(100)
            n += 1
    b.status = "completed"
    db.flush()
    return n


# ── Question bank and assessments ───────────────────────────────────

def question_out(q: Question, reveal: bool = True) -> dict:
    opts = [{"id": o["id"], "text": o["text"], **({"correct": bool(o.get("correct"))} if reveal else {})} for o in q.options or []]
    out = {"id": q.id, "uuid": str(q.uuid), "category": q.category, "kind": q.kind, "text": q.body, "options": opts,
           "marks": float(q.marks), "difficulty": q.difficulty, "active": q.active}
    if reveal:
        out.update({"accepted_answers": q.accepted_answers or [], "explanation": q.explanation})
    return out


def save_question(db: Session, actor: Actor, data: dict, q: Question | None = None) -> Question:
    actor.require()
    text = (data.get("text") or "").strip()
    kind = data.get("kind")
    if not text:
        raise BadRequest("Write the question.")
    if kind not in Q_KINDS:
        raise BadRequest("Pick single choice, multiple choice, true/false or short answer.")
    opts = []
    if kind == "true_false":
        right = data.get("answer") if data.get("answer") is not None else next(
            (o.get("id") for o in data.get("options") or [] if o.get("correct")), "true")
        right = "true" if str(right).lower() in ("true", "t", "1") else "false"
        opts = [{"id": "true", "text": "True", "correct": right == "true"}, {"id": "false", "text": "False", "correct": right == "false"}]
    elif kind in ("single", "multi"):
        for i, o in enumerate(data.get("options") or []):
            t = (o.get("text") or "").strip()
            if t:
                opts.append({"id": o.get("id") or "abcdefghij"[len(opts)], "text": t[:300], "correct": bool(o.get("correct"))})
        if len(opts) < 2:
            raise BadRequest("Add at least two options.")
        n_right = sum(1 for o in opts if o["correct"])
        if kind == "single" and n_right != 1:
            raise BadRequest("Mark exactly one right answer.")
        if kind == "multi" and n_right < 1:
            raise BadRequest("Mark at least one right answer.")
    accepted = []
    if kind == "short":
        accepted = [a.strip()[:200] for a in data.get("accepted_answers") or [] if (a or "").strip()]
        if not accepted:
            raise BadRequest("Add at least one accepted answer.")
    marks = Decimal(str(data.get("marks") or 1))
    if marks <= 0 or marks > 100:
        raise BadRequest("Marks must be between 0 and 100.")
    if q is None:
        q = Question(company_id=actor.company_id)
        db.add(q)
    q.body, q.kind, q.options, q.accepted_answers, q.marks = text[:3000], kind, opts, accepted, marks
    q.category = (data.get("category") or "General").strip()[:100]
    q.difficulty = data.get("difficulty") if data.get("difficulty") in ("easy", "medium", "hard") else "medium"
    q.explanation = data.get("explanation")
    if "active" in data:
        q.active = bool(data["active"])
    db.flush()
    return q


def list_questions(db: Session, actor: Actor, category: str | None = None, q: str | None = None) -> list[dict]:
    actor.require()
    query = db.query(Question).filter(Question.company_id == actor.company_id)
    if category:
        query = query.filter(Question.category == category)
    if q:
        query = query.filter(Question.body.ilike(f"%{q.strip()}%"))
    return [question_out(x) for x in query.order_by(Question.category, Question.id).all()]


def question_categories(db: Session, company_id: int) -> list[dict]:
    return [{"category": c, "count": n} for c, n in db.query(Question.category, func.count(Question.id)).filter(
        Question.company_id == company_id, Question.active.is_(True)).group_by(Question.category).order_by(Question.category).all()]


def assessment_out(a: Assessment) -> dict:
    return {"id": a.id, "uuid": str(a.uuid), "title": a.title, "description": a.description, "selection": a.selection,
            "question_ids": a.question_ids or [], "random_rule": a.random_rule or {}, "time_limit_minutes": a.time_limit_minutes,
            "max_attempts": a.max_attempts, "pass_pct": float(a.pass_pct), "negative_marking": a.negative_marking,
            "negative_value": float(a.negative_value), "shuffle": a.shuffle, "show_answers": a.show_answers,
            "final_score": a.final_score, "status": a.status,
            "question_count": len(a.question_ids or []) if a.selection == "fixed" else int((a.random_rule or {}).get("count") or 0)}


def _attempts(db: Session, company_id: int, assessment_id: int, employee_id: int) -> list[Attempt]:
    return (db.query(Attempt).filter(Attempt.company_id == company_id, Attempt.assessment_id == assessment_id,
                                     Attempt.employee_id == employee_id).order_by(Attempt.number).all())


def _best_attempt(db: Session, company_id: int, assessment_id: int | None, employee_id: int) -> Attempt | None:
    if not assessment_id:
        return None
    a = db.query(Assessment).filter(Assessment.id == assessment_id).first()
    done = [x for x in _attempts(db, company_id, assessment_id, employee_id) if x.status == "submitted"]
    best = calc.final_score([{"number": x.number, "pct": float(x.pct) if x.pct is not None else None, "row": x} for x in done],
                            a.final_score if a else "highest")
    return best["row"] if best else None


def assessment_brief(db: Session, actor: Actor, a: Assessment) -> dict:
    out = assessment_out(a)
    if actor.employee_id:
        atts = _attempts(db, actor.company_id, a.id, actor.employee_id)
        best = _best_attempt(db, actor.company_id, a.id, actor.employee_id)
        out["my_attempts"] = len([x for x in atts if x.status == "submitted"])
        out["in_progress"] = next((str(x.uuid) for x in atts if x.status == "in_progress"), None)
        out["best"] = {"pct": float(best.pct), "passed": best.passed} if best else None
        out["attempts_left"] = (a.max_attempts - out["my_attempts"]) if a.max_attempts else None
    out.pop("question_ids", None)
    return out


def save_assessment(db: Session, actor: Actor, data: dict, a: Assessment | None = None) -> Assessment:
    actor.require()
    title = (data.get("title") or "").strip()
    if not title:
        raise BadRequest("Give the assessment a title.")
    sel = data.get("selection") or "fixed"
    if sel not in ("fixed", "random"):
        raise BadRequest("Questions are fixed or picked at random.")
    qids = [int(x) for x in data.get("question_ids") or []]
    rule = data.get("random_rule") or {}
    if sel == "fixed":
        found = {i for (i,) in db.query(Question.id).filter(Question.company_id == actor.company_id, Question.id.in_(qids or [0])).all()}
        qids = [i for i in qids if i in found]
        if not qids:
            raise BadRequest("Add at least one question.")
    else:
        count = int(rule.get("count") or 0)
        if count < 1:
            raise BadRequest("Say how many questions to pick.")
        pool = _pool(db, actor.company_id, rule)
        if len(pool) < count:
            raise BadRequest(f"The question bank has only {len(pool)} matching questions.")
        rule = {"count": count, "categories": [c for c in rule.get("categories") or [] if c], "difficulty": rule.get("difficulty") or None}
    pass_pct = Decimal(str(data.get("pass_pct") if data.get("pass_pct") is not None else 60))
    if pass_pct < 0 or pass_pct > 100:
        raise BadRequest("Pass mark must be between 0 and 100%.")
    neg = data.get("negative_marking") or "none"
    if neg not in ("none", "fixed", "percent"):
        raise BadRequest("Negative marking is none, fixed marks or a percentage.")
    nval = Decimal(str(data.get("negative_value") or 0))
    if neg == "percent" and (nval < 0 or nval > 100):
        raise BadRequest("Negative marking % must be between 0 and 100.")
    if a is None:
        a = Assessment(company_id=actor.company_id)
        db.add(a)
    a.title, a.description, a.selection, a.question_ids, a.random_rule = title[:200], data.get("description"), sel, qids, rule
    a.time_limit_minutes = data.get("time_limit_minutes") or None
    a.max_attempts = data.get("max_attempts") or None
    a.pass_pct, a.negative_marking, a.negative_value = pass_pct, neg, nval
    a.shuffle = bool(data.get("shuffle", True))
    a.show_answers = data.get("show_answers") if data.get("show_answers") in ("after_submit", "after_pass", "never") else "after_submit"
    a.final_score = data.get("final_score") if data.get("final_score") in ("highest", "latest") else "highest"
    if data.get("status") in ("draft", "published"):
        a.status = data["status"]
    db.flush()
    return a


def _pool(db: Session, company_id: int, rule: dict) -> list[int]:
    q = db.query(Question.id).filter(Question.company_id == company_id, Question.active.is_(True))
    if rule.get("categories"):
        q = q.filter(Question.category.in_(rule["categories"]))
    if rule.get("difficulty"):
        q = q.filter(Question.difficulty == rule["difficulty"])
    return [i for (i,) in q.all()]


def pick_questions(db: Session, a: Assessment, rng: random.Random | None = None) -> list[int]:
    rng = rng or random.SystemRandom()
    if a.selection == "random":
        pool = _pool(db, a.company_id, a.random_rule or {})
        n = int((a.random_rule or {}).get("count") or 0)
        return rng.sample(pool, min(n, len(pool)))
    ids = list(a.question_ids or [])
    if a.shuffle:
        rng.shuffle(ids)
    return ids


def _enrolment_for(db: Session, company_id: int, a: Assessment, employee_id: int) -> Enrolment | None:
    course_ids = {i for (i,) in db.query(Course.id).filter(Course.company_id == company_id, Course.assessment_id == a.id).all()}
    course_ids |= {i for (i,) in db.query(Lesson.course_id).filter(Lesson.company_id == company_id, Lesson.assessment_id == a.id).all()}
    if not course_ids:
        return None
    return (db.query(Enrolment).filter(Enrolment.company_id == company_id, Enrolment.employee_id == employee_id,
                                       Enrolment.course_id.in_(course_ids), Enrolment.status.in_(("enrolled", "in_progress", "completed")))
            .order_by(Enrolment.created_at.desc()).first())


def start_attempt(db: Session, actor: Actor, a: Assessment) -> Attempt:
    if actor.employee_id is None:
        raise BadRequest("Only employees take assessments.")
    if a.status != "published":
        raise BadRequest("This assessment is not open.")
    atts = _attempts(db, actor.company_id, a.id, actor.employee_id)
    running = next((x for x in atts if x.status == "in_progress"), None)
    if running is not None:
        if running.deadline_at and running.deadline_at < _now():
            submit_attempt(db, actor, running)
        else:
            return running
    done = [x for x in atts if x.status == "submitted"]
    if a.max_attempts and len(done) >= a.max_attempts:
        raise BadRequest("You have used all your attempts.")
    enrolment = _enrolment_for(db, actor.company_id, a, actor.employee_id)
    linked = (db.query(Course.id).filter(Course.company_id == actor.company_id, Course.assessment_id == a.id).first()
              or db.query(Lesson.id).filter(Lesson.company_id == actor.company_id, Lesson.assessment_id == a.id).first())
    if enrolment is None and linked:
        raise BadRequest("Enrol in the course first.")
    qids = pick_questions(db, a)
    if not qids:
        raise BadRequest("This assessment has no questions yet.")
    started = _now()
    att = Attempt(company_id=actor.company_id, assessment_id=a.id, employee_id=actor.employee_id,
                  enrolment_id=enrolment.id if enrolment else None, number=len(atts) + 1, question_ids=qids, answers={},
                  started_at=started, deadline_at=started + timedelta(minutes=a.time_limit_minutes) if a.time_limit_minutes else None)
    db.add(att)
    db.flush()
    return att


def get_attempt(db: Session, actor: Actor, uuid) -> Attempt:
    att = db.query(Attempt).options(joinedload(Attempt.assessment)).filter(Attempt.company_id == actor.company_id, Attempt.uuid == uuid).first()
    if att is None or not (att.employee_id == actor.employee_id or actor.is_admin):
        raise NotFound("Attempt")
    return att


def attempt_out(db: Session, actor: Actor, att: Attempt) -> dict:
    a = att.assessment
    qs = {q.id: q for q in db.query(Question).filter(Question.company_id == att.company_id, Question.id.in_(att.question_ids or [0])).all()}
    reveal = att.status == "submitted" and (a.show_answers == "after_submit" or (a.show_answers == "after_pass" and att.passed))
    results = {r["id"]: r for r in att.result or []}
    questions = []
    for qid in att.question_ids or []:
        q = qs.get(qid)
        if q is None:
            continue
        d = question_out(q, reveal=reveal)
        if att.status == "submitted" and qid in results:
            d["result"] = results[qid]
        questions.append(d)
    remaining = None
    if att.deadline_at and att.status == "in_progress":
        remaining = max(int((att.deadline_at - _now()).total_seconds()), 0)
    return {"uuid": str(att.uuid), "assessment": assessment_out(a) | {"question_ids": None}, "number": att.number, "status": att.status,
            "questions": questions, "answers": att.answers or {}, "started_at": att.started_at.isoformat(),
            "deadline_at": att.deadline_at.isoformat() if att.deadline_at else None, "seconds_left": remaining,
            "submitted_at": att.submitted_at.isoformat() if att.submitted_at else None,
            "score": float(att.score) if att.score is not None else None, "max_score": float(att.max_score) if att.max_score is not None else None,
            "pct": float(att.pct) if att.pct is not None else None, "passed": att.passed, "reveal": reveal,
            "summary": {"correct": sum(1 for r in att.result or [] if r["correct"]),
                        "wrong": sum(1 for r in att.result or [] if r["answered"] and not r["correct"]),
                        "unanswered": sum(1 for r in att.result or [] if not r["answered"])} if att.status == "submitted" else None}


def save_answers(db: Session, actor: Actor, att: Attempt, answers: dict) -> None:
    if att.employee_id != actor.employee_id or att.status != "in_progress":
        raise BadRequest("This attempt is closed.")
    if att.deadline_at and att.deadline_at + timedelta(seconds=15) < _now():
        raise BadRequest("Time is up.")
    allowed = {str(i) for i in att.question_ids or []}
    att.answers = {**(att.answers or {}), **{k: v for k, v in (answers or {}).items() if k in allowed}}
    flag_modified(att, "answers")
    db.flush()


def submit_attempt(db: Session, actor: Actor, att: Attempt, answers: dict | None = None) -> Attempt:
    if att.status != "in_progress":
        raise BadRequest("This attempt is already submitted.")
    if att.employee_id != actor.employee_id:
        raise Forbidden()
    late = att.deadline_at and att.deadline_at + timedelta(seconds=15) < _now()
    if answers and not late:
        allowed = {str(i) for i in att.question_ids or []}
        att.answers = {**(att.answers or {}), **{k: v for k, v in answers.items() if k in allowed}}
    a = att.assessment
    qs = {q.id: q for q in db.query(Question).filter(Question.company_id == att.company_id, Question.id.in_(att.question_ids or [0])).all()}
    qdicts = [{"id": str(qid), "kind": qs[qid].kind, "options": qs[qid].options, "accepted_answers": qs[qid].accepted_answers,
               "marks": float(qs[qid].marks)} for qid in att.question_ids or [] if qid in qs]
    res = calc.score_attempt(qdicts, att.answers or {}, a.negative_marking, a.negative_value, a.pass_pct)
    for r in res["results"]:
        r["id"] = int(r["id"])
    att.result = res["results"]
    att.score, att.max_score, att.pct, att.passed = res["score"], res["max_score"], res["pct"], res["passed"]
    att.status, att.submitted_at = "submitted", _now()
    flag_modified(att, "answers")
    db.flush()
    e = db.query(Enrolment).options(joinedload(Enrolment.course).selectinload(Course.lessons)).filter(
        Enrolment.id == att.enrolment_id).first() if att.enrolment_id else None
    if e is not None and e.status in ("enrolled", "in_progress", "completed"):
        best = _best_attempt(db, att.company_id, a.id, att.employee_id)
        if e.course.assessment_id == a.id and best is not None:
            e.score, e.passed = best.pct, best.passed
        if e.status == "enrolled":
            e.status, e.started_at = "in_progress", _now()
        if att.passed:
            ids = [l.id for l in e.course.lessons]
            done = list(e.lessons_done or [])
            for l in e.course.lessons:
                if l.kind == "assessment" and l.assessment_id == a.id and l.id not in done:
                    done.append(l.id)
            e.lessons_done = done
            flag_modified(e, "lessons_done")
            if e.status != "completed":
                e.progress = Decimal(str(calc.course_progress(ids, done)))
        _maybe_complete(db, e)
        db.flush()
    audit.event(db, "learning.assessment_submitted", "learning_attempts", att.id,
                {"assessment": a.title, "pct": res["pct"], "passed": res["passed"]}, entity_uuid=str(att.uuid))
    return att


# ── Course requests ─────────────────────────────────────────────────

def request_out(r: CourseRequest) -> dict:
    return {"uuid": str(r.uuid), "title": r.title, "reason": r.reason, "link": r.link, "provider": r.provider,
            "estimated_cost": float(r.estimated_cost) if r.estimated_cost is not None else None, "status": r.status,
            "requester": r.requester_name, "course_id": r.course_id, "decision_note": r.decision_note,
            "created_at": r.created_at.isoformat()}


def create_request(db: Session, actor: Actor, data: dict) -> CourseRequest:
    title = (data.get("title") or "").strip()
    if not title:
        raise BadRequest("Say which course or training you need.")
    from core.workflow import service as workflow
    r = CourseRequest(company_id=actor.company_id, employee_id=actor.employee_id, requester_name=actor.name, title=title[:200],
                      reason=data.get("reason"), link=data.get("link"), provider=data.get("provider"),
                      estimated_cost=Decimal(str(data["estimated_cost"])) if data.get("estimated_cost") not in (None, "") else None)
    db.add(r)
    db.flush()
    inst = workflow.submit(db, actor.company_id, "learning.course_request",
                           {"request_id": r.id, "title": r.title, "reason": r.reason, "provider": r.provider, "link": r.link,
                            "estimated_cost": float(r.estimated_cost) if r.estimated_cost is not None else 0},
                           f"Course request: {r.title}", subject_type="learning_course_requests", subject_id=r.id)
    r.workflow_instance_id = inst.id
    if inst.status == "approved":
        r.status = "approved"
    db.flush()
    return r


def on_request_decision(db: Session, instance, status: str) -> None:
    r = db.query(CourseRequest).filter(CourseRequest.company_id == instance.company_id,
                                       CourseRequest.id == (instance.payload or {}).get("request_id")).first()
    if r is None or r.status != "pending":
        return
    r.status = {"approved": "approved", "rejected": "rejected", "cancelled": "cancelled"}[status]
    r.decision_note = instance.result_note
    db.flush()


def list_requests(db: Session, actor: Actor, mine: bool) -> list[dict]:
    q = db.query(CourseRequest).filter(CourseRequest.company_id == actor.company_id)
    if mine or not actor.is_admin:
        q = q.filter(CourseRequest.employee_id == actor.employee_id) if actor.employee_id else q.filter(CourseRequest.employee_id.is_(None))
    return [request_out(r) for r in q.order_by(CourseRequest.created_at.desc()).all()]


def fulfil_request(db: Session, actor: Actor, r: CourseRequest, course_id: int) -> None:
    actor.require()
    if r.status not in ("approved", "pending"):
        raise BadRequest("Only an approved request can be fulfilled.")
    c = _course_q(db, actor.company_id).filter(Course.id == course_id).first()
    if c is None:
        raise BadRequest("Pick the course.")
    r.course_id, r.status = c.id, "fulfilled"
    if r.employee_id and c.status == "published":
        enrol(db, actor.company_id, c, r.employee_id, source="assigned", by=actor.name)
    db.flush()


# ── Reports ─────────────────────────────────────────────────────────

def enrolments_report(db: Session, actor: Actor, course_id: int | None = None, status: str | None = None,
                      department_id: int | None = None, overdue: bool = False, page: int = 1, size: int = 25) -> dict:
    E = _E()
    if not actor.is_admin:
        ids = _reportees(db, actor.company_id, actor.employee_id)
        if not ids:
            raise Forbidden()
    q = (db.query(Enrolment).options(joinedload(Enrolment.course), joinedload(Enrolment.employee).joinedload(E.department))
         .join(E, E.id == Enrolment.employee_id)
         .filter(Enrolment.company_id == actor.company_id, Enrolment.status.notin_(("cancelled", "rejected"))))
    if not actor.is_admin:
        q = q.filter(Enrolment.employee_id.in_(ids))
    if course_id:
        q = q.filter(Enrolment.course_id == course_id)
    if status:
        q = q.filter(Enrolment.status == status)
    if department_id:
        q = q.filter(E.department_id == department_id)
    t = _today(db, actor.company_id)
    if overdue:
        q = q.filter(Enrolment.status.in_(OPEN), Enrolment.due_date < t)
    total = q.count()
    rows = q.order_by(Enrolment.due_date.nulls_last(), Enrolment.id.desc()).offset((page - 1) * size).limit(size).all()
    return {"items": [{"uuid": str(e.uuid), "employee": _person(e.employee), "course": {"uuid": str(e.course.uuid), "title": e.course.title},
                       **enrolment_brief(e), "overdue": bool(e.due_date and e.due_date < t and e.status in OPEN)} for e in rows],
            "total": total, "page": page, "size": size}


def summary_report(db: Session, actor: Actor) -> dict:
    actor.require()
    t = _today(db, actor.company_id)
    base = db.query(Enrolment).filter(Enrolment.company_id == actor.company_id, Enrolment.status.notin_(("cancelled", "rejected")))
    total = base.count()
    completed = base.filter(Enrolment.status == "completed").count()
    overdue = base.filter(Enrolment.status.in_(OPEN), Enrolment.due_date < t).count()
    E = _E()
    from modules.departments.models import Department
    by_dep = []
    for dep, n, done in (db.query(Department.department_name, func.count(Enrolment.id),
                                  func.sum(func.cast(Enrolment.status == "completed", type_=Integer)))
                         .select_from(Enrolment).join(E, E.id == Enrolment.employee_id).outerjoin(Department, Department.id == E.department_id)
                         .filter(Enrolment.company_id == actor.company_id, Enrolment.status.notin_(("cancelled", "rejected")))
                         .group_by(Department.department_name).all()):
        by_dep.append({"department": dep or "No department", "enrolled": n, "completed": int(done or 0),
                       "completion_pct": round(int(done or 0) / n * 100) if n else 0})
    mandatory = []
    for c in _course_q(db, actor.company_id).filter(Course.status == "published").all():
        rows = base.filter(Enrolment.course_id == c.id, Enrolment.mandatory.is_(True)).all()
        if not rows and not c.mandatory:
            continue
        done = sum(1 for e in rows if e.status == "completed")
        mandatory.append({"course_uuid": str(c.uuid), "course": c.title, "assigned": len(rows), "completed": done,
                          "overdue": sum(1 for e in rows if e.status in OPEN and e.due_date and e.due_date < t),
                          "expiring": sum(1 for e in rows if e.expires_on and t <= e.expires_on <= t + timedelta(days=30)),
                          "completion_pct": round(done / len(rows) * 100) if rows else 0})
    avg = db.query(func.avg(Attempt.pct)).filter(Attempt.company_id == actor.company_id, Attempt.status == "submitted").scalar()
    pass_rate = db.query(func.avg(func.cast(Attempt.passed, type_=Integer))).filter(
        Attempt.company_id == actor.company_id, Attempt.status == "submitted").scalar()
    return {"courses": _course_q(db, actor.company_id).filter(Course.status == "published").count(),
            "enrolments": total, "completed": completed, "overdue": overdue,
            "completion_pct": round(completed / total * 100) if total else 0,
            "certificates": base.filter(Enrolment.certificate_no.isnot(None)).count(),
            "avg_score": round(float(avg), 1) if avg is not None else None,
            "pass_rate": round(float(pass_rate) * 100) if pass_rate is not None else None,
            "by_department": by_dep, "mandatory": mandatory,
            "pending_requests": db.query(CourseRequest).filter(CourseRequest.company_id == actor.company_id,
                                                               CourseRequest.status.in_(("pending", "approved"))).count()}
