"""Default help desk set-up for every company and demo cases for the development company."""
from datetime import timedelta

from sqlalchemy.orm import Session

from modules.helpdesk.models import (HelpdeskArticle, HelpdeskCannedResponse, HelpdeskCase, HelpdeskCategory,
                                     HelpdeskMessage, HelpdeskSla)

CATEGORIES = [
    ("HR", "Policies, leave, attendance, letters and anything about your employment", "users",
     ["Leave and attendance", "Letters and documents", "Policies", "Onboarding and exit"]),
    ("Payroll", "Salary, payslips, deductions, tax and reimbursements", "wallet",
     ["Salary and payslips", "Income tax and Form 16", "PF and ESI", "Reimbursements"]),
    ("IT", "Laptop, accounts, email, software and access", "laptop",
     ["Laptop and hardware", "Accounts and access", "Software"]),
    ("Admin and facilities", "Seating, ID cards, transport, cafeteria and travel", "building", ["ID and access cards", "Travel", "Facilities"]),
    ("General questions", "Post anything that doesn't fit elsewhere", "help", []),
]

CANNED = [
    ("Acknowledge", "Hello {{requester.first_name}}, thanks for reaching out. We are looking into case {{case.reference_no}} and will update you shortly."),
    ("Need more details", "Hello {{requester.first_name}}, could you share a few more details (dates, screenshots or documents) so we can help faster?"),
    ("Resolved", "Hello {{requester.first_name}}, this is now sorted. If anything is still not right, reply here and we will reopen the case."),
    ("Payslip location", "You can download your payslips from My payslips in the app. Payslips are published on the last working day of the month."),
]

ARTICLES = [
    ("How do I download my payslip?", "Payroll", "Open the app, go to Payroll → My payslips and pick the month. Payslips for the last 24 months are available. Former employees can download theirs for 3 years after leaving.", ["payslip", "salary"]),
    ("When is salary credited?", "Payroll", "Salary is credited on the last working day of each month. If that day is a bank holiday, it is paid on the previous working day.", ["salary", "credit", "pay day"]),
    ("How do I get a salary certificate or address proof?", "HR", "Go to My documents → Request a letter and pick Salary certificate or Address proof. HR approves the request and the signed letter appears in My documents, usually within a day.", ["letter", "certificate", "address proof", "bank"]),
    ("How many leave days do I get?", "HR", "Your leave balance is shown under Time → Leave. Earned leave is credited monthly; casual and sick leave are credited at the start of the year as per the leave policy.", ["leave", "balance", "earned leave"]),
    ("I forgot my laptop password", "IT", "Raise a case under IT → Accounts and access. Keep your employee ID ready; IT will verify your identity and reset it within 4 working hours.", ["password", "laptop", "reset"]),
    ("How do I submit tax-saving investment proofs?", "Payroll", "Declare your investments at the start of the financial year under Payroll → Income tax. Upload proofs between January and February so that TDS can be adjusted before March payroll.", ["tax", "80C", "investment", "proof"]),
    ("Whom do I contact for a new ID card?", "Admin and facilities", "Raise a case under Admin and facilities → ID and access cards. A lost card is replaced within 2 working days; the first replacement is free.", ["id card", "access"]),
]


def setup_company(db: Session, company_id: int) -> None:
    from modules.helpdesk import service
    if not db.query(HelpdeskCategory.id).filter(HelpdeskCategory.company_id == company_id).first():
        for i, (name, desc, icon, subs) in enumerate(CATEGORIES):
            c = HelpdeskCategory(company_id=company_id, name=name, description=desc, icon=icon, sort_order=i,
                                 assignment_mode="pickup", feedback_mandatory=False)
            db.add(c)
            db.flush()
            for j, s in enumerate(subs):
                db.add(HelpdeskCategory(company_id=company_id, parent_id=c.id, name=s, sort_order=j))
        db.flush()
    if not db.query(HelpdeskSla.id).filter(HelpdeskSla.company_id == company_id).first():
        for p, (resp, res) in service.DEFAULT_SLAS.items():
            db.add(HelpdeskSla(company_id=company_id, priority=p, response_minutes=resp, resolution_minutes=res,
                               raise_priority=True))
    if not db.query(HelpdeskCannedResponse.id).filter(HelpdeskCannedResponse.company_id == company_id).first():
        for title, body in CANNED:
            db.add(HelpdeskCannedResponse(company_id=company_id, title=title, body=body, created_by_name="System"))
    if not db.query(HelpdeskArticle.id).filter(HelpdeskArticle.company_id == company_id).first():
        cats = {c.name: c.id for c in db.query(HelpdeskCategory).filter(HelpdeskCategory.company_id == company_id,
                                                                         HelpdeskCategory.parent_id.is_(None))}
        for title, cat, body, tags in ARTICLES:
            db.add(HelpdeskArticle(company_id=company_id, title=title, body=body, category_id=cats.get(cat), tags=tags,
                                   author_name="HR team"))
    db.flush()


DEMO_CASES = [
    # category, subcategory, subject, description, priority, status, hours ago, rating
    ("Payroll", "Salary and payslips", "October payslip shows wrong HRA", "My HRA in the October payslip is ₹2,000 less than last month. Nothing changed in my rent declaration.", "high", "in_progress", 30, None),
    ("HR", "Letters and documents", "Need an address proof letter for passport", "I am applying for a passport and need an address proof letter from the company.", "medium", "resolved", 70, 5),
    ("IT", "Laptop and hardware", "Laptop battery drains in an hour", "My laptop battery lasts barely an hour since last week. Could it be replaced?", "medium", "new", 3, None),
    ("IT", "Accounts and access", "Can't access the shared finance drive", "I moved to the finance team but still can't open the finance shared drive.", "high", "open", 9, None),
    ("Payroll", "Income tax and Form 16", "Form 16 for last year", "I could not find my Form 16 for FY 2025-26 in the app.", "low", "closed", 240, 4),
    ("Admin and facilities", "ID and access cards", "Lost my ID card", "I lost my ID card on the way home yesterday. Please block it and issue a new one.", "urgent", "waiting", 20, None),
    ("HR", "Leave and attendance", "Attendance marked absent on a holiday", "On 20 October (Diwali) my attendance shows absent although it is a company holiday.", "medium", "resolved", 50, 2),
    ("HR", "Policies", "Is work from home allowed on Fridays?", "Is there any policy on working from home on Fridays for the operations team?", "low", "new", 1, None),
    ("Payroll", "Reimbursements", "Travel claim not paid", "My travel claim from the Pune visit in September was approved but not paid with the October salary.", "high", "in_progress", 100, None),
]


def seed_demo(db: Session, company_id: int) -> None:
    from modules.employees.models import Employee
    from modules.helpdesk import service
    from modules.helpdesk.models import HelpdeskAgent
    setup_company(db, company_id)
    if db.query(HelpdeskCase.id).filter(HelpdeskCase.company_id == company_id).first():
        return
    emps = db.query(Employee).filter(Employee.company_id == company_id, Employee.active.is_(True)).order_by(Employee.id).all()
    if len(emps) < 2:
        return
    agent = emps[0]
    cats = {c.name: c for c in db.query(HelpdeskCategory).filter(HelpdeskCategory.company_id == company_id)}
    for name in ("HR", "Payroll", "IT", "Admin and facilities"):
        if name in cats and not cats[name].agents:
            db.add(HelpdeskAgent(company_id=company_id, category_id=cats[name].id, employee_id=agent.id))
    if "Payroll" in cats:
        cats["Payroll"].owner_employee_id = agent.id
        cats["Payroll"].escalate_to_employee_id = agent.id
    db.flush()
    from core import numbering
    now = service._now()
    requesters = [e for e in emps if e.id != agent.id] or emps
    for i, (cat, sub, subject, desc, priority, status, hours, rating) in enumerate(DEMO_CASES):
        created = now - timedelta(hours=hours)
        requester = requesters[i % len(requesters)]
        c = HelpdeskCase(company_id=company_id, reference_no=numbering.next_number(db, company_id, "ticket"),
                         category_id=cats[cat].id, subcategory_id=cats[sub].id if sub in cats else None,
                         subject=subject, description=desc, priority=priority, status=status, source="web",
                         requester_employee_id=requester.id, created_at=created, last_activity_at=created)
        db.add(c)
        db.flush()
        service.apply_sla(db, c, created)
        db.add(HelpdeskMessage(company_id=company_id, case_id=c.id, kind="event", author_name=service.display_name(requester),
                               body=f"Case raised by {service.display_name(requester)}.", created_at=created))
        if status != "new":
            c.assignee_employee_id = agent.id
            t = created + timedelta(minutes=40)
            c.first_response_at = t
            db.add(HelpdeskMessage(company_id=company_id, case_id=c.id, kind="reply", author_employee_id=agent.id,
                                   author_name=service.display_name(agent), created_at=t,
                                   body=f"Hello {requester.first_name}, thanks for reaching out. I'm looking into this."))
            db.add(HelpdeskMessage(company_id=company_id, case_id=c.id, kind="note", author_employee_id=agent.id,
                                   author_name=service.display_name(agent), created_at=t + timedelta(minutes=5),
                                   body="Checked the records; needs a quick confirmation from the team lead."))
            c.last_activity_at = t
        if status == "waiting":
            c.paused_at = now - timedelta(hours=2)
            db.add(HelpdeskMessage(company_id=company_id, case_id=c.id, kind="reply", author_employee_id=agent.id,
                                   author_name=service.display_name(agent), created_at=now - timedelta(hours=2),
                                   body="Could you confirm the card number printed on your old card, if you have it?"))
        if status in ("resolved", "closed"):
            c.resolved_at = created + timedelta(hours=min(hours - 1, 20))
            if status == "closed":
                c.closed_at = c.resolved_at + timedelta(hours=1)
            if rating:
                c.rating, c.rated_at = rating, c.resolved_at + timedelta(minutes=30)
                c.rating_comment = "Quick and clear, thank you." if rating >= 4 else "Took a while to get an answer."
    db.flush()
    service.check_sla(db, company_id)
