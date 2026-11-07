"""In-app notifications (the bell in the app header).

`notify(...)` adds one row per recipient. Recipients are employee ids, or None for the
company owner (who has no employee record). A `group_key` collapses repeats: while an
unread notification with the same key exists for the person, it is updated instead of
adding another ("Priya and 3 others reacted to your post").
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from modules.notifications.models import Notification

OWNER = None  # recipient id meaning "the company owner"


def _now():
    return datetime.now(timezone.utc)


def visible(query, company_id: int, employee_id: int | None):
    """Rows the signed-in person may see: their own, plus old-style broadcasts."""
    query = query.filter(Notification.company_id == company_id)
    if employee_id:
        return query.filter(or_(Notification.employee_id == employee_id,
                                (Notification.employee_id.is_(None)) & (Notification.recipient.is_(None))))
    return query.filter(Notification.employee_id.is_(None))


def notify(db: Session, company_id: int, recipients, *, kind: str, title: str, body: str = "",
           link: str | None = None, actor_employee_id: int | None = None, actor_name: str | None = None,
           group_key: str | None = None, data: dict | None = None, skip: set | None = None) -> int:
    """Notify each recipient (employee id, or None for the owner). People in `skip` (usually
    the actor) are left out. Returns how many rows were written."""
    seen, count = set(), 0
    for rid in recipients:
        if rid in seen or (skip and rid in skip):
            continue
        seen.add(rid)
        existing = None
        if group_key:
            q = db.query(Notification).filter(Notification.company_id == company_id,
                                              Notification.group_key == group_key,
                                              Notification.read_at.is_(None))
            q = q.filter(Notification.employee_id == rid) if rid else q.filter(Notification.employee_id.is_(None),
                                                                                Notification.recipient == "owner")
            existing = q.first()
        if existing is not None:
            existing.title, existing.message_content = title[:200], body or title
            existing.actor_name, existing.actor_employee_id = actor_name, actor_employee_id
            existing.link, existing.data = link, data
            existing.created_at = _now()
        else:
            db.add(Notification(
                company_id=company_id, employee_id=rid, recipient=None if rid else "owner",
                channel="in_app", message_type=kind, title=title[:200], message_content=body or title,
                link=link, actor_employee_id=actor_employee_id, actor_name=actor_name, group_key=group_key,
                data=data, status="sent", sent_at=_now(),
            ))
        count += 1
    return count


def unread_count(db: Session, company_id: int, employee_id: int | None) -> int:
    return visible(db.query(Notification.id), company_id, employee_id).filter(Notification.read_at.is_(None)).count()


def mark_read(db: Session, company_id: int, employee_id: int | None, uuids: list | None = None) -> int:
    q = visible(db.query(Notification), company_id, employee_id).filter(Notification.read_at.is_(None))
    if uuids is not None:
        q = q.filter(Notification.uuid.in_(uuids))
    # old-style broadcast rows are shared, so they can't carry one person's read state
    q = q.filter(Notification.employee_id == employee_id) if employee_id else q
    now, n = _now(), 0
    for row in q.all():
        row.read_at = now
        n += 1
    return n
