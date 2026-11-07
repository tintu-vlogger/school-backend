"""Review cycles: launch from a template to everyone who matches the participant rule;
self, manager, peer (nominated, optionally approved) and skip-level forms with deadlines;
scores from section weights and reviewer weightage; reminders with limits and escalation;
calibration against a target distribution with manager acknowledgement and reasons;
publish with letters; nine-box; the employee's response.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy.orm.attributes import flag_modified

from core import audit, expressions
from core.errors import BadRequest, Forbidden, NotFound
from modules.performance import calc, library
from modules.performance.common import (
    Actor, config, emp_model, employee, is_manager_of, name_of, notify, now, person, reportee_ids,
    scale_label, scale_values, today,
)
from modules.performance.models import (
    GoalPeriod, Objective, RatingScale, ReviewCycle, ReviewForm, ReviewParticipant, ReviewTemplate,
)

REVIEWER_TYPES = ("self", "manager", "peer", "skip")
STAGE_KEYS = ("self", "peer", "manager", "skip", "calibration")
STAGE_LABELS = {"self": "Self review", "peer": "Peer review", "manager": "Manager review",
                "skip": "Skip-level review", "calibration": "Calibration"}
CYCLE_KINDS = ("annual", "half_yearly", "quarterly", "probation")
DEFAULT_SETTINGS = {
    "peer": {"min": 1, "max": 3, "nominate_by": "employee", "needs_approval": True},
    "calibration": {"enabled": True, "distribution": {}, "require_ack": True, "require_reason": True},
    "letters": True,
    "manager_sees_self_review": True,
}


# ── Helpers ─────────────────────────────────────────────────────────

def _scale(db: Session, cycle: ReviewCycle) -> RatingScale | None:
    sid = cycle.rating_scale_id or (cycle.template.rating_scale_id if cycle.template else None)
    if sid:
        s = db.query(RatingScale).filter(RatingScale.company_id == cycle.company_id, RatingScale.id == sid).first()
        if s:
            return s
    from modules.performance.common import default_scale
    return default_scale(db, cycle.company_id)


def _settings(cycle: ReviewCycle) -> dict:
    s = {**DEFAULT_SETTINGS, **(cycle.settings or {})}
    for k in ("peer", "calibration"):
        s[k] = {**DEFAULT_SETTINGS[k], **((cycle.settings or {}).get(k) or {})}
    return s


def _stage(cycle: ReviewCycle, key: str) -> dict | None:
    return next((s for s in (cycle.stages or []) if s.get("key") == key and s.get("enabled", True)), None)


def _deadline(cycle: ReviewCycle, key: str) -> date | None:
    s = _stage(cycle, key)
    if s and s.get("deadline"):
        return date.fromisoformat(s["deadline"])
    return None


def get_cycle(db: Session, company_id: int, uuid) -> ReviewCycle:
    c = (db.query(ReviewCycle).options(joinedload(ReviewCycle.template), joinedload(ReviewCycle.goal_period))
         .filter(ReviewCycle.company_id == company_id, ReviewCycle.uuid == uuid).first())
    if c is None:
        raise NotFound("Review cycle")
    return c


def clean_stages(stages: list[dict]) -> list[dict]:
    out = []
    by_key = {s.get("key"): s for s in stages or []}
    for key in STAGE_KEYS:
        s = by_key.get(key) or {}
        enabled = bool(s.get("enabled", key in ("self", "manager")))
        out.append({"key": key, "label": STAGE_LABELS[key], "enabled": enabled, "deadline": s.get("deadline") or None})
    if not any(s["enabled"] for s in out if s["key"] in ("self", "manager", "peer", "skip")):
        raise BadRequest("Turn on at least one review stage.")
    if not next(s for s in out if s["key"] == "manager")["enabled"] and not next(s for s in out if s["key"] == "self")["enabled"]:
        raise BadRequest("A cycle needs a self review or a manager review.")
    return out


def clean_weightage(weightage: dict, stages: list[dict]) -> dict:
    enabled = {s["key"] for s in stages if s["enabled"]}
    out = {}
    for t in REVIEWER_TYPES:
        v = float((weightage or {}).get(t) or 0)
        if v < 0 or v > 100:
            raise BadRequest("Weightage must be between 0 and 100.")
        out[t] = v if t in enabled else 0.0
    total = sum(out.values())
    if abs(total - 100) > 0.01:
        raise BadRequest(f"Reviewer weightage must add up to 100% (now {total:g}%).")
    return out


def clean_settings(settings: dict, values: list[float]) -> dict:
    s = {**DEFAULT_SETTINGS, **(settings or {})}
    peer = {**DEFAULT_SETTINGS["peer"], **((settings or {}).get("peer") or {})}
    peer["min"], peer["max"] = int(peer.get("min") or 0), int(peer.get("max") or 3)
    if peer["max"] < peer["min"] or peer["max"] > 10:
        raise BadRequest("Peer reviewers: the most must be at least the fewest, and at most 10.")
    if peer.get("nominate_by") not in ("employee", "manager"):
        raise BadRequest("Peers are nominated by the employee or the manager.")
    cal = {**DEFAULT_SETTINGS["calibration"], **((settings or {}).get("calibration") or {})}
    dist = {str(float(k)): float(v or 0) for k, v in (cal.get("distribution") or {}).items() if float(v or 0) > 0}
    if dist:
        bad = [k for k in dist if float(k) not in values]
        if bad:
            raise BadRequest("The target distribution uses a rating that isn't on the scale.")
        total = sum(dist.values())
        if abs(total - 100) > 0.01:
            raise BadRequest(f"The target distribution must add up to 100% (now {total:g}%).")
    cal["distribution"] = dist
    s["peer"], s["calibration"] = peer, cal
    s["letters"] = bool(s.get("letters", True))
    s["manager_sees_self_review"] = bool(s.get("manager_sees_self_review", True))
    return s


# ── Participants ────────────────────────────────────────────────────

def _matches_rule(emp, rule: dict, today_: date) -> bool:
    if emp.id in (rule.get("exclude_ids") or []):
        return False
    if emp.id in (rule.get("include_ids") or []):
        return True
    if rule.get("department_ids") and emp.department_id not in rule["department_ids"]:
        return False
    if rule.get("designation_ids") and emp.designation_id not in rule["designation_ids"]:
        return False
    if rule.get("joined_before") and emp.created_at and emp.created_at.date() > date.fromisoformat(rule["joined_before"]):
        return False
    cond = (rule.get("condition") or "").strip()
    if cond:
        variables = {"employee": {"id": emp.id, "role": emp.role, "department_id": emp.department_id,
                                  "designation_id": emp.designation_id, "employee_code": emp.employee_code,
                                  "custom": emp.custom or {}}}
        try:
            return bool(expressions.check_condition(cond, variables, today=today_))
        except expressions.ExpressionError:
            return False
    return True


def matching_employees(db: Session, company_id: int, rule: dict) -> list:
    E = emp_model()
    t = today(db, company_id)
    rows = (db.query(E).options(joinedload(E.department), joinedload(E.designation))
            .filter(E.company_id == company_id, E.active.is_(True)).order_by(E.first_name, E.last_name).all())
    return [e for e in rows if _matches_rule(e, rule or {}, t)]


def _add_participant(db: Session, cycle: ReviewCycle, emp) -> ReviewParticipant:
    mgr_id = emp.reporting_manager_id
    skip_id = None
    if mgr_id:
        mgr = employee(db, cycle.company_id, mgr_id, required=False)
        skip_id = mgr.reporting_manager_id if mgr else None
    p = ReviewParticipant(company_id=cycle.company_id, cycle_id=cycle.id, employee_id=emp.id,
                          manager_employee_id=mgr_id, skip_manager_employee_id=skip_id, department_id=emp.department_id)
    db.add(p)
    db.flush()
    if _stage(cycle, "self"):
        db.add(ReviewForm(company_id=cycle.company_id, participant_id=p.id, reviewer_employee_id=emp.id,
                          reviewer_type="self", due_date=_deadline(cycle, "self")))
    if _stage(cycle, "manager"):
        db.add(ReviewForm(company_id=cycle.company_id, participant_id=p.id, reviewer_employee_id=mgr_id,
                          reviewer_type="manager", due_date=_deadline(cycle, "manager")))
    if _stage(cycle, "skip") and skip_id and skip_id != emp.id:
        db.add(ReviewForm(company_id=cycle.company_id, participant_id=p.id, reviewer_employee_id=skip_id,
                          reviewer_type="skip", due_date=_deadline(cycle, "skip")))
    db.flush()
    return p


# ── Cycles ──────────────────────────────────────────────────────────

def save_cycle(db: Session, actor: Actor, data: dict, cycle: ReviewCycle | None = None) -> ReviewCycle:
    actor.require()
    name = (data.get("name") or "").strip()
    if not name:
        raise BadRequest("Give the cycle a name.")
    tmpl = db.query(ReviewTemplate).filter(ReviewTemplate.company_id == actor.company_id,
                                           ReviewTemplate.id == data.get("template_id")).first()
    if tmpl is None:
        raise BadRequest("Pick a review template.")
    if not data.get("period_start") or not data.get("period_end") or data["period_end"] < data["period_start"]:
        raise BadRequest("Give the review period a start and an end after it.")
    kind = data.get("kind") or "annual"
    if kind not in CYCLE_KINDS:
        raise BadRequest("Pick annual, half-yearly, quarterly or probation.")
    if cycle is not None and cycle.status not in ("draft", "active"):
        raise BadRequest("This cycle can no longer be changed.")
    if cycle is not None and cycle.status == "active" and tmpl.id != cycle.template_id:
        raise BadRequest("The template can't change after the cycle has started.")
    stages = clean_stages(data.get("stages") or [])
    weightage = clean_weightage(data.get("weightage") or {}, stages)
    if cycle is None:
        cycle = ReviewCycle(company_id=actor.company_id, status="draft")
        db.add(cycle)
    cycle.template_id = tmpl.id
    cycle.template = tmpl
    cycle.rating_scale_id = data.get("rating_scale_id") or tmpl.rating_scale_id
    scale = _scale(db, cycle)
    values = scale_values(scale)
    cycle.name, cycle.kind = name[:200], kind
    cycle.period_start, cycle.period_end = data["period_start"], data["period_end"]
    cycle.goal_period_id = data.get("goal_period_id") or None
    rule = data.get("participant_rule") or {}
    cycle.participant_rule = {
        "department_ids": [int(x) for x in rule.get("department_ids") or []],
        "designation_ids": [int(x) for x in rule.get("designation_ids") or []],
        "condition": (rule.get("condition") or "").strip(),
        "include_ids": [int(x) for x in rule.get("include_ids") or []],
        "exclude_ids": [int(x) for x in rule.get("exclude_ids") or []],
        "joined_before": rule.get("joined_before") or None,
    }
    if cycle.participant_rule["condition"]:
        try:
            expressions.parse(cycle.participant_rule["condition"])
        except expressions.ExpressionError as e:
            raise BadRequest(f"Condition: {e}")
    cycle.stages = stages
    cycle.weightage = weightage
    cycle.settings = clean_settings(data.get("settings") or {}, values)
    db.flush()
    if cycle.status == "active":
        for p in db.query(ReviewParticipant).filter(ReviewParticipant.company_id == cycle.company_id, ReviewParticipant.cycle_id == cycle.id).all():
            for f in p.forms:
                if f.status != "submitted":
                    f.due_date = _deadline(cycle, f.reviewer_type)
        db.flush()
    return cycle


def launch(db: Session, actor: Actor, cycle: ReviewCycle) -> int:
    actor.require()
    if cycle.status != "draft":
        raise BadRequest("Only a draft cycle can be launched.")
    emps = matching_employees(db, cycle.company_id, cycle.participant_rule or {})
    if not emps:
        raise BadRequest("Nobody matches the participant rule.")
    for e in emps:
        _add_participant(db, cycle, e)
    cycle.status, cycle.launched_at = "active", now()
    db.flush()
    for e in emps:
        notify(db, cycle.company_id, e.id, f"{cycle.name} has started. Please complete your self review.")
    audit.event(db, "review_cycle.launched", "perf_cycles", cycle.id, {"participants": len(emps)},
                entity_uuid=str(cycle.uuid), entity_label=cycle.name)
    return len(emps)


def add_participants(db: Session, actor: Actor, cycle: ReviewCycle, employee_ids: list[int]) -> int:
    actor.require()
    if cycle.status not in ("active", "calibration"):
        raise BadRequest("Add people once the cycle has started.")
    have = {i for (i,) in db.query(ReviewParticipant.employee_id).filter(
        ReviewParticipant.company_id == cycle.company_id, ReviewParticipant.cycle_id == cycle.id).all()}
    n = 0
    for eid in employee_ids:
        if eid in have:
            continue
        _add_participant(db, cycle, employee(db, cycle.company_id, eid))
        n += 1
    return n


def remove_participant(db: Session, actor: Actor, p: ReviewParticipant) -> None:
    actor.require()
    if p.published_at:
        raise BadRequest("A published review can't be removed.")
    if any(f.status == "submitted" for f in p.forms if f.reviewer_type == "manager"):
        raise BadRequest("The manager has already submitted this review.")
    audit.event(db, "review_cycle.participant_removed", "perf_participants", p.id, {"employee_id": p.employee_id})
    db.delete(p)
    db.flush()


def set_status(db: Session, actor: Actor, cycle: ReviewCycle, status: str) -> None:
    actor.require()
    allowed = {"active": {"calibration"}, "calibration": {"active"}, "published": {"closed"}}
    if status not in allowed.get(cycle.status, set()):
        raise BadRequest("The cycle can't move to that stage now.")
    cycle.status = status
    if status == "closed":
        cycle.closed_at = now()
    if status == "calibration":
        for p in _participants(db, cycle):
            recompute_participant(db, cycle, p)
    db.flush()


def _participants(db: Session, cycle: ReviewCycle, department_id: int | None = None) -> list[ReviewParticipant]:
    E = emp_model()
    q = (db.query(ReviewParticipant)
         .options(selectinload(ReviewParticipant.forms), joinedload(ReviewParticipant.employee).joinedload(E.department),
                  joinedload(ReviewParticipant.employee).joinedload(E.designation), joinedload(ReviewParticipant.manager))
         .filter(ReviewParticipant.company_id == cycle.company_id, ReviewParticipant.cycle_id == cycle.id))
    if department_id:
        q = q.filter(ReviewParticipant.department_id == department_id)
    return q.all()


def cycle_out(db: Session, cycle: ReviewCycle, stats: bool = True) -> dict:
    scale = _scale(db, cycle)
    out = {
        "id": cycle.id, "uuid": str(cycle.uuid), "name": cycle.name, "kind": cycle.kind, "status": cycle.status,
        "template": library.template_out(cycle.template) if cycle.template else None,
        "rating_scale": {"id": scale.id, "name": scale.name, "points": scale.points} if scale else None,
        "goal_period_id": cycle.goal_period_id, "goal_period": cycle.goal_period.name if cycle.goal_period else None,
        "period_start": cycle.period_start.isoformat(), "period_end": cycle.period_end.isoformat(),
        "participant_rule": cycle.participant_rule or {}, "stages": cycle.stages or [],
        "weightage": cycle.weightage or {}, "settings": _settings(cycle),
        "launched_at": cycle.launched_at.isoformat() if cycle.launched_at else None,
        "published_at": cycle.published_at.isoformat() if cycle.published_at else None,
    }
    if stats:
        out["stats"] = cycle_stats(db, cycle)
    return out


def cycle_stats(db: Session, cycle: ReviewCycle) -> dict:
    forms = (db.query(ReviewForm.reviewer_type, ReviewForm.status, ReviewForm.due_date)
             .join(ReviewParticipant, ReviewParticipant.id == ReviewForm.participant_id)
             .filter(ReviewForm.company_id == cycle.company_id, ReviewParticipant.cycle_id == cycle.id).all())
    t = today(db, cycle.company_id)
    by = {}
    for typ, st, due in forms:
        if st in ("declined", "nominated"):
            continue
        b = by.setdefault(typ, {"total": 0, "submitted": 0, "overdue": 0})
        b["total"] += 1
        if st == "submitted":
            b["submitted"] += 1
        elif due and due < t:
            b["overdue"] += 1
    n = db.query(ReviewParticipant).filter(ReviewParticipant.company_id == cycle.company_id,
                                           ReviewParticipant.cycle_id == cycle.id).count()
    pub = db.query(ReviewParticipant).filter(ReviewParticipant.company_id == cycle.company_id, ReviewParticipant.cycle_id == cycle.id,
                                             ReviewParticipant.published_at.isnot(None)).count()
    total = sum(b["total"] for b in by.values())
    done = sum(b["submitted"] for b in by.values())
    return {"participants": n, "published": pub, "by_type": by, "forms_total": total, "forms_submitted": done,
            "completion": calc.r2(done / total * 100) if total else 0.0,
            "overdue": sum(b["overdue"] for b in by.values())}


def list_cycles(db: Session, actor: Actor, status: str | None = None) -> list[dict]:
    actor.require()
    q = db.query(ReviewCycle).options(joinedload(ReviewCycle.template), joinedload(ReviewCycle.goal_period)).filter(
        ReviewCycle.company_id == actor.company_id)
    if status:
        q = q.filter(ReviewCycle.status == status)
    return [cycle_out(db, c) for c in q.order_by(ReviewCycle.created_at.desc()).all()]


# ── Scores ──────────────────────────────────────────────────────────

def _form_items(db: Session, cycle: ReviewCycle, p: ReviewParticipant) -> list[dict]:
    """The things to rate, per template section, with their weights."""
    emp = p.employee or employee(db, cycle.company_id, p.employee_id)
    values = scale_values(_scale(db, cycle))
    sections = []
    for s in (cycle.template.sections or []):
        items = []
        if s["kind"] == "goals":
            for o in _goals_for(db, cycle, p.employee_id):
                items.append({"ref": str(o.id), "title": o.title, "weight": float(o.weight), "progress": float(o.progress),
                              "detail": f"{o.level.capitalize()} objective · {float(o.progress):g}% done",
                              "suggested": calc.progress_to_rating(float(o.progress), values)})
        elif s["kind"] == "kras":
            for k in library.employee_kras(db, cycle.company_id, p.employee_id):
                items.append({"ref": str(k["id"]), "title": k["title"], "weight": k["weight"],
                              "detail": k.get("measure") or k.get("description")})
        elif s["kind"] == "competencies":
            for c in library.competencies_for(db, cycle.company_id, emp):
                items.append({"ref": str(c.id), "title": c.name, "weight": float(c.weight), "detail": c.description,
                              "behaviours": c.behaviours or [], "category": c.category})
        else:
            for q in s.get("questions") or []:
                items.append({"ref": q["id"], "title": q["text"], "weight": 1, "type": q["type"], "required": q["required"]})
        sections.append({**s, "items": items})
    return sections


def _goals_for(db: Session, cycle: ReviewCycle, employee_id: int) -> list[Objective]:
    q = db.query(Objective).join(GoalPeriod, GoalPeriod.id == Objective.period_id).filter(
        Objective.company_id == cycle.company_id, Objective.owner_employee_id == employee_id,
        Objective.archived_at.is_(None), Objective.level.in_(("individual", "team")))
    if cycle.goal_period_id:
        q = q.filter(Objective.period_id == cycle.goal_period_id)
    else:
        q = q.filter(GoalPeriod.start_date <= cycle.period_end, GoalPeriod.end_date >= cycle.period_start)
    return q.order_by(Objective.created_at).all()


def score_form(sections: list[dict], answers: dict) -> float | None:
    """A reviewer's score from their ratings: section = weighted average of rated items;
    form = sections weighted by their weight (unrated sections drop out)."""
    items = (answers or {}).get("items") or {}
    sec_scores = []
    for s in sections:
        rated = []
        for it in s["items"]:
            if it.get("type") == "text":
                continue
            a = items.get(f"{s['key']}:{it['ref']}") or {}
            r = a.get("rating")
            rated.append((float(r) if r not in (None, "", "na") else None, float(it.get("weight") or 1)))
        sec_scores.append((calc.section_score(rated), float(s.get("weight") or 0)))
    return calc.form_score(sec_scores)


def recompute_participant(db: Session, cycle: ReviewCycle, p: ReviewParticipant) -> None:
    by_type: dict[str, list[float]] = {}
    for f in p.forms:
        if f.status == "submitted" and f.score is not None:
            by_type.setdefault(f.reviewer_type, []).append(float(f.score))
    scores = {t: round(sum(v) / len(v), 3) for t, v in by_type.items()}
    p.scores = scores
    flag_modified(p, "scores")
    combined = calc.combined_score(scores, cycle.weightage or {})
    p.computed_score = Decimal(str(combined)) if combined is not None else None
    rating = calc.round_to_scale(combined, scale_values(_scale(db, cycle)))
    p.proposed_rating = Decimal(str(rating)) if rating is not None else None
    mgr_done = any(f.reviewer_type == "manager" and f.status == "submitted" for f in p.forms)
    if p.status == "in_progress" and mgr_done:
        p.status = "reviewed"
    db.flush()


# ── Forms ───────────────────────────────────────────────────────────

def _form_query(db: Session, company_id: int):
    E = emp_model()
    return (db.query(ReviewForm)
            .options(joinedload(ReviewForm.participant).joinedload(ReviewParticipant.employee).joinedload(E.designation),
                     joinedload(ReviewForm.participant).joinedload(ReviewParticipant.cycle).joinedload(ReviewCycle.template),
                     joinedload(ReviewForm.reviewer))
            .filter(ReviewForm.company_id == company_id))


def _is_reviewer(actor: Actor, f: ReviewForm) -> bool:
    if f.reviewer_employee_id is None:
        return actor.is_owner
    return f.reviewer_employee_id == actor.employee_id


def form_summary(f: ReviewForm, t: date) -> dict:
    p = f.participant
    c = p.cycle
    return {"uuid": str(f.uuid), "reviewer_type": f.reviewer_type, "status": f.status,
            "reviewer": person(f.reviewer) or {"name": "Company owner"},
            "employee": person(p.employee), "participant_uuid": str(p.uuid),
            "cycle": {"uuid": str(c.uuid), "name": c.name, "status": c.status},
            "due_date": f.due_date.isoformat() if f.due_date else None,
            "overdue": bool(f.due_date and f.due_date < t and f.status not in ("submitted", "declined")),
            "submitted_at": f.submitted_at.isoformat() if f.submitted_at else None,
            "score": float(f.score) if f.score is not None else None}


def my_forms(db: Session, actor: Actor) -> dict:
    """Reviews I have to write (self, as manager, as peer, as skip-level)."""
    q = _form_query(db, actor.company_id).join(ReviewParticipant, ReviewParticipant.id == ReviewForm.participant_id).join(
        ReviewCycle, ReviewCycle.id == ReviewParticipant.cycle_id).filter(ReviewCycle.status.in_(("active", "calibration", "published")))
    if actor.is_owner:
        q = q.filter(ReviewForm.reviewer_employee_id.is_(None))
    else:
        q = q.filter(ReviewForm.reviewer_employee_id == actor.employee_id)
    t = today(db, actor.company_id)
    rows = [form_summary(f, t) for f in q.order_by(ReviewForm.due_date.nulls_last(), ReviewForm.id).all()
            if f.status != "declined"]
    return {"self": [r for r in rows if r["reviewer_type"] == "self"],
            "team": [r for r in rows if r["reviewer_type"] in ("manager", "skip")],
            "peer": [r for r in rows if r["reviewer_type"] == "peer"]}


def get_form(db: Session, actor: Actor, uuid) -> ReviewForm:
    f = _form_query(db, actor.company_id).filter(ReviewForm.uuid == uuid).first()
    if f is None:
        raise NotFound("Review")
    if not (_is_reviewer(actor, f) or actor.is_admin):
        raise Forbidden("This review isn't yours to open.")
    return f


def form_detail(db: Session, actor: Actor, f: ReviewForm) -> dict:
    p, cycle = f.participant, f.participant.cycle
    scale = _scale(db, cycle)
    t = today(db, actor.company_id)
    sections = _form_items(db, cycle, p)
    others = []
    s = _settings(cycle)
    if f.reviewer_type in ("manager", "skip") or actor.is_admin:
        for o in p.forms:
            if o.id == f.id or o.status != "submitted":
                continue
            if o.reviewer_type == "self" and not s.get("manager_sees_self_review", True) and not actor.is_admin:
                continue
            if o.reviewer_type == "peer" and f.reviewer_type == "skip" and not actor.is_admin:
                continue
            others.append({"reviewer_type": o.reviewer_type,
                           "reviewer": (person(o.reviewer) or {"name": "Company owner"})["name"] if o.reviewer_type != "peer" else "Peer",
                           "answers": o.answers or {}, "score": float(o.score) if o.score is not None else None})
    editable = _is_reviewer(actor, f) and f.status in ("pending", "draft") and cycle.status in ("active", "calibration")
    return {**form_summary(f, t), "sections": sections, "answers": f.answers or {},
            "scale": {"name": scale.name, "points": scale.points} if scale else None,
            "others": others, "editable": editable,
            "weightage": cycle.weightage or {},
            "can_nominate_peers": _can_nominate(db, actor, p, cycle),
            "peers": [{"uuid": str(o.uuid), "name": name_of(o.reviewer), "status": o.status} for o in p.forms if o.reviewer_type == "peer"]}


def save_form(db: Session, actor: Actor, f: ReviewForm, answers: dict, submit: bool) -> ReviewForm:
    if not _is_reviewer(actor, f):
        raise Forbidden("This review isn't yours to fill.")
    cycle = f.participant.cycle
    if cycle.status not in ("active", "calibration"):
        raise BadRequest("This cycle is not open for reviews.")
    if f.status == "submitted":
        raise BadRequest("This review has already been submitted.")
    if f.status in ("nominated", "declined"):
        raise BadRequest("This peer review hasn't been approved.")
    sections = _form_items(db, cycle, f.participant)
    values = set(scale_values(_scale(db, cycle)))
    items = {}
    for key, a in ((answers or {}).get("items") or {}).items():
        r = a.get("rating")
        if r not in (None, "", "na"):
            try:
                r = float(r)
            except (TypeError, ValueError):
                raise BadRequest("Ratings must be numbers on the scale.")
            if r not in values:
                raise BadRequest("Pick a rating from the scale.")
        comment = (a.get("comment") or "")[:15000]
        items[key] = {"rating": r, "comment": comment}
    clean = {"items": items, "summary": ((answers or {}).get("summary") or "")[:15000]}
    if submit:
        missing = []
        for s in sections:
            for it in s["items"]:
                a = items.get(f"{s['key']}:{it['ref']}") or {}
                if it.get("type") == "text":
                    if it.get("required") and not (a.get("comment") or "").strip():
                        missing.append(it["title"])
                elif a.get("rating") is None:
                    missing.append(it["title"])
        if missing:
            raise BadRequest(f"Rate every item before submitting (missing: {missing[0]}{' and more' if len(missing) > 1 else ''}).")
    f.answers = clean
    flag_modified(f, "answers")
    sc = score_form(sections, clean)
    f.score = Decimal(str(sc)) if sc is not None else None
    if submit:
        f.status, f.submitted_at = "submitted", now()
        recompute_participant(db, cycle, f.participant)
        p = f.participant
        if f.reviewer_type == "self" and p.manager_employee_id:
            notify(db, cycle.company_id, p.manager_employee_id, f"{name_of(p.employee)} submitted their self review for {cycle.name}.")
        if f.reviewer_type == "manager":
            notify(db, cycle.company_id, p.employee_id, f"Your manager has completed your review for {cycle.name}.")
        audit.event(db, "review.submitted", "perf_review_forms", f.id,
                    {"type": f.reviewer_type, "score": sc}, entity_uuid=str(f.uuid))
    else:
        f.status = "draft"
    db.flush()
    return f


def reopen_form(db: Session, actor: Actor, f: ReviewForm) -> None:
    actor.require()
    if f.status != "submitted":
        raise BadRequest("Only a submitted review can be reopened.")
    if f.participant.published_at:
        raise BadRequest("Results are already published.")
    f.status, f.submitted_at = "draft", None
    recompute_participant(db, f.participant.cycle, f.participant)
    audit.event(db, "review.reopened", "perf_review_forms", f.id, {}, entity_uuid=str(f.uuid))


# ── Peers ───────────────────────────────────────────────────────────

def _can_nominate(db: Session, actor: Actor, p: ReviewParticipant, cycle: ReviewCycle) -> bool:
    if not _stage(cycle, "peer") or cycle.status != "active":
        return False
    by = _settings(cycle)["peer"]["nominate_by"]
    if actor.is_admin:
        return True
    if by == "employee":
        return actor.employee_id == p.employee_id
    return actor.employee_id == p.manager_employee_id or is_manager_of(db, actor, p.employee_id)


def nominate_peers(db: Session, actor: Actor, p: ReviewParticipant, employee_ids: list[int]) -> list[ReviewForm]:
    cycle = p.cycle
    if not _can_nominate(db, actor, p, cycle):
        raise Forbidden("You can't choose peer reviewers for this review.")
    s = _settings(cycle)["peer"]
    current = [f for f in p.forms if f.reviewer_type == "peer" and f.status != "declined"]
    have = {f.reviewer_employee_id for f in current}
    new = [i for i in dict.fromkeys(employee_ids) if i not in have]
    if p.employee_id in new:
        raise BadRequest("People can't be their own peer reviewer.")
    if p.manager_employee_id in new:
        raise BadRequest("The manager already reviews this person; pick a colleague instead.")
    if len(current) + len(new) > s["max"]:
        raise BadRequest(f"Choose at most {s['max']} peer reviewers.")
    needs = bool(s.get("needs_approval")) and not actor.is_admin and actor.employee_id != p.manager_employee_id
    out = []
    for eid in new:
        e = employee(db, cycle.company_id, eid)
        f = ReviewForm(company_id=cycle.company_id, participant_id=p.id, reviewer_employee_id=e.id, reviewer_type="peer",
                       status="nominated" if needs else "pending", due_date=_deadline(cycle, "peer"),
                       nominated_by_name=actor.name)
        p.forms.append(f)
        out.append(f)
        if not needs:
            notify(db, cycle.company_id, e.id, f"{name_of(p.employee)} asked you for a peer review in {cycle.name}.")
    db.flush()
    if needs and p.manager_employee_id:
        notify(db, cycle.company_id, p.manager_employee_id, f"{name_of(p.employee)} chose peer reviewers. Please approve them.")
    return out


def decide_peer(db: Session, actor: Actor, f: ReviewForm, approve: bool) -> None:
    p = f.participant
    if f.reviewer_type != "peer" or f.status != "nominated":
        raise BadRequest("This peer reviewer isn't waiting for approval.")
    if not (actor.is_admin or actor.employee_id == p.manager_employee_id or is_manager_of(db, actor, p.employee_id)):
        raise Forbidden("Only the manager can approve peer reviewers.")
    f.status = "pending" if approve else "declined"
    if approve:
        notify(db, p.company_id, f.reviewer_employee_id, f"{name_of(p.employee)} asked you for a peer review in {p.cycle.name}.")
    db.flush()


def pending_peer_approvals(db: Session, actor: Actor) -> list[dict]:
    if actor.employee_id is None and not actor.is_admin:
        return []
    q = _form_query(db, actor.company_id).join(ReviewParticipant, ReviewParticipant.id == ReviewForm.participant_id).filter(
        ReviewForm.reviewer_type == "peer", ReviewForm.status == "nominated")
    if not actor.is_admin:
        q = q.filter(ReviewParticipant.manager_employee_id == actor.employee_id)
    t = today(db, actor.company_id)
    return [{**form_summary(f, t), "nominated_by": f.nominated_by_name} for f in q.all()]


# ── Reminders and escalation ────────────────────────────────────────

def _can_remind(f: ReviewForm, cfg: dict, at) -> bool:
    from datetime import datetime
    log = [datetime.fromisoformat(x) for x in (f.reminder_log or [])]
    min_h = float(cfg.get("reminder_min_hours") or 1)
    per_day = int(cfg.get("reminder_max_per_day") or 2)
    if log and (at - max(log)).total_seconds() < min_h * 3600:
        return False
    return sum(1 for x in log if (at - x).total_seconds() < 86400) < per_day


def remind(db: Session, actor: Actor, cycle: ReviewCycle, form_uuids: list[str] | None = None) -> dict:
    """Remind reviewers of pending forms. One reminder per person per hour, at most twice a day."""
    actor.require()
    cfg = config(db, cycle.company_id)["reviews"]
    at = now()
    q = (db.query(ReviewForm).join(ReviewParticipant, ReviewParticipant.id == ReviewForm.participant_id)
         .options(joinedload(ReviewForm.participant).joinedload(ReviewParticipant.employee))
         .filter(ReviewForm.company_id == cycle.company_id, ReviewParticipant.cycle_id == cycle.id,
                 ReviewForm.status.in_(("pending", "draft"))))
    if form_uuids:
        q = q.filter(ReviewForm.uuid.in_(form_uuids))
    sent, skipped = 0, 0
    grouped: dict[int, list[str]] = {}
    for f in q.all():
        if f.reviewer_employee_id is None:
            continue
        if not _can_remind(f, cfg, at):
            skipped += 1
            continue
        grouped.setdefault(f.reviewer_employee_id, []).append(
            "your self review" if f.reviewer_type == "self" else f"{f.reviewer_type} review of {name_of(f.participant.employee)}")
        f.reminder_log = [*(f.reminder_log or []), at.isoformat()][-10:]
        f.last_reminded_at = at
        sent += 1
    for eid, what in grouped.items():
        notify(db, cycle.company_id, eid, f"Reminder for {cycle.name}: please complete {', '.join(what[:5])}"
               f"{' and more' if len(what) > 5 else ''}.")
    audit.event(db, "review_cycle.reminded", "perf_cycles", cycle.id, {"sent": sent, "skipped": skipped},
                entity_uuid=str(cycle.uuid))
    db.flush()
    return {"sent": sent, "people": len(grouped), "skipped": skipped}


def escalate(db: Session, company_id: int, cycle: ReviewCycle | None = None) -> int:
    """Forms overdue by more than the configured days go to the reviewer's manager."""
    cfg = config(db, company_id)["reviews"]
    days = int(cfg.get("escalate_after_days") or 3)
    t = today(db, company_id)
    q = (db.query(ReviewForm).join(ReviewParticipant, ReviewParticipant.id == ReviewForm.participant_id)
         .join(ReviewCycle, ReviewCycle.id == ReviewParticipant.cycle_id)
         .filter(ReviewForm.company_id == company_id, ReviewCycle.status == "active",
                 ReviewForm.status.in_(("pending", "draft")), ReviewForm.escalated_at.is_(None),
                 ReviewForm.due_date.isnot(None), ReviewForm.due_date < t - timedelta(days=days)))
    if cycle is not None:
        q = q.filter(ReviewParticipant.cycle_id == cycle.id)
    n = 0
    for f in q.all():
        reviewer = f.reviewer
        boss = reviewer.reporting_manager_id if reviewer else None
        target = boss or None
        msg = (f"{name_of(reviewer)}'s {f.reviewer_type} review of {name_of(f.participant.employee)} in "
               f"{f.participant.cycle.name} is {(t - f.due_date).days} days late.")
        notify(db, company_id, target, msg)
        f.escalated_at = now()
        n += 1
        audit.event(db, "review.escalated", "perf_review_forms", f.id, {"to": target}, entity_uuid=str(f.uuid))
    db.flush()
    return n


# ── Participants, calibration and results ───────────────────────────

def participant_out(db: Session, cycle: ReviewCycle, p: ReviewParticipant, scale, t: date, labels: dict | None = None,
                    actor: Actor | None = None) -> dict:
    values = scale_values(scale)
    current = p.calibrated_rating if p.calibrated_rating is not None else p.proposed_rating
    box = calc.nine_box(float(current) if current is not None else None, p.potential, values)
    forms = []
    for f in p.forms:
        if f.status == "declined":
            continue
        forms.append({"uuid": str(f.uuid), "reviewer_type": f.reviewer_type, "status": f.status,
                      "reviewer": (person(f.reviewer) or {"name": "Company owner"})["name"],
                      "due_date": f.due_date.isoformat() if f.due_date else None,
                      "overdue": bool(f.due_date and f.due_date < t and f.status not in ("submitted",)),
                      "score": float(f.score) if f.score is not None else None,
                      "reminders": len(f.reminder_log or []), "escalated": f.escalated_at is not None})
    fl = lambda x: float(x) if x is not None else None  # noqa: E731
    return {
        "uuid": str(p.uuid), "employee": person(p.employee), "manager": person(p.manager),
        "department_id": p.department_id, "status": p.status, "scores": p.scores or {},
        "computed_score": fl(p.computed_score), "proposed_rating": fl(p.proposed_rating),
        "proposed_label": scale_label(scale, p.proposed_rating),
        "calibrated_rating": fl(p.calibrated_rating), "calibrated_label": scale_label(scale, p.calibrated_rating),
        "calibration_reason": p.calibration_reason, "calibrated_by": p.calibrated_by_name,
        "current_rating": fl(current), "current_label": scale_label(scale, current),
        "final_rating": fl(p.final_rating), "final_label": scale_label(scale, p.final_rating),
        "ack_status": p.ack_status, "ack_note": p.ack_note, "potential": p.potential, "box": box,
        "box_label": (labels or {}).get(str(box)) if box else None,
        "published_at": p.published_at.isoformat() if p.published_at else None,
        "has_letter": bool(p.letter_html), "response": p.response, "response_note": p.response_note,
        "forms": forms,
    }


def get_participant(db: Session, actor: Actor, uuid) -> ReviewParticipant:
    p = (db.query(ReviewParticipant).options(selectinload(ReviewParticipant.forms), joinedload(ReviewParticipant.cycle))
         .filter(ReviewParticipant.company_id == actor.company_id, ReviewParticipant.uuid == uuid).first())
    if p is None:
        raise NotFound("Review")
    return p


def board(db: Session, actor: Actor, cycle: ReviewCycle, department_id: int | None = None, manager_id: int | None = None) -> dict:
    """Calibration board: everyone with their scores, the distribution against the target."""
    if not actor.is_admin:
        if actor.employee_id is None:
            raise Forbidden()
    scale = _scale(db, cycle)
    values = scale_values(scale)
    t = today(db, cycle.company_id)
    labels = config(db, cycle.company_id)["nine_box"]["labels"]
    rows = _participants(db, cycle, department_id)
    if not actor.is_admin:
        mine = reportee_ids(db, actor.company_id, actor.employee_id, deep=True)
        rows = [p for p in rows if p.employee_id in mine]
    if manager_id:
        rows = [p for p in rows if p.manager_employee_id == manager_id]
    out = [participant_out(db, cycle, p, scale, t, labels) for p in rows]
    out.sort(key=lambda r: (-(r["current_rating"] or 0), r["employee"]["name"] if r["employee"] else ""))
    targets = _settings(cycle)["calibration"]["distribution"]
    dist = calc.distribution([r["current_rating"] for r in out], targets, values)
    for d in dist:
        d["label"] = scale_label(scale, d["value"])
    return {"cycle": cycle_out(db, cycle, stats=False), "participants": out, "distribution": dist,
            "unrated": sum(1 for r in out if r["current_rating"] is None),
            "pending_ack": sum(1 for r in out if r["ack_status"] == "pending"),
            "declined_ack": sum(1 for r in out if r["ack_status"] == "declined")}


def calibrate(db: Session, actor: Actor, p: ReviewParticipant, rating, reason: str | None) -> None:
    actor.require()
    cycle = p.cycle
    if cycle.status not in ("active", "calibration"):
        raise BadRequest("Calibration is closed for this cycle.")
    if p.published_at:
        raise BadRequest("This result is already published.")
    s = _settings(cycle)["calibration"]
    if rating in (None, ""):
        p.calibrated_rating, p.calibration_reason, p.ack_status = None, None, None
        p.calibrated_by_name, p.calibrated_at = None, None
        if p.status == "calibrated":
            p.status = "reviewed"
        db.flush()
        return
    rating = float(rating)
    if rating not in scale_values(_scale(db, cycle)):
        raise BadRequest("Pick a rating from the scale.")
    reason = (reason or "").strip()
    changed = p.proposed_rating is None or float(p.proposed_rating) != rating
    if s.get("require_reason") and changed and not reason:
        raise BadRequest("Give a reason for changing the rating.")
    before = float(p.calibrated_rating) if p.calibrated_rating is not None else (float(p.proposed_rating) if p.proposed_rating is not None else None)
    p.calibrated_rating = Decimal(str(rating))
    p.calibration_reason = reason or None
    p.calibrated_by_name, p.calibrated_at = actor.name, now()
    p.status = "calibrated"
    p.ack_status = "pending" if (s.get("require_ack") and changed and p.manager_employee_id) else ("accepted" if changed else None)
    if p.ack_status == "accepted":
        p.ack_note = "No acknowledgement needed"
    audit.event(db, "review.calibrated", "perf_participants", p.id,
                {"from": before, "to": rating, "reason": reason}, entity_uuid=str(p.uuid))
    if p.ack_status == "pending":
        notify(db, p.company_id, p.manager_employee_id,
               f"The rating for {name_of(p.employee)} in {cycle.name} was changed in calibration. Please acknowledge it.")
    db.flush()


def acknowledge(db: Session, actor: Actor, p: ReviewParticipant, accept: bool, note: str | None) -> None:
    if not (actor.employee_id and actor.employee_id == p.manager_employee_id) and not actor.is_admin:
        raise Forbidden("Only the manager can acknowledge this rating.")
    if p.ack_status != "pending":
        raise BadRequest("Nothing to acknowledge.")
    if not accept and not (note or "").strip():
        raise BadRequest("Say why you disagree.")
    p.ack_status = "accepted" if accept else "declined"
    p.ack_note = (note or "").strip() or None
    audit.event(db, "review.calibration_acknowledged", "perf_participants", p.id, {"accepted": accept, "note": p.ack_note},
                entity_uuid=str(p.uuid))
    db.flush()


def pending_acks(db: Session, actor: Actor) -> list[dict]:
    if actor.employee_id is None:
        return []
    rows = (db.query(ReviewParticipant).options(selectinload(ReviewParticipant.forms), joinedload(ReviewParticipant.cycle))
            .filter(ReviewParticipant.company_id == actor.company_id, ReviewParticipant.manager_employee_id == actor.employee_id,
                    ReviewParticipant.ack_status == "pending").all())
    t = today(db, actor.company_id)
    out = []
    for p in rows:
        scale = _scale(db, p.cycle)
        out.append({**participant_out(db, p.cycle, p, scale, t), "cycle": {"uuid": str(p.cycle.uuid), "name": p.cycle.name}})
    return out


def set_potential(db: Session, actor: Actor, p: ReviewParticipant, potential: int | None) -> None:
    if not (actor.is_admin or is_manager_of(db, actor, p.employee_id)):
        raise Forbidden("Only the manager or an administrator can set potential.")
    if potential is not None and potential not in (1, 2, 3):
        raise BadRequest("Potential is low, medium or high.")
    p.potential = potential
    db.flush()


def nine_box(db: Session, actor: Actor, cycle: ReviewCycle, department_id: int | None = None) -> dict:
    b = board(db, actor, cycle, department_id)
    labels = config(db, cycle.company_id)["nine_box"]["labels"]
    boxes = {str(i): {"box": i, "label": labels.get(str(i)), "people": []} for i in range(1, 10)}
    unplaced = []
    for r in b["participants"]:
        if r["box"]:
            boxes[str(r["box"])]["people"].append(r)
        else:
            unplaced.append(r)
    return {"cycle": b["cycle"], "boxes": list(boxes.values()), "unplaced": unplaced}


def _letter(db: Session, cycle: ReviewCycle, p: ReviewParticipant, scale) -> str:
    from modules.company.models import Company
    cfg = config(db, cycle.company_id)
    company = db.get(Company, cycle.company_id)
    mgr_form = next((f for f in p.forms if f.reviewer_type == "manager" and f.status == "submitted"), None)
    comment = ((mgr_form.answers or {}).get("summary") or "").strip() if mgr_form else ""
    vals = {
        "employee_name": name_of(p.employee), "cycle_name": cycle.name,
        "period": f"{cycle.period_start.strftime('%d %b %Y')} to {cycle.period_end.strftime('%d %b %Y')}",
        "final_rating": f"{float(p.final_rating):g}" if p.final_rating is not None else "—",
        "rating_label": scale_label(scale, p.final_rating) or "", "manager_comment": comment,
        "company_name": company.company_name if company else "",
    }
    text = cfg["letters"]["review_template"]
    for k, v in vals.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def publish(db: Session, actor: Actor, cycle: ReviewCycle, participant_uuids: list[str] | None = None) -> dict:
    actor.require()
    if cycle.status not in ("active", "calibration", "published"):
        raise BadRequest("This cycle can't be published now.")
    s = _settings(cycle)
    scale = _scale(db, cycle)
    rows = _participants(db, cycle)
    if participant_uuids:
        rows = [p for p in rows if str(p.uuid) in set(participant_uuids)]
    declined = [p for p in rows if p.ack_status == "declined" and not p.published_at]
    if declined:
        raise BadRequest(f"{len(declined)} manager(s) disagreed with a calibrated rating. Settle those first.")
    from core import numbering
    t = today(db, cycle.company_id)
    n = skipped = 0
    for p in rows:
        if p.published_at:
            continue
        rating = p.calibrated_rating if p.calibrated_rating is not None else p.proposed_rating
        if rating is None:
            skipped += 1
            continue
        p.final_rating = rating
        p.published_at, p.status = now(), "published"
        if p.ack_status == "pending":
            p.ack_status, p.ack_note = "accepted", "Published before acknowledgement"
        if s.get("letters", True):
            p.letter_no = numbering.next_number(db, cycle.company_id, "letter", t)
            p.letter_html = _letter(db, cycle, p, scale)
            p.letter_generated_at = now()
        notify(db, cycle.company_id, p.employee_id, f"Your results for {cycle.name} are ready.")
        n += 1
    remaining = sum(1 for p in _participants(db, cycle) if not p.published_at)
    cycle.status = "published" if remaining == 0 or n else cycle.status
    cycle.published_at = cycle.published_at or now()
    audit.event(db, "review_cycle.published", "perf_cycles", cycle.id, {"published": n, "skipped": skipped},
                entity_uuid=str(cycle.uuid), entity_label=cycle.name)
    db.flush()
    return {"published": n, "skipped": skipped, "remaining": remaining}


def my_results(db: Session, actor: Actor) -> list[dict]:
    if actor.employee_id is None:
        return []
    rows = (db.query(ReviewParticipant).options(selectinload(ReviewParticipant.forms), joinedload(ReviewParticipant.cycle))
            .filter(ReviewParticipant.company_id == actor.company_id, ReviewParticipant.employee_id == actor.employee_id)
            .order_by(ReviewParticipant.created_at.desc()).all())
    t = today(db, actor.company_id)
    out = []
    for p in rows:
        scale = _scale(db, p.cycle)
        published = p.published_at is not None
        manager_form = next((f for f in p.forms if f.reviewer_type == "manager" and f.status == "submitted"), None)
        out.append({
            "uuid": str(p.uuid), "cycle": {"uuid": str(p.cycle.uuid), "name": p.cycle.name, "status": p.cycle.status,
                                          "period_start": p.cycle.period_start.isoformat(), "period_end": p.cycle.period_end.isoformat()},
            "published": published, "final_rating": float(p.final_rating) if published and p.final_rating is not None else None,
            "final_label": scale_label(scale, p.final_rating) if published else None,
            "scale": scale.points if scale else [],
            "manager_summary": ((manager_form.answers or {}).get("summary") if manager_form and published else None),
            "has_letter": published and bool(p.letter_html), "response": p.response, "response_note": p.response_note,
            "published_at": p.published_at.isoformat() if p.published_at else None,
            "forms": [{"reviewer_type": f.reviewer_type, "status": f.status} for f in p.forms if f.status != "declined"],
        })
    return out


def letter(db: Session, actor: Actor, p: ReviewParticipant) -> dict:
    if not (actor.is_admin or actor.employee_id == p.employee_id or is_manager_of(db, actor, p.employee_id)):
        raise Forbidden()
    if not p.letter_html or (actor.employee_id == p.employee_id and not p.published_at):
        raise NotFound("Letter")
    return {"letter_no": p.letter_no, "body": p.letter_html, "employee": name_of(p.employee), "cycle": p.cycle.name,
            "generated_at": p.letter_generated_at.isoformat() if p.letter_generated_at else None}


def respond(db: Session, actor: Actor, p: ReviewParticipant, response: str, note: str | None) -> None:
    if actor.employee_id != p.employee_id:
        raise Forbidden("Only the employee can respond to their review.")
    if not p.published_at:
        raise BadRequest("Your results are not published yet.")
    if response not in ("agree", "disagree"):
        raise BadRequest("Say whether you agree or disagree.")
    if response == "disagree" and not (note or "").strip():
        raise BadRequest("Tell us why you disagree.")
    p.response, p.response_note = response, (note or "").strip() or None
    if p.manager_employee_id:
        notify(db, p.company_id, p.manager_employee_id,
               f"{name_of(p.employee)} {'agreed with' if response == 'agree' else 'disagreed with'} their {p.cycle.name} review.")
    db.flush()


def team_overview(db: Session, actor: Actor) -> list[dict]:
    """Manager view: my reportees in active and published cycles."""
    ids = reportee_ids(db, actor.company_id, actor.employee_id, deep=False) if actor.employee_id else set()
    if not ids:
        return []
    rows = (db.query(ReviewParticipant).options(selectinload(ReviewParticipant.forms), joinedload(ReviewParticipant.cycle))
            .join(ReviewCycle, ReviewCycle.id == ReviewParticipant.cycle_id)
            .filter(ReviewParticipant.company_id == actor.company_id, ReviewParticipant.employee_id.in_(ids),
                    ReviewCycle.status.in_(("active", "calibration", "published"))).all())
    t = today(db, actor.company_id)
    labels = config(db, actor.company_id)["nine_box"]["labels"]
    out = []
    for p in rows:
        scale = _scale(db, p.cycle)
        out.append({**participant_out(db, p.cycle, p, scale, t, labels),
                    "cycle": {"uuid": str(p.cycle.uuid), "name": p.cycle.name, "status": p.cycle.status}})
    return out
