"""Onboarding settings per company, stored as one JSON document over these defaults."""
from __future__ import annotations

import copy

from sqlalchemy.orm import Session

from core.errors import BadRequest
from modules.recruitment.common import deep_merge

SECTIONS = {
    "personal": "Personal details",
    "contact": "Address and contact",
    "family": "Family",
    "nominees": "Nominees (PF, EPS, gratuity)",
    "bank": "Bank account",
    "statutory": "PAN, Aadhaar, UAN and ESI",
    "previous_employment": "Previous employment and income this year",
    "tax": "Tax regime",
    "education": "Education",
    "emergency": "Emergency contact",
    "documents": "Documents",
}
OWNERS = {"hr": "HR", "it": "IT", "admin": "Admin", "manager": "Manager", "buddy": "Buddy", "joiner": "New joiner",
          "employee": "A specific person"}

DEFAULTS = {
    "portal_link_days": 21,
    "reminder_first_after_days": 2,
    "reminder_every_days": 3,
    "min_gap_days": 2,                    # pre-joining should be finished this many days before joining
    "checkin_days": [30, 60, 90],
    "welcome_email": True,
    "welcome_text": ("Welcome to the team, {first_name}! We are glad you are joining us on {joining_date}. "
                     "Your manager and buddy will help you settle in during the first weeks."),
    "buzz_post": True,
    "buzz_text": "Please welcome {name}, who joins us as {designation} in {department}.",
    "auto_start_day_one": True,           # move to day one on the joining date
    "require_appointment_letter": True,   # OSH Code: every employee gets an appointment letter
    "it_owner_id": None,                  # who does IT tasks
    "admin_owner_id": None,               # who does admin tasks (ID card, desk)
    "hr_owner_id": None,                  # default HR partner for new joiners
    "minimum_age": 18,
}


def _row(db: Session, company_id: int, create: bool = False):
    from modules.onboarding.models import OnboardingSettings
    row = db.query(OnboardingSettings).filter(OnboardingSettings.company_id == company_id).first()
    if row is None and create:
        row = OnboardingSettings(company_id=company_id, config={})
        db.add(row)
        db.flush()
    return row


def get(db: Session, company_id: int) -> dict:
    row = _row(db, company_id)
    return deep_merge(copy.deepcopy(DEFAULTS), (row.config if row else None) or {})


def save(db: Session, company_id: int, data: dict) -> dict:
    merged = deep_merge(get(db, company_id), data or {})
    try:
        for k in ("portal_link_days", "reminder_first_after_days", "reminder_every_days", "min_gap_days", "minimum_age"):
            merged[k] = int(merged[k])
        merged["checkin_days"] = sorted({int(x) for x in merged.get("checkin_days") or [] if int(x) > 0})
    except (TypeError, ValueError):
        raise BadRequest("Use whole numbers of days.")
    if not 1 <= merged["portal_link_days"] <= 90:
        raise BadRequest("The joining link can stay open for 1 to 90 days.")
    if merged["minimum_age"] < 14:
        raise BadRequest("The minimum age can't be below 14 (Child and Adolescent Labour Act).")
    row = _row(db, company_id, create=True)
    row.config = merged
    return merged
