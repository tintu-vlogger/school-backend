"""Succession planning: key positions, successors with readiness, development actions,
nine-box placement from performance reviews, bench strength and talent pools.

Readiness: ready_now, one_two_years (ready in 1-2 years), three_plus_years.
Risk of loss and impact of loss: low, medium, high. A plan with high risk and high impact
is flagged as urgent.

Bench strength of a plan (targets in settings, default 1 ready-now and 2 in all):
- strong: at least the ready-now target and the total target
- moderate: at least one successor who is ready now or in 1-2 years
- weak: only successors who need 3+ years
- none: no successors
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import func
from sqlalchemy.orm import Session

from core import audit
from core.errors import BadRequest, NotFound
from modules.positions import common as c
from modules.positions.models import (DevelopmentAction, Position, SuccessionCandidate, SuccessionPlan, TalentPool,
                                      TalentPoolMember)

READINESS = {"ready_now": "Ready now", "one_two_years": "Ready in 1-2 years", "three_plus_years": "Ready in 3+ years"}
LEVELS = {"low": "Low", "medium": "Medium", "high": "High"}
ACTION_KINDS = {"course": "Course", "goal": "Goal", "mentoring": "Mentoring", "project": "Project",
                "stretch": "Stretch assignment", "other": "Other"}
ACTION_STATUSES = {"planned": "Planned", "in_progress": "In progress", "done": "Done", "cancelled": "Cancelled"}
BENCH_LABELS = {"strong": "Strong", "moderate": "Moderate", "weak": "Weak", "none": "No successors"}
HIGH_POTENTIAL_BOXES = (6, 8, 9)


def bench_strength(counts: dict, ready_now_target: int = 1, total_target: int = 2) -> str:
    """counts: {"ready_now": n, "one_two_years": n, "three_plus_years": n}."""
    now_ = int(counts.get("ready_now") or 0)
    soon = int(counts.get("one_two_years") or 0)
    later = int(counts.get("three_plus_years") or 0)
    total = now_ + soon + later
    if total == 0:
        return "none"
    if now_ >= max(1, ready_now_target) and total >= max(1, total_target):
        return "strong"
    if now_ + soon > 0:
        return "moderate"
    return "weak"


def urgency(risk: str, impact: str) -> str:
    score = {"low": 1, "medium": 2, "high": 3}
    s = score.get(risk, 2) * score.get(impact, 2)
    return "urgent" if s >= 6 else ("watch" if s >= 4 else "stable")


# ── Nine-box from performance ───────────────────────────────────────

def talent_snapshot(db: Session, company_id: int, employee_ids) -> dict[int, dict]:
    """Latest review rating and potential per person, placed on the nine-box."""
    ids = {int(i) for i in employee_ids if i}
    if not ids:
        return {}
    try:
        from modules.performance import calc
        from modules.performance.common import config, default_scale
        from modules.performance.models import RatingScale, ReviewCycle, ReviewParticipant
    except ImportError:  # performance module absent
        return {}
    rows = (db.query(ReviewParticipant, ReviewCycle)
            .join(ReviewCycle, ReviewCycle.id == ReviewParticipant.cycle_id)
            .filter(ReviewParticipant.company_id == company_id, ReviewParticipant.employee_id.in_(ids))
            .order_by(ReviewCycle.period_end.desc(), ReviewParticipant.id.desc()).all())
    labels = config(db, company_id)["nine_box"]["labels"]
    scales: dict = {}
    default = default_scale(db, company_id)
    out: dict[int, dict] = {}
    for p, cyc in rows:
        rating = p.final_rating if p.final_rating is not None else (
            p.calibrated_rating if p.calibrated_rating is not None else p.proposed_rating)
        cur = out.get(p.employee_id)
        if cur is not None and (cur["rating"] is not None or rating is None) and (cur["potential"] is not None or p.potential is None):
            continue
        sid = cyc.rating_scale_id
        if sid not in scales:
            s = db.query(RatingScale).filter(RatingScale.company_id == company_id, RatingScale.id == sid).first() if sid else None
            scales[sid] = s or default
        scale = scales[sid]
        values = [float(x.get("value")) for x in ((scale.points if scale else None) or []) if x.get("value") is not None]
        r = float(rating) if rating is not None else (cur["rating"] if cur else None)
        pot = p.potential if p.potential is not None else (cur["potential"] if cur else None)
        box = calc.nine_box(r, pot, values or [1, 2, 3, 4, 5])
        out[p.employee_id] = {"rating": r, "potential": pot, "box": box, "box_label": labels.get(str(box)) if box else None,
                              "performance_level": calc.performance_level(r, values or [1, 2, 3, 4, 5]),
                              "cycle": cyc.name}
    return out


def nine_box(db: Session, company_id: int, department_id: int | None = None) -> dict:
    from modules.employees.models import Employee
    q = db.query(Employee.id).filter(Employee.company_id == company_id, Employee.active.is_(True),
                                     Employee.status.notin_(("exited", "archived")))
    if department_id:
        q = q.filter(Employee.department_id == department_id)
    ids = [i for (i,) in q.all()]
    snap = talent_snapshot(db, company_id, ids)
    names = c.people(db, company_id, [i for i in ids if i in snap])
    from modules.performance.common import config
    labels = config(db, company_id)["nine_box"]["labels"]
    boxes = {i: {"box": i, "label": labels.get(str(i)), "people": []} for i in range(1, 10)}
    unplaced = []
    pools = _pool_names_by_employee(db, company_id, list(snap))
    successor_of = _successor_positions(db, company_id, list(snap))
    for eid, s in snap.items():
        p = {**c.person_ref(names.get(eid)), **s, "pools": pools.get(eid, []), "successor_for": successor_of.get(eid, [])}
        if s["box"]:
            boxes[s["box"]]["people"].append(p)
        else:
            unplaced.append(p)
    for b in boxes.values():
        b["people"].sort(key=lambda x: (-(x["rating"] or 0), x["name"] or ""))
    return {"boxes": list(boxes.values()), "unplaced": unplaced, "reviewed": len(snap)}


def _pool_names_by_employee(db: Session, company_id: int, ids: list[int]) -> dict[int, list[str]]:
    if not ids:
        return {}
    rows = (db.query(TalentPoolMember.employee_id, TalentPool.name).join(TalentPool, TalentPool.id == TalentPoolMember.pool_id)
            .filter(TalentPoolMember.company_id == company_id, TalentPoolMember.employee_id.in_(ids),
                    TalentPool.active.is_(True)).all())
    out: dict[int, list] = {}
    for eid, name in rows:
        out.setdefault(eid, []).append(name)
    return out


def _successor_positions(db: Session, company_id: int, ids: list[int]) -> dict[int, list[dict]]:
    if not ids:
        return {}
    rows = (db.query(SuccessionCandidate.employee_id, SuccessionCandidate.readiness, Position.title, Position.code)
            .join(SuccessionPlan, SuccessionPlan.id == SuccessionCandidate.plan_id)
            .join(Position, Position.id == SuccessionPlan.position_id)
            .filter(SuccessionCandidate.company_id == company_id, SuccessionCandidate.employee_id.in_(ids),
                    SuccessionCandidate.status == "active", SuccessionPlan.status == "active").all())
    out: dict[int, list] = {}
    for eid, readiness, title, code in rows:
        out.setdefault(eid, []).append({"title": title, "code": code, "readiness": readiness,
                                        "readiness_label": READINESS.get(readiness)})
    return out


# ── Plans ───────────────────────────────────────────────────────────

def get_plan(db: Session, company_id: int, uuid) -> SuccessionPlan:
    p = db.query(SuccessionPlan).filter(SuccessionPlan.company_id == company_id, SuccessionPlan.uuid == uuid).first()
    if p is None:
        raise NotFound("Succession plan")
    return p


def _counts(cands) -> dict:
    out = {k: 0 for k in READINESS}
    for x in cands:
        if x.status == "active":
            out[x.readiness] = out.get(x.readiness, 0) + 1
    return out


def plan_outputs(db: Session, company_id: int, plans: list[SuccessionPlan], detail: bool = False) -> list[dict]:
    from modules.positions import service as pos
    settings = c.get_settings(db, company_id)
    positions = pos.outputs(db, company_id, [p.position for p in plans]) if plans else []
    cand_ids = [x.employee_id for p in plans for x in p.candidates if x.status == "active"]
    incumbents = [i["id"] for d in positions for i in d["incumbents"]]
    names = c.people(db, company_id, cand_ids + [p.plan_manager_id for p in plans])
    snap = talent_snapshot(db, company_id, cand_ids + incumbents) if detail else talent_snapshot(db, company_id, cand_ids)
    out = []
    for p, posd in zip(plans, positions):
        counts = _counts(p.candidates)
        strength = bench_strength(counts, settings["bench_ready_now_target"], settings["bench_total_target"])
        d = {"uuid": str(p.uuid), "position": {k: posd[k] for k in ("uuid", "code", "title", "department", "designation",
                                                                    "location", "is_critical", "fill_status", "fill_label")},
             "incumbents": posd["incumbents"], "plan_manager": c.person_ref(names.get(p.plan_manager_id)),
             "plan_manager_id": p.plan_manager_id,
             "risk_of_loss": p.risk_of_loss, "impact_of_loss": p.impact_of_loss,
             "risk_label": LEVELS.get(p.risk_of_loss), "impact_label": LEVELS.get(p.impact_of_loss),
             "urgency": urgency(p.risk_of_loss, p.impact_of_loss), "notes": p.notes,
             "review_on": p.review_on.isoformat() if p.review_on else None, "status": p.status,
             "counts": counts, "successors": sum(counts.values()), "bench": strength, "bench_label": BENCH_LABELS[strength],
             "created_at": p.created_at, "updated_at": p.updated_at}
        cands = [x for x in p.candidates if x.status == "active"]
        cands.sort(key=lambda x: (list(READINESS).index(x.readiness) if x.readiness in READINESS else 9, x.rank, x.id))
        d["candidates"] = [{"uuid": str(x.uuid), "employee": c.person_ref(names.get(x.employee_id)),
                            "employee_id": x.employee_id, "readiness": x.readiness,
                            "readiness_label": READINESS.get(x.readiness), "rank": x.rank,
                            "strengths": x.strengths, "gaps": x.gaps, "talent": snap.get(x.employee_id)} for x in cands]
        if detail:
            d["incumbent_talent"] = [snap.get(i["id"]) for i in posd["incumbents"]]
            d["actions"] = action_outputs(db, company_id, actions_for(db, company_id, candidate_ids=[x.id for x in cands]))
        out.append(d)
    return out


def list_plans(db: Session, company_id: int, *, status: str = "active", q: str | None = None,
               bench: str | None = None, urgency_filter: str | None = None) -> list[dict]:
    query = db.query(SuccessionPlan).join(Position, Position.id == SuccessionPlan.position_id).filter(
        SuccessionPlan.company_id == company_id, SuccessionPlan.status == status)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter((Position.title.ilike(like)) | (Position.code.ilike(like)))
    items = plan_outputs(db, company_id, query.order_by(Position.code).all())
    if bench:
        items = [i for i in items if i["bench"] in bench.split(",")]
    if urgency_filter:
        items = [i for i in items if i["urgency"] in urgency_filter.split(",")]
    return items


def save_plan(db: Session, company_id: int, data: dict, row: SuccessionPlan | None = None) -> SuccessionPlan:
    from modules.positions import service as pos
    if row is None:
        if not data.get("position_uuid"):
            raise BadRequest("Pick the key position.")
        position = pos.get(db, company_id, data["position_uuid"])
        if position.status == "closed":
            raise BadRequest("That position is closed.")
        if db.query(SuccessionPlan.id).filter(SuccessionPlan.company_id == company_id, SuccessionPlan.position_id == position.id,
                                              SuccessionPlan.status == "active").first():
            raise BadRequest("This position already has a succession plan.")
        row = SuccessionPlan(company_id=company_id, position_id=position.id, created_by_name=c.actor_name())
        db.add(row)
        if not position.is_critical and data.get("mark_critical", True):
            position.is_critical = True
    for key in ("risk_of_loss", "impact_of_loss"):
        if key in data:
            if data[key] not in LEVELS:
                raise BadRequest("Risk and impact are low, medium or high.")
            setattr(row, key, data[key])
    if "plan_manager_id" in data:
        if data.get("plan_manager_id"):
            c.require_employee(db, company_id, data["plan_manager_id"], "plan manager")
        row.plan_manager_id = data.get("plan_manager_id") or None
    if "notes" in data:
        row.notes = (data.get("notes") or "").strip() or None
    if "review_on" in data:
        row.review_on = data.get("review_on") or None
    db.flush()
    return row


def archive_plan(db: Session, company_id: int, row: SuccessionPlan) -> None:
    row.status = "archived"
    db.flush()


def _check_readiness(value) -> str:
    if value not in READINESS:
        raise BadRequest("Readiness is ready now, in 1-2 years or in 3+ years.")
    return value


def add_candidate(db: Session, company_id: int, plan: SuccessionPlan, data: dict) -> SuccessionCandidate:
    from modules.positions import service as pos
    if plan.status != "active":
        raise BadRequest("This plan is archived.")
    emp = c.require_employee(db, company_id, data.get("employee_id"), "successor")
    if emp.status in ("exited", "archived") or not emp.active:
        raise BadRequest("This person has left.")
    holders = {s.employee_id for s in pos.seats_of(db, company_id, [plan.position_id]).get(plan.position_id, [])}
    if emp.id in holders:
        raise BadRequest("This person already holds the position.")
    have = [x for x in plan.candidates if x.employee_id == emp.id]
    readiness = _check_readiness(data.get("readiness") or "one_two_years")
    if have and have[0].status == "active":
        raise BadRequest(f"{c.display_name(emp)} is already a successor in this plan.")
    if len([x for x in plan.candidates if x.status == "active"]) >= 10:
        raise BadRequest("A plan can have up to 10 successors.")
    rank = int(data.get("rank") or (max([x.rank for x in plan.candidates if x.status == "active"] or [0]) + 1))
    if have:
        x = have[0]
        x.status, x.readiness, x.rank = "active", readiness, rank
    else:
        x = SuccessionCandidate(company_id=company_id, plan_id=plan.id, employee_id=emp.id, readiness=readiness, rank=rank,
                                added_by_name=c.actor_name())
        db.add(x)
        plan.candidates.append(x)
    x.strengths = (data.get("strengths") or "").strip() or None
    x.gaps = (data.get("gaps") or "").strip() or None
    db.flush()
    return x


def get_candidate(db: Session, company_id: int, uuid) -> SuccessionCandidate:
    x = db.query(SuccessionCandidate).filter(SuccessionCandidate.company_id == company_id,
                                             SuccessionCandidate.uuid == uuid).first()
    if x is None:
        raise NotFound("Successor")
    return x


def update_candidate(db: Session, company_id: int, x: SuccessionCandidate, data: dict) -> SuccessionCandidate:
    if "readiness" in data:
        old = x.readiness
        x.readiness = _check_readiness(data["readiness"])
        if old != x.readiness:
            audit.event(db, "succession.readiness_changed", "succession_candidates", x.id,
                        {"from": old, "to": x.readiness}, entity_uuid=str(x.uuid))
    if "rank" in data and data["rank"] is not None:
        r = int(data["rank"])
        if not 1 <= r <= 99:
            raise BadRequest("Rank should be between 1 and 99.")
        x.rank = r
    for k in ("strengths", "gaps"):
        if k in data:
            setattr(x, k, (data.get(k) or "").strip() or None)
    db.flush()
    return x


def remove_candidate(db: Session, company_id: int, x: SuccessionCandidate) -> None:
    x.status = "removed"
    for a in db.query(DevelopmentAction).filter(DevelopmentAction.company_id == company_id,
                                                DevelopmentAction.candidate_id == x.id,
                                                DevelopmentAction.status.in_(("planned", "in_progress"))):
        a.status = "cancelled"
    db.flush()


# ── Development actions ─────────────────────────────────────────────

def actions_for(db: Session, company_id: int, *, candidate_ids=None, employee_id: int | None = None) -> list[DevelopmentAction]:
    q = db.query(DevelopmentAction).filter(DevelopmentAction.company_id == company_id,
                                           DevelopmentAction.status != "cancelled")
    if candidate_ids is not None:
        if not candidate_ids:
            return []
        q = q.filter(DevelopmentAction.candidate_id.in_(candidate_ids))
    if employee_id:
        q = q.filter(DevelopmentAction.employee_id == employee_id)
    return q.order_by(DevelopmentAction.due_date.is_(None), DevelopmentAction.due_date, DevelopmentAction.id).all()


def _linked_progress(db: Session, company_id: int, rows: list[DevelopmentAction]) -> tuple[dict, dict, dict, dict]:
    courses, enrol, objectives = {}, {}, {}
    cids = {a.course_id for a in rows if a.course_id}
    oids = {a.objective_id for a in rows if a.objective_id}
    if cids:
        try:
            from modules.learning.models import Course, Enrolment
            courses = {r.id: r.title for r in db.query(Course.id, Course.title).filter(Course.company_id == company_id, Course.id.in_(cids))}
            for e in (db.query(Enrolment).filter(Enrolment.company_id == company_id, Enrolment.course_id.in_(cids),
                                                 Enrolment.employee_id.in_({a.employee_id for a in rows}))
                      .order_by(Enrolment.id)):
                enrol[(e.employee_id, e.course_id)] = {"status": e.status, "progress": float(e.progress or 0)}
        except ImportError:
            pass
    if oids:
        try:
            from modules.performance.models import Objective
            objectives = {o.id: {"title": o.title, "progress": float(o.progress or 0), "uuid": str(o.uuid)}
                          for o in db.query(Objective).filter(Objective.company_id == company_id, Objective.id.in_(oids))}
        except ImportError:
            pass
    return courses, enrol, objectives, {}


def action_outputs(db: Session, company_id: int, rows: list[DevelopmentAction]) -> list[dict]:
    courses, enrol, objectives, _ = _linked_progress(db, company_id, rows)
    names = c.people(db, company_id, [a.mentor_id for a in rows])
    cand_uuid = {x.id: str(x.uuid) for x in db.query(SuccessionCandidate).filter(
        SuccessionCandidate.company_id == company_id, SuccessionCandidate.id.in_({a.candidate_id for a in rows if a.candidate_id} or {0}))}
    out = []
    for a in rows:
        progress = None
        linked = None
        if a.course_id:
            e = enrol.get((a.employee_id, a.course_id))
            progress = e["progress"] if e else 0.0
            linked = {"type": "course", "title": courses.get(a.course_id), "status": e["status"] if e else "not_enrolled"}
        elif a.objective_id and a.objective_id in objectives:
            o = objectives[a.objective_id]
            progress = o["progress"]
            linked = {"type": "goal", "title": o["title"], "uuid": o["uuid"]}
        if a.status == "done":
            progress = 100.0
        out.append({"uuid": str(a.uuid), "employee_id": a.employee_id, "candidate_uuid": cand_uuid.get(a.candidate_id),
                    "kind": a.kind, "kind_label": ACTION_KINDS.get(a.kind), "title": a.title,
                    "course_id": a.course_id, "objective_id": a.objective_id, "linked": linked,
                    "mentor": c.person_ref(names.get(a.mentor_id)), "mentor_id": a.mentor_id,
                    "due_date": a.due_date.isoformat() if a.due_date else None, "status": a.status,
                    "status_label": ACTION_STATUSES.get(a.status), "progress": progress, "notes": a.notes})
    return out


def add_action(db: Session, user: dict, x: SuccessionCandidate, data: dict) -> DevelopmentAction:
    company_id = user["company_id"]
    kind = data.get("kind") or "other"
    if kind not in ACTION_KINDS:
        raise BadRequest("Pick the kind of action.")
    title = (data.get("title") or "").strip()
    a = DevelopmentAction(company_id=company_id, employee_id=x.employee_id, candidate_id=x.id, kind=kind,
                          created_by_name=c.actor_name(), status="planned")
    due = data.get("due_date") or None
    if kind == "course":
        from modules.learning.models import Course
        course = db.query(Course).filter(Course.company_id == company_id, Course.id == data.get("course_id")).first()
        if course is None:
            raise BadRequest("Pick the course.")
        a.course_id = course.id
        title = title or course.title
        if data.get("enrol", True):
            from modules.learning import service as learning
            learning.enrol(db, company_id, course, x.employee_id, source="manager", by=c.actor_name(),
                           due_date=date.fromisoformat(due) if isinstance(due, str) else due)
            a.status = "in_progress"
    elif kind == "goal":
        from modules.performance.models import Objective
        if data.get("objective_id"):
            obj = db.query(Objective).filter(Objective.company_id == company_id, Objective.id == data["objective_id"],
                                             Objective.owner_employee_id == x.employee_id).first()
            if obj is None:
                raise BadRequest("Pick one of this person's goals.")
        else:
            if not title:
                raise BadRequest("Give the goal a title.")
            from modules.performance import okr
            from modules.performance.common import Actor
            period = okr.current_period(db, company_id) or okr.current_period(db, company_id, "year")
            if period is None:
                raise BadRequest("There is no open goal period to add the goal to.")
            actor = Actor(company_id=company_id, employee_id=None, role="admin", name=c.actor_name() or "HR")
            obj = okr.create_objective(db, actor, {"level": "individual", "title": title, "period_id": period.id,
                                                   "owner_employee_id": x.employee_id, "due_date": due,
                                                   "description": data.get("notes"), "visibility": "team"})
        a.objective_id = obj.id
        title = title or obj.title
        a.status = "in_progress"
    if not title:
        raise BadRequest("Give the action a title.")
    a.title = title[:250]
    if data.get("mentor_id"):
        c.require_employee(db, company_id, data["mentor_id"], "mentor")
        a.mentor_id = data["mentor_id"]
    a.due_date = date.fromisoformat(due) if isinstance(due, str) else due
    a.notes = (data.get("notes") or "").strip() or None
    db.add(a)
    db.flush()
    c.notify(db, company_id, x.employee_id, f"A development step was added for you: {a.title}.", "development")
    return a


def get_action(db: Session, company_id: int, uuid) -> DevelopmentAction:
    a = db.query(DevelopmentAction).filter(DevelopmentAction.company_id == company_id, DevelopmentAction.uuid == uuid).first()
    if a is None:
        raise NotFound("Development action")
    return a


def update_action(db: Session, company_id: int, a: DevelopmentAction, data: dict) -> DevelopmentAction:
    if "status" in data:
        if data["status"] not in ACTION_STATUSES:
            raise BadRequest("Pick a status.")
        a.status = data["status"]
    if "title" in data and (data.get("title") or "").strip():
        a.title = data["title"].strip()[:250]
    if "due_date" in data:
        a.due_date = data.get("due_date") or None
    if "notes" in data:
        a.notes = (data.get("notes") or "").strip() or None
    db.flush()
    return a


# ── Bench strength ──────────────────────────────────────────────────

def bench_report(db: Session, company_id: int) -> dict:
    settings = c.get_settings(db, company_id)
    plans = plan_outputs(db, company_id, db.query(SuccessionPlan).filter(
        SuccessionPlan.company_id == company_id, SuccessionPlan.status == "active").all())
    critical = (db.query(Position).filter(Position.company_id == company_id, Position.is_critical.is_(True),
                                          Position.status != "closed").all())
    planned_ids = {p["position"]["uuid"] for p in plans}
    uncovered = [{"uuid": str(p.uuid), "code": p.code, "title": p.title} for p in critical if str(p.uuid) not in planned_ids]
    by_strength = {k: 0 for k in BENCH_LABELS}
    for p in plans:
        by_strength[p["bench"]] += 1
    with_ready = sum(1 for p in plans if p["counts"]["ready_now"] > 0)
    key_positions = len(critical) or len(plans)
    by_dept: dict[str, dict] = {}
    for p in plans:
        dname = p["position"]["department"] or "No department"
        d = by_dept.setdefault(dname, {"department": dname, "plans": 0, "ready_now": 0, "one_two_years": 0,
                                       "three_plus_years": 0, "strong": 0, "urgent": 0})
        d["plans"] += 1
        for k in READINESS:
            d[k] += p["counts"][k]
        d["strong"] += p["bench"] == "strong"
        d["urgent"] += p["urgency"] == "urgent"
    successors = {x["employee_id"] for p in plans for x in p["candidates"]}
    return {
        "key_positions": key_positions, "plans": len(plans), "uncovered": uncovered,
        "coverage_pct": round(100 * with_ready / key_positions, 1) if key_positions else 0.0,
        "by_strength": by_strength, "strength_labels": BENCH_LABELS,
        "ready_now": sum(p["counts"]["ready_now"] for p in plans),
        "one_two_years": sum(p["counts"]["one_two_years"] for p in plans),
        "three_plus_years": sum(p["counts"]["three_plus_years"] for p in plans),
        "unique_successors": len(successors),
        "urgent": [p for p in plans if p["urgency"] == "urgent" and p["bench"] in ("weak", "none", "moderate")],
        "by_department": sorted(by_dept.values(), key=lambda d: d["department"]),
        "targets": {"ready_now": settings["bench_ready_now_target"], "total": settings["bench_total_target"]},
        "plans_list": [{k: p[k] for k in ("uuid", "position", "counts", "successors", "bench", "bench_label", "urgency",
                                          "risk_label", "impact_label", "incumbents")} for p in plans],
    }


# ── Talent pools ────────────────────────────────────────────────────

def get_pool(db: Session, company_id: int, uuid) -> TalentPool:
    p = db.query(TalentPool).filter(TalentPool.company_id == company_id, TalentPool.uuid == uuid).first()
    if p is None:
        raise NotFound("Talent pool")
    return p


def pool_outputs(db: Session, company_id: int, pools: list[TalentPool], detail: bool = False) -> list[dict]:
    ids = [m.employee_id for p in pools for m in p.members] + [p.manager_employee_id for p in pools]
    names = c.people(db, company_id, ids)
    snap = talent_snapshot(db, company_id, [m.employee_id for p in pools for m in p.members]) if detail else {}
    labels = c.org_labels(db, company_id, departments=[n["department_id"] for n in names.values()],
                          designations=[n["designation_id"] for n in names.values()]) if detail else None
    out = []
    for p in pools:
        d = {"uuid": str(p.uuid), "name": p.name, "description": p.description, "active": p.active,
             "manager": c.person_ref(names.get(p.manager_employee_id)), "manager_employee_id": p.manager_employee_id,
             "members_count": len(p.members),
             "preview": [c.person_ref(names.get(m.employee_id)) for m in p.members[:6] if names.get(m.employee_id)]}
        if detail:
            d["members"] = [{"uuid": str(m.uuid), "employee": c.person_ref(names.get(m.employee_id)),
                             "employee_id": m.employee_id, "note": m.note, "added_by_name": m.added_by_name,
                             "added_at": m.created_at, "talent": snap.get(m.employee_id),
                             "designation": labels["designation"].get((names.get(m.employee_id) or {}).get("designation_id")),
                             "department": labels["department"].get((names.get(m.employee_id) or {}).get("department_id"))}
                            for m in p.members if names.get(m.employee_id)]
        out.append(d)
    return out


def save_pool(db: Session, company_id: int, data: dict, row: TalentPool | None = None) -> TalentPool:
    name = (data.get("name") or (row.name if row else "")).strip()
    if not name:
        raise BadRequest("Give the pool a name.")
    clash = db.query(TalentPool.id).filter(TalentPool.company_id == company_id, func.lower(TalentPool.name) == name.lower())
    if row is not None:
        clash = clash.filter(TalentPool.id != row.id)
    if clash.first():
        raise BadRequest("Another pool already has this name.")
    if row is None:
        row = TalentPool(company_id=company_id, name=name)
        db.add(row)
    row.name = name[:150]
    if "description" in data:
        row.description = (data.get("description") or "").strip() or None
    if "manager_employee_id" in data:
        if data.get("manager_employee_id"):
            c.require_employee(db, company_id, data["manager_employee_id"], "pool manager")
        row.manager_employee_id = data.get("manager_employee_id") or None
    if "active" in data:
        row.active = bool(data["active"])
    db.flush()
    return row


def add_members(db: Session, company_id: int, pool: TalentPool, employee_ids: list, note: str | None = None) -> int:
    have = {m.employee_id for m in pool.members}
    n = 0
    for eid in employee_ids:
        emp = c.require_employee(db, company_id, eid)
        if emp.id in have:
            continue
        m = TalentPoolMember(company_id=company_id, pool_id=pool.id, employee_id=emp.id, note=(note or "").strip() or None,
                             added_by_name=c.actor_name())
        db.add(m)
        pool.members.append(m)
        have.add(emp.id)
        n += 1
    db.flush()
    return n


def remove_member(db: Session, company_id: int, pool: TalentPool, member_uuid) -> None:
    m = next((m for m in pool.members if str(m.uuid) == str(member_uuid)), None)
    if m is None:
        raise NotFound("Pool member")
    pool.members.remove(m)
    db.delete(m)
    db.flush()


def pool_suggestions(db: Session, company_id: int, pool: TalentPool) -> list[dict]:
    """People in the high-potential boxes of the nine-box who are not in the pool yet."""
    nb = nine_box(db, company_id)
    have = {m.employee_id for m in pool.members}
    out = []
    for b in nb["boxes"]:
        if b["box"] in HIGH_POTENTIAL_BOXES:
            out += [{**p, "box_label": b["label"]} for p in b["people"] if p["id"] not in have]
    return out[:30]
