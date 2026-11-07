"""Employee documents: type (document_type master), number, issue and expiry dates, an
uploaded file, verification (who, when, remarks) and expiry reminders. Documents are
archived, never deleted."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session, joinedload

from core import context
from core.errors import BadRequest, NotFound
from core.masters import labels_for, validate_value_ids
from modules.employees.models import Employee, EmployeeDocument, StoredFile

MAX_FILE_BYTES = 5 * 1024 * 1024
ALLOWED_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/webp", "image/heic",
                 "application/msword", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
STATUSES = {"pending": "Waiting for check", "verified": "Verified", "rejected": "Rejected"}


def _today():
    from modules.employees.service import today
    return today()


def store_file(db: Session, company_id: int, employee_id: int, filename: str, content_type: str, raw: bytes) -> StoredFile:
    if not raw:
        raise BadRequest("The file is empty.")
    if len(raw) > MAX_FILE_BYTES:
        raise BadRequest("Files must be under 5 MB.")
    ctype = (content_type or "").lower()
    if ctype not in ALLOWED_TYPES:
        raise BadRequest("Upload a PDF, an image (JPG, PNG) or a Word file.")
    f = StoredFile(company_id=company_id, employee_id=employee_id, filename=(filename or "document")[:255],
                   content_type=ctype, size=len(raw), data=raw)
    db.add(f)
    db.flush()
    return f


def get(db: Session, company_id: int, employee_id: int, uuid) -> EmployeeDocument:
    row = (db.query(EmployeeDocument).options(joinedload(EmployeeDocument.file))
           .filter(EmployeeDocument.company_id == company_id, EmployeeDocument.employee_id == employee_id,
                   EmployeeDocument.uuid == uuid).first())
    if row is None:
        raise NotFound("Document")
    return row


def save(db: Session, company_id: int, employee_id: int, data: dict, row: EmployeeDocument | None = None) -> EmployeeDocument:
    data = {k: (val.strip() or None) if isinstance(val, str) else val for k, val in data.items()}
    if row is None and not data.get("document_type_id"):
        raise BadRequest("Pick the document type.")
    if data.get("document_type_id"):
        validate_value_ids(db, company_id, "document_type", [int(data["document_type_id"])])
    issue = data.get("issue_date", row.issue_date if row else None)
    expiry = data.get("expiry_date", row.expiry_date if row else None)
    if issue and expiry and expiry <= issue:
        raise BadRequest("The expiry date should be after the issue date.")
    if issue and issue > _today():
        raise BadRequest("The issue date can't be in the future.")
    if row is None:
        row = EmployeeDocument(company_id=company_id, employee_id=employee_id)
        db.add(row)
    elif any(k in data and data[k] != getattr(row, k) for k in ("document_number", "expiry_date", "issue_date",
                                                                 "document_type_id")):
        _reset_verification(row)
    for key in ("document_type_id", "title", "document_number", "issue_date", "expiry_date", "issued_by",
                "remind_days_before", "visible_to_employee", "remarks"):
        if key not in data or (data[key] is None and key in ("remind_days_before", "visible_to_employee")):
            continue
        setattr(row, key, data[key])
    if "expiry_date" in data:
        row.reminder_sent_at = None
    db.flush()
    return row


def _reset_verification(row: EmployeeDocument) -> None:
    if row.verification_status != "pending":
        row.verification_status = "pending"
        row.verified_by_name = row.verified_by_employee_id = row.verified_at = None


def attach_file(db: Session, row: EmployeeDocument, file: StoredFile) -> None:
    row.file_id = file.id
    _reset_verification(row)


def verify(db: Session, row: EmployeeDocument, status: str, remarks: str | None) -> EmployeeDocument:
    if status not in STATUSES:
        raise BadRequest("Mark the document verified, rejected or waiting.")
    if status == "rejected" and not (remarks or "").strip():
        raise BadRequest("Say why the document is rejected, so the employee can fix it.")
    if status == "verified" and row.file_id is None and not row.document_number:
        raise BadRequest("There is nothing to verify yet: upload the file or enter the number first.")
    ctx = context.current()
    row.verification_status = status
    row.remarks = (remarks or "").strip() or row.remarks
    if status == "pending":
        row.verified_by_name = row.verified_by_employee_id = row.verified_at = None
    else:
        row.verified_by_name = ctx.actor_name
        row.verified_by_employee_id = ctx.employee_id
        row.verified_at = datetime.now(timezone.utc)
    return row


def archive(row: EmployeeDocument) -> None:
    if row.archived_at is not None:
        raise BadRequest("This document is already archived.")
    row.archived_at = datetime.now(timezone.utc)


def expiry_state(row: EmployeeDocument) -> str | None:
    if not row.expiry_date:
        return None
    days = (row.expiry_date - _today()).days
    if days < 0:
        return "expired"
    if days <= (row.remind_days_before or 30):
        return "expiring"
    return "valid"


def out(db: Session, company_id: int, rows: list[EmployeeDocument], employees: dict | None = None) -> list[dict]:
    labels = labels_for(db, company_id, [r.document_type_id for r in rows if r.document_type_id])
    from core.masters import MasterValue
    parents = dict(db.query(MasterValue.id, MasterValue.parent_id).filter(
        MasterValue.id.in_([r.document_type_id for r in rows if r.document_type_id] or [0])).all())
    parent_labels = labels_for(db, company_id, [p for p in parents.values() if p])
    result = []
    for r in rows:
        d = {
            "uuid": str(r.uuid), "document_type_id": r.document_type_id,
            "document_type_name": labels.get(r.document_type_id),
            "category_name": parent_labels.get(parents.get(r.document_type_id)),
            "title": r.title, "document_number": r.document_number, "issue_date": r.issue_date,
            "expiry_date": r.expiry_date, "issued_by": r.issued_by, "verification_status": r.verification_status,
            "verification_label": STATUSES.get(r.verification_status), "verified_by_name": r.verified_by_name,
            "verified_at": r.verified_at, "remarks": r.remarks, "remind_days_before": r.remind_days_before,
            "visible_to_employee": r.visible_to_employee, "archived_at": r.archived_at,
            "expiry_state": expiry_state(r),
            "days_to_expiry": (r.expiry_date - _today()).days if r.expiry_date else None,
            "file": {"uuid": str(r.file.uuid), "filename": r.file.filename, "content_type": r.file.content_type,
                     "size": r.file.size} if r.file_id and r.file else None,
            "created_at": r.created_at, "updated_at": r.updated_at,
        }
        if employees is not None:
            d["employee"] = employees.get(r.employee_id)
        result.append(d)
    return result


def company_query(db: Session, company_id: int):
    return (db.query(EmployeeDocument).options(joinedload(EmployeeDocument.file))
            .join(Employee, Employee.id == EmployeeDocument.employee_id)
            .filter(EmployeeDocument.company_id == company_id, EmployeeDocument.archived_at.is_(None)))


def send_expiry_reminders(db: Session, company_id: int) -> int:
    """Notify the employee once per document when it is within its reminder window.
    Runs at start-up and whenever the documents list is opened. A failure here never
    blocks the caller (it runs in a savepoint)."""
    import logging
    try:
        with db.begin_nested():
            return _send_expiry_reminders(db, company_id)
    except Exception as exc:  # notifications are best effort
        logging.getLogger("hrms").warning("Document expiry reminders failed: %s", exc)
        return 0


def _send_expiry_reminders(db: Session, company_id: int) -> int:
    from modules.notifications.models import Notification
    horizon = _today() + timedelta(days=365)
    rows = (db.query(EmployeeDocument).filter(
        EmployeeDocument.company_id == company_id, EmployeeDocument.archived_at.is_(None),
        EmployeeDocument.expiry_date.isnot(None), EmployeeDocument.expiry_date <= horizon,
        EmployeeDocument.reminder_sent_at.is_(None)).all())
    sent = 0
    for r in rows:
        days = (r.expiry_date - _today()).days
        if days > (r.remind_days_before or 30):
            continue
        emp = db.query(Employee).filter(Employee.id == r.employee_id).first()
        if emp is None or emp.status in ("exited", "archived"):
            continue
        dtype = labels_for(db, company_id, [r.document_type_id]).get(r.document_type_id, "A document")
        when = "has expired" if days < 0 else f"expires in {days} day{'s' if days != 1 else ''}"
        db.add(Notification(company_id=company_id, employee_id=emp.id, channel="in_app", message_type="document_expiry",
                            message_content=f"{dtype} of {emp.first_name} {emp.last_name} {when} ({r.expiry_date:%d %b %Y}).",
                            status="sent", sent_at=datetime.now(timezone.utc)))
        r.reminder_sent_at = datetime.now(timezone.utc)
        sent += 1
    return sent
