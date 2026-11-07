"""Students, guardians, enrolment and portal accounts.

Functions other modules use:
    section_students(db, company_id, section_id)    -> [(Student, Enrollment)] active learners, by roll no
    current_enrollment(db, company_id, student_id)  -> Enrollment | None (current session)
    student_section_id(db, company_id, student_id)  -> int | None
    visible_student_ids(db, user)                   -> set[int] | None: for parents their children, for
                                                       students themselves, None for staff (no limit)
    guardians_to_notify(db, company_id, student_id) -> [Guardian]
    student_brief(student, enrollment)              -> small dict for lists
"""
import secrets
import string
from datetime import date

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from core.security import hash_password
from modules.academics import service as academics
from modules.academics.models import Program, Section
from modules.students.models import Enrollment, Guardian, PortalAccount, Student, StudentGuardian

ACTIVE = "active"
LIVE_ENROLMENT = ("enrolled",)


def section_strength(section: Section) -> int:
    from sqlalchemy.orm import object_session
    db = object_session(section)
    if db is None:
        return 0
    return db.query(func.count(Enrollment.id)).filter(Enrollment.section_id == section.id,
                                                      Enrollment.status.in_(LIVE_ENROLMENT)).scalar() or 0


def current_enrollment(db: Session, company_id: int, student_id: int) -> Enrollment | None:
    sess = academics.current_session(db, company_id)
    q = db.query(Enrollment).options(joinedload(Enrollment.section).joinedload(Section.program),
                                     joinedload(Enrollment.program)).filter(
        Enrollment.company_id == company_id, Enrollment.student_id == student_id)
    if sess:
        row = q.filter(Enrollment.session_id == sess.id).first()
        if row:
            return row
    return q.order_by(Enrollment.id.desc()).first()


def student_section_id(db: Session, company_id: int, student_id: int) -> int | None:
    e = current_enrollment(db, company_id, student_id)
    return e.section_id if e else None


def section_students(db: Session, company_id: int, section_id: int) -> list[tuple[Student, Enrollment]]:
    rows = (db.query(Student, Enrollment).join(Enrollment, Enrollment.student_id == Student.id)
            .filter(Enrollment.company_id == company_id, Enrollment.section_id == section_id,
                    Enrollment.status.in_(LIVE_ENROLMENT), Student.status == ACTIVE).all())

    def key(r):
        roll = r[1].roll_no or ""
        return (0, int(roll)) if roll.isdigit() else (1, roll or r[0].first_name)
    return sorted(rows, key=key)


def visible_student_ids(db: Session, user: dict) -> set[int] | None:
    """Which learners the signed-in person may see in portal views. None = staff (use permissions)."""
    if user.get("role") == "student":
        return {user["student_id"]} if user.get("student_id") else set()
    if user.get("role") == "parent":
        if not user.get("guardian_id"):
            return set()
        return {sid for (sid,) in db.query(StudentGuardian.student_id).filter(
            StudentGuardian.company_id == user["company_id"], StudentGuardian.guardian_id == user["guardian_id"]).all()}
    return None


def require_student_access(db: Session, user: dict, student_id: int) -> None:
    """Parents may open only their children, students only themselves (staff pass)."""
    ids = visible_student_ids(db, user)
    if ids is not None and student_id not in ids:
        raise HTTPException(status_code=404, detail="Student not found")


def guardians_to_notify(db: Session, company_id: int, student_id: int) -> list[Guardian]:
    return [l.guardian for l in db.query(StudentGuardian).options(joinedload(StudentGuardian.guardian)).filter(
        StudentGuardian.company_id == company_id, StudentGuardian.student_id == student_id,
        StudentGuardian.receives_notifications.is_(True)).all()]


def portal_account_ids_for_students(db: Session, company_id: int, student_ids: list[int],
                                    parents: bool = True, students: bool = True) -> list[int]:
    """Portal account ids of these learners and/or their guardians (for notifications)."""
    if not student_ids:
        return []
    out: list[int] = []
    if students:
        out += [i for (i,) in db.query(PortalAccount.id).filter(
            PortalAccount.company_id == company_id, PortalAccount.student_id.in_(student_ids),
            PortalAccount.active.is_(True)).all()]
    if parents:
        gids = [g for (g,) in db.query(StudentGuardian.guardian_id).filter(
            StudentGuardian.company_id == company_id, StudentGuardian.student_id.in_(student_ids),
            StudentGuardian.receives_notifications.is_(True)).all()]
        if gids:
            out += [i for (i,) in db.query(PortalAccount.id).filter(
                PortalAccount.company_id == company_id, PortalAccount.guardian_id.in_(gids),
                PortalAccount.active.is_(True)).all()]
    return sorted(set(out))


def student_brief(s: Student, e: Enrollment | None = None) -> dict:
    return {
        "id": s.id, "uuid": str(s.uuid), "admission_no": s.admission_no, "name": s.full_name,
        "first_name": s.first_name, "last_name": s.last_name, "photo_url": s.photo_url, "gender": s.gender,
        "status": s.status,
        "section_id": e.section_id if e else None,
        "section_label": academics.section_label(e.section) if e and e.section else None,
        "program_name": e.program.name if e and e.program else None,
        "roll_no": e.roll_no if e else None,
    }


# ── Create and change ────────────────────────────────────────────────

def new_admission_no(db: Session, company_id: int) -> str:
    from core.numbering import next_number
    num = next_number(db, company_id, "student")
    if num:
        return num
    n = db.query(func.count(Student.id)).filter(Student.company_id == company_id).scalar() or 0
    return f"S{n + 1:05d}"


def enroll(db: Session, company_id: int, student: Student, section_id: int | None, program_id: int | None = None,
           session_id: int | None = None, roll_no: str | None = None, term_no: int | None = None) -> Enrollment:
    """Put a learner in a section for a session (replacing any enrolment in that session)."""
    section = None
    if section_id:
        section = db.query(Section).filter(Section.company_id == company_id, Section.id == section_id).first()
        if section is None:
            raise HTTPException(status_code=400, detail="The section was not found.")
        program_id, session_id = section.program_id, section.session_id
        if section.capacity:
            n = db.query(func.count(Enrollment.id)).filter(Enrollment.section_id == section.id,
                                                           Enrollment.status.in_(LIVE_ENROLMENT),
                                                           Enrollment.student_id != student.id).scalar()
            if n >= section.capacity:
                raise HTTPException(status_code=400, detail=f"{academics.section_label(section)} is full ({section.capacity}).")
    if not program_id:
        raise HTTPException(status_code=400, detail="Pick a class or section.")
    if not session_id:
        sess = academics.current_session(db, company_id)
        if sess is None:
            raise HTTPException(status_code=400, detail="Add an academic session first.")
        session_id = sess.id
    e = db.query(Enrollment).filter(Enrollment.student_id == student.id, Enrollment.session_id == session_id).first()
    if e is None:
        e = Enrollment(company_id=company_id, student_id=student.id, session_id=session_id, enrolled_on=date.today())
        db.add(e)
    e.program_id = program_id
    e.section_id = section_id
    e.status = "enrolled"
    if term_no is not None:
        e.term_no = term_no
    e.roll_no = roll_no if roll_no else (e.roll_no or _next_roll(db, section_id))
    db.flush()
    return e


def _next_roll(db: Session, section_id: int | None) -> str | None:
    if not section_id:
        return None
    rolls = [r for (r,) in db.query(Enrollment.roll_no).filter(Enrollment.section_id == section_id).all()]
    nums = [int(r) for r in rolls if r and r.isdigit()]
    return str(max(nums) + 1 if nums else 1)


def renumber_rolls(db: Session, company_id: int, section_id: int, by: str = "name") -> int:
    rows = section_students(db, company_id, section_id)
    if by == "name":
        rows.sort(key=lambda r: (r[0].first_name.lower(), (r[0].last_name or "").lower()))
    elif by == "admission":
        rows.sort(key=lambda r: r[0].admission_no)
    for i, (_, e) in enumerate(rows, 1):
        e.roll_no = str(i)
    db.flush()
    return len(rows)


def promote(db: Session, company_id: int, from_section_id: int, to_section_id: int | None, student_ids: list[int],
            outcome: str = "promoted", to_session_id: int | None = None) -> dict:
    """Move learners to next session's section. outcome: promoted | detained (same class again) |
    completed (finished the program; marked graduated/alumni)."""
    src = db.query(Section).filter(Section.company_id == company_id, Section.id == from_section_id).first()
    if src is None:
        raise HTTPException(status_code=404, detail="Section not found")
    dst = None
    if to_section_id:
        dst = db.query(Section).filter(Section.company_id == company_id, Section.id == to_section_id).first()
        if dst is None:
            raise HTTPException(status_code=400, detail="The target section was not found.")
        if dst.session_id == src.session_id:
            raise HTTPException(status_code=400, detail="Pick a section in the next session.")
    moved = 0
    for sid in student_ids:
        old = db.query(Enrollment).filter(Enrollment.company_id == company_id, Enrollment.student_id == sid,
                                          Enrollment.session_id == src.session_id).first()
        if old is None:
            continue
        student = db.query(Student).filter(Student.id == sid, Student.company_id == company_id).first()
        if outcome == "completed":
            old.status = "completed"
            student.status = "graduated"
            student.left_on = date.today()
            moved += 1
            continue
        if dst is None:
            raise HTTPException(status_code=400, detail="Pick the section to move them to.")
        old.status = "promoted" if outcome == "promoted" else "detained"
        enroll(db, company_id, student, dst.id)
        moved += 1
    db.flush()
    return {"moved": moved}


def random_password(n: int = 10) -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(n))
        if any(c.isdigit() for c in pw) and any(c.isalpha() for c in pw):
            return pw


def ensure_portal_account(db: Session, company_id: int, *, student: Student | None = None,
                          guardian: Guardian | None = None, login: str | None = None,
                          password: str | None = None) -> tuple[PortalAccount, str | None]:
    """Create (or return) the portal account of a learner or a guardian. Returns the account and
    the new password (None when it already existed and no password was given)."""
    kind = "student" if student is not None else "parent"
    q = db.query(PortalAccount).filter(PortalAccount.company_id == company_id, PortalAccount.kind == kind)
    acc = q.filter(PortalAccount.student_id == student.id).first() if student else \
        q.filter(PortalAccount.guardian_id == guardian.id).first()
    new_pw = None
    if acc is None:
        login = (login or (student.email if student else guardian.email) or
                 (student.admission_no if student else guardian.phone) or "").strip().lower()
        if not login:
            raise HTTPException(status_code=400, detail="Add an email or phone number first.")
        if db.query(PortalAccount.id).filter(PortalAccount.company_id == company_id, PortalAccount.login == login).first():
            if student:
                login = student.admission_no.lower()
            if db.query(PortalAccount.id).filter(PortalAccount.company_id == company_id,
                                                 PortalAccount.login == login).first():
                raise HTTPException(status_code=409, detail=f"The sign-in name {login} is already used.")
        new_pw = password or random_password()
        acc = PortalAccount(company_id=company_id, kind=kind, login=login, password=hash_password(new_pw),
                            student_id=student.id if student else None,
                            guardian_id=guardian.id if guardian else None, must_change_password=password is None)
        db.add(acc)
        db.flush()
    elif password:
        acc.password = hash_password(password)
        new_pw = password
    return acc, new_pw


def search_students(db: Session, company_id: int, q: str | None = None, section_id: int | None = None,
                    program_id: int | None = None, status: str | None = ACTIVE, session_id: int | None = None):
    """Query of (Student, Enrollment|None) for the current (or given) session."""
    sess_id = session_id or (academics.current_session(db, company_id).id if academics.current_session(db, company_id) else None)
    query = (db.query(Student, Enrollment)
             .outerjoin(Enrollment, (Enrollment.student_id == Student.id) & (Enrollment.session_id == sess_id))
             .options(joinedload(Enrollment.section).joinedload(Section.program), joinedload(Enrollment.program))
             .filter(Student.company_id == company_id, Student.archived_at.is_(None)))
    if status:
        query = query.filter(Student.status == status)
    if section_id:
        query = query.filter(Enrollment.section_id == section_id)
    if program_id:
        query = query.filter(Enrollment.program_id == program_id)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Student.first_name.ilike(like), Student.last_name.ilike(like),
                                 Student.admission_no.ilike(like), Student.phone.ilike(like),
                                 Student.email.ilike(like),
                                 func.concat(Student.first_name, " ", Student.last_name).ilike(like)))
    return query.order_by(Student.first_name, Student.last_name)
