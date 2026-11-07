"""Goals and OKRs: periods, objectives at company / department / team / individual level,
key results (number, %, milestones, yes/no), alignment (a child objective counts as one
more key result of its parent), check-ins with confidence, and planned check-in dates
with on-time / late / missed flags.

Progress rolls up: key result -> objective -> parent objective, on every change.
"""
from __future__ import annotations

import uuid as uuid_mod
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload, selectinload

from core import audit
from core.errors import BadRequest, Forbidden, NotFound
from modules.performance import calc
from modules.performance.common import (
    Actor, config, employee, employees_by_id, get_by_uuid, is_manager_of, name_of, notify, person, reportee_ids, today,
)
from modules.performance.models import GoalCheckin, GoalPeriod, KeyResult, Objective

LEVELS = ("company", "department", "team", "individual")
KR_KINDS = ("number", "percent", "milestone", "boolean")
# which levels a level may align to
ALIGN_TO = {"department": {"company"}, "team": {"company", "department"},
            "individual": {"company", "department", "team"}, "company": {"company"}}


# ── Periods ─────────────────────────────────────────────────────────

def _fy_quarters(start_month: int, year: int) -> list[tuple[str, date, date]]:
    out = []
    for q in range(4):
        m0 = (start_month - 1 + q * 3) % 12 + 1
        y0 = year + (start_month - 1 + q * 3) // 12
        m1 = (m0 - 1 + 3) % 12 + 1
        y1 = y0 + (m0 - 1 + 3) // 12
        end = date(y1, m1, 1) - timedelta(days=1)
        fy = f"FY{str(year)[2:]}-{str(year + 1)[2:]}" if start_month != 1 else str(year)
        out.append((f"Q{q + 1} {fy}", date(y0, m0, 1), end))
    return out


def ensure_periods(db: Session, company_id: int) -> None:
    """Make sure this and next financial year's quarters, halves and the year exist."""
    cfg = config(db, company_id)["okr"]
    start_month = int(cfg.get("cycle_start_month") or 4)
    t = today(db, company_id)
    fy_year = t.year if t.month >= start_month else t.year - 1
    have = {(p.start_date, p.end_date) for p in db.query(GoalPeriod).filter(GoalPeriod.company_id == company_id).all()}
    for year in (fy_year, fy_year + 1):
        qs = _fy_quarters(start_month, year)
        fy = qs[0][0].split(" ", 1)[1]
        items = [(n, s, e, "quarter") for n, s, e in qs]
        items += [(f"H1 {fy}", qs[0][1], qs[1][2], "half"), (f"H2 {fy}", qs[2][1], qs[3][2], "half"),
                  (f"Year {fy}", qs[0][1], qs[3][2], "year")]
        for n, s, e, kind in items:
            if (s, e) not in have:
                db.add(GoalPeriod(company_id=company_id, name=n, kind=kind, start_date=s, end_date=e))
    db.flush()


def list_periods(db: Session, company_id: int) -> list[dict]:
    ensure_periods(db, company_id)
    t = today(db, company_id)
    rows = db.query(GoalPeriod).filter(GoalPeriod.company_id == company_id).order_by(GoalPeriod.start_date, GoalPeriod.end_date).all()
    return [period_out(p, t) for p in rows]


def period_out(p: GoalPeriod, t: date | None = None) -> dict:
    current = bool(t and p.start_date <= t <= p.end_date)
    return {"id": p.id, "uuid": str(p.uuid), "name": p.name, "kind": p.kind, "start_date": p.start_date.isoformat(),
            "end_date": p.end_date.isoformat(), "status": p.status, "current": current}


def current_period(db: Session, company_id: int, kind: str = "quarter") -> GoalPeriod | None:
    ensure_periods(db, company_id)
    t = today(db, company_id)
    return (db.query(GoalPeriod).filter(GoalPeriod.company_id == company_id, GoalPeriod.kind == kind,
                                        GoalPeriod.start_date <= t, GoalPeriod.end_date >= t).first())


def save_period(db: Session, actor: Actor, data: dict, row: GoalPeriod | None = None) -> GoalPeriod:
    actor.require()
    name = (data.get("name") or "").strip()
    s, e = data.get("start_date"), data.get("end_date")
    if not name or not s or not e:
        raise BadRequest("Give the period a name, a start and an end date.")
    if e < s:
        raise BadRequest("The period must end after it starts.")
    if data.get("kind") not in ("quarter", "half", "year", "custom"):
        raise BadRequest("Pick quarter, half year, year or custom.")
    if row is None:
        row = GoalPeriod(company_id=actor.company_id)
        db.add(row)
    row.name, row.kind, row.start_date, row.end_date = name[:120], data["kind"], s, e
    if data.get("status") in ("open", "closed"):
        row.status = data["status"]
    db.flush()
    return row


# ── Access ──────────────────────────────────────────────────────────

def can_edit(db: Session, actor: Actor, obj: Objective) -> bool:
    if actor.is_admin:
        return True
    if obj.level == "company":
        return False
    if obj.owner_employee_id and obj.owner_employee_id == actor.employee_id:
        return True
    if obj.owner_employee_id and is_manager_of(db, actor, obj.owner_employee_id):
        return True
    if obj.level == "department" and actor.employee_id:
        from modules.departments.models import Department
        head = db.query(Department.head_employee_id).filter(Department.id == obj.department_id).scalar()
        return head == actor.employee_id
    return False


def can_see(db: Session, actor: Actor, obj: Objective) -> bool:
    if obj.level in ("company", "department") or obj.visibility == "everyone" or actor.is_admin:
        return True
    if obj.owner_employee_id == actor.employee_id:
        return True
    return bool(obj.owner_employee_id and is_manager_of(db, actor, obj.owner_employee_id))


def _can_checkin(db: Session, actor: Actor, obj: Objective, kr: KeyResult | None) -> bool:
    if can_edit(db, actor, obj):
        return True
    return bool(kr and kr.owner_employee_id and kr.owner_employee_id == actor.employee_id)


# ── Roll-up ─────────────────────────────────────────────────────────

def _expected(db: Session, obj: Objective) -> float:
    p = obj.period
    end = obj.due_date or p.end_date
    return calc.expected_progress(p.start_date, end, today(db, obj.company_id))


def recompute_kr(db: Session, kr: KeyResult, statuses: list[dict], auto: bool) -> None:
    kr.progress = Decimal(str(calc.kr_progress(kr.kind, kr.start_value, kr.target_value, kr.current_value, kr.milestones)))
    if auto and kr.objective is not None:
        p = kr.objective.period
        exp = calc.expected_progress(kr.start_date or p.start_date, kr.due_date or p.end_date, today(db, kr.company_id))
        if kr.status_key not in ("paused",):
            kr.status_key = calc.auto_status(float(kr.progress), exp, statuses)


def recompute(db: Session, obj: Objective, _seen: set | None = None) -> None:
    """Recalculate an objective from its key results and aligned children, then its parents."""
    seen = _seen or set()
    if obj.id in seen:
        return
    seen.add(obj.id)
    cfg = config(db, obj.company_id)["okr"]
    statuses, auto = cfg.get("statuses") or calc.DEFAULT_STATUSES, bool(cfg.get("auto_status", True))
    krs = [(float(k.progress), float(k.weight)) for k in obj.key_results]
    children = (db.query(Objective).filter(Objective.company_id == obj.company_id, Objective.parent_id == obj.id,
                                           Objective.archived_at.is_(None)).all()) if obj.id else []
    kids = [(float(c.progress), float(c.weight)) for c in children]
    obj.progress = Decimal(str(calc.objective_progress(krs, kids)))
    if auto and not obj.status_manual:
        obj.status_key = calc.auto_status(float(obj.progress), _expected(db, obj), statuses)
    db.flush()
    if obj.parent_id:
        parent = db.query(Objective).filter(Objective.company_id == obj.company_id, Objective.id == obj.parent_id).first()
        if parent is not None:
            recompute(db, parent, seen)


def refresh_statuses(db: Session, company_id: int, period_id: int | None = None) -> int:
    """Re-run the automatic status of every objective (statuses depend on the date)."""
    cfg = config(db, company_id)["okr"]
    statuses, auto = cfg.get("statuses") or calc.DEFAULT_STATUSES, bool(cfg.get("auto_status", True))
    q = db.query(Objective).options(selectinload(Objective.key_results)).filter(
        Objective.company_id == company_id, Objective.archived_at.is_(None))
    if period_id:
        q = q.filter(Objective.period_id == period_id)
    n = 0
    for obj in q.all():
        for kr in obj.key_results:
            recompute_kr(db, kr, statuses, auto)
        if auto and not obj.status_manual:
            obj.status_key = calc.auto_status(float(obj.progress), _expected(db, obj), statuses)
        n += 1
    db.flush()
    return n


# ── Output ──────────────────────────────────────────────────────────

def kr_out(db: Session, kr: KeyResult, checkins: list[GoalCheckin] | None = None, t: date | None = None,
           grace: int = 2, owners: dict | None = None) -> dict:
    obj = kr.objective
    start = kr.start_date or obj.period.start_date
    end = kr.due_date or obj.period.end_date
    plan = calc.planned_checkins(start, end, kr.checkin_frequency)
    done_dates = [c.checkin_date for c in (checkins or []) if c.key_result_id == kr.id]
    flags = calc.checkin_flags(plan, done_dates, t or date.today(), grace)
    exp = calc.expected_progress(start, end, t or date.today())
    owner = (owners or {}).get(kr.owner_employee_id) if owners is not None else kr.owner
    return {
        "id": kr.id, "uuid": str(kr.uuid), "title": kr.title, "kind": kr.kind, "unit": kr.unit,
        "start_value": float(kr.start_value), "target_value": float(kr.target_value),
        "current_value": float(kr.current_value), "milestones": kr.milestones or [], "weight": float(kr.weight),
        "owner": person(owner), "start_date": start.isoformat(), "due_date": end.isoformat(),
        "checkin_frequency": kr.checkin_frequency, "progress": float(kr.progress), "confidence": kr.confidence,
        "status_key": kr.status_key, "expected_progress": exp, "deviation": calc.r2(max(exp - float(kr.progress), 0)),
        "checkin_plan": flags,
        "missed_checkins": sum(1 for f in flags if f["state"] == "missed"),
        "late_checkins": sum(1 for f in flags if f["state"] == "late"),
        "next_checkin": next((f["planned"] for f in flags if f["state"] == "upcoming"), None),
    }


def objective_out(db: Session, actor: Actor, obj: Objective, *, detail: bool = False, child_counts: dict | None = None,
                  checkins: list | None = None, t: date | None = None) -> dict:
    t = t or today(db, obj.company_id)
    grace = int(config(db, obj.company_id)["okr"].get("grace_days") or 2)
    if checkins is None:
        checkins = _checkins_for(db, obj.company_id, [obj.id])
    owners = employees_by_id(db, obj.company_id, [k.owner_employee_id for k in obj.key_results])
    krs = [kr_out(db, k, checkins, t, grace, owners) for k in obj.key_results]
    out = {
        "id": obj.id, "uuid": str(obj.uuid), "title": obj.title, "description": obj.description, "level": obj.level,
        "period": {"id": obj.period.id, "name": obj.period.name, "start_date": obj.period.start_date.isoformat(),
                   "end_date": obj.period.end_date.isoformat()},
        "owner": person(obj.owner),
        "department": {"id": obj.department.id, "name": obj.department.department_name} if obj.department else None,
        "parent_id": obj.parent_id, "weight": float(obj.weight), "progress": float(obj.progress),
        "status_key": obj.status_key, "status_manual": obj.status_manual, "confidence": obj.confidence,
        "due_date": obj.due_date.isoformat() if obj.due_date else None, "visibility": obj.visibility,
        "expected_progress": _expected(db, obj),
        "key_results": krs,
        "children_count": (child_counts or {}).get(obj.id, 0),
        "missed_checkins": sum(k["missed_checkins"] for k in krs),
        "can_edit": can_edit(db, actor, obj),
        "archived": obj.archived_at is not None,
        "created_at": obj.created_at.isoformat() if obj.created_at else None,
    }
    if detail:
        parent = None
        if obj.parent_id:
            p = db.query(Objective).filter(Objective.company_id == obj.company_id, Objective.id == obj.parent_id).first()
            if p:
                parent = {"id": p.id, "uuid": str(p.uuid), "title": p.title, "level": p.level, "progress": float(p.progress)}
        kids = (db.query(Objective).filter(Objective.company_id == obj.company_id, Objective.parent_id == obj.id,
                                           Objective.archived_at.is_(None)).all())
        hist = (db.query(GoalCheckin).filter(GoalCheckin.company_id == obj.company_id, GoalCheckin.objective_id == obj.id)
                .order_by(GoalCheckin.checkin_date.desc(), GoalCheckin.id.desc()).limit(100).all())
        kr_titles = {k.id: k.title for k in obj.key_results}
        out["parent"] = parent
        out["children"] = [{"id": c.id, "uuid": str(c.uuid), "title": c.title, "level": c.level,
                            "progress": float(c.progress), "weight": float(c.weight), "status_key": c.status_key,
                            "owner": person(c.owner)} for c in kids]
        out["checkins"] = [{
            "id": c.id, "uuid": str(c.uuid), "key_result_id": c.key_result_id, "key_result": kr_titles.get(c.key_result_id),
            "date": c.checkin_date.isoformat(), "value": float(c.value) if c.value is not None else None,
            "progress": float(c.progress) if c.progress is not None else None, "confidence": c.confidence,
            "status_key": c.status_key, "note": c.note, "author": c.author_name,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        } for c in hist]
    return out


def _base_query(db: Session, company_id: int):
    return (db.query(Objective)
            .options(joinedload(Objective.period), joinedload(Objective.owner), joinedload(Objective.department),
                     selectinload(Objective.key_results))
            .filter(Objective.company_id == company_id))


def list_objectives(db: Session, actor: Actor, *, period_id: int | None = None, level: str | None = None,
                    scope: str | None = None, owner_id: int | None = None, department_id: int | None = None,
                    q: str | None = None, include_archived: bool = False) -> list[dict]:
    """scope: mine (owned by me or with a key result I own), team (my reportees' and my team
    objectives), company (company + department), all."""
    query = _base_query(db, actor.company_id)
    if not include_archived:
        query = query.filter(Objective.archived_at.is_(None))
    if period_id:
        query = query.filter(Objective.period_id == period_id)
    if level:
        query = query.filter(Objective.level == level)
    if department_id:
        query = query.filter(Objective.department_id == department_id)
    if owner_id:
        query = query.filter(Objective.owner_employee_id == owner_id)
    if q:
        query = query.filter(Objective.title.ilike(f"%{q.strip()}%"))
    if scope == "mine":
        if actor.employee_id is None:
            return []
        kr_obj_ids = [i for (i,) in db.query(KeyResult.objective_id).filter(
            KeyResult.company_id == actor.company_id, KeyResult.owner_employee_id == actor.employee_id).all()]
        query = query.filter(or_(Objective.owner_employee_id == actor.employee_id, Objective.id.in_(kr_obj_ids or [0])))
        query = query.filter(Objective.level.in_(("individual", "team")) | Objective.id.in_(kr_obj_ids or [0]))
    elif scope == "team":
        ids = reportee_ids(db, actor.company_id, actor.employee_id, deep=True)
        if actor.employee_id:
            ids.add(actor.employee_id)
        if not ids and not actor.is_admin:
            return []
        query = query.filter(Objective.owner_employee_id.in_(ids or {0}), Objective.level.in_(("team", "individual")))
    elif scope == "company":
        query = query.filter(Objective.level.in_(("company", "department")))
    rows = query.order_by(Objective.level, Objective.created_at).all()
    rows = [o for o in rows if can_see(db, actor, o)]
    counts = _child_counts(db, actor.company_id, [o.id for o in rows])
    checkins = _checkins_for(db, actor.company_id, [o.id for o in rows])
    t = today(db, actor.company_id)
    order = {lv: i for i, lv in enumerate(LEVELS)}
    rows.sort(key=lambda o: (order.get(o.level, 9), o.created_at))
    return [objective_out(db, actor, o, child_counts=counts, checkins=checkins, t=t) for o in rows]


def _child_counts(db: Session, company_id: int, ids: list[int]) -> dict:
    if not ids:
        return {}
    from sqlalchemy import func
    return dict(db.query(Objective.parent_id, func.count(Objective.id))
                .filter(Objective.company_id == company_id, Objective.parent_id.in_(ids), Objective.archived_at.is_(None))
                .group_by(Objective.parent_id).all())


def _checkins_for(db: Session, company_id: int, objective_ids: list[int]) -> list[GoalCheckin]:
    if not objective_ids:
        return []
    return (db.query(GoalCheckin.key_result_id, GoalCheckin.checkin_date)
            .filter(GoalCheckin.company_id == company_id, GoalCheckin.objective_id.in_(objective_ids),
                    GoalCheckin.key_result_id.isnot(None)).all())


def tree(db: Session, actor: Actor, period_id: int) -> list[dict]:
    """The alignment tree of a period: top-level objectives with nested children."""
    rows = (_base_query(db, actor.company_id)
            .filter(Objective.period_id == period_id, Objective.archived_at.is_(None)).all())
    visible = {o.id: o for o in rows if can_see(db, actor, o)}
    t = today(db, actor.company_id)
    checkins = _checkins_for(db, actor.company_id, list(visible))
    nodes = {i: {**objective_out(db, actor, o, checkins=checkins, t=t), "children": []} for i, o in visible.items()}
    roots = []
    order = {lv: i for i, lv in enumerate(LEVELS)}
    for i, o in sorted(visible.items(), key=lambda kv: (order.get(kv[1].level, 9), kv[1].created_at)):
        if o.parent_id and o.parent_id in nodes:
            nodes[o.parent_id]["children"].append(nodes[i])
        else:
            roots.append(nodes[i])
    for n in nodes.values():
        n["children_count"] = len(n["children"])
    return roots


def get(db: Session, actor: Actor, uuid) -> Objective:
    obj = _base_query(db, actor.company_id).filter(Objective.uuid == uuid).first()
    if obj is None or not can_see(db, actor, obj):
        raise NotFound("Objective")
    return obj


# ── Changes ─────────────────────────────────────────────────────────

def _check_parent(db: Session, company_id: int, obj: Objective | None, level: str, period_id: int, parent_id: int | None):
    if not parent_id:
        return None
    parent = db.query(Objective).filter(Objective.company_id == company_id, Objective.id == parent_id).first()
    if parent is None or parent.archived_at is not None:
        raise BadRequest("The objective to align with was not found.")
    if parent.level not in ALIGN_TO.get(level, set()):
        raise BadRequest(f"A {level} objective can't align to a {parent.level} objective.")
    if parent.period_id != period_id:
        p = db.get(GoalPeriod, period_id)
        if not (p and parent.period.start_date <= p.start_date and p.end_date <= parent.period.end_date):
            raise BadRequest("Align to an objective in the same period (or a longer period that contains it).")
    if obj is not None:
        cursor, seen = parent, set()
        while cursor is not None and cursor.id not in seen:
            if cursor.id == obj.id:
                raise BadRequest("That would make a loop: the objective is already above this one.")
            seen.add(cursor.id)
            cursor = db.query(Objective).filter(Objective.company_id == company_id, Objective.id == cursor.parent_id).first() if cursor.parent_id else None
    return parent


def _clean_milestones(ms: list | None) -> list[dict]:
    out = []
    for i, m in enumerate(ms or []):
        title = (m.get("title") or "").strip()
        if not title:
            continue
        out.append({"id": m.get("id") or uuid_mod.uuid4().hex[:8], "title": title[:200], "due_date": m.get("due_date") or None,
                    "weight": float(m.get("weight") or 1), "done": bool(m.get("done")), "done_on": m.get("done_on")})
    if len(out) > 10:
        raise BadRequest("A key result can have at most 10 milestones.")
    return out


def _apply_kr(kr: KeyResult, d: dict) -> None:
    title = (d.get("title") or "").strip()
    if not title:
        raise BadRequest("Every key result needs a title.")
    kind = d.get("kind") or "number"
    if kind not in KR_KINDS:
        raise BadRequest("Key result type must be number, percentage, milestones or yes/no.")
    kr.title, kr.kind = title[:300], kind
    kr.unit = (d.get("unit") or None) if kind == "number" else ("%" if kind == "percent" else None)
    if kind in ("number", "percent"):
        kr.start_value = Decimal(str(d.get("start_value") or 0))
        kr.target_value = Decimal(str(d.get("target_value") if d.get("target_value") is not None else 100))
        if kr.target_value == kr.start_value:
            raise BadRequest(f"“{title}”: the target must differ from the starting value.")
        if d.get("current_value") is not None and kr.id is None:
            kr.current_value = Decimal(str(d["current_value"]))
        elif kr.id is None:
            kr.current_value = kr.start_value
    elif kind == "boolean":
        kr.start_value, kr.target_value = Decimal(0), Decimal(1)
        if kr.id is None:
            kr.current_value = Decimal(1 if d.get("current_value") else 0)
    else:
        kr.start_value, kr.target_value = Decimal(0), Decimal(100)
        kr.milestones = _clean_milestones(d.get("milestones"))
        if not kr.milestones:
            raise BadRequest(f"“{title}”: add at least one milestone.")
    w = d.get("weight")
    kr.weight = Decimal(str(w if w not in (None, "") else 1))
    if kr.weight < 0:
        raise BadRequest("Weights can't be negative.")
    kr.owner_employee_id = d.get("owner_employee_id") or kr.owner_employee_id
    kr.start_date = d.get("start_date") or None
    kr.due_date = d.get("due_date") or None
    if kr.start_date and kr.due_date and kr.due_date < kr.start_date:
        raise BadRequest(f"“{title}”: the due date is before the start date.")
    freq = d.get("checkin_frequency") or "weekly"
    if freq not in calc.FREQUENCY_DAYS:
        raise BadRequest("Check-ins can be weekly, fortnightly, monthly or none.")
    kr.checkin_frequency = freq


def create_objective(db: Session, actor: Actor, data: dict) -> Objective:
    level = data.get("level")
    if level not in LEVELS:
        raise BadRequest("Pick the level: company, department, team or individual.")
    title = (data.get("title") or "").strip()
    if not title:
        raise BadRequest("Give the objective a title.")
    period = db.query(GoalPeriod).filter(GoalPeriod.company_id == actor.company_id, GoalPeriod.id == data.get("period_id")).first()
    if period is None:
        raise BadRequest("Pick the period.")
    if period.status == "closed":
        raise BadRequest("That period is closed.")
    owner_id = data.get("owner_employee_id") or actor.employee_id
    if level == "company":
        actor.require()
    elif level == "department":
        if not data.get("department_id"):
            raise BadRequest("Pick the department.")
        if not actor.is_admin:
            from modules.departments.models import Department
            head = db.query(Department.head_employee_id).filter(Department.id == data["department_id"]).scalar()
            if head != actor.employee_id and actor.role != "manager":
                raise Forbidden("Only administrators, managers and department heads can add department objectives.")
    else:
        if owner_id is None:
            raise BadRequest("Pick who owns this objective.")
        if not actor.is_admin and owner_id != actor.employee_id and not is_manager_of(db, actor, owner_id):
            raise Forbidden("You can add objectives for yourself and your team.")
    if owner_id:
        employee(db, actor.company_id, owner_id)
    obj = Objective(company_id=actor.company_id, level=level, title=title[:300], description=data.get("description"),
                    period_id=period.id, owner_employee_id=owner_id,
                    department_id=data.get("department_id") or None,
                    weight=Decimal(str(data.get("weight") or 1)), due_date=data.get("due_date") or None,
                    visibility=data.get("visibility") or "everyone", created_by_name=actor.name)
    if obj.visibility not in ("everyone", "team", "private"):
        raise BadRequest("Visibility must be everyone, team or private.")
    if level == "department" and obj.department_id is None:
        raise BadRequest("Pick the department.")
    if level in ("team", "individual") and obj.department_id is None and owner_id:
        obj.department_id = employee(db, actor.company_id, owner_id).department_id
    _check_parent(db, actor.company_id, None, level, period.id, data.get("parent_id"))
    obj.parent_id = data.get("parent_id") or None
    db.add(obj)
    db.flush()
    for i, d in enumerate(data.get("key_results") or []):
        kr = KeyResult(company_id=actor.company_id, objective_id=obj.id, sort_order=i)
        kr.objective = obj
        _apply_kr(kr, {**d, "owner_employee_id": d.get("owner_employee_id") or owner_id})
        db.add(kr)
    db.flush()
    db.refresh(obj)
    cfg = config(db, actor.company_id)["okr"]
    for kr in obj.key_results:
        recompute_kr(db, kr, cfg.get("statuses"), bool(cfg.get("auto_status", True)))
    recompute(db, obj)
    if obj.owner_employee_id and obj.owner_employee_id != actor.employee_id:
        notify(db, actor.company_id, obj.owner_employee_id, f"{actor.name} added an objective for you: {obj.title}")
    return obj


def update_objective(db: Session, actor: Actor, obj: Objective, data: dict) -> Objective:
    if not can_edit(db, actor, obj):
        raise Forbidden("You can't change this objective.")
    if "title" in data:
        t = (data.get("title") or "").strip()
        if not t:
            raise BadRequest("Give the objective a title.")
        obj.title = t[:300]
    for k in ("description", "due_date", "visibility"):
        if k in data:
            setattr(obj, k, data[k] or (None if k != "visibility" else "everyone"))
    if "weight" in data and data["weight"] is not None:
        obj.weight = Decimal(str(data["weight"]))
    if "owner_employee_id" in data and data["owner_employee_id"] and data["owner_employee_id"] != obj.owner_employee_id:
        employee(db, actor.company_id, data["owner_employee_id"])
        obj.owner_employee_id = data["owner_employee_id"]
    if "department_id" in data and obj.level == "department" and data["department_id"]:
        obj.department_id = data["department_id"]
    if "status_key" in data:
        if data["status_key"]:
            keys = {s["key"] for s in config(db, actor.company_id)["okr"]["statuses"]}
            if data["status_key"] not in keys:
                raise BadRequest("Unknown status.")
            obj.status_key, obj.status_manual = data["status_key"], True
        else:
            obj.status_manual = False
    old_parent = obj.parent_id
    if "parent_id" in data:
        _check_parent(db, actor.company_id, obj, obj.level, obj.period_id, data["parent_id"])
        obj.parent_id = data["parent_id"] or None
    if "key_results" in data and data["key_results"] is not None:
        _sync_krs(db, actor, obj, data["key_results"])
    db.flush()
    recompute(db, obj)
    if old_parent and old_parent != obj.parent_id:
        p = db.query(Objective).filter(Objective.company_id == obj.company_id, Objective.id == old_parent).first()
        if p:
            recompute(db, p)
    return obj


def _sync_krs(db: Session, actor: Actor, obj: Objective, items: list[dict]) -> None:
    existing = {str(k.uuid): k for k in obj.key_results}
    keep = set()
    for i, d in enumerate(items):
        kr = existing.get(str(d.get("uuid") or ""))
        if kr is None:
            kr = KeyResult(company_id=obj.company_id, objective_id=obj.id)
            kr.objective = obj
            obj.key_results.append(kr)
        else:
            keep.add(str(kr.uuid))
        kr.sort_order = i
        _apply_kr(kr, {**d, "owner_employee_id": d.get("owner_employee_id") or kr.owner_employee_id or obj.owner_employee_id})
    for u, kr in existing.items():
        if u not in keep:
            obj.key_results.remove(kr)
    db.flush()
    cfg = config(db, actor.company_id)["okr"]
    for kr in obj.key_results:
        recompute_kr(db, kr, cfg.get("statuses"), bool(cfg.get("auto_status", True)))


def archive_objective(db: Session, actor: Actor, obj: Objective) -> None:
    if not can_edit(db, actor, obj):
        raise Forbidden("You can't change this objective.")
    from modules.performance.common import now
    obj.archived_at = now()
    parent_id = obj.parent_id
    # children lose their link; their parent's progress no longer counts them
    for c in db.query(Objective).filter(Objective.company_id == obj.company_id, Objective.parent_id == obj.id).all():
        c.parent_id = None
    obj.parent_id = None
    db.flush()
    if parent_id:
        p = db.query(Objective).filter(Objective.company_id == obj.company_id, Objective.id == parent_id).first()
        if p:
            recompute(db, p)
    audit.event(db, "goal.archived", "perf_objectives", obj.id, {"title": obj.title}, entity_uuid=str(obj.uuid))


def checkin(db: Session, actor: Actor, obj: Objective, data: dict) -> GoalCheckin:
    """Record progress on a key result (or a note on the objective)."""
    kr = None
    if data.get("key_result_uuid"):
        kr = next((k for k in obj.key_results if str(k.uuid) == str(data["key_result_uuid"])), None)
        if kr is None:
            raise BadRequest("That key result was not found.")
    if not _can_checkin(db, actor, obj, kr):
        raise Forbidden("Only the owner, their manager or an administrator can check in.")
    t = today(db, actor.company_id)
    when = data.get("checkin_date") or t
    if when > t:
        raise BadRequest("You can't check in for a future date.")
    conf = data.get("confidence")
    if conf is not None and not (1 <= int(conf) <= 10):
        raise BadRequest("Confidence is from 1 to 10.")
    row = GoalCheckin(company_id=actor.company_id, objective_id=obj.id, key_result_id=kr.id if kr else None,
                      employee_id=actor.employee_id, author_name=actor.name, checkin_date=when,
                      confidence=conf, note=(data.get("note") or None))
    cfg = config(db, actor.company_id)["okr"]
    if kr is not None:
        if kr.kind in ("number", "percent"):
            if data.get("value") is None:
                raise BadRequest("Enter the current value.")
            kr.current_value = Decimal(str(data["value"]))
        elif kr.kind == "boolean":
            kr.current_value = Decimal(1 if data.get("value") else 0)
        else:
            ms = [dict(m) for m in (kr.milestones or [])]
            done_ids = set(data.get("milestones_done") or [])
            for m in ms:
                was = m.get("done")
                m["done"] = m["id"] in done_ids
                if m["done"] and not was:
                    m["done_on"] = when.isoformat()
                if not m["done"]:
                    m["done_on"] = None
            kr.milestones = ms
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(kr, "milestones")
        recompute_kr(db, kr, cfg.get("statuses"), bool(cfg.get("auto_status", True)))
        if data.get("status_key"):
            kr.status_key = data["status_key"]
        if conf is not None:
            kr.confidence = conf
        row.value = kr.current_value
        row.progress = kr.progress
        row.status_key = kr.status_key
    if conf is not None:
        obj.confidence = conf
    db.add(row)
    db.flush()
    recompute(db, obj)
    if kr is None:
        row.progress = obj.progress
    if obj.owner_employee_id and obj.owner_employee_id != actor.employee_id:
        notify(db, actor.company_id, obj.owner_employee_id, f"{actor.name} checked in on “{obj.title}”.")
    return row


def summary(db: Session, actor: Actor, period_id: int | None) -> dict:
    """Numbers for the goals page header: my objectives, team progress, due check-ins."""
    if period_id is None:
        p = current_period(db, actor.company_id)
        period_id = p.id if p else None
    mine = list_objectives(db, actor, period_id=period_id, scope="mine") if period_id else []
    company = list_objectives(db, actor, period_id=period_id, scope="company") if period_id else []
    due = []
    for o in mine:
        for k in o["key_results"]:
            if k["owner"] and k["owner"]["id"] == actor.employee_id and (k["missed_checkins"] or k["next_checkin"]):
                due.append({"objective": o["title"], "objective_uuid": o["uuid"], "key_result": k["title"],
                            "next_checkin": k["next_checkin"], "missed": k["missed_checkins"]})
    avg = calc.weighted_progress([(o["progress"], o["weight"]) for o in mine]) if mine else 0.0
    comp = calc.weighted_progress([(o["progress"], 1) for o in company if o["level"] == "company"]) if company else 0.0
    return {"period_id": period_id, "my_count": len(mine), "my_progress": avg, "company_progress": comp,
            "company_count": sum(1 for o in company if o["level"] == "company"),
            "checkins_due": due[:20], "missed_total": sum(d["missed"] for d in due)}
