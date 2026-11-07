"""HR process builder: process types, change requests through approval, job history,
future-dated changes, duplicates, revoke, permissions and tenant isolation."""
from datetime import date, timedelta

from core.db import tenant_session
from tests.conftest import login_as


def _types(company):
    r = company.get("/hr-processes/types")
    assert r.status_code == 200, r.text
    return {t["code"]: t for t in r.json()}


def _dept(company, name):
    r = company.post("/departments/", json={"department_name": name})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _location(company, name):
    items = company.get("/masters/options/state", params={"q": "Karnataka"}).json()["items"]
    state = next(i["value"] for i in items if i["label"] == "Karnataka")
    r = company.post("/organization/locations", json={"name": name, "city": name, "state_id": state})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _approve_all(company):
    for _ in range(5):
        items = company.get("/workflow/inbox").json()["items"]
        if not items:
            return
        for t in items:
            r = company.post(f"/workflow/tasks/{t['uuid']}/decision", json={"decision": "approve"})
            assert r.status_code == 200, r.text


def _hr(company):
    """HR raises changes, so the approval steps are not skipped as the owner's own request."""
    return login_as(company, company.add_employee("Hari", "Hr", role="hr"))


def _history(company, emp):
    return company.get(f"/employees/{emp['uuid']}/history").json()


def test_defaults_seeded_with_flow(company_a):
    types = _types(company_a)
    assert {"TRF", "PRM", "CNF", "DPT", "MGR"} <= set(types)
    assert types["TRF"]["key_field"] == "location_id"
    assert types["CNF"]["target_status"] == "confirmed"
    flows = company_a.get("/workflow/definitions", params={"request_type": "hr.process"}).json()
    assert flows and flows[0]["steps"][0]["approver_type"] == "field"
    meta = company_a.get("/hr-processes/meta").json()
    assert any(f["value"] == "designation_id" for f in meta["fields"])


def test_transfer_approved_applies_and_writes_history(company_a):
    hr = _hr(company_a)
    loc1, loc2 = _location(company_a, "Pune"), _location(company_a, "Chennai")
    emp = company_a.add_employee("Arun", "Rao", location_id=loc1, date_of_joining="2024-01-15")
    trf = _types(company_a)["TRF"]
    r = hr.post("/hr-processes/requests", json={
        "process_type_uuid": trf["uuid"], "employee_ids": [emp["id"]], "effective_date": date.today().isoformat(),
        "values": {"location_id": loc2}, "reason": "New office"})
    assert r.status_code == 201, r.text
    req = r.json()["items"][0]
    assert req["status"] == "pending"
    assert company_a.get(f"/employees/{emp['uuid']}").json()["location_id"] == loc1
    _approve_all(company_a)
    detail = company_a.get(f"/hr-processes/requests/{req['uuid']}").json()
    assert detail["status"] == "applied"
    assert detail["changes"][0]["old"] == "Pune" and detail["changes"][0]["new"] == "Chennai"
    assert company_a.get(f"/employees/{emp['uuid']}").json()["location_id"] == loc2
    hist = _history(company_a, emp)
    assert any(h["change_type"] == "transfer" and h["status"] == "applied" for h in hist)

    # revoke puts the old value back and is recorded
    r = company_a.post(f"/hr-processes/requests/{req['uuid']}/revoke", json={"reason": "Raised by mistake"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "revoked"
    assert company_a.get(f"/employees/{emp['uuid']}").json()["location_id"] == loc1
    assert any(h["change_type"] == "revoke" for h in _history(company_a, emp))
    assert company_a.post(f"/hr-processes/requests/{req['uuid']}/revoke", json={}).status_code == 400


def test_future_dated_waits_then_applies(company_a):
    hr = _hr(company_a)
    d1, d2 = _dept(company_a, "Sales"), _dept(company_a, "Marketing")
    emp = company_a.add_employee("Bina", department_id=d1, date_of_joining="2023-05-01")
    dpt = _types(company_a)["DPT"]
    later = date.today() + timedelta(days=10)
    r = hr.post("/hr-processes/requests", json={
        "process_type_uuid": dpt["uuid"], "employee_ids": [emp["id"]], "effective_date": later.isoformat(),
        "values": {"department_id": d2}, "reason": "Reorganisation"})
    req = r.json()["items"][0]
    _approve_all(company_a)
    assert company_a.get(f"/hr-processes/requests/{req['uuid']}").json()["status"] == "scheduled"
    assert company_a.get(f"/employees/{emp['uuid']}").json()["department_id"] == d1

    # the day comes
    from modules.hr_processes import service
    from modules.hr_processes.models import HrProcessRequest
    with tenant_session(company_a.id) as db:
        row = db.query(HrProcessRequest).filter(HrProcessRequest.uuid == req["uuid"]).one()
        row.effective_date = date.today()
        db.commit()
        assert service.apply_due(db, company_a.id) == 1
        db.commit()
    assert company_a.get(f"/employees/{emp['uuid']}").json()["department_id"] == d2

    # duplicate for the same person, process and date is blocked
    r = company_a.post("/hr-processes/requests", json={
        "process_type_uuid": dpt["uuid"], "employee_ids": [emp["id"]], "effective_date": date.today().isoformat(),
        "values": {"department_id": d1}, "reason": "Again"})
    assert r.status_code == 400 and "already has" in r.json()["detail"]


def test_checks_and_bulk(company_a):
    loc = _location(company_a, "Kochi")
    a = company_a.add_employee("Charu", date_of_joining="2025-01-01")
    b = company_a.add_employee("Dev", date_of_joining="2025-02-01")
    trf = _types(company_a)["TRF"]
    base = {"process_type_uuid": trf["uuid"], "effective_date": date.today().isoformat(), "reason": "Move"}
    # key field is required
    r = company_a.post("/hr-processes/requests", json={**base, "employee_ids": [a["id"]], "values": {}})
    assert r.status_code == 400 and "location" in r.json()["detail"].lower()
    # before joining
    r = company_a.post("/hr-processes/requests", json={**base, "employee_ids": [a["id"]], "values": {"location_id": loc},
                                                        "effective_date": "2024-06-01"})
    assert r.status_code == 400 and "joined" in r.json()["detail"]
    # a field the process does not change
    r = company_a.post("/hr-processes/requests", json={**base, "employee_ids": [a["id"]],
                                                        "values": {"location_id": loc, "grade_id": 1}})
    assert r.status_code == 400
    # many at once
    r = company_a.post("/hr-processes/requests", json={**base, "employee_ids": [a["id"], b["id"]], "values": {"location_id": loc}})
    assert r.status_code == 201 and len(r.json()["items"]) == 2
    assert r.json()["items"][0]["batch_id"]
    assert company_a.get("/hr-processes/requests", params={"status": "applied"}).json()["total"] == 2


def test_confirmation_and_custom_process(company_a):
    emp = company_a.add_employee("Esha", date_of_joining="2025-03-01", status="probation")
    assert company_a.get(f"/employees/{emp['uuid']}").json()["status"] == "probation"
    cnf = _types(company_a)["CNF"]
    r = company_a.post("/hr-processes/requests", json={
        "process_type_uuid": cnf["uuid"], "employee_ids": [emp["id"]], "effective_date": date.today().isoformat(),
        "values": {}, "reason": "Probation completed"})
    assert r.status_code == 201, r.text
    _approve_all(company_a)
    after = company_a.get(f"/employees/{emp['uuid']}").json()
    assert after["status"] == "confirmed"

    # a company's own process without approval
    r = company_a.post("/hr-processes/types", json={"code": "WFH", "name": "Work mode change", "change_type": "other",
                                                    "fields": ["work_mode"], "key_field": "work_mode",
                                                    "needs_approval": False})
    assert r.status_code == 201, r.text
    assert company_a.post("/hr-processes/types", json={"code": "WFH", "name": "Again", "fields": ["work_mode"]}).status_code == 400
    assert company_a.post("/hr-processes/types", json={"code": "BAD", "name": "Bad", "fields": ["salary"]}).status_code == 400
    r = company_a.post("/hr-processes/requests", json={
        "process_type_uuid": r.json()["uuid"], "employee_ids": [emp["id"]], "effective_date": date.today().isoformat(),
        "values": {"work_mode": "remote"}, "reason": "Moved city"})
    assert r.status_code == 201 and r.json()["items"][0]["status"] == "applied"
    assert company_a.get(f"/employees/{emp['uuid']}").json()["work_mode"] == "remote"


def test_rejected_changes_nothing_and_permissions(company_a):
    hr = _hr(company_a)
    loc = _location(company_a, "Mysuru")
    emp = company_a.add_employee("Farah", date_of_joining="2024-01-01")
    trf = _types(company_a)["TRF"]
    req = hr.post("/hr-processes/requests", json={
        "process_type_uuid": trf["uuid"], "employee_ids": [emp["id"]], "effective_date": date.today().isoformat(),
        "values": {"location_id": loc}, "reason": "Move"}).json()["items"][0]
    t = company_a.get("/workflow/inbox").json()["items"][0]
    company_a.post(f"/workflow/tasks/{t['uuid']}/decision", json={"decision": "reject", "comment": "Not now"})
    assert company_a.get(f"/hr-processes/requests/{req['uuid']}").json()["status"] == "rejected"
    assert company_a.get(f"/employees/{emp['uuid']}").json()["location_id"] is None

    plain = login_as(company_a, emp)
    assert plain.get("/hr-processes/types").status_code == 403
    assert plain.get("/hr-processes/requests").status_code == 403
    assert plain.post(f"/hr-processes/requests/{req['uuid']}/revoke", json={}).status_code == 403
    hr = login_as(company_a, company_a.add_employee("Hema", role="hr"))
    assert hr.get("/hr-processes/types").status_code == 200


def test_tenant_isolation(company_a, company_b):
    loc = _location(company_a, "Delhi")
    emp = company_a.add_employee("Gita", date_of_joining="2024-01-01")
    trf = _types(company_a)["TRF"]
    req = company_a.post("/hr-processes/requests", json={
        "process_type_uuid": trf["uuid"], "employee_ids": [emp["id"]], "effective_date": date.today().isoformat(),
        "values": {"location_id": loc}, "reason": "Move"}).json()["items"][0]
    assert company_b.get(f"/hr-processes/requests/{req['uuid']}").status_code == 404
    assert company_b.get(f"/hr-processes/types/{trf['uuid']}").status_code == 404
    assert company_b.get("/hr-processes/requests").json()["total"] == 0
    b_trf = _types(company_b)["TRF"]
    # company B can't raise a change for company A's employee
    r = company_b.post("/hr-processes/requests", json={
        "process_type_uuid": b_trf["uuid"], "employee_ids": [emp["id"]], "effective_date": date.today().isoformat(),
        "values": {"location_id": loc}, "reason": "x"})
    assert r.status_code == 400
    assert company_b.post(f"/hr-processes/requests/{req['uuid']}/revoke", json={}).status_code == 404
