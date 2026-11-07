"""Admissions and certificates: enquiry pipeline rules, application statuses, merit list and seat
limits, offer approval, conversion to a student, the public online form, certificates,
permissions and tenant isolation."""
import json
import uuid
from datetime import date, timedelta

from tests.conftest import login_as


def _setup(company, programs=("Grade 1", "Grade 2")):
    r = company.post("/academics/quick-setup", json={"institution_type": "school", "programs": list(programs),
                                                     "sections": ["A"], "subjects": ["English"]})
    assert r.status_code == 200, r.text
    prof = company.get("/academics/profile").json()
    sess_id = prof["current_session"]["id"]
    secs = company.get("/academics/section-options").json()
    prog = {}
    for s in secs:
        prog.setdefault(s["label"].split(" - ")[0], {"program_id": s["program_id"], "section_id": s["id"]})
    return sess_id, prog


def _offering(company, sess_id, program_id, seats=10, **extra):
    r = company.post("/admissions/offerings", json={"session_id": sess_id, "program_id": program_id, "seats": seats,
                                                     **extra})
    assert r.status_code == 201, r.text
    return r.json()


def _application(company, sess_id, program_id, first="Asha", **extra):
    body = {"session_id": sess_id, "program_id": program_id, "first_name": first, "last_name": "Rao",
            "date_of_birth": "2020-03-04", "gender": "female",
            "guardians": [{"relation": "father", "first_name": "Ravi", "last_name": "Rao", "phone": "9876500001",
                           "email": f"ravi-{uuid.uuid4().hex[:6]}@example.com", "is_primary": True},
                          {"relation": "mother", "first_name": "Rekha", "last_name": "Rao", "phone": "9876500002"}],
            **extra}
    r = company.post("/admissions/applications", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ── Enquiry pipeline ────────────────────────────────────────────────

def test_enquiry_pipeline_rules(company_a):
    sess_id, prog = _setup(company_a)
    pid = prog["Grade 1"]["program_id"]
    assert company_a.post("/admissions/enquiries", json={"child_first_name": "Kiran"}).status_code == 400
    r = company_a.post("/admissions/enquiries", json={"child_first_name": "Kiran", "phone": "9000011111",
                                                       "program_id": pid, "session_id": sess_id})
    assert r.status_code == 201, r.text
    e = r.json()
    assert e["stage"] == "new" and e["number"].startswith("ENQ")
    path = f"/admissions/enquiries/{e['uuid']}"
    # converted only through enrolment; applied only with an application
    assert company_a.post(f"{path}/stage", json={"stage": "converted"}).status_code == 400
    assert company_a.post(f"{path}/stage", json={"stage": "applied"}).status_code == 400
    # visit needs a date; lost needs a reason
    assert company_a.post(f"{path}/stage", json={"stage": "visit_scheduled"}).status_code == 400
    ok = company_a.post(f"{path}/stage", json={"stage": "visit_scheduled", "visit_at": "2030-01-05T10:00:00Z"})
    assert ok.status_code == 200 and ok.json()["stage"] == "visit_scheduled"
    assert company_a.post(f"{path}/stage", json={"stage": "lost"}).status_code == 400
    lost = company_a.post(f"{path}/stage", json={"stage": "lost", "lost_reason": "Fees too high"})
    assert lost.status_code == 200 and lost.json()["lost_reason"] == "Fees too high"
    # a lost enquiry reopens only as new or contacted
    assert company_a.post(f"{path}/stage", json={"stage": "visit_scheduled"}).status_code == 400
    again = company_a.post(f"{path}/stage", json={"stage": "contacted"})
    assert again.status_code == 200 and again.json()["lost_reason"] is None
    # starting an application moves it to applied
    app = company_a.post(f"{path}/application")
    assert app.status_code == 201, app.text
    assert company_a.get(path).json()["stage"] == "applied"
    assert company_a.post(f"{path}/application").status_code == 400
    detail = company_a.get(path).json()
    kinds = [a["kind"] for a in detail["activities"]]
    assert "stage" in kinds and detail["application"]["application_no"]


def test_first_call_moves_new_enquiry_to_contacted_and_board(company_a):
    _setup(company_a)
    e = company_a.post("/admissions/enquiries", json={"child_first_name": "Neel", "email": "neel@example.com"}).json()
    r = company_a.post(f"/admissions/enquiries/{e['uuid']}/activities",
                       json={"kind": "call", "body": "Shared the fee details", "next_follow_up": "2030-01-02"})
    assert r.status_code == 201, r.text
    assert r.json()["enquiry"]["stage"] == "contacted"
    board = company_a.get("/admissions/enquiries/board").json()["stages"]
    assert [s["key"] for s in board][:2] == ["new", "contacted"]
    assert any(i["uuid"] == e["uuid"] for i in board[1]["items"])


# ── Applications, merit and seats ───────────────────────────────────

def test_application_status_rules(company_a):
    sess_id, prog = _setup(company_a)
    a = _application(company_a, sess_id, prog["Grade 1"]["program_id"])
    assert a["application_no"].startswith("APP") and a["status"] == "submitted"
    p = f"/admissions/applications/{a['uuid']}/status"
    assert company_a.post(p, json={"status": "offered"}).status_code == 400
    assert company_a.post(p, json={"status": "enrolled"}).status_code == 400
    assert company_a.post(p, json={"status": "accepted"}).status_code == 400
    assert company_a.post(p, json={"status": "rejected"}).status_code == 400  # needs a reason
    assert company_a.post(p, json={"status": "rejected", "reason": "Not eligible"}).json()["status"] == "rejected"
    assert company_a.post(p, json={"status": "shortlisted"}).status_code == 400
    assert company_a.post(p, json={"status": "under_review"}).json()["status"] == "under_review"


def test_merit_list_and_seat_limit(company_a):
    sess_id, prog = _setup(company_a)
    pid = prog["Grade 1"]["program_id"]
    a = _application(company_a, sess_id, pid, "Low")
    assert company_a.post(f"/admissions/applications/{a['uuid']}/offer", json={}).status_code == 400  # not open yet
    _offering(company_a, sess_id, pid, seats=2)
    b = _application(company_a, sess_id, pid, "Top")
    c = _application(company_a, sess_id, pid, "Mid")
    for app, score in ((a, 40), (b, 95), (c, 70)):
        r = company_a.post(f"/admissions/applications/{app['uuid']}/scores",
                           json={"test_score": score, "test_max": 100, "interview_score": 8, "interview_max": 10})
        assert r.status_code == 200, r.text
    ranked = company_a.post("/admissions/merit/rank", json={"session_id": sess_id, "program_id": pid}).json()
    order = [i["first_name"] for i in ranked["items"]]
    assert order == ["Top", "Mid", "Low"] and [i["merit_rank"] for i in ranked["items"]] == [1, 2, 3]
    res = company_a.post("/admissions/merit/offer", json={"session_id": sess_id, "program_id": pid,
                                                          "waitlist_rest": True}).json()
    assert res["offered"] == 2 and res["waitlisted"] == 1 and res["seats"]["available"] == 0
    low = company_a.get(f"/admissions/applications/{a['uuid']}").json()
    assert low["status"] == "waitlisted"
    r = company_a.post(f"/admissions/applications/{a['uuid']}/offer", json={})
    assert r.status_code == 400 and "No seats left" in r.json()["detail"]
    # a seat frees up when an offer is declined
    company_a.post(f"/admissions/applications/{c['uuid']}/status", json={"status": "withdrawn", "reason": "Moved city"})
    r = company_a.post(f"/admissions/applications/{a['uuid']}/offer", json={})
    assert r.status_code == 200 and r.json()["status"] == "offered"
    off = company_a.get("/admissions/offerings", params={"session_id": sess_id}).json()[0]
    assert off["taken"] == 2 and off["available"] == 0


def test_fee_required_before_offer(company_a):
    sess_id, prog = _setup(company_a)
    pid = prog["Grade 1"]["program_id"]
    _offering(company_a, sess_id, pid, seats=5, application_fee="500.00", fee_required=True)
    a = _application(company_a, sess_id, pid)
    assert a["fee_amount"] == "500.00"
    assert company_a.post(f"/admissions/applications/{a['uuid']}/offer", json={}).status_code == 400
    company_a.post(f"/admissions/applications/{a['uuid']}/fee", json={"paid": True, "reference": "R-1"})
    assert company_a.post(f"/admissions/applications/{a['uuid']}/offer", json={}).json()["status"] == "offered"


def test_offer_goes_through_approval_flow_when_set_up(company_a):
    sess_id, prog = _setup(company_a)
    pid = prog["Grade 1"]["program_id"]
    _offering(company_a, sess_id, pid, seats=1)
    head = company_a.add_employee("Hari", "Head", role="hr")
    r = company_a.post("/workflow/definitions", json={
        "request_type": "admissions.offer", "name": "Head approves offers",
        "steps": [{"name": "Head", "approver_type": "employee", "approver_value": head["id"], "mode": "any"}]})
    assert r.status_code == 201, r.text
    a = _application(company_a, sess_id, pid)
    b = _application(company_a, sess_id, pid, "Second")
    out = company_a.post(f"/admissions/applications/{a['uuid']}/offer", json={}).json()
    assert out["status"] == "submitted" and out["offer_state"] == "pending"
    # the pending offer holds the only seat
    assert company_a.post(f"/admissions/applications/{b['uuid']}/offer", json={}).status_code == 400
    approver = login_as(company_a, head)
    tasks = approver.get("/workflow/inbox").json()["items"]
    task = next(t for t in tasks if t["request"]["request_type"] == "admissions.offer")
    d = approver.post(f"/workflow/tasks/{task['uuid']}/decision", json={"decision": "approve"})
    assert d.status_code == 200, d.text
    after = company_a.get(f"/admissions/applications/{a['uuid']}").json()
    assert after["status"] == "offered" and after["offer_state"] == "approved"


# ── Conversion to a student ─────────────────────────────────────────

def test_enrol_creates_student_guardians_and_enrolment(company_a):
    sess_id, prog = _setup(company_a)
    pid, sec = prog["Grade 1"]["program_id"], prog["Grade 1"]["section_id"]
    other_sec = prog["Grade 2"]["section_id"]
    _offering(company_a, sess_id, pid, seats=3)
    e = company_a.post("/admissions/enquiries", json={"child_first_name": "Meera", "phone": "9000022222",
                                                       "parent_name": "Suresh Iyer", "program_id": pid,
                                                       "session_id": sess_id}).json()
    a = company_a.post(f"/admissions/enquiries/{e['uuid']}/application").json()
    company_a.put(f"/admissions/applications/{a['uuid']}", json={
        "date_of_birth": "2020-01-15", "guardians": [
            {"relation": "father", "first_name": "Suresh", "last_name": "Iyer", "phone": "9000022222",
             "email": f"suresh-{uuid.uuid4().hex[:6]}@example.com", "is_primary": True},
            {"relation": "mother", "first_name": "Lata", "last_name": "Iyer", "phone": "9000022223"}]})
    enrol = f"/admissions/applications/{a['uuid']}/enrol"
    assert company_a.post(enrol, json={"section_id": sec}).status_code == 400  # not offered yet
    assert company_a.post(f"/admissions/applications/{a['uuid']}/offer", json={}).status_code == 200
    assert company_a.post(f"/admissions/applications/{a['uuid']}/accept").json()["status"] == "accepted"
    assert company_a.post(enrol, json={"section_id": other_sec}).status_code == 400  # wrong program
    r = company_a.post(enrol, json={"section_id": sec, "create_portal_accounts": True})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "enrolled" and out["student"]["admission_no"]
    assert {l["kind"] for l in out["new_logins"]} >= {"student", "parent"} or out["new_logins"]
    st = company_a.get(f"/students/{out['student']['uuid']}").json()
    assert st["first_name"] == "Meera" and st["date_of_birth"] == "2020-01-15"
    assert {g["relation"] for g in st["guardians"]} == {"father", "mother"}
    assert any(g["is_primary"] and g["first_name"] == "Suresh" for g in st["guardians"])
    assert st["enrollments"][0]["section_id"] == sec and st["enrollments"][0]["status"] == "enrolled"
    assert company_a.get(f"/admissions/enquiries/{e['uuid']}").json()["stage"] == "converted"
    assert company_a.post(enrol, json={"section_id": sec}).status_code == 400  # only once


# ── Public online application ───────────────────────────────────────

def _public_apply(client, slug, sess_id, pid, phone="9811100000", files=True, **extra):
    data = {"session_id": sess_id, "program_id": pid, "first_name": "Tara", "last_name": "Das",
            "date_of_birth": "2020-05-05", "gender": "female", "contact_phone": phone,
            "contact_email": "tara.parent@example.com", "consent": True,
            "guardians": [{"relation": "mother", "first_name": "Rina", "last_name": "Das", "phone": phone}], **extra}
    up = {}
    if files:
        up = {"doc_birth_certificate": ("birth.pdf", b"%PDF-1.4 test", "application/pdf"),
              "doc_photo": ("photo.png", b"\x89PNG fake image", "image/png")}
    return client.post(f"/api/v1/public/admissions/{slug}/apply", data={"data": json.dumps(data)}, files=up or None)


def test_public_application_without_sign_in(client, company_a, company_b):
    sess_id, prog = _setup(company_a)
    pid = prog["Grade 1"]["program_id"]
    form = client.get(f"/api/v1/public/admissions/{company_a.slug}/form")
    assert form.status_code == 200 and form.json()["open"] is False  # nothing open yet
    _offering(company_a, sess_id, pid, seats=5, application_fee="250.00")
    form = client.get(f"/api/v1/public/admissions/{company_a.slug}/form").json()
    assert form["open"] and form["programs"][0]["program_id"] == pid
    assert any(d["key"] == "birth_certificate" and d["required"] for d in form["documents"])
    # required documents are checked
    assert _public_apply(client, company_a.slug, sess_id, pid, files=False).status_code == 400
    r = _public_apply(client, company_a.slug, sess_id, pid)
    assert r.status_code == 201, r.text
    no = r.json()["application_no"]
    assert no
    # a duplicate is refused
    assert _public_apply(client, company_a.slug, sess_id, pid).status_code == 400
    # staff of A see it as an online application with documents attached
    items = company_a.get("/admissions/applications", params={"q": no}).json()["items"]
    assert len(items) == 1 and items[0]["channel"] == "online"
    full = company_a.get(f"/admissions/applications/{items[0]['uuid']}").json()
    assert next(d for d in full["documents"] if d["key"] == "birth_certificate")["uuid"]
    # company B never sees it, and B's address does not accept A's programs
    assert company_b.get("/admissions/applications", params={"q": no}).json()["total"] == 0
    assert company_b.get(f"/admissions/applications/{items[0]['uuid']}").status_code == 404
    assert _public_apply(client, company_b.slug, sess_id, pid, phone="9811100099").status_code == 400
    # status check needs the matching phone or email, and only on the right institution
    st = f"/api/v1/public/admissions/{company_a.slug}/status"
    assert client.post(st, json={"application_no": no, "contact": "9999999999"}).status_code == 404
    assert client.post(f"/api/v1/public/admissions/{company_b.slug}/status",
                       json={"application_no": no, "contact": "9811100000"}).status_code == 404
    ok = client.post(st, json={"application_no": no.lower(), "contact": "+91 98111 00000"})
    assert ok.status_code == 200 and ok.json()["status_label"] == "Received"
    assert "contact_email" not in ok.json() and "guardians" not in ok.json()
    assert client.post(st, json={"application_no": no, "contact": "TARA.PARENT@example.com"}).status_code == 200
    assert client.get("/api/v1/public/admissions/no-such-place-xyz/form").status_code == 404


def test_public_enquiry_becomes_website_lead(client, company_a):
    _setup(company_a)
    r = client.post(f"/api/v1/public/admissions/{company_a.slug}/enquiry",
                    json={"child_first_name": "Ira", "parent_name": "Anil Shah", "phone": "9822200000"})
    assert r.status_code == 201, r.text
    items = company_a.get("/admissions/enquiries", params={"q": "Ira"}).json()["items"]
    assert len(items) == 1 and items[0]["source_label"] == "Website"


# ── Certificates ────────────────────────────────────────────────────

def _student(company, section_id):
    r = company.post("/students", json={"first_name": "Rahul", "last_name": "Verma", "gender": "male",
                                        "date_of_birth": "2015-07-09", "section_id": section_id,
                                        "guardians": [{"first_name": "Vijay", "last_name": "Verma", "relation": "father",
                                                       "phone": "9833300000", "is_primary": True}]})
    assert r.status_code == 201, r.text
    return r.json()


def test_certificates_issue_register_cancel_and_tc_marks_transferred(company_a):
    sess_id, prog = _setup(company_a)
    s = _student(company_a, prog["Grade 1"]["section_id"])
    templates = company_a.get("/certificates/templates").json()
    kinds = {t["kind"]: t for t in templates}
    assert {"transfer", "bonafide", "character"} <= set(kinds)
    tc = kinds["transfer"]
    assert company_a.post("/certificates/issued", json={"template_id": tc["id"], "student_id": s["id"]}).status_code == 400
    r = company_a.post("/certificates/issued", json={
        "template_id": tc["id"], "student_id": s["id"], "mark_student": True,
        "values": {"date_of_leaving": date.today().isoformat(), "reason": "Parent transferred"}})
    assert r.status_code == 201, r.text
    cert = r.json()
    assert cert["number"].startswith("CERT/") and cert["marked_student"] == "transferred"
    st = company_a.get(f"/students/{s['uuid']}").json()
    assert st["status"] == "transferred" and st["leaving_reason"] == "Parent transferred"
    detail = company_a.get(f"/certificates/issued/{cert['uuid']}").json()
    assert "Rahul Verma" in detail["html"] and "Vijay Verma" in detail["html"]
    pdf = company_a.get(f"/certificates/issued/{cert['uuid']}/pdf")
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    bona = company_a.post("/certificates/issued", json={"template_id": kinds["bonafide"]["id"], "student_id": s["id"]})
    assert bona.status_code == 201
    assert bona.json()["number"] != cert["number"]
    reg = company_a.get("/certificates/issued").json()
    assert reg["total"] == 2
    # no hard delete; cancel needs a reason and stays in the register
    assert company_a.delete(f"/certificates/issued/{cert['uuid']}").status_code == 405
    assert company_a.post(f"/certificates/issued/{cert['uuid']}/cancel", json={"reason": ""}).status_code == 422
    c = company_a.post(f"/certificates/issued/{cert['uuid']}/cancel", json={"reason": "Issued in error"})
    assert c.status_code == 200 and c.json()["status"] == "cancelled"
    assert company_a.post(f"/certificates/issued/{cert['uuid']}/cancel", json={"reason": "Again"}).status_code == 400
    assert company_a.get("/certificates/issued", params={"status": "cancelled"}).json()["total"] == 1


# ── Permissions and isolation ───────────────────────────────────────

def test_permissions(company_a):
    _setup(company_a)
    teacher = login_as(company_a, company_a.add_employee("Tina", "Staff", role="employee"))
    assert teacher.get("/admissions/enquiries").status_code == 403
    assert teacher.post("/admissions/enquiries", json={"child_first_name": "X", "phone": "9000000000"}).status_code == 403
    assert teacher.get("/certificates/issued").status_code == 403
    hr = login_as(company_a, company_a.add_employee("Hema", "Office", role="hr"))
    assert hr.get("/admissions/enquiries").status_code == 200
    assert hr.post("/admissions/enquiries", json={"child_first_name": "X", "phone": "9000000000"}).status_code == 201
    acct = login_as(company_a, company_a.add_employee("Arun", "Head", role="manager"))
    assert acct.get("/admissions/applications").status_code == 200
    assert acct.post("/admissions/enquiries", json={"child_first_name": "Y", "phone": "9000000001"}).status_code == 403


def test_tenant_isolation(company_a, company_b):
    sess_a, prog_a = _setup(company_a)
    _setup(company_b)
    pid = prog_a["Grade 1"]["program_id"]
    off = _offering(company_a, sess_a, pid, seats=3)
    e = company_a.post("/admissions/enquiries", json={"child_first_name": "Secret", "phone": "9000033333"}).json()
    a = _application(company_a, sess_a, pid)
    s = _student(company_a, prog_a["Grade 1"]["section_id"])
    tpl = company_a.get("/certificates/templates").json()[0]
    cert = company_a.post("/certificates/issued", json={"template_id": next(
        t for t in company_a.get("/certificates/templates").json() if t["kind"] == "bonafide")["id"],
        "student_id": s["id"]}).json()
    assert company_b.get(f"/admissions/enquiries/{e['uuid']}").status_code == 404
    assert company_b.post(f"/admissions/enquiries/{e['uuid']}/stage", json={"stage": "contacted"}).status_code == 404
    assert company_b.get("/admissions/enquiries", params={"q": "Secret"}).json()["total"] == 0
    assert company_b.get(f"/admissions/applications/{a['uuid']}").status_code == 404
    assert company_b.put(f"/admissions/offerings/{off['uuid']}", json={"seats": 99}).status_code == 404
    assert company_b.get(f"/certificates/issued/{cert['uuid']}").status_code == 404
    assert company_b.get(f"/certificates/templates/{tpl['uuid']}").status_code == 404
    assert company_b.post(f"/certificates/issued/{cert['uuid']}/cancel", json={"reason": "x y"}).status_code == 404
    # B cannot use A's ids in its own records
    assert company_b.post("/admissions/enquiries", json={"child_first_name": "Z", "phone": "9000000000",
                                                          "program_id": pid}).status_code == 400
    b_tpl = company_b.get("/certificates/templates").json()[0]
    assert company_b.post("/certificates/issued", json={"template_id": b_tpl["id"], "student_id": s["id"]}).status_code == 400
    assert company_b.post("/certificates/issued", json={"template_id": tpl["id"], "student_id": s["id"]}).status_code == 400
    assert company_b.get("/admissions/summary").json()["enquiries"] == 0
