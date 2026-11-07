"""Positions, succession and the internal job marketplace: seat control and job sync,
headcount plan limits through approval, position requests, succession readiness and bench
strength, the internal application flow with manager consent and the move, permissions and
tenant isolation."""
from modules.positions import marketplace as mkt
from modules.positions import succession as suc
from tests.conftest import login_as


# ── helpers ─────────────────────────────────────────────────────────

def _enable(company, **extra):
    r = company.put("/positions/settings", json={"enabled": True, "min_months_in_role": 0, **extra})
    assert r.status_code == 200, r.text


def _dept(company, name):
    r = company.post("/departments/", json={"department_name": name})
    assert r.status_code in (200, 201), r.text
    return r.json()


def _position(company, title, **extra):
    r = company.post("/positions/positions", json={"title": title, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def _decide_inbox(session, request_type, decision="approve"):
    items = [t for t in session.get("/workflow/inbox").json()["items"] if t["request"]["request_type"] == request_type]
    assert items, f"no {request_type} task"
    r = session.post(f"/workflow/tasks/{items[0]['uuid']}/decision", json={"decision": decision, "comment": "ok"})
    assert r.status_code == 200, r.text
    return r.json()


def _approve_all(session, request_type):
    for _ in range(4):
        items = [t for t in session.get("/workflow/inbox").json()["items"] if t["request"]["request_type"] == request_type]
        if not items:
            return
        session.post(f"/workflow/tasks/{items[0]['uuid']}/decision", json={"decision": "approve", "comment": "ok"})


# ── seat control ────────────────────────────────────────────────────

def test_seat_control_and_job_sync(company_a):
    _enable(company_a)
    eng = _dept(company_a, "Engineering")
    boss = company_a.add_employee("Kavya", "Head")
    lead = _position(company_a, "Engineering Head", department_id=eng["id"], headcount=1)
    r = company_a.post(f"/positions/positions/{lead['uuid']}/seats", json={"employee_id": boss["id"]})
    assert r.status_code == 201, r.text

    dev = _position(company_a, "Developer", department_id=eng["id"], parent_id=lead["id"], headcount=1)
    assert dev["code"] and dev["vacant"] == 1 and dev["fill_status"] == "vacant"
    a = company_a.add_employee("Asha", "Dev")
    b = company_a.add_employee("Bala", "Dev")
    r = company_a.post(f"/positions/positions/{dev['uuid']}/seats", json={"employee_id": a["id"], "reason": "New team"})
    assert r.status_code == 201, r.text
    assert r.json()["job_change"] is True
    pos = r.json()["position"]
    assert pos["filled"] == 1 and pos["vacant"] == 0 and pos["fill_status"] == "filled"

    # The person's record follows the position: department and manager (the reports-to incumbent).
    prof = company_a.get(f"/employees/{a['uuid']}").json()
    assert prof["department_id"] == eng["id"]
    assert prof["reporting_manager_id"] == boss["id"]

    # No free seat: refused, until the headcount goes up.
    r = company_a.post(f"/positions/positions/{dev['uuid']}/seats", json={"employee_id": b["id"]})
    assert r.status_code == 400 and "No seats" in r.json()["detail"]
    r = company_a.put(f"/positions/positions/{dev['uuid']}", json={"headcount": 2})
    assert r.status_code == 200 and r.json()["vacant"] == 1
    assert company_a.post(f"/positions/positions/{dev['uuid']}/seats", json={"employee_id": b["id"]}).status_code == 201
    # Can't cut seats below the people in them, nor close while occupied.
    r = company_a.put(f"/positions/positions/{dev['uuid']}", json={"headcount": 1})
    assert r.status_code == 400 and "at least 2 seats" in r.json()["detail"]
    assert company_a.post(f"/positions/positions/{dev['uuid']}/close", json={"reason": "x"}).status_code == 400
    # A position can't report to one of its own reports.
    r = company_a.put(f"/positions/positions/{lead['uuid']}", json={"parent_id": dev["id"]})
    assert r.status_code == 400 and "report to itself" in r.json()["detail"]
    # The same person twice is refused; moving someone ends their old seat.
    assert company_a.post(f"/positions/positions/{dev['uuid']}/seats", json={"employee_id": a["id"]}).status_code == 400
    qa = _position(company_a, "QA", department_id=eng["id"], parent_id=lead["id"])
    assert company_a.post(f"/positions/positions/{qa['uuid']}/seats", json={"employee_id": a["id"]}).status_code == 201
    dev_now = company_a.get(f"/positions/positions/{dev['uuid']}").json()
    assert dev_now["filled"] == 1 and dev_now["vacant"] == 1

    # Vacant picker and the tree with roll-ups.
    vac = company_a.get("/positions/vacant").json()
    assert vac["enabled"] and dev["uuid"] in [p["uuid"] for p in vac["items"]]
    tree = company_a.get("/positions/tree").json()["items"]
    root = next(p for p in tree if p["uuid"] == lead["uuid"])
    assert root["root"] and root["below"] == 2 and root["below_filled"] == 2

    # Releasing a seat frees it; budgeted positions can't be filled.
    seat = next(i for i in dev_now["incumbents"])
    assert company_a.post(f"/positions/seats/{seat['seat_uuid']}/release", json={"reason": "Left team"}).status_code == 200
    plan = _position(company_a, "Planned role", status="budgeted")
    r = company_a.post(f"/positions/positions/{plan['uuid']}/seats", json={"employee_id": b["id"]})
    assert r.status_code == 400 and "approved" in r.json()["detail"]


def test_hiring_request_from_vacant_position(company_a):
    _enable(company_a)
    p = _position(company_a, "Support Engineer", headcount=2, salary_min=400000, salary_max=600000)
    r = company_a.post(f"/positions/positions/{p['uuid']}/hiring-request", json={"openings": 3})
    assert r.status_code == 400 and "Only 2" in r.json()["detail"]
    r = company_a.post(f"/positions/positions/{p['uuid']}/hiring-request", json={"openings": 2})
    assert r.status_code == 201, r.text
    detail = company_a.get(f"/positions/positions/{p['uuid']}").json()
    assert len(detail["hiring_requests"]) == 1 and detail["hiring_requests"][0]["openings"] == 2
    # Every vacant seat now has a request.
    r = company_a.post(f"/positions/positions/{p['uuid']}/hiring-request", json={"openings": 1})
    assert r.status_code == 400


# ── headcount limits and position requests ──────────────────────────

def test_headcount_plan_limits_and_requests(company_a):
    _enable(company_a)
    sales = _dept(company_a, "Sales")
    meta = company_a.get("/positions/meta").json()
    fy = meta["financial_year"]
    r = company_a.post("/positions/headcount-plans", json={"department_id": sales["id"], "financial_year": fy,
                                                           "planned_headcount": 3, "submit": True})
    assert r.status_code == 201, r.text
    assert r.json()["status"] in ("pending", "approved")  # the owner may approve their own plan at once
    # Duplicate plan for the same year is refused.
    assert company_a.post("/positions/headcount-plans", json={"department_id": sales["id"], "financial_year": fy,
                                                              "planned_headcount": 5}).status_code == 400
    _approve_all(company_a, "positions.headcount_plan")
    plan = next(p for p in company_a.get("/positions/headcount-plans").json()["items"] if p["department_id"] == sales["id"])
    assert plan["status"] == "approved" and plan["approved_headcount"] == 3

    _position(company_a, "Account Executive", department_id=sales["id"], headcount=2)
    r = company_a.post("/positions/positions", json={"title": "Sales Lead", "department_id": sales["id"], "headcount": 2})
    assert r.status_code == 400 and "approved headcount of 3" in r.json()["detail"]
    one = _position(company_a, "Sales Lead", department_id=sales["id"], headcount=1)
    assert company_a.put(f"/positions/positions/{one['uuid']}", json={"headcount": 2}).status_code == 400

    # A revision keeps the old limit until it is approved.
    r = company_a.put(f"/positions/headcount-plans/{plan['uuid']}", json={"planned_headcount": 5})
    assert r.status_code == 200 and r.json()["status"] == "draft" and r.json()["approved_headcount"] == 3
    assert company_a.put(f"/positions/positions/{one['uuid']}", json={"headcount": 2}).status_code == 400
    assert company_a.post(f"/positions/headcount-plans/{plan['uuid']}/submit").status_code == 200
    _approve_all(company_a, "positions.headcount_plan")
    assert company_a.put(f"/positions/positions/{one['uuid']}", json={"headcount": 2}).status_code == 200

    # Position request: a new position through approval; rejected requests change nothing.
    r = company_a.post("/positions/requests", json={"kind": "new", "reason": "More deals",
                                                    "data": {"title": "Inside Sales", "department_id": sales["id"], "headcount": 1}})
    assert r.status_code == 201, r.text
    req = r.json()
    assert req["status"] in ("pending", "approved") and req["number"]
    _approve_all(company_a, "positions.request")
    got = next(x for x in company_a.get("/positions/requests").json()["items"] if x["uuid"] == req["uuid"])
    assert got["status"] == "approved" and got["position"]["title"] == "Inside Sales"
    # The department is now at its limit (2 + 2 + 1 = 5), so another new position is refused at request time.
    r = company_a.post("/positions/requests", json={"kind": "new", "reason": "x",
                                                    "data": {"title": "One more", "department_id": sales["id"]}})
    assert r.status_code == 400
    # A manager asks to close an empty position; the request is turned down and nothing changes.
    mgr = login_as(company_a, company_a.add_employee("Manu", "Lead", role="manager"))
    r = mgr.post("/positions/requests", json={"kind": "close", "reason": "Not needed", "position_uuid": one["uuid"]})
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "pending"
    assert mgr.post("/positions/requests", json={"kind": "close", "reason": "again", "position_uuid": one["uuid"]}).status_code == 400
    _decide_inbox(company_a, "positions.request", "reject")
    assert company_a.get(f"/positions/positions/{one['uuid']}").json()["status"] == "approved"
    assert mgr.get("/positions/requests").json()["items"][0]["status"] == "rejected"


# ── succession ──────────────────────────────────────────────────────

def test_bench_strength_rules():
    assert suc.bench_strength({}) == "none"
    assert suc.bench_strength({"three_plus_years": 2}) == "weak"
    assert suc.bench_strength({"one_two_years": 1}) == "moderate"
    assert suc.bench_strength({"ready_now": 1}) == "moderate"  # one person is not a bench of two
    assert suc.bench_strength({"ready_now": 1, "three_plus_years": 1}) == "strong"
    assert suc.bench_strength({"ready_now": 1, "one_two_years": 1}, ready_now_target=2) == "moderate"
    assert suc.urgency("high", "high") == "urgent" and suc.urgency("low", "medium") == "stable"
    assert suc.urgency("medium", "high") == "urgent" and suc.urgency("medium", "medium") == "watch"


def test_succession_readiness_and_bench(company_a):
    _enable(company_a)
    head = company_a.add_employee("Meera", "Head")
    s1 = company_a.add_employee("Ravi", "Next")
    s2 = company_a.add_employee("Sana", "Later")
    p = _position(company_a, "Head of Finance")
    company_a.post(f"/positions/positions/{p['uuid']}/seats", json={"employee_id": head["id"]})
    r = company_a.post("/succession/plans", json={"position_uuid": p["uuid"], "risk_of_loss": "high", "impact_of_loss": "high"})
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["urgency"] == "urgent" and plan["bench"] == "none"
    assert company_a.get(f"/positions/positions/{p['uuid']}").json()["is_critical"] is True
    assert company_a.post("/succession/plans", json={"position_uuid": p["uuid"]}).status_code == 400

    url = f"/succession/plans/{plan['uuid']}/candidates"
    assert company_a.post(url, json={"employee_id": s1["id"], "readiness": "someday"}).status_code == 400
    r = company_a.post(url, json={"employee_id": head["id"], "readiness": "ready_now"})
    assert r.status_code == 400 and "holds the position" in r.json()["detail"]
    plan = company_a.post(url, json={"employee_id": s2["id"], "readiness": "three_plus_years"}).json()
    assert plan["bench"] == "weak"
    plan = company_a.post(url, json={"employee_id": s1["id"], "readiness": "one_two_years"}).json()
    assert plan["bench"] == "moderate" and plan["counts"]["one_two_years"] == 1
    assert company_a.post(url, json={"employee_id": s1["id"]}).status_code == 400  # already a successor
    cand = next(x for x in plan["candidates"] if x["employee_id"] == s1["id"])
    plan = company_a.put(f"/succession/candidates/{cand['uuid']}", json={"readiness": "ready_now"}).json()
    assert plan["bench"] == "strong" and plan["candidates"][0]["employee_id"] == s1["id"]

    r = company_a.post(f"/succession/candidates/{cand['uuid']}/actions",
                       json={"kind": "mentoring", "title": "Shadow the head monthly", "mentor_id": head["id"]})
    assert r.status_code == 201, r.text
    act = r.json()
    assert company_a.put(f"/succession/actions/{act['uuid']}", json={"status": "done"}).json()["progress"] == 100.0

    bench = company_a.get("/succession/bench").json()
    assert bench["plans"] == 1 and bench["ready_now"] == 1 and bench["by_strength"]["strong"] == 1
    assert bench["coverage_pct"] == 100.0

    # Talent pools.
    r = company_a.post("/succession/pools", json={"name": "High potentials", "employee_ids": [s1["id"], s2["id"]]})
    assert r.status_code == 201 and r.json()["members_count"] == 2
    assert company_a.post("/succession/pools", json={"name": "high potentials"}).status_code == 400


# ── internal marketplace ────────────────────────────────────────────

def test_match_score():
    score, detail = mkt.match_score([{"skill_id": 1, "level": 4}, {"skill_id": 2, "level": 2}], {1: 2, 2: 5})
    assert score == 75.0 and detail[0]["met"] is False and detail[1]["met"] is True
    assert mkt.match_score([{"skill_id": 1, "level": 3}], {})[0] == 0.0
    assert mkt.match_score([], {1: 3})[0] is None


def test_internal_application_flow(company_a):
    _enable(company_a)
    ops = _dept(company_a, "Operations")
    manager = company_a.add_employee("Mohan", "Manager")
    hiring = company_a.add_employee("Hema", "Hiring")
    emp = company_a.add_employee("Isha", "Mover", reporting_manager_id=manager["id"])
    skill = company_a.get("/organization/skills", params={"q": "Python"}).json()[0]
    sql = company_a.get("/organization/skills", params={"q": "SQL"}).json()[0]
    company_a.post(f"/employees/{emp['uuid']}/skills", json={"skill_id": skill["id"], "level": 4})
    company_a.post(f"/employees/{emp['uuid']}/skills", json={"skill_id": sql["id"], "level": 1})

    target = _position(company_a, "Ops Analyst", department_id=ops["id"], headcount=1)
    r = company_a.post("/internal-jobs/postings", json={
        "kind": "job", "title": "Ops Analyst", "position_uuid": target["uuid"], "hiring_manager_id": hiring["id"],
        "skills": [{"skill_id": skill["id"], "level": 4}, {"skill_id": sql["id"], "level": 2}], "publish": True})
    assert r.status_code == 201, r.text
    post = r.json()
    assert post["status"] == "open" and post["position"]["uuid"] == target["uuid"] and post["department_id"] == ops["id"]

    me = login_as(company_a, emp)
    listed = me.get("/internal-jobs/postings").json()["items"]
    mine = next(p for p in listed if p["uuid"] == post["uuid"])
    assert mine["my_match"] == 75.0 and mine["can_manage"] is False
    assert me.get(f"/internal-jobs/postings/{post['uuid']}/applications").status_code == 403
    r = me.post(f"/internal-jobs/postings/{post['uuid']}/apply", json={"note": "Keen to move"})
    assert r.status_code == 201, r.text
    app = r.json()
    assert app["status"] == "manager_review" and app["match_score"] == 75.0
    assert me.post(f"/internal-jobs/postings/{post['uuid']}/apply", json={}).status_code == 400

    # The current manager consents from the approvals inbox.
    _decide_inbox(login_as(company_a, manager), "positions.internal_application", "approve")
    mine = me.get("/internal-jobs/applications/mine").json()["items"]
    assert mine[0]["status"] == "submitted"

    # The hiring manager decides; select then move into the position.
    hm = login_as(company_a, hiring)
    apps = hm.get(f"/internal-jobs/postings/{post['uuid']}/applications").json()["items"]
    assert len(apps) == 1
    url = f"/internal-jobs/applications/{app['uuid']}/decision"
    assert hm.post(url, json={"action": "move"}).status_code == 400  # select first
    assert hm.post(url, json={"action": "shortlist"}).json()["status"] == "shortlisted"
    assert hm.post(url, json={"action": "select"}).json()["status"] == "selected"
    r = hm.post(url, json={"action": "move"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "moved"
    prof = company_a.get(f"/employees/{emp['uuid']}").json()
    assert prof["department_id"] == ops["id"]
    seat = company_a.get(f"/positions/employees/{emp['id']}/seat").json()["seat"]
    assert seat["position"]["uuid"] == target["uuid"]
    assert company_a.get(f"/internal-jobs/postings/{post['uuid']}").json()["status"] == "filled"


def test_marketplace_rules_and_declined_consent(company_a):
    _enable(company_a, max_open_applications=1)
    manager = company_a.add_employee("Nina", "Boss")
    emp = company_a.add_employee("Om", "Applicant", reporting_manager_id=manager["id"])
    gig = company_a.post("/internal-jobs/postings", json={"kind": "gig", "title": "Office move", "publish": True})
    assert gig.status_code == 400 and "weeks" in gig.json()["detail"]
    gig = company_a.post("/internal-jobs/postings", json={"kind": "gig", "title": "Office move", "duration_weeks": 3,
                                                          "hours_per_week": 4, "publish": True}).json()
    job = company_a.post("/internal-jobs/postings", json={"kind": "job", "title": "Analyst", "needs_manager_consent": False,
                                                          "publish": True}).json()
    me = login_as(company_a, emp)
    # Employees can't post.
    assert me.post("/internal-jobs/postings", json={"kind": "job", "title": "x"}).status_code == 403
    a = me.post(f"/internal-jobs/postings/{gig['uuid']}/apply", json={}).json()
    assert a["status"] == "manager_review"
    r = me.post(f"/internal-jobs/postings/{job['uuid']}/apply", json={})
    assert r.status_code == 400 and "up to 1" in r.json()["detail"]
    _decide_inbox(login_as(company_a, manager), "positions.internal_application", "reject")
    assert me.get("/internal-jobs/applications/mine").json()["items"][0]["status"] == "manager_declined"
    # No consent needed for this one; the slot is free again.
    r = me.post(f"/internal-jobs/postings/{job['uuid']}/apply", json={})
    assert r.status_code == 201 and r.json()["status"] == "submitted"
    assert me.post(f"/internal-jobs/applications/{r.json()['uuid']}/withdraw").json()["status"] == "withdrawn"

    # Time in role: a fresh joiner can't apply for jobs (gigs are fine).
    company_a.put("/positions/settings", json={"min_months_in_role": 6, "max_open_applications": 3})
    from datetime import date
    newbie = company_a.add_employee("New", "Joiner", date_of_joining=date.today().isoformat())
    nb = login_as(company_a, newbie)
    r = nb.post(f"/internal-jobs/postings/{job['uuid']}/apply", json={})
    assert r.status_code == 400 and "months" in r.json()["detail"]


# ── permissions and tenant isolation ────────────────────────────────

def test_permissions(company_a):
    emp = company_a.add_employee("Plain", "Person")
    me = login_as(company_a, emp)
    assert me.get("/positions/positions").status_code == 403
    assert me.post("/positions/positions", json={"title": "x"}).status_code == 403
    assert me.get("/succession/plans").status_code == 403
    assert me.get("/positions/meta").status_code == 200
    assert me.get("/internal-jobs/postings").status_code == 200


def test_tenant_isolation(company_a, company_b):
    _enable(company_a)
    _enable(company_b)
    p = _position(company_a, "Secret Role")
    plan = company_a.post("/succession/plans", json={"position_uuid": p["uuid"]}).json()
    pool = company_a.post("/succession/pools", json={"name": "A pool"}).json()
    post = company_a.post("/internal-jobs/postings", json={"kind": "job", "title": "A job", "publish": True}).json()
    hp = company_a.post("/positions/headcount-plans", json={"department_id": _dept(company_a, "A dept")["id"],
                                                            "planned_headcount": 4}).json()

    assert company_b.get(f"/positions/positions/{p['uuid']}").status_code == 404
    assert company_b.put(f"/positions/positions/{p['uuid']}", json={"title": "Hacked"}).status_code == 404
    assert all(x["uuid"] != p["uuid"] for x in company_b.get("/positions/positions").json()["items"])
    assert all(x["uuid"] != p["uuid"] for x in company_b.get("/positions/tree").json()["items"])
    assert company_b.get(f"/succession/plans/{plan['uuid']}").status_code == 404
    assert company_b.get(f"/succession/pools/{pool['uuid']}").status_code == 404
    assert company_b.post("/succession/plans", json={"position_uuid": p["uuid"]}).status_code == 404
    assert company_b.get(f"/internal-jobs/postings/{post['uuid']}").status_code == 404
    assert all(x["uuid"] != post["uuid"] for x in company_b.get("/internal-jobs/postings").json()["items"])
    assert company_b.put(f"/positions/headcount-plans/{hp['uuid']}", json={"planned_headcount": 1}).status_code == 404
    b_emp = company_b.add_employee("Bee", "Worker")
    assert company_b.post(f"/positions/positions/{p['uuid']}/seats", json={"employee_id": b_emp["id"]}).status_code == 404
    bs = login_as(company_b, b_emp)
    assert bs.post(f"/internal-jobs/postings/{post['uuid']}/apply", json={}).status_code == 404
    # Company A can't seat company B's employee in its own position.
    r = company_a.post(f"/positions/positions/{p['uuid']}/seats", json={"employee_id": b_emp["id"]})
    assert r.status_code == 400

    # Row-level security: a tenant session for B sees none of A's rows.
    from core.db import tenant_session
    from modules.positions.models import InternalPosting, Position, SuccessionPlan, TalentPool
    db = tenant_session(company_b.id)
    try:
        assert db.query(Position).filter(Position.company_id == company_a.id).count() == 0
        assert db.query(SuccessionPlan).count() == 0
        assert db.query(TalentPool).count() == 0
        assert db.query(InternalPosting).filter(InternalPosting.company_id == company_a.id).count() == 0
    finally:
        db.close()
