"""Hostel permissions and portal paths (parents ask for leave from the portal)."""
from core.security import register_portal_prefix
from modules.roles.models import register_permissions

register_permissions(
    {
        "hostel.view": "See hostel blocks, rooms, boarders, roll-call and leave",
        "hostel.manage": "Run the hostel: rooms, allocation, roll-call, leave and out-passes, visitors",
    },
    {
        "principal": ["hostel.view"],
        "hr": ["hostel.view"],
        "accountant": ["hostel.view"],
    },
)

register_portal_prefix("/hostel/portal")
