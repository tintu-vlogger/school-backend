"""Default probation policies for every company."""
from sqlalchemy.orm import Session

from modules.probation.models import ProbationPolicy


def setup_company(db: Session, company_id: int) -> None:
    if db.query(ProbationPolicy.id).filter(ProbationPolicy.company_id == company_id).first():
        return
    db.add(ProbationPolicy(
        company_id=company_id, name="Standard probation", description="Six months, reviewed by the manager and HR.",
        priority=100, audience={}, duration_value=6, duration_unit="months", review_before_days=15,
        allow_extension=True, extension_value=3, extension_unit="months", max_extensions=1, max_total_months=12,
        reviewers=[{"level": 1, "type": "reporting_manager", "value": None, "label": "Reporting manager"},
                   {"level": 2, "type": "role", "value": "hr", "label": "HR"}],
        feedback_from=[], auto_confirm=False, confirmation_letter=True))
    db.add(ProbationPolicy(
        company_id=company_id, name="Interns and trainees", description="Three months, no extension.",
        priority=50, audience={}, duration_value=3, duration_unit="months", review_before_days=10, active=False,
        allow_extension=False, extension_value=1, extension_unit="months", max_extensions=0, max_total_months=3,
        reviewers=[{"level": 1, "type": "reporting_manager", "value": None, "label": "Reporting manager"}],
        feedback_from=[], auto_confirm=True, confirmation_letter=True))
    db.flush()
