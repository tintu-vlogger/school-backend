"""Background job: SLA breaches and escalation, and closing resolved cases nobody rated."""
from sqlalchemy.orm import Session

from core import periodic


def _sla(db: Session) -> int:
    from modules.helpdesk import service
    return service.check_sla(db) + service.auto_close(db)


periodic.register("helpdesk.sla", _sla)
