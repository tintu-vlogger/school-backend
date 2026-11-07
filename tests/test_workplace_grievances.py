"""Grievances and POSH: who can see POSH complaints, anonymous follow-up with the
tracking code, legal deadlines and the annual report."""
from datetime import date, timedelta

from modules.grievances import service
from tests.conftest import login_as


def _ic(company):
    presiding = company.add_employee("Padma", "Presiding")
    member = company.add_employee("Mohan", "Member")
    r = company.post("/grievances/committees", json={
        "kind": "posh_ic", "name": "Internal Committee", "members": [
            {"employee_id": presiding["id"], "role": "presiding_officer"},
            {"employee_id": member["id"], "role": "member"},
            {"external_name": "Adv. Meera", "external_org": "NGO", "role": "external_member"}]})
    assert r.status_code == 201, r.text
    return presiding, member


def test_internal_committee_needs_presiding_officer_and_external_member(company_a):
    e = company_a.add_employee("Esha")
    r = company_a.post("/grievances/committees", json={"kind": "posh_ic", "name": "IC",
                                                       "members": [{"employee_id": e["id"], "role": "member"}]})
    assert r.status_code == 400


def test_posh_visible_only_to_internal_committee(company_a):
    presiding, member = _ic(company_a)
    hr = company_a.add_employee("Harini", role="hr")
    complainant = company_a.add_employee("Kavya")
    bystander = company_a.add_employee("Bala")
    me = login_as(company_a, complainant)
    r = me.post("/grievances", json={"kind": "posh", "subject": "Harassment", "description": "Details",
                                     "incident_date": str(date.today() - timedelta(days=10)), "respondent_name": "X"})
    assert r.status_code == 201, r.text
    uuid = r.json()["uuid"]
    # the committee sees it, with the confidential timeline
    ic = login_as(company_a, presiding)
    d = ic.get(f"/grievances/{uuid}")
    assert d.status_code == 200 and d.json()["timeline"]
    assert any(g["uuid"] == uuid for g in ic.get("/grievances", params={"scope": "posh"}).json()["items"])
    # HR (who handles ordinary grievances), the owner and others do not
    for s in (login_as(company_a, hr), company_a, login_as(company_a, bystander)):
        assert s.get(f"/grievances/{uuid}").status_code == 404
    assert login_as(company_a, hr).get("/grievances", params={"scope": "posh"}).status_code == 403
    assert all(g["kind"] != "posh" for g in login_as(company_a, hr).get("/grievances", params={"scope": "all"}).json()["items"])
    # the owner sees them only when the company switches that on
    company_a.put("/grievances/settings", json={"owner_sees_posh": True})
    assert company_a.get(f"/grievances/{uuid}").status_code == 200
    # the complainant sees their own case but never the committee's notes
    ic.post(f"/grievances/{uuid}/messages", json={"kind": "note", "body": "Witness statements recorded"})
    ic.post(f"/grievances/{uuid}/messages", json={"kind": "reply", "body": "We have taken this up"})
    mine = me.get(f"/grievances/{uuid}").json()
    assert [m["body"] for m in mine["messages"] if m["kind"] != "event"] == ["We have taken this up"]
    assert "timeline" not in mine
    assert me.post(f"/grievances/{uuid}/messages", json={"kind": "note", "body": "x"}).status_code == 403


def test_posh_deadlines():
    conf = dict(service.DEFAULT_SETTINGS)
    d = service.posh_deadlines(conf, date(2026, 5, 20), date(2026, 1, 10))
    assert d["filing_deadline"] == date(2026, 4, 10) and d["late_filing"] is True and not d["beyond_extension"]
    assert d["inquiry_due_on"] == date(2026, 8, 18)  # 90 days from receipt
    d = service.posh_deadlines(conf, date(2026, 5, 20), date(2026, 3, 31), inquiry_completed=date(2026, 7, 1),
                               report_submitted=date(2026, 7, 8))
    assert d["filing_deadline"] == date(2026, 6, 30) and d["late_filing"] is False
    assert d["report_due_on"] == date(2026, 7, 11) and d["action_due_on"] == date(2026, 9, 6)
    conf["posh_inquiry_days"] = 60  # configurable
    assert service.posh_deadlines(conf, date(2026, 5, 20), None)["inquiry_due_on"] == date(2026, 7, 19)
    assert service.add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)


def test_posh_flow_and_annual_report(company_a):
    presiding, _ = _ic(company_a)
    complainant = company_a.add_employee("Kiran")
    g = login_as(company_a, complainant).post("/grievances", json={
        "kind": "posh", "subject": "Remarks", "description": "Details", "incident_date": str(date.today())}).json()
    ic = login_as(company_a, presiding)
    for body in ({"action": "acknowledge"}, {"action": "start_inquiry"}, {"action": "complete_inquiry"},
                 {"action": "submit_report", "outcome": "upheld", "summary": "Proved"},
                 {"action": "record_action", "action_taken": "Written warning and transfer"}):
        r = ic.post(f"/grievances/{g['uuid']}/actions", json=body)
        assert r.status_code == 200, (body, r.text)
    d = r.json()
    assert d["status"] == "resolved" and d["report_due_on"] and d["action_due_on"]
    assert all(s["state"] == "done" for s in d["timeline"] if s["key"] in ("acknowledge", "inquiry", "report", "action"))
    rep = ic.get("/grievances/posh/annual-report", params={"year": date.today().year}).json()
    assert rep["complaints_received"] >= 1 and rep["complaints_disposed"] >= 1
    assert rep["committee"]["members"]


def test_anonymous_grievance_follow_up_with_code(company_a, client):
    company_a.add_employee("Hari", role="hr")
    worker = company_a.add_employee("Wasim")
    me = login_as(company_a, worker)
    r = me.post("/grievances", json={"kind": "grievance", "subject": "Unsafe wiring", "description": "Sparks near line 3",
                                     "anonymous": True})
    assert r.status_code == 201, r.text
    out = r.json()
    code = out["tracking_code"]
    assert out["uuid"] is None and len(code) == 14
    # nothing links the record to the person: not on the record, not in the audit log
    assert me.get("/grievances", params={"scope": "mine"}).json()["items"] == []
    from core.db import tenant_session
    from sqlalchemy import text
    db = tenant_session(company_a.id)
    try:
        row = db.execute(text("SELECT id, complainant_employee_id FROM grievances WHERE company_id = :c"),
                         {"c": company_a.id}).first()
        assert row.complainant_employee_id is None
        logs = db.execute(text("SELECT actor_id, actor_type, ip_address, user_agent FROM audit_logs "
                               "WHERE company_id = :c AND entity_type IN ('grievances', 'grievance_messages')"),
                          {"c": company_a.id}).all()
        assert logs and all(l.actor_id is None and l.actor_type == 'anonymous' and l.ip_address is None
                            and l.user_agent is None for l in logs)
    finally:
        db.close()
    # follow up without signing in, using the company address and the code
    base = f"/api/v1/public/grievances/{company_a.slug}"
    t = client.post(f"{base}/track", json={"code": code.lower().replace("-", "")})
    assert t.status_code == 200 and t.json()["subject"] == "Unsafe wiring" and t.json()["uuid"] is None
    assert client.post(f"{base}/track", json={"code": "AAAA-BBBB-CCCC"}).status_code == 404
    client.post(f"{base}/messages", json={"code": code, "body": "It happened again today"})
    # HR (grievance handler) sees it and replies; the complainant reads the reply with the code
    items = company_a.get("/grievances", params={"scope": "grievance"}).json()["items"]
    gid = next(i["uuid"] for i in items if i["subject"] == "Unsafe wiring")
    assert company_a.get(f"/grievances/{gid}").json()["complainant_name"] is None
    company_a.post(f"/grievances/{gid}/messages", json={"kind": "reply", "body": "Electrician on the way"})
    company_a.post(f"/grievances/{gid}/messages", json={"kind": "note", "body": "Internal: check supervisor"})
    msgs = client.post(f"{base}/track", json={"code": code}).json()["messages"]
    bodies = [m["body"] for m in msgs if m["kind"] != "event"]
    assert bodies == ["It happened again today", "Electrician on the way"]
    # another company's address does not find it
    from tests.conftest import Company
    other = Company(client, "Gamma Co")
    assert client.post(f"/api/v1/public/grievances/{other.slug}/track", json={"code": code}).status_code == 404


def test_posh_complaint_cannot_be_anonymous(company_a):
    e = login_as(company_a, company_a.add_employee("Neha"))
    assert e.post("/grievances", json={"kind": "posh", "subject": "x", "description": "y", "anonymous": True}).status_code == 400
