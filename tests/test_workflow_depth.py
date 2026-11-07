"""Workflow engine, second pass: email links, time-limit actions, comments and files,
nudges, reminders, forwarding in bulk, approvers who leave, fallbacks, cancelling an
approved request and the approvals history report."""
from datetime import datetime, timedelta, timezone

import pytest

from core import context
from core.db import tenant_session
from core.workflow import email_actions, extras, registry
from core.workflow import service as wf
from core.workflow.models import WorkflowActionToken, WorkflowTask
from modules.notifications import dispatch
from tests.conftest import login_as

REVOKED: list[int] = []

registry.register(registry.RequestType(
    key="test.revocable", label="Test revocable request",
    on_revoked=lambda db, i: REVOKED.append(i.id),
    default_steps=[{"name": "Manager", "approver_type": "reporting_manager", "mode": "any"}],
))


@pytest.fixture(autouse=True)
def _quiet_dispatch():
    old = dispatch.MODE
    dispatch.MODE = "off"
    yield
    dispatch.MODE = old


def _flow(company, steps, request_type="employee.profile_change"):
    flows = company.get("/workflow/definitions", params={"request_type": request_type}).json()
    body = {"name": "Test flow", "steps": steps}
    if flows:
        r = company.put(f"/workflow/definitions/{flows[0]['uuid']}", json=body)
    else:
        r = company.post("/workflow/definitions", json={"request_type": request_type, **body})
    assert r.status_code in (200, 201), r.text
    return r.json()


def _ask(session, **changes):
    r = session.post("/me/profile-change", json={"changes": changes, "reason": "Spelling"})
    assert r.status_code == 201, r.text
    return r.json()


def _inbox(session):
    return session.get("/workflow/inbox").json()["items"]


def _submit_as(company_id, employee_id, request_type, payload, title):
    """Start a request directly as an employee (for request types without their own screen)."""
    ctx = context.current()
    old = (ctx.company_id, ctx.employee_id, ctx.actor_type, ctx.actor_name)
    ctx.company_id, ctx.employee_id, ctx.actor_type, ctx.actor_name = company_id, employee_id, "employee", "Tester"
    try:
        with tenant_session(company_id) as db:
            inst = wf.submit(db, company_id, request_type, payload, title)
            db.commit()
            return str(inst.uuid)
    finally:
        ctx.company_id, ctx.employee_id, ctx.actor_type, ctx.actor_name = old


def _token_for(company, task_uuid, action):
    with tenant_session(company.id) as db:
        links = email_actions.links_for_task(db, company.id, task_uuid)
        db.commit()
        return links[f"{action}_url"].rsplit("/approve/", 1)[1]


def test_email_links_are_signed_one_time_and_audited(client, company_a):
    mgr = company_a.add_employee("Mohan", role="manager")
    emp = company_a.add_employee("Esha", reporting_manager_id=mgr["id"])
    _flow(company_a, [{"name": "Manager", "approver_type": "reporting_manager"}])
    req = _ask(login_as(company_a, emp), first_name="Eesha")
    task = _inbox(login_as(company_a, mgr))[0]
    token = _token_for(company_a, task["uuid"], "approve")

    # a tampered signature or an unknown token is refused
    secret, sig = token.split(".")
    assert client.get(f"/api/v1/workflow/email-actions/{secret}.x{sig[1:]}").status_code == 404
    assert client.get("/api/v1/workflow/email-actions/nope.nope").status_code == 404

    info = client.get(f"/api/v1/workflow/email-actions/{token}").json()
    assert info["state"] == "valid" and info["action"] == "approve" and info["approver_name"].startswith("Mohan")
    r = client.post(f"/api/v1/workflow/email-actions/{token}", json={})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    # one time only
    assert client.post(f"/api/v1/workflow/email-actions/{token}", json={}).status_code == 400
    assert client.get(f"/api/v1/workflow/email-actions/{token}").json()["state"] == "used"
    detail = company_a.get(f"/workflow/requests/{req['uuid']}").json()
    assert detail["tasks"][0]["decided_via"] == "email"
    assert "(by email)" in detail["tasks"][0]["decided_by_name"]
    events = company_a.get("/audit/", params={"action": "workflow.email_link_used"}).json()["items"]
    assert events


def test_email_reject_needs_comment_and_links_expire(client, company_a):
    mgr = company_a.add_employee("Mira", role="manager")
    emp = company_a.add_employee("Arun", reporting_manager_id=mgr["id"])
    _flow(company_a, [{"name": "Manager", "approver_type": "reporting_manager"}])
    _ask(login_as(company_a, emp), first_name="Arun K")
    task = _inbox(login_as(company_a, mgr))[0]
    reject = _token_for(company_a, task["uuid"], "reject")
    assert client.post(f"/api/v1/workflow/email-actions/{reject}", json={}).status_code == 400
    with tenant_session(company_a.id) as db:
        db.query(WorkflowActionToken).update({WorkflowActionToken.expires_at: datetime.now(timezone.utc)
                                              - timedelta(minutes=1)})
        db.commit()
    assert client.get(f"/api/v1/workflow/email-actions/{reject}").json()["state"] == "expired"
    assert client.post(f"/api/v1/workflow/email-actions/{reject}", json={"comment": "No"}).status_code == 400
    # switched off: no links are made
    company_a.put("/workflow/settings", json={"email_actions": False})
    with tenant_session(company_a.id) as db:
        links = email_actions.links_for_task(db, company_a.id, task["uuid"])
        assert "approve_url" not in links and links["view_url"].endswith(task["uuid"])


@pytest.mark.parametrize("action,expected", [("approve", "approved"), ("reject", "rejected")])
def test_time_limit_approves_or_rejects_automatically(company_a, action, expected):
    mgr = company_a.add_employee("Tara", role="manager")
    emp = company_a.add_employee("Tom", reporting_manager_id=mgr["id"])
    _flow(company_a, [{"name": "Manager", "approver_type": "reporting_manager", "sla_hours": 2,
                       "timeout_action": action}])
    req = _ask(login_as(company_a, emp), first_name="Thomas")
    with tenant_session(company_a.id) as db:
        assert wf.escalate_overdue(db, company_a.id, now=datetime.now(timezone.utc) + timedelta(hours=1)) == 0
        assert wf.escalate_overdue(db, company_a.id, now=datetime.now(timezone.utc) + timedelta(hours=3)) == 1
        db.commit()
    detail = company_a.get(f"/workflow/requests/{req['uuid']}").json()
    assert detail["status"] == expected
    assert detail["tasks"][0]["decided_via"] == "auto"


def test_timeout_action_needs_a_time_limit(company_a):
    flows = company_a.get("/workflow/definitions", params={"request_type": "employee.profile_change"}).json()
    r = company_a.put(f"/workflow/definitions/{flows[0]['uuid']}", json={
        "steps": [{"approver_type": "reporting_manager", "timeout_action": "approve"}]})
    assert r.status_code == 400


def test_comments_and_attachments(company_a):
    mgr = company_a.add_employee("Kiran", role="manager")
    emp = company_a.add_employee("Leela", reporting_manager_id=mgr["id"])
    outsider = company_a.add_employee("Otto")
    _flow(company_a, [{"name": "Manager", "approver_type": "reporting_manager"}])
    me = login_as(company_a, emp)
    req = _ask(me, first_name="Leela R")
    up = me.client.post("/api/v1/documents/uploads", headers=me.headers,
                        files={"file": ("proof.pdf", b"%PDF-1.4 test", "application/pdf")})
    assert up.status_code == 201, up.text
    upload_uuid = up.json()["uuid"]
    r = me.post(f"/workflow/requests/{req['uuid']}/comments", json={"body": "Proof attached",
                                                                     "attachment_uuids": [upload_uuid]})
    assert r.status_code == 201, r.text
    assert r.json()["attachments"][0]["name"] == "proof.pdf"
    assert me.post(f"/workflow/requests/{req['uuid']}/comments", json={"body": "  "}).status_code == 400
    mgr_s = login_as(company_a, mgr)
    assert mgr_s.post(f"/workflow/requests/{req['uuid']}/comments", json={"body": "Thanks"}).status_code == 201
    detail = mgr_s.get(f"/workflow/requests/{req['uuid']}").json()
    assert [c["body"] for c in detail["comments"]] == ["Proof attached", "Thanks"]
    # someone not involved can't read or add
    other = login_as(company_a, outsider)
    assert other.get(f"/workflow/requests/{req['uuid']}/comments").status_code == 404
    assert other.post(f"/workflow/requests/{req['uuid']}/comments", json={"body": "hi"}).status_code == 404


def test_nudge_has_a_cooldown(company_a):
    mgr = company_a.add_employee("Nina", role="manager")
    emp = company_a.add_employee("Neel", reporting_manager_id=mgr["id"])
    _flow(company_a, [{"name": "Manager", "approver_type": "reporting_manager"}])
    me = login_as(company_a, emp)
    req = _ask(me, first_name="Neelesh")
    mgr_s = login_as(company_a, mgr)
    before = mgr_s.get("/notifications/unread-count").json()["count"]
    r = me.post(f"/workflow/requests/{req['uuid']}/nudge")
    assert r.status_code == 200 and r.json()["reminded"] == 1
    assert mgr_s.get("/notifications/unread-count").json()["count"] == before + 1
    again = me.post(f"/workflow/requests/{req['uuid']}/nudge")
    assert again.status_code == 400 and "recently" in again.json()["detail"]
    # only the requester can nudge
    assert mgr_s.post(f"/workflow/requests/{req['uuid']}/nudge").status_code == 403
    assert me.get(f"/workflow/requests/{req['uuid']}").json()["nudge_count"] == 1


def test_step_reminders_repeat(company_a):
    mgr = company_a.add_employee("Rhea", role="manager")
    emp = company_a.add_employee("Raj", reporting_manager_id=mgr["id"])
    _flow(company_a, [{"name": "Manager", "approver_type": "reporting_manager", "remind_every_hours": 4}])
    _ask(login_as(company_a, emp), first_name="Rajan")
    with tenant_session(company_a.id) as db:
        now = datetime.now(timezone.utc)
        assert extras.send_reminders(db, now + timedelta(hours=1)) == 0
        assert extras.send_reminders(db, now + timedelta(hours=5)) == 1
        assert extras.send_reminders(db, now + timedelta(hours=6)) == 0
        assert extras.send_reminders(db, now + timedelta(hours=10)) == 1
        db.commit()


def test_bulk_forward_and_admin_hand_over(company_a):
    mgr = company_a.add_employee("Bala", role="manager")
    other = company_a.add_employee("Bina", role="manager")
    emps = [company_a.add_employee(f"E{i}", reporting_manager_id=mgr["id"]) for i in range(3)]
    _flow(company_a, [{"name": "Manager", "approver_type": "reporting_manager"}])
    for e in emps:
        _ask(login_as(company_a, e), first_name="Changed")
    mgr_s = login_as(company_a, mgr)
    tasks = _inbox(mgr_s)
    assert len(tasks) == 3
    r = mgr_s.post("/workflow/tasks/bulk-reassign", json={"to_employee_id": other["id"],
                                                           "task_uuids": [tasks[0]["uuid"]], "comment": "On leave"})
    assert r.status_code == 200 and r.json()["moved"] == 1
    # a manager can't take someone else's approvals in bulk
    assert mgr_s.post("/workflow/tasks/bulk-reassign", json={"to_employee_id": mgr["id"],
                                                              "from_employee_id": other["id"]}).status_code == 403
    r = company_a.post("/workflow/tasks/bulk-reassign", json={"to_employee_id": other["id"],
                                                               "from_employee_id": mgr["id"]})
    assert r.json()["moved"] == 2
    assert len(_inbox(login_as(company_a, other))) == 3
    assert _inbox(mgr_s) == []
    holders = company_a.get("/workflow/pending-by-approver").json()
    assert holders[0]["pending"] == 3


def test_pending_approvals_move_when_approver_leaves(company_a):
    boss = company_a.add_employee("Bose", role="manager")
    mgr = company_a.add_employee("Leaving", role="manager", reporting_manager_id=boss["id"])
    emp = company_a.add_employee("Stay", reporting_manager_id=mgr["id"])
    _flow(company_a, [{"name": "Manager", "approver_type": "reporting_manager"}])
    req = _ask(login_as(company_a, emp), first_name="Stays")
    r = company_a.post(f"/employees/{mgr['uuid']}/status", json={"status": "exited", "reason": "Left"})
    assert r.status_code == 200, r.text
    detail = company_a.get(f"/workflow/requests/{req['uuid']}").json()
    statuses = [(t["approver_name"], t["status"]) for t in detail["tasks"]]
    assert statuses[0][1] == "reassigned" and statuses[1][0].startswith("Bose") and statuses[1][1] == "pending"


def test_no_approver_fallback_rules(company_a):
    hr = company_a.add_employee("Harini", role="hr")
    emp = company_a.add_employee("Nomgr")  # no manager
    _flow(company_a, [{"name": "Manager", "approver_type": "reporting_manager"}])
    r = company_a.put("/workflow/settings", json={"no_approver": {"action": "role", "value": "hr"}})
    assert r.status_code == 200, r.text
    _ask(login_as(company_a, emp), first_name="X")
    assert len(_inbox(login_as(company_a, hr))) == 1
    company_a.put("/workflow/settings", json={"no_approver": {"action": "auto_approve"}})
    req = _ask(login_as(company_a, company_a.add_employee("Auto")), first_name="Y")
    assert req["status"] == "approved"
    assert company_a.put("/workflow/settings", json={"no_approver": {"action": "role"}}).status_code == 400
    # only admins change settings
    assert login_as(company_a, emp).put("/workflow/settings", json={"nudge_cooldown_hours": 1}).status_code == 403


def test_cancel_after_approval_with_and_without_approval(company_a):
    mgr = company_a.add_employee("Cara", role="manager")
    emp = company_a.add_employee("Cyril", reporting_manager_id=mgr["id"])
    uuid = _submit_as(company_a.id, emp["id"], "test.revocable", {"what": "x"}, "Test request")
    mgr_s = login_as(company_a, mgr)
    t = _inbox(mgr_s)[0]
    mgr_s.post(f"/workflow/tasks/{t['uuid']}/decision", json={"decision": "approve"})
    me = login_as(company_a, emp)
    detail = me.get(f"/workflow/requests/{uuid}").json()
    assert detail["status"] == "approved" and detail["can_revoke"] and detail["revoke_mode"] == "approval"
    assert me.post(f"/workflow/requests/{uuid}/revoke", json={"reason": ""}).status_code == 422
    r = me.post(f"/workflow/requests/{uuid}/revoke", json={"reason": "Plans changed"})
    assert r.status_code == 200 and r.json()["pending"] is True
    assert me.post(f"/workflow/requests/{uuid}/revoke", json={"reason": "again"}).status_code == 400
    t2 = [x for x in _inbox(mgr_s) if x["request"]["request_type"] == "workflow.revoke"][0]
    mgr_s.post(f"/workflow/tasks/{t2['uuid']}/decision", json={"decision": "approve"})
    detail = me.get(f"/workflow/requests/{uuid}").json()
    assert detail["status"] == "revoked" and detail["revoke_reason"] == "Plans changed"

    # direct mode: no approval needed; off: refused
    company_a.put("/workflow/settings", json={"revoke": {"test.revocable": "direct"}})
    uuid2 = _submit_as(company_a.id, emp["id"], "test.revocable", {}, "Second")
    t = [x for x in _inbox(mgr_s) if x["request"]["uuid"] == uuid2][0]
    mgr_s.post(f"/workflow/tasks/{t['uuid']}/decision", json={"decision": "approve"})
    assert me.post(f"/workflow/requests/{uuid2}/revoke", json={"reason": "No longer needed"}).json()["status"] == "revoked"
    company_a.put("/workflow/settings", json={"revoke": {"test.revocable": "off"}})
    uuid3 = _submit_as(company_a.id, emp["id"], "test.revocable", {}, "Third")
    t = [x for x in _inbox(mgr_s) if x["request"]["uuid"] == uuid3][0]
    mgr_s.post(f"/workflow/tasks/{t['uuid']}/decision", json={"decision": "approve"})
    assert me.post(f"/workflow/requests/{uuid3}/revoke", json={"reason": "x"}).status_code == 400
    # request types without an undo can't be cancelled after approval
    assert company_a.put("/workflow/settings", json={"revoke": {"employee.profile_change": "direct"}}).status_code == 400


def test_history_report_with_sla(company_a):
    mgr = company_a.add_employee("Sona", role="manager")
    emp = company_a.add_employee("Sam", reporting_manager_id=mgr["id"])
    _flow(company_a, [{"name": "Manager", "approver_type": "reporting_manager", "sla_hours": 24}])
    _ask(login_as(company_a, emp), first_name="Samuel")
    mgr_s = login_as(company_a, mgr)
    t = _inbox(mgr_s)[0]
    mgr_s.post(f"/workflow/tasks/{t['uuid']}/decision", json={"decision": "approve"})
    _ask(login_as(company_a, emp), last_name="Late")
    with tenant_session(company_a.id) as db:  # the second one is already past its limit
        db.query(WorkflowTask).filter(WorkflowTask.status == "pending").update(
            {WorkflowTask.due_at: datetime.now(timezone.utc) - timedelta(hours=1)})
        db.commit()
    rows = company_a.get("/workflow/reports/history").json()["items"]
    decided = [r for r in rows if r["status"] == "approved"][0]
    assert decided["within_sla"] is True and decided["hours_taken"] is not None
    s = company_a.get("/workflow/reports/summary").json()
    assert s["overall"]["decided"] >= 1 and s["overall"]["overdue"] == 1 and s["overall"]["sla_rate"] == 100
    csv = company_a.get("/workflow/reports/history.csv")
    assert csv.status_code == 200 and "Within time limit" in csv.text
    assert mgr_s.get("/workflow/reports/history").status_code == 403


def test_isolation_of_new_endpoints(company_a, company_b):
    mgr = company_a.add_employee("Iso", role="manager")
    emp = company_a.add_employee("Lated", reporting_manager_id=mgr["id"])
    _flow(company_a, [{"name": "Manager", "approver_type": "reporting_manager"}])
    req = _ask(login_as(company_a, emp), first_name="Z")
    assert company_b.get(f"/workflow/requests/{req['uuid']}/comments").status_code == 404
    assert company_b.post(f"/workflow/requests/{req['uuid']}/comments", json={"body": "x"}).status_code == 404
    assert company_b.post(f"/workflow/requests/{req['uuid']}/revoke", json={"reason": "x"}).status_code == 404
    assert company_b.get("/workflow/reports/history").json()["total"] == 0
    task = _inbox(login_as(company_a, mgr))[0]
    r = company_b.post("/workflow/tasks/bulk-reassign", json={"to_employee_id": emp["id"],
                                                               "task_uuids": [task["uuid"]]})
    assert r.status_code == 400  # company A's employee is not an employee of B
