"""Syllabus (units and topics per program + subject, optionally per term number), topic
coverage per section, weekly lesson plans and coverage percentages for principals."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from core.errors import BadRequest, Forbidden, NotFound
from core.timeutil import iso_utc
from modules.academics import service as academics
from modules.academics.models import Program, Section, SectionSubject, Subject
from modules.lms import service as svc
from modules.lms.models import Assignment, LessonPlan, SyllabusTopic, SyllabusUnit, TopicCoverage


def require_syllabus_editor(db: Session, user: dict, program_id: int, subject_id: int) -> None:
    """lms.manage, or a teacher who teaches the subject in a section of the program."""
    cid = user["company_id"]
    if not db.query(Program.id).filter(Program.company_id == cid, Program.id == program_id).first():
        raise BadRequest("Pick a program.")
    if not db.query(Subject.id).filter(Subject.company_id == cid, Subject.id == subject_id).first():
        raise BadRequest("Pick a subject.")
    if svc.is_manager(db, user):
        return
    if svc.TEACH not in svc.perms(db, user):
        raise Forbidden("You don't have permission to change the syllabus.")
    if not any(a["program_id"] == program_id and a["subject_id"] == subject_id for a in svc.allocations(db, user)):
        raise Forbidden("You can only change the syllabus of subjects you teach.")


def units(db: Session, company_id: int, program_id: int, subject_id: int, term_no: int | None = None) -> list[SyllabusUnit]:
    q = (db.query(SyllabusUnit).options(joinedload(SyllabusUnit.topics))
         .filter(SyllabusUnit.company_id == company_id, SyllabusUnit.program_id == program_id,
                 SyllabusUnit.subject_id == subject_id, SyllabusUnit.active.is_(True)))
    if term_no:
        q = q.filter(or_(SyllabusUnit.term_no == term_no, SyllabusUnit.term_no.is_(None)))
    return q.order_by(SyllabusUnit.term_no.nullsfirst(), SyllabusUnit.sort_order, SyllabusUnit.id).all()


def syllabus_out(db: Session, company_id: int, rows: list[SyllabusUnit], section_id: int | None = None) -> dict:
    covered: dict[int, TopicCoverage] = {}
    topic_ids = [t.id for u in rows for t in u.topics if t.active]
    if section_id and topic_ids:
        covered = {c.topic_id: c for c in db.query(TopicCoverage).filter(
            TopicCoverage.section_id == section_id, TopicCoverage.topic_id.in_(topic_ids)).all()}
    names = svc.employee_names(db, [c.covered_by_employee_id for c in covered.values()])
    out = []
    for u in rows:
        topics = []
        for t in u.topics:
            if not t.active:
                continue
            c = covered.get(t.id)
            topics.append({"id": t.id, "uuid": str(t.uuid), "title": t.title, "description": t.description,
                           "planned_periods": t.planned_periods, "sort_order": t.sort_order,
                           "covered": c is not None, "covered_on": c.covered_on.isoformat() if c else None,
                           "covered_by": names.get(c.covered_by_employee_id) if c else None, "note": c.note if c else None})
        out.append({"id": u.id, "uuid": str(u.uuid), "title": u.title, "description": u.description, "term_no": u.term_no,
                    "sort_order": u.sort_order, "topics": topics,
                    "covered": sum(1 for t in topics if t["covered"]), "total": len(topics)})
    total = sum(u["total"] for u in out)
    done = sum(u["covered"] for u in out)
    return {"units": out, "total": total, "covered": done, "percent": round(done * 100 / total, 1) if total else None}


def save_unit(db: Session, user: dict, data: dict, row: SyllabusUnit | None = None) -> SyllabusUnit:
    pid = data.get("program_id", row.program_id if row else None)
    sid = data.get("subject_id", row.subject_id if row else None)
    require_syllabus_editor(db, user, pid, sid)
    if row is None:
        n = db.query(func.count(SyllabusUnit.id)).filter(SyllabusUnit.company_id == user["company_id"],
                                                         SyllabusUnit.program_id == pid, SyllabusUnit.subject_id == sid).scalar()
        row = SyllabusUnit(company_id=user["company_id"], sort_order=(n or 0) + 1)
        db.add(row)
    topics = data.pop("topics", None)
    for k, v in data.items():
        setattr(row, k, v)
    db.flush()
    if topics is not None:
        for i, t in enumerate(topics, 1):
            title = (t.get("title") or "").strip()
            if title:
                db.add(SyllabusTopic(company_id=row.company_id, unit_id=row.id, title=title[:200], sort_order=i,
                                     planned_periods=t.get("planned_periods"), description=t.get("description")))
        db.flush()
    return row


def get_unit(db: Session, company_id: int, uuid) -> SyllabusUnit:
    row = db.query(SyllabusUnit).filter(SyllabusUnit.company_id == company_id, SyllabusUnit.uuid == uuid).first()
    if row is None:
        raise NotFound("Unit")
    return row


def get_topic(db: Session, company_id: int, uuid) -> SyllabusTopic:
    row = (db.query(SyllabusTopic).options(joinedload(SyllabusTopic.unit))
           .filter(SyllabusTopic.company_id == company_id, SyllabusTopic.uuid == uuid).first())
    if row is None:
        raise NotFound("Topic")
    return row


def save_topic(db: Session, user: dict, unit: SyllabusUnit, data: dict, row: SyllabusTopic | None = None) -> SyllabusTopic:
    require_syllabus_editor(db, user, unit.program_id, unit.subject_id)
    if row is None:
        n = db.query(func.count(SyllabusTopic.id)).filter(SyllabusTopic.unit_id == unit.id).scalar()
        row = SyllabusTopic(company_id=unit.company_id, unit_id=unit.id, sort_order=(n or 0) + 1)
        db.add(row)
    for k, v in data.items():
        setattr(row, k, v)
    db.flush()
    return row


def set_coverage(db: Session, user: dict, topic: SyllabusTopic, section_id: int, covered: bool,
                 covered_on: date | None, note: str | None) -> None:
    sec = db.query(Section).filter(Section.company_id == user["company_id"], Section.id == section_id).first()
    if sec is None or sec.program_id != topic.unit.program_id:
        raise BadRequest("The section doesn't follow this syllabus.")
    svc.require_teaching(db, user, [section_id], topic.unit.subject_id)
    row = db.query(TopicCoverage).filter(TopicCoverage.topic_id == topic.id, TopicCoverage.section_id == section_id).first()
    if not covered:
        if row is not None:
            db.delete(row)  # a tick mark, not a business record
        db.flush()
        return
    if row is None:
        row = TopicCoverage(company_id=user["company_id"], topic_id=topic.id, section_id=section_id)
        db.add(row)
    row.covered_on = covered_on or svc.local_today(db, user["company_id"])
    row.covered_by_employee_id = user.get("employee_id")
    row.note = (note or "").strip() or None
    db.flush()


def coverage_summary(db: Session, user: dict, program_id: int | None = None, subject_id: int | None = None) -> list[dict]:
    """Coverage % per section + subject in the current session (within the person's scope)."""
    cid = user["company_id"]
    sess = academics.current_session(db, cid)
    if not sess:
        return []
    q = (db.query(SectionSubject).join(Section, Section.id == SectionSubject.section_id)
         .options(joinedload(SectionSubject.section).joinedload(Section.program), joinedload(SectionSubject.subject),
                  joinedload(SectionSubject.teacher))
         .filter(SectionSubject.company_id == cid, Section.session_id == sess.id, Section.active.is_(True)))
    if not svc.is_manager(db, user):  # teachers see the subjects they teach (and their class-teacher sections)
        emp = user.get("employee_id") or -1
        led = db.query(Section.id).filter(Section.company_id == cid, Section.class_teacher_id == emp)
        q = q.filter(or_(SectionSubject.teacher_id == emp, SectionSubject.assistant_ids.contains([emp]),
                         SectionSubject.section_id.in_(led)))
    if program_id:
        q = q.filter(Section.program_id == program_id)
    if subject_id:
        q = q.filter(SectionSubject.subject_id == subject_id)
    allocs = q.all()
    totals = dict(((p, s), n) for p, s, n in db.query(SyllabusUnit.program_id, SyllabusUnit.subject_id, func.count(SyllabusTopic.id))
                  .join(SyllabusTopic, SyllabusTopic.unit_id == SyllabusUnit.id)
                  .filter(SyllabusUnit.company_id == cid, SyllabusUnit.active.is_(True), SyllabusTopic.active.is_(True))
                  .group_by(SyllabusUnit.program_id, SyllabusUnit.subject_id).all())
    done = dict(((sec, sub), n) for sec, sub, n in db.query(TopicCoverage.section_id, SyllabusUnit.subject_id, func.count(TopicCoverage.id))
                .join(SyllabusTopic, SyllabusTopic.id == TopicCoverage.topic_id)
                .join(SyllabusUnit, SyllabusUnit.id == SyllabusTopic.unit_id)
                .filter(TopicCoverage.company_id == cid, SyllabusTopic.active.is_(True), SyllabusUnit.active.is_(True))
                .group_by(TopicCoverage.section_id, SyllabusUnit.subject_id).all())
    out = []
    for a in allocs:
        total = totals.get((a.section.program_id, a.subject_id), 0)
        covered = done.get((a.section_id, a.subject_id), 0)
        out.append({"section_id": a.section_id, "section_label": academics.section_label(a.section),
                    "program_id": a.section.program_id, "subject_id": a.subject_id, "subject_name": a.subject.name,
                    "teacher_id": a.teacher_id, "teacher_name": svc._emp_name(a.teacher), "total": total,
                    "covered": covered, "percent": round(covered * 100 / total, 1) if total else None})
    out.sort(key=lambda r: (r["section_label"], r["subject_name"]))
    return out


# ── Lesson plans ─────────────────────────────────────────────────────

def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def plan_query(db: Session, user: dict):
    q = (db.query(LessonPlan).options(joinedload(LessonPlan.section).joinedload(Section.program),
                                      joinedload(LessonPlan.subject), joinedload(LessonPlan.teacher),
                                      joinedload(LessonPlan.homework))
         .filter(LessonPlan.company_id == user["company_id"], LessonPlan.archived_at.is_(None)))
    scope = svc.staff_section_scope(db, user)
    if scope is not None:
        q = q.filter(or_(LessonPlan.section_id.in_(scope or [-1]), LessonPlan.teacher_id == user.get("employee_id")))
    return q


def get_plan(db: Session, user: dict, uuid) -> LessonPlan:
    row = plan_query(db, user).filter(LessonPlan.uuid == uuid).first()
    if row is None:
        raise NotFound("Lesson plan")
    return row


def plan_out(db: Session, p: LessonPlan, topics: dict[int, str] | None = None) -> dict:
    ids = p.topic_ids or []
    if topics is None:
        topics = {t.id: t.title for t in db.query(SyllabusTopic).filter(SyllabusTopic.id.in_(ids or [-1])).all()}
    return {"id": p.id, "uuid": str(p.uuid), "section_id": p.section_id, "section_label": academics.section_label(p.section),
            "subject_id": p.subject_id, "subject_name": p.subject.name if p.subject else None,
            "teacher_id": p.teacher_id, "teacher_name": svc._emp_name(p.teacher),
            "week_start": p.week_start.isoformat(), "week_end": (p.week_start + timedelta(days=6)).isoformat(),
            "title": p.title, "topic_ids": ids, "topics": [{"id": i, "title": topics.get(i)} for i in ids if i in topics],
            "objectives": p.objectives, "activities": p.activities, "resources": p.resources, "assessment": p.assessment,
            "homework_id": p.homework_id,
            "homework": {"uuid": str(p.homework.uuid), "title": p.homework.title} if p.homework else None,
            "status": p.status, "reflection": p.reflection, "created_at": iso_utc(p.created_at),
            "updated_at": iso_utc(p.updated_at)}


def save_plan(db: Session, user: dict, data: dict, row: LessonPlan | None = None) -> LessonPlan:
    cid = user["company_id"]
    section_id = data.get("section_id", row.section_id if row else None)
    subject_id = data.get("subject_id", row.subject_id if row else None)
    svc.require_teaching(db, user, [section_id], subject_id)
    if data.get("week_start"):
        data["week_start"] = monday(data["week_start"])
    if "topic_ids" in data:
        ids = [int(i) for i in data["topic_ids"] or []]
        sec = db.query(Section).filter(Section.id == section_id).first()
        valid = {t for (t,) in db.query(SyllabusTopic.id).join(SyllabusUnit, SyllabusUnit.id == SyllabusTopic.unit_id)
                 .filter(SyllabusTopic.company_id == cid, SyllabusTopic.id.in_(ids or [-1]),
                         SyllabusUnit.program_id == sec.program_id, SyllabusUnit.subject_id == subject_id).all()}
        if set(ids) - valid:
            raise BadRequest("A topic doesn't belong to this subject's syllabus.")
        data["topic_ids"] = ids
    if data.get("homework_id"):
        if not db.query(Assignment.id).filter(Assignment.company_id == cid, Assignment.id == data["homework_id"]).first():
            raise BadRequest("The homework was not found.")
    mark = data.pop("mark_topics_covered", False)
    if row is None:
        teacher = user.get("employee_id")
        if svc.is_manager(db, user) and data.get("teacher_id"):
            teacher = data["teacher_id"]
        row = LessonPlan(company_id=cid, teacher_id=teacher)
        db.add(row)
    data.pop("teacher_id", None)
    for k, v in data.items():
        setattr(row, k, v)
    db.flush()
    if mark and row.status == "done":
        for tid in row.topic_ids or []:
            t = db.query(SyllabusTopic).options(joinedload(SyllabusTopic.unit)).filter(SyllabusTopic.id == tid).first()
            if t and not db.query(TopicCoverage.id).filter(TopicCoverage.topic_id == tid,
                                                           TopicCoverage.section_id == row.section_id).first():
                set_coverage(db, user, t, row.section_id, True, min(row.week_start + timedelta(days=4),
                                                                      svc.local_today(db, cid)), None)
    return row
