"""Institution profile, academic structure helpers and the quick set-up used on sign-up.

Every other school module reads the academic structure through these functions:
    current_session(db, company_id)          -> AcademicSession | None
    teacher_section_ids(db, company_id, employee_id) -> set of section ids the person teaches or leads
    section_label(section)                   -> "Grade 5 - A"
    profile(company)                         -> institution type, words used on screen, enabled modules
"""
from datetime import date

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.orm.attributes import flag_modified

from modules.academics.models import (AcademicSession, House, Program, ProgramSubject, Room, Section,
                                      SectionSubject, Subject, Term)

# ── Institution types and the words each uses ───────────────────────

INSTITUTION_TYPES = {
    "school": {
        "label": "School (K-12)",
        "terms": {"program": "Class", "programs": "Classes", "section": "Section", "sections": "Sections",
                  "term": "Term", "terms": "Terms", "session": "Academic year", "subject": "Subject",
                  "subjects": "Subjects", "student": "Student", "students": "Students", "teacher": "Teacher",
                  "teachers": "Teachers", "guardian": "Parent", "guardians": "Parents", "roll": "Roll no."},
        "term_kind": "term", "terms_per_session": 3, "grading": "percentage_grades",
    },
    "preschool": {
        "label": "Preschool / Kindergarten",
        "terms": {"program": "Class", "programs": "Classes", "section": "Group", "sections": "Groups",
                  "term": "Term", "terms": "Terms", "session": "Academic year", "subject": "Activity area",
                  "subjects": "Activity areas", "student": "Child", "students": "Children", "teacher": "Teacher",
                  "teachers": "Teachers", "guardian": "Parent", "guardians": "Parents", "roll": "Roll no."},
        "term_kind": "term", "terms_per_session": 2, "grading": "descriptive",
    },
    "college": {
        "label": "College",
        "terms": {"program": "Programme", "programs": "Programmes", "section": "Batch", "sections": "Batches",
                  "term": "Semester", "terms": "Semesters", "session": "Academic year", "subject": "Course",
                  "subjects": "Courses", "student": "Student", "students": "Students", "teacher": "Faculty",
                  "teachers": "Faculty", "guardian": "Guardian", "guardians": "Guardians", "roll": "Register no."},
        "term_kind": "semester", "terms_per_session": 2, "grading": "gpa_10",
    },
    "university": {
        "label": "University",
        "terms": {"program": "Programme", "programs": "Programmes", "section": "Batch", "sections": "Batches",
                  "term": "Semester", "terms": "Semesters", "session": "Academic year", "subject": "Course",
                  "subjects": "Courses", "student": "Student", "students": "Students", "teacher": "Faculty",
                  "teachers": "Faculty", "guardian": "Guardian", "guardians": "Guardians", "roll": "Enrolment no."},
        "term_kind": "semester", "terms_per_session": 2, "grading": "gpa_10",
    },
    "coaching": {
        "label": "Coaching / Tuition centre",
        "terms": {"program": "Course", "programs": "Courses", "section": "Batch", "sections": "Batches",
                  "term": "Module", "terms": "Modules", "session": "Session", "subject": "Subject",
                  "subjects": "Subjects", "student": "Student", "students": "Students", "teacher": "Tutor",
                  "teachers": "Tutors", "guardian": "Parent", "guardians": "Parents", "roll": "Roll no."},
        "term_kind": "module", "terms_per_session": 1, "grading": "percentage",
    },
    "training": {
        "label": "Training institute / Academy",
        "terms": {"program": "Course", "programs": "Courses", "section": "Batch", "sections": "Batches",
                  "term": "Module", "terms": "Modules", "session": "Intake", "subject": "Unit",
                  "subjects": "Units", "student": "Learner", "students": "Learners", "teacher": "Trainer",
                  "teachers": "Trainers", "guardian": "Emergency contact", "guardians": "Emergency contacts",
                  "roll": "Learner no."},
        "term_kind": "module", "terms_per_session": 1, "grading": "competency",
    },
}

# Modules an institution can switch on or off (the menu hides what is off).
MODULE_KEYS = ["admissions", "attendance", "timetable", "exams", "lms", "online_classes", "fees", "library",
               "transport", "hostel", "wellbeing", "comms", "staff_payroll"]


def profile(company) -> dict:
    cfg = company.config if isinstance(company.config, dict) else {}
    inst = cfg.get("institution") if isinstance(cfg.get("institution"), dict) else {}
    kind = inst.get("type") if inst.get("type") in INSTITUTION_TYPES else "school"
    preset = INSTITUTION_TYPES[kind]
    words = {**preset["terms"], **(inst.get("terms") or {})}
    modules = {k: True for k in MODULE_KEYS}
    modules.update({k: bool(v) for k, v in (inst.get("modules") or {}).items() if k in MODULE_KEYS})
    return {
        "type": kind,
        "type_label": preset["label"],
        "board": inst.get("board") or "",
        "affiliation_no": inst.get("affiliation_no") or "",
        "established_year": inst.get("established_year"),
        "motto": inst.get("motto") or "",
        "principal_name": inst.get("principal_name") or "",
        "terms": words,
        "modules": modules,
        "week_days": inst.get("week_days") or [1, 2, 3, 4, 5, 6],  # ISO weekdays the institution works
        "attendance_mode": inst.get("attendance_mode") or ("period" if kind in ("college", "university") else "daily"),
        "grading": inst.get("grading") or preset["grading"],
        "types": [{"key": k, "label": v["label"]} for k, v in INSTITUTION_TYPES.items()],
        "module_keys": MODULE_KEYS,
    }


def save_profile(db: Session, company, data: dict) -> dict:
    cfg = dict(company.config) if isinstance(company.config, dict) else {}
    inst = dict(cfg.get("institution") or {})
    for key in ("type", "board", "affiliation_no", "established_year", "motto", "principal_name", "week_days",
                "attendance_mode", "grading"):
        if key in data and data[key] is not None:
            inst[key] = data[key]
    if isinstance(data.get("terms"), dict):
        inst["terms"] = {k: str(v)[:40] for k, v in data["terms"].items() if v}
    if isinstance(data.get("modules"), dict):
        inst["modules"] = {k: bool(v) for k, v in data["modules"].items() if k in MODULE_KEYS}
    cfg["institution"] = inst
    company.config = cfg
    flag_modified(company, "config")
    db.commit()
    return profile(company)


# ── Lookups other modules use ────────────────────────────────────────

def current_session(db: Session, company_id: int) -> AcademicSession | None:
    rows = db.query(AcademicSession).filter(AcademicSession.company_id == company_id, AcademicSession.active.is_(True))
    cur = rows.filter(AcademicSession.is_current.is_(True)).first()
    if cur:
        return cur
    today = date.today()
    return (rows.filter(AcademicSession.start_date <= today, AcademicSession.end_date >= today).first()
            or rows.order_by(AcademicSession.start_date.desc()).first())


def current_term(db: Session, company_id: int, on: date | None = None) -> Term | None:
    sess = current_session(db, company_id)
    if not sess:
        return None
    on = on or date.today()
    q = db.query(Term).filter(Term.company_id == company_id, Term.session_id == sess.id, Term.active.is_(True))
    return q.filter(Term.start_date <= on, Term.end_date >= on).first() or q.order_by(Term.sort_order).first()


def section_label(section: Section | None) -> str:
    if section is None:
        return ""
    return f"{section.program.name} - {section.name}" if section.program else section.name


def teacher_section_ids(db: Session, company_id: int, employee_id: int | None, session_id: int | None = None) -> set[int]:
    """Sections the person is class teacher of or teaches a subject in."""
    if not employee_id:
        return set()
    q1 = db.query(Section.id).filter(Section.company_id == company_id, Section.class_teacher_id == employee_id)
    q2 = (db.query(SectionSubject.section_id).join(Section, Section.id == SectionSubject.section_id)
          .filter(SectionSubject.company_id == company_id,
                  or_(SectionSubject.teacher_id == employee_id,
                      SectionSubject.assistant_ids.contains([employee_id]))))
    if session_id:
        q1 = q1.filter(Section.session_id == session_id)
        q2 = q2.filter(Section.session_id == session_id)
    return {i for (i,) in q1.all()} | {i for (i,) in q2.all()}


def my_classes(db: Session, company_id: int, employee_id: int | None) -> list[dict]:
    """The signed-in teacher's sections in the current session, with the subjects they teach."""
    sess = current_session(db, company_id)
    if not sess or not employee_id:
        return []
    ids = teacher_section_ids(db, company_id, employee_id, sess.id)
    if not ids:
        return []
    sections = (db.query(Section).options(joinedload(Section.program))
                .filter(Section.id.in_(ids)).order_by(Section.program_id, Section.name).all())
    allocs = (db.query(SectionSubject).options(joinedload(SectionSubject.subject))
              .filter(SectionSubject.section_id.in_(ids)).all())
    out = []
    for s in sections:
        subs = [a for a in allocs if a.section_id == s.id and
                (a.teacher_id == employee_id or employee_id in (a.assistant_ids or []))]
        out.append({
            "section_id": s.id, "section_uuid": str(s.uuid), "label": section_label(s),
            "program_id": s.program_id, "is_class_teacher": s.class_teacher_id == employee_id,
            "subjects": [{"subject_id": a.subject_id, "name": a.subject.name, "code": a.subject.code} for a in subs],
        })
    return out


# ── Quick set-up ─────────────────────────────────────────────────────

SCHOOL_CLASSES = ["Nursery", "LKG", "UKG"] + [f"Grade {i}" for i in range(1, 13)]
SCHOOL_SUBJECTS = [("English", "ENG", "language"), ("Hindi", "HIN", "language"), ("Mathematics", "MAT", "theory"),
                   ("Science", "SCI", "theory_practical"), ("Social Science", "SST", "theory"),
                   ("Computer Science", "CS", "theory_practical"), ("Physical Education", "PE", "activity"),
                   ("Art & Craft", "ART", "activity"), ("Music", "MUS", "activity")]


def session_dates(start_month: int = 4, today: date | None = None) -> tuple[str, date, date]:
    today = today or date.today()
    year = today.year if today.month >= start_month else today.year - 1
    start = date(year, start_month, 1)
    end_year = year + 1 if start_month > 1 else year
    end = date(end_year, start_month - 1 if start_month > 1 else 12, 28)
    import calendar
    end = end.replace(day=calendar.monthrange(end.year, end.month)[1])
    name = f"{year}-{str(year + 1)[-2:]}" if start_month > 1 else str(year)
    return name, start, end


def quick_setup(db: Session, company_id: int, *, institution_type: str = "school", start_month: int = 4,
                programs: list[str] | None = None, sections: list[str] | None = None,
                subjects: list[tuple[str, str, str]] | None = None) -> dict:
    """Create a current session with its terms, programs, sections and subjects in one go.
    Skips anything that already exists (safe to run twice)."""
    preset = INSTITUTION_TYPES.get(institution_type, INSTITUTION_TYPES["school"])
    name, start, end = session_dates(start_month)
    sess = db.query(AcademicSession).filter(AcademicSession.company_id == company_id, AcademicSession.name == name).first()
    if sess is None:
        db.query(AcademicSession).filter(AcademicSession.company_id == company_id).update({"is_current": False})
        sess = AcademicSession(company_id=company_id, name=name, start_date=start, end_date=end, is_current=True)
        db.add(sess)
        db.flush()
        n = preset["terms_per_session"]
        span = (end - start).days + 1
        label = {"term": "Term", "semester": "Semester", "module": "Module"}.get(preset["term_kind"], "Term")
        from datetime import timedelta
        for i in range(n):
            t_start = start + timedelta(days=span * i // n)
            t_end = start + timedelta(days=span * (i + 1) // n - 1)
            db.add(Term(company_id=company_id, session_id=sess.id, name=f"{label} {i + 1}", kind=preset["term_kind"],
                        start_date=t_start, end_date=t_end, sort_order=i + 1))
    if programs is None:
        programs = SCHOOL_CLASSES if institution_type == "school" else []
    if sections is None:
        sections = ["A", "B"] if institution_type == "school" else ["Batch 1"]
    if subjects is None:
        subjects = SCHOOL_SUBJECTS if institution_type == "school" else []
    subject_rows = []
    for sname, code, kind in subjects:
        sub = db.query(Subject).filter(Subject.company_id == company_id, Subject.name == sname).first()
        if sub is None:
            sub = Subject(company_id=company_id, name=sname, code=code, kind=kind)
            db.add(sub)
            db.flush()
        subject_rows.append(sub)
    prev = None
    for i, pname in enumerate(programs):
        prog = db.query(Program).filter(Program.company_id == company_id, Program.name == pname).first()
        if prog is None:
            level = None
            if institution_type == "school":
                level = ("Pre-primary" if i < 3 else "Primary" if i < 8 else "Middle" if i < 11 else
                         "Secondary" if i < 13 else "Senior secondary")
            prog = Program(company_id=company_id, name=pname, level=level, sort_order=i + 1)
            db.add(prog)
            db.flush()
            for j, sub in enumerate(subject_rows):
                if level == "Pre-primary" and sub.kind not in ("language", "activity") and sub.code != "MAT":
                    continue
                db.add(ProgramSubject(company_id=company_id, program_id=prog.id, subject_id=sub.id, sort_order=j + 1))
        if prev is not None and prev.next_program_id is None:
            prev.next_program_id = prog.id
        prev = prog
        for sec in sections:
            if not db.query(Section.id).filter(Section.program_id == prog.id, Section.session_id == sess.id,
                                               Section.name == sec).first():
                db.add(Section(company_id=company_id, program_id=prog.id, session_id=sess.id, name=sec, capacity=40))
    db.flush()
    return {"session_id": sess.id, "programs": len(programs), "subjects": len(subject_rows)}


def ensure_defaults(db: Session, company_id: int) -> None:
    """Every institution gets at least one current session and a few houses and rooms."""
    if not db.query(AcademicSession.id).filter(AcademicSession.company_id == company_id).first():
        name, start, end = session_dates(4)
        sess = AcademicSession(company_id=company_id, name=name, start_date=start, end_date=end, is_current=True)
        db.add(sess)
        db.flush()
        from datetime import timedelta
        span = (end - start).days + 1
        for i in range(3):
            db.add(Term(company_id=company_id, session_id=sess.id, name=f"Term {i + 1}", kind="term",
                        start_date=start + timedelta(days=span * i // 3),
                        end_date=start + timedelta(days=span * (i + 1) // 3 - 1), sort_order=i + 1))
    if not db.query(House.id).filter(House.company_id == company_id).first():
        for n, c in (("Red", "#e5484d"), ("Blue", "#3e63dd"), ("Green", "#30a46c"), ("Yellow", "#f5d90a")):
            db.add(House(company_id=company_id, name=n, color=c))
    if not db.query(Room.id).filter(Room.company_id == company_id).first():
        db.add(Room(company_id=company_id, name="Online room", kind="online"))
    db.flush()
