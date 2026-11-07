"""Onboarding background job (every 15 minutes, idempotent): day-one switch on the joining
date, reminders for joiners who have not finished the joining details, and reminders for
due 30/60/90-day check-ins."""
from core import periodic


def _daily(db):
    from modules.onboarding import service
    return service.run_daily(db)


periodic.register("onboarding.daily", _daily)
