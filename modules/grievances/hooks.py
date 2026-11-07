"""Background job: POSH deadline alerts to the Internal Committee."""
from sqlalchemy.orm import Session

from core import periodic


def _alerts(db: Session) -> int:
    from modules.grievances import service
    return service.deadline_alerts(db)


periodic.register("grievances.deadlines", _alerts)
