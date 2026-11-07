"""Institution website: school defaults from the profile, programmes offered, notices and admissions."""
from tests.conftest import school_setup


def test_public_site_has_school_defaults_and_programs(client, company_a):
    company_a.put("/academics/profile", json={"motto": "Learn, lead, serve", "principal_name": "Dr. Meera Iyer",
                                              "board": "CBSE"})
    school_setup(company_a)
    body = client.get(f"/api/v1/public/companies/{company_a.slug}/site").json()
    site = body["site"]
    assert site["hero"]["headline"] == "Learn, lead, serve"
    assert "CBSE" in site["hero"]["eyebrow"]
    assert site["leaders"][0]["name"] == "Dr. Meera Iyer" and site["leaders"][0]["role"] == "Principal"
    assert site["values"] and site["life"]["perks"]
    # school sections on, staff vacancies off by default
    assert site["sections"]["programs"] and site["sections"]["admissions"] and site["sections"]["notices"]
    assert site["sections"]["jobs"] is False and body["jobs"] == []
    names = [p["name"] for p in body["programs"]]
    assert names[:2] == ["Grade 1", "Grade 2"] and body["programs"][0]["sections"] == 2
    inst = body["institution"]
    assert inst["type"] == "school" and inst["terms"]["programs"] == "Classes" and inst["admissions_online"] is True


def test_save_notices_admissions_and_old_configs_still_load(client, company_a):
    w = company_a.get("/company/website").json()["website"]
    w["notices"] = [{"title": "Annual day", "date": "2026-12-12", "body": "All parents are welcome.", "kind": "event"}]
    w["admissions"] = {"title": "Admissions 2027-28", "body": "Apply online.", "open": False, "button_label": "Apply"}
    w["programs"] = {"title": "Our classes", "body": "Nursery to Grade 12."}
    w["sections"]["programs"] = False
    r = company_a.put("/company/website", json=w)
    assert r.status_code == 200, r.text
    body = client.get(f"/api/v1/public/companies/{company_a.slug}/site").json()
    site = body["site"]
    assert site["notices"][0]["title"] == "Annual day" and site["notices"][0]["kind"] == "event"
    assert site["admissions"]["open"] is False and site["programs"]["title"] == "Our classes"
    assert body["programs"] == []  # section switched off

    # A site saved by the first version (no programs / notices / admissions keys) keeps loading.
    old = {k: v for k, v in w.items() if k not in ("programs", "notices", "admissions")}
    old["sections"] = {k: True for k in ("about", "values", "life", "leaders", "jobs", "offices", "contact")}
    assert company_a.put("/company/website", json=old).status_code == 200
    site = client.get(f"/api/v1/public/companies/{company_a.slug}/site").json()["site"]
    assert site["sections"]["programs"] is True and site["notices"] == [] and site["admissions"]["open"] is True


def test_stored_first_version_config_merges_with_school_defaults():
    from types import SimpleNamespace
    from modules.website.router import current
    old = {"theme": "modern", "hero": {"eyebrow": "", "headline": "Old headline", "subline": "", "image_url": None},
           "sections": {k: True for k in ("about", "values", "life", "leaders", "jobs", "offices", "contact")}}
    comp = SimpleNamespace(company_name="Green Valley School", city="Kochi", industry=None, address=None, phone=None,
                           email="office@gv.example", state=None, pincode=None, employee_count_band=None,
                           config={"website": old, "institution": {"type": "school"}})
    site = current(comp)
    assert site["theme"] == "modern" and site["hero"]["headline"] == "Old headline"
    assert site["admissions"]["title"] and site["programs"]["title"] == "Classes we offer"
    assert site["sections"]["programs"] and site["sections"]["notices"] and site["sections"]["jobs"]


def test_bad_notice_kind_rejected(company_a):
    w = company_a.get("/company/website").json()["website"]
    w["notices"] = [{"title": "x", "date": "", "body": "", "kind": "party"}]
    assert company_a.put("/company/website", json=w).status_code == 422
