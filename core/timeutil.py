"""Company-local clock helpers.

Dates such as attendance and roster days are local to the company's own timezone.
Anything that needs "today" or "now" for a company must use these
helpers instead of date.today() / datetime.utcnow(), which are server time.
"""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

DEFAULT_TZ = "Asia/Kolkata"


def tz_for(company) -> ZoneInfo:
    name = getattr(company, "timezone", None) or DEFAULT_TZ
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def now_for(company) -> datetime:
    """Timezone-aware 'now' in the company's local timezone."""
    return datetime.now(tz_for(company))


def today_for(company) -> date:
    return now_for(company).date()


def _load_company(db: Session, company_id: int):
    from modules.company.models import Company
    return db.query(Company).filter(Company.id == company_id).first()


def now_for_company_id(db: Session, company_id: int) -> datetime:
    return now_for(_load_company(db, company_id))


def today_for_company_id(db: Session, company_id: int) -> date:
    return today_for(_load_company(db, company_id))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None) -> str | None:
    """ISO 8601 in UTC with a Z, the format every API response uses."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def is_valid_timezone(name: str | None) -> bool:
    if not name or len(name) > 64:
        return False
    try:
        ZoneInfo(name)
        return True
    except Exception:
        return False


def effective_timezone(employee=None, company=None) -> str:
    """The employee's own timezone, else the company's, else India."""
    for candidate in (getattr(employee, "timezone", None), getattr(company, "timezone", None)):
        if is_valid_timezone(candidate):
            return candidate
    return DEFAULT_TZ
