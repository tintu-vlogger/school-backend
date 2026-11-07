"""Teaching and learning: submission rules, quiz marking, portal access and tenant isolation."""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.db import tenant_session
from core.security import hash_password
from modules.lms import quiz
from tests.conftest import login_as

PW = "Secret123#"


# ── Pure marking rules ───────────────────────────────────────────────

def test_mark_single_and_true_false():
    assert quiz.mark_item("single", ["b"], "b", Decimal("1")) == (Decimal("1"), True)
    assert quiz.mark_item("single", ["b"], "a", Decimal("1"), Decimal("0.25")) == (Decimal("-0.25"), False)
    assert quiz.mark_item("single", ["b"], None, Decimal("1"), Decimal("0.25")) == (Decimal("0"), False)
    assert quiz.mark_item("true_false", ["false"], "false", Decimal("2")) == (Decimal("2"), True)


def test_mark_multiple_choice_needs_exact_set():
    key = ["a", "b", "d"]
    assert quiz.mark_item("multiple", key, ["d", "a", "b"], Decimal("2")) == (Decimal("2"), True)
    assert quiz.mark_item("multiple", key, ["a", "b"], Decimal("2"))[1] is False
    assert quiz.mark_item("multiple", key, ["a", "b", "c", "d"], Decimal("2"))[0] == Decimal("0")


def test_mark_numeric_tolerance_and_short_answer():
    ans = {"value": "9.8", "tolerance": "0.1"}
    assert quiz.mark_item("numeric", ans, "9.75", Decimal("1"))[1] is True
    assert quiz.mark_item("numeric", ans, "9.9", Decimal("1"))[1] is True
    assert quiz.mark_item("numeric", ans, "9.95", Decimal("1"))[1] is False
    assert quiz.mark_item("numeric", ans, "abc", Decimal("1"))[1] is False
    assert quiz.mark_item("numeric", {"value": "1024", "tolerance": "0"}, "1,024", Decimal("1"))[1] is True
    assert quiz.mark_item("short", ["Photosynthesis"], "  photosynthesis. ", Decimal("1"))[1] is True
    assert quiz.mark_item("short", ["Photosynthesis"], "respiration", Decimal("1"))[1] is False
    assert quiz.mark_item("long", None, "An essay", Decimal("5")) == (None, None)


def test_question_validation():
    with pytest.raises(Exception):
        quiz.validate_question({"kind": "single", "options": [{"text": "A"}, {"text": "B"}], "answer": ["a", "b"]})
    q = quiz.validate_question({"kind": "multiple", "options": [{"text": "A"}, {"text": "B"}, {"text": "C"}],
                                "answer": ["a", "c"], "tags": ["Grade 8", "grade 8"]})
    assert q["answer"] == ["a", "c"] and q["tags"] == ["grade 8"]


# ── API set-up ───────────────────────────────────────────────────────

class School:
    """A company with Grade 8 A/B, Computer Science taught by one teacher, two learners
    (one per section) with student and parent sign-ins."""

    def __init__(self, company):
        self.c = company
        r = company.post("/academics/quick-setup", json={"institution_type": "school", "programs": ["Grade 8"],
                                                         "sections": ["A", "B"], "subjects": ["Computer Science"]})
        assert r.status_code == 200, r.text
        self.teacher_emp = company.add_employee("Divya", "Menon")
        db = tenant_session(company.id)
        try:
            from modules.academics.models import Section, SectionSubject, Subject
            from modules.students import service as students
            from modules.students.models import Guardian, PortalAccount, Student, StudentGuardian
            from modules.employees.models import Employee
            emp = db.query(Employee).filter(Employee.uuid == self.teacher_emp["uuid"]).first()
            emp.role = "teacher"
            secs = {s.name: s for s in db.query(Section).filter(Section.company_id == company.id).all()}
            cs = db.query(Subject).filter(Subject.company_id == company.id, Subject.name == "Computer Science").first()
            self.sec_a, self.sec_b, self.subject_id = secs["A"].id, secs["B"].id, cs.id
            for s in secs.values():
                db.add(SectionSubject(company_id=company.id, section_id=s.id, subject_id=cs.id,
                                      teacher_id=emp.id if s.name == "A" else None))
            self.logins = {}
            self.student_ids = {}
            for sec_name, first in (("A", "Aarav"), ("B", "Bela")):
                st = Student(company_id=company.id, admission_no=f"S-{first}-{uuid.uuid4().hex[:4]}", first_name=first,
                             last_name="Test")
                db.add(st)
                db.flush()
                students.enroll(db, company.id, st, secs[sec_name].id)
                g = Guardian(company_id=company.id, first_name=f"{first}'s parent")
                db.add(g)
                db.flush()
                db.add(StudentGuardian(company_id=company.id, student_id=st.id, guardian_id=g.id))
                tag = uuid.uuid4().hex[:6]
                db.add(PortalAccount(company_id=company.id, kind="student", student_id=st.id,
                                     login=f"{first.lower()}-{tag}@kids.test", password=hash_password(PW),
                                     must_change_password=False))
                db.add(PortalAccount(company_id=company.id, kind="parent", guardian_id=g.id,
                                     login=f"parent-{first.lower()}-{tag}@kids.test", password=hash_password(PW),
                                     must_change_password=False))
                self.logins[first] = (f"{first.lower()}-{tag}@kids.test", f"parent-{first.lower()}-{tag}@kids.test")
                self.student_ids[first] = st.id
            db.commit()
        finally:
            db.close()
        self.teacher = login_as(company, self.teacher_emp)

    def portal(self, login):
        r = self.c.client.post("/api/v1/auth/login", json={"email": login, "password": PW},
                               headers={"X-Company-Slug": self.c.slug})
        assert r.status_code == 200, r.text
        return Portal(self.c, r.json()["access_token"])


class Portal:
    def __init__(self, company, token):
        self.client, self.slug, self.token = company.client, company.slug, token

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.token}", "X-Company-Slug": self.slug}

    def get(self, path, **kw):
        return self.client.get(f"/api/v1{path}", headers=self.headers, **kw)

    def post(self, path, json=None, **kw):
        return self.client.post(f"/api/v1{path}", headers=self.headers, json=json, **kw)

    def put(self, path, json=None, **kw):
        return self.client.put(f"/api/v1{path}", headers=self.headers, json=json, **kw)


@pytest.fixture
def school(company_a):
    return School(company_a)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def _homework(school, **extra):
    body = {"title": "Flowchart", "subject_id": school.subject_id, "section_ids": [school.sec_a],
            "due_at": _iso(datetime.now(timezone.utc) + timedelta(days=2)), "max_marks": "10", **extra}
    r = school.teacher.post("/lms/homework", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ── Homework ─────────────────────────────────────────────────────────

def test_teacher_only_sets_work_for_own_allocation(school):
    r = school.teacher.post("/lms/homework", json={
        "title": "Not mine", "subject_id": school.subject_id, "section_ids": [school.sec_b],
        "due_at": _iso(datetime.now(timezone.utc) + timedelta(days=1))})
    assert r.status_code == 403
    # the owner (lms.manage) may
    r = school.c.post("/lms/homework", json={
        "title": "Section B work", "subject_id": school.subject_id, "section_ids": [school.sec_b],
        "due_at": _iso(datetime.now(timezone.utc) + timedelta(days=1))})
    assert r.status_code == 201, r.text
    # the teacher doesn't see section B's homework
    titles = [h["title"] for h in school.teacher.get("/lms/homework").json()["items"]]
    assert "Section B work" not in titles


def test_submission_rules_late_and_resubmit(school):
    hw = _homework(school)
    aarav = school.portal(school.logins["Aarav"][0])
    r = aarav.post(f"/lms/portal/homework/{hw['uuid']}/submit", json={"content": ""})
    assert r.status_code == 400  # empty
    r = aarav.post(f"/lms/portal/homework/{hw['uuid']}/submit", json={"content": "My flowchart"})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "submitted"
    # may send again before it is marked
    r = aarav.post(f"/lms/portal/homework/{hw['uuid']}/submit", json={"content": "Better flowchart"})
    assert r.status_code == 200 and r.json()["submission"]["attempts"] == 2
    # teacher grades and returns; no more submissions
    student_uuid = next(x["student_uuid"] for x in school.teacher.get(f"/lms/homework/{hw['uuid']}/submissions").json())
    r = school.teacher.post(f"/lms/homework/{hw['uuid']}/submissions/{student_uuid}/grade",
                            json={"marks": "8", "feedback": "Good", "return_now": True})
    assert r.status_code == 200, r.text
    r = school.teacher.post(f"/lms/homework/{hw['uuid']}/submissions/{student_uuid}/grade", json={"marks": "11"})
    assert r.status_code == 400  # more than max marks
    r = aarav.post(f"/lms/portal/homework/{hw['uuid']}/submit", json={"content": "Again"})
    assert r.status_code == 400
    detail = aarav.get(f"/lms/portal/homework/{hw['uuid']}").json()
    assert detail["state"] == "returned" and detail["submission"]["marks"] == "8.00"
    # teacher asks for another try: allowed again
    school.teacher.post(f"/lms/homework/{hw['uuid']}/submissions/{student_uuid}/resubmit", json={"note": "Fix step 3"})
    assert aarav.post(f"/lms/portal/homework/{hw['uuid']}/submit", json={"content": "Fixed"}).status_code == 200


def test_late_work_only_when_allowed(school):
    past = _iso(datetime.now(timezone.utc) - timedelta(hours=2))
    open_late = _homework(school, title="Late ok", allow_late=True)
    closed_late = _homework(school, title="No late", allow_late=False)
    for hw in (open_late, closed_late):
        assert school.teacher.put(f"/lms/homework/{hw['uuid']}", json={"due_at": past}).status_code == 200
    aarav = school.portal(school.logins["Aarav"][0])
    r = aarav.post(f"/lms/portal/homework/{open_late['uuid']}/submit", json={"content": "Sorry, late"})
    assert r.status_code == 200 and r.json()["state"] == "late"
    r = aarav.post(f"/lms/portal/homework/{closed_late['uuid']}/submit", json={"content": "Late"})
    assert r.status_code == 400
    offline = _homework(school, title="On paper", submission_type="offline")
    assert aarav.post(f"/lms/portal/homework/{offline['uuid']}/submit", json={"content": "x"}).status_code == 400


def test_portal_cannot_see_other_section_or_submit_for_others(school):
    hw = _homework(school)
    bela = school.portal(school.logins["Bela"][0])  # section B
    assert hw["uuid"] not in [h["uuid"] for h in bela.get("/lms/portal/homework").json()["items"]]
    assert bela.get(f"/lms/portal/homework/{hw['uuid']}").status_code == 404
    assert bela.post(f"/lms/portal/homework/{hw['uuid']}/submit", json={"content": "x"}).status_code == 404
    # asking for another learner's id is refused
    assert bela.get(f"/lms/portal/homework?student_id={school.student_ids['Aarav']}").status_code == 404
    # Bela's parent can't see Aarav either, and parents can't submit
    bela_parent = school.portal(school.logins["Bela"][1])
    assert bela_parent.get(f"/lms/portal/homework?student_id={school.student_ids['Aarav']}").status_code == 404
    aarav_parent = school.portal(school.logins["Aarav"][1])
    assert hw["uuid"] in [h["uuid"] for h in aarav_parent.get("/lms/portal/homework").json()["items"]]
    assert aarav_parent.post(f"/lms/portal/homework/{hw['uuid']}/submit", json={"content": "x"}).status_code == 403
    # students can't reach staff endpoints
    assert bela.get("/lms/homework").status_code == 403


# ── Quizzes ──────────────────────────────────────────────────────────

def _q(school, **body):
    r = school.teacher.post("/lms/questions", json={"subject_id": school.subject_id, **body})
    assert r.status_code == 201, r.text
    return r.json()


def test_quiz_attempts_auto_marking_and_limit(school):
    q1 = _q(school, kind="single", prompt="Input device?", options=[{"text": "Monitor"}, {"text": "Keyboard"}], answer=["b"])
    q2 = _q(school, kind="multiple", prompt="Storage?", options=[{"text": "HDD"}, {"text": "Mouse"}, {"text": "SSD"}],
            answer=["a", "c"], marks="2")
    q3 = _q(school, kind="numeric", prompt="g?", answer={"value": "9.8", "tolerance": "0.1"})
    q4 = _q(school, kind="long", prompt="Explain", marks="3")
    r = school.teacher.post("/lms/quizzes", json={
        "title": "Basics", "subject_id": school.subject_id, "section_ids": [school.sec_a], "attempts_allowed": 1,
        "time_limit_min": 10, "show_answers": "after_submit", "status": "published",
        "questions": [{"question_id": q["id"]} for q in (q1, q2, q3, q4)]})
    assert r.status_code == 201, r.text
    qz = r.json()
    assert qz["total_marks"] == "7.00"
    bela = school.portal(school.logins["Bela"][0])
    assert bela.post(f"/lms/portal/quizzes/{qz['uuid']}/start").status_code == 404  # other section
    aarav = school.portal(school.logins["Aarav"][0])
    att = aarav.post(f"/lms/portal/quizzes/{qz['uuid']}/start").json()
    assert all("answer" not in i for i in att["items"])
    answers = {str(q1["id"]): "b", str(q2["id"]): ["c", "a"], str(q3["id"]): "9.75", str(q4["id"]): "Because..."}
    assert aarav.put(f"/lms/portal/attempts/{att['uuid']}/answers", json={"answers": answers}).status_code == 200
    # starting again resumes the same attempt
    assert aarav.post(f"/lms/portal/quizzes/{qz['uuid']}/start").json()["uuid"] == att["uuid"]
    res = aarav.post(f"/lms/portal/attempts/{att['uuid']}/submit", json={"answers": {}}).json()
    assert res["status"] == "submitted"  # long answer waits for the teacher
    assert aarav.post(f"/lms/portal/quizzes/{qz['uuid']}/start").status_code == 400  # attempts used
    # teacher marks the long answer
    staff = school.teacher.get(f"/lms/quiz-attempts/{att['uuid']}").json()
    by_q = {i["question_id"]: i for i in staff["items"]}
    assert [Decimal(by_q[q["id"]]["score"]) for q in (q1, q2, q3)] == [1, 2, 1]
    r = school.teacher.post(f"/lms/quiz-attempts/{att['uuid']}/mark", json={"items": {str(q4["id"]): {"score": "2"}}})
    assert r.status_code == 200 and r.json()["status"] == "marked" and r.json()["score"] == "6.00"
    results = school.teacher.get(f"/lms/quizzes/{qz['uuid']}/results").json()
    assert results["summary"]["average"] is not None and len(results["items"]) == 4


# ── Online classes ───────────────────────────────────────────────────

def test_online_class_join_log(school):
    start = datetime.now(timezone.utc) + timedelta(minutes=5)
    r = school.teacher.post("/lms/online-classes", json={"title": "Live", "section_id": school.sec_a,
                                                          "subject_id": school.subject_id, "starts_at": _iso(start),
                                                          "duration_min": 40, "provider": "jitsi"})
    assert r.status_code == 201, r.text
    c = r.json()["items"][0]
    assert c["room_name"] and len(c["room_name"]) > 20
    aarav = school.portal(school.logins["Aarav"][0])
    info = aarav.post(f"/lms/portal/online-classes/{c['uuid']}/join").json()
    assert info["jitsi_domain"] == "meet.jit.si" and info["room_name"] == c["room_name"]
    bela = school.portal(school.logins["Bela"][0])
    assert bela.post(f"/lms/portal/online-classes/{c['uuid']}/join").status_code == 404
    att = school.teacher.get(f"/lms/online-classes/{c['uuid']}/attendance").json()
    assert att["present"] == 1
    # weekly series
    r = school.teacher.post("/lms/online-classes", json={
        "title": "Weekly", "section_id": school.sec_a, "starts_at": _iso(start + timedelta(days=1)),
        "repeat": "weekly", "repeat_until": (start + timedelta(days=22)).date().isoformat()})
    assert r.status_code == 201 and r.json()["created"] >= 3


# ── Tenant isolation ─────────────────────────────────────────────────

def test_lms_tenant_isolation(school, company_b):
    hw = _homework(school)
    assert company_b.get(f"/lms/homework/{hw['uuid']}").status_code == 404
    assert company_b.put(f"/lms/homework/{hw['uuid']}", json={"title": "Hacked"}).status_code == 404
    assert hw["uuid"] not in [h["uuid"] for h in company_b.get("/lms/homework").json()["items"]]
    q = _q(school, kind="true_false", prompt="Sky is blue", answer="true")
    assert company_b.get(f"/lms/questions/{q['uuid']}").status_code == 404
    db = tenant_session(company_b.id)
    try:
        from modules.lms.models import Assignment, BankQuestion
        assert db.query(Assignment).count() == 0 and db.query(BankQuestion).count() == 0
    finally:
        db.close()
