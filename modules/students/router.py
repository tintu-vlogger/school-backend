"""Students API: learners, guardians, enrolment, promotion, documents and portal accounts."""
from datetime import date
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload, selectinload

from core import audit, custom_fields
from core.crud import by_id_or_400, get_or_404
from core.db import get_db
from core.pagination import PageParams, paginate
from core.security import _get_role_permissions, get_current_user, require_permission
from modules.academics import service as academics
from modules.academics.models import Section
from modules.students import service
from modules.students.models import (Enrollment, Guardian, PortalAccount, Student, StudentDocument,
                                     StudentGuardian)

router = APIRouter(prefix="/students", tags=["Students"])
guardians_router = APIRouter(prefix="/guardians", tags=["Students"])
portal_router = APIRouter(prefix="/portal-accounts", tags=["Students"])
extra_routers = [guardians_router, portal_router]

VIEW, MANAGE = "students.view", "students.manage"


def _mask(v: str | None, show: bool) -> str | None:
    if not v or show:
        return v
    return "•" * max(0, len(v) - 4) + v[-4:]


# ── Schemas ──────────────────────────────────────────────────────────

class GuardianIn(BaseModel):
    id: int | None = None  # link an existing guardian (siblings)
    first_name: str | None = Field(None, max_length=100)
    last_name: str | None = Field(None, max_length=100)
    email: EmailStr | None = None
    phone: str | None = Field(None, max_length=20)
    alt_phone: str | None = Field(None, max_length=20)
    occupation: str | None = Field(None, max_length=120)
    organisation: str | None = Field(None, max_length=200)
    annual_income: Decimal | None = None
    education: str | None = Field(None, max_length=120)
    address: str | None = None
    national_id: str | None = Field(None, max_length=40)
    relation: str = Field("guardian", max_length=30)
    is_primary: bool = False
    can_pickup: bool = True
    receives_notifications: bool = True


class StudentBase(BaseModel):
    first_name: str = Field(..., min_length=1, max_length=100)
    middle_name: str | None = Field(None, max_length=100)
    last_name: str | None = Field(None, max_length=100)
    gender: str | None = Field(None, max_length=20)
    date_of_birth: date | None = None
    blood_group: str | None = Field(None, max_length=10)
    email: EmailStr | None = None
    phone: str | None = Field(None, max_length=20)
    photo_url: str | None = None
    address: str | None = None
    city: str | None = Field(None, max_length=100)
    state: str | None = Field(None, max_length=100)
    pincode: str | None = Field(None, max_length=12)
    country: str | None = Field(None, max_length=100)
    nationality: str | None = Field(None, max_length=60)
    religion: str | None = Field(None, max_length=60)
    category: str | None = Field(None, max_length=40)
    mother_tongue: str | None = Field(None, max_length=60)
    national_id: str | None = Field(None, max_length=40)
    student_reg_id: str | None = Field(None, max_length=60)
    previous_institution: str | None = Field(None, max_length=200)
    admission_date: date | None = None
    house_id: int | None = None
    medical_notes: str | None = None
    allergies: str | None = None
    is_boarder: bool = False
    uses_transport: bool = False
    fee_category: str | None = Field(None, max_length=60)
    custom: dict = {}


class StudentCreate(StudentBase):
    admission_no: str | None = Field(None, max_length=40)  # empty = next from the number series
    section_id: int | None = None
    program_id: int | None = None
    roll_no: str | None = Field(None, max_length=20)
    guardians: list[GuardianIn] = []
    create_portal_accounts: bool = False


class StudentUpdate(BaseModel):
    admission_no: str | None = Field(None, min_length=1, max_length=40)
    first_name: str | None = Field(None, min_length=1, max_length=100)
    middle_name: str | None = None
    last_name: str | None = None
    gender: str | None = None
    date_of_birth: date | None = None
    blood_group: str | None = None
    email: EmailStr | None = None
    phone: str | None = None
    photo_url: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    pincode: str | None = None
    country: str | None = None
    nationality: str | None = None
    religion: str | None = None
    category: str | None = None
    mother_tongue: str | None = None
    national_id: str | None = None
    student_reg_id: str | None = None
    previous_institution: str | None = None
    admission_date: date | None = None
    house_id: int | None = None
    medical_notes: str | None = None
    allergies: str | None = None
    is_boarder: bool | None = None
    uses_transport: bool | None = None
    fee_category: str | None = None
    custom: dict | None = None


def _student_out(db: Session, s: Student, e: Enrollment | None, perms: list[str], full: bool = False) -> dict:
    out = service.student_brief(s, e)
    out.update({
        "email": s.email, "phone": s.phone, "date_of_birth": s.date_of_birth, "admission_date": s.admission_date,
        "house_id": s.house_id, "house_name": s.house.name if s.house else None, "is_boarder": s.is_boarder,
        "uses_transport": s.uses_transport, "fee_category": s.fee_category, "created_at": s.created_at,
    })
    if full:
        show = "students.sensitive.view" in perms
        out.update({k: getattr(s, k) for k in (
            "middle_name", "blood_group", "address", "city", "state", "pincode", "country", "nationality", "religion",
            "category", "mother_tongue", "student_reg_id", "previous_institution", "medical_notes", "allergies",
            "left_on", "leaving_reason")})
        out["national_id"] = _mask(s.national_id, show)
        out["custom"] = s.custom or {}
        out["guardians"] = [_link_out(l) for l in s.guardian_links]
        if VIEW in perms or "students.portal.manage" in perms:
            gids = [l.guardian_id for l in s.guardian_links]
            accs = {a.guardian_id: a for a in db.query(PortalAccount).filter(
                PortalAccount.company_id == s.company_id, PortalAccount.guardian_id.in_(gids or [-1])).all()}
            for g in out["guardians"]:
                acc = accs.get(g["guardian_id"])
                g["portal_account"] = _account_out(acc) if acc else None
        sec = e.section if e else None
        ct = sec.class_teacher if sec is not None else None
        out["class_teacher_name"] = f"{ct.first_name} {ct.last_name or ''}".strip() if ct else None
        out["house_color"] = s.house.color if s.house else None
        out["session_name"] = e.session.name if e and e.session else None
        out["enrollments"] = [{
            "id": x.id, "uuid": str(x.uuid), "session_id": x.session_id, "session_name": x.session.name if x.session else None,
            "program_id": x.program_id, "program_name": x.program.name if x.program else None,
            "section_id": x.section_id, "section_label": academics.section_label(x.section) if x.section else None,
            "roll_no": x.roll_no, "status": x.status, "term_no": x.term_no, "enrolled_on": x.enrolled_on,
            "elective_subject_ids": x.elective_subject_ids or [],
        } for x in s.enrollments]
        acc = db.query(PortalAccount).filter(PortalAccount.company_id == s.company_id,
                                             PortalAccount.student_id == s.id).first()
        out["portal_account"] = _account_out(acc) if acc else None
    return out


def _link_out(l: StudentGuardian) -> dict:
    g = l.guardian
    return {"link_id": l.id, "guardian_id": g.id, "guardian_uuid": str(g.uuid), "name": g.full_name,
            "first_name": g.first_name, "last_name": g.last_name, "email": g.email, "phone": g.phone,
            "alt_phone": g.alt_phone, "occupation": g.occupation, "relation": l.relation, "is_primary": l.is_primary,
            "can_pickup": l.can_pickup, "receives_notifications": l.receives_notifications}


def _account_out(a: PortalAccount) -> dict:
    return {"id": a.id, "uuid": str(a.uuid), "kind": a.kind, "login": a.login, "active": a.active,
            "must_change_password": a.must_change_password, "last_login_at": a.last_login_at}


def _perms(db: Session, user: dict) -> list[str]:
    return _get_role_permissions(user["company_id"], user["role"], db)


# ── List, stats, my children ─────────────────────────────────────────

def _primary_guardian(s: Student) -> Guardian | None:
    links = sorted(s.guardian_links, key=lambda l: (not l.is_primary, l.id))
    return links[0].guardian if links else None


def _list_row(db: Session, s: Student, e: Enrollment | None, perms: list[str]) -> dict:
    out = _student_out(db, s, e, perms)
    g = _primary_guardian(s)
    out.update({"guardian_name": g.full_name if g else None, "guardian_phone": g.phone if g else None,
                "blood_group": s.blood_group})
    return out


def _filtered_students(db: Session, cid: int, q, section_id, program_id, status, session_id, house_id, gender):
    query = service.search_students(db, cid, q, section_id, program_id, status or None, session_id)
    if house_id:
        query = query.filter(Student.house_id == house_id)
    if gender:
        query = query.filter(Student.gender == gender)
    return query.options(joinedload(Student.house),
                         selectinload(Student.guardian_links).joinedload(StudentGuardian.guardian))


@router.get("")
def list_students(q: str | None = None, section_id: int | None = None, program_id: int | None = None,
                  status: str | None = Query("active"), session_id: int | None = None,
                  house_id: int | None = None, gender: str | None = None,
                  params: PageParams = Depends(), db: Session = Depends(get_db),
                  user: dict = Depends(require_permission(VIEW))):
    perms = _perms(db, user)
    query = _filtered_students(db, user["company_id"], q, section_id, program_id, status, session_id, house_id, gender)
    return paginate(query, params, lambda r: _list_row(db, r[0], r[1], perms))


EXPORT_COLUMNS = [("admission_no", "Admission no"), ("name", "Name"), ("gender", "Gender"),
                  ("date_of_birth", "Date of birth"), ("program_name", "Class"), ("section_label", "Section"),
                  ("roll_no", "Roll no"), ("house_name", "House"), ("blood_group", "Blood group"),
                  ("guardian_name", "Guardian"), ("guardian_phone", "Guardian phone"), ("email", "Email"),
                  ("phone", "Phone"), ("admission_date", "Admission date"), ("status", "Status")]


@router.get("/export")
def export_students(q: str | None = None, section_id: int | None = None, program_id: int | None = None,
                    status: str | None = Query("active"), session_id: int | None = None,
                    house_id: int | None = None, gender: str | None = None,
                    db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    """The filtered directory as CSV (up to 10,000 rows). Audited as an export."""
    import csv
    import io
    from fastapi.responses import Response
    perms = _perms(db, user)
    rows = _filtered_students(db, user["company_id"], q, section_id, program_id, status, session_id, house_id,
                              gender).limit(10000).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([label for _, label in EXPORT_COLUMNS])
    for s, e in rows:
        r = _list_row(db, s, e, perms)
        w.writerow(["" if r.get(k) is None else str(r.get(k)) for k, _ in EXPORT_COLUMNS])
    audit.event(db, "export.downloaded", "students", None, {"rows": len(rows), "format": "csv"})
    db.commit()
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="students.csv"'})


@router.get("/stats")
def stats(db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    cid = user["company_id"]
    by_status = dict(db.query(Student.status, func.count(Student.id)).filter(
        Student.company_id == cid, Student.archived_at.is_(None)).group_by(Student.status).all())
    by_gender = dict(db.query(func.coalesce(Student.gender, "unknown"), func.count(Student.id)).filter(
        Student.company_id == cid, Student.status == "active").group_by(Student.gender).all())
    sess = academics.current_session(db, cid)
    from modules.academics.models import Program
    by_program = []
    if sess:
        by_program = [{"program": n, "count": c} for n, c in db.query(Program.name, func.count(Enrollment.id))
                      .join(Enrollment, Enrollment.program_id == Program.id)
                      .filter(Enrollment.company_id == cid, Enrollment.session_id == sess.id,
                              Enrollment.status == "enrolled")
                      .group_by(Program.name, Program.sort_order).order_by(Program.sort_order).all()]
    month_start = date.today().replace(day=1)
    new_this_month = db.query(func.count(Student.id)).filter(Student.company_id == cid,
                                                             Student.admission_date >= month_start).scalar()
    return {"by_status": by_status, "by_gender": by_gender, "by_program": by_program,
            "active": by_status.get("active", 0), "new_this_month": new_this_month,
            "guardians": db.query(func.count(Guardian.id)).filter(Guardian.company_id == cid).scalar()}


@router.get("/mine")
def my_students(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Portal: a parent's children, or the signed-in student. Empty for staff."""
    ids = service.visible_student_ids(db, user)
    if not ids:
        return []
    rows = db.query(Student).filter(Student.company_id == user["company_id"], Student.id.in_(ids)).order_by(
        Student.date_of_birth).all()
    return [service.student_brief(s, service.current_enrollment(db, user["company_id"], s.id)) for s in rows]


@router.get("/section/{section_id}/roster")
def section_roster(section_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Learners of a section by roll number. Staff with students.view, or a teacher of the section."""
    cid = user["company_id"]
    if VIEW not in _perms(db, user) and section_id not in academics.teacher_section_ids(db, cid, user.get("employee_id")):
        raise HTTPException(status_code=403, detail="You don't teach this section.")
    return [service.student_brief(s, e) for s, e in service.section_students(db, cid, section_id)]


# ── Create / read / update ───────────────────────────────────────────

def _save_guardian(db: Session, cid: int, student: Student, g: GuardianIn) -> StudentGuardian:
    if g.id:
        guardian = by_id_or_400(db, Guardian, cid, g.id, "The guardian")
    else:
        if not g.first_name:
            raise HTTPException(status_code=400, detail="Each parent or guardian needs a first name.")
        guardian = None
        if g.phone:  # reuse a guardian already on record with the same phone (siblings)
            guardian = db.query(Guardian).filter(Guardian.company_id == cid, Guardian.phone == g.phone,
                                                 Guardian.first_name.ilike(g.first_name)).first()
        if guardian is None:
            guardian = Guardian(company_id=cid, **g.model_dump(exclude={"id", "relation", "is_primary", "can_pickup",
                                                                         "receives_notifications"}))
            db.add(guardian)
            db.flush()
    link = db.query(StudentGuardian).filter(StudentGuardian.student_id == student.id,
                                            StudentGuardian.guardian_id == guardian.id).first()
    if link is None:
        link = StudentGuardian(company_id=cid, student_id=student.id, guardian_id=guardian.id)
        db.add(link)
    link.relation, link.is_primary = g.relation, g.is_primary
    link.can_pickup, link.receives_notifications = g.can_pickup, g.receives_notifications
    if g.is_primary:
        db.query(StudentGuardian).filter(StudentGuardian.student_id == student.id,
                                         StudentGuardian.guardian_id != guardian.id).update({"is_primary": False})
    db.flush()
    return link


def create_student(db: Session, cid: int, payload: StudentCreate) -> tuple[Student, list[dict]]:
    """Shared by the API and the admissions module. Returns the student and any new portal logins."""
    data = payload.model_dump(exclude={"admission_no", "section_id", "program_id", "roll_no", "guardians",
                                       "create_portal_accounts", "custom"})
    if data.get("house_id"):
        from modules.academics.models import House
        by_id_or_400(db, House, cid, data["house_id"], "The house")
    adm = (payload.admission_no or "").strip() or service.new_admission_no(db, cid)
    if db.query(Student.id).filter(Student.company_id == cid, Student.admission_no == adm).first():
        raise HTTPException(status_code=409, detail=f"Admission number {adm} is already used.")
    s = Student(company_id=cid, admission_no=adm, **data,
                custom=custom_fields.merge_values(db, cid, "student", payload.custom) if payload.custom else {})
    if not s.admission_date:
        s.admission_date = date.today()
    db.add(s)
    db.flush()
    if payload.section_id or payload.program_id:
        service.enroll(db, cid, s, payload.section_id, payload.program_id, roll_no=payload.roll_no)
    for g in payload.guardians:
        _save_guardian(db, cid, s, g)
    logins = []
    if payload.create_portal_accounts:
        logins = _make_accounts(db, cid, s)
    return s, logins


def _make_accounts(db: Session, cid: int, s: Student) -> list[dict]:
    out = []
    acc, pw = service.ensure_portal_account(db, cid, student=s)
    if pw:
        out.append({"kind": "student", "name": s.full_name, "login": acc.login, "password": pw})
    for l in db.query(StudentGuardian).filter(StudentGuardian.student_id == s.id).all():
        g = db.query(Guardian).get(l.guardian_id)
        if not (g.email or g.phone):
            continue
        acc, pw = service.ensure_portal_account(db, cid, guardian=g)
        if pw:
            out.append({"kind": "parent", "name": g.full_name, "login": acc.login, "password": pw})
    return out


@router.post("", status_code=201)
def add_student(payload: StudentCreate, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    s, logins = create_student(db, user["company_id"], payload)
    db.commit()
    db.refresh(s)
    out = _student_out(db, s, service.current_enrollment(db, user["company_id"], s.id), _perms(db, user), full=True)
    out["new_logins"] = logins
    return out


class ImportRow(StudentBase):
    admission_no: str | None = None
    program: str | None = None
    section: str | None = None
    roll_no: str | None = None
    father_name: str | None = None
    mother_name: str | None = None
    guardian_phone: str | None = None
    guardian_email: EmailStr | None = None


class ImportIn(BaseModel):
    rows: list[ImportRow] = Field(..., max_length=5000)
    dry_run: bool = False


@router.post("/import")
def import_students(payload: ImportIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    """Bulk add learners from a spreadsheet (parsed in the browser). Class and section are matched
    by name in the current session; father/mother become guardians."""
    cid = user["company_id"]
    from modules.academics.models import Program
    sess = academics.current_session(db, cid)
    sections = {}
    if sess:
        for sec in db.query(Section).options(joinedload(Section.program)).filter(Section.session_id == sess.id).all():
            sections[(sec.program.name.lower(), sec.name.lower())] = sec.id
    programs = {p.name.lower(): p.id for p in db.query(Program).filter(Program.company_id == cid).all()}
    errors, created = [], 0
    for i, row in enumerate(payload.rows, 1):
        section_id = program_id = None
        if row.program:
            program_id = programs.get(row.program.strip().lower())
            if program_id is None:
                errors.append({"row": i, "message": f"Class '{row.program}' not found"})
                continue
            if row.section:
                section_id = sections.get((row.program.strip().lower(), row.section.strip().lower()))
                if section_id is None:
                    errors.append({"row": i, "message": f"Section '{row.section}' not found in {row.program}"})
                    continue
        guardians = []
        if row.father_name:
            f, _, l = row.father_name.strip().partition(" ")
            guardians.append(GuardianIn(first_name=f, last_name=l or None, relation="father", is_primary=True,
                                        phone=row.guardian_phone, email=row.guardian_email))
        if row.mother_name:
            f, _, l = row.mother_name.strip().partition(" ")
            guardians.append(GuardianIn(first_name=f, last_name=l or None, relation="mother",
                                        phone=None if row.father_name else row.guardian_phone))
        data = row.model_dump(exclude={"program", "section", "father_name", "mother_name", "guardian_phone",
                                       "guardian_email"})
        try:
            with db.begin_nested():
                create_student(db, cid, StudentCreate(**data, section_id=section_id, program_id=program_id,
                                                      guardians=guardians))
            created += 1
        except HTTPException as exc:
            errors.append({"row": i, "message": exc.detail})
    if payload.dry_run:
        db.rollback()
    else:
        audit.event(db, "students.imported", "students", None, {"created": created, "errors": len(errors)})
        db.commit()
    return {"created": created, "errors": errors, "dry_run": payload.dry_run}


def _student(db: Session, user: dict, uuid: UUID) -> Student:
    s = get_or_404(db, Student, user["company_id"], uuid, "Student")
    return s


# ── Portal summary, ID cards and bulk sign-ins for a section ─────────
# (declared before /{uuid} so these fixed paths win)

@router.get("/portal/summary")
def portal_summary(student_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """A learner's card for the home page: photo, class, roll number, class teacher, house.
    Parents see only their children, students themselves; staff need students.view."""
    cid = user["company_id"]
    ids = service.visible_student_ids(db, user)
    if ids is None and VIEW not in _perms(db, user):
        raise HTTPException(status_code=403, detail="Permission denied")
    if ids is not None and student_id not in ids:
        raise HTTPException(status_code=404, detail="Student not found")
    s = db.query(Student).options(joinedload(Student.house)).filter(Student.company_id == cid,
                                                                     Student.id == student_id).first()
    if s is None:
        raise HTTPException(status_code=404, detail="Student not found")
    e = service.current_enrollment(db, cid, s.id)
    out = service.student_brief(s, e)
    ct = e.section.class_teacher if e and e.section else None
    out.update({"class_teacher_name": f"{ct.first_name} {ct.last_name or ''}".strip() if ct else None,
                "house_name": s.house.name if s.house else None, "house_color": s.house.color if s.house else None,
                "session_name": e.session.name if e and e.session else None})
    return out


@router.get("/section/{section_id}/id-cards")
def section_id_cards(section_id: int, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    """Everything printed on the ID cards of a section's learners, by roll number."""
    cid = user["company_id"]
    sec = by_id_or_400(db, Section, cid, section_id, "The section")
    rows = service.section_students(db, cid, section_id)
    sids = [s.id for s, _ in rows]
    links: dict[int, list[StudentGuardian]] = {}
    for l in db.query(StudentGuardian).options(joinedload(StudentGuardian.guardian)).filter(
            StudentGuardian.company_id == cid, StudentGuardian.student_id.in_(sids or [-1])).all():
        links.setdefault(l.student_id, []).append(l)
    out = []
    for s, e in rows:
        ls = sorted(links.get(s.id, []), key=lambda l: (not l.is_primary, l.id))
        g = ls[0].guardian if ls else None
        out.append({**service.student_brief(s, e), "blood_group": s.blood_group, "date_of_birth": s.date_of_birth,
                    "address": s.address, "city": s.city, "house_name": s.house.name if s.house else None,
                    "guardian_name": g.full_name if g else None, "guardian_phone": g.phone if g else None})
    return {"section": {"id": sec.id, "label": academics.section_label(sec),
                        "session_name": sec.session.name if sec.session else None},
            "students": out}


@router.post("/section/{section_id}/portal-accounts")
def section_portal_accounts(section_id: int, db: Session = Depends(get_db),
                            user: dict = Depends(require_permission("students.portal.manage"))):
    """Create sign-ins for every learner of a section and their guardians (those with an email
    or phone). Existing sign-ins are left alone; new passwords are returned once."""
    cid = user["company_id"]
    sec = by_id_or_400(db, Section, cid, section_id, "The section")
    logins, skipped = [], []
    for s, _ in service.section_students(db, cid, section_id):
        try:
            with db.begin_nested():
                for row in _make_accounts(db, cid, s):
                    logins.append({**row, "student": s.full_name, "admission_no": s.admission_no})
        except HTTPException as exc:
            skipped.append({"student": s.full_name, "reason": exc.detail})
    audit.event(db, "students.portal_accounts_created", "academic_sections", sec.id,
                {"count": len(logins), "skipped": len(skipped)}, entity_label=academics.section_label(sec))
    db.commit()
    return {"new_logins": logins, "skipped": skipped, "section": academics.section_label(sec)}


@router.get("/{uuid}")
def get_student(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    s = _student(db, user, uuid)
    perms = _perms(db, user)
    if VIEW not in perms:
        ids = service.visible_student_ids(db, user)
        if ids is not None:
            if s.id not in ids:
                raise HTTPException(status_code=404, detail="Student not found")
        elif service.student_section_id(db, user["company_id"], s.id) not in academics.teacher_section_ids(
                db, user["company_id"], user.get("employee_id")):
            raise HTTPException(status_code=404, detail="Student not found")
    return _student_out(db, s, service.current_enrollment(db, user["company_id"], s.id), perms, full=True)


@router.put("/{uuid}")
def update_student(uuid: UUID, payload: StudentUpdate, db: Session = Depends(get_db),
                   user: dict = Depends(require_permission(MANAGE))):
    s = _student(db, user, uuid)
    data = payload.model_dump(exclude_unset=True)
    if "admission_no" in data and data["admission_no"] != s.admission_no:
        if db.query(Student.id).filter(Student.company_id == s.company_id, Student.admission_no == data["admission_no"],
                                       Student.id != s.id).first():
            raise HTTPException(status_code=409, detail="That admission number is already used.")
    if "custom" in data:
        data["custom"] = custom_fields.merge_values(db, s.company_id, "student", data["custom"] or {}, s.custom or {})
    if data.get("house_id"):
        from modules.academics.models import House
        by_id_or_400(db, House, s.company_id, data["house_id"], "The house")
    for k, v in data.items():
        setattr(s, k, v)
    db.commit()
    db.refresh(s)
    return _student_out(db, s, service.current_enrollment(db, s.company_id, s.id), _perms(db, user), full=True)


class StatusIn(BaseModel):
    status: str = Field(..., pattern="^(active|alumni|transferred|left|suspended|graduated)$")
    on: date | None = None
    reason: str | None = None


@router.post("/{uuid}/status")
def change_status(uuid: UUID, payload: StatusIn, db: Session = Depends(get_db),
                  user: dict = Depends(require_permission(MANAGE))):
    s = _student(db, user, uuid)
    s.status = payload.status
    if payload.status == "active":
        s.left_on, s.leaving_reason = None, None
    else:
        s.left_on = payload.on or date.today()
        s.leaving_reason = payload.reason
        e = service.current_enrollment(db, s.company_id, s.id)
        if e and e.status == "enrolled" and payload.status in ("transferred", "left"):
            e.status, e.left_on = ("transferred" if payload.status == "transferred" else "dropped"), s.left_on
        db.query(PortalAccount).filter(PortalAccount.student_id == s.id).update(
            {"active": payload.status not in ("left", "transferred")})
    audit.event(db, "students.status_changed", "students", s.id, {"status": payload.status, "reason": payload.reason},
                entity_label=s.full_name)
    db.commit()
    return {"status": s.status}


class EnrollIn(BaseModel):
    section_id: int | None = None
    program_id: int | None = None
    session_id: int | None = None
    roll_no: str | None = Field(None, max_length=20)
    term_no: int | None = None
    elective_subject_ids: list[int] | None = None


@router.post("/{uuid}/enroll")
def enroll_student(uuid: UUID, payload: EnrollIn, db: Session = Depends(get_db),
                   user: dict = Depends(require_permission(MANAGE))):
    s = _student(db, user, uuid)
    e = service.enroll(db, s.company_id, s, payload.section_id, payload.program_id, payload.session_id,
                       payload.roll_no, payload.term_no)
    if payload.elective_subject_ids is not None:
        e.elective_subject_ids = payload.elective_subject_ids
    db.commit()
    return {"enrollment_id": e.id, "roll_no": e.roll_no}


class PromoteIn(BaseModel):
    from_section_id: int
    to_section_id: int | None = None
    student_ids: list[int]
    outcome: str = Field("promoted", pattern="^(promoted|detained|completed)$")


@router.post("/promote")
def promote(payload: PromoteIn, db: Session = Depends(get_db), user: dict = Depends(require_permission("students.promote"))):
    res = service.promote(db, user["company_id"], payload.from_section_id, payload.to_section_id, payload.student_ids,
                          payload.outcome)
    audit.event(db, "students.promoted", "academic_sections", payload.from_section_id,
                {"to_section_id": payload.to_section_id, "outcome": payload.outcome, "count": res["moved"]})
    db.commit()
    return res


class RenumberIn(BaseModel):
    by: str = Field("name", pattern="^(name|admission)$")


@router.post("/section/{section_id}/renumber")
def renumber(section_id: int, payload: RenumberIn, db: Session = Depends(get_db),
             user: dict = Depends(require_permission(MANAGE))):
    by_id_or_400(db, Section, user["company_id"], section_id, "The section")
    n = service.renumber_rolls(db, user["company_id"], section_id, payload.by)
    db.commit()
    return {"renumbered": n}


# ── Guardians of a student ───────────────────────────────────────────

@router.post("/{uuid}/guardians")
def add_guardian(uuid: UUID, payload: GuardianIn, db: Session = Depends(get_db),
                 user: dict = Depends(require_permission(MANAGE))):
    s = _student(db, user, uuid)
    link = _save_guardian(db, s.company_id, s, payload)
    db.commit()
    db.refresh(link)
    return _link_out(link)


@router.delete("/{uuid}/guardians/{link_id}")
def remove_guardian(uuid: UUID, link_id: int, db: Session = Depends(get_db),
                    user: dict = Depends(require_permission(MANAGE))):
    s = _student(db, user, uuid)
    link = db.query(StudentGuardian).filter(StudentGuardian.id == link_id, StudentGuardian.student_id == s.id).first()
    if link is None:
        raise HTTPException(status_code=404, detail="Guardian link not found")
    db.delete(link)
    db.commit()
    return {"detail": "Removed"}


# ── Documents ────────────────────────────────────────────────────────

class DocIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    doc_type: str | None = Field(None, max_length=60)
    file_url: str | None = None
    upload_uuid: UUID | None = None  # a file from POST /documents/uploads (links are signed when read)
    expires_on: date | None = None


UPLOAD_PREFIX = "upload:"


def _doc_links(db: Session, cid: int, docs: list[StudentDocument]) -> dict[str, dict]:
    """Fresh signed links for documents stored as uploaded files."""
    ids = [d.file_url[len(UPLOAD_PREFIX):] for d in docs if (d.file_url or "").startswith(UPLOAD_PREFIX)]
    if not ids:
        return {}
    from modules.documents import storage
    from modules.documents.models import FileBlob
    rows = db.query(FileBlob).filter(FileBlob.company_id == cid, FileBlob.uuid.in_(ids)).all()
    return {str(r.uuid): storage.describe(r) for r in rows}


@router.get("/{uuid}/documents")
def list_documents(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    s = _student(db, user, uuid)
    if VIEW not in _perms(db, user):
        service.require_student_access(db, user, s.id)
        if service.visible_student_ids(db, user) is None:
            raise HTTPException(status_code=403, detail="Permission denied")
    rows = db.query(StudentDocument).filter(StudentDocument.student_id == s.id).order_by(StudentDocument.created_at.desc()).all()
    links = _doc_links(db, s.company_id, rows)

    def one(d: StudentDocument) -> dict:
        f = links.get((d.file_url or "")[len(UPLOAD_PREFIX):]) if (d.file_url or "").startswith(UPLOAD_PREFIX) else None
        return {"id": d.id, "uuid": str(d.uuid), "title": d.title, "doc_type": d.doc_type,
                "file_url": f["url"] if f else d.file_url, "download_url": f["download_url"] if f else d.file_url,
                "file_name": f["name"] if f else None, "file_size": f["size"] if f else None,
                "verified": d.verified, "expires_on": d.expires_on, "uploaded_by": d.uploaded_by,
                "created_at": d.created_at}
    return [one(d) for d in rows]


@router.post("/{uuid}/documents", status_code=201)
def add_document(uuid: UUID, payload: DocIn, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    s = _student(db, user, uuid)
    if MANAGE not in _perms(db, user):
        service.require_student_access(db, user, s.id)
        if service.visible_student_ids(db, user) is None:
            raise HTTPException(status_code=403, detail="Permission denied")
    from core import context
    data = payload.model_dump(exclude={"upload_uuid"})
    if payload.upload_uuid:
        from modules.documents.models import FileBlob
        if not db.query(FileBlob.id).filter(FileBlob.company_id == s.company_id,
                                            FileBlob.uuid == payload.upload_uuid).first():
            raise HTTPException(status_code=400, detail="The file could not be found. Upload it again.")
        data["file_url"] = f"{UPLOAD_PREFIX}{payload.upload_uuid}"
    if not data.get("file_url"):
        raise HTTPException(status_code=400, detail="Attach a file.")
    d = StudentDocument(company_id=s.company_id, student_id=s.id, uploaded_by=context.current().actor_name, **data)
    db.add(d)
    db.commit()
    return {"id": d.id, "uuid": str(d.uuid)}


@router.post("/{uuid}/documents/{doc_id}/verify")
def verify_document(uuid: UUID, doc_id: int, db: Session = Depends(get_db),
                    user: dict = Depends(require_permission(MANAGE))):
    s = _student(db, user, uuid)
    d = db.query(StudentDocument).filter(StudentDocument.id == doc_id, StudentDocument.student_id == s.id).first()
    if d is None:
        raise HTTPException(status_code=404, detail="Document not found")
    d.verified = True
    db.commit()
    return {"verified": True}


@router.delete("/{uuid}/documents/{doc_id}")
def delete_document(uuid: UUID, doc_id: int, db: Session = Depends(get_db),
                    user: dict = Depends(require_permission(MANAGE))):
    s = _student(db, user, uuid)
    d = db.query(StudentDocument).filter(StudentDocument.id == doc_id, StudentDocument.student_id == s.id).first()
    if d is None:
        raise HTTPException(status_code=404, detail="Document not found")
    db.delete(d)
    db.commit()
    return {"detail": "Removed"}


# ── Portal accounts ──────────────────────────────────────────────────

class AccountIn(BaseModel):
    login: str | None = Field(None, max_length=255)
    password: str | None = Field(None, min_length=8, max_length=100)


@router.post("/{uuid}/portal-accounts")
def create_accounts(uuid: UUID, db: Session = Depends(get_db),
                    user: dict = Depends(require_permission("students.portal.manage"))):
    """Create sign-ins for the learner and every guardian with an email or phone. New passwords
    are returned once so the office can hand them over (or send them)."""
    s = _student(db, user, uuid)
    logins = _make_accounts(db, s.company_id, s)
    audit.event(db, "students.portal_accounts_created", "students", s.id, {"count": len(logins)}, entity_label=s.full_name)
    db.commit()
    return {"new_logins": logins}


@guardians_router.get("")
def list_guardians(q: str | None = None, params: PageParams = Depends(), db: Session = Depends(get_db),
                   user: dict = Depends(require_permission(VIEW))):
    from sqlalchemy import or_
    query = db.query(Guardian).options(joinedload(Guardian.student_links).joinedload(StudentGuardian.student)).filter(
        Guardian.company_id == user["company_id"])
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Guardian.first_name.ilike(like), Guardian.last_name.ilike(like),
                                 Guardian.phone.ilike(like), Guardian.email.ilike(like)))
    accounts = {a.guardian_id: a for a in db.query(PortalAccount).filter(
        PortalAccount.company_id == user["company_id"], PortalAccount.kind == "parent").all()}
    return paginate(query.order_by(Guardian.first_name), params, lambda g: {
        "id": g.id, "uuid": str(g.uuid), "name": g.full_name, "first_name": g.first_name, "last_name": g.last_name,
        "email": g.email, "phone": g.phone, "occupation": g.occupation,
        "children": [{"id": l.student.id, "uuid": str(l.student.uuid), "name": l.student.full_name,
                      "relation": l.relation} for l in g.student_links],
        "portal_account": _account_out(accounts[g.id]) if g.id in accounts else None,
    })


class GuardianPatch(BaseModel):
    first_name: str | None = Field(None, min_length=1, max_length=100)
    last_name: str | None = None
    email: EmailStr | None = None
    phone: str | None = None
    alt_phone: str | None = None
    occupation: str | None = None
    organisation: str | None = None
    annual_income: Decimal | None = None
    education: str | None = None
    address: str | None = None
    national_id: str | None = None


@guardians_router.put("/{uuid}")
def update_guardian(uuid: UUID, payload: GuardianPatch, db: Session = Depends(get_db),
                    user: dict = Depends(require_permission(MANAGE))):
    g = get_or_404(db, Guardian, user["company_id"], uuid, "Guardian")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(g, k, v)
    db.commit()
    return {"id": g.id, "name": g.full_name}


@guardians_router.post("/{uuid}/portal-account")
def guardian_account(uuid: UUID, payload: AccountIn, db: Session = Depends(get_db),
                     user: dict = Depends(require_permission("students.portal.manage"))):
    g = get_or_404(db, Guardian, user["company_id"], uuid, "Guardian")
    acc, pw = service.ensure_portal_account(db, g.company_id, guardian=g, login=payload.login, password=payload.password)
    db.commit()
    return {**_account_out(acc), "password": pw}


@portal_router.get("")
def list_accounts(kind: str | None = None, q: str | None = None, active: bool | None = None,
                  params: PageParams = Depends(),
                  db: Session = Depends(get_db), user: dict = Depends(require_permission("students.portal.manage"))):
    query = db.query(PortalAccount).options(joinedload(PortalAccount.student), joinedload(PortalAccount.guardian)).filter(
        PortalAccount.company_id == user["company_id"])
    if kind:
        query = query.filter(PortalAccount.kind == kind)
    if active is not None:
        query = query.filter(PortalAccount.active.is_(active))
    if q:
        like = f"%{q.strip()}%"
        query = query.outerjoin(Student, Student.id == PortalAccount.student_id).outerjoin(
            Guardian, Guardian.id == PortalAccount.guardian_id).filter(
            or_(PortalAccount.login.ilike(like), Student.first_name.ilike(like), Student.last_name.ilike(like),
                Student.admission_no.ilike(like), Guardian.first_name.ilike(like), Guardian.last_name.ilike(like)))
    page = paginate(query.order_by(PortalAccount.login), params)
    gids = [a.guardian_id for a in page["items"] if a.guardian_id]
    children: dict[int, list[str]] = {}
    for l in db.query(StudentGuardian).options(joinedload(StudentGuardian.student)).filter(
            StudentGuardian.company_id == user["company_id"], StudentGuardian.guardian_id.in_(gids or [-1])).all():
        children.setdefault(l.guardian_id, []).append(l.student.full_name)
    page["items"] = [{
        **_account_out(a), "name": (a.student.full_name if a.student else a.guardian.full_name if a.guardian else ""),
        "student_uuid": str(a.student.uuid) if a.student else None,
        "admission_no": a.student.admission_no if a.student else None,
        "guardian_uuid": str(a.guardian.uuid) if a.guardian else None,
        "children": children.get(a.guardian_id, []) if a.guardian_id else [],
    } for a in page["items"]]
    return page


@portal_router.post("/{uuid}/reset-password")
def reset_account_password(uuid: UUID, payload: AccountIn, db: Session = Depends(get_db),
                           user: dict = Depends(require_permission("students.portal.manage"))):
    from core.security import hash_password
    acc = get_or_404(db, PortalAccount, user["company_id"], uuid, "Account")
    pw = payload.password or service.random_password()
    acc.password = hash_password(pw)
    acc.must_change_password = payload.password is None
    audit.event(db, "students.portal_password_reset", "portal_accounts", acc.id, {}, entity_label=acc.login)
    db.commit()
    return {"login": acc.login, "password": pw}


class ActiveIn(BaseModel):
    active: bool


@portal_router.post("/{uuid}/active")
def set_account_active(uuid: UUID, payload: ActiveIn, db: Session = Depends(get_db),
                       user: dict = Depends(require_permission("students.portal.manage"))):
    acc = get_or_404(db, PortalAccount, user["company_id"], uuid, "Account")
    acc.active = payload.active
    db.commit()
    return _account_out(acc)
