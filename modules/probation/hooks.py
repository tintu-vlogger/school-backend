"""Probation background job (every 15 minutes, idempotent): probation records for people
on probation, reviews started N days before the end date, one daily reminder per reviewer."""
from core import periodic


def _daily(db):
    from modules.probation import service
    return service.run_daily(db)


periodic.register("probation.daily", _daily)
