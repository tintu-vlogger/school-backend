"""Notification channels: categories, each person's channel choices, email templates and
the delivery log.

Every in-app notification (a row in `notifications`, whoever wrote it) is picked up by
`dispatch.py`, which looks up the recipient's choices for the notification's category and
sends it by email and push as well. Other modules that need a plain email to an address
(automation rules, digests, tests) call `send_email(...)`, which logs the delivery and
retries failures.

Email goes through the company's own SMTP server (Settings > Integrations > Email) or the
platform's SMTP settings. Without either, in development the message is written to the
log (status "logged") so flows can be tried; in production it fails and is retried.
"""
from __future__ import annotations

import logging
import re
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from sqlalchemy.orm import Session

from core.config import get_settings
from modules.notifications.models import (NotificationDelivery, NotificationPreference, NotificationSettings,
                                          NotificationTemplate)

logger = logging.getLogger("hrms")

MAX_ATTEMPTS = 5

# ── Categories ──────────────────────────────────────────────────────

CATEGORIES = {
    "approvals": ("Approvals", "Requests waiting for you and decisions on your requests"),
    "time": ("Leave and attendance", "Leave, attendance, timesheets and shifts"),
    "payroll": ("Pay and expenses", "Payslips, salary, tax, expenses, loans and benefits"),
    "people": ("Your records", "Documents, letters, profile changes, onboarding, probation and exits"),
    "tasks": ("Tasks and help desk", "Tasks, help desk cases, grievances and assets"),
    "talent": ("Hiring, performance and learning", "Interviews, reviews, goals, feedback and courses"),
    "engagement": ("Buzz and celebrations", "Posts, kudos, announcements, surveys and birthdays"),
    "automation": ("Automatic alerts", "Messages sent by the company's automation rules"),
    "general": ("Everything else", "Other messages from the system"),
}
# Out of the box: email for things people must act on, not for social updates.
DEFAULT_CHANNELS = {
    "approvals": {"email": True, "push": True},
    "time": {"email": True, "push": True},
    "payroll": {"email": True, "push": False},
    "people": {"email": True, "push": False},
    "tasks": {"email": True, "push": False},
    "talent": {"email": True, "push": False},
    "engagement": {"email": False, "push": False},
    "automation": {"email": True, "push": False},
    "general": {"email": False, "push": False},
}
_PREFIXES = [
    ("approvals", ("approval", "workflow", "nudge")),
    ("time", ("leave", "attendance", "timesheet", "roster", "shift", "holiday", "regular", "comp_off", "overtime")),
    ("payroll", ("payroll", "payslip", "salary", "expense", "loan", "fbp", "tax", "benefit", "settlement", "form16",
                 "investment", "reimburse")),
    ("people", ("document", "letter", "esign", "profile", "employee", "onboarding", "probation", "exit", "hr_process",
                "policy", "clearance", "job")),
    ("tasks", ("task", "helpdesk", "ticket", "case", "grievance", "posh", "asset")),
    ("talent", ("recruit", "interview", "candidate", "offer", "referral", "performance", "review", "goal", "okr",
                "learning", "course", "feedback", "pip", "one_on_one", "calibration")),
    ("engagement", ("kudos", "buzz", "post", "comment", "reaction", "announcement", "survey", "celebration",
                    "birthday", "anniversary", "mention", "poll", "kiosk")),
    ("automation", ("automation",)),
]


def category_for(message_type: str | None) -> str:
    kind = (message_type or "").lower()
    for cat, prefixes in _PREFIXES:
        if any(kind.startswith(p) for p in prefixes):
            return cat
    return "general"


def recipient_key(employee_id: int | None) -> str:
    return f"e:{employee_id}" if employee_id else "owner"


# ── Company settings and personal choices ───────────────────────────

DEFAULT_SETTINGS = {
    "email_enabled": True,
    "push_enabled": True,
    "categories": DEFAULT_CHANNELS,
    "digest": {"manager": True, "hr": True, "hour": 8},
}


def settings_row(db: Session, company_id: int, create: bool = False) -> NotificationSettings | None:
    row = db.query(NotificationSettings).filter(NotificationSettings.company_id == company_id).first()
    if row is None and create:
        row = NotificationSettings(company_id=company_id, config={})
        db.add(row)
        db.flush()
    return row


def company_settings(db: Session, company_id: int) -> dict:
    row = settings_row(db, company_id)
    cfg = (row.config if row else None) or {}
    cats = {k: {**DEFAULT_CHANNELS[k], **((cfg.get("categories") or {}).get(k) or {})} for k in CATEGORIES}
    return {**DEFAULT_SETTINGS, **cfg, "categories": cats,
            "digest": {**DEFAULT_SETTINGS["digest"], **(cfg.get("digest") or {})}}


def channels_for(db: Session, company_id: int, employee_id: int | None, category: str,
                 settings: dict | None = None) -> dict:
    """{"in_app": True, "email": bool, "push": bool} for one person and category."""
    settings = settings or company_settings(db, company_id)
    base = dict(settings["categories"].get(category) or DEFAULT_CHANNELS["general"])
    pref = (db.query(NotificationPreference)
            .filter(NotificationPreference.company_id == company_id,
                    NotificationPreference.recipient_key == recipient_key(employee_id),
                    NotificationPreference.category == category).first())
    if pref is not None:
        base = {"email": pref.email, "push": pref.push}
    return {"in_app": True, "email": bool(base.get("email")) and settings.get("email_enabled", True),
            "push": bool(base.get("push")) and settings.get("push_enabled", True)}


def my_preferences(db: Session, company_id: int, employee_id: int | None) -> list[dict]:
    settings = company_settings(db, company_id)
    rows = {p.category: p for p in db.query(NotificationPreference).filter(
        NotificationPreference.company_id == company_id,
        NotificationPreference.recipient_key == recipient_key(employee_id)).all()}
    out = []
    for key, (label, desc) in CATEGORIES.items():
        d = settings["categories"][key]
        p = rows.get(key)
        out.append({"category": key, "label": label, "description": desc,
                    "email": p.email if p else bool(d.get("email")), "push": p.push if p else bool(d.get("push")),
                    "default_email": bool(d.get("email")), "default_push": bool(d.get("push")),
                    "customised": p is not None})
    return out


def save_preferences(db: Session, company_id: int, employee_id: int | None, items: list[dict]) -> None:
    key = recipient_key(employee_id)
    rows = {p.category: p for p in db.query(NotificationPreference).filter(
        NotificationPreference.company_id == company_id, NotificationPreference.recipient_key == key).all()}
    for item in items:
        cat = item.get("category")
        if cat not in CATEGORIES:
            continue
        row = rows.get(cat)
        if row is None:
            row = NotificationPreference(company_id=company_id, recipient_key=key, category=cat)
            db.add(row)
        row.email = bool(item.get("email"))
        row.push = bool(item.get("push"))
    db.flush()


# ── Templates ───────────────────────────────────────────────────────

TEMPLATES = {
    "notification": {
        "label": "Any notification",
        "description": "Used for every notification sent by email unless a more specific email below applies.",
        "subject": "{{title}}",
        "body": "Hello {{name}},\n\n{{message}}\n\n{{link_line}}\n\n{{company}}",
        "placeholders": ["name", "title", "message", "link_line", "company"],
    },
    "approval.request": {
        "label": "Approval needed",
        "description": "Sent to an approver when a request is waiting for them. Includes one-click approve and "
                       "reject links when those are switched on.",
        "subject": "Approval needed: {{request_title}}",
        "body": "Hello {{name}},\n\n{{requester}} asked for: {{request_title}} ({{reference}}).\n{{summary}}\n\n"
                "Approve: {{approve_link}}\nReject: {{reject_link}}\n\nOr open it in the app: {{view_link}}\n\n"
                "The approve and reject links work once and stop working after {{link_hours}} hours.\n\n{{company}}",
        "placeholders": ["name", "requester", "request_title", "reference", "summary", "approve_link", "reject_link",
                         "view_link", "link_hours", "company"],
    },
    "digest.manager": {
        "label": "Daily summary for managers",
        "description": "Sent each morning to people who have approvals waiting.",
        "subject": "{{count}} approval(s) waiting for you",
        "body": "Hello {{name}},\n\nThese requests are waiting for your decision:\n\n{{items}}\n\n"
                "Open your approvals: {{inbox_link}}\n\n{{company}}",
        "placeholders": ["name", "count", "items", "inbox_link", "company"],
    },
    "digest.hr": {
        "label": "Daily summary for HR",
        "description": "Sent each morning to HR: requests waiting across the company and what needs attention.",
        "subject": "HR summary for {{date}}",
        "body": "Hello {{name}},\n\nHere is today's summary.\n\n{{items}}\n\nOpen the app: {{app_link}}\n\n{{company}}",
        "placeholders": ["name", "date", "items", "app_link", "company"],
    },
}
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")


def render(text: str, values: dict) -> str:
    return _PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), "")), text or "")


def template_for(db: Session, company_id: int, key: str) -> dict:
    base = TEMPLATES[key]
    row = (db.query(NotificationTemplate).filter(NotificationTemplate.company_id == company_id,
                                                 NotificationTemplate.key == key,
                                                 NotificationTemplate.active.is_(True)).first())
    if row is not None:
        return {"subject": row.subject, "body": row.body, "custom": True}
    return {"subject": base["subject"], "body": base["body"], "custom": False}


def render_template(db: Session, company_id: int, key: str, values: dict) -> tuple[str, str]:
    t = template_for(db, company_id, key)
    subject = " ".join(render(t["subject"], values).split())[:250]
    body = re.sub(r"\n{3,}", "\n\n", render(t["body"], values)).strip() + "\n"
    return subject, body


# ── Email ───────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def smtp_for(db: Session, company_id: int) -> dict | None:
    try:
        from modules.assistant import integrations
        cfg = integrations.smtp_config(db, company_id)
        if cfg:
            return cfg
    except Exception as exc:  # pragma: no cover
        logger.warning("Company SMTP settings unreadable: %s", exc)
    s = get_settings()
    if s.SMTP_HOST and s.SMTP_FROM:
        return {"host": s.SMTP_HOST, "port": s.SMTP_PORT, "tls": s.SMTP_TLS, "username": s.SMTP_USER,
                "password": s.SMTP_PASSWORD, "from_email": s.SMTP_FROM}
    return None


def _smtp_send(cfg: dict, to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = f"{cfg['from_name']} <{cfg['from_email']}>" if cfg.get("from_name") else cfg["from_email"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    port = int(cfg.get("port") or 587)
    if port == 465:
        smtp = smtplib.SMTP_SSL(cfg["host"], port, timeout=20)
    else:
        smtp = smtplib.SMTP(cfg["host"], port, timeout=20)
        if cfg.get("tls", True):
            smtp.starttls()
    with smtp:
        if cfg.get("username"):
            smtp.login(cfg["username"], cfg.get("password") or "")
        smtp.send_message(msg)


def _is_development() -> bool:
    return not get_settings().is_production


def attempt(db: Session, d: NotificationDelivery) -> NotificationDelivery:
    """Try one delivery now and record what happened."""
    d.attempts = (d.attempts or 0) + 1
    try:
        if d.channel == "email":
            cfg = smtp_for(db, d.company_id)
            if cfg is None:
                if _is_development():
                    logger.warning("[email not configured] To: %s | %s\n%s", d.to_address, d.subject, d.body)
                    d.status, d.sent_at, d.last_error, d.next_attempt_at = "logged", _now(), None, None
                    return d
                raise RuntimeError("Email is not set up (Settings > Integrations > Email).")
            _smtp_send(cfg, d.to_address, d.subject or "", d.body or "")
        elif d.channel == "push":
            _push(db, d)
        d.status, d.sent_at, d.last_error, d.next_attempt_at = "sent", _now(), None, None
    except Exception as exc:
        d.last_error = str(exc)[:500]
        if d.attempts >= MAX_ATTEMPTS:
            d.status, d.next_attempt_at = "failed", None
        else:
            d.status = "queued"
            d.next_attempt_at = _now() + timedelta(minutes=5 * (2 ** (d.attempts - 1)))
    return d


def _push(db: Session, d: NotificationDelivery) -> None:
    """Mobile push through the saved device token (FCM). Without a token there is nothing
    to send to, which is not an error."""
    import asyncio
    from modules.employees.models import EmployeeSettings
    token = None
    if d.employee_id:
        s = db.query(EmployeeSettings).filter(EmployeeSettings.employee_id == d.employee_id).first()
        token = ((s.config or {}) if s else {}).get("fcm_token")
    if not token:
        raise _NoDevice()
    from core.push import send_fcm_push
    ok = asyncio.run(send_fcm_push(token, d.subject or "", d.body or "", debug=False))
    if not ok:
        raise RuntimeError("The push service did not accept the message.")


class _NoDevice(Exception):
    pass


def queue(db: Session, company_id: int, channel: str, to_address: str | None, subject: str, body: str, *,
          category: str = "general", source: str = "notification", employee_id: int | None = None,
          notification_id: int | None = None, send_now: bool = True) -> NotificationDelivery:
    d = NotificationDelivery(company_id=company_id, channel=channel, to_address=(to_address or "")[:320] or None,
                             subject=(subject or "")[:250], body=body, category=category, source=source,
                             employee_id=employee_id, recipient_key=recipient_key(employee_id) if employee_id or
                             source == "notification" else None, notification_id=notification_id,
                             status="queued", attempts=0)
    db.add(d)
    db.flush()
    if send_now:
        try:
            attempt(db, d)
        except _NoDevice:
            d.status, d.last_error = "skipped", "No phone is linked for push messages."
    return d


def send_email(db: Session, company_id: int, to: list[str] | str, subject: str, body: str, *,
               category: str = "automation", source: str = "automation", employee_id: int | None = None) -> int:
    """Email one or more addresses now, with a delivery log row per address (failures are
    retried later). Returns how many were queued."""
    if isinstance(to, str):
        to = [to]
    addresses = [a for a in dict.fromkeys((x or "").strip() for x in to) if a and "@" in a]
    for addr in addresses[:50]:
        queue(db, company_id, "email", addr, subject, body, category=category, source=source, employee_id=employee_id)
    return len(addresses[:50])


def retry_due(db: Session, company_id: int | None = None, limit: int = 200) -> int:
    """Try again the deliveries whose retry time has come."""
    q = db.query(NotificationDelivery).filter(NotificationDelivery.status == "queued",
                                              NotificationDelivery.next_attempt_at.isnot(None),
                                              NotificationDelivery.next_attempt_at <= _now())
    if company_id is not None:
        q = q.filter(NotificationDelivery.company_id == company_id)
    n = 0
    for d in q.order_by(NotificationDelivery.next_attempt_at).limit(limit).all():
        try:
            attempt(db, d)
        except _NoDevice:
            d.status, d.last_error = "skipped", "No phone is linked for push messages."
        n += 1
    db.flush()
    return n


def retry_now(db: Session, d: NotificationDelivery) -> NotificationDelivery:
    """Someone pressed Retry on a failed delivery."""
    d.attempts = 0 if d.status == "failed" else d.attempts
    try:
        attempt(db, d)
    except _NoDevice:
        d.status, d.last_error = "skipped", "No phone is linked for push messages."
    db.flush()
    return d
