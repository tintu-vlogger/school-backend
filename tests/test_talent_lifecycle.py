"""Talent lifecycle: hiring pipeline, screening, offers (approval outside the band, e-sign,
acceptance → pre-boarding), onboarding, probation (auto review start, extension limits),
exits (resignation → clearance → settlement handoff with the 2-working-day deadline,
login disabled at exit), candidate data purge and tenant isolation."""
from datetime import date, timedelta

from sqlalchemy import text

from core.db import system_session, tenant_session
from modules.recruitment import screening
from tests.conftest import login_as


# ── helpers ─────────────────────────────────────────────────────────

def _job(company, **extra):
    body = {"title": "Backend Engineer", "description": "<p>Build APIs.</p>", "skills": ["Python", "Django", "SQL"],
            "nice_skills": ["Docker"], "min_experience": 3, "max_experience": 6, "salary_min": 1000000,
            "salary_max": 1200000, "work_mode": "remote", **extra}
    r = company.post("/recruitment/jobs", json=body)
    assert r.status_code == 201, r.text
    job = r.json()
    r = company.post(f"/recruitment/jobs/{job['uuid']}/status", json={"action": "publish"})
    assert r.status_code == 200, r.text
    return r.json()


def _candidate(company, job=None, **extra):
    import uuid
    body = {"first_name": "Asha", "last_name": "Rao", "email": f"asha-{uuid.uuid4().hex[:6]}@example.com",
            "total_experience": 4, "skills": ["Python", "Django", "SQL"], **extra}
    if job:
        body["job_uuid"] = job["uuid"]
    r = company.post("/recruitment/candidates", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _today(company) -> date:
    db = tenant_session(company.id)
    try:
        from core.timeutil import today_for_company_id
        return today_for_company_id(db, company.id)
    finally:
        db.close()


# ── screening (pure) ────────────────────────────────────────────────

def test_screening_score_explains_itself():
    job = {"skills": ["Python", "Django", "SQL"], "nice_skills": ["Docker"], "min_experience": 3, "max_experience": 6,
           "work_mode": "office", "location": "Bengaluru"}
    strong = screening.score(job, {"skills": ["Python", "Django"], "total_experience": 4, "location": "Bengaluru",
                                   "resume_text": "Worked with SQL and Docker daily"})
    weak = screening.score(job, {"skills": ["Java"], "total_experience": 1, "location": "Delhi", "resume_text": ""})
    assert strong["score"] == 100
    assert strong["scorer"] == "rules"
    assert weak["score"] < 40
    parts = {p["key"]: p for p in weak["parts"]}
    assert parts["skills"]["missing"] == ["Python", "Django", "SQL"]
    assert "below the 3-year minimum" in parts["experience"]["detail"]
    assert "weak match" in weak["explanation"]
    # "Java" must not match "JavaScript"
    js = screening.score({"skills": ["Java"]}, {"skills": [], "resume_text": "JavaScript developer"})
    assert {p["key"]: p for p in js["parts"]}["skills"]["matched"] == []
    # a plug-in scorer can replace the rules without changing callers
    screening.register("fixed", lambda j, c, w: {"score": 77, "scorer": "fixed", "parts": [], "explanation": "x"})
    assert screening.score(job, {}, scorer="fixed")["score"] == 77


def test_resume_parsing_fills_fields():
    from modules.recruitment import resume
    text = ("Ravi Kumar\nravi.kumar@gmail.com | +91 98450 12345 | Bengaluru\nlinkedin.com/in/ravikumar\n"
            "6 years of experience. Working as Senior Engineer at Acme Corp.\nSkills: Python, Django, PostgreSQL\n"
            "B.Tech in Computer Science\nNotice period: 2 months")
    f = resume.parse(text)
    assert f["email"] == "ravi.kumar@gmail.com" and f["phone"] == "9845012345"
    assert (f["first_name"], f["last_name"]) == ("Ravi", "Kumar")
    assert f["total_experience"] == 6 and "Python" in f["skills"] and f["location"] == "Bengaluru"
    assert f["notice_days"] == 60 and "B.Tech" in f["education"]


# ── pipeline ────────────────────────────────────────────────────────

def test_pipeline_stage_moves(company_a):
    job = _job(company_a)
    cand = _candidate(company_a, job)
    app = cand["applications"][0]
    assert app["stage"] == "applied" and app["score"] is not None
    a2 = _candidate(company_a, job, first_name="Bina")["applications"][0]

    r = company_a.post(f"/recruitment/applications/{app['uuid']}/move", json={"stage": "screening"})
    assert r.status_code == 200 and r.json()["stage"] == "screening"
    # place the second card above the first in the same column
    company_a.post(f"/recruitment/applications/{a2['uuid']}/move", json={"stage": "screening", "before_uuid": app["uuid"]})
    board = company_a.get(f"/recruitment/jobs/{job['uuid']}/board").json()
    col = sorted([c for c in board["cards"] if c["stage"] == "screening"], key=lambda c: c["position"])
    assert [c["uuid"] for c in col] == [a2["uuid"], app["uuid"]]

    assert company_a.post(f"/recruitment/applications/{app['uuid']}/move", json={"stage": "nowhere"}).status_code == 400
    r = company_a.post(f"/recruitment/applications/{app['uuid']}/move", json={"stage": "hired"})
    assert r.status_code == 400 and "offer" in r.json()["detail"]
    assert company_a.post(f"/recruitment/applications/{app['uuid']}/move", json={"stage": "interview"}).status_code == 200

    assert company_a.post(f"/recruitment/applications/{app['uuid']}/reject", json={}).status_code == 400
    assert company_a.post(f"/recruitment/applications/{app['uuid']}/reject", json={"reason": "Too far"}).status_code == 200
    assert company_a.post(f"/recruitment/applications/{app['uuid']}/move", json={"stage": "offer"}).status_code == 400
    detail = company_a.get(f"/recruitment/applications/{app['uuid']}").json()
    assert detail["status"] == "rejected"
    assert any("Moved from Screening to Interviews" in h["text"] for h in detail["history"])
    # an employee outside the hiring team can't move cards
    emp = company_a.add_employee("Outsider")
    assert login_as(company_a, emp).post(f"/recruitment/applications/{a2['uuid']}/move",
                                         json={"stage": "interview"}).status_code == 404


def test_interview_scorecards_and_evaluation(company_a):
    job = _job(company_a)
    app = _candidate(company_a, job)["applications"][0]
    i1 = company_a.add_employee("Ivy")
    from datetime import datetime, timezone
    when = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0).isoformat()
    r = company_a.post(f"/recruitment/applications/{app['uuid']}/interviews",
                       json={"round_name": "Tech", "starts_at": when, "duration_minutes": 45, "panel": [i1["id"]]})
    assert r.status_code == 201, r.text
    iv = r.json()
    assert iv["scorecards_total"] == 1
    s = login_as(company_a, i1)
    assert s.get("/recruitment/interviews").json()["items"][0]["uuid"] == iv["uuid"]
    comp = iv["kit"]["competencies"][0]["key"]
    r = s.post(f"/recruitment/interviews/{iv['uuid']}/scorecard",
               json={"ratings": {comp: 5}, "overall": 5, "recommendation": "strong_yes", "strengths": "Sharp"})
    assert r.status_code == 200, r.text
    ev = company_a.get(f"/recruitment/applications/{app['uuid']}").json()["evaluation"]
    assert ev["count"] == 1 and ev["average"] == 5 and ev["recommendation"] == "hire"
    ics = company_a.get(f"/recruitment/interviews/{iv['uuid']}/invite.ics")
    assert ics.status_code == 200 and "BEGIN:VEVENT" in ics.text
    r = company_a.post(f"/recruitment/applications/{app['uuid']}/evaluation", json={"recommendation": "hold"})
    assert r.status_code == 400  # needs a note
    r = company_a.post(f"/recruitment/applications/{app['uuid']}/evaluation", json={"recommendation": "hold", "note": "Wait"})
    assert r.json()["evaluation_override"]["recommendation"] == "hold"


# ── offers ──────────────────────────────────────────────────────────

def _decide_inbox(session, request_type, decision="approve"):
    items = [t for t in session.get("/workflow/inbox").json()["items"] if t["request"]["request_type"] == request_type]
    assert items, f"no {request_type} task"
    r = session.post(f"/workflow/tasks/{items[0]['uuid']}/decision", json={"decision": decision, "comment": "ok"})
    assert r.status_code == 200, r.text
    return r.json()


def test_offer_outside_band_needs_approval(company_a):
    job = _job(company_a)
    app = _candidate(company_a, job)["applications"][0]
    hr = company_a.add_employee("Hema", role="hr")
    hs = login_as(company_a, hr)
    joining = (_today(company_a) + timedelta(days=20)).isoformat()
    # inside the band: approved at once
    inside = hs.post(f"/recruitment/applications/{app['uuid']}/offers",
                     json={"joining_date": joining, "annual_ctc": 1100000, "submit": True})
    assert inside.status_code == 201, inside.text
    assert inside.json()["status"] == "approved" and inside.json()["outside_band"] is False
    assert hs.post(f"/recruitment/offers/{inside.json()['uuid']}/withdraw", json={"reason": "Re-do"}).status_code == 200
    # outside the band: waits for the HR admin
    o = hs.post(f"/recruitment/applications/{app['uuid']}/offers",
                json={"joining_date": joining, "annual_ctc": 1500000, "submit": True}).json()
    assert o["outside_band"] is True and o["status"] == "pending_approval"
    assert sum(float(b["monthly"]) for b in o["breakup"] if b["kind"] == "earning") > 0
    assert hs.post(f"/recruitment/offers/{o['uuid']}/send", json={}).status_code == 400
    _decide_inbox(company_a, "recruitment.offer")
    assert company_a.get(f"/recruitment/offers/{o['uuid']}").json()["status"] == "approved"


def test_offer_acceptance_creates_preboarding_employee(company_a, client):
    job = _job(company_a, salary_min=None, salary_max=None)
    cand = _candidate(company_a, job, first_name="Neel", last_name="Shah", phone="9845011111")
    app = cand["applications"][0]
    joining = _today(company_a) + timedelta(days=15)
    o = company_a.post(f"/recruitment/applications/{app['uuid']}/offers",
                       json={"joining_date": joining.isoformat(), "annual_ctc": 900000, "submit": True}).json()
    assert o["status"] == "approved"
    sent = company_a.post(f"/recruitment/offers/{o['uuid']}/send", json={}).json()
    assert sent["status"] == "sent" and sent["letter"]["file"]
    token = sent["portal_link"].rsplit("/", 1)[1]
    page = client.get(f"/api/v1/public/offers/{token}").json()
    assert page["esign"]["required"] and not page["esign"]["signed"]
    assert client.post(f"/api/v1/public/offers/{token}/accept", json={"typed_name": "Neel Shah"}).status_code == 400
    sign_token = page["esign"]["token"]
    code = client.post(f"/api/v1/public/esign/{sign_token}/otp").json()["dev_code"]
    r = client.post(f"/api/v1/public/esign/{sign_token}/sign", json={"code": code, "typed_name": "Neel Shah"})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/v1/public/offers/{token}/accept", json={"typed_name": "Neel Shah"})
    assert r.status_code == 200 and r.json()["status"] == "accepted"

    detail = company_a.get(f"/recruitment/offers/{o['uuid']}").json()
    emp_id = detail["employee_id"]
    assert emp_id
    emps = company_a.get("/employees/", params={"q": "Neel"}).json()
    emp = next(e for e in (emps["items"] if isinstance(emps, dict) else emps) if e["id"] == emp_id)
    assert emp["status"] == "pre_joining" and emp["date_of_joining"] == joining.isoformat()
    assert emp["email"] == cand["email"] and emp["first_name"] == "Neel"
    app_d = company_a.get(f"/recruitment/applications/{app['uuid']}").json()
    assert app_d["status"] == "hired" and app_d["stage"] == "hired"
    ob = company_a.get("/onboarding/onboardings").json()["items"]
    mine = next(x for x in ob if x["employee_id"] == emp_id)
    assert mine["status"] == "pre_joining"
    full = company_a.get(f"/onboarding/onboardings/{mine['uuid']}").json()
    assert full["portal_link"] and full["tasks"]["total"] > 0
    # the joiner fills the portal, HR accepts and the details reach the employee record
    ptoken = full["portal_link"].rsplit("/", 1)[1]
    base = f"/api/v1/public/joining/{ptoken}"
    assert client.put(f"{base}/sections/statutory", json={"data": {"pan": "ABCPS1234K"}}).status_code == 200
    assert client.put(f"{base}/sections/bank", json={"data": {"account_holder": "Neel Shah",
                                                              "account_number": "123456789012", "ifsc": "HDFC0001234"}}).status_code == 200
    assert client.put(f"{base}/sections/nominees", json={"data": [{"kind": "pf", "name": "Rina", "share_percent": 60}]}).status_code == 400
    r = company_a.post(f"/onboarding/onboardings/{mine['uuid']}/review",
                       json={"items": [{"key": "statutory.pan", "status": "sent_back", "remark": "Upload a clear copy"}]})
    assert r.json()["review_status"] == "sent_back"
    company_a.post(f"/onboarding/onboardings/{mine['uuid']}/accept-all")
    db = tenant_session(company_a.id)
    try:
        pan = db.execute(text("SELECT pan FROM employees WHERE id = :i"), {"i": emp_id}).scalar()
        banks = db.execute(text("SELECT count(*) FROM employee_bank_accounts WHERE employee_id = :i"), {"i": emp_id}).scalar()
    finally:
        db.close()
    assert pan == "ABCPS1234K" and banks == 1
    # onboarding can't complete without the appointment letter
    r = company_a.post(f"/onboarding/onboardings/{mine['uuid']}/complete", json={"force": True})
    assert r.status_code == 400 and "appointment letter" in r.json()["detail"]


# ── probation ───────────────────────────────────────────────────────

def test_probation_auto_review_and_extension_limits(company_a):
    t0 = _today(company_a)
    mgr = company_a.add_employee("Manu", role="manager")
    emp = company_a.add_employee("Pia", status="probation", date_of_joining=(t0 - timedelta(days=170)).isoformat(),
                                 probation_end_date=(t0 + timedelta(days=10)).isoformat(), reporting_manager_id=mgr["id"])
    db = system_session()
    try:
        from modules.probation import service as probation
        probation.run_daily(db)  # creates the record and starts the review (10 days left < 15)
        db.commit()
    finally:
        db.close()
    rows = company_a.get("/probation/probations").json()["items"]
    p = next(x for x in rows if x["employee_id"] == emp["id"])
    assert p["status"] == "in_review" and p["reviews_total"] >= 1
    ms = login_as(company_a, mgr)
    mine = ms.get("/probation/my-reviews").json()["items"]
    assert mine and mine[0]["probation"]["employee_id"] == emp["id"]
    r = ms.post(f"/probation/reviews/{mine[0]['uuid']}", json={"rating": 3, "recommendation": "extend",
                                                               "comments": "Needs more time on ownership"})
    assert r.status_code == 200, r.text
    # one extension of 3 months is allowed (policy: max 1, max 12 months in total)
    r = company_a.post(f"/probation/probations/{p['uuid']}/decide", json={"outcome": "extend", "reason": "Ownership"})
    assert r.status_code == 200, r.text
    ext = r.json()
    assert ext["status"] == "extended" and ext["extensions"] == 1
    assert date.fromisoformat(ext["end_date"]) > t0 + timedelta(days=80)
    r = company_a.post(f"/probation/probations/{p['uuid']}/decide", json={"outcome": "extend", "reason": "Again"})
    assert r.status_code == 400 and "already been extended" in r.json()["detail"]
    # confirm writes the confirmation date back to the employee record
    cdate = (date.fromisoformat(ext["end_date"]) + timedelta(days=1)).isoformat()
    r = company_a.post(f"/probation/probations/{p['uuid']}/decide", json={"outcome": "confirm", "confirmation_date": cdate})
    assert r.status_code == 200 and r.json()["status"] == "confirmed"
    db = tenant_session(company_a.id)
    try:
        st, cd = db.execute(text("SELECT status, confirmation_date FROM employees WHERE id = :i"), {"i": emp["id"]}).first()
    finally:
        db.close()
    assert st == "confirmed" and cd.isoformat() == cdate


def test_probation_hold_needs_reason(company_a):
    t0 = _today(company_a)
    emp = company_a.add_employee("Om", status="probation", date_of_joining=(t0 - timedelta(days=20)).isoformat(),
                                 probation_end_date=(t0 + timedelta(days=160)).isoformat())
    p = company_a.post("/probation/probations", json={"employee_id": emp["id"]}).json()
    assert p["status"] == "active" and p["end_date"] == (t0 + timedelta(days=160)).isoformat()
    assert company_a.post(f"/probation/probations/{p['uuid']}/decide", json={"outcome": "hold"}).status_code == 400
    r = company_a.post(f"/probation/probations/{p['uuid']}/decide", json={"outcome": "hold", "reason": "Attendance"})
    assert r.json()["status"] == "on_hold"


# ── exits ───────────────────────────────────────────────────────────

def test_resignation_clearance_settlement_handoff(company_a):
    t0 = _today(company_a)
    emp = company_a.add_employee("Ravi", date_of_joining=(t0 - timedelta(days=800)).isoformat(), notice_period_days=30)
    es = login_as(company_a, emp)
    # resign, withdraw before acceptance, resign again
    r = es.post("/exits/me/resign", json={"reason_text": "Higher studies"})
    assert r.status_code == 201, r.text
    assert r.json()["exit"]["status"] == "requested"
    assert es.post("/exits/me/withdraw", json={"reason": "Changed my mind"}).json()["exit"]["status"] == "withdrawn"
    lwd = t0 + timedelta(days=20)
    r = es.post("/exits/me/resign", json={"reason_text": "Higher studies", "proposed_last_day": lwd.isoformat()})
    x = r.json()["exit"]
    assert x["notice_required_days"] == 30 and x["notice_shortfall_days"] == 10
    # no manager and no HR employee: the owner accepts both steps
    _decide_inbox(company_a, "offboarding.resignation")
    while company_a.get(f"/exits/{x['uuid']}").json()["status"] == "requested":
        _decide_inbox(company_a, "offboarding.resignation")
    x = company_a.get(f"/exits/{x['uuid']}").json()
    assert x["status"] == "in_progress" and len(x["clearances"]) >= 4
    assert es.post("/exits/me/withdraw", json={}).status_code == 400  # accepted: only HR can withdraw now
    db = tenant_session(company_a.id)
    try:
        st, xd = db.execute(text("SELECT status, exit_date FROM employees WHERE id = :i"), {"i": emp["id"]}).first()
    finally:
        db.close()
    assert st == "notice" and xd == lwd
    # notice shortfall recovered in the settlement
    r = company_a.put(f"/exits/{x['uuid']}", json={"notice_action": "recover"})
    assert r.json()["notice_action"] == "recover"
    # every department clears; the last one hands the settlement to payroll
    for cl in x["clearances"]:
        items = [{"label": i["label"], "done": True} for i in cl["items"]]
        r = company_a.put(f"/exits/clearances/{cl['uuid']}", json={"items": items, "status": "cleared"})
        assert r.status_code == 200, r.text
    x = company_a.get(f"/exits/{x['uuid']}").json()
    s = x["settlement"]
    assert s["status"] == "handed_over"
    due = date.fromisoformat(s["due_date"])
    db = tenant_session(company_a.id)
    try:
        from modules.recruitment import common as c
        from modules.employees.models import Employee
        e_row = db.query(Employee).filter(Employee.id == emp["id"]).first()
        fn = c.is_working_day_fn(db, e_row)
        assert c.working_days_between(lwd, due, fn) == 2  # Code on Wages: 2 working days after the last day
    finally:
        db.close()
    assert s["state"] == "not_started" and s["working_days_left"] >= 2


def test_login_disabled_and_record_archived_at_exit(company_a, client):
    t0 = _today(company_a)
    emp = company_a.add_employee("Lata", date_of_joining=(t0 - timedelta(days=400)).isoformat())
    assert client.post("/api/v1/auth/login", json={"email": emp["email"], "password": "Secret123#"},
                       headers={"X-Company-Slug": company_a.slug}).status_code == 200
    r = company_a.post("/exits", json={"employee_id": emp["id"], "exit_type": "termination", "reason_text": "Misconduct",
                                       "last_working_day": t0.isoformat(), "notice_action": "pay_in_lieu"})
    assert r.status_code == 201, r.text
    x = r.json()
    assert x["status"] == "in_progress"
    assert company_a.post(f"/exits/{x['uuid']}/complete", json={}).status_code == 400  # clearances open
    r = company_a.post(f"/exits/{x['uuid']}/complete", json={"force": True})
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["status"] == "completed" and done["login_disabled_at"] and done["archived_at"]
    assert client.post("/api/v1/auth/login", json={"email": emp["email"], "password": "Secret123#"},
                       headers={"X-Company-Slug": company_a.slug}).status_code == 401
    db = tenant_session(company_a.id)
    try:
        st, active = db.execute(text("SELECT status, active FROM employees WHERE id = :i"), {"i": emp["id"]}).first()
    finally:
        db.close()
    assert st == "archived" and active is False  # archived, never deleted
    assert done["settlement"]["status"] in ("handed_over", "approved", "paid")
    # alumni access by link (not for absconding or death)
    assert done["alumni_link"]
    token = done["alumni_link"].rsplit("/", 1)[1]
    a = client.get(f"/api/v1/public/alumni/{token}")
    assert a.status_code == 200 and a.json()["person"]["name"].startswith("Lata")


def test_retrenchment_compensation(company_a):
    t0 = _today(company_a)
    emp = company_a.add_employee("Raj", date_of_joining=(t0 - timedelta(days=int(365 * 5.6))).isoformat())
    r = company_a.post("/exits", json={"employee_id": emp["id"], "exit_type": "retrenchment", "reason_text": "Unit closed",
                                       "last_working_day": (t0 + timedelta(days=30)).isoformat(),
                                       "compensation": {"average_monthly_pay": 26000}})
    assert r.status_code == 201, r.text
    comp = r.json()["compensation"]
    assert comp["years_counted"] == 6  # 5 years 7 months: more than 6 months counts as a year
    assert comp["compensation"] == "90000"  # 26000 / 26 × 15 × 6
    assert comp["reskilling_fund"] == "15000"


# ── privacy ─────────────────────────────────────────────────────────

def test_candidate_purge_after_retention(company_a):
    job = _job(company_a)
    cand = _candidate(company_a, job, first_name="Old", last_name="Applicant", phone="9845022222")
    app = cand["applications"][0]
    keep = _candidate(company_a, job, first_name="Busy", last_name="Applicant")
    company_a.post(f"/recruitment/applications/{app['uuid']}/reject", json={"reason": "Not a fit"})
    db = system_session()
    try:
        db.execute(text("UPDATE candidates SET purge_after = CURRENT_DATE - 1 WHERE company_id = :c"), {"c": company_a.id})
        db.commit()
        from modules.recruitment import service
        n = service.purge_due(db, company_a.id)
        db.commit()
        row = db.execute(text("SELECT first_name, email, phone, resume_text, status, purged_at FROM candidates WHERE uuid = :u"),
                         {"u": cand["uuid"]}).first()
        busy = db.execute(text("SELECT status FROM candidates WHERE uuid = :u"), {"u": keep["uuid"]}).scalar()
    finally:
        db.close()
    assert n == 1
    assert row.first_name == "Deleted" and row.email is None and row.phone is None and row.resume_text is None
    assert row.status == "purged" and row.purged_at is not None
    assert busy == "active"  # still in an open application: kept
    assert company_a.get(f"/recruitment/candidates/{cand['uuid']}").status_code == 404
    # funnel counts survive the purge
    assert company_a.get("/recruitment/reports").json()["totals"]["applications"] == 2


def test_public_careers_apply(company_a, client):
    job = _job(company_a, questions=[{"label": "Notice period?", "type": "number", "required": True}])
    page = client.get(f"/api/v1/public/careers/{company_a.slug}").json()
    assert any(j["title"] == "Backend Engineer" for j in page["jobs"])
    assert "salary_min" not in page["jobs"][0]  # hidden unless the job shows it
    form = {"first_name": "Zoya", "last_name": "Ali", "email": "zoya@example.com", "phone": "9845033333",
            "answers": '{"q1": "30"}', "consent": "1"}
    files = {"resume": ("cv.txt", b"Zoya Ali\n5 years of experience\nSkills: Python, Django, SQL", "text/plain")}
    r = client.post(f"/api/v1/public/careers/{company_a.slug}/jobs/{job['slug']}/apply", data=form, files=files)
    assert r.status_code == 201, r.text
    cands = company_a.get("/recruitment/candidates", params={"q": "Zoya"}).json()["items"]
    assert cands and cands[0]["source"] == "careers" and cands[0]["consent_at"] and cands[0]["has_resume"]
    assert cands[0]["applications"][0]["score"] >= 75
    no_consent = {**form, "consent": "", "email": "zoya2@example.com"}
    assert client.post(f"/api/v1/public/careers/{company_a.slug}/jobs/{job['slug']}/apply", data=no_consent).status_code == 400
    feed = client.get(f"/api/v1/public/careers/{company_a.slug}/feed.xml")
    assert feed.status_code == 200 and "<title><![CDATA[Backend Engineer]]></title>" in feed.text


def test_requisition_approval_then_job(company_a):
    mgr = company_a.add_employee("Mira", role="manager")
    ms = login_as(company_a, mgr)
    r = ms.post("/recruitment/requisitions", json={"title": "QA Engineer", "openings": 2, "budget_min": 600000,
                                                   "budget_max": 900000, "justification": "New product line",
                                                   "submit": True})
    assert r.status_code == 201, r.text
    req = r.json()
    assert req["status"] == "pending"
    while company_a.get(f"/recruitment/requisitions/{req['uuid']}").json()["status"] == "pending":
        _decide_inbox(company_a, "recruitment.requisition")
    assert company_a.get(f"/recruitment/requisitions/{req['uuid']}").json()["status"] == "approved"
    job = company_a.post("/recruitment/jobs", json={"requisition_uuid": req["uuid"]}).json()
    assert job["title"] == "QA Engineer" and job["openings"] == 2 and float(job["salary_max"]) == 900000


# ── tenant isolation ────────────────────────────────────────────────

TABLES = ["requisitions", "jobs", "candidates", "applications", "interviews", "interview_scorecards", "offers",
          "referrals", "reference_checks", "onboardings", "onboarding_flows", "probations", "probation_policies",
          "exits", "exit_clearances", "exit_flows", "recruitment_settings"]


def test_tenant_isolation(company_a, company_b):
    job = _job(company_b)
    cand = _candidate(company_b, job)
    app = cand["applications"][0]
    t0 = _today(company_b)
    b_emp = company_b.add_employee("Bina", date_of_joining=(t0 - timedelta(days=500)).isoformat())
    x = company_b.post("/exits", json={"employee_id": b_emp["id"], "exit_type": "contract_end",
                                       "last_working_day": (t0 + timedelta(days=5)).isoformat()}).json()
    pb = company_b.add_employee("Prob", status="probation", date_of_joining=t0.isoformat(),
                                probation_end_date=(t0 + timedelta(days=90)).isoformat())
    p = company_b.post("/probation/probations", json={"employee_id": pb["id"]}).json()

    assert company_a.get(f"/recruitment/jobs/{job['uuid']}").status_code == 404
    assert company_a.get(f"/recruitment/candidates/{cand['uuid']}").status_code == 404
    assert company_a.post(f"/recruitment/applications/{app['uuid']}/move", json={"stage": "screening"}).status_code == 404
    assert company_a.post(f"/recruitment/applications/{app['uuid']}/offers",
                          json={"joining_date": "2030-01-01", "annual_ctc": 1}).status_code == 404
    assert company_a.get(f"/exits/{x['uuid']}").status_code == 404
    assert company_a.post(f"/exits/{x['uuid']}/complete", json={"force": True}).status_code == 404
    assert company_a.post("/exits", json={"employee_id": b_emp["id"], "exit_type": "resignation"}).status_code == 404
    assert company_a.get(f"/probation/probations/{p['uuid']}").status_code == 404
    assert company_a.post("/probation/probations", json={"employee_id": pb["id"]}).status_code == 404
    assert all(j["uuid"] != job["uuid"] for j in company_a.get("/recruitment/jobs").json()["items"])
    assert all(c["uuid"] != cand["uuid"] for c in company_a.get("/recruitment/candidates").json()["items"])
    # public pages of company A never show company B's jobs
    assert all(j["uuid"] != job["uuid"] for j in company_a.client.get(f"/api/v1/public/careers/{company_a.slug}").json()["jobs"])
    db = tenant_session(company_a.id)
    try:
        for t in TABLES:
            n = db.execute(text(f"SELECT count(*) FROM {t} WHERE company_id = :b"), {"b": company_b.id}).scalar()
            assert n == 0, t
    finally:
        db.close()
