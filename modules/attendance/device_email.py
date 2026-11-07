"""Email attachments: many attendance programs can email a daily punch report. We check a
mailbox over IMAP on a schedule and import CSV / Excel attachments with the device's
column mapping (the same mapping as a file upload).

Settings (source_config, connection "email"): host, port (993), ssl, username, password,
folder (INBOX), from_filter, subject_filter, file_pattern (*.csv), lookback_days,
every_minutes. Messages already read are remembered by their id.
"""
from __future__ import annotations

import email
import fnmatch
import imaplib
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header

from core.errors import BadRequest
from modules.attendance.device_common import check_public_host, store_secrets

SECRET_KEYS = ("password",)


def clean(cfg: dict, old: dict | None) -> dict:
    host = (cfg.get("host") or "").strip()
    if not host:
        raise BadRequest("Enter the mail server (IMAP) address, e.g. imap.gmail.com.")
    if not (cfg.get("username") or "").strip():
        raise BadRequest("Enter the mailbox user name.")
    out = {"host": host[:255], "port": int(cfg.get("port") or 993), "ssl": cfg.get("ssl") is not False,
           "username": cfg["username"].strip()[:255], "folder": (cfg.get("folder") or "INBOX").strip()[:200],
           "from_filter": (cfg.get("from_filter") or "").strip()[:255],
           "subject_filter": (cfg.get("subject_filter") or "").strip()[:255],
           "file_pattern": (cfg.get("file_pattern") or "*.csv").strip()[:100],
           "lookback_days": max(1, min(int(cfg.get("lookback_days") or 7), 60)),
           "every_minutes": max(5, min(int(cfg.get("every_minutes") or 60), 1440))}
    return store_secrets(out, SECRET_KEYS, cfg, old)


def _quoted(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _name(part) -> str:
    raw = part.get_filename() or ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def messages(cfg: dict, seen: set[str], limit: int = 30):
    """Yield (message id, file name, bytes) for matching attachments of new messages."""
    check_public_host(cfg["host"])
    box = imaplib.IMAP4_SSL(cfg["host"], int(cfg.get("port") or 993), timeout=30) if cfg.get("ssl", True) \
        else imaplib.IMAP4(cfg["host"], int(cfg.get("port") or 143), timeout=30)
    try:
        try:
            box.login(cfg["username"], cfg.get("password") or "")
        except imaplib.IMAP4.error:
            raise BadRequest("The mail server refused the sign-in. For Gmail or Outlook use an app password.")
        typ, _ = box.select(_quoted(cfg.get("folder") or "INBOX"), readonly=True)
        if typ != "OK":
            raise BadRequest(f"There's no folder called {cfg.get('folder')}.")
        since = (datetime.now(timezone.utc) - timedelta(days=int(cfg.get("lookback_days") or 7))).strftime("%d-%b-%Y")
        crit = ["SINCE", since]
        if cfg.get("from_filter"):
            crit += ["FROM", _quoted(cfg["from_filter"])]
        if cfg.get("subject_filter"):
            crit += ["SUBJECT", _quoted(cfg["subject_filter"])]
        typ, data = box.uid("search", None, *crit)
        uids = [u.decode() for u in (data[0] or b"").split()] if typ == "OK" else []
        new = [u for u in uids if u not in seen][:limit]
        for uid in new:
            typ, msg_data = box.uid("fetch", uid, "(BODY.PEEK[])")
            if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            found = False
            for part in msg.walk():
                name = _name(part)
                if name and fnmatch.fnmatch(name.lower(), (cfg.get("file_pattern") or "*").lower()):
                    payload = part.get_payload(decode=True) or b""
                    found = True
                    yield uid, name, payload
            if not found:
                yield uid, None, b""
    finally:
        try:
            box.logout()
        except Exception:
            pass
