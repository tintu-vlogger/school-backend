"""Learning API under /learning: catalogue, my learning, course player progress,
certificates, batches, question bank, assessments and attempts, paths, enrolment rules,
course requests and reports. Admin actions need `talent.learning.manage`."""
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session, selectinload

from core.db import get_db
from modules.learning import service as svc
from modules.learning.models import Assessment, CourseRequest, EnrolmentRule, LearningPath, Question
from modules.learning.schemas import (
    AnswersIn, AssessmentIn, AssignIn, AttendanceIn, BatchIn, CompleteBatchIn, CourseIn, FulfilIn, PathIn, QuestionIn,
    RequestIn, RuleIn, SeatIn, SettingsIn, StatusIn,
)
from modules.learning.service import Actor, get_actor

router = APIRouter(prefix="/learning", tags=["Learning"])


def _done(db: Session, value=None):
    db.commit()
    return value if value is not None else {"ok": True}


def _course_body(body: CourseIn) -> dict:
    data = body.model_dump(exclude_unset=True)
    if body.lessons is not None:
        data["lessons"] = [l.model_dump() for l in body.lessons]
    return data


# ── Settings ────────────────────────────────────────────────────────

@router.get("/settings")
def get_settings(db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return svc.config(db, actor.company_id)


@router.put("/settings")
def put_settings(body: SettingsIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return _done(db, svc.save_config(db, actor, body.model_dump(exclude_none=True)))


@router.get("/me")
def me(db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    team = svc._reportees(db, actor.company_id, actor.employee_id)
    return {"is_admin": actor.is_admin, "is_manager": bool(team), "employee_id": actor.employee_id}


# ── Courses ─────────────────────────────────────────────────────────

@router.get("/catalogue")
def catalogue(q: str | None = None, category_id: int | None = None, level: str | None = None, delivery: str | None = None,
              db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return svc.catalogue(db, actor, q, category_id, level, delivery)


@router.get("/courses")
def admin_courses(status: str | None = None, q: str | None = None, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return svc.admin_courses(db, actor, status, q)


@router.post("/courses", status_code=201)
def create_course(body: CourseIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    c = svc.save_course(db, actor, _course_body(body))
    db.commit()
    return svc.course_detail(db, actor, svc.get_course(db, actor.company_id, c.uuid))


@router.get("/courses/{uuid}")
def get_course(uuid: UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return svc.course_detail(db, actor, svc.get_course(db, actor.company_id, uuid))


@router.put("/courses/{uuid}")
def update_course(uuid: UUID, body: CourseIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    svc.save_course(db, actor, _course_body(body), svc.get_course(db, actor.company_id, uuid))
    db.commit()
    return svc.course_detail(db, actor, svc.get_course(db, actor.company_id, uuid))


@router.post("/courses/{uuid}/status")
def course_status(uuid: UUID, body: StatusIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    c = svc.get_course(db, actor.company_id, uuid)
    svc.save_course(db, actor, {"title": c.title, "status": body.status}, c)
    return _done(db)


@router.post("/courses/{uuid}/enrol", status_code=201)
def enrol(uuid: UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    e = svc.self_enrol(db, actor, svc.get_course(db, actor.company_id, uuid))
    return _done(db, svc.enrolment_brief(e))


@router.post("/courses/{uuid}/assign")
def assign(uuid: UUID, body: AssignIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    n = svc.assign(db, actor, svc.get_course(db, actor.company_id, uuid), body.employee_ids, body.due_date, body.mandatory)
    return _done(db, {"enrolled": n})


# ── My learning and the player ──────────────────────────────────────

@router.get("/my")
def my_learning(db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return svc.my_learning(db, actor)


@router.get("/enrolments")
def enrolments(course_id: int | None = None, status: str | None = None, department_id: int | None = None,
               overdue: bool = False, page: int = Query(1, ge=1), size: int = Query(25, ge=1, le=200),
               db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return svc.enrolments_report(db, actor, course_id, status, department_id, overdue, page, size)


@router.post("/enrolments/{uuid}/lessons/{lesson_uuid}/open")
def open_lesson(uuid: UUID, lesson_uuid: UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    svc.open_lesson(db, actor, svc.get_enrolment(db, actor, uuid), lesson_uuid)
    return _done(db)


@router.post("/enrolments/{uuid}/lessons/{lesson_uuid}/complete")
def complete_lesson(uuid: UUID, lesson_uuid: UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    e = svc.complete_lesson(db, actor, svc.get_enrolment(db, actor, uuid), lesson_uuid)
    db.commit()
    return svc.course_detail(db, actor, svc.get_course(db, actor.company_id, e.course.uuid))


@router.post("/enrolments/{uuid}/cancel")
def cancel_enrolment(uuid: UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    svc.cancel_enrolment(db, actor, svc.get_enrolment(db, actor, uuid))
    return _done(db)


@router.get("/enrolments/{uuid}/certificate")
def certificate(uuid: UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return svc.certificate(db, actor, svc.get_enrolment(db, actor, uuid))


# ── Paths and rules ─────────────────────────────────────────────────

@router.get("/paths")
def list_paths(db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    q = db.query(LearningPath).filter(LearningPath.company_id == actor.company_id)
    if not actor.is_admin:
        q = q.filter(LearningPath.status == "published")
    return [svc.path_out(db, actor, p) for p in q.order_by(LearningPath.title).all()]


@router.post("/paths", status_code=201)
def create_path(body: PathIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    p = svc.save_path(db, actor, body.model_dump())
    return _done(db, svc.path_out(db, actor, p))


@router.put("/paths/{uuid}")
def update_path(uuid: UUID, body: PathIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    p = svc.save_path(db, actor, body.model_dump(), svc._get(db, LearningPath, actor.company_id, uuid, "Learning path"))
    return _done(db, svc.path_out(db, actor, p))


@router.post("/paths/{uuid}/assign")
def assign_path(uuid: UUID, body: AssignIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    p = svc._get(db, LearningPath, actor.company_id, uuid, "Learning path")
    return _done(db, {"enrolled": svc.assign_path(db, actor, p, body.employee_ids, body.due_date)})


@router.post("/paths/{uuid}/join")
def join_path(uuid: UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    p = svc._get(db, LearningPath, actor.company_id, uuid, "Learning path")
    return _done(db, {"enrolled": svc.self_join_path(db, actor, p)})


@router.get("/rules")
def list_rules(db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require()
    rows = db.query(EnrolmentRule).filter(EnrolmentRule.company_id == actor.company_id).order_by(EnrolmentRule.name).all()
    return [svc.rule_out(db, r) for r in rows]


@router.post("/rules", status_code=201)
def create_rule(body: RuleIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    r = svc.save_rule(db, actor, body.model_dump())
    n = svc.run_rule(db, actor.company_id, r, by=actor.name)
    return _done(db, {**svc.rule_out(db, r), "enrolled": n})


@router.put("/rules/{uuid}")
def update_rule(uuid: UUID, body: RuleIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    r = svc.save_rule(db, actor, body.model_dump(), svc._get(db, EnrolmentRule, actor.company_id, uuid, "Rule"))
    return _done(db, svc.rule_out(db, r))


@router.post("/rules/{uuid}/run")
def run_rule(uuid: UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require()
    r = svc._get(db, EnrolmentRule, actor.company_id, uuid, "Rule")
    return _done(db, {"enrolled": svc.run_rule(db, actor.company_id, r, by=actor.name)})


# ── Batches ─────────────────────────────────────────────────────────

@router.get("/batches")
def list_batches(course_id: int | None = None, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return svc.list_batches(db, actor, course_id)


@router.post("/batches", status_code=201)
def create_batch(body: BatchIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    b = svc.save_batch(db, actor, body.model_dump())
    db.commit()
    return svc.batch_out(db, actor, svc.get_batch(db, actor.company_id, b.uuid), roster=True)


@router.get("/batches/{uuid}")
def get_batch(uuid: UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    b = svc.get_batch(db, actor.company_id, uuid)
    return svc.batch_out(db, actor, b, roster=actor.is_admin or actor.employee_id == b.trainer_employee_id)


@router.put("/batches/{uuid}")
def update_batch(uuid: UUID, body: BatchIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    svc.save_batch(db, actor, body.model_dump(), svc.get_batch(db, actor.company_id, uuid))
    db.commit()
    return svc.batch_out(db, actor, svc.get_batch(db, actor.company_id, uuid), roster=True)


@router.post("/batches/{uuid}/seats", status_code=201)
def book_seat(uuid: UUID, body: SeatIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    s = svc.book_seat(db, actor, svc.get_batch(db, actor.company_id, uuid), body.employee_id)
    return _done(db, {"status": s.status})


@router.delete("/batches/{uuid}/seats/{seat_uuid}")
def cancel_seat(uuid: UUID, seat_uuid: UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    svc.cancel_seat(db, actor, svc.get_batch(db, actor.company_id, uuid), seat_uuid)
    return _done(db)


@router.put("/batches/{uuid}/attendance")
def attendance(uuid: UUID, body: AttendanceIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    svc.mark_attendance(db, actor, svc.get_batch(db, actor.company_id, uuid), body.session_id, body.present)
    db.commit()
    return svc.batch_out(db, actor, svc.get_batch(db, actor.company_id, uuid), roster=True)


@router.post("/batches/{uuid}/complete")
def complete_batch(uuid: UUID, body: CompleteBatchIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return _done(db, {"completed": svc.complete_batch(db, actor, svc.get_batch(db, actor.company_id, uuid), body.min_attendance_pct)})


# ── Question bank and assessments ───────────────────────────────────

@router.get("/questions")
def list_questions(category: str | None = None, q: str | None = None, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return svc.list_questions(db, actor, category, q)


@router.get("/questions/categories")
def question_categories(db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require()
    return svc.question_categories(db, actor.company_id)


@router.post("/questions", status_code=201)
def create_question(body: QuestionIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return _done(db, svc.question_out(svc.save_question(db, actor, body.model_dump())))


@router.put("/questions/{uuid}")
def update_question(uuid: UUID, body: QuestionIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    q = svc._get(db, Question, actor.company_id, uuid, "Question")
    return _done(db, svc.question_out(svc.save_question(db, actor, body.model_dump(), q)))


@router.get("/assessments")
def list_assessments(db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    rows = db.query(Assessment).filter(Assessment.company_id == actor.company_id).order_by(Assessment.title).all()
    if not actor.is_admin:
        return [svc.assessment_brief(db, actor, a) for a in rows if a.status == "published"]
    return [svc.assessment_out(a) for a in rows]


@router.post("/assessments", status_code=201)
def create_assessment(body: AssessmentIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return _done(db, svc.assessment_out(svc.save_assessment(db, actor, body.model_dump())))


@router.get("/assessments/{uuid}")
def get_assessment(uuid: UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    a = svc._get(db, Assessment, actor.company_id, uuid, "Assessment")
    return svc.assessment_out(a) if actor.is_admin else svc.assessment_brief(db, actor, a)


@router.put("/assessments/{uuid}")
def update_assessment(uuid: UUID, body: AssessmentIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    a = svc._get(db, Assessment, actor.company_id, uuid, "Assessment")
    return _done(db, svc.assessment_out(svc.save_assessment(db, actor, body.model_dump(), a)))


@router.post("/assessments/{uuid}/attempts", status_code=201)
def start_attempt(uuid: UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    att = svc.start_attempt(db, actor, svc._get(db, Assessment, actor.company_id, uuid, "Assessment"))
    db.commit()
    return svc.attempt_out(db, actor, svc.get_attempt(db, actor, att.uuid))


@router.get("/attempts/{uuid}")
def get_attempt(uuid: UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return svc.attempt_out(db, actor, svc.get_attempt(db, actor, uuid))


@router.put("/attempts/{uuid}")
def save_answers(uuid: UUID, body: AnswersIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    svc.save_answers(db, actor, svc.get_attempt(db, actor, uuid), body.answers)
    return _done(db)


@router.post("/attempts/{uuid}/submit")
def submit_attempt(uuid: UUID, body: AnswersIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    svc.submit_attempt(db, actor, svc.get_attempt(db, actor, uuid), body.answers)
    db.commit()
    return svc.attempt_out(db, actor, svc.get_attempt(db, actor, uuid))


# ── Course requests and reports ─────────────────────────────────────

@router.get("/requests")
def list_requests(mine: bool = True, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return svc.list_requests(db, actor, mine)


@router.post("/requests", status_code=201)
def create_request(body: RequestIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return _done(db, svc.request_out(svc.create_request(db, actor, body.model_dump())))


@router.post("/requests/{uuid}/fulfil")
def fulfil_request(uuid: UUID, body: FulfilIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    svc.fulfil_request(db, actor, svc._get(db, CourseRequest, actor.company_id, uuid, "Request"), body.course_id)
    return _done(db)


@router.get("/reports/summary")
def summary(db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return svc.summary_report(db, actor)
