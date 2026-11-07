"""Roles and access: the owner's own employee record, custom roles with several roles per
employee, data scopes, sensitive-field visibility and tenant isolation of assignments."""
from tests.conftest import login_as


def _roles(c):
    r = c.get("/roles/")
    assert r.status_code == 200, r.text
    return {x["role_name"]: x for x in r.json()}


def test_owner_has_employee_record_and_admin_rights(company_a):
    me = company_a.get("/auth/me").json()
    assert me["role"] == "admin" and me["is_owner"] is True
    assert me["employee_id"] and me["employee_uuid"]
    # self-service works for the owner
    for path in ("/payroll/me/summary", "/attendance/me/requests?page=1&size=10"):
        assert company_a.get(path).status_code == 200, path
    perms = company_a.get("/roles/my-permissions").json()
    assert perms["is_owner"] and "settings.roles.manage" in perms["permissions"]
    # the owner record is listed as an employee with the HR admin role
    people = company_a.get("/roles/assignments/people?size=50").json()["items"]
    owner = next(p for p in people if p["is_owner"])
    assert owner["base_role"] == "admin"
    # and the owner can't lose HR admin
    employee_role = _roles(company_a)["employee"]["id"]
    r = company_a.put(f"/roles/assignments/{owner['id']}", json={"role_ids": [employee_role]})
    assert r.status_code == 400


def test_owner_record_is_created_once(company_a):
    from core.db import system_session
    from modules.company.owner import ensure_owner_employee
    db = system_session()
    try:
        first = ensure_owner_employee(db, company_a.id).id
        second = ensure_owner_employee(db, company_a.id).id
        db.commit()
    finally:
        db.close()
    assert first == second == company_a.get("/auth/me").json()["employee_id"]


def test_custom_role_clone_assign_and_delete(company_a):
    emp = company_a.add_employee("Neha", "Joshi")
    s = login_as(company_a, emp)
    assert s.get("/payroll/settings").status_code == 403

    r = company_a.post("/roles/", json={"display_name": "Payroll clerk", "permissions": ["payroll.view"],
                                        "field_access": {"salary": "full"}})
    assert r.status_code == 201, r.text
    role = r.json()
    assert role["field_access"]["salary"] == "full" and role["field_access"]["bank"] == "hidden"
    clone = company_a.post(f"/roles/{role['id']}/clone", json={}).json()
    assert clone["display_name"] == "Payroll clerk (copy)" and clone["permissions"] == ["payroll.view"]

    roles = _roles(company_a)
    r = company_a.put(f"/roles/assignments/{emp['id']}", json={"role_ids": [roles["employee"]["id"], role["id"]]})
    assert r.status_code == 200, r.text
    assert "Payroll clerk" in r.json()["added"]
    s = login_as(company_a, emp)
    assert s.get("/payroll/settings").status_code == 200
    mine = s.get("/roles/my-permissions").json()
    assert "payroll.view" in mine["permissions"] and set(mine["roles"]) == {"employee", role["role_name"]}

    # bulk remove, then the permission is gone
    r = company_a.post("/roles/assignments/bulk", json={"employee_ids": [emp["id"]], "role_id": role["id"], "action": "remove"})
    assert r.json()["changed"] == 1
    assert login_as(company_a, emp).get("/payroll/settings").status_code == 403

    # system roles stay; custom ones go
    assert company_a.delete(f"/roles/{roles['manager']['id']}").status_code == 400
    assert company_a.delete(f"/roles/{clone['id']}").status_code == 200
    assert company_a.put(f"/roles/{roles['admin']['id']}", json={"permissions": []}).status_code == 400
    # assignment changes are audited
    log = company_a.get("/audit/?page=1&size=50").json()
    assert any(e.get("action") == "roles.assigned" for e in log["items"])


def test_only_role_managers_change_roles(company_a):
    emp = company_a.add_employee("Kiran", "Rao")
    s = login_as(company_a, emp)
    assert s.get("/roles/").status_code == 403
    assert s.post("/roles/", json={"display_name": "Sneaky", "permissions": ["payroll.view"]}).status_code == 403


def test_scope_limits_employee_list(company_a):
    boss = company_a.add_employee("Asha", "Iyer", role="manager")
    company_a.add_employee("Ravi", "Shah", reporting_manager_id=boss["id"])
    company_a.add_employee("Other", "Person")
    role = company_a.post("/roles/", json={"display_name": "Team viewer", "permissions": ["people.employees.view"],
                                           "scopes": {"people.employees.view": "direct_reports"}}).json()
    roles = _roles(company_a)
    company_a.put(f"/roles/assignments/{boss['id']}", json={"role_ids": [roles["employee"]["id"], role["id"]]})
    names = {e["first_name"] for e in login_as(company_a, boss).get("/employees/").json()}
    assert names == {"Asha", "Ravi"}


def test_field_access_masks_pan(company_a):
    target = company_a.add_employee("Pan", "Holder", pan="ABCPE1234F")
    viewer = company_a.add_employee("Hr", "Viewer")
    roles = _roles(company_a)
    company_a.put(f"/roles/assignments/{viewer['id']}", json={"role_ids": [roles["hr"]["id"]]})
    s = login_as(company_a, viewer)
    assert s.get(f"/employees/{target['uuid']}/profile").json()["pan"] == "ABCPE1234F"
    company_a.put(f"/roles/{roles['hr']['id']}", json={"field_access": {"pan": "hidden"}})
    assert login_as(company_a, viewer).get(f"/employees/{target['uuid']}/profile").json()["pan"] is None


def test_assignments_are_isolated_between_companies(company_a, company_b):
    emp_b = company_b.add_employee("Bea", "Bee")
    role_a = company_a.post("/roles/", json={"display_name": "A only", "permissions": []}).json()
    assert company_a.put(f"/roles/assignments/{emp_b['id']}", json={"role_ids": [role_a["id"]]}).status_code == 404
    assert company_b.get(f"/roles/{role_a['id']}").status_code == 404
