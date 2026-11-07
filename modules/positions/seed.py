"""Positions: number series come from the shared defaults (see hooks.py) and approval flows
from the request types. Demo data for the development company: positions built from the
current reporting lines with everyone seated, headcount plans, a pending position
request, succession plans with successors and development steps, talent pools, and
internal jobs and gigs with applications."""
from datetime import timedelta

from sqlalchemy.orm import Session

from modules.positions.models import (HeadcountPlan, InternalApplication, InternalPosting, Position, PositionSeat,
                                      SuccessionPlan, TalentPool)


def setup_company(db: Session, company_id: int) -> None:
    from core.numbering import ensure_default_series
    ensure_default_series(db, company_id)


def seed_demo(db: Session, company_id: int) -> None:
    from modules.employees.models import Employee
    from modules.positions import common as c
    if db.query(Position.id).filter(Position.company_id == company_id).first():
        return
    emps = (db.query(Employee).filter(Employee.company_id == company_id, Employee.active.is_(True),
                                      Employee.status.notin_(("exited", "archived", "pre_joining")),
                                      Employee.designation_id.isnot(None))
            .order_by(Employee.id).all())
    if len(emps) < 8:
        return  # people are seeded on a later start
    c.update_settings(db, company_id, {"enabled": True})
    _positions(db, company_id, emps)
    db.flush()
    _salary_bands(db, company_id)
    _plans(db, company_id)
    _succession(db, company_id, emps)
    _marketplace(db, company_id, emps)
    db.commit()


def _positions(db: Session, company_id: int, emps) -> None:
    from modules.departments.models import Department
    from modules.designations.models import Designation
    from modules.positions import common as c
    t = c.today(db, company_id)
    desig = {d.id: d.designation_name for d in db.query(Designation).filter(Designation.id.in_({e.designation_id for e in emps}))}
    by_emp: dict[int, Position] = {}
    groups: dict[tuple, Position] = {}
    n = 0
    ids = {e.id for e in emps}
    # Managers first so their positions exist before their reports'.
    order, seen = [], set()

    def visit(e):
        if e.id in seen:
            return
        seen.add(e.id)
        mgr = next((m for m in emps if m.id == e.reporting_manager_id), None)
        if mgr is not None:
            visit(mgr)
        order.append(e)
    for e in emps:
        visit(e)
    is_manager = {e.reporting_manager_id for e in emps if e.reporting_manager_id}
    for e in order:
        parent = by_emp.get(e.reporting_manager_id) if e.reporting_manager_id in ids else None
        key = (e.department_id, e.designation_id, parent.id if parent else None)
        pos = groups.get(key) if e.id not in is_manager else None
        if pos is None:
            n += 1
            pos = Position(company_id=company_id, code=f"POS-{n:04d}", title=desig.get(e.designation_id) or "Position",
                           department_id=e.department_id, designation_id=e.designation_id, location_id=e.location_id,
                           grade_id=e.grade_id, cost_centre_id=e.cost_centre_id, parent_id=parent.id if parent else None,
                           status="approved", headcount=0, created_by_name="Demo data",
                           is_critical=e.id in is_manager and (parent is None or parent.parent_id is None))
            db.add(pos)
            db.flush()
            if e.id not in is_manager:
                groups[key] = pos
        pos.headcount += 1
        by_emp[e.id] = pos
        start = max(e.date_of_joining or t - timedelta(days=400), t - timedelta(days=365))
        db.add(PositionSeat(company_id=company_id, position_id=pos.id, employee_id=e.id, start_date=start, status="active",
                            source="manual", created_by_name="Demo data"))
    db.flush()
    # A spare seat in two teams, and new vacant / budgeted positions.
    eng_mgr = next((by_emp[e.id] for e in emps if desig.get(e.designation_id) == "Engineering Manager"), None)
    sales = next((by_emp[e.id] for e in emps if desig.get(e.designation_id) == "Head of Sales"), None)
    hr = next((by_emp[e.id] for e in emps if desig.get(e.designation_id) == "HR Manager"), None)
    for p in groups.values():
        if p.title in ("Software Engineer", "Account Executive"):
            p.headcount += 1
    dept = {d.department_name: d.id for d in db.query(Department).filter(Department.company_id == company_id)}
    from modules.organization.models import Skill
    skill = {s.name: s.id for s in db.query(Skill).filter(Skill.company_id == company_id)}

    def extra(title, parent, dept_name, status="approved", headcount=1, skills=(), critical=False, grade=None):
        nonlocal n
        n += 1
        base = parent
        db.add(Position(company_id=company_id, code=f"POS-{n:04d}", title=title, parent_id=parent.id if parent else None,
                        department_id=dept.get(dept_name) or (base.department_id if base else None),
                        location_id=base.location_id if base else None, grade_id=grade or (base.grade_id - 1 if base and base.grade_id and base.grade_id > 1 else None),
                        status=status, headcount=headcount, is_critical=critical, created_by_name="Demo data",
                        skills=[{"skill_id": skill[s], "level": lvl} for s, lvl in skills if s in skill]))
    extra("Data Engineer", eng_mgr, "Platform", skills=[("Python", 4), ("SQL", 4), ("AWS", 3)])
    extra("Site Reliability Engineer", eng_mgr, "Platform", status="budgeted", skills=[("Kubernetes", 4), ("AWS", 4)])
    extra("Regional Sales Manager, Chennai", sales, "Sales", skills=[("B2B sales", 4), ("Negotiation", 4), ("Tamil", 3)])
    extra("HR Business Partner", hr, "Human Resources", skills=[("Stakeholder management", 4), ("Recruitment", 3)])
    db.flush()


BANDS = {"G1": (300000, 500000), "G2": (500000, 800000), "G3": (800000, 1400000), "G4": (1400000, 2200000),
         "G5": (2200000, 3500000), "G6": (3500000, 5500000), "G7": (5500000, 9000000)}


def _salary_bands(db: Session, company_id: int) -> None:
    """Demo yearly CTC bands by grade (from the grade when it has one)."""
    from modules.organization.models import Grade
    grades = {g.id: g for g in db.query(Grade).filter(Grade.company_id == company_id)}
    for p in db.query(Position).filter(Position.company_id == company_id, Position.salary_min.is_(None)):
        g = grades.get(p.grade_id)
        if g is None:
            continue
        lo, hi = (g.min_ctc, g.max_ctc) if g.min_ctc else BANDS.get(g.code or "", (None, None))
        p.salary_min, p.salary_max = lo, hi
    db.flush()


def _bump_series(db: Session, company_id: int) -> None:
    """Move the position code counter past the demo codes."""
    from core import numbering
    from modules.positions import common as c
    count = db.query(Position.id).filter(Position.company_id == company_id).count()
    for _ in range(count):
        numbering.next_number(db, company_id, "position", c.today(db, company_id))


def _plans(db: Session, company_id: int) -> None:
    from modules.positions import common as c
    from modules.positions import service as svc
    _bump_series(db, company_id)
    t = c.today(db, company_id)
    fy = c.fy_of(t)
    depts = {d for (d,) in db.query(Position.department_id).filter(Position.company_id == company_id,
                                                                   Position.department_id.isnot(None)).distinct()}
    for d in depts:
        seats = svc.department_seats(db, company_id, d)
        db.add(HeadcountPlan(company_id=company_id, department_id=d, financial_year=fy, planned_headcount=seats + 2,
                             approved_headcount=seats + 2, status="approved", created_by_name="Demo data",
                             notes="Approved in the annual operating plan.", decided_at=c.now()))
    eng = next(iter(sorted(depts)), None)
    nxt = f"{int(fy[:4]) + 1}-{(int(fy[:4]) + 2) % 100:02d}"
    if eng:
        db.add(HeadcountPlan(company_id=company_id, department_id=eng, financial_year=nxt,
                             planned_headcount=svc.department_seats(db, company_id, eng) + 4, status="draft",
                             created_by_name="Demo data", notes="Growth plan for next year, to be discussed."))
    db.flush()
    # A request waiting for approval.
    from modules.positions.models import PositionRequest
    from core.workflow import service as wf
    fin = next((p for p in db.query(Position).filter(Position.company_id == company_id, Position.title == "Finance Controller")), None)
    if fin is not None:
        req = PositionRequest(company_id=company_id, kind="new", status="pending", reason="Month-end close needs a second pair of hands.",
                              data={"title": "Financial Analyst", "department_id": fin.department_id, "parent_id": fin.id,
                                    "location_id": fin.location_id, "headcount": 1, "status": "approved"},
                              requested_by_name="Meenakshi Iyer", number="PRQ/demo/0001")
        db.add(req)
        db.flush()
        inst = wf.submit(db, company_id, "positions.request",
                         {"request_id": req.id, "kind": "new", "kind_label": "New position", "title": "Financial Analyst",
                          "department_id": fin.department_id, "department": "Finance", "headcount": 1,
                          "reason": req.reason}, "New position: Financial Analyst",
                         subject_type="position_request", subject_id=req.id)
        req.workflow_instance_id = inst.id
    db.flush()


def _succession(db: Session, company_id: int, emps) -> None:
    from modules.positions import succession as suc
    by_name = {f"{e.first_name} {e.last_name}": e for e in emps}
    seat_of = {s.employee_id: s.position_id for s in db.query(PositionSeat).filter(PositionSeat.company_id == company_id,
                                                                                    PositionSeat.status == "active")}
    plans = [
        ("Kavitha Raghavan", "medium", "high", "Leads all engineering; long notice period in the market.",
         [("Rahul Verma", "ready_now", "Runs the platform team well; owns delivery.", "Wider org leadership"),
          ("Ananya Reddy", "one_two_years", "Strong architect.", "People management"),
          ("Suresh Babu", "three_plus_years", "Deep infrastructure skills.", "Leading teams")]),
        ("Rohan Kulkarni", "high", "high", "Has had outside offers this year.",
         [("Manish Agarwal", "one_two_years", "Closes large deals.", "Forecasting and territory planning"),
          ("Lakshmi Narayanan", "three_plus_years", "Best negotiator on the team.", "Managing people")]),
        ("Meenakshi Iyer", "low", "high", None,
         [("Shalini Krishnan", "one_two_years", "Knows payroll and GST in depth.", "Statutory audit")]),
        ("Priya Sharma", "medium", "medium", None,
         [("Farah Khan", "ready_now", "Runs hiring end to end.", None),
          ("Deepak Chauhan", "three_plus_years", "Good with employee queries.", "Policy and labour law")]),
    ]
    courses = []
    try:
        from modules.learning.models import Course
        courses = db.query(Course).filter(Course.company_id == company_id).order_by(Course.id).all()
    except ImportError:
        pass
    for holder, risk, impact, notes, cands in plans:
        e = by_name.get(holder)
        if e is None or e.id not in seat_of:
            continue
        pos = db.get(Position, seat_of[e.id])
        pos.is_critical = True
        plan = SuccessionPlan(company_id=company_id, position_id=pos.id, risk_of_loss=risk, impact_of_loss=impact,
                              notes=notes, plan_manager_id=e.reporting_manager_id, created_by_name="Demo data")
        db.add(plan)
        db.flush()
        for rank, (name, readiness, strengths, gaps) in enumerate(cands, start=1):
            ce = by_name.get(name)
            if ce is None:
                continue
            x = suc.add_candidate(db, company_id, plan, {"employee_id": ce.id, "readiness": readiness, "rank": rank,
                                                         "strengths": strengths, "gaps": gaps})
            from modules.positions.models import DevelopmentAction
            if rank == 1:
                db.add(DevelopmentAction(company_id=company_id, employee_id=ce.id, candidate_id=x.id, kind="mentoring",
                                         title=f"Monthly mentoring with {holder.split()[0]}", mentor_id=e.id,
                                         status="in_progress", created_by_name="Demo data"))
            if rank == 2:
                db.add(DevelopmentAction(company_id=company_id, employee_id=ce.id, candidate_id=x.id, kind="project",
                                         title="Lead a cross-team project this quarter", status="planned",
                                         created_by_name="Demo data"))
            if courses and readiness != "ready_now":
                course = courses[(rank + 2) % len(courses)]
                db.add(DevelopmentAction(company_id=company_id, employee_id=ce.id, candidate_id=x.id, kind="course",
                                         title=course.title, course_id=course.id, status="planned", created_by_name="Demo data"))
    pools = [("High potentials", "People rated high on potential in the last review, groomed for bigger roles.",
              "Priya Sharma", ["Ananya Reddy", "Neha Gupta", "Farah Khan", "Manish Agarwal"]),
             ("Future leaders", "Managers ready to lead a function in the next two years.", "Arjun Mehta",
              ["Rahul Verma", "Vikram Nair", "Sneha Patil"])]
    for name, desc, mgr, members in pools:
        p = TalentPool(company_id=company_id, name=name, description=desc,
                       manager_employee_id=by_name[mgr].id if mgr in by_name else None)
        db.add(p)
        db.flush()
        suc.add_members(db, company_id, p, [by_name[m].id for m in members if m in by_name])
    db.flush()


def _marketplace(db: Session, company_id: int, emps) -> None:
    from modules.organization.models import Skill
    from modules.positions import common as c
    from modules.positions import marketplace as mkt
    t = c.today(db, company_id)
    by_name = {f"{e.first_name} {e.last_name}": e for e in emps}
    skill = {s.name: s.id for s in db.query(Skill).filter(Skill.company_id == company_id)}

    def sk(*pairs):
        return [{"skill_id": skill[s], "level": lvl} for s, lvl in pairs if s in skill]

    def eid(name):
        return by_name[name].id if name in by_name else None
    data_eng = db.query(Position).filter(Position.company_id == company_id, Position.title == "Data Engineer").first()
    hrbp = db.query(Position).filter(Position.company_id == company_id, Position.title == "HR Business Partner").first()
    rows = [
        dict(kind="job", title="Data Engineer", position_id=data_eng.id if data_eng else None,
             department_id=data_eng.department_id if data_eng else None, location_id=data_eng.location_id if data_eng else None,
             designation_id=None, skills=sk(("Python", 4), ("SQL", 4), ("AWS", 3)), hiring_manager_id=eid("Rahul Verma"),
             description="Build and run the data pipelines behind payroll and attendance analytics. Open to engineers who "
                         "want to move into data.", work_mode="hybrid", closes_on=t + timedelta(days=21)),
        dict(kind="job", title="HR Business Partner", position_id=hrbp.id if hrbp else None,
             department_id=hrbp.department_id if hrbp else None, location_id=hrbp.location_id if hrbp else None,
             skills=sk(("Stakeholder management", 4), ("Recruitment", 3), ("Excel", 3)), hiring_manager_id=eid("Priya Sharma"),
             description="Partner with the engineering and product leaders on hiring, reviews and team health.",
             work_mode="office", closes_on=t + timedelta(days=5)),
        dict(kind="gig", title="Build a sales dashboard in Power BI", skills=sk(("Power BI", 3), ("SQL", 3), ("Excel", 3)),
             hiring_manager_id=eid("Rohan Kulkarni"), duration_weeks=6, hours_per_week=6, work_mode="remote",
             description="Help the sales team see pipeline, wins and targets in one place. About a day a week for six weeks.",
             closes_on=t + timedelta(days=14), starts_on=t + timedelta(days=15)),
        dict(kind="gig", title="Campus hiring drive volunteers", skills=sk(("Public speaking", 3), ("Recruitment", 2)),
             hiring_manager_id=eid("Farah Khan"), duration_weeks=4, hours_per_week=4, openings=3, work_mode="office",
             description="Join the campus team: talks, interviews and stalls at three engineering colleges.",
             closes_on=t + timedelta(days=10)),
    ]
    postings = []
    for i, r in enumerate(rows, start=1):
        p = InternalPosting(company_id=company_id, number=f"IJP-{i:04d}", status="open", published_at=c.now(),
                            created_by_name="Demo data", needs_manager_consent=True, **r)
        db.add(p)
        postings.append(p)
    db.flush()
    from core import numbering
    for _ in postings:
        numbering.next_number(db, company_id, "internal_posting", t)

    def application(p, name, status, note=None):
        e = by_name.get(name)
        if e is None:
            return None
        score, detail = mkt.match_score(p.skills or [], mkt.skill_levels(db, company_id, e.id))
        a = InternalApplication(company_id=company_id, posting_id=p.id, employee_id=e.id, status=status, note=note,
                                match_score=score, match_detail=detail, manager_employee_id=e.reporting_manager_id)
        db.add(a)
        db.flush()
        return a
    application(postings[0], "Karthik Subramanian", "submitted", "I have been building our reporting queries for a year.")
    application(postings[0], "Ananya Reddy", "shortlisted", "Keen to own the data platform.")
    application(postings[2], "Priya Sharma", "submitted", "I use Excel and Power BI for HR reports every month.")
    application(postings[3], "Ritu Singh", "shortlisted")
    application(postings[3], "Deepak Chauhan", "selected")
    pending = application(postings[2], "Pooja Deshmukh", "manager_review", "Would love to learn more about sales.")
    if pending is not None:
        from core.workflow import service as wf
        e = by_name["Pooja Deshmukh"]
        inst = wf.submit(db, company_id, "positions.internal_application",
                         {"application_id": pending.id, "manager_id": e.reporting_manager_id, "employee_id": e.id,
                          "employee": "Pooja Deshmukh", "posting": postings[2].title, "kind": "gig",
                          "kind_label": "Gig or project", "match_score": float(pending.match_score or 0),
                          "duration_weeks": 6, "hours_per_week": 6},
                         f"Internal application: Pooja Deshmukh for {postings[2].title}",
                         subject_type="internal_application", subject_id=pending.id)
        pending.workflow_instance_id = inst.id
    db.flush()
