"""Engagement preferences per company (Buzz, celebrations, kudos, surveys).

Stored as one JSONB row; anything missing falls back to DEFAULTS, so new preferences
need no migration. Changes are audited automatically (the row is a normal model).
"""
from __future__ import annotations

import copy
import re

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core.errors import BadRequest
from modules.engagement.models import EngagementSettings

# Emoji reactions people can use. The first eight show in the quick picker.
ALL_REACTIONS = ["👍", "❤️", "😂", "🎉", "👏", "🔥", "🤩", "💯", "🙏", "🚀", "😮", "😢", "💡", "🙌", "✨", "🥳",
                 "😍", "💪", "🤝", "✅", "👀", "🤔", "🌟", "🏆"]

BIRTHDAY_EMAIL = {
    "subject": "Happy birthday, {first_name}! 🎂",
    "body": ("Dear {first_name},\n\nEveryone at {company_name} wishes you a very happy birthday! "
             "May the year ahead bring you good health, happiness and success.\n\n"
             "Your colleagues have left wishes for you on Buzz: {buzz_link}\n\nWarm wishes,\n{company_name}"),
}
ANNIVERSARY_EMAIL = {
    "subject": "Happy {years_ordinal} work anniversary, {first_name}! 🎉",
    "body": ("Dear {first_name},\n\nToday marks {years} {years_word} since you joined {company_name}. "
             "Thank you for your hard work, ideas and the energy you bring every day.\n\n"
             "See what your colleagues are saying on Buzz: {buzz_link}\n\nWith gratitude,\n{company_name}"),
}
WELCOME_EMAIL = {
    "subject": "Welcome to {company_name}, {first_name}!",
    "body": ("Dear {first_name},\n\nWelcome aboard! We are delighted to have you with us as {designation}. "
             "Your colleagues have said hello on Buzz: {buzz_link}\n\nSee you around,\n{company_name}"),
}

EMAIL_PLACEHOLDERS = {
    "first_name": "First name", "full_name": "Full name", "company_name": "Company name",
    "designation": "Designation", "department": "Department", "years": "Years (anniversaries)",
    "years_word": "“year” or “years”", "years_ordinal": "1st, 2nd, 3rd …", "buzz_link": "Link to the Buzz post",
}

DEFAULTS = {
    "buzz": {
        "enabled": True,
        "who_can_post": "everyone",        # everyone | roles
        "post_roles": ["admin", "hr", "manager"],
        "review_posts": False,             # new posts wait for a moderator (approval flow)
        "allow_comments": True,
        "allow_polls": True,
        "allow_attachments": True,
        "allow_audience": True,            # people may post to a group instead of everyone
        "reactions": ALL_REACTIONS[:12],
        "blocked_words": [],
        "auto_hide_reports": 3,            # hide a post automatically after this many reports (0 = never)
        "edit_window_minutes": 0,          # 0 = authors may always edit
    },
    "celebrations": {
        "post_birthdays": True,
        "post_work_anniversaries": True,
        "post_wedding_anniversaries": False,
        "post_new_joiners": True,
        "post_hour": 9,                    # local time the morning job runs
        "show_upcoming_days": 14,
        "email_enabled": True,
        "email_birthday": BIRTHDAY_EMAIL,
        "email_work_anniversary": ANNIVERSARY_EMAIL,
        "email_welcome": WELCOME_EMAIL,
        "email_welcome_enabled": False,
        "default_birthday_privacy": "show",  # show | hide (until the employee chooses)
    },
    "kudos": {
        "enabled": True,
        "monthly_limit": 0,                # kudos one person may give per month (0 = no limit)
        "allow_multiple_recipients": True,
        "show_leaderboard": True,
        "post_to_buzz": True,
    },
    "surveys": {
        "min_anonymous_responses": 5,
        "default_reminder_days": 2,
    },
    "announcements": {
        "default_pinned_days": 7,
        "remind_ack_after_days": 2,
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in ("email_birthday", "email_work_anniversary", "email_welcome"):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def get_row(db: Session, company_id: int, create: bool = False) -> EngagementSettings | None:
    row = db.query(EngagementSettings).filter(EngagementSettings.company_id == company_id).first()
    if row is None and create:
        row = EngagementSettings(company_id=company_id, config={})
        db.add(row)
        db.flush()
    return row


def get(db: Session, company_id: int) -> dict:
    row = get_row(db, company_id)
    return _merge(DEFAULTS, row.config if row else {})


def section(db: Session, company_id: int, name: str) -> dict:
    return get(db, company_id)[name]


def _clean(name: str, data: dict, current: dict) -> dict:
    data = {k: v for k, v in (data or {}).items() if k in DEFAULTS[name]}
    if name == "buzz":
        if "who_can_post" in data and data["who_can_post"] not in ("everyone", "roles"):
            raise BadRequest("Choose who can post.")
        if "reactions" in data:
            reactions = [r for r in data["reactions"] if isinstance(r, str) and 0 < len(r) <= 16]
            if not reactions:
                raise BadRequest("Keep at least one reaction.")
            data["reactions"] = list(dict.fromkeys(reactions))[:24]
        if "blocked_words" in data:
            data["blocked_words"] = sorted({w.strip().lower() for w in data["blocked_words"] if isinstance(w, str) and w.strip()})[:200]
        if "auto_hide_reports" in data:
            data["auto_hide_reports"] = max(0, min(50, int(data["auto_hide_reports"] or 0)))
    if name == "celebrations":
        if "post_hour" in data:
            h = int(data["post_hour"])
            if not 0 <= h <= 23:
                raise BadRequest("Pick an hour between 0 and 23.")
            data["post_hour"] = h
        for key in ("email_birthday", "email_work_anniversary", "email_welcome"):
            if key in data:
                tpl = data[key] or {}
                subject, body = (tpl.get("subject") or "").strip(), (tpl.get("body") or "").strip()
                if not subject or not body:
                    raise BadRequest("An email needs a subject and a message.")
                try:
                    render_template(subject + body, SAMPLE_VALUES)
                except KeyError as exc:
                    raise BadRequest(f"Unknown placeholder {{{exc.args[0]}}} in the email.")
                data[key] = {"subject": subject[:200], "body": body[:5000]}
    if name == "surveys" and "min_anonymous_responses" in data:
        n = int(data["min_anonymous_responses"])
        if n < 5:
            raise BadRequest("Anonymous results need at least 5 responses before they show.")
        data["min_anonymous_responses"] = min(n, 50)
    if name == "kudos" and "monthly_limit" in data:
        data["monthly_limit"] = max(0, min(100, int(data["monthly_limit"] or 0)))
    return data


def update(db: Session, company_id: int, name: str, data: dict) -> dict:
    if name not in DEFAULTS:
        raise BadRequest("Unknown settings section.")
    row = get_row(db, company_id, create=True)
    current = get(db, company_id)[name]
    cleaned = _clean(name, data, current)
    config = dict(row.config or {})
    config[name] = {**(config.get(name) or {}), **cleaned}
    row.config = config
    flag_modified(row, "config")
    db.flush()
    return get(db, company_id)[name]


SAMPLE_VALUES = {
    "first_name": "Priya", "full_name": "Priya Sharma", "company_name": "Acme India", "designation": "Designer",
    "department": "Design", "years": 3, "years_word": "years", "years_ordinal": "3rd",
    "buzz_link": "https://example.com/dashboard/buzz",
}


_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def render_template(text: str, values: dict) -> str:
    """Fill {placeholders} (plain names only, no attribute access). Unknown ones raise
    KeyError, which is caught when the template is saved."""
    def sub(m):
        key = m.group(1)
        if key not in values:
            raise KeyError(key)
        return str(values[key])
    return _PLACEHOLDER.sub(sub, text)


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"
