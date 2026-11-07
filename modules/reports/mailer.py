"""Email with attachments for scheduled reports. Uses the company's own SMTP server when
it is set up under Integrations → Email, otherwise the platform's SMTP settings."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from sqlalchemy.orm import Session

from core.config import get_settings

logger = logging.getLogger("hrms")


def _smtp(db: Session, company_id: int) -> dict | None:
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


def send(db: Session, company_id: int, to: list[str], subject: str, body: str,
         attachments: list[tuple[str, bytes, str]] | None = None) -> tuple[bool, str]:
    """Returns (sent, message)."""
    to = [t for t in dict.fromkeys(x.strip() for x in to if x and "@" in x)]
    if not to:
        return False, "No one to send to."
    cfg = _smtp(db, company_id)
    if not cfg:
        logger.warning("[email not configured] To: %s | %s (%s attachment(s))", ", ".join(to), subject,
                       len(attachments or []))
        return False, "Email is not set up yet (Settings → Integrations → Email)."
    msg = EmailMessage()
    msg["From"] = cfg.get("from_name") and f"{cfg['from_name']} <{cfg['from_email']}>" or cfg["from_email"]
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    for name, data, ctype in attachments or []:
        main, _, sub = ctype.split(";")[0].partition("/")
        msg.add_attachment(data, maintype=main, subtype=sub or "octet-stream", filename=name)
    try:
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
        return True, f"Sent to {len(to)} recipient(s)."
    except Exception as exc:
        logger.error("Report email failed: %s", exc)
        return False, f"The email could not be sent: {str(exc)[:200]}"
