"""Approvals: the extra request type for cancelling approved requests, reminders, and
handing over the approvals of people who leave."""
from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from core import periodic
from core.workflow import extras  # noqa: F401  (registers "workflow.revoke")


def _reminders(db) -> int:
    return extras.send_reminders(db)


def _departed(db) -> int:
    return extras.reassign_departed(db)


periodic.register("workflow.reminders", _reminders)
periodic.register("workflow.departed_approvers", _departed)


@event.listens_for(Session, "before_flush")
def _approver_left(session: Session, _ctx, _instances) -> None:
    """When someone is switched off (exit, archive), their pending approvals go to their
    manager or the fallback, in the same save."""
    from modules.employees.models import Employee
    left = []
    for obj in session.dirty:
        if isinstance(obj, Employee) and obj.id:
            hist = inspect(obj).attrs.active.history
            if hist.has_changes() and obj.active is False and True in (hist.deleted or ()):
                left.append(obj.id)
    if not left:
        return
    try:
        with session.no_autoflush:
            extras.reassign_departed(session, employee_ids=left, leaving=True)
    except Exception as exc:  # pragma: no cover - the periodic job tries again
        import logging
        logging.getLogger("hrms").warning("Handing over approvals failed: %s", exc)
