"""Travel defaults for every company: version 1 of the travel policy and the daily
allowance expense category used when a trip is settled."""
from sqlalchemy.orm import Session


def setup_company(db: Session, company_id: int) -> None:
    from modules.travel.service import ensure_defaults
    ensure_defaults(db, company_id)
