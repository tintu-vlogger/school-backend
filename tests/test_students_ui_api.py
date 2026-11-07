"""Endpoints added for the academics and students screens: directory filters and export,
ID cards, bulk portal sign-ins for a section, the portal summary card, documents stored as
uploaded files, and the teacher workload view. Portal access: a parent never opens another
family's child."""
import io

import pytest


def _setup(company):
    r = company.post("/academics/quick-setup", json={
        "institution_type": "school", "start_month": 4, "programs": ["Grade 1", "Grade 2"],
        "sections": ["A"], "subjects": ["English", "Mathematics"],
    })
    assert r.status_code == 200, r.text
    sections = company.get("/academics/section-options").json()
    return {s["label"]: s["id"] for s in sections}


def _student(company, first, section_id, guardian_email=None, guardian_phone=None, house_id=None, **extra):
    r = company.post("/students", json={
        "first_name": first, "last_name": "Test", "section_id": section_id, "house_id": house_id,
        "blood_group": "B+",
        "guardians": [{"first_name": f"{first}Dad", "last_name": "Test", "relation": "father", "is_primary": True,
                       "email": guardian_email, "phone": guardian_phone}],
        **extra,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _portal_login(company, login, password):
    r = company.client.post("/api/v1/auth/login", json={"email": login, "password": password},
                            headers={"X-Company-Slug": company.slug})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    return {"Authorization": f"Bearer {token}", "X-Company-Slug": company.slug}


def test_section_portal_accounts_created_once(company_a):
    secs = _setup(company_a)
    sid = secs["Grade 1 - A"]
    _student(company_a, "Meera", sid, guardian_email="meera.dad@example.com")
    _student(company_a, "Rohan", sid, guardian_phone="9800000001")
    r = company_a.post(f"/students/section/{sid}/portal-accounts")
    assert r.status_code == 200, r.text
    logins = r.json()["new_logins"]
    kinds = sorted(x["kind"] for x in logins)
    assert kinds == ["parent", "parent", "student", "student"]
    assert all(x["password"] for x in logins)
    assert {x["admission_no"] for x in logins}
    # Running it again creates nothing new (passwords are shown only once).
    again = company_a.post(f"/students/section/{sid}/portal-accounts").json()
    assert again["new_logins"] == []
    # They show up in the sign-ins list with the children of each parent.
    lst = company_a.get("/portal-accounts?kind=parent").json()
    assert lst["total"] == 2
    assert any("Meera Test" in a["children"] for a in lst["items"])
    off = company_a.get("/portal-accounts?active=false").json()
    assert off["total"] == 0


def test_parent_cannot_open_another_familys_child(company_a):
    secs = _setup(company_a)
    sid = secs["Grade 2 - A"]
    mine = _student(company_a, "Kabir", sid, guardian_email="kabir.parent@example.com")
    other = _student(company_a, "Zara", sid, guardian_email="zara.parent@example.com")
    r = company_a.post(f"/students/{mine['uuid']}/portal-accounts").json()
    parent = next(x for x in r["new_logins"] if x["kind"] == "parent")
    h = _portal_login(company_a, parent["login"], parent["password"])
    c = company_a.client

    kids = c.get("/api/v1/students/mine", headers=h).json()
    assert [k["uuid"] for k in kids] == [mine["uuid"]]
    # Own child: profile, summary card and documents open.
    own = c.get(f"/api/v1/students/{mine['uuid']}", headers=h)
    assert own.status_code == 200
    assert own.json()["guardians"][0]["name"] == "KabirDad Test"
    assert "portal_account" not in own.json()["guardians"][0]  # sign-in details of guardians are staff only
    summary = c.get(f"/api/v1/students/portal/summary?student_id={mine['id']}", headers=h)
    assert summary.status_code == 200 and summary.json()["section_label"] == "Grade 2 - A"
    assert c.get(f"/api/v1/students/{mine['uuid']}/documents", headers=h).status_code == 200
    # Another family's child: not found everywhere.
    assert c.get(f"/api/v1/students/{other['uuid']}", headers=h).status_code == 404
    assert c.get(f"/api/v1/students/portal/summary?student_id={other['id']}", headers=h).status_code == 404
    assert c.get(f"/api/v1/students/{other['uuid']}/documents", headers=h).status_code == 404
    # Staff-only lists stay closed to portal sign-ins.
    assert c.get("/api/v1/students", headers=h).status_code == 403
    assert c.get(f"/api/v1/students/section/{sid}/id-cards", headers=h).status_code in (403, 404)
    assert c.post(f"/api/v1/students/section/{sid}/portal-accounts", headers=h).status_code in (403, 404)


def test_directory_filters_export_and_id_cards(company_a):
    secs = _setup(company_a)
    sid = secs["Grade 1 - A"]
    house = company_a.get("/academics/houses?all=true").json()["items"][0]
    _student(company_a, "Anu", sid, guardian_phone="9811111111", house_id=house["id"])
    _student(company_a, "Bala", sid, guardian_phone="9822222222")
    lst = company_a.get(f"/students?house_id={house['id']}").json()
    assert lst["total"] == 1
    row = lst["items"][0]
    assert row["guardian_phone"] == "9811111111" and row["house_name"] == house["name"]

    r = company_a.get(f"/students/export?section_id={sid}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    text = r.text.splitlines()
    assert text[0].startswith("Admission no,Name")
    assert len(text) == 3

    cards = company_a.get(f"/students/section/{sid}/id-cards").json()
    assert cards["section"]["label"] == "Grade 1 - A"
    assert [c["name"] for c in cards["students"]] == ["Anu Test", "Bala Test"]
    assert cards["students"][0]["blood_group"] == "B+"
    assert cards["students"][0]["guardian_phone"] == "9811111111"


def test_documents_from_uploaded_files_get_signed_links(company_a):
    secs = _setup(company_a)
    s = _student(company_a, "Diya", secs["Grade 1 - A"])
    up = company_a.client.post("/api/v1/documents/uploads", headers=company_a.headers,
                               files={"file": ("birth.pdf", io.BytesIO(b"%PDF-1.4 test"), "application/pdf")})
    assert up.status_code == 201, up.text
    r = company_a.post(f"/students/{s['uuid']}/documents", json={"title": "Birth certificate",
                                                                 "upload_uuid": up.json()["uuid"]})
    assert r.status_code == 201, r.text
    docs = company_a.get(f"/students/{s['uuid']}/documents").json()
    assert docs[0]["file_url"].startswith("/api/v1/documents/blob/")
    assert docs[0]["file_name"] == "birth.pdf"
    # Missing file or nothing attached is refused.
    bad = company_a.post(f"/students/{s['uuid']}/documents", json={"title": "X"})
    assert bad.status_code == 400


def test_teacher_workload_and_detail_fields(company_a):
    secs = _setup(company_a)
    sid = secs["Grade 1 - A"]
    t = company_a.add_employee("Lata", "Iyer")
    alloc = company_a.get(f"/academics/allocations?section_id={sid}&all=true").json()["items"]
    if not alloc:
        sec_uuid = next(s for s in company_a.get("/academics/section-options").json() if s["id"] == sid)["uuid"]
        company_a.post(f"/academics/sections/{sec_uuid}/apply-curriculum")
        alloc = company_a.get(f"/academics/allocations?section_id={sid}&all=true").json()["items"]
    assert alloc
    for a in alloc:
        r = company_a.put(f"/academics/allocations/{a['uuid']}", json={"teacher_id": t["id"], "weekly_periods": 6})
        assert r.status_code == 200, r.text
    sec_uuid = next(s for s in company_a.get("/academics/section-options").json() if s["id"] == sid)["uuid"]
    company_a.put(f"/academics/sections/{sec_uuid}", json={"class_teacher_id": t["id"]})
    w = company_a.get("/academics/workload").json()
    me = next(i for i in w["items"] if i["teacher_id"] == t["id"])
    assert me["periods"] == 6 * len(alloc)
    assert me["class_teacher_of"] == ["Grade 1 - A"]

    s = _student(company_a, "Ishan", sid)
    full = company_a.get(f"/students/{s['uuid']}").json()
    assert full["class_teacher_name"] == "Lata Iyer"
    assert "portal_account" in full["guardians"][0]


def test_other_institution_cannot_use_my_section(company_a, company_b):
    secs = _setup(company_a)
    sid = secs["Grade 1 - A"]
    _student(company_a, "Neel", sid, guardian_email="neel.parent@example.com")
    assert company_b.post(f"/students/section/{sid}/portal-accounts").status_code == 400
    assert company_b.get(f"/students/section/{sid}/id-cards").status_code == 400
    assert company_b.get(f"/students?section_id={sid}").json()["total"] == 0
    assert company_b.get("/students/export").text.count("\n") == 1
