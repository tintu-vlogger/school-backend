"""Company website: defaults, saving, validation, permissions, the public page and company separation."""
from tests.conftest import EmployeeSession


def test_defaults_are_complete_and_public(client, company_a):
    r = company_a.get("/company/website")
    assert r.status_code == 200, r.text
    w = r.json()["website"]
    assert w["theme"] == "classic" and w["published"] and w["hero"]["headline"] and w["values"]
    pub = client.get(f"/api/v1/public/companies/{company_a.slug}/site")
    assert pub.status_code == 200 and pub.headers["cache-control"] == "no-cache"
    assert pub.json()["company"]["slug"] == company_a.slug


def test_save_changes_theme_and_content(client, company_a):
    w = company_a.get("/company/website").json()["website"]
    w.update(theme="pulse", accent="#db2777")
    w["hero"]["headline"] = "We build bridges"
    w["leaders"] = [{"name": "Asha Rao", "role": "CEO", "bio": "", "photo_url": None}]
    r = company_a.put("/company/website", json=w)
    assert r.status_code == 200, r.text
    site = client.get(f"/api/v1/public/companies/{company_a.slug}/site").json()["site"]
    assert site["theme"] == "pulse" and site["hero"]["headline"] == "We build bridges" and site["leaders"][0]["name"] == "Asha Rao"


def test_rejects_bad_values(company_a):
    w = company_a.get("/company/website").json()["website"]
    assert company_a.put("/company/website", json={**w, "theme": "neon"}).status_code == 422
    assert company_a.put("/company/website", json={**w, "accent": "red"}).status_code == 422
    bad = {**w, "hero": {**w["hero"], "image_url": "javascript:alert(1)"}}
    assert company_a.put("/company/website", json=bad).status_code == 422


def test_unpublished_site_hides_content(client, company_a):
    w = company_a.get("/company/website").json()["website"]
    company_a.put("/company/website", json={**w, "published": False})
    body = client.get(f"/api/v1/public/companies/{company_a.slug}/site").json()
    assert body["site"]["published"] is False and "about" not in body["site"] and body["jobs"] == []


def test_reset_goes_back_to_defaults(company_a):
    w = company_a.get("/company/website").json()["website"]
    company_a.put("/company/website", json={**w, "theme": "serene"})
    assert company_a.post("/company/website/reset").json()["website"]["theme"] == "classic"


def test_employee_cannot_change_website(company_a):
    emp = EmployeeSession(company_a, company_a.add_employee("Nila"))
    w = company_a.get("/company/website").json()["website"]
    r = emp.client.put("/api/v1/company/website", json=w, headers=emp.headers)
    assert r.status_code == 403


def test_companies_have_separate_websites(client, company_a, company_b):
    w = company_a.get("/company/website").json()["website"]
    w["hero"]["headline"] = "Alpha only"
    company_a.put("/company/website", json=w)
    other = client.get(f"/api/v1/public/companies/{company_b.slug}/site").json()["site"]
    assert other["hero"]["headline"] != "Alpha only"
    assert client.get("/api/v1/public/companies/no-such-company-xyz/site").status_code == 404
