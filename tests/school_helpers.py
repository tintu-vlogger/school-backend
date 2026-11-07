"""Small school set-up shared by the student attendance and timetable tests: one session, a
class with a section, subjects, a teacher allocation, learners with a parent sign-in."""
import uuid
from datetime import date, timedelta

from core.db import tenant_session
from core.security import hash_password

PASSWORD = "Secret123#"


def portal_login(company, login: str):
    r = company.client.post("/api/v1/auth/login", json={"email": login, "password": PASSWORD},
                            headers={"X-Company-Slug": company.slug})
    assert r.status_code == 200, r.text

    class S:
        pass
    s = S()
    s.client, s.slug, s.token = company.client, company.slug, r.json()["access_token"]
    from tests.conftest import Company
    s.headers = {"Authorization": f"Bearer {s.token}", "X-Company-Slug": s.slug}
    s.get = lambda path, **kw: company.client.get(f"/api/v1{path}", headers=s.headers, **kw)
    s.post = lambda path, json=None, **kw: company.client.post(f"/api/v1{path}", headers=s.headers, json=json, **kw)
    return s


def setup_school(company, teachers: int = 2, learners: int = 3) -> dict:
    """Returns ids: section_id, program_id, subject_ids, teacher (employee dicts), students, parent login."""
    staff = [company.add_employee(f"Teach{i}", "Er") for i in range(teachers)]
    from modules.employees.models import Employee
    db0 = tenant_session(company.id)
    db0.query(Employee).filter(Employee.id.in_([e["id"] for e in staff])).update({"role": "teacher"})
    db0.commit()
    db0.close()
    # Every day is a working day and staff holidays are ignored, so tests never hit a weekend.
    assert company.put("/academics/profile", json={"week_days": [1, 2, 3, 4, 5, 6, 7]}).status_code == 200
    company.put("/student-attendance/settings", json={"use_staff_holidays": False})
    from modules.academics.models import AcademicSession, Program, Section, SectionSubject, Subject
    from modules.students.models import Enrollment, Guardian, PortalAccount, Student, StudentGuardian
    db = tenant_session(company.id)
    try:
        today = date.today()
        sess = db.query(AcademicSession).filter(AcademicSession.company_id == company.id).first()
        if sess is None:
            sess = AcademicSession(company_id=company.id, name="2026-27", start_date=today - timedelta(days=200),
                                   end_date=today + timedelta(days=200), is_current=True)
            db.add(sess)
        else:
            sess.start_date, sess.end_date, sess.is_current = today - timedelta(days=200), today + timedelta(days=200), True
        db.flush()
        prog = Program(company_id=company.id, name=f"Grade {uuid.uuid4().hex[:4]}")
        db.add(prog)
        db.flush()
        sec = Section(company_id=company.id, program_id=prog.id, session_id=sess.id, name="A",
                      class_teacher_id=staff[0]["id"])
        sec2 = Section(company_id=company.id, program_id=prog.id, session_id=sess.id, name="B",
                       class_teacher_id=staff[-1]["id"])
        db.add_all([sec, sec2])
        subs = [Subject(company_id=company.id, name=f"Maths {uuid.uuid4().hex[:4]}", code="MAT"),
                Subject(company_id=company.id, name=f"English {uuid.uuid4().hex[:4]}", code="ENG")]
        db.add_all(subs)
        db.flush()
        for i, s in enumerate(subs):
            db.add(SectionSubject(company_id=company.id, section_id=sec.id, subject_id=s.id,
                                  teacher_id=staff[i % len(staff)]["id"], weekly_periods=3))
            db.add(SectionSubject(company_id=company.id, section_id=sec2.id, subject_id=s.id,
                                  teacher_id=staff[(i + 1) % len(staff)]["id"], weekly_periods=3))
        kids = []
        tag = uuid.uuid4().hex[:6]
        for i in range(learners):
            st = Student(company_id=company.id, admission_no=f"T{tag}{i}", first_name=f"Kid{i}", last_name="Test")
            db.add(st)
            db.flush()
            db.add(Enrollment(company_id=company.id, student_id=st.id, session_id=sess.id, program_id=prog.id,
                              section_id=sec.id, roll_no=str(i + 1)))
            kids.append(st.id)
        other = Student(company_id=company.id, admission_no=f"T{tag}X", first_name="Other", last_name="Kid")
        db.add(other)
        db.flush()
        db.add(Enrollment(company_id=company.id, student_id=other.id, session_id=sess.id, program_id=prog.id,
                          section_id=sec2.id, roll_no="1"))
        g = Guardian(company_id=company.id, first_name="Pat", last_name="Parent", email=f"p{tag}@example.com")
        g2 = Guardian(company_id=company.id, first_name="Oth", last_name="Parent", email=f"o{tag}@example.com")
        db.add_all([g, g2])
        db.flush()
        db.add(StudentGuardian(company_id=company.id, student_id=kids[0], guardian_id=g.id, relation="father"))
        db.add(StudentGuardian(company_id=company.id, student_id=other.id, guardian_id=g2.id, relation="mother"))
        db.add(PortalAccount(company_id=company.id, kind="parent", guardian_id=g.id, login=f"p{tag}@example.com",
                             password=hash_password(PASSWORD), must_change_password=False))
        db.add(PortalAccount(company_id=company.id, kind="parent", guardian_id=g2.id, login=f"o{tag}@example.com",
                             password=hash_password(PASSWORD), must_change_password=False))
        db.commit()
        return {"section_id": sec.id, "section2_id": sec2.id, "program_id": prog.id, "subject_ids": [s.id for s in subs],
                "teachers": staff, "students": kids, "other_student": other.id,
                "parent_login": f"p{tag}@example.com", "other_parent_login": f"o{tag}@example.com"}
    finally:
        db.close()
