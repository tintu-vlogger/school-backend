"""Company setup and demo data for learning (called by seed/seed_data.py).

setup_company(db, company_id)  a certificate number series, and a ready POSH awareness
                               course with its question bank and quiz (the POSH Act requires
                               awareness programmes); an enrolment rule for everyone is
                               created switched off.
seed_demo(db, company_id)      the demo company: six courses (POSH, fire safety, information
                               security, feedback for managers, Excel, customer conversations
                               with a classroom batch), a learning path, enrolments with real
                               progress, quiz attempts, certificates and a course request.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from core.context import RequestContext, use_context

POSH_LESSONS = [
    ("Why this matters", "text", 6,
     "Everyone has the right to a workplace free of sexual harassment. In India this is protected by the Sexual "
     "Harassment of Women at Workplace (Prevention, Prohibition and Redressal) Act, 2013, often called the POSH Act.\n\n"
     "The Act applies to every workplace, including offices, client sites, travel for work, events and online work. "
     "Every employer with 10 or more employees must set up an Internal Committee and run awareness programmes like this one.\n\n"
     "This course takes about 30 minutes. At the end there is a short quiz; you need 70% to pass."),
    ("What counts as sexual harassment", "text", 6,
     "Sexual harassment is unwelcome behaviour of a sexual nature, whether physical, verbal, non-verbal or online. "
     "It includes physical contact and advances, a demand or request for sexual favours, sexually coloured remarks, "
     "showing pornography, and any other unwelcome conduct of a sexual nature.\n\n"
     "What matters is how the behaviour is received, not what the person intended. A joke that one person finds "
     "harmless can make another feel unsafe.\n\n"
     "Promises of preferential treatment, threats about someone's job, or creating an intimidating or hostile "
     "environment connected to such behaviour are also harassment."),
    ("The Internal Committee", "text", 5,
     "The Internal Committee (IC) is the body that receives and inquires into complaints. It is chaired by a senior "
     "woman employee, has at least two employee members committed to the cause of women, and one external member "
     "from an NGO or someone familiar with the issues. At least half of the members are women.\n\n"
     "The IC must complete an inquiry within 90 days. Its proceedings are confidential; the identity of the person "
     "who complains, the respondent and witnesses must not be made public.\n\n"
     "Where a workplace has fewer than 10 employees, complaints go to the Local Committee set up by the district."),
    ("How to raise a complaint", "text", 5,
     "A complaint should be made in writing to the Internal Committee within three months of the incident (or of the "
     "last incident in a series). The committee can extend this by another three months if there were reasons that "
     "prevented it.\n\n"
     "If you need help writing the complaint, any IC member will assist. You can also ask for interim relief during "
     "the inquiry, such as a transfer or leave.\n\n"
     "Retaliation against anyone who complains or helps an inquiry is itself misconduct. A complaint that is found "
     "to be false and malicious can lead to action, but a complaint that simply cannot be proved is not false."),
    ("Being a good colleague and bystander", "text", 5,
     "If you see something that makes a colleague uncomfortable, you can help: check in with them privately, "
     "interrupt the behaviour if it is safe, and tell them about the Internal Committee.\n\n"
     "Managers must take every concern seriously, never discourage a complaint, and keep what they hear confidential.\n\n"
     "Respect is everyone's job. Thank you for completing the lessons. The quiz is next."),
]

POSH_QUESTIONS = [
    ("single", "Which law protects women from sexual harassment at the workplace in India?",
     [("The POSH Act, 2013", True), ("The Factories Act, 1948", False), ("The Payment of Wages Act, 1936", False), ("The Shops Act", False)]),
    ("true_false", "The POSH Act applies only inside the office building.", "false"),
    ("single", "Within how many months of an incident should a complaint usually be made?",
     [("One month", False), ("Three months", True), ("Six months", False), ("One year", False)]),
    ("single", "Who must chair the Internal Committee?",
     [("The CEO", False), ("A senior woman employee", True), ("The HR manager", False), ("An external lawyer", False)]),
    ("multi", "Which of these can be sexual harassment? (pick all that apply)",
     [("Sexually coloured remarks", True), ("Showing pornography", True), ("Unwelcome physical contact", True), ("Asking for a status update", False)]),
    ("true_false", "What matters is how the behaviour is received, not only what was intended.", "true"),
    ("single", "Within how many days must the Internal Committee finish its inquiry?",
     [("30 days", False), ("60 days", False), ("90 days", True), ("180 days", False)]),
    ("single", "An employer must set up an Internal Committee when it has at least…",
     [("5 employees", False), ("10 employees", True), ("50 employees", False), ("100 employees", False)]),
    ("short", "What is the short name of the committee that inquires into complaints? (two words)", ["Internal Committee", "IC"]),
    ("true_false", "Taking action against someone because they complained is allowed if the complaint is not proved.", "false"),
]


def _ctx(company_id: int, emp=None) -> RequestContext:
    if emp is None:
        return RequestContext(company_id=company_id, actor_type="owner", actor_name="Company owner", role="admin")
    return RequestContext(company_id=company_id, employee_id=emp.id, actor_type="employee", role=emp.role,
                          actor_name=f"{emp.first_name} {emp.last_name}".strip())


def _actor(company_id: int, emp=None):
    from modules.learning.service import Actor
    if emp is None:
        return Actor(company_id, None, "admin", "Company owner", set())
    return Actor(company_id, emp.id, emp.role, f"{emp.first_name} {emp.last_name}", set())


def _category(db: Session, company_id: int, code: str) -> int | None:
    from core.masters import MasterType, MasterValue
    return (db.query(MasterValue.id).join(MasterType, MasterType.id == MasterValue.type_id)
            .filter(MasterValue.company_id == company_id, MasterType.key == "course_category", MasterValue.code == code).scalar())


def _questions(db, company_id, actor, category, items) -> list[int]:
    from modules.learning import service
    ids = []
    for it in items:
        kind, text = it[0], it[1]
        data = {"category": category, "kind": kind, "text": text, "marks": 1, "difficulty": "easy" if kind == "true_false" else "medium"}
        if kind in ("single", "multi"):
            data["options"] = [{"text": t, "correct": c} for t, c in it[2]]
        elif kind == "true_false":
            data["answer"] = it[2]
        else:
            data["accepted_answers"] = it[2]
        ids.append(service.save_question(db, actor, data).id)
    return ids


def setup_company(db: Session, company_id: int) -> None:
    from core.numbering import NumberSeries
    from modules.learning import service
    from modules.learning.models import Course, EnrolmentRule

    if not db.query(NumberSeries.id).filter(NumberSeries.company_id == company_id, NumberSeries.key == "certificate").first():
        db.add(NumberSeries(company_id=company_id, key="certificate", name="Certificates", prefix="CERT",
                            pattern="{PREFIX}/{YYYY}/{SEQ:5}", reset="yearly", active=True, is_system=False,
                            description="Numbers on course completion certificates."))
        db.flush()
    if db.query(Course.id).filter(Course.company_id == company_id).first():
        try:
            with db.begin_nested():
                service.run_all_rules(db, company_id)  # new joiners get their mandatory courses
        except Exception as exc:  # catches up on the next start
            import logging
            logging.getLogger("hrms").warning("Learning rules failed for company %s: %s", company_id, exc)
        return
    actor = _actor(company_id)
    with use_context(_ctx(company_id)):
        qids = _questions(db, company_id, actor, "POSH", POSH_QUESTIONS)
        quiz = service.save_assessment(db, actor, {
            "title": "POSH awareness quiz", "description": "8 questions picked from the POSH question bank.",
            "selection": "random", "random_rule": {"count": 8, "categories": ["POSH"]}, "question_ids": qids,
            "time_limit_minutes": 15, "max_attempts": 3, "pass_pct": 70, "negative_marking": "percent",
            "negative_value": 25, "show_answers": "after_submit", "final_score": "highest", "status": "published"})
        lessons = [{"title": t, "kind": k, "duration_minutes": m, "body": b} for t, k, m, b in POSH_LESSONS]
        lessons.append({"title": "Quiz: check your understanding", "kind": "assessment", "assessment_id": quiz.id, "duration_minutes": 10})
        c = service.save_course(db, actor, {
            "title": "POSH awareness", "summary": "Prevent sexual harassment at work: what it is, your rights and how to raise a concern.",
            "description": "Covers the POSH Act, 2013: what counts as harassment, the Internal Committee, how complaints work and how to be a good bystander.",
            "category_id": _category(db, company_id, "compliance"), "level": "beginner", "delivery": "online", "cover": "rose",
            "sequential": True, "self_enrol": True, "mandatory": True, "due_days": 30, "recert_months": 12,
            "assessment_id": quiz.id, "certificate": True, "tags": ["POSH", "Compliance", "Mandatory"], "lessons": lessons,
            "status": "published"})
        db.add(EnrolmentRule(company_id=company_id, name="POSH awareness for everyone", course_id=c.id,
                             criteria={"all": True, "department_ids": [], "designation_ids": [], "condition": ""},
                             mandatory=True, due_days=30, active=False))
    db.flush()


# ── Demo ────────────────────────────────────────────────────────────

def _learn(db, company_id, emp, course, lessons_to_do: int, rng, pass_quiz: bool = True):
    """Walk an employee through the first N lessons, taking quizzes on the way."""
    from modules.learning import service
    from modules.learning.models import Assessment, Question
    actor = _actor(company_id, emp)
    e = service._open_enrolment(db, company_id, course.id, emp.id)
    if e is None:
        return
    with use_context(_ctx(company_id, emp)):
        for l in course.lessons[:lessons_to_do]:
            if l.kind == "assessment":
                a = db.get(Assessment, l.assessment_id)
                att = service.start_attempt(db, actor, a)
                qs = {q.id: q for q in db.query(Question).filter(Question.id.in_(att.question_ids)).all()}
                answers = {}
                for qid in att.question_ids:
                    q = qs[qid]
                    right = rng.random() < (0.92 if pass_quiz else 0.4)
                    if q.kind == "short":
                        answers[str(qid)] = (q.accepted_answers or ["x"])[0] if right else "not sure"
                    else:
                        good = [o["id"] for o in q.options if o.get("correct")]
                        bad = [o["id"] for o in q.options if not o.get("correct")]
                        pick = good if right or not bad else [bad[0]]
                        answers[str(qid)] = pick if q.kind == "multi" else pick[0]
                service.submit_attempt(db, actor, att, answers)
                db.refresh(e)
                if not att.passed:
                    return
            else:
                service.complete_lesson(db, actor, e, l.uuid)
    db.flush()


def seed_demo(db: Session, company_id: int) -> None:
    from modules.employees.models import Employee
    from modules.learning import service
    from modules.learning.models import Batch, Course, CourseRequest, Enrolment, EnrolmentRule, LearningPath
    db.info["company_id"] = company_id
    emps = (db.query(Employee).filter(Employee.company_id == company_id, Employee.active.is_(True),
                                      Employee.department_id.isnot(None)).order_by(Employee.id).all())
    if len(emps) < 8 or db.query(Course.id).filter(Course.company_id == company_id).count() > 1:
        return
    by = {f"{e.first_name} {e.last_name}": e for e in emps}
    rng = random.Random(11)
    owner = _actor(company_id)
    t = service._today(db, company_id)
    with use_context(_ctx(company_id)):
        posh = db.query(Course).filter(Course.company_id == company_id, Course.title == "POSH awareness").first()
        # fire safety
        fq = _questions(db, company_id, owner, "Fire safety", [
            ("single", "What is the first thing to do when you hear the fire alarm?",
             [("Finish your email", False), ("Leave by the nearest safe exit", True), ("Take the lift", False)]),
            ("true_false", "Lifts are safe to use during a fire.", "false"),
            ("single", "Which extinguisher is safe for electrical fires?",
             [("Water", False), ("CO2", True), ("Foam", False)]),
            ("multi", "PASS for using an extinguisher means… (pick all that apply)",
             [("Pull the pin", True), ("Aim at the base", True), ("Squeeze the handle", True), ("Shout for help first", False)]),
            ("short", "Where do we gather after leaving the building? (two words)", ["Assembly point", "assembly area"]),
        ])
        fire_quiz = service.save_assessment(db, owner, {"title": "Fire safety check", "selection": "fixed", "question_ids": fq,
                                                        "pass_pct": 60, "max_attempts": None, "negative_marking": "none",
                                                        "time_limit_minutes": 10, "status": "published"})
        fire = service.save_course(db, owner, {
            "title": "Fire and workplace safety", "summary": "Know what to do in a fire, an earthquake or a medical emergency.",
            "category_id": _category(db, company_id, "safety"), "level": "beginner", "cover": "amber", "mandatory": True,
            "due_days": 45, "recert_months": 12, "certificate": True, "tags": ["Safety", "Mandatory"], "status": "published",
            "assessment_id": fire_quiz.id,
            "lessons": [
                {"title": "Fire safety basics", "kind": "text", "duration_minutes": 6, "body":
                    "Most office fires start with electrical faults, overloaded sockets or kitchen appliances.\n\n"
                    "Keep exits and corridors clear, never block fire doors, and report damaged wiring at once.\n\n"
                    "Know where the nearest two exits and the fire extinguishers are on your floor."},
                {"title": "When the alarm sounds", "kind": "text", "duration_minutes": 5, "body":
                    "Stop work, leave by the nearest safe exit and walk to the assembly point. Do not use lifts. "
                    "Do not go back for belongings.\n\nFloor wardens will check that everyone is out. Wait at the assembly "
                    "point until you are told it is safe."},
                {"title": "Using a fire extinguisher (video)", "kind": "video", "duration_minutes": 4,
                 "url": "https://www.w3schools.com/html/mov_bbb.mp4"},
                {"title": "First aid and medical emergencies", "kind": "text", "duration_minutes": 5, "body":
                    "Call the emergency number 112 or the building security desk. First aiders on each floor are listed "
                    "near the pantry.\n\nStay with the person, keep them comfortable and do not move them if they may be injured."},
                {"title": "Safety check", "kind": "assessment", "assessment_id": fire_quiz.id, "duration_minutes": 5},
            ]})
        infosec = service.save_course(db, owner, {
            "title": "Information security basics", "summary": "Spot phishing, protect passwords and keep customer data safe.",
            "category_id": _category(db, company_id, "technical"), "level": "beginner", "cover": "sky", "certificate": True,
            "due_days": 30, "tags": ["Security", "Data privacy"], "status": "published",
            "lessons": [
                {"title": "Passwords and two-step sign-in", "kind": "text", "duration_minutes": 5, "body":
                    "Use a password manager and a different password for every system. Turn on two-step sign-in wherever it is offered."},
                {"title": "Spotting phishing", "kind": "text", "duration_minutes": 6, "body":
                    "Phishing emails create urgency, ask you to sign in through a link or open an attachment. Check the sender, "
                    "hover over links, and when in doubt, report the email to IT."},
                {"title": "Handling personal data (DPDP Act)", "kind": "text", "duration_minutes": 7, "body":
                    "The Digital Personal Data Protection Act, 2023 requires us to collect only the data we need, use it only "
                    "for the purpose given, keep it safe and delete it when no longer needed. Never share customer or employee "
                    "data on personal email or chat."},
                {"title": "Security policy (PDF)", "kind": "file", "duration_minutes": 3, "file_name": "information-security-policy.pdf",
                 "url": "https://www.africau.edu/images/default/sample.pdf"},
            ]})
        feedback = service.save_course(db, owner, {
            "title": "Giving great feedback", "summary": "A practical guide for managers: clear, kind and useful feedback every week.",
            "category_id": _category(db, company_id, "leadership"), "level": "intermediate", "cover": "violet", "certificate": True,
            "tags": ["Managers", "Feedback"], "status": "published",
            "lessons": [
                {"title": "Why feedback matters", "kind": "text", "duration_minutes": 4, "body":
                    "People who get regular, specific feedback grow faster and stay longer. Feedback is a habit, not an annual event."},
                {"title": "The SBI model", "kind": "text", "duration_minutes": 6, "body":
                    "Situation: when and where. Behaviour: what the person did, exactly. Impact: what happened as a result.\n\n"
                    "Example: In Tuesday's client call (situation), you summarised the open issues before we closed (behaviour). "
                    "The client said it was the clearest call they have had with us (impact)."},
                {"title": "Feedback in one-to-ones", "kind": "text", "duration_minutes": 5, "body":
                    "Make feedback a standing agenda item. Ask for feedback on yourself first; it makes the conversation two-way."},
                {"title": "Difficult conversations", "kind": "video", "duration_minutes": 8, "url": "https://www.w3schools.com/html/mov_bbb.mp4"},
            ]})
        excel = service.save_course(db, owner, {
            "title": "Excel for business", "summary": "Lookups, pivot tables and charts for everyday reporting.",
            "category_id": _category(db, company_id, "technical"), "level": "intermediate", "cover": "emerald", "certificate": True,
            "sequential": False, "tags": ["Excel", "Reporting"], "status": "published",
            "lessons": [
                {"title": "Clean data first", "kind": "text", "duration_minutes": 8, "body": "Use tables, consistent formats and no merged cells. Clean data saves hours later."},
                {"title": "XLOOKUP in practice", "kind": "video", "duration_minutes": 10, "url": "https://www.w3schools.com/html/mov_bbb.mp4"},
                {"title": "Pivot tables", "kind": "text", "duration_minutes": 12, "body": "Drag fields into rows, columns and values. Group dates by month and show values as % of total."},
                {"title": "Charts that tell a story", "kind": "text", "duration_minutes": 8, "body": "One message per chart. Label directly, remove clutter, and highlight the number that matters."},
                {"title": "Practice workbook", "kind": "file", "duration_minutes": 20, "file_name": "excel-practice.xlsx",
                 "url": "https://www.africau.edu/images/default/sample.pdf"},
            ]})
        sales = service.save_course(db, owner, {
            "title": "Customer conversations that convert", "summary": "A hands-on classroom workshop on discovery, demos and closing.",
            "category_id": _category(db, company_id, "sales"), "level": "intermediate", "delivery": "blended", "cover": "orange",
            "certificate": True, "tags": ["Sales", "Workshop"], "status": "published",
            "lessons": [
                {"title": "Pre-read: the discovery checklist", "kind": "text", "duration_minutes": 10, "body":
                    "Before the workshop, list three recent deals: what the customer needed, who decided, and why they bought (or did not)."},
            ]})
        service.save_batch(db, owner, {
            "course_id": sales.id, "name": f"Bengaluru · {(t + timedelta(days=12)).strftime('%B %Y')}", "mode": "classroom",
            "venue": "Training room 2, 4th floor", "capacity": 6, "trainer_name": "Anita Desai (external)",
            "sessions": [{"date": (t + timedelta(days=12)).isoformat(), "start": "10:00", "end": "13:00", "topic": "Discovery that uncovers real needs"},
                         {"date": (t + timedelta(days=13)).isoformat(), "start": "10:00", "end": "13:00", "topic": "Demos and closing"}]})
        service.save_batch(db, owner, {
            "course_id": sales.id, "name": "Online · evening batch", "mode": "virtual", "meeting_link": "https://meet.example.com/sales-workshop",
            "capacity": 20, "trainer_employee_id": by["Rohan Kulkarni"].id if by.get("Rohan Kulkarni") else None,
            "sessions": [{"date": (t + timedelta(days=20)).isoformat(), "start": "17:30", "end": "19:00", "topic": "Discovery"},
                         {"date": (t + timedelta(days=22)).isoformat(), "start": "17:30", "end": "19:00", "topic": "Closing"}]})
        path = service.save_path(db, owner, {
            "title": "New manager essentials", "kind": "plan", "description": "Everything a first-time manager needs in their first 90 days.",
            "items": [{"course_id": feedback.id, "required": True}, {"course_id": posh.id if posh else infosec.id, "required": True},
                      {"course_id": excel.id, "required": False}], "due_days": 90, "status": "published"})
        service.save_path(db, owner, {
            "title": "Compliance essentials", "kind": "path", "description": "The trainings every employee completes each year.",
            "items": [{"course_id": c.id, "required": True} for c in (posh, fire, infosec) if c], "status": "published"})

        # Enrolments: POSH and fire safety for everyone (by rule), the rest by choice
        if posh:
            rule = db.query(EnrolmentRule).filter(EnrolmentRule.company_id == company_id, EnrolmentRule.course_id == posh.id).first()
            if rule:
                rule.active = True
                service.run_rule(db, company_id, rule, by="HR")
        fire_rule = service.save_rule(db, owner, {"name": "Fire safety for everyone", "course_id": fire.id, "criteria": {"all": True},
                                                  "mandatory": True, "due_days": 45})
        service.run_rule(db, company_id, fire_rule, by="HR")
        managers = [e for e in emps if e.role in ("manager", "admin") or by.get("Priya Sharma") == e]
        service.assign_path(db, owner, path, [m.id for m in managers[:6]], None)
        for e in rng.sample(emps, min(10, len(emps))):
            service.enrol(db, company_id, infosec, e.id, source="self")
        for e in rng.sample(emps, min(7, len(emps))):
            service.enrol(db, company_id, excel, e.id, source="self")
    db.flush()
    for c in (posh, fire, infosec, feedback, excel):
        if c is not None:
            db.refresh(c)
    priya = by.get("Priya Sharma")
    for e in emps:
        if posh:
            if priya and e.id == priya.id:
                _learn(db, company_id, e, posh, 2, rng)
            else:
                r = rng.random()
                _learn(db, company_id, e, posh, 6 if r < 0.62 else (rng.randint(1, 4) if r < 0.9 else 0), rng)
        r = rng.random()
        _learn(db, company_id, e, fire, 5 if (r < 0.45 or (priya and e.id == priya.id)) else (rng.randint(1, 3) if r < 0.8 else 0), rng)
    for c in (infosec, feedback, excel):
        for en in db.query(Enrolment).filter(Enrolment.company_id == company_id, Enrolment.course_id == c.id).all():
            emp = next((x for x in emps if x.id == en.employee_id), None)
            if emp:
                _learn(db, company_id, emp, c, rng.choice([0, 1, 2, 3, len(c.lessons)]), rng)
    # a few people are late on mandatory training
    late = (db.query(Enrolment).filter(Enrolment.company_id == company_id, Enrolment.mandatory.is_(True),
                                       Enrolment.status.in_(("enrolled", "in_progress"))).order_by(Enrolment.id).limit(4).all())
    for en in late:
        if not (priya and en.employee_id == priya.id):
            en.due_date = t - timedelta(days=rng.randint(2, 9))
    # backdate completions so they spread over the past weeks
    for en in db.query(Enrolment).filter(Enrolment.company_id == company_id, Enrolment.status == "completed").all():
        back = timedelta(days=rng.randint(1, 40))
        en.completed_at = (en.completed_at or datetime.now(timezone.utc)) - back
        en.certificate_issued_at = en.completed_at
        if en.expires_on:
            en.expires_on = en.expires_on - back
    # seats on the classroom batch (fills up, one waitlisted)
    batch = db.query(Batch).filter(Batch.company_id == company_id, Batch.course_id == sales.id, Batch.mode == "classroom").first()
    sellers = [e for e in emps if e.department_id == (by["Rohan Kulkarni"].department_id if by.get("Rohan Kulkarni") else -1)]
    sellers += [e for e in emps if e not in sellers][:max(0, 7 - len(sellers))]
    if batch:
        for e in sellers[:7]:
            with use_context(_ctx(company_id)):
                service.book_seat(db, owner, batch, e.id)
    if not db.query(CourseRequest.id).filter(CourseRequest.company_id == company_id).first():
        for nm, title, why, cost in (("Ananya Reddy", "AWS Solutions Architect – Associate", "We are moving more services to AWS; I'd like to lead the design.", 15000),
                                     ("Tanvi Bhat", "Google Analytics 4 certification", "To measure campaigns properly.", 0)):
            if by.get(nm):
                with use_context(_ctx(company_id, by[nm])):
                    service.create_request(db, _actor(company_id, by[nm]), {"title": title, "reason": why,
                                                                           "provider": "Online", "estimated_cost": cost or None})
    db.flush()
