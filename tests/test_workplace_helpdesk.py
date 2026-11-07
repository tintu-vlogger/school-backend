"""Help desk: SLA due times and breaches, escalation, the status flow, ratings and reopening."""
from datetime import datetime, timedelta, timezone

from core.db import tenant_session
from modules.helpdesk import service
from modules.helpdesk.models import HelpdeskCase
from tests.conftest import login_as


def _cat(company, name="Payroll"):
    cats = company.get("/helpdesk/categories").json()["items"]
    return next(c for c in cats if c["name"] == name and c["parent_id"] is None)


def _setup(company):
    agent = company.add_employee("Anita", "Agent")
    req = company.add_employee("Ravi", "Requester")
    cat = _cat(company)
    r = company.put(f"/helpdesk/categories/{cat['uuid']}", json={"name": cat["name"], "agents": [{"employee_id": agent["id"]}],
                                                               "escalate_to_employee_id": agent["id"]})
    assert r.status_code == 200, r.text
    return agent, req, cat


def test_sla_due_computation_and_pause():
    start = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
    assert service.compute_due(start, 240) == datetime(2026, 11, 2, 14, 0, tzinfo=timezone.utc)
    assert service.compute_due(start, None) is None
    due = datetime(2026, 11, 2, 14, 0, tzinfo=timezone.utc)
    # waited 90 minutes on the employee: the target moves by 90 minutes
    assert service.shift_for_pause(due, start, start + timedelta(minutes=90)) == due + timedelta(minutes=90)
    c = HelpdeskCase(status="in_progress", response_due_at=due, resolution_due_at=due + timedelta(hours=20),
                     first_response_at=start + timedelta(hours=5), created_at=start)
    st = service.sla_state(c, now=start + timedelta(hours=6))
    assert st["response"]["state"] == "breached" and st["response"]["late_by_minutes"] == 60
    assert st["resolution"]["state"] == "running" and st["resolution"]["minutes_left"] == 18 * 60
    c.paused_at = start + timedelta(hours=1)
    assert service.sla_state(c, now=start + timedelta(days=5))["resolution"]["state"] == "paused"


def test_case_gets_sla_from_priority_and_breach_escalates(company_a):
    agent, req, cat = _setup(company_a)
    me = login_as(company_a, req)
    r = me.post("/helpdesk/cases", json={"category_id": cat["id"], "subject": "Payslip missing",
                                         "description": "October payslip is not visible", "priority": "high"})
    assert r.status_code == 201, r.text
    case = r.json()
    assert case["reference_no"].startswith("TKT") and case["status"] == "new"
    assert case["sla"] is None  # requesters don't see SLA internals
    a = login_as(company_a, agent)
    detail = a.get(f"/helpdesk/cases/{case['uuid']}").json()
    created = datetime.fromisoformat(detail["created_at"].replace("Z", "+00:00"))
    due = datetime.fromisoformat(detail["sla"]["resolution"]["due_at"].replace("Z", "+00:00"))
    assert abs((due - created) - timedelta(hours=24)) < timedelta(seconds=5)  # default high = 24 h
    # time passes beyond both targets: breach flags, escalation, priority goes up one step
    db = tenant_session(company_a.id)
    try:
        n = service.check_sla(db, company_a.id, now=created + timedelta(hours=25))
        db.commit()
        assert n == 2
        assert service.check_sla(db, company_a.id, now=created + timedelta(hours=26)) == 0  # idempotent
        db.commit()
    finally:
        db.close()
    detail = a.get(f"/helpdesk/cases/{case['uuid']}").json()
    assert detail["escalation_level"] == 1 and detail["priority"] == "urgent" and detail["breached"]
    assert any("Escalated" in m["body"] for m in detail["messages"] if m["kind"] == "event")
    esc = a.get("/helpdesk/cases", params={"queue": "escalated"}).json()
    assert any(c["uuid"] == case["uuid"] for c in esc["items"])
    rep = company_a.get("/helpdesk/reports").json()
    assert rep["breached"] >= 1 and any(b["uuid"] == case["uuid"] for b in rep["breaches"])


def test_pickup_reply_notes_resolve_rate_reopen(company_a):
    agent, req, cat = _setup(company_a)
    me, a = login_as(company_a, req), login_as(company_a, agent)
    case = me.post("/helpdesk/cases", json={"category_id": cat["id"], "subject": "PF number",
                                            "description": "What is my UAN?"}).json()
    assert any(c["uuid"] == case["uuid"] for c in a.get("/helpdesk/cases", params={"queue": "unassigned"}).json()["items"])
    assert a.post(f"/helpdesk/cases/{case['uuid']}/pick").json()["assignee_employee_id"] == agent["id"]
    # resolving needs a reply first
    assert a.post(f"/helpdesk/cases/{case['uuid']}/status", json={"status": "resolved"}).status_code == 400
    a.post(f"/helpdesk/cases/{case['uuid']}/messages", json={"kind": "note", "body": "Check EPFO portal"})
    d = a.post(f"/helpdesk/cases/{case['uuid']}/messages", json={"kind": "reply", "body": "Your UAN is on the payslip."}).json()
    assert d["first_response_at"] and d["status"] == "in_progress"
    # the requester never sees internal notes, and can't add them
    mine = me.get(f"/helpdesk/cases/{case['uuid']}").json()
    assert all(m["kind"] != "note" for m in mine["messages"])
    assert me.post(f"/helpdesk/cases/{case['uuid']}/messages", json={"kind": "note", "body": "x"}).status_code == 403
    # requester can't move the case to in progress
    assert me.post(f"/helpdesk/cases/{case['uuid']}/status", json={"status": "in_progress"}).status_code == 403
    assert a.post(f"/helpdesk/cases/{case['uuid']}/status", json={"status": "resolved"}).json()["status"] == "resolved"
    # a poor rating (2 of 5) reopens the case
    r = me.post(f"/helpdesk/cases/{case['uuid']}/rate", json={"rating": 2, "comment": "Still unclear"}).json()
    assert r["reopened"] and r["case"]["status"] == "in_progress" and r["case"]["reopened_count"] == 1
    a.post(f"/helpdesk/cases/{case['uuid']}/status", json={"status": "resolved", "note": "UAN is 1001…"})
    r = me.post(f"/helpdesk/cases/{case['uuid']}/rate", json={"rating": 5}).json()
    assert not r["reopened"] and r["case"]["status"] == "closed"
    assert me.post(f"/helpdesk/cases/{case['uuid']}/rate", json={"rating": 4}).status_code == 400


def test_raise_on_behalf_and_visibility(company_a):
    agent, req, cat = _setup(company_a)
    other = company_a.add_employee("Olga")
    a = login_as(company_a, agent)
    r = a.post("/helpdesk/cases", json={"category_id": cat["id"], "subject": "Bank change", "description": "Called HR",
                                        "requester_employee_id": req["id"], "source": "phone"})
    assert r.status_code == 201 and r.json()["requester_employee_id"] == req["id"]
    # a plain employee can't raise for others, or see someone else's case
    o = login_as(company_a, other)
    assert o.post("/helpdesk/cases", json={"category_id": cat["id"], "subject": "x", "description": "y",
                                           "requester_employee_id": req["id"]}).status_code == 403
    assert o.get(f"/helpdesk/cases/{r.json()['uuid']}").status_code == 404
    assert o.get("/helpdesk/cases", params={"queue": "all"}).status_code == 403


def test_knowledge_base_search(company_a):
    hits = company_a.get("/helpdesk/articles", params={"q": "payslips"}).json()["items"]
    assert hits and "payslip" in hits[0]["title"].lower()
    assert company_a.get("/helpdesk/articles", params={"q": "zzzz-no-match"}).json()["items"] == []


def test_open_counts_cover_subcategories(company_a):
    req = company_a.add_employee("Sub", "Counter")
    cats = company_a.get("/helpdesk/categories").json()["items"]
    sub = next(c for c in cats if c["parent_id"] is not None)
    main = next(c for c in cats if c["id"] == sub["parent_id"])
    r = login_as(company_a, req).post("/helpdesk/cases", json={"category_id": main["id"], "subcategory_id": sub["id"],
                                                                "subject": "Count me", "description": "Counting open cases"})
    assert r.status_code == 201, r.text
    after = {c["id"]: c["open_cases"] for c in company_a.get("/helpdesk/categories").json()["items"]}
    assert after[sub["id"]] == 1 and after[main["id"]] >= 1
