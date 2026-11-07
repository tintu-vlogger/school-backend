"""Foundation: academic set-up, students, enrolment, promotion and parent / student sign-in."""
import uuid

from tests.conftest import PortalSession, add_student, school_setup


def test_quick_setup_creates_structure(company_a):
    sections = school_setup(company_a)
    assert len(sections) == 4
    assert {s["label"] for s in sections} >= {"Grade 1 - A", "Grade 2 - B"}
    prof = company_a.get("/academics/profile").json()
    assert prof["type"] == "school" and prof["terms"]["program"] == "Class"
    assert prof["current_session"] is not None


def test_profile_words_follow_institution_type(company_a):
    r = company_a.put("/academics/profile", json={"type": "university"})
    assert r.status_code == 200
    assert r.json()["terms"]["program"] == "Programme" and r.json()["terms"]["section"] == "Batch"
    r = company_a.put("/academics/profile", json={"terms": {"teacher": "Professor"}, "modules": {"hostel": False}})
    assert r.json()["terms"]["teacher"] == "Professor" and r.json()["modules"]["hostel"] is False


def test_add_student_with_guardian_and_roll_numbers(company_a):
    sec = school_setup(company_a)[0]
    s1 = add_student(company_a, sec["id"], "Asha")
    s2 = add_student(company_a, sec["id"], "Bala")
    assert s1["admission_no"] and s1["admission_no"] != s2["admission_no"]
    assert s1["roll_no"] == "1" and s2["roll_no"] == "2"
    assert s1["guardians"][0]["relation"] == "father"
    roster = company_a.get(f"/students/section/{sec['id']}/roster").json()
    assert [r["name"] for r in roster] == ["Asha Test", "Bala Test"]


def test_section_capacity_is_enforced(company_a):
    sec = school_setup(company_a)[0]
    company_a.put(f"/academics/sections/{sec['uuid']}", json={"capacity": 1})
    add_student(company_a, sec["id"], "One")
    r = company_a.post("/students", json={"first_name": "Two", "section_id": sec["id"]})
    assert r.status_code == 400 and "full" in r.json()["detail"]


def test_parent_and_student_sign_in_and_see_only_their_own(company_a):
    sec = school_setup(company_a)[0]
    mine = add_student(company_a, sec["id"], "Mine", parent_email=f"p-{uuid.uuid4().hex[:6]}@example.com", portal=True)
    other = add_student(company_a, sec["id"], "Other")
    logins = {l["kind"]: l for l in mine["new_logins"]}
    assert set(logins) == {"student", "parent"}

    parent = PortalSession(company_a, logins["parent"]["login"], logins["parent"]["password"])
    assert parent.user["role"] == "parent" and parent.user["must_change_password"] is True
    kids = parent.get("/students/mine").json()
    assert [k["id"] for k in kids] == [mine["id"]]
    assert parent.get(f"/students/{mine['uuid']}").status_code == 200
    assert parent.get(f"/students/{other['uuid']}").status_code == 404
    assert parent.get("/students").status_code == 403
    me = parent.get("/auth/me").json()
    assert me["role"] == "parent" and me["guardian_id"]

    student = PortalSession(company_a, logins["student"]["login"], logins["student"]["password"])
    assert [k["id"] for k in student.get("/students/mine").json()] == [mine["id"]]
    assert student.get(f"/students/{other['uuid']}").status_code == 404


def test_portal_tokens_cannot_reach_staff_apis(company_a):
    sec = school_setup(company_a)[0]
    s = add_student(company_a, sec["id"], "Guard", parent_email=f"g-{uuid.uuid4().hex[:6]}@example.com", portal=True)
    login = next(l for l in s["new_logins"] if l["kind"] == "parent")
    parent = PortalSession(company_a, login["login"], login["password"])
    for path in ("/employees/", "/departments/", "/payroll/runs", "/academics/sections", "/roles/"):
        assert parent.get(path).status_code == 403, path
    assert parent.get("/notifications/unread-count").json()["count"] == 0


def test_first_password_change_without_current_password(company_a):
    sec = school_setup(company_a)[0]
    s = add_student(company_a, sec["id"], "Pw", parent_email=f"pw-{uuid.uuid4().hex[:6]}@example.com", portal=True)
    login = next(l for l in s["new_logins"] if l["kind"] == "student")
    st = PortalSession(company_a, login["login"], login["password"])
    r = st.post("/auth/change-password", json={"current_password": "", "new_password": "NewPass123#"})
    assert r.status_code == 200, r.text
    PortalSession(company_a, login["login"], "NewPass123#")
    st2 = PortalSession(company_a, login["login"], "NewPass123#")
    r = st2.post("/auth/change-password", json={"current_password": "", "new_password": "Other123#"})
    assert r.status_code == 400  # now the current password is required


def test_switched_off_portal_account_is_refused(company_a):
    sec = school_setup(company_a)[0]
    s = add_student(company_a, sec["id"], "Off", parent_email=f"off-{uuid.uuid4().hex[:6]}@example.com", portal=True)
    acc = s["portal_account"]
    login = next(l for l in s["new_logins"] if l["kind"] == "student")
    r = company_a.post(f"/portal-accounts/{acc['uuid']}/active", json={"active": False})
    assert r.status_code == 200
    r = company_a.client.post("/api/v1/auth/login", json={"email": login["login"], "password": login["password"]},
                              headers={"X-Company-Slug": company_a.slug})
    assert r.status_code == 401


def test_promotion_moves_learners_to_next_session(company_a):
    sec = school_setup(company_a)[0]
    s = add_student(company_a, sec["id"], "Promo")
    r = company_a.post("/academics/sessions", json={"name": "Next year", "start_date": "2099-04-01",
                                                   "end_date": "2100-03-31"})
    assert r.status_code == 201, r.text
    nxt = r.json()
    prog = company_a.get("/academics/programs?all=true").json()["items"][1]
    r = company_a.post("/academics/sections", json={"program_id": prog["id"], "session_id": nxt["id"], "name": "A"})
    assert r.status_code == 201, r.text
    target = r.json()
    r = company_a.post("/students/promote", json={"from_section_id": sec["id"], "to_section_id": target["id"],
                                                  "student_ids": [s["id"]], "outcome": "promoted"})
    assert r.status_code == 200 and r.json()["moved"] == 1
    detail = company_a.get(f"/students/{s['uuid']}").json()
    statuses = {e["session_id"]: e["status"] for e in detail["enrollments"]}
    assert statuses[nxt["id"]] == "enrolled" and "promoted" in statuses.values()


def test_students_are_separate_per_institution(company_a, company_b):
    sec = school_setup(company_a)[0]
    s = add_student(company_a, sec["id"], "Iso")
    school_setup(company_b)
    assert company_b.get(f"/students/{s['uuid']}").status_code == 404
    assert all(x["id"] != s["id"] for x in company_b.get("/students").json()["items"])
    assert company_b.get(f"/students/section/{sec['id']}/roster").json() == []
