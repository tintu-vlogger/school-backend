"""Settings of the files, policies and letters module (one row per company)."""
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core.errors import BadRequest
from modules.documents.models import DocumentSettings

DEFAULTS = {
    "personal_locker": True,          # employees have a private locker
    "personal_file_limit": 50,        # files per locker
    "department_heads_can_share": True,
    "default_ack_days": 14,           # deadline for new policies
    "default_reminder_days": 3,       # remind every N days until acknowledged
    "expiry_reminder_days": 30,
    "self_service_letters": True,     # employees may request letters
    "esign_link_days": 14,
    "signatory_name": "",             # default signatory on letters
    "signatory_designation": "Head of HR",
}


def _row(db: Session, company_id: int) -> DocumentSettings:
    row = db.query(DocumentSettings).filter(DocumentSettings.company_id == company_id).first()
    if row is None:
        row = DocumentSettings(company_id=company_id, config={})
        db.add(row)
        db.flush()
    return row


def get(db: Session, company_id: int) -> dict:
    row = db.query(DocumentSettings).filter(DocumentSettings.company_id == company_id).first()
    return {**DEFAULTS, **((row.config if row else None) or {})}


def update(db: Session, company_id: int, data: dict) -> dict:
    row = _row(db, company_id)
    config = {**DEFAULTS, **(row.config or {})}
    for k, v in data.items():
        if k not in DEFAULTS:
            continue
        if isinstance(DEFAULTS[k], bool):
            config[k] = bool(v)
        elif isinstance(DEFAULTS[k], str):
            config[k] = str(v or "").strip()[:150]
        else:
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise BadRequest("Use whole numbers for limits and days.")
            if not 1 <= v <= 365:
                raise BadRequest("Limits and days must be between 1 and 365.")
            config[k] = v
    row.config = config
    flag_modified(row, "config")
    return config
