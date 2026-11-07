"""Teaching and learning: shared access rules, settings, homework and study materials.

Online classes live in `live.py`, quizzes in `quiz.py`, syllabus and lesson plans in
`syllabus.py`; they all use the access helpers here.

Who sees what
- Staff with `lms.manage` see and change everything in the institution.
- Other staff (`lms.view` / `lms.teach`) see only the sections they teach or lead
  (`academics.teacher_section_ids`) and create work only for a section + subject pair from
  their teacher allocation.
- Parents and students use `/lms/portal/...`; `portal_student()` checks the learner is the
  signed-in student or one of the parent's children and returns the learner's current
  section and program, which every portal query filters on.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from core import audit, context
from core.errors import BadRequest, Forbidden, NotFound
from core.security import _get_role_permissions
from core.timeutil import iso_utc, tz_for, utc_now
from modules.academics import service as academics
from modules.academics.models import Program, Section, SectionSubject, Subject
from modules.lms.models import (Assignment, AssignmentSection, LmsSettings, Material, MaterialSection, MaterialView,
                                Submission, SyllabusTopic, SyllabusUnit)
from modules.students import service as students
from modules.students.models import Student

MANAGE, TEACH, VIEW, SETTINGS = "lms.manage", "lms.teach", "lms.view", "lms.settings.manage"

DEFAULT_SETTINGS: dict = {
    # Provider picked for new live classes: jitsi (built-in video room) | zoom | meet | teams | other
    "video_provider": "jitsi",
    # Built-in video rooms: the public Jitsi Meet service or the institution's own Jitsi server.
    "jitsi_domain": "meet.jit.si",
    "jitsi_room_prefix": "campusly",
    # Minutes before a class when learners get the "starting soon" reminder.
    "reminder_minutes": 15,
    # How early learners may open the room before the start time.
    "join_early_minutes": 10,
    # Parents may also open their child's live class (e.g. for young learners).
    "parents_can_join": False,
    # Defaults for new homework.
    "homework_allow_late": True,
    "homework_notify_parents": True,
    "homework_default_due_hour": 9,
    # New quizzes.
    "quiz_default_time_limit": 30,
    "quiz_show_answers": "after_close",
}
PROVIDERS = ("jitsi", "zoom", "meet", "teams", "other")


# ── Access ───────────────────────────────────────────────────────────

def perms(db: Session, user: dict) -> list[str]:
    return _get_role_permissions(user["company_id"], user["role"], db)


def is_manager(db: Session, user: dict) -> bool:
    return MANAGE in perms(db, user)


def is_portal(user: dict) -> bool:
    return bool(user.get("portal_id"))


def staff_section_scope(db: Session, user: dict) -> set[int] | None:
    """Sections a staff member may see: None = all (lms.manage), else the sections they teach
    or lead in the current session."""
    if is_portal(user):
        return set()
    if is_manager(db, user):
        return None
    sess = academics.current_session(db, user["company_id"])
    return academics.teacher_section_ids(db, user["company_id"], user.get("employee_id"), sess.id if sess else None)


def allocations(db: Session, user: dict) -> list[dict]:
    """Section + subject pairs the person may set work for (all of them with lms.manage)."""
    cid = user["company_id"]
    sess = academics.current_session(db, cid)
    if not sess:
        return []
    q = (db.query(SectionSubject).join(Section, Section.id == SectionSubject.section_id)
         .options(joinedload(SectionSubject.section).joinedload(Section.program), joinedload(SectionSubject.subject),
                  joinedload(SectionSubject.teacher))
         .filter(SectionSubject.company_id == cid, Section.session_id == sess.id, Section.active.is_(True)))
    if not is_manager(db, user):
        emp = user.get("employee_id")
        if not emp:
            return []
        q = q.filter(or_(SectionSubject.teacher_id == emp, SectionSubject.assistant_ids.contains([emp])))
    rows = q.all()
    rows.sort(key=lambda a: (a.section.program.sort_order if a.section.program else 0, a.section.name, a.subject.name))
    return [{
        "section_id": a.section_id, "section_uuid": str(a.section.uuid), "section_label": academics.section_label(a.section),
        "program_id": a.section.program_id, "program_name": a.section.program.name if a.section.program else None,
        "subject_id": a.subject_id, "subject_name": a.subject.name, "subject_code": a.subject.code,
        "teacher_id": a.teacher_id, "teacher_name": _emp_name(a.teacher),
    } for a in rows]


def require_teaching(db: Session, user: dict, section_ids: list[int], subject_id: int | None) -> None:
    """Refuse unless the person teaches `subject_id` in every section (lms.manage passes)."""
    cid = user["company_id"]
    if not section_ids:
        raise BadRequest("Pick at least one section.")
    found = db.query(Section.id).filter(Section.company_id == cid, Section.id.in_(section_ids)).count()
    if found != len(set(section_ids)):
        raise BadRequest("A section was not found.")
    if subject_id is not None and not db.query(Subject.id).filter(Subject.company_id == cid, Subject.id == subject_id).first():
        raise BadRequest("The subject was not found.")
    if is_manager(db, user):
        return
    if TEACH not in perms(db, user):
        raise Forbidden("You don't have permission to set work for learners.")
    emp = user.get("employee_id")
    if not emp:
        raise Forbidden("Only teachers can do this.")
    q = db.query(SectionSubject.section_id).filter(
        SectionSubject.company_id == cid, SectionSubject.section_id.in_(section_ids),
        or_(SectionSubject.teacher_id == emp, SectionSubject.assistant_ids.contains([emp])))
    if subject_id is not None:
        q = q.filter(SectionSubject.subject_id == subject_id)
    allowed = {s for (s,) in q.all()}
    if subject_id is None:  # class teachers may also use their own sections without a subject
        allowed |= {s for (s,) in db.query(Section.id).filter(Section.company_id == cid, Section.id.in_(section_ids),
                                                              Section.class_teacher_id == emp).all()}
    if set(section_ids) - allowed:
        raise Forbidden("You can only set work for the sections and subjects you teach.")


def require_section_visible(db: Session, user: dict, section_ids) -> None:
    scope = staff_section_scope(db, user)
    if scope is not None and not (set(section_ids) & scope):
        raise NotFound("Record")


@dataclass
class Learner:
    student: Student
    section_id: int | None
    program_id: int | None


def portal_student(db: Session, user: dict, student_id: int | None) -> Learner:
    """The learner a parent or student is looking at (404 for anyone else's child)."""
    ids = students.visible_student_ids(db, user)
    if ids is None:
        raise Forbidden("This page is for parents and students.")
    if student_id is None:
        if user.get("role") == "student" and user.get("student_id"):
            student_id = user["student_id"]
        elif len(ids) == 1:
            student_id = next(iter(ids))
        else:
            raise BadRequest("Pick a child first.")
    if student_id not in ids:
        raise NotFound("Student")
    s = db.query(Student).filter(Student.company_id == user["company_id"], Student.id == student_id).first()
    if s is None:
        raise NotFound("Student")
    e = students.current_enrollment(db, user["company_id"], s.id)
    return Learner(s, e.section_id if e else None, e.program_id if e else None)


def _emp_name(e) -> str | None:
    return f"{e.first_name} {e.last_name or ''}".strip() if e else None


def employee_names(db: Session, ids) -> dict[int, str]:
    from modules.employees.models import Employee
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {e.id: _emp_name(e) for e in db.query(Employee).filter(Employee.id.in_(ids)).all()}


def section_labels(db: Session, company_id: int, ids) -> dict[int, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    rows = db.query(Section).options(joinedload(Section.program)).filter(
        Section.company_id == company_id, Section.id.in_(ids)).all()
    return {s.id: academics.section_label(s) for s in rows}


def actor_name() -> str | None:
    return context.current().actor_name


def as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def company_tz(db: Session, company_id: int):
    from modules.company.models import Company
    return tz_for(db.query(Company).filter(Company.id == company_id).first())


def day_bounds(db: Session, company_id: int, day: date) -> tuple[datetime, datetime]:
    """UTC start and end of a calendar day in the institution's timezone."""
    tz = company_tz(db, company_id)
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def local_today(db: Session, company_id: int) -> date:
    return datetime.now(company_tz(db, company_id)).date()


def dec(v) -> str | None:
    return None if v is None else str(Decimal(v).quantize(Decimal("0.01")))


# ── Settings ─────────────────────────────────────────────────────────

def get_settings_row(db: Session, company_id: int) -> LmsSettings:
    row = db.query(LmsSettings).filter(LmsSettings.company_id == company_id).first()
    if row is None:
        row = LmsSettings(company_id=company_id, config={})
        db.add(row)
        db.flush()
    return row


def settings(db: Session, company_id: int) -> dict:
    row = db.query(LmsSettings).filter(LmsSettings.company_id == company_id).first()
    cfg = row.config if row and isinstance(row.config, dict) else {}
    return {**DEFAULT_SETTINGS, **{k: v for k, v in cfg.items() if k in DEFAULT_SETTINGS}}


_DOMAIN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+(:\d{2,5})?$")


def save_settings(db: Session, company_id: int, data: dict) -> dict:
    row = get_settings_row(db, company_id)
    cfg = dict(row.config or {})
    for k, v in data.items():
        if k not in DEFAULT_SETTINGS or v is None:
            continue
        if k == "video_provider" and v not in PROVIDERS:
            raise BadRequest("Pick a video provider from the list.")
        if k == "jitsi_domain":
            v = str(v).strip().lower().removeprefix("https://").removeprefix("http://").rstrip("/")
            if not _DOMAIN.match(v):
                raise BadRequest("Enter the video server address, for example meet.jit.si or meet.yourschool.in.")
        if k == "jitsi_room_prefix":
            v = re.sub(r"[^a-z0-9-]", "", str(v).lower())[:20] or "campusly"
        if k in ("reminder_minutes", "join_early_minutes", "homework_default_due_hour", "quiz_default_time_limit"):
            v = max(0, min(int(v), 240))
        if k == "quiz_show_answers" and v not in ("after_submit", "after_close", "never"):
            raise BadRequest("Pick when learners see the answers.")
        cfg[k] = v
    row.config = cfg
    db.flush()
    return settings(db, company_id)


# ── Uploads ──────────────────────────────────────────────────────────

def attachments(db: Session, company_id: int, uuids: list | None) -> list[dict]:
    from modules.documents import storage
    return storage.attachments_from(db, company_id, uuids, limit=10)


def with_links(db: Session, company_id: int, items: list | None) -> list[dict]:
    from modules.documents import storage
    return storage.with_links(db, company_id, items)


# ── Homework and assignments ─────────────────────────────────────────

def assignment_query(db: Session, user: dict):
    q = db.query(Assignment).options(joinedload(Assignment.subject)).filter(
        Assignment.company_id == user["company_id"], Assignment.archived_at.is_(None))
    scope = staff_section_scope(db, user)
    if scope is not None:
        sub = db.query(AssignmentSection.assignment_id).filter(AssignmentSection.section_id.in_(scope or [-1]))
        q = q.filter(or_(Assignment.id.in_(sub), Assignment.created_by_employee_id == user.get("employee_id")))
    return q


def get_assignment(db: Session, user: dict, uuid) -> Assignment:
    row = assignment_query(db, user).filter(Assignment.uuid == uuid).first()
    if row is None:
        raise NotFound("Homework")
    return row


def assignment_learners(db: Session, a: Assignment) -> list[tuple[Student, object]]:
    out = []
    for link in a.sections:
        out += students.section_students(db, a.company_id, link.section_id)
    return out


def submission_state(a: Assignment, sub: Submission | None, now: datetime | None = None) -> str:
    """pending | submitted | late | graded | returned | resubmit | missing | offline"""
    now = now or utc_now()
    if sub is None:
        if a.submission_type == "offline":
            return "pending"
        return "missing" if as_utc(a.due_at) < now and not a.allow_late else "pending"
    if sub.status in ("graded", "returned", "resubmit"):
        return sub.status
    if sub.status == "offline":
        return "submitted"
    return "late" if sub.is_late else "submitted"


def assignment_counts(db: Session, rows: list[Assignment]) -> dict[int, dict]:
    if not rows:
        return {}
    ids = [a.id for a in rows]
    sec_ids = {l.section_id for a in rows for l in a.sections}
    from modules.students.models import Enrollment
    strength = dict(db.query(Enrollment.section_id, func.count(Enrollment.id)).join(Student, Student.id == Enrollment.student_id)
                    .filter(Enrollment.section_id.in_(sec_ids or [-1]), Enrollment.status.in_(students.LIVE_ENROLMENT),
                            Student.status == students.ACTIVE)
                    .group_by(Enrollment.section_id).all())
    stats: dict[int, dict] = {i: {"submitted": 0, "late": 0, "graded": 0, "to_grade": 0} for i in ids}
    for aid, status, late, n in (db.query(Submission.assignment_id, Submission.status, Submission.is_late, func.count(Submission.id))
                                 .filter(Submission.assignment_id.in_(ids))
                                 .group_by(Submission.assignment_id, Submission.status, Submission.is_late).all()):
        st = stats[aid]
        if status in ("graded", "returned"):
            st["graded"] += n
            st["submitted"] += n
        elif status in ("submitted", "offline"):
            st["submitted"] += n
            st["to_grade"] += n
        if late:
            st["late"] += n
    for a in rows:
        total = sum(strength.get(l.section_id, 0) for l in a.sections)
        stats[a.id]["learners"] = total
        stats[a.id]["pending"] = max(total - stats[a.id]["submitted"], 0)
    return stats


def assignment_out(db: Session, a: Assignment, labels: dict[int, str] | None = None, counts: dict | None = None,
                   links: bool = False) -> dict:
    labels = labels if labels is not None else section_labels(db, a.company_id, [l.section_id for l in a.sections])
    out = {
        "id": a.id, "uuid": str(a.uuid), "title": a.title, "instructions": a.instructions, "kind": a.kind,
        "subject_id": a.subject_id, "subject_name": a.subject.name if a.subject else None,
        "program_id": a.program_id,
        "section_ids": [l.section_id for l in a.sections],
        "sections": [{"id": l.section_id, "label": labels.get(l.section_id)} for l in a.sections],
        "attachments": with_links(db, a.company_id, a.attachments) if links else (a.attachments or []),
        "assigned_on": a.assigned_on.isoformat() if a.assigned_on else None,
        "due_at": iso_utc(a.due_at), "max_marks": dec(a.max_marks), "submission_type": a.submission_type,
        "allow_late": a.allow_late, "status": a.status, "published_at": iso_utc(a.published_at),
        "created_by_employee_id": a.created_by_employee_id, "created_by_name": a.created_by_name,
        "created_at": iso_utc(a.created_at), "is_overdue": as_utc(a.due_at) < utc_now(),
    }
    if counts is not None:
        out["counts"] = counts.get(a.id)
    return out


def save_assignment(db: Session, user: dict, data: dict, row: Assignment | None = None) -> Assignment:
    cid = user["company_id"]
    section_ids = data.pop("section_ids", None)
    if row is None and not section_ids:
        raise BadRequest("Pick at least one section.")
    subject_id = data.get("subject_id", row.subject_id if row else None)
    if section_ids is not None or "subject_id" in data:
        require_teaching(db, user, section_ids if section_ids is not None else [l.section_id for l in row.sections],
                         subject_id)
    elif row is not None:
        require_teaching(db, user, [l.section_id for l in row.sections], row.subject_id)
    if "attachments" in data:
        data["attachments"] = attachments(db, cid, data["attachments"])
    if data.get("max_marks") is not None and Decimal(data["max_marks"]) <= 0:
        raise BadRequest("Maximum marks must be more than zero.")
    was_published = row is not None and row.status == "published"
    if row is None:
        row = Assignment(company_id=cid, created_by_employee_id=user.get("employee_id"), created_by_name=actor_name(),
                         assigned_on=local_today(db, cid))
        db.add(row)
    for k, v in data.items():
        setattr(row, k, v)
    if section_ids is not None:
        secs = db.query(Section).filter(Section.company_id == cid, Section.id.in_(section_ids)).all()
        programs = {s.program_id for s in secs}
        row.program_id = programs.pop() if len(programs) == 1 else None
        keep = {l.section_id: l for l in row.sections}
        row.sections = [keep.get(sid) or AssignmentSection(company_id=cid, section_id=sid) for sid in dict.fromkeys(section_ids)]
    if row.status == "published" and row.published_at is None:
        row.published_at = utc_now()
    db.flush()
    if row.status == "published" and not was_published:
        notify_new_homework(db, row)
    return row


def notify_new_homework(db: Session, a: Assignment) -> None:
    from modules.notifications.service import notify_portal
    cfg = settings(db, a.company_id)
    sids = [s.id for s, _ in assignment_learners(db, a)]
    accounts = students.portal_account_ids_for_students(db, a.company_id, sids, parents=cfg["homework_notify_parents"],
                                                        students=True)
    tz = company_tz(db, a.company_id)
    due = as_utc(a.due_at).astimezone(tz).strftime("%d %b, %I:%M %p")
    subject = a.subject.name if a.subject else db.query(Subject.name).filter(Subject.id == a.subject_id).scalar()
    notify_portal(db, a.company_id, accounts, kind="lms.homework", title=f"New {a.kind}: {a.title}",
                  body=f"{subject}. Due {due}.", link="/dashboard/my/homework", actor_name=a.created_by_name)


def submissions_roster(db: Session, a: Assignment) -> list[dict]:
    subs = {s.student_id: s for s in db.query(Submission).filter(Submission.assignment_id == a.id).all()}
    labels = section_labels(db, a.company_id, [l.section_id for l in a.sections])
    now = utc_now()
    out = []
    for st, enr in assignment_learners(db, a):
        sub = subs.get(st.id)
        out.append({
            "student_id": st.id, "student_uuid": str(st.uuid), "name": st.full_name, "admission_no": st.admission_no,
            "roll_no": enr.roll_no, "photo_url": st.photo_url, "section_id": enr.section_id,
            "section_label": labels.get(enr.section_id), "state": submission_state(a, sub, now),
            "submission": submission_out(db, sub, links=True) if sub else None,
        })
    return out


def submission_out(db: Session, s: Submission, links: bool = False, show_marks: bool = True) -> dict:
    return {
        "uuid": str(s.uuid), "status": s.status, "content": s.content,
        "attachments": with_links(db, s.company_id, s.attachments) if links else (s.attachments or []),
        "submitted_at": iso_utc(s.submitted_at), "is_late": s.is_late, "attempts": s.attempts,
        "marks": dec(s.marks) if show_marks else None, "feedback": s.feedback if show_marks else None,
        "graded_at": iso_utc(s.graded_at) if show_marks else None, "returned_at": iso_utc(s.returned_at),
    }


def submit(db: Session, user: dict, a: Assignment, student: Student, section_id: int | None, content: str | None,
           files: list | None, now: datetime | None = None) -> Submission:
    """A learner hands in work. Rules: the homework is published and set for the learner's
    section, it takes online submissions, late work only when allowed, and marked work can only
    be sent again when the teacher asks for another try."""
    now = now or utc_now()
    if a.archived_at is not None or a.status != "published":
        raise BadRequest("This homework is not open for submissions.")
    if section_id is None or section_id not in {l.section_id for l in a.sections}:
        raise NotFound("Homework")
    if a.submission_type != "online":
        raise BadRequest("This homework is handed in on paper, not online.")
    content = (content or "").strip() or None
    if not content and not files:
        raise BadRequest("Write an answer or attach a file.")
    late = as_utc(a.due_at) < now
    if late and not a.allow_late:
        raise BadRequest("The due time has passed and late work isn't accepted for this homework.")
    sub = db.query(Submission).filter(Submission.assignment_id == a.id, Submission.student_id == student.id).first()
    if sub is not None and sub.status in ("graded", "returned"):
        raise BadRequest("This work has already been marked. Ask your teacher if you need to send it again.")
    stored = attachments(db, a.company_id, files)
    if sub is None:
        sub = Submission(company_id=a.company_id, assignment_id=a.id, student_id=student.id, attempts=1)
        db.add(sub)
    else:
        sub.attempts = (sub.attempts or 0) + 1
    sub.content, sub.attachments, sub.status = content, stored, "submitted"
    sub.submitted_at, sub.is_late, sub.submitted_by_portal_id = now, late, user.get("portal_id")
    db.flush()
    if a.created_by_employee_id:
        from modules.notifications.service import notify
        n = db.query(func.count(Submission.id)).filter(Submission.assignment_id == a.id,
                                                       Submission.status == "submitted").scalar()
        notify(db, a.company_id, [a.created_by_employee_id], kind="lms.submission",
               title=f"{n} submission{'s' if n != 1 else ''} to check: {a.title}",
               body=f"{student.full_name} handed in{' late' if late else ''}.",
               link=f"/dashboard/lms/homework?view={a.uuid}", group_key=f"lms.submission:{a.id}")
    return sub


def grade(db: Session, user: dict, a: Assignment, student: Student, marks: Decimal | None, feedback: str | None,
          return_now: bool) -> Submission:
    require_teaching(db, user, [l.section_id for l in a.sections], a.subject_id)
    if student.id not in {s.id for s, _ in assignment_learners(db, a)}:
        raise NotFound("Student")
    if marks is not None:
        if marks < 0:
            raise BadRequest("Marks can't be negative.")
        if a.max_marks is not None and marks > a.max_marks:
            raise BadRequest(f"Marks can't be more than {dec(a.max_marks)}.")
    sub = db.query(Submission).filter(Submission.assignment_id == a.id, Submission.student_id == student.id).first()
    if sub is None:  # handed in on paper, or marked without an online submission
        sub = Submission(company_id=a.company_id, assignment_id=a.id, student_id=student.id, status="offline",
                         submitted_at=utc_now(), attempts=1)
        db.add(sub)
    sub.marks, sub.feedback = marks, (feedback or "").strip() or None
    sub.graded_at, sub.graded_by_employee_id = utc_now(), user.get("employee_id")
    sub.status = "graded"
    if return_now:
        _return(db, a, [sub])
    db.flush()
    return sub


def _return(db: Session, a: Assignment, subs: list[Submission]) -> int:
    from modules.notifications.service import notify_portal
    now = utc_now()
    n = 0
    for s in subs:
        if s.status != "graded":
            continue
        s.status, s.returned_at = "returned", now
        accounts = students.portal_account_ids_for_students(db, a.company_id, [s.student_id], parents=True, students=True)
        mark = f" Marks: {dec(s.marks)}{' / ' + dec(a.max_marks) if a.max_marks is not None else ''}." if s.marks is not None else ""
        notify_portal(db, a.company_id, accounts, kind="lms.graded", title=f"Homework marked: {a.title}",
                      body=f"Your teacher has marked this work.{mark}", link="/dashboard/my/homework")
        n += 1
    return n


def return_all(db: Session, user: dict, a: Assignment) -> int:
    require_teaching(db, user, [l.section_id for l in a.sections], a.subject_id)
    subs = db.query(Submission).filter(Submission.assignment_id == a.id, Submission.status == "graded").all()
    n = _return(db, a, subs)
    audit.event(db, "lms.homework_returned", "lms_assignments", a.id, {"count": n}, entity_label=a.title)
    return n


def ask_resubmit(db: Session, user: dict, a: Assignment, student: Student, note: str | None) -> Submission:
    require_teaching(db, user, [l.section_id for l in a.sections], a.subject_id)
    sub = db.query(Submission).filter(Submission.assignment_id == a.id, Submission.student_id == student.id).first()
    if sub is None:
        raise BadRequest("Nothing has been handed in yet.")
    sub.status, sub.feedback = "resubmit", (note or sub.feedback)
    from modules.notifications.service import notify_portal
    notify_portal(db, a.company_id, students.portal_account_ids_for_students(db, a.company_id, [student.id]),
                  kind="lms.resubmit", title=f"Please send again: {a.title}", body=note or "Your teacher asked for another try.",
                  link="/dashboard/my/homework")
    db.flush()
    return sub


def portal_assignments(db: Session, learner: Learner, status: str | None = None):
    if not learner.section_id:
        return []
    rows = (db.query(Assignment).options(joinedload(Assignment.subject))
            .join(AssignmentSection, AssignmentSection.assignment_id == Assignment.id)
            .filter(Assignment.company_id == learner.student.company_id, AssignmentSection.section_id == learner.section_id,
                    Assignment.archived_at.is_(None), Assignment.status.in_(("published", "closed")))
            .order_by(Assignment.due_at.desc()).limit(300).all())
    subs = {s.assignment_id: s for s in db.query(Submission).filter(
        Submission.student_id == learner.student.id, Submission.assignment_id.in_([a.id for a in rows] or [-1])).all()}
    now = utc_now()
    out = []
    for a in rows:
        sub = subs.get(a.id)
        state = submission_state(a, sub, now)
        if status == "due" and state not in ("pending", "resubmit"):
            continue
        if status == "done" and state in ("pending", "resubmit", "missing"):
            continue
        out.append((a, sub, state))
    return out


def portal_assignment_out(db: Session, a: Assignment, sub: Submission | None, state: str, full: bool = False) -> dict:
    out = assignment_out(db, a, labels={}, links=full)
    out.pop("created_by_employee_id", None)
    out["state"] = state
    shown = sub is not None and sub.status == "returned"
    out["submission"] = submission_out(db, sub, links=full, show_marks=shown or (sub is not None and sub.status == "resubmit")) if sub else None
    out["can_submit"] = (a.status == "published" and a.submission_type == "online"
                         and (sub is None or sub.status in ("submitted", "resubmit"))
                         and (a.allow_late or as_utc(a.due_at) >= utc_now()))
    return out


# ── Study materials ──────────────────────────────────────────────────

_YT = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/|live/)|youtu\.be/)([A-Za-z0-9_-]{11})")


def youtube_id(url: str | None) -> str | None:
    m = _YT.search(url or "")
    return m.group(1) if m else None


def material_query(db: Session, user: dict):
    q = db.query(Material).options(joinedload(Material.subject), joinedload(Material.program)).filter(
        Material.company_id == user["company_id"], Material.archived_at.is_(None))
    scope = staff_section_scope(db, user)
    if scope is not None:
        programs = {p for (p,) in db.query(Section.program_id).filter(Section.id.in_(scope or [-1])).all()}
        q = q.filter(or_(Material.program_id.in_(programs or [-1]), Material.created_by_employee_id == user.get("employee_id")))
    return q


def get_material(db: Session, user: dict, uuid) -> Material:
    row = material_query(db, user).filter(Material.uuid == uuid).first()
    if row is None:
        raise NotFound("Material")
    return row


def material_out(db: Session, m: Material, labels: dict | None = None, links: bool = False) -> dict:
    secs = [l.section_id for l in m.sections]
    labels = labels if labels is not None else section_labels(db, m.company_id, secs)
    yt = youtube_id(m.url) if m.kind == "video" else None
    return {
        "id": m.id, "uuid": str(m.uuid), "title": m.title, "description": m.description, "kind": m.kind, "url": m.url,
        "youtube_id": yt, "embed_url": f"https://www.youtube-nocookie.com/embed/{yt}" if yt else None,
        "attachments": with_links(db, m.company_id, m.attachments) if links else (m.attachments or []),
        "program_id": m.program_id, "program_name": m.program.name if m.program else None,
        "subject_id": m.subject_id, "subject_name": m.subject.name if m.subject else None,
        "unit_id": m.unit_id, "unit_title": m.unit.title if m.unit else None,
        "topic_id": m.topic_id, "topic": m.topic_row.title if m.topic_row else m.topic,
        "section_ids": secs, "sections": [{"id": s, "label": labels.get(s)} for s in secs],
        "published": m.published, "view_count": m.view_count, "created_by_employee_id": m.created_by_employee_id,
        "created_by_name": m.created_by_name, "created_at": iso_utc(m.created_at), "updated_at": iso_utc(m.updated_at),
    }


def save_material(db: Session, user: dict, data: dict, row: Material | None = None) -> Material:
    cid = user["company_id"]
    section_ids = data.pop("section_ids", None)
    program_id = data.get("program_id", row.program_id if row else None)
    subject_id = data.get("subject_id", row.subject_id if row else None)
    if not db.query(Program.id).filter(Program.company_id == cid, Program.id == program_id).first():
        raise BadRequest("Pick a program.")
    target = section_ids if section_ids is not None else ([l.section_id for l in row.sections] if row else [])
    if target:
        bad = db.query(Section.id).filter(Section.company_id == cid, Section.id.in_(target),
                                          Section.program_id != program_id).first()
        if bad:
            raise BadRequest("The sections must belong to the chosen program.")
        require_teaching(db, user, target, subject_id)
    else:
        sess = academics.current_session(db, cid)
        program_sections = [s for (s,) in db.query(Section.id).filter(
            Section.company_id == cid, Section.program_id == program_id,
            Section.session_id == (sess.id if sess else -1)).all()]
        if not is_manager(db, user):
            # Teachers sharing with the whole program must teach the subject in at least one of its sections.
            mine = {a["section_id"] for a in allocations(db, user) if a["subject_id"] == subject_id}
            if not (mine & set(program_sections)):
                raise Forbidden("You can only share materials for the subjects you teach.")
    kind = data.get("kind", row.kind if row else "file")
    if "attachments" in data:
        data["attachments"] = attachments(db, cid, data["attachments"])
    url = data.get("url", row.url if row else None)
    if kind in ("link", "video") and not url:
        raise BadRequest("Add the link.")
    if kind == "video" and url and not youtube_id(url) and not url.startswith("https://"):
        raise BadRequest("Paste a YouTube or other https video link.")
    if "unit_id" in data and data["unit_id"]:
        if not db.query(SyllabusUnit.id).filter(SyllabusUnit.company_id == cid, SyllabusUnit.id == data["unit_id"]).first():
            raise BadRequest("The unit was not found.")
    if "topic_id" in data and data["topic_id"]:
        t = db.query(SyllabusTopic).filter(SyllabusTopic.company_id == cid, SyllabusTopic.id == data["topic_id"]).first()
        if t is None:
            raise BadRequest("The topic was not found.")
        data.setdefault("unit_id", t.unit_id)
    if row is None:
        row = Material(company_id=cid, created_by_employee_id=user.get("employee_id"), created_by_name=actor_name())
        db.add(row)
    for k, v in data.items():
        setattr(row, k, v)
    if section_ids is not None:
        keep = {l.section_id: l for l in row.sections}
        row.sections = [keep.get(s) or MaterialSection(company_id=cid, section_id=s) for s in dict.fromkeys(section_ids)]
    db.flush()
    return row


def portal_materials_query(db: Session, learner: Learner):
    cid = learner.student.company_id
    if not learner.program_id:
        return None
    restricted = db.query(MaterialSection.material_id)
    mine = db.query(MaterialSection.material_id).filter(MaterialSection.section_id == (learner.section_id or -1))
    return (db.query(Material).options(joinedload(Material.subject), joinedload(Material.program))
            .filter(Material.company_id == cid, Material.archived_at.is_(None), Material.published.is_(True),
                    Material.program_id == learner.program_id,
                    or_(~Material.id.in_(restricted), Material.id.in_(mine))))


def record_view(db: Session, user: dict, m: Material, student_id: int | None) -> None:
    key = f"s:{student_id}" if user.get("role") == "student" and student_id else (
        f"p:{user['portal_id']}" if user.get("portal_id") else f"e:{user.get('employee_id') or 0}")
    row = db.query(MaterialView).filter(MaterialView.material_id == m.id, MaterialView.viewer_key == key).first()
    if row is None:
        db.add(MaterialView(company_id=m.company_id, material_id=m.id, viewer_key=key,
                            student_id=student_id if user.get("role") == "student" else None))
        m.view_count = (m.view_count or 0) + 1
    else:
        row.views = (row.views or 0) + 1
        row.last_viewed_at = utc_now()
    db.flush()


def material_viewers(db: Session, m: Material) -> list[dict]:
    rows = db.query(MaterialView).filter(MaterialView.material_id == m.id).order_by(MaterialView.last_viewed_at.desc()).all()
    names = {s.id: s.full_name for s in db.query(Student).filter(
        Student.id.in_([r.student_id for r in rows if r.student_id] or [-1])).all()}
    return [{"name": names.get(r.student_id) or ("Parent" if r.viewer_key.startswith("p:") else "Staff"),
             "student_id": r.student_id, "views": r.views, "first_viewed_at": iso_utc(r.first_viewed_at),
             "last_viewed_at": iso_utc(r.last_viewed_at)} for r in rows]


def require_assignment_visible(db: Session, user: dict, a: Assignment) -> None:
    require_section_visible(db, user, [l.section_id for l in a.sections])


def student_by_uuid(db: Session, company_id: int, uuid) -> Student:
    s = db.query(Student).filter(Student.company_id == company_id, Student.uuid == uuid).first()
    if s is None:
        raise NotFound("Student")
    return s


def http_error(status: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)
