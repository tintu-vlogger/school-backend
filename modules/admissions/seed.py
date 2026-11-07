"""Admissions defaults for every institution (certificate templates) and demo data:
programs open for admission with seats, ~30 enquiries across the pipeline, ~20 applications
across statuses, and two certificates issued."""
import logging
import random
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

logger = logging.getLogger("hrms")

_SIGN = ('<table class="plain" style="margin-top:4em"><tr><td>Date: {{certificate.date}}<br/>Place: {{institution.city}}'
         '</td><td class="right">{{institution.principal_name}}<br/>Principal</td></tr></table>')

DEFAULT_TEMPLATES = [
    {
        "name": "Transfer certificate", "kind": "transfer", "title": "Transfer Certificate", "marks_student": "transferred",
        "fields": [
            {"key": "date_of_leaving", "label": "Date of leaving", "type": "date", "required": True, "default": ""},
            {"key": "reason", "label": "Reason for leaving", "type": "text", "required": True, "default": ""},
            {"key": "last_class", "label": "Class last studied", "type": "text", "required": False, "default": ""},
            {"key": "promotion", "label": "Qualified for promotion", "type": "text", "required": False, "default": "Yes"},
            {"key": "conduct", "label": "General conduct", "type": "text", "required": False, "default": "Good"},
            {"key": "dues", "label": "Dues paid up to", "type": "text", "required": False, "default": ""},
        ],
        "body_html": (
            '<h2 class="center" style="letter-spacing:0.08em">TRANSFER CERTIFICATE</h2>'
            '<table class="plain"><tr><td>TC No.: {{certificate.number}}</td>'
            '<td class="right">Admission No.: {{student.admission_no}}</td></tr></table>'
            '<table>'
            '<tr><td style="width:46%">1. Name of the student</td><td>{{student.full_name}}</td></tr>'
            "<tr><td>2. Father's name</td><td>{{student.father_name}}</td></tr>"
            "<tr><td>3. Mother's name</td><td>{{student.mother_name}}</td></tr>"
            '<tr><td>4. Nationality</td><td>{{student.nationality}}</td></tr>'
            '<tr><td>5. Category</td><td>{{student.category}}</td></tr>'
            '<tr><td>6. Date of birth (in figures)</td><td>{{student.date_of_birth}}</td></tr>'
            '<tr><td>7. Date of birth (in words)</td><td>{{student.date_of_birth_words}}</td></tr>'
            '<tr><td>8. Date of admission</td><td>{{student.admission_date}}</td></tr>'
            '<tr><td>9. Class in which the student last studied</td><td>{{certificate.last_class}} {{enrolment.class_label}}</td></tr>'
            '<tr><td>10. Whether qualified for promotion</td><td>{{certificate.promotion}}</td></tr>'
            '<tr><td>11. Dues paid up to</td><td>{{certificate.dues}}</td></tr>'
            '<tr><td>12. General conduct</td><td>{{certificate.conduct}}</td></tr>'
            '<tr><td>13. Date of leaving</td><td>{{certificate.date_of_leaving}}</td></tr>'
            '<tr><td>14. Reason for leaving</td><td>{{certificate.reason}}</td></tr>'
            '<tr><td>15. Date of issue of certificate</td><td>{{certificate.date}}</td></tr>'
            '</table>'
            '<table class="plain" style="margin-top:4em"><tr><td>Prepared by</td><td class="center">Checked by</td>'
            '<td class="right">{{institution.principal_name}}<br/>Principal</td></tr></table>'),
    },
    {
        "name": "Bonafide certificate", "kind": "bonafide", "title": "Bonafide Certificate",
        "fields": [{"key": "purpose", "label": "Purpose", "type": "text", "required": False,
                    "default": "applying for a scholarship"}],
        "body_html": (
            '<h2 class="center" style="letter-spacing:0.08em">BONAFIDE CERTIFICATE</h2>'
            '<p class="right">No. {{certificate.number}}</p>'
            '<p style="line-height:2">This is to certify that <b>{{student.full_name}}</b>, {{student.son_daughter}} of '
            '{{student.guardian_name}}, is a bonafide student of this institution, studying in '
            '<b>{{enrolment.class_label}}</b> in the academic year {{enrolment.session}}. '
            '{{student.his_her}} admission number is {{student.admission_no}} and {{student.his_her}} date of birth '
            'as per our records is {{student.date_of_birth}}.</p>'
            '<p style="line-height:2">This certificate is issued on request for the purpose of {{certificate.purpose}}.</p>'
            + _SIGN),
    },
    {
        "name": "Character certificate", "kind": "character", "title": "Character Certificate",
        "fields": [{"key": "conduct", "label": "Character and conduct", "type": "text", "required": False,
                    "default": "good"}],
        "body_html": (
            '<h2 class="center" style="letter-spacing:0.08em">CHARACTER CERTIFICATE</h2>'
            '<p class="right">No. {{certificate.number}}</p>'
            '<p style="line-height:2">This is to certify that <b>{{student.full_name}}</b> (admission number '
            '{{student.admission_no}}), {{student.son_daughter}} of {{student.guardian_name}}, has been a student of '
            'this institution since {{student.admission_date}}. During this period {{student.his_her}} character and '
            'conduct have been <b>{{certificate.conduct}}</b>.</p>'
            '<p style="line-height:2">We wish {{student.first_name}} every success in the future.</p>' + _SIGN),
    },
    {
        "name": "Study certificate", "kind": "study", "title": "Study Certificate",
        "fields": [{"key": "from_year", "label": "Studying since (year)", "type": "text", "required": False, "default": ""}],
        "body_html": (
            '<h2 class="center" style="letter-spacing:0.08em">STUDY CERTIFICATE</h2>'
            '<p class="right">No. {{certificate.number}}</p>'
            '<p style="line-height:2">This is to certify that <b>{{student.full_name}}</b>, {{student.son_daughter}} of '
            '{{student.guardian_name}}, is studying in <b>{{enrolment.class_label}}</b> of this institution during the '
            'academic year {{enrolment.session}}. {{student.his_her}} date of birth as per our admission register is '
            '{{student.date_of_birth}} ({{student.date_of_birth_words}}).</p>' + _SIGN),
    },
]


def setup_company(db: Session, company_id: int) -> None:
    """Default certificate templates, added once (never raises)."""
    try:
        from modules.admissions.models import CertificateTemplate
        if db.query(CertificateTemplate.id).filter(CertificateTemplate.company_id == company_id).first():
            return
        for t in DEFAULT_TEMPLATES:
            db.add(CertificateTemplate(company_id=company_id, **t))
        db.flush()
    except Exception as exc:  # pragma: no cover - set-up must never block sign-up
        logger.warning("Admissions set-up skipped for company %s: %s", company_id, exc)


CHILD_M = ["Advik", "Arnav", "Ishaan", "Kabir", "Reyansh", "Vihaan", "Aditya", "Yuvan", "Atharv", "Dhruv", "Shaurya",
           "Ayaan", "Rudra", "Neel", "Vivaan"]
CHILD_F = ["Aanya", "Myra", "Kiara", "Saanvi", "Ira", "Navya", "Avni", "Diya", "Tara", "Riya", "Meher", "Anvi",
           "Siya", "Zara", "Pihu"]
SURNAMES = ["Sharma", "Iyer", "Nair", "Reddy", "Patel", "Gupta", "Menon", "Khan", "Das", "Joshi", "Rao", "Shetty",
            "Hegde", "Bhat", "Kamath", "Pillai", "Mehta", "Bose"]
PARENT_M = ["Rahul", "Vikram", "Suresh", "Anand", "Karthik", "Manoj", "Sanjay", "Imran", "Prakash", "Nikhil"]
PARENT_F = ["Pooja", "Lakshmi", "Neha", "Divya", "Shruti", "Anjali", "Fatima", "Meena", "Swati", "Rekha"]
SCHOOLS = ["Little Flowers Playschool", "St. Mary's School", "Kidzee Jayanagar", "DPS Whitefield", "Vidya Niketan",
           "Euro Kids HSR", "Kendriya Vidyalaya", "National Public School"]
NOTES = ["Asked about the bus route from Koramangala.", "Wants to know the fee structure and instalments.",
         "Interested in the sports programme.", "Sibling already studies here.", "Moving from Pune in May.",
         "Asked about the CBSE curriculum and class size.", "Prefers the morning shift.",
         "Will visit with grandparents on Saturday."]


def seed_demo(db: Session, company_id: int) -> None:
    from modules.admissions import service
    from modules.admissions.models import AdmissionOffering, Application, Certificate, CertificateTemplate, Enquiry
    if db.query(Enquiry.id).filter(Enquiry.company_id == company_id).first():
        return
    # Students come from the students module's demo; make sure they exist first (it is idempotent).
    try:
        from modules.students.seed import seed_demo as students_demo
        students_demo(db, company_id)
        db.flush()
    except Exception as exc:  # pragma: no cover
        logger.warning("Students demo not available for admissions demo: %s", exc)
    from modules.academics import service as academics
    from modules.academics.models import Program, Section
    from modules.employees.models import Employee
    sess = academics.current_session(db, company_id)
    if sess is None:
        return
    programs = {p.name: p for p in db.query(Program).filter(Program.company_id == company_id).all()}
    if not programs:
        return
    setup_company(db, company_id)
    rng = random.Random(42)
    today = date.today()
    now = datetime.now(timezone.utc)

    # Programs open for admission (entry classes get more seats; tests from Grade 1 up)
    plan = [("Nursery", 12, "500", False, False, (2.5, 4.0)), ("LKG", 6, "500", False, False, (3.5, 5.0)),
            ("UKG", 4, "500", False, False, (4.5, 6.0)), ("Grade 1", 6, "750", True, True, (5.5, 7.0)),
            ("Grade 5", 3, "750", True, True, None), ("Grade 8", 3, "1000", True, True, None),
            ("Grade 11", 8, "1000", True, True, None)]
    offerings = {}
    for name, seats, fee, test, interview, age in plan:
        p = programs.get(name)
        if p is None:
            continue
        o = service.get_offering(db, company_id, sess.id, p.id)
        if o is None:
            o = AdmissionOffering(company_id=company_id, session_id=sess.id, program_id=p.id, seats=seats,
                                  application_fee=Decimal(fee), fee_required=False, open_online=True,
                                  closes_on=today + timedelta(days=75), test_required=test,
                                  interview_required=interview,
                                  min_age_years=Decimal(str(age[0])) if age else None,
                                  max_age_years=Decimal(str(age[1])) if age else None,
                                  age_as_on=date(sess.start_date.year, 6, 1) if age else None)
            db.add(o)
            db.flush()
        offerings[name] = o
    if not offerings:
        return
    db.refresh(sess)
    staff = (db.query(Employee).filter(Employee.company_id == company_id, Employee.active.is_(True),
                                       Employee.role.in_(("hr", "principal", "employee"))).order_by(Employee.id).limit(4).all())
    counsellors = [e.id for e in staff] or [None]
    sources = {code: service.source_id_by_code(db, company_id, code)
               for code in ("walk_in", "website", "phone", "campaign", "referral", "social")}
    src_cycle = ["walk_in", "website", "phone", "website", "referral", "campaign", "walk_in", "social", "phone", "website"]

    def child_dob(prog_name: str) -> date:
        base = {"Nursery": 3, "LKG": 4, "UKG": 5, "Grade 1": 6, "Grade 5": 10, "Grade 8": 13, "Grade 11": 16}[prog_name]
        return date(sess.start_date.year - base, rng.randint(1, 10), rng.randint(1, 28))

    def family():
        male = rng.random() < 0.5
        first = rng.choice(CHILD_M if male else CHILD_F)
        last = rng.choice(SURNAMES)
        dad, mum = rng.choice(PARENT_M), rng.choice(PARENT_F)
        phone = f"+91 9{rng.randint(100000000, 999999999)}"
        return male, first, last, dad, mum, phone

    names = list(offerings)
    # 30 enquiries across the pipeline
    stage_plan = ["new"] * 7 + ["contacted"] * 7 + ["visit_scheduled"] * 5 + ["applied"] * 5 + ["converted"] * 2 + ["lost"] * 4
    enquiries = []
    for i, stage in enumerate(stage_plan):
        male, first, last, dad, mum, phone = family()
        pname = names[i % len(names)]
        e = Enquiry(company_id=company_id, number=service._number(db, company_id, "enquiry", Enquiry, Enquiry.number, "ENQ"),
                    source_id=sources.get(src_cycle[i % len(src_cycle)]), session_id=sess.id,
                    program_id=offerings[pname].program_id, child_first_name=first, child_last_name=last,
                    child_dob=child_dob(pname), child_gender="male" if male else "female",
                    current_institution=rng.choice(SCHOOLS) if pname not in ("Nursery",) else None,
                    parent_name=f"{dad} {last}" if rng.random() < 0.6 else f"{mum} {last}",
                    parent_relation="father" if rng.random() < 0.6 else "mother", phone=phone,
                    email=f"{dad.lower()}.{last.lower()}{rng.randint(1, 99)}@example.com" if rng.random() < 0.7 else None,
                    city="Bengaluru", stage="new", counsellor_id=counsellors[i % len(counsellors)],
                    notes=rng.choice(NOTES), next_follow_up=today + timedelta(days=rng.randint(-3, 7)))
        e.created_at = now - timedelta(days=rng.randint(1, 40), hours=rng.randint(0, 9))
        db.add(e)
        db.flush()
        service.log(db, company_id, enquiry=e, kind="system", body="Enquiry added", to_value="new",
                    actor_name="Admissions office")
        if stage != "new":
            service.add_activity(db, company_id, e, "call", rng.choice(["Spoke to the parent; shared the fee details.",
                                                                       "Called back; parent wants a campus visit.",
                                                                       "Explained the admission process."]))
        if stage == "visit_scheduled":
            service.change_stage(db, company_id, e, "visit_scheduled",
                                 visit_at=datetime.combine(today + timedelta(days=rng.randint(1, 6)), time(4, 30),
                                                           tzinfo=timezone.utc))
        if stage == "lost":
            service.change_stage(db, company_id, e, "lost",
                                 lost_reason=rng.choice(["Fees too high", "Too far away", "Chose another institution"]))
        enquiries.append((e, stage))

    # Applications: from the "applied" and "converted" enquiries, plus direct and online ones
    def guardians_for(last, dad, mum, phone, email):
        return [{"relation": "father", "first_name": dad, "last_name": last, "phone": phone, "email": email,
                 "occupation": rng.choice(["Engineer", "Doctor", "Business owner", "Teacher", "Banker"]),
                 "is_primary": True},
                {"relation": "mother", "first_name": mum, "last_name": last,
                 "phone": f"+91 9{rng.randint(100000000, 999999999)}", "is_primary": False}]

    apps = []
    for e, stage in enquiries:
        if stage not in ("applied", "converted"):
            continue
        dad = (e.parent_name or "Parent").split(" ")[0]
        a = service.create_application(db, company_id, {
            "session_id": e.session_id, "program_id": e.program_id, "first_name": e.child_first_name,
            "last_name": e.child_last_name, "gender": e.child_gender, "date_of_birth": e.child_dob,
            "previous_institution": e.current_institution, "city": "Bengaluru", "state": "Karnataka",
            "nationality": "Indian", "contact_phone": e.phone, "contact_email": e.email},
            guardians=guardians_for(e.child_last_name, dad, rng.choice(PARENT_F), e.phone, e.email), enquiry=e)
        apps.append((a, stage))
    while len(apps) < 20:
        male, first, last, dad, mum, phone = family()
        pname = names[len(apps) % len(names)]
        email = f"{mum.lower()}.{last.lower()}{rng.randint(1, 99)}@example.com"
        a = service.create_application(db, company_id, {
            "session_id": sess.id, "program_id": offerings[pname].program_id, "first_name": first, "last_name": last,
            "gender": "male" if male else "female", "date_of_birth": child_dob(pname), "city": "Bengaluru",
            "state": "Karnataka", "nationality": "Indian", "contact_phone": phone, "contact_email": email,
            "previous_institution": rng.choice(SCHOOLS)},
            guardians=guardians_for(last, dad, mum, phone, email), channel="online" if len(apps) % 2 else "office")
        apps.append((a, None))
    for a, _ in apps:
        a.submitted_at = now - timedelta(days=rng.randint(1, 30))
        if rng.random() < 0.7:
            service.set_fee(db, company_id, a, True, today - timedelta(days=rng.randint(0, 20)),
                            f"RCPT-{rng.randint(1000, 9999)}")
    # Move them along: a realistic spread of statuses
    plan_status = ["under_review", "under_review", "test_scheduled", "test_scheduled", "interview", "interview",
                   "shortlisted", "shortlisted", "shortlisted", "offered", "offered", "accepted", "waitlisted",
                   "rejected", "withdrawn", "submitted", "submitted", "under_review", "shortlisted", "submitted"]
    for idx, (a, stage) in enumerate(apps):
        target = "enrolled" if stage == "converted" else plan_status[idx % len(plan_status)]
        if stage == "applied" and target in ("rejected", "withdrawn"):
            target = "under_review"
        if target in ("submitted",):
            continue
        service.change_status(db, company_id, a, "under_review", system=True)
        if target in ("test_scheduled", "interview", "shortlisted", "offered", "accepted", "enrolled", "waitlisted"):
            service.schedule(db, company_id, a, {"test_at": now + timedelta(days=rng.randint(-10, 8)),
                                                 "test_venue": "Main block, Room 12"})
            if target != "test_scheduled":
                service.save_scores(db, company_id, a, {"test_score": Decimal(rng.randint(48, 98)), "test_max": Decimal(100)})
        if target in ("interview", "shortlisted", "offered", "accepted", "enrolled", "waitlisted"):
            service.schedule(db, company_id, a, {"interview_at": now + timedelta(days=rng.randint(-6, 6)),
                                                 "interviewer_id": counsellors[0]})
            if target != "interview":
                service.save_scores(db, company_id, a, {"interview_score": Decimal(rng.randint(5, 10)),
                                                        "interview_max": Decimal(10)})
        if target in ("shortlisted", "waitlisted"):
            service.change_status(db, company_id, a, target, system=True)
        if target in ("rejected", "withdrawn"):
            service.change_status(db, company_id, a, target, reason="Did not meet the criteria" if target == "rejected"
                                  else "Family relocated", system=True)
        if target in ("offered", "accepted", "enrolled"):
            service.change_status(db, company_id, a, "shortlisted", system=True)
            if not a.fee_paid:
                service.set_fee(db, company_id, a, True, today, "RCPT-DEMO")
            try:
                service.offer(db, company_id, a, note="Please bring the original documents for verification.")
            except Exception as exc:
                logger.info("Demo offer skipped: %s", getattr(exc, "detail", exc))
                continue
        if target in ("accepted", "enrolled") and a.status == "offered":
            service.accept(db, company_id, a)
        if target == "enrolled" and a.status in ("offered", "accepted"):
            sec = (db.query(Section).filter(Section.company_id == company_id, Section.program_id == a.program_id,
                                            Section.session_id == a.session_id).order_by(Section.name).first())
            if sec is not None:
                try:
                    service.enrol(db, company_id, a, section_id=sec.id)
                except Exception as exc:
                    logger.info("Demo enrolment skipped: %s", getattr(exc, "detail", exc))
    for o in offerings.values():
        service.rank(db, company_id, o.session_id, o.program_id)

    # Two certificates issued once
    if not db.query(Certificate.id).filter(Certificate.company_id == company_id).first():
        from modules.students.models import Student
        students = (db.query(Student).filter(Student.company_id == company_id, Student.status == "active")
                    .order_by(Student.id).limit(30).all())
        bona = db.query(CertificateTemplate).filter(CertificateTemplate.company_id == company_id,
                                                    CertificateTemplate.kind == "bonafide").first()
        char = db.query(CertificateTemplate).filter(CertificateTemplate.company_id == company_id,
                                                    CertificateTemplate.kind == "character").first()
        aarav = next((s for s in students if s.first_name == "Aarav" and s.last_name == "Kumar"), None)
        if bona and (aarav or students):
            service.issue_certificate(db, company_id, bona, aarav or students[0],
                                      values={"purpose": "applying for a passport"}, issued_on=today - timedelta(days=6))
        if char and len(students) > 3:
            service.issue_certificate(db, company_id, char, students[3], values={"conduct": "very good"},
                                      issued_on=today - timedelta(days=2))
    db.flush()
