"""Performance services, for routers and for other modules.

The rules are split by area: okr.py (goals and OKRs), library.py (scales, KRAs,
competencies, templates), reviews.py (cycles, calibration, nine-box), talent.py
(one-to-ones, feedback, improvement plans) and increments.py (pay reviews). Other modules
should call only the functions re-exported here.
"""
from modules.performance.increments import apply_due_changes, current_ctc  # noqa: F401
from modules.performance.okr import current_period, ensure_periods, refresh_statuses  # noqa: F401
from modules.performance.reviews import escalate  # noqa: F401


def latest_rating(db, company_id: int, employee_id: int) -> dict | None:
    """The employee's latest published review result: {cycle, rating, label}."""
    from modules.performance.models import ReviewCycle, ReviewParticipant
    from modules.performance.reviews import _scale
    from modules.performance.common import scale_label
    p = (db.query(ReviewParticipant).filter(ReviewParticipant.company_id == company_id, ReviewParticipant.employee_id == employee_id,
                                            ReviewParticipant.published_at.isnot(None))
         .order_by(ReviewParticipant.published_at.desc()).first())
    if p is None:
        return None
    cycle = db.get(ReviewCycle, p.cycle_id)
    return {"cycle": cycle.name, "rating": float(p.final_rating) if p.final_rating is not None else None,
            "label": scale_label(_scale(db, cycle), p.final_rating)}


def daily(db, company_id: int) -> None:
    """Daily housekeeping: goal statuses move with the calendar, overdue reviews escalate,
    and pay changes whose date has come are applied."""
    refresh_statuses(db, company_id)
    escalate(db, company_id)
    apply_due_changes(db, company_id)
