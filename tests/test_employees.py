"""Employee timezone, reporting manager and pagination."""


def test_me_returns_effective_timezone(company_a):
    me = company_a.get("/auth/me").json()
    assert me["timezone"] == "Asia/Kolkata" and me["timezone_source"] == "company"
    assert company_a.put("/auth/me/timezone", json={"timezone": "Europe/London"}).status_code == 200
    me = company_a.get("/auth/me").json()
    assert me["timezone"] == "Europe/London" and me["timezone_source"] == "own"
    assert company_a.put("/auth/me/timezone", json={"timezone": "Mars/Base"}).status_code == 400


def test_employee_timezone_and_manager(company_a):
    boss = company_a.add_employee("Hari")
    emp = company_a.add_employee("Indu", reporting_manager_id=boss["id"], timezone="Asia/Dubai")
    assert emp["reporting_manager_name"].startswith("Hari") and emp["timezone"] == "Asia/Dubai"
    # loops are refused
    r = company_a.put(f"/employees/{boss['uuid']}", json={"reporting_manager_id": emp["id"]})
    assert r.status_code == 400 and "loop" in r.json()["detail"]
    assert company_a.put(f"/employees/{boss['uuid']}", json={"timezone": "Nowhere/City"}).status_code == 400


def test_manager_from_other_company_is_refused(company_a, company_b):
    other = company_b.add_employee("Jaya")
    r = company_a.post("/employees/", json={"first_name": "K", "last_name": "L", "email": "kl@example.com",
                                            "password": "Secret123#", "reporting_manager_id": other["id"]})
    assert r.status_code == 400


def test_paginated_list_and_lookup(company_a):
    for name in ("Mohan", "Mohit", "Meera"):
        company_a.add_employee(name)
    page = company_a.get("/employees/", params={"page": 1, "size": 2, "q": "Mo"}).json()
    assert page["total"] == 2 and len(page["items"]) == 2 and page["size"] == 2
    options = company_a.get("/employees/lookup", params={"q": "Mee"}).json()
    assert [o["label"].split()[0] for o in options] == ["Meera"]
