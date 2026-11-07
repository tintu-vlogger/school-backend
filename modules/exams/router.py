"""Exams API: grading schemes, exam types, exams and papers, seating, hall tickets, marks entry,
co-scholastic grades, result rules, results and publishing, report cards, transcripts,
analysis, summaries and the parent / student portal (/exams/portal/...)."""
from datetime import date, time
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from core.crud import by_id_or_400, crud_router, get_or_404
from core.db import get_db
from core.pagination import PageParams, paginate
from core.security import get_current_user, require_permission, require_portal
from modules.academics import service as academics
from modules.academics.models import AcademicSession, Program, Room, Section, Subject, Term
from modules.exams import documents, service
from modules.exams.models import (CoScholasticArea, Exam, ExamPaper, ExamResult, ExamRoom, ExamType, GradingScheme,
                                  ReportTemplate, ResultRule)
from modules.students import service as students
from modules.students.models import Student

router = APIRouter(prefix="/exams", tags=["Exams"])

VIEW, MANAGE, MARKS, PUBLISH = service.VIEW, service.MANAGE, service.MARKS, service.PUBLISH
ANY_STAFF = (VIEW, MARKS, service.MARKS_ALL, service.MODERATE)


def require_any(*permissions: str):
    """Dependency: the signed-in person holds at least one of the permissions."""
    def checker(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
        if not service.perms(db, current_user) & set(permissions):
            raise HTTPException(status_code=403, detail="You don't have access to exams.")
        return current_user
    return checker


def _pdf(data: bytes, name: str) -> Response:
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{name}"'})


def _ids(raw: str | None) -> list[int] | None:
    if not raw:
        return None
    try:
        return [int(x) for x in raw.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="Section ids must be numbers.")


# ── Options for pickers ──────────────────────────────────────────────

@router.get("/options")
def options(session_id: int | None = None, db: Session = Depends(get_db),
            user: dict = Depends(require_any(*ANY_STAFF))):
    """Sessions, terms, programs, subjects, rooms, exam types, schemes and exams for pickers."""
    cid = user["company_id"]
    sess = (db.query(AcademicSession).filter(AcademicSession.company_id == cid, AcademicSession.id == session_id).first()
            if session_id else academics.current_session(db, cid))
    sid = sess.id if sess else None
    default_code = service.default_scheme_code(db, cid)
    return {
        "session": {"id": sess.id, "name": sess.name} if sess else None,
        "sessions": [{"id": s.id, "name": s.name, "is_current": s.is_current} for s in db.query(AcademicSession).filter(
            AcademicSession.company_id == cid, AcademicSession.active.is_(True)).order_by(AcademicSession.start_date.desc())],
        "terms": [{"id": t.id, "name": t.name, "start_date": t.start_date, "end_date": t.end_date}
                  for t in db.query(Term).filter(Term.company_id == cid, Term.session_id == sid, Term.active.is_(True))
                  .order_by(Term.sort_order)] if sid else [],
        "programs": [{"id": p.id, "name": p.name, "grading_scheme_id": p.grading_scheme_id} for p in db.query(Program).filter(
            Program.company_id == cid, Program.active.is_(True)).order_by(Program.sort_order, Program.name)],
        "subjects": [{"id": s.id, "name": s.name, "code": s.code} for s in db.query(Subject).filter(
            Subject.company_id == cid, Subject.active.is_(True)).order_by(Subject.name)],
        "rooms": [{"id": r.id, "name": r.name, "capacity": r.capacity} for r in db.query(Room).filter(
            Room.company_id == cid, Room.active.is_(True)).order_by(Room.name)],
        "exam_types": [{"id": t.id, "name": t.name, "category": t.category,
                        "default_max_marks": service.num(t.default_max_marks)}
                       for t in db.query(ExamType).filter(ExamType.company_id == cid, ExamType.active.is_(True))
                       .order_by(ExamType.sort_order)],
        "schemes": [{"id": s.id, "name": s.name, "code": s.code, "kind": s.kind, "is_default": s.code == default_code}
                    for s in db.query(GradingScheme).filter(GradingScheme.company_id == cid,
                                                            GradingScheme.active.is_(True)).order_by(GradingScheme.name)],
        "exams": [{"id": e.id, "uuid": str(e.uuid), "name": e.name, "term_id": e.term_id, "status": e.status,
                   "start_date": e.start_date}
                  for e in db.query(Exam).filter(Exam.company_id == cid, Exam.session_id == sid,
                                                 Exam.archived_at.is_(None)).order_by(Exam.start_date, Exam.id)] if sid else [],
        "rules": [{"id": r.id, "uuid": str(r.uuid), "name": r.name, "term_id": r.term_id}
                  for r in db.query(ResultRule).filter(ResultRule.company_id == cid, ResultRule.session_id == sid,
                                                       ResultRule.active.is_(True)).order_by(ResultRule.id)] if sid else [],
        "codes": [{"code": k, "label": v} for k, v in service.calc.CODES.items()],
        "default_scheme_code": default_code,
    }


# ── Grading schemes ──────────────────────────────────────────────────

class BandIn(BaseModel):
    label: str = Field(..., min_length=1, max_length=40)
    min_percent: Decimal = Field(..., ge=0, le=100)
    grade_point: Decimal | None = Field(None, ge=0, le=100)
    description: str | None = Field(None, max_length=120)
    is_pass: bool = True
    color: str | None = Field(None, max_length=20)


class SchemeIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    code: str | None = Field(None, max_length=40, pattern=r"^[a-z0-9_]*$")
    kind: str = Field("percentage_bands", pattern="^(percentage_bands|gpa|pass_fail|descriptive|marks)$")
    scale_max: Decimal | None = Field(None, gt=0, le=100)
    pass_percent: Decimal = Field(Decimal(33), ge=0, le=100)
    decimals: int = Field(2, ge=0, le=4)
    percent_formula: str | None = Field(None, max_length=200)
    description: str | None = None
    bands: list[BandIn] = []

    @field_validator("percent_formula")
    @classmethod
    def _formula(cls, v):
        if v:
            from core.expressions import ExpressionError, validate
            try:
                validate(v, {"cgpa", "gpa"})
            except ExpressionError as exc:
                raise ValueError(exc.message)
        return v or None


class SchemePatch(SchemeIn):
    name: str | None = Field(None, min_length=1, max_length=120)
    kind: str | None = Field(None, pattern="^(percentage_bands|gpa|pass_fail|descriptive|marks)$")
    pass_percent: Decimal | None = Field(None, ge=0, le=100)
    decimals: int | None = Field(None, ge=0, le=4)
    bands: list[BandIn] | None = None


@router.get("/schemes")
def list_schemes(include_old: bool = False, db: Session = Depends(get_db),
                 user: dict = Depends(require_any(*ANY_STAFF))):
    cid = user["company_id"]
    q = db.query(GradingScheme).options(joinedload(GradingScheme.bands)).filter(GradingScheme.company_id == cid)
    if not include_old:
        q = q.filter(GradingScheme.active.is_(True))
    default = service.default_scheme_code(db, cid)
    rows = q.order_by(GradingScheme.name, GradingScheme.version.desc()).all()
    usage = {}
    for p in db.query(Program).filter(Program.company_id == cid, Program.grading_scheme_id.isnot(None)).all():
        usage.setdefault(p.grading_scheme_id, []).append(p.name)
    return [{**service.scheme_out(s, default), "programs": usage.get(s.id, [])} for s in rows]


@router.post("/schemes", status_code=201)
def create_scheme(payload: SchemeIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    data = payload.model_dump()
    data["bands"] = [b for b in data["bands"]]
    s = service.save_scheme(db, user["company_id"], data)
    db.commit()
    return service.scheme_out(s, service.default_scheme_code(db, user["company_id"]))


@router.put("/schemes/{uuid}")
def update_scheme(uuid: UUID, payload: SchemePatch, db: Session = Depends(get_db),
                  user: dict = Depends(require_permission(MANAGE))):
    cid = user["company_id"]
    s = get_or_404(db, GradingScheme, cid, uuid, "Grading scheme")
    if not s.active:
        raise HTTPException(status_code=409, detail="This is an old version. Edit the current one.")
    data = payload.model_dump(exclude_unset=True)
    data.pop("code", None)
    s = service.save_scheme(db, cid, data, s)
    db.commit()
    return service.scheme_out(s, service.default_scheme_code(db, cid))


@router.post("/schemes/{uuid}/default")
def make_default_scheme(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    """Make this the institution's default grading (stored in the institution profile)."""
    cid = user["company_id"]
    s = get_or_404(db, GradingScheme, cid, uuid, "Grading scheme")
    academics.save_profile(db, service._company(db, cid), {"grading": s.code})
    return {"detail": f"{s.name} is now the default"}


@router.delete("/schemes/{uuid}")
def archive_scheme(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = user["company_id"]
    s = get_or_404(db, GradingScheme, cid, uuid, "Grading scheme")
    if s.code == service.default_scheme_code(db, cid):
        raise HTTPException(status_code=409, detail="This is the default scheme. Pick another default first.")
    s.active = False
    db.commit()
    return {"detail": "Switched off"}


@router.get("/schemes/{uuid}/lookup")
def scheme_lookup(uuid: UUID, percent: Decimal = Query(..., ge=0, le=100), db: Session = Depends(get_db),
                  user: dict = Depends(require_any(*ANY_STAFF))):
    s = get_or_404(db, GradingScheme, user["company_id"], uuid, "Grading scheme")
    return {"percent": float(percent), "band": service.lookup(s, percent)}


class ProgramSchemeIn(BaseModel):
    program_id: int
    grading_scheme_id: int | None = None


@router.put("/program-schemes")
def set_program_scheme(payload: ProgramSchemeIn, db: Session = Depends(get_db),
                       user: dict = Depends(require_permission(MANAGE))):
    """Program override of the institution default (Program.grading_scheme_id)."""
    cid = user["company_id"]
    prog = by_id_or_400(db, Program, cid, payload.program_id, "The program")
    if payload.grading_scheme_id:
        by_id_or_400(db, GradingScheme, cid, payload.grading_scheme_id, "The grading scheme")
    prog.grading_scheme_id = payload.grading_scheme_id
    db.commit()
    return {"program_id": prog.id, "grading_scheme_id": prog.grading_scheme_id}


# ── Simple set-up lists ──────────────────────────────────────────────

class ExamTypeIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    code: str | None = Field(None, max_length=20)
    category: str = Field("other", pattern="^(unit_test|mid_term|final|internal|practical|project|viva|continuous|other)$")
    default_max_marks: Decimal | None = Field(None, gt=0, le=10000)
    sort_order: int = 1
    description: str | None = None


class ExamTypePatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    code: str | None = Field(None, max_length=20)
    category: str | None = Field(None, pattern="^(unit_test|mid_term|final|internal|practical|project|viva|continuous|other)$")
    default_max_marks: Decimal | None = Field(None, gt=0, le=10000)
    sort_order: int | None = None
    description: str | None = None
    active: bool | None = None


router.include_router(crud_router(
    model=ExamType, prefix="/types", tag="Exams", create_schema=ExamTypeIn, update_schema=ExamTypePatch,
    view_perm=VIEW, manage_perm=MANAGE, search_fields=("name", "code"), order_by=("sort_order", "name"),
    label="Exam type",
))


class AreaIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    group_name: str | None = Field(None, max_length=80)
    grading_scheme_id: int | None = None
    program_ids: list[int] = []
    sort_order: int = 1


class AreaPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    group_name: str | None = Field(None, max_length=80)
    grading_scheme_id: int | None = None
    program_ids: list[int] | None = None
    sort_order: int | None = None
    active: bool | None = None


def _check_area(db: Session, cid: int, data: dict, row) -> None:
    if data.get("grading_scheme_id"):
        by_id_or_400(db, GradingScheme, cid, data["grading_scheme_id"], "The grading scheme")


router.include_router(crud_router(
    model=CoScholasticArea, prefix="/coscholastic-areas", tag="Exams", create_schema=AreaIn, update_schema=AreaPatch,
    view_perm=VIEW, manage_perm=MANAGE, search_fields=("name", "group_name"), order_by=("sort_order", "name"),
    label="Area", validate=_check_area,
))


class TemplateIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    kind: str = Field("report_card", pattern="^(report_card|transcript)$")
    title: str | None = Field(None, max_length=160)
    session_id: int | None = None
    program_ids: list[int] = []
    columns: list[dict] = []
    summary_source: str | None = Field(None, max_length=30)
    show_marks: bool = True
    show_grade: bool = True
    show_rank: bool = False
    show_coscholastic: bool = True
    show_attendance: bool = True
    show_remarks: bool = True
    show_logo: bool = True
    show_grading_key: bool = True
    signatures: list[str] = ["Class teacher", "Principal", "Parent"]
    footer_note: str | None = None
    is_default: bool = False


class TemplatePatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    kind: str | None = Field(None, pattern="^(report_card|transcript)$")
    title: str | None = None
    session_id: int | None = None
    program_ids: list[int] | None = None
    columns: list[dict] | None = None
    summary_source: str | None = None
    show_marks: bool | None = None
    show_grade: bool | None = None
    show_rank: bool | None = None
    show_coscholastic: bool | None = None
    show_attendance: bool | None = None
    show_remarks: bool | None = None
    show_logo: bool | None = None
    show_grading_key: bool | None = None
    signatures: list[str] | None = None
    footer_note: str | None = None
    is_default: bool | None = None
    active: bool | None = None


def _check_template(db: Session, cid: int, data: dict, row) -> None:
    cols = []
    for c in data.get("columns") or []:
        src = str(c.get("source") or "")
        kind, _, ident = src.partition(":")
        if kind not in ("exam", "rule") or not ident.isdigit():
            raise HTTPException(status_code=400, detail="Each column must be an exam or a result rule.")
        model = Exam if kind == "exam" else ResultRule
        by_id_or_400(db, model, cid, int(ident), "A column's exam or rule")
        cols.append({"source": src, "label": str(c.get("label") or "")[:60]})
    if "columns" in data and data["columns"] is not None:
        data["columns"] = cols
    if data.get("signatures") is not None:
        data["signatures"] = [str(s)[:40] for s in data["signatures"] if str(s).strip()][:4]


def _one_default(db: Session, cid: int, row) -> None:
    if row.is_default:
        db.query(ReportTemplate).filter(ReportTemplate.company_id == cid, ReportTemplate.kind == row.kind,
                                        ReportTemplate.id != row.id).update({"is_default": False})


router.include_router(crud_router(
    model=ReportTemplate, prefix="/templates", tag="Exams", create_schema=TemplateIn, update_schema=TemplatePatch,
    view_perm=VIEW, manage_perm=MANAGE, search_fields=("name", "title"), order_by=("kind", "name"),
    label="Template", validate=_check_template, after_change=_one_default, filters=("kind",),
))


# ── Result rules ─────────────────────────────────────────────────────

class GroupIn(BaseModel):
    key: str = Field(..., min_length=1, max_length=20, pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field("", max_length=60)
    exam_ids: list[int] = Field(..., min_length=1)
    combine: str = Field("average", pattern="^(average|best_of|sum|max|min|latest)$")
    best_of: int | None = Field(None, ge=1, le=20)
    weight: Decimal = Field(Decimal(1), ge=0, le=1000)


class RuleIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    session_id: int | None = None
    term_id: int | None = None
    program_ids: list[int] = []
    grading_scheme_id: int | None = None
    method: str = Field("weighted", pattern="^(weighted|formula)$")
    formula: str | None = Field(None, max_length=500)
    groups: list[GroupIn] = Field(..., min_length=1)
    pass_all_subjects: bool = True
    subject_pass_percent: Decimal | None = Field(None, ge=0, le=100)
    min_aggregate_percent: Decimal | None = Field(None, ge=0, le=100)
    max_failed_subjects: int | None = Field(None, ge=0, le=20)
    min_attendance_percent: Decimal | None = Field(None, ge=0, le=100)
    grace_per_subject: Decimal | None = Field(None, ge=0, le=100)
    grace_max_subjects: int | None = Field(None, ge=0, le=20)
    grace_total: Decimal | None = Field(None, ge=0, le=1000)
    ranking: bool = True
    cumulative: bool = False
    out_of: Decimal | None = Field(None, gt=0, le=1000)


def _rule_out(db: Session, r: ResultRule) -> dict:
    exams = {e.id: e.name for e in db.query(Exam).filter(Exam.company_id == r.company_id).all()}
    return {
        "id": r.id, "uuid": str(r.uuid), "name": r.name, "session_id": r.session_id, "term_id": r.term_id,
        "term_name": r.term.name if r.term else None, "program_ids": r.program_ids or [],
        "grading_scheme_id": r.grading_scheme_id, "method": r.method, "formula": r.formula,
        "groups": [{**g, "exam_names": [exams.get(int(e), "?") for e in g.get("exam_ids") or []]} for g in r.groups or []],
        "pass_all_subjects": r.pass_all_subjects, "subject_pass_percent": service.num(r.subject_pass_percent),
        "min_aggregate_percent": service.num(r.min_aggregate_percent), "max_failed_subjects": r.max_failed_subjects,
        "min_attendance_percent": service.num(r.min_attendance_percent),
        "grace_per_subject": service.num(r.grace_per_subject), "grace_max_subjects": r.grace_max_subjects,
        "grace_total": service.num(r.grace_total), "ranking": r.ranking, "cumulative": r.cumulative,
        "out_of": (r.config or {}).get("out_of"), "active": r.active,
        "results": db.query(ExamResult.id).filter(ExamResult.rule_id == r.id).count(),
        "published": db.query(ExamResult.id).filter(ExamResult.rule_id == r.id, ExamResult.published_at.isnot(None)).count(),
    }


def _apply_rule(db: Session, cid: int, r: ResultRule, data: dict) -> None:
    if data.get("session_id"):
        by_id_or_400(db, AcademicSession, cid, data["session_id"], "The session")
    elif r.session_id is not None:
        data.pop("session_id", None)
    else:
        sess = academics.current_session(db, cid)
        if sess is None:
            raise HTTPException(status_code=400, detail="Add an academic session first.")
        data["session_id"] = sess.id
    if data.get("term_id"):
        by_id_or_400(db, Term, cid, data["term_id"], "The term")
    if data.get("grading_scheme_id"):
        by_id_or_400(db, GradingScheme, cid, data["grading_scheme_id"], "The grading scheme")
    groups = data.pop("groups")
    keys = [g["key"] for g in groups]
    if len(set(keys)) != len(keys):
        raise HTTPException(status_code=400, detail="Each part needs its own short key.")
    for g in groups:
        for eid in g["exam_ids"]:
            by_id_or_400(db, Exam, cid, eid, "An exam in the rule")
        g["weight"] = float(g["weight"])
    if data.get("method") == "formula":
        from core.expressions import ExpressionError, validate
        if not data.get("formula"):
            raise HTTPException(status_code=400, detail="Write the formula, e.g. ut * 0.2 + mt * 0.8.")
        try:
            validate(data["formula"], set(keys))
        except ExpressionError as exc:
            raise HTTPException(status_code=400, detail=f"The formula has a problem: {exc}")
    out_of = data.pop("out_of", None)
    for k, v in data.items():
        setattr(r, k, v)
    r.groups = groups
    r.config = {**(r.config or {}), "out_of": float(out_of) if out_of else None}


@router.get("/rules")
def list_rules(session_id: int | None = None, db: Session = Depends(get_db),
               user: dict = Depends(require_any(*ANY_STAFF))):
    cid = user["company_id"]
    sess_id = session_id or (academics.current_session(db, cid).id if academics.current_session(db, cid) else -1)
    rows = (db.query(ResultRule).filter(ResultRule.company_id == cid, ResultRule.session_id == sess_id,
                                        ResultRule.active.is_(True)).order_by(ResultRule.id).all())
    return [_rule_out(db, r) for r in rows]


@router.post("/rules", status_code=201)
def create_rule(payload: RuleIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = user["company_id"]
    r = ResultRule(company_id=cid)
    _apply_rule(db, cid, r, payload.model_dump(mode="python"))
    db.add(r)
    db.flush()
    service.compute_rule(db, cid, r)
    db.commit()
    return _rule_out(db, r)


@router.put("/rules/{uuid}")
def update_rule(uuid: UUID, payload: RuleIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = user["company_id"]
    r = get_or_404(db, ResultRule, cid, uuid, "Result rule")
    if db.query(ExamResult.id).filter(ExamResult.rule_id == r.id, ExamResult.published_at.isnot(None)).first():
        raise HTTPException(status_code=409, detail="Results of this rule are published. Unpublish them first to change the rule.")
    _apply_rule(db, cid, r, payload.model_dump(mode="python"))
    db.flush()
    service.compute_rule(db, cid, r)
    db.commit()
    return _rule_out(db, r)


@router.delete("/rules/{uuid}")
def archive_rule(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    r = get_or_404(db, ResultRule, user["company_id"], uuid, "Result rule")
    r.active = False
    db.commit()
    return {"detail": "Switched off"}


# ── Marks entry ──────────────────────────────────────────────────────

class EntryIn(BaseModel):
    student_id: int
    marks: Decimal | str | None = None
    components: dict | None = None
    code: str | None = Field(None, max_length=4)
    grade: str | None = Field(None, max_length=40)
    remarks: str | None = Field(None, max_length=300)


class SheetSaveIn(BaseModel):
    paper: UUID
    section_id: int
    entries: list[EntryIn]


class SheetActionIn(BaseModel):
    paper: UUID
    section_id: int
    action: str = Field(..., pattern="^(submit|return|lock|unlock)$")
    note: str | None = Field(None, max_length=2000)


class ModerateIn(BaseModel):
    paper: UUID
    section_id: int
    add_to_all: Decimal | None = None
    adjustments: list[dict] = []
    note: str | None = Field(None, max_length=2000)


class ImportIn(BaseModel):
    paper: UUID
    section_id: int
    csv: str = Field(..., max_length=500_000)


@router.get("/marks/sheets")
def mark_sheets(exam: UUID, mine: bool = False, db: Session = Depends(get_db),
                user: dict = Depends(require_any(*ANY_STAFF))):
    """Mark sheets (section x paper) of an exam the person may open, with progress."""
    e = service.get_exam(db, user["company_id"], exam)
    return service.sheets_for(db, user, e, mine_only=mine)


@router.get("/marks/sheet")
def mark_sheet(paper: UUID, section_id: int, db: Session = Depends(get_db),
               user: dict = Depends(require_any(*ANY_STAFF))):
    p = service.get_paper(db, user["company_id"], paper)
    return service.sheet_view(db, user, p, section_id)


@router.put("/marks/sheet")
def save_sheet(payload: SheetSaveIn, db: Session = Depends(get_db),
               user: dict = Depends(require_any(MARKS, service.MARKS_ALL, service.MODERATE))):
    p = service.get_paper(db, user["company_id"], payload.paper)
    res = service.save_marks(db, user, p, payload.section_id, [e.model_dump() for e in payload.entries])
    db.commit()
    return {**res, "sheet": service.sheet_view(db, user, p, payload.section_id)}


@router.post("/marks/sheet/action")
def sheet_action(payload: SheetActionIn, db: Session = Depends(get_db),
                 user: dict = Depends(require_any(MARKS, service.MARKS_ALL, service.MODERATE))):
    p = service.get_paper(db, user["company_id"], payload.paper)
    service.set_sheet_status(db, user, p, payload.section_id, payload.action, payload.note)
    db.commit()
    return service.sheet_view(db, user, p, payload.section_id)


@router.post("/marks/sheet/moderate")
def sheet_moderate(payload: ModerateIn, db: Session = Depends(get_db),
                   user: dict = Depends(require_permission(service.MODERATE))):
    p = service.get_paper(db, user["company_id"], payload.paper)
    n = service.moderate(db, user, p, payload.section_id, add_to_all=payload.add_to_all,
                         adjustments=payload.adjustments, note=payload.note)
    db.commit()
    return {"changed": n, "sheet": service.sheet_view(db, user, p, payload.section_id)}


@router.post("/marks/sheet/import")
def sheet_import(payload: ImportIn, db: Session = Depends(get_db),
                 user: dict = Depends(require_any(MARKS, service.MARKS_ALL, service.MODERATE))):
    p = service.get_paper(db, user["company_id"], payload.paper)
    res = service.import_csv(db, user, p, payload.section_id, payload.csv)
    db.commit()
    return {**res, "sheet": service.sheet_view(db, user, p, payload.section_id)}


@router.get("/marks/sheet/template.csv")
def sheet_template(paper: UUID, section_id: int, db: Session = Depends(get_db),
                   user: dict = Depends(require_any(*ANY_STAFF))):
    p = service.get_paper(db, user["company_id"], paper)
    if not service.sheet_access(db, user, p, section_id)["view"]:
        raise HTTPException(status_code=403, detail="You can open marks only for the subjects you teach.")
    service.section_for_paper(db, user["company_id"], p, section_id)
    text = service.csv_template(db, user["company_id"], p, section_id)
    return Response(content=text, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="marks-{p.subject.code or "paper"}.csv"'})


class CoScholasticIn(BaseModel):
    section_id: int
    term_id: int | None = None
    rows: list[dict]


@router.get("/coscholastic")
def coscholastic(section_id: int, term_id: int | None = None, db: Session = Depends(get_db),
                 user: dict = Depends(require_any(*ANY_STAFF))):
    return service.coscholastic_sheet(db, user, section_id, term_id)


@router.put("/coscholastic")
def save_coscholastic(payload: CoScholasticIn, db: Session = Depends(get_db),
                      user: dict = Depends(require_any(MARKS, service.MARKS_ALL))):
    n = service.save_coscholastic(db, user, payload.section_id, payload.term_id, payload.rows)
    db.commit()
    return {"saved": n, "sheet": service.coscholastic_sheet(db, user, payload.section_id, payload.term_id)}


# ── Results ──────────────────────────────────────────────────────────

class PublishIn(BaseModel):
    source: str = Field(..., max_length=60)  # exam:<uuid> | rule:<uuid>
    section_ids: list[int] | None = None
    notify: bool = True


@router.post("/results/compute")
def compute(payload: PublishIn, db: Session = Depends(get_db), user: dict = Depends(require_any(MANAGE, PUBLISH))):
    cid = user["company_id"]
    kind, obj = service.source_of(db, cid, payload.source)
    n = service.compute_exam(db, cid, obj, payload.section_ids) if kind == "exam" else \
        service.compute_rule(db, cid, obj, payload.section_ids)
    db.commit()
    return {"computed": n}


@router.get("/results")
def list_results(source: str, section_id: int | None = None, q: str | None = None, status: str | None = None,
                 params: PageParams = Depends(), db: Session = Depends(get_db),
                 user: dict = Depends(require_any(*ANY_STAFF))):
    cid = user["company_id"]
    kind, obj = service.source_of(db, cid, source)
    visible = service.visible_section_ids(db, user)
    query = (db.query(ExamResult, Student).join(Student, Student.id == ExamResult.student_id)
             .filter(ExamResult.company_id == cid, ExamResult.source == f"{kind}:{obj.id}"))
    if section_id:
        query = query.filter(ExamResult.section_id == section_id)
    if visible is not None:
        query = query.filter(ExamResult.section_id.in_(visible or {-1}))
    if status:
        query = query.filter(ExamResult.status == status)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Student.first_name.ilike(like), Student.last_name.ilike(like),
                                 Student.admission_no.ilike(like)))
    query = query.order_by(ExamResult.section_id, ExamResult.rank.asc().nulls_last(), Student.first_name)
    secs = {s.id: academics.section_label(s) for s in db.query(Section).options(joinedload(Section.program))
            .filter(Section.company_id == cid).all()}
    page = paginate(query, params, lambda row: service.result_out(row[0], row[1], secs.get(row[0].section_id), full=True))
    page["source"] = {"kind": kind, "uuid": str(obj.uuid), "name": obj.name}
    return page


@router.get("/results/{uuid}")
def get_result(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_any(*ANY_STAFF))):
    cid = user["company_id"]
    r = get_or_404(db, ExamResult, cid, uuid, "Result")
    visible = service.visible_section_ids(db, user)
    if visible is not None and r.section_id not in visible:
        raise HTTPException(status_code=404, detail="Result not found")
    s = db.query(Student).filter(Student.id == r.student_id).first()
    return {**service.result_out(r, s, full=True), "name": service.source_label(db, r)}


@router.post("/results/publish")
def publish(payload: PublishIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(PUBLISH))):
    res = service.publish(db, user, payload.source, payload.section_ids, payload.notify)
    db.commit()
    return res


@router.post("/results/unpublish")
def unpublish(payload: PublishIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(PUBLISH))):
    n = service.unpublish(db, user, payload.source, payload.section_ids)
    db.commit()
    return {"unpublished": n}


# ── Report cards and transcripts ─────────────────────────────────────

def _template(db: Session, cid: int, template: UUID | None, kind: str = "report_card") -> ReportTemplate:
    if template:
        return get_or_404(db, ReportTemplate, cid, template, "Template")
    t = documents.default_template(db, cid, kind)
    if t is None:
        raise HTTPException(status_code=400, detail="Add a report card template first.")
    return t


@router.get("/report-cards.pdf")
def report_cards_pdf(section_id: int | None = None, student: UUID | None = None, template: UUID | None = None,
                     published_only: bool = False, db: Session = Depends(get_db),
                     user: dict = Depends(require_any(*ANY_STAFF))):
    cid = user["company_id"]
    visible = service.visible_section_ids(db, user)
    tpl = _template(db, cid, template)
    ids = None
    if student:
        s = get_or_404(db, Student, cid, student, "Student")
        ids = [s.id]
        if visible is not None and students.student_section_id(db, cid, s.id) not in visible:
            raise HTTPException(status_code=404, detail="Student not found")
    elif not section_id:
        raise HTTPException(status_code=400, detail="Pick a section or a learner.")
    if section_id and visible is not None and section_id not in visible:
        raise HTTPException(status_code=403, detail="You can print report cards only for your own sections.")
    data = documents.report_cards(db, cid, tpl, section_id=section_id, student_ids=ids, published_only=published_only)
    return _pdf(data, "report-cards.pdf")


@router.get("/transcripts/{student_uuid}")
def transcript(student_uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    s = get_or_404(db, Student, user["company_id"], student_uuid, "Student")
    return documents.transcript_data(db, user["company_id"], s, published_only=False)


@router.get("/transcripts/{student_uuid}/pdf")
def transcript_pdf(student_uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    s = get_or_404(db, Student, user["company_id"], student_uuid, "Student")
    return _pdf(documents.transcript_pdf(db, user["company_id"], s, published_only=False), f"transcript-{s.admission_no}.pdf")


# ── Analysis and summaries ───────────────────────────────────────────

@router.get("/analysis")
def analysis(source: str, section_id: int | None = None, db: Session = Depends(get_db),
             user: dict = Depends(require_any(*ANY_STAFF))):
    return service.analysis(db, user["company_id"], source, section_id, service.visible_section_ids(db, user))


@router.get("/analysis/progress/{student_uuid}")
def progress(student_uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_any(*ANY_STAFF))):
    cid = user["company_id"]
    s = get_or_404(db, Student, cid, student_uuid, "Student")
    visible = service.visible_section_ids(db, user)
    if visible is not None and students.student_section_id(db, cid, s.id) not in visible:
        raise HTTPException(status_code=404, detail="Student not found")
    return {"student": students.student_brief(s, students.current_enrollment(db, cid, s.id)),
            "points": service.student_progress(db, cid, s.id)}


@router.get("/summary")
def summary(db: Session = Depends(get_db), user: dict = Depends(require_any(*ANY_STAFF))):
    return service.staff_summary(db, user)


# ── Portal (parents and students) ────────────────────────────────────

def _portal_student(db: Session, user: dict, student_id: int) -> Student:
    students.require_student_access(db, user, student_id)
    s = db.query(Student).filter(Student.company_id == user["company_id"], Student.id == student_id).first()
    if s is None:
        raise HTTPException(status_code=404, detail="Student not found")
    return s


@router.get("/portal/summary")
def portal_summary(student_id: int, db: Session = Depends(get_db), user: dict = Depends(require_portal("parent", "student"))):
    _portal_student(db, user, student_id)
    return service.portal_summary(db, user["company_id"], student_id)


@router.get("/portal/results")
def portal_results(student_id: int, db: Session = Depends(get_db), user: dict = Depends(require_portal("parent", "student"))):
    """Published results only."""
    _portal_student(db, user, student_id)
    cid = user["company_id"]
    return [{**service.result_out(r, full=True), "name": service.source_label(db, r)}
            for r in service.published_results(db, cid, student_id)]


@router.get("/portal/progress")
def portal_progress(student_id: int, db: Session = Depends(get_db), user: dict = Depends(require_portal("parent", "student"))):
    _portal_student(db, user, student_id)
    return service.student_progress(db, user["company_id"], student_id, published_only=True)


@router.get("/portal/schedule")
def portal_schedule(student_id: int, db: Session = Depends(get_db), user: dict = Depends(require_portal("parent", "student"))):
    _portal_student(db, user, student_id)
    return service.portal_schedule(db, user["company_id"], student_id)


@router.get("/portal/report-card.pdf")
def portal_report_card(student_id: int, db: Session = Depends(get_db),
                       user: dict = Depends(require_portal("parent", "student"))):
    s = _portal_student(db, user, student_id)
    tpl = _template(db, user["company_id"], None)
    return _pdf(documents.report_cards(db, user["company_id"], tpl, student_ids=[s.id], published_only=True),
                f"report-card-{s.admission_no}.pdf")


@router.get("/portal/transcript.pdf")
def portal_transcript(student_id: int, db: Session = Depends(get_db),
                      user: dict = Depends(require_portal("parent", "student"))):
    s = _portal_student(db, user, student_id)
    return _pdf(documents.transcript_pdf(db, user["company_id"], s, published_only=True), f"transcript-{s.admission_no}.pdf")


@router.get("/portal/hall-ticket.pdf")
def portal_hall_ticket(student_id: int, exam: UUID, db: Session = Depends(get_db),
                       user: dict = Depends(require_portal("parent", "student"))):
    s = _portal_student(db, user, student_id)
    e = service.get_exam(db, user["company_id"], exam)
    if e.status == "draft" or e.archived_at:
        raise HTTPException(status_code=404, detail="Exam not found")
    return _pdf(documents.hall_tickets(db, user["company_id"], e, student_ids=[s.id]), "admit-card.pdf")


# ── Exams, papers and seating (dynamic /{uuid} routes last) ──────────

class ExamIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    code: str | None = Field(None, max_length=30)
    session_id: int | None = None
    term_id: int | None = None
    exam_type_id: int | None = None
    program_ids: list[int] = []
    start_date: date | None = None
    end_date: date | None = None
    marks_due_on: date | None = None
    grading_scheme_id: int | None = None
    status: str = Field("draft", pattern="^(draft|scheduled)$")
    ranking: bool = True
    instructions: str | None = None
    generate_papers: bool = True
    max_marks: Decimal | None = Field(None, gt=0, le=10000)


class ExamPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    code: str | None = Field(None, max_length=30)
    term_id: int | None = None
    exam_type_id: int | None = None
    program_ids: list[int] | None = None
    start_date: date | None = None
    end_date: date | None = None
    marks_due_on: date | None = None
    grading_scheme_id: int | None = None
    status: str | None = Field(None, pattern="^(draft|scheduled)$")
    ranking: bool | None = None
    instructions: str | None = None


def _check_exam(db: Session, cid: int, data: dict) -> None:
    for key, model, label in (("term_id", Term, "The term"), ("exam_type_id", ExamType, "The exam type"),
                              ("grading_scheme_id", GradingScheme, "The grading scheme"),
                              ("session_id", AcademicSession, "The session")):
        if data.get(key):
            by_id_or_400(db, model, cid, data[key], label)
    for pid in data.get("program_ids") or []:
        by_id_or_400(db, Program, cid, pid, "A program")
    if data.get("start_date") and data.get("end_date") and data["end_date"] < data["start_date"]:
        raise HTTPException(status_code=400, detail="The last day must be on or after the first day.")


@router.get("")
def list_exams(q: str | None = None, session_id: int | None = None, term_id: int | None = None,
               status: str | None = None, archived: bool = False, params: PageParams = Depends(),
               db: Session = Depends(get_db), user: dict = Depends(require_any(*ANY_STAFF))):
    cid = user["company_id"]
    sess_id = session_id or (academics.current_session(db, cid).id if academics.current_session(db, cid) else -1)
    query = (db.query(Exam).options(joinedload(Exam.exam_type), joinedload(Exam.term), joinedload(Exam.session))
             .filter(Exam.company_id == cid, Exam.session_id == sess_id))
    query = query.filter(Exam.archived_at.isnot(None) if archived else Exam.archived_at.is_(None))
    if term_id:
        query = query.filter(Exam.term_id == term_id)
    if status:
        query = query.filter(Exam.status == status)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Exam.name.ilike(like), Exam.code.ilike(like)))
    return paginate(query.order_by(Exam.start_date.desc().nulls_first(), Exam.id.desc()), params,
                    lambda e: service.exam_out(db, e))


@router.post("", status_code=201)
def create_exam(payload: ExamIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = user["company_id"]
    data = payload.model_dump()
    gen, mx = data.pop("generate_papers"), data.pop("max_marks")
    _check_exam(db, cid, data)
    if not data.get("session_id"):
        sess = academics.current_session(db, cid)
        if sess is None:
            raise HTTPException(status_code=400, detail="Add an academic session first.")
        data["session_id"] = sess.id
    e = Exam(company_id=cid, **data)
    db.add(e)
    db.flush()
    if gen:
        service.generate_papers(db, cid, e, max_marks=mx)
    db.commit()
    db.refresh(e)
    return service.exam_out(db, e, with_papers=True)


@router.get("/{uuid}")
def get_exam(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_any(*ANY_STAFF))):
    e = service.get_exam(db, user["company_id"], uuid)
    out = service.exam_out(db, e, with_papers=True)
    out["clashes"] = service.schedule_clashes(e.papers)
    out["seating"] = _seating_out(db, e)
    return out


@router.put("/{uuid}")
def update_exam(uuid: UUID, payload: ExamPatch, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = user["company_id"]
    e = service.get_exam(db, cid, uuid)
    data = payload.model_dump(exclude_unset=True)
    _check_exam(db, cid, {**{"start_date": e.start_date, "end_date": e.end_date}, **data})
    if e.status == "published" and "status" in data:
        data.pop("status")
    for k, v in data.items():
        setattr(e, k, v)
    db.flush()
    if "grading_scheme_id" in data or "ranking" in data:
        service.compute_exam(db, cid, e)
    db.commit()
    db.refresh(e)
    return service.exam_out(db, e, with_papers=True)


@router.delete("/{uuid}")
def archive_exam(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    e = service.get_exam(db, user["company_id"], uuid)
    if e.published_at:
        raise HTTPException(status_code=409, detail="Results of this exam are published. Unpublish them first.")
    e.archived_at = service.now()
    db.commit()
    return {"detail": "Archived"}


@router.post("/{uuid}/restore")
def restore_exam(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    e = service.get_exam(db, user["company_id"], uuid)
    e.archived_at = None
    db.commit()
    return service.exam_out(db, e)


@router.post("/{uuid}/papers/generate")
def generate(uuid: UUID, max_marks: Decimal | None = Body(None, embed=True), db: Session = Depends(get_db),
             user: dict = Depends(require_permission(MANAGE))):
    e = service.get_exam(db, user["company_id"], uuid)
    n = service.generate_papers(db, user["company_id"], e, max_marks=max_marks)
    db.commit()
    db.refresh(e)
    return {"added": n, "exam": service.exam_out(db, e, with_papers=True)}


class ComponentIn(BaseModel):
    key: str = Field(..., min_length=1, max_length=20, pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(..., min_length=1, max_length=40)
    max_marks: Decimal = Field(..., gt=0, le=10000)
    pass_marks: Decimal | None = Field(None, ge=0, le=10000)


class PaperIn(BaseModel):
    uuid: UUID | None = None
    program_id: int
    subject_id: int
    paper_date: date | None = None
    start_time: time | None = None
    end_time: time | None = None
    room_id: int | None = None
    invigilator_id: int | None = None
    max_marks: Decimal = Field(Decimal(100), gt=0, le=10000)
    pass_marks: Decimal | None = Field(None, ge=0, le=10000)
    components: list[ComponentIn] = []
    entry_mode: str = Field("marks", pattern="^(marks|grade)$")
    counts_in_total: bool = True
    credits: Decimal | None = Field(None, ge=0, le=100)
    grading_scheme_id: int | None = None
    sort_order: int = 1


@router.put("/{uuid}/papers")
def save_papers(uuid: UUID, papers: list[PaperIn], db: Session = Depends(get_db),
                user: dict = Depends(require_permission(MANAGE))):
    """Add or change papers (the exam timetable). Papers not listed are left alone."""
    cid = user["company_id"]
    e = service.get_exam(db, cid, uuid)
    existing = {str(p.uuid): p for p in e.papers}
    from modules.employees.models import Employee
    for p in papers:
        d = p.model_dump()
        d.pop("uuid")
        by_id_or_400(db, Program, cid, d["program_id"], "The program")
        by_id_or_400(db, Subject, cid, d["subject_id"], "The subject")
        by_id_or_400(db, Room, cid, d.get("room_id"), "The room")
        by_id_or_400(db, Employee, cid, d.get("invigilator_id"), "The invigilator")
        by_id_or_400(db, GradingScheme, cid, d.get("grading_scheme_id"), "The grading scheme")
        if d["pass_marks"] is not None and d["pass_marks"] > d["max_marks"]:
            raise HTTPException(status_code=400, detail="Pass marks cannot be more than the maximum.")
        comps = d["components"]
        if comps:
            total = sum((Decimal(str(c["max_marks"])) for c in comps), Decimal(0))
            if total != d["max_marks"]:
                raise HTTPException(status_code=400, detail="The parts must add up to the maximum marks.")
            d["components"] = [{**c, "max_marks": float(c["max_marks"]),
                                "pass_marks": float(c["pass_marks"]) if c.get("pass_marks") is not None else None}
                               for c in comps]
        if d["start_time"] and d["end_time"] and d["end_time"] <= d["start_time"]:
            raise HTTPException(status_code=400, detail="The end time must be after the start time.")
        row = existing.get(str(p.uuid)) if p.uuid else None
        if row is None:
            row = db.query(ExamPaper).filter(ExamPaper.exam_id == e.id, ExamPaper.program_id == d["program_id"],
                                             ExamPaper.subject_id == d["subject_id"]).first()
        if row is None:
            row = ExamPaper(company_id=cid, exam_id=e.id)
            db.add(row)
        for k, v in d.items():
            setattr(row, k, v)
    db.flush()
    dates = [p.paper_date for p in db.query(ExamPaper).filter(ExamPaper.exam_id == e.id).all() if p.paper_date]
    if dates:
        e.start_date, e.end_date = min(dates), max(dates)
    service.compute_exam(db, cid, e)
    db.commit()
    db.refresh(e)
    out = service.exam_out(db, e, with_papers=True)
    out["clashes"] = service.schedule_clashes(e.papers)
    return out


@router.delete("/{uuid}/papers/{paper_uuid}")
def delete_paper(uuid: UUID, paper_uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = user["company_id"]
    e = service.get_exam(db, cid, uuid)
    p = service.get_paper(db, cid, paper_uuid)
    if p.exam_id != e.id:
        raise HTTPException(status_code=404, detail="Paper not found")
    from modules.exams.models import Mark
    if db.query(Mark.id).filter(Mark.paper_id == p.id).first():
        raise HTTPException(status_code=409, detail="Marks are already entered for this paper.")
    db.delete(p)
    db.commit()
    return {"detail": "Removed"}


class SeatIn(BaseModel):
    room_id: int
    section_id: int
    roll_from: str | None = Field(None, max_length=20)
    roll_to: str | None = Field(None, max_length=20)
    seats: int | None = Field(None, ge=1, le=5000)
    invigilator_id: int | None = None


def _seating_out(db: Session, e: Exam) -> list[dict]:
    rows = (db.query(ExamRoom).options(joinedload(ExamRoom.room), joinedload(ExamRoom.section).joinedload(Section.program),
                                       joinedload(ExamRoom.invigilator)).filter(ExamRoom.exam_id == e.id)
            .order_by(ExamRoom.id).all())
    return [{"uuid": str(r.uuid), "room_id": r.room_id, "room_name": r.room.name if r.room else None,
             "section_id": r.section_id, "section_label": academics.section_label(r.section),
             "roll_from": r.roll_from, "roll_to": r.roll_to, "seats": r.seats, "invigilator_id": r.invigilator_id,
             "invigilator_name": f"{r.invigilator.first_name} {r.invigilator.last_name}".strip() if r.invigilator else None}
            for r in rows]


@router.put("/{uuid}/seating")
def save_seating(uuid: UUID, rows: list[SeatIn], db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    """Replace the seating plan: roll ranges of sections per room, with invigilators."""
    cid = user["company_id"]
    e = service.get_exam(db, cid, uuid)
    from modules.employees.models import Employee
    db.query(ExamRoom).filter(ExamRoom.exam_id == e.id).delete()
    for r in rows:
        by_id_or_400(db, Room, cid, r.room_id, "The room")
        by_id_or_400(db, Section, cid, r.section_id, "The section")
        by_id_or_400(db, Employee, cid, r.invigilator_id, "The invigilator")
        db.add(ExamRoom(company_id=cid, exam_id=e.id, **r.model_dump()))
    db.commit()
    return _seating_out(db, e)


@router.post("/{uuid}/seating/auto")
def auto_seating(uuid: UUID, room_ids: list[int] = Body(..., embed=True), db: Session = Depends(get_db),
                 user: dict = Depends(require_permission(MANAGE))):
    """Fill the chosen rooms section by section, in roll order, up to each room's capacity."""
    cid = user["company_id"]
    e = service.get_exam(db, cid, uuid)
    rooms = [by_id_or_400(db, Room, cid, rid, "The room") for rid in room_ids]
    if not rooms:
        raise HTTPException(status_code=400, detail="Pick at least one room.")
    db.query(ExamRoom).filter(ExamRoom.exam_id == e.id).delete()
    ri, left = 0, rooms[0].capacity or 40
    for sec in service.exam_sections(db, cid, e):
        roster = students.section_students(db, cid, sec.id)
        i = 0
        while i < len(roster):
            if ri >= len(rooms):
                raise HTTPException(status_code=400, detail="The rooms don't have enough seats. Add more rooms.")
            take = roster[i:i + left]
            db.add(ExamRoom(company_id=cid, exam_id=e.id, room_id=rooms[ri].id, section_id=sec.id,
                            roll_from=take[0][1].roll_no, roll_to=take[-1][1].roll_no, seats=len(take)))
            i += len(take)
            left -= len(take)
            if left <= 0:
                ri += 1
                left = (rooms[ri].capacity or 40) if ri < len(rooms) else 0
    db.commit()
    return _seating_out(db, e)


@router.get("/{uuid}/hall-tickets.pdf")
def hall_tickets(uuid: UUID, section_id: int | None = None, db: Session = Depends(get_db),
                 user: dict = Depends(require_any(*ANY_STAFF))):
    e = service.get_exam(db, user["company_id"], uuid)
    return _pdf(documents.hall_tickets(db, user["company_id"], e, section_id), f"admit-cards-{e.code or 'exam'}.pdf")
