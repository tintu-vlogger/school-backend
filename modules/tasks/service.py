"""Tasks: assign, track, comment, repeat and remind.

Other modules create tasks with `create_task(...)`, for example an onboarding checklist:

    from modules.tasks import service as tasks
    tasks.create_task(db, company_id, title="Issue laptop", assignee_employee_id=it_admin.id,
                      due_date=joining_date, source_module="onboarding", source_ref=str(checklist.uuid),
                      checklist=["Image the laptop", "Create email account"])

Who can do what
- the assignee: change status, tick the checklist, comment
- the person who created it, people who manage tasks (workplace.tasks.manage) and the
  assignee's reporting manager: everything, including editing and reassigning
- nobody deletes a task; it is cancelled instead
"""
from __future__ import annotations

import calendar
import uuid as uuid_mod
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload, selectinload

from core import context
from core.errors import BadRequest, Forbidden, NotFound
from core.timeutil import today_for_company_id
from modules.tasks.models import Task, TaskChecklistItem, TaskComment

PRIORITIES = ["low", "medium", "high", "urgent"]
STATUSES = {"todo": "To do", "in_progress": "In progress", "done": "Done", "cancelled": "Cancelled"}
FREQS = {"daily": "Every day", "weekly": "Every week", "monthly": "Every month", "yearly": "Every year"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def actor_name() -> str:
    return context.current().actor_name or "System"


def _employee_model():
    from modules.employees.models import Employee
    return Employee


def employee(db: Session, company_id: int, eid):
    if not eid:
        return None
    E = _employee_model()
    return db.query(E).filter(E.company_id == company_id, E.id == eid).first()


def names_for(db: Session, company_id: int, ids) -> dict[int, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    E = _employee_model()
    return {r.id: f"{r.first_name} {r.last_name}".strip()
            for r in db.query(E.id, E.first_name, E.last_name).filter(E.company_id == company_id, E.id.in_(ids))}


def notify(db: Session, company_id: int, employee_id, message: str) -> None:
    if not employee_id or employee_id == context.current().employee_id:
        return
    from modules.notifications.models import Notification
    db.add(Notification(company_id=company_id, employee_id=employee_id, channel="in_app", message_type="task",
                        message_content=message[:1000], status="sent", sent_at=_now()))


def has_perm(db: Session, user: dict, perm: str) -> bool:
    from core.security import _get_role_permissions
    return perm in _get_role_permissions(user["company_id"], user["role"], db)


def report_ids(db: Session, company_id: int, manager_id: int | None) -> list[int]:
    if not manager_id:
        return []
    E = _employee_model()
    return [i for (i,) in db.query(E.id).filter(E.company_id == company_id, E.reporting_manager_id == manager_id,
                                                E.active.is_(True))]


# ── Recurrence (pure) ───────────────────────────────────────────────

def clean_recurrence(rec: dict | None) -> dict | None:
    if not rec or not rec.get("freq"):
        return None
    if rec["freq"] not in FREQS:
        raise BadRequest("Repeat every day, week, month or year.")
    interval = int(rec.get("interval") or 1)
    if not 1 <= interval <= 12:
        raise BadRequest("Repeat between every 1 and 12 periods.")
    out = {"freq": rec["freq"], "interval": interval}
    if rec.get("until"):
        out["until"] = str(rec["until"])[:10]
    return out


def next_date(d: date, rec: dict) -> date:
    n = int(rec.get("interval") or 1)
    if rec["freq"] == "daily":
        return d + timedelta(days=n)
    if rec["freq"] == "weekly":
        return d + timedelta(weeks=n)
    months = n * (12 if rec["freq"] == "yearly" else 1)
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


# ── Create and read ─────────────────────────────────────────────────

def create_task(db: Session, company_id: int, *, title: str, assignee_employee_id: int | None = None,
                description: str | None = None, due_date: date | None = None, start_date: date | None = None,
                priority: str = "medium", checklist: list[str] | None = None, labels: list[str] | None = None,
                attachments: list[dict] | None = None, recurrence: dict | None = None,
                source_module: str | None = None, source_ref: str | None = None,
                created_by_employee_id: int | None = None, created_by_name: str | None = None,
                remind_days_before: int = 1, series_id=None) -> Task:
    """Create a task (the one function other modules call)."""
    title = (title or "").strip()
    if not title:
        raise BadRequest("Give the task a title.")
    if priority not in PRIORITIES:
        raise BadRequest("Unknown priority.")
    if assignee_employee_id and employee(db, company_id, assignee_employee_id) is None:
        raise BadRequest("The person was not found.")
    if start_date and due_date and due_date < start_date:
        raise BadRequest("The due date can't be before the start date.")
    rec = clean_recurrence(recurrence)
    if rec and not due_date:
        raise BadRequest("A repeating task needs a due date.")
    ctx = context.current()
    pos = (db.query(func.coalesce(func.max(Task.position), 0)).filter(Task.company_id == company_id).scalar() or 0) + 1
    t = Task(company_id=company_id, title=title[:200], description=(description or "").strip() or None,
             assignee_employee_id=assignee_employee_id, priority=priority, status="todo", start_date=start_date,
             due_date=due_date, labels=[str(x).strip()[:30] for x in (labels or []) if str(x).strip()][:8],
             attachments=attachments or [], recurrence=rec, series_id=series_id or (uuid_mod.uuid4() if rec else None),
             source_module=source_module, source_ref=source_ref, position=pos,
             remind_days_before=max(0, min(int(remind_days_before or 0), 30)),
             created_by_employee_id=created_by_employee_id if created_by_employee_id is not None else ctx.employee_id,
             created_by_name=created_by_name or actor_name())
    db.add(t)
    db.flush()
    for i, item in enumerate(checklist or []):
        if str(item).strip():
            db.add(TaskChecklistItem(company_id=company_id, task_id=t.id, title=str(item).strip()[:300], position=i))
    db.flush()
    if assignee_employee_id:
        notify(db, company_id, assignee_employee_id, f"New task for you: {t.title}"
               + (f" (due {t.due_date.strftime('%d %b')})" if t.due_date else ""))
    return t


def tasks_for_source(db: Session, company_id: int, source_module: str, source_ref: str) -> list[Task]:
    return (db.query(Task).filter(Task.company_id == company_id, Task.source_module == source_module,
                                  Task.source_ref == source_ref).order_by(Task.id).all())


def get(db: Session, company_id: int, uuid) -> Task:
    row = (db.query(Task).options(selectinload(Task.checklist), selectinload(Task.comments))
           .filter(Task.company_id == company_id, Task.uuid == uuid).first())
    if row is None:
        raise NotFound("Task")
    return row


class Viewer:
    def __init__(self, db: Session, user: dict):
        self.company_id = user["company_id"]
        self.employee_id = user.get("employee_id")
        self.admin = has_perm(db, user, "workplace.tasks.manage")
        self.reports = set(report_ids(db, self.company_id, self.employee_id))

    def can_see(self, t: Task) -> bool:
        return self.admin or (self.employee_id is not None and (
            t.assignee_employee_id == self.employee_id or t.created_by_employee_id == self.employee_id
            or t.assignee_employee_id in self.reports))

    def can_edit(self, t: Task) -> bool:
        return self.admin or (self.employee_id is not None and (
            t.created_by_employee_id == self.employee_id or t.assignee_employee_id in self.reports))

    def can_work(self, t: Task) -> bool:
        return self.can_edit(t) or (self.employee_id is not None and t.assignee_employee_id == self.employee_id)

    def can_assign_to(self, employee_id: int | None) -> bool:
        return (self.admin or employee_id is None or employee_id == self.employee_id or employee_id in self.reports
                or self.employee_id is not None)  # anyone may hand a task to a colleague


def visible(db: Session, v: Viewer, uuid) -> Task:
    t = get(db, v.company_id, uuid)
    if not v.can_see(t):
        raise NotFound("Task")
    return t


def list_query(db: Session, v: Viewer, scope: str):
    q = db.query(Task).options(selectinload(Task.checklist)).filter(Task.company_id == v.company_id)
    me = v.employee_id or 0
    if scope == "mine":
        return q.filter(Task.assignee_employee_id == me)
    if scope == "assigned_by_me":
        if v.employee_id is None:  # the owner: tasks the owner or the system created
            return q.filter(Task.created_by_employee_id.is_(None), Task.source_module.is_(None))
        return q.filter(Task.created_by_employee_id == me)
    if scope == "team":
        return q.filter(Task.assignee_employee_id.in_(list(v.reports) or [0]))
    if scope == "all":
        if not v.admin:
            raise Forbidden("Only people who manage tasks can see everyone's tasks.")
        return q
    raise BadRequest("Unknown list.")


def apply_filters(db: Session, q, company_id: int, *, status: str | None = None, priority: str | None = None,
                  due: str | None = None, assignee_id: int | None = None, search: str | None = None,
                  source_module: str | None = None, include_done: bool = True):
    today = today_for_company_id(db, company_id)
    if status:
        q = q.filter(Task.status.in_(status.split(",")))
    elif not include_done:
        q = q.filter(Task.status.in_(("todo", "in_progress")))
    else:
        q = q.filter(Task.status != "cancelled")
    if priority:
        q = q.filter(Task.priority == priority)
    if assignee_id:
        q = q.filter(Task.assignee_employee_id == assignee_id)
    if source_module:
        q = q.filter(Task.source_module == source_module)
    if due == "overdue":
        q = q.filter(Task.due_date < today, Task.status.in_(("todo", "in_progress")))
    elif due == "today":
        q = q.filter(Task.due_date == today)
    elif due == "week":
        q = q.filter(Task.due_date >= today, Task.due_date <= today + timedelta(days=7))
    elif due == "none":
        q = q.filter(Task.due_date.is_(None))
    if search:
        like = f"%{search.strip()}%"
        q = q.filter(or_(Task.title.ilike(like), Task.description.ilike(like)))
    return q


# ── Change ──────────────────────────────────────────────────────────

def update(db: Session, v: Viewer, t: Task, data: dict) -> Task:
    if not v.can_edit(t):
        raise Forbidden("Only the person who created the task or a manager can edit it.")
    for k in ("title", "description"):
        if k in data and data[k] is not None:
            val = data[k].strip()
            if k == "title" and not val:
                raise BadRequest("Give the task a title.")
            setattr(t, k, (val or None) if k == "description" else val[:200])
    if "priority" in data and data["priority"]:
        if data["priority"] not in PRIORITIES:
            raise BadRequest("Unknown priority.")
        t.priority = data["priority"]
    for k in ("due_date", "start_date"):
        if k in data:
            setattr(t, k, data[k])
            if k == "due_date":
                t.reminded_on, t.overdue_notified = None, False
    if t.start_date and t.due_date and t.due_date < t.start_date:
        raise BadRequest("The due date can't be before the start date.")
    if "labels" in data and data["labels"] is not None:
        t.labels = [str(x).strip()[:30] for x in data["labels"] if str(x).strip()][:8]
    if "recurrence" in data:
        t.recurrence = clean_recurrence(data["recurrence"])
        if t.recurrence and not t.series_id:
            t.series_id = uuid_mod.uuid4()
        if t.recurrence and not t.due_date:
            raise BadRequest("A repeating task needs a due date.")
    if "remind_days_before" in data and data["remind_days_before"] is not None:
        t.remind_days_before = max(0, min(int(data["remind_days_before"]), 30))
    if "assignee_employee_id" in data and data["assignee_employee_id"] != t.assignee_employee_id:
        new = data["assignee_employee_id"]
        if new and employee(db, t.company_id, new) is None:
            raise BadRequest("The person was not found.")
        t.assignee_employee_id = new
        notify(db, t.company_id, new, f"Task assigned to you: {t.title}")
    return t


def set_status(db: Session, v: Viewer, t: Task, status: str) -> Task | None:
    """Returns the next occurrence when a repeating task is completed."""
    if status not in STATUSES:
        raise BadRequest("Unknown status.")
    if not v.can_work(t):
        raise Forbidden("You can't change this task.")
    if status == "cancelled" and not v.can_edit(t):
        raise Forbidden("Only the person who created the task can cancel it.")
    if status == t.status:
        return None
    was = t.status
    t.status = status
    nxt = None
    if status == "done":
        t.completed_at = _now()
        if t.created_by_employee_id and t.created_by_employee_id != v.employee_id:
            notify(db, t.company_id, t.created_by_employee_id, f"Task done: {t.title}")
        nxt = spawn_next(db, t)
    elif was == "done":
        t.completed_at = None
    return nxt


def spawn_next(db: Session, t: Task) -> Task | None:
    rec = t.recurrence
    if not rec or not t.due_date:
        return None
    due = next_date(t.due_date, rec)
    if rec.get("until") and due > date.fromisoformat(rec["until"]):
        return None
    if db.query(Task.id).filter(Task.company_id == t.company_id, Task.series_id == t.series_id,
                                Task.due_date == due).first():
        return None
    start = next_date(t.start_date, rec) if t.start_date else None
    return create_task(db, t.company_id, title=t.title, description=t.description, assignee_employee_id=t.assignee_employee_id,
                       due_date=due, start_date=start, priority=t.priority,
                       checklist=[c.title for c in t.checklist], labels=t.labels, recurrence=rec,
                       source_module=t.source_module, source_ref=t.source_ref,
                       created_by_employee_id=t.created_by_employee_id, created_by_name=t.created_by_name,
                       remind_days_before=t.remind_days_before, series_id=t.series_id)


def move(db: Session, v: Viewer, t: Task, status: str, before_uuid=None, after_uuid=None) -> Task | None:
    """Kanban: drop a card into a column between two others."""
    nxt = set_status(db, v, t, status) if status != t.status else None
    def pos_of(u):
        if not u:
            return None
        r = db.query(Task.position).filter(Task.company_id == t.company_id, Task.uuid == u).first()
        return r[0] if r else None
    before, after = pos_of(before_uuid), pos_of(after_uuid)
    if before is not None and after is not None:
        t.position = (before + after) / 2
    elif before is not None:
        t.position = before - 1
    elif after is not None:
        t.position = after + 1
    return nxt


def add_checklist_item(db: Session, v: Viewer, t: Task, title: str) -> TaskChecklistItem:
    if not v.can_work(t):
        raise Forbidden("You can't change this task.")
    title = (title or "").strip()
    if not title:
        raise BadRequest("Write the checklist item.")
    if len(t.checklist) >= 50:
        raise BadRequest("A task can have up to 50 checklist items.")
    item = TaskChecklistItem(company_id=t.company_id, task_id=t.id, title=title[:300],
                             position=(max([c.position for c in t.checklist], default=-1) + 1))
    db.add(item)
    db.flush()
    return item


def toggle_checklist_item(db: Session, v: Viewer, t: Task, item_uuid, done: bool | None, title: str | None = None) -> None:
    if not v.can_work(t):
        raise Forbidden("You can't change this task.")
    item = next((c for c in t.checklist if str(c.uuid) == str(item_uuid)), None)
    if item is None:
        raise NotFound("Checklist item")
    if title is not None and title.strip():
        item.title = title.strip()[:300]
    if done is not None:
        item.done = done
        item.done_at = _now() if done else None
        item.done_by_name = actor_name() if done else None
    if t.status == "todo" and any(c.done for c in t.checklist):
        t.status = "in_progress"


def remove_checklist_item(db: Session, v: Viewer, t: Task, item_uuid) -> None:
    if not v.can_work(t):
        raise Forbidden("You can't change this task.")
    item = next((c for c in t.checklist if str(c.uuid) == str(item_uuid)), None)
    if item is None:
        raise NotFound("Checklist item")
    t.checklist.remove(item)


def add_comment(db: Session, v: Viewer, t: Task, body: str, attachments: list[dict]) -> TaskComment:
    if not v.can_see(t):
        raise Forbidden("You can't comment on this task.")
    body = (body or "").strip()
    if not body and not attachments:
        raise BadRequest("Write a comment.")
    c = TaskComment(company_id=t.company_id, task_id=t.id, body=body or "(attachment)", author_employee_id=v.employee_id,
                    author_name=actor_name(), attachments=attachments)
    db.add(c)
    for target in {t.assignee_employee_id, t.created_by_employee_id} - {v.employee_id, None}:
        notify(db, t.company_id, target, f"New comment on {t.title}")
    db.flush()
    return c


# ── Reminders (background job) ──────────────────────────────────────

def run_reminders(db: Session) -> int:
    """Due-soon reminder (remind_days_before) and one overdue notice per task."""
    n = 0
    open_tasks = (db.query(Task).filter(Task.status.in_(("todo", "in_progress")), Task.due_date.isnot(None),
                                        Task.assignee_employee_id.isnot(None)).limit(5000).all())
    today_by_company: dict[int, date] = {}
    for t in open_tasks:
        today = today_by_company.setdefault(t.company_id, today_for_company_id(db, t.company_id))
        if t.due_date < today and not t.overdue_notified:
            _notify_job(db, t.company_id, t.assignee_employee_id, f"Overdue: {t.title} was due {t.due_date.strftime('%d %b')}.")
            if t.created_by_employee_id and t.created_by_employee_id != t.assignee_employee_id:
                _notify_job(db, t.company_id, t.created_by_employee_id, f"Overdue: {t.title}.")
            t.overdue_notified = True
            n += 1
        elif t.due_date >= today and t.remind_days_before and (t.due_date - today).days <= t.remind_days_before \
                and t.reminded_on is None:
            when = "today" if t.due_date == today else ("tomorrow" if (t.due_date - today).days == 1
                                                       else t.due_date.strftime("on %d %b"))
            _notify_job(db, t.company_id, t.assignee_employee_id, f"Reminder: {t.title} is due {when}.")
            t.reminded_on = today
            n += 1
    return n


def _notify_job(db: Session, company_id: int, employee_id, message: str) -> None:
    from modules.notifications.models import Notification
    db.add(Notification(company_id=company_id, employee_id=employee_id, channel="in_app", message_type="task",
                        message_content=message[:1000], status="sent", sent_at=_now()))


def summary(db: Session, v: Viewer) -> dict:
    today = today_for_company_id(db, v.company_id)
    me = v.employee_id or 0
    base = db.query(Task).filter(Task.company_id == v.company_id, Task.assignee_employee_id == me)
    open_ = base.filter(Task.status.in_(("todo", "in_progress")))
    return {"open": open_.count(), "overdue": open_.filter(Task.due_date < today).count(),
            "due_today": open_.filter(Task.due_date == today).count(),
            "done_this_week": base.filter(Task.status == "done",
                                          Task.completed_at >= _now() - timedelta(days=7)).count(),
            "team_open": db.query(Task).filter(Task.company_id == v.company_id,
                                               Task.assignee_employee_id.in_(list(v.reports) or [0]),
                                               Task.status.in_(("todo", "in_progress"))).count()}
