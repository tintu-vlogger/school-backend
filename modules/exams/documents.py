"""Printable exam documents: hall tickets, report cards and transcripts (HTML -> PDF with the
shared helpers in modules/documents/pdf.py)."""
from __future__ import annotations

import html
from datetime import date
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.orm import Session, joinedload

from modules.academics import service as academics
from modules.academics.models import AcademicSession, Section, Term
from modules.documents.pdf import pdf_html, render_pdf
from modules.exams import calc, service
from modules.exams.models import (CoScholasticArea, Exam, ExamPaper, ExamResult, ExamRoom, GradingScheme,
                                  ReportTemplate, ResultRule, TermRecord)
from modules.students import service as students
from modules.students.models import Enrollment, Student

E = lambda v: html.escape("" if v is None else str(v))  # noqa: E731

CSS = """
  .doc { page-break-after: always; }
  .doc:last-child { page-break-after: auto; }
  .hd { display: flex; align-items: center; gap: 12pt; border-bottom: 0.8pt solid #1f2430; padding-bottom: 8pt; margin-bottom: 10pt; }
  .hd img { height: 46pt; }
  .hd .org { font-size: 15pt; font-weight: 600; }
  .hd .sub { font-size: 9pt; color: #6b7080; }
  .title { text-align: center; font-size: 12.5pt; letter-spacing: 0.06em; text-transform: uppercase; margin: 4pt 0 10pt; }
  table.info td { border: none; padding: 1.5pt 0; font-size: 10pt; }
  table.info td.k { color: #6b7080; width: 28%; }
  table.marks th, table.marks td { font-size: 9.5pt; text-align: center; }
  table.marks td.l, table.marks th.l { text-align: left; }
  .fail { color: #b42318; }
  .sum { display: flex; flex-wrap: wrap; gap: 6pt 18pt; font-size: 10pt; margin: 6pt 0 10pt; }
  .sum b { font-weight: 600; }
  .sum span { margin-right: 16pt; display: inline-block; }
  .sig { display: flex; justify-content: space-between; margin-top: 34pt; }
  .sig div { width: 30%; border-top: 0.6pt solid #6b7080; text-align: center; font-size: 9pt; padding-top: 3pt; color: #444; }
  .key { font-size: 8.5pt; color: #6b7080; }
  .box { border: 0.6pt solid #c9ccd4; padding: 6pt 8pt; min-height: 30pt; font-size: 10pt; }
  h3 { font-size: 10.5pt; margin: 10pt 0 4pt; }
"""


def _num(v, places=2):
    if v is None:
        return ""
    d = Decimal(str(v)).quantize(Decimal(1).scaleb(-places))
    s = f"{d:f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _org_header(db: Session, company_id: int, show_logo: bool = True) -> str:
    company = service._company(db, company_id)
    prof = academics.profile(company)
    logo = ""
    if show_logo and company.logo_url and company.logo_url.startswith(("http://", "https://", "data:")):
        logo = f'<img src="{E(company.logo_url)}" alt="">'
    addr = ", ".join(x for x in (company.address, company.city, company.state, company.pincode) if x)
    sub = " · ".join(x for x in (prof.get("board"), f"Affiliation {prof['affiliation_no']}" if prof.get("affiliation_no") else "", addr) if x)
    return f'<div class="hd">{logo}<div><div class="org">{E(company.company_name)}</div><div class="sub">{E(sub)}</div></div></div>'


def _pdf(body: str, title: str) -> bytes:
    doc = pdf_html(body, header=None, footer=None, header_mm=0, footer_mm=0, title=title,
                   margins=(14, 14, 14, 14), font_size=10)
    # The body is sanitised (no <style> allowed), so the document styles go into the head.
    doc = doc.replace("</style></head>", CSS + "</style></head>", 1)
    return render_pdf(doc)


def _words(db: Session, company_id: int) -> dict:
    return academics.profile(service._company(db, company_id))["terms"]


# ── Hall tickets ─────────────────────────────────────────────────────

def hall_tickets(db: Session, company_id: int, exam: Exam, section_id: int | None = None,
                 student_ids: list[int] | None = None) -> bytes:
    words = _words(db, company_id)
    sections = service.exam_sections(db, company_id, exam, [section_id] if section_id else None)
    seating = db.query(ExamRoom).options(joinedload(ExamRoom.room)).filter(ExamRoom.exam_id == exam.id).all()
    blocks = []
    head = _org_header(db, company_id)
    for sec in sections:
        papers = sorted([p for p in exam.papers if p.program_id == sec.program_id],
                        key=lambda p: (p.paper_date or date.max, p.start_time.isoformat() if p.start_time else "99"))
        if not papers:
            continue
        for s, e in students.section_students(db, company_id, sec.id):
            if student_ids and s.id not in student_ids:
                continue
            room = _room_for(seating, sec.id, e.roll_no)
            rows = "".join(
                f"<tr><td>{E(p.paper_date.strftime('%d %b %Y') if p.paper_date else 'To be announced')}</td>"
                f"<td>{E(p.paper_date.strftime('%A') if p.paper_date else '')}</td>"
                f"<td>{E(p.start_time.strftime('%I:%M %p') if p.start_time else '')}"
                f"{' - ' + E(p.end_time.strftime('%I:%M %p')) if p.end_time else ''}</td>"
                f"<td class='l'>{E(p.subject.name if p.subject else '')}</td><td></td></tr>" for p in papers)
            blocks.append(f"""<div class="doc">{head}<div class="title">Admit card · {E(exam.name)}</div>
<table class="info plain"><tr><td class="k">{E(words['student'])}</td><td>{E(s.full_name)}</td>
<td class="k">Admission no.</td><td>{E(s.admission_no)}</td></tr>
<tr><td class="k">{E(words['program'])} / {E(words['section'])}</td><td>{E(academics.section_label(sec))}</td>
<td class="k">{E(words['roll'])}</td><td>{E(e.roll_no)}</td></tr>
<tr><td class="k">{E(words['session'])}</td><td>{E(exam.session.name if exam.session else '')}</td>
<td class="k">Room</td><td>{E(room)}</td></tr></table>
<table class="marks"><tr><th>Date</th><th>Day</th><th>Time</th><th class="l">{E(words['subject'])}</th><th>Invigilator's signature</th></tr>{rows}</table>
{f'<h3>Instructions</h3><div class="box">{E(exam.instructions).replace(chr(10), "<br>")}</div>' if exam.instructions else ''}
<div class="sig"><div>{E(words['student'])}'s signature</div><div>{E(words['teacher'])}</div><div>Principal / Controller of exams</div></div></div>""")
    if not blocks:
        raise HTTPException(status_code=400, detail="There is no one to print an admit card for. Add papers to the exam first.")
    return _pdf("".join(blocks), f"Admit cards - {exam.name}")


def _room_for(seating: list[ExamRoom], section_id: int, roll: str | None) -> str:
    def key(v):
        return (0, int(v)) if v and str(v).isdigit() else (1, str(v or ""))
    for r in seating:
        if r.section_id != section_id:
            continue
        if not r.roll_from and not r.roll_to:
            return r.room.name if r.room else ""
        if roll and (not r.roll_from or key(roll) >= key(r.roll_from)) and (not r.roll_to or key(roll) <= key(r.roll_to)):
            return r.room.name if r.room else ""
    return ""


# ── Report cards ─────────────────────────────────────────────────────

def template_columns(db: Session, company_id: int, tpl: ReportTemplate, session_id: int, program_id: int) -> list[dict]:
    cols = []
    for c in tpl.columns or []:
        src = c.get("source") or ""
        kind, _, ident = src.partition(":")
        if kind == "exam" and ident.isdigit():
            e = db.query(Exam).filter(Exam.company_id == company_id, Exam.id == int(ident)).first()
            if e and not e.archived_at:
                cols.append({"source": f"exam:{e.id}", "label": c.get("label") or e.name, "kind": "exam", "obj": e})
        elif kind == "rule" and ident.isdigit():
            r = db.query(ResultRule).filter(ResultRule.company_id == company_id, ResultRule.id == int(ident)).first()
            if r:
                cols.append({"source": f"rule:{r.id}", "label": c.get("label") or r.name, "kind": "rule", "obj": r})
    if cols:
        return cols
    # Default: every exam of the session this program writes, then every result rule.
    exams = (db.query(Exam).filter(Exam.company_id == company_id, Exam.session_id == session_id,
                                   Exam.archived_at.is_(None)).order_by(Exam.start_date, Exam.id).all())
    for e in exams:
        if e.program_ids and program_id not in [int(x) for x in e.program_ids]:
            continue
        cols.append({"source": f"exam:{e.id}", "label": e.name, "kind": "exam", "obj": e})
    for r in db.query(ResultRule).filter(ResultRule.company_id == company_id, ResultRule.session_id == session_id,
                                         ResultRule.active.is_(True)).order_by(ResultRule.id).all():
        if r.program_ids and program_id not in [int(x) for x in r.program_ids]:
            continue
        cols.append({"source": f"rule:{r.id}", "label": r.name, "kind": "rule", "obj": r})
    return cols


def default_template(db: Session, company_id: int, kind: str = "report_card") -> ReportTemplate | None:
    q = db.query(ReportTemplate).filter(ReportTemplate.company_id == company_id, ReportTemplate.kind == kind,
                                        ReportTemplate.active.is_(True))
    return q.filter(ReportTemplate.is_default.is_(True)).first() or q.order_by(ReportTemplate.id).first()


def report_card_data(db: Session, company_id: int, tpl: ReportTemplate, student: Student, enrollment: Enrollment,
                     published_only: bool) -> dict:
    sec = enrollment.section
    cols = template_columns(db, company_id, tpl, enrollment.session_id, enrollment.program_id)
    qy = db.query(ExamResult).filter(ExamResult.company_id == company_id, ExamResult.student_id == student.id,
                                     ExamResult.source.in_([c["source"] for c in cols] or ["-"]))
    if published_only:
        qy = qy.filter(ExamResult.published_at.isnot(None))
    results = {r.source: r for r in qy.all()}
    cols = [c for c in cols if c["source"] in results]
    subjects: list[tuple[int, str]] = []
    seen = set()
    for c in cols:
        for s in results[c["source"]].subjects or []:
            if s["subject_id"] not in seen:
                seen.add(s["subject_id"])
                subjects.append((s["subject_id"], s["subject"]))
    cells = {}
    for c in cols:
        for s in results[c["source"]].subjects or []:
            cells[(c["source"], s["subject_id"])] = s
    summary = None
    if tpl.summary_source and tpl.summary_source in results:
        summary = results[tpl.summary_source]
    elif cols:
        summary = results[cols[-1]["source"]]
    records = (db.query(TermRecord).filter(TermRecord.company_id == company_id, TermRecord.student_id == student.id,
                                           TermRecord.session_id == enrollment.session_id).all())
    terms = {t.id: t for t in db.query(Term).filter(Term.session_id == enrollment.session_id).all()}
    records.sort(key=lambda r: (terms[r.term_id].sort_order if r.term_id in terms else 99))
    areas = [a for a in db.query(CoScholasticArea).filter(CoScholasticArea.company_id == company_id,
                                                         CoScholasticArea.active.is_(True))
             .order_by(CoScholasticArea.sort_order).all()
             if not a.program_ids or enrollment.program_id in [int(x) for x in a.program_ids]]
    scheme = db.query(GradingScheme).filter(GradingScheme.id == summary.scheme_id).first() if summary and summary.scheme_id else None
    att = summary.attendance_percent if summary else None
    present = total = None
    if records:
        p_sum = sum((Decimal(str(r.days_present)) for r in records if r.days_present is not None), Decimal(0))
        t_sum = sum((Decimal(str(r.days_total)) for r in records if r.days_total is not None), Decimal(0))
        if t_sum:
            present, total = p_sum, t_sum
            if att is None:
                att = p_sum * 100 / t_sum
    if att is None:
        sess = enrollment.session
        att = service.attendance_percent(db, company_id, student.id, sess.start_date if sess else None,
                                         sess.end_date if sess else None)
    remarks = next((r.remarks for r in reversed(records) if r.remarks), None)
    return {"columns": cols, "results": results, "subjects": subjects, "cells": cells, "summary": summary,
            "records": records, "terms": terms, "areas": areas, "scheme": scheme, "attendance": att,
            "present": present, "total": total, "remarks": remarks, "section": sec}


def _cell(s: dict | None, tpl: ReportTemplate) -> str:
    if not s:
        return "<td></td>"
    if s.get("excluded"):
        return f"<td>{E(s.get('mark_code') or 'EX')}</td>"
    if s.get("missing"):
        return "<td>-</td>"
    parts = []
    cls = "" if s.get("passed") is not False else ' class="fail"'
    if tpl.show_marks and s.get("obtained") is not None and s.get("counts"):
        parts.append("AB" if s.get("absent") and s.get("mark_code") == "AB" else _num(s["obtained"]))
    if tpl.show_grade and s.get("grade"):
        parts.append(E(s["grade"]))
    return f"<td{cls}>{' · '.join(parts) or '-'}</td>"


def report_card_html(db: Session, company_id: int, tpl: ReportTemplate, student: Student, enrollment: Enrollment,
                     published_only: bool, head: str, words: dict) -> str | None:
    d = report_card_data(db, company_id, tpl, student, enrollment, published_only)
    if not d["columns"]:
        return None
    sess = enrollment.session
    sec = d["section"]
    title = tpl.title or "Report card"
    th = "".join(f"<th>{E(c['label'])}<div class='key'>{_max_label(d['results'][c['source']], tpl)}</div></th>"
                 for c in d["columns"])
    body_rows = "".join(
        f"<tr><td class='l'>{E(name)}</td>" + "".join(_cell(d["cells"].get((c["source"], sid)), tpl) for c in d["columns"]) + "</tr>"
        for sid, name in d["subjects"])
    s = d["summary"]
    summary = ""
    if s is not None:
        bits = []
        if tpl.show_marks and s.total_max:
            bits.append(f"Total <b>{_num(s.total_obtained)} / {_num(s.total_max)}</b>")
        if s.percentage is not None:
            bits.append(f"Percentage <b>{_num(s.percentage)}%</b>")
        if s.gpa is not None:
            bits.append(f"{'SGPA' if s.term_id else 'GPA'} <b>{_num(s.gpa)}</b>")
        if s.cgpa is not None:
            bits.append(f"CGPA <b>{_num(s.cgpa)}</b>")
        if tpl.show_grade and s.grade:
            bits.append(f"Grade <b>{E(s.grade)}</b>")
        if tpl.show_rank and s.rank:
            bits.append(f"Rank <b>{s.rank}</b>")
        bits.append(f"Result <b>{E(STATUS_LABEL.get(s.status, s.status))}</b>")
        summary = f"<div class='sum'>{''.join(f'<span>{b}</span>' for b in bits)}</div>"
    cos = ""
    if tpl.show_coscholastic and d["areas"] and any(r.grades for r in d["records"]):
        recs = [r for r in d["records"] if r.grades]
        head_terms = "".join(f"<th>{E(d['terms'][r.term_id].name if r.term_id in d['terms'] else words['session'])}</th>" for r in recs)
        rows = "".join(f"<tr><td class='l'>{E(a.name)}</td>" + "".join(
            f"<td>{E((r.grades or {}).get(str(a.id), ''))}</td>" for r in recs) + "</tr>" for a in d["areas"])
        cos = f"<h3>Co-scholastic areas</h3><table class='marks'><tr><th class='l'>Area</th>{head_terms}</tr>{rows}</table>"
    att = ""
    if tpl.show_attendance and d["attendance"] is not None:
        days = f" ({_num(d['present'], 1)} of {_num(d['total'], 1)} days)" if d["total"] else ""
        att = f"<div class='sum'><span>Attendance <b>{_num(d['attendance'])}%</b>{days}</span></div>"
    rem = f"<h3>Remarks</h3><div class='box'>{E(d['remarks'])}</div>" if tpl.show_remarks and d["remarks"] else ""
    key = ""
    if tpl.show_grading_key and d["scheme"] is not None and d["scheme"].bands:
        bands = sorted(d["scheme"].bands, key=lambda b: -b.min_percent)
        key = "<p class='key'>Grading: " + ", ".join(
            f"{E(b.label)} {_num(b.min_percent)}%+" + (f" ({_num(b.grade_point)})" if b.grade_point is not None else "")
            for b in bands) + "</p>"
    sigs = "".join(f"<div>{E(x)}</div>" for x in (tpl.signatures or []))
    foot = f"<p class='key'>{E(tpl.footer_note)}</p>" if tpl.footer_note else ""
    return f"""<div class="doc">{head}<div class="title">{E(title)} · {E(sess.name if sess else '')}</div>
<table class="info plain"><tr><td class="k">{E(words['student'])}</td><td>{E(student.full_name)}</td>
<td class="k">Admission no.</td><td>{E(student.admission_no)}</td></tr>
<tr><td class="k">{E(words['program'])} / {E(words['section'])}</td><td>{E(academics.section_label(sec))}</td>
<td class="k">{E(words['roll'])}</td><td>{E(enrollment.roll_no)}</td></tr>
<tr><td class="k">Date of birth</td><td>{E(student.date_of_birth.strftime('%d %b %Y') if student.date_of_birth else '')}</td>
<td class="k">{E(words['teacher'])}</td><td>{E(_class_teacher(sec))}</td></tr></table>
<table class="marks"><tr><th class="l">{E(words['subject'])}</th>{th}</tr>{body_rows}</table>
{summary}{att}{cos}{rem}{key}
{f'<div class="sig">{sigs}</div>' if sigs else ''}{foot}</div>"""


STATUS_LABEL = {"pass": "Pass", "fail": "Needs improvement", "compartment": "Supplementary",
                "not_eligible": "Not eligible (attendance)", "incomplete": "Incomplete"}


def _max_label(r: ExamResult, tpl: ReportTemplate) -> str:
    subs = [s for s in (r.subjects or []) if s.get("counts") and s.get("max")]
    maxes = {s["max"] for s in subs}
    if tpl.show_marks and len(maxes) == 1:
        return f"out of {_num(next(iter(maxes)))}"
    return ""


def _class_teacher(sec: Section | None) -> str:
    if sec is None or sec.class_teacher is None:
        return ""
    return f"{sec.class_teacher.first_name} {sec.class_teacher.last_name}".strip()


def report_cards(db: Session, company_id: int, tpl: ReportTemplate, *, section_id: int | None = None,
                 student_ids: list[int] | None = None, published_only: bool = False) -> bytes:
    words = _words(db, company_id)
    head = _org_header(db, company_id, tpl.show_logo)
    pairs: list[tuple[Student, Enrollment]] = []
    if section_id:
        pairs = students.section_students(db, company_id, section_id)
        if student_ids:
            pairs = [p for p in pairs if p[0].id in student_ids]
    elif student_ids:
        for sid in student_ids:
            s = db.query(Student).filter(Student.company_id == company_id, Student.id == sid).first()
            e = students.current_enrollment(db, company_id, sid) if s else None
            if s and e:
                pairs.append((s, e))
    blocks = []
    for s, e in pairs:
        if e.section is None:
            e = students.current_enrollment(db, company_id, s.id)
        if e is None:
            continue
        h = report_card_html(db, company_id, tpl, s, e, published_only, head, words)
        if h:
            blocks.append(h)
    if not blocks:
        raise HTTPException(status_code=400, detail="There are no results to print yet." if not published_only
                            else "No results have been published yet.")
    return _pdf("".join(blocks), tpl.title or "Report cards")


# ── Transcript / grade sheet ─────────────────────────────────────────

def transcript_data(db: Session, company_id: int, student: Student, published_only: bool) -> dict:
    qy = (db.query(ExamResult, ResultRule).join(ResultRule, ResultRule.id == ExamResult.rule_id)
          .filter(ExamResult.company_id == company_id, ExamResult.student_id == student.id))
    if published_only:
        qy = qy.filter(ExamResult.published_at.isnot(None))
    rows = qy.all()
    terms = {t.id: t for t in db.query(Term).filter(Term.id.in_({r.term_id for r, _ in rows if r.term_id} or {-1})).all()}
    sessions = {s.id: s for s in db.query(AcademicSession).filter(
        AcademicSession.id.in_({r.session_id for r, _ in rows if r.session_id} or {-1})).all()}

    def key(x):
        r = x[0]
        sess = sessions.get(r.session_id)
        t = terms.get(r.term_id)
        return (sess.start_date if sess else date.min, t.sort_order if t else 99, r.id)
    out_terms = []
    for r, rule in sorted(rows, key=key):
        t = terms.get(r.term_id)
        sess = sessions.get(r.session_id)
        out_terms.append({
            "label": f"{t.name if t else rule.name} · {sess.name if sess else ''}", "rule": rule.name,
            "sgpa": service.num(r.gpa), "cgpa": service.num(r.cgpa), "percentage": service.num(r.percentage),
            "credits_attempted": service.num(r.credits_attempted), "credits_earned": service.num(r.credits_earned),
            "status": r.status, "grade": r.grade, "published": r.published_at is not None,
            "subjects": [{"code": s.get("code"), "subject": s["subject"], "credits": s.get("credits"),
                          "grade": s.get("grade"), "grade_point": s.get("grade_point"), "passed": s.get("passed"),
                          "percent": s.get("percent")} for s in (r.subjects or []) if not s.get("excluded")],
        })
    with_gpa = [(t["sgpa"], t["credits_attempted"]) for t in out_terms if t["sgpa"] is not None]
    cg = calc.cgpa(with_gpa) if with_gpa else None
    scheme_id = next((r.scheme_id for r, _ in rows if r.scheme_id), None)
    scheme = db.query(GradingScheme).filter(GradingScheme.id == scheme_id).first() if scheme_id else None
    pct = service.cgpa_to_percent(scheme, cg)
    earned = sum((Decimal(str(t["credits_earned"])) for t in out_terms if t["credits_earned"] is not None), Decimal(0))
    return {"student": students.student_brief(student, students.current_enrollment(db, company_id, student.id)),
            "terms": out_terms, "cgpa": service.num(cg), "percent_equivalent": service.num(pct),
            "credits_earned": service.num(earned) if out_terms else None,
            "scheme": scheme.name if scheme else None, "scale": service.num(scheme.scale_max) if scheme else None}


def transcript_pdf(db: Session, company_id: int, student: Student, published_only: bool) -> bytes:
    d = transcript_data(db, company_id, student, published_only)
    if not d["terms"]:
        raise HTTPException(status_code=400, detail="There are no term results to put on a transcript yet.")
    words = _words(db, company_id)
    st = d["student"]
    blocks = []
    for t in d["terms"]:
        rows = "".join(f"<tr><td>{E(s['code'])}</td><td class='l'>{E(s['subject'])}</td><td>{_num(s['credits'])}</td>"
                       f"<td{'' if s['passed'] is not False else ' class=fail'}>{E(s['grade'])}</td><td>{_num(s['grade_point'])}</td></tr>"
                       for s in t["subjects"])
        cg = f"<span>CGPA <b>{_num(t['cgpa'])}</b></span>" if t["cgpa"] is not None else ""
        blocks.append(f"<h3>{E(t['label'])}</h3><table class='marks'><tr><th>Code</th><th class='l'>{E(words['subject'])}</th>"
                      f"<th>Credits</th><th>Grade</th><th>Grade point</th></tr>{rows}</table>"
                      f"<div class='sum'><span>SGPA <b>{_num(t['sgpa'])}</b></span><span>Credits earned <b>{_num(t['credits_earned'])}"
                      f" / {_num(t['credits_attempted'])}</b></span>{cg}"
                      f"<span>Result <b>{E(STATUS_LABEL.get(t['status'], t['status']))}</b></span></div>")
    scale = f" on {_num(d['scale'])}" if d["scale"] else ""
    eq = (f"<span>Percentage equivalent <b>{_num(d['percent_equivalent'])}%</b></span>"
          if d["percent_equivalent"] is not None else "")
    final = (f"<div class='sum'><span>CGPA <b>{_num(d['cgpa'])}</b>{scale}</span>"
             f"<span>Total credits earned <b>{_num(d['credits_earned'])}</b></span>{eq}</div>")
    body = f"""<div class="doc">{_org_header(db, company_id)}<div class="title">Transcript of academic record</div>
<table class="info plain"><tr><td class="k">{E(words['student'])}</td><td>{E(st['name'])}</td><td class="k">{E(words['roll'])}</td><td>{E(st['admission_no'])}</td></tr>
<tr><td class="k">{E(words['program'])}</td><td>{E(st.get('program_name'))}</td><td class="k">Grading</td><td>{E(d['scheme'])}</td></tr></table>
{''.join(blocks)}{final}<div class="sig"><div>Prepared by</div><div>Checked by</div><div>Controller of examinations</div></div></div>"""
    return _pdf(body, f"Transcript - {st['name']}")
