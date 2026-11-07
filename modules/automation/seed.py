"""Example rules every company gets, switched off, so HR can see how rules look and turn
them on."""
from sqlalchemy.orm import Session

from modules.automation.models import AutomationRule

EXAMPLES = [
    {"name": "Remind the manager before probation ends",
     "description": "Seven days before someone's probation ends, their manager is reminded to decide on confirmation.",
     "entity": "employee", "trigger": "date", "date_field": "probation_end_date", "offset_days": -7,
     "condition": "employee.status == 'probation'",
     "actions": [{"type": "notify", "to": ["manager"], "title": "Probation of {{employee.name}} ends in 7 days",
                  "body": "Please confirm, extend or end the probation of {{employee.name}} by "
                          "{{record.probation_end_date}}.", "link": "/dashboard/probation"}]},
    {"name": "Tell HR about documents that are about to expire",
     "description": "Thirty days before an employee document expires, HR gets an email.",
     "entity": "employee_document", "trigger": "date", "date_field": "expiry_date", "offset_days": -30,
     "actions": [{"type": "email", "to": ["role:hr"], "addresses": [],
                  "subject": "{{record.title}} of {{employee.name}} expires on {{record.expiry_date}}",
                  "body": "Hello,\n\n{{record.title}} ({{record.document_number}}) of {{employee.name}} expires on "
                          "{{record.expiry_date}}. Please ask for a renewed copy.\n"}]},
    {"name": "Birthday wishes",
     "description": "On each birthday the person gets a short note.",
     "entity": "employee", "trigger": "date", "date_field": "date_of_birth", "offset_days": 0, "repeat_yearly": True,
     "actions": [{"type": "notify", "to": ["employee"], "title": "Happy birthday, {{employee.first_name}}!",
                  "body": "Everyone at the company wishes you a great year ahead."}]},
]


def setup_company(db: Session, company_id: int) -> None:
    have = {n for (n,) in db.query(AutomationRule.name).filter(AutomationRule.company_id == company_id).all()}
    if have:
        return  # examples are added once; companies may archive or rename them
    for ex in EXAMPLES:
        db.add(AutomationRule(company_id=company_id, name=ex["name"], description=ex["description"],
                              entity=ex["entity"], trigger=ex["trigger"], date_field=ex.get("date_field"),
                              offset_days=ex.get("offset_days", 0), repeat_yearly=ex.get("repeat_yearly", False),
                              condition=ex.get("condition"), actions=ex["actions"], fields=[], active=False,
                              created_by_name="System"))
    db.flush()
