"""HR help desk rules: who sees which case, assignment, the status flow, SLA due times
and breaches, escalation, ratings and reopening.

SLA: each case gets a response target and a resolution target from the SLA for its
category and priority (or the company-wide SLA for that priority). Targets run on the
clock and stop while the case waits on the employee; when it resumes, the targets move
by the time it was waiting. A target is breached when it passes before the first reply
(response) or before the case is resolved (resolution). A resolution breach escalates
the case (once per breach): the escalation contact is told and, when the SLA says so, the
priority goes up one step.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import Text, and_, case as sa_case, cast, func, or_, text as sql_text
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.orm.attributes import flag_modified

from core import audit, context
from core.errors import BadRequest, Forbidden, NotFound
from modules.helpdesk.models import (HelpdeskAgent, HelpdeskArticle, HelpdeskCannedResponse, HelpdeskCase,
                                     HelpdeskCategory, HelpdeskMessage, HelpdeskSettings, HelpdeskSla)

PRIORITIES = ["low", "medium", "high", "urgent"]
STATUSES = {"new": "New", "open": "Open", "in_progress": "In progress", "waiting": "Waiting on employee",
            "resolved": "Resolved", "closed": "Closed", "cancelled": "Cancelled"}
ACTIVE = ("new", "open", "in_progress", "waiting")
DONE = ("resolved", "closed", "cancelled")
TRANSITIONS = {
    "new": {"open", "in_progress", "waiting", "resolved", "cancelled"},
    "open": {"in_progress", "waiting", "resolved", "cancelled"},
    "in_progress": {"open", "waiting", "resolved", "cancelled"},
    "waiting": {"in_progress", "resolved", "cancelled"},
    "resolved": {"closed", "in_progress"},
    "closed": {"in_progress"},
    "cancelled": set(),
}
SOURCES = {"web": "App", "email": "Email", "phone": "Phone call", "whatsapp": "WhatsApp", "walk_in": "In person"}
DEFAULT_SLAS = {"urgent": (60, 8 * 60), "high": (4 * 60, 24 * 60), "medium": (8 * 60, 72 * 60), "low": (24 * 60, 120 * 60)}

DEFAULT_SETTINGS = {
    "rating_enabled": True,
    "rating_scale": 5,
    "rating_labels": {"3": ["Not happy", "Okay", "Happy"],
                      "5": ["Very poor", "Poor", "Okay", "Good", "Excellent"]},
    "thank_you_message": "Thank you for your feedback. It helps us serve you better.",
    "reopen_on_poor_rating": True,
    "poor_rating_at_most": {"3": 1, "5": 2},
    "allow_requester_reopen": True,
    "reopen_window_days": 7,
    "block_comments_when_closed": True,
    "hide_priority_from_requester": False,
    "require_reply_before_resolve": True,
    "auto_close_after_days": 3,
    "pause_sla_while_waiting": True,
    "raise_for_others": True,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def actor_name() -> str:
    return context.current().actor_name or "System"


def _employee_model():
    from modules.employees.models import Employee
    return Employee


def employee(db: Session, company_id: int, employee_id):
    if not employee_id:
        return None
    E = _employee_model()
    return db.query(E).filter(E.company_id == company_id, E.id == employee_id).first()


def display_name(emp) -> str | None:
    return f"{emp.first_name} {emp.last_name}".strip() if emp else None


def names_for(db: Session, company_id: int, ids) -> dict[int, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    E = _employee_model()
    return {r.id: f"{r.first_name} {r.last_name}".strip()
            for r in db.query(E.id, E.first_name, E.last_name).filter(E.company_id == company_id, E.id.in_(ids))}


def notify(db: Session, company_id: int, employee_id, message: str) -> None:
    if not employee_id:
        return
    from modules.notifications.models import Notification
    db.add(Notification(company_id=company_id, employee_id=employee_id, channel="in_app", message_type="helpdesk",
                        message_content=message[:1000], status="sent", sent_at=_now()))


def has_perm(db: Session, user: dict, perm: str) -> bool:
    from core.security import _get_role_permissions
    return perm in _get_role_permissions(user["company_id"], user["role"], db)


def location_names(db: Session, company_id: int, ids) -> dict[int, str]:
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    try:
        with db.begin_nested():
            rows = db.execute(sql_text("SELECT id, name FROM locations WHERE company_id = :c AND id = ANY(:ids)"),
                              {"c": company_id, "ids": ids}).all()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}


def location_options(db: Session, company_id: int) -> list[dict]:
    try:
        with db.begin_nested():
            rows = db.execute(sql_text("SELECT id, name FROM locations WHERE company_id = :c ORDER BY name"),
                              {"c": company_id}).all()
        return [{"value": r[0], "label": r[1]} for r in rows]
    except Exception:
        return []


# ── Settings ────────────────────────────────────────────────────────

def get_settings(db: Session, company_id: int) -> dict:
    row = db.query(HelpdeskSettings).filter(HelpdeskSettings.company_id == company_id).first()
    return {**DEFAULT_SETTINGS, **((row.config if row else None) or {})}


def update_settings(db: Session, company_id: int, data: dict) -> dict:
    row = db.query(HelpdeskSettings).filter(HelpdeskSettings.company_id == company_id).first()
    if row is None:
        row = HelpdeskSettings(company_id=company_id, config={})
        db.add(row)
    config = {**DEFAULT_SETTINGS, **(row.config or {})}
    for k, v in data.items():
        if k not in DEFAULT_SETTINGS:
            continue
        if k == "rating_scale":
            if int(v) not in (3, 5):
                raise BadRequest("The rating scale is 3 or 5 points.")
            if int(v) != config["rating_scale"] and db.query(HelpdeskCase.id).filter(
                    HelpdeskCase.company_id == company_id, HelpdeskCase.rating.isnot(None)).first():
                raise BadRequest("Cases have already been rated on the current scale, so it can't change.")
            config[k] = int(v)
        elif k == "rating_labels":
            labels = dict(config["rating_labels"])
            for scale in ("3", "5"):
                if isinstance(v, dict) and isinstance(v.get(scale), list) and len(v[scale]) == int(scale):
                    labels[scale] = [str(x).strip()[:40] or labels[scale][i] for i, x in enumerate(v[scale])]
            config[k] = labels
        elif k == "poor_rating_at_most":
            config[k] = {s: max(1, min(int((v or {}).get(s, config[k][s])), int(s) - 1)) for s in ("3", "5")}
        elif isinstance(DEFAULT_SETTINGS[k], bool):
            config[k] = bool(v)
        elif isinstance(DEFAULT_SETTINGS[k], int):
            config[k] = max(0, min(int(v), 90))
        else:
            config[k] = str(v or "").strip()[:500] or DEFAULT_SETTINGS[k]
    row.config = config
    flag_modified(row, "config")
    db.flush()
    return config


# ── Categories, agents and SLAs ─────────────────────────────────────

def get_category(db: Session, company_id: int, uuid) -> HelpdeskCategory:
    row = (db.query(HelpdeskCategory).options(joinedload(HelpdeskCategory.agents))
           .filter(HelpdeskCategory.company_id == company_id, HelpdeskCategory.uuid == uuid).first())
    if row is None:
        raise NotFound("Category")
    return row


def category_by_id(db: Session, company_id: int, cid: int | None) -> HelpdeskCategory | None:
    if not cid:
        return None
    return db.query(HelpdeskCategory).filter(HelpdeskCategory.company_id == company_id, HelpdeskCategory.id == cid).first()


def save_category(db: Session, company_id: int, data: dict, row: HelpdeskCategory | None = None) -> HelpdeskCategory:
    name = (data.get("name") or (row.name if row else "")).strip()
    if not name:
        raise BadRequest("Give the category a name.")
    parent = category_by_id(db, company_id, data.get("parent_id")) if data.get("parent_id") else None
    if data.get("parent_id") and (parent is None or parent.parent_id is not None):
        raise BadRequest("A subcategory must sit under a main category.")
    if row is not None and parent is not None and parent.id == row.id:
        raise BadRequest("A category can't sit under itself.")
    dup = db.query(HelpdeskCategory.id).filter(HelpdeskCategory.company_id == company_id,
                                               func.lower(HelpdeskCategory.name) == name.lower(),
                                               HelpdeskCategory.parent_id == (parent.id if parent else None)
                                               if parent else HelpdeskCategory.parent_id.is_(None))
    if row is not None:
        dup = dup.filter(HelpdeskCategory.id != row.id)
    if dup.first():
        raise BadRequest(f"There is already a category called {name}.")
    if row is None:
        row = HelpdeskCategory(company_id=company_id, name=name)
        db.add(row)
    row.name = name[:120]
    if "parent_id" in data or row.id is None:
        row.parent_id = parent.id if parent else None
    for k in ("description", "icon", "require_subcategory", "feedback_mandatory", "active", "sort_order"):
        if k in data and data[k] is not None:
            setattr(row, k, data[k])
    if data.get("assignment_mode"):
        if data["assignment_mode"] not in ("pickup", "managed", "auto"):
            raise BadRequest("Assignment is pickup, managed or automatic.")
        row.assignment_mode = data["assignment_mode"]
    if data.get("default_priority"):
        if data["default_priority"] not in PRIORITIES:
            raise BadRequest("Unknown priority.")
        row.default_priority = data["default_priority"]
    for k in ("owner_employee_id", "escalate_to_employee_id"):
        if k in data:
            if data[k] and employee(db, company_id, data[k]) is None:
                raise BadRequest("The person was not found.")
            setattr(row, k, data[k] or None)
    db.flush()
    if "agents" in data and data["agents"] is not None:
        set_agents(db, company_id, row, data["agents"])
    return row


def set_agents(db: Session, company_id: int, cat: HelpdeskCategory, agents: list[dict]) -> None:
    """Replace the agents of a category. Open cases of agents who were removed go back to
    the queue (pickup) or to the category owner (managed)."""
    wanted = set()
    for a in agents:
        eid = a.get("employee_id")
        if employee(db, company_id, eid) is None:
            raise BadRequest("An agent was not found.")
        wanted.add((int(eid), a.get("location_id") or None))
    current = {(a.employee_id, a.location_id): a for a in cat.agents}
    for key, a in current.items():
        if key not in wanted:
            cat.agents.remove(a)
    for key in wanted - set(current):
        cat.agents.append(HelpdeskAgent(company_id=company_id, employee_id=key[0], location_id=key[1]))
    db.flush()
    still = {e for e, _ in wanted}
    removed = {e for e, _ in current} - still
    if removed:
        for c in db.query(HelpdeskCase).filter(HelpdeskCase.company_id == company_id,
                                               or_(HelpdeskCase.category_id == cat.id, HelpdeskCase.subcategory_id == cat.id),
                                               HelpdeskCase.assignee_employee_id.in_(removed),
                                               HelpdeskCase.status.in_(ACTIVE)).all():
            c.assignee_employee_id = None
            c.status = "new"
            _event(db, c, "Agent removed from the category; the case went back to the queue.")
            _auto_assign(db, c)


def categories(db: Session, company_id: int, include_inactive: bool = True) -> list[HelpdeskCategory]:
    q = db.query(HelpdeskCategory).options(joinedload(HelpdeskCategory.agents)).filter(
        HelpdeskCategory.company_id == company_id)
    if not include_inactive:
        q = q.filter(HelpdeskCategory.active.is_(True))
    return q.order_by(HelpdeskCategory.sort_order, HelpdeskCategory.name).all()


def save_sla(db: Session, company_id: int, data: dict, row: HelpdeskSla | None = None) -> HelpdeskSla:
    priority = data.get("priority") or (row.priority if row else None)
    if priority not in PRIORITIES:
        raise BadRequest("Pick a priority.")
    resp, res = int(data.get("response_minutes") or 0), int(data.get("resolution_minutes") or 0)
    if resp <= 0 or res <= 0:
        raise BadRequest("Set both the first response and the resolution time.")
    if resp > res:
        raise BadRequest("The first response time can't be longer than the resolution time.")
    cat_id = data.get("category_id") if "category_id" in data else (row.category_id if row else None)
    if cat_id and category_by_id(db, company_id, cat_id) is None:
        raise BadRequest("The category was not found.")
    dup = db.query(HelpdeskSla.id).filter(HelpdeskSla.company_id == company_id, HelpdeskSla.priority == priority,
                                          HelpdeskSla.active.is_(True),
                                          HelpdeskSla.category_id == cat_id if cat_id else HelpdeskSla.category_id.is_(None))
    if row is not None:
        dup = dup.filter(HelpdeskSla.id != row.id)
    if dup.first():
        raise BadRequest("There is already a target for this priority and category.")
    if row is None:
        row = HelpdeskSla(company_id=company_id, priority=priority, response_minutes=resp, resolution_minutes=res)
        db.add(row)
    row.priority, row.category_id, row.response_minutes, row.resolution_minutes = priority, cat_id or None, resp, res
    if "escalate_to_employee_id" in data:
        row.escalate_to_employee_id = data["escalate_to_employee_id"] or None
    for k in ("raise_priority", "active"):
        if k in data and data[k] is not None:
            setattr(row, k, bool(data[k]))
    db.flush()
    return row


def sla_for(db: Session, company_id: int, category_ids: list[int], priority: str) -> HelpdeskSla | None:
    """Most specific target: subcategory, then category, then company-wide."""
    rows = db.query(HelpdeskSla).filter(HelpdeskSla.company_id == company_id, HelpdeskSla.priority == priority,
                                        HelpdeskSla.active.is_(True)).all()
    for cid in [c for c in category_ids if c] + [None]:
        for r in rows:
            if r.category_id == cid:
                return r
    return None


# ── SLA computation (pure) ──────────────────────────────────────────

def compute_due(start: datetime, minutes: int | None) -> datetime | None:
    return start + timedelta(minutes=minutes) if minutes else None


def shift_for_pause(due: datetime | None, paused_at: datetime | None, resumed_at: datetime) -> datetime | None:
    if due is None or paused_at is None:
        return due
    return due + (resumed_at - paused_at)


def sla_state(c: HelpdeskCase, now: datetime | None = None) -> dict:
    """Where each target stands: met, breached, paused or running, with minutes left
    (negative when late)."""
    now = now or _now()
    clock = c.paused_at or now

    def one(due, done_at):
        if due is None:
            return {"due_at": None, "state": "none", "minutes_left": None, "late_by_minutes": None}
        if done_at is not None:
            late = (done_at - due).total_seconds() / 60
            return {"due_at": due, "state": "breached" if late > 0 else "met", "minutes_left": None,
                    "late_by_minutes": int(late) if late > 0 else 0}
        left = (due - clock).total_seconds() / 60
        state = "breached" if left < 0 else ("paused" if c.paused_at else ("due_soon" if left < 60 else "running"))
        return {"due_at": due, "state": state, "minutes_left": int(left), "late_by_minutes": int(-left) if left < 0 else 0}

    resolution_done = c.resolved_at if c.status in ("resolved", "closed") else (c.closed_at if c.status == "cancelled" else None)
    return {"response": one(c.response_due_at, c.first_response_at), "resolution": one(c.resolution_due_at, resolution_done)}


def is_breached(c: HelpdeskCase, now: datetime | None = None) -> bool:
    s = sla_state(c, now)
    return s["response"]["state"] == "breached" or s["resolution"]["state"] == "breached"


def apply_sla(db: Session, c: HelpdeskCase, start: datetime | None = None) -> None:
    sla = sla_for(db, c.company_id, [c.subcategory_id, c.category_id], c.priority)
    start = start or c.created_at or _now()
    if sla is None:
        c.response_due_at = c.resolution_due_at = None
        return
    c.response_due_at = compute_due(start, sla.response_minutes) if c.first_response_at is None else c.response_due_at
    c.resolution_due_at = compute_due(start, sla.resolution_minutes)


# ── Visibility ──────────────────────────────────────────────────────

def agent_category_ids(db: Session, company_id: int, employee_id: int | None) -> set[int]:
    if not employee_id:
        return set()
    ids = {r[0] for r in db.query(HelpdeskAgent.category_id).filter(HelpdeskAgent.company_id == company_id,
                                                                     HelpdeskAgent.employee_id == employee_id)}
    # agents of a category also see its subcategories
    if ids:
        ids |= {r[0] for r in db.query(HelpdeskCategory.id).filter(HelpdeskCategory.company_id == company_id,
                                                                    HelpdeskCategory.parent_id.in_(ids))}
    return ids


def owned_category_ids(db: Session, company_id: int, employee_id: int | None) -> set[int]:
    if not employee_id:
        return set()
    ids = {r[0] for r in db.query(HelpdeskCategory.id).filter(HelpdeskCategory.company_id == company_id,
                                                               HelpdeskCategory.owner_employee_id == employee_id)}
    if ids:
        ids |= {r[0] for r in db.query(HelpdeskCategory.id).filter(HelpdeskCategory.company_id == company_id,
                                                                    HelpdeskCategory.parent_id.in_(ids))}
    return ids


class Viewer:
    """What the signed-in person may do in the help desk."""

    def __init__(self, db: Session, user: dict):
        self.company_id = user["company_id"]
        self.employee_id = user.get("employee_id")
        self.admin = has_perm(db, user, "workplace.helpdesk.manage")
        self.agent_of = agent_category_ids(db, self.company_id, self.employee_id)
        self.owner_of = owned_category_ids(db, self.company_id, self.employee_id)

    @property
    def is_staff(self) -> bool:
        return self.admin or bool(self.agent_of) or bool(self.owner_of)

    def handles(self, c: HelpdeskCase) -> bool:
        cats = {c.category_id, c.subcategory_id}
        return (self.admin or bool(cats & self.agent_of) or bool(cats & self.owner_of)
                or (self.employee_id is not None and c.assignee_employee_id == self.employee_id))

    def manages(self, c: HelpdeskCase) -> bool:
        return self.admin or bool({c.category_id, c.subcategory_id} & self.owner_of)

    def is_requester(self, c: HelpdeskCase) -> bool:
        return self.employee_id is not None and c.requester_employee_id == self.employee_id

    def can_see(self, c: HelpdeskCase) -> bool:
        return self.handles(c) or self.is_requester(c)

    def staff_filter(self):
        if self.admin:
            return HelpdeskCase.company_id == self.company_id
        cats = list(self.agent_of | self.owner_of) or [0]
        return or_(HelpdeskCase.category_id.in_(cats), HelpdeskCase.subcategory_id.in_(cats),
                   HelpdeskCase.assignee_employee_id == (self.employee_id or 0))


def get_case(db: Session, company_id: int, uuid) -> HelpdeskCase:
    row = (db.query(HelpdeskCase).options(joinedload(HelpdeskCase.category), joinedload(HelpdeskCase.subcategory))
           .filter(HelpdeskCase.company_id == company_id, HelpdeskCase.uuid == uuid).first())
    if row is None:
        raise NotFound("Case")
    return row


def visible_case(db: Session, viewer: Viewer, uuid) -> HelpdeskCase:
    c = get_case(db, viewer.company_id, uuid)
    if not viewer.can_see(c):
        raise NotFound("Case")
    return c


def queue_query(db: Session, viewer: Viewer, queue: str):
    """my_requests | mine (assigned to me) | unassigned (pickup queue) | unmanaged (waiting
    for the owner to assign) | escalated | open | all."""
    cid = viewer.company_id
    q = (db.query(HelpdeskCase).options(joinedload(HelpdeskCase.category), joinedload(HelpdeskCase.subcategory))
         .filter(HelpdeskCase.company_id == cid))
    if queue == "my_requests":
        return q.filter(HelpdeskCase.requester_employee_id == (viewer.employee_id or 0))
    if not viewer.is_staff:
        raise Forbidden("Only help desk agents can see this list.")
    q = q.filter(viewer.staff_filter())
    if queue == "mine":
        return q.filter(HelpdeskCase.assignee_employee_id == (viewer.employee_id or 0), HelpdeskCase.status.in_(ACTIVE))
    if queue in ("unassigned", "unmanaged"):
        mode = ["pickup", "auto"] if queue == "unassigned" else ["managed"]
        cat_ids = [r[0] for r in db.query(HelpdeskCategory.id).filter(HelpdeskCategory.company_id == cid,
                                                                       HelpdeskCategory.assignment_mode.in_(mode))]
        return q.filter(HelpdeskCase.assignee_employee_id.is_(None), HelpdeskCase.status.in_(ACTIVE),
                        HelpdeskCase.category_id.in_(cat_ids or [0]))
    if queue == "escalated":
        return q.filter(HelpdeskCase.escalation_level > 0, HelpdeskCase.status.in_(ACTIVE))
    if queue == "open":
        return q.filter(HelpdeskCase.status.in_(ACTIVE))
    return q


# ── Cases ───────────────────────────────────────────────────────────

def _event(db: Session, c: HelpdeskCase, text: str) -> None:
    db.add(HelpdeskMessage(company_id=c.company_id, case_id=c.id, kind="event", body=text, author_name=actor_name()))


def _agents_for(db: Session, c: HelpdeskCase) -> list[int]:
    """Agents of the subcategory (else the category), for the case's location when any are
    set for it, else the agents for every location."""
    for cat_id in (c.subcategory_id, c.category_id):
        if not cat_id:
            continue
        rows = db.query(HelpdeskAgent).filter(HelpdeskAgent.company_id == c.company_id,
                                              HelpdeskAgent.category_id == cat_id).all()
        if not rows:
            continue
        local = [r.employee_id for r in rows if c.location_id and r.location_id == c.location_id]
        general = [r.employee_id for r in rows if r.location_id is None]
        picked = local or general or [r.employee_id for r in rows]
        E = _employee_model()
        active = {i for (i,) in db.query(E.id).filter(E.id.in_(picked), E.active.is_(True))}
        picked = [i for i in dict.fromkeys(picked) if i in active]
        if picked:
            return picked
    return []


def _auto_assign(db: Session, c: HelpdeskCase) -> None:
    cat = db.get(HelpdeskCategory, c.category_id)
    agents = _agents_for(db, c)
    if cat.assignment_mode == "auto" and agents:
        load = dict(db.query(HelpdeskCase.assignee_employee_id, func.count(HelpdeskCase.id))
                    .filter(HelpdeskCase.company_id == c.company_id, HelpdeskCase.assignee_employee_id.in_(agents),
                            HelpdeskCase.status.in_(ACTIVE)).group_by(HelpdeskCase.assignee_employee_id).all())
        target = min(agents, key=lambda a: (load.get(a, 0), agents.index(a)))
        c.assignee_employee_id = target
        c.status = "open"
        _event(db, c, f"Assigned automatically to {display_name(employee(db, c.company_id, target))}.")
        notify(db, c.company_id, target, f"New case {c.reference_no} assigned to you: {c.subject}")
    elif cat.assignment_mode == "managed":
        notify(db, c.company_id, cat.owner_employee_id, f"New case {c.reference_no} waiting to be assigned: {c.subject}")
    else:
        for a in agents:
            notify(db, c.company_id, a, f"New case {c.reference_no} in {cat.name}: {c.subject}")


def _custom(db: Session, company_id: int, values: dict | None, existing: dict | None) -> dict:
    """Custom field values of a case. Cases are raised from self service, so required
    custom fields are not enforced when a case is raised; agents fill them in later."""
    from core import custom_fields
    return custom_fields.merge_values(db, company_id, "helpdesk_case", values, existing, partial=True)


def create_case(db: Session, user: dict, data: dict) -> HelpdeskCase:
    cid = user["company_id"]
    viewer = Viewer(db, user)
    conf = get_settings(db, cid)
    requester_id = data.get("requester_employee_id") or user.get("employee_id")
    on_behalf = requester_id != user.get("employee_id")
    if on_behalf and not (viewer.is_staff and conf["raise_for_others"]):
        raise Forbidden("Only help desk agents can raise a case for someone else.")
    requester = employee(db, cid, requester_id)
    if requester is None:
        raise BadRequest("Pick the employee the case is for.")
    cat = category_by_id(db, cid, data.get("category_id"))
    if cat is None or not cat.active:
        raise BadRequest("Pick a category.")
    sub = None
    if cat.parent_id is not None:  # picked a subcategory directly
        sub, cat = cat, category_by_id(db, cid, cat.parent_id)
    elif data.get("subcategory_id"):
        sub = category_by_id(db, cid, data["subcategory_id"])
        if sub is None or sub.parent_id != cat.id or not sub.active:
            raise BadRequest("Pick a subcategory of this category.")
    has_subs = db.query(HelpdeskCategory.id).filter(HelpdeskCategory.parent_id == cat.id,
                                                    HelpdeskCategory.active.is_(True)).first()
    if cat.require_subcategory and has_subs and sub is None:
        raise BadRequest("Pick a subcategory.")
    subject = (data.get("subject") or "").strip()
    description = (data.get("description") or "").strip()
    if not subject:
        raise BadRequest("Write a short subject.")
    if not description:
        raise BadRequest("Describe your question or request.")
    priority = data.get("priority") or (sub.default_priority if sub else cat.default_priority)
    if priority not in PRIORITIES:
        raise BadRequest("Unknown priority.")
    if conf["hide_priority_from_requester"] and not viewer.is_staff:
        priority = sub.default_priority if sub else cat.default_priority
    source = data.get("source") or "web"
    if source not in SOURCES:
        raise BadRequest("Unknown source.")
    from core import numbering
    from modules.documents import service as documents
    now = _now()
    c = HelpdeskCase(company_id=cid, reference_no=numbering.next_number(db, cid, "ticket"), category_id=cat.id,
                     subcategory_id=sub.id if sub else None, case_type_id=data.get("case_type_id"),
                     location_id=data.get("location_id") or getattr(requester, "location_id", None),
                     subject=subject[:250], description=description, priority=priority, status="new", source=source,
                     requester_employee_id=requester.id, raised_by_employee_id=user.get("employee_id"),
                     raised_by_name=actor_name() if on_behalf else None,
                     attachments=documents.attachments_from(db, cid, data.get("attachments")),
                     custom=_custom(db, cid, data.get("custom"), None),
                     created_at=now, last_activity_at=now)
    db.add(c)
    db.flush()
    apply_sla(db, c, now)
    _event(db, c, f"Case raised by {actor_name()}" + (f" for {display_name(requester)}" if on_behalf else "") + ".")
    _auto_assign(db, c)
    if on_behalf:
        notify(db, cid, requester.id, f"{actor_name()} raised case {c.reference_no} for you: {c.subject}")
    db.flush()
    return c


def _require_handler(viewer: Viewer, c: HelpdeskCase) -> None:
    if not viewer.handles(c):
        raise Forbidden("Only the agents of this category can do this.")


def pick(db: Session, viewer: Viewer, c: HelpdeskCase) -> HelpdeskCase:
    if c.status not in ACTIVE:
        raise BadRequest("Only open cases can be picked up.")
    if c.assignee_employee_id:
        raise BadRequest("Someone has already picked up this case.")
    if not viewer.employee_id or not ({c.category_id, c.subcategory_id} & viewer.agent_of or viewer.admin):
        raise Forbidden("Only the agents of this category can pick up its cases.")
    c.assignee_employee_id = viewer.employee_id
    if c.status == "new":
        c.status = "open"
    _event(db, c, f"Picked up by {actor_name()}.")
    return c


def assign(db: Session, viewer: Viewer, c: HelpdeskCase, employee_id: int | None) -> HelpdeskCase:
    if not viewer.manages(c) and not viewer.handles(c):
        raise Forbidden("Only the category owner or its agents can assign cases.")
    if c.status not in ACTIVE:
        raise BadRequest("Closed cases can't be assigned.")
    if employee_id is None:
        c.assignee_employee_id = None
        c.status = "new"
        _event(db, c, f"{actor_name()} unassigned the case.")
        return c
    target = employee(db, c.company_id, employee_id)
    if target is None:
        raise BadRequest("The agent was not found.")
    c.assignee_employee_id = target.id
    if c.status == "new":
        c.status = "open"
    _event(db, c, f"Assigned to {display_name(target)} by {actor_name()}.")
    if target.id != viewer.employee_id:
        notify(db, c.company_id, target.id, f"Case {c.reference_no} assigned to you: {c.subject}")
    return c


def _pause(c: HelpdeskCase, now: datetime) -> None:
    if c.paused_at is None:
        c.paused_at = now


def _resume(c: HelpdeskCase, now: datetime) -> None:
    if c.paused_at is not None:
        if c.first_response_at is None:
            c.response_due_at = shift_for_pause(c.response_due_at, c.paused_at, now)
        c.resolution_due_at = shift_for_pause(c.resolution_due_at, c.paused_at, now)
        c.paused_at = None


def change_status(db: Session, viewer: Viewer, c: HelpdeskCase, status: str, note: str | None = None) -> HelpdeskCase:
    if status not in STATUSES:
        raise BadRequest("Unknown status.")
    requester_only = viewer.is_requester(c) and not viewer.handles(c)
    if requester_only and status not in ("cancelled", "closed"):
        raise Forbidden("You can only withdraw or close your own case.")
    if not requester_only and not viewer.handles(c):
        raise Forbidden("Only the agents of this category can change the status.")
    if status == c.status:
        return c
    if requester_only and status == "cancelled" and c.status not in ("new", "open"):
        raise BadRequest("The case is already being worked on. Reply instead, or ask the agent to close it.")
    if requester_only and status == "closed" and c.status != "resolved":
        raise BadRequest("You can close the case once it is resolved.")
    if status not in TRANSITIONS[c.status]:
        raise BadRequest(f"A {STATUSES[c.status].lower()} case can't move to {STATUSES[status].lower()}.")
    conf = get_settings(db, c.company_id)
    now = _now()
    if status == "resolved" and conf["require_reply_before_resolve"]:
        replied = db.query(HelpdeskMessage.id).filter(HelpdeskMessage.case_id == c.id, HelpdeskMessage.kind == "reply",
                                                      HelpdeskMessage.author_employee_id != c.requester_employee_id).first()
        if not replied and not (note or "").strip():
            raise BadRequest("Reply to the employee before resolving the case.")
    old = c.status
    if status == "waiting" and conf["pause_sla_while_waiting"]:
        _pause(c, now)
    elif old == "waiting":
        _resume(c, now)
    if status == "resolved":
        c.resolved_at = now
        c.paused_at = None
    if status == "closed":
        c.closed_at = now
        c.resolved_at = c.resolved_at or now
    if status == "cancelled":
        c.closed_at = now
    if status == "in_progress" and old in ("resolved", "closed"):
        return reopen(db, viewer, c, note)
    if status in ("open", "in_progress") and not c.assignee_employee_id and viewer.employee_id and viewer.handles(c):
        c.assignee_employee_id = viewer.employee_id
    c.status = status
    c.last_activity_at = now
    if (note or "").strip():
        add_message(db, viewer, c, "reply", note, [], _skip_status=True)
    _event(db, c, f"{actor_name()} changed the status from {STATUSES[old].lower()} to {STATUSES[status].lower()}.")
    if not requester_only:
        notify(db, c.company_id, c.requester_employee_id, f"Case {c.reference_no} is now {STATUSES[status].lower()}.")
    return c


def reopen(db: Session, viewer: Viewer, c: HelpdeskCase, reason: str | None = None, *, automatic: bool = False) -> HelpdeskCase:
    if c.status not in ("resolved", "closed"):
        raise BadRequest("Only resolved or closed cases can be reopened.")
    conf = get_settings(db, c.company_id)
    if not automatic and viewer.is_requester(c) and not viewer.handles(c):
        if not conf["allow_requester_reopen"]:
            raise Forbidden("Closed cases can't be reopened. Raise a new case instead.")
        since = c.closed_at or c.resolved_at
        if since and (_now() - since) > timedelta(days=conf["reopen_window_days"]):
            raise BadRequest(f"Cases can be reopened within {conf['reopen_window_days']} days. Raise a new case instead.")
        if not (reason or "").strip():
            raise BadRequest("Say why you are reopening the case.")
    now = _now()
    c.status = "in_progress" if c.assignee_employee_id else "new"
    c.resolved_at = c.closed_at = None
    c.reopened_count += 1
    c.last_activity_at = now
    if c.resolution_due_at and c.resolution_due_at < now and not c.resolution_breached:
        pass  # a breach stays on record (resolution_breached is set by the check)
    if (reason or "").strip():
        db.add(HelpdeskMessage(company_id=c.company_id, case_id=c.id, kind="reply", body=reason.strip(),
                               author_employee_id=viewer.employee_id if viewer else None, author_name=actor_name()))
    _event(db, c, ("Reopened automatically after a poor rating." if automatic else f"Reopened by {actor_name()}."))
    notify(db, c.company_id, c.assignee_employee_id, f"Case {c.reference_no} was reopened.")
    return c


def add_message(db: Session, viewer: Viewer, c: HelpdeskCase, kind: str, body: str, attachments: list | None,
                _skip_status: bool = False) -> HelpdeskMessage:
    body = (body or "").strip()
    if not body and not attachments:
        raise BadRequest("Write a message.")
    conf = get_settings(db, c.company_id)
    handler = viewer.handles(c)
    if kind == "note" and not handler:
        raise Forbidden("Only agents can add internal notes.")
    if kind not in ("reply", "note"):
        raise BadRequest("Unknown message type.")
    if c.status in ("closed", "cancelled") and conf["block_comments_when_closed"] and kind == "reply":
        raise BadRequest("This case is closed. Reopen it to continue the conversation.")
    from modules.documents import service as documents
    now = _now()
    m = HelpdeskMessage(company_id=c.company_id, case_id=c.id, kind=kind, body=body or "(attachment)",
                        author_employee_id=viewer.employee_id, author_name=actor_name(),
                        attachments=documents.attachments_from(db, c.company_id, attachments), created_at=now)
    db.add(m)
    c.last_activity_at = now
    if kind == "reply" and not _skip_status:
        from_requester = viewer.is_requester(c) and not (handler and c.assignee_employee_id == viewer.employee_id)
        if from_requester:
            if c.status == "waiting":
                _resume(c, now)
                c.status = "in_progress"
            notify(db, c.company_id, c.assignee_employee_id, f"New reply on case {c.reference_no}.")
        else:
            if c.first_response_at is None:
                c.first_response_at = now
                if c.response_due_at and now > c.response_due_at:
                    c.response_breached = True
            if not c.assignee_employee_id and viewer.employee_id:
                c.assignee_employee_id = viewer.employee_id
            if c.status in ("new", "open"):
                c.status = "in_progress"
            notify(db, c.company_id, c.requester_employee_id, f"{actor_name()} replied on case {c.reference_no}.")
    elif kind == "reply" and handler and c.first_response_at is None:
        c.first_response_at = now
    db.flush()
    return m


def rate(db: Session, viewer: Viewer, c: HelpdeskCase, rating: int, comment: str | None) -> dict:
    conf = get_settings(db, c.company_id)
    if not viewer.is_requester(c):
        raise Forbidden("Only the person who raised the case can rate it.")
    if not conf["rating_enabled"]:
        raise BadRequest("Ratings are switched off.")
    if c.status not in ("resolved", "closed"):
        raise BadRequest("You can rate the case once it is resolved.")
    if c.rating is not None:
        raise BadRequest("You have already rated this case.")
    scale = conf["rating_scale"]
    if not 1 <= int(rating) <= scale:
        raise BadRequest(f"Pick a rating from 1 to {scale}.")
    c.rating, c.rating_comment, c.rated_at = int(rating), (comment or "").strip()[:1000] or None, _now()
    label = conf["rating_labels"][str(scale)][int(rating) - 1]
    _event(db, c, f"Rated {rating} of {scale} ({label}).")
    poor = int(rating) <= conf["poor_rating_at_most"][str(scale)]
    reopened = False
    if poor and conf["reopen_on_poor_rating"]:
        reopen(db, viewer, c, c.rating_comment, automatic=True)
        c.rating = None  # a new rating is asked when the case is resolved again
        c.rated_at = None
        reopened = True
    elif c.status == "resolved":
        c.status = "closed"
        c.closed_at = _now()
    notify(db, c.company_id, c.assignee_employee_id, f"Case {c.reference_no} was rated {rating} of {scale}.")
    audit.event(db, "helpdesk.rated", "helpdesk_cases", c.id, {"rating": rating, "scale": scale, "reopened": reopened},
                entity_uuid=str(c.uuid), entity_label=c.reference_no)
    return {"reopened": reopened, "message": conf["thank_you_message"]}


def update_case(db: Session, viewer: Viewer, c: HelpdeskCase, data: dict) -> HelpdeskCase:
    _require_handler(viewer, c)
    changes = []
    if data.get("priority") and data["priority"] != c.priority:
        if data["priority"] not in PRIORITIES:
            raise BadRequest("Unknown priority.")
        changes.append(f"priority {c.priority} → {data['priority']}")
        c.priority = data["priority"]
        if c.status in ACTIVE:
            apply_sla(db, c, c.created_at)
    if data.get("category_id") and data["category_id"] != c.category_id:
        cat = category_by_id(db, c.company_id, data["category_id"])
        if cat is None or cat.parent_id is not None:
            raise BadRequest("Pick a main category.")
        changes.append(f"category → {cat.name}")
        c.category_id, c.subcategory_id = cat.id, None
    if "subcategory_id" in data and data["subcategory_id"] != c.subcategory_id:
        sub = category_by_id(db, c.company_id, data["subcategory_id"]) if data["subcategory_id"] else None
        if sub is not None and sub.parent_id != c.category_id:
            raise BadRequest("Pick a subcategory of this category.")
        c.subcategory_id = sub.id if sub else None
        changes.append(f"subcategory → {sub.name if sub else 'none'}")
    if "case_type_id" in data:
        c.case_type_id = data["case_type_id"]
    if data.get("custom") is not None:
        c.custom = _custom(db, c.company_id, data["custom"], c.custom)
        changes.append("other details")
    if changes:
        _event(db, c, f"{actor_name()} changed " + ", ".join(changes) + ".")
    return c


# ── SLA checks and escalation (background job) ──────────────────────

PRIORITY_UP = {"low": "medium", "medium": "high", "high": "urgent", "urgent": "urgent"}


def check_sla(db: Session, company_id: int | None = None, now: datetime | None = None) -> int:
    """Mark breaches and escalate late cases. Safe to run any number of times."""
    now = now or _now()
    q = db.query(HelpdeskCase).filter(HelpdeskCase.status.in_(ACTIVE), HelpdeskCase.paused_at.is_(None))
    if company_id is not None:
        q = q.filter(HelpdeskCase.company_id == company_id)
    count = 0
    for c in q.filter(or_(and_(HelpdeskCase.response_breached.is_(False), HelpdeskCase.first_response_at.is_(None),
                               HelpdeskCase.response_due_at < now),
                          and_(HelpdeskCase.resolution_breached.is_(False), HelpdeskCase.resolution_due_at < now))
                      ).limit(500).all():
        if not c.response_breached and c.first_response_at is None and c.response_due_at and c.response_due_at < now:
            c.response_breached = True
            notify(db, c.company_id, c.assignee_employee_id, f"Case {c.reference_no} has passed its first response time.")
            _event(db, c, "First response time passed.")
            count += 1
        if not c.resolution_breached and c.resolution_due_at and c.resolution_due_at < now:
            c.resolution_breached = True
            escalate(db, c, "Resolution time passed")
            count += 1
    db.flush()
    return count


def escalate(db: Session, c: HelpdeskCase, reason: str) -> None:
    sla = sla_for(db, c.company_id, [c.subcategory_id, c.category_id], c.priority)
    cat = db.get(HelpdeskCategory, c.category_id)
    target = (sla.escalate_to_employee_id if sla else None) or cat.escalate_to_employee_id or cat.owner_employee_id
    c.escalation_level += 1
    c.escalated_at = _now()
    raised = ""
    if (sla is None or sla.raise_priority) and c.priority != "urgent":
        old = c.priority
        c.priority = PRIORITY_UP[c.priority]
        raised = f" Priority raised from {old} to {c.priority}."
    who = display_name(employee(db, c.company_id, target)) if target else None
    db.add(HelpdeskMessage(company_id=c.company_id, case_id=c.id, kind="event", author_name="System",
                           body=f"Escalated: {reason}." + (f" {who} was told." if who else "") + raised))
    notify(db, c.company_id, target, f"Escalated case {c.reference_no}: {c.subject}." + raised)
    notify(db, c.company_id, c.assignee_employee_id, f"Case {c.reference_no} was escalated.{raised}")
    audit.event(db, "helpdesk.escalated", "helpdesk_cases", c.id, {"reason": reason, "to": who, "priority": c.priority},
                company_id=c.company_id, entity_uuid=str(c.uuid), entity_label=c.reference_no)


def auto_close(db: Session, now: datetime | None = None) -> int:
    """Resolved cases nobody rated or reopened close after the configured days."""
    now = now or _now()
    n = 0
    by_company: dict[int, dict] = {}
    for c in db.query(HelpdeskCase).filter(HelpdeskCase.status == "resolved").limit(1000).all():
        conf = by_company.setdefault(c.company_id, get_settings(db, c.company_id))
        days = conf["auto_close_after_days"]
        if days and c.resolved_at and now - c.resolved_at > timedelta(days=days):
            c.status, c.closed_at = "closed", now
            db.add(HelpdeskMessage(company_id=c.company_id, case_id=c.id, kind="event", author_name="System",
                                   body=f"Closed automatically {days} days after it was resolved."))
            n += 1
    return n


# ── Canned responses and knowledge base ─────────────────────────────

def save_canned(db: Session, company_id: int, data: dict, row: HelpdeskCannedResponse | None = None):
    title, body = (data.get("title") or "").strip(), (data.get("body") or "").strip()
    if not title or not body:
        raise BadRequest("Give the response a title and text.")
    if row is None:
        row = HelpdeskCannedResponse(company_id=company_id, title=title, body=body, created_by_name=actor_name())
        db.add(row)
    row.title, row.body = title[:150], body
    row.category_id = data.get("category_id") or None
    if "active" in data and data["active"] is not None:
        row.active = bool(data["active"])
    db.flush()
    return row


def save_article(db: Session, company_id: int, data: dict, row: HelpdeskArticle | None = None):
    title, body = (data.get("title") or "").strip(), (data.get("body") or "").strip()
    if not title or not body:
        raise BadRequest("Give the article a title and text.")
    if row is None:
        row = HelpdeskArticle(company_id=company_id, title=title, body=body, author_name=actor_name())
        db.add(row)
    row.title, row.body = title[:250], body
    row.category_id = data.get("category_id") or None
    row.tags = [str(t).strip()[:40] for t in (data.get("tags") or []) if str(t).strip()][:10]
    if "published" in data and data["published"] is not None:
        row.published = bool(data["published"])
    db.flush()
    return row


def search_articles(db: Session, company_id: int, q: str | None, category_id: int | None = None,
                    published_only: bool = True, limit: int = 20):
    """Full-text search (English stemming) on title, text and tags, with a plain
    substring match as a fallback for short or partial words."""
    query = db.query(HelpdeskArticle).filter(HelpdeskArticle.company_id == company_id)
    if published_only:
        query = query.filter(HelpdeskArticle.published.is_(True))
    if category_id:
        query = query.filter(HelpdeskArticle.category_id == category_id)
    q = (q or "").strip()
    if not q:
        return query.order_by(HelpdeskArticle.views.desc(), HelpdeskArticle.title).limit(limit).all()
    doc = func.to_tsvector("english", HelpdeskArticle.title + " " + HelpdeskArticle.body + " " +
                           func.coalesce(cast(HelpdeskArticle.tags, Text), ""))
    ts = func.websearch_to_tsquery("english", q)
    like = f"%{q}%"
    rank = func.ts_rank(doc, ts) + sa_case((HelpdeskArticle.title.ilike(like), 1.0), else_=0.0)
    return (query.filter(or_(doc.op("@@")(ts), HelpdeskArticle.title.ilike(like), HelpdeskArticle.body.ilike(like)))
            .order_by(rank.desc(), HelpdeskArticle.title).limit(limit).all())


# ── Reports ─────────────────────────────────────────────────────────

def report(db: Session, company_id: int, start: datetime, end: datetime) -> dict:
    base = db.query(HelpdeskCase).filter(HelpdeskCase.company_id == company_id, HelpdeskCase.created_at >= start,
                                         HelpdeskCase.created_at < end)
    rows = base.all()
    cats = {c.id: c.name for c in db.query(HelpdeskCategory).filter(HelpdeskCategory.company_id == company_id)}
    now = _now()
    by_cat: dict[int, dict] = {}
    breaches = []
    ratings = {}
    for c in rows:
        b = by_cat.setdefault(c.category_id, {"category_id": c.category_id, "category": cats.get(c.category_id),
                                              "total": 0, "open": 0, "resolved": 0, "breached": 0,
                                              "_res_minutes": [], "_resp_minutes": []})
        b["total"] += 1
        if c.status in ACTIVE:
            b["open"] += 1
        if c.status in ("resolved", "closed"):
            b["resolved"] += 1
            if c.resolved_at:
                b["_res_minutes"].append((c.resolved_at - c.created_at).total_seconds() / 60)
        if c.first_response_at:
            b["_resp_minutes"].append((c.first_response_at - c.created_at).total_seconds() / 60)
        state = sla_state(c, now)
        if c.response_breached or c.resolution_breached or state["response"]["state"] == "breached" \
                or state["resolution"]["state"] == "breached":
            b["breached"] += 1
            breaches.append({"uuid": str(c.uuid), "reference_no": c.reference_no, "subject": c.subject,
                             "category": cats.get(c.category_id), "priority": c.priority, "status": c.status,
                             "assignee_employee_id": c.assignee_employee_id,
                             "response_late_minutes": state["response"]["late_by_minutes"],
                             "resolution_late_minutes": state["resolution"]["late_by_minutes"],
                             "resolution_due_at": c.resolution_due_at, "created_at": c.created_at})
        if c.rating:
            ratings[c.rating] = ratings.get(c.rating, 0) + 1
    names = names_for(db, company_id, [b["assignee_employee_id"] for b in breaches])
    for b in breaches:
        b["assignee_name"] = names.get(b["assignee_employee_id"])

    def avg(xs):
        return round(sum(xs) / len(xs) / 60, 1) if xs else None
    volume = []
    for b in sorted(by_cat.values(), key=lambda x: -x["total"]):
        b["avg_resolution_hours"] = avg(b.pop("_res_minutes"))
        b["avg_first_response_hours"] = avg(b.pop("_resp_minutes"))
        b["sla_met_percent"] = round(100 * (b["total"] - b["breached"]) / b["total"], 1) if b["total"] else None
        volume.append(b)
    total = len(rows)
    breached = sum(b["breached"] for b in volume)
    rated = sum(ratings.values())
    return {"total": total, "open": sum(b["open"] for b in volume), "breached": breached,
            "sla_met_percent": round(100 * (total - breached) / total, 1) if total else None,
            "avg_rating": round(sum(k * v for k, v in ratings.items()) / rated, 2) if rated else None,
            "ratings": ratings, "by_category": volume,
            "breaches": sorted(breaches, key=lambda x: -(x["resolution_late_minutes"] or 0))[:200]}
