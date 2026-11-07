"""Exams: grade bands, GPA / SGPA / CGPA, combining exams, pass rules, marks entry rights,
publishing, portal access and tenant isolation."""
from decimal import Decimal

import pytest

from modules.exams import calc
from tests.conftest import EmployeeSession
from tests.school_helpers import PASSWORD, portal_login, setup_school

CBSE = [{"label": lab, "min_percent": lo, "grade_point": gp, "is_pass": ok} for lab, lo, gp, ok in (
    ("A1", 91, 10, True), ("A2", 81, 9, True), ("B1", 71, 8, True), ("B2", 61, 7, True), ("C1", 51, 6, True),
    ("C2", 41, 5, True), ("D", 33, 4, True), ("E", 0, 0, False))]


# ── Calculations ─────────────────────────────────────────────────────

@pytest.mark.parametrize("pct,label", [(100, "A1"), (91, "A1"), (90.99, "A2"), (81, "A2"), (72.5, "B1"),
                                       (33, "D"), (32.99, "E"), (0, "E")])
def test_grade_band_lookup(pct, label):
    assert calc.band_for(pct, CBSE)["label"] == label


def test_band_lookup_ignores_order_and_handles_missing():
    assert calc.band_for(None, CBSE) is None
    assert calc.band_for(55, list(reversed(CBSE)))["label"] == "C1"
    assert calc.band_by_label("b2", CBSE)["grade_point"] == 7


def test_gpa_sgpa_cgpa_with_credits():
    # SGPA: (9*4 + 7*3 + 10*2) / 9 = 77/9
    sgpa = calc.gpa([(9, 4), (7, 3), (10, 2)])
    assert calc.q(sgpa) == Decimal("8.56")
    # No credits = simple average
    assert calc.gpa([(8, None), (6, None)]) == Decimal(7)
    # CGPA across terms weighted by credits: (8.2*22 + 8.8*24) / 46
    assert calc.q(calc.cgpa([(Decimal("8.2"), 22), (Decimal("8.8"), 24)])) == Decimal("8.51")
    assert calc.gpa([]) is None


def test_weighted_combination_and_best_of():
    assert calc.weighted([(80, 20), (65, 80)]) == Decimal(68)
    # A missing part does not count against the learner: weights re-normalised.
    assert calc.weighted([(80, 20), (None, 80)]) == Decimal(80)
    assert calc.combine([60, 72, 81], "best_of", 2) == Decimal("76.5")
    assert calc.combine([60, 72, 81], "average") == Decimal(71)
    assert calc.combine([60, None, 81], "max") == Decimal(81)
    assert calc.combine([10, 20], "sum") == Decimal(30)
    assert calc.combine([None], "average") is None


def test_grace_and_pass_rules():
    subjects = [{"percent": 31, "pass_percent": 33}, {"percent": 28, "pass_percent": 33},
                {"percent": 32, "pass_percent": 33}, {"percent": 70, "pass_percent": 33}]
    # 3 points per subject, at most 2 subjects: the two smallest shortfalls are lifted.
    assert calc.apply_grace(subjects, per_subject=3, max_subjects=2) == [Decimal(2), 0, Decimal(1), 0]
    # A total cap of 2 points helps only the smallest shortfall.
    assert calc.apply_grace(subjects, per_subject=3, total=2) == [0, 0, Decimal(1), 0]
    assert calc.decide([True, True], 60) == "pass"
    assert calc.decide([True, False], 60) == "fail"
    assert calc.decide([True, False], 60, max_failed_subjects=1) == "compartment"
    assert calc.decide([True, True], 30, min_aggregate=33) == "fail"
    assert calc.decide([True, False], 60, pass_all_subjects=False) == "pass"
    assert calc.decide([True], 80, attendance=70, min_attendance=75) == "not_eligible"
    assert calc.decide([True], 80, complete=False) == "incomplete"


def test_competition_rank():
    assert calc.competition_rank({"a": 90, "b": 80, "c": 90, "d": 70, "e": None}) == {"a": 1, "c": 1, "b": 3, "d": 4}


# ── API ──────────────────────────────────────────────────────────────

def _exam(company, school, name="Mid-term", max_marks=100):
    r = company.post("/exams", json={"name": name, "status": "scheduled", "program_ids": [school["program_id"]],
                                     "generate_papers": False})
    assert r.status_code == 201, r.text
    exam = r.json()
    papers = [{"program_id": school["program_id"], "subject_id": sid, "max_marks": max_marks,
               "pass_marks": max_marks * 0.33, "sort_order": i + 1} for i, sid in enumerate(school["subject_ids"])]
    r = company.put(f"/exams/{exam['uuid']}/papers", json=papers)
    assert r.status_code == 200, r.text
    return r.json()


def _paper(exam, subject_id):
    return next(p for p in exam["papers"] if p["subject_id"] == subject_id)


def _enter(session, paper, section_id, marks: dict):
    return session.put("/exams/marks/sheet", json={
        "paper": paper["uuid"], "section_id": section_id,
        "entries": [{"student_id": sid, **({"code": v} if isinstance(v, str) else {"marks": v})} for sid, v in marks.items()]})


@pytest.fixture
def school(company_a):
    return setup_school(company_a, teachers=2, learners=3)


def test_teacher_enters_marks_only_for_own_allocation(company_a, school):
    exam = _exam(company_a, school)
    t0 = EmployeeSession(company_a, school["teachers"][0], PASSWORD)
    maths, english = (_paper(exam, s) for s in school["subject_ids"])
    sec = school["section_id"]
    kids = school["students"]
    # Teacher 0 teaches maths in section A (and english in section B), not english in A.
    r = _enter(t0, maths, sec, {kids[0]: 78, kids[1]: 45.5, kids[2]: "AB"})
    assert r.status_code == 200, r.text
    assert _enter(t0, english, sec, {kids[0]: 50}).status_code == 403
    # Teachers may look at other sheets (exams.view) but only read them.
    view = t0.get(f"/exams/marks/sheet?paper={english['uuid']}&section_id={sec}").json()
    assert view["can_enter"] is False and view["editable"] is False
    # Over the maximum is refused with a plain message.
    r = _enter(t0, maths, sec, {kids[0]: 120})
    assert r.status_code == 400 and "between 0 and 100" in r.json()["detail"]
    # Only own sheets are listed with mine=true.
    mine = t0.get(f"/exams/marks/sheets?exam={exam['uuid']}&mine=true").json()
    assert {(s["subject_id"], s["section_id"]) for s in mine} == {(maths["subject_id"], sec),
                                                                 (english["subject_id"], school["section2_id"])}
    # Submit, then the teacher can no longer change the marks; locking needs moderation rights.
    r = t0.post("/exams/marks/sheet/action", json={"paper": maths["uuid"], "section_id": sec, "action": "submit"})
    assert r.status_code == 200, r.text
    assert _enter(t0, maths, sec, {kids[0]: 80}).status_code == 409
    assert t0.post("/exams/marks/sheet/action", json={"paper": maths["uuid"], "section_id": sec,
                                                      "action": "lock"}).status_code == 403
    assert company_a.post("/exams/marks/sheet/action", json={"paper": maths["uuid"], "section_id": sec,
                                                             "action": "lock"}).status_code == 200
    assert company_a.put("/exams/marks/sheet", json={"paper": maths["uuid"], "section_id": sec,
                                                     "entries": [{"student_id": kids[0], "marks": 10}]}).status_code == 409


def test_results_publishing_and_portal_access(company_a, school):
    exam = _exam(company_a, school)
    maths, english = (_paper(exam, s) for s in school["subject_ids"])
    sec, kids = school["section_id"], school["students"]
    assert _enter(company_a, maths, sec, {kids[0]: 90, kids[1]: 30, kids[2]: 60}).status_code == 200
    assert _enter(company_a, english, sec, {kids[0]: 80, kids[1]: 50, kids[2]: "EX"}).status_code == 200
    rows = company_a.get(f"/exams/results?source=exam:{exam['uuid']}&section_id={sec}").json()["items"]
    by = {r["student_id"]: r for r in rows}
    assert by[kids[0]]["percentage"] == 85.0 and by[kids[0]]["grade"] == "A2" and by[kids[0]]["status"] == "pass"
    assert by[kids[0]]["rank"] == 1
    assert by[kids[1]]["status"] == "fail" and by[kids[1]]["failed_subjects"] == 1  # 30 < 33 in maths
    assert by[kids[2]]["total_max"] == 100.0  # exempt paper left out of the total

    parent = portal_login(company_a, school["parent_login"])
    other_parent = portal_login(company_a, school["other_parent_login"])
    # Nothing is visible before publishing.
    assert parent.get(f"/exams/portal/results?student_id={kids[0]}").json() == []
    # Teachers cannot publish.
    t0 = EmployeeSession(company_a, school["teachers"][0], PASSWORD)
    assert t0.post("/exams/results/publish", json={"source": f"exam:{exam['uuid']}"}).status_code == 403
    r = company_a.post("/exams/results/publish", json={"source": f"exam:{exam['uuid']}", "section_ids": [sec]})
    assert r.status_code == 200, r.text
    assert r.json()["published"] == 3 and r.json()["notified"] >= 1
    mine = parent.get(f"/exams/portal/results?student_id={kids[0]}").json()
    assert len(mine) == 1 and mine[0]["percentage"] == 85.0 and mine[0]["subjects"]
    # Parents never see another family's child, and portal tokens can't reach staff endpoints.
    assert parent.get(f"/exams/portal/results?student_id={school['other_student']}").status_code == 404
    assert parent.get(f"/exams/portal/summary?student_id={kids[1]}").status_code == 404
    assert other_parent.get(f"/exams/portal/results?student_id={kids[0]}").status_code == 404
    assert parent.get(f"/exams/results?source=exam:{exam['uuid']}").status_code == 403
    assert parent.get(f"/exams/portal/report-card.pdf?student_id={kids[0]}").status_code == 200
    # Publishing is audited.
    audit = company_a.get("/audit/?action=exams.results.published").json()
    items = audit.get("items", audit) if isinstance(audit, dict) else audit
    assert any(a.get("action") == "exams.results.published" for a in items)
    # Unpublished again: hidden from the portal.
    assert company_a.post("/exams/results/unpublish", json={"source": f"exam:{exam['uuid']}"}).status_code == 200
    assert parent.get(f"/exams/portal/results?student_id={kids[0]}").json() == []


def test_result_rule_weighted_and_best_of(company_a, school):
    ut1 = _exam(company_a, school, "Unit test 1", 25)
    ut2 = _exam(company_a, school, "Unit test 2", 25)
    mt = _exam(company_a, school, "Mid-term", 100)
    sec, kid = school["section_id"], school["students"][0]
    maths = school["subject_ids"][0]
    for exam, val in ((ut1, 10), (ut2, 20), (mt, 70)):
        assert _enter(company_a, _paper(exam, maths), sec, {kid: val}).status_code == 200
    r = company_a.post("/exams/rules", json={
        "name": "Term 1", "ranking": False,
        "groups": [{"key": "ut", "label": "Unit tests", "exam_ids": [ut1["id"], ut2["id"]], "combine": "best_of",
                    "best_of": 1, "weight": 20},
                   {"key": "mt", "label": "Mid-term", "exam_ids": [mt["id"]], "weight": 80}]})
    assert r.status_code == 201, r.text
    rule = r.json()
    rows = company_a.get(f"/exams/results?source=rule:{rule['uuid']}&section_id={sec}").json()["items"]
    row = next(x for x in rows if x["student_id"] == kid)
    subj = next(s for s in row["subjects"] if s["subject_id"] == maths)
    # best of (40%, 80%) = 80, then 80*0.2 + 70*0.8 = 72
    assert subj["percent"] == 72.0 and subj["grade"] == "B1"
    assert row["rank"] is None  # ranking off
    # Formula method over the same parts.
    r = company_a.put(f"/exams/rules/{rule['uuid']}", json={**{k: rule[k] for k in ("name",)}, "method": "formula",
                                                            "formula": "max(ut, mt)", "groups": [
        {"key": "ut", "exam_ids": [ut1["id"], ut2["id"]], "combine": "best_of", "best_of": 1, "weight": 20},
        {"key": "mt", "exam_ids": [mt["id"]], "weight": 80}]})
    assert r.status_code == 200, r.text
    rows = company_a.get(f"/exams/results?source=rule:{rule['uuid']}&section_id={sec}").json()["items"]
    subj = next(s for s in next(x for x in rows if x["student_id"] == kid)["subjects"] if s["subject_id"] == maths)
    assert subj["percent"] == 80.0


def test_gpa_scheme_on_program_uses_credits(company_a, school):
    schemes = {s["code"]: s for s in company_a.get("/exams/schemes").json()}
    assert {"percentage_grades", "gpa_10", "gpa_4", "descriptive", "competency", "pass_fail"} <= set(schemes)
    assert company_a.put("/exams/program-schemes", json={"program_id": school["program_id"],
                                                         "grading_scheme_id": schemes["gpa_10"]["id"]}).status_code == 200
    exam = _exam(company_a, school)
    maths, english = school["subject_ids"]
    # Credits per paper: maths 4, english 2.
    papers = [{**{k: p[k] for k in ("uuid", "program_id", "subject_id", "max_marks")}, "credits": 4 if p["subject_id"] == maths else 2}
              for p in exam["papers"]]
    assert company_a.put(f"/exams/{exam['uuid']}/papers", json=papers).status_code == 200
    sec, kid = school["section_id"], school["students"][0]
    assert _enter(company_a, _paper(exam, maths), sec, {kid: 92}).status_code == 200    # O = 10
    assert _enter(company_a, _paper(exam, english), sec, {kid: 65}).status_code == 200  # B+ = 7
    row = next(x for x in company_a.get(f"/exams/results?source=exam:{exam['uuid']}&section_id={sec}").json()["items"]
               if x["student_id"] == kid)
    assert row["gpa"] == 9.0  # (10*4 + 7*2) / 6
    assert row["credits_attempted"] == 6.0 and row["credits_earned"] == 6.0
    assert row["grade"] == "A+"


def test_scheme_edit_after_publishing_creates_new_version(company_a, school):
    s = next(x for x in company_a.get("/exams/schemes").json() if x["code"] == "percentage_grades")
    exam = _exam(company_a, school)
    sec, kid = school["section_id"], school["students"][0]
    assert _enter(company_a, _paper(exam, school["subject_ids"][0]), sec, {kid: 70}).status_code == 200
    assert company_a.post("/exams/results/publish", json={"source": f"exam:{exam['uuid']}"}).status_code == 200
    r = company_a.put(f"/exams/schemes/{s['uuid']}", json={"pass_percent": 40})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["version"] == s["version"] + 1 and new["uuid"] != s["uuid"] and new["code"] == s["code"]
    old = [x for x in company_a.get("/exams/schemes?include_old=true").json() if x["uuid"] == s["uuid"]][0]
    assert old["active"] is False


def test_exam_tables_are_tenant_isolated(company_a, company_b, school):
    exam = _exam(company_a, school)
    sec, kid = school["section_id"], school["students"][0]
    assert _enter(company_a, _paper(exam, school["subject_ids"][0]), sec, {kid: 70}).status_code == 200
    assert company_b.get(f"/exams/{exam['uuid']}").status_code == 404
    assert company_b.put(f"/exams/{exam['uuid']}", json={"name": "Hacked"}).status_code == 404
    assert company_b.get(f"/exams/results?source=exam:{exam['uuid']}").status_code == 404
    assert company_b.get(f"/exams/marks/sheet?paper={exam['papers'][0]['uuid']}&section_id={sec}").status_code == 404
    assert company_b.post("/exams/results/publish", json={"source": f"exam:{exam['uuid']}"}).status_code == 404
    assert all(e["uuid"] != exam["uuid"] for e in company_b.get("/exams").json()["items"])
    assert company_a.get(f"/exams/{exam['uuid']}").json()["name"] == "Mid-term"
    from core.db import tenant_session
    from modules.exams.models import Exam, ExamResult, Mark
    db = tenant_session(company_b.id)
    try:
        assert db.query(Exam).filter(Exam.uuid == exam["uuid"]).first() is None
        assert db.query(Mark).filter(Mark.company_id == company_a.id).count() == 0
        assert db.query(ExamResult).filter(ExamResult.company_id == company_a.id).count() == 0
    finally:
        db.close()
