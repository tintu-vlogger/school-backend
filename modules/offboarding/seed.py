"""Default exit flows for every company: one per exit type with its clearances and letters."""
from sqlalchemy.orm import Session

from modules.offboarding import settings as xsettings
from modules.offboarding.models import ExitFlow


def setup_company(db: Session, company_id: int) -> None:
    have = {t for (t,) in db.query(ExitFlow.exit_type).filter(ExitFlow.company_id == company_id).distinct()}
    for key, rules in xsettings.EXIT_TYPES.items():
        if key in have:
            continue
        clearances = xsettings.DEFAULT_CLEARANCES
        if key == "death":
            clearances = [c for c in clearances if c["department"] in ("assets", "finance", "hr")]
        db.add(ExitFlow(company_id=company_id, name=f"{rules['label']}", exit_type=key, audience={}, priority=100,
                        clearances=clearances, letters=rules["letters"],
                        exit_interview=key not in ("death", "absconding"), raised_by=rules["raised_by"],
                        needs_approval=key in ("resignation", "termination", "absconding", "contract_end")))
    db.flush()
