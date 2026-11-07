"""Demo learners: a few students per section with parents, and known portal logins:
parent  parent@test.com / Welcome9#   (two children)
student student@test.com / Welcome9#
"""
import random
from datetime import date, timedelta

from sqlalchemy.orm import Session

FIRST_M = ["Aarav", "Vivaan", "Aditya", "Vihaan", "Arjun", "Sai", "Reyansh", "Ayaan", "Krishna", "Ishaan", "Shaurya",
           "Atharv", "Kabir", "Rudra", "Dhruv", "Arnav", "Rohan", "Yash", "Dev", "Kian"]
FIRST_F = ["Aadhya", "Ananya", "Diya", "Saanvi", "Pari", "Myra", "Anika", "Navya", "Kiara", "Ira", "Riya", "Aarohi",
           "Meera", "Siya", "Tara", "Avni", "Kavya", "Prisha", "Sara", "Zoya"]
LAST = ["Sharma", "Iyer", "Nair", "Reddy", "Patel", "Gupta", "Menon", "Khan", "Das", "Joshi", "Kulkarni", "Rao",
        "Singh", "Verma", "Pillai", "Bose", "Mehta", "Chopra", "Agarwal", "Fernandes"]
JOBS = ["Software engineer", "Doctor", "Teacher", "Business owner", "Bank manager", "Architect", "Lawyer", "Accountant",
        "Civil servant", "Pharmacist"]
PER_SECTION = 6


def seed_demo(db: Session, company_id: int) -> None:
    from core.security import hash_password
    from modules.academics.models import House, Section
    from modules.students import service
    from modules.students.models import Guardian, PortalAccount, Student, StudentGuardian
    if db.query(Student.id).filter(Student.company_id == company_id).first():
        return
    rng = random.Random(7)
    sess_ids = [s.id for s in [service.academics.current_session(db, company_id)] if s]
    if not sess_ids:
        return
    sections = (db.query(Section).filter(Section.company_id == company_id, Section.session_id == sess_ids[0])
                .order_by(Section.program_id, Section.name).all())
    houses = [h.id for h in db.query(House).filter(House.company_id == company_id).all()]
    today = date.today()
    first_parent = None
    for si, sec in enumerate(sections):
        grade_no = si // 2  # 0 = Nursery
        for k in range(PER_SECTION):
            male = rng.random() < 0.5
            last = rng.choice(LAST)
            age = 3 + grade_no
            s = Student(company_id=company_id, admission_no=service.new_admission_no(db, company_id),
                        first_name=rng.choice(FIRST_M if male else FIRST_F), last_name=last,
                        gender="male" if male else "female",
                        date_of_birth=date(today.year - age, rng.randint(1, 12), rng.randint(1, 28)),
                        blood_group=rng.choice(["A+", "B+", "O+", "AB+", "O-"]), city="Bengaluru", state="Karnataka",
                        country="India", nationality="Indian", house_id=rng.choice(houses) if houses else None,
                        admission_date=today - timedelta(days=rng.randint(30, 365 * max(1, grade_no))),
                        uses_transport=rng.random() < 0.4, address=f"#{rng.randint(1, 300)}, {rng.choice(['Indiranagar', 'Koramangala', 'Jayanagar', 'HSR Layout', 'Whitefield'])}")
            db.add(s)
            db.flush()
            service.enroll(db, company_id, s, sec.id)
            family = sec.program.name in ("Grade 8", "Grade 3") and sec.name == "A" and k == 0
            if family:
                s.last_name = "Kumar"
                s.first_name = "Aarav" if sec.program.name == "Grade 8" else "Anika"
                s.gender = "male" if sec.program.name == "Grade 8" else "female"
                last = "Kumar"
            if family and first_parent is not None:
                father = first_parent
            else:
                father = Guardian(company_id=company_id, first_name=rng.choice(FIRST_M), last_name=last,
                                  phone=f"+91 9{rng.randint(100000000, 999999999)}", occupation=rng.choice(JOBS))
                db.add(father)
            mother = Guardian(company_id=company_id, first_name=rng.choice(FIRST_F), last_name=last,
                              phone=f"+91 9{rng.randint(100000000, 999999999)}", occupation=rng.choice(JOBS))
            db.add(mother)
            db.flush()
            db.add(StudentGuardian(company_id=company_id, student_id=s.id, guardian_id=father.id, relation="father",
                                   is_primary=True))
            db.add(StudentGuardian(company_id=company_id, student_id=s.id, guardian_id=mother.id, relation="mother"))
            # Demo family: the first learners of Grade 3 - A and Grade 8 - A share a parent with a login.
            if family and first_parent is None:
                father.first_name, father.last_name, father.email = "Rajesh", "Kumar", "parent@test.com"
                first_parent = father
                db.add(PortalAccount(company_id=company_id, kind="parent", guardian_id=father.id,
                                     login="parent@test.com", password=hash_password("Welcome9#"),
                                     must_change_password=False))
            if family and sec.program.name == "Grade 8":
                s.email = "student@test.com"
                db.add(PortalAccount(company_id=company_id, kind="student", student_id=s.id,
                                     login="student@test.com", password=hash_password("Welcome9#"),
                                     must_change_password=False))
            db.flush()
    db.flush()
