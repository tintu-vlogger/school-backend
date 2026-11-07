"""Custom fields: definitions per company and value checks on the employee record."""


def _setup(company):
    sec = company.post("/custom-fields/employee/sections", json={"name": "Extra details"}).json()
    f1 = company.post("/custom-fields/employee/fields", json={
        "label": "T-shirt size", "field_type": "single_choice", "options": ["S", "M", "L"], "section_id": sec["id"],
        "required": True}).json()
    f2 = company.post("/custom-fields/employee/fields", json={
        "label": "Blood group", "field_type": "single_choice", "master_type_key": "blood_group", "section_id": sec["id"]})
    assert f2.status_code == 201, f2.text
    f3 = company.post("/custom-fields/employee/fields", json={
        "label": "Years in industry", "field_type": "number", "validation": {"min": 0, "max": 60}}).json()
    return sec, f1, f2.json(), f3


def test_layout_and_values(company_a):
    sec, f1, f2, f3 = _setup(company_a)
    layout = company_a.get("/custom-fields/employee").json()
    assert layout["sections"][0]["name"] == "Extra details"
    assert [f["key"] for f in layout["sections"][0]["fields"]] == ["t_shirt_size", "blood_group"]
    assert layout["unsectioned"][0]["key"] == "years_in_industry"

    bg = company_a.get("/masters/options/blood_group").json()["items"][0]
    emp = company_a.add_employee("Nina", custom={"t_shirt_size": "M", "blood_group": bg["value"], "years_in_industry": 7})
    assert emp["custom"] == {"t_shirt_size": "M", "blood_group": bg["value"], "years_in_industry": 7}

    r = company_a.put(f"/employees/{emp['uuid']}", json={"custom": {"years_in_industry": 99}})
    assert r.status_code == 400 and "at most 60" in r.json()["detail"]
    r = company_a.put(f"/employees/{emp['uuid']}", json={"custom": {"years_in_industry": 8}})
    assert r.json()["custom"]["years_in_industry"] == 8 and r.json()["custom"]["t_shirt_size"] == "M"


def test_required_and_unknown_values(company_a):
    _setup(company_a)
    r = company_a.post("/employees/", json={"first_name": "O", "last_name": "P", "email": "op@example.com",
                                            "password": "Secret123#", "custom": {}})
    assert r.status_code == 400 and "T-shirt size is required" in r.json()["detail"]
    r = company_a.post("/employees/", json={"first_name": "O", "last_name": "P", "email": "op@example.com",
                                            "password": "Secret123#", "custom": {"t_shirt_size": "XXL"}})
    assert r.status_code == 400
    r = company_a.post("/employees/", json={"first_name": "O", "last_name": "P", "email": "op@example.com",
                                            "password": "Secret123#", "custom": {"t_shirt_size": "S", "nope": 1}})
    assert r.status_code == 400 and "Unknown field" in r.json()["detail"]


def test_master_value_from_other_company_refused(company_a, company_b):
    _setup(company_a)
    other = company_b.get("/masters/options/blood_group").json()["items"][0]["value"]
    r = company_a.post("/employees/", json={"first_name": "Q", "last_name": "R", "email": "qr@example.com",
                                            "password": "Secret123#", "custom": {"t_shirt_size": "S", "blood_group": other}})
    assert r.status_code == 400


def test_definitions_are_per_company(company_a, company_b):
    _setup(company_a)
    assert company_b.get("/custom-fields/employee").json()["sections"] == []
