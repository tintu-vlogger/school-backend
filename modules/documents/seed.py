"""Defaults for every company (folders, header and footer designs, a template for every
kind of letter) and demo data for the development company."""
from datetime import timedelta


from sqlalchemy.orm import Session

from modules.documents.models import DocFile, Letter, Letterhead, LetterTemplate, Policy, PolicyAcknowledgement


def setup_company(db: Session, company_id: int) -> None:
    from modules.documents import defaults, service
    service.ensure_system_folders(db, company_id)
    if not db.query(Letterhead.id).filter(Letterhead.company_id == company_id).first():
        for kind, items in (("header", defaults.HEADERS), ("footer", defaults.FOOTERS)):
            for name, height, is_default, html in items:
                db.add(Letterhead(company_id=company_id, kind=kind, name=name, html=html, height_mm=height,
                                  is_default=is_default))
        db.flush()
    have = {t for (t,) in db.query(LetterTemplate.letter_type).filter(LetterTemplate.company_id == company_id).distinct()}
    for letter_type, (name, subject, body, extra, self_service, esign) in defaults.TEMPLATES.items():
        if letter_type in have:
            continue
        db.add(LetterTemplate(company_id=company_id, name=name, letter_type=letter_type, subject=subject,
                              body_html=body, extra_fields=extra, self_service=self_service, esign_required=esign,
                              signers=[{"kind": "employee", "label": "Employee"}] if esign else [],
                              is_default=True, description=service.LETTER_TYPES.get(letter_type)))
    db.flush()


POLICIES = [
    ("Prevention of Sexual Harassment (POSH) policy", "POSH", True, """<h2>Prevention of Sexual Harassment at the Workplace</h2>
<p>The company is committed to a workplace free of sexual harassment, as required by the Sexual Harassment of Women at Workplace
(Prevention, Prohibition and Redressal) Act, 2013.</p>
<h3>What is sexual harassment</h3><p>Unwelcome physical contact or advances, a demand or request for sexual favours, sexually
coloured remarks, showing pornography, or any other unwelcome physical, verbal or non-verbal conduct of a sexual nature.</p>
<h3>Internal Committee</h3><p>Complaints are heard by the Internal Committee, presided over by a senior woman employee and including
an external member from an NGO. Only committee members can see complaints.</p>
<h3>How to complain</h3><p>Raise a complaint under Grievances → POSH in the app within three months of the incident. The committee
completes its inquiry within 90 days and gives its report within 10 days after that.</p>
<h3>Confidentiality and protection</h3><p>The identity of the complainant, the respondent and witnesses is kept confidential.
Retaliation against anyone who complains or helps an inquiry is misconduct.</p>"""),
    ("Code of conduct", "Code of conduct", False, """<h2>Code of conduct</h2><p>Act with integrity, treat colleagues with respect,
protect company information and avoid conflicts of interest. Report concerns through the help desk or the grievance channel.</p>"""),
    ("IT and acceptable use policy", "IT and security", False, """<h2>IT and acceptable use</h2><p>Use company devices and accounts
for work, keep passwords private, lock your screen when away and report lost devices to IT at once.</p>"""),
    ("Leave and attendance policy", "Leave and attendance", False, """<h2>Leave and attendance</h2><p>Apply for leave in advance
through the app. Working hours, weekly offs and holidays follow the company's standing orders and the state Shops and
Establishments Act.</p>"""),
]


def seed_demo(db: Session, company_id: int) -> None:
    from core.masters import get_type, values_query
    from modules.documents import service
    from modules.employees.models import Employee

    setup_company(db, company_id)
    employees = (db.query(Employee).filter(Employee.company_id == company_id, Employee.active.is_(True))
                 .order_by(Employee.id).all())
    if not db.query(Policy.id).filter(Policy.company_id == company_id).first():
        try:
            cats = {v.label: v.id for v in values_query(db, company_id, get_type(db, company_id, "policy_category")).all()}
        except Exception:
            cats = {}
        today = service._today(db, company_id)
        for i, (title, cat, is_posh, body) in enumerate(POLICIES):
            p = service.save_policy(db, company_id, {"title": title, "category_id": cats.get(cat), "requires_ack": True,
                                                    "is_posh": is_posh, "ack_due_days": 14 if i else 7,
                                                    "description": body.split("</h2>")[-1][:200].replace("<p>", "").split("</p>")[0],
                                                    "review_on": today + timedelta(days=330 - i * 100),
                                                    "owner_name": "HR team", "audience": {"all": True}})
            service.publish_version(db, company_id, p, blob=None, body_html=body, effective_from=today - timedelta(days=20),
                                    change_note="First version")
        db.flush()
        # most people have acknowledged; a few are pending and one is overdue
        acks = db.query(PolicyAcknowledgement).filter(PolicyAcknowledgement.company_id == company_id).order_by(
            PolicyAcknowledgement.id).all()
        for n, a in enumerate(acks):
            if n % 3 != 2:
                a.acknowledged_at = service._now() - timedelta(days=n % 9)
            elif n % 6 == 5:
                a.due_on = today - timedelta(days=2)
    if not db.query(DocFile.id).filter(DocFile.company_id == company_id, DocFile.source == "upload").first():
        from modules.documents import pdf
        folder = service.system_folder(db, company_id, "company", "Handbooks and guides")
        for title, body in (("Employee handbook 2026", "<h1>Employee handbook</h1><p>Welcome to the team. This handbook explains how we work.</p>"),
                            ("Holiday list 2026", "<h1>Holiday list 2026</h1><table><tr><th>Date</th><th>Holiday</th></tr>"
                             "<tr><td>26 January</td><td>Republic Day</td></tr><tr><td>15 August</td><td>Independence Day</td></tr>"
                             "<tr><td>2 October</td><td>Gandhi Jayanti</td></tr><tr><td>20 October</td><td>Diwali</td></tr></table>")):
            blob = service.store_file(db, company_id, f"{title}.pdf", pdf.render_pdf(pdf.pdf_html(
                body, header=None, footer=None, header_mm=0, footer_mm=0, title=title)), "application/pdf", "file")
            db.add(DocFile(company_id=company_id, folder_id=folder.id, title=title, stored_file_id=blob.id,
                           audience={"all": True}, uploaded_by_name="HR team", source="upload"))
        db.flush()
    if employees and not db.query(Letter.id).filter(Letter.company_id == company_id).first():
        for letter_type, emp, values in (
            ("appointment", employees[0], {"salary.ctc": "960000", "job.probation_months": "6"}),
            ("confirmation", employees[-1], {}),
            ("salary_certificate", employees[-1], {"salary.ctc": "840000", "salary.gross_monthly": "64000",
                                                   "letter.reason": "Bank loan"}),
        ):
            t = service.default_template(db, company_id, letter_type)
            if t is not None:
                service.generate_letter(db, company_id, t, emp, values)
    db.flush()
