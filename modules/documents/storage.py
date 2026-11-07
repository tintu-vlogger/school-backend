"""File bytes: store, read and hand out short-lived signed links.

Bytes live in `stored_files` (Postgres, behind row-level security). A signed link carries
the company, the file and an expiry, signed with the app secret, so a browser can open a
PDF in a frame or download an attachment without sending the sign-in token. Swap this
file for object storage later without touching callers.
"""
from __future__ import annotations

import hashlib
import hmac
import mimetypes
import time
from uuid import UUID

from sqlalchemy.orm import Session, undefer

from core import context
from core.config import get_settings
from core.errors import BadRequest, NotFound
from modules.documents.models import FileBlob

MAX_BYTES = 10 * 1024 * 1024
BLOCKED_EXTENSIONS = {"exe", "bat", "cmd", "com", "msi", "dll", "scr", "jar", "apk", "sh", "ps1", "vbs", "js", "app",
                      "dmg", "iso", "bin", "cpl", "reg"}
LINK_SECONDS = 60 * 60


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def clean_name(name: str) -> str:
    name = (name or "file").replace("\\", "/").split("/")[-1].strip()
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '<>:"|?*')
    return (name or "file")[:200]


def store(db: Session, company_id: int, filename: str, data: bytes, content_type: str | None = None,
          purpose: str | None = None) -> FileBlob:
    """Check and save bytes. Raises BadRequest with a plain message when refused."""
    filename = clean_name(filename)
    if not data:
        raise BadRequest(f"{filename} is empty.")
    if len(data) > MAX_BYTES:
        raise BadRequest(f"{filename} is larger than {MAX_BYTES // (1024 * 1024)} MB.")
    if _ext(filename) in BLOCKED_EXTENSIONS or data[:2] == b"MZ":
        raise BadRequest(f"{filename} looks like a program. Programs can't be uploaded.")
    if data[:5] == b"%PDF-" and b"/Encrypt" in data[:4096]:
        raise BadRequest(f"{filename} is password protected. Upload a copy without a password.")
    ctype = (content_type or "").split(";")[0].strip() or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    row = FileBlob(company_id=company_id, filename=filename, content_type=ctype[:120], size=len(data),
                     sha256=hashlib.sha256(data).hexdigest(), data=data, purpose=purpose,
                     created_by_employee_id=context.current().employee_id)
    db.add(row)
    db.flush()
    return row


def get(db: Session, company_id: int, uuid, with_data: bool = False) -> FileBlob:
    q = db.query(FileBlob).filter(FileBlob.company_id == company_id, FileBlob.uuid == uuid)
    if with_data:
        q = q.options(undefer(FileBlob.data))
    row = q.first()
    if row is None:
        raise NotFound("File")
    return row


def by_id(db: Session, company_id: int, file_id: int, with_data: bool = False) -> FileBlob | None:
    q = db.query(FileBlob).filter(FileBlob.company_id == company_id, FileBlob.id == file_id)
    if with_data:
        q = q.options(undefer(FileBlob.data))
    return q.first()


def _sig(company_id: int, uuid: str, exp: int) -> str:
    key = get_settings().SECRET_KEY.encode()
    return hmac.new(key, f"{company_id}:{uuid}:{exp}".encode(), hashlib.sha256).hexdigest()[:40]


def signed_url(row: FileBlob, download: bool = False, seconds: int = LINK_SECONDS) -> str:
    exp = int(time.time()) + seconds
    sig = _sig(row.company_id, str(row.uuid), exp)
    return f"/api/v1/documents/blob/{row.company_id}/{row.uuid}?exp={exp}&sig={sig}" + ("&dl=1" if download else "")


def check_signature(company_id: int, uuid: UUID, exp: int, sig: str) -> bool:
    if exp < int(time.time()):
        return False
    return hmac.compare_digest(_sig(company_id, str(uuid), exp), sig or "")


def describe(row: FileBlob | None, download: bool = False) -> dict | None:
    if row is None:
        return None
    return {"uuid": str(row.uuid), "name": row.filename, "size": row.size, "content_type": row.content_type,
            "url": signed_url(row), "download_url": signed_url(row, download=True)}


def attachments_from(db: Session, company_id: int, uuids: list | None, limit: int = 10) -> list[dict]:
    """Turn uploaded file ids into the JSON kept on a record: [{uuid, name, size, content_type}]."""
    out = []
    for raw in (uuids or [])[:limit]:
        try:
            row = get(db, company_id, UUID(str(raw)))
        except (ValueError, NotFound):
            raise BadRequest("An attachment could not be found. Upload it again.")
        out.append({"uuid": str(row.uuid), "name": row.filename, "size": row.size, "content_type": row.content_type})
    return out


def with_links(db: Session, company_id: int, items: list[dict] | None) -> list[dict]:
    """Attachment JSON with fresh signed links for the reader."""
    if not items:
        return []
    ids = [i.get("uuid") for i in items if i.get("uuid")]
    rows = {str(r.uuid): r for r in db.query(FileBlob).filter(FileBlob.company_id == company_id,
                                                                FileBlob.uuid.in_(ids)).all()}
    out = []
    for i in items:
        row = rows.get(i.get("uuid"))
        if row is not None:
            out.append(describe(row))
    return out
