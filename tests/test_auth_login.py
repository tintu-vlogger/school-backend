"""Signing in on the main site: owners and employees both work; the company is found for them."""


def _login(client, email, password, slug=None):
    headers = {"X-Company-Slug": slug} if slug else {}
    return client.post("/api/v1/auth/login", json={"email": email, "password": password}, headers=headers)


def test_employee_can_sign_in_on_main_site(client, company_a):
    emp = company_a.add_employee("Lata")
    r = _login(client, emp["email"], "Secret123#")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["slug"] == company_a.slug and body["user"]["company_id"] == company_a.id


def test_wrong_password_on_main_site_is_refused(client, company_a):
    emp = company_a.add_employee("Mohan")
    assert _login(client, emp["email"], "wrong-pass").status_code == 401


def test_same_email_in_two_companies_asks_for_company_address(client, company_a, company_b):
    email = "shared-person@example.com"
    for c in (company_a, company_b):
        r = c.post("/employees/", json={"first_name": "Shared", "last_name": "Person", "email": email, "password": "Secret123#"})
        assert r.status_code == 201, r.text
    r = _login(client, email, "Secret123#")
    assert r.status_code == 409 and "more than one company" in r.json()["detail"]
    # on the company's own address it works
    assert _login(client, email, "Secret123#", slug=company_a.slug).status_code == 200
