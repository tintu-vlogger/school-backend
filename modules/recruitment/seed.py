"""Hiring defaults for every company (interview kits) and the talent lifecycle demo for
the development company: 3 open jobs with 20 candidates across the stages, interviews
and scorecards, 2 offers, 2 people onboarding, 3 on probation and 2 exits in progress."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from sqlalchemy.orm import Session

from modules.recruitment.models import InterviewKit, Job

KITS = [
    ("Technical interview", "Problem solving and role skills for engineering roles", [
        ("problem_solving", "Problem solving"), ("coding", "Coding and design"), ("system_design", "System design"),
        ("communication", "Communication")],
     ["Walk us through a system you built end to end.", "How would you design a rate limiter?",
      "Tell us about a production incident and what you learned."], 60),
    ("HR and culture fit", "Values, motivation and team fit", [
        ("motivation", "Motivation"), ("culture", "Values and team fit"), ("communication", "Communication"),
        ("stability", "Career stability")],
     ["Why are you looking for a change?", "What does a great manager do for you?",
      "Where do you want to be in three years?"], 30),
    ("Sales role-play", "Pitch, objection handling and closing", [
        ("pitch", "Pitch"), ("objections", "Handling objections"), ("negotiation", "Negotiation"),
        ("closing", "Closing")],
     ["Sell us our product in five minutes.", "The client says the price is too high. Respond."], 45),
]


def setup_company(db: Session, company_id: int) -> None:
    if db.query(InterviewKit.id).filter(InterviewKit.company_id == company_id).first():
        return
    for name, desc, comps, questions, minutes in KITS:
        db.add(InterviewKit(company_id=company_id, name=name, description=desc,
                            competencies=[{"key": k, "label": label} for k, label in comps], questions=questions,
                            duration_minutes=minutes))
    db.flush()


JOBS = [
    {"title": "Senior Backend Engineer", "dept": "Engineering", "loc": "Bengaluru", "mode": "hybrid",
     "skills": ["Python", "Django", "PostgreSQL", "AWS", "REST"], "nice": ["Docker", "Kubernetes", "Redis"],
     "exp": (4, 8), "sal": (1800000, 3000000), "kit": "Technical interview", "openings": 2,
     "summary": "Build the APIs that run payroll and HR for thousands of Indian companies.",
     "description": "<p>You will design and build reliable services in Python and PostgreSQL, own features end to end "
                    "and mentor engineers.</p><ul><li>4+ years building backend systems</li><li>Strong SQL and API design"
                    "</li><li>Experience with AWS</li></ul>"},
    {"title": "HR Executive", "dept": "Human Resources", "loc": "Pune", "mode": "office",
     "skills": ["Recruitment", "Payroll", "Labour Law", "Excel"], "nice": ["HRMS", "Statutory Compliance"],
     "exp": (2, 5), "sal": (500000, 800000), "kit": "HR and culture fit", "openings": 1,
     "summary": "Run joining, records and statutory compliance for a growing team.",
     "description": "<p>Own onboarding, employee records, PF and ESI filings and employee queries.</p>"
                    "<ul><li>2-5 years in HR operations</li><li>Working knowledge of the Labour Codes</li></ul>"},
    {"title": "Enterprise Sales Manager", "dept": "Sales", "loc": "Chennai", "mode": "office",
     "skills": ["Sales", "Negotiation", "Business Development"], "nice": ["SaaS", "CRM"],
     "exp": (5, 10), "sal": (1500000, 2400000), "kit": "Sales role-play", "openings": 1,
     "summary": "Win mid-market and enterprise customers across South India.",
     "description": "<p>Build pipeline, run demos and close deals with HR and finance leaders.</p>"},
]

# first, last, job index, stage, years, city, skills, current company, title, source
CANDIDATES = [
    ("Aarav", "Shah", 0, "applied", 5, "Bengaluru", ["Python", "Django", "PostgreSQL", "AWS"], "Razorpay", "Software Engineer", "careers"),
    ("Ishita", "Banerjee", 0, "applied", 3, "Kolkata", ["Java", "Spring", "MySQL"], "TCS", "Developer", "job_board"),
    ("Vivek", "Raman", 0, "screening", 6, "Bengaluru", ["Python", "FastAPI", "PostgreSQL", "Docker", "AWS"], "Swiggy", "Senior Engineer", "linkedin"),
    ("Megha", "Arora", 0, "shortlisted", 7, "Hyderabad", ["Python", "Django", "REST", "Redis", "AWS"], "Freshworks", "Lead Engineer", "referral"),
    ("Kunal", "Desai", 0, "interview", 5, "Bengaluru", ["Python", "Django", "PostgreSQL", "Kubernetes"], "Zoho", "Senior Engineer", "careers"),
    ("Nandini", "Rao", 0, "interview", 8, "Pune", ["Python", "Django", "PostgreSQL", "AWS", "Docker"], "Infosys", "Tech Lead", "agency"),
    ("Harsh", "Vardhan", 0, "offer", 6, "Bengaluru", ["Python", "Django", "PostgreSQL", "AWS", "Redis"], "PhonePe", "Senior Engineer", "careers"),
    ("Tara", "Joseph", 0, "hired_offer", 5, "Kochi", ["Python", "Django", "PostgreSQL", "AWS"], "UST", "Software Engineer", "referral"),
    ("Rakesh", "Yadav", 1, "applied", 2, "Pune", ["Recruitment", "Excel"], "Quess", "HR Associate", "job_board"),
    ("Priyanka", "Kulkarni", 1, "applied", 4, "Mumbai", ["Payroll", "Excel", "HRMS"], "TeamLease", "Payroll Executive", "careers"),
    ("Swati", "Jain", 1, "screening", 3, "Pune", ["Recruitment", "Payroll", "Labour Law"], "Persistent", "HR Executive", "careers"),
    ("Arvind", "Menon", 1, "shortlisted", 5, "Pune", ["Payroll", "Labour Law", "Statutory Compliance", "Excel"], "KPIT", "HR Generalist", "walk_in"),
    ("Fatima", "Sheikh", 1, "interview", 4, "Pune", ["Recruitment", "Payroll", "Labour Law", "Excel", "HRMS"], "Bajaj Finserv", "HR Executive", "linkedin"),
    ("Gaurav", "Malhotra", 2, "applied", 9, "Chennai", ["Sales", "Negotiation", "SaaS"], "Freshworks", "Account Executive", "linkedin"),
    ("Lavanya", "Sundaram", 2, "screening", 6, "Chennai", ["Sales", "Business Development", "CRM"], "Chargebee", "Sales Manager", "careers"),
    ("Mohit", "Saxena", 2, "screening", 4, "Delhi", ["Sales", "Marketing"], "Byju's", "Sales Associate", "job_board"),
    ("Deepa", "Natarajan", 2, "interview", 7, "Chennai", ["Sales", "Negotiation", "Business Development", "SaaS"], "Zoho", "Enterprise AE", "referral"),
    ("Sameer", "Kapoor", 2, "interview", 8, "Bengaluru", ["Sales", "Negotiation", "Business Development", "CRM"], "Salesforce", "Account Manager", "agency"),
    ("Anjali", "Pandey", 2, "rejected", 3, "Lucknow", ["Customer Service"], "Teleperformance", "Team Lead", "careers"),
    ("Yusuf", "Khan", 0, "rejected", 2, "Delhi", ["PHP", "Laravel"], "Freelance", "Developer", "job_board"),
]


def _resume(first, last, years, city, skills, company, title) -> str:
    return (f"{first} {last}\n{first.lower()}.{last.lower()}@example.com | {city}\n\nSummary\n{years} years of experience. "
            f"Currently working as {title} at {company}.\n\nSkills\n{', '.join(skills)}\n\nEducation\nB.Tech, 2016\n")


def seed_demo(db: Session, company_id: int) -> None:
    from modules.recruitment import common as c, service
    if db.query(Job.id).filter(Job.company_id == company_id).first():
        return
    E = c.Employee()
    emps = db.query(E).filter(E.company_id == company_id, E.active.is_(True)).order_by(E.id).all()
    if len(emps) < 5:
        return  # people are seeded later on the first start; this runs again on the next start
    setup_company(db, company_id)
    from modules.departments.models import Department
    from modules.organization.models import Location
    depts = {d.department_name: d.id for d in db.query(Department).filter(Department.company_id == company_id)}
    locs = {l_.city: l_.id for l_ in db.query(Location).filter(Location.company_id == company_id)}
    hr = next((e for e in emps if e.role in ("hr", "admin")), emps[0])
    managers = [e for e in emps if e.role in ("manager", "admin")] or emps
    user = {"company_id": company_id, "role": "admin", "employee_id": None}

    class _V(service.Viewer):
        def __init__(self):
            self.db, self.user, self.company_id, self.employee_id = db, user, company_id, None
            self.admin = self.offers = True
    v = _V()
    kits = {k.name: k for k in db.query(InterviewKit).filter(InterviewKit.company_id == company_id)}
    jobs = []
    for i, j in enumerate(JOBS):
        mgr = managers[i % len(managers)]
        job = service.save_job(db, v, {
            "title": j["title"], "department_id": depts.get(j["dept"]), "location_id": locs.get(j["loc"]),
            "location_text": None if locs.get(j["loc"]) else j["loc"], "work_mode": j["mode"], "skills": j["skills"],
            "nice_skills": j["nice"], "min_experience": j["exp"][0], "max_experience": j["exp"][1],
            "salary_min": j["sal"][0], "salary_max": j["sal"][1], "show_salary": i != 2, "openings": j["openings"],
            "hiring_manager_id": mgr.id, "summary": j["summary"], "description": j["description"],
            "team": [{"employee_id": hr.id, "role": "recruiter"}, {"employee_id": mgr.id, "role": "interviewer"}],
            "questions": [{"key": "notice", "label": "Notice period (days)", "type": "number", "required": False},
                          {"key": "relocate", "label": "Are you open to working from our office?", "type": "yesno"}],
        })
        service.set_job_status(db, v, job, "publish")
        job.published_at = c.now() - timedelta(days=20 - i * 4)
        jobs.append(job)
    db.flush()
    now = c.now()
    t0 = c.today(db, company_id)
    offers_made = []
    for idx, (first, last, ji, stage, years, city, skills, company, title, source) in enumerate(CANDIDATES):
        job = jobs[ji]
        text = _resume(first, last, years, city, skills, company, title)
        cand = service.save_candidate(db, company_id, {
            "first_name": first, "last_name": last, "email": f"{first.lower()}.{last.lower()}@example.com",
            "phone": f"98{(45000000 + idx * 1371) % 100000000:08d}", "location": city, "current_company": company,
            "current_title": title, "total_experience": years, "skills": skills, "source": source,
            "current_ctc": int(j_sal(job) * 0.75), "expected_ctc": int(j_sal(job) * 0.95), "notice_days": 30 + (idx % 3) * 30,
            "education": "B.Tech", "tags": ["strong"] if idx % 5 == 0 else []},
            resume_text=text, consent={"text": "Demo consent", "ip": "127.0.0.1"})
        cand.created_at = now - timedelta(days=18 - (idx % 15))
        app = service.apply(db, company_id, job, cand, source=source, answers={"notice": "30", "relocate": "yes"},
                            auto_rules=False)
        app.applied_at = cand.created_at
        if source == "referral":
            from modules.recruitment.models import Referral
            db.add(Referral(company_id=company_id, referrer_employee_id=emps[(idx + 3) % len(emps)].id, candidate_id=cand.id,
                            application_id=app.id, job_id=job.id, bonus_amount=25000, bonus_status="in_process",
                            note="Worked with them before."))
        keys = {s["kind"]: s["key"] for s in job.stages}
        target = {"applied": "applied", "screening": "screening", "shortlisted": "shortlisted", "interview": "interview",
                  "offer": "offer", "hired_offer": "offer", "rejected": "screening"}[stage]
        if target != app.stage:
            service.move_stage(db, v, app, target)
        if stage == "rejected":
            service.reject(db, v, app, "Skills do not match")
        if stage in ("interview", "offer", "hired_offer"):
            panel = [hr.id, job.hiring_manager_id] if job.hiring_manager_id != hr.id else [hr.id]
            past = stage != "interview" or idx % 2 == 0
            when = (now - timedelta(days=3 + idx % 4)) if past else (now + timedelta(days=1 + idx % 4))
            when = datetime.combine(when.date(), time(5 + idx % 5, 0), tzinfo=timezone.utc)
            from modules.recruitment.models import Interview, Scorecard
            kit = kits.get(JOBS[ji]["kit"])
            iv = Interview(company_id=company_id, application_id=app.id, kit_id=kit.id if kit else None,
                           round_name="Technical round" if ji == 0 else ("HR round" if ji == 1 else "Role-play round"),
                           mode="video", starts_at=when, ends_at=when + timedelta(minutes=45), panel=panel,
                           meeting_link="https://meet.google.com/abc-defg-hij", status="completed" if past else "scheduled",
                           created_by_name="Demo")
            db.add(iv)
            db.flush()
            for n, eid in enumerate(panel):
                sc = Scorecard(company_id=company_id, interview_id=iv.id, application_id=app.id,
                               interviewer_employee_id=eid, interviewer_name=c.names_for(db, company_id, [eid]).get(eid))
                if past:
                    base = 4 if stage != "interview" else 3 + (idx % 2)
                    comps = (kit.competencies if kit else [])
                    sc.ratings = {cp["key"]: max(1, min(5, base + ((k + n) % 2))) for k, cp in enumerate(comps)}
                    sc.overall = base + (n % 2 if base < 5 else 0)
                    sc.recommendation = "strong_yes" if sc.overall >= 5 else ("yes" if sc.overall >= 4 else "no")
                    sc.strengths = "Clear thinking and good examples from past work."
                    sc.concerns = None if sc.overall >= 4 else "Needs more depth in the core area."
                    sc.submitted_at = when + timedelta(hours=2)
                db.add(sc)
            db.flush()
            service.evaluate(db, app)
        if stage in ("offer", "hired_offer"):
            offers_made.append((app, stage))
    db.flush()
    # Offers: one waiting for the candidate, one accepted → pre-boarding and onboarding
    for app, stage in offers_made:
        job = app.job
        ctc = int((float(job.salary_min) + float(job.salary_max)) / 2) if job.salary_min else 1200000
        o = service.save_offer(db, v, app, {"joining_date": t0 + timedelta(days=12 if stage == "hired_offer" else 21),
                                            "annual_ctc": ctc})
        service.submit_offer(db, v, o)
        try:
            with db.begin_nested():
                service.send_offer(db, v, o, "We enjoyed meeting you.")
        except Exception:
            o.status = "sent"
            o.access_token = c.token()
            o.expires_on = t0 + timedelta(days=7)
        if stage == "hired_offer":
            from modules.documents.models import EsignRequest
            if o.esign_request_id:
                req = db.get(EsignRequest, o.esign_request_id)
                if req is not None:
                    req.status = "completed"
                    if req.letter is not None:
                        req.letter.status = "signed"
                    for s in req.signers:
                        s.status, s.signed_at, s.signature_method, s.provider_ref = "signed", c.now(), "typed", s.name
            service.accept_offer(db, o, f"{app.candidate.first_name} {app.candidate.last_name}", "127.0.0.1")
    db.flush()
    _demo_people(db, company_id, emps, depts, locs, t0)


def j_sal(job) -> float:
    return float(job.salary_min or 800000)


def _demo_people(db: Session, company_id: int, emps, depts, locs, t0) -> None:
    """A second onboarding (joined last week), three on probation and two exits."""
    from modules.employees import service as people
    from modules.offboarding import service as offboarding
    from modules.onboarding import service as onboarding
    from modules.probation import service as probation
    from modules.recruitment import common as c
    mgr = next((e for e in emps if e.role == "manager"), emps[0])

    def make(first, last, dept, status, doj, **extra):
        E = c.Employee()
        email = f"{first.lower()}.{last.lower()}@example.com"
        have = db.query(E).filter(E.company_id == company_id, E.email == email).first()
        if have is not None:
            return have
        data = {"first_name": first, "last_name": last, "email": email, "status": status, "date_of_joining": doj,
                "department_id": depts.get(dept), "reporting_manager_id": mgr.id, "location_id": next(iter(locs.values()), None),
                "phone": f"97{abs(hash(email)) % 100000000:08d}", **extra}
        return people.create(db, company_id, {k: v for k, v in data.items() if v is not None})

    # onboarding no. 2: joined 5 days ago, on probation
    joined = make("Kabir", "Malhotra", "Engineering", "probation", t0 - timedelta(days=5),
                  probation_end_date=c.add_months(t0 - timedelta(days=5), 6) - timedelta(days=1), date_of_birth=t0.replace(year=t0.year - 27))
    o = onboarding.start(db, company_id, joined, send_invite=False)
    o.portal_submitted_at = c.now() - timedelta(days=8)
    o.review_status = "submitted"
    o.portal_data = {**(o.portal_data or {}),
                     "personal": {"first_name": "Kabir", "last_name": "Malhotra", "date_of_birth": "1999-04-12",
                                  "father_name": "Rajiv Malhotra", "personal_email": "kabir.m@gmail.com"},
                     "bank": {"account_holder": "Kabir Malhotra", "account_number": "50100234567891", "ifsc": "HDFC0001234"},
                     "statutory": {"pan": "ABCPM1234K", "uan": "100987654321"}, "tax": {"regime": "new"},
                     "emergency": {"name": "Rajiv Malhotra", "phone": "9876500011"},
                     "nominees": [{"kind": "pf", "name": "Rajiv Malhotra", "share_percent": "100"}]}
    probation.start_for(db, company_id, joined)
    # two more on probation: one ending soon (review under way), one mid-way
    near = make("Rohit", "Nair", "Sales", "probation", t0 - timedelta(days=172),
                probation_end_date=c.add_months(t0 - timedelta(days=172), 6) - timedelta(days=1))
    pn = probation.start_for(db, company_id, near)
    probation.start_review(db, pn)
    mid = make("Sneha", "Kulkarni", "Marketing", "probation", t0 - timedelta(days=60),
               probation_end_date=c.add_months(t0 - timedelta(days=60), 6) - timedelta(days=1))
    probation.start_for(db, company_id, mid)
    # two exits in progress
    leaver = make("Anil", "Kumar", "Product", "confirmed", t0 - timedelta(days=900), notice_period_days=30)
    x1 = offboarding.start_exit(db, company_id, leaver, "resignation",
                                {"reason_text": "Better opportunity", "resignation_date": t0 - timedelta(days=18),
                                 "replacement_needed": True}, raised_by="hr")
    for cl in x1.clearances[:2]:
        cl.status, cl.cleared_at, cl.cleared_by = "cleared", c.now(), "Demo"
        cl.items = [{**it, "done": True} for it in cl.items or []]
    retiree = make("Gopal", "Krishnan", "Finance", "confirmed", t0 - timedelta(days=9000),
                   date_of_birth=t0.replace(year=t0.year - 58) + timedelta(days=20))
    offboarding.start_exit(db, company_id, retiree, "retirement",
                           {"reason_text": "Superannuation at 58", "resignation_date": t0,
                            "last_working_day": t0 + timedelta(days=25)}, raised_by="system")
    db.flush()
