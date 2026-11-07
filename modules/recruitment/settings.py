"""Hiring settings per company, stored as one JSON document over these defaults."""
from __future__ import annotations

import copy

from sqlalchemy.orm import Session

from core.errors import BadRequest
from modules.recruitment.common import deep_merge

DEFAULT_STAGES = [
    {"key": "applied", "label": "Applied", "kind": "applied"},
    {"key": "screening", "label": "Screening", "kind": "screening"},
    {"key": "shortlisted", "label": "Shortlisted", "kind": "screening"},
    {"key": "interview", "label": "Interviews", "kind": "interview"},
    {"key": "offer", "label": "Offer", "kind": "offer"},
    {"key": "hired", "label": "Hired", "kind": "hired"},
]
STAGE_KINDS = {"applied": "Applied", "screening": "Screening", "interview": "Interview", "offer": "Offer",
               "hired": "Hired"}

SOURCES = {"careers": "Careers page", "referral": "Employee referral", "agency": "Agency", "job_board": "Job site",
           "walk_in": "Walk-in", "direct": "Added by HR", "campus": "Campus", "linkedin": "LinkedIn", "other": "Other"}

BOARDS = {
    "naukri": {"label": "Naukri", "share": "https://www.naukri.com/recruit/login", "docs": "Naukri RMS job posting API"},
    "indeed": {"label": "Indeed", "share": "https://employers.indeed.com/", "docs": "Indeed reads the XML feed"},
    "linkedin": {"label": "LinkedIn", "share": "https://www.linkedin.com/sharing/share-offsite/?url={url}",
                 "docs": "LinkedIn Job Posting API (partner access)"},
}

DEFAULTS = {
    "careers": {
        "enabled": True,
        "headline": "Build your career with us",
        "intro": "We are a growing team. Find a role where you can do your best work.",
        "about": "",
        "accent": "#4f46e5",
        "show_department_filter": True,
        "show_location_filter": True,
        "allow_general_application": False,
        "default_questions": [
            {"key": "notice", "label": "What is your notice period (days)?", "type": "number", "required": False},
        ],
    },
    "privacy": {
        "notice": ("We use the details and CV you share only to consider you for jobs with us. "
                   "HR and the hiring team can see them. We keep them for the period below and then delete them, "
                   "unless you join us. You can ask us to see, correct or delete your data at any time."),
        "retention_days": 180,           # delete candidates who did not join after this many days of inactivity
        "talent_pool_retention_days": 365,
        "grievance_contact": "",
    },
    "screening": {
        "scorer": "rules",
        "weights": {"skills": 50, "experience": 30, "nice_skills": 10, "location": 10},
        "auto_reject_below": None,
        "auto_shortlist_above": None,
    },
    "interviews": {
        "reminder_hours": 24,
        "send_invites": True,
        "default_duration_minutes": 45,
        "competencies": [
            {"key": "role_knowledge", "label": "Role knowledge"},
            {"key": "problem_solving", "label": "Problem solving"},
            {"key": "communication", "label": "Communication"},
            {"key": "culture", "label": "Values and team fit"},
        ],
    },
    "offers": {
        "expiry_days": 7,
        "default_probation_months": 6,
        "default_notice_days": 30,
        "approval_outside_band": True,
        "esign": True,
    },
    "referrals": {
        "default_bonus": "25000",
        "qualifying_days": 90,          # bonus becomes due this many days after joining
        "pay_via_payroll": True,
    },
    "boards": {
        "naukri": {"enabled": False, "api_key": "", "account_id": ""},
        "indeed": {"enabled": True, "api_key": "", "account_id": ""},
        "linkedin": {"enabled": False, "api_key": "", "account_id": ""},
    },
    "follow_up_days": 3,
    "default_stages": DEFAULT_STAGES,
}

SECRET_KEYS = {"api_key"}


def _row(db: Session, company_id: int, create: bool = False):
    from modules.recruitment.models import RecruitmentSettings
    row = db.query(RecruitmentSettings).filter(RecruitmentSettings.company_id == company_id).first()
    if row is None and create:
        row = RecruitmentSettings(company_id=company_id, config={})
        db.add(row)
        db.flush()
    return row


def get(db: Session, company_id: int) -> dict:
    row = _row(db, company_id)
    return deep_merge(copy.deepcopy(DEFAULTS), (row.config if row else None) or {})


def public(conf: dict) -> dict:
    """Settings for the screen: connector keys are never sent back, only whether one is set."""
    out = copy.deepcopy(conf)
    for b in out.get("boards", {}).values():
        for k in SECRET_KEYS:
            if k in b:
                b[f"{k}_set"] = bool(b[k])
                b[k] = ""
    return out


def save(db: Session, company_id: int, data: dict) -> dict:
    current = get(db, company_id)
    incoming = copy.deepcopy(data or {})
    # keep stored connector keys when the screen sends them back empty
    for name, b in (incoming.get("boards") or {}).items():
        for k in SECRET_KEYS:
            if k in b and not b[k]:
                b[k] = current.get("boards", {}).get(name, {}).get(k, "")
            b.pop(f"{k}_set", None)
    merged = deep_merge(current, incoming)
    p = merged["privacy"]
    try:
        p["retention_days"] = int(p["retention_days"])
        p["talent_pool_retention_days"] = int(p["talent_pool_retention_days"])
    except (TypeError, ValueError):
        raise BadRequest("Keep candidate data for a whole number of days.")
    if not 30 <= p["retention_days"] <= 1095:
        raise BadRequest("Keep candidate data for between 30 and 1,095 days.")
    if (merged["offers"].get("expiry_days") or 0) < 1:
        raise BadRequest("An offer should stay open for at least a day.")
    stages = merged.get("default_stages") or []
    clean_stages(stages)
    row = _row(db, company_id, create=True)
    row.config = merged
    return merged


def clean_stages(stages: list[dict]) -> list[dict]:
    """A pipeline must start with an applied stage and end with hired; keys are unique."""
    if not stages:
        raise BadRequest("Add the pipeline stages.")
    out, seen = [], set()
    for s in stages:
        label = (s.get("label") or "").strip()
        if not label:
            raise BadRequest("Give every stage a name.")
        key = (s.get("key") or "").strip() or label.lower().replace(" ", "_")[:40]
        key = "".join(ch for ch in key if ch.isalnum() or ch == "_")[:40] or f"stage{len(out) + 1}"
        if key in seen:
            raise BadRequest(f"Two stages share the key {key}.")
        kind = s.get("kind") or "screening"
        if kind not in STAGE_KINDS:
            raise BadRequest("Unknown stage kind.")
        seen.add(key)
        out.append({"key": key, "label": label[:60], "kind": kind})
    if out[0]["kind"] != "applied":
        raise BadRequest("The first stage should be the applied stage.")
    if out[-1]["kind"] != "hired":
        raise BadRequest("The last stage should be the hired stage.")
    if sum(1 for s in out if s["kind"] == "hired") != 1:
        raise BadRequest("Keep exactly one hired stage.")
    return out
