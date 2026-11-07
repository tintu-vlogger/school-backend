"""Emails with a calendar invite (.ics). Uses the same SMTP settings as core.email;
without SMTP in development the message is logged."""
from __future__ import annotations

import logging
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage

from core.config import get_settings
from core.email import email_configured, send_email

logger = logging.getLogger("hrms")


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _esc(s: str | None) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def ics(*, uid: str, start: datetime, end: datetime, summary: str, description: str = "", location: str = "",
        organizer: str = "", attendees: list[tuple[str, str]] | None = None, sequence: int = 0,
        cancel: bool = False) -> str:
    """An iCalendar invite (RFC 5545) that Google, Outlook and Apple calendars accept."""
    lines = ["BEGIN:VCALENDAR", "PRODID:-//Campusly//Interviews//EN", "VERSION:2.0", "CALSCALE:GREGORIAN",
             f"METHOD:{'CANCEL' if cancel else 'REQUEST'}", "BEGIN:VEVENT", f"UID:{uid}",
             f"DTSTAMP:{_fmt(datetime.now(timezone.utc))}", f"DTSTART:{_fmt(start)}", f"DTEND:{_fmt(end)}",
             f"SEQUENCE:{sequence}", f"SUMMARY:{_esc(summary)}", f"DESCRIPTION:{_esc(description)}",
             f"LOCATION:{_esc(location)}", f"STATUS:{'CANCELLED' if cancel else 'CONFIRMED'}"]
    if organizer:
        lines.append(f"ORGANIZER:mailto:{organizer}")
    for name, email in attendees or []:
        if email:
            lines.append(f"ATTENDEE;CN={_esc(name)};ROLE=REQ-PARTICIPANT;RSVP=TRUE:mailto:{email}")
    if not cancel:
        lines += ["BEGIN:VALARM", "TRIGGER:-PT30M", "ACTION:DISPLAY", f"DESCRIPTION:{_esc(summary)}", "END:VALARM"]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


def send_with_invite(to: str, subject: str, body: str, ics_text: str) -> bool:
    if not to:
        return False
    s = get_settings()
    if not email_configured():
        return send_email(to, subject, body + "\n\n[calendar invite attached]")
    msg = EmailMessage()
    msg["From"] = s.SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    msg.add_attachment(ics_text.encode("utf-8"), maintype="text", subtype="calendar", filename="invite.ics",
                       params={"method": "REQUEST"})
    try:
        with smtplib.SMTP(s.SMTP_HOST, s.SMTP_PORT, timeout=15) as smtp:
            if s.SMTP_TLS:
                smtp.starttls()
            if s.SMTP_USER:
                smtp.login(s.SMTP_USER, s.SMTP_PASSWORD)
            smtp.send_message(msg)
        return True
    except Exception as e:  # pragma: no cover
        logger.error("Invite to %s failed: %s", to, e)
        return False
