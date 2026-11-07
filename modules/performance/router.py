"""Performance API under /performance: goals and OKRs, libraries, review cycles,
calibration, one-to-ones, feedback, improvement plans and pay reviews.

Access: everyone works on their own records; managers on their reportees (reporting
line); `talent.performance.admin` runs cycles, calibration and settings;
`talent.increments.manage` runs pay reviews. Checks live in the services.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.db import get_db
from core.errors import BadRequest
from modules.performance import common, increments, library, okr, reviews, talent
from modules.performance.common import Actor, get_actor
from modules.performance.models import (
    Competency, GoalPeriod, Kra, RatingScale, ReviewTemplate,
)
from modules.performance.schemas import (
    AckIn, BudgetsIn, CalibrationIn, CheckinIn, CompetencyIn, CycleIn, DecisionIn, EmployeeKrasIn, FeedbackAnswerIn,
    FeedbackIn, FeedbackRequestIn, FormIn, IdsIn, IncrementCycleIn, KraIn, MeetingIn, MeetingUpdate, NoteIn, ObjectiveIn,
    ObjectiveUpdate, PeriodIn, PipCheckinIn, PipCloseIn, PipIn, PotentialIn, ProposalIn, ResponseIn, RuleIn, ScaleIn,
    SettingsIn, StatusIn, TemplateIn, UuidsIn,
)

router = APIRouter(prefix="/performance", tags=["Performance"])


def _done(db: Session, value=None):
    db.commit()
    return value if value is not None else {"ok": True}


# ── Me ──────────────────────────────────────────────────────────────

@router.get("/me")
def me(db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    """What the signed-in person can do here, and what is waiting for them."""
    team = common.reportee_ids(db, actor.company_id, actor.employee_id) if actor.employee_id else set()
    forms = reviews.my_forms(db, actor)
    pending = [f for group in forms.values() for f in group if f["status"] in ("pending", "draft")]
    fb_requests = talent.list_feedback(db, actor, "requests") if actor.employee_id else []
    goals = okr.summary(db, actor, None)
    return {
        "employee_id": actor.employee_id, "is_admin": actor.is_admin, "can_increments": actor.can(common.INCREMENTS),
        "is_manager": bool(team), "team_size": len(team),
        "pending_reviews": len(pending), "overdue_reviews": sum(1 for f in pending if f["overdue"]),
        "feedback_requests": len(fb_requests), "pending_acks": len(reviews.pending_acks(db, actor)),
        "peer_approvals": len(reviews.pending_peer_approvals(db, actor)) if team or actor.is_admin else 0,
        "goals": goals,
    }


# ── Settings and libraries ──────────────────────────────────────────

@router.get("/settings")
def get_settings(db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return common.config(db, actor.company_id)


@router.put("/settings")
def put_settings(body: SettingsIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require()
    return _done(db, common.save_config(db, actor.company_id, body.model_dump(exclude_none=True)))


@router.get("/rating-scales")
def list_scales(db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    rows = db.query(RatingScale).filter(RatingScale.company_id == actor.company_id).order_by(RatingScale.id).all()
    return [library.scale_out(r) for r in rows]


@router.post("/rating-scales", status_code=201)
def create_scale(body: ScaleIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return _done(db, library.scale_out(library.save_scale(db, actor, body.model_dump())))


@router.put("/rating-scales/{uuid}")
def update_scale(uuid: UUID, body: ScaleIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    row = common.get_by_uuid(db, RatingScale, actor.company_id, uuid, "Rating scale")
    return _done(db, library.scale_out(library.save_scale(db, actor, body.model_dump(), row)))


@router.get("/kras")
def list_kras(db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    rows = db.query(Kra).filter(Kra.company_id == actor.company_id).order_by(Kra.title).all()
    return [library.kra_out(r) for r in rows]


@router.post("/kras", status_code=201)
def create_kra(body: KraIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return _done(db, library.kra_out(library.save_kra(db, actor, body.model_dump())))


@router.put("/kras/{uuid}")
def update_kra(uuid: UUID, body: KraIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    row = common.get_by_uuid(db, Kra, actor.company_id, uuid, "KRA")
    return _done(db, library.kra_out(library.save_kra(db, actor, body.model_dump(), row)))


@router.post("/kras/auto-assign")
def auto_assign(db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require()
    return _done(db, {"assigned": library.auto_assign_kras(db, actor.company_id)})


@router.get("/employees/{employee_id}/kras")
def get_employee_kras(employee_id: int, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    common.ensure_can_see(db, actor, employee_id)
    return library.employee_kras(db, actor.company_id, employee_id)


@router.put("/employees/{employee_id}/kras")
def put_employee_kras(employee_id: int, body: EmployeeKrasIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return _done(db, library.set_employee_kras(db, actor, employee_id, body.items))


@router.get("/competencies")
def list_competencies(db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    rows = db.query(Competency).filter(Competency.company_id == actor.company_id).order_by(Competency.category, Competency.name).all()
    return [library.competency_out(r) for r in rows]


@router.post("/competencies", status_code=201)
def create_competency(body: CompetencyIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return _done(db, library.competency_out(library.save_competency(db, actor, body.model_dump())))


@router.put("/competencies/{uuid}")
def update_competency(uuid: UUID, body: CompetencyIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    row = common.get_by_uuid(db, Competency, actor.company_id, uuid, "Competency")
    return _done(db, library.competency_out(library.save_competency(db, actor, body.model_dump(), row)))


@router.get("/templates")
def list_templates(db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    rows = db.query(ReviewTemplate).filter(ReviewTemplate.company_id == actor.company_id).order_by(ReviewTemplate.name).all()
    return [library.template_out(r) for r in rows]


@router.post("/templates", status_code=201)
def create_template(body: TemplateIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return _done(db, library.template_out(library.save_template(db, actor, body.model_dump())))


@router.put("/templates/{uuid}")
def update_template(uuid: UUID, body: TemplateIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    row = common.get_by_uuid(db, ReviewTemplate, actor.company_id, uuid, "Template")
    return _done(db, library.template_out(library.save_template(db, actor, body.model_dump(), row)))


# ── Goals and OKRs ──────────────────────────────────────────────────

@router.get("/periods")
def list_periods(db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return _done(db, okr.list_periods(db, actor.company_id))


@router.post("/periods", status_code=201)
def create_period(body: PeriodIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return _done(db, okr.period_out(okr.save_period(db, actor, body.model_dump())))


@router.put("/periods/{uuid}")
def update_period(uuid: UUID, body: PeriodIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    row = common.get_by_uuid(db, GoalPeriod, actor.company_id, uuid, "Period")
    return _done(db, okr.period_out(okr.save_period(db, actor, body.model_dump(), row)))


@router.get("/objectives")
def list_objectives(period_id: int | None = None, scope: str | None = None, level: str | None = None,
                    owner_id: int | None = None, department_id: int | None = None, q: str | None = None,
                    db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    if owner_id and owner_id != actor.employee_id:
        common.ensure_can_see(db, actor, owner_id)
    return okr.list_objectives(db, actor, period_id=period_id, level=level, scope=scope, owner_id=owner_id,
                               department_id=department_id, q=q)


@router.get("/objectives/tree")
def objective_tree(period_id: int, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return okr.tree(db, actor, period_id)


@router.get("/objectives/summary")
def objective_summary(period_id: int | None = None, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return _done(db, okr.summary(db, actor, period_id))


@router.post("/objectives/refresh-statuses")
def refresh_statuses(period_id: int | None = None, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require()
    return _done(db, {"updated": okr.refresh_statuses(db, actor.company_id, period_id)})


@router.post("/objectives", status_code=201)
def create_objective(body: ObjectiveIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    data = body.model_dump()
    obj = okr.create_objective(db, actor, data)
    db.commit()
    return okr.objective_out(db, actor, okr.get(db, actor, obj.uuid), detail=True)


@router.get("/objectives/{uuid}")
def get_objective(uuid: UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return okr.objective_out(db, actor, okr.get(db, actor, uuid), detail=True)


@router.put("/objectives/{uuid}")
def update_objective(uuid: UUID, body: ObjectiveUpdate, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    data = body.model_dump(exclude_unset=True)
    if data.get("key_results") is not None:
        data["key_results"] = [k.model_dump() if hasattr(k, "model_dump") else k for k in body.key_results or []]
    okr.update_objective(db, actor, okr.get(db, actor, uuid), data)
    db.commit()
    return okr.objective_out(db, actor, okr.get(db, actor, uuid), detail=True)


@router.delete("/objectives/{uuid}")
def archive_objective(uuid: UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    okr.archive_objective(db, actor, okr.get(db, actor, uuid))
    return _done(db)


@router.post("/objectives/{uuid}/checkins", status_code=201)
def checkin(uuid: UUID, body: CheckinIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    okr.checkin(db, actor, okr.get(db, actor, uuid), body.model_dump())
    db.commit()
    return okr.objective_out(db, actor, okr.get(db, actor, uuid), detail=True)


# ── Review cycles ───────────────────────────────────────────────────

@router.get("/cycles")
def list_cycles(status: str | None = None, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return reviews.list_cycles(db, actor, status)


@router.post("/cycles", status_code=201)
def create_cycle(body: CycleIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    c = reviews.save_cycle(db, actor, body.model_dump())
    db.commit()
    return reviews.cycle_out(db, reviews.get_cycle(db, actor.company_id, c.uuid))


@router.post("/cycles/preview-participants")
def preview_participants(body: RuleIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require()
    emps = reviews.matching_employees(db, actor.company_id, body.participant_rule)
    return {"count": len(emps), "people": [common.person(e) for e in emps[:200]]}


@router.get("/cycles/{uuid}")
def get_cycle(uuid: UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require()
    return reviews.cycle_out(db, reviews.get_cycle(db, actor.company_id, uuid))


@router.put("/cycles/{uuid}")
def update_cycle(uuid: UUID, body: CycleIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    c = reviews.save_cycle(db, actor, body.model_dump(), reviews.get_cycle(db, actor.company_id, uuid))
    db.commit()
    return reviews.cycle_out(db, reviews.get_cycle(db, actor.company_id, c.uuid))


@router.post("/cycles/{uuid}/launch")
def launch_cycle(uuid: UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    n = reviews.launch(db, actor, reviews.get_cycle(db, actor.company_id, uuid))
    return _done(db, {"participants": n})


@router.post("/cycles/{uuid}/status")
def cycle_status(uuid: UUID, body: StatusIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    reviews.set_status(db, actor, reviews.get_cycle(db, actor.company_id, uuid), body.status)
    return _done(db)


@router.post("/cycles/{uuid}/remind")
def remind(uuid: UUID, body: UuidsIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return _done(db, reviews.remind(db, actor, reviews.get_cycle(db, actor.company_id, uuid), body.uuids or None))


@router.post("/cycles/{uuid}/escalate")
def escalate(uuid: UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require()
    return _done(db, {"escalated": reviews.escalate(db, actor.company_id, reviews.get_cycle(db, actor.company_id, uuid))})


@router.post("/cycles/{uuid}/publish")
def publish(uuid: UUID, body: UuidsIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return _done(db, reviews.publish(db, actor, reviews.get_cycle(db, actor.company_id, uuid), body.uuids or None))


@router.post("/cycles/{uuid}/participants")
def add_participants(uuid: UUID, body: IdsIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return _done(db, {"added": reviews.add_participants(db, actor, reviews.get_cycle(db, actor.company_id, uuid), body.employee_ids)})


@router.get("/cycles/{uuid}/calibration")
def calibration(uuid: UUID, department_id: int | None = None, manager_id: int | None = None,
                db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return reviews.board(db, actor, reviews.get_cycle(db, actor.company_id, uuid), department_id, manager_id)


@router.get("/cycles/{uuid}/nine-box")
def nine_box(uuid: UUID, department_id: int | None = None, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return reviews.nine_box(db, actor, reviews.get_cycle(db, actor.company_id, uuid), department_id)


@router.delete("/participants/{uuid}")
def remove_participant(uuid: UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    reviews.remove_participant(db, actor, reviews.get_participant(db, actor, uuid))
    return _done(db)


@router.put("/participants/{uuid}/calibration")
def calibrate(uuid: UUID, body: CalibrationIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    reviews.calibrate(db, actor, reviews.get_participant(db, actor, uuid), body.rating, body.reason)
    return _done(db)


@router.post("/participants/{uuid}/acknowledge")
def acknowledge(uuid: UUID, body: AckIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    reviews.acknowledge(db, actor, reviews.get_participant(db, actor, uuid), body.accept, body.note)
    return _done(db)


@router.put("/participants/{uuid}/potential")
def potential(uuid: UUID, body: PotentialIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    reviews.set_potential(db, actor, reviews.get_participant(db, actor, uuid), body.potential)
    return _done(db)


@router.get("/participants/{uuid}/letter")
def review_letter(uuid: UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return reviews.letter(db, actor, reviews.get_participant(db, actor, uuid))


@router.post("/participants/{uuid}/respond")
def respond(uuid: UUID, body: ResponseIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    reviews.respond(db, actor, reviews.get_participant(db, actor, uuid), body.response, body.note)
    return _done(db)


@router.post("/participants/{uuid}/peers")
def nominate_peers(uuid: UUID, body: IdsIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    rows = reviews.nominate_peers(db, actor, reviews.get_participant(db, actor, uuid), body.employee_ids)
    return _done(db, {"added": len(rows)})


@router.get("/reviews/mine")
def my_reviews(db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return reviews.my_forms(db, actor)


@router.get("/reviews/results")
def my_results(db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return reviews.my_results(db, actor)


@router.get("/reviews/team")
def team_reviews(db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return reviews.team_overview(db, actor)


@router.get("/reviews/acknowledgements")
def pending_acks(db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return reviews.pending_acks(db, actor)


@router.get("/reviews/peer-approvals")
def peer_approvals(db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return reviews.pending_peer_approvals(db, actor)


@router.get("/forms/{uuid}")
def get_form(uuid: UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return reviews.form_detail(db, actor, reviews.get_form(db, actor, uuid))


@router.put("/forms/{uuid}")
def save_form(uuid: UUID, body: FormIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    f = reviews.save_form(db, actor, reviews.get_form(db, actor, uuid), body.answers, body.submit)
    db.commit()
    return reviews.form_detail(db, actor, reviews.get_form(db, actor, f.uuid))


@router.post("/forms/{uuid}/reopen")
def reopen_form(uuid: UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    reviews.reopen_form(db, actor, reviews.get_form(db, actor, uuid))
    return _done(db)


@router.post("/forms/{uuid}/peer-decision")
def peer_decision(uuid: UUID, body: DecisionIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    from modules.performance.models import ReviewForm
    f = common.get_by_uuid(db, ReviewForm, actor.company_id, uuid, "Peer review")
    reviews.decide_peer(db, actor, f, body.approve)
    return _done(db)


# ── One-to-ones ─────────────────────────────────────────────────────

@router.get("/one-on-ones")
def list_meetings(when: str | None = None, employee_id: int | None = None, db: Session = Depends(get_db),
                  actor: Actor = Depends(get_actor)):
    return talent.list_meetings(db, actor, when, employee_id)


@router.post("/one-on-ones", status_code=201)
def create_meeting(body: MeetingIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    m = talent.create_meeting(db, actor, body.model_dump())
    db.commit()
    return talent.meeting_out(db, actor, talent.get_meeting(db, actor, m.uuid), with_private=True)


@router.get("/one-on-ones/{uuid}")
def get_meeting(uuid: UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return talent.meeting_out(db, actor, talent.get_meeting(db, actor, uuid), with_private=True)


@router.put("/one-on-ones/{uuid}")
def update_meeting(uuid: UUID, body: MeetingUpdate, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    talent.update_meeting(db, actor, talent.get_meeting(db, actor, uuid), body.model_dump(exclude_unset=True))
    db.commit()
    return talent.meeting_out(db, actor, talent.get_meeting(db, actor, uuid), with_private=True)


@router.put("/one-on-ones/{uuid}/private-note")
def private_note(uuid: UUID, body: NoteIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    talent.save_private_note(db, actor, talent.get_meeting(db, actor, uuid), body.note)
    return _done(db)


@router.post("/one-on-ones/{uuid}/complete")
def complete_meeting(uuid: UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    nxt = talent.complete_meeting(db, actor, talent.get_meeting(db, actor, uuid))
    db.commit()
    return {"ok": True, "next_uuid": str(nxt.uuid) if nxt else None}


# ── Feedback ────────────────────────────────────────────────────────

@router.get("/feedback")
def list_feedback(box: str = "received", db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return talent.list_feedback(db, actor, box)


@router.post("/feedback", status_code=201)
def give_feedback(body: FeedbackIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    f = talent.give_feedback(db, actor, body.model_dump())
    db.commit()
    return talent.feedback_out(talent.get_feedback(db, actor, f.uuid), actor)


@router.post("/feedback/requests", status_code=201)
def request_feedback(body: FeedbackRequestIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    rows = talent.request_feedback(db, actor, body.model_dump())
    return _done(db, {"requested": len(rows)})


@router.post("/feedback/{uuid}/answer")
def answer_feedback(uuid: UUID, body: FeedbackAnswerIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    talent.answer_feedback(db, actor, talent.get_feedback(db, actor, uuid), body.message, body.decline)
    return _done(db)


# ── Improvement plans ───────────────────────────────────────────────

@router.get("/pips")
def list_pips(status: str | None = None, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return talent.list_pips(db, actor, status)


@router.post("/pips", status_code=201)
def create_pip(body: PipIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    p = talent.save_pip(db, actor, body.model_dump(exclude_unset=True))
    db.commit()
    return talent.pip_out(db, actor, talent.get_pip(db, actor, p.uuid))


@router.get("/pips/{uuid}")
def get_pip(uuid: UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return talent.pip_out(db, actor, talent.get_pip(db, actor, uuid))


@router.put("/pips/{uuid}")
def update_pip(uuid: UUID, body: PipIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    talent.save_pip(db, actor, body.model_dump(exclude_unset=True), talent.get_pip(db, actor, uuid))
    db.commit()
    return talent.pip_out(db, actor, talent.get_pip(db, actor, uuid))


@router.post("/pips/{uuid}/activate")
def activate_pip(uuid: UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    talent.activate_pip(db, actor, talent.get_pip(db, actor, uuid))
    return _done(db)


@router.post("/pips/{uuid}/checkins", status_code=201)
def pip_checkin(uuid: UUID, body: PipCheckinIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    talent.pip_checkin(db, actor, talent.get_pip(db, actor, uuid), body.model_dump())
    db.commit()
    return talent.pip_out(db, actor, talent.get_pip(db, actor, uuid))


@router.post("/pips/{uuid}/close")
def close_pip(uuid: UUID, body: PipCloseIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    talent.close_pip(db, actor, talent.get_pip(db, actor, uuid), body.outcome, body.note, body.new_end_date)
    db.commit()
    return talent.pip_out(db, actor, talent.get_pip(db, actor, uuid))


@router.post("/pips/{uuid}/acknowledge")
def acknowledge_pip(uuid: UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    talent.acknowledge_pip(db, actor, talent.get_pip(db, actor, uuid))
    return _done(db)


# ── Pay reviews ─────────────────────────────────────────────────────

@router.get("/increment-cycles")
def list_increment_cycles(db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return increments.list_cycles(db, actor)


@router.post("/increment-cycles", status_code=201)
def create_increment_cycle(body: IncrementCycleIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    c = increments.save_cycle(db, actor, body.model_dump(exclude_unset=True))
    db.commit()
    return increments.cycle_out(db, increments.get_cycle(db, actor.company_id, c.uuid))


@router.get("/increment-cycles/{uuid}")
def increment_grid(uuid: UUID, department_id: int | None = None, status: str | None = None,
                   db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return increments.grid(db, actor, increments.get_cycle(db, actor.company_id, uuid), department_id, status)


@router.put("/increment-cycles/{uuid}")
def update_increment_cycle(uuid: UUID, body: IncrementCycleIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    c = increments.save_cycle(db, actor, body.model_dump(exclude_unset=True), increments.get_cycle(db, actor.company_id, uuid))
    db.commit()
    return increments.cycle_out(db, increments.get_cycle(db, actor.company_id, c.uuid))


@router.post("/increment-cycles/{uuid}/open")
def open_increment_cycle(uuid: UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return _done(db, {"proposals": increments.open_cycle(db, actor, increments.get_cycle(db, actor.company_id, uuid))})


@router.put("/increment-cycles/{uuid}/budgets")
def increment_budgets(uuid: UUID, body: BudgetsIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    c = increments.get_cycle(db, actor.company_id, uuid)
    increments.set_budgets(db, actor, c, body.budgets)
    return _done(db, increments.budgets(db, c))


@router.post("/increment-cycles/{uuid}/submit")
def submit_increments(uuid: UUID, body: UuidsIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return _done(db, increments.submit(db, actor, increments.get_cycle(db, actor.company_id, uuid), body.uuids))


@router.post("/increment-cycles/{uuid}/release")
def release_increments(uuid: UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return _done(db, increments.release(db, actor, increments.get_cycle(db, actor.company_id, uuid)))


@router.put("/increment-proposals/{uuid}")
def update_proposal(uuid: UUID, body: ProposalIn, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    p = increments.update_proposal(db, actor, increments.get_proposal(db, actor, uuid), body.model_dump(exclude_unset=True))
    db.commit()
    from modules.designations.models import Designation
    names = dict(db.query(Designation.id, Designation.designation_name).all())
    return increments.proposal_out(increments.get_proposal(db, actor, p.uuid), names)


@router.get("/increment-proposals/{uuid}/letter")
def increment_letter(uuid: UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return increments.letter(db, actor, increments.get_proposal(db, actor, uuid))


@router.get("/increments/mine")
def my_increment_letters(db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return increments.my_letters(db, actor)


@router.post("/increments/apply-due")
def apply_due(db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    if not actor.can(common.INCREMENTS):
        raise BadRequest("Only people who run pay reviews can do this.")
    return _done(db, {"applied": increments.apply_due_changes(db, actor.company_id)})


@router.get("/employees/{employee_id}/pay-history")
def pay_history(employee_id: int, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return increments.job_history(db, actor, employee_id)
