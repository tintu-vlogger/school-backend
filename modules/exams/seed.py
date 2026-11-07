"""Exam defaults every institution gets (grading schemes, exam types, co-scholastic areas, a
report card template) and demo data for the demo school."""
from __future__ import annotations

import logging
import random
from datetime import date, time, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

log = logging.getLogger("hrms")

# code: (name, kind, scale, pass %, description, [(label, min %, grade point, description, is_pass)], formula)
SCHEMES = {
    "percentage_grades": ("CBSE 9-point grades", "percentage_bands", None, 33,
                          "Percentage with grade bands A1 to E, as used by CBSE (pass at 33%).",
                          [("A1", 91, 10, "Outstanding", True), ("A2", 81, 9, "Excellent", True),
                           ("B1", 71, 8, "Very good", True), ("B2", 61, 7, "Good", True),
                           ("C1", 51, 6, "Above average", True), ("C2", 41, 5, "Average", True),
                           ("D", 33, 4, "Pass", True), ("E", 0, 0, "Needs improvement", False)], None),
    "icse_grades": ("ICSE / state board divisions", "percentage_bands", None, 35,
                    "Percentage with divisions, as used by ICSE and many state boards (pass at 35%).",
                    [("Distinction", 75, None, "Distinction", True), ("First division", 60, None, "First division", True),
                     ("Second division", 45, None, "Second division", True), ("Pass", 35, None, "Pass", True),
                     ("Fail", 0, None, "Fail", False)], None),
    "percentage": ("Marks and percentage", "marks", None, 40,
                   "Marks and percentage only, no grades (pass at 40%).", [], None),
    "gpa_10": ("10-point CGPA (university)", "gpa", 10, 40,
               "Letter grades O to F with grade points on 10, credit-weighted SGPA and CGPA (UGC style).",
               [("O", 90, 10, "Outstanding", True), ("A+", 80, 9, "Excellent", True), ("A", 70, 8, "Very good", True),
                ("B+", 60, 7, "Good", True), ("B", 50, 6, "Above average", True), ("C", 45, 5, "Average", True),
                ("P", 40, 4, "Pass", True), ("F", 0, 0, "Fail", False)], "cgpa * 9.5"),
    "gpa_4": ("4-point GPA", "gpa", 4, 60,
              "Letter grades A to F with grade points on 4, credit-weighted GPA.",
              [("A", 93, 4, "Excellent", True), ("A-", 90, 3.7, None, True), ("B+", 87, 3.3, None, True),
               ("B", 83, 3, "Good", True), ("B-", 80, 2.7, None, True), ("C+", 77, 2.3, None, True),
               ("C", 73, 2, "Satisfactory", True), ("C-", 70, 1.7, None, True), ("D+", 67, 1.3, None, True),
               ("D", 60, 1, "Pass", True), ("F", 0, 0, "Fail", False)], "cgpa * 25"),
    "gpa_5": ("5-point GPA", "gpa", 5, 40,
              "Grades A to F with grade points on 5, credit-weighted GPA.",
              [("A", 70, 5, "Excellent", True), ("B", 60, 4, "Very good", True), ("C", 50, 3, "Good", True),
               ("D", 45, 2, "Fair", True), ("E", 40, 1, "Pass", True), ("F", 0, 0, "Fail", False)], "cgpa * 20"),
    "pass_fail": ("Pass / fail", "pass_fail", None, 40, "Only pass or fail (pass at 40%).",
                  [("Pass", 40, None, None, True), ("Fail", 0, None, None, False)], None),
    "descriptive": ("Developmental levels (preschool)", "descriptive", None, 0,
                    "Descriptive levels for early years: Beginning, Developing, Proficient, Advanced.",
                    [("Advanced", 90, 4, "Goes beyond what is expected", True),
                     ("Proficient", 70, 3, "Does it on their own", True),
                     ("Developing", 40, 2, "Does it with some help", True),
                     ("Beginning", 0, 1, "Just starting", True)], None),
    "competency": ("Competency based (training)", "descriptive", None, 50,
                   "Competent or not yet competent, as used in vocational training.",
                   [("Competent", 50, None, "Meets the standard", True),
                    ("Not yet competent", 0, None, "Needs more practice", False)], None),
    "coscholastic_3pt": ("Co-scholastic 3-point", "descriptive", None, 0,
                         "A, B, C grades for co-scholastic areas and personal qualities.",
                         [("A", 67, 3, "Outstanding", True), ("B", 34, 2, "Very good", True),
                          ("C", 0, 1, "Fair", True)], None),
}

EXAM_TYPES = [("Unit test", "UT", "unit_test", 25), ("Mid-term", "MT", "mid_term", 100),
              ("Final exam", "FE", "final", 100), ("Internal assessment", "IA", "internal", 20),
              ("Practical", "PR", "practical", 30), ("Project", "PJ", "project", 20), ("Viva", "VV", "viva", 10),
              ("Continuous assessment", "CA", "continuous", 10)]

AREAS = [("Work education", "Co-scholastic"), ("Art education", "Co-scholastic"),
         ("Health and physical education", "Co-scholastic"), ("Discipline", "Personal qualities"),
         ("Punctuality", "Personal qualities"), ("Team work", "Personal qualities")]


def ensure_schemes(db: Session, company_id: int) -> None:
    from modules.exams.models import GradeBand, GradingScheme
    have = {c for (c,) in db.query(GradingScheme.code).filter(GradingScheme.company_id == company_id).all()}
    for code, (name, kind, scale, pass_pct, desc, bands, formula) in SCHEMES.items():
        if code in have:
            continue
        s = GradingScheme(company_id=company_id, code=code, name=name, kind=kind, scale_max=scale,
                          pass_percent=Decimal(str(pass_pct)), description=desc, percent_formula=formula, is_system=True)
        db.add(s)
        db.flush()
        for label, lo, gp, bdesc, ok in bands:
            db.add(GradeBand(company_id=company_id, scheme_id=s.id, label=label, min_percent=Decimal(str(lo)),
                             grade_point=Decimal(str(gp)) if gp is not None else None, description=bdesc, is_pass=ok))
    db.flush()


def setup_company(db: Session, company_id: int) -> None:
    from modules.exams.models import CoScholasticArea, ExamType, ReportTemplate
    try:
        with db.begin_nested():
            ensure_schemes(db, company_id)
            if not db.query(ExamType.id).filter(ExamType.company_id == company_id).first():
                for i, (name, code, cat, mx) in enumerate(EXAM_TYPES):
                    db.add(ExamType(company_id=company_id, name=name, code=code, category=cat,
                                    default_max_marks=Decimal(mx), sort_order=i + 1))
            if not db.query(CoScholasticArea.id).filter(CoScholasticArea.company_id == company_id).first():
                for i, (name, group) in enumerate(AREAS):
                    db.add(CoScholasticArea(company_id=company_id, name=name, group_name=group, sort_order=i + 1))
            if not db.query(ReportTemplate.id).filter(ReportTemplate.company_id == company_id).first():
                db.add(ReportTemplate(company_id=company_id, name="Standard report card", title="Report card",
                                      is_default=True))
                db.add(ReportTemplate(company_id=company_id, name="Grade sheet / transcript", kind="transcript",
                                      title="Grade sheet", show_rank=False, show_coscholastic=False,
                                      signatures=["Controller of examinations"], is_default=True))
            db.flush()
    except Exception as exc:  # set-up must never block sign-up or start-up
        log.warning("Exam defaults for company %s failed: %s", company_id, exc)


# ── Demo ─────────────────────────────────────────────────────────────

def _days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() != 6:  # no exams on Sundays
            out.append(d)
        d += timedelta(days=1)
    return out


def seed_demo(db: Session, company_id: int) -> None:
    from modules.academics import service as academics
    from modules.academics.models import Program, Section, Term
    from modules.exams import service
    from modules.exams.models import (CoScholasticArea, Exam, ExamPaper, ExamType, GradingScheme, Mark, MarkSheet,
                                      ReportTemplate, ResultRule, TermRecord)
    from modules.students import service as students
    if db.query(Exam.id).filter(Exam.company_id == company_id).first():
        return
    setup_company(db, company_id)
    sess = academics.current_session(db, company_id)
    if sess is None:
        return
    terms = db.query(Term).filter(Term.session_id == sess.id).order_by(Term.sort_order).all()
    if not terms:
        return
    t1 = terms[0]
    t2 = terms[1] if len(terms) > 1 else terms[0]
    types = {t.category: t for t in db.query(ExamType).filter(ExamType.company_id == company_id).all()}
    rng = random.Random(42)

    # Early years classes use descriptive levels (a program override of the institution default).
    desc = db.query(GradingScheme).filter(GradingScheme.company_id == company_id, GradingScheme.code == "descriptive").first()
    if desc:
        for p in db.query(Program).filter(Program.company_id == company_id, Program.level == "Pre-primary").all():
            if not p.grading_scheme_id:
                p.grading_scheme_id = desc.id

    ut_start = max(t1.start_date, t1.end_date - timedelta(days=30))
    ut = Exam(company_id=company_id, session_id=sess.id, term_id=t1.id, exam_type_id=types["unit_test"].id,
              name="Unit test 1", code="UT1", start_date=ut_start, status="scheduled",
              instructions="Bring your admit card and school ID.\nReach the room 15 minutes before the start.")
    mt_start = max(t1.start_date, t1.end_date - timedelta(days=14))
    mt = Exam(company_id=company_id, session_id=sess.id, term_id=t1.id, exam_type_id=types["mid_term"].id,
              name="Mid-term examination", code="MT1", start_date=mt_start, status="scheduled",
              instructions="Bring your admit card, school ID and your own stationery.\n"
                           "Phones and smart watches are not allowed in the exam hall.")
    today = date.today()
    ut2_start = today + timedelta(days=10)
    ut2 = Exam(company_id=company_id, session_id=sess.id, term_id=t2.id, exam_type_id=types["unit_test"].id,
               name="Unit test 2", code="UT2", start_date=ut2_start, status="scheduled",
               marks_due_on=ut2_start + timedelta(days=14), instructions="Bring your admit card and school ID.")
    db.add_all([ut, mt, ut2])
    db.flush()
    for exam, mx, hours in ((ut, 25, 1), (mt, 100, 3), (ut2, 25, 1)):
        service.generate_papers(db, company_id, exam, max_marks=mx)
        db.flush()
        db.refresh(exam)
        by_prog: dict[int, list[ExamPaper]] = {}
        for p in exam.papers:
            by_prog.setdefault(p.program_id, []).append(p)
        last = exam.start_date
        for papers in by_prog.values():
            papers.sort(key=lambda p: p.sort_order)
            days = _days(exam.start_date, len(papers))
            for p, d in zip(papers, days):
                p.paper_date = d
                p.start_time = time(9, 30)
                p.end_time = time(9 + hours, 30)
                if p.entry_mode == "marks":
                    p.pass_marks = Decimal(mx) * Decimal("0.33")
                    if exam is mt and p.subject and p.subject.kind == "theory_practical":
                        p.components = [{"key": "theory", "label": "Theory", "max_marks": 80, "pass_marks": 26},
                                        {"key": "internal", "label": "Practical / internal", "max_marks": 20}]
                last = max(last, d)
        exam.end_date = last
        exam.marks_due_on = exam.marks_due_on or last + timedelta(days=7)
    db.flush()

    divya_id = None
    from modules.employees.models import Employee
    divya = db.query(Employee).filter(Employee.company_id == company_id, Employee.email == "divya.menon@example.com").first()
    divya_id = divya.id if divya else None

    sections = db.query(Section).filter(Section.company_id == company_id, Section.session_id == sess.id).all()
    ability: dict[int, float] = {}
    for exam in (ut, mt):
        papers_by_prog: dict[int, list[ExamPaper]] = {}
        for p in exam.papers:
            papers_by_prog.setdefault(p.program_id, []).append(p)
        for sec in sections:
            for s, _e in students.section_students(db, company_id, sec.id):
                base = ability.setdefault(s.id, min(97.0, max(28.0, rng.gauss(70, 13))))
                for p in papers_by_prog.get(sec.program_id, []):
                    m = Mark(company_id=company_id, exam_id=exam.id, paper_id=p.id, student_id=s.id, section_id=sec.id)
                    if p.entry_mode == "grade":
                        m.grade = "A" if base > 75 else "B" if base > 55 else "C"
                    roll = rng.random()
                    if p.entry_mode == "marks":
                        if roll < 0.01:
                            m.code = "AB"
                        else:
                            mx = float(p.max_marks)
                            pct = min(100.0, max(5.0, rng.gauss(base + rng.uniform(-6, 6), 8)))
                            if p.components:
                                th = round(min(80, pct * 0.8 * rng.uniform(0.95, 1.03)))
                                it = round(min(20, max(8, pct * 0.2 * rng.uniform(1.0, 1.15))))
                                m.components = {"theory": th, "internal": it}
                                m.marks = Decimal(th + it)
                            else:
                                m.marks = Decimal(str(round(pct * mx / 100 * 2) / 2))
                    db.add(m)
        db.flush()
        for sec in sections:
            for p in papers_by_prog.get(sec.program_id, []):
                status = "locked" if exam is ut else "submitted"
                if exam is mt and divya_id and p.subject and p.subject.code == "CS":
                    alloc_teacher = _teacher(db, company_id, sec.id, p.subject_id)
                    if alloc_teacher == divya_id:
                        status = "draft"
                db.add(MarkSheet(company_id=company_id, exam_id=exam.id, paper_id=p.id, section_id=sec.id,
                                 status=status, entered_by=_teacher(db, company_id, sec.id, p.subject_id)))
        db.flush()

    # Co-scholastic grades, remarks and attendance for Term 1.
    areas = db.query(CoScholasticArea).filter(CoScholasticArea.company_id == company_id).all()
    remarks_good = ["Consistently works hard and takes part eagerly in class.", "A cheerful learner who helps others.",
                    "Shows real curiosity; keep reading beyond the textbook.", "Very good progress this term."]
    remarks_mid = ["Can do better with regular revision at home.", "Needs to pay more attention to written work.",
                   "Improving steadily; practise mathematics daily.", "Participates well; work on neatness."]
    for sec in sections:
        for s, _e in students.section_students(db, company_id, sec.id):
            base = ability.get(s.id, 65)
            total = 96
            present = max(60, min(total, round(rng.gauss(88 if base > 60 else 80, 6))))
            db.add(TermRecord(company_id=company_id, student_id=s.id, session_id=sess.id, term_id=t1.id,
                              scope=f"term:{t1.id}", section_id=sec.id,
                              grades={str(a.id): ("A" if base + rng.uniform(-10, 10) > 70 else "B" if base > 45 else "C")
                                      for a in areas},
                              remarks=rng.choice(remarks_good if base > 72 else remarks_mid),
                              days_present=Decimal(present), days_total=Decimal(total),
                              entered_by=sec.class_teacher_id))
    db.flush()

    rule = ResultRule(company_id=company_id, session_id=sess.id, term_id=t1.id, name=f"{t1.name} result",
                      groups=[{"key": "ut", "label": "Unit test", "exam_ids": [ut.id], "combine": "average", "weight": 20},
                              {"key": "mt", "label": "Mid-term", "exam_ids": [mt.id], "combine": "average", "weight": 80}],
                      ranking=True, min_attendance_percent=Decimal(75), grace_per_subject=Decimal(3),
                      grace_max_subjects=2)
    db.add(rule)
    db.flush()
    service.compute_exam(db, company_id, ut)
    service.compute_exam(db, company_id, mt)
    service.compute_rule(db, company_id, rule)
    db.flush()
    # Unit test 1 results are published.
    from modules.exams.models import ExamResult
    stamp = service.now()
    for r in db.query(ExamResult).filter(ExamResult.company_id == company_id, ExamResult.source == f"exam:{ut.id}").all():
        r.published_at = stamp
    ut.status, ut.published_at = "published", stamp
    tpl = db.query(ReportTemplate).filter(ReportTemplate.company_id == company_id, ReportTemplate.kind == "report_card",
                                          ReportTemplate.is_default.is_(True)).first()
    if tpl is not None:
        tpl.is_default = False
    db.add(ReportTemplate(company_id=company_id, name=f"{t1.name} report card", title=f"{t1.name} report card",
                          session_id=sess.id,
                          columns=[{"source": f"exam:{ut.id}", "label": "Unit test 1"},
                                   {"source": f"exam:{mt.id}", "label": "Mid-term"},
                                   {"source": f"rule:{rule.id}", "label": f"{t1.name} (weighted)"}],
                          summary_source=f"rule:{rule.id}", show_rank=False, is_default=True,
                          footer_note="Weighted: unit test 20%, mid-term 80%."))
    db.flush()


def _teacher(db: Session, company_id: int, section_id: int, subject_id: int) -> int | None:
    from modules.academics.models import SectionSubject
    a = db.query(SectionSubject.teacher_id).filter(SectionSubject.company_id == company_id,
                                                   SectionSubject.section_id == section_id,
                                                   SectionSubject.subject_id == subject_id).first()
    return a[0] if a else None
