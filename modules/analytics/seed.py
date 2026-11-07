"""Ready dashboards for every company, kept in step with modules/analytics/ready.py. A
dashboard whose data source is not installed (e.g. hiring) is skipped."""
from sqlalchemy.orm import Session


def setup_company(db: Session, company_id: int) -> None:
    from modules.analytics.models import Dashboard
    from modules.analytics.ready import READY
    from modules.reports import registry
    existing = {d.key: d for d in db.query(Dashboard).filter(Dashboard.company_id == company_id,
                                                             Dashboard.is_standard.is_(True)).all()}
    for i, spec in enumerate(READY):
        widgets = [w for w in spec["widgets"] if registry.get(w["source"]) is not None]
        if not widgets:
            continue
        d = existing.get(spec["key"])
        if d is None:
            db.add(Dashboard(company_id=company_id, key=spec["key"], name=spec["name"], description=spec["description"],
                             icon=spec["icon"], widgets=widgets, is_standard=True, owner_name="Next Gen HRMS",
                             sort_order=i))
        elif d.widgets != widgets and not (d.updated_at and d.created_at and d.updated_at > d.created_at):
            # keep a company's own rearrangement; refresh only untouched ones
            d.widgets = widgets
    db.flush()
