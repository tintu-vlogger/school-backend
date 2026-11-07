"""Benefits permission and the daily auto-enrolment job (joiners in, leavers out)."""
from modules.roles.models import ALL_PERMISSIONS, DEFAULT_ROLE_PERMISSIONS

ALL_PERMISSIONS.setdefault("benefits.manage", "Set up insurance and benefit plans and manage enrolments")
for _role in ("admin", "hr"):
    lst = DEFAULT_ROLE_PERMISSIONS.setdefault(_role, [])
    if "benefits.manage" not in lst:
        lst.append("benefits.manage")


def _job(db) -> int:
    from modules.benefits import service
    from modules.company.models import Company
    done = 0
    for (cid,) in db.query(Company.id).filter(Company.active.is_(True)).all():
        db.info["company_id"] = cid
        with db.begin_nested():
            r = service.auto_enrol(db, cid)
            done += r["enrolled"] + r["ended"]
    db.info.pop("company_id", None)
    return done


try:
    from core import periodic
    periodic.register("benefits.auto_enrol", _job)
except ImportError:  # pragma: no cover
    pass
