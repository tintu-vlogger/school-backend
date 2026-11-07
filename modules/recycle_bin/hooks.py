"""Recycle bin permissions, the registry of soft-deletable models and the optional
automatic purge job."""
from core import periodic
from modules.roles.models import ALL_PERMISSIONS

ALL_PERMISSIONS.setdefault("settings.recycle_bin.manage", "See the recycle bin and restore deleted records")
ALL_PERMISSIONS.setdefault("settings.recycle_bin.purge", "Remove records from the recycle bin for good and set how long they are kept")
# Only the HR admin role (which holds every permission) gets these by default.


def _auto_purge(db) -> int:
    from modules.recycle_bin import service
    return service.auto_purge(db)


periodic.register("recycle_bin.auto_purge", _auto_purge)
