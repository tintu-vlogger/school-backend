"""Performance: OKR roll-up maths, check-in flags, review weightage and final rating,
calibration distribution, nine-box, increment guidelines, and the API end to end
(goals, a review cycle from launch to publish, permissions and tenant isolation)."""
from datetime import date, timedelta

import pytest

from modules.performance import calc
from modules.performance.reviews import score_form
from tests.conftest import login_as


# ── OKR maths ───────────────────────────────────────────────────────

def test_kr_progress_number_percent_and_decreasing_targets():
    assert calc.kr_progress("number", 0, 25, 10) == 40.0
    assert calc.kr_progress("percent", 100, 110, 104.5) == 45.0
    # a target that goes down: cut response time from 6 to 2 hours, now 3.5
    assert calc.kr_progress("number", 6, 2, 3.5) == 62.5
    # never below 0 or above 100
    assert calc.kr_progress("number", 0, 10, -5) == 0.0
    assert calc.kr_progress("number", 0, 10, 15) == 100.0


def test_kr_progress_milestones_and_yes_no():
    ms = [{"weight": 1, "done": True}, {"weight": 1, "done": False}, {"weight": 2, "done": True}]
    assert calc.kr_progress("milestone", milestones=ms) == 75.0
    assert calc.kr_progress("boolean", current=1) == 100.0
    assert calc.kr_progress("boolean", current=0) == 0.0


def test_objective_rolls_up_key_results_and_aligned_children_by_weight():
    # two key results (40% w1, 80% w3) and one aligned child at 20% with weight 1
    assert calc.objective_progress([(40, 1), (80, 3)], [(20, 1)]) == 60.0
    # zero weights fall back to a plain average
    assert calc.weighted_progress([(30, 0), (70, 0)]) == 50.0
    assert calc.objective_progress([], []) == 0.0


def test_auto_status_compares_progress_with_time():
    assert calc.expected_progress(date(2026, 10, 1), date(2026, 12, 31), date(2026, 11, 15)) == pytest.approx(49.45, abs=0.01)  # 45 of 91 days
    assert calc.auto_status(45, 50) == "on_track"      # 90% of where it should be
    assert calc.auto_status(35, 50) == "at_risk"       # 70%
    assert calc.auto_status(10, 50) == "behind"
    assert calc.auto_status(100, 50) == "done"


def test_planned_checkins_and_missed_late_flags():
    plan = calc.planned_checkins(date(2026, 10, 1), date(2026, 10, 29), "weekly")
    assert [d.isoformat() for d in plan] == ["2026-10-08", "2026-10-15", "2026-10-22", "2026-10-29"]
    flags = calc.checkin_flags(plan, [date(2026, 10, 8), date(2026, 10, 19)], today=date(2026, 10, 30), grace_days=2)
    # 19 Oct is late for the 15th (grace ends the 17th) and is not counted again for the 22nd
    assert [f["state"] for f in flags] == ["on_time", "late", "missed", "upcoming"]
    upcoming = calc.checkin_flags(plan, [], today=date(2026, 10, 20), grace_days=2)
    assert [f["state"] for f in upcoming] == ["missed", "missed", "upcoming", "upcoming"]


# ── Reviews ─────────────────────────────────────────────────────────

def test_form_score_uses_item_and_section_weights():
    sections = [
        {"key": "goals", "weight": 40, "items": [{"ref": "1", "weight": 3}, {"ref": "2", "weight": 1}]},
        {"key": "comp", "weight": 60, "items": [{"ref": "a", "weight": 1}, {"ref": "b", "weight": 1}]},
        {"key": "q", "weight": 0, "items": [{"ref": "t", "weight": 1, "type": "text"}]},
    ]
    answers = {"items": {"goals:1": {"rating": 4}, "goals:2": {"rating": 2}, "comp:a": {"rating": 5}, "comp:b": {"rating": 3}}}
    # goals = (4*3 + 2*1)/4 = 3.5; competencies = 4; overall = 3.5*0.4 + 4*0.6 = 3.8
    assert score_form(sections, answers) == 3.8
    # an unrated section drops out and the rest are re-weighted
    answers2 = {"items": {"comp:a": {"rating": 5}, "comp:b": {"rating": 3}}}
    assert score_form(sections, answers2) == 4.0


def test_combined_score_weightage_and_final_rating():
    w = {"self": 10, "manager": 60, "peer": 20, "skip": 10}
    s = calc.combined_score({"self": 4.5, "manager": 3.8, "peer": 4.0, "skip": 3.5}, w)
    assert s == pytest.approx(0.45 + 2.28 + 0.8 + 0.35)
    # no peer and skip yet: self and manager re-weighted (10:60)
    assert calc.combined_score({"self": 5, "manager": 3, "peer": None}, w) == pytest.approx((5 * 10 + 3 * 60) / 70, abs=1e-3)
    assert calc.round_to_scale(3.88, [1, 2, 3, 4, 5]) == 4
    assert calc.round_to_scale(3.5, [1, 2, 3, 4, 5]) == 4  # half rounds up
    assert calc.round_to_scale(3.49, [1, 2, 3, 4, 5]) == 3
    assert calc.progress_to_rating(75, [1, 2, 3, 4, 5]) == 4


def test_calibration_distribution_largest_remainder():
    ratings = [3, 3, 3, 4, 4, 4, 4, 5, 2, 3, 3]  # 11 people
    targets = {"1": 5, "2": 15, "3": 50, "4": 20, "5": 10}
    rows = {r["value"]: r for r in calc.distribution(ratings, targets, [1, 2, 3, 4, 5])}
    assert sum(r["target_count"] for r in rows.values()) == 11
    # 11 people: raw targets 0.55, 1.65, 5.5, 2.2, 1.1 -> floors 0,1,5,2,1 (9); the two left over go to the
    # largest remainders (rating 2 with .65, rating 1 with .55)
    assert [rows[v]["target_count"] for v in (1.0, 2.0, 3.0, 4.0, 5.0)] == [1, 2, 5, 2, 1]
    assert rows[3.0]["count"] == 5 and rows[3.0]["gap"] == 0
    assert rows[4.0]["count"] == 4 and rows[4.0]["gap"] == 2           # 2.2 -> 2 target, 2 too many
    assert rows[4.0]["actual_pct"] == pytest.approx(36.36, abs=0.01)
    no_target = calc.distribution(ratings, {}, [1, 2, 3, 4, 5])
    assert all(r["target_count"] is None for r in no_target)


def test_nine_box_and_increment_guidelines():
    assert calc.nine_box(5, 3, [1, 2, 3, 4, 5]) == 9
    assert calc.nine_box(1, 1, [1, 2, 3, 4, 5]) == 1
    assert calc.nine_box(3, 2, [1, 2, 3, 4, 5]) == 5
    g = {"bands": [{"min": None, "max": 0.9}, {"min": 0.9, "max": 1.1}, {"min": 1.1, "max": None}],
         "rows": [{"rating": 5, "pcts": [16, 13, 10]}, {"rating": 3, "pcts": [9, 7, 5]}]}
    assert calc.guideline_pct(5, 0.85, g) == 16
    assert calc.guideline_pct(3, 1.0, g) == 7
    assert calc.guideline_pct(4, 1.2, g) == 5  # 4 uses the nearest lower row (3)
    assert calc.compa_ratio(900000, 1000000) == 0.9
    amount, new = calc.increment(1234567, 7.5)
    assert str(amount) == "92592.53" and str(new) == "1327159.53"


# ── API ─────────────────────────────────────────────────────────────

def _period(company):
    periods = company.get("/performance/periods").json()
    return next(p for p in periods if p["kind"] == "quarter" and p["current"])


def test_goals_roll_up_through_the_api(company_a):
    mgr = company_a.add_employee("Mira", "Manager", role="manager")
    emp = company_a.add_employee("Eshan", "Engineer", reporting_manager_id=mgr["id"])
    period = _period(company_a)
    r = company_a.post("/performance/objectives", json={
        "period_id": period["id"], "level": "company", "title": "Grow revenue",
        "key_results": [{"title": "ARR", "kind": "number", "start_value": 0, "target_value": 100}]})
    assert r.status_code == 201, r.text
    top = r.json()
    me = login_as(company_a, emp)
    r = me.post("/performance/objectives", json={
        "period_id": period["id"], "level": "individual", "title": "Ship billing", "parent_id": top["id"], "weight": 1,
        "key_results": [{"title": "Invoices automated", "kind": "percent", "start_value": 0, "target_value": 100},
                        {"title": "Launch", "kind": "boolean"}]})
    assert r.status_code == 201, r.text
    mine = r.json()
    kr = mine["key_results"][0]
    r = me.post(f"/performance/objectives/{mine['uuid']}/checkins",
                json={"key_result_uuid": kr["uuid"], "value": 60, "confidence": 8, "note": "Going well"})
    assert r.status_code == 201, r.text
    assert r.json()["progress"] == 30.0  # (60 + 0) / 2
    parent = company_a.get(f"/performance/objectives/{top['uuid']}").json()
    # parent: its own KR at 0% (w1) and the aligned child at 30% (w1)
    assert parent["progress"] == 15.0
    assert parent["children"][0]["title"] == "Ship billing"
    # employees can't add company objectives; nor change someone else's
    assert me.post("/performance/objectives", json={"period_id": period["id"], "level": "company", "title": "X"}).status_code == 403
    other = company_a.add_employee("Olu", "Other")
    assert login_as(company_a, other).put(f"/performance/objectives/{mine['uuid']}", json={"title": "Hacked"}).status_code == 403
    # the manager can see and edit the reportee's goal
    m = login_as(company_a, mgr)
    team = m.get("/performance/objectives", params={"scope": "team", "period_id": period["id"]}).json()
    assert any(o["uuid"] == mine["uuid"] for o in team)
    assert m.put(f"/performance/objectives/{mine['uuid']}", json={"title": "Ship billing v2"}).status_code == 200


def _cycle(company, **over):
    templates = company.get("/performance/templates").json()
    tmpl = next(t for t in templates if t["name"] == "Annual review")
    body = {
        "name": "Annual review", "kind": "annual", "template_id": tmpl["id"],
        "period_start": "2026-04-01", "period_end": "2027-03-31",
        "stages": [{"key": "self", "enabled": True}, {"key": "manager", "enabled": True}, {"key": "calibration", "enabled": True}],
        "weightage": {"self": 20, "manager": 80},
        "settings": {"calibration": {"require_reason": True, "require_ack": True, "distribution": {"3": 60, "4": 40}}},
        **over,
    }
    r = company.post("/performance/cycles", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _fill(session, form_uuid, rating, submit=True):
    form = session.get(f"/performance/forms/{form_uuid}").json()
    items = {}
    for s in form["sections"]:
        for it in s["items"]:
            key = f"{s['key']}:{it['ref']}"
            items[key] = {"comment": "Done well"} if it.get("type") == "text" else {"rating": rating}
    return session.put(f"/performance/forms/{form_uuid}", json={"answers": {"items": items, "summary": "Good year"}, "submit": submit})


def test_review_cycle_end_to_end(company_a):
    mgr = company_a.add_employee("Maya", "Lead", role="manager")
    emp = company_a.add_employee("Arun", "Dev", reporting_manager_id=mgr["id"])
    cycle = _cycle(company_a, participant_rule={"include_ids": [emp["id"]], "department_ids": [-1]})
    assert company_a.post(f"/performance/cycles/{cycle['uuid']}/launch").json()["participants"] == 1
    me, boss = login_as(company_a, emp), login_as(company_a, mgr)
    self_form = me.get("/performance/reviews/mine").json()["self"][0]
    # can't submit with ratings missing
    bad = me.put(f"/performance/forms/{self_form['uuid']}", json={"answers": {"items": {}}, "submit": True})
    assert bad.status_code == 400
    assert _fill(me, self_form["uuid"], 5).status_code == 200
    mgr_form = boss.get("/performance/reviews/mine").json()["team"][0]
    detail = boss.get(f"/performance/forms/{mgr_form['uuid']}").json()
    assert detail["others"] and detail["others"][0]["reviewer_type"] == "self"  # manager sees the submitted self review
    assert _fill(boss, mgr_form["uuid"], 3).status_code == 200
    # the employee can't open the manager's form
    assert me.get(f"/performance/forms/{mgr_form['uuid']}").status_code == 403
    board = company_a.get(f"/performance/cycles/{cycle['uuid']}/calibration").json()
    row = board["participants"][0]
    assert row["scores"] == {"self": 5.0, "manager": 3.0}
    assert row["computed_score"] == pytest.approx(3.4)        # 5*0.2 + 3*0.8
    assert row["proposed_rating"] == 3.0
    # calibration needs a reason when the rating changes, and the manager acknowledges it
    p = row["uuid"]
    assert company_a.put(f"/performance/participants/{p}/calibration", json={"rating": 4}).status_code == 400
    assert company_a.put(f"/performance/participants/{p}/calibration", json={"rating": 4, "reason": "Peer comparison"}).status_code == 200
    acks = boss.get("/performance/reviews/acknowledgements").json()
    assert acks and acks[0]["uuid"] == p
    assert boss.post(f"/performance/participants/{p}/acknowledge", json={"accept": False}).status_code == 400  # reason needed
    assert boss.post(f"/performance/participants/{p}/acknowledge", json={"accept": True}).status_code == 200
    assert boss.put(f"/performance/participants/{p}/potential", json={"potential": 3}).status_code == 200
    # nothing published yet
    assert me.get("/performance/reviews/results").json()[0]["final_rating"] is None
    res = company_a.post(f"/performance/cycles/{cycle['uuid']}/publish", json={}).json()
    assert res["published"] == 1
    mine = me.get("/performance/reviews/results").json()[0]
    assert mine["final_rating"] == 4.0 and mine["final_label"] == "Exceeds expectations" and mine["has_letter"]
    letter = me.get(f"/performance/participants/{mine['uuid']}/letter").json()
    assert "Exceeds expectations" in letter["body"] and letter["letter_no"]
    box = company_a.get(f"/performance/cycles/{cycle['uuid']}/nine-box").json()
    assert any(b["box"] == 9 and b["people"] for b in box["boxes"])  # 4 on 1..5 is high performance, potential high
    assert me.post(f"/performance/participants/{mine['uuid']}/respond", json={"response": "agree"}).status_code == 200


def test_reminders_are_rate_limited(company_a):
    mgr = company_a.add_employee("Rita", "Lead", role="manager")
    emp = company_a.add_employee("Ravi", "Dev", reporting_manager_id=mgr["id"])
    cycle = _cycle(company_a, participant_rule={"include_ids": [emp["id"]], "department_ids": [-1]})
    company_a.post(f"/performance/cycles/{cycle['uuid']}/launch")
    first = company_a.post(f"/performance/cycles/{cycle['uuid']}/remind", json={}).json()
    assert first["sent"] == 2 and first["people"] == 2
    again = company_a.post(f"/performance/cycles/{cycle['uuid']}/remind", json={}).json()
    assert again["sent"] == 0 and again["skipped"] == 2


def test_cycle_weightage_and_distribution_must_add_up(company_a):
    tmpl = company_a.get("/performance/templates").json()[0]
    base = {"name": "X", "template_id": tmpl["id"], "period_start": "2026-04-01", "period_end": "2026-09-30",
            "stages": [{"key": "self", "enabled": True}, {"key": "manager", "enabled": True}]}
    assert company_a.post("/performance/cycles", json={**base, "weightage": {"self": 30, "manager": 30}}).status_code == 400
    assert company_a.post("/performance/cycles", json={**base, "weightage": {"self": 30, "manager": 70},
                                                       "settings": {"calibration": {"distribution": {"3": 50}}}}).status_code == 400


def test_employees_cannot_run_cycles_or_settings(company_a):
    emp = login_as(company_a, company_a.add_employee("Nina", "Staff"))
    assert emp.get("/performance/cycles").status_code == 403
    assert emp.put("/performance/settings", json={"okr": {"grace_days": 5}}).status_code == 403
    assert emp.post("/performance/kras", json={"title": "X"}).status_code == 403


def test_one_on_ones_private_notes_feedback_and_pip(company_a):
    mgr = company_a.add_employee("Kala", "Lead", role="manager")
    emp = company_a.add_employee("Ved", "Dev", reporting_manager_id=mgr["id"])
    boss, me = login_as(company_a, mgr), login_as(company_a, emp)
    r = boss.post("/performance/one-on-ones", json={"employee_id": emp["id"], "scheduled_at": "2026-12-01T05:00:00Z",
                                                     "recurrence": "weekly", "agenda": ["Goals"]})
    assert r.status_code == 201, r.text
    m = r.json()
    assert boss.put(f"/performance/one-on-ones/{m['uuid']}/private-note", json={"note": "Only for me"}).status_code == 200
    assert me.get(f"/performance/one-on-ones/{m['uuid']}").json()["private_note"] == ""
    assert boss.get(f"/performance/one-on-ones/{m['uuid']}").json()["private_note"] == "Only for me"
    nxt = boss.post(f"/performance/one-on-ones/{m['uuid']}/complete").json()
    assert nxt["next_uuid"]  # weekly: the next one is booked with open agenda items carried over
    assert boss.get(f"/performance/one-on-ones/{nxt['next_uuid']}").json()["agenda"][0]["text"] == "Goals"
    # feedback shared with the manager shows in their team view; private does not
    peer = company_a.add_employee("Pia", "Peer")
    p = login_as(company_a, peer)
    assert p.post("/performance/feedback", json={"receiver_employee_id": emp["id"], "message": "Great demo", "visibility": "manager"}).status_code == 201
    assert p.post("/performance/feedback", json={"receiver_employee_id": emp["id"], "message": "Just for you", "visibility": "private"}).status_code == 201
    team = boss.get("/performance/feedback", params={"box": "team"}).json()
    assert [f["message"] for f in team] == ["Great demo"]
    assert len(me.get("/performance/feedback", params={"box": "received"}).json()) == 2
    # a PIP: only the manager or HR can start it; the employee sees it and acknowledges
    assert me.post("/performance/pips", json={"employee_id": emp["id"], "reason": "x", "start_date": "2026-11-01",
                                               "end_date": "2027-01-31", "goals": [{"title": "A"}]}).status_code == 403
    r = boss.post("/performance/pips", json={"employee_id": emp["id"], "reason": "Missed targets", "start_date": "2026-11-01",
                                              "end_date": "2027-01-31", "goals": [{"title": "Close 3 deals"}]})
    assert r.status_code == 201, r.text
    pip = r.json()
    assert me.post(f"/performance/pips/{pip['uuid']}/acknowledge").status_code == 200
    assert boss.post(f"/performance/pips/{pip['uuid']}/checkins", json={"note": "Good start", "rating": "on_track"}).status_code == 201
    closed = boss.post(f"/performance/pips/{pip['uuid']}/close", json={"outcome": "successful", "note": "Met all goals"}).json()
    assert closed["status"] == "closed" and closed["outcome"] == "successful"


def test_praise_can_also_be_posted_as_kudos(company_a):
    giver_emp = company_a.add_employee("Tara", "Giver")
    rec = company_a.add_employee("Uday", "Taker")
    giver = login_as(company_a, giver_emp)
    r = giver.post("/performance/feedback", json={"receiver_employee_id": rec["id"], "message": "Brilliant launch plan",
                                                  "kind": "praise", "also_kudos": True})
    assert r.status_code == 201, r.text
    assert login_as(company_a, rec).get(f"/engagement/kudos/profile/{rec['uuid']}").json()["received"] == 1
    # when kudos are refused (limit used up) the feedback is still saved
    assert company_a.put("/engagement/settings/kudos", json={"monthly_limit": 1}).status_code == 200
    r = giver.post("/performance/feedback", json={"receiver_employee_id": rec["id"], "message": "And again, thanks",
                                                  "kind": "praise", "also_kudos": True})
    assert r.status_code == 201, r.text
    assert giver.get(f"/engagement/kudos/profile/{rec['uuid']}").json()["received"] == 1
    assert len(login_as(company_a, rec).get("/performance/feedback", params={"box": "received"}).json()) == 2


def test_pay_review_proposals_go_through_approval(company_a):
    mgr = company_a.add_employee("Hari", "Lead", role="manager")
    emp = company_a.add_employee("Isha", "Dev", reporting_manager_id=mgr["id"])
    r = company_a.post("/performance/increment-cycles", json={
        "name": "Pay review", "effective_date": "2026-01-01",
        "guidelines": {"bands": [{"min": None, "max": None}], "rows": [{"rating": 1, "pcts": [8]}]}})
    assert r.status_code == 201, r.text
    cyc = r.json()
    assert company_a.post(f"/performance/increment-cycles/{cyc['uuid']}/open").json()["proposals"] >= 2
    grid = company_a.get(f"/performance/increment-cycles/{cyc['uuid']}").json()
    prop = next(p for p in grid["proposals"] if p["employee"]["id"] == emp["id"])
    assert company_a.put(f"/performance/increment-proposals/{prop['uuid']}", json={"current_ctc": 1000000}).status_code == 200
    boss = login_as(company_a, mgr)
    # the manager sees only their team
    mine = boss.get(f"/performance/increment-cycles/{cyc['uuid']}").json()
    assert [p["employee"]["id"] for p in mine["proposals"]] == [emp["id"]]
    r = boss.put(f"/performance/increment-proposals/{prop['uuid']}", json={"proposed_pct": 10})
    assert r.status_code == 200 and r.json()["new_ctc"] == 1100000.0
    assert boss.put(f"/performance/increment-proposals/{prop['uuid']}", json={"current_ctc": 5}).status_code == 403
    sub = boss.post(f"/performance/increment-cycles/{cyc['uuid']}/submit", json={"uuids": [prop["uuid"]]}).json()
    assert sub["count"] == 1
    # default flow: the manager's manager, then HR; with neither, the owner approves both steps
    for _ in range(2):
        tasks = company_a.get("/workflow/inbox", params={"request_type": "performance.increment"}).json()["items"]
        assert len(tasks) == 1 and "Isha Dev: 10%" in " ".join(tasks[0]["request"]["summary"])
        assert company_a.post(f"/workflow/tasks/{tasks[0]['uuid']}/decision", json={"decision": "approve"}).status_code == 200
    grid = company_a.get(f"/performance/increment-cycles/{cyc['uuid']}").json()
    assert next(p for p in grid["proposals"] if p["uuid"] == prop["uuid"])["status"] == "approved"
    res = company_a.post(f"/performance/increment-cycles/{cyc['uuid']}/release").json()
    assert res["released"] == 1 and res["applied_now"] >= 1
    me = login_as(company_a, emp)
    letter = me.get(f"/performance/increment-proposals/{prop['uuid']}/letter").json()
    assert "₹1,100,000" in letter["body"] and "10%" in letter["body"]
    hist = me.get(f"/performance/employees/{emp['id']}/pay-history").json()
    assert hist[0]["after"]["ctc"] == 1100000.0 and hist[0]["status"] == "applied"


def test_performance_tenant_isolation(company_a, company_b):
    period = _period(company_a)
    obj = company_a.post("/performance/objectives", json={"period_id": period["id"], "level": "company", "title": "A only"}).json()
    cycle = _cycle(company_a)
    assert company_b.get(f"/performance/objectives/{obj['uuid']}").status_code == 404
    assert company_b.put(f"/performance/objectives/{obj['uuid']}", json={"title": "x"}).status_code == 404
    assert company_b.get(f"/performance/cycles/{cycle['uuid']}").status_code == 404
    assert company_b.post(f"/performance/cycles/{cycle['uuid']}/launch").status_code == 404
    b_period = _period(company_b)
    assert all(o["uuid"] != obj["uuid"] for o in company_b.get("/performance/objectives", params={"period_id": b_period["id"]}).json())
    # a period id from company A can't be used by company B
    assert company_b.post("/performance/objectives", json={"period_id": period["id"], "level": "company", "title": "x"}).status_code == 400
    assert all(c["uuid"] != cycle["uuid"] for c in company_b.get("/performance/cycles").json())
