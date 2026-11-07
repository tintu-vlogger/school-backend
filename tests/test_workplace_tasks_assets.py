"""Tasks (permissions, recurrence, checklist, the service other modules call) and assets
(assign, acknowledge, return, recovery for final settlement, requests)."""
from datetime import date, timedelta

from core.db import tenant_session
from modules.tasks import service as tasks
from tests.conftest import login_as


def test_recurrence_dates():
    assert tasks.next_date(date(2026, 1, 31), {"freq": "monthly", "interval": 1}) == date(2026, 2, 28)
    assert tasks.next_date(date(2026, 11, 7), {"freq": "weekly", "interval": 2}) == date(2026, 11, 21)
    assert tasks.next_date(date(2028, 2, 29), {"freq": "yearly", "interval": 1}) == date(2029, 2, 28)


def test_task_flow_permissions_and_recurrence(company_a):
    mgr = company_a.add_employee("Manju", role="manager")
    emp = company_a.add_employee("Esa", reporting_manager_id=mgr["id"])
    other = company_a.add_employee("Oli")
    m, e, o = login_as(company_a, mgr), login_as(company_a, emp), login_as(company_a, other)
    due = str(date.today() + timedelta(days=3))
    r = m.post("/tasks", json={"title": "Weekly report", "assignee_employee_id": emp["id"], "due_date": due,
                               "priority": "high", "checklist": ["Collect numbers", "Send"],
                               "recurrence": {"freq": "weekly", "interval": 1}})
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["checklist_total"] == 2 and t["can_edit"]
    assert any(x["uuid"] == t["uuid"] for x in e.get("/tasks", params={"scope": "mine"}).json()["items"])
    assert any(x["uuid"] == t["uuid"] for x in m.get("/tasks", params={"scope": "team"}).json()["items"])
    assert any(x["uuid"] == t["uuid"] for x in m.get("/tasks", params={"scope": "assigned_by_me"}).json()["items"])
    # outsiders can't see it; the assignee works on it but can't edit or cancel it
    assert o.get(f"/tasks/{t['uuid']}").status_code == 404
    assert e.put(f"/tasks/{t['uuid']}", json={"title": "Hacked"}).status_code == 403
    assert e.post(f"/tasks/{t['uuid']}/status", json={"status": "cancelled"}).status_code == 403
    item = t["checklist"][0]["uuid"]
    d = e.put(f"/tasks/{t['uuid']}/checklist/{item}", json={"done": True}).json()
    assert d["checklist_done"] == 1 and d["status"] == "in_progress"
    e.post(f"/tasks/{t['uuid']}/comments", json={"body": "Half done"})
    done = e.post(f"/tasks/{t['uuid']}/status", json={"status": "done"}).json()
    nxt = done["next_occurrence"]
    assert nxt and nxt["due_date"] == str(date.today() + timedelta(days=10))
    again = e.get(f"/tasks/{nxt['uuid']}").json()
    assert again["checklist_total"] == 2 and again["checklist_done"] == 0
    # completing the same task again does not create a second copy
    e.post(f"/tasks/{t['uuid']}/status", json={"status": "in_progress"})
    assert e.post(f"/tasks/{t['uuid']}/status", json={"status": "done"}).json()["next_occurrence"] is None
    board = e.get("/tasks", params={"scope": "mine", "board": True}).json()["items"]
    assert {x["status"] for x in board} >= {"todo", "done"}


def test_other_modules_create_tasks(company_a):
    emp = company_a.add_employee("Ira")
    db = tenant_session(company_a.id)
    try:
        t = tasks.create_task(db, company_a.id, title="Issue laptop", assignee_employee_id=emp["id"],
                              due_date=date.today(), source_module="onboarding", source_ref="chk-1",
                              checklist=["Image laptop", "Create email"], created_by_name="Onboarding")
        db.commit()
        assert [x.uuid for x in tasks.tasks_for_source(db, company_a.id, "onboarding", "chk-1")] == [t.uuid]
        # reminders: due today, so one reminder now and none on a second run
        assert tasks.run_reminders(db) >= 1
        db.commit()
        before = tasks.run_reminders(db)
        db.commit()
    finally:
        db.close()
    items = login_as(company_a, emp).get("/tasks", params={"scope": "mine", "source_module": "onboarding"}).json()["items"]
    assert items and items[0]["title"] == "Issue laptop" and items[0]["checklist_total"] == 2
    assert before == 0


def _cat(company, label="Laptop"):
    opts = company.get("/masters/options/asset_category", params={"q": label}).json()["items"]
    return next(o["value"] for o in opts if o["label"] == label)


def test_asset_assign_acknowledge_return_and_recovery(company_a):
    emp = company_a.add_employee("Asif")
    r = company_a.post("/assets", json={"name": "Dell Latitude 5440", "category_id": _cat(company_a), "serial_number": "SN-1",
                                        "purchase_value": "78500.00", "purchase_date": "2026-01-10",
                                        "warranty_until": "2029-01-09"})
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["asset_tag"].startswith("AST") and a["status"] == "available"
    assert company_a.post("/assets", json={"name": "Dup", "serial_number": "SN-1"}).status_code == 400
    d = company_a.post(f"/assets/{a['uuid']}/assign", json={"employee_id": emp["id"], "condition": "new"}).json()
    assert d["status"] == "assigned" and d["assigned_to"]["acknowledged"] is False
    # can't hand the same asset to someone else
    other = company_a.add_employee("Ola")
    assert company_a.post(f"/assets/{a['uuid']}/assign", json={"employee_id": other["id"]}).status_code == 400
    me = login_as(company_a, emp)
    mine = me.get("/assets/me").json()["items"]
    x = mine[0]
    assert x["status"] == "pending_ack" and x["asset"]["asset_tag"] == a["asset_tag"]
    assert login_as(company_a, other).post(f"/assets/assignments/{x['uuid']}/acknowledge").status_code == 403
    assert me.post(f"/assets/assignments/{x['uuid']}/acknowledge").json()["status"] == "active"
    out = company_a.get(f"/assets/outstanding/{emp['id']}").json()
    assert len(out["items"]) == 1
    # returned damaged with a recovery: shows up for final settlement
    assert company_a.post(f"/assets/assignments/{x['uuid']}/return",
                          json={"condition": "damaged", "recovery_amount": "4500"}).status_code == 400  # needs reason
    ret = company_a.post(f"/assets/assignments/{x['uuid']}/return",
                         json={"condition": "damaged", "recovery_amount": "4500", "recovery_reason": "Cracked screen"})
    assert ret.status_code == 200 and ret.json()["recovery_status"] == "pending"
    rec = company_a.get("/assets/recoveries", params={"employee_id": emp["id"]}).json()
    assert rec["total_pending"] == "4500.00"
    after = company_a.get(f"/assets/{a['uuid']}").json()
    assert after["status"] == "in_repair" and after["assigned_to"] is None and len(after["history"]) == 1
    company_a.post(f"/assets/{a['uuid']}/maintenance", json={"kind": "repair", "performed_on": str(date.today()),
                                                            "cost": "4500", "status": "done"})
    assert company_a.get(f"/assets/{a['uuid']}").json()["status"] == "available"
    company_a.post(f"/assets/assignments/{x['uuid']}/recovery", json={"status": "waived"})
    assert company_a.get("/assets/recoveries", params={"employee_id": emp["id"]}).json()["total_pending"] == "0.00"
    rep = company_a.get("/assets/report").json()
    assert rep["total"] >= 1


def test_asset_request_through_approval(company_a):
    emp = company_a.add_employee("Rhea")
    me = login_as(company_a, emp)
    r = me.post("/assets/requests", json={"category_id": _cat(company_a, "Monitor"), "details": "Second screen"})
    assert r.status_code == 201, r.text
    for _ in range(3):  # manager step, then HR step (both fall to the owner here)
        waiting = company_a.get("/workflow/inbox").json()["items"]
        if not waiting:
            break
        company_a.post(f"/workflow/tasks/{waiting[0]['uuid']}/decision", json={"decision": "approve"})
    reqs = company_a.get("/assets/requests", params={"status": "approved"}).json()["items"]
    req = next(x for x in reqs if x["employee_id"] == emp["id"])
    a = company_a.post("/assets", json={"name": "LG monitor", "category_id": _cat(company_a, "Monitor")}).json()
    assert company_a.post(f"/assets/{a['uuid']}/assign", json={"employee_id": emp["id"],
                                                             "request_uuid": req["uuid"]}).status_code == 200
    assert me.get("/assets/me").json()["requests"][0]["status"] == "fulfilled"
    # employees can't see the register
    assert me.get("/assets").status_code == 403
