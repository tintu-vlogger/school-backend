"""Custom fields on departments, designations, locations, legal entities, assets, help desk
cases and candidates: values are checked on every record type, required fields are enforced
on admin forms, definitions and values stay inside one company, and ordering is saved."""
import uuid as uuidlib

import pytest

from tests.conftest import Company, login_as


@pytest.fixture
def company_a(client):
    return Company(client, f"Cf A {uuidlib.uuid4().hex[:8]}")


@pytest.fixture
def company_b(client):
    return Company(client, f"Cf B {uuidlib.uuid4().hex[:8]}")


def _field(company, entity, label, **extra):
    r = company.post(f"/custom-fields/{entity}/fields", json={"label": label, "field_type": "text", **extra})
    assert r.status_code == 201, r.text
    return r.json()


def _state(company, label="Karnataka"):
    items = company.get("/masters/options/state", params={"q": label}).json()["items"]
    return next(i["value"] for i in items if i["label"] == label)


def test_new_record_types_are_listed(company_a):
    entities = {e["value"] for e in company_a.get("/custom-fields/meta").json()["entities"]}
    assert {"employee", "department", "designation", "location", "legal_entity", "asset", "helpdesk_case",
            "candidate"} <= entities
    assert company_a.get("/custom-fields/payslip").status_code == 400


def test_department_and_designation_values(company_a):
    _field(company_a, "department", "Budget owner", required=True)
    _field(company_a, "department", "Floor", field_type="number", validation={"min": 0, "max": 50})
    r = company_a.post("/departments/", json={"department_name": "Finance", "custom": {}})
    assert r.status_code == 400 and "Budget owner is required" in r.json()["detail"]
    r = company_a.post("/departments/", json={"department_name": "Finance", "custom": {"budget_owner": "CFO", "floor": 3}})
    assert r.status_code == 201, r.text
    dept = r.json()
    assert dept["custom"] == {"budget_owner": "CFO", "floor": 3}
    r = company_a.put(f"/departments/{dept['uuid']}", json={"custom": {"floor": 99}})
    assert r.status_code == 400 and "at most 50" in r.json()["detail"]
    r = company_a.put(f"/departments/{dept['uuid']}", json={"custom": {"floor": 4}})
    assert r.json()["custom"] == {"budget_owner": "CFO", "floor": 4}
    r = company_a.put(f"/departments/{dept['uuid']}", json={"custom": {"nope": 1}})
    assert r.status_code == 400 and "Unknown field" in r.json()["detail"]

    _field(company_a, "designation", "Job family", field_type="single_choice", options=["Tech", "Sales"])
    r = company_a.post("/designations/", json={"designation_name": "Engineer II", "custom": {"job_family": "Ops"}})
    assert r.status_code == 400
    r = company_a.post("/designations/", json={"designation_name": "Engineer II", "custom": {"job_family": "Tech"}})
    assert r.status_code == 201 and r.json()["custom"] == {"job_family": "Tech"}
    r = company_a.put(f"/designations/{r.json()['uuid']}", json={"custom": {"job_family": "Sales"}})
    assert r.json()["custom"] == {"job_family": "Sales"}


def test_location_and_legal_entity_values(company_a):
    _field(company_a, "location", "Site manager phone", field_type="phone")
    ka = _state(company_a)
    r = company_a.post("/organization/locations", json={"name": "Mysuru", "state_id": ka,
                                                       "custom": {"site_manager_phone": "abc"}})
    assert r.status_code == 400 and "phone number" in r.json()["detail"]
    r = company_a.post("/organization/locations", json={"name": "Mysuru", "state_id": ka,
                                                       "custom": {"site_manager_phone": "+91 98450 12345"}})
    assert r.status_code == 201, r.text
    loc = r.json()
    assert loc["custom"]["site_manager_phone"] == "+91 98450 12345"
    got = company_a.get(f"/organization/locations/{loc['uuid']}").json()
    assert got["custom"]["site_manager_phone"] == "+91 98450 12345"

    _field(company_a, "legal_entity", "MSME number", required=True)
    r = company_a.post("/organization/legal-entities", json={"name": "Alpha Two"})
    assert r.status_code == 400 and "MSME number is required" in r.json()["detail"]
    r = company_a.post("/organization/legal-entities", json={"name": "Alpha Two", "custom": {"msme_number": "UDYAM-KR-01"}})
    assert r.status_code == 201, r.text
    ent = r.json()
    r = company_a.put(f"/organization/legal-entities/{ent['uuid']}", json={"city": "Mysuru"})
    assert r.status_code == 200, r.text  # leaving custom out keeps the values
    assert company_a.get(f"/organization/legal-entities/{ent['uuid']}").json()["custom"] == {"msme_number": "UDYAM-KR-01"}


def test_asset_case_and_candidate_values(company_a):
    _field(company_a, "asset", "RAM (GB)", field_type="number", required=True)
    r = company_a.post("/assets", json={"name": "ThinkPad"})
    assert r.status_code == 400 and "RAM (GB) is required" in r.json()["detail"]
    r = company_a.post("/assets", json={"name": "ThinkPad", "custom": {"ram_gb": 16}})
    assert r.status_code == 201, r.text
    asset = r.json()
    assert asset["custom"] == {"ram_gb": 16}
    r = company_a.put(f"/assets/{asset['uuid']}", json={"name": "ThinkPad", "custom": {"ram_gb": "lots"}})
    assert r.status_code == 400

    _field(company_a, "helpdesk_case", "Laptop serial")
    emp = company_a.add_employee("Kiran")
    me = login_as(company_a, emp)
    cat = next(c for c in company_a.get("/helpdesk/categories").json()["items"] if c["parent_id"] is None)
    r = me.post("/helpdesk/cases", json={"category_id": cat["id"], "subject": "Laptop slow", "description": "Very slow",
                                         "custom": {"laptop_serial": "SN-9"}})
    assert r.status_code == 201, r.text
    assert r.json()["custom"] == {"laptop_serial": "SN-9"}
    r = me.post("/helpdesk/cases", json={"category_id": cat["id"], "subject": "x", "description": "y",
                                         "custom": {"bogus": 1}})
    assert r.status_code == 400

    _field(company_a, "candidate", "Notice negotiable", field_type="boolean")
    r = company_a.post("/recruitment/candidates", json={"first_name": "Zara", "last_name": "Khan",
                                                       "email": f"zara-{uuidlib.uuid4().hex[:6]}@example.com",
                                                       "custom": {"notice_negotiable": "yes"}})
    assert r.status_code == 201, r.text
    cand = r.json()
    assert cand["custom"] == {"notice_negotiable": True}
    r = company_a.put(f"/recruitment/candidates/{cand['uuid']}", json={"custom": {"notice_negotiable": False}})
    assert r.status_code == 200 and r.json()["custom"] == {"notice_negotiable": False}


def test_definitions_and_values_are_per_company(company_a, company_b):
    _field(company_a, "department", "Budget owner")
    assert company_b.get("/custom-fields/department").json()["unsectioned"] == []
    # company B does not know company A's field
    r = company_b.post("/departments/", json={"department_name": "Ops", "custom": {"budget_owner": "x"}})
    assert r.status_code == 400 and "Unknown field" in r.json()["detail"]
    a_field = company_a.get("/custom-fields/department").json()["unsectioned"][0]
    assert company_b.put(f"/custom-fields/fields/{a_field['uuid']}", json={"label": "Hacked"}).status_code == 404


def test_reorder_fields_and_sections(company_a):
    f1 = _field(company_a, "asset", "First")
    f2 = _field(company_a, "asset", "Second")
    f3 = _field(company_a, "asset", "Third")
    r = company_a.post("/custom-fields/asset/fields/reorder", json={"ids": [f3["id"], f1["id"], f2["id"]]})
    assert r.status_code == 200
    keys = [f["key"] for f in company_a.get("/custom-fields/asset").json()["unsectioned"]]
    assert keys == ["third", "first", "second"]
    s1 = company_a.post("/custom-fields/asset/sections", json={"name": "Hardware"}).json()
    s2 = company_a.post("/custom-fields/asset/sections", json={"name": "Purchase"}).json()
    company_a.post("/custom-fields/asset/sections/reorder", json={"ids": [s2["id"], s1["id"]]})
    names = [s["name"] for s in company_a.get("/custom-fields/asset").json()["sections"]]
    assert names == ["Purchase", "Hardware"]
