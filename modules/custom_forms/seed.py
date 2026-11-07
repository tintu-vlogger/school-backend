"""Custom services: nothing is added for a new company (admins add templates with one
click). The demo company gets two templates so the pages have something to show."""
from sqlalchemy.orm import Session


def seed_demo(db: Session, company_id: int) -> None:
    from modules.custom_forms.models import CustomService
    from modules.custom_forms.service import install_template
    have = {k for (k,) in db.query(CustomService.template_key).filter(CustomService.company_id == company_id).all()}
    for key in ("training_request", "id_card_request", "visitor_pass"):
        if key not in have:
            install_template(db, company_id, key)
    db.flush()
