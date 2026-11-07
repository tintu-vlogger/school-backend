"""Demo tasks for the development company."""
from datetime import timedelta

from sqlalchemy.orm import Session

from modules.tasks.models import Task

DEMO = [
    # title, assignee index, creator index, priority, due in days, status, checklist, labels, recurrence
    ("Collect investment proofs for FY 2026-27", 1, 0, "high", 5, "in_progress", ["Send reminder email", "Verify 80C proofs", "Update TDS"], ["payroll"], None),
    ("Prepare October attendance summary", 0, None, "medium", -2, "todo", [], ["attendance"], None),
    ("Update the leave policy for 2027", 0, None, "low", 21, "todo", ["Draft changes", "Legal review", "Publish"], ["policy"], None),
    ("Weekly payroll inputs check", 0, None, "medium", 2, "todo", [], ["payroll"], {"freq": "weekly", "interval": 1}),
    ("Book venue for the POSH workshop", 1, 0, "medium", 9, "todo", [], ["posh"], None),
    ("Issue ID card to new joiner", 1, 0, "urgent", 0, "in_progress", ["Click photo", "Print card", "Activate access"], ["onboarding"], None),
    ("Renew group health insurance", 0, None, "high", 30, "todo", ["Collect quotes", "Compare cover", "Approval"], ["benefits"], None),
    ("Send monthly headcount report", 0, None, "low", -9, "done", [], ["reports"], {"freq": "monthly", "interval": 1}),
    ("Exit interview with leaving employee", 1, 0, "medium", 1, "todo", [], ["exit"], None),
    ("Verify bank details of new joiners", 0, None, "high", 3, "in_progress", ["Collect cancelled cheques", "Penny-drop check"], ["onboarding"], None),
    ("Plan Diwali celebration", 1, 0, "low", -15, "done", ["Budget", "Vendors", "Invites"], ["engagement"], None),
    ("Quarterly asset audit", 0, None, "medium", 12, "todo", ["Laptops", "Phones", "ID cards"], ["assets"], {"freq": "monthly", "interval": 3}),
]


def seed_demo(db: Session, company_id: int) -> None:
    from modules.employees.models import Employee
    from modules.tasks import service
    if db.query(Task.id).filter(Task.company_id == company_id).first():
        return
    emps = db.query(Employee).filter(Employee.company_id == company_id, Employee.active.is_(True)).order_by(Employee.id).all()
    if not emps:
        return
    today = service.today_for_company_id(db, company_id)
    for title, a, c, priority, due, status, checklist, labels, rec in DEMO:
        assignee = emps[a % len(emps)]
        creator = emps[c % len(emps)] if c is not None else None
        t = service.create_task(db, company_id, title=title, assignee_employee_id=assignee.id, priority=priority,
                                due_date=today + timedelta(days=due), checklist=checklist, labels=labels, recurrence=rec,
                                created_by_employee_id=creator.id if creator else None,
                                created_by_name=service.names_for(db, company_id, [creator.id])[creator.id] if creator else "Tintu",
                                description="Created as sample data.")
        if status != "todo":
            t.status = status
            if status == "done":
                t.completed_at = service._now() - timedelta(days=1)
            for i, item in enumerate(t.checklist):
                item.done = status == "done" or i == 0
    db.flush()
