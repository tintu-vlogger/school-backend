"""Exams service: grading schemes, exams and papers, marks entry, result computation,
publishing, analysis and summaries. Printable documents live in documents.py.

Functions other modules may use:
    resolve_scheme(db, company_id, program_id=None, override_id=None) -> GradingScheme | None
    published_results(db, company_id, student_id)                   -> [ExamResult]
    student_progress(db, company_id, student_id, published_only)    -> [{exam, percentage, ...}]
"""
from __future__ import annotations

import csv
import importlib
import io
import uuid as uuid_mod
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from core import audit
from core.security import _get_role_permissions
from modules.academics import service as academics
from modules.academics.models import Program, ProgramSubject, Section, SectionSubject, Subject, Term
from modules.exams import calc
from modules.exams.models import (CoScholasticArea, Exam, ExamPaper, ExamResult, ExamRoom, ExamType, GradeBand,
                                  GradingScheme, Mark, MarkSheet, ReportTemplate, ResultRule, TermRecord)
from modules.students import service as students
from modules.students.models import Enrollment, Student

D = calc.D
q = calc.q

VIEW, MANAGE, MARKS, MARKS_ALL, MODERATE, PUBLISH = (
    "exams.view", "exams.manage", "exams.marks", "exams.marks.all", "exams.moderate", "exams.publish")


def now() -> datetime:
    return datetime.now(timezone.utc)


def perms(db: Session, user: dict) -> set[str]:
    return set(_get_role_permissions(user["company_id"], user["role"], db))


def num(v) -> float | None:
    """Decimal -> float for JSON payloads (rounded to 2 places)."""
    return None if v is None else float(q(v))


# ── Grading schemes ──────────────────────────────────────────────────

def bands_of(scheme: GradingScheme | None) -> list[dict]:
    if scheme is None:
        return []
    return [{"label": b.label, "min_percent": b.min_percent, "grade_point": b.grade_point, "is_pass": b.is_pass,
             "description": b.description, "color": b.color} for b in scheme.bands]


def scheme_out(s: GradingScheme, default_code: str | None = None) -> dict:
    return {
        "id": s.id, "uuid": str(s.uuid), "name": s.name, "code": s.code, "kind": s.kind,
        "scale_max": num(s.scale_max), "pass_percent": num(s.pass_percent), "decimals": s.decimals,
        "percent_formula": s.percent_formula, "description": s.description, "version": s.version,
        "lineage_id": str(s.lineage_id), "effective_from": s.effective_from, "effective_to": s.effective_to,
        "is_system": s.is_system, "active": s.active, "is_default": default_code == s.code,
        "bands": [{"id": b.id, "label": b.label, "min_percent": num(b.min_percent), "grade_point": num(b.grade_point),
                   "description": b.description, "is_pass": b.is_pass, "color": b.color} for b in s.bands],
    }


def _company(db: Session, company_id: int):
    from modules.company.models import Company
    return db.query(Company).filter(Company.id == company_id).first()


def default_scheme_code(db: Session, company_id: int) -> str:
    return academics.profile(_company(db, company_id))["grading"]


def scheme_by_code(db: Session, company_id: int, code: str) -> GradingScheme | None:
    return (db.query(GradingScheme).filter(GradingScheme.company_id == company_id, GradingScheme.code == code,
                                           GradingScheme.active.is_(True))
            .order_by(GradingScheme.version.desc()).first())


def resolve_scheme(db: Session, company_id: int, program_id: int | None = None,
                   override_id: int | None = None) -> GradingScheme | None:
    """Which grading scheme applies: an explicit override (exam / rule / paper), else the
    program's `grading_scheme_id`, else the institution default (profile grading)."""
    for sid in (override_id,):
        if sid:
            s = db.query(GradingScheme).filter(GradingScheme.company_id == company_id, GradingScheme.id == sid).first()
            if s:
                return s
    if program_id:
        pid = db.query(Program.grading_scheme_id).filter(Program.company_id == company_id, Program.id == program_id).scalar()
        if pid:
            s = db.query(GradingScheme).filter(GradingScheme.company_id == company_id, GradingScheme.id == pid).first()
            if s:
                # A newer version of the same scheme replaces an old one that was edited.
                return scheme_by_code(db, company_id, s.code) if not s.active else s
    s = scheme_by_code(db, company_id, default_scheme_code(db, company_id))
    if s:
        return s
    return (db.query(GradingScheme).filter(GradingScheme.company_id == company_id, GradingScheme.active.is_(True))
            .order_by(GradingScheme.id).first())


def save_scheme(db: Session, company_id: int, data: dict, scheme: GradingScheme | None = None) -> GradingScheme:
    """Create a scheme, or change one. A scheme already used by published results is never
    changed in place: a new version is created (same code and lineage) and the old one retired,
    so published results keep the rules they were made with."""
    bands = data.pop("bands", None)
    if bands is not None:
        _check_bands(bands)
    if scheme is not None:
        in_use = db.query(ExamResult.id).filter(ExamResult.company_id == company_id, ExamResult.scheme_id == scheme.id,
                                                ExamResult.published_at.isnot(None)).first()
        if in_use:
            new = GradingScheme(company_id=company_id, code=scheme.code, lineage_id=scheme.lineage_id,
                                version=scheme.version + 1, effective_from=date.today(), is_system=False)
            for col in ("name", "kind", "scale_max", "pass_percent", "decimals", "percent_formula", "description"):
                setattr(new, col, data.get(col, getattr(scheme, col)))
            scheme.active = False
            scheme.effective_to = date.today()
            db.add(new)
            db.flush()
            src = bands if bands is not None else bands_of(scheme)
            for b in src:
                db.add(GradeBand(company_id=company_id, scheme_id=new.id, label=b["label"], min_percent=b["min_percent"],
                                 grade_point=b.get("grade_point"), description=b.get("description"),
                                 is_pass=b.get("is_pass", True), color=b.get("color")))
            # Programs pointing at the old version follow the new one.
            db.query(Program).filter(Program.company_id == company_id, Program.grading_scheme_id == scheme.id).update(
                {"grading_scheme_id": new.id})
            db.flush()
            db.refresh(new)
            return new
        for k, v in data.items():
            setattr(scheme, k, v)
    else:
        code = (data.get("code") or "").strip() or f"custom_{uuid_mod.uuid4().hex[:6]}"
        if scheme_by_code(db, company_id, code):
            raise HTTPException(status_code=409, detail="A grading scheme with this code already exists.")
        data["code"] = code
        scheme = GradingScheme(company_id=company_id, **data)
        db.add(scheme)
        db.flush()
    if bands is not None:
        for b in list(scheme.bands):
            db.delete(b)
        db.flush()
        for b in bands:
            db.add(GradeBand(company_id=company_id, scheme_id=scheme.id, label=b["label"], min_percent=b["min_percent"],
                             grade_point=b.get("grade_point"), description=b.get("description"),
                             is_pass=b.get("is_pass", True), color=b.get("color")))
    db.flush()
    db.refresh(scheme)
    return scheme


def _check_bands(bands: list[dict]) -> None:
    seen = set()
    for b in bands:
        lab = (b.get("label") or "").strip()
        if not lab:
            raise HTTPException(status_code=400, detail="Every grade needs a label.")
        if lab.lower() in seen:
            raise HTTPException(status_code=400, detail=f"The grade {lab} is listed twice.")
        seen.add(lab.lower())
        mp = D(b.get("min_percent"))
        if mp is None or mp < 0 or mp > 100:
            raise HTTPException(status_code=400, detail=f"Give {lab} a starting percentage between 0 and 100.")
    if bands and not any(D(b["min_percent"]) == 0 for b in bands):
        raise HTTPException(status_code=400, detail="One grade must start at 0% so every score gets a grade.")


def lookup(scheme: GradingScheme, pct) -> dict | None:
    b = calc.band_for(pct, bands_of(scheme))
    if not b:
        return None
    return {"label": b["label"], "grade_point": num(b["grade_point"]), "is_pass": b["is_pass"],
            "description": b["description"]}


def cgpa_to_percent(scheme: GradingScheme | None, cgpa) -> Decimal | None:
    if scheme is None or cgpa is None or not scheme.percent_formula:
        return None
    from core.expressions import ExpressionError, evaluate
    try:
        return D(evaluate(scheme.percent_formula, {"cgpa": D(cgpa), "gpa": D(cgpa)}))
    except (ExpressionError, Exception):
        return None


# ── Look-ups ─────────────────────────────────────────────────────────

def credits_map(db: Session, company_id: int, program_ids: set[int]) -> dict[tuple[int, int], Decimal | None]:
    """(program_id, subject_id) -> credits from the curriculum, else the subject's credits."""
    out: dict[tuple[int, int], Decimal | None] = {}
    if not program_ids:
        return out
    subj = {s.id: s.credits for s in db.query(Subject).filter(Subject.company_id == company_id).all()}
    for ps in db.query(ProgramSubject).filter(ProgramSubject.company_id == company_id,
                                              ProgramSubject.program_id.in_(program_ids)).all():
        out[(ps.program_id, ps.subject_id)] = ps.credits if ps.credits is not None else subj.get(ps.subject_id)
    out["_subjects"] = subj  # type: ignore[index]
    return out


def _credits(cmap: dict, program_id: int, subject_id: int, override=None):
    if override is not None:
        return D(override)
    if (program_id, subject_id) in cmap:
        return cmap[(program_id, subject_id)]
    return (cmap.get("_subjects") or {}).get(subject_id)


def exam_sections(db: Session, company_id: int, exam: Exam, section_ids: list[int] | None = None) -> list[Section]:
    qy = (db.query(Section).options(joinedload(Section.program))
          .filter(Section.company_id == company_id, Section.session_id == exam.session_id, Section.active.is_(True)))
    progs = [int(p) for p in (exam.program_ids or [])]
    if progs:
        qy = qy.filter(Section.program_id.in_(progs))
    if section_ids:
        qy = qy.filter(Section.id.in_(section_ids))
    return qy.join(Program, Program.id == Section.program_id).order_by(Program.sort_order, Section.name).all()


def get_exam(db: Session, company_id: int, exam_uuid) -> Exam:
    e = db.query(Exam).filter(Exam.company_id == company_id, Exam.uuid == exam_uuid).first()
    if e is None:
        raise HTTPException(status_code=404, detail="Exam not found")
    return e


def exam_out(db: Session, e: Exam, with_papers: bool = False) -> dict:
    today = date.today()
    if e.archived_at:
        phase = "archived"
    elif e.status == "published":
        phase = "published"
    elif e.status == "draft":
        phase = "draft"
    elif e.start_date and e.start_date > today:
        phase = "upcoming"
    elif e.end_date and e.end_date >= today:
        phase = "ongoing"
    else:
        phase = "marks_entry"
    progs = [int(p) for p in (e.program_ids or [])]
    names = {}
    if progs:
        names = {p.id: p.name for p in db.query(Program).filter(Program.id.in_(progs)).all()}
    out = {
        "id": e.id, "uuid": str(e.uuid), "name": e.name, "code": e.code, "session_id": e.session_id,
        "session_name": e.session.name if e.session else None, "term_id": e.term_id,
        "term_name": e.term.name if e.term else None, "exam_type_id": e.exam_type_id,
        "exam_type_name": e.exam_type.name if e.exam_type else None,
        "category": e.exam_type.category if e.exam_type else None,
        "program_ids": progs, "program_names": [names[p] for p in progs if p in names],
        "start_date": e.start_date, "end_date": e.end_date, "marks_due_on": e.marks_due_on,
        "grading_scheme_id": e.grading_scheme_id, "status": e.status, "phase": phase, "ranking": e.ranking,
        "instructions": e.instructions, "published_at": e.published_at, "archived": e.archived_at is not None,
        "paper_count": len(e.papers), "created_at": e.created_at,
    }
    if with_papers:
        out["papers"] = [paper_out(p) for p in sorted(e.papers, key=lambda p: (
            p.program.sort_order if p.program else 0, p.paper_date or date.max, p.start_time or datetime.min.time(),
            p.sort_order))]
    return out


def paper_out(p: ExamPaper) -> dict:
    return {
        "id": p.id, "uuid": str(p.uuid), "exam_id": p.exam_id, "program_id": p.program_id,
        "program_name": p.program.name if p.program else None, "subject_id": p.subject_id,
        "subject_name": p.subject.name if p.subject else None, "subject_code": p.subject.code if p.subject else None,
        "paper_date": p.paper_date, "start_time": p.start_time.strftime("%H:%M") if p.start_time else None,
        "end_time": p.end_time.strftime("%H:%M") if p.end_time else None, "room_id": p.room_id,
        "room_name": p.room.name if p.room else None, "invigilator_id": p.invigilator_id,
        "invigilator_name": f"{p.invigilator.first_name} {p.invigilator.last_name}".strip() if p.invigilator else None,
        "max_marks": num(p.max_marks), "pass_marks": num(p.pass_marks), "components": p.components or [],
        "entry_mode": p.entry_mode, "counts_in_total": p.counts_in_total, "credits": num(p.credits),
        "grading_scheme_id": p.grading_scheme_id, "sort_order": p.sort_order,
    }


def generate_papers(db: Session, company_id: int, exam: Exam, max_marks=None, pass_marks=None) -> int:
    """Add a paper for every subject in the curriculum of each program the exam covers
    (programs already set up are left alone)."""
    progs = [int(p) for p in (exam.program_ids or [])]
    if not progs:
        progs = sorted({s.program_id for s in exam_sections(db, company_id, exam)})
    default_max = D(max_marks) or (exam.exam_type.default_max_marks if exam.exam_type and exam.exam_type.default_max_marks else Decimal(100))
    have = {(p.program_id, p.subject_id) for p in exam.papers}
    term_no = None
    if exam.term_id:
        t = db.query(Term).filter(Term.id == exam.term_id).first()
        term_no = t.sort_order if t else None
    graded_scheme = scheme_by_code(db, company_id, "coscholastic_3pt")
    added = 0
    for pid in progs:
        rows = (db.query(ProgramSubject).options(joinedload(ProgramSubject.subject))
                .filter(ProgramSubject.company_id == company_id, ProgramSubject.program_id == pid)
                .order_by(ProgramSubject.sort_order).all())
        for i, ps in enumerate(rows):
            if ps.term_no and term_no and ps.term_no != term_no:
                continue
            if (pid, ps.subject_id) in have:
                continue
            graded = ps.subject is not None and ps.subject.kind == "activity"
            pm = D(pass_marks)
            db.add(ExamPaper(company_id=company_id, exam_id=exam.id, program_id=pid, subject_id=ps.subject_id,
                             max_marks=default_max, pass_marks=pm if pm is not None else None,
                             entry_mode="grade" if graded else "marks", counts_in_total=not graded,
                             grading_scheme_id=graded_scheme.id if graded and graded_scheme else None,
                             sort_order=i + 1))
            have.add((pid, ps.subject_id))
            added += 1
    db.flush()
    return added


def schedule_clashes(papers: list[ExamPaper]) -> list[dict]:
    """Two papers of one program at overlapping times on the same day."""
    out = []
    by_prog = defaultdict(list)
    for p in papers:
        if p.paper_date and p.start_time:
            by_prog[(p.program_id, p.paper_date)].append(p)
    for (_, day), rows in by_prog.items():
        rows.sort(key=lambda p: p.start_time)
        for a, b in zip(rows, rows[1:]):
            end_a = a.end_time or a.start_time
            if b.start_time < end_a or b.start_time == a.start_time:
                out.append({"date": day, "program": a.program.name if a.program else "",
                            "subjects": [a.subject.name if a.subject else "", b.subject.name if b.subject else ""]})
    return out


# ── Marks entry ──────────────────────────────────────────────────────

def get_paper(db: Session, company_id: int, paper_uuid) -> ExamPaper:
    p = (db.query(ExamPaper).options(joinedload(ExamPaper.exam), joinedload(ExamPaper.subject),
                                     joinedload(ExamPaper.program))
         .filter(ExamPaper.company_id == company_id, ExamPaper.uuid == paper_uuid).first())
    if p is None:
        raise HTTPException(status_code=404, detail="Paper not found")
    return p


def teaches(db: Session, company_id: int, employee_id: int | None, section_id: int, subject_id: int) -> bool:
    if not employee_id:
        return False
    a = db.query(SectionSubject).filter(SectionSubject.company_id == company_id, SectionSubject.section_id == section_id,
                                        SectionSubject.subject_id == subject_id).first()
    return bool(a and (a.teacher_id == employee_id or employee_id in (a.assistant_ids or [])))


def is_class_teacher(db: Session, company_id: int, employee_id: int | None, section_id: int) -> bool:
    if not employee_id:
        return False
    return bool(db.query(Section.id).filter(Section.company_id == company_id, Section.id == section_id,
                                            Section.class_teacher_id == employee_id).first())


def sheet_access(db: Session, user: dict, paper: ExamPaper, section_id: int) -> dict:
    """What the signed-in person may do with one mark sheet."""
    p = perms(db, user)
    cid = user["company_id"]
    own = MARKS in p and teaches(db, cid, user.get("employee_id"), section_id, paper.subject_id)
    return {"enter": own or MARKS_ALL in p, "moderate": MODERATE in p, "view": own or VIEW in p or MARKS_ALL in p,
            "own": own}


def section_for_paper(db: Session, company_id: int, paper: ExamPaper, section_id: int) -> Section:
    sec = (db.query(Section).options(joinedload(Section.program))
           .filter(Section.company_id == company_id, Section.id == section_id).first())
    if sec is None or sec.program_id != paper.program_id or sec.session_id != paper.exam.session_id:
        raise HTTPException(status_code=400, detail="This paper is not written by that section.")
    return sec


def get_sheet(db: Session, company_id: int, paper: ExamPaper, section_id: int, create: bool = False) -> MarkSheet | None:
    s = db.query(MarkSheet).filter(MarkSheet.company_id == company_id, MarkSheet.paper_id == paper.id,
                                   MarkSheet.section_id == section_id).first()
    if s is None and create:
        s = MarkSheet(company_id=company_id, exam_id=paper.exam_id, paper_id=paper.id, section_id=section_id)
        db.add(s)
        db.flush()
    return s


def sheet_view(db: Session, user: dict, paper: ExamPaper, section_id: int) -> dict:
    cid = user["company_id"]
    sec = section_for_paper(db, cid, paper, section_id)
    access = sheet_access(db, user, paper, section_id)
    if not access["view"]:
        raise HTTPException(status_code=403, detail="You can open marks only for the subjects you teach.")
    sheet = get_sheet(db, cid, paper, section_id)
    roster = students.section_students(db, cid, section_id)
    marks = {m.student_id: m for m in db.query(Mark).filter(Mark.company_id == cid, Mark.paper_id == paper.id).all()}
    scheme = resolve_scheme(db, cid, paper.program_id, paper.grading_scheme_id or paper.exam.grading_scheme_id)
    rows = []
    for s, e in roster:
        m = marks.get(s.id)
        rows.append({
            "student_id": s.id, "student_uuid": str(s.uuid), "name": s.full_name, "roll_no": e.roll_no,
            "admission_no": s.admission_no, "photo_url": s.photo_url,
            "marks": num(m.marks) if m else None, "components": (m.components if m else {}) or {},
            "code": m.code if m else None, "grade": m.grade if m else None,
            "grace": num(m.grace) if m else 0, "moderation": num(m.moderation) if m else 0,
            "remarks": m.remarks if m else None,
        })
    status = sheet.status if sheet else "draft"
    editable = access["enter"] and status == "draft" or access["moderate"] and status == "submitted"
    stats = sheet_stats(paper, rows)
    return {
        "paper": paper_out(paper), "exam": {"uuid": str(paper.exam.uuid), "name": paper.exam.name,
                                           "status": paper.exam.status},
        "section": {"id": sec.id, "label": academics.section_label(sec)},
        "status": status, "moderated": bool(sheet and sheet.moderated),
        "moderation_note": sheet.moderation_note if sheet else None,
        "submitted_at": sheet.submitted_at if sheet else None, "locked_at": sheet.locked_at if sheet else None,
        "can_enter": access["enter"], "can_moderate": access["moderate"], "editable": bool(editable),
        "codes": [{"code": k, "label": v} for k, v in calc.CODES.items()],
        "grades": [b["label"] for b in sorted(bands_of(scheme), key=lambda b: -D(b["min_percent"]))],
        "rows": rows, "stats": stats,
    }


def sheet_stats(paper: ExamPaper, rows: list[dict]) -> dict:
    vals = [D(r["marks"]) for r in rows if r["marks"] is not None and not r["code"]]
    entered = sum(1 for r in rows if r["marks"] is not None or r["code"] or r.get("grade"))
    mx = D(paper.max_marks) or Decimal(100)
    pm = D(paper.pass_marks)
    return {
        "students": len(rows), "entered": entered, "absent": sum(1 for r in rows if r["code"] == "AB"),
        "average": num(sum(vals, Decimal(0)) / len(vals)) if vals else None,
        "highest": num(max(vals)) if vals else None, "lowest": num(min(vals)) if vals else None,
        "below_pass": sum(1 for v in vals if pm is not None and v < pm) if pm is not None else None,
        "max_marks": num(mx),
    }


def _validate_entry(paper: ExamPaper, entry: dict, grades: set[str]) -> dict:
    """Clean one learner's entry; raises a plain error naming what is wrong."""
    code = (entry.get("code") or "").strip().upper() or None
    if code and code not in calc.CODES:
        raise ValueError(f"Unknown code {code}. Use AB, EX or ML.")
    mx = D(paper.max_marks) or Decimal(100)
    comps = paper.components or []
    out = {"code": code, "marks": None, "components": {}, "grade": None,
           "remarks": (entry.get("remarks") or None)}
    if out["remarks"]:
        out["remarks"] = str(out["remarks"])[:300]
    if paper.entry_mode == "grade":
        g = (entry.get("grade") or "").strip()
        if g and not code:
            match = next((x for x in grades if x.lower() == g.lower()), None)
            if match is None:
                raise ValueError(f"{g} is not a grade in this scheme.")
            out["grade"] = match
        return out
    if code:
        return out
    if comps and isinstance(entry.get("components"), dict) and any(
            entry["components"].get(c["key"]) not in (None, "") for c in comps):
        total = Decimal(0)
        for c in comps:
            raw = entry["components"].get(c["key"])
            if raw in (None, ""):
                continue
            v = D(raw)
            cmax = D(c.get("max_marks")) or mx
            if v < 0 or v > cmax:
                raise ValueError(f"{c.get('label') or c['key']} must be between 0 and {num(cmax):g}.")
            out["components"][c["key"]] = float(v)
            total += v
        out["marks"] = total
    else:
        raw = entry.get("marks")
        if raw not in (None, ""):
            try:
                v = D(raw)
            except Exception:
                raise ValueError(f"{raw} is not a number.")
            if v < 0 or v > mx:
                raise ValueError(f"Marks must be between 0 and {num(mx):g}.")
            out["marks"] = v
    if out["marks"] is not None and out["marks"] > mx:
        raise ValueError(f"Marks must not be more than {num(mx):g}.")
    return out


def save_marks(db: Session, user: dict, paper: ExamPaper, section_id: int, entries: list[dict],
               remove_missing: bool = False) -> dict:
    cid = user["company_id"]
    section_for_paper(db, cid, paper, section_id)
    access = sheet_access(db, user, paper, section_id)
    if not access["enter"] and not access["moderate"]:
        raise HTTPException(status_code=403, detail="You can enter marks only for the subjects you teach.")
    sheet = get_sheet(db, cid, paper, section_id, create=True)
    if sheet.status == "locked":
        raise HTTPException(status_code=409, detail="These marks are locked. Ask the principal to unlock them.")
    if sheet.status == "submitted" and not access["moderate"]:
        raise HTTPException(status_code=409, detail="These marks were submitted. Ask for them to be sent back to edit.")
    if paper.exam.archived_at:
        raise HTTPException(status_code=409, detail="This exam is archived.")
    roster = {s.id for s, _ in students.section_students(db, cid, section_id)}
    scheme = resolve_scheme(db, cid, paper.program_id, paper.grading_scheme_id or paper.exam.grading_scheme_id)
    grades = {b["label"] for b in bands_of(scheme)}
    existing = {m.student_id: m for m in db.query(Mark).filter(Mark.company_id == cid, Mark.paper_id == paper.id).all()}
    errors, saved = [], 0
    for entry in entries:
        sid = entry.get("student_id")
        if sid not in roster:
            errors.append({"student_id": sid, "error": "Not in this section."})
            continue
        try:
            clean = _validate_entry(paper, entry, grades)
        except ValueError as exc:
            errors.append({"student_id": sid, "error": str(exc)})
            continue
        m = existing.get(sid)
        empty = clean["marks"] is None and not clean["code"] and not clean["grade"] and not clean["remarks"]
        if m is None:
            if empty:
                continue
            m = Mark(company_id=cid, exam_id=paper.exam_id, paper_id=paper.id, student_id=sid, section_id=section_id)
            db.add(m)
            existing[sid] = m
        m.marks, m.code, m.grade, m.remarks = clean["marks"], clean["code"], clean["grade"], clean["remarks"]
        m.components = clean["components"]
        m.section_id = section_id
        saved += 1
    if errors and not saved:
        db.rollback()
        raise HTTPException(status_code=400, detail=errors[0]["error"] if len(errors) == 1 else
                            f"{len(errors)} entries could not be saved. Check the highlighted rows.")
    sheet.entered_by = user.get("employee_id") or sheet.entered_by
    db.flush()
    recompute_for_section(db, cid, paper.exam, section_id)
    return {"saved": saved, "errors": errors}


def set_sheet_status(db: Session, user: dict, paper: ExamPaper, section_id: int, action: str,
                     note: str | None = None) -> MarkSheet:
    cid = user["company_id"]
    section_for_paper(db, cid, paper, section_id)
    access = sheet_access(db, user, paper, section_id)
    sheet = get_sheet(db, cid, paper, section_id, create=True)
    emp = user.get("employee_id")
    if action == "submit":
        if not access["enter"]:
            raise HTTPException(status_code=403, detail="You can submit marks only for the subjects you teach.")
        if sheet.status != "draft":
            raise HTTPException(status_code=409, detail="These marks are already submitted.")
        roster = students.section_students(db, cid, section_id)
        have = {m.student_id for m in db.query(Mark).filter(Mark.paper_id == paper.id, Mark.section_id == section_id).all()
                if m.marks is not None or m.code or m.grade}
        missing = [s.full_name for s, _ in roster if s.id not in have]
        if missing:
            raise HTTPException(status_code=400, detail=f"Enter marks or a code for everyone first ({len(missing)} missing, "
                                                        f"e.g. {missing[0]}).")
        sheet.status, sheet.submitted_at, sheet.submitted_by = "submitted", now(), emp
    elif action == "return":
        if not access["moderate"]:
            raise HTTPException(status_code=403, detail="Only the principal or exam cell can send marks back.")
        if sheet.status == "locked":
            raise HTTPException(status_code=409, detail="Unlock the marks first.")
        sheet.status = "draft"
    elif action == "lock":
        if not access["moderate"]:
            raise HTTPException(status_code=403, detail="Only the principal or exam cell can lock marks.")
        sheet.status, sheet.locked_at, sheet.locked_by = "locked", now(), emp
    elif action == "unlock":
        if not access["moderate"]:
            raise HTTPException(status_code=403, detail="Only the principal or exam cell can unlock marks.")
        sheet.status, sheet.locked_at, sheet.locked_by = "submitted", None, None
    else:
        raise HTTPException(status_code=400, detail="Unknown action.")
    if note:
        sheet.moderation_note = note[:2000]
    audit.event(db, f"exams.marks.{action}", "exam_mark_sheets", sheet.id,
                {"paper": str(paper.uuid), "section_id": section_id}, entity_uuid=str(sheet.uuid),
                entity_label=f"{paper.exam.name} · {paper.subject.name if paper.subject else ''}")
    db.flush()
    recompute_for_section(db, cid, paper.exam, section_id)
    return sheet


def moderate(db: Session, user: dict, paper: ExamPaper, section_id: int, *, add_to_all=None,
             adjustments: list[dict] | None = None, note: str | None = None) -> int:
    """Moderation by the principal / exam cell: add (or take away) marks for everyone or for
    single learners, and grace marks. The original marks stay as typed."""
    cid = user["company_id"]
    section_for_paper(db, cid, paper, section_id)
    if MODERATE not in perms(db, user):
        raise HTTPException(status_code=403, detail="Only the principal or exam cell can moderate marks.")
    sheet = get_sheet(db, cid, paper, section_id, create=True)
    mx = D(paper.max_marks) or Decimal(100)
    marks = {m.student_id: m for m in db.query(Mark).filter(Mark.company_id == cid, Mark.paper_id == paper.id,
                                                            Mark.section_id == section_id).all()}
    changed = 0
    if add_to_all not in (None, "", 0):
        delta = D(add_to_all)
        if abs(delta) > mx:
            raise HTTPException(status_code=400, detail="That adjustment is larger than the paper.")
        for m in marks.values():
            if m.marks is not None and not m.code:
                m.moderation = delta
                changed += 1
    for adj in adjustments or []:
        m = marks.get(adj.get("student_id"))
        if m is None:
            continue
        if adj.get("moderation") is not None:
            m.moderation = D(adj["moderation"])
        if adj.get("grace") is not None:
            g = D(adj["grace"])
            if g < 0 or g > mx:
                raise HTTPException(status_code=400, detail="Grace marks must be between 0 and the paper's maximum.")
            m.grace = g
        changed += 1
    sheet.moderated = True
    if note:
        sheet.moderation_note = note[:2000]
    audit.event(db, "exams.marks.moderated", "exam_mark_sheets", sheet.id,
                {"paper": str(paper.uuid), "section_id": section_id, "add_to_all": str(add_to_all) if add_to_all else None,
                 "changed": changed}, entity_uuid=str(sheet.uuid))
    db.flush()
    recompute_for_section(db, cid, paper.exam, section_id)
    return changed


def import_csv(db: Session, user: dict, paper: ExamPaper, section_id: int, text: str) -> dict:
    """Rows keyed by roll number or admission number; columns marks (or component keys),
    code, grade, remarks."""
    cid = user["company_id"]
    roster = students.section_students(db, cid, section_id)
    by_roll = {(e.roll_no or "").strip().lower(): s.id for s, e in roster if e.roll_no}
    by_adm = {s.admission_no.strip().lower(): s.id for s, _ in roster}
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames:
        raise HTTPException(status_code=400, detail="The file is empty.")
    fields = {f.strip().lower(): f for f in reader.fieldnames if f}
    comp_keys = [c["key"] for c in (paper.components or [])]
    entries, errors = [], []
    for i, row in enumerate(reader, start=2):
        def col(name):
            k = fields.get(name)
            return (row.get(k) or "").strip() if k else ""
        sid = by_roll.get(col("roll_no").lower() or col("roll").lower()) or by_adm.get(col("admission_no").lower())
        if not sid:
            errors.append({"line": i, "error": "No learner with this roll or admission number in the section."})
            continue
        e = {"student_id": sid, "code": col("code") or None, "grade": col("grade") or None,
             "remarks": col("remarks") or None}
        raw = col("marks")
        if raw.upper() in calc.CODES:
            e["code"] = raw.upper()
        elif comp_keys and any(col(k.lower()) for k in comp_keys):
            e["components"] = {k: col(k.lower()) or None for k in comp_keys}
        else:
            e["marks"] = raw or None
        entries.append(e)
    if not entries:
        raise HTTPException(status_code=400, detail=errors[0]["error"] if errors else "No rows found in the file.")
    res = save_marks(db, user, paper, section_id, entries)
    res["errors"] = errors + res["errors"]
    return res


def csv_template(db: Session, company_id: int, paper: ExamPaper, section_id: int) -> str:
    from core.csv_safe import writer as safe_writer
    buf = io.StringIO()
    w = safe_writer(buf)
    comp_keys = [c["key"] for c in (paper.components or [])]
    head = ["roll_no", "admission_no", "name"] + (comp_keys or ["marks"]) + ["code", "remarks"]
    if paper.entry_mode == "grade":
        head = ["roll_no", "admission_no", "name", "grade", "code", "remarks"]
    w.writerow(head)
    marks = {m.student_id: m for m in db.query(Mark).filter(Mark.company_id == company_id, Mark.paper_id == paper.id).all()}
    for s, e in students.section_students(db, company_id, section_id):
        m = marks.get(s.id)
        if paper.entry_mode == "grade":
            vals = [m.grade if m else ""]
        elif comp_keys:
            vals = [((m.components or {}).get(k, "") if m else "") for k in comp_keys]
        else:
            vals = [num(m.marks) if m and m.marks is not None else ""]
        w.writerow([e.roll_no or "", s.admission_no, s.full_name] + vals + [m.code if m and m.code else "",
                                                                          m.remarks if m and m.remarks else ""])
    return buf.getvalue()


def sheets_for(db: Session, user: dict, exam: Exam, mine_only: bool = False) -> list[dict]:
    """Every (section, paper) mark sheet of an exam the person may open, with progress."""
    cid = user["company_id"]
    p = perms(db, user)
    emp = user.get("employee_id")
    see_all = (MARKS_ALL in p or VIEW in p or MODERATE in p) and not mine_only
    sections = exam_sections(db, cid, exam)
    allocs = {}
    if emp:
        for a in db.query(SectionSubject).filter(SectionSubject.company_id == cid,
                                                 SectionSubject.section_id.in_([s.id for s in sections] or [-1])).all():
            allocs[(a.section_id, a.subject_id)] = a
    papers = [pp for pp in exam.papers]
    sheets = {(s.paper_id, s.section_id): s for s in db.query(MarkSheet).filter(MarkSheet.exam_id == exam.id).all()}
    counts = defaultdict(int)
    for paper_id, section_id in db.query(Mark.paper_id, Mark.section_id).filter(
            Mark.exam_id == exam.id, or_(Mark.marks.isnot(None), Mark.code.isnot(None), Mark.grade.isnot(None))).all():
        counts[(paper_id, section_id)] += 1
    strength = defaultdict(int)
    for (sec_id,) in db.query(Enrollment.section_id).join(Student, Student.id == Enrollment.student_id).filter(
            Enrollment.company_id == cid, Enrollment.section_id.in_([s.id for s in sections] or [-1]),
            Enrollment.status.in_(students.LIVE_ENROLMENT), Student.status == students.ACTIVE).all():
        strength[sec_id] += 1
    out = []
    for sec in sections:
        for paper in papers:
            if paper.program_id != sec.program_id:
                continue
            a = allocs.get((sec.id, paper.subject_id))
            own = bool(a and emp and (a.teacher_id == emp or emp in (a.assistant_ids or []))) and MARKS in p
            if not own and not see_all:
                continue
            sh = sheets.get((paper.id, sec.id))
            out.append({
                "paper_uuid": str(paper.uuid), "paper_id": paper.id, "subject_id": paper.subject_id,
                "subject_name": paper.subject.name if paper.subject else "", "section_id": sec.id,
                "section_label": academics.section_label(sec), "program_id": sec.program_id,
                "status": sh.status if sh else "draft", "moderated": bool(sh and sh.moderated),
                "entered": counts[(paper.id, sec.id)], "students": strength[sec.id], "own": own,
                "teacher_name": (f"{a.teacher.first_name} {a.teacher.last_name}".strip() if a and a.teacher else None),
                "paper_date": paper.paper_date, "max_marks": num(paper.max_marks), "entry_mode": paper.entry_mode,
            })
    return out


# ── Attendance (optional module) ─────────────────────────────────────

def attendance_percent(db: Session, company_id: int, student_id: int, start: date | None, end: date | None,
                       record: TermRecord | None = None) -> Decimal | None:
    """Attendance % for the period: from the student attendance module when it offers a
    percentage function, else from the days typed in on the term record."""
    for modname in ("modules.student_attendance.service",):
        try:
            mod = importlib.import_module(modname)
        except Exception:
            continue
        bulk = getattr(mod, "percentages", None)  # percentages(db, cid, [ids], start, end) -> {id: {pct, ...}}
        if callable(bulk) and start and end:
            try:
                row = (bulk(db, company_id, [student_id], start, end) or {}).get(student_id) or {}
                if row.get("pct") is not None:
                    return D(row["pct"])
            except Exception:
                pass
        for fname in ("attendance_percent", "student_attendance_percent", "percentage", "attendance_percentage"):
            fn = getattr(mod, fname, None)
            if not callable(fn):
                continue
            for call in (lambda: fn(db, company_id, student_id, start, end),
                         lambda: fn(db, company_id, student_id, start=start, end=end),
                         lambda: fn(db, company_id, student_id)):
                try:
                    v = call()
                except TypeError:
                    continue
                except Exception:
                    break
                if isinstance(v, dict):
                    v = v.get("percent") if "percent" in v else v.get("percentage")
                if v is not None:
                    try:
                        return D(v)
                    except Exception:
                        pass
                break
    if record is not None and record.days_total:
        return D(record.days_present or 0) * 100 / D(record.days_total)
    return None


# ── Result computation ───────────────────────────────────────────────

def _subject_row(paper: ExamPaper, mark: Mark | None, bands: list[dict], scheme: GradingScheme | None,
                 credits, pass_override=None) -> dict:
    mx = D(paper.max_marks) or Decimal(100)
    row = {"subject_id": paper.subject_id, "subject": paper.subject.name if paper.subject else "",
           "code": paper.subject.code if paper.subject else None, "paper_id": paper.id,
           "max": num(mx), "obtained": None, "percent": None, "grade": None, "grade_point": None,
           "credits": num(credits), "passed": None, "mark_code": mark.code if mark else None,
           "counts": bool(paper.counts_in_total and paper.entry_mode == "marks"), "excluded": False,
           "missing": False, "absent": False, "grace": 0, "components": (mark.components if mark else {}) or {}}
    pass_pct = D(pass_override) if pass_override is not None else (
        D(paper.pass_marks) * 100 / mx if paper.pass_marks is not None else (D(scheme.pass_percent) if scheme else None))
    row["pass_percent"] = num(pass_pct)
    if paper.entry_mode == "grade":
        if mark is None or (not mark.grade and not mark.code):
            row["missing"] = True
            return row
        if mark.code in calc.EXCLUDED_CODES:
            row["excluded"] = True
            return row
        b = calc.band_by_label(mark.grade, bands)
        row["grade"] = mark.grade
        row["grade_point"] = num(b["grade_point"]) if b else None
        row["passed"] = bool(b["is_pass"]) if b else True
        return row
    if mark is None or (mark.marks is None and not mark.code):
        row["missing"] = True
        return row
    if mark.code in calc.EXCLUDED_CODES:
        row["excluded"] = True
        return row
    if mark.code == "AB":
        obtained, row["absent"] = Decimal(0), True
    else:
        obtained = D(mark.marks) + D(mark.moderation or 0) + D(mark.grace or 0)
    obtained = max(Decimal(0), min(obtained, mx))
    pct = calc.percent(obtained, mx)
    row.update(obtained=num(obtained), percent=num(pct), grace=num(mark.grace or 0))
    b = calc.band_for(pct, bands)
    if b:
        row["grade"], row["grade_point"] = b["label"], num(b["grade_point"])
    passed = (pct >= pass_pct) if pass_pct is not None else (bool(b["is_pass"]) if b else True)
    # Components with their own pass mark (e.g. practical) must each be passed.
    for c in paper.components or []:
        if c.get("pass_marks") not in (None, "") and mark.code != "AB":
            got = D((mark.components or {}).get(c["key"]))
            if got is None or got < D(c["pass_marks"]):
                passed = False
    if mark.code == "AB":
        passed = False
    row["passed"] = bool(passed)
    return row


def _overall(rows: list[dict], scheme: GradingScheme | None, bands: list[dict]) -> dict:
    counted = [r for r in rows if r["counts"] and not r["excluded"]]
    complete = not any(r["missing"] for r in rows if r["counts"] or r["grade_point"] is not None)
    tot_o = sum((D(r["obtained"]) for r in counted if r["obtained"] is not None), Decimal(0))
    tot_m = sum((D(r["max"]) for r in counted if r["obtained"] is not None), Decimal(0))
    pct = calc.percent(tot_o, tot_m) if tot_m else None
    band = calc.band_for(pct, bands) if pct is not None else None
    gp_rows = [(r["grade_point"], r["credits"]) for r in rows if r["grade_point"] is not None and not r["excluded"]
               and (r["counts"] or (scheme is not None and scheme.kind == "gpa"))]
    g = calc.gpa(gp_rows) if scheme is not None and scheme.kind == "gpa" else None
    credit_rows = [r for r in rows if not r["excluded"] and r["credits"] is not None and not r["missing"]]
    attempted = sum((D(r["credits"]) for r in credit_rows), Decimal(0)) if credit_rows else None
    earned = sum((D(r["credits"]) for r in credit_rows if r["passed"]), Decimal(0)) if credit_rows else None
    grade = band["label"] if band else None
    gpoint = band["grade_point"] if band else None
    if g is not None and bands:
        # On GPA schemes the overall letter follows the GPA (closest grade point not above it).
        cands = [b for b in bands if b["grade_point"] is not None and D(b["grade_point"]) <= g]
        if cands:
            best = max(cands, key=lambda b: D(b["grade_point"]))
            grade, gpoint = best["label"], best["grade_point"]
    return {"total_obtained": tot_o if tot_m else None, "total_max": tot_m if tot_m else None, "percentage": pct,
            "grade": grade, "grade_point": gpoint, "gpa": g, "credits_attempted": attempted, "credits_earned": earned,
            "complete": complete}


def _store(db: Session, company_id: int, source: str, student_id: int, sec: Section, data: dict,
           existing: dict) -> ExamResult:
    r = existing.get(student_id)
    if r is None:
        r = ExamResult(company_id=company_id, source=source, student_id=student_id)
        db.add(r)
        existing[student_id] = r
    for k, v in data.items():
        setattr(r, k, v)
    r.section_id, r.program_id, r.session_id = sec.id, sec.program_id, sec.session_id
    r.computed_at = now()
    return r


def compute_exam(db: Session, company_id: int, exam: Exam, section_ids: list[int] | None = None) -> int:
    """(Re)compute every learner's result for one exam (optionally only some sections)."""
    source = f"exam:{exam.id}"
    sections = exam_sections(db, company_id, exam, section_ids)
    papers_by_prog = defaultdict(list)
    for p in exam.papers:
        papers_by_prog[p.program_id].append(p)
    cmap = credits_map(db, company_id, set(papers_by_prog))
    marks = {(m.paper_id, m.student_id): m for m in db.query(Mark).filter(Mark.company_id == company_id,
                                                                          Mark.exam_id == exam.id).all()}
    existing = {r.student_id: r for r in db.query(ExamResult).filter(ExamResult.company_id == company_id,
                                                                     ExamResult.source == source).all()}
    schemes: dict = {}
    n = 0
    for sec in sections:
        papers = papers_by_prog.get(sec.program_id) or []
        if not papers:
            continue
        key = sec.program_id
        if key not in schemes:
            schemes[key] = resolve_scheme(db, company_id, sec.program_id, exam.grading_scheme_id)
        scheme = schemes[key]
        bands = bands_of(scheme)
        paper_schemes = {}
        for p in papers:
            if p.grading_scheme_id:
                s2 = resolve_scheme(db, company_id, None, p.grading_scheme_id)
                paper_schemes[p.id] = (s2, bands_of(s2))
        for s, _e in students.section_students(db, company_id, sec.id):
            rows = []
            for p in sorted(papers, key=lambda x: x.sort_order):
                sch, bnd = paper_schemes.get(p.id, (scheme, bands))
                rows.append(_subject_row(p, marks.get((p.id, s.id)), bnd, sch,
                                         _credits(cmap, p.program_id, p.subject_id, p.credits)))
            ov = _overall(rows, scheme, bands)
            graded = [r for r in rows if not r["excluded"] and not r["missing"]]
            failed = sum(1 for r in graded if r["passed"] is False)
            status = calc.decide([r["passed"] for r in graded if r["passed"] is not None], ov["percentage"],
                                 complete=ov["complete"])
            _store(db, company_id, source, s.id, sec, {
                "exam_id": exam.id, "rule_id": None, "term_id": exam.term_id, "scheme_id": scheme.id if scheme else None,
                "total_obtained": q(ov["total_obtained"]), "total_max": q(ov["total_max"]),
                "percentage": q(ov["percentage"]), "grade": ov["grade"], "grade_point": ov["grade_point"],
                "gpa": q(ov["gpa"]), "cgpa": None, "credits_attempted": ov["credits_attempted"],
                "credits_earned": ov["credits_earned"], "status": status,
                "passed": None if status == "incomplete" else status == "pass", "failed_subjects": failed,
                "grace_applied": sum((D(r["grace"] or 0) for r in rows), Decimal(0)), "subjects": rows,
            }, existing)
            n += 1
    db.flush()
    rerank(db, company_id, source, exam.ranking)
    return n


def _rule_programs(db: Session, company_id: int, rule: ResultRule) -> list[int]:
    progs = [int(p) for p in (rule.program_ids or [])]
    if progs:
        return progs
    ids = set()
    for g in rule.groups or []:
        for eid in g.get("exam_ids") or []:
            e = db.query(Exam).filter(Exam.company_id == company_id, Exam.id == int(eid)).first()
            if e:
                if e.program_ids:
                    ids |= {int(x) for x in e.program_ids}
                else:
                    ids |= {p.program_id for p in e.papers}
    return sorted(ids)


def compute_rule(db: Session, company_id: int, rule: ResultRule, section_ids: list[int] | None = None) -> int:
    """(Re)compute term / final results that combine several exams under a result rule."""
    from core.expressions import ExpressionError, evaluate
    source = f"rule:{rule.id}"
    groups = [g for g in (rule.groups or []) if g.get("exam_ids")]
    if not groups:
        return 0
    exam_ids = sorted({int(e) for g in groups for e in g["exam_ids"]})
    exams = {e.id: e for e in db.query(Exam).options(joinedload(Exam.papers)).filter(
        Exam.company_id == company_id, Exam.id.in_(exam_ids)).all()}
    progs = _rule_programs(db, company_id, rule)
    qy = (db.query(Section).options(joinedload(Section.program)).filter(
        Section.company_id == company_id, Section.session_id == rule.session_id, Section.active.is_(True)))
    if progs:
        qy = qy.filter(Section.program_id.in_(progs))
    if section_ids:
        qy = qy.filter(Section.id.in_(section_ids))
    sections = qy.all()
    marks = defaultdict(dict)
    for m in db.query(Mark).filter(Mark.company_id == company_id, Mark.exam_id.in_(exam_ids)).all():
        marks[m.exam_id][(m.paper_id, m.student_id)] = m
    cmap = credits_map(db, company_id, {s.program_id for s in sections})
    existing = {r.student_id: r for r in db.query(ExamResult).filter(ExamResult.company_id == company_id,
                                                                     ExamResult.source == source).all()}
    term = rule.term
    sess_start = term.start_date if term else None
    sess_end = term.end_date if term else None
    if not term:
        from modules.academics.models import AcademicSession
        sess = db.query(AcademicSession).filter(AcademicSession.id == rule.session_id).first()
        sess_start, sess_end = (sess.start_date, sess.end_date) if sess else (None, None)
    scope = f"term:{rule.term_id}" if rule.term_id else "session"
    records = {r.student_id: r for r in db.query(TermRecord).filter(
        TermRecord.company_id == company_id, TermRecord.session_id == rule.session_id, TermRecord.scope == scope).all()}
    out_of = D((rule.config or {}).get("out_of")) or Decimal(100)
    n = 0
    for sec in sections:
        scheme = resolve_scheme(db, company_id, sec.program_id, rule.grading_scheme_id)
        bands = bands_of(scheme)
        pass_pct = D(rule.subject_pass_percent) if rule.subject_pass_percent is not None else (
            D(scheme.pass_percent) if scheme else None)
        # Papers of this program in each exam, by subject.
        subj_papers: dict[int, dict[int, ExamPaper]] = defaultdict(dict)  # subject -> exam -> paper
        subj_meta: dict[int, ExamPaper] = {}
        for eid, ex in exams.items():
            for p in ex.papers:
                if p.program_id == sec.program_id:
                    subj_papers[p.subject_id][eid] = p
                    subj_meta.setdefault(p.subject_id, p)
        if not subj_papers:
            continue
        ordered_subjects = sorted(subj_papers, key=lambda sid: (subj_meta[sid].sort_order, subj_meta[sid].subject_id))
        for s, _e in students.section_students(db, company_id, sec.id):
            rows = []
            for sid in ordered_subjects:
                meta = subj_meta[sid]
                credits = _credits(cmap, sec.program_id, sid, meta.credits)
                row = {"subject_id": sid, "subject": meta.subject.name if meta.subject else "",
                       "code": meta.subject.code if meta.subject else None, "max": num(out_of), "obtained": None,
                       "percent": None, "grade": None, "grade_point": None, "credits": num(credits), "passed": None,
                       "counts": bool(meta.counts_in_total and meta.entry_mode == "marks"), "excluded": False,
                       "missing": False, "absent": False, "grace": 0, "parts": {}, "pass_percent": num(pass_pct)}
                if meta.entry_mode == "grade":
                    # Graded subjects take the latest grade given.
                    latest = None
                    for eid in sorted(subj_papers[sid], key=lambda e: (exams[e].start_date or date.min)):
                        m = marks[eid].get((subj_papers[sid][eid].id, s.id))
                        if m and m.grade:
                            latest = m.grade
                    if latest:
                        pb = bands_of(resolve_scheme(db, company_id, None, meta.grading_scheme_id)) \
                            if meta.grading_scheme_id else bands
                        b = calc.band_by_label(latest, pb)
                        row.update(grade=latest, grade_point=num(b["grade_point"]) if b else None,
                                   passed=bool(b["is_pass"]) if b else True)
                    else:
                        row["missing"] = True
                    rows.append(row)
                    continue
                any_missing, any_value, absent = False, False, False
                vars_: dict = {}
                parts = []
                for g in groups:
                    vals = []
                    for eid in g["exam_ids"]:
                        eid = int(eid)
                        p = subj_papers[sid].get(eid)
                        if p is None:
                            continue
                        r1 = _subject_row(p, marks[eid].get((p.id, s.id)), bands, scheme, credits)
                        if r1["missing"]:
                            any_missing = True
                            continue
                        if r1["excluded"]:
                            continue
                        absent = absent or r1["absent"]
                        vals.append(D(r1["percent"]))
                    gv = calc.combine(vals, g.get("combine") or "average", g.get("best_of"))
                    key = g.get("key") or f"g{len(parts) + 1}"
                    vars_[key] = gv if gv is not None else Decimal(0)
                    parts.append((gv, g.get("weight", 1)))
                    row["parts"][key] = num(gv)
                    any_value = any_value or gv is not None
                if not any_value:
                    row["missing" if any_missing else "excluded"] = True
                    rows.append(row)
                    continue
                if rule.method == "formula" and rule.formula:
                    try:
                        pct = D(evaluate(rule.formula, vars_))
                    except (ExpressionError, Exception):
                        pct = calc.weighted(parts)
                else:
                    pct = calc.weighted(parts)
                pct = max(Decimal(0), min(pct, Decimal(100))) if pct is not None else None
                row["percent"] = num(pct)
                row["obtained"] = num(pct * out_of / 100) if pct is not None else None
                row["absent"] = absent
                row["missing"] = any_missing and not any_value
                rows.append(row)
            # Grace marks under the rule, then grades and passes.
            grace = calc.apply_grace([{"percent": r["percent"], "pass_percent": pass_pct, "absent": r["absent"]}
                                      if r["counts"] and not r["excluded"] and not r["missing"] else {}
                                      for r in rows], rule.grace_per_subject, rule.grace_max_subjects, rule.grace_total)
            total_grace = Decimal(0)
            for r, gr in zip(rows, grace):
                if r["percent"] is None or r["excluded"] or r["missing"]:
                    continue
                pct = D(r["percent"]) + gr
                if gr:
                    r["grace"] = num(gr)
                    total_grace += gr
                    r["percent"] = num(pct)
                    r["obtained"] = num(pct * out_of / 100)
                b = calc.band_for(pct, bands)
                if b:
                    r["grade"], r["grade_point"] = b["label"], num(b["grade_point"])
                r["passed"] = (pct >= pass_pct) if pass_pct is not None else (bool(b["is_pass"]) if b else True)
            ov = _overall(rows, scheme, bands)
            rec = records.get(s.id)
            att = attendance_percent(db, company_id, s.id, sess_start, sess_end, rec)
            graded = [r for r in rows if not r["excluded"] and not r["missing"]]
            failed = sum(1 for r in graded if r["passed"] is False)
            status = calc.decide([r["passed"] for r in graded if r["passed"] is not None], ov["percentage"],
                                 pass_all_subjects=rule.pass_all_subjects, min_aggregate=rule.min_aggregate_percent,
                                 max_failed_subjects=rule.max_failed_subjects, attendance=att,
                                 min_attendance=rule.min_attendance_percent, complete=ov["complete"])
            cg = None
            if rule.cumulative and ov["gpa"] is not None:
                prev = (db.query(ExamResult.gpa, ExamResult.credits_attempted)
                        .join(ResultRule, ResultRule.id == ExamResult.rule_id)
                        .filter(ExamResult.company_id == company_id, ExamResult.student_id == s.id,
                                ExamResult.rule_id.isnot(None), ExamResult.rule_id != rule.id,
                                ExamResult.gpa.isnot(None), ResultRule.term_id.isnot(None),
                                ResultRule.cumulative.is_(True)).all())
                cg = calc.cgpa([(g, c) for g, c in prev] + [(ov["gpa"], ov["credits_attempted"])])
            _store(db, company_id, source, s.id, sec, {
                "exam_id": None, "rule_id": rule.id, "term_id": rule.term_id, "scheme_id": scheme.id if scheme else None,
                "total_obtained": q(ov["total_obtained"]), "total_max": q(ov["total_max"]),
                "percentage": q(ov["percentage"]), "grade": ov["grade"], "grade_point": ov["grade_point"],
                "gpa": q(ov["gpa"]), "cgpa": q(cg), "credits_attempted": ov["credits_attempted"],
                "credits_earned": ov["credits_earned"], "status": status,
                "passed": None if status == "incomplete" else status == "pass", "failed_subjects": failed,
                "grace_applied": total_grace, "attendance_percent": q(att), "subjects": rows,
            }, existing)
            n += 1
    db.flush()
    rerank(db, company_id, source, rule.ranking)
    return n


def rerank(db: Session, company_id: int, source: str, ranking: bool) -> None:
    rows = db.query(ExamResult).filter(ExamResult.company_id == company_id, ExamResult.source == source).all()
    if not ranking:
        for r in rows:
            r.rank = r.program_rank = None
        db.flush()
        return
    by_sec, by_prog = defaultdict(dict), defaultdict(dict)
    for r in rows:
        val = r.percentage if r.status != "incomplete" else None
        if r.gpa is not None and r.status != "incomplete":
            val = r.gpa
        by_sec[r.section_id][r.id] = val
        by_prog[r.program_id][r.id] = val
    ranks_s, ranks_p = {}, {}
    for d in by_sec.values():
        ranks_s.update(calc.competition_rank(d))
    for d in by_prog.values():
        ranks_p.update(calc.competition_rank(d))
    for r in rows:
        r.rank, r.program_rank = ranks_s.get(r.id), ranks_p.get(r.id)
    db.flush()


def recompute_for_section(db: Session, company_id: int, exam: Exam, section_id: int) -> None:
    """After marks change: the exam's results for the section and every rule using the exam."""
    compute_exam(db, company_id, exam, [section_id])
    for rule in db.query(ResultRule).filter(ResultRule.company_id == company_id, ResultRule.session_id == exam.session_id,
                                            ResultRule.active.is_(True)).all():
        if any(int(e) == exam.id for g in (rule.groups or []) for e in (g.get("exam_ids") or [])):
            compute_rule(db, company_id, rule, [section_id])


def source_of(db: Session, company_id: int, source: str):
    """'exam:<uuid>' / 'rule:<uuid>' (or ids) -> (kind, object)."""
    kind, _, ident = (source or "").partition(":")
    model = {"exam": Exam, "rule": ResultRule}.get(kind)
    if model is None or not ident:
        raise HTTPException(status_code=400, detail="Pick an exam or a result rule.")
    col = model.id if ident.isdigit() else model.uuid
    try:
        obj = db.query(model).filter(model.company_id == company_id, col == (int(ident) if ident.isdigit() else ident)).first()
    except Exception:
        db.rollback()
        obj = None
    if obj is None:
        raise HTTPException(status_code=404, detail="Exam or result rule not found")
    return kind, obj


def result_out(r: ExamResult, s: Student | None = None, section_label: str | None = None, full: bool = False) -> dict:
    out = {
        "id": r.id, "uuid": str(r.uuid), "source": r.source, "student_id": r.student_id,
        "section_id": r.section_id, "program_id": r.program_id, "term_id": r.term_id,
        "total_obtained": num(r.total_obtained), "total_max": num(r.total_max), "percentage": num(r.percentage),
        "grade": r.grade, "grade_point": num(r.grade_point), "gpa": num(r.gpa), "cgpa": num(r.cgpa),
        "credits_attempted": num(r.credits_attempted), "credits_earned": num(r.credits_earned), "rank": r.rank,
        "program_rank": r.program_rank, "status": r.status, "passed": r.passed, "failed_subjects": r.failed_subjects,
        "grace_applied": num(r.grace_applied), "attendance_percent": num(r.attendance_percent),
        "computed_at": r.computed_at, "published_at": r.published_at, "published": r.published_at is not None,
    }
    if s is not None:
        out.update({"student_name": s.full_name, "admission_no": s.admission_no, "student_uuid": str(s.uuid),
                    "photo_url": s.photo_url})
    if section_label is not None:
        out["section_label"] = section_label
    if full:
        out["subjects"] = r.subjects or []
    return out


def source_label(db: Session, r: ExamResult) -> str:
    if r.exam_id:
        e = db.query(Exam).filter(Exam.id == r.exam_id).first()
        return e.name if e else "Exam"
    rule = db.query(ResultRule).filter(ResultRule.id == r.rule_id).first()
    return rule.name if rule else "Result"


# ── Publishing ───────────────────────────────────────────────────────

def publish(db: Session, user: dict, source: str, section_ids: list[int] | None, notify: bool = True) -> dict:
    cid = user["company_id"]
    kind, obj = source_of(db, cid, source)
    src = f"{kind}:{obj.id}"
    if kind == "exam":
        compute_exam(db, cid, obj, section_ids)
    else:
        compute_rule(db, cid, obj, section_ids)
    qy = db.query(ExamResult).filter(ExamResult.company_id == cid, ExamResult.source == src)
    if section_ids:
        qy = qy.filter(ExamResult.section_id.in_(section_ids))
    rows = qy.all()
    if not rows:
        raise HTTPException(status_code=400, detail="There are no results to publish yet. Enter marks first.")
    incomplete = sum(1 for r in rows if r.status == "incomplete")
    stamp = now()
    newly = []
    for r in rows:
        if r.published_at is None:
            r.published_at, r.published_by = stamp, user.get("employee_id")
            newly.append(r.student_id)
    if kind == "exam":
        total = db.query(ExamResult.id).filter(ExamResult.company_id == cid, ExamResult.source == src).count()
        pub = db.query(ExamResult.id).filter(ExamResult.company_id == cid, ExamResult.source == src,
                                             ExamResult.published_at.isnot(None)).count()
        if pub == total:
            obj.status, obj.published_at = "published", stamp
    name = obj.name
    audit.event(db, "exams.results.published", "exams" if kind == "exam" else "exam_result_rules", obj.id,
                {"source": src, "sections": section_ids or "all", "results": len(rows), "newly": len(newly),
                 "incomplete": incomplete}, entity_uuid=str(obj.uuid), entity_label=name)
    sent = 0
    if notify and newly:
        from modules.notifications.service import notify_portal
        ids = students.portal_account_ids_for_students(db, cid, newly, parents=True, students=True)
        sent = notify_portal(db, cid, ids, kind="exams.results", title=f"{name} results are out",
                             body=f"The results of {name} have been published. Open Exams & results to see them.",
                             link="/dashboard/my/results")
    db.flush()
    return {"published": len(newly), "total": len(rows), "incomplete": incomplete, "notified": sent}


def unpublish(db: Session, user: dict, source: str, section_ids: list[int] | None) -> int:
    cid = user["company_id"]
    kind, obj = source_of(db, cid, source)
    src = f"{kind}:{obj.id}"
    qy = db.query(ExamResult).filter(ExamResult.company_id == cid, ExamResult.source == src,
                                     ExamResult.published_at.isnot(None))
    if section_ids:
        qy = qy.filter(ExamResult.section_id.in_(section_ids))
    n = 0
    for r in qy.all():
        r.published_at, r.published_by = None, None
        n += 1
    if kind == "exam" and obj.status == "published":
        obj.status, obj.published_at = "scheduled", None
    audit.event(db, "exams.results.unpublished", "exams" if kind == "exam" else "exam_result_rules", obj.id,
                {"source": src, "sections": section_ids or "all", "results": n}, entity_uuid=str(obj.uuid),
                entity_label=obj.name)
    db.flush()
    return n


def published_results(db: Session, company_id: int, student_id: int) -> list[ExamResult]:
    return (db.query(ExamResult).filter(ExamResult.company_id == company_id, ExamResult.student_id == student_id,
                                        ExamResult.published_at.isnot(None))
            .order_by(ExamResult.published_at.desc()).all())


# ── Co-scholastic grades, remarks, attendance ────────────────────────

def term_scope(term_id: int | None) -> str:
    return f"term:{term_id}" if term_id else "session"


def coscholastic_sheet(db: Session, user: dict, section_id: int, term_id: int | None) -> dict:
    cid = user["company_id"]
    sec = db.query(Section).options(joinedload(Section.program)).filter(Section.company_id == cid,
                                                                         Section.id == section_id).first()
    if sec is None:
        raise HTTPException(status_code=404, detail="Section not found")
    p = perms(db, user)
    ct = is_class_teacher(db, cid, user.get("employee_id"), section_id)
    if not (ct or MARKS_ALL in p or VIEW in p):
        raise HTTPException(status_code=403, detail="Only the class teacher can enter these.")
    areas = [a for a in db.query(CoScholasticArea).filter(CoScholasticArea.company_id == cid,
                                                         CoScholasticArea.active.is_(True))
             .order_by(CoScholasticArea.sort_order, CoScholasticArea.name).all()
             if not a.program_ids or sec.program_id in [int(x) for x in a.program_ids]]
    scope = term_scope(term_id)
    recs = {r.student_id: r for r in db.query(TermRecord).filter(TermRecord.company_id == cid,
                                                                 TermRecord.session_id == sec.session_id,
                                                                 TermRecord.scope == scope).all()}
    area_out = []
    for a in areas:
        sch = resolve_scheme(db, cid, None, a.grading_scheme_id) if a.grading_scheme_id else scheme_by_code(db, cid, "coscholastic_3pt")
        area_out.append({"id": a.id, "name": a.name, "group_name": a.group_name,
                         "grades": [b["label"] for b in bands_of(sch)] if sch else ["A", "B", "C"]})
    rows = []
    for s, e in students.section_students(db, cid, section_id):
        r = recs.get(s.id)
        rows.append({"student_id": s.id, "name": s.full_name, "roll_no": e.roll_no,
                     "grades": (r.grades if r else {}) or {}, "remarks": r.remarks if r else None,
                     "days_present": num(r.days_present) if r else None, "days_total": num(r.days_total) if r else None})
    return {"section": {"id": sec.id, "label": academics.section_label(sec)}, "term_id": term_id, "areas": area_out,
            "rows": rows, "editable": ct or MARKS_ALL in p}


def save_coscholastic(db: Session, user: dict, section_id: int, term_id: int | None, rows: list[dict]) -> int:
    cid = user["company_id"]
    sec = db.query(Section).filter(Section.company_id == cid, Section.id == section_id).first()
    if sec is None:
        raise HTTPException(status_code=404, detail="Section not found")
    if not (is_class_teacher(db, cid, user.get("employee_id"), section_id) or MARKS_ALL in perms(db, user)):
        raise HTTPException(status_code=403, detail="Only the class teacher can enter these.")
    if term_id and not db.query(Term.id).filter(Term.company_id == cid, Term.id == term_id).first():
        raise HTTPException(status_code=400, detail="Term not found.")
    roster = {s.id for s, _ in students.section_students(db, cid, section_id)}
    scope = term_scope(term_id)
    recs = {r.student_id: r for r in db.query(TermRecord).filter(TermRecord.company_id == cid,
                                                                 TermRecord.session_id == sec.session_id,
                                                                 TermRecord.scope == scope).all()}
    n = 0
    for row in rows:
        sid = row.get("student_id")
        if sid not in roster:
            continue
        r = recs.get(sid)
        if r is None:
            r = TermRecord(company_id=cid, student_id=sid, session_id=sec.session_id, term_id=term_id, scope=scope)
            db.add(r)
        r.section_id = section_id
        if "grades" in row and isinstance(row["grades"], dict):
            r.grades = {str(k): str(v)[:40] for k, v in row["grades"].items() if v}
        if "remarks" in row:
            r.remarks = (row["remarks"] or None) and str(row["remarks"])[:2000]
        for f in ("days_present", "days_total"):
            if f in row:
                v = D(row[f])
                if v is not None and v < 0:
                    raise HTTPException(status_code=400, detail="Days cannot be negative.")
                setattr(r, f, v)
        if r.days_total is not None and r.days_present is not None and D(r.days_present) > D(r.days_total):
            raise HTTPException(status_code=400, detail="Days present cannot be more than working days.")
        r.entered_by = user.get("employee_id")
        n += 1
    db.flush()
    return n


# ── Analysis ─────────────────────────────────────────────────────────

def analysis(db: Session, company_id: int, source: str, section_id: int | None = None,
             visible_sections: set[int] | None = None) -> dict:
    kind, obj = source_of(db, company_id, source)
    src = f"{kind}:{obj.id}"
    qy = (db.query(ExamResult, Student).join(Student, Student.id == ExamResult.student_id)
          .filter(ExamResult.company_id == company_id, ExamResult.source == src))
    if section_id:
        qy = qy.filter(ExamResult.section_id == section_id)
    if visible_sections is not None:
        qy = qy.filter(ExamResult.section_id.in_(visible_sections or {-1}))
    rows = qy.all()
    secs = {s.id: s for s in db.query(Section).options(joinedload(Section.program)).filter(
        Section.id.in_({r.section_id for r, _ in rows} or {-1})).all()}
    done = [r for r, _ in rows if r.status != "incomplete" and r.percentage is not None]
    pcts = [D(r.percentage) for r in done]
    passed = [r for r in done if r.status == "pass"]

    def avg(vals):
        vals = [D(v) for v in vals if v is not None]
        return num(sum(vals, Decimal(0)) / len(vals)) if vals else None

    by_sec = defaultdict(list)
    for r in done:
        by_sec[r.section_id].append(r)
    sections_out = [{"section_id": sid, "label": academics.section_label(secs.get(sid)), "students": len(rs),
                     "average": avg([r.percentage for r in rs]),
                     "pass_percent": num(Decimal(sum(1 for r in rs if r.status == "pass")) * 100 / len(rs)),
                     "highest": num(max(D(r.percentage) for r in rs))}
                    for sid, rs in sorted(by_sec.items(), key=lambda kv: academics.section_label(secs.get(kv[0])))]
    subj = defaultdict(list)
    for r in done:
        for s in r.subjects or []:
            if s.get("percent") is not None and not s.get("excluded"):
                subj[(s["subject_id"], s["subject"])].append(s)
    subjects_out = [{"subject_id": k[0], "subject": k[1], "average": avg([x["percent"] for x in v]),
                     "highest": num(max(D(x["percent"]) for x in v)), "lowest": num(min(D(x["percent"]) for x in v)),
                     "pass_percent": num(Decimal(sum(1 for x in v if x.get("passed"))) * 100 / len(v)),
                     "students": len(v)} for k, v in subj.items()]
    subjects_out.sort(key=lambda x: x["subject"])
    scheme_id = next((r.scheme_id for r in done if r.scheme_id), None)
    scheme = db.query(GradingScheme).filter(GradingScheme.id == scheme_id).first() if scheme_id else None
    order = [b["label"] for b in bands_of(scheme)]
    dist = defaultdict(int)
    for r in done:
        if r.grade:
            dist[r.grade] += 1
    labels = order + sorted(k for k in dist if k not in order)
    distribution = [{"grade": g, "count": dist.get(g, 0)} for g in labels if g in dist or g in order]
    bins = [0] * 10
    for p in pcts:
        bins[min(9, int(p // 10))] += 1
    histogram = [{"range": f"{i * 10}-{i * 10 + 9 if i < 9 else 100}", "count": c} for i, c in enumerate(bins)]
    studs = {r.id: s for r, s in rows}
    use_gpa = any(r.gpa is not None for r in done)
    toppers = sorted(done, key=lambda r: (-(D(r.gpa) if use_gpa and r.gpa is not None else D(r.percentage))))[:10]
    return {
        "source": src, "name": obj.name, "kind": kind,
        "overview": {"students": len(rows), "appeared": len(done), "passed": len(passed),
                     "pass_percent": num(Decimal(len(passed)) * 100 / len(done)) if done else None,
                     "average": avg(pcts), "highest": num(max(pcts)) if pcts else None,
                     "lowest": num(min(pcts)) if pcts else None,
                     "average_gpa": avg([r.gpa for r in done]) if use_gpa else None,
                     "published": sum(1 for r, _ in rows if r.published_at)},
        "sections": sections_out, "subjects": subjects_out, "distribution": distribution, "histogram": histogram,
        "toppers": [{**result_out(r, studs[r.id], academics.section_label(secs.get(r.section_id)))} for r in toppers],
    }


def student_progress(db: Session, company_id: int, student_id: int, published_only: bool = False) -> list[dict]:
    qy = (db.query(ExamResult, Exam).join(Exam, Exam.id == ExamResult.exam_id)
          .filter(ExamResult.company_id == company_id, ExamResult.student_id == student_id,
                  ExamResult.exam_id.isnot(None), Exam.archived_at.is_(None)))
    if published_only:
        qy = qy.filter(ExamResult.published_at.isnot(None))
    out = []
    for r, e in sorted(qy.all(), key=lambda x: (x[1].start_date or date.min, x[1].id)):
        out.append({"exam": e.name, "exam_uuid": str(e.uuid), "date": e.start_date, "percentage": num(r.percentage),
                    "grade": r.grade, "gpa": num(r.gpa), "rank": r.rank, "status": r.status,
                    "subjects": {s["subject"]: s.get("percent") for s in (r.subjects or []) if s.get("percent") is not None}})
    return out


# ── Summaries ────────────────────────────────────────────────────────

def staff_summary(db: Session, user: dict) -> dict:
    cid = user["company_id"]
    today = date.today()
    sess = academics.current_session(db, cid)
    if not sess:
        return {"upcoming": [], "pending_sheets": [], "pending_count": 0}
    exams = (db.query(Exam).options(joinedload(Exam.papers)).filter(
        Exam.company_id == cid, Exam.session_id == sess.id, Exam.archived_at.is_(None), Exam.status != "draft").all())
    upcoming = []
    for e in exams:
        if e.end_date and e.end_date < today:
            continue
        if e.start_date and e.start_date > today + timedelta(days=45):
            continue
        nxt = min((p.paper_date for p in e.papers if p.paper_date and p.paper_date >= today), default=None)
        upcoming.append({"uuid": str(e.uuid), "name": e.name, "start_date": e.start_date, "end_date": e.end_date,
                         "next_paper_on": nxt, "ongoing": bool(e.start_date and e.start_date <= today)})
    upcoming.sort(key=lambda x: x["start_date"] or date.max)
    pending = []
    if user.get("employee_id") and MARKS in perms(db, user):
        for e in exams:
            if e.status == "published" or (e.start_date and e.start_date > today):
                continue
            for sh in sheets_for(db, user, e, mine_only=True):
                if sh["status"] == "draft" and (not sh["paper_date"] or sh["paper_date"] <= today):
                    pending.append({**sh, "exam_uuid": str(e.uuid), "exam_name": e.name,
                                    "marks_due_on": e.marks_due_on})
    return {"upcoming": upcoming[:6], "pending_sheets": pending[:8], "pending_count": len(pending)}


def portal_summary(db: Session, company_id: int, student_id: int) -> dict:
    latest = next(iter(published_results(db, company_id, student_id)), None)
    sec_id = students.student_section_id(db, company_id, student_id)
    nxt = None
    if sec_id:
        sec = db.query(Section).filter(Section.id == sec_id).first()
        today = date.today()
        p = (db.query(ExamPaper).join(Exam, Exam.id == ExamPaper.exam_id)
             .options(joinedload(ExamPaper.subject), joinedload(ExamPaper.exam))
             .filter(ExamPaper.company_id == company_id, ExamPaper.program_id == sec.program_id,
                     Exam.session_id == sec.session_id, Exam.archived_at.is_(None), Exam.status != "draft",
                     ExamPaper.paper_date >= today)
             .order_by(ExamPaper.paper_date, ExamPaper.start_time).first())
        if p:
            nxt = {"exam": p.exam.name, "subject": p.subject.name if p.subject else "", "date": p.paper_date,
                   "start_time": p.start_time.strftime("%H:%M") if p.start_time else None}
    return {
        "latest": ({**result_out(latest), "name": source_label(db, latest)} if latest else None),
        "next_exam": nxt,
    }


def portal_schedule(db: Session, company_id: int, student_id: int) -> list[dict]:
    sec_id = students.student_section_id(db, company_id, student_id)
    if not sec_id:
        return []
    sec = db.query(Section).filter(Section.id == sec_id).first()
    today = date.today()
    exams = (db.query(Exam).options(joinedload(Exam.papers)).filter(
        Exam.company_id == company_id, Exam.session_id == sec.session_id, Exam.archived_at.is_(None),
        Exam.status != "draft").order_by(Exam.start_date).all())
    out = []
    for e in exams:
        if e.end_date and e.end_date < today - timedelta(days=3):
            continue
        papers = [p for p in e.papers if p.program_id == sec.program_id]
        if not papers:
            continue
        out.append({"uuid": str(e.uuid), "name": e.name, "start_date": e.start_date, "end_date": e.end_date,
                    "instructions": e.instructions,
                    "papers": [{"subject": p.subject.name if p.subject else "", "date": p.paper_date,
                                "start_time": p.start_time.strftime("%H:%M") if p.start_time else None,
                                "end_time": p.end_time.strftime("%H:%M") if p.end_time else None,
                                "max_marks": num(p.max_marks)}
                               for p in sorted(papers, key=lambda p: (p.paper_date or date.max, p.start_time or datetime.min.time()))]})
    return out


def visible_section_ids(db: Session, user: dict) -> set[int] | None:
    """None = every section (exams.view or exam cell); else the sections the person teaches."""
    p = perms(db, user)
    if VIEW in p or MARKS_ALL in p or MODERATE in p or PUBLISH in p:
        return None
    return academics.teacher_section_ids(db, user["company_id"], user.get("employee_id"))
