"""Public functions of the engagement module for other modules. The rules themselves
live in feed.py, celebrations.py, kudos.py, announcements.py, surveys.py and kiosk.py.

    from modules.engagement import service as engagement
    engagement.kudos_summary(db, company_id, employee_id)
    engagement.register_audience_dimension(Dimension(...))
"""
from sqlalchemy.orm import Session

from modules.engagement.audience import Dimension, register_dimension as register_audience_dimension  # noqa: F401
from modules.engagement.audience import resolve as resolve_audience  # noqa: F401


def kudos_summary(db: Session, company_id: int, employee_id: int) -> dict:
    """Kudos received and given, badges and the latest few, for a profile page."""
    from modules.engagement.kudos import profile_summary
    return profile_summary(db, company_id, employee_id)


def post_system_message(db: Session, company_id: int, body: str, *, audience: dict | None = None,
                        subject_employee_id: int | None = None) -> None:
    """Let another module put a message from the company on Buzz (e.g. a confirmed hire)."""
    from modules.engagement.audience import normalize
    from modules.engagement.models import BuzzPost
    db.add(BuzzPost(company_id=company_id, kind="post", is_system=True, body=body[:5000],
                    audience=normalize(audience), subject_employee_id=subject_employee_id))
    db.flush()
