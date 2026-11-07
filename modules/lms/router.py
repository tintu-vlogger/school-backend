"""Teaching and learning API (/lms): homework, study materials, online classes, quizzes,
syllabus and lesson plans for staff, and /lms/portal for parents and students."""
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from core import audit
from core.db import get_db
from core.errors import BadRequest, Forbidden, NotFound
from core.pagination import PageParams
from core.security import require_permission, require_portal
from core.timeutil import iso_utc, utc_now
from modules.lms import live, quiz, service as svc, syllabus
from modules.lms.models import (Assignment, AssignmentSection, LessonPlan, LiveClass, Material, MaterialSection, BankQuestion,
                                Quiz, QuizAttempt, QuizSection, Submission)

router = APIRouter(prefix="/lms", tags=["Teaching & learning"])
portal = APIRouter(prefix="/lms/portal", tags=["Teaching & learning portal"])
extra_routers = [portal]

VIEW = require_permission(svc.VIEW)
PORTAL = require_portal("parent", "student")
STUDENT = require_portal("student")


def _page(query, params: PageParams, convert):
    total = query.order_by(None).count()
    rows = query.offset(params.offset).limit(params.size).all()
    return {"items": convert(rows), "total": total, "page": params.page, "size": params.size}


# ── Options, settings, uploads, summary ──────────────────────────────

@router.get("/options")
def options(db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    """Section + subject pairs the person may use, plus what they may do."""
    p = svc.perms(db, user)
    cfg = svc.settings(db, user["company_id"])
    return {"allocations": svc.allocations(db, user), "can_manage": svc.MANAGE in p,
            "can_teach": svc.MANAGE in p or svc.TEACH in p, "can_settings": svc.SETTINGS in p,
            "employee_id": user.get("employee_id"),
            "settings": {k: cfg[k] for k in ("video_provider", "jitsi_domain", "homework_allow_late",
                                             "homework_default_due_hour", "quiz_default_time_limit", "quiz_show_answers",
                                             "join_early_minutes")}}


class SettingsIn(BaseModel):
    video_provider: str | None = None
    jitsi_domain: str | None = Field(None, max_length=120)
    jitsi_room_prefix: str | None = Field(None, max_length=20)
    reminder_minutes: int | None = Field(None, ge=0, le=240)
    join_early_minutes: int | None = Field(None, ge=0, le=120)
    parents_can_join: bool | None = None
    homework_allow_late: bool | None = None
    homework_notify_parents: bool | None = None
    homework_default_due_hour: int | None = Field(None, ge=0, le=23)
    quiz_default_time_limit: int | None = Field(None, ge=0, le=240)
    quiz_show_answers: str | None = None


@router.get("/settings")
def get_settings(db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    return svc.settings(db, user["company_id"])


@router.put("/settings")
def put_settings(payload: SettingsIn, db: Session = Depends(get_db),
                 user: dict = Depends(require_permission(svc.SETTINGS))):
    out = svc.save_settings(db, user["company_id"], payload.model_dump(exclude_unset=True))
    db.commit()
    return out


@router.post("/uploads", status_code=201)
async def upload(file: UploadFile = File(...), db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    from modules.documents import storage
    blob = storage.store(db, user["company_id"], file.filename or "file", await file.read(), file.content_type, "lms")
    db.commit()
    return storage.describe(blob)


@router.get("/summary")
def summary(db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    """Home card: the signed-in teacher's live classes today and work waiting to be marked."""
    cid, emp = user["company_id"], user.get("employee_id")
    start, end = svc.day_bounds(db, cid, svc.local_today(db, cid))
    cq = live.query(db, user).filter(LiveClass.starts_at >= start, LiveClass.starts_at < end,
                                     LiveClass.status != "cancelled")
    if emp and not svc.is_manager(db, user):
        cq = cq.filter(LiveClass.teacher_id == emp)
    elif emp:
        mine = cq.filter(LiveClass.teacher_id == emp)
        cq = mine if mine.count() else cq
    cfg = svc.settings(db, cid)
    classes = [live.out(db, c, cfg) for c in cq.order_by(LiveClass.starts_at).limit(8).all()]
    aq = svc.assignment_query(db, user).filter(Assignment.status != "draft")
    if emp:
        aq = aq.filter(Assignment.created_by_employee_id == emp) if aq.filter(Assignment.created_by_employee_id == emp).first() else aq
    pending = (db.query(Assignment, func.count(Submission.id)).join(Submission, Submission.assignment_id == Assignment.id)
               .filter(Assignment.id.in_(aq.with_entities(Assignment.id)), Submission.status.in_(("submitted", "offline")))
               .group_by(Assignment.id).order_by(Assignment.due_at.desc()).limit(6).all())
    qq = quiz.quiz_query(db, user)
    if emp:
        qq = qq.filter(Quiz.created_by_employee_id == emp) if qq.filter(Quiz.created_by_employee_id == emp).first() else qq
    to_mark = db.query(func.count(QuizAttempt.id)).filter(QuizAttempt.quiz_id.in_(qq.with_entities(Quiz.id)),
                                                          QuizAttempt.needs_marking.is_(True)).scalar()
    return {"classes_today": classes,
            "to_grade": [{"uuid": str(a.uuid), "title": a.title, "count": n, "due_at": iso_utc(a.due_at),
                          "subject_name": a.subject.name if a.subject else None} for a, n in pending],
            "to_grade_total": sum(n for _, n in pending), "quiz_attempts_to_mark": to_mark or 0}


# ── Homework ─────────────────────────────────────────────────────────

class HomeworkIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    instructions: str | None = Field(None, max_length=20000)
    kind: str = Field("homework", pattern="^(homework|assignment|project)$")
    subject_id: int
    section_ids: list[int] = Field(..., min_length=1, max_length=40)
    attachments: list[str] = []
    due_at: datetime
    max_marks: Decimal | None = Field(None, ge=0, le=10000)
    submission_type: str = Field("online", pattern="^(online|offline)$")
    allow_late: bool = True
    status: str = Field("published", pattern="^(draft|published)$")


class HomeworkPatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    instructions: str | None = Field(None, max_length=20000)
    kind: str | None = Field(None, pattern="^(homework|assignment|project)$")
    subject_id: int | None = None
    section_ids: list[int] | None = Field(None, min_length=1, max_length=40)
    attachments: list[str] | None = None
    due_at: datetime | None = None
    max_marks: Decimal | None = Field(None, ge=0, le=10000)
    submission_type: str | None = Field(None, pattern="^(online|offline)$")
    allow_late: bool | None = None
    status: str | None = Field(None, pattern="^(draft|published|closed)$")


@router.get("/homework")
def list_homework(q: str | None = None, section_id: int | None = None, subject_id: int | None = None,
                  status: str | None = None, mine: bool = False, due: str | None = Query(None, pattern="^(upcoming|past)$"),
                  params: PageParams = Depends(), db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    query = svc.assignment_query(db, user)
    if q:
        query = query.filter(Assignment.title.ilike(f"%{q.strip()}%"))
    if section_id:
        query = query.filter(Assignment.id.in_(db.query(AssignmentSection.assignment_id).filter(AssignmentSection.section_id == section_id)))
    if subject_id:
        query = query.filter(Assignment.subject_id == subject_id)
    if status:
        query = query.filter(Assignment.status == status)
    if mine:
        query = query.filter(Assignment.created_by_employee_id == user.get("employee_id"))
    if due == "upcoming":
        query = query.filter(Assignment.due_at >= utc_now())
    elif due == "past":
        query = query.filter(Assignment.due_at < utc_now())
    query = query.order_by(Assignment.due_at.desc(), Assignment.id.desc())

    def convert(rows):
        labels = svc.section_labels(db, user["company_id"], [l.section_id for a in rows for l in a.sections])
        counts = svc.assignment_counts(db, rows)
        return [svc.assignment_out(db, a, labels, counts) for a in rows]
    return _page(query, params, convert)


@router.post("/homework", status_code=201)
def create_homework(payload: HomeworkIn, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    a = svc.save_assignment(db, user, payload.model_dump())
    db.commit()
    return svc.assignment_out(db, a, links=True)


@router.get("/homework/{uuid}")
def get_homework(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    a = svc.get_assignment(db, user, uuid)
    return svc.assignment_out(db, a, counts=svc.assignment_counts(db, [a]), links=True)


@router.put("/homework/{uuid}")
def update_homework(uuid: UUID, payload: HomeworkPatch, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    a = svc.get_assignment(db, user, uuid)
    a = svc.save_assignment(db, user, payload.model_dump(exclude_unset=True), a)
    db.commit()
    return svc.assignment_out(db, a, links=True)


@router.delete("/homework/{uuid}")
def archive_homework(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    a = svc.get_assignment(db, user, uuid)
    svc.require_teaching(db, user, [l.section_id for l in a.sections], a.subject_id)
    a.archived_at = utc_now()
    db.commit()
    return {"detail": "Removed"}


@router.get("/homework/{uuid}/submissions")
def homework_submissions(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    a = svc.get_assignment(db, user, uuid)
    return svc.submissions_roster(db, a)


class GradeIn(BaseModel):
    marks: Decimal | None = Field(None, ge=0, le=10000)
    feedback: str | None = Field(None, max_length=5000)
    return_now: bool = True


@router.post("/homework/{uuid}/submissions/{student_uuid}/grade")
def grade_submission(uuid: UUID, student_uuid: UUID, payload: GradeIn, db: Session = Depends(get_db),
                     user: dict = Depends(VIEW)):
    a = svc.get_assignment(db, user, uuid)
    s = svc.student_by_uuid(db, user["company_id"], student_uuid)
    sub = svc.grade(db, user, a, s, payload.marks, payload.feedback, payload.return_now)
    db.commit()
    return svc.submission_out(db, sub, links=True)


class NoteIn(BaseModel):
    note: str | None = Field(None, max_length=2000)


@router.post("/homework/{uuid}/submissions/{student_uuid}/resubmit")
def ask_resubmit(uuid: UUID, student_uuid: UUID, payload: NoteIn, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    a = svc.get_assignment(db, user, uuid)
    s = svc.student_by_uuid(db, user["company_id"], student_uuid)
    sub = svc.ask_resubmit(db, user, a, s, payload.note)
    db.commit()
    return svc.submission_out(db, sub, links=True)


@router.post("/homework/{uuid}/return")
def return_homework(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    a = svc.get_assignment(db, user, uuid)
    n = svc.return_all(db, user, a)
    db.commit()
    return {"returned": n}


# ── Study materials ──────────────────────────────────────────────────

class MaterialIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(None, max_length=50000)
    kind: str = Field("file", pattern="^(file|link|video|note)$")
    url: str | None = Field(None, max_length=2000)
    attachments: list[str] = []
    program_id: int
    subject_id: int
    unit_id: int | None = None
    topic_id: int | None = None
    topic: str | None = Field(None, max_length=200)
    section_ids: list[int] = []
    published: bool = True


class MaterialPatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=50000)
    kind: str | None = Field(None, pattern="^(file|link|video|note)$")
    url: str | None = Field(None, max_length=2000)
    attachments: list[str] | None = None
    program_id: int | None = None
    subject_id: int | None = None
    unit_id: int | None = None
    topic_id: int | None = None
    topic: str | None = Field(None, max_length=200)
    section_ids: list[int] | None = None
    published: bool | None = None


@router.get("/materials")
def list_materials(q: str | None = None, program_id: int | None = None, subject_id: int | None = None,
                   section_id: int | None = None, kind: str | None = None, unit_id: int | None = None,
                   params: PageParams = Depends(), db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    query = svc.material_query(db, user)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Material.title.ilike(like), Material.topic.ilike(like), Material.description.ilike(like)))
    if program_id:
        query = query.filter(Material.program_id == program_id)
    if subject_id:
        query = query.filter(Material.subject_id == subject_id)
    if kind:
        query = query.filter(Material.kind == kind)
    if unit_id:
        query = query.filter(Material.unit_id == unit_id)
    if section_id:
        from modules.academics.models import Section
        sec = db.query(Section).filter(Section.company_id == user["company_id"], Section.id == section_id).first()
        restricted = db.query(MaterialSection.material_id)
        mine = db.query(MaterialSection.material_id).filter(MaterialSection.section_id == section_id)
        query = query.filter(Material.program_id == (sec.program_id if sec else -1),
                             or_(~Material.id.in_(restricted), Material.id.in_(mine)))
    query = query.order_by(Material.created_at.desc())

    def convert(rows):
        labels = svc.section_labels(db, user["company_id"], [l.section_id for m in rows for l in m.sections])
        return [svc.material_out(db, m, labels) for m in rows]
    return _page(query, params, convert)


@router.post("/materials", status_code=201)
def create_material(payload: MaterialIn, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    m = svc.save_material(db, user, payload.model_dump())
    db.commit()
    return svc.material_out(db, m, links=True)


@router.get("/materials/{uuid}")
def get_material(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    return svc.material_out(db, svc.get_material(db, user, uuid), links=True)


@router.put("/materials/{uuid}")
def update_material(uuid: UUID, payload: MaterialPatch, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    m = svc.save_material(db, user, payload.model_dump(exclude_unset=True), svc.get_material(db, user, uuid))
    db.commit()
    return svc.material_out(db, m, links=True)


@router.delete("/materials/{uuid}")
def archive_material(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    m = svc.get_material(db, user, uuid)
    if not svc.is_manager(db, user) and m.created_by_employee_id != user.get("employee_id"):
        raise Forbidden("Only the person who shared it can remove it.")
    m.archived_at = utc_now()
    db.commit()
    return {"detail": "Removed"}


@router.get("/materials/{uuid}/views")
def material_views(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    return svc.material_viewers(db, svc.get_material(db, user, uuid))


# ── Online classes ───────────────────────────────────────────────────

class LiveIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    agenda: str | None = Field(None, max_length=5000)
    section_id: int
    subject_id: int | None = None
    teacher_id: int | None = None
    starts_at: datetime
    duration_min: int = Field(40, ge=5, le=480)
    provider: str | None = None
    join_url: str | None = Field(None, max_length=2000)
    meeting_code: str | None = Field(None, max_length=120)
    passcode: str | None = Field(None, max_length=60)
    notes: str | None = Field(None, max_length=5000)
    repeat: str = Field("none", pattern="^(none|weekly)$")
    repeat_until: date | None = None
    weekdays: list[int] | None = None


class LivePatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    agenda: str | None = Field(None, max_length=5000)
    section_id: int | None = None
    subject_id: int | None = None
    teacher_id: int | None = None
    starts_at: datetime | None = None
    duration_min: int | None = Field(None, ge=5, le=480)
    provider: str | None = None
    join_url: str | None = Field(None, max_length=2000)
    meeting_code: str | None = Field(None, max_length=120)
    passcode: str | None = Field(None, max_length=60)
    notes: str | None = Field(None, max_length=5000)
    recording_url: str | None = Field(None, max_length=2000)


@router.get("/online-classes")
def list_classes(when: str = Query("upcoming", pattern="^(today|upcoming|past|all)$"), q: str | None = None,
                 section_id: int | None = None, subject_id: int | None = None, mine: bool = False,
                 status: str | None = None, params: PageParams = Depends(), db: Session = Depends(get_db),
                 user: dict = Depends(VIEW)):
    cid = user["company_id"]
    query = live.query(db, user)
    now = utc_now()
    if when == "today":
        start, end = svc.day_bounds(db, cid, svc.local_today(db, cid))
        query = query.filter(LiveClass.starts_at >= start, LiveClass.starts_at < end).order_by(LiveClass.starts_at)
    elif when == "upcoming":
        query = query.filter(LiveClass.ends_at >= now - timedelta(minutes=30), LiveClass.status != "cancelled",
                             LiveClass.status != "completed").order_by(LiveClass.starts_at)
    elif when == "past":
        query = query.filter(or_(LiveClass.ends_at < now - timedelta(minutes=30), LiveClass.status == "completed")
                             ).order_by(LiveClass.starts_at.desc())
    else:
        query = query.order_by(LiveClass.starts_at.desc())
    if q:
        query = query.filter(LiveClass.title.ilike(f"%{q.strip()}%"))
    if section_id:
        query = query.filter(LiveClass.section_id == section_id)
    if subject_id:
        query = query.filter(LiveClass.subject_id == subject_id)
    if mine:
        query = query.filter(LiveClass.teacher_id == user.get("employee_id"))
    if status:
        query = query.filter(LiveClass.status == status)
    cfg = svc.settings(db, cid)

    def convert(rows):
        joins = live.join_counts(db, [c.id for c in rows])
        return [live.out(db, c, cfg, joins) for c in rows]
    return _page(query, params, convert)


@router.post("/online-classes", status_code=201)
def create_class(payload: LiveIn, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    rows = live.create(db, user, payload.model_dump())
    db.commit()
    cfg = svc.settings(db, user["company_id"])
    return {"created": len(rows), "items": [live.out(db, c, cfg) for c in rows[:10]]}


@router.get("/online-classes/{uuid}")
def get_class(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    c = live.get(db, user, uuid)
    out = live.out(db, c, joins=live.join_counts(db, [c.id]))
    if c.series_uuid:
        out["series_count"] = db.query(func.count(LiveClass.id)).filter(LiveClass.series_uuid == c.series_uuid,
                                                                        LiveClass.status == "scheduled").scalar()
    return out


@router.put("/online-classes/{uuid}")
def update_class(uuid: UUID, payload: LivePatch, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    c = live.update(db, user, live.get(db, user, uuid), payload.model_dump(exclude_unset=True))
    db.commit()
    return live.out(db, c)


class CancelIn(BaseModel):
    reason: str | None = Field(None, max_length=300)
    whole_series: bool = False


@router.post("/online-classes/{uuid}/cancel")
def cancel_class(uuid: UUID, payload: CancelIn, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    n = live.cancel(db, user, live.get(db, user, uuid), payload.reason, payload.whole_series)
    db.commit()
    return {"cancelled": n}


@router.post("/online-classes/{uuid}/end")
def end_class(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    c = live.end(db, user, live.get(db, user, uuid))
    db.commit()
    return live.out(db, c)


@router.post("/online-classes/{uuid}/join")
def join_class(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    info = live.join(db, user, live.get(db, user, uuid))
    db.commit()
    return info


@router.get("/online-classes/{uuid}/attendance")
def class_attendance(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    return live.attendance(db, live.get(db, user, uuid))


# ── Question bank ────────────────────────────────────────────────────

class QuestionIn(BaseModel):
    subject_id: int
    program_id: int | None = None
    topic_id: int | None = None
    topic: str | None = Field(None, max_length=200)
    kind: str
    prompt: str = Field(..., min_length=1, max_length=10000)
    options: list[dict] = []
    answer: Any = None
    marks: Decimal = Field(Decimal("1"), gt=0, le=1000)
    negative_marks: Decimal = Field(Decimal("0"), ge=0, le=1000)
    difficulty: str = Field("medium", pattern="^(easy|medium|hard)$")
    tags: list[str] = []
    explanation: str | None = Field(None, max_length=5000)


class QuestionPatch(BaseModel):
    subject_id: int | None = None
    program_id: int | None = None
    topic_id: int | None = None
    topic: str | None = Field(None, max_length=200)
    kind: str | None = None
    prompt: str | None = Field(None, min_length=1, max_length=10000)
    options: list[dict] | None = None
    answer: Any = None
    marks: Decimal | None = Field(None, gt=0, le=1000)
    negative_marks: Decimal | None = Field(None, ge=0, le=1000)
    difficulty: str | None = Field(None, pattern="^(easy|medium|hard)$")
    tags: list[str] | None = None
    explanation: str | None = Field(None, max_length=5000)
    active: bool | None = None


@router.get("/questions")
def list_questions(q: str | None = None, subject_id: int | None = None, kind: str | None = None,
                   difficulty: str | None = None, tag: str | None = None, topic: str | None = None,
                   active: bool | None = True, params: PageParams = Depends(), db: Session = Depends(get_db),
                   user: dict = Depends(VIEW)):
    query = quiz.question_query(db, user)
    if q:
        query = query.filter(or_(BankQuestion.prompt.ilike(f"%{q.strip()}%"), BankQuestion.topic.ilike(f"%{q.strip()}%")))
    if subject_id:
        query = query.filter(BankQuestion.subject_id == subject_id)
    if kind:
        query = query.filter(BankQuestion.kind == kind)
    if difficulty:
        query = query.filter(BankQuestion.difficulty == difficulty)
    if tag:
        query = query.filter(BankQuestion.tags.contains([tag.lower()]))
    if topic:
        query = query.filter(BankQuestion.topic.ilike(topic))
    if active is not None:
        query = query.filter(BankQuestion.active.is_(active))
    query = query.order_by(BankQuestion.created_at.desc(), BankQuestion.id.desc())
    return _page(query, params, lambda rows: [quiz.question_out(x) for x in rows])


@router.get("/questions/facets")
def question_facets(subject_id: int | None = None, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    query = quiz.question_query(db, user).filter(BankQuestion.active.is_(True))
    if subject_id:
        query = query.filter(BankQuestion.subject_id == subject_id)
    rows = query.with_entities(BankQuestion.topic, BankQuestion.tags, BankQuestion.difficulty).all()
    topics = sorted({t for t, _, _ in rows if t})
    tags = sorted({x for _, tg, _ in rows for x in (tg or [])})
    diff: dict[str, int] = {}
    for _, _, d in rows:
        diff[d] = diff.get(d, 0) + 1
    return {"topics": topics, "tags": tags, "difficulty": diff, "total": len(rows)}


@router.post("/questions", status_code=201)
def create_question(payload: QuestionIn, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    row = quiz.save_question(db, user, payload.model_dump())
    db.commit()
    return quiz.question_out(row)


def _question(db, user, uuid) -> BankQuestion:
    row = quiz.question_query(db, user).filter(BankQuestion.uuid == uuid).first()
    if row is None:
        raise NotFound("Question")
    return row


@router.get("/questions/{uuid}")
def get_question(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    return quiz.question_out(_question(db, user, uuid))


@router.put("/questions/{uuid}")
def update_question(uuid: UUID, payload: QuestionPatch, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    row = quiz.save_question(db, user, payload.model_dump(exclude_unset=True), _question(db, user, uuid))
    db.commit()
    return quiz.question_out(row)


@router.delete("/questions/{uuid}")
def archive_question(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    row = _question(db, user, uuid)
    quiz.save_question(db, user, {"active": False}, row)
    db.commit()
    return {"detail": "Switched off"}


# ── Quizzes ──────────────────────────────────────────────────────────

class QuizItem(BaseModel):
    question_id: int
    marks: Decimal | None = Field(None, gt=0, le=1000)


class QuizIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    instructions: str | None = Field(None, max_length=10000)
    kind: str = Field("quiz", pattern="^(quiz|test)$")
    subject_id: int
    section_ids: list[int] = Field(..., min_length=1, max_length=40)
    mode: str = Field("fixed", pattern="^(fixed|random)$")
    random_rules: list[dict] = []
    questions: list[QuizItem] | None = None
    time_limit_min: int | None = Field(None, ge=1, le=600)
    attempts_allowed: int = Field(1, ge=1, le=20)
    shuffle_questions: bool = False
    shuffle_options: bool = False
    opens_at: datetime | None = None
    closes_at: datetime | None = None
    show_answers: str = Field("after_close", pattern="^(after_submit|after_close|never)$")
    pass_percent: Decimal | None = Field(None, ge=0, le=100)
    status: str = Field("draft", pattern="^(draft|published)$")


class QuizPatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    instructions: str | None = Field(None, max_length=10000)
    kind: str | None = Field(None, pattern="^(quiz|test)$")
    subject_id: int | None = None
    section_ids: list[int] | None = Field(None, min_length=1, max_length=40)
    mode: str | None = Field(None, pattern="^(fixed|random)$")
    random_rules: list[dict] | None = None
    questions: list[QuizItem] | None = None
    time_limit_min: int | None = Field(None, ge=1, le=600)
    attempts_allowed: int | None = Field(None, ge=1, le=20)
    shuffle_questions: bool | None = None
    shuffle_options: bool | None = None
    opens_at: datetime | None = None
    closes_at: datetime | None = None
    show_answers: str | None = Field(None, pattern="^(after_submit|after_close|never)$")
    pass_percent: Decimal | None = Field(None, ge=0, le=100)
    status: str | None = Field(None, pattern="^(draft|published|closed)$")


def _quiz_payload(payload: BaseModel, partial: bool) -> dict:
    data = payload.model_dump(exclude_unset=partial)
    if data.get("questions") is not None:
        data["questions"] = [dict(i) for i in data["questions"]]
    elif "questions" in data:
        data.pop("questions")
    return data


def _quiz_detail(db: Session, qz: Quiz) -> dict:
    out = quiz.quiz_out(db, qz, stats=quiz.quiz_stats(db, [qz]))
    out["questions"] = [{**quiz.question_out(qq.question), "quiz_marks": svc.dec(qq.marks), "sort_order": qq.sort_order}
                        for qq in qz.questions]
    out["has_attempts"] = db.query(QuizAttempt.id).filter(QuizAttempt.quiz_id == qz.id).first() is not None
    return out


@router.get("/quizzes")
def list_quizzes(q: str | None = None, section_id: int | None = None, subject_id: int | None = None,
                 status: str | None = None, mine: bool = False, params: PageParams = Depends(),
                 db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    query = quiz.quiz_query(db, user)
    if q:
        query = query.filter(Quiz.title.ilike(f"%{q.strip()}%"))
    if section_id:
        query = query.filter(Quiz.id.in_(db.query(QuizSection.quiz_id).filter(QuizSection.section_id == section_id)))
    if subject_id:
        query = query.filter(Quiz.subject_id == subject_id)
    if status:
        query = query.filter(Quiz.status == status)
    if mine:
        query = query.filter(Quiz.created_by_employee_id == user.get("employee_id"))
    query = query.order_by(Quiz.created_at.desc())

    def convert(rows):
        labels = svc.section_labels(db, user["company_id"], [l.section_id for x in rows for l in x.sections])
        stats = quiz.quiz_stats(db, rows)
        return [quiz.quiz_out(db, x, labels, stats) for x in rows]
    return _page(query, params, convert)


@router.post("/quizzes", status_code=201)
def create_quiz(payload: QuizIn, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    qz = quiz.save_quiz(db, user, _quiz_payload(payload, False))
    db.commit()
    return _quiz_detail(db, qz)


@router.get("/quizzes/{uuid}")
def get_quiz(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    return _quiz_detail(db, quiz.get_quiz(db, user, uuid))


@router.put("/quizzes/{uuid}")
def update_quiz(uuid: UUID, payload: QuizPatch, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    qz = quiz.get_quiz(db, user, uuid)
    was = qz.status
    qz = quiz.save_quiz(db, user, _quiz_payload(payload, True), qz)
    if was != "published" and qz.status == "published":
        quiz.audit_publish(db, qz)
    db.commit()
    return _quiz_detail(db, qz)


@router.delete("/quizzes/{uuid}")
def archive_quiz(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    qz = quiz.get_quiz(db, user, uuid)
    svc.require_teaching(db, user, [l.section_id for l in qz.sections], qz.subject_id)
    qz.archived_at = utc_now()
    db.commit()
    return {"detail": "Removed"}


@router.get("/quizzes/{uuid}/results")
def quiz_results(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    return quiz.results(db, quiz.get_quiz(db, user, uuid))


def _staff_attempt(db: Session, user: dict, uuid) -> QuizAttempt:
    a = db.query(QuizAttempt).filter(QuizAttempt.company_id == user["company_id"], QuizAttempt.uuid == uuid).first()
    if a is None:
        raise NotFound("Attempt")
    quiz.get_quiz(db, user, a.quiz.uuid)  # scope check
    return a


@router.get("/quiz-attempts/{uuid}")
def staff_attempt(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    a = _staff_attempt(db, user, uuid)
    out = quiz.attempt_out(db, a, for_learner=False)
    s = quiz.student_of_attempt(db, a)
    out["student_name"] = s.full_name if s else None
    out["quiz"] = {"uuid": str(a.quiz.uuid), "title": a.quiz.title}
    return out


class MarkIn(BaseModel):
    items: dict[str, dict]


@router.post("/quiz-attempts/{uuid}/mark")
def mark_attempt(uuid: UUID, payload: MarkIn, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    a = _staff_attempt(db, user, uuid)
    svc.require_teaching(db, user, [l.section_id for l in a.quiz.sections], a.quiz.subject_id)
    quiz.mark_manually(db, user, a, payload.items)
    db.commit()
    return quiz.attempt_out(db, a, for_learner=False)


# ── Syllabus and coverage ────────────────────────────────────────────

@router.get("/syllabus")
def get_syllabus(program_id: int, subject_id: int, term_no: int | None = None, section_id: int | None = None,
                 db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    if section_id:
        svc.require_section_visible(db, user, [section_id])
    rows = syllabus.units(db, user["company_id"], program_id, subject_id, term_no)
    return syllabus.syllabus_out(db, user["company_id"], rows, section_id)


class TopicIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(None, max_length=5000)
    planned_periods: int | None = Field(None, ge=0, le=500)
    sort_order: int | None = None


class TopicPatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=5000)
    planned_periods: int | None = Field(None, ge=0, le=500)
    sort_order: int | None = None


class UnitIn(BaseModel):
    program_id: int
    subject_id: int
    term_no: int | None = Field(None, ge=1, le=20)
    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(None, max_length=5000)
    sort_order: int | None = None
    topics: list[TopicIn] = []


class UnitPatch(BaseModel):
    term_no: int | None = Field(None, ge=1, le=20)
    title: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=5000)
    sort_order: int | None = None


@router.post("/syllabus/units", status_code=201)
def create_unit(payload: UnitIn, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    data = payload.model_dump()
    data["topics"] = [t for t in data["topics"]]
    if data.get("sort_order") is None:
        data.pop("sort_order")
    u = syllabus.save_unit(db, user, data)
    db.commit()
    return {"uuid": str(u.uuid), "id": u.id}


@router.put("/syllabus/units/{uuid}")
def update_unit(uuid: UUID, payload: UnitPatch, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    u = syllabus.save_unit(db, user, payload.model_dump(exclude_unset=True), syllabus.get_unit(db, user["company_id"], uuid))
    db.commit()
    return {"uuid": str(u.uuid), "id": u.id}


@router.delete("/syllabus/units/{uuid}")
def archive_unit(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    syllabus.save_unit(db, user, {"active": False}, syllabus.get_unit(db, user["company_id"], uuid))
    db.commit()
    return {"detail": "Removed"}


@router.post("/syllabus/units/{uuid}/topics", status_code=201)
def create_topic(uuid: UUID, payload: TopicIn, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    u = syllabus.get_unit(db, user["company_id"], uuid)
    data = payload.model_dump()
    if data.get("sort_order") is None:
        data.pop("sort_order")
    t = syllabus.save_topic(db, user, u, data)
    db.commit()
    return {"uuid": str(t.uuid), "id": t.id}


@router.put("/syllabus/topics/{uuid}")
def update_topic(uuid: UUID, payload: TopicPatch, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    t = syllabus.get_topic(db, user["company_id"], uuid)
    syllabus.save_topic(db, user, t.unit, payload.model_dump(exclude_unset=True), t)
    db.commit()
    return {"uuid": str(t.uuid), "id": t.id}


@router.delete("/syllabus/topics/{uuid}")
def archive_topic(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    t = syllabus.get_topic(db, user["company_id"], uuid)
    syllabus.save_topic(db, user, t.unit, {"active": False}, t)
    db.commit()
    return {"detail": "Removed"}


class CoverageIn(BaseModel):
    section_id: int
    covered: bool = True
    covered_on: date | None = None
    note: str | None = Field(None, max_length=300)


@router.post("/syllabus/topics/{uuid}/coverage")
def topic_coverage(uuid: UUID, payload: CoverageIn, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    t = syllabus.get_topic(db, user["company_id"], uuid)
    syllabus.set_coverage(db, user, t, payload.section_id, payload.covered, payload.covered_on, payload.note)
    db.commit()
    return {"covered": payload.covered}


@router.get("/coverage")
def coverage(program_id: int | None = None, subject_id: int | None = None, db: Session = Depends(get_db),
             user: dict = Depends(VIEW)):
    return syllabus.coverage_summary(db, user, program_id, subject_id)


# ── Lesson plans ─────────────────────────────────────────────────────

class PlanIn(BaseModel):
    section_id: int
    subject_id: int
    teacher_id: int | None = None
    week_start: date
    title: str | None = Field(None, max_length=200)
    topic_ids: list[int] = []
    objectives: str | None = Field(None, max_length=10000)
    activities: str | None = Field(None, max_length=10000)
    resources: str | None = Field(None, max_length=10000)
    assessment: str | None = Field(None, max_length=10000)
    homework_id: int | None = None
    status: str = Field("planned", pattern="^(planned|done|skipped)$")
    reflection: str | None = Field(None, max_length=5000)
    mark_topics_covered: bool = False


class PlanPatch(BaseModel):
    section_id: int | None = None
    subject_id: int | None = None
    week_start: date | None = None
    title: str | None = Field(None, max_length=200)
    topic_ids: list[int] | None = None
    objectives: str | None = Field(None, max_length=10000)
    activities: str | None = Field(None, max_length=10000)
    resources: str | None = Field(None, max_length=10000)
    assessment: str | None = Field(None, max_length=10000)
    homework_id: int | None = None
    status: str | None = Field(None, pattern="^(planned|done|skipped)$")
    reflection: str | None = Field(None, max_length=5000)
    mark_topics_covered: bool = False


@router.get("/lesson-plans")
def list_plans(q: str | None = None, section_id: int | None = None, subject_id: int | None = None,
               week: date | None = None, status: str | None = None, mine: bool = False,
               params: PageParams = Depends(), db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    query = syllabus.plan_query(db, user)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(LessonPlan.title.ilike(like), LessonPlan.objectives.ilike(like)))
    if section_id:
        query = query.filter(LessonPlan.section_id == section_id)
    if subject_id:
        query = query.filter(LessonPlan.subject_id == subject_id)
    if week:
        query = query.filter(LessonPlan.week_start == syllabus.monday(week))
    if status:
        query = query.filter(LessonPlan.status == status)
    if mine:
        query = query.filter(LessonPlan.teacher_id == user.get("employee_id"))
    query = query.order_by(LessonPlan.week_start.desc(), LessonPlan.id.desc())

    def convert(rows):
        from modules.lms.models import SyllabusTopic
        ids = {i for p in rows for i in (p.topic_ids or [])}
        topics = {t.id: t.title for t in db.query(SyllabusTopic).filter(SyllabusTopic.id.in_(ids or [-1])).all()}
        return [syllabus.plan_out(db, p, topics) for p in rows]
    return _page(query, params, convert)


@router.post("/lesson-plans", status_code=201)
def create_plan(payload: PlanIn, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    p = syllabus.save_plan(db, user, payload.model_dump())
    db.commit()
    return syllabus.plan_out(db, p)


@router.get("/lesson-plans/{uuid}")
def get_plan(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    return syllabus.plan_out(db, syllabus.get_plan(db, user, uuid))


@router.put("/lesson-plans/{uuid}")
def update_plan(uuid: UUID, payload: PlanPatch, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    p = syllabus.save_plan(db, user, payload.model_dump(exclude_unset=True), syllabus.get_plan(db, user, uuid))
    db.commit()
    return syllabus.plan_out(db, p)


@router.delete("/lesson-plans/{uuid}")
def archive_plan(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    p = syllabus.get_plan(db, user, uuid)
    svc.require_teaching(db, user, [p.section_id], p.subject_id)
    p.archived_at = utc_now()
    db.commit()
    return {"detail": "Removed"}


# ══ Portal: parents and students ═════════════════════════════════════

@portal.get("/summary")
def portal_summary(student_id: int | None = None, db: Session = Depends(get_db), user: dict = Depends(PORTAL)):
    """Home card: homework due soon and the next live class."""
    learner = svc.portal_student(db, user, student_id)
    now = utc_now()
    due = [(a, s, st) for a, s, st in svc.portal_assignments(db, learner, "due")
           if svc.as_utc(a.due_at) >= now - timedelta(days=1)]
    due.sort(key=lambda r: r[0].due_at)
    upcoming = live.portal_list(db, learner, "upcoming", limit=3)
    cfg = svc.settings(db, user["company_id"])
    quizzes = [x for x in quiz.portal_quizzes(db, learner) if quiz.quiz_window(x[0]) == "open"
               and len(x[1]) < (x[0].attempts_allowed or 1)]
    db.commit()  # expired attempts may have been closed
    return {"student_id": learner.student.id,
            "homework_due": [svc.portal_assignment_out(db, a, s, st) for a, s, st in due[:5]],
            "homework_due_total": len(due),
            "next_class": live.out(db, upcoming[0], cfg, portal=True) if upcoming else None,
            "open_quizzes": len(quizzes), "can_join": user.get("role") == "student" or cfg["parents_can_join"]}


@portal.get("/subjects")
def portal_subjects(student_id: int | None = None, db: Session = Depends(get_db), user: dict = Depends(PORTAL)):
    from modules.academics.models import SectionSubject
    learner = svc.portal_student(db, user, student_id)
    rows = db.query(SectionSubject).filter(SectionSubject.section_id == (learner.section_id or -1)).all()
    return [{"id": r.subject_id, "name": r.subject.name} for r in sorted(rows, key=lambda r: r.subject.name)]


@portal.post("/uploads", status_code=201)
async def portal_upload(file: UploadFile = File(...), db: Session = Depends(get_db), user: dict = Depends(STUDENT)):
    from modules.documents import storage
    blob = storage.store(db, user["company_id"], file.filename or "file", await file.read(), file.content_type, "lms")
    db.commit()
    return storage.describe(blob)


@portal.get("/homework")
def portal_homework(student_id: int | None = None, status: str | None = Query(None, pattern="^(due|done)$"),
                    subject_id: int | None = None, db: Session = Depends(get_db), user: dict = Depends(PORTAL)):
    learner = svc.portal_student(db, user, student_id)
    rows = svc.portal_assignments(db, learner, status)
    if subject_id:
        rows = [r for r in rows if r[0].subject_id == subject_id]
    return {"student_id": learner.student.id, "items": [svc.portal_assignment_out(db, a, s, st) for a, s, st in rows]}


def _portal_assignment(db: Session, learner: svc.Learner, uuid) -> Assignment:
    a = (db.query(Assignment).join(AssignmentSection, AssignmentSection.assignment_id == Assignment.id)
         .filter(Assignment.company_id == learner.student.company_id, Assignment.uuid == uuid,
                 AssignmentSection.section_id == (learner.section_id or -1), Assignment.archived_at.is_(None),
                 Assignment.status.in_(("published", "closed"))).first())
    if a is None:
        raise NotFound("Homework")
    return a


@portal.get("/homework/{uuid}")
def portal_homework_detail(uuid: UUID, student_id: int | None = None, db: Session = Depends(get_db),
                           user: dict = Depends(PORTAL)):
    learner = svc.portal_student(db, user, student_id)
    a = _portal_assignment(db, learner, uuid)
    sub = db.query(Submission).filter(Submission.assignment_id == a.id, Submission.student_id == learner.student.id).first()
    out = svc.portal_assignment_out(db, a, sub, svc.submission_state(a, sub), full=True)
    if user.get("role") != "student":
        out["can_submit"] = False
    return out


class SubmitIn(BaseModel):
    content: str | None = Field(None, max_length=50000)
    attachments: list[str] = Field([], max_length=10)


@portal.post("/homework/{uuid}/submit")
def portal_submit(uuid: UUID, payload: SubmitIn, db: Session = Depends(get_db), user: dict = Depends(STUDENT)):
    """Students hand in their own work only (the learner comes from the sign-in, never the request)."""
    learner = svc.portal_student(db, user, user.get("student_id"))
    a = _portal_assignment(db, learner, uuid)
    sub = svc.submit(db, user, a, learner.student, learner.section_id, payload.content, payload.attachments)
    db.commit()
    return svc.portal_assignment_out(db, a, sub, svc.submission_state(a, sub), full=True)


@portal.get("/materials")
def portal_materials(student_id: int | None = None, subject_id: int | None = None, q: str | None = None,
                     kind: str | None = None, db: Session = Depends(get_db), user: dict = Depends(PORTAL)):
    learner = svc.portal_student(db, user, student_id)
    query = svc.portal_materials_query(db, learner)
    if query is None:
        return {"student_id": learner.student.id, "items": []}
    if subject_id:
        query = query.filter(Material.subject_id == subject_id)
    if kind:
        query = query.filter(Material.kind == kind)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Material.title.ilike(like), Material.topic.ilike(like)))
    rows = query.order_by(Material.created_at.desc()).limit(300).all()
    items = []
    for m in rows:
        d = svc.material_out(db, m, labels={})
        for k in ("view_count", "created_by_employee_id", "sections", "section_ids"):
            d.pop(k, None)
        items.append(d)
    return {"student_id": learner.student.id, "items": items}


@portal.get("/materials/{uuid}")
def portal_material(uuid: UUID, student_id: int | None = None, db: Session = Depends(get_db), user: dict = Depends(PORTAL)):
    learner = svc.portal_student(db, user, student_id)
    query = svc.portal_materials_query(db, learner)
    m = query.filter(Material.uuid == uuid).first() if query is not None else None
    if m is None:
        raise NotFound("Material")
    svc.record_view(db, user, m, learner.student.id)
    db.commit()
    d = svc.material_out(db, m, labels={}, links=True)
    for k in ("view_count", "created_by_employee_id", "sections", "section_ids"):
        d.pop(k, None)
    return d


@portal.get("/online-classes")
def portal_classes(student_id: int | None = None, when: str = Query("upcoming", pattern="^(upcoming|past)$"),
                   db: Session = Depends(get_db), user: dict = Depends(PORTAL)):
    learner = svc.portal_student(db, user, student_id)
    rows = live.portal_list(db, learner, when, limit=100)
    cfg = svc.settings(db, user["company_id"])
    joined = live.portal_joined(db, learner, [c.id for c in rows])
    items = []
    for c in rows:
        d = live.out(db, c, cfg, portal=True)
        d["attended"] = c.id in joined
        items.append(d)
    return {"student_id": learner.student.id, "items": items,
            "can_join": user.get("role") == "student" or cfg["parents_can_join"]}


def _portal_class(db: Session, learner: svc.Learner, uuid) -> LiveClass:
    c = db.query(LiveClass).filter(LiveClass.company_id == learner.student.company_id, LiveClass.uuid == uuid,
                                   LiveClass.section_id == (learner.section_id or -1)).first()
    if c is None:
        raise NotFound("Class")
    return c


@portal.get("/online-classes/{uuid}")
def portal_class(uuid: UUID, student_id: int | None = None, db: Session = Depends(get_db), user: dict = Depends(PORTAL)):
    learner = svc.portal_student(db, user, student_id)
    c = _portal_class(db, learner, uuid)
    cfg = svc.settings(db, user["company_id"])
    d = live.out(db, c, cfg, portal=True)
    d["can_join"] = user.get("role") == "student" or cfg["parents_can_join"]
    return d


@portal.post("/online-classes/{uuid}/join")
def portal_join(uuid: UUID, student_id: int | None = None, db: Session = Depends(get_db), user: dict = Depends(PORTAL)):
    learner = svc.portal_student(db, user, user.get("student_id") if user.get("role") == "student" else student_id)
    info = live.join(db, user, _portal_class(db, learner, uuid), learner)
    db.commit()
    return info


@portal.get("/quizzes")
def portal_quizzes(student_id: int | None = None, db: Session = Depends(get_db), user: dict = Depends(PORTAL)):
    learner = svc.portal_student(db, user, student_id)
    rows = quiz.portal_quizzes(db, learner)
    db.commit()
    items = [quiz.portal_quiz_out(db, qz, atts) for qz, atts in rows]
    if user.get("role") != "student":
        for i in items:
            i["can_start"] = False
    return {"student_id": learner.student.id, "items": items}


def _portal_quiz(db: Session, learner: svc.Learner, uuid) -> Quiz:
    qz = (db.query(Quiz).join(QuizSection, QuizSection.quiz_id == Quiz.id)
          .filter(Quiz.company_id == learner.student.company_id, Quiz.uuid == uuid, Quiz.archived_at.is_(None),
                  QuizSection.section_id == (learner.section_id or -1), Quiz.status.in_(("published", "closed"))).first())
    if qz is None:
        raise NotFound("Quiz")
    return qz


@portal.post("/quizzes/{uuid}/start")
def portal_start(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(STUDENT)):
    learner = svc.portal_student(db, user, user.get("student_id"))
    qz = _portal_quiz(db, learner, uuid)
    a = quiz.start_attempt(db, qz, learner)
    db.commit()
    out = quiz.attempt_out(db, a, for_learner=True)
    out["quiz"] = {"uuid": str(qz.uuid), "title": qz.title, "instructions": qz.instructions,
                   "time_limit_min": qz.time_limit_min, "subject_name": qz.subject.name if qz.subject else None}
    return out


def _portal_attempt(db: Session, user: dict, uuid, student_id: int | None) -> tuple[QuizAttempt, svc.Learner]:
    learner = svc.portal_student(db, user, user.get("student_id") if user.get("role") == "student" else student_id)
    a = db.query(QuizAttempt).filter(QuizAttempt.company_id == user["company_id"], QuizAttempt.uuid == uuid,
                                     QuizAttempt.student_id == learner.student.id).first()
    if a is None:
        raise NotFound("Attempt")
    return a, learner


@portal.get("/attempts/{uuid}")
def portal_attempt(uuid: UUID, student_id: int | None = None, db: Session = Depends(get_db), user: dict = Depends(PORTAL)):
    a, _ = _portal_attempt(db, user, uuid, student_id)
    if a.status == "in_progress" and a.deadline_at and utc_now() > svc.as_utc(a.deadline_at):
        quiz.finish(db, a)
        db.commit()
    if a.status == "in_progress" and user.get("role") != "student":
        raise BadRequest("This attempt is still in progress.")
    qz = a.quiz
    out = quiz.attempt_out(db, a, for_learner=True, show_answers=quiz.answers_visible(qz, a))
    out["quiz"] = {"uuid": str(qz.uuid), "title": qz.title, "instructions": qz.instructions,
                   "time_limit_min": qz.time_limit_min, "subject_name": qz.subject.name if qz.subject else None,
                   "show_answers": qz.show_answers, "window": quiz.quiz_window(qz)}
    return out


class AnswersIn(BaseModel):
    answers: dict[str, Any]


@portal.put("/attempts/{uuid}/answers")
def portal_save_answers(uuid: UUID, payload: AnswersIn, db: Session = Depends(get_db), user: dict = Depends(STUDENT)):
    a, _ = _portal_attempt(db, user, uuid, None)
    try:
        quiz.save_answers(db, a, payload.answers)
    except BadRequest:
        db.commit()  # keep the automatic submission when time ran out
        raise
    db.commit()
    return {"saved_at": iso_utc(utc_now()), "deadline_at": iso_utc(a.deadline_at)}


@portal.post("/attempts/{uuid}/submit")
def portal_submit_attempt(uuid: UUID, payload: AnswersIn | None = None, db: Session = Depends(get_db),
                          user: dict = Depends(STUDENT)):
    a, _ = _portal_attempt(db, user, uuid, None)
    if a.status == "in_progress" and payload and payload.answers:
        try:
            quiz.save_answers(db, a, payload.answers)
        except BadRequest:
            pass
    quiz.finish(db, a)
    audit.event(db, "lms.quiz_submitted", "lms_quiz_attempts", a.id, {"score": str(a.score)}, entity_label=a.quiz.title)
    db.commit()
    qz = a.quiz
    out = quiz.attempt_out(db, a, for_learner=True, show_answers=quiz.answers_visible(qz, a))
    out["quiz"] = {"uuid": str(qz.uuid), "title": qz.title, "show_answers": qz.show_answers, "window": quiz.quiz_window(qz)}
    return out
