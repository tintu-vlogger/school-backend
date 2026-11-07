"""Company files and policies, letter templates, generated letters and e-signatures.

Functions other modules may call:
    store_file(db, company_id, filename, data, content_type, purpose) -> FileBlob
    file_link(db, company_id, file_id_or_row, download=False) -> dict (name, size, signed url)
    attachments_from(db, company_id, uuids) / attachments_with_links(db, company_id, items)
    file_into_employee_documents(db, company_id, employee_id, blob, title, ...) -> DocFile
    generate_letter(db, company_id, template, employee, values, ...) -> Letter
    policy_ack_summary(db, company_id, posh_only=False) -> dict
    pending_acknowledgements(db, company_id, employee_id) -> int
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, text as sql_text
from sqlalchemy.orm import Session, joinedload, undefer

from core import audit, context
from core.config import get_settings
from core.errors import BadRequest, Forbidden, NotFound
from core.timeutil import today_for_company_id
from modules.documents import merge, pdf, storage
from modules.documents.models import (DocFile, DocFileVersion, DocFolder, EsignRequest, EsignSigner, FileBlob, Letter,
                                      Letterhead, LetterTemplate, Policy, PolicyAcknowledgement, PolicyVersion)

logger = logging.getLogger("hrms")

AREAS = {"company": "Company files", "department": "Department files", "hr": "Employee files (HR only)",
         "personal": "Personal locker"}
LETTER_TYPES = {
    "offer": "Offer letter", "appointment": "Appointment letter", "confirmation": "Confirmation letter",
    "increment": "Increment letter", "promotion": "Promotion letter", "transfer": "Transfer letter",
    "experience": "Experience letter", "relieving": "Relieving letter", "address_proof": "Address proof",
    "salary_certificate": "Salary certificate", "bonafide": "Bonafide letter", "warning": "Warning letter",
    "show_cause": "Show-cause notice", "termination": "Termination letter", "other": "Other letter",
}
SYSTEM_FOLDERS = [
    ("company", "Handbooks and guides", "Employee handbook, holiday list and how-to guides"),
    ("company", "Forms", "Forms employees can download"),
    ("hr", "Letters", "Letters generated for employees"),
    ("hr", "Signed documents", "Documents signed electronically"),
    ("hr", "Employee records", "Confidential employee files"),
]
ESIGN_PROVIDERS = {"in_app_otp": "Signature in the app with a one-time code by email",
                   "aadhaar": "Aadhaar eSign (needs an eSign service provider)"}
OTP_MINUTES = 10
OTP_MAX_ATTEMPTS = 5


# ── Small helpers ───────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def actor_name() -> str:
    return context.current().actor_name or "System"


def _employee_model():
    from modules.employees.models import Employee
    return Employee


def employee(db: Session, company_id: int, employee_id: int | None):
    if not employee_id:
        return None
    E = _employee_model()
    return db.query(E).filter(E.company_id == company_id, E.id == employee_id).first()


def display_name(emp) -> str | None:
    return f"{emp.first_name} {emp.last_name}".strip() if emp else None


def names_for(db: Session, company_id: int, ids) -> dict[int, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    E = _employee_model()
    rows = db.query(E.id, E.first_name, E.last_name).filter(E.company_id == company_id, E.id.in_(ids)).all()
    return {r.id: f"{r.first_name} {r.last_name}".strip() for r in rows}


def notify(db: Session, company_id: int, employee_id: int | None, message: str, kind: str = "documents") -> None:
    if not employee_id:
        return
    from modules.notifications.models import Notification
    db.add(Notification(company_id=company_id, employee_id=employee_id, channel="in_app", message_type=kind,
                        message_content=message[:1000], status="sent", sent_at=_now()))


def has_perm(db: Session, user: dict, perm: str) -> bool:
    from core.security import _get_role_permissions
    return perm in _get_role_permissions(user["company_id"], user["role"], db)


def _today(db: Session, company_id: int) -> date:
    return today_for_company_id(db, company_id)


def _safe_rows(db: Session, sql: str, params: dict) -> list:
    """Read from a table owned by another module that may not exist yet (organisation)."""
    try:
        with db.begin_nested():
            return db.execute(sql_text(sql), params).mappings().all()
    except Exception:
        return []


# ── Files: storage for every module ─────────────────────────────────

def store_file(db: Session, company_id: int, filename: str, data: bytes, content_type: str | None = None,
               purpose: str | None = None) -> FileBlob:
    return storage.store(db, company_id, filename, data, content_type, purpose)


def file_link(db: Session, company_id: int, blob: FileBlob | int | None, download: bool = False) -> dict | None:
    if isinstance(blob, int):
        blob = storage.by_id(db, company_id, blob)
    return storage.describe(blob, download) if blob is not None else None


def attachments_from(db: Session, company_id: int, uuids: list | None, limit: int = 10) -> list[dict]:
    return storage.attachments_from(db, company_id, uuids, limit)


def attachments_with_links(db: Session, company_id: int, items: list[dict] | None) -> list[dict]:
    return storage.with_links(db, company_id, items)


# ── Folders and files ───────────────────────────────────────────────

def ensure_system_folders(db: Session, company_id: int) -> None:
    have = {(f.area, f.name) for f in db.query(DocFolder).filter(DocFolder.company_id == company_id,
                                                                  DocFolder.is_system.is_(True)).all()}
    for i, (area, name, desc) in enumerate(SYSTEM_FOLDERS):
        if (area, name) not in have:
            db.add(DocFolder(company_id=company_id, area=area, name=name, description=desc, is_system=True, sort_order=i))
    db.flush()


def system_folder(db: Session, company_id: int, area: str, name: str) -> DocFolder:
    row = db.query(DocFolder).filter(DocFolder.company_id == company_id, DocFolder.area == area,
                                     DocFolder.name == name, DocFolder.is_system.is_(True)).first()
    if row is None:
        ensure_system_folders(db, company_id)
        row = db.query(DocFolder).filter(DocFolder.company_id == company_id, DocFolder.area == area,
                                         DocFolder.name == name).first()
    return row


def personal_folder(db: Session, company_id: int, employee_id: int) -> DocFolder:
    row = db.query(DocFolder).filter(DocFolder.company_id == company_id, DocFolder.area == "personal",
                                     DocFolder.owner_employee_id == employee_id).first()
    if row is None:
        row = DocFolder(company_id=company_id, area="personal", name="My locker", owner_employee_id=employee_id,
                        is_system=True)
        db.add(row)
        db.flush()
    return row


def _me(db: Session, user: dict):
    return employee(db, user["company_id"], user.get("employee_id"))


def _audience_match(audience: dict | None, emp) -> bool:
    a = audience or {"all": True}
    if a.get("all", False) and not any(a.get(k) for k in ("department_ids", "roles", "employee_ids")):
        return True
    if emp is None:
        return False
    return (emp.department_id in (a.get("department_ids") or []) or emp.role in (a.get("roles") or [])
            or emp.id in (a.get("employee_ids") or []))


def clean_audience(db: Session, company_id: int, audience: dict | None) -> dict:
    a = audience or {}
    dept_ids = [int(x) for x in (a.get("department_ids") or []) if str(x).isdigit()]
    emp_ids = [int(x) for x in (a.get("employee_ids") or []) if str(x).isdigit()]
    roles = [str(r) for r in (a.get("roles") or []) if r in ("admin", "hr", "manager", "employee")]
    if not dept_ids and not emp_ids and not roles:
        return {"all": True}
    if emp_ids:
        E = _employee_model()
        found = {i for (i,) in db.query(E.id).filter(E.company_id == company_id, E.id.in_(emp_ids)).all()}
        emp_ids = [i for i in emp_ids if i in found]
    return {"all": False, "department_ids": dept_ids, "roles": roles, "employee_ids": emp_ids}


def audience_employees(db: Session, company_id: int, audience: dict | None) -> list:
    E = _employee_model()
    q = db.query(E).filter(E.company_id == company_id, E.active.is_(True))
    a = audience or {"all": True}
    if not (a.get("all", False) and not any(a.get(k) for k in ("department_ids", "roles", "employee_ids"))):
        conds = []
        if a.get("department_ids"):
            conds.append(E.department_id.in_(a["department_ids"]))
        if a.get("roles"):
            conds.append(E.role.in_(a["roles"]))
        if a.get("employee_ids"):
            conds.append(E.id.in_(a["employee_ids"]))
        if not conds:
            return []
        q = q.filter(or_(*conds))
    return q.all()


def _is_dept_head(db: Session, company_id: int, emp, department_id: int | None) -> bool:
    if emp is None or not department_id:
        return False
    from modules.departments.models import Department
    d = db.query(Department).filter(Department.id == department_id).first()
    return bool(d and d.head_employee_id == emp.id)


def can_see_folder(db: Session, user: dict, folder: DocFolder, manager: bool) -> bool:
    me = _me(db, user)
    if folder.area == "personal":
        return me is not None and folder.owner_employee_id == me.id
    if manager:
        return True
    if folder.area == "company":
        return True
    if folder.area == "department":
        return me is not None and (me.department_id == folder.department_id
                                   or _is_dept_head(db, user["company_id"], me, folder.department_id))
    return False  # hr: employees only see their own files through "My documents"


def can_add_to_folder(db: Session, user: dict, folder: DocFolder, manager: bool) -> bool:
    me = _me(db, user)
    if folder.area == "personal":
        return me is not None and folder.owner_employee_id == me.id
    if manager:
        return True
    return folder.area == "department" and _is_dept_head(db, user["company_id"], me, folder.department_id)


def can_see_file(db: Session, user: dict, f: DocFile, manager: bool) -> bool:
    me = _me(db, user)
    folder = f.folder
    if folder.area == "personal":
        return me is not None and folder.owner_employee_id == me.id
    if manager:
        return True
    if folder.area == "hr":
        return me is not None and f.employee_id == me.id and f.visible_to_employee
    if not can_see_folder(db, user, folder, False):
        return False
    return _audience_match(f.audience, me)


def list_folders(db: Session, user: dict, manager: bool, area: str | None = None) -> list[DocFolder]:
    cid = user["company_id"]
    me = _me(db, user)
    q = db.query(DocFolder).filter(DocFolder.company_id == cid, DocFolder.archived_at.is_(None))
    if area:
        q = q.filter(DocFolder.area == area)
    rows = q.order_by(DocFolder.area, DocFolder.sort_order, DocFolder.name).all()
    out = []
    for f in rows:
        if f.area == "personal":
            if me is not None and f.owner_employee_id == me.id:
                out.append(f)
            continue
        if can_see_folder(db, user, f, manager):
            out.append(f)
    if me is not None and (area in (None, "personal")) and not any(f.area == "personal" for f in out):
        out.append(personal_folder(db, cid, me.id))
    return out


def get_folder(db: Session, company_id: int, uuid) -> DocFolder:
    row = db.query(DocFolder).filter(DocFolder.company_id == company_id, DocFolder.uuid == uuid).first()
    if row is None:
        raise NotFound("Folder")
    return row


def save_folder(db: Session, user: dict, data: dict, row: DocFolder | None = None) -> DocFolder:
    cid = user["company_id"]
    name = (data.get("name") or "").strip()
    if not name or "/" in name:
        raise BadRequest("Give the folder a name without a slash.")
    if row is not None and row.is_system:
        raise BadRequest("Built-in folders can't be renamed.")
    area = data.get("area") or (row.area if row else "company")
    if area not in ("company", "department", "hr"):
        raise BadRequest("Pick where the folder lives: company, department or employee files.")
    department_id = data.get("department_id") if area == "department" else None
    if area == "department" and not department_id:
        raise BadRequest("Pick the department this folder is for.")
    if row is None:
        row = DocFolder(company_id=cid, area=area)
        db.add(row)
    row.name, row.area, row.department_id = name[:150], area, department_id
    row.description = (data.get("description") or "").strip()[:300] or None
    db.flush()
    return row


def archive_folder(db: Session, folder: DocFolder) -> None:
    if folder.is_system:
        raise BadRequest("Built-in folders can't be removed.")
    live = db.query(func.count(DocFile.id)).filter(DocFile.folder_id == folder.id, DocFile.archived_at.is_(None)).scalar()
    if live:
        raise BadRequest("Only empty folders can be removed. Move or archive its files first.")
    folder.archived_at = _now()


def get_file(db: Session, company_id: int, uuid) -> DocFile:
    row = (db.query(DocFile).options(joinedload(DocFile.folder), joinedload(DocFile.stored))
           .filter(DocFile.company_id == company_id, DocFile.uuid == uuid).first())
    if row is None:
        raise NotFound("File")
    return row


def files_query(db: Session, user: dict, manager: bool, *, folder: DocFolder | None = None, q: str | None = None,
                employee_id: int | None = None, archived: bool = False, expiring_days: int | None = None):
    cid = user["company_id"]
    query = (db.query(DocFile).options(joinedload(DocFile.folder), joinedload(DocFile.stored))
             .join(DocFolder, DocFolder.id == DocFile.folder_id).filter(DocFile.company_id == cid))
    query = query.filter(DocFile.archived_at.isnot(None) if archived else DocFile.archived_at.is_(None))
    if folder is not None:
        query = query.filter(DocFile.folder_id == folder.id)
    else:
        query = query.filter(DocFolder.area != "personal")
    if employee_id:
        query = query.filter(DocFile.employee_id == employee_id)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(DocFile.title.ilike(like), DocFile.description.ilike(like)))
    if expiring_days is not None:
        today = _today(db, cid)
        query = query.filter(DocFile.expires_on.isnot(None), DocFile.expires_on <= today + timedelta(days=expiring_days))
    return query.order_by(DocFile.created_at.desc())


def add_file(db: Session, user: dict, folder: DocFolder, blob: FileBlob, data: dict, manager: bool) -> DocFile:
    cid = user["company_id"]
    if not can_add_to_folder(db, user, folder, manager):
        raise Forbidden("You can't add files to this folder.")
    me = _me(db, user)
    if folder.area == "personal":
        from modules.documents import settings as doc_settings
        limit = doc_settings.get(db, cid).get("personal_file_limit", 50)
        count = db.query(func.count(DocFile.id)).filter(DocFile.folder_id == folder.id, DocFile.archived_at.is_(None)).scalar()
        if count >= limit:
            raise BadRequest(f"Your locker holds up to {limit} files. Remove one to add another.")
    employee_id = data.get("employee_id") if folder.area == "hr" else (me.id if folder.area == "personal" else None)
    if folder.area == "hr" and not employee_id:
        raise BadRequest("Pick the employee this file is about.")
    if employee_id and employee(db, cid, employee_id) is None:
        raise BadRequest("The employee was not found.")
    title = (data.get("title") or blob.filename).strip()[:250]
    row = DocFile(company_id=cid, folder_id=folder.id, title=title, description=(data.get("description") or None),
                  stored_file_id=blob.id, version=1, employee_id=employee_id,
                  audience=clean_audience(db, cid, data.get("audience")) if folder.area in ("company", "department") else {"all": True},
                  visible_to_employee=bool(data.get("visible_to_employee", True)),
                  allow_download=bool(data.get("allow_download", True)), expires_on=data.get("expires_on"),
                  tags=[str(t)[:40] for t in (data.get("tags") or [])][:10], source=data.get("source") or "upload",
                  uploaded_by_employee_id=me.id if me else None, uploaded_by_name=actor_name())
    db.add(row)
    db.flush()
    db.add(DocFileVersion(company_id=cid, doc_file_id=row.id, version=1, stored_file_id=blob.id, note="First upload",
                          created_by_name=actor_name()))
    if folder.area == "hr" and employee_id and row.visible_to_employee:
        notify(db, cid, employee_id, f"A new document was added to your files: {title}.")
    return row


def update_file(db: Session, user: dict, f: DocFile, data: dict) -> DocFile:
    cid = user["company_id"]
    for k in ("title", "description", "expires_on", "visible_to_employee", "allow_download"):
        if k in data:
            setattr(f, k, data[k])
    if "audience" in data and f.folder.area in ("company", "department"):
        f.audience = clean_audience(db, cid, data["audience"])
    if "tags" in data:
        f.tags = [str(t)[:40] for t in (data["tags"] or [])][:10]
    if "folder_uuid" in data and data["folder_uuid"]:
        target = get_folder(db, cid, data["folder_uuid"])
        if target.area != f.folder.area:
            raise BadRequest("Files can only move between folders of the same kind.")
        f.folder_id = target.id
    if "expires_on" in data:
        f.expiry_reminded_on = None
    return f


def new_version(db: Session, f: DocFile, blob: FileBlob, note: str | None) -> DocFile:
    f.version += 1
    f.stored_file_id = blob.id
    db.add(DocFileVersion(company_id=f.company_id, doc_file_id=f.id, version=f.version, stored_file_id=blob.id,
                          note=(note or "")[:300] or None, created_by_name=actor_name()))
    db.flush()
    return f


def restore_version(db: Session, f: DocFile, version: int) -> DocFile:
    v = db.query(DocFileVersion).filter(DocFileVersion.doc_file_id == f.id, DocFileVersion.version == version).first()
    if v is None:
        raise NotFound("Version")
    blob = storage.by_id(db, f.company_id, v.stored_file_id)
    return new_version(db, f, blob, f"Restored version {version}")


def file_into_employee_documents(db: Session, company_id: int, employee_id: int, blob: FileBlob, title: str,
                                 folder_name: str = "Letters", source: str = "letter",
                                 visible_to_employee: bool = True) -> DocFile:
    """File a generated or signed document under the employee's HR files."""
    folder = system_folder(db, company_id, "hr", folder_name)
    row = DocFile(company_id=company_id, folder_id=folder.id, title=title[:250], stored_file_id=blob.id,
                  employee_id=employee_id, visible_to_employee=visible_to_employee, source=source,
                  uploaded_by_name=actor_name(), audience={"all": True})
    db.add(row)
    db.flush()
    db.add(DocFileVersion(company_id=company_id, doc_file_id=row.id, version=1, stored_file_id=blob.id,
                          note="Generated", created_by_name=actor_name()))
    return row


def locker_usage(db: Session, company_id: int) -> list[dict]:
    """Admins see only how many files and how much space each locker uses, never the files."""
    rows = (db.query(DocFolder.owner_employee_id, func.count(DocFile.id), func.coalesce(func.sum(FileBlob.size), 0))
            .join(DocFile, and_(DocFile.folder_id == DocFolder.id, DocFile.archived_at.is_(None)))
            .join(FileBlob, FileBlob.id == DocFile.stored_file_id)
            .filter(DocFolder.company_id == company_id, DocFolder.area == "personal")
            .group_by(DocFolder.owner_employee_id).all())
    names = names_for(db, company_id, [r[0] for r in rows])
    return [{"employee_id": r[0], "employee_name": names.get(r[0]), "files": r[1], "bytes": int(r[2])} for r in rows]


# ── Policies and acknowledgements ───────────────────────────────────

def get_policy(db: Session, company_id: int, uuid) -> Policy:
    row = (db.query(Policy).options(joinedload(Policy.current_version))
           .filter(Policy.company_id == company_id, Policy.uuid == uuid).first())
    if row is None:
        raise NotFound("Policy")
    return row


def save_policy(db: Session, company_id: int, data: dict, row: Policy | None = None) -> Policy:
    title = (data.get("title") or (row.title if row else "")).strip()
    if not title:
        raise BadRequest("Give the policy a title.")
    if row is None:
        row = Policy(company_id=company_id, title=title)
        db.add(row)
    row.title = title[:200]
    for k in ("category_id", "description", "requires_ack", "is_posh", "review_on", "owner_name", "active"):
        if k in data:
            setattr(row, k, data[k])
    if "ack_due_days" in data and data["ack_due_days"] is not None:
        if not 1 <= int(data["ack_due_days"]) <= 365:
            raise BadRequest("The acknowledgement deadline must be between 1 and 365 days.")
        row.ack_due_days = int(data["ack_due_days"])
    if "reminder_every_days" in data and data["reminder_every_days"] is not None:
        row.reminder_every_days = max(1, min(int(data["reminder_every_days"]), 30))
    if "audience" in data:
        row.audience = clean_audience(db, company_id, data["audience"])
    if "review_on" in data:
        row.review_reminded_on = None
    db.flush()
    return row


def publish_version(db: Session, company_id: int, policy: Policy, *, blob: FileBlob | None, body_html: str | None,
                    effective_from: date | None, change_note: str | None, ask_again: bool = True) -> PolicyVersion:
    """A new version becomes current. When acknowledgement is required, everyone in the
    audience is asked to acknowledge it by the deadline."""
    if blob is None and not (body_html or "").strip():
        raise BadRequest("Attach the policy document or write its text.")
    today = _today(db, company_id)
    last = db.query(func.max(PolicyVersion.version)).filter(PolicyVersion.policy_id == policy.id).scalar() or 0
    v = PolicyVersion(company_id=company_id, policy_id=policy.id, version=last + 1,
                      stored_file_id=blob.id if blob else None, body_html=pdf.sanitize(body_html) if body_html else None,
                      change_note=(change_note or "")[:300] or None, effective_from=effective_from or today,
                      ack_due_on=(today + timedelta(days=policy.ack_due_days)) if policy.requires_ack else None,
                      published_by_name=actor_name())
    db.add(v)
    db.flush()
    previous = policy.current_version_id
    policy.current_version_id = v.id
    db.flush()
    if policy.requires_ack and policy.active:
        if ask_again or previous is None:
            assign_acknowledgements(db, company_id, policy, v)
        else:  # minor change: carry earlier acknowledgements over
            for old in db.query(PolicyAcknowledgement).filter(PolicyAcknowledgement.version_id == previous).all():
                db.add(PolicyAcknowledgement(company_id=company_id, policy_id=policy.id, version_id=v.id,
                                             employee_id=old.employee_id, due_on=v.ack_due_on,
                                             acknowledged_at=old.acknowledged_at, ip_address=old.ip_address))
            db.flush()
            assign_acknowledgements(db, company_id, policy, v)
    audit.event(db, "policy.published", "policies", policy.id, {"version": v.version, "title": policy.title},
                entity_uuid=str(policy.uuid), entity_label=policy.title)
    return v


def assign_acknowledgements(db: Session, company_id: int, policy: Policy, version: PolicyVersion,
                            employees: list | None = None) -> int:
    if not policy.requires_ack:
        return 0
    targets = employees if employees is not None else audience_employees(db, company_id, policy.audience)
    have = {e for (e,) in db.query(PolicyAcknowledgement.employee_id)
            .filter(PolicyAcknowledgement.version_id == version.id).all()}
    added = 0
    for emp in targets:
        if emp.id in have or not _audience_match(policy.audience, emp):
            continue
        db.add(PolicyAcknowledgement(company_id=company_id, policy_id=policy.id, version_id=version.id,
                                     employee_id=emp.id, due_on=version.ack_due_on))
        notify(db, company_id, emp.id, f"Please read and acknowledge the {policy.title}"
               + (f" by {merge.format_date(version.ack_due_on)}." if version.ack_due_on else "."), "policy")
        added += 1
    db.flush()
    return added


def ensure_my_acknowledgements(db: Session, company_id: int, emp) -> None:
    """People who joined after a policy was published are asked to acknowledge it too."""
    if emp is None:
        return
    for p in db.query(Policy).filter(Policy.company_id == company_id, Policy.active.is_(True),
                                     Policy.requires_ack.is_(True), Policy.current_version_id.isnot(None)).all():
        if not _audience_match(p.audience, emp):
            continue
        exists = db.query(PolicyAcknowledgement.id).filter(PolicyAcknowledgement.version_id == p.current_version_id,
                                                           PolicyAcknowledgement.employee_id == emp.id).first()
        if not exists:
            v = db.get(PolicyVersion, p.current_version_id)
            due = max(v.ack_due_on or _today(db, company_id), _today(db, company_id) + timedelta(days=p.ack_due_days)) \
                if p.requires_ack else None
            db.add(PolicyAcknowledgement(company_id=company_id, policy_id=p.id, version_id=v.id, employee_id=emp.id,
                                         due_on=due))
    db.flush()


def my_policies(db: Session, user: dict) -> list[dict]:
    cid = user["company_id"]
    me = _me(db, user)
    ensure_my_acknowledgements(db, cid, me)
    today = _today(db, cid)
    rows = (db.query(Policy).options(joinedload(Policy.current_version))
            .filter(Policy.company_id == cid, Policy.active.is_(True), Policy.current_version_id.isnot(None))
            .order_by(Policy.title).all())
    acks = {}
    if me is not None:
        acks = {a.version_id: a for a in db.query(PolicyAcknowledgement)
                .filter(PolicyAcknowledgement.company_id == cid, PolicyAcknowledgement.employee_id == me.id).all()}
    out = []
    for p in rows:
        if me is not None and not _audience_match(p.audience, me):
            continue
        a = acks.get(p.current_version_id)
        status = "not_needed"
        if p.requires_ack and a is not None:
            status = "acknowledged" if a.acknowledged_at else ("overdue" if a.due_on and a.due_on < today else "pending")
        out.append({"policy": p, "ack": a, "status": status})
    return out


def acknowledge(db: Session, user: dict, policy: Policy) -> PolicyAcknowledgement:
    cid = user["company_id"]
    me = _me(db, user)
    if me is None:
        raise BadRequest("Only employees acknowledge policies.")
    ensure_my_acknowledgements(db, cid, me)
    a = db.query(PolicyAcknowledgement).filter(PolicyAcknowledgement.version_id == policy.current_version_id,
                                               PolicyAcknowledgement.employee_id == me.id).first()
    if a is None:
        raise BadRequest("This policy doesn't need your acknowledgement.")
    if a.acknowledged_at is None:
        a.acknowledged_at = _now()
        a.ip_address = context.current().ip_address
        audit.event(db, "policy.acknowledged", "policies", policy.id,
                    {"version_id": policy.current_version_id, "employee": display_name(me)},
                    entity_uuid=str(policy.uuid), entity_label=policy.title)
    return a


def ack_counts(db: Session, company_id: int, version_ids: list[int]) -> dict[int, dict]:
    if not version_ids:
        return {}
    today = _today(db, company_id)
    rows = (db.query(PolicyAcknowledgement.version_id,
                     func.count(PolicyAcknowledgement.id),
                     func.count(PolicyAcknowledgement.acknowledged_at),
                     func.count(PolicyAcknowledgement.id).filter(and_(PolicyAcknowledgement.acknowledged_at.is_(None),
                                                                      PolicyAcknowledgement.due_on < today)))
            .filter(PolicyAcknowledgement.company_id == company_id, PolicyAcknowledgement.version_id.in_(version_ids))
            .group_by(PolicyAcknowledgement.version_id).all())
    return {r[0]: {"assigned": r[1], "acknowledged": r[2], "pending": r[1] - r[2], "overdue": r[3]} for r in rows}


def ack_report(db: Session, company_id: int, policy: Policy, status: str | None = None) -> list[dict]:
    if not policy.current_version_id:
        return []
    today = _today(db, company_id)
    E = _employee_model()
    q = (db.query(PolicyAcknowledgement, E).join(E, E.id == PolicyAcknowledgement.employee_id)
         .filter(PolicyAcknowledgement.company_id == company_id,
                 PolicyAcknowledgement.version_id == policy.current_version_id))
    out = []
    for a, e in q.order_by(E.first_name, E.last_name).all():
        st = "acknowledged" if a.acknowledged_at else ("overdue" if a.due_on and a.due_on < today else "pending")
        if status and st != status:
            continue
        out.append({"employee_id": e.id, "employee_uuid": str(e.uuid), "employee_name": display_name(e),
                    "employee_code": e.employee_code, "department_id": e.department_id, "status": st,
                    "due_on": a.due_on, "acknowledged_at": a.acknowledged_at, "reminder_count": a.reminder_count,
                    "ip_address": a.ip_address})
    return out


def remind_pending(db: Session, company_id: int, policy: Policy) -> int:
    today = _today(db, company_id)
    n = 0
    for a in db.query(PolicyAcknowledgement).filter(PolicyAcknowledgement.version_id == policy.current_version_id,
                                                    PolicyAcknowledgement.acknowledged_at.is_(None)).all():
        notify(db, company_id, a.employee_id, f"Reminder: please acknowledge the {policy.title}.", "policy")
        a.reminder_count += 1
        a.last_reminded_on = today
        n += 1
    return n


def policy_ack_summary(db: Session, company_id: int, posh_only: bool = False) -> dict:
    q = db.query(Policy).filter(Policy.company_id == company_id, Policy.active.is_(True),
                                Policy.current_version_id.isnot(None), Policy.requires_ack.is_(True))
    if posh_only:
        q = q.filter(Policy.is_posh.is_(True))
    policies = q.all()
    counts = ack_counts(db, company_id, [p.current_version_id for p in policies])
    items = [{"uuid": str(p.uuid), "title": p.title, **counts.get(p.current_version_id,
             {"assigned": 0, "acknowledged": 0, "pending": 0, "overdue": 0})} for p in policies]
    return {"policies": items, "assigned": sum(i["assigned"] for i in items),
            "acknowledged": sum(i["acknowledged"] for i in items), "overdue": sum(i["overdue"] for i in items)}


def pending_acknowledgements(db: Session, company_id: int, employee_id: int) -> int:
    return (db.query(func.count(PolicyAcknowledgement.id))
            .join(Policy, Policy.id == PolicyAcknowledgement.policy_id)
            .filter(PolicyAcknowledgement.company_id == company_id, PolicyAcknowledgement.employee_id == employee_id,
                    PolicyAcknowledgement.acknowledged_at.is_(None), Policy.current_version_id == PolicyAcknowledgement.version_id,
                    Policy.active.is_(True)).scalar() or 0)


# ── Letterheads and templates ───────────────────────────────────────

def get_letterhead(db: Session, company_id: int, uuid) -> Letterhead:
    row = db.query(Letterhead).filter(Letterhead.company_id == company_id, Letterhead.uuid == uuid).first()
    if row is None:
        raise NotFound("Header or footer")
    return row


def letterhead_by_id(db: Session, company_id: int, rid: int | None) -> Letterhead | None:
    if not rid:
        return None
    return db.query(Letterhead).filter(Letterhead.company_id == company_id, Letterhead.id == rid).first()


def default_letterhead(db: Session, company_id: int, kind: str) -> Letterhead | None:
    return (db.query(Letterhead).filter(Letterhead.company_id == company_id, Letterhead.kind == kind,
                                        Letterhead.active.is_(True))
            .order_by(Letterhead.is_default.desc(), Letterhead.id).first())


def save_letterhead(db: Session, company_id: int, data: dict, row: Letterhead | None = None) -> Letterhead:
    name = (data.get("name") or (row.name if row else "")).strip()
    if not name:
        raise BadRequest("Give the design a name.")
    kind = data.get("kind") or (row.kind if row else "header")
    if kind not in ("header", "footer"):
        raise BadRequest("A design is a header or a footer.")
    html = data.get("html", row.html if row else "")
    if len(html or "") > 3_000_000:
        raise BadRequest("The design is too large. Use smaller images.")
    if row is None:
        row = Letterhead(company_id=company_id, kind=kind, name=name)
        db.add(row)
    row.name, row.kind, row.html = name[:120], kind, pdf.sanitize(html or "")
    if data.get("height_mm") is not None:
        row.height_mm = max(5, min(int(data["height_mm"]), 120))
    if "active" in data:
        row.active = bool(data["active"])
    if data.get("is_default"):
        db.query(Letterhead).filter(Letterhead.company_id == company_id, Letterhead.kind == kind,
                                    Letterhead.id != (row.id or 0)).update({"is_default": False})
        row.is_default = True
    elif "is_default" in data:
        row.is_default = False
    db.flush()
    return row


def get_template(db: Session, company_id: int, uuid) -> LetterTemplate:
    row = (db.query(LetterTemplate).options(joinedload(LetterTemplate.header), joinedload(LetterTemplate.footer))
           .filter(LetterTemplate.company_id == company_id, LetterTemplate.uuid == uuid).first())
    if row is None:
        raise NotFound("Template")
    return row


def _clean_signers(db: Session, company_id: int, signers: list | None) -> list[dict]:
    out = []
    for s in (signers or [])[:5]:
        kind = s.get("kind")
        if kind not in ("employee", "manager", "specific"):
            raise BadRequest("A signer is the employee, their manager or a specific person.")
        item = {"kind": kind, "label": (s.get("label") or "").strip()[:120] or None}
        if kind == "specific":
            emp = employee(db, company_id, s.get("employee_id"))
            if emp is None:
                raise BadRequest("Pick the person who signs.")
            item["employee_id"] = emp.id
        out.append(item)
    return out


def _clean_extra_fields(fields: list | None) -> list[dict]:
    out = []
    for f in (fields or [])[:30]:
        key = (f.get("key") or "").strip()
        if not merge.FIELD_RE.fullmatch("{{" + key + "}}"):
            raise BadRequest(f"“{key}” is not a valid field name.")
        out.append({"key": key, "label": (f.get("label") or key)[:100],
                    "type": f.get("type") if f.get("type") in ("text", "money", "date", "number", "textarea") else "text",
                    "required": bool(f.get("required"))})
    return out


def save_template(db: Session, company_id: int, data: dict, row: LetterTemplate | None = None) -> LetterTemplate:
    name = (data.get("name") or (row.name if row else "")).strip()
    if not name:
        raise BadRequest("Give the template a name.")
    letter_type = data.get("letter_type") or (row.letter_type if row else None)
    if letter_type not in LETTER_TYPES:
        raise BadRequest("Pick the kind of letter.")
    if row is None:
        row = LetterTemplate(company_id=company_id, name=name, letter_type=letter_type)
        db.add(row)
    elif any(k in data and data[k] != getattr(row, k) for k in ("body_html", "header_id", "footer_id", "subject")):
        row.version += 1
    row.name, row.letter_type = name[:150], letter_type
    for k in ("description", "subject", "self_service", "esign_required", "signing_order", "active", "font_family"):
        if k in data and data[k] is not None:
            setattr(row, k, data[k])
    if "body_html" in data:
        row.body_html = pdf.sanitize(data["body_html"] or "")
    for k in ("header_id", "footer_id"):
        if k in data:
            lh = letterhead_by_id(db, company_id, data[k])
            if data[k] and (lh is None or lh.kind != k.split("_")[0]):
                raise BadRequest(f"Pick a {k.split('_')[0]} design.")
            setattr(row, k, lh.id if lh else None)
    if data.get("page_size") is not None:
        if data["page_size"] not in pdf.PAGE_SIZES_MM:
            raise BadRequest("Pick a page size: A4, A5, Letter or Legal.")
        row.page_size = data["page_size"]
    if data.get("orientation") is not None:
        row.orientation = "landscape" if data["orientation"] == "landscape" else "portrait"
    for k in ("margin_top", "margin_right", "margin_bottom", "margin_left"):
        if data.get(k) is not None:
            v = int(data[k])
            if not 5 <= v <= 60:
                raise BadRequest("Margins must be between 5 and 60 mm.")
            setattr(row, k, v)
    if data.get("font_size") is not None:
        row.font_size = max(8, min(int(data["font_size"]), 16))
    if "signers" in data:
        row.signers = _clean_signers(db, company_id, data["signers"])
    if "extra_fields" in data:
        row.extra_fields = _clean_extra_fields(data["extra_fields"])
    if row.esign_required and not row.signers:
        row.signers = [{"kind": "employee", "label": "Employee"}]
    if data.get("is_default"):
        db.query(LetterTemplate).filter(LetterTemplate.company_id == company_id,
                                        LetterTemplate.letter_type == letter_type,
                                        LetterTemplate.id != (row.id or 0)).update({"is_default": False})
        row.is_default = True
    elif "is_default" in data:
        row.is_default = False
    db.flush()
    return row


def default_template(db: Session, company_id: int, letter_type: str) -> LetterTemplate | None:
    return (db.query(LetterTemplate).filter(LetterTemplate.company_id == company_id,
                                            LetterTemplate.letter_type == letter_type, LetterTemplate.active.is_(True))
            .order_by(LetterTemplate.is_default.desc(), LetterTemplate.id).first())


# ── Merge values ────────────────────────────────────────────────────

def _fmt_addr(*parts) -> str:
    return ", ".join(str(p).strip() for p in parts if p and str(p).strip())


def _address(value) -> str | None:
    if isinstance(value, dict):
        return _fmt_addr(value.get("line1"), value.get("line2"), value.get("city"), value.get("state"),
                         value.get("pincode") or value.get("pin"))
    return value or None


def _tenure(start: date | None, end: date) -> str | None:
    if not start:
        return None
    months = (end.year - start.year) * 12 + end.month - start.month - (1 if end.day < start.day else 0)
    years, months = divmod(max(months, 0), 12)
    parts = []
    if years:
        parts.append(f"{years} year{'s' if years != 1 else ''}")
    if months or not years:
        parts.append(f"{months} month{'s' if months != 1 else ''}")
    return " ".join(parts)


def company_values(db: Session, company_id: int) -> dict:
    from modules.company.models import Company
    c = db.query(Company).filter(Company.id == company_id).first()
    if c is None:
        return {}
    v = {"company.name": c.company_name, "company.address": _fmt_addr(c.address, c.city, c.state, c.pincode),
         "company.city": c.city, "company.state": c.state, "company.pincode": c.pincode, "company.email": c.email,
         "company.phone": c.phone,
         "legal_entity.name": c.company_name, "legal_entity.address": _fmt_addr(c.address, c.city, c.state, c.pincode),
         "legal_entity.pan": c.pan, "legal_entity.tan": c.tan, "legal_entity.cin": c.cin, "legal_entity.gstin": c.gstin}
    if c.logo_url and (c.logo_url.startswith("data:image/") or c.logo_url.startswith("https://")):
        v["company.logo"] = f'<img src="{c.logo_url}" style="max-height:18mm;max-width:60mm" alt="">'
    return {k: val for k, val in v.items() if val}


def employee_values(db: Session, company_id: int, emp, today: date) -> dict:
    if emp is None:
        return {}
    g = lambda name: getattr(emp, name, None)  # noqa: E731 - columns added by the people module may be missing
    custom = g("custom") or {}
    v = {
        "employee.full_name": display_name(emp), "employee.first_name": emp.first_name,
        "employee.last_name": emp.last_name, "employee.title": emp.greeting, "employee.employee_code": emp.employee_code,
        "employee.email": emp.email, "employee.phone": emp.phone,
        "employee.address": _address(g("current_address")) or _address(custom.get("address")),
        "employee.date_of_birth": g("date_of_birth"), "employee.pan": g("pan"),
        "job.designation": emp.designation.designation_name if getattr(emp, "designation", None) else None,
        "job.department": emp.department.department_name if getattr(emp, "department", None) else None,
        "job.manager": display_name(emp.reporting_manager) if getattr(emp, "reporting_manager", None) else None,
        "job.joining_date": g("date_of_joining") or custom.get("date_of_joining"),
        "job.confirmation_date": g("confirmation_date"), "job.notice_period_days": g("notice_period_days"),
        "job.last_working_day": g("exit_date"),
        "job.tenure": _tenure(g("date_of_joining"), g("exit_date") or today),
    }
    if g("employment_type_id"):
        from core.masters import labels_for
        v["job.employment_type"] = labels_for(db, company_id, [g("employment_type_id")]).get(g("employment_type_id"))
    if g("location_id"):
        rows = _safe_rows(db, "SELECT name FROM locations WHERE company_id = :c AND id = :i",
                          {"c": company_id, "i": g("location_id")})
        if rows:
            v["job.location"] = rows[0]["name"]
    if g("legal_entity_id"):
        rows = _safe_rows(db, "SELECT name, legal_name, pan, tan, cin, gstin, address_line1, address_line2, city, pincode "
                              "FROM legal_entities WHERE company_id = :c AND id = :i",
                          {"c": company_id, "i": g("legal_entity_id")})
        if rows:
            r = rows[0]
            v.update({"legal_entity.name": r["legal_name"] or r["name"], "legal_entity.pan": r["pan"],
                      "legal_entity.tan": r["tan"], "legal_entity.cin": r["cin"], "legal_entity.gstin": r["gstin"],
                      "legal_entity.address": _fmt_addr(r["address_line1"], r["address_line2"], r["city"], r["pincode"])})
    return {k: val for k, val in v.items() if val not in (None, "")}


def build_values(db: Session, company_id: int, emp, extra: dict | None = None, reference_no: str | None = None) -> dict:
    today = _today(db, company_id)
    values = {"letter.date": today.isoformat()}
    from modules.documents import settings as doc_settings
    conf = doc_settings.get(db, company_id)
    if conf.get("signatory_name"):
        values["letter.signatory_name"] = conf["signatory_name"]
    if conf.get("signatory_designation"):
        values["letter.signatory_designation"] = conf["signatory_designation"]
    values.update(company_values(db, company_id))
    values.update({k: v for k, v in employee_values(db, company_id, emp, today).items() if v not in (None, "")})
    for provider in merge.providers():
        try:
            values.update({k: v for k, v in (provider(db, company_id, emp) or {}).items() if v not in (None, "")})
        except Exception as exc:  # a broken provider never blocks a letter
            logger.warning("Merge provider failed: %s", exc)
    if reference_no:
        values["letter.reference_no"] = reference_no
    for k, v in (extra or {}).items():
        if v not in (None, "") and merge.FIELD_RE.fullmatch("{{" + str(k) + "}}"):
            values[str(k)] = v
    if values.get("salary.ctc") and not values.get("salary.ctc_monthly"):
        try:
            from decimal import Decimal
            values["salary.ctc_monthly"] = str((Decimal(str(values["salary.ctc"]).replace(",", "")) / 12).quantize(Decimal("1")))
        except Exception:
            pass
    return values


def _layout_kwargs(t: LetterTemplate) -> dict:
    return {"page_size": t.page_size, "orientation": t.orientation,
            "margins": (t.margin_top, t.margin_right, t.margin_bottom, t.margin_left),
            "font_family": t.font_family, "font_size": t.font_size}


def render_parts(db: Session, company_id: int, template: LetterTemplate, values: dict,
                 header: Letterhead | None, footer: Letterhead | None) -> dict:
    body, missing = merge.render(template.body_html, values)
    hd, m1 = merge.render(header.html, values) if header else ("", [])
    ft, m2 = merge.render(footer.html.replace("{{page.number}}", "\x00PN\x00").replace("{{page.total}}", "\x00PT\x00"),
                          values) if footer else ("", [])
    ft = ft.replace("\x00PN\x00", "{{page.number}}").replace("\x00PT\x00", "{{page.total}}")
    subject, _ = merge.render(template.subject or LETTER_TYPES.get(template.letter_type, template.name), values, escape=False)
    return {"body": body, "header": hd or None, "footer": ft or None, "subject": subject.strip() or template.name,
            "missing": sorted(set(missing) | set(m1) | set(m2)),
            "header_mm": header.height_mm if header else 0, "footer_mm": footer.height_mm if footer else 0}


def pick_letterheads(db: Session, company_id: int, template: LetterTemplate, header_id=None, footer_id=None,
                     use_header: bool = True, use_footer: bool = True):
    header = letterhead_by_id(db, company_id, header_id) if header_id else (
        template.header or default_letterhead(db, company_id, "header"))
    footer = letterhead_by_id(db, company_id, footer_id) if footer_id else (
        template.footer or default_letterhead(db, company_id, "footer"))
    return (header if use_header else None), (footer if use_footer else None)


def preview(db: Session, company_id: int, template: LetterTemplate, emp=None, extra: dict | None = None,
            header_id=None, footer_id=None, sample: bool = True, as_pdf: bool = False):
    values = dict(merge.sample_values()) if sample and emp is None else {}
    values.update(build_values(db, company_id, emp, extra, reference_no="LTR/PREVIEW"))
    header, footer = pick_letterheads(db, company_id, template, header_id, footer_id)
    parts = render_parts(db, company_id, template, values, header, footer)
    kw = dict(header=parts["header"], footer=parts["footer"], header_mm=parts["header_mm"],
              footer_mm=parts["footer_mm"], **_layout_kwargs(template))
    if as_pdf:
        return pdf.render_pdf(pdf.pdf_html(parts["body"], title=parts["subject"], **kw))
    return {"html": pdf.preview_html(parts["body"], **kw), "missing": parts["missing"], "subject": parts["subject"]}


# ── Letters ─────────────────────────────────────────────────────────

def get_letter(db: Session, company_id: int, uuid) -> Letter:
    row = (db.query(Letter).options(joinedload(Letter.template))
           .filter(Letter.company_id == company_id, Letter.uuid == uuid).first())
    if row is None:
        raise NotFound("Letter")
    return row


def generate_letter(db: Session, company_id: int, template: LetterTemplate, emp, values: dict | None = None, *,
                    header_id=None, footer_id=None, use_header: bool = True, use_footer: bool = True,
                    workflow_instance_id: int | None = None, file_to_employee: bool = True,
                    allow_missing: bool = True) -> Letter:
    """Merge, render the PDF, keep both, and file the PDF in the employee's documents."""
    if not template.active:
        raise BadRequest("This template is switched off.")
    for f in template.extra_fields or []:
        if f.get("required") and not (values or {}).get(f["key"]):
            raise BadRequest(f"Fill in {f['label']}.")
    from core import numbering
    reference = numbering.next_number(db, company_id, "letter", _today(db, company_id))
    merged = build_values(db, company_id, emp, values, reference_no=reference)
    header, footer = pick_letterheads(db, company_id, template, header_id, footer_id, use_header, use_footer)
    parts = render_parts(db, company_id, template, merged, header, footer)
    if parts["missing"] and not allow_missing:
        raise BadRequest("Some fields have no value: " + ", ".join(parts["missing"]))
    doc_html = pdf.pdf_html(parts["body"], header=parts["header"], footer=parts["footer"], header_mm=parts["header_mm"],
                            footer_mm=parts["footer_mm"], title=parts["subject"], **_layout_kwargs(template))
    pdf_bytes = pdf.render_pdf(doc_html)
    who = display_name(emp) or "letter"
    blob = storage.store(db, company_id, f"{parts['subject']} - {who}.pdf", pdf_bytes, "application/pdf", "letter")
    letter = Letter(company_id=company_id, template_id=template.id, letter_type=template.letter_type,
                    reference_no=reference, title=f"{parts['subject']}"[:250], employee_id=emp.id if emp else None,
                    html=doc_html, values={k: str(v) for k, v in (values or {}).items()},
                    header_id=header.id if header else None, footer_id=footer.id if footer else None,
                    stored_file_id=blob.id, status="issued", workflow_instance_id=workflow_instance_id,
                    generated_by_name=actor_name())
    db.add(letter)
    db.flush()
    if emp is not None and file_to_employee:
        doc = file_into_employee_documents(db, company_id, emp.id, blob, letter.title,
                                           visible_to_employee=not template.esign_required)
        letter.doc_file_id = doc.id
    audit.event(db, "letter.generated", "letters", letter.id,
                {"template": template.name, "employee": who, "reference": reference},
                entity_uuid=str(letter.uuid), entity_label=letter.title)
    if template.esign_required and emp is not None:
        send_for_signature(db, company_id, letter, None, template.signing_order)
    elif emp is not None:
        notify(db, company_id, emp.id, f"Your {letter.title} is ready in My documents.", "letter")
    return letter


def bulk_generate(db: Session, company_id: int, template: LetterTemplate, employee_ids: list[int], values: dict,
                  header_id=None, footer_id=None) -> dict:
    if not employee_ids:
        raise BadRequest("Pick at least one employee.")
    if len(employee_ids) > 200:
        raise BadRequest("Generate up to 200 letters at a time.")
    made, failed = [], []
    for eid in employee_ids:
        emp = employee(db, company_id, eid)
        if emp is None:
            failed.append({"employee_id": eid, "reason": "Employee not found"})
            continue
        try:
            with db.begin_nested():
                made.append(generate_letter(db, company_id, template, emp, values, header_id=header_id,
                                            footer_id=footer_id))
        except BadRequest as exc:
            failed.append({"employee_id": eid, "reason": exc.detail})
    return {"letters": made, "failed": failed}


def cancel_letter(db: Session, letter: Letter) -> None:
    if letter.status == "cancelled":
        return
    for r in db.query(EsignRequest).filter(EsignRequest.letter_id == letter.id,
                                           EsignRequest.status == "out_for_signature").all():
        cancel_signature(db, r, "Letter withdrawn")
    letter.status = "cancelled"
    if letter.doc_file_id:
        f = db.get(DocFile, letter.doc_file_id)
        if f is not None and f.archived_at is None:
            f.archived_at = _now()


# ── E-signature ─────────────────────────────────────────────────────

def _token() -> str:
    return secrets.token_urlsafe(32)[:43]


def _otp_hash(signer: EsignSigner, code: str) -> str:
    key = get_settings().SECRET_KEY.encode()
    return hmac.new(key, f"{signer.uuid}:{code}".encode(), hashlib.sha256).hexdigest()


def resolve_signers(db: Session, company_id: int, letter: Letter, signers: list[dict] | None,
                    template: LetterTemplate | None) -> list[dict]:
    spec = signers if signers else (template.signers if template else None) or [{"kind": "employee", "label": "Employee"}]
    emp = employee(db, company_id, letter.employee_id)
    out = []
    for s in spec:
        kind = s.get("kind")
        if kind == "employee":
            target = emp
        elif kind == "manager":
            target = employee(db, company_id, emp.reporting_manager_id) if emp else None
        elif kind == "external":
            name, email = (s.get("name") or "").strip(), (s.get("email") or "").strip()
            if not name or "@" not in email:
                raise BadRequest("Give each outside signer a name and email address.")
            out.append({"employee_id": None, "name": name, "email": email, "label": s.get("label")})
            continue
        else:
            target = employee(db, company_id, s.get("employee_id"))
        if target is None:
            raise BadRequest("A signer could not be found (for example, the employee has no reporting manager).")
        if any(o.get("employee_id") == target.id for o in out):
            continue
        out.append({"employee_id": target.id, "name": display_name(target), "email": target.email,
                    "label": s.get("label") or {"employee": "Employee", "manager": "Reporting manager"}.get(kind)})
    if not out:
        raise BadRequest("Add at least one signer.")
    return out


def send_for_signature(db: Session, company_id: int, letter: Letter, signers: list[dict] | None,
                       signing_order: bool = True, expires_days: int = 14, message: str | None = None,
                       provider: str = "in_app_otp") -> EsignRequest:
    if provider not in ESIGN_PROVIDERS:
        raise BadRequest("Unknown signing method.")
    if provider == "aadhaar":
        from modules.documents import esign_providers
        esign_providers.get(provider).check_ready(db, company_id)
    if letter.status in ("out_for_signature", "signed"):
        raise BadRequest("This letter is already out for signature or signed.")
    people = resolve_signers(db, company_id, letter, signers, letter.template)
    req = EsignRequest(company_id=company_id, letter_id=letter.id, title=letter.title, provider=provider,
                       signing_order=signing_order, message=(message or "")[:500] or None,
                       expires_at=_now() + timedelta(days=max(1, min(expires_days, 90))), created_by_name=actor_name())
    db.add(req)
    db.flush()
    for i, p in enumerate(people, start=1):
        s = EsignSigner(company_id=company_id, request_id=req.id, position=i, employee_id=p["employee_id"],
                        name=p["name"], email=p["email"], label=p.get("label"), access_token=_token(),
                        status="pending" if (i == 1 or not signing_order) else "waiting")
        db.add(s)
    db.flush()
    db.refresh(req)
    letter.status = "out_for_signature"
    for s in req.signers:
        if s.status == "pending":
            _invite(db, req, s)
    audit.event(db, "esign.sent", "letters", letter.id, {"signers": [p["name"] for p in people]},
                entity_uuid=str(letter.uuid), entity_label=letter.title)
    return req


def _invite(db: Session, req: EsignRequest, s: EsignSigner) -> None:
    from core.email import send_email
    if s.employee_id:
        notify(db, req.company_id, s.employee_id, f"Please sign: {req.title}.", "esign")
    link = f"{get_settings().FRONTEND_URL}/sign/{s.access_token}"
    send_email(s.email, f"Please sign: {req.title}",
               f"Hello {s.name},\n\n{req.title} is waiting for your signature."
               + (f"\n\n{req.message}" if req.message else "")
               + f"\n\nSign in to the app and open My documents, or use this link: {link}\n")


def get_signer_by_token(db: Session, token: str) -> EsignSigner:
    row = (db.query(EsignSigner).options(joinedload(EsignSigner.request).joinedload(EsignRequest.letter))
           .filter(EsignSigner.access_token == token).first())
    if row is None:
        raise NotFound("Signing request")
    return row


def get_esign(db: Session, company_id: int, uuid) -> EsignRequest:
    row = (db.query(EsignRequest).options(joinedload(EsignRequest.signers), joinedload(EsignRequest.letter))
           .filter(EsignRequest.company_id == company_id, EsignRequest.uuid == uuid).first())
    if row is None:
        raise NotFound("Signing request")
    return row


def _check_open(req: EsignRequest, s: EsignSigner) -> None:
    if req.status != "out_for_signature":
        raise BadRequest("This document is no longer waiting for signatures.")
    if req.expires_at and req.expires_at < _now():
        raise BadRequest("The signing link has expired. Ask HR to send it again.")
    if s.status != "pending":
        raise BadRequest({"waiting": "Someone else needs to sign first.", "signed": "You have already signed.",
                          "declined": "You declined this document."}.get(s.status, "You can't sign this now."))


def send_otp(db: Session, s: EsignSigner) -> dict:
    """Email a 6-digit code. In development without email the code is returned so the
    flow can be tried."""
    req = s.request
    _check_open(req, s)
    if s.otp_expires_at and s.otp_expires_at > _now() + timedelta(minutes=OTP_MINUTES - 1):
        raise BadRequest("A code was just sent. Wait a minute before asking for another.")
    code = f"{secrets.randbelow(1_000_000):06d}"
    s.otp_hash = _otp_hash(s, code)
    s.otp_expires_at = _now() + timedelta(minutes=OTP_MINUTES)
    s.otp_attempts = 0
    from core.email import email_configured, send_email
    send_email(s.email, f"Your signing code: {code}",
               f"Hello {s.name},\n\nUse {code} to sign {req.title}. The code works for {OTP_MINUTES} minutes.\n")
    out = {"sent_to": _mask_email(s.email), "expires_in_minutes": OTP_MINUTES}
    if not email_configured() and not get_settings().is_production:
        out["dev_code"] = code
    return out


def _mask_email(email: str) -> str:
    name, _, domain = email.partition("@")
    return (name[:2] + "•" * max(len(name) - 2, 1)) + "@" + domain


def sign(db: Session, s: EsignSigner, code: str, signature_image: str | None, typed_name: str | None,
         ip: str | None, user_agent: str | None) -> EsignRequest:
    req = s.request
    _check_open(req, s)
    if not s.otp_hash or not s.otp_expires_at or s.otp_expires_at < _now():
        raise BadRequest("Ask for a new code; the last one has expired.")
    if s.otp_attempts >= OTP_MAX_ATTEMPTS:
        raise BadRequest("Too many wrong codes. Ask for a new code.")
    if not hmac.compare_digest(s.otp_hash, _otp_hash(s, (code or "").strip())):
        s.otp_attempts += 1
        db.flush()
        raise BadRequest("That code is not right.")
    if signature_image:
        if not signature_image.startswith("data:image/png;base64,") or len(signature_image) > 400_000:
            raise BadRequest("Draw your signature again.")
        s.signature_image, s.signature_method = signature_image, "drawn"
    elif (typed_name or "").strip():
        s.signature_image, s.signature_method = None, "typed"
        s.provider_ref = typed_name.strip()[:200]
    else:
        raise BadRequest("Draw or type your signature.")
    s.otp_hash = None
    s.otp_verified_at = s.signed_at = _now()
    s.status = "signed"
    s.ip_address, s.user_agent = (ip or "")[:64] or None, (user_agent or "")[:300] or None
    audit.event(db, "esign.signed", "letters", req.letter_id, {"signer": s.name, "method": s.signature_method},
                company_id=req.company_id, entity_uuid=str(req.letter.uuid), entity_label=req.title)
    db.flush()
    _advance(db, req)
    return req


def _advance(db: Session, req: EsignRequest) -> None:
    signers = sorted(req.signers, key=lambda x: x.position)
    if all(x.status == "signed" for x in signers):
        _complete(db, req)
        return
    if req.signing_order:
        nxt = next((x for x in signers if x.status == "waiting"), None)
        if nxt and all(x.status == "signed" for x in signers if x.position < nxt.position):
            nxt.status = "pending"
            _invite(db, req, nxt)


def _complete(db: Session, req: EsignRequest) -> None:
    letter = req.letter
    rows = [{"name": x.name, "label": x.label, "email": x.email, "image": x.signature_image,
             "typed": x.provider_ref if x.signature_method == "typed" else None,
             "signed_at": x.signed_at.strftime("%d %b %Y %H:%M UTC") if x.signed_at else "", "ip": x.ip_address}
            for x in sorted(req.signers, key=lambda x: x.position)]
    html = letter.html.replace("</main>", pdf.signature_page(req.title, rows) + "</main>")
    blob = storage.store(db, req.company_id, f"{letter.title} (signed).pdf", pdf.render_pdf(html),
                         "application/pdf", "signed")
    req.status, req.completed_at, req.signed_file_id = "completed", _now(), blob.id
    letter.status = "signed"
    if letter.employee_id:
        doc = file_into_employee_documents(db, req.company_id, letter.employee_id, blob, f"{letter.title} (signed)",
                                           folder_name="Signed documents", source="signed")
        if letter.doc_file_id:
            unsigned = db.get(DocFile, letter.doc_file_id)
            if unsigned is not None:
                unsigned.archived_at = _now()
        letter.doc_file_id = doc.id
        notify(db, req.company_id, letter.employee_id, f"{letter.title} is signed and filed in My documents.", "esign")
    audit.event(db, "esign.completed", "letters", letter.id, {"title": letter.title},
                company_id=req.company_id, entity_uuid=str(letter.uuid), entity_label=letter.title)


def decline(db: Session, s: EsignSigner, reason: str | None) -> EsignRequest:
    req = s.request
    _check_open(req, s)
    if not (reason or "").strip():
        raise BadRequest("Say why you are declining.")
    s.status, s.declined_at, s.decline_reason = "declined", _now(), reason.strip()[:500]
    req.status = "declined"
    req.letter.status = "declined"
    for x in req.signers:
        if x.status in ("waiting", "pending") and x.id != s.id:
            x.status = "cancelled"
    audit.event(db, "esign.declined", "letters", req.letter_id, {"signer": s.name, "reason": s.decline_reason},
                company_id=req.company_id, entity_uuid=str(req.letter.uuid), entity_label=req.title)
    return req


def cancel_signature(db: Session, req: EsignRequest, reason: str | None = None) -> None:
    if req.status != "out_for_signature":
        raise BadRequest("Only documents waiting for signatures can be recalled.")
    req.status = "cancelled"
    for x in req.signers:
        if x.status in ("waiting", "pending"):
            x.status = "cancelled"
    if req.letter.status == "out_for_signature":
        req.letter.status = "issued"
    audit.event(db, "esign.recalled", "letters", req.letter_id, {"reason": reason}, company_id=req.company_id,
                entity_uuid=str(req.letter.uuid), entity_label=req.title)


def remind_signers(db: Session, req: EsignRequest) -> int:
    if req.status != "out_for_signature":
        raise BadRequest("Only documents waiting for signatures need reminders.")
    n = 0
    for s in req.signers:
        if s.status == "pending":
            _invite(db, req, s)
            n += 1
    return n


def my_signatures(db: Session, company_id: int, employee_id: int) -> list[EsignSigner]:
    return (db.query(EsignSigner).options(joinedload(EsignSigner.request).joinedload(EsignRequest.letter))
            .filter(EsignSigner.company_id == company_id, EsignSigner.employee_id == employee_id)
            .order_by(EsignSigner.created_at.desc()).limit(100).all())


# ── Background jobs ─────────────────────────────────────────────────

def run_reminders(db: Session) -> int:
    """Policy acknowledgement reminders, file and policy expiry reminders, expired
    signing requests. Runs for every company (system session)."""
    from modules.company.models import Company
    done = 0
    for (cid,) in db.query(Company.id).filter(Company.active.is_(True)).all():
        today = _today(db, cid)
        # acknowledgements still pending: remind every N days
        rows = (db.query(PolicyAcknowledgement, Policy).join(Policy, Policy.id == PolicyAcknowledgement.policy_id)
                .filter(PolicyAcknowledgement.company_id == cid, PolicyAcknowledgement.acknowledged_at.is_(None),
                        Policy.active.is_(True), Policy.current_version_id == PolicyAcknowledgement.version_id).all())
        for a, p in rows:
            last = a.last_reminded_on or (a.created_at.date() if a.created_at else today)
            if (today - last).days >= p.reminder_every_days:
                overdue = a.due_on and a.due_on < today
                notify(db, cid, a.employee_id, ("Overdue: " if overdue else "Reminder: ")
                       + f"please acknowledge the {p.title}.", "policy")
                a.reminder_count += 1
                a.last_reminded_on = today
                done += 1
        # files expiring within 30 days: tell the uploader once
        for f in db.query(DocFile).filter(DocFile.company_id == cid, DocFile.archived_at.is_(None),
                                          DocFile.expires_on.isnot(None), DocFile.expires_on <= today + timedelta(days=30),
                                          DocFile.expiry_reminded_on.is_(None)).all():
            notify(db, cid, f.uploaded_by_employee_id, f"{f.title} expires on {merge.format_date(f.expires_on)}.", "file")
            f.expiry_reminded_on = today
            done += 1
        for p in db.query(Policy).filter(Policy.company_id == cid, Policy.active.is_(True), Policy.review_on.isnot(None),
                                         Policy.review_on <= today + timedelta(days=30),
                                         Policy.review_reminded_on.is_(None)).all():
            from modules.employees.models import Employee
            for (hr_id,) in db.query(Employee.id).filter(Employee.company_id == cid, Employee.role.in_(["hr", "admin"]),
                                                          Employee.active.is_(True)).all():
                notify(db, cid, hr_id, f"The {p.title} is due for review on {merge.format_date(p.review_on)}.", "policy")
            p.review_reminded_on = today
            done += 1
        for r in db.query(EsignRequest).filter(EsignRequest.company_id == cid, EsignRequest.status == "out_for_signature",
                                               EsignRequest.expires_at < _now()).all():
            r.status = "expired"
            if r.letter and r.letter.status == "out_for_signature":
                r.letter.status = "issued"
            done += 1
    db.flush()
    return done
