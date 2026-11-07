"""Teaching and learning defaults for every institution, and sample data for the demo:
homework for Grade 8 - A and Divya Menon's Computer Science sections (Aarav has submitted one,
another is pending), study materials, two built-in video classes (today and tomorrow), a
10-question quiz and the Computer Science syllabus for Grade 8 with lesson plans."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

log = logging.getLogger("hrms")


def setup_company(db: Session, company_id: int) -> None:
    try:
        from modules.lms.models import LmsSettings
        if not db.query(LmsSettings.id).filter(LmsSettings.company_id == company_id).first():
            db.add(LmsSettings(company_id=company_id, config={}))
            db.flush()
    except Exception as exc:  # defaults must never block sign-up or start-up
        log.warning("LMS defaults for company %s failed: %s", company_id, exc)


def seed_demo(db: Session, company_id: int) -> None:
    from modules.academics import service as academics
    from modules.academics.models import Program, Section, SectionSubject, Subject
    from modules.employees.models import Employee
    from modules.lms import live, service as svc
    from modules.lms.models import (Assignment, AssignmentSection, LessonPlan, LiveClass, Material, MaterialSection,
                                    BankQuestion, Quiz, QuizAttempt, QuizQuestion, QuizSection, Submission, SyllabusTopic,
                                    SyllabusUnit, TopicCoverage)
    from modules.lms import quiz as quizsvc
    from modules.students import service as students

    setup_company(db, company_id)
    if db.query(Assignment.id).filter(Assignment.company_id == company_id).first():
        return
    try:  # modules seed in name order; make sure the learners exist first
        from modules.students.seed import seed_demo as students_demo
        students_demo(db, company_id)
    except Exception as exc:
        log.warning("LMS demo could not seed students first: %s", exc)
    sess = academics.current_session(db, company_id)
    divya = db.query(Employee).filter(Employee.company_id == company_id, Employee.email == "divya.menon@example.com").first()
    cs = db.query(Subject).filter(Subject.company_id == company_id, Subject.code == "CS").first()
    g8 = db.query(Program).filter(Program.company_id == company_id, Program.name == "Grade 8").first()
    if not (sess and divya and cs and g8):
        return
    g8a = db.query(Section).filter(Section.program_id == g8.id, Section.session_id == sess.id, Section.name == "A").first()
    g8b = db.query(Section).filter(Section.program_id == g8.id, Section.session_id == sess.id, Section.name == "B").first()
    if g8a is None:
        return
    tz = svc.company_tz(db, company_id)
    now = datetime.now(tz)
    today = now.date()
    name = f"{divya.first_name} {divya.last_name or ''}".strip()

    def at(days: int, hour: int, minute: int = 0) -> datetime:
        d = today + timedelta(days=days)
        return datetime(d.year, d.month, d.day, hour, minute, tzinfo=tz).astimezone(timezone.utc)

    def homework(title, sections, subject, teacher_id, teacher_name, due, instructions, max_marks=None,
                 kind="homework", submission_type="online", allow_late=True, assigned_days=-2):
        a = Assignment(company_id=company_id, title=title, instructions=instructions, kind=kind, subject_id=subject.id,
                       program_id=sections[0].program_id, due_at=due, max_marks=max_marks,
                       submission_type=submission_type, allow_late=allow_late, status="published",
                       published_at=datetime.now(timezone.utc) + timedelta(days=assigned_days),
                       assigned_on=today + timedelta(days=assigned_days),
                       created_by_employee_id=teacher_id, created_by_name=teacher_name)
        a.sections = [AssignmentSection(company_id=company_id, section_id=s.id) for s in sections]
        db.add(a)
        db.flush()
        return a

    learners = students.section_students(db, company_id, g8a.id)
    aarav = next((s for s, _ in learners if s.first_name == "Aarav" and s.last_name == "Kumar"), None)
    others = [s for s, _ in learners if aarav is None or s.id != aarav.id]

    # ── Homework ──
    flow = homework("Flowchart for a simple calculator", [g8a] + ([g8b] if g8b else []), cs, divya.id, name, at(2, 9),
                    "Draw a flowchart for a calculator that adds, subtracts, multiplies and divides two numbers.\n\n"
                    "- Use the standard symbols (start/stop, input/output, process, decision)\n"
                    "- Handle division by zero\n- Upload a photo or type the steps", Decimal("10"))
    binary = homework("Binary numbers worksheet", [g8a], cs, divya.id, name, at(5, 9),
                      "Convert the following decimal numbers to binary: 5, 12, 25, 31, 64.\n"
                      "Then convert 1011 and 110010 back to decimal. Show your working.", Decimal("10"))
    poster = homework("Parts of a computer: poster", [g8a], cs, divya.id, name, at(-3, 9),
                      "Make a chart-paper poster of the parts of a computer and bring it to class.", Decimal("20"),
                      kind="project", submission_type="offline", assigned_days=-10)
    maths_alloc = db.query(SectionSubject).join(Subject, Subject.id == SectionSubject.subject_id).filter(
        SectionSubject.section_id == g8a.id, Subject.code == "MAT").first()
    if maths_alloc and maths_alloc.teacher_id:
        t = db.query(Employee).filter(Employee.id == maths_alloc.teacher_id).first()
        homework("Exercise 4.2: linear equations", [g8a], maths_alloc.subject, t.id,
                 f"{t.first_name} {t.last_name or ''}".strip(), at(1, 9),
                 "Solve questions 1 to 10 of exercise 4.2 in your notebook.", submission_type="offline")
    for prog_name in ("Grade 7", "Grade 9"):
        p = db.query(Program).filter(Program.company_id == company_id, Program.name == prog_name).first()
        sec = p and db.query(Section).filter(Section.program_id == p.id, Section.session_id == sess.id, Section.name == "A").first()
        if sec:
            homework("HTML basics: your first web page" if prog_name == "Grade 7" else "Python: loops practice", [sec], cs,
                     divya.id, name, at(3, 9), "Follow the steps shared in class and submit your file.", Decimal("10"))
    t_now = datetime.now(timezone.utc)
    if aarav:
        db.add(Submission(company_id=company_id, assignment_id=flow.id, student_id=aarav.id, status="submitted",
                          content="Start -> Input A, B and operator -> Decision on operator -> If divide and B = 0 show "
                                  "'cannot divide' -> else compute -> Output result -> Stop",
                          submitted_at=t_now - timedelta(hours=5), attempts=1))
        db.add(Submission(company_id=company_id, assignment_id=poster.id, student_id=aarav.id, status="returned",
                          submitted_at=t_now - timedelta(days=3), marks=Decimal("17"), feedback="Neat and well labelled.",
                          graded_at=t_now - timedelta(days=2), returned_at=t_now - timedelta(days=2),
                          graded_by_employee_id=divya.id))
    for i, s in enumerate(others[:3]):
        db.add(Submission(company_id=company_id, assignment_id=flow.id, student_id=s.id,
                          status="returned" if i == 0 else "submitted", is_late=False,
                          content="Flowchart attached in class notebook; steps: start, read numbers, choose operation, show result, stop.",
                          submitted_at=t_now - timedelta(hours=10 + i), attempts=1,
                          marks=Decimal("9") if i == 0 else None, feedback="Good use of the decision box." if i == 0 else None,
                          graded_at=t_now - timedelta(hours=2) if i == 0 else None,
                          returned_at=t_now - timedelta(hours=2) if i == 0 else None,
                          graded_by_employee_id=divya.id if i == 0 else None))
    for i, s in enumerate(others[:4]):
        db.add(Submission(company_id=company_id, assignment_id=poster.id, student_id=s.id, status="returned",
                          submitted_at=t_now - timedelta(days=3), marks=Decimal(14 + i), graded_at=t_now - timedelta(days=2),
                          returned_at=t_now - timedelta(days=2), graded_by_employee_id=divya.id))

    # ── Syllabus: Computer Science, Grade 8 ──
    plan = [
        (1, "Computer fundamentals", ["Parts of a computer", "Input and output devices", "Memory and storage"]),
        (1, "Number systems", ["Decimal and binary", "Binary arithmetic", "Octal and hexadecimal"]),
        (2, "Algorithms and flowcharts", ["What is an algorithm", "Flowchart symbols", "Decision making in flowcharts"]),
        (2, "Introduction to Python", ["Variables and data types", "Input and output", "Conditions with if", "Loops"]),
        (3, "Internet and safety", ["How the internet works", "Email etiquette", "Cyber safety and passwords"]),
    ]
    topics: dict[str, SyllabusTopic] = {}
    for i, (term, unit_title, tlist) in enumerate(plan, 1):
        u = SyllabusUnit(company_id=company_id, program_id=g8.id, subject_id=cs.id, term_no=term, title=unit_title, sort_order=i)
        db.add(u)
        db.flush()
        for j, tt in enumerate(tlist, 1):
            t = SyllabusTopic(company_id=company_id, unit_id=u.id, title=tt, sort_order=j, planned_periods=2)
            db.add(t)
            db.flush()
            topics[tt] = t
    covered = ["Parts of a computer", "Input and output devices", "Memory and storage", "Decimal and binary",
               "Binary arithmetic", "What is an algorithm", "Flowchart symbols"]
    for k, tt in enumerate(covered):
        db.add(TopicCoverage(company_id=company_id, topic_id=topics[tt].id, section_id=g8a.id,
                             covered_on=today - timedelta(days=40 - k * 5), covered_by_employee_id=divya.id))
    if g8b:
        for k, tt in enumerate(covered[:4]):
            db.add(TopicCoverage(company_id=company_id, topic_id=topics[tt].id, section_id=g8b.id,
                                 covered_on=today - timedelta(days=30 - k * 5), covered_by_employee_id=divya.id))
    mon = today - timedelta(days=today.weekday())
    db.add(LessonPlan(company_id=company_id, section_id=g8a.id, subject_id=cs.id, teacher_id=divya.id,
                      week_start=mon - timedelta(days=7), title="Flowchart symbols",
                      topic_ids=[topics["Flowchart symbols"].id], status="done",
                      objectives="Learners name and use the five basic flowchart symbols.",
                      activities="Symbol matching game; draw a flowchart for making tea in pairs.",
                      resources="Chart of symbols, worksheet", assessment="Exit ticket: 3 symbols",
                      reflection="Most learners got it; revisit the decision box."))
    db.add(LessonPlan(company_id=company_id, section_id=g8a.id, subject_id=cs.id, teacher_id=divya.id, week_start=mon,
                      title="Decision making in flowcharts", topic_ids=[topics["Decision making in flowcharts"].id],
                      objectives="Use decision boxes for yes/no choices.",
                      activities="Worked example: calculator; group work on a traffic light flowchart.",
                      resources="Projector, video on flowcharts", homework_id=flow.id, status="planned"))
    db.add(LessonPlan(company_id=company_id, section_id=g8a.id, subject_id=cs.id, teacher_id=divya.id,
                      week_start=mon + timedelta(days=7), title="Binary arithmetic practice",
                      topic_ids=[topics["Binary arithmetic"].id], objectives="Add binary numbers with carries.",
                      activities="Card flip activity; worksheet.", homework_id=binary.id, status="planned"))
    db.flush()

    # ── Materials ──
    def material(title, kind, description=None, url=None, topic=None, sections=None, files=None, views=0):
        m = Material(company_id=company_id, title=title, kind=kind, description=description, url=url, program_id=g8.id,
                     subject_id=cs.id, topic_id=topics[topic].id if topic else None,
                     unit_id=topics[topic].unit_id if topic else None, attachments=files or [], view_count=views,
                     created_by_employee_id=divya.id, created_by_name=name)
        m.sections = [MaterialSection(company_id=company_id, section_id=s.id) for s in (sections or [])]
        db.add(m)
        return m

    files = []
    try:
        from modules.documents import storage
        blob = storage.store(db, company_id, "Binary conversion cheat sheet.txt",
                             ("Binary cheat sheet\n\nPlace values: 128 64 32 16 8 4 2 1\n"
                              "To convert decimal to binary, divide by 2 and read the remainders from bottom to top.\n"
                              "Example: 13 = 1101\n").encode(), "text/plain", "lms")
        files = [{"uuid": str(blob.uuid), "name": blob.filename, "size": blob.size, "content_type": blob.content_type}]
    except Exception as exc:
        log.warning("LMS demo file failed: %s", exc)
    material("Binary conversion cheat sheet", "file", "Keep this handy for the worksheet.", topic="Decimal and binary",
             files=files, views=11)
    material("Flowcharts explained (video)", "video", "Watch before Thursday's class.",
             url="https://www.youtube.com/watch?v=SWRDqTx8d4k", topic="Flowchart symbols", views=18)
    material("Class notes: input and output devices", "note",
             "## Input devices\n- Keyboard, mouse, scanner, microphone\n\n## Output devices\n- Monitor, printer, speakers\n\n"
             "Some devices do both, such as a touch screen.", topic="Input and output devices", sections=[g8a], views=9)
    material("Practice Python online", "link", "A free in-browser Python editor for practice at home.",
             url="https://www.online-python.com/", topic="Variables and data types", views=4)
    material("Cyber safety tips for students", "link", "Read with your parents.",
             url="https://www.cybercrime.gov.in/", topic="Cyber safety and passwords")

    # ── Online classes (built-in video room) ──
    cfg = svc.settings(db, company_id)
    start_today = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)
    if start_today.date() != today:
        start_today = now + timedelta(minutes=30)
    tomorrow = at(1, 10)
    for title, starts, agenda in (("Decision making in flowcharts", start_today.astimezone(timezone.utc),
                                   "Revise symbols, then build the calculator flowchart together."),
                                  ("Binary numbers: live practice", tomorrow, "Bring your worksheet and a notebook.")):
        db.add(LiveClass(company_id=company_id, title=title, agenda=agenda, section_id=g8a.id, subject_id=cs.id,
                         teacher_id=divya.id, starts_at=starts, ends_at=starts + timedelta(minutes=40), provider="jitsi",
                         room_name=live.new_room_name(cfg["jitsi_room_prefix"]), created_by_employee_id=divya.id))
    past = at(-2, 11)
    pc = LiveClass(company_id=company_id, title="Memory and storage", section_id=g8a.id, subject_id=cs.id,
                   teacher_id=divya.id, starts_at=past, ends_at=past + timedelta(minutes=40), provider="jitsi",
                   room_name=live.new_room_name(cfg["jitsi_room_prefix"]), status="completed", started_at=past,
                   ended_at=past + timedelta(minutes=42), recording_url="https://www.youtube.com/watch?v=p3q5zWCw8J4",
                   created_by_employee_id=divya.id)
    db.add(pc)
    db.flush()
    from modules.lms.models import LiveClassJoin
    for s, _ in learners[: max(len(learners) - 1, 1)]:
        db.add(LiveClassJoin(company_id=company_id, live_class_id=pc.id, who_key=f"s:{s.id}", role="student",
                             student_id=s.id, name=s.full_name, first_joined_at=past + timedelta(minutes=1),
                             last_joined_at=past + timedelta(minutes=1)))
    db.add(LiveClassJoin(company_id=company_id, live_class_id=pc.id, who_key=f"e:{divya.id}", role="teacher",
                         employee_id=divya.id, name=name, first_joined_at=past, last_joined_at=past))

    # ── Question bank and a 10-question quiz ──
    qdefs = [
        ("single", "Which of these is an input device?", [("a", "Monitor"), ("b", "Keyboard"), ("c", "Printer"), ("d", "Speaker")], ["b"], "easy", "Computer fundamentals"),
        ("single", "What is the binary form of decimal 10?", [("a", "1010"), ("b", "1100"), ("c", "1001"), ("d", "0110")], ["a"], "medium", "Number systems"),
        ("multiple", "Which of these are storage devices? (pick all)", [("a", "Hard disk"), ("b", "Pen drive"), ("c", "Mouse"), ("d", "SSD")], ["a", "b", "d"], "medium", "Computer fundamentals"),
        ("true_false", "RAM keeps its data when the computer is switched off.", [], ["false"], "easy", "Computer fundamentals"),
        ("true_false", "A diamond shape in a flowchart shows a decision.", [], ["true"], "easy", "Algorithms and flowcharts"),
        ("short", "What do we call a step-by-step method to solve a problem?", [], ["algorithm", "an algorithm"], "easy", "Algorithms and flowcharts"),
        ("numeric", "What is binary 1111 in decimal?", [], {"value": "15", "tolerance": "0"}, "medium", "Number systems"),
        ("numeric", "1 kilobyte is about how many bytes? (within 30)", [], {"value": "1024", "tolerance": "30"}, "hard", "Computer fundamentals"),
        ("multiple", "Which symbols are used in flowcharts?", [("a", "Oval"), ("b", "Parallelogram"), ("c", "Star"), ("d", "Rectangle")], ["a", "b", "d"], "medium", "Algorithms and flowcharts"),
        ("long", "Explain in your own words why we should not share passwords. Give two examples.", [], None, "medium", "Internet and safety"),
    ]
    qrows = []
    for kind, prompt, opts, ans, diff, topic in qdefs:
        data = quizsvc.validate_question({"kind": kind, "options": [{"key": k, "text": t} for k, t in opts],
                                          "answer": ans, "difficulty": diff, "tags": ["grade 8"]})
        q = BankQuestion(company_id=company_id, subject_id=cs.id, program_id=g8.id, topic=topic, kind=kind, prompt=prompt,
                     options=data["options"], answer=data["answer"], marks=Decimal("2") if kind == "long" else Decimal("1"),
                     difficulty=diff, tags=data["tags"], created_by_employee_id=divya.id)
        db.add(q)
        qrows.append(q)
    db.flush()
    qz = Quiz(company_id=company_id, title="Computer basics quiz", kind="quiz", subject_id=cs.id, program_id=g8.id,
              instructions="Answer all questions. You have 15 minutes and two attempts.", mode="fixed",
              time_limit_min=15, attempts_allowed=2, shuffle_questions=True, shuffle_options=True,
              opens_at=datetime.now(timezone.utc) - timedelta(days=1), closes_at=datetime.now(timezone.utc) + timedelta(days=6),
              show_answers="after_close", pass_percent=Decimal("40"), status="published",
              created_by_employee_id=divya.id, created_by_name=name)
    qz.sections = [QuizSection(company_id=company_id, section_id=g8a.id)]
    qz.questions = [QuizQuestion(company_id=company_id, question_id=q.id, sort_order=i) for i, q in enumerate(qrows, 1)]
    db.add(qz)
    db.flush()
    import random as _r
    rng = _r.Random(3)
    for n, s in enumerate(others[:3]):
        learner = svc.Learner(s, g8a.id, g8.id)
        a = quizsvc.start_attempt(db, qz, learner, rng=rng)
        answers = {}
        for q in qrows:
            right = q.answer
            if q.kind in ("single", "true_false"):
                answers[str(q.id)] = right[0] if rng.random() > 0.25 * n else (q.options[-1]["key"])
            elif q.kind == "multiple":
                answers[str(q.id)] = right if rng.random() > 0.3 else right[:1]
            elif q.kind == "short":
                answers[str(q.id)] = "Algorithm"
            elif q.kind == "numeric":
                answers[str(q.id)] = right["value"]
            else:
                answers[str(q.id)] = "Someone could log in as me and change my work. They could also see my messages."
        a.answers = answers
        quizsvc.finish(db, a)
        if n == 0:
            long_q = next(q for q in qrows if q.kind == "long")
            quizsvc.mark_manually(db, {"employee_id": divya.id, "company_id": company_id}, a,
                                  {str(long_q.id): {"score": "2", "feedback": "Clear examples."}})
    db.flush()
