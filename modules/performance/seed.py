"""Company setup and demo data for performance (called by seed/seed_data.py).

setup_company(db, company_id)  a five-point rating scale, core competencies and two review
                               templates (annual and probation), and this year's goal periods.
seed_demo(db, company_id)      the demo company: an OKR tree for the current quarter with
                               check-ins, last year's published review (ratings, potential,
                               letters), an active mid-year review with some reviews
                               submitted, KRAs, one-to-ones, feedback, an improvement plan and
                               an open pay review. Each part runs only once.
"""
from __future__ import annotations

import logging
import random
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from core.context import RequestContext, use_context

log = logging.getLogger("hrms")

FIVE_POINT = [
    {"value": 1, "label": "Needs improvement", "description": "Well below what the role needs", "color": "rose"},
    {"value": 2, "label": "Partly meets", "description": "Meets some expectations; clear gaps", "color": "amber"},
    {"value": 3, "label": "Meets expectations", "description": "Solid, reliable performance", "color": "sky"},
    {"value": 4, "label": "Exceeds expectations", "description": "Often goes beyond the role", "color": "emerald"},
    {"value": 5, "label": "Outstanding", "description": "Exceptional impact, a role model", "color": "indigo"},
]

COMPETENCIES = [
    ("Communication", "core", "Shares information clearly and listens well.",
     ["Often unclear or misses key people", "Clear in routine situations", "Clear and well-timed with everyone",
      "Shapes how the team communicates", "Communicates complex ideas simply across the company"]),
    ("Ownership", "core", "Takes responsibility for outcomes, not just tasks.",
     ["Needs reminders to finish work", "Finishes own tasks", "Owns outcomes end to end",
      "Picks up gaps nobody owns", "Raises the bar for ownership around them"]),
    ("Collaboration", "core", "Works well with others across teams.",
     ["Works mostly alone", "Helps when asked", "Builds good working relationships",
      "Brings teams together", "Known as the person who connects teams"]),
    ("Customer focus", "core", "Keeps the customer at the centre of decisions.",
     ["Rarely thinks about the customer", "Responds to customer requests", "Anticipates customer needs",
      "Uses customer insight to change plans", "Champions the customer across the company"]),
    ("Problem solving", "core", "Gets to the root of problems and finds good solutions.",
     ["Struggles with new problems", "Solves familiar problems", "Breaks down new problems well",
      "Solves problems others get stuck on", "Solves the hardest problems and teaches others"]),
]


def _ctx(company_id: int, emp=None) -> RequestContext:
    if emp is None:
        return RequestContext(company_id=company_id, actor_type="owner", actor_name="Company owner", role="admin")
    return RequestContext(company_id=company_id, employee_id=emp.id, actor_type="employee", role=emp.role,
                          actor_name=f"{emp.first_name} {emp.last_name}".strip())


def setup_company(db: Session, company_id: int) -> None:
    from modules.performance.models import Competency, RatingScale, ReviewTemplate

    scale = db.query(RatingScale).filter(RatingScale.company_id == company_id).first()
    if scale is None:
        scale = RatingScale(company_id=company_id, name="Five-point scale", is_default=True, points=FIVE_POINT,
                            description="Used for reviews and competencies")
        db.add(scale)
        db.flush()
    if not db.query(Competency.id).filter(Competency.company_id == company_id).first():
        for name, cat, desc, levels in COMPETENCIES:
            db.add(Competency(company_id=company_id, name=name, category=cat, description=desc, rating_scale_id=scale.id,
                              weight=Decimal(1), behaviours=[{"value": i + 1, "text": t} for i, t in enumerate(levels)]))
    if not db.query(ReviewTemplate.id).filter(ReviewTemplate.company_id == company_id).first():
        db.add(ReviewTemplate(company_id=company_id, name="Annual review", rating_scale_id=scale.id,
                              description="Goals, KRAs, competencies and reflection",
                              sections=[
                                  {"key": "goals", "kind": "goals", "title": "Goals", "weight": 40, "questions": [],
                                   "description": "How the goals for the period went"},
                                  {"key": "kras", "kind": "kras", "title": "Key result areas", "weight": 20, "questions": []},
                                  {"key": "competencies", "kind": "competencies", "title": "Competencies", "weight": 25, "questions": []},
                                  {"key": "reflection", "kind": "questions", "title": "Reflection", "weight": 15, "questions": [
                                      {"id": "q1", "text": "What were the biggest achievements in this period?", "type": "text", "required": True},
                                      {"id": "q2", "text": "What should be the focus for the next period?", "type": "text", "required": True},
                                      {"id": "q3", "text": "Overall, how well were the expectations of the role met?", "type": "rating", "required": True},
                                  ]},
                              ]))
        db.add(ReviewTemplate(company_id=company_id, name="Probation review", rating_scale_id=scale.id,
                              description="Before confirming a new joiner",
                              sections=[
                                  {"key": "competencies", "kind": "competencies", "title": "Competencies", "weight": 60, "questions": []},
                                  {"key": "fit", "kind": "questions", "title": "Fit for the role", "weight": 40, "questions": [
                                      {"id": "p1", "text": "How quickly has the person become productive?", "type": "rating", "required": True},
                                      {"id": "p2", "text": "Recommend confirmation? Why?", "type": "text", "required": True},
                                  ]},
                              ]))
    db.flush()
    try:
        with db.begin_nested():
            from modules.performance import service
            service.daily(db, company_id)
    except Exception as exc:  # housekeeping catches up on the next start; never block start-up
        log.warning("Performance housekeeping failed for company %s: %s", company_id, exc)


# ── Demo data ───────────────────────────────────────────────────────

def _people(db: Session, company_id: int):
    from modules.employees.models import Employee
    emps = (db.query(Employee).filter(Employee.company_id == company_id, Employee.active.is_(True),
                                      Employee.department_id.isnot(None)).order_by(Employee.id).all())
    by_name = {f"{e.first_name} {e.last_name}": e for e in emps}
    return emps, by_name


def _actor(company_id: int, emp=None):
    from modules.performance.common import ADMIN, INCREMENTS, Actor
    if emp is None:
        return Actor(company_id, None, "admin", "Company owner", set())
    return Actor(company_id, emp.id, emp.role, f"{emp.first_name} {emp.last_name}", {ADMIN, INCREMENTS} if emp.role in ("admin", "hr") else set())


def seed_demo(db: Session, company_id: int) -> None:
    db.info["company_id"] = company_id
    emps, by = _people(db, company_id)
    if len(emps) < 8:
        return  # wait for the demo people (seeded later on the first start)
    rng = random.Random(42)
    with use_context(_ctx(company_id)):
        _seed_kras(db, company_id, emps)
        _seed_okrs(db, company_id, emps, by, rng)
        _seed_reviews(db, company_id, emps, by, rng)
        _seed_talent(db, company_id, emps, by, rng)
        _seed_pay_review(db, company_id, emps, by, rng)
    db.flush()


def _seed_kras(db, company_id, emps):
    from modules.departments.models import Department
    from modules.performance import library
    from modules.performance.models import Kra
    if db.query(Kra.id).filter(Kra.company_id == company_id).first():
        return
    deps = {d.department_name: d.id for d in db.query(Department).filter(Department.company_id == company_id).all()}
    items = [
        ("Delivery and quality", "Ship planned work on time with few defects", "Sprint commitments met, escaped defects", 40, ["Engineering", "Platform", "Mobile"]),
        ("Engineering excellence", "Clean design, reviews and documentation", "Review turnaround, design docs", 30, ["Engineering", "Platform", "Mobile"]),
        ("Reliability", "Keep services healthy and recover quickly", "Uptime, incident response time", 30, ["Platform", "Engineering"]),
        ("Revenue achievement", "Meet the quarterly revenue target", "Bookings against quota", 50, ["Sales"]),
        ("Pipeline health", "Build and keep a healthy pipeline", "Pipeline coverage 3x", 30, ["Sales"]),
        ("Customer relationships", "Keep accounts happy and growing", "Renewals and expansion", 20, ["Sales"]),
        ("Accurate books", "Close books on time with no errors", "Monthly close within 5 days", 50, ["Finance"]),
        ("Statutory compliance", "File PF, ESI, PT and TDS on time", "Zero late filings", 50, ["Finance", "Human Resources"]),
        ("Talent and engagement", "Hire well and keep people engaged", "Time to hire, engagement score", 50, ["Human Resources"]),
        ("Brand and demand", "Grow awareness and qualified leads", "MQLs, share of voice", 60, ["Marketing"]),
        ("Content quality", "Publish useful, on-brand content", "Content calendar delivered", 40, ["Marketing"]),
        ("Product discovery", "Understand users and shape the roadmap", "Interviews held, decisions logged", 50, ["Product"]),
        ("Product outcomes", "Ship features that move the metrics", "Adoption of shipped features", 50, ["Product"]),
    ]
    actor = _actor(company_id)
    for title, desc, measure, w, dnames in items:
        library.save_kra(db, actor, {"title": title, "description": desc, "measure": measure, "weight": w,
                                     "tags": {"department_ids": [deps[n] for n in dnames if n in deps]}})
    library.auto_assign_kras(db, company_id)


def _period(db, company_id, kind, at: date):
    from modules.performance import okr
    from modules.performance.models import GoalPeriod
    okr.ensure_periods(db, company_id)
    return (db.query(GoalPeriod).filter(GoalPeriod.company_id == company_id, GoalPeriod.kind == kind,
                                        GoalPeriod.start_date <= at, GoalPeriod.end_date >= at).first())


def _checkins(db, company_id, actor, obj, kr_vals: dict, start: date, today: date, rng):
    """Weekly check-ins from the start of the period to today that walk each key result
    to its current value; one week is skipped now and then (shows as a missed check-in)."""
    from modules.performance import okr
    from modules.performance.models import GoalCheckin
    weeks = []
    d = start + timedelta(days=7)
    while d <= today:
        weeks.append(d)
        d += timedelta(days=7)
    for kr in list(obj.key_results):
        final = kr_vals.get(kr.title)
        if final is None:
            continue
        skip = rng.choice([None, None, 2, 3])
        for i, wk in enumerate(weeks):
            if skip is not None and i == skip and i != len(weeks) - 1:
                continue
            frac = (i + 1) / len(weeks)
            data = {"key_result_uuid": str(kr.uuid), "checkin_date": wk, "confidence": rng.choice([6, 7, 7, 8, 8, 9])}
            if kr.kind in ("number", "percent"):
                s = float(kr.start_value)
                v = s + (float(final) - s) * frac + (rng.uniform(-0.04, 0.04) * (float(kr.target_value) - s) if i < len(weeks) - 1 else 0)
                data["value"] = round(v, 1)
            elif kr.kind == "boolean":
                data["value"] = bool(final) and i == len(weeks) - 1
            else:
                done_n = round(len(kr.milestones) * frac * float(final) / 100)
                data["milestones_done"] = [m["id"] for m in kr.milestones[:done_n]]
            if i == len(weeks) - 1:
                data["note"] = rng.choice([
                    "Good week. On track for the quarter.", "Slower than planned; unblocking next week.",
                    "Closed two big items; momentum is good.", "Waiting on another team; flagged in stand-up.",
                    "Steady progress, nothing blocking."])
            okr.checkin(db, actor, obj, data)
    db.flush()


def _seed_okrs(db, company_id, emps, by, rng):
    from modules.departments.models import Department
    from modules.performance import okr
    from modules.performance.common import today
    from modules.performance.models import Objective
    if db.query(Objective.id).filter(Objective.company_id == company_id).first():
        return
    t = today(db, company_id)
    q = _period(db, company_id, "quarter", t)
    if q is None:
        return
    owner = _actor(company_id)
    ceo = next((e for e in emps if e.reporting_manager_id is None), emps[0])
    deps = {d.department_name: d for d in db.query(Department).filter(Department.company_id == company_id).all()}

    def g(name, fallback=None):
        return by.get(name) or fallback

    def make(level, title, owner_emp, krs, parent=None, dep=None, weight=1, desc=None, period=None):
        p = period or q
        data = {"period_id": p.id, "level": level, "title": title, "description": desc,
                "owner_employee_id": owner_emp.id if owner_emp else None, "parent_id": parent.id if parent else None,
                "department_id": dep.id if dep else (owner_emp.department_id if owner_emp and level == "department" else None),
                "weight": weight, "key_results": []}
        finals = {}
        for k in krs:
            kd = {"title": k[0], "kind": k[1], "checkin_frequency": "weekly", "weight": k[5] if len(k) > 5 else 1}
            if k[1] in ("number", "percent"):
                kd.update({"start_value": k[2], "target_value": k[3], "current_value": k[2], "unit": k[6] if len(k) > 6 else None})
                finals[k[0]] = k[4]
            elif k[1] == "boolean":
                finals[k[0]] = k[4]
            else:
                kd["milestones"] = [{"title": m, "weight": 1} for m in k[2]]
                finals[k[0]] = k[4]
            data["key_results"].append(kd)
        obj = okr.create_objective(db, owner, data)
        db.flush()
        db.refresh(obj)
        if p.start_date <= t:
            _checkins(db, company_id, owner, obj, finals, p.start_date, min(t, p.end_date), rng)
        return obj

    c1 = make("company", "Grow revenue to ₹48 crore ARR", ceo, [
        ("Annual recurring revenue (₹ crore)", "number", 36, 48, 41.6, 2, "₹ cr"),
        ("New enterprise customers signed", "number", 0, 25, 11, 1, "customers"),
        ("Net revenue retention", "percent", 100, 110, 104.5, 1),
    ], desc="The number that matters most this quarter.")
    c2 = make("company", "Delight customers in every interaction", ceo, [
        ("Raise NPS from 42 to 55", "number", 42, 55, 47, 1, "NPS"),
        ("Median first response time (hours)", "number", 6, 2, 3.4, 1, "hours"),
        ("Launch the customer community", "milestone", ["Pick the platform", "Invite 50 founding members", "Public launch"], 34, 1),
    ])
    c3 = make("company", "Build a great place to work", ceo, [
        ("Regretted attrition (%)", "number", 14, 8, 11.2, 1, "%"),
        ("Employees who completed POSH training", "percent", 0, 100, 74, 1),
        ("Launch the career framework", "milestone", ["Draft levels for every function", "Review with managers", "Publish to everyone"], 67, 1),
    ])
    sales_head = deps.get("Sales") and by.get("Rohan Kulkarni")
    eng_head = by.get("Kavitha Raghavan")
    d_sales = make("department", "Build a predictable sales engine", sales_head or ceo, [
        ("Qualified pipeline (₹ crore)", "number", 8, 20, 13.5, 1, "₹ cr"),
        ("Win rate on qualified deals", "percent", 18, 28, 22, 1),
    ], parent=c1, dep=deps.get("Sales")) if deps.get("Sales") else None
    d_eng = make("department", "Ship the v3 platform reliably", eng_head or ceo, [
        ("Platform uptime", "percent", 99.5, 99.95, 99.9, 1),
        ("v3 features shipped", "number", 0, 12, 5, 1, "features"),
        ("Escaped defects per release", "number", 9, 3, 5, 1, "defects"),
    ], parent=c2, dep=deps.get("Engineering")) if deps.get("Engineering") else None
    if deps.get("Marketing"):
        make("department", "Generate 1,200 qualified leads", g("Sneha Patil", ceo), [
            ("Marketing qualified leads", "number", 0, 1200, 520, 2, "leads"),
            ("Webinars run", "number", 0, 6, 3, 1, "webinars"),
        ], parent=c1, dep=deps.get("Marketing"))
    d_hr = None
    if deps.get("Human Resources"):
        d_hr = make("department", "Make us a magnet for talent", g("Priya Sharma", ceo), [
            ("Offer acceptance rate", "percent", 70, 90, 81, 1),
            ("Average days to hire", "number", 45, 30, 38, 1, "days"),
            ("Mandatory training completion", "percent", 0, 100, 74, 1),
        ], parent=c3, dep=deps.get("Human Resources"))
    if deps.get("Product"):
        make("department", "Make onboarding self-serve", g("Vikram Nair", ceo), [
            ("Customers onboarded without help", "percent", 20, 60, 34, 1),
            ("Ship the setup wizard", "milestone", ["Research", "Design", "Beta", "General release"], 50, 1),
        ], parent=c2, dep=deps.get("Product"))
    if deps.get("Finance"):
        make("department", "Close the books faster", g("Meenakshi Iyer", ceo), [
            ("Days to close the month", "number", 9, 4, 6, 1, "days"),
            ("Statutory filings on time", "percent", 90, 100, 100, 1),
        ], parent=c1, dep=deps.get("Finance"))
    t_platform = None
    if by.get("Rahul Verma") and d_eng:
        t_platform = make("team", "Run the platform at 99.95%", by["Rahul Verma"], [
            ("p95 API latency (ms)", "number", 520, 300, 410, 1, "ms"),
            ("Incidents resolved within SLA", "percent", 70, 95, 88, 1),
        ], parent=d_eng)
    t_sales = None
    if by.get("Manish Agarwal") and d_sales:
        t_sales = make("team", "Close ₹4 crore of new business", by["Manish Agarwal"], [
            ("New business closed (₹ lakh)", "number", 0, 400, 165, 1, "₹ lakh"),
            ("Demos delivered", "number", 0, 60, 31, 1, "demos"),
        ], parent=d_sales)
    if by.get("Ananya Reddy") and t_platform:
        make("individual", "Make the API fast", by["Ananya Reddy"], [
            ("Endpoints under 300 ms", "number", 12, 40, 26, 1, "endpoints"),
            ("Cache layer live", "boolean", 0, 1, True, 1),
        ], parent=t_platform)
    if by.get("Karthik Subramanian") and t_platform:
        make("individual", "Automate deployments", by["Karthik Subramanian"], [
            ("Services on one-click deploy", "number", 3, 15, 9, 1, "services"),
        ], parent=t_platform)
    for nm, n in (("Lakshmi Narayanan", 6), ("Imran Shaikh", 6)):
        if by.get(nm) and t_sales:
            make("individual", f"Close {n} new accounts", by[nm], [
                ("New accounts closed", "number", 0, n, 3 if nm.startswith("L") else 1, 2, "accounts"),
                ("Pipeline added (₹ lakh)", "number", 0, 120, 70 if nm.startswith("L") else 30, 1, "₹ lakh"),
            ], parent=t_sales)
    if by.get("Priya Sharma") and d_hr:
        make("individual", "Roll out the new review process", by["Priya Sharma"], [
            ("Train every manager on reviews", "percent", 0, 100, 60, 1),
            ("Review templates live", "milestone", ["Draft templates", "Pilot with Engineering", "Company-wide launch"], 67, 1),
        ], parent=d_hr)
    if by.get("Farah Khan") and d_hr:
        make("individual", "Hire 12 engineers this quarter", by["Farah Khan"], [
            ("Engineers hired", "number", 0, 12, 5, 2, "hires"),
            ("Candidate NPS", "number", 30, 60, 48, 1, "NPS"),
        ], parent=d_hr)
    if by.get("Deepak Chauhan") and d_hr:
        make("individual", "Make induction great", by["Deepak Chauhan"], [
            ("New joiners inducted in week one", "percent", 60, 100, 92, 1),
            ("Induction feedback score", "number", 3.5, 4.5, 4.1, 1, "/5"),
        ], parent=d_hr)
    # last half-year: finished goals for the mid-year review
    h1 = _period(db, company_id, "half", t - timedelta(days=130))
    if h1 is not None:
        for nm, title, krs in (
            ("Priya Sharma", "Launch the HR helpdesk", [("Helpdesk live for all employees", "boolean", 0, 1, True, 1),
                                                         ("Tickets closed within 2 days", "percent", 40, 90, 84, 1)]),
            ("Deepak Chauhan", "Digitise employee files", [("Files scanned and tagged", "percent", 0, 100, 100, 1)]),
            ("Farah Khan", "Build the campus hiring programme", [("Campus offers made", "number", 0, 20, 17, 1, "offers")]),
            ("Ananya Reddy", "Move search to the new index", [("Search traffic on new index", "percent", 0, 100, 100, 1),
                                                               ("Search p95 latency (ms)", "number", 900, 250, 310, 1, "ms")]),
            ("Karthik Subramanian", "Set up observability", [("Services with dashboards", "number", 2, 20, 18, 1, "services")]),
            ("Lakshmi Narayanan", "Hit the H1 quota", [("Bookings (₹ lakh)", "number", 0, 150, 162, 1, "₹ lakh")]),
            ("Divya Menon", "Ship the Android revamp", [("Revamp milestones", "milestone", ["Design", "Build", "Beta", "Store release"], 100, 1)]),
            ("Siddharth Joshi", "Cut app crash rate", [("Crash-free sessions", "percent", 97, 99.5, 99.1, 1)]),
        ):
            if by.get(nm):
                make("individual", title, by[nm], krs, period=h1)
    okr.refresh_statuses(db, company_id)


def _fill_form(db, company_id, form, rating_centre: float, rng, actor, summary: str, submit: bool = True):
    from modules.performance import reviews
    d = reviews.form_detail(db, actor, form)
    items = {}
    for s in d["sections"]:
        for it in s["items"]:
            key = f"{s['key']}:{it['ref']}"
            if it.get("type") == "text":
                items[key] = {"comment": rng.choice([
                    "Led the quarter's key launch and mentored two new joiners.",
                    "Delivered steadily; improved how the team plans its work.",
                    "Took on a stretch project and learned a lot from it.",
                    "Focus next on cross-team collaboration and documentation.",
                    "Keep building depth in the core product and take on a lead role."])}
            else:
                base = it.get("suggested") or rating_centre
                r = min(5, max(1, round(base + rng.choice([-1, 0, 0, 0, 1]) * 0.6)))
                items[key] = {"rating": float(r), "comment": rng.choice(["", "Good progress.", "Clear impact here.", "Room to grow.", ""])}
    reviews.save_form(db, actor, form, {"items": items, "summary": summary}, submit=submit)


def _seed_reviews(db, company_id, emps, by, rng):
    from modules.performance import reviews
    from modules.performance.common import today
    from modules.performance.models import GoalPeriod, ReviewCycle, ReviewForm, ReviewParticipant, ReviewTemplate
    if db.query(ReviewCycle.id).filter(ReviewCycle.company_id == company_id).first():
        return
    tmpl = db.query(ReviewTemplate).filter(ReviewTemplate.company_id == company_id, ReviewTemplate.name == "Annual review").first()
    if tmpl is None:
        return
    owner = _actor(company_id)
    t = today(db, company_id)
    emp_by_id = {e.id: e for e in emps}
    dep_ids = sorted({e.department_id for e in emps if e.department_id})
    rule = {"department_ids": dep_ids}
    # 1. last year's annual review, published
    fy = t.year if t.month >= 4 else t.year - 1
    a = reviews.save_cycle(db, owner, {
        "name": f"Annual review FY{str(fy - 1)[2:]}-{str(fy)[2:]}", "kind": "annual", "template_id": tmpl.id,
        "period_start": date(fy - 1, 4, 1), "period_end": date(fy, 3, 31), "participant_rule": rule,
        "stages": [{"key": "self", "enabled": True, "deadline": f"{fy}-04-15"},
                   {"key": "manager", "enabled": True, "deadline": f"{fy}-04-25"},
                   {"key": "calibration", "enabled": True, "deadline": f"{fy}-05-05"}],
        "weightage": {"self": 20, "manager": 80},
        "settings": {"calibration": {"enabled": True, "require_ack": True, "require_reason": True,
                                     "distribution": {"1": 5, "2": 15, "3": 50, "4": 20, "5": 10}}},
    })
    reviews.launch(db, owner, a)
    talent = {e.id: rng.choice([2.4, 3, 3, 3, 3.4, 3.6, 4, 4.3]) for e in emps}
    for p in db.query(ReviewParticipant).filter(ReviewParticipant.company_id == company_id, ReviewParticipant.cycle_id == a.id).all():
        for f in p.forms:
            rev = emp_by_id.get(f.reviewer_employee_id)
            with use_context(_ctx(company_id, rev)):
                actor = _actor(company_id, rev)
                bias = 0.3 if f.reviewer_type == "self" else 0
                _fill_form(db, company_id, f, talent[p.employee_id] + bias, rng, actor,
                           "A strong year with clear growth." if talent[p.employee_id] >= 3.4 else "A steady year; let's work on a few focus areas.")
        p.potential = 3 if talent[p.employee_id] >= 4 else (2 if talent[p.employee_id] >= 3 else rng.choice([1, 2]))
    db.flush()
    reviews.set_status(db, owner, a, "calibration")
    parts = db.query(ReviewParticipant).filter(ReviewParticipant.company_id == company_id, ReviewParticipant.cycle_id == a.id).all()
    for p in parts[:3]:
        if p.proposed_rating is not None:
            new = min(5.0, float(p.proposed_rating) + 1) if float(p.proposed_rating) < 4 else float(p.proposed_rating) - 1
            reviews.calibrate(db, owner, p, new, "Adjusted after comparing with peers in the same role.")
            reviews.acknowledge(db, owner, p, True, "Agreed after discussion.")
    a.published_at = None
    reviews.publish(db, owner, a)
    a.launched_at = datetime(fy, 4, 1, 4, 30, tzinfo=timezone.utc)
    a.published_at = datetime(fy, 5, 10, 6, 0, tzinfo=timezone.utc)
    for p in parts:
        p.published_at = a.published_at
        if rng.random() < 0.5 and p.final_rating is not None:
            p.response, p.response_note = "agree", None
    reviews.set_status(db, owner, a, "closed")

    # 2. this year's mid-year review, running now
    h1 = (db.query(GoalPeriod).filter(GoalPeriod.company_id == company_id, GoalPeriod.kind == "half",
                                      GoalPeriod.start_date == date(fy, 4, 1)).first())
    b = reviews.save_cycle(db, owner, {
        "name": f"Mid-year review FY{str(fy)[2:]}-{str(fy + 1)[2:]}", "kind": "half_yearly", "template_id": tmpl.id,
        "goal_period_id": h1.id if h1 else None,
        "period_start": date(fy, 4, 1), "period_end": date(fy, 9, 30), "participant_rule": rule,
        "stages": [{"key": "self", "enabled": True, "deadline": (t + timedelta(days=5)).isoformat()},
                   {"key": "peer", "enabled": True, "deadline": (t + timedelta(days=9)).isoformat()},
                   {"key": "manager", "enabled": True, "deadline": (t + timedelta(days=15)).isoformat()},
                   {"key": "skip", "enabled": True, "deadline": (t + timedelta(days=18)).isoformat()},
                   {"key": "calibration", "enabled": True, "deadline": (t + timedelta(days=24)).isoformat()}],
        "weightage": {"self": 10, "peer": 15, "manager": 65, "skip": 10},
        "settings": {"peer": {"min": 1, "max": 3, "nominate_by": "employee", "needs_approval": True},
                     "calibration": {"enabled": True, "require_ack": True, "require_reason": True,
                                     "distribution": {"1": 5, "2": 15, "3": 50, "4": 20, "5": 10}}},
    })
    reviews.launch(db, owner, b)
    b.launched_at = datetime.now(timezone.utc) - timedelta(days=12)
    parts = (db.query(ReviewParticipant).filter(ReviewParticipant.company_id == company_id, ReviewParticipant.cycle_id == b.id)
             .order_by(ReviewParticipant.id).all())
    priya = by.get("Priya Sharma")
    for i, p in enumerate(parts):
        emp = emp_by_id.get(p.employee_id)
        if priya and p.employee_id == priya.id:
            continue  # Priya still has her own self review to do
        self_form = next((f for f in p.forms if f.reviewer_type == "self"), None)
        done_self = rng.random() < 0.7
        if self_form and done_self:
            with use_context(_ctx(company_id, emp)):
                _fill_form(db, company_id, self_form, talent[p.employee_id] + 0.3, rng, _actor(company_id, emp),
                           "I am proud of what I delivered this half.")
        # peers: chosen by the employee, some approved and answered
        peers = [e for e in emps if e.department_id == emp.department_id and e.id not in (emp.id, p.manager_employee_id)]
        if peers and rng.random() < 0.6:
            chosen = rng.sample(peers, min(2, len(peers)))
            with use_context(_ctx(company_id, emp)):
                reviews.nominate_peers(db, _actor(company_id, emp), p, [c.id for c in chosen])
            for f in [f for f in p.forms if f.reviewer_type == "peer"]:
                if rng.random() < 0.7:
                    reviews.decide_peer(db, owner, f, True)
                    if rng.random() < 0.5:
                        rev = emp_by_id.get(f.reviewer_employee_id)
                        with use_context(_ctx(company_id, rev)):
                            _fill_form(db, company_id, f, talent[p.employee_id], rng, _actor(company_id, rev), "Great to work with.")
        mgr_form = next((f for f in p.forms if f.reviewer_type == "manager"), None)
        if mgr_form and done_self and rng.random() < 0.5 and mgr_form.reviewer_employee_id and not (priya and mgr_form.reviewer_employee_id == priya.id):
            rev = emp_by_id.get(mgr_form.reviewer_employee_id)
            with use_context(_ctx(company_id, rev)):
                _fill_form(db, company_id, mgr_form, talent[p.employee_id], rng, _actor(company_id, rev),
                           "Good half. Keep the momentum going into H2.")
    db.flush()


def _seed_talent(db, company_id, emps, by, rng):
    from modules.performance import talent
    from modules.performance.common import today
    from modules.performance.models import Feedback, OneOnOne, Pip
    t = today(db, company_id)
    now = datetime.now(timezone.utc)

    def at(days: int, hour_utc: int = 5):
        d = t + timedelta(days=days)
        return datetime.combine(d, time(hour_utc, 0), tzinfo=timezone.utc)

    if not db.query(OneOnOne.id).filter(OneOnOne.company_id == company_id).first():
        pairs = []
        for mgr_name, emp_name in (("Arjun Mehta", "Priya Sharma"), ("Priya Sharma", "Deepak Chauhan"),
                                   ("Priya Sharma", "Farah Khan"), ("Kavitha Raghavan", "Divya Menon"),
                                   ("Rahul Verma", "Ananya Reddy"), ("Manish Agarwal", "Imran Shaikh")):
            if by.get(mgr_name) and by.get(emp_name):
                pairs.append((by[mgr_name], by[emp_name]))
        for i, (m, e) in enumerate(pairs):
            actor = _actor(company_id, m)
            with use_context(_ctx(company_id, m)):
                past = talent.create_meeting(db, actor, {"employee_id": e.id, "title": f"{m.first_name} / {e.first_name} weekly",
                                                         "scheduled_at": at(-7 + i % 3), "duration_minutes": 30,
                                                         "recurrence": "weekly",
                                                         "agenda": ["Wins since last week", "Blockers", "Goals check-in"]})
                talent.update_meeting(db, actor, past, {
                    "shared_notes": "Good progress on the quarter's goals. Agreed to bring the hiring plan forward.",
                    "agenda": [{**a, "done": True} for a in past.agenda],
                    "action_items": [{"text": "Share the updated plan by Friday", "done": False},
                                     {"text": "Book a skip-level chat", "done": True}]})
                talent.save_private_note(db, actor, past, "Seems stretched this month; check workload next time.")
                nxt = talent.complete_meeting(db, actor, past)
                if nxt is not None:
                    nxt.agenda = [*(nxt.agenda or []), {"id": f"x{i}", "text": "Mid-year review: self review progress",
                                                         "by": f"{e.first_name} {e.last_name}", "done": False}]
    if not db.query(Feedback.id).filter(Feedback.company_id == company_id).first():
        items = [
            ("Kavitha Raghavan", "Rahul Verma", "praise", "The way you ran the v3 incident review was calm and clear. The whole team learned from it.", "manager"),
            ("Ananya Reddy", "Karthik Subramanian", "praise", "Thanks for pairing on the cache rollout. It went out without a hitch.", "private"),
            ("Rohan Kulkarni", "Lakshmi Narayanan", "praise", "Brilliant close on the Sharma Textiles deal. The discovery notes were top class.", "manager"),
            ("Priya Sharma", "Farah Khan", "suggestion", "Candidates love the process. Share interview feedback within 24 hours to keep that going.", "private"),
            ("Arjun Mehta", "Priya Sharma", "praise", "The review templates are clear and simple. Managers are already using them well.", "manager"),
            ("Sneha Patil", "Tanvi Bhat", "feedback", "Your campaign drafts are getting sharper. Next: tie each one to a lead target.", "private"),
            ("Deepak Chauhan", "Priya Sharma", "praise", "Thank you for backing the new induction plan in the leadership meeting.", "private"),
        ]
        for g, r, kind, msg, vis in items:
            if by.get(g) and by.get(r):
                with use_context(_ctx(company_id, by[g])):
                    talent.give_feedback(db, _actor(company_id, by[g]), {"receiver_employee_id": by[r].id, "message": msg,
                                                                        "kind": kind, "visibility": vis})
        if by.get("Deepak Chauhan") and by.get("Priya Sharma") and by.get("Farah Khan"):
            with use_context(_ctx(company_id, by["Deepak Chauhan"])):
                talent.request_feedback(db, _actor(company_id, by["Deepak Chauhan"]), {
                    "giver_employee_ids": [by["Priya Sharma"].id, by["Farah Khan"].id],
                    "prompt": "How did the October induction week go from your side? What should I change?"})
        if by.get("Priya Sharma") and by.get("Arjun Mehta"):
            with use_context(_ctx(company_id, by["Priya Sharma"])):
                talent.request_feedback(db, _actor(company_id, by["Priya Sharma"]), {
                    "giver_employee_ids": [by["Arjun Mehta"].id],
                    "prompt": "How can I make the leadership HR updates more useful?"})
    if not db.query(Pip.id).filter(Pip.company_id == company_id).first():
        e = by.get("Imran Shaikh")
        if e:
            mgr = by.get("Manish Agarwal")
            with use_context(_ctx(company_id, mgr)):
                actor = _actor(company_id, mgr) if mgr else _actor(company_id)
                p = talent.save_pip(db, _actor(company_id), {
                    "employee_id": e.id, "reason": "Bookings have been below 50% of quota for two quarters and pipeline hygiene needs work.",
                    "start_date": t - timedelta(days=24), "end_date": t + timedelta(days=66),
                    "support": "Weekly deal reviews with Manish, call shadowing with Lakshmi, CRM refresher course.",
                    "goals": [{"title": "Close 3 new accounts", "measure": "Signed contracts in CRM", "due_date": (t + timedelta(days=60)).isoformat()},
                              {"title": "Keep pipeline at 3x quota", "measure": "Weekly pipeline report", "due_date": (t + timedelta(days=30)).isoformat()},
                              {"title": "Update every deal within 24 hours of a call", "measure": "CRM audit", "due_date": (t + timedelta(days=14)).isoformat(), "status": "met"}]})
                for days, rating, note in ((-17, "some_progress", "Pipeline up to 2.1x. CRM notes much better."),
                                           (-3, "on_track", "Two proposals out; one verbal yes. Keep going.")):
                    talent.pip_checkin(db, _actor(company_id), p, {"checkin_date": t + timedelta(days=days), "rating": rating, "note": note})
                p.acknowledged_at = now - timedelta(days=22)
    db.flush()


CTC = {
    "Chief Executive Officer": 6000000, "VP Engineering": 4800000, "Head of Sales": 3600000, "Finance Controller": 2600000,
    "HR Manager": 2200000, "Product Manager": 2800000, "Marketing Manager": 2000000, "Engineering Manager": 3400000,
    "Sales Manager": 2100000, "Senior Software Engineer": 2400000, "Product Designer": 1600000, "DevOps Engineer": 1800000,
    "Software Engineer": 1400000, "Mobile Developer": 1500000, "QA Engineer": 1000000, "Account Executive": 900000,
    "Content Strategist": 850000, "Accountant": 700000, "Payroll Specialist": 750000, "Talent Acquisition Specialist": 800000,
    "HR Executive": 600000, "Marketing Intern": 300000,
}


def _seed_pay_review(db, company_id, emps, by, rng):
    from modules.designations.models import Designation
    from modules.performance import increments
    from modules.performance.common import today
    from modules.performance.models import IncrementCycle, IncrementProposal, ReviewCycle
    if db.query(IncrementCycle.id).filter(IncrementCycle.company_id == company_id).first():
        return
    last = (db.query(ReviewCycle).filter(ReviewCycle.company_id == company_id, ReviewCycle.status == "closed")
            .order_by(ReviewCycle.id).first())
    owner = _actor(company_id)
    t = today(db, company_id)
    desig = {d.id: d.designation_name for d in db.query(Designation).filter(
        (Designation.company_id == company_id) | (Designation.company_id.is_(None))).all()}
    ranges = {}
    for did, name in desig.items():
        base = CTC.get(name)
        if base:
            ranges[str(did)] = {"min": base * 0.8, "mid": base * 1.05, "max": base * 1.3}
    c = increments.save_cycle(db, owner, {
        "name": f"Annual pay review {t.year}", "effective_date": date(t.year, 12, 1) if t.month < 12 else date(t.year + 1, 1, 1),
        "review_cycle_id": last.id if last else None,
        "guidelines": {"bands": [{"label": "Below 0.9", "min": None, "max": 0.9}, {"label": "0.9 – 1.1", "min": 0.9, "max": 1.1},
                                 {"label": "Above 1.1", "min": 1.1, "max": None}],
                       "rows": [{"rating": 5, "pcts": [16, 13, 10]}, {"rating": 4, "pcts": [12, 10, 8]},
                                {"rating": 3, "pcts": [9, 7, 5]}, {"rating": 2, "pcts": [4, 3, 2]}, {"rating": 1, "pcts": [0, 0, 0]}]},
        "pay_ranges": ranges,
    })
    increments.open_cycle(db, owner, c)
    for p in db.query(IncrementProposal).filter(IncrementProposal.company_id == company_id, IncrementProposal.cycle_id == c.id).all():
        base = CTC.get(desig.get(p.current_designation_id), 800000)
        p.current_ctc = Decimal(str(round(base * rng.uniform(0.85, 1.2), -3)))
        increments._guide(c, p, p.current_designation_id)
        p.proposed_pct = p.guideline_pct or Decimal(0)
        increments._recalc(p)
    db.flush()
    from modules.departments.models import Department
    budgets = []
    for d in db.query(Department).filter(Department.company_id == company_id).all():
        budgets.append({"department_id": d.id, "budget_pct": 8.5})
    increments.set_budgets(db, owner, c, budgets)
    kav = by.get("Kavitha Raghavan")
    if kav:
        from modules.performance.common import reportee_ids
        team = reportee_ids(db, company_id, kav.id)
        rows = db.query(IncrementProposal).filter(IncrementProposal.company_id == company_id, IncrementProposal.cycle_id == c.id,
                                                  IncrementProposal.employee_id.in_(team or {0})).all()
        if rows:
            star = max(rows, key=lambda r: float(r.rating or 0))
            if by.get("Rahul Verma") and star.employee_id == by["Rahul Verma"].id:
                pass
            star.justification = "Consistently top performer; leads the platform team."
            with use_context(_ctx(company_id, kav)):
                increments.submit(db, _actor(company_id, kav), c, [str(r.uuid) for r in rows])
    db.flush()
