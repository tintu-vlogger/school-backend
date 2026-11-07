"""Number series for grievances, and demo committees and cases for the development company."""
from datetime import timedelta

from sqlalchemy.orm import Session

from modules.grievances.models import Grievance, GrievanceCommittee, GrievanceMessage, PoshProgramme


def setup_company(db: Session, company_id: int) -> None:
    from core.numbering import NumberSeries
    if not db.query(NumberSeries.id).filter(NumberSeries.company_id == company_id, NumberSeries.key == "grievance").first():
        db.add(NumberSeries(company_id=company_id, key="grievance", name="Grievances", prefix="GRV",
                            pattern="{PREFIX}-{YYYY}-{SEQ:4}", reset="yearly", active=True, is_system=True,
                            description="Reference numbers for grievances and POSH complaints."))
        db.flush()


def seed_demo(db: Session, company_id: int) -> None:
    from core import numbering
    from modules.employees.models import Employee
    from modules.grievances import service
    setup_company(db, company_id)
    if db.query(GrievanceCommittee.id).filter(GrievanceCommittee.company_id == company_id).first():
        return
    emps = db.query(Employee).filter(Employee.company_id == company_id, Employee.active.is_(True)).order_by(Employee.id).all()
    if len(emps) < 2:
        return
    today = service._today(db, company_id)
    presiding = next((e for e in emps if e.greeting in ("Ms.", "Mrs.", "Ms", "Mrs")), emps[0])
    others = [e for e in emps if e.id != presiding.id]
    service.save_committee(db, company_id, {
        "kind": "posh_ic", "name": "Internal Committee (POSH)", "constituted_on": today - timedelta(days=200),
        "valid_until": today + timedelta(days=895), "active": True,
        "description": "Constituted under section 4 of the POSH Act, 2013 for a term of three years.",
        "members": [{"employee_id": presiding.id, "role": "presiding_officer"},
                    *[{"employee_id": e.id, "role": "member"} for e in others[:2]],
                    {"external_name": "Adv. Meera Krishnan", "external_org": "Sakhi Legal Aid Trust",
                     "external_email": "meera@sakhi.example.org", "role": "external_member"}]})
    service.save_committee(db, company_id, {
        "kind": "grievance", "name": "Grievance Redressal Committee", "constituted_on": today - timedelta(days=300),
        "active": True, "description": "Under section 4 of the Industrial Relations Code, 2020.",
        "members": [{"employee_id": e.id, "role": "chairperson" if i == 0 else "member"} for i, e in enumerate(emps[:3])]})
    conf = service.get_settings(db, company_id)
    cases = [
        ("posh", False, "Inappropriate messages from a team lead", "Since August I have received repeated personal messages late at night that make me uncomfortable.", 40, 25, "inquiry"),
        ("posh", False, "Comments during the offsite", "Remarks of a sexual nature were made about me at the team offsite in front of others.", 120, 115, "report_submitted"),
        ("grievance", True, "Overtime not paid for night shifts", "Night shift overtime in the warehouse has not been paid for two months.", 12, None, "under_review"),
        ("grievance", False, "Seat near the server room is too cold", "The AC near the server room is set very low and several of us have fallen ill.", 45, None, "resolved"),
        ("grievance", True, "Unfair shift allocation", "Weekend shifts always go to the same few people in the support team.", 3, None, "submitted"),
    ]
    for i, (kind, anon, subject, desc, days_ago, incident_days, status) in enumerate(cases):
        received = today - timedelta(days=days_ago)
        complainant = others[i % len(others)] if others else emps[0]
        g = Grievance(company_id=company_id, kind=kind, reference_no=numbering.next_number(db, company_id, "grievance"),
                      tracking_hash=service.code_hash(company_id, f"DEMO-{i:04d}-CODE"), subject=subject, description=desc,
                      anonymous=anon, complainant_employee_id=None if anon else complainant.id, received_on=received,
                      status=status, incident_date=received - timedelta(days=incident_days - 10) if incident_days else None,
                      created_at=service._now() - timedelta(days=days_ago))
        if kind == "posh":
            g.respondent_name = "A team lead (name with the committee)"
            d = service.posh_deadlines(conf, received, g.incident_date)
            g.filing_deadline, g.late_filing = d["filing_deadline"], d["late_filing"]
            g.acknowledge_due_on, g.inquiry_due_on = d["acknowledge_due_on"], d["inquiry_due_on"]
            g.acknowledged_on = received + timedelta(days=3)
            g.inquiry_started_on = received + timedelta(days=10)
            if status == "report_submitted":
                g.inquiry_completed_on = received + timedelta(days=80)
                g.report_due_on = g.inquiry_completed_on + timedelta(days=conf["posh_report_days"])
                g.report_submitted_on = g.inquiry_completed_on + timedelta(days=6)
                g.action_due_on = g.report_submitted_on + timedelta(days=conf["posh_action_days"])
                g.outcome, g.outcome_summary = "upheld", "The committee found the complaint proved."
        else:
            g.acknowledge_due_on = received + timedelta(days=7)
            g.inquiry_due_on = received + timedelta(days=conf["grievance_resolution_days"])
            if status != "submitted":
                g.acknowledged_on = received + timedelta(days=2)
            if status == "resolved":
                g.inquiry_completed_on = received + timedelta(days=9)
                g.outcome, g.outcome_summary = "upheld", "Facilities adjusted the temperature and moved two desks."
                g.action_taken = "AC set to 24°C; seating changed."
        db.add(g)
        db.flush()
        db.add(GrievanceMessage(company_id=company_id, grievance_id=g.id, kind="event", author_label="System",
                                body="Received.", created_at=g.created_at))
        if status != "submitted":
            db.add(GrievanceMessage(company_id=company_id, grievance_id=g.id, kind="reply", author_label="Committee",
                                    author_employee_id=presiding.id, created_at=g.created_at + timedelta(days=2),
                                    body="Thank you for raising this. The committee has taken it up and will keep you informed."))
            db.add(GrievanceMessage(company_id=company_id, grievance_id=g.id, kind="note", author_label=service.display_name(presiding),
                                    author_employee_id=presiding.id, created_at=g.created_at + timedelta(days=3),
                                    body="Confidential: spoke to two witnesses; statements recorded."))
    for d, title, kind, n in ((today - timedelta(days=160), "POSH awareness session for all staff", "awareness", 42),
                              (today - timedelta(days=60), "Internal Committee training", "training", 5),
                              (today - timedelta(days=20), "Workshop for people managers", "workshop", 14)):
        db.add(PoshProgramme(company_id=company_id, held_on=d, title=title, kind=kind, participants=n))
    db.flush()
