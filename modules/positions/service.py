"""Positions: approved seats, who sits in them, headcount plans and position requests.

Rules
- Positions are switched on per company (settings.enabled). While off, the pages still
  work for set-up, but hires and job changes do not ask for a position.
- A position has `headcount` seats. Seats taken = active + reserved (starting later)
  seats. Without allow_overfill a position can't take more people than it has seats, and
  its headcount can't be cut below the seats taken.
- Only approved positions can be filled or hired for; budgeted ones are planned only.
- A position can't report to itself or to one of its own reports (no loops), and can't
  be closed while people sit in it or open positions report to it.
- Headcount plans: one per department per financial year (April to March). Once
  approved, the seats of the department's open positions (budgeted + approved) can't go
  above the approved headcount when enforce_headcount_plan is on. A revision keeps the
  old limit until it is approved.
- Position requests (new, change, close) and headcount plans go through the workflow
  engine (positions.request, positions.headcount_plan).
- Putting someone in a position records a job change on their record (department,
  designation, location, grade, cost centre and the manager, i.e. the person in the
  reports-to position) when sync_job_from_position is on.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core import audit
from core.errors import BadRequest, Forbidden, NotFound
from modules.positions import common as c
from modules.positions.models import HeadcountPlan, Position, PositionRequest, PositionSeat

STATUSES = {"budgeted": "Budgeted", "approved": "Approved", "closed": "Closed"}
OPEN_STATUSES = ("budgeted", "approved")
TAKEN = ("active", "reserved")
REQUEST_KINDS = {"new": "New position", "change": "Change a position", "close": "Close a position"}
PLAN_STATUSES = {"draft": "Draft", "pending": "Waiting for approval", "approved": "Approved", "rejected": "Rejected"}
FIELDS = ("title", "description", "designation_id", "department_id", "location_id", "grade_id", "cost_centre_id",
          "parent_id", "headcount", "fte", "salary_min", "salary_max", "is_critical", "skills", "status")


def enabled(db: Session, company_id: int) -> bool:
    return bool(c.get_settings(db, company_id)["enabled"])


# ── Lookups ─────────────────────────────────────────────────────────

def get(db: Session, company_id: int, uuid) -> Position:
    row = db.query(Position).filter(Position.company_id == company_id, Position.uuid == uuid).first()
    if row is None:
        raise NotFound("Position")
    return row


def by_id(db: Session, company_id: int, pid) -> Position | None:
    if not pid:
        return None
    return db.query(Position).filter(Position.company_id == company_id, Position.id == pid).first()


def taken_counts(db: Session, company_id: int, position_ids=None) -> dict[int, dict]:
    q = (db.query(PositionSeat.position_id, PositionSeat.status, func.count(PositionSeat.id))
         .filter(PositionSeat.company_id == company_id, PositionSeat.status.in_(TAKEN)))
    if position_ids is not None:
        ids = [i for i in position_ids if i]
        if not ids:
            return {}
        q = q.filter(PositionSeat.position_id.in_(ids))
    out: dict[int, dict] = {}
    for pid, status, n in q.group_by(PositionSeat.position_id, PositionSeat.status):
        out.setdefault(pid, {"active": 0, "reserved": 0})[status] = n
    return out


def seats_of(db: Session, company_id: int, position_ids, statuses=TAKEN) -> dict[int, list[PositionSeat]]:
    ids = [i for i in position_ids if i]
    if not ids:
        return {}
    rows = (db.query(PositionSeat).filter(PositionSeat.company_id == company_id, PositionSeat.position_id.in_(ids),
                                          PositionSeat.status.in_(statuses))
            .order_by(PositionSeat.start_date, PositionSeat.id).all())
    out: dict[int, list] = {}
    for s in rows:
        out.setdefault(s.position_id, []).append(s)
    return out


def current_seat(db: Session, company_id: int, employee_id: int) -> PositionSeat | None:
    return (db.query(PositionSeat).filter(PositionSeat.company_id == company_id, PositionSeat.employee_id == employee_id,
                                          PositionSeat.status == "active")
            .order_by(PositionSeat.start_date.desc()).first())


def incumbent_id(db: Session, company_id: int, position_id) -> int | None:
    """The person in a position (the earliest active seat), e.g. the manager for its reports."""
    if not position_id:
        return None
    return (db.query(PositionSeat.employee_id)
            .filter(PositionSeat.company_id == company_id, PositionSeat.position_id == position_id,
                    PositionSeat.status == "active")
            .order_by(PositionSeat.start_date, PositionSeat.id).limit(1).scalar())


def fill_status(headcount: int, taken: int) -> str:
    if taken <= 0:
        return "vacant"
    return "filled" if taken >= headcount else "partly_filled"


FILL_LABELS = {"vacant": "Vacant", "partly_filled": "Partly filled", "filled": "Filled", "closed": "Closed"}


# ── Output ──────────────────────────────────────────────────────────

def outputs(db: Session, company_id: int, rows: list[Position], *, detail: bool = False) -> list[dict]:
    labels = c.org_labels(db, company_id, departments=[r.department_id for r in rows],
                          designations=[r.designation_id for r in rows], locations=[r.location_id for r in rows],
                          grades=[r.grade_id for r in rows], cost_centres=[r.cost_centre_id for r in rows])
    seats = seats_of(db, company_id, [r.id for r in rows])
    parents = {p.id: p for p in db.query(Position).filter(Position.company_id == company_id,
                                                         Position.id.in_({r.parent_id for r in rows if r.parent_id} or {0}))}
    names = c.people(db, company_id, [s.employee_id for lst in seats.values() for s in lst])
    child_counts = dict(db.query(Position.parent_id, func.count(Position.id))
                        .filter(Position.company_id == company_id, Position.parent_id.in_([r.id for r in rows] or [0]),
                                Position.status != "closed").group_by(Position.parent_id).all())
    skill_ids = {s.get("skill_id") for r in rows for s in (r.skills or [])}
    skills = c.skill_names(db, company_id, skill_ids)
    out = []
    for r in rows:
        lst = seats.get(r.id, [])
        active = [s for s in lst if s.status == "active"]
        reserved = [s for s in lst if s.status == "reserved"]
        taken = len(active) + len(reserved)
        vacant = max(0, r.headcount - taken) if r.status != "closed" else 0
        p = parents.get(r.parent_id)
        d = {
            "id": r.id, "uuid": str(r.uuid), "code": r.code, "title": r.title, "description": r.description,
            "status": r.status, "status_label": STATUSES.get(r.status, r.status),
            "designation_id": r.designation_id, "designation": labels["designation"].get(r.designation_id),
            "department_id": r.department_id, "department": labels["department"].get(r.department_id),
            "location_id": r.location_id, "location": labels["location"].get(r.location_id),
            "grade_id": r.grade_id, "grade": labels["grade"].get(r.grade_id),
            "cost_centre_id": r.cost_centre_id, "cost_centre": labels["cost_centre"].get(r.cost_centre_id),
            "parent_id": r.parent_id, "parent_uuid": str(p.uuid) if p else None,
            "parent": f"{p.code} · {p.title}" if p else None,
            "headcount": r.headcount, "fte": float(r.fte or 0), "total_fte": float((r.fte or 0) * r.headcount),
            "salary_min": str(r.salary_min) if r.salary_min is not None else None,
            "salary_max": str(r.salary_max) if r.salary_max is not None else None,
            "is_critical": r.is_critical,
            "skills": [{**s, "name": skills.get(s.get("skill_id"))} for s in (r.skills or [])],
            "filled": len(active), "reserved": len(reserved), "vacant": vacant,
            "fill_status": "closed" if r.status == "closed" else fill_status(r.headcount, taken),
            "incumbents": [{**c.person_ref(names.get(s.employee_id)), "seat_uuid": str(s.uuid), "status": s.status,
                            "start_date": s.start_date.isoformat()} for s in lst if names.get(s.employee_id)],
            "children": child_counts.get(r.id, 0),
            "requisition_ids": list(r.requisition_ids or []),
            "closed_reason": r.closed_reason,
            "created_at": r.created_at, "updated_at": r.updated_at,
        }
        d["fill_label"] = FILL_LABELS[d["fill_status"]]
        out.append(d)
    if detail and rows:
        for d, r in zip(out, rows):
            d["history"] = seat_history(db, company_id, r.id)
            d["hiring_requests"] = hiring_requests(db, company_id, r)
    return out


def output(db: Session, company_id: int, row: Position, detail: bool = True) -> dict:
    return outputs(db, company_id, [row], detail=detail)[0]


def seat_history(db: Session, company_id: int, position_id: int) -> list[dict]:
    rows = (db.query(PositionSeat).filter(PositionSeat.company_id == company_id, PositionSeat.position_id == position_id)
            .order_by(PositionSeat.start_date.desc(), PositionSeat.id.desc()).limit(50).all())
    names = c.people(db, company_id, [s.employee_id for s in rows])
    return [{"uuid": str(s.uuid), "employee": c.person_ref(names.get(s.employee_id)), "status": s.status,
             "start_date": s.start_date.isoformat(), "end_date": s.end_date.isoformat() if s.end_date else None,
             "source": s.source, "ended_reason": s.ended_reason} for s in rows]


def hiring_requests(db: Session, company_id: int, row: Position) -> list[dict]:
    ids = [int(i) for i in (row.requisition_ids or []) if i]
    if not ids:
        return []
    from modules.recruitment.models import Requisition
    return [{"uuid": str(r.uuid), "number": r.number, "title": r.title, "openings": r.openings, "status": r.status}
            for r in db.query(Requisition).filter(Requisition.company_id == company_id, Requisition.id.in_(ids))
            .order_by(Requisition.id.desc())]


def open_requisition_openings(db: Session, company_id: int, row: Position) -> int:
    ids = [int(i) for i in (row.requisition_ids or []) if i]
    if not ids:
        return 0
    from modules.recruitment.models import Requisition
    return int(db.query(func.coalesce(func.sum(Requisition.openings), 0))
               .filter(Requisition.company_id == company_id, Requisition.id.in_(ids),
                       Requisition.status.in_(("draft", "pending", "approved"))).scalar() or 0)


# ── List and tree ───────────────────────────────────────────────────

def list_query(db: Session, company_id: int, *, q: str | None = None, status: str | None = None,
               department_id: int | None = None, fill: str | None = None, critical: bool | None = None):
    query = db.query(Position).filter(Position.company_id == company_id)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Position.title.ilike(like), Position.code.ilike(like)))
    if status:
        query = query.filter(Position.status.in_(status.split(",")))
    else:
        query = query.filter(Position.status != "closed")
    if department_id:
        query = query.filter(Position.department_id == department_id)
    if critical is not None:
        query = query.filter(Position.is_critical.is_(critical))
    if fill in ("vacant", "has_vacancy", "filled"):
        taken = (db.query(func.count(PositionSeat.id)).filter(PositionSeat.company_id == company_id,
                                                              PositionSeat.position_id == Position.id,
                                                              PositionSeat.status.in_(TAKEN))
                 .correlate(Position).scalar_subquery())
        if fill == "vacant":
            query = query.filter(taken == 0)
        elif fill == "has_vacancy":
            query = query.filter(taken < Position.headcount)
        else:
            query = query.filter(taken >= Position.headcount)
    return query.order_by(Position.code)


def summary(db: Session, company_id: int) -> dict:
    rows = db.query(Position.id, Position.headcount, Position.status, Position.is_critical).filter(
        Position.company_id == company_id, Position.status != "closed").all()
    taken = taken_counts(db, company_id)
    seats = sum(r.headcount for r in rows if r.status == "approved")
    filled = sum(taken.get(r.id, {}).get("active", 0) for r in rows)
    reserved = sum(taken.get(r.id, {}).get("reserved", 0) for r in rows)
    vacant = sum(max(0, r.headcount - sum(taken.get(r.id, {}).values())) for r in rows if r.status == "approved")
    pending = db.query(func.count(PositionRequest.id)).filter(PositionRequest.company_id == company_id,
                                                             PositionRequest.status == "pending").scalar()
    return {"positions": len(rows), "approved_seats": seats, "budgeted": sum(1 for r in rows if r.status == "budgeted"),
            "filled": filled, "reserved": reserved, "vacant": vacant,
            "critical": sum(1 for r in rows if r.is_critical), "pending_requests": pending,
            "enabled": enabled(db, company_id)}


def tree(db: Session, company_id: int) -> list[dict]:
    """Flat list of open positions with parent links and roll-up counts below each one."""
    rows = db.query(Position).filter(Position.company_id == company_id, Position.status != "closed").order_by(Position.code).all()
    items = outputs(db, company_id, rows)
    by_id = {d["id"]: d for d in items}
    kids: dict[int | None, list] = {}
    for d in items:
        parent = d["parent_id"] if d["parent_id"] in by_id else None
        kids.setdefault(parent, []).append(d)

    def roll(d, seen):
        below = filled = vacant = 0
        for k in kids.get(d["id"], []):
            if k["id"] in seen:
                continue
            seen.add(k["id"])
            b, f, v = roll(k, seen)
            below += 1 + b
            filled += k["filled"] + f
            vacant += k["vacant"] + v
        d["below"], d["below_filled"], d["below_vacant"] = below, filled, vacant
        return below, filled, vacant

    for root in kids.get(None, []):
        roll(root, {root["id"]})
    for d in items:
        d["root"] = d["parent_id"] not in by_id
        d.pop("history", None)
    return items


# ── Validation helpers ──────────────────────────────────────────────

def _check_refs(db: Session, company_id: int, data: dict) -> None:
    from modules.departments.models import Department
    from modules.designations.models import Designation
    from modules.organization.models import CostCentre, Grade, Location
    checks = (("department_id", Department, "department"), ("designation_id", Designation, "designation"),
              ("location_id", Location, "location"), ("grade_id", Grade, "grade"),
              ("cost_centre_id", CostCentre, "cost centre"))
    for key, model, label in checks:
        v = data.get(key)
        if v:
            q = db.query(model.id).filter(model.id == v)
            if hasattr(model, "company_id"):
                q = q.filter(or_(model.company_id == company_id, model.company_id.is_(None)))
            if q.first() is None:
                raise BadRequest(f"That {label} doesn't exist.")


def _check_parent(db: Session, company_id: int, row: Position | None, parent_id) -> None:
    if not parent_id:
        return
    parent = by_id(db, company_id, parent_id)
    if parent is None:
        raise BadRequest("The reports-to position doesn't exist.")
    if parent.status == "closed":
        raise BadRequest("The reports-to position is closed.")
    if row is None:
        return
    seen, cur = set(), parent
    while cur is not None:
        if cur.id == row.id:
            raise BadRequest("A position can't report to itself or to one of the positions below it.")
        if cur.id in seen:
            break
        seen.add(cur.id)
        cur = by_id(db, company_id, cur.parent_id)


def department_seats(db: Session, company_id: int, department_id: int, exclude_id: int | None = None) -> int:
    q = db.query(func.coalesce(func.sum(Position.headcount), 0)).filter(
        Position.company_id == company_id, Position.department_id == department_id, Position.status.in_(OPEN_STATUSES))
    if exclude_id:
        q = q.filter(Position.id != exclude_id)
    return int(q.scalar() or 0)


def plan_for(db: Session, company_id: int, department_id: int, on: date) -> HeadcountPlan | None:
    return db.query(HeadcountPlan).filter(HeadcountPlan.company_id == company_id,
                                          HeadcountPlan.department_id == department_id,
                                          HeadcountPlan.financial_year == c.fy_of(on)).first()


def check_headcount_plan(db: Session, company_id: int, department_id, new_total_for_position: int,
                         exclude_id: int | None = None) -> None:
    """Refuse when the department's open seats would go above its approved plan this year."""
    if not department_id or not c.get_settings(db, company_id)["enforce_headcount_plan"]:
        return
    plan = plan_for(db, company_id, department_id, c.today(db, company_id))
    if plan is None or plan.approved_headcount is None:
        return
    total = department_seats(db, company_id, department_id, exclude_id) + new_total_for_position
    if total > plan.approved_headcount:
        raise BadRequest(f"This would take the department to {total} seats, above its approved headcount of "
                         f"{plan.approved_headcount} for {plan.financial_year}. Revise the headcount plan first.")


def _next_code(db: Session, company_id: int) -> str:
    from core import numbering
    code = numbering.next_number(db, company_id, "position", c.today(db, company_id))
    if code:
        return code
    n = db.query(func.count(Position.id)).filter(Position.company_id == company_id).scalar() or 0
    while True:
        n += 1
        code = f"POS-{n:04d}"
        if not db.query(Position.id).filter(Position.company_id == company_id, Position.code == code).first():
            return code


def validate(db: Session, company_id: int, data: dict, row: Position | None = None) -> dict:
    """Check and normalise position fields (all or some of them). Returns the clean values."""
    out: dict = {}
    if "title" in data or row is None:
        title = (data.get("title") or "").strip()
        if not title:
            raise BadRequest("Give the position a title.")
        out["title"] = title[:200]
    if "description" in data:
        out["description"] = (data.get("description") or "").strip() or None
    for key in ("designation_id", "department_id", "location_id", "grade_id", "cost_centre_id", "parent_id"):
        if key in data:
            out[key] = int(data[key]) if data.get(key) else None
    _check_refs(db, company_id, out)
    if "parent_id" in out:
        _check_parent(db, company_id, row, out["parent_id"])
    if "headcount" in data or row is None:
        try:
            hc = int(data.get("headcount") if data.get("headcount") is not None else 1)
        except (TypeError, ValueError):
            raise BadRequest("Seats should be a whole number.")
        if not 1 <= hc <= 1000:
            raise BadRequest("Seats should be between 1 and 1000.")
        out["headcount"] = hc
    if "fte" in data and data.get("fte") is not None:
        try:
            fte = Decimal(str(data["fte"])).quantize(Decimal("0.01"))
        except Exception:
            raise BadRequest("FTE should be a number like 1 or 0.5.")
        if not Decimal("0.1") <= fte <= Decimal("1"):
            raise BadRequest("FTE per seat should be between 0.1 and 1.")
        out["fte"] = fte
    if "status" in data or row is None:
        st = data.get("status") or "approved"
        if st not in OPEN_STATUSES:
            raise BadRequest("A position is budgeted or approved (close it to retire it).")
        out["status"] = st
    if "is_critical" in data:
        out["is_critical"] = bool(data.get("is_critical"))
    if "skills" in data:
        out["skills"] = c.clean_skills(db, company_id, data.get("skills") or [])
    for key, label in (("salary_min", "Minimum salary"), ("salary_max", "Maximum salary")):
        if key in data:
            out[key] = c.money(data.get(key), label)
    grade_id = out.get("grade_id", row.grade_id if row else None)
    if grade_id and row is None and out.get("salary_min") is None and out.get("salary_max") is None:
        from modules.organization.models import Grade
        g = db.query(Grade).filter(Grade.company_id == company_id, Grade.id == grade_id).first()
        if g is not None:
            out["salary_min"], out["salary_max"] = g.min_ctc, g.max_ctc
    smin = out.get("salary_min", row.salary_min if row else None)
    smax = out.get("salary_max", row.salary_max if row else None)
    if smin is not None and smax is not None and smin > smax:
        raise BadRequest("The minimum salary can't be more than the maximum.")
    return out


def save(db: Session, company_id: int, data: dict, row: Position | None = None) -> Position:
    """Create or change a position directly (HR), checking seats and the headcount plan."""
    clean = validate(db, company_id, data, row)
    if row is not None and row.status == "closed":
        raise BadRequest("This position is closed. Reopen it first.")
    code = (data.get("code") or "").strip().upper() if "code" in data else None
    if row is None:
        code = code or _next_code(db, company_id)
    if code is not None and (row is None or code != row.code):
        if len(code) > 40:
            raise BadRequest("The code is too long (40 characters at most).")
        clash = db.query(Position.id).filter(Position.company_id == company_id, Position.code == code)
        if row is not None:
            clash = clash.filter(Position.id != row.id)
        if clash.first():
            raise BadRequest(f"Another position already has the code {code}.")
    headcount = clean.get("headcount", row.headcount if row else 1)
    if row is not None and "headcount" in clean:
        taken = sum(taken_counts(db, company_id, [row.id]).get(row.id, {}).values())
        if headcount < taken and not c.get_settings(db, company_id)["allow_overfill"]:
            raise BadRequest(f"{taken} people sit in this position, so it needs at least {taken} seats.")
    dept = clean.get("department_id", row.department_id if row else None)
    status = clean.get("status", row.status if row else "approved")
    if status in OPEN_STATUSES:
        grows = row is None or dept != row.department_id or headcount > row.headcount or row.status not in OPEN_STATUSES
        if grows:
            check_headcount_plan(db, company_id, dept, headcount, exclude_id=row.id if row else None)
    if row is None:
        row = Position(company_id=company_id, code=code, created_by_name=c.actor_name(), title=clean["title"])
        db.add(row)
    elif code is not None:
        row.code = code
    for k, v in clean.items():
        setattr(row, k, v)
    if "skills" in clean:
        flag_modified(row, "skills")
    db.flush()
    return row


def close(db: Session, company_id: int, row: Position, reason: str | None) -> Position:
    if row.status == "closed":
        raise BadRequest("This position is already closed.")
    if taken_counts(db, company_id, [row.id]).get(row.id):
        raise BadRequest("People still sit in this position. Move them out first.")
    kids = db.query(Position.id).filter(Position.company_id == company_id, Position.parent_id == row.id,
                                        Position.status != "closed").count()
    if kids:
        raise BadRequest(f"{kids} open position(s) report to this one. Move or close them first.")
    row.status = "closed"
    row.closed_at = c.now()
    row.closed_reason = (reason or "").strip()[:300] or None
    db.flush()
    audit.event(db, "position.closed", "positions", row.id, {"code": row.code, "reason": row.closed_reason},
                entity_uuid=str(row.uuid), entity_label=row.title)
    return row


def reopen(db: Session, company_id: int, row: Position) -> Position:
    if row.status != "closed":
        raise BadRequest("This position is open.")
    check_headcount_plan(db, company_id, row.department_id, row.headcount, exclude_id=row.id)
    row.status = "approved"
    row.closed_at = None
    row.closed_reason = None
    db.flush()
    return row


# ── Seats ───────────────────────────────────────────────────────────

def vacant_count(db: Session, company_id: int, row: Position) -> int:
    taken = sum(taken_counts(db, company_id, [row.id]).get(row.id, {}).values())
    return max(0, row.headcount - taken)


def job_values(db: Session, company_id: int, row: Position, employee_id: int) -> dict:
    """The job fields a position sets on the person's record."""
    vals = {k: getattr(row, k) for k in ("department_id", "designation_id", "location_id", "grade_id", "cost_centre_id")
            if getattr(row, k)}
    mgr = incumbent_id(db, company_id, row.parent_id)
    if mgr and mgr != employee_id:
        vals["reporting_manager_id"] = mgr
    return vals


def assign(db: Session, company_id: int, row: Position, employee_id: int, effective: date, *,
           reason: str | None = None, change_type: str = "transfer", source: str = "manual",
           sync_job: bool | None = None) -> PositionSeat:
    """Put someone in a position from a date: ends their current seat the day before,
    records the job change on their record and refuses when no seat is free."""
    settings = c.get_settings(db, company_id)
    if row.status != "approved":
        raise BadRequest("Only approved positions can be filled.")
    emp = c.require_employee(db, company_id, employee_id)
    if emp.status in ("exited", "archived") or not emp.active:
        raise BadRequest("This person has left.")
    if emp.date_of_joining and effective < emp.date_of_joining:
        raise BadRequest("The seat can't start before the joining date.")
    mine = (db.query(PositionSeat).filter(PositionSeat.company_id == company_id, PositionSeat.employee_id == emp.id,
                                          PositionSeat.status.in_(TAKEN)).all())
    if any(s.position_id == row.id for s in mine):
        raise BadRequest(f"{c.display_name(emp)} already sits in this position.")
    if vacant_count(db, company_id, row) <= 0 and not settings["allow_overfill"]:
        raise BadRequest("No seats are free in this position. Raise its headcount or pick another position.")
    t = c.today(db, company_id)
    for s in mine:
        if s.status == "reserved":
            s.status, s.end_date, s.ended_reason = "ended", s.start_date, "Moved to another position before starting"
        elif s.start_date < effective:
            s.end_date = effective - timedelta(days=1)
            if effective <= t:
                s.status, s.ended_reason = "ended", "Moved to another position"
        else:
            s.status, s.end_date, s.ended_reason = "ended", effective, "Moved to another position"
    seat = PositionSeat(company_id=company_id, position_id=row.id, employee_id=emp.id, start_date=effective,
                        status="active" if effective <= t else "reserved", source=source,
                        created_by_name=c.actor_name())
    db.add(seat)
    db.flush()
    if settings["sync_job_from_position"] if sync_job is None else sync_job:
        vals = job_values(db, company_id, row, emp.id)
        diff = {k: v for k, v in vals.items() if getattr(emp, k) != v}
        if diff:
            from modules.employees import service as emp_service
            jc = emp_service.record_job_change(db, company_id, emp, change_type, max(effective, emp.date_of_joining or effective),
                                               diff, reason or f"Moved to position {row.code} · {row.title}")
            seat.job_change_id = jc.id
    audit.event(db, "position.seat_assigned", "positions", row.id,
                {"employee_id": emp.id, "from": effective.isoformat(), "source": source},
                entity_uuid=str(row.uuid), entity_label=row.title)
    c.notify(db, company_id, emp.id, f"You are now in the position {row.title} ({row.code}) from {effective.strftime('%d %b %Y')}.")
    db.flush()
    return seat


def get_seat(db: Session, company_id: int, uuid) -> PositionSeat:
    s = db.query(PositionSeat).filter(PositionSeat.company_id == company_id, PositionSeat.uuid == uuid).first()
    if s is None:
        raise NotFound("Seat")
    return s


def release(db: Session, company_id: int, seat: PositionSeat, end: date, reason: str | None) -> PositionSeat:
    if seat.status == "ended":
        raise BadRequest("This seat has already ended.")
    if seat.status == "reserved" or end < seat.start_date:
        seat.status, seat.end_date = "ended", seat.start_date
    else:
        seat.end_date = end
        if end <= c.today(db, company_id):
            seat.status = "ended"
    seat.ended_reason = (reason or "").strip()[:300] or "Seat released"
    db.flush()
    return seat


def refresh_seats(db: Session, company_id: int | None = None) -> int:
    """Start reserved seats whose date has come, end seats past their end date and free the
    seats of people who have left."""
    from modules.employees.models import Employee
    q = db.query(PositionSeat).filter(PositionSeat.status.in_(TAKEN))
    if company_id:
        q = q.filter(PositionSeat.company_id == company_id)
    rows = q.all()
    if not rows:
        return 0
    emps = {e.id: e for e in db.query(Employee).filter(Employee.id.in_({s.employee_id for s in rows}))}
    todays: dict[int, date] = {}
    n = 0
    for s in rows:
        t = todays.get(s.company_id) or todays.setdefault(s.company_id, c.today(db, s.company_id))
        e = emps.get(s.employee_id)
        if e is not None and (e.status in ("exited", "archived") or not e.active):
            s.status, s.end_date = "ended", s.end_date or t
            s.ended_reason = s.ended_reason or "Left the company"
            n += 1
        elif s.end_date and s.end_date < t:
            s.status = "ended"
            n += 1
        elif s.status == "reserved" and s.start_date <= t:
            s.status = "active"
            n += 1
    db.flush()
    return n


def vacant_options(db: Session, company_id: int, q: str | None = None, include_uuid: str | None = None) -> list[dict]:
    rows = list_query(db, company_id, q=q, status="approved", fill="has_vacancy").limit(30).all()
    if include_uuid and not any(str(r.uuid) == include_uuid for r in rows):
        extra = db.query(Position).filter(Position.company_id == company_id, Position.uuid == include_uuid).first()
        if extra:
            rows.append(extra)
    items = outputs(db, company_id, rows)
    for d, r in zip(items, rows):
        d["manager_id"] = incumbent_id(db, company_id, r.parent_id)
    return items


# ── Hiring requests ─────────────────────────────────────────────────

def raise_hiring_request(db: Session, user: dict, row: Position, data: dict) -> dict:
    """Open a hiring request in recruitment for a vacant position (fields copied over)."""
    from modules.recruitment import service as rec
    company_id = user["company_id"]
    if row.status != "approved":
        raise BadRequest("Only approved positions can be hired for.")
    free = vacant_count(db, company_id, row) - open_requisition_openings(db, company_id, row)
    openings = int(data.get("openings") or 1)
    if free <= 0:
        raise BadRequest("Every vacant seat already has an open hiring request.")
    if openings > free:
        raise BadRequest(f"Only {free} seat(s) are free for hiring in this position.")
    v = rec.Viewer(db, user)
    justification = (data.get("justification") or "").strip() or f"Vacant seat in position {row.code} · {row.title}."
    req = rec.save_requisition(db, v, {
        "title": row.title, "openings": openings, "kind": data.get("kind") or "new",
        "replacement_for_id": data.get("replacement_for_id"), "department_id": row.department_id,
        "designation_id": row.designation_id, "location_id": row.location_id,
        "budget_min": row.salary_min, "budget_max": row.salary_max,
        "hiring_manager_id": data.get("hiring_manager_id") or incumbent_id(db, company_id, row.parent_id),
        "needed_by": data.get("needed_by"), "justification": justification,
    })
    row.requisition_ids = [*(row.requisition_ids or []), req.id]
    flag_modified(row, "requisition_ids")
    if data.get("submit", True):
        rec.submit_requisition(db, v, req)
    db.flush()
    return {"uuid": str(req.uuid), "number": req.number, "status": req.status, "title": req.title}


# ── Headcount plans ─────────────────────────────────────────────────

def plan_outputs(db: Session, company_id: int, rows: list[HeadcountPlan]) -> list[dict]:
    labels = c.org_labels(db, company_id, departments=[r.department_id for r in rows])
    out = []
    t = c.today(db, company_id)
    for r in rows:
        seats = department_seats(db, company_id, r.department_id)
        filled = (db.query(func.count(PositionSeat.id)).join(Position, Position.id == PositionSeat.position_id)
                  .filter(PositionSeat.company_id == company_id, Position.department_id == r.department_id,
                          PositionSeat.status == "active").scalar() or 0)
        from modules.employees.models import Employee
        people = (db.query(func.count(Employee.id)).filter(Employee.company_id == company_id, Employee.department_id == r.department_id,
                                                           Employee.active.is_(True), Employee.status.notin_(("exited", "archived")))
                  .scalar() or 0)
        out.append({"uuid": str(r.uuid), "department_id": r.department_id, "department": labels["department"].get(r.department_id),
                    "financial_year": r.financial_year, "current_year": r.financial_year == c.fy_of(t),
                    "planned_headcount": r.planned_headcount, "approved_headcount": r.approved_headcount,
                    "budget": str(r.budget) if r.budget is not None else None, "notes": r.notes,
                    "status": r.status, "status_label": PLAN_STATUSES.get(r.status, r.status),
                    "position_seats": seats, "filled": filled, "employees": people,
                    "decision_note": r.decision_note, "created_by_name": r.created_by_name,
                    "updated_at": r.updated_at})
    return out


def get_plan(db: Session, company_id: int, uuid) -> HeadcountPlan:
    r = db.query(HeadcountPlan).filter(HeadcountPlan.company_id == company_id, HeadcountPlan.uuid == uuid).first()
    if r is None:
        raise NotFound("Headcount plan")
    return r


def save_plan(db: Session, company_id: int, data: dict, row: HeadcountPlan | None = None) -> HeadcountPlan:
    if row is not None and row.status == "pending":
        raise BadRequest("This plan is waiting for approval. Withdraw it before changing it.")
    dept = int(data.get("department_id") or (row.department_id if row else 0) or 0)
    if not dept:
        raise BadRequest("Pick the department.")
    _check_refs(db, company_id, {"department_id": dept})
    fy = (data.get("financial_year") or (row.financial_year if row else "") or c.fy_of(c.today(db, company_id))).strip()
    c.fy_bounds(fy)
    try:
        planned = int(data.get("planned_headcount") if data.get("planned_headcount") is not None else (row.planned_headcount if row else -1))
    except (TypeError, ValueError):
        raise BadRequest("Planned headcount should be a whole number.")
    if not 0 <= planned <= 100000:
        raise BadRequest("Give the planned headcount (0 or more).")
    clash = db.query(HeadcountPlan.id).filter(HeadcountPlan.company_id == company_id, HeadcountPlan.department_id == dept,
                                              HeadcountPlan.financial_year == fy)
    if row is not None:
        clash = clash.filter(HeadcountPlan.id != row.id)
    if clash.first():
        raise BadRequest(f"This department already has a plan for {fy}. Open it to revise it.")
    if row is None:
        row = HeadcountPlan(company_id=company_id, department_id=dept, financial_year=fy, planned_headcount=planned,
                            status="draft", created_by_name=c.actor_name())
        db.add(row)
    row.department_id, row.financial_year, row.planned_headcount = dept, fy, planned
    if "budget" in data:
        row.budget = c.money(data.get("budget"), "Budget")
    if "notes" in data:
        row.notes = (data.get("notes") or "").strip() or None
    if row.status in ("approved", "rejected"):
        row.status = "draft"  # a revision; the approved number stays in force until the new one is approved
    db.flush()
    return row


def submit_plan(db: Session, company_id: int, row: HeadcountPlan) -> HeadcountPlan:
    if row.status not in ("draft",):
        raise BadRequest("Only drafts can be sent for approval.")
    from core.workflow import service as wf
    from modules.departments.models import Department
    dept = db.query(Department).filter(Department.id == row.department_id).first()
    payload = {"plan_id": row.id, "department_id": row.department_id,
               "department": dept.department_name if dept else None,
               "department_head_id": dept.head_employee_id if dept else None,
               "financial_year": row.financial_year, "planned_headcount": row.planned_headcount,
               "previous_headcount": row.approved_headcount,
               "change": row.planned_headcount - (row.approved_headcount or 0),
               "budget": str(row.budget) if row.budget is not None else None, "notes": row.notes}
    row.status = "pending"
    inst = wf.submit(db, company_id, "positions.headcount_plan", payload,
                     f"Headcount plan {row.financial_year}: {payload['department'] or 'department'} ({row.planned_headcount})",
                     subject_type="headcount_plan", subject_id=row.id)
    row.workflow_instance_id = inst.id
    db.flush()
    return row


def withdraw_plan(db: Session, company_id: int, row: HeadcountPlan) -> HeadcountPlan:
    if row.status != "pending":
        raise BadRequest("Only plans waiting for approval can be withdrawn.")
    from core.workflow import service as wf
    from core.workflow.models import WorkflowInstance
    inst = db.query(WorkflowInstance).filter(WorkflowInstance.company_id == company_id,
                                             WorkflowInstance.id == row.workflow_instance_id).first()
    if inst is not None and inst.status == "pending":
        wf.cancel(db, company_id, inst, "Withdrawn")
    row.status = "draft"
    db.flush()
    return row


def on_plan_decided(db: Session, instance, status: str) -> None:
    row = db.query(HeadcountPlan).filter(HeadcountPlan.company_id == instance.company_id,
                                         HeadcountPlan.id == (instance.payload or {}).get("plan_id")).first()
    if row is None or row.status != "pending":
        return
    row.decided_at = c.now()
    row.decision_note = (instance.result_note or "")[:500] or None
    if status == "approved":
        row.status = "approved"
        row.approved_headcount = row.planned_headcount
    elif status == "rejected":
        row.status = "rejected"
    else:
        row.status = "draft"
    db.flush()


# ── Position requests ───────────────────────────────────────────────

def get_request(db: Session, company_id: int, uuid) -> PositionRequest:
    r = db.query(PositionRequest).filter(PositionRequest.company_id == company_id, PositionRequest.uuid == uuid).first()
    if r is None:
        raise NotFound("Position request")
    return r


def _jsonable(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        out[k] = str(v) if isinstance(v, Decimal) else v
    return out


def create_request(db: Session, v: c.Viewer, data: dict) -> PositionRequest:
    """A new / change / close request from a manager or HR, sent through approval."""
    company_id = v.company_id
    kind = data.get("kind")
    if kind not in REQUEST_KINDS:
        raise BadRequest("Pick what the request is for: a new position, a change or closing one.")
    reason = (data.get("reason") or "").strip()
    if not reason:
        raise BadRequest("Say why this is needed.")
    target = None
    fields = {k: data["data"][k] for k in FIELDS if k in (data.get("data") or {})}
    if kind in ("change", "close"):
        if not data.get("position_uuid"):
            raise BadRequest("Pick the position.")
        target = get(db, company_id, data["position_uuid"])
        if target.status == "closed":
            raise BadRequest("This position is already closed.")
        pending = db.query(PositionRequest.id).filter(PositionRequest.company_id == company_id,
                                                      PositionRequest.position_id == target.id,
                                                      PositionRequest.status == "pending").first()
        if pending:
            raise BadRequest("This position already has a request waiting for approval.")
    if kind == "new":
        clean = validate(db, company_id, {**fields, "status": "approved"})
    elif kind == "change":
        if not fields:
            raise BadRequest("Choose at least one thing to change.")
        clean = validate(db, company_id, fields, target)
    else:
        clean = {}
        if taken_counts(db, company_id, [target.id]).get(target.id):
            raise BadRequest("People still sit in this position. Move them out before asking to close it.")
    me = c.employee(db, company_id, v.employee_id)
    from core import numbering
    row = PositionRequest(company_id=company_id, kind=kind, position_id=target.id if target else None,
                          data=_jsonable(clean), reason=reason, status="pending",
                          number=numbering.next_number(db, company_id, "position_request", c.today(db, company_id)),
                          requested_by_employee_id=v.employee_id, requested_by_name=c.display_name(me) or c.actor_name())
    db.add(row)
    db.flush()
    dept_id = clean.get("department_id") or (target.department_id if target else None)
    from modules.departments.models import Department
    dept = db.query(Department).filter(Department.id == dept_id).first() if dept_id else None
    title = clean.get("title") or (target.title if target else "")
    payload = {"request_id": row.id, "kind": kind, "kind_label": REQUEST_KINDS[kind], "title": title,
               "code": target.code if target else None, "department_id": dept_id,
               "department": dept.department_name if dept else None,
               "department_head_id": dept.head_employee_id if dept else None,
               "headcount": clean.get("headcount", target.headcount if target else None),
               "changes": sorted(clean.keys()), "reason": reason}
    from core.workflow import service as wf
    inst = wf.submit(db, company_id, "positions.request", payload,
                     f"{REQUEST_KINDS[kind]}: {title}" + (f" ({target.code})" if target else ""),
                     subject_type="position_request", subject_id=row.id)
    row.workflow_instance_id = inst.id
    db.flush()
    return row


def on_request_decided(db: Session, instance, status: str) -> None:
    row = db.query(PositionRequest).filter(PositionRequest.company_id == instance.company_id,
                                           PositionRequest.id == (instance.payload or {}).get("request_id")).first()
    if row is None or row.status != "pending":
        return
    row.decided_at = c.now()
    row.decision_note = (instance.result_note or "")[:500] or None
    if status != "approved":
        row.status = "rejected" if status == "rejected" else "cancelled"
        db.flush()
        return
    data = dict(row.data or {})
    if row.kind == "new":
        pos = save(db, row.company_id, data)
        row.result_position_id = pos.id
    elif row.kind == "change":
        pos = by_id(db, row.company_id, row.position_id)
        if pos is None:
            raise BadRequest("The position no longer exists.")
        save(db, row.company_id, data, pos)
        row.result_position_id = pos.id
    else:
        pos = by_id(db, row.company_id, row.position_id)
        if pos is not None and pos.status != "closed":
            close(db, row.company_id, pos, row.reason)
        row.result_position_id = row.position_id
    row.status = "approved"
    db.flush()


def cancel_request(db: Session, v: c.Viewer, row: PositionRequest) -> PositionRequest:
    if row.status != "pending":
        raise BadRequest("Only pending requests can be withdrawn.")
    if not v.manage and row.requested_by_employee_id != v.employee_id:
        raise Forbidden("You can withdraw only your own requests.")
    from core.workflow import service as wf
    from core.workflow.models import WorkflowInstance
    inst = db.query(WorkflowInstance).filter(WorkflowInstance.company_id == v.company_id,
                                             WorkflowInstance.id == row.workflow_instance_id).first()
    if inst is not None and inst.status == "pending":
        wf.cancel(db, v.company_id, inst, "Withdrawn")
    if row.status == "pending":
        row.status = "cancelled"
    db.flush()
    return row


def request_outputs(db: Session, company_id: int, rows: list[PositionRequest]) -> list[dict]:
    pos = {p.id: p for p in db.query(Position).filter(Position.company_id == company_id, Position.id.in_(
        {r.position_id for r in rows if r.position_id} | {r.result_position_id for r in rows if r.result_position_id} or {0}))}
    labels = c.org_labels(db, company_id, departments=[(r.data or {}).get("department_id") for r in rows],
                          designations=[(r.data or {}).get("designation_id") for r in rows])
    out = []
    for r in rows:
        p = pos.get(r.position_id) or pos.get(r.result_position_id)
        d = r.data or {}
        out.append({"uuid": str(r.uuid), "number": r.number, "kind": r.kind, "kind_label": REQUEST_KINDS.get(r.kind),
                    "position": {"uuid": str(p.uuid), "code": p.code, "title": p.title} if p else None,
                    "title": d.get("title") or (p.title if p else None), "data": d,
                    "department": labels["department"].get(d.get("department_id")),
                    "designation": labels["designation"].get(d.get("designation_id")),
                    "reason": r.reason, "status": r.status, "requested_by_name": r.requested_by_name,
                    "requested_by_employee_id": r.requested_by_employee_id,
                    "decision_note": r.decision_note, "created_at": r.created_at, "decided_at": r.decided_at})
    return out
