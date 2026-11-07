"""Standard reports for every company; a weekly scheduled report for the demo company."""
from sqlalchemy.orm import Session


def setup_company(db: Session, company_id: int) -> None:
    from modules.reports.service import ensure_standard
    ensure_standard(db, company_id)


def seed_demo(db: Session, company_id: int) -> None:
    from modules.company.models import Company
    from modules.reports.models import Report, ReportSchedule
    from modules.reports.service import _company_zone, next_run
    if db.query(ReportSchedule.id).filter(ReportSchedule.company_id == company_id).first():
        return
    r = db.query(Report).filter(Report.company_id == company_id, Report.key == "headcount").first()
    company = db.query(Company).filter(Company.id == company_id).first()
    if r is None or company is None:
        return
    s = ReportSchedule(company_id=company_id, report_id=r.id, name="Monday headcount", frequency="weekly",
                       weekday=0, hour=9, format="xlsx", recipient_emails=[company.email], recipient_employee_ids=[],
                       run_as_employee_id=None, active=True, created_by_name="Company owner")
    db.add(s)
    db.flush()
    s.next_run_at = next_run(s, _company_zone(db, company_id))
