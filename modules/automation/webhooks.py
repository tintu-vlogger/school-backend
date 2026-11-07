"""Outgoing webhooks: signed JSON POSTs with retries and a delivery log.

Each call carries
    X-Hrms-Event:      what happened (e.g. automation.rule)
    X-Hrms-Timestamp:  seconds since 1970 when it was sent
    X-Hrms-Signature:  sha256=<hex HMAC-SHA256 of the raw body, keyed with the webhook's secret>
    X-Hrms-Delivery:   the delivery id (the same on retries, so receivers can ignore repeats)

A call that fails (no answer, or a status other than 2xx) is tried again after 1, 5, 25
and 120 minutes; after 5 attempts it is marked failed. In production, addresses on
private, loopback or link-local networks are refused.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from core.config import get_settings
from core.errors import BadRequest
from modules.automation.models import AutomationWebhook, AutomationWebhookDelivery

MAX_ATTEMPTS = 5
BACKOFF_MINUTES = [1, 5, 25, 120]
TIMEOUT_SECONDS = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_secret() -> str:
    return "whsec_" + secrets.token_hex(20)


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def check_url(url: str) -> str:
    url = (url or "").strip()
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise BadRequest("Enter a full web address starting with https://")
    if get_settings().is_production:
        if p.scheme != "https":
            raise BadRequest("Use an https:// address.")
        try:
            infos = socket.getaddrinfo(p.hostname, p.port or 443)
        except socket.gaierror:
            raise BadRequest("That address could not be found.")
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise BadRequest("Webhooks can't call addresses inside a private network.")
    return url


def clean_headers(headers: dict | None) -> dict:
    out = {}
    for k, v in (headers or {}).items():
        k = str(k).strip()
        if not k:
            continue
        if not all(c.isalnum() or c in "-_" for c in k) or len(k) > 60:
            raise BadRequest(f"“{k}” is not a valid header name.")
        if k.lower().startswith("x-hrms-") or k.lower() in ("content-type", "content-length", "host"):
            raise BadRequest(f"The header {k} is set by the system.")
        out[k] = str(v)[:500]
    if len(out) > 10:
        raise BadRequest("Add up to 10 headers.")
    return out


def body_of(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str).encode()


def attempt(db: Session, d: AutomationWebhookDelivery, hook: AutomationWebhook | None = None) -> AutomationWebhookDelivery:
    hook = hook or db.query(AutomationWebhook).filter(AutomationWebhook.id == d.webhook_id).first()
    d.attempts = (d.attempts or 0) + 1
    try:
        if hook is None or not hook.active or hook.archived_at is not None:
            raise RuntimeError("The webhook is switched off.")
        check_url(hook.url)
        body = body_of(d.payload or {})
        headers = {**(hook.headers or {}), "Content-Type": "application/json", "User-Agent": "NextGenHRMS-Webhook/1",
                   "X-Hrms-Event": d.event, "X-Hrms-Timestamp": str(int(time.time())),
                   "X-Hrms-Delivery": str(d.uuid), "X-Hrms-Signature": sign(hook.secret, body)}
        r = httpx.post(hook.url, content=body, headers=headers, timeout=TIMEOUT_SECONDS, follow_redirects=False)
        d.response_status = r.status_code
        d.response_body = (r.text or "")[:500]
        if not 200 <= r.status_code < 300:
            raise RuntimeError(f"The receiver answered {r.status_code}.")
        d.status, d.sent_at, d.error, d.next_attempt_at = "sent", _now(), None, None
    except Exception as exc:
        d.error = str(exc)[:500] or exc.__class__.__name__
        if d.attempts >= MAX_ATTEMPTS:
            d.status, d.next_attempt_at = "failed", None
        else:
            d.status = "queued"
            d.next_attempt_at = _now() + timedelta(minutes=BACKOFF_MINUTES[min(d.attempts - 1, len(BACKOFF_MINUTES) - 1)])
    db.flush()
    return d


def send(db: Session, hook: AutomationWebhook, event: str, payload: dict, rule_id: int | None = None,
         send_now: bool = True) -> AutomationWebhookDelivery:
    d = AutomationWebhookDelivery(company_id=hook.company_id, webhook_id=hook.id, rule_id=rule_id, event=event,
                                  payload=json.loads(body_of(payload)), status="queued", attempts=0)
    db.add(d)
    db.flush()
    db.refresh(d, ["uuid"])
    if send_now:
        attempt(db, d, hook)
    return d


def retry_due(db: Session, company_id: int | None = None, limit: int = 200) -> int:
    q = db.query(AutomationWebhookDelivery).filter(AutomationWebhookDelivery.status == "queued",
                                                   AutomationWebhookDelivery.next_attempt_at.isnot(None),
                                                   AutomationWebhookDelivery.next_attempt_at <= _now())
    if company_id is not None:
        q = q.filter(AutomationWebhookDelivery.company_id == company_id)
    n = 0
    for d in q.order_by(AutomationWebhookDelivery.next_attempt_at).limit(limit).all():
        attempt(db, d)
        n += 1
    return n


def retry_now(db: Session, d: AutomationWebhookDelivery) -> AutomationWebhookDelivery:
    if d.status == "failed":
        d.attempts = 0
    return attempt(db, d)
