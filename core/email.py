"""Outgoing email. SMTP when configured; in development without SMTP the message is
logged so the flow can still be exercised. Never silently succeeds in production."""
import logging
import smtplib
from email.message import EmailMessage

from core.config import get_settings

logger = logging.getLogger(__name__)


def email_configured() -> bool:
    s = get_settings()
    return bool(s.SMTP_HOST and s.SMTP_FROM)


def send_email(to: str, subject: str, body: str) -> bool:
    """Returns True when the message was handed to an SMTP server."""
    s = get_settings()
    if not email_configured():
        if s.APP_ENV == "development":
            logger.warning("[email not configured] To: %s | %s\n%s", to, subject, body)
            return False
        logger.error("SMTP is not configured; cannot send email to %s", to)
        return False
    msg = EmailMessage()
    msg["From"] = s.SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(s.SMTP_HOST, s.SMTP_PORT, timeout=15) as smtp:
            if s.SMTP_TLS:
                smtp.starttls()
            if s.SMTP_USER:
                smtp.login(s.SMTP_USER, s.SMTP_PASSWORD)
            smtp.send_message(msg)
        return True
    except Exception as e:  # pragma: no cover
        logger.error("Email to %s failed: %s", to, e)
        return False
