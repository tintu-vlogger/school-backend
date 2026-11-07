"""Permissions of the timetable module and the portal paths it opens."""
from core.security import register_portal_prefix
from modules.roles.models import register_permissions

register_permissions(
    {
        "timetable.view": "See timetables of every section, teacher and room",
        "timetable.manage": "Build and publish timetables, set bell schedules and assign substitutions",
    },
    {
        "principal": ["timetable.view", "timetable.manage"],
        "hr": ["timetable.view"],
        "manager": ["timetable.view"],
        "teacher": ["timetable.view"],
        "employee": [],
    },
)

register_portal_prefix("/timetable/portal")
