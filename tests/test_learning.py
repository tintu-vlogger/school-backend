"""Learning: assessment scoring (negative marking, multi-select, short answers, pass
rule), course progress and locking, enrolment rules and mandatory training, batches with
a waitlist, course requests through approval, permissions and tenant isolation."""
import pytest

from modules.learning import calc
from tests.conftest import login_as

Q_SINGLE = {"id": "1", "kind": "single", "marks": 2, "options": [{"id": "a", "correct": True}, {"id": "b"}, {"id": "c"}]}
Q_MULTI = {"id": "2", "kind": "multi", "marks": 2, "options": [{"id": "a", "correct": True}, {"id": "b", "correct": True}, {"id": "c"}]}
Q_TF = {"id": "3", "kind": "true_false", "marks": 1, "options": [{"id": "true", "correct": False}, {"id": "false", "correct": True}]}
Q_SHORT = {"id": "4", "kind": "short", "marks": 1, "accepted_answers": ["Internal Committee", "IC"]}
ALL = [Q_SINGLE, Q_MULTI, Q_TF, Q_SHORT]  # 6 marks


def test_all_correct_scores_full_marks():
    r = calc.score_attempt(ALL, {"1": "a", "2": ["b", "a"], "3": "false", "4": "  internal   committee "}, pass_pct=60)
    assert r["score"] == 6 and r["pct"] == 100 and r["passed"] and r["correct"] == 4


def test_multi_select_needs_the_exact_set():
    assert calc.score_question(Q_MULTI, ["a"])["correct"] is False
    assert calc.score_question(Q_MULTI, ["a", "b", "c"])["correct"] is False
    assert calc.score_question(Q_MULTI, ["a", "b"])["marks"] == 2


def test_negative_marking_fixed_and_percent():
    answers = {"1": "b", "2": ["a", "b"], "3": "true"}  # wrong (2 marks), right (2), wrong (1), unanswered short
    fixed = calc.score_attempt(ALL, answers, "fixed", 0.5, pass_pct=50)
    assert fixed["score"] == 1.0  # 2 - 0.5 - 0.5
    assert fixed["unanswered"] == 1 and fixed["wrong"] == 2
    pct = calc.score_attempt(ALL, answers, "percent", 25, pass_pct=50)
    assert pct["score"] == 1.25  # 2 - (25% of 2) - (25% of 1)
    none = calc.score_attempt(ALL, answers, "none", 0, pass_pct=50)
    assert none["score"] == 2 and none["pct"] == 33.33


def test_score_never_goes_below_zero_and_unanswered_is_not_penalised():
    r = calc.score_attempt(ALL, {"1": "b", "3": "true"}, "fixed", 5)
    assert r["score"] == 0 and r["pct"] == 0 and not r["passed"]
    assert calc.score_question(Q_SINGLE, None, "fixed", 5)["marks"] == 0


def test_pass_rule_is_inclusive():
    qs = [{"id": str(i), "kind": "true_false", "marks": 1, "options": [{"id": "true", "correct": True}, {"id": "false"}]} for i in range(10)]
    seven = {str(i): "true" for i in range(7)}
    assert calc.score_attempt(qs, seven, pass_pct=70)["passed"] is True     # exactly 70%
    assert calc.score_attempt(qs, seven, pass_pct=70.01)["passed"] is False


def test_final_score_and_lesson_locks():
    attempts = [{"number": 1, "pct": 80}, {"number": 2, "pct": 60}]
    assert calc.final_score(attempts, "highest")["number"] == 1
    assert calc.final_score(attempts, "latest")["number"] == 2
    assert calc.unlocked([1, 2, 3], [], True) == {1}
    assert calc.unlocked([1, 2, 3], [1], True) == {1, 2}
    assert calc.unlocked([1, 2, 3], [], False) == {1, 2, 3}
    assert calc.course_progress([1, 2, 3, 4], [1, 3]) == 50.0


# ── API ─────────────────────────────────────────────────────────────

def _quiz_course(company, negative="percent", value=50):
    qs = []
    for i in range(4):
        r = company.post("/learning/questions", json={"category": "Test", "kind": "single", "text": f"Q{i}",
                                                      "options": [{"text": "Right", "correct": True}, {"text": "Wrong"}]})
        assert r.status_code == 201, r.text
        qs.append(r.json())
    r = company.post("/learning/assessments", json={"title": "Quiz", "selection": "random",
                                                    "random_rule": {"count": 3, "categories": ["Test"]}, "pass_pct": 60,
                                                    "negative_marking": negative, "negative_value": value, "max_attempts": 2,
                                                    "time_limit_minutes": 10, "status": "published"})
    assert r.status_code == 201, r.text
    quiz = r.json()
    r = company.post("/learning/courses", json={
        "title": "Safety", "status": "published", "sequential": True, "assessment_id": quiz["id"],
        "lessons": [{"title": "Read me", "kind": "text", "body": "Hello"},
                    {"title": "Watch", "kind": "video", "url": "https://example.com/v.mp4"},
                    {"title": "Quiz", "kind": "assessment", "assessment_id": quiz["id"]}]})
    assert r.status_code == 201, r.text
    return r.json(), quiz, qs


def _answers(attempt, right: int):
    out = {}
    for i, q in enumerate(attempt["questions"]):
        opt = q["options"][0]["id"] if i < right else q["options"][1]["id"]
        out[str(q["id"])] = opt
    return out


def test_course_player_quiz_and_certificate(company_a):
    course, quiz, _ = _quiz_course(company_a)
    me = login_as(company_a, company_a.add_employee("Lata", "Learner"))
    assert me.post(f"/learning/courses/{course['uuid']}/enrol").status_code == 201
    detail = me.get(f"/learning/courses/{course['uuid']}").json()
    lessons = detail["lessons"]
    assert [l["locked"] for l in lessons] == [False, True, True]
    enr = detail["enrolment"]["uuid"]
    # can't skip ahead
    assert me.post(f"/learning/enrolments/{enr}/lessons/{lessons[1]['uuid']}/complete").status_code == 400
    assert me.post(f"/learning/enrolments/{enr}/lessons/{lessons[0]['uuid']}/complete").json()["enrolment"]["progress"] == 33.33
    me.post(f"/learning/enrolments/{enr}/lessons/{lessons[1]['uuid']}/complete")
    # the quiz lesson needs a pass
    assert me.post(f"/learning/enrolments/{enr}/lessons/{lessons[2]['uuid']}/complete").status_code == 400
    att = me.post(f"/learning/assessments/{quiz['uuid']}/attempts").json()
    assert len(att["questions"]) == 3 and att["seconds_left"] > 500
    assert all("correct" not in o for q in att["questions"] for o in q["options"])  # answers hidden while taking it
    res = me.post(f"/learning/attempts/{att['uuid']}/submit", json={"answers": _answers(att, 1)}).json()
    # 1 right (1 mark), 2 wrong at 50% negative: 1 - 0.5 - 0.5 = 0 -> fail
    assert res["score"] == 0 and res["passed"] is False and res["reveal"]
    att2 = me.post(f"/learning/assessments/{quiz['uuid']}/attempts").json()
    res2 = me.post(f"/learning/attempts/{att2['uuid']}/submit", json={"answers": _answers(att2, 3)}).json()
    assert res2["pct"] == 100 and res2["passed"]
    assert me.post(f"/learning/assessments/{quiz['uuid']}/attempts").status_code == 400  # 2 attempts used
    mine = me.get("/learning/my").json()
    e = next(x for x in mine["enrolments"] if x["uuid"] == course["uuid"])
    assert e["enrolment"]["status"] == "completed" and e["enrolment"]["certificate_no"]
    cert = me.get(f"/learning/enrolments/{enr}/certificate").json()
    assert "Lata Learner" in cert["text"] and "Safety" in cert["text"]


def test_rules_enrol_everyone_with_due_dates(company_a):
    course, _, _ = _quiz_course(company_a)
    a = company_a.add_employee("Ram", "One")
    b = company_a.add_employee("Sam", "Two")
    r = company_a.post("/learning/rules", json={"name": "All", "course_id": course["id"], "criteria": {"all": True},
                                                "mandatory": True, "due_days": 30})
    assert r.status_code == 201, r.text
    assert r.json()["enrolled"] >= 2
    again = company_a.post(f"/learning/rules/{r.json()['uuid']}/run").json()
    assert again["enrolled"] == 0  # nobody twice
    mine = login_as(company_a, a).get("/learning/my").json()["enrolments"][0]
    assert mine["enrolment"]["mandatory"] and mine["enrolment"]["due_date"]
    # a mandatory course can't be dropped by the learner
    assert login_as(company_a, b).post(f"/learning/enrolments/{login_as(company_a, b).get('/learning/my').json()['enrolments'][0]['enrolment']['uuid']}/cancel").status_code == 403
    summary = company_a.get("/learning/reports/summary").json()
    assert summary["enrolments"] >= 2 and any(m["course"] == "Safety" for m in summary["mandatory"])


def test_batch_waitlist_moves_up(company_a):
    r = company_a.post("/learning/courses", json={"title": "Workshop", "delivery": "classroom", "status": "published"})
    assert r.status_code == 201, r.text
    course = r.json()
    r = company_a.post("/learning/batches", json={"course_id": course["id"], "name": "Batch 1", "capacity": 1,
                                                  "sessions": [{"date": "2026-12-01", "start": "10:00", "end": "12:00"}]})
    assert r.status_code == 201, r.text
    batch = r.json()
    a, b = company_a.add_employee("Asha", "A"), company_a.add_employee("Bina", "B")
    sa, sb = login_as(company_a, a), login_as(company_a, b)
    assert sa.post(f"/learning/batches/{batch['uuid']}/seats", json={}).json()["status"] == "confirmed"
    assert sb.post(f"/learning/batches/{batch['uuid']}/seats", json={}).json()["status"] == "waitlisted"
    seat = sa.get(f"/learning/batches/{batch['uuid']}").json()["my_seat"]
    assert sa.delete(f"/learning/batches/{batch['uuid']}/seats/{seat['uuid']}").status_code == 200
    assert sb.get(f"/learning/batches/{batch['uuid']}").json()["my_seat"]["status"] == "confirmed"
    roster = company_a.get(f"/learning/batches/{batch['uuid']}").json()["seats"]
    present = [s["uuid"] for s in roster if s["status"] == "confirmed"]
    session_id = company_a.get(f"/learning/batches/{batch['uuid']}").json()["sessions"][0]["id"]
    assert company_a.put(f"/learning/batches/{batch['uuid']}/attendance", json={"session_id": session_id, "present": present}).status_code == 200
    assert company_a.post(f"/learning/batches/{batch['uuid']}/complete", json={}).json()["completed"] == 1
    assert sb.get("/learning/my").json()["stats"]["completed"] == 1


def test_course_request_goes_to_the_manager(company_a):
    mgr = company_a.add_employee("Mohan", "Lead", role="manager")
    emp = company_a.add_employee("Nila", "Dev", reporting_manager_id=mgr["id"])
    me = login_as(company_a, emp)
    r = me.post("/learning/requests", json={"title": "Kubernetes course", "reason": "Platform work"})
    assert r.status_code == 201 and r.json()["status"] == "pending"
    boss = login_as(company_a, mgr)
    tasks = boss.get("/workflow/inbox").json()["items"]
    t = next(t for t in tasks if t["request"]["request_type"] == "learning.course_request")
    assert boss.post(f"/workflow/tasks/{t['uuid']}/decision", json={"decision": "approve"}).status_code == 200
    assert me.get("/learning/requests").json()[0]["status"] == "approved"


def test_learning_admin_permissions(company_a):
    me = login_as(company_a, company_a.add_employee("Om", "Staff"))
    assert me.post("/learning/courses", json={"title": "X"}).status_code == 403
    assert me.get("/learning/questions").status_code == 403
    assert me.get("/learning/reports/summary").status_code == 403


def test_learning_tenant_isolation(company_a, company_b):
    course, quiz, qs = _quiz_course(company_a)
    assert company_b.get(f"/learning/courses/{course['uuid']}").status_code == 404
    assert company_b.put(f"/learning/courses/{course['uuid']}", json={"title": "Mine"}).status_code == 404
    assert company_b.get(f"/learning/assessments/{quiz['uuid']}").status_code == 404
    assert company_b.put(f"/learning/questions/{qs[0]['uuid']}", json={"kind": "single", "text": "x"}).status_code == 404
    assert all(c["uuid"] != course["uuid"] for c in company_b.get("/learning/courses").json())
    assert all(q["uuid"] != qs[0]["uuid"] for q in company_b.get("/learning/questions").json())
    b_emp = login_as(company_b, company_b.add_employee("Zed", "B"))
    assert b_emp.post(f"/learning/courses/{course['uuid']}/enrol").status_code == 404
    assert b_emp.post(f"/learning/assessments/{quiz['uuid']}/attempts").status_code == 404
