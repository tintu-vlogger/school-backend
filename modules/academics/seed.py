"""Academic defaults for every institution, and a full sample structure for the demo."""
from sqlalchemy.orm import Session

from modules.academics import service


def setup_company(db: Session, company_id: int) -> None:
    service.ensure_defaults(db, company_id)


def seed_demo(db: Session, company_id: int) -> None:
    from modules.academics.models import Program
    if db.query(Program.id).filter(Program.company_id == company_id).first():
        return
    service.quick_setup(db, company_id, institution_type="school")
    # Allocate the demo staff as class teachers and subject teachers.
    from modules.academics.models import ProgramSubject, Section, SectionSubject
    from modules.employees.models import Employee
    teachers = (db.query(Employee).filter(Employee.company_id == company_id, Employee.active.is_(True),
                                          Employee.role.in_(("teacher", "manager")))
                .order_by(Employee.id).all())
    staff = [e.id for e in teachers]
    if not staff:
        return
    sections = db.query(Section).filter(Section.company_id == company_id).order_by(Section.program_id, Section.name).all()
    for i, sec in enumerate(sections):
        sec.class_teacher_id = staff[i % len(staff)]
        subs = db.query(ProgramSubject).filter(ProgramSubject.program_id == sec.program_id).all()
        for j, ps in enumerate(subs):
            db.add(SectionSubject(company_id=company_id, section_id=sec.id, subject_id=ps.subject_id,
                                  teacher_id=staff[(i + j) % len(staff)], weekly_periods=5))
    # The demo teacher login (Divya Menon) leads Grade 8 - A and teaches Computer Science across Grades 6-10.
    divya = next((e for e in teachers if e.email == "divya.menon@example.com"), None)
    if divya:
        from modules.academics.models import Program, Subject
        cs = db.query(Subject).filter(Subject.company_id == company_id, Subject.code == "CS").first()
        for sec in sections:
            pname = sec.program.name
            if pname == "Grade 8" and sec.name == "A":
                sec.class_teacher_id = divya.id
            if cs and pname in ("Grade 6", "Grade 7", "Grade 8", "Grade 9", "Grade 10"):
                db.flush()
                a = db.query(SectionSubject).filter(SectionSubject.section_id == sec.id,
                                                    SectionSubject.subject_id == cs.id).first()
                if a:
                    a.teacher_id = divya.id
    db.flush()
