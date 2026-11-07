"""Library permissions, the accession number series, portal paths and daily jobs
(overdue reminders, expired holds)."""
from core import periodic
from core.numbering import register_series
from core.security import register_portal_prefix
from modules.roles.models import register_permissions

register_permissions(
    {
        "library.view": "See the library catalogue, members, loans and reports",
        "library.circulate": "Issue, return and renew books, take fines and place holds",
        "library.manage": "Run the library: catalogue, copies, members, rules, waivers and stock checks",
    },
    {
        "librarian": ["library.view", "library.circulate", "library.manage"],
        "principal": ["library.view"],
        "teacher": ["library.view"],
        "hr": ["library.view"],
        "accountant": ["library.view"],
    },
)

register_series("library_accession", "Library accession number", "ACC", "{PREFIX}{SEQ:6}", "never", True,
                "Given to every new copy added to the library; printed as its barcode.")

register_portal_prefix("/library/portal")


def _daily(db) -> int:
    from modules.library import service
    return service.expire_holds(db) + service.send_overdue_reminders(db)


periodic.register("library.daily", _daily)
