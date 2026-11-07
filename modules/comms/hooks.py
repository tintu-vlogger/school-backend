"""Permissions, portal API access and background jobs of the communication module."""
from sqlalchemy.orm import Session

from core import periodic
from core.security import register_portal_prefix
from modules.roles.models import register_permissions

register_permissions(
    {
        "comms.view": "See notices, the events calendar and parent-teacher meetings",
        "comms.notices.manage": "Write and send notices and circulars to anyone",
        "comms.notices.section": "Send notices to the parents and students of own sections",
        "comms.messages.broadcast": "Send one message to all parents of own sections",
        "comms.messages.all": "Message any parent or student",
        "comms.messages.office": "Answer messages sent to the office",
        "comms.messages.audit": "Read every conversation (safeguarding review)",
        "comms.events.manage": "Add and change events and holidays in the calendar",
        "comms.ptm.manage": "Plan parent-teacher meetings and their slots",
        "comms.diary.post": "Write the daily class diary for own sections",
        "comms.settings.manage": "Change communication settings: office hours, who may message, channels",
        "comms.portal": "Notices, messages, events and meetings in the parent and student portal",
    },
    {
        "principal": ["comms.view", "comms.notices.manage", "comms.messages.broadcast", "comms.messages.all",
                      "comms.messages.office", "comms.messages.audit", "comms.events.manage", "comms.ptm.manage",
                      "comms.diary.post", "comms.settings.manage"],
        "hr": ["comms.view", "comms.notices.manage", "comms.messages.office", "comms.events.manage"],
        "manager": ["comms.view", "comms.notices.section", "comms.messages.broadcast", "comms.diary.post"],
        "teacher": ["comms.view", "comms.notices.section", "comms.messages.broadcast", "comms.diary.post"],
        "accountant": ["comms.view"],
        "librarian": ["comms.view"],
        "employee": ["comms.view"],
        "parent": ["comms.portal"],
        "student": ["comms.portal"],
    },
)

register_portal_prefix("/comms/portal")


def _publish_due(db: Session) -> int:
    from modules.comms import notices
    return notices.publish_due(db)


def _ptm_reminders(db: Session) -> int:
    from modules.comms import ptm
    return ptm.send_reminders(db)


periodic.register("comms.scheduled_notices", _publish_due)
periodic.register("comms.ptm_reminders", _ptm_reminders)
