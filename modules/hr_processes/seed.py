"""Default HR processes for every company."""
from sqlalchemy.orm import Session


def setup_company(db: Session, company_id: int) -> None:
    from modules.hr_processes.service import ensure_defaults
    ensure_defaults(db, company_id)
