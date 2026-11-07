"""Face attendance defaults for each company: switched off, with the standard consent notice."""
from sqlalchemy.orm import Session


def setup_company(db: Session, company_id: int) -> None:
    from modules.face_attendance.service import ensure_settings
    ensure_settings(db, company_id)
