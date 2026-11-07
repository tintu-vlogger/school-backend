"""Reports, dashboards, import/export, compliance, the data assistant and the MCP server.

Covers: row-level scope (a manager sees only their team, an employee only themselves),
field masking, export audit and password protection, import validation and update by
key, assistant tools and MCP tokens respecting access, and tenant isolation."""
import io
import uuid

import pytest

from tests.conftest import login_as


def _team(company):
    """A manager with two reports, and one person outside the team."""
    boss = company.add_employee("Meera", "Rao", role="manager", pan="ABCPR1234K")
    a = company.add_employee("Arun", "Shah", pan="ABCPS1234L")
    b = company.add_employee("Bina", "Das")
    out = company.add_employee("Omar", "Khan", pan="ABCPK1234M")
    for e in (a, b):
        r = company.put(f"/employees/{e['uuid']}", json={"reporting_manager_id": boss["id"]})
        assert r.status_code == 200, r.text
    return boss, a, b, out


def _report(company, key):
    rows = company.get("/reports/").json()
    return next(r for r in rows if r["key"] == key)


def _names(res):
    return sorted(r.get("employee_name") for r in res["rows"])


# ── Report scope and masking ───────────────────────────────────────

def test_standard_reports_and_dashboards_seeded(company_a):
    keys = {r["key"] for r in company_a.get("/reports/").json()}
    assert {"headcount", "joiners", "leavers", "attrition", "birthdays", "probation_due", "documents_expiring",
            "leave_balances", "lop", "attendance_summary", "overtime", "muster_roll", "late_early",
            "helpdesk_sla", "survey_results", "training_completion"} <= keys
    dash = {d["key"] for d in company_a.get("/dashboards/").json()}
    assert {"hr_overview", "workforce", "attendance", "leave", "engagement"} <= dash


def test_manager_sees_only_their_team(company_a):
    boss, a, b, out = _team(company_a)
    rep = _report(company_a, "employee_list")
    owner = company_a.post(f"/reports/{rep['uuid']}/run", json={}).json()
    assert owner["scope"] == "all"
    assert {"Arun Shah", "Bina Das", "Omar Khan", "Meera Rao"} <= set(_names(owner))

    mgr = login_as(company_a, boss)
    res = mgr.post(f"/reports/{rep['uuid']}/run", json={})
    assert res.status_code == 200, res.text
    res = res.json()
    assert res["scope"] == "team"
    assert _names(res) == ["Arun Shah", "Bina Das", "Meera Rao"]

    # a grouped report counts only the team too
    hc = mgr.post(f"/reports/{_report(company_a, 'headcount')['uuid']}/run", json={}).json()
    assert hc["totals"]["count"] == 3

    # ad hoc preview with the scope narrowed to the manager's own row
    me = mgr.post("/reports/preview", json={"source": "employees", "config": {"columns": ["employee_name"]},
                                             "scope": "self"}).json()
    assert _names(me) == ["Meera Rao"]


def test_employee_cannot_open_reports_but_shared_report_shows_own_rows(company_a):
    boss, a, b, out = _team(company_a)
    emp = login_as(company_a, out)
    assert emp.get("/reports/").json() == []
    rep = _report(company_a, "employee_list")
    assert emp.post(f"/reports/{rep['uuid']}/run", json={}).status_code == 404
    # HR shares it with the employee role: they see only themselves
    r = company_a.post(f"/reports/{rep['uuid']}/share", json={"shared_roles": ["employee"]})
    assert r.status_code == 200, r.text
    res = emp.post(f"/reports/{rep['uuid']}/run", json={}).json()
    assert res["scope"] == "self" and _names(res) == ["Omar Khan"]
    # payroll data stays closed to a manager even when shared
    mgr = login_as(company_a, boss)
    pay = _report(company_a, "payroll_register")
    company_a.post(f"/reports/{pay['uuid']}/share", json={"shared_roles": ["manager"]})
    res = mgr.post(f"/reports/{pay['uuid']}/run", json={}).json()
    assert res["scope"] == "self"


def test_sensitive_fields_are_masked_for_managers(company_a):
    boss, a, b, out = _team(company_a)
    mgr = login_as(company_a, boss)
    res = mgr.post("/reports/preview", json={"source": "employees", "config": {"columns": ["employee_name", "pan"]}})
    res = res.json()
    by = {r["employee_name"]: r["pan"] for r in res["rows"]}
    assert by["Arun Shah"] is None and "pan" in res["masked"]
    owner = company_a.post("/reports/preview", json={"source": "employees",
                                                       "config": {"columns": ["employee_name", "pan"]}}).json()
    assert {r["employee_name"]: r["pan"] for r in owner["rows"]}["Arun Shah"] == "ABCPS1234L"


def test_custom_report_group_filter_and_sharing(company_a):
    boss, a, b, out = _team(company_a)
    r = company_a.post("/reports/", json={
        "name": "People by stage", "source": "employees",
        "config": {"filters": [{"field": "first_name", "op": "in", "value": ["Arun", "Bina", "Omar"]}],
                   "group_by": ["status"], "measures": [{"agg": "count"}]},
        "shared_roles": ["manager"]})
    assert r.status_code == 201, r.text
    rep = r.json()
    res = company_a.post(f"/reports/{rep['uuid']}/run", json={}).json()
    assert res["totals"]["count"] == 3
    mgr = login_as(company_a, boss)
    assert any(x["uuid"] == rep["uuid"] for x in mgr.get("/reports/").json())
    assert mgr.post(f"/reports/{rep['uuid']}/run", json={}).json()["totals"]["count"] == 2  # Meera is not in the filter
    # a manager can't edit someone else's report
    assert mgr.put(f"/reports/{rep['uuid']}", json={"name": "Mine now"}).status_code == 403
    bad = company_a.post("/reports/", json={"name": "Bad", "source": "employees",
                                            "config": {"filters": [{"field": "nope", "op": "eq", "value": 1}]}})
    assert bad.status_code == 400


# ── Exports ────────────────────────────────────────────────────────

def test_every_export_is_audited_and_can_be_password_protected(company_a):
    _team(company_a)
    rep = _report(company_a, "employee_list")
    r = company_a.post(f"/reports/{rep['uuid']}/export", json={"format": "xlsx", "protect": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["password"] and len(body["password"]) >= 12
    f = company_a.get(f"/reports/exports/{body['uuid']}/download")
    assert f.status_code == 200
    assert f.content[:4] == bytes.fromhex("D0CF11E0")      # encrypted Office file
    plain = company_a.post(f"/reports/{rep['uuid']}/export", json={"format": "csv"}).json()
    assert plain["password"] is None
    csv_body = company_a.get(f"/reports/exports/{plain['uuid']}/download").text
    assert "Arun Shah" in csv_body
    pdf = company_a.post(f"/reports/{rep['uuid']}/export", json={"format": "pdf", "protect": True}).json()
    assert company_a.get(f"/reports/exports/{pdf['uuid']}/download").content[:4] == b"%PDF"

    hist = company_a.get("/reports/exports").json()
    assert hist["total"] >= 3 and hist["items"][0]["actor_name"]
    audit = company_a.get("/audit/", params={"page": 1, "size": 50}).json()
    actions = [x["action"] for x in audit["items"]]
    assert "export.created" in actions and "export.downloaded" in actions


def test_export_download_only_by_its_maker(company_a):
    boss, *_ = _team(company_a)
    rep = _report(company_a, "employee_list")
    body = company_a.post(f"/reports/{rep['uuid']}/export", json={"format": "csv"}).json()
    mgr = login_as(company_a, boss)
    assert mgr.get(f"/reports/exports/{body['uuid']}/download").status_code == 404


def test_schedule_runs_with_creators_access_and_is_logged(company_a):
    _team(company_a)
    rep = _report(company_a, "headcount")
    s = company_a.post("/reports/schedules", json={"report_uuid": rep["uuid"], "name": "Weekly headcount",
                                                   "frequency": "weekly", "weekday": 0, "hour": 9, "format": "xlsx",
                                                   "recipient_emails": ["hr@example.com"]})
    assert s.status_code == 201, s.text
    s = s.json()
    assert s["next_run_at"]
    sent = company_a.post(f"/reports/schedules/{s['uuid']}/send-now").json()
    assert sent["last_status"] in ("sent", "not_sent")
    hist = company_a.get("/reports/exports", params={"channel": "schedule"}).json()
    assert hist["total"] >= 1
    assert company_a.post("/reports/schedules", json={"report_uuid": rep["uuid"], "name": "x",
                                                      "frequency": "weekly", "weekday": 0}).status_code == 400


# ── Dashboards ─────────────────────────────────────────────────────

def test_dashboard_widgets_use_viewer_access(company_a):
    boss, *_ = _team(company_a)
    d = next(x for x in company_a.get("/dashboards/").json() if x["key"] == "hr_overview")
    full = company_a.get(f"/dashboards/{d['uuid']}").json()
    k1 = company_a.get(f"/dashboards/{d['uuid']}/widgets/k1/data").json()
    assert k1["status"] == "ok" and k1["value"] >= 4
    company_a.put(f"/dashboards/{d['uuid']}", json={"shared_roles": ["manager"]})
    mgr = login_as(company_a, boss)
    m1 = mgr.get(f"/dashboards/{d['uuid']}/widgets/k1/data").json()
    assert m1["value"] == 3
    # build a new dashboard with a chart and move it
    new = company_a.post("/dashboards/", json={"name": "My board", "widgets": [
        {"type": "bar", "title": "By department", "source": "employees", "x": 0, "y": 0, "w": 6, "h": 4,
         "config": {"group_by": ["department"], "measures": [{"agg": "count"}]}}]})
    assert new.status_code == 201, new.text
    w = new.json()["widgets"][0]
    moved = company_a.put(f"/dashboards/{new.json()['uuid']}", json={"widgets": [{**w, "x": 6, "y": 2}]}).json()
    assert moved["widgets"][0]["x"] == 6
    assert len(full["widgets"]) >= 5


# ── Import and export wizard ───────────────────────────────────────

def _upload(company, entity, text, name="file.csv"):
    r = company.client.post("/api/v1/data/imports", headers=company.headers, data={"entity": entity},
                            files={"file": (name, io.BytesIO(text.encode()), "text/csv")})
    assert r.status_code == 201, r.text
    return r.json()


def test_import_validation_and_update_by_key(company_a):
    tag = uuid.uuid4().hex[:6]
    csv = ("Emp ID,First Name,Last Name,Email,Department,Date of joining\n"
           f"IMP{tag}1,Kavya,Iyer,kavya.{tag}@example.com,,01-04-2026\n"
           f"IMP{tag}2,Rohit,Verma,not-an-email,,01-04-2026\n"
           f"IMP{tag}3,Sana,Ali,sana.{tag}@example.com,Nowhere Dept,01-04-2026\n")
    job = _upload(company_a, "employees", csv)
    assert job["mapping"]["employee_code"] == "Emp ID" and job["mapping"]["email"] == "Email"
    v = company_a.post(f"/data/imports/{job['uuid']}/validate").json()
    rows_with_errors = {e["row"] for e in v["errors"]}
    assert rows_with_errors == {3, 4}
    assert v["options"]["would_add"] == 1
    run = company_a.post(f"/data/imports/{job['uuid']}/run").json()
    done = company_a.get(f"/data/imports/{run['uuid']}").json()
    assert done["status"] == "done" and done["added"] == 1 and done["failed"] == 2
    err = company_a.get(f"/data/imports/{job['uuid']}/errors")
    assert err.status_code == 200 and len(err.content) > 100

    # update by employee ID: change the last name of the imported person
    csv2 = f"Employee ID,First name,Last name,Work email\nIMP{tag}1,Kavya,Iyengar,kavya.{tag}@example.com\n"
    job2 = _upload(company_a, "employees", csv2)
    company_a.put(f"/data/imports/{job2['uuid']}", json={"mode": "update", "match_key": "employee_code"})
    v2 = company_a.post(f"/data/imports/{job2['uuid']}/validate").json()
    assert not v2["errors"] and v2["options"]["would_update"] == 1
    company_a.post(f"/data/imports/{job2['uuid']}/run")
    done2 = company_a.get(f"/data/imports/{job2['uuid']}").json()
    assert done2["updated"] == 1, done2
    found = company_a.get("/employees/", params={"q": f"IMP{tag}1", "page": 1}).json()
    assert found["items"][0]["last_name"] == "Iyengar"
    # update mode refuses rows that match nothing
    job3 = _upload(company_a, "employees", f"Employee ID,First name,Last name,Email\nNOPE{tag},A,B,a.{tag}@x.com\n")
    company_a.put(f"/data/imports/{job3['uuid']}", json={"mode": "update", "match_key": "employee_code"})
    assert company_a.post(f"/data/imports/{job3['uuid']}/validate").json()["errors"]
    hist = company_a.get("/data/jobs", params={"kind": "import"}).json()
    assert hist["total"] >= 3


def test_import_needs_permission_and_export_round_trip(company_a):
    boss, a, *_ = _team(company_a)
    mgr = login_as(company_a, boss)
    r = mgr.client.post("/api/v1/data/imports", headers=mgr.headers, data={"entity": "employees"},
                        files={"file": ("f.csv", io.BytesIO(b"First name\nX\n"), "text/csv")})
    assert r.status_code == 403
    ex = company_a.post("/data/exports", json={"entity": "departments", "format": "csv"}).json()
    assert ex["export_uuid"]
    body = company_a.get(f"/reports/exports/{ex['export_uuid']}/download").text
    assert body.splitlines()[0].lstrip("﻿").startswith("Department")
    tpl = company_a.get("/data/entities/assets/template")
    assert tpl.status_code == 200


def test_masters_import_upsert(company_a):
    csv = "List,Value,Code\nAsset category,Drone,drone\nAsset category,Drone,drone\n"
    job = _upload(company_a, "masters", csv)
    company_a.put(f"/data/imports/{job['uuid']}", json={"mode": "upsert", "match_key": "list_code"})
    v = company_a.post(f"/data/imports/{job['uuid']}/validate").json()
    assert len(v["errors"]) == 1          # the duplicate row
    company_a.post(f"/data/imports/{job['uuid']}/run")
    assert company_a.get(f"/data/imports/{job['uuid']}").json()["added"] == 1


# ── Assistant and MCP ──────────────────────────────────────────────

def test_assistant_tools_respect_access(company_a):
    boss, a, b, out = _team(company_a)
    owner = company_a.post("/assistant/ask", json={"question": "How many people do we have?"}).json()
    assert owner["answer"]["mode"] == "builtin"
    mgr = login_as(company_a, boss)
    ans = mgr.post("/assistant/ask", json={"question": "How many people do we have?"}).json()["answer"]
    assert ans["text"].startswith("3 people")
    found = mgr.post("/assistant/ask", json={"question": "Find Omar"}).json()["answer"]
    assert "No one" in found["text"]
    emp = login_as(company_a, out)
    e = emp.post("/assistant/ask", json={"question": "headcount"}).json()["answer"]
    assert e["text"].startswith("1 people")
    t = emp.post("/assistant/ask", json={"question": "what can you do"}).json()
    assert "AI model" in t["answer"]["text"]
    threads = emp.get("/assistant/threads").json()
    assert len(threads) == 2
    assert mgr.get(f"/assistant/threads/{threads[0]['uuid']}").status_code == 404


def test_mcp_token_auth_scope_and_revoke(company_a, company_b):
    boss, a, b, out = _team(company_a)
    company_b.add_employee("Zed", "Other")
    c = company_a.client
    r = c.post("/api/v1/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401
    mgr = login_as(company_a, boss)
    tok = mgr.post("/integrations/tokens", json={"name": "Desktop assistant", "scopes": ["headcount",
                                                                                        "search_employees"]})
    assert tok.status_code == 201, tok.text
    tok = tok.json()
    h = {"Authorization": f"Bearer {tok['token']}"}
    init = c.post("/api/v1/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                 "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                                            "clientInfo": {"name": "test", "version": "1"}}}).json()
    assert init["result"]["serverInfo"]["name"] == "campusly"
    assert c.post("/api/v1/mcp", headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
                  ).status_code == 202
    listed = c.post("/api/v1/mcp", headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()
    assert sorted(t["name"] for t in listed["result"]["tools"]) == ["headcount", "search_employees"]
    call = c.post("/api/v1/mcp", headers=h, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                                 "params": {"name": "headcount", "arguments": {}}}).json()
    assert call["result"]["structuredContent"]["value"] == 3          # the manager and their team, nothing more
    denied = c.post("/api/v1/mcp", headers=h, json={"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                                   "params": {"name": "run_report", "arguments": {"report": "x"}}}
                    ).json()
    assert denied["result"]["isError"]
    audit = company_a.get("/audit/", params={"page": 1, "size": 100}).json()
    assert "assistant.tool_called" in [x["action"] for x in audit["items"]]
    # an employee without the permission can't make tokens
    emp = login_as(company_a, out)
    assert emp.post("/integrations/tokens", json={"name": "x"}).status_code == 403
    # revoke
    mgr.delete(f"/integrations/tokens/{tok['uuid']}")
    assert c.post("/api/v1/mcp", headers=h, json={"jsonrpc": "2.0", "id": 5, "method": "ping"}).status_code == 401


def test_owner_token_sees_only_own_company(company_a, company_b):
    _team(company_a)
    company_b.add_employee("Zed", "Other")
    tok = company_b.post("/integrations/tokens", json={"name": "B"}).json()
    h = {"Authorization": f"Bearer {tok['token']}"}
    r = company_b.client.post("/api/v1/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                               "params": {"name": "search_employees",
                                                                          "arguments": {"query": "Arun"}}}).json()
    assert r["result"]["structuredContent"]["row_count"] == 0


def test_ai_model_settings_store_key_encrypted(company_a):
    r = company_a.put("/integrations/ai_model", json={"enabled": True, "config": {
        "provider": "openai", "base_url": "http://127.0.0.1:9/v1", "model": "test-model", "api_key": "sk-secret-1234"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["secret_hints"]["api_key"] == "…1234" and "api_key" not in body["config"]
    from core.db import SessionLocal, bypass_rls
    from modules.assistant.models import IntegrationSetting
    db = SessionLocal()
    with bypass_rls(db):
        row = db.query(IntegrationSetting).filter(IntegrationSetting.company_id == company_a.id,
                                                  IntegrationSetting.kind == "ai_model").one()
        assert "sk-secret" not in (row.secrets_encrypted or "") and "sk-secret" not in str(row.config)
    db.close()
    # the model can't be reached: the assistant falls back to the built-in answers
    ans = company_a.post("/assistant/ask", json={"question": "headcount"}).json()["answer"]
    assert ans["mode"] == "builtin" and "could not answer" in ans["text"]
    t = company_a.post("/integrations/ai_model/test").json()
    assert t["status"] == "error"


# ── Compliance ─────────────────────────────────────────────────────

def test_compliance_dashboard_and_marking(company_a):
    _team(company_a)
    d = company_a.get("/compliance/dashboard")
    assert d.status_code == 200, d.text
    d = d.json()
    keys = {a["key"] for a in d["alerts"]}
    assert {"min_wage", "missing_ids", "settlement", "overtime", "policy_ack"} <= keys
    missing = next(a for a in d["alerts"] if a["key"] == "missing_ids")
    assert missing["count"] >= 4          # nobody has a bank account or UAN yet
    reg = next(i for i in d["items"] if i["kind"] == "registers")
    assert company_a.post("/compliance/records", json={"key": reg["key"], "status": "done"}).status_code == 200
    d2 = company_a.get("/compliance/dashboard").json()
    assert next(i for i in d2["items"] if i["key"] == reg["key"])["status"] == "done"
    assert company_a.post("/compliance/records", json={"key": "payroll:pf:0::2026-10"}).status_code == 400


# ── Tenant isolation ───────────────────────────────────────────────

def test_tenant_isolation_of_insights(company_a, company_b):
    _team(company_a)
    rep = _report(company_a, "employee_list")
    exp = company_a.post(f"/reports/{rep['uuid']}/export", json={"format": "csv"}).json()
    dash = company_a.get("/dashboards/").json()[0]
    job = _upload(company_a, "departments", "Department\nIsolation test\n")
    assert company_b.post(f"/reports/{rep['uuid']}/run", json={}).status_code == 404
    assert company_b.get(f"/reports/exports/{exp['uuid']}/download").status_code == 404
    assert company_b.get(f"/dashboards/{dash['uuid']}").status_code == 404
    assert company_b.get(f"/data/imports/{job['uuid']}").status_code == 404
    names = _names(company_b.post(f"/reports/{_report(company_b, 'employee_list')['uuid']}/run", json={}).json())
    assert "Arun Shah" not in names
    # directly through a session scoped to company B: no rows of A
    from core.db import tenant_session
    from modules.reports.models import ExportLog, Report
    db = tenant_session(company_b.id)
    assert db.query(Report).filter(Report.company_id == company_a.id).count() == 0
    assert db.query(ExportLog).filter(ExportLog.company_id == company_a.id).count() == 0
    db.close()


@pytest.mark.parametrize("q,tool", [
    ("How many people by department?", "headcount"), ("who is on leave tomorrow", "who_is_on_leave"),
    ("what's my leave balance", "leave_balances"), ("who joined last month", "joiners_leavers"),
    ("show attendance for last month", "attendance_summary"), ("anything waiting for my approval?",
                                                              "pending_approvals"),
    ("find rahul", "search_employees"), ("run the overtime report", "run_report")])
def test_intent_matcher(q, tool):
    from modules.assistant import intents
    assert intents.match(q)[0] == tool
