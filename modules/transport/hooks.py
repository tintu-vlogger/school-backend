"""Transport permissions, portal paths and daily jobs (expiry reminders, finished allocations)."""
from core import periodic
from core.security import register_portal_prefix
from modules.roles.models import register_permissions

register_permissions(
    {
        "transport.view": "See transport routes, vehicles, drivers and riders",
        "transport.manage": "Run transport: vehicles, drivers, routes, stops, fees and rider allocation",
    },
    {
        "principal": ["transport.view"],
        "hr": ["transport.view", "transport.manage"],
        "accountant": ["transport.view"],
        "teacher": ["transport.view"],
    },
)

register_portal_prefix("/transport/portal")


def _daily(db) -> int:
    from modules.transport import service
    return service.close_finished(db) + service.send_expiry_reminders(db)


periodic.register("transport.daily", _daily)
