"""Delivery of notices beyond the app: email, SMS and WhatsApp.

- Email goes through the platform's delivery log (`notifications.channels.queue`): the
  institution's own SMTP server or the platform's, retried on failure. Small batches are
  tried at once, bigger ones are queued for the background sender.
- SMS and WhatsApp use the connections saved in Settings > Integrations (`assistant.integrations`).
  When a connection is not set up the channel is skipped and the notice says so. In
  development messages are written to the log instead of being sent.
Every attempt is written to `notification_deliveries` (source "comms") so the office can see
what went out.
"""
from __future__ import annotations

import json
import logging
import urllib.request

from sqlalchemy.orm import Session

from core.config import get_settings as platform_settings

logger = logging.getLogger("hrms")

SEND_NOW_LIMIT = 30


def channel_status(db: Session, company_id: int) -> dict:
    """Which channels can be used right now (for the notice form)."""
    out = {"in_app": {"ready": True, "label": "In the app"}}
    out["email"] = {"ready": True, "label": "Email"}
    for kind, label in (("sms", "SMS"), ("whatsapp", "WhatsApp")):
        cfg = _integration(db, company_id, kind)
        out[kind] = {"ready": cfg is not None, "label": label,
                     "hint": None if cfg else f"{label} is not set up (Settings > Integrations)."}
    return out


def _integration(db: Session, company_id: int, kind: str) -> dict | None:
    try:
        from modules.assistant import integrations
        return integrations.get_config(db, company_id, kind)
    except Exception:
        return None


def send_email(db: Session, company_id: int, targets: list[tuple[str, str | None]], subject: str, body: str) -> dict:
    """targets: [(email, recipient key)]. Returns {"sent": n, "skipped": n}."""
    from datetime import datetime, timezone
    from modules.notifications import channels
    addresses = [a for a in dict.fromkeys((t[0] or "").strip().lower() for t in targets) if a and "@" in a]
    skipped = len(targets) - len(addresses)
    now_ = len(addresses) <= SEND_NOW_LIMIT
    for addr in addresses:
        try:
            d = channels.queue(db, company_id, "email", addr, subject, body, category="general", source="comms",
                               send_now=now_)
            if not now_:
                d.next_attempt_at = datetime.now(timezone.utc)
        except Exception as exc:  # delivery must never break publishing
            logger.warning("Notice email to %s failed: %s", addr, exc)
    return {"sent": len(addresses), "skipped": skipped}


def send_text(db: Session, company_id: int, kind: str, targets: list[tuple[str | None, str | None]], text: str) -> dict:
    """SMS or WhatsApp to phone numbers. targets: [(phone, recipient key)]."""
    from modules.notifications.models import NotificationDelivery
    cfg = _integration(db, company_id, kind)
    phones = [p for p in dict.fromkeys(_phone(t[0]) for t in targets) if p]
    skipped = len(targets) - len(phones)
    if cfg is None:
        return {"sent": 0, "skipped": len(targets), "note": f"{'SMS' if kind == 'sms' else 'WhatsApp'} is not set up."}
    sent = failed = 0
    dev = not platform_settings().is_production
    for phone in phones:
        status, error = "sent", None
        try:
            if dev:
                logger.warning("[%s not sent in development] To: %s | %s", kind, phone, text[:300])
                status = "logged"
            else:
                _gateway(cfg, kind, phone, text)
        except Exception as exc:
            status, error = "failed", str(exc)[:500]
        db.add(NotificationDelivery(company_id=company_id, channel=kind, to_address=phone, subject=text[:250],
                                    body=text, category="general", source="comms", status=status, attempts=1,
                                    last_error=error))
        if status == "failed":
            failed += 1
        else:
            sent += 1
    db.flush()
    return {"sent": sent, "skipped": skipped, "failed": failed}


def _phone(v: str | None) -> str | None:
    if not v:
        return None
    digits = "".join(c for c in v if c.isdigit() or c == "+")
    if digits.startswith("+"):
        return digits if len(digits) >= 11 else None
    digits = digits.lstrip("0")
    if len(digits) == 10:
        return "+91" + digits
    return "+" + digits if len(digits) >= 11 else None


def _gateway(cfg: dict, kind: str, phone: str, text: str) -> None:
    """Hand one message to the configured gateway."""
    provider = (cfg.get("provider") or "").lower()
    if kind == "whatsapp" and provider.startswith("meta") and cfg.get("phone_number_id") and cfg.get("access_token"):
        req = urllib.request.Request(
            f"https://graph.facebook.com/v19.0/{cfg['phone_number_id']}/messages",
            data=json.dumps({"messaging_product": "whatsapp", "to": phone.lstrip("+"), "type": "text",
                             "text": {"body": text[:4000]}}).encode(),
            headers={"Authorization": f"Bearer {cfg['access_token']}", "Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=10) as res:  # noqa: S310 (fixed https host)
            if res.status >= 300:
                raise RuntimeError(f"WhatsApp refused the message ({res.status}).")
        return
    if kind == "sms" and provider == "msg91" and cfg.get("api_key") and cfg.get("sender_id"):
        req = urllib.request.Request(
            "https://control.msg91.com/api/v5/flow/",
            data=json.dumps({"sender": cfg["sender_id"], "short_url": "0", "mobiles": phone.lstrip("+"),
                             "message": text[:600]}).encode(),
            headers={"authkey": cfg["api_key"], "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as res:  # noqa: S310 (fixed https host)
            if res.status >= 300:
                raise RuntimeError(f"The SMS gateway refused the message ({res.status}).")
        return
    raise RuntimeError(f"Sending through {cfg.get('provider') or 'this gateway'} is not available yet.")
