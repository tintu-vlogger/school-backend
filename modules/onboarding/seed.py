"""Default onboarding flow for every company: pre-joining and day-one checklists for HR,
IT, admin, the manager and the buddy, the portal sections, required documents and an
induction day."""
from sqlalchemy.orm import Session

from modules.onboarding.models import OnboardingFlow

TASKS = [
    # title, owner, stage, due days from joining, checklist
    ("Verify the joiner's details and documents", "hr", "pre_joining", -3, ["Identity", "Address", "Education", "Bank"]),
    ("Issue the appointment letter", "hr", "pre_joining", -1, []),
    ("Create work email and system accounts", "it", "pre_joining", -2, ["Email", "HRMS login", "Chat and tools"]),
    ("Prepare laptop", "it", "pre_joining", -1, ["Image the laptop", "Install software", "Asset tag"]),
    ("Print ID card and set up access card", "admin", "pre_joining", -1, []),
    ("Plan the first week and the first goals", "manager", "pre_joining", -2, []),
    ("Welcome the joiner and take them around", "buddy", "day_one", 0, ["Office tour", "Lunch together"]),
    ("Collect signed joining forms (PF, ESI, gratuity)", "hr", "day_one", 1, []),
    ("Set 30-day goals with the joiner", "manager", "day_one", 7, []),
]
DOCS = [("PAN card", True), ("Aadhaar card", True), ("Passport-size photograph", True), ("Highest degree certificate", True),
        ("Relieving letter from the last employer", False), ("Last three payslips", False), ("Cancelled cheque", True)]
INDUCTION = [
    {"title": "Welcome and company overview", "day": 1, "time": "10:00", "duration_minutes": 60, "where": "Board room"},
    {"title": "HR policies, leave and payroll", "day": 1, "time": "14:00", "duration_minutes": 45, "where": "HR room"},
    {"title": "POSH awareness and code of conduct", "day": 2, "time": "11:00", "duration_minutes": 45, "where": "Online"},
    {"title": "IT and information security", "day": 2, "time": "15:00", "duration_minutes": 30, "where": "Online"},
]


def setup_company(db: Session, company_id: int) -> None:
    if db.query(OnboardingFlow.id).filter(OnboardingFlow.company_id == company_id).first():
        return
    db.add(OnboardingFlow(
        company_id=company_id, name="Standard onboarding", description="For everyone unless a more specific flow applies.",
        priority=100, audience={},
        tasks=[{"title": t, "owner": o, "employee_id": None, "stage": s, "due_days": d, "checklist": cl, "kind": None}
               for t, o, s, d, cl in TASKS],
        sections=["personal", "contact", "family", "nominees", "bank", "statutory", "previous_employment", "tax",
                  "education", "emergency", "documents"],
        documents=[{"label": label, "required": req} for label, req in DOCS], induction=INDUCTION,
        statutory_forms=["pf_form2", "pf_form11", "esi_declaration", "gratuity_form_f"]))
    db.add(OnboardingFlow(
        company_id=company_id, name="Interns and trainees", description="Shorter flow without previous employment.",
        priority=50, audience={}, active=False,
        tasks=[{"title": t, "owner": o, "employee_id": None, "stage": s, "due_days": d, "checklist": cl, "kind": None}
               for t, o, s, d, cl in TASKS if o != "admin"],
        sections=["personal", "contact", "family", "bank", "statutory", "education", "emergency", "documents"],
        documents=[{"label": "Aadhaar card", "required": True}, {"label": "College ID card", "required": True}],
        induction=INDUCTION[:2], statutory_forms=["pf_form11"]))
    db.flush()
