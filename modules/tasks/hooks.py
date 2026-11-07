"""Background job: due-soon reminders and overdue notices for tasks."""
from sqlalchemy.orm import Session

from core import periodic


def _reminders(db: Session) -> int:
    from modules.tasks import service
    return service.run_reminders(db)


periodic.register("tasks.reminders", _reminders)
