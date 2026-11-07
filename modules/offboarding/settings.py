"""Exit settings per company (one JSON document over these defaults) and the exit types."""
from __future__ import annotations

import copy

from sqlalchemy.orm import Session

from core.errors import BadRequest
from modules.recruitment.common import deep_merge

EXIT_TYPES = {
    "resignation": {"label": "Resignation", "notice": True, "letters": ["relieving", "experience"],
                    "raised_by": ["employee", "manager", "hr"]},
    "termination": {"label": "Termination", "notice": True, "letters": ["termination", "experience"],
                    "raised_by": ["manager", "hr"]},
    "retrenchment": {"label": "Retrenchment", "notice": True, "letters": ["termination", "experience"],
                     "raised_by": ["hr"]},
    "layoff": {"label": "Lay-off", "notice": False, "letters": ["experience"], "raised_by": ["hr"]},
    "contract_end": {"label": "End of contract", "notice": False, "letters": ["relieving", "experience"],
                     "raised_by": ["manager", "hr"]},
    "retirement": {"label": "Retirement", "notice": False, "letters": ["relieving", "experience"],
                   "raised_by": ["hr"]},
    "death": {"label": "Death", "notice": False, "letters": [], "raised_by": ["hr"]},
    "absconding": {"label": "Absconding", "notice": False, "letters": ["termination"], "raised_by": ["manager", "hr"]},
}
CLEARANCE_DEPTS = {"it": "IT", "admin": "Admin", "finance": "Finance", "manager": "Reporting manager",
                   "assets": "Assets", "hr": "HR", "other": "Other"}
NOTICE_ACTIONS = {"serve": "Serve the notice", "buyout": "Employee buys out the shortfall",
                  "waive": "Company waives the shortfall", "recover": "Recover the shortfall in settlement",
                  "pay_in_lieu": "Company pays notice in lieu"}

DEFAULT_CLEARANCES = [
    {"key": "manager", "label": "Manager clearance", "department": "manager", "owner_type": "reporting_manager",
     "due_days": -2, "items": ["Work handed over", "Knowledge transfer done", "Pending approvals delegated"]},
    {"key": "it", "label": "IT clearance", "department": "it", "owner_type": "role", "owner_value": "hr",
     "due_days": 0, "items": ["Laptop returned", "Email and system access closed", "Data backed up"]},
    {"key": "admin", "label": "Admin clearance", "department": "admin", "owner_type": "role", "owner_value": "hr",
     "due_days": 0, "items": ["ID card returned", "Access card returned", "Locker keys returned"]},
    {"key": "assets", "label": "Asset return", "department": "assets", "owner_type": "role", "owner_value": "hr",
     "due_days": 0, "items": []},
    {"key": "finance", "label": "Finance clearance", "department": "finance", "owner_type": "role", "owner_value": "hr",
     "due_days": 0, "items": ["Advances and loans settled", "Expense claims closed", "Corporate card returned"]},
    {"key": "hr", "label": "HR clearance", "department": "hr", "owner_type": "role", "owner_value": "hr",
     "due_days": 1, "items": ["Exit interview done", "Documents collected", "PF exit date updated"]},
]

INTERVIEW_QUESTIONS = [
    {"key": "liked_most", "label": "What did you like most about working here?"},
    {"key": "improve", "label": "What should we improve?"},
    {"key": "trigger", "label": "What made you decide to leave?"},
    {"key": "manager_feedback", "label": "How was your relationship with your manager?"},
    {"key": "anything_else", "label": "Anything else you would like to share?"},
]
RATING_AREAS = {"manager": "Manager", "growth": "Growth and learning", "pay": "Pay and benefits",
                "culture": "Culture and team", "worklife": "Work-life balance", "role": "The work itself"}

DEFAULTS = {
    "probation_notice_days": 15,
    "default_notice_days": 30,
    "notice_rate_basis": "gross",        # gross | basic; daily rate = monthly / 30
    "settlement_working_days": 2,        # Code on Wages
    "alumni_access_days": 365,
    "alumni_documents": ["letters", "payslips", "tax"],
    "retirement_age": 58,
    "retirement_start_days_before": 90,
    "auto_complete_after_last_day": True,
    "archive_on_completion": True,
    "rehire_default": True,
    "retrenchment": {"days_per_year": 15, "reskilling_days": 15, "notice_days": 30,
                     "permission_threshold": 300, "headcount": None},
    "layoff_compensation_percent": 50,   # of basic + DA for laid-off days
    "exit_interview_days_before": 3,
}


def _row(db: Session, company_id: int, create: bool = False):
    from modules.offboarding.models import OffboardingSettings
    row = db.query(OffboardingSettings).filter(OffboardingSettings.company_id == company_id).first()
    if row is None and create:
        row = OffboardingSettings(company_id=company_id, config={})
        db.add(row)
        db.flush()
    return row


def get(db: Session, company_id: int) -> dict:
    row = _row(db, company_id)
    return deep_merge(copy.deepcopy(DEFAULTS), (row.config if row else None) or {})


def save(db: Session, company_id: int, data: dict) -> dict:
    merged = deep_merge(get(db, company_id), data or {})
    try:
        for k in ("probation_notice_days", "default_notice_days", "settlement_working_days", "alumni_access_days",
                  "retirement_age", "retirement_start_days_before", "layoff_compensation_percent"):
            merged[k] = int(merged[k])
    except (TypeError, ValueError):
        raise BadRequest("Use whole numbers.")
    if merged["settlement_working_days"] > 2:
        raise BadRequest("The law requires settlement within 2 working days; a longer limit is not allowed.")
    if not 50 <= merged["retirement_age"] <= 75:
        raise BadRequest("Retirement age should be between 50 and 75.")
    if not 0 <= merged["alumni_access_days"] <= 3650:
        raise BadRequest("Alumni access can last up to 10 years.")
    row = _row(db, company_id, create=True)
    row.config = merged
    return merged
