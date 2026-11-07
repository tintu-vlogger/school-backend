"""Automation rules: triggers, conditions, every action, webhooks with signatures and
retries, the test-run preview, permissions and tenant isolation."""
import hashlib
import hmac
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from core.db import tenant_session
from core.timeutil import today_for_company_id
from tests.conftest import login_as


def _rule(company, **over):
    body = {"name": "Rule", "entity": "employee", "trigger": "created",
            "actions": [{"type": "notify", "to": ["employee"], "title": "Hello {{employee.first_name}}"}]}
    body.update(over)
    r = company.post("/automation/rules", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _runs(company, rule_uuid):
    return company.get("/automation/runs", params={"rule": rule_uuid}).json()["items"]


def _notifications(session):
    return session.get("/notifications/inbox").json()["items"]


# ── A tiny receiver for webhooks ────────────────────────────────────

class _Receiver:
    def __init__(self):
        self.calls = []
        self.status = 200
        receiver = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                receiver.calls.append({"headers": dict(self.headers), "body": body})
                self.send_response(receiver.status)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def receiver():
    r = _Receiver()
    yield r
    r.close()


# ── Triggers and conditions ─────────────────────────────────────────

def test_created_trigger_with_condition(company_a):
    rule = _rule(company_a, name="Welcome", condition="record.work_mode == 'remote'")
    remote = company_a.add_employee("Asha", "Rao", work_mode="remote")
    company_a.add_employee("Binu", "Das", work_mode="office")
    assert any(n["title"] == "Hello Asha" for n in _notifications(login_as(company_a, remote)))
    runs = _runs(company_a, rule["uuid"])
    assert {r["status"] for r in runs} == {"done", "skipped"}
    assert company_a.get(f"/automation/rules/{rule['uuid']}").json()["run_count"] == 1


def test_updated_and_field_changed_triggers(company_a):
    emp = company_a.add_employee("Chitra", "Nair")
    any_change = _rule(company_a, name="Any change", trigger="updated")
    phone = _rule(company_a, name="Phone changed", trigger="field_changed", fields=["phone"],
                  actions=[{"type": "notify", "to": ["employee"], "title": "Phone now {{changes.phone.new}}"}])
    assert company_a.put(f"/employees/{emp['uuid']}", json={"last_name": "Menon"}).status_code == 200
    assert len(_runs(company_a, any_change["uuid"])) == 1 and _runs(company_a, phone["uuid"]) == []
    assert company_a.put(f"/employees/{emp['uuid']}", json={"phone": "9876500000"}).status_code == 200
    assert len(_runs(company_a, phone["uuid"])) == 1
    assert any(n["title"] == "Phone now 9876500000" for n in _notifications(login_as(company_a, emp)))
    # field_changed needs fields; unknown fields are refused
    assert company_a.post("/automation/rules", json={"name": "x", "entity": "employee", "trigger": "field_changed",
                                                     "actions": [{"type": "notify", "to": ["employee"], "title": "t"}]}
                          ).status_code == 400
    bad = company_a.post("/automation/rules", json={"name": "x", "entity": "employee", "trigger": "created",
                                                    "condition": "record.days >", "actions": [
                                                        {"type": "notify", "to": ["employee"], "title": "t"}]})
    assert bad.status_code == 400 and "Condition" in bad.json()["detail"]


def test_approved_trigger_via_a_request(company_a):
    rule = _rule(company_a, name="On approval", entity="request", trigger="approved",
                 request_type="employee.profile_change",
                 actions=[{"type": "notify", "to": ["employee"], "title": "{{record.title}} done"}])
    rejected = _rule(company_a, name="On rejection", entity="request", trigger="rejected",
                     actions=[{"type": "notify", "to": ["employee"], "title": "Sorry"}])
    emp = company_a.add_employee("Deepa", "Shah")
    me = login_as(company_a, emp)
    assert me.post("/me/profile-change", json={"changes": {"phone": "9000000011"}, "reason": "New"}).status_code == 201
    task = company_a.get("/workflow/inbox").json()["items"][0]
    r = company_a.post(f"/workflow/tasks/{task['uuid']}/decision", json={"decision": "approve"})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    runs = _runs(company_a, rule["uuid"])
    assert len(runs) == 1 and runs[0]["status"] == "done"
    assert _runs(company_a, rejected["uuid"]) == []
    assert any(n["title"].endswith(" done") for n in _notifications(me))


def test_date_rule_runs_once_per_occurrence(company_a):
    today = None
    with tenant_session(company_a.id) as db:
        today = today_for_company_id(db, company_a.id)
    emp = company_a.add_employee("Esha", "Pillai", probation_end_date=(today + timedelta(days=7)).isoformat())
    company_a.add_employee("Farah", "Khan", probation_end_date=(today + timedelta(days=8)).isoformat())
    rule = _rule(company_a, name="Probation reminder", trigger="date", date_field="probation_end_date", offset_days=-7,
                 actions=[{"type": "notify", "to": ["employee"], "title": "Probation ends {{record.probation_end_date}}"}])
    assert company_a.post("/automation/run-date-rules").json()["ran"] == 1
    assert company_a.post("/automation/run-date-rules").json()["ran"] == 0
    runs = _runs(company_a, rule["uuid"])
    assert len(runs) == 1 and runs[0]["record_label"] == "Esha Pillai"
    assert any("Probation ends" in n["title"] for n in _notifications(login_as(company_a, emp)))


def test_yearly_birthday_rule(company_a):
    with tenant_session(company_a.id) as db:
        today = today_for_company_id(db, company_a.id)
    born = today.replace(year=today.year - 30) if not (today.month == 2 and today.day == 29) else today.replace(
        year=today.year - 28)
    company_a.add_employee("Gita", "Joshi", date_of_birth=born.isoformat())
    rule = _rule(company_a, name="Birthday", trigger="date", date_field="date_of_birth", repeat_yearly=True,
                 actions=[{"type": "notify", "to": ["employee"], "title": "Happy birthday"}])
    assert company_a.post("/automation/run-date-rules").json()["ran"] == 1
    assert len(_runs(company_a, rule["uuid"])) == 1


# ── Actions ─────────────────────────────────────────────────────────

def test_email_task_workflow_and_update_field_actions(company_a):
    from modules.notifications.models import NotificationDelivery
    from modules.tasks.models import Task
    from core.workflow.models import WorkflowInstance
    rule = _rule(company_a, name="Everything", actions=[
        {"type": "email", "to": ["employee"], "addresses": ["hr@example.com"], "subject": "Welcome {{employee.name}}",
         "body": "Hi {{employee.first_name}}"},
        {"type": "task", "title": "Set up laptop for {{employee.first_name}}", "assignee": "employee", "due_in_days": 3},
        {"type": "update_field", "field": "notice_period_days", "value": 60},
        {"type": "workflow", "title": "Check {{employee.name}}"},
    ])
    emp = company_a.add_employee("Hari", "Varma")
    runs = _runs(company_a, rule["uuid"])
    assert len(runs) == 1 and runs[0]["status"] == "done", runs
    with tenant_session(company_a.id) as db:
        sent = db.query(NotificationDelivery).filter(NotificationDelivery.company_id == company_a.id,
                                                     NotificationDelivery.source == "automation").all()
        assert {d.to_address for d in sent} >= {emp["email"], "hr@example.com"}
        assert all(d.subject == "Welcome Hari Varma" for d in sent)
        task = db.query(Task).filter(Task.company_id == company_a.id, Task.source_module == "automation").one()
        assert task.title == "Set up laptop for Hari" and task.due_date is not None
        inst = db.query(WorkflowInstance).filter(WorkflowInstance.company_id == company_a.id,
                                                 WorkflowInstance.request_type == "automation.approval").one()
        assert inst.title == "Check Hari Varma"
    with tenant_session(company_a.id) as db:
        from modules.employees.models import Employee
        assert db.query(Employee.notice_period_days).filter(Employee.id == emp["id"]).scalar() == 60
    # invalid values are refused when saving
    bad = company_a.post("/automation/rules", json={"name": "x", "entity": "employee", "trigger": "created", "actions": [
        {"type": "update_field", "field": "work_mode", "value": "moon"}]})
    assert bad.status_code == 400
    bad = company_a.post("/automation/rules", json={"name": "x", "entity": "employee", "trigger": "created", "actions": [
        {"type": "update_field", "field": "password", "value": "x"}]})
    assert bad.status_code == 400


def test_update_field_does_not_loop(company_a):
    rule = _rule(company_a, name="Remote on change", trigger="updated",
                 actions=[{"type": "update_field", "field": "work_mode", "value": "remote"}])
    emp = company_a.add_employee("Indu", "Bose")
    assert company_a.put(f"/employees/{emp['uuid']}", json={"last_name": "Ghosh"}).status_code == 200
    assert company_a.get(f"/employees/{emp['uuid']}").json()["work_mode"] == "remote"
    assert len(_runs(company_a, rule["uuid"])) == 1


def test_webhook_is_signed_and_retried(company_a, receiver):
    from modules.automation import webhooks
    from modules.automation.models import AutomationWebhookDelivery
    hook = company_a.post("/automation/webhooks", json={"name": "HRIS", "url": receiver.url,
                                                        "headers": {"X-Api-Key": "k1"}})
    assert hook.status_code == 201, hook.text
    hook = hook.json()
    secret = hook["secret"]
    assert secret.startswith("whsec_")
    assert company_a.get("/automation/webhooks").json()[0]["secret"] is None
    rule = _rule(company_a, name="Tell HRIS", actions=[{"type": "webhook", "webhook_uuid": hook["uuid"]}])
    emp = company_a.add_employee("Jaya", "Kapoor")
    assert len(receiver.calls) == 1
    call = receiver.calls[0]
    expected = "sha256=" + hmac.new(secret.encode(), call["body"], hashlib.sha256).hexdigest()
    assert call["headers"]["X-Hrms-Signature"] == expected
    assert call["headers"]["X-Api-Key"] == "k1" and call["headers"]["X-Hrms-Timestamp"]
    payload = json.loads(call["body"])
    assert payload["record"]["first_name"] == "Jaya" and payload["rule"]["name"] == "Tell HRIS"
    assert _runs(company_a, rule["uuid"])[0]["status"] == "done"

    # a failing receiver: queued for retry, then retried by the job
    receiver.status = 500
    company_a.add_employee("Kiran", "Lal")
    items = company_a.get("/automation/webhook-deliveries", params={"status": "queued"}).json()["items"]
    assert len(items) == 1 and items[0]["attempts"] == 1 and items[0]["response_status"] == 500
    receiver.status = 200
    with tenant_session(company_a.id) as db:
        d = db.query(AutomationWebhookDelivery).filter(AutomationWebhookDelivery.company_id == company_a.id,
                                                       AutomationWebhookDelivery.status == "queued").one()
        d.next_attempt_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
        assert webhooks.retry_due(db, company_a.id) == 1
        db.commit()
    sent = company_a.get("/automation/webhook-deliveries", params={"status": "sent"}).json()["items"]
    assert len(sent) == 2 and any(s["attempts"] == 2 for s in sent)
    # gives up after five attempts
    receiver.status = 500
    r = company_a.post(f"/automation/webhooks/{hook['uuid']}/test").json()
    assert r["status"] == "queued"
    for _ in range(4):
        r = company_a.post(f"/automation/webhook-deliveries/{r['uuid']}/retry").json()
    assert r["status"] == "failed" and r["attempts"] == 5


def test_preview_does_nothing(company_a):
    emp = company_a.add_employee("Lata", "Mehta", work_mode="hybrid")
    draft = {"name": "Draft", "entity": "employee", "trigger": "updated", "condition": "record.work_mode == 'hybrid'",
             "record_uuid": emp["uuid"],
             "actions": [{"type": "notify", "to": ["employee"], "title": "Hi {{employee.first_name}}"},
                         {"type": "update_field", "field": "work_mode", "value": "office"}]}
    r = company_a.post("/automation/preview", json=draft)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["matched"] is True and "Hi Lata" in body["results"][0]["detail"]
    assert company_a.get(f"/employees/{emp['uuid']}").json()["work_mode"] == "hybrid"
    assert company_a.get("/automation/runs").json()["total"] == 0
    r = company_a.post("/automation/preview", json={**draft, "condition": "record.work_mode == 'office'"}).json()
    assert r["matched"] is False and r["results"] == []


def test_examples_seeded_inactive(company_a):
    rules = company_a.get("/automation/rules").json()["items"]
    assert len(rules) >= 2 and all(not r["active"] for r in rules)


def test_permissions_and_isolation(company_a, company_b):
    emp = company_a.add_employee("Mohan", "Nair")
    me = login_as(company_a, emp)
    assert me.get("/automation/rules").status_code == 403
    assert me.post("/automation/rules", json={"name": "x", "entity": "employee", "trigger": "created",
                                              "actions": []}).status_code == 403
    rule = _rule(company_a, name="Only A")
    hook = company_a.post("/automation/webhooks", json={"name": "A hook", "url": "http://127.0.0.1:9/x"}).json()
    assert company_b.get(f"/automation/rules/{rule['uuid']}").status_code == 404
    assert company_b.put(f"/automation/rules/{rule['uuid']}", json={"name": "x", "entity": "employee",
                                                                   "trigger": "created", "actions": [
                                                                       {"type": "notify", "to": ["employee"],
                                                                        "title": "t"}]}).status_code == 404
    assert all(r["name"] != "Only A" for r in company_b.get("/automation/rules").json()["items"])
    assert company_b.post(f"/automation/webhooks/{hook['uuid']}/test").status_code == 404
    # B can't point a rule at A's webhook
    bad = company_b.post("/automation/rules", json={"name": "x", "entity": "employee", "trigger": "created",
                                                    "actions": [{"type": "webhook", "webhook_uuid": hook["uuid"]}]})
    assert bad.status_code == 400
    # B's new employees never trigger A's rules
    company_b.add_employee("Nina", "Paul")
    assert all(r["record_label"] != "Nina Paul" for r in _runs(company_a, rule["uuid"]))
    assert company_b.get("/automation/runs").json()["total"] == 0
