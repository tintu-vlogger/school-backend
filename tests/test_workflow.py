"""Workflow engine end to end, using the employee.profile_change request type."""
from datetime import date, datetime, timedelta, timezone

from core.db import tenant_session
from core.workflow import service as wf
from tests.conftest import login_as


def _flow(company, steps, name="Profile changes", **extra):
    flows = company.get("/workflow/definitions", params={"request_type": "employee.profile_change"}).json()
    body = {"name": name, "steps": steps, **extra}
    if flows:
        r = company.put(f"/workflow/definitions/{flows[0]['uuid']}", json=body)
    else:
        r = company.post("/workflow/definitions", json={"request_type": "employee.profile_change", **body})
    assert r.status_code in (200, 201), r.text
    return r.json()


def _ask(session, **changes):
    r = session.post("/me/profile-change", json={"changes": changes, "reason": "Spelling"})
    assert r.status_code == 201, r.text
    return r.json()


def _pending(session):
    return session.get("/workflow/inbox").json()["items"]


def test_default_flow_goes_to_owner_and_applies_on_approval(company_a):
    emp = company_a.add_employee("Ramesh", "Iyer")
    me = login_as(company_a, emp)
    req = _ask(me, first_name="Ramesh K", phone="9000000001")
    assert req["status"] == "pending" and req["reference_no"].startswith("REQ-")
    # nothing changed yet
    assert company_a.get(f"/employees/{emp['uuid']}").json()["first_name"] == "Ramesh"
    tasks = _pending(company_a)  # no HR employees, so the owner approves
    assert len(tasks) == 1 and tasks[0]["request"]["uuid"] == req["uuid"]
    assert "First name: Ramesh → Ramesh K" in tasks[0]["request"]["summary"]
    r = company_a.post(f"/workflow/tasks/{tasks[0]['uuid']}/decision", json={"decision": "approve"})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    after = company_a.get(f"/employees/{emp['uuid']}").json()
    assert after["first_name"] == "Ramesh K" and after["phone"] == "9000000001"
    audit = company_a.get("/audit/", params={"entity_uuid": emp["uuid"], "action": "updated"}).json()["items"]
    assert any(a["changes"].get("first_name", {}).get("new") == "Ramesh K" for a in audit)
    detail = me.get(f"/workflow/requests/{req['uuid']}").json()
    assert detail["status"] == "approved" and detail["tasks"][0]["status"] == "approved"


def test_reject_needs_a_comment_and_changes_nothing(company_a):
    emp = company_a.add_employee("Sunil")
    me = login_as(company_a, emp)
    _ask(me, last_name="Changed")
    task = _pending(company_a)[0]
    assert company_a.post(f"/workflow/tasks/{task['uuid']}/decision", json={"decision": "reject"}).status_code == 400
    r = company_a.post(f"/workflow/tasks/{task['uuid']}/decision", json={"decision": "reject", "comment": "Need proof"})
    assert r.json()["status"] == "rejected"
    assert company_a.get(f"/employees/{emp['uuid']}").json()["last_name"] == "Kumar"
    # a decided task cannot be decided again
    assert company_a.post(f"/workflow/tasks/{task['uuid']}/decision", json={"decision": "approve"}).status_code == 400


def test_manager_then_all_hr_with_condition(company_a):
    hr1 = company_a.add_employee("Hema", role="hr")
    hr2 = company_a.add_employee("Hari", role="hr")
    mgr = company_a.add_employee("Mala", role="manager")
    emp = company_a.add_employee("Esha", reporting_manager_id=mgr["id"])
    flow = _flow(company_a, [
        {"name": "Manager", "approver_type": "reporting_manager"},
        {"name": "HR", "approver_type": "role", "approver_value": "hr", "mode": "all", "condition": "'phone' in request.fields"},
    ])
    assert flow["version"] >= 1
    me = login_as(company_a, emp)

    # no phone change: the HR step is skipped
    _ask(me, first_name="Eesha")
    mgr_s = login_as(company_a, mgr)
    t = _pending(mgr_s)[0]
    assert mgr_s.post(f"/workflow/tasks/{t['uuid']}/decision", json={"decision": "approve"}).json()["status"] == "approved"

    # phone change: manager, then both HR people must approve
    req = _ask(me, phone="9111111111")
    t = _pending(mgr_s)[0]
    assert mgr_s.post(f"/workflow/tasks/{t['uuid']}/decision", json={"decision": "approve"}).json()["status"] == "pending"
    h1, h2 = login_as(company_a, hr1), login_as(company_a, hr2)
    t1, t2 = _pending(h1)[0], _pending(h2)[0]
    assert h1.post(f"/workflow/tasks/{t1['uuid']}/decision", json={"decision": "approve"}).json()["status"] == "pending"
    # HR 1 cannot act on HR 2's task
    assert h1.post(f"/workflow/tasks/{t2['uuid']}/decision", json={"decision": "approve"}).status_code == 403
    assert h2.post(f"/workflow/tasks/{t2['uuid']}/decision", json={"decision": "approve"}).json()["status"] == "approved"
    assert company_a.get(f"/employees/{emp['uuid']}").json()["phone"] == "9111111111"
    assert me.get(f"/workflow/requests/{req['uuid']}").json()["status"] == "approved"


def test_cancel_and_one_pending_at_a_time(company_a):
    emp = company_a.add_employee("Tara")
    me = login_as(company_a, emp)
    req = _ask(me, first_name="Taara")
    r = me.post("/me/profile-change", json={"changes": {"first_name": "T"}})
    assert r.status_code == 400 and "already waiting" in r.json()["detail"]
    other = login_as(company_a, company_a.add_employee("Other"))
    assert other.post(f"/workflow/requests/{req['uuid']}/cancel", json={}).status_code in (403, 404)
    assert me.post(f"/workflow/requests/{req['uuid']}/cancel", json={"reason": "Typo"}).json()["status"] == "cancelled"
    assert _pending(company_a) == []
    assert me.get("/workflow/requests").json()["items"][0]["status"] == "cancelled"


def test_reassign_and_delegate(company_a):
    mgr = company_a.add_employee("Mohan", role="manager")
    deputy = company_a.add_employee("Deepak", role="manager")
    emp = company_a.add_employee("Usha", reporting_manager_id=mgr["id"])
    _flow(company_a, [{"name": "Manager", "approver_type": "reporting_manager"}])
    me, mgr_s, dep_s = login_as(company_a, emp), login_as(company_a, mgr), login_as(company_a, deputy)

    _ask(me, first_name="Ushaa")
    t = _pending(mgr_s)[0]
    r = mgr_s.post(f"/workflow/tasks/{t['uuid']}/reassign", json={"to_employee_id": deputy["id"], "comment": "On leave"})
    assert r.status_code == 200
    assert _pending(mgr_s) == [] and len(_pending(dep_s)) == 1
    dep_s.post(f"/workflow/tasks/{_pending(dep_s)[0]['uuid']}/decision", json={"decision": "approve"})

    today = date.today()
    r = mgr_s.post("/workflow/delegations", json={"to_employee_id": deputy["id"], "start_date": str(today - timedelta(days=1)),
                                                  "end_date": str(today + timedelta(days=5))})
    assert r.status_code == 201
    _ask(me, first_name="Usha M")
    assert _pending(mgr_s) == []
    d = _pending(dep_s)
    assert len(d) == 1 and d[0]["delegated"] is True


def test_escalation_after_sla(company_a):
    mgr = company_a.add_employee("Kiran", role="manager")
    boss = company_a.add_employee("Bhaskar", role="manager")
    emp = company_a.add_employee("Lata", reporting_manager_id=mgr["id"])
    _flow(company_a, [{"name": "Manager", "approver_type": "reporting_manager", "sla_hours": 4,
                       "escalate_to": {"approver_type": "employee", "approver_value": boss["id"]}}])
    _ask(login_as(company_a, emp), first_name="Latha")
    db = tenant_session(company_a.id)
    try:
        assert wf.escalate_overdue(db, company_a.id) == 0
        later = datetime.now(timezone.utc) + timedelta(hours=5)
        assert wf.escalate_overdue(db, company_a.id, now=later) == 1
        db.commit()
    finally:
        db.close()
    assert _pending(login_as(company_a, mgr)) == []
    boss_tasks = _pending(login_as(company_a, boss))
    assert len(boss_tasks) == 1


def test_versions_keep_running_requests_on_old_steps(company_a):
    mgr = company_a.add_employee("Vani", role="manager")
    emp = company_a.add_employee("Wasim", reporting_manager_id=mgr["id"])
    v1 = _flow(company_a, [{"name": "Manager", "approver_type": "reporting_manager"}])
    me = login_as(company_a, emp)
    req = _ask(me, first_name="Waseem")
    v2 = _flow(company_a, [{"name": "Owner", "approver_type": "owner"}])
    assert v2["version"] == v1["version"] + 1 and v2["lineage_id"] == v1["lineage_id"]
    detail = company_a.get(f"/workflow/definitions/{v2['uuid']}").json()
    assert [v["version"] for v in detail["versions"]][:2] == [v2["version"], v1["version"]]
    running = me.get(f"/workflow/requests/{req['uuid']}").json()
    assert running["flow_version"] == v1["version"] and running["tasks"][0]["approver_name"].startswith("Vani")
    # old version can no longer be edited
    assert company_a.put(f"/workflow/definitions/{v1['uuid']}", json={"name": "x"}).status_code == 400


def test_bulk_approve(company_a):
    uuids = []
    for name in ("Xavier", "Yamini", "Zoya"):
        e = company_a.add_employee(name)
        _ask(login_as(company_a, e), last_name="Changed")
    uuids = [t["uuid"] for t in _pending(company_a)]
    assert len(uuids) == 3
    r = company_a.post("/workflow/tasks/bulk-decision", json={"task_uuids": uuids + ["00000000-0000-0000-0000-000000000000"],
                                                             "decision": "approve", "comment": "OK"})
    body = r.json()
    assert body["done"] == 3 and body["failed"] == 1
    assert _pending(company_a) == []


def test_invalid_flow_is_refused(company_a):
    r = company_a.post("/workflow/definitions", json={"request_type": "employee.profile_change", "name": "Bad",
                                                      "steps": [{"approver_type": "role"}]})
    assert r.status_code == 400
    r = company_a.post("/workflow/definitions", json={"request_type": "employee.profile_change", "name": "Bad",
                                                      "steps": [{"approver_type": "owner", "condition": "request.days >"}]})
    assert r.status_code == 400 and "condition" in r.json()["detail"].lower()


def test_other_company_cannot_touch_tasks(company_a, company_b):
    emp = company_a.add_employee("Isolated")
    _ask(login_as(company_a, emp), first_name="Iso")
    task = _pending(company_a)[0]
    assert company_b.post(f"/workflow/tasks/{task['uuid']}/decision", json={"decision": "approve"}).status_code == 404
    assert company_b.get(f"/workflow/requests/{task['request']['uuid']}").status_code == 404
    assert _pending(company_b) == []
