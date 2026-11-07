"""Wellbeing defaults (incident types) and demo data: health records, a check-up camp, a few
infirmary visits, discipline incidents, merit points across houses, today's visitor log and a
gate pass."""
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session


def setup_company(db: Session, company_id: int) -> None:
    try:
        from modules.wellbeing import service
        with db.begin_nested():
            service.ensure_defaults(db, company_id)
    except Exception:
        pass


def seed_demo(db: Session, company_id: int) -> None:
    from modules.employees.models import Employee
    from modules.students.models import Student, StudentGuardian
    from modules.wellbeing import service
    from modules.wellbeing.models import (WbCheckup, WbCheckupCamp, WbGatePass, WbHealthRecord, WbIncident,
                                          WbIncidentType, WbInfirmaryVisit, WbPoint, WbVisitor)
    if db.query(WbHealthRecord.id).filter(WbHealthRecord.company_id == company_id).first():
        return
    from modules.library.seed import _foundation
    _foundation(db, company_id)
    students = db.query(Student).filter(Student.company_id == company_id, Student.status == "active").order_by(Student.id).all()
    if not students:
        return
    service.ensure_defaults(db, company_id)
    rng = random.Random(41)
    on = service.today(db, company_id)
    now = datetime.now(timezone.utc)
    aarav = next((s for s in students if s.email == "student@test.com"), None)
    if aarav is not None and not aarav.allergies:
        aarav.allergies = "Peanuts"
    camp = WbCheckupCamp(company_id=company_id, name="Annual health check-up", kind="general", held_on=on - timedelta(days=40),
                         doctor="Dr. Anand Rao", agency="Manipal Clinics")
    db.add(camp)
    db.flush()
    for s in students:
        age = max(3, (on - s.date_of_birth).days // 365) if s.date_of_birth else 10
        h = Decimal(str(round(95 + age * 5.8 + rng.uniform(-6, 6), 1)))
        w = Decimal(str(round(14 + age * 2.6 + rng.uniform(-4, 6), 1)))
        vis = rng.choice(["6/6", "6/6", "6/6", "6/9", "6/12"])
        rec = WbHealthRecord(company_id=company_id, student_id=s.id, height_cm=h, weight_kg=w, measured_on=camp.held_on,
                             vision_left=vis, vision_right=rng.choice([vis, "6/6"]),
                             dental=rng.choice(["Healthy", "Healthy", "Cavity in lower molar", "Braces"]),
                             immunisations=[{"vaccine": "MMR", "dose": "2", "date": None, "notes": None},
                                            {"vaccine": "DPT booster", "dose": "2", "date": None, "notes": None}],
                             chronic_conditions="Mild asthma" if rng.random() < 0.05 else None,
                             doctor_name="Dr. Anand Rao" if rng.random() < 0.3 else None)
        db.add(rec)
        db.add(WbCheckup(company_id=company_id, student_id=s.id, camp_id=camp.id, checked_on=camp.held_on, height_cm=h,
                         weight_kg=w, vision_left=rec.vision_left, vision_right=rec.vision_right, dental=rec.dental,
                         examined_by="Dr. Anand Rao",
                         recommendations="Eye test recommended" if vis in ("6/9", "6/12") else None))
    complaints = [("Headache", "Rest in the infirmary for 30 minutes", "Paracetamol 250 mg", False),
                  ("Fever (100.4 F)", "Cold compress, observed", "Paracetamol 500 mg", True),
                  ("Scraped knee during PE", "Cleaned and dressed", None, False),
                  ("Stomach ache", "Rest, warm water", None, False),
                  ("Nosebleed", "Pressure applied, stopped in 5 minutes", None, False)]
    for k in range(8):
        s = aarav if (k == 0 and aarav) else rng.choice(students)
        c = complaints[k % len(complaints)]
        when = now - timedelta(days=rng.randint(0, 25), hours=rng.randint(0, 5)) if k else now - timedelta(days=9)
        db.add(WbInfirmaryVisit(company_id=company_id, student_id=s.id, visit_at=when, complaint=c[0], treatment=c[1],
                                medicine_given=c[2], sent_home=c[3], parent_informed=c[3] or k % 2 == 0,
                                temperature=Decimal("38.0") if c[3] else None, attended_by="School nurse"))
    types = {t.name: t for t in db.query(WbIncidentType).filter(WbIncidentType.company_id == company_id).all()}
    teachers = db.query(Employee).filter(Employee.company_id == company_id, Employee.role == "teacher").all()
    for k, (tname, sev, desc, action) in enumerate([
            ("Late to class", "low", "Arrived 15 minutes late to the first period three times this week.", "Spoken to; note in diary"),
            ("Use of mobile phone", "medium", "Phone used during the maths test.", "Phone kept with the office till the end of the day"),
            ("Disrupting class", "medium", "Repeated talking and distracting others in science.", "Moved seat; parents called"),
            ("Fighting", "high", "Scuffle on the playground during lunch break.", "Both students counselled; parents met the principal"),
            ("Uniform not as per rules", "low", "No ID card and wrong shoes.", "Reminder given"),
            ("Damage to property", "high", "Broke a lab beaker while playing.", "Asked to pay for replacement")]):
        s = rng.choice(students)
        t = types.get(tname)
        rep = rng.choice(teachers) if teachers else None
        i = WbIncident(company_id=company_id, student_id=s.id, type_id=t.id if t else None,
                       occurred_at=now - timedelta(days=rng.randint(1, 40)), location=rng.choice(["Classroom", "Playground", "Lab", "Corridor"]),
                       severity=sev, description=desc, action_taken=action, status="closed" if k % 3 == 0 else "open",
                       follow_up_on=on + timedelta(days=rng.randint(-2, 10)) if sev != "low" else None,
                       parent_informed=sev != "low", confidential=tname == "Fighting",
                       reported_by=f"{rep.first_name} {rep.last_name}" if rep else "Office", reported_by_id=rep.id if rep else None)
        db.add(i)
        db.flush()
        if t and t.default_points:
            db.add(WbPoint(company_id=company_id, student_id=s.id, points=t.default_points, reason=tname, category="Conduct",
                           awarded_on=i.occurred_at.date(), awarded_by=i.reported_by, incident_id=i.id))
    merits = [("Won the inter-school quiz", "Academics", 10), ("Helped organise the science fair", "Leadership", 5),
              ("Kept the classroom tidy all week", "Service", 2), ("100 m sprint gold at sports day", "Sports", 8),
              ("Excellent art exhibition entry", "Arts", 5), ("Helped a classmate with lessons", "Conduct", 3),
              ("Perfect attendance this month", "Conduct", 2)]
    for _ in range(60):
        s = rng.choice(students)
        reason, cat, pts = rng.choice(merits)
        db.add(WbPoint(company_id=company_id, student_id=s.id, points=pts, reason=reason, category=cat,
                       awarded_on=on - timedelta(days=rng.randint(0, 60)), awarded_by="Class teacher"))
    if aarav is not None:
        db.add(WbPoint(company_id=company_id, student_id=aarav.id, points=5, reason="Built the class robotics model",
                       category="Academics", awarded_on=on - timedelta(days=3), awarded_by="Divya Menon"))
    # Today's gate log.
    staff = db.query(Employee).filter(Employee.company_id == company_id, Employee.active.is_(True)).limit(10).all()
    day_start = now.replace(hour=3, minute=0, second=0, microsecond=0)
    for k, (name, purpose, comp) in enumerate([
            ("Rakesh Sharma", "Parent-teacher meeting", None), ("Priya Nair", "Admission enquiry", None),
            ("Mohan Kumar", "Deliver lab equipment", "Scientific Traders"), ("Sunita Rao", "Fee payment", None),
            ("Arjun Mehta", "Water purifier service", "AquaPure Services"), ("Neha Gupta", "Meet the principal", None)]):
        e = staff[k % len(staff)] if staff else None
        v = WbVisitor(company_id=company_id, pass_no=service.new_pass_no(db, company_id, "visitor_pass", "VP"), name=name,
                      phone=f"+91 9{rng.randint(100000000, 999999999)}", company_name=comp, purpose=purpose,
                      employee_id=e.id if e else None, person_to_meet=f"{e.first_name} {e.last_name}" if e else None,
                      id_kind=rng.choice(["Aadhaar", "Driving licence", "Company ID"]), id_number=f"XXXX{rng.randint(1000, 9999)}",
                      in_at=day_start + timedelta(minutes=40 * k + 15), logged_by="Security desk",
                      out_at=None if k >= 4 else day_start + timedelta(minutes=40 * k + 55))
        db.add(v)
    link = None
    for s in students[5:]:
        link = db.query(StudentGuardian).filter(StudentGuardian.student_id == s.id, StudentGuardian.can_pickup.is_(True)).first()
        if link and s.email != "student@test.com":
            break
    if link:
        db.add(WbGatePass(company_id=company_id, student_id=link.student_id, guardian_id=link.guardian_id,
                          pass_no=service.new_pass_no(db, company_id, "gate_pass", "GP"), reason="Doctor's appointment",
                          out_at=day_start + timedelta(hours=3), issued_by="Front office"))
    db.flush()
