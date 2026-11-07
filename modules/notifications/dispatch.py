"""Sends in-app notifications through the other channels, and the daily digests.

- Right after a transaction that wrote notifications commits, a background thread sends
  them (`MODE = "thread"`); the periodic job catches anything left over. Each row is
  claimed with one UPDATE ... RETURNING, so a notification goes out at most once even
  when two workers race.
- For each notification: the recipient's channels for its category (their own choice,
  else the company default); email uses the company's wording (templates); approval
  emails carry one-time approve and reject links.
- Digests: once a day after the company's digest hour, a summary of pending approvals to
  each approver, and an HR summary to HR and the owner.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from modules.notifications import channels
from modules.notifications.models import Notification, NotificationDigest

logger = logging.getLogger("hrms")

# "thread": send right after commit in a background thread; "inline": send in the
# committing thread (tests); "off": leave it to the periodic job.
MODE = "thread"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Claiming and sending ────────────────────────────────────────────

def _claim(db: Session, ids: list[int] | None = None, limit: int = 200) -> list[int]:
    if ids is not None:
        if not ids:
            return []
        rows = db.execute(text("""
            UPDATE notifications SET dispatched_at = now()
            WHERE id = ANY(:ids) AND dispatched_at IS NULL
            RETURNING id"""), {"ids": list(ids)}).fetchall()
    else:
        rows = db.execute(text("""
            UPDATE notifications SET dispatched_at = now()
            WHERE id IN (SELECT id FROM notifications WHERE dispatched_at IS NULL
                         ORDER BY id LIMIT :limit FOR UPDATE SKIP LOCKED)
            RETURNING id"""), {"limit": limit}).fetchall()
    return [r[0] for r in rows]


def _recipient(db: Session, n: Notification) -> tuple[str | None, str | None]:
    """(email address, display name) of the person a notification is for."""
    from modules.employees.models import Employee
    if n.employee_id:
        e = db.query(Employee).filter(Employee.id == n.employee_id, Employee.company_id == n.company_id).first()
        if e is None or not e.active:
            return None, None
        return e.email, (e.preferred_name or e.first_name)
    if n.recipient == "owner":
        from modules.company.models import Company
        c = db.query(Company).filter(Company.id == n.company_id).first()
        if c is None:
            return None, None
        return c.email, "there"
    return None, None


def _company_name(db: Session, company_id: int) -> str:
    from modules.company.models import Company
    return db.query(Company.company_name).filter(Company.id == company_id).scalar() or ""


def _email_for(db: Session, n: Notification, name: str) -> tuple[str, str]:
    from core.workflow.email_actions import company_url
    base = company_url(db, n.company_id)
    company = _company_name(db, n.company_id)
    data = n.data or {}
    if (n.message_type or "").startswith("approval") and data.get("task_uuid") and \
            n.message_type in ("approval", "approval.nudge", "approval.reminder"):
        from core.workflow.email_actions import links_for_task
        links = links_for_task(db, n.company_id, data["task_uuid"])
        if links is not None:
            task = links["task"]
            inst = task.instance
            from core.workflow import registry
            rt = registry.get(inst.request_type)
            summary = "\n".join(x for x in (rt.summarize(inst) if rt and rt.summarize else []) if x)
            values = {"name": name, "requester": inst.requester_name or "Someone", "request_title": inst.title,
                      "reference": inst.reference_no or "", "summary": summary,
                      "approve_link": links.get("approve_url") or links["view_url"],
                      "reject_link": links.get("reject_url") or links["view_url"],
                      "view_link": links["view_url"], "link_hours": links["link_hours"], "company": company}
            subject, body = channels.render_template(db, n.company_id, "approval.request", values)
            if n.message_type != "approval":
                subject = f"{n.title or subject}"
            return subject, body
    link = n.link
    if link and link.startswith("/"):
        link = f"{base}{link}"
    message = n.message_content if n.message_content and n.message_content != n.title else (n.title or "")
    values = {"name": name, "title": n.title or n.message_content, "message": message,
              "link_line": f"Open it: {link}" if link else "", "company": company}
    return channels.render_template(db, n.company_id, "notification", values)


def send_one(db: Session, n: Notification, settings_cache: dict) -> int:
    if n.channel != "in_app":
        return 0
    category = channels.category_for(n.message_type)
    if n.company_id not in settings_cache:
        settings_cache[n.company_id] = channels.company_settings(db, n.company_id)
    ch = channels.channels_for(db, n.company_id, n.employee_id, category, settings_cache[n.company_id])
    sent = 0
    if ch["email"]:
        address, name = _recipient(db, n)
        if address:
            subject, body = _email_for(db, n, name or "there")
            channels.queue(db, n.company_id, "email", address, subject, body, category=category,
                           employee_id=n.employee_id, notification_id=n.id)
            sent += 1
    if ch["push"] and n.employee_id:
        channels.queue(db, n.company_id, "push", None, n.title or "Notification", n.message_content or "",
                       category=category, employee_id=n.employee_id, notification_id=n.id)
        sent += 1
    return sent


def dispatch(db: Session, ids: list[int] | None = None) -> int:
    """Send claimed notifications. Run with an RLS-bypassing session."""
    claimed = _claim(db, ids)
    if not claimed:
        return 0
    cache: dict = {}
    n_sent = 0
    for n in db.query(Notification).filter(Notification.id.in_(claimed)).order_by(Notification.id).all():
        try:
            with db.begin_nested():
                n_sent += send_one(db, n, cache)
        except Exception as exc:  # one bad row never blocks the rest
            logger.warning("Notification %s could not be sent: %s", n.id, exc)
    db.flush()
    return n_sent


def _dispatch_in_new_session(ids: list[int]) -> None:
    from core.db import system_session
    db = system_session()
    try:
        dispatch(db, ids)
        db.commit()
    except Exception as exc:  # pragma: no cover
        db.rollback()
        logger.warning("Notification dispatch failed: %s", exc)
    finally:
        db.close()


@event.listens_for(Session, "after_flush")
def _collect(session: Session, _ctx) -> None:
    ids = [o.id for o in session.new if isinstance(o, Notification) and o.channel == "in_app" and o.id]
    if ids:
        session.info.setdefault("new_notification_ids", []).extend(ids)


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    ids = session.info.pop("new_notification_ids", None)
    if not ids or MODE == "off":
        return
    if MODE == "inline":
        _dispatch_in_new_session(ids)
        return
    threading.Thread(target=_dispatch_in_new_session, args=(ids,), daemon=True).start()


@event.listens_for(Session, "after_rollback")
def _after_rollback(session: Session) -> None:
    session.info.pop("new_notification_ids", None)


def periodic_job(db: Session) -> int:
    """Every 15 minutes: anything not yet sent, retries that are due, and digests."""
    n = dispatch(db)
    n += channels.retry_due(db)
    n += send_digests(db)
    return n


# ── Digests ─────────────────────────────────────────────────────────

def _company_now(db: Session, company_id: int):
    from zoneinfo import ZoneInfo
    from modules.company.models import Company
    tz = db.query(Company.timezone).filter(Company.id == company_id).scalar() or "Asia/Kolkata"
    try:
        return datetime.now(ZoneInfo(tz))
    except Exception:
        return datetime.now(ZoneInfo("Asia/Kolkata"))


def _mark(db: Session, company_id: int, key: str, kind: str, day, items: int) -> bool:
    """Record that a digest went out; False when it already had."""
    try:
        with db.begin_nested():
            db.add(NotificationDigest(company_id=company_id, recipient_key=key, kind=kind, digest_date=day, items=items))
            db.flush()
        return True
    except IntegrityError:
        return False


def _pending_by_approver(db: Session, company_id: int) -> dict:
    from core.workflow.models import WorkflowInstance, WorkflowTask
    rows = (db.query(WorkflowTask, WorkflowInstance)
            .join(WorkflowInstance, WorkflowTask.instance_id == WorkflowInstance.id)
            .filter(WorkflowTask.company_id == company_id, WorkflowTask.status == "pending",
                    WorkflowInstance.status == "pending")
            .order_by(WorkflowTask.created_at).all())
    out: dict = {}
    for t, i in rows:
        key = channels.recipient_key(t.approver_employee_id if t.approver_kind == "employee" else None)
        out.setdefault(key, []).append((t, i))
    return out


def _line(t, i, now) -> str:
    days = max(0, (now - t.created_at).days) if t.created_at else 0
    late = " (late)" if t.due_at and t.due_at < now else ""
    waited = "today" if days == 0 else f"{days} day(s)"
    return f"- {i.title} ({i.reference_no or 'no number'}) from {i.requester_name or 'someone'}, waiting {waited}{late}"


def send_digests(db: Session, company_id: int | None = None, force: bool = False) -> int:
    from modules.company.models import Company
    q = db.query(Company.id)
    if company_id is not None:
        q = q.filter(Company.id == company_id)
    sent = 0
    for (cid,) in q.all():
        try:
            with db.begin_nested():
                sent += _company_digests(db, cid, force)
        except Exception as exc:
            logger.warning("Digest for company %s failed: %s", cid, exc)
    return sent


def _company_digests(db: Session, cid: int, force: bool) -> int:
    from core.workflow.email_actions import company_url
    from modules.employees.models import Employee
    settings = channels.company_settings(db, cid)
    dg = settings["digest"]
    local = _company_now(db, cid)
    if not force and local.hour < int(dg.get("hour", 8)):
        return 0
    today = local.date()
    now = _now()
    base = company_url(db, cid)
    company = _company_name(db, cid)
    sent = 0
    pending = _pending_by_approver(db, cid)
    if dg.get("manager", True):
        for key, items in pending.items():
            emp_id = int(key[2:]) if key.startswith("e:") else None
            if not channels.channels_for(db, cid, emp_id, "approvals", settings)["email"]:
                continue
            fake = Notification(company_id=cid, employee_id=emp_id, recipient=None if emp_id else "owner")
            address, name = _recipient(db, fake)
            if not address or not _mark(db, cid, key, "manager", today, len(items)):
                continue
            subject, body = channels.render_template(db, cid, "digest.manager", {
                "name": name or "there", "count": len(items), "items": "\n".join(_line(t, i, now) for t, i in items[:30])
                + (f"\n... and {len(items) - 30} more" if len(items) > 30 else ""),
                "inbox_link": f"{base}/dashboard/inbox", "company": company})
            channels.queue(db, cid, "email", address, subject, body, category="approvals", source="digest",
                           employee_id=emp_id)
            sent += 1
    if dg.get("hr", True):
        lines = _hr_lines(db, cid, pending, now, today)
        hr_people = (db.query(Employee).filter(Employee.company_id == cid, Employee.role == "hr",
                                               Employee.active.is_(True)).limit(20).all())
        recipients = [(channels.recipient_key(e.id), e.email, e.preferred_name or e.first_name, e.id) for e in hr_people]
        from modules.company.models import Company
        c = db.query(Company).filter(Company.id == cid).first()
        if c is not None:
            recipients.append(("owner", c.email, "there", None))
        for key, address, name, emp_id in recipients:
            if not address or not channels.channels_for(db, cid, emp_id, "approvals", settings)["email"]:
                continue
            if not _mark(db, cid, key, "hr", today, len(lines)):
                continue
            subject, body = channels.render_template(db, cid, "digest.hr", {
                "name": name or "there", "date": today.strftime("%d %b %Y"), "items": "\n".join(lines),
                "app_link": f"{base}/dashboard", "company": company})
            channels.queue(db, cid, "email", address, subject, body, category="approvals", source="digest",
                           employee_id=emp_id)
            sent += 1
    return sent


def _count(db: Session, sql: str, params: dict) -> int:
    try:
        with db.begin_nested():
            return int(db.execute(text(sql), params).scalar() or 0)
    except Exception:
        return 0


def _hr_lines(db: Session, cid: int, pending: dict, now, today) -> list[str]:
    total = sum(len(v) for v in pending.values())
    late = sum(1 for v in pending.values() for t, _ in v if t.due_at and t.due_at < now)
    p = {"cid": cid, "today": today, "soon": today + timedelta(days=30), "fortnight": today + timedelta(days=14),
         "week_ago": today - timedelta(days=7)}
    lines = [f"- Requests waiting for a decision: {total}" + (f" ({late} past their time limit)" if late else "")]
    joiners = _count(db, "SELECT count(*) FROM employees WHERE company_id = :cid AND date_of_joining BETWEEN :today "
                         "AND :fortnight", p)
    probation = _count(db, "SELECT count(*) FROM employees WHERE company_id = :cid AND status = 'probation' AND "
                           "probation_end_date BETWEEN :today AND :fortnight", p)
    leaving = _count(db, "SELECT count(*) FROM employees WHERE company_id = :cid AND status = 'notice' AND "
                         "exit_date BETWEEN :today AND :fortnight", p)
    expiring = _count(db, "SELECT count(*) FROM employee_documents WHERE company_id = :cid AND archived_at IS NULL "
                          "AND expiry_date BETWEEN :today AND :soon", p)
    unverified = _count(db, "SELECT count(*) FROM employee_documents WHERE company_id = :cid AND archived_at IS NULL "
                            "AND verification_status = 'pending'", p)
    lines += [f"- Joining in the next 14 days: {joiners}", f"- Probation ending in the next 14 days: {probation}",
              f"- Leaving in the next 14 days: {leaving}", f"- Documents expiring in the next 30 days: {expiring}",
              f"- Documents waiting to be checked: {unverified}"]
    return lines
