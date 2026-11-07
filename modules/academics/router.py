"""Academics API: institution profile, sessions, terms, programs, sections, subjects,
curriculum, teacher allocation, rooms and houses."""
from datetime import date
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from core.crud import by_id_or_400, crud_router, default_out, get_or_404
from core.db import get_db
from core.security import get_current_user, require_permission
from modules.academics import service
from modules.academics.models import (AcademicSession, House, Program, ProgramSubject, Room, Section,
                                      SectionSubject, Subject, Term)

router = APIRouter(prefix="/academics", tags=["Academics"])
VIEW, MANAGE = "academics.view", "academics.manage"


def _name(e) -> str | None:
    return f"{e.first_name} {e.last_name}".strip() if e else None


# ── Institution profile ──────────────────────────────────────────────

class ProfileIn(BaseModel):
    type: str | None = None
    board: str | None = Field(None, max_length=120)
    affiliation_no: str | None = Field(None, max_length=60)
    established_year: int | None = None
    motto: str | None = Field(None, max_length=200)
    principal_name: str | None = Field(None, max_length=120)
    terms: dict[str, str] | None = None
    modules: dict[str, bool] | None = None
    week_days: list[int] | None = None
    attendance_mode: str | None = Field(None, pattern="^(daily|period|session)$")
    grading: str | None = Field(None, max_length=40)


def _company(db: Session, company_id: int):
    from modules.company.models import Company
    return db.query(Company).filter(Company.id == company_id).first()


@router.get("/profile")
def get_profile(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Institution type, the words used on screen and the modules switched on. Open to every
    signed-in person (parents and students included) so menus and labels can follow it."""
    company = _company(db, user["company_id"])
    out = service.profile(company)
    sess = service.current_session(db, user["company_id"])
    out["current_session"] = {"id": sess.id, "uuid": str(sess.uuid), "name": sess.name} if sess else None
    term = service.current_term(db, user["company_id"])
    out["current_term"] = {"id": term.id, "name": term.name} if term else None
    return out


@router.put("/profile")
def update_profile(payload: ProfileIn, db: Session = Depends(get_db),
                   user: dict = Depends(require_permission("academics.profile.manage"))):
    if payload.type and payload.type not in service.INSTITUTION_TYPES:
        raise HTTPException(status_code=400, detail="Pick an institution type from the list.")
    return service.save_profile(db, _company(db, user["company_id"]), payload.model_dump(exclude_unset=True))


class QuickSetupIn(BaseModel):
    institution_type: str = "school"
    start_month: int = Field(4, ge=1, le=12)
    programs: list[str] | None = None
    sections: list[str] | None = None
    subjects: list[str] | None = None


@router.post("/quick-setup")
def quick_setup(payload: QuickSetupIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    """Create the current session, its terms, programs, sections and subjects in one step."""
    subjects = None
    if payload.subjects is not None:
        subjects = [(s.strip(), s.strip()[:3].upper(), "theory") for s in payload.subjects if s.strip()]
    res = service.quick_setup(db, user["company_id"], institution_type=payload.institution_type,
                              start_month=payload.start_month,
                              programs=[p.strip() for p in payload.programs if p.strip()] if payload.programs is not None else None,
                              sections=[s.strip() for s in payload.sections if s.strip()] if payload.sections is not None else None,
                              subjects=subjects)
    company = _company(db, user["company_id"])
    service.save_profile(db, company, {"type": payload.institution_type})
    db.commit()
    return res


@router.get("/overview")
def overview(db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    cid = user["company_id"]
    sess = service.current_session(db, cid)
    sections = db.query(func.count(Section.id)).filter(Section.company_id == cid, Section.active.is_(True),
                                                       Section.session_id == (sess.id if sess else -1)).scalar()
    return {
        "session": sess.name if sess else None,
        "programs": db.query(func.count(Program.id)).filter(Program.company_id == cid, Program.active.is_(True)).scalar(),
        "sections": sections,
        "subjects": db.query(func.count(Subject.id)).filter(Subject.company_id == cid, Subject.active.is_(True)).scalar(),
        "rooms": db.query(func.count(Room.id)).filter(Room.company_id == cid, Room.active.is_(True)).scalar(),
    }


@router.get("/my-classes")
def my_classes(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """The signed-in teacher's sections in the current session and the subjects they teach there."""
    return service.my_classes(db, user["company_id"], user.get("employee_id"))


# ── Sessions and terms ───────────────────────────────────────────────

class SessionIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    start_date: date
    end_date: date
    status: str = Field("active", pattern="^(planned|active|closed)$")
    is_current: bool = False

    @model_validator(mode="after")
    def _dates(self):
        if self.end_date <= self.start_date:
            raise ValueError("The end date must be after the start date")
        return self


class SessionPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=60)
    start_date: date | None = None
    end_date: date | None = None
    status: str | None = Field(None, pattern="^(planned|active|closed)$")
    active: bool | None = None


def _session_out(s: AcademicSession) -> dict:
    return {**default_out(s), "terms": [default_out(t) for t in s.terms if t.active]}


def _one_current(db: Session, company_id: int, row) -> None:
    if getattr(row, "is_current", False):
        db.query(AcademicSession).filter(AcademicSession.company_id == company_id,
                                         AcademicSession.id != row.id).update({"is_current": False})


router.include_router(crud_router(
    model=AcademicSession, prefix="/sessions", tag="Academics", create_schema=SessionIn, update_schema=SessionPatch,
    view_perm=VIEW, manage_perm=MANAGE, out=_session_out, search_fields=("name",), order_by=("start_date",),
    label="Session", after_change=_one_current,
))


@router.post("/sessions/{uuid}/make-current")
def make_current(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    sess = get_or_404(db, AcademicSession, user["company_id"], uuid, "Session")
    db.query(AcademicSession).filter(AcademicSession.company_id == user["company_id"]).update({"is_current": False})
    sess.is_current = True
    db.commit()
    return _session_out(sess)


class TermIn(BaseModel):
    session_id: int
    name: str = Field(..., min_length=1, max_length=60)
    kind: str = Field("term", pattern="^(term|semester|trimester|quarter|module)$")
    start_date: date
    end_date: date
    sort_order: int = 1


class TermPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=60)
    kind: str | None = Field(None, pattern="^(term|semester|trimester|quarter|module)$")
    start_date: date | None = None
    end_date: date | None = None
    sort_order: int | None = None
    active: bool | None = None


def _check_term(db: Session, cid: int, data: dict, row) -> None:
    if "session_id" in data:
        by_id_or_400(db, AcademicSession, cid, data["session_id"], "The session")


router.include_router(crud_router(
    model=Term, prefix="/terms", tag="Academics", create_schema=TermIn, update_schema=TermPatch,
    view_perm=VIEW, manage_perm=MANAGE, order_by=("session_id", "sort_order"), label="Term",
    filters=("session_id",), validate=_check_term,
))


# ── Programs (classes / courses) ─────────────────────────────────────

class ProgramIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    code: str | None = Field(None, max_length=30)
    level: str | None = Field(None, max_length=60)
    department_id: int | None = None
    duration_terms: int | None = Field(None, ge=1, le=40)
    grading_scheme_id: int | None = None
    next_program_id: int | None = None
    sort_order: int = 1
    description: str | None = None
    config: dict = {}


class ProgramPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    code: str | None = Field(None, max_length=30)
    level: str | None = Field(None, max_length=60)
    department_id: int | None = None
    duration_terms: int | None = Field(None, ge=1, le=40)
    grading_scheme_id: int | None = None
    next_program_id: int | None = None
    sort_order: int | None = None
    description: str | None = None
    config: dict | None = None
    active: bool | None = None


def _program_out(p: Program) -> dict:
    return {**default_out(p), "section_count": len([s for s in p.sections if s.active])}


def _check_program(db: Session, cid: int, data: dict, row) -> None:
    if data.get("next_program_id"):
        if row is not None and data["next_program_id"] == row.id:
            raise HTTPException(status_code=400, detail="A class can't be promoted into itself.")
        by_id_or_400(db, Program, cid, data["next_program_id"], "The next class")
    if data.get("department_id"):
        from modules.departments.models import Department
        if not db.query(Department.id).filter(Department.id == data["department_id"],
                                              Department.company_id == cid).first():
            raise HTTPException(status_code=400, detail="The department was not found.")


router.include_router(crud_router(
    model=Program, prefix="/programs", tag="Academics", create_schema=ProgramIn, update_schema=ProgramPatch,
    view_perm=VIEW, manage_perm=MANAGE, out=_program_out, search_fields=("name", "code", "level"),
    order_by=("sort_order", "name"), label="Class", validate=_check_program, filters=("level",),
))


# ── Sections (divisions / batches) ───────────────────────────────────

class SectionIn(BaseModel):
    program_id: int
    session_id: int | None = None  # empty = current session
    name: str = Field(..., min_length=1, max_length=60)
    capacity: int | None = Field(None, ge=1, le=5000)
    class_teacher_id: int | None = None
    room_id: int | None = None
    medium: str | None = Field(None, max_length=40)
    shift: str | None = Field(None, max_length=40)


class SectionPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=60)
    capacity: int | None = Field(None, ge=1, le=5000)
    class_teacher_id: int | None = None
    room_id: int | None = None
    medium: str | None = Field(None, max_length=40)
    shift: str | None = Field(None, max_length=40)
    active: bool | None = None


def _section_out(s: Section) -> dict:
    from modules.students.service import section_strength
    return {
        **default_out(s), "label": service.section_label(s), "program_name": s.program.name if s.program else None,
        "session_name": s.session.name if s.session else None, "class_teacher_name": _name(s.class_teacher),
        "room_name": s.room.name if s.room else None, "strength": section_strength(s),
    }


def _check_section(db: Session, cid: int, data: dict, row) -> None:
    if row is None:
        by_id_or_400(db, Program, cid, data.get("program_id"), "The class")
        if not data.get("session_id"):
            sess = service.current_session(db, cid)
            if sess is None:
                raise HTTPException(status_code=400, detail="Add an academic session first.")
            data["session_id"] = sess.id
        by_id_or_400(db, AcademicSession, cid, data["session_id"], "The session")
    if data.get("class_teacher_id"):
        from modules.employees.models import Employee
        by_id_or_400(db, Employee, cid, data["class_teacher_id"], "The teacher")
    if data.get("room_id"):
        by_id_or_400(db, Room, cid, data["room_id"], "The room")


section_routes = crud_router(
    model=Section, prefix="/sections", tag="Academics", create_schema=SectionIn, update_schema=SectionPatch,
    view_perm=VIEW, manage_perm=MANAGE, out=_section_out, search_fields=("name",),
    order_by=("program_id", "name"), label="Section", validate=_check_section,
    filters=("program_id", "session_id", "class_teacher_id"),
)
router.include_router(section_routes)


@router.get("/section-options")
def section_options(session_id: int | None = None, db: Session = Depends(get_db),
                    user: dict = Depends(get_current_user)):
    """Every section of a session (default: current) as {id, label} for pickers. Teachers
    without academics.view see only their own sections."""
    from core.security import _get_role_permissions
    cid = user["company_id"]
    sess_id = session_id or (service.current_session(db, cid).id if service.current_session(db, cid) else None)
    if sess_id is None:
        return []
    q = (db.query(Section).options(joinedload(Section.program))
         .join(Program, Program.id == Section.program_id)
         .filter(Section.company_id == cid, Section.session_id == sess_id, Section.active.is_(True)))
    if VIEW not in _get_role_permissions(cid, user["role"], db):
        q = q.filter(Section.id.in_(service.teacher_section_ids(db, cid, user.get("employee_id"), sess_id) or {-1}))
    rows = q.order_by(Program.sort_order, Program.name, Section.name).all()
    return [{"id": s.id, "uuid": str(s.uuid), "label": service.section_label(s), "program_id": s.program_id} for s in rows]


# ── Subjects and curriculum ──────────────────────────────────────────

class SubjectIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    code: str | None = Field(None, max_length=30)
    kind: str = Field("theory", max_length=30)
    credits: Decimal | None = Field(None, ge=0, le=100)
    department_id: int | None = None
    color: str | None = Field(None, max_length=20)
    description: str | None = None


class SubjectPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    code: str | None = Field(None, max_length=30)
    kind: str | None = Field(None, max_length=30)
    credits: Decimal | None = Field(None, ge=0, le=100)
    department_id: int | None = None
    color: str | None = Field(None, max_length=20)
    description: str | None = None
    active: bool | None = None


router.include_router(crud_router(
    model=Subject, prefix="/subjects", tag="Academics", create_schema=SubjectIn, update_schema=SubjectPatch,
    view_perm=VIEW, manage_perm=MANAGE, search_fields=("name", "code"), order_by=("name",), label="Subject",
    filters=("kind",),
))


class ProgramSubjectIn(BaseModel):
    program_id: int
    subject_id: int
    term_no: int | None = Field(None, ge=1, le=40)
    is_elective: bool = False
    elective_group: str | None = Field(None, max_length=60)
    credits: Decimal | None = Field(None, ge=0, le=100)
    weekly_periods: int | None = Field(None, ge=0, le=80)
    sort_order: int = 1


class ProgramSubjectPatch(BaseModel):
    term_no: int | None = Field(None, ge=1, le=40)
    is_elective: bool | None = None
    elective_group: str | None = Field(None, max_length=60)
    credits: Decimal | None = Field(None, ge=0, le=100)
    weekly_periods: int | None = Field(None, ge=0, le=80)
    sort_order: int | None = None


def _ps_out(ps: ProgramSubject) -> dict:
    return {**default_out(ps), "subject_name": ps.subject.name if ps.subject else None,
            "subject_code": ps.subject.code if ps.subject else None,
            "effective_credits": ps.credits if ps.credits is not None else (ps.subject.credits if ps.subject else None)}


def _check_ps(db: Session, cid: int, data: dict, row) -> None:
    if row is None:
        by_id_or_400(db, Program, cid, data.get("program_id"), "The class")
        by_id_or_400(db, Subject, cid, data.get("subject_id"), "The subject")


router.include_router(crud_router(
    model=ProgramSubject, prefix="/curriculum", tag="Academics", create_schema=ProgramSubjectIn,
    update_schema=ProgramSubjectPatch, view_perm=VIEW, manage_perm=MANAGE, out=_ps_out, search_fields=(),
    order_by=("program_id", "term_no", "sort_order"), label="Curriculum subject", filters=("program_id", "term_no"),
    validate=_check_ps,
))


# ── Teacher allocation ───────────────────────────────────────────────

class AllocationIn(BaseModel):
    section_id: int
    subject_id: int
    teacher_id: int | None = None
    assistant_ids: list[int] = []
    weekly_periods: int | None = Field(None, ge=0, le=80)


class AllocationPatch(BaseModel):
    teacher_id: int | None = None
    assistant_ids: list[int] | None = None
    weekly_periods: int | None = Field(None, ge=0, le=80)


def _alloc_out(a: SectionSubject) -> dict:
    return {**default_out(a), "subject_name": a.subject.name if a.subject else None,
            "section_label": service.section_label(a.section), "teacher_name": _name(a.teacher)}


def _check_alloc(db: Session, cid: int, data: dict, row) -> None:
    from modules.employees.models import Employee
    if row is None:
        by_id_or_400(db, Section, cid, data.get("section_id"), "The section")
        by_id_or_400(db, Subject, cid, data.get("subject_id"), "The subject")
    if data.get("teacher_id"):
        by_id_or_400(db, Employee, cid, data["teacher_id"], "The teacher")
    for eid in data.get("assistant_ids") or []:
        by_id_or_400(db, Employee, cid, eid, "An assistant teacher")


router.include_router(crud_router(
    model=SectionSubject, prefix="/allocations", tag="Academics", create_schema=AllocationIn,
    update_schema=AllocationPatch, view_perm=VIEW, manage_perm=MANAGE, out=_alloc_out, search_fields=(),
    order_by=("section_id", "subject_id"), label="Allocation", filters=("section_id", "subject_id", "teacher_id"),
    validate=_check_alloc,
))


@router.post("/sections/{uuid}/apply-curriculum")
def apply_curriculum(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    """Add every curriculum subject of the section's class to the section (teachers left empty)."""
    sec = get_or_404(db, Section, user["company_id"], uuid, "Section")
    have = {a for (a,) in db.query(SectionSubject.subject_id).filter(SectionSubject.section_id == sec.id).all()}
    added = 0
    for ps in db.query(ProgramSubject).filter(ProgramSubject.program_id == sec.program_id).all():
        if ps.subject_id not in have:
            db.add(SectionSubject(company_id=sec.company_id, section_id=sec.id, subject_id=ps.subject_id,
                                  weekly_periods=ps.weekly_periods))
            have.add(ps.subject_id)
            added += 1
    db.commit()
    return {"added": added}


# ── Rooms and houses ─────────────────────────────────────────────────

class RoomIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    code: str | None = Field(None, max_length=30)
    building: str | None = Field(None, max_length=80)
    floor: str | None = Field(None, max_length=20)
    capacity: int | None = Field(None, ge=0, le=100000)
    kind: str = Field("classroom", pattern="^(classroom|lab|hall|library|sports|online|other)$")


class RoomPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    code: str | None = Field(None, max_length=30)
    building: str | None = Field(None, max_length=80)
    floor: str | None = Field(None, max_length=20)
    capacity: int | None = Field(None, ge=0, le=100000)
    kind: str | None = Field(None, pattern="^(classroom|lab|hall|library|sports|online|other)$")
    active: bool | None = None


router.include_router(crud_router(
    model=Room, prefix="/rooms", tag="Academics", create_schema=RoomIn, update_schema=RoomPatch,
    view_perm=VIEW, manage_perm=MANAGE, search_fields=("name", "code", "building"), label="Room", filters=("kind",),
))


class HouseIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    color: str | None = Field(None, max_length=20)
    motto: str | None = Field(None, max_length=200)


class HousePatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=60)
    color: str | None = Field(None, max_length=20)
    motto: str | None = Field(None, max_length=200)
    active: bool | None = None


router.include_router(crud_router(
    model=House, prefix="/houses", tag="Academics", create_schema=HouseIn, update_schema=HousePatch,
    view_perm=VIEW, manage_perm=MANAGE, label="House",
))
