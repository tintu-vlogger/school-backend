"""Sign-up for institutions: institution type, chosen web address and the availability check."""
import uuid


def _register(client, **extra):
    email = f"owner-{uuid.uuid4().hex[:10]}@example.com"
    body = {"email": email, "password": "Secret123#", "full_name": "Asha Nair",
            "company_name": f"Green Valley School {uuid.uuid4().hex[:4]}", **extra}
    return client.post("/api/v1/auth/register", json=body)


def _profile(client, body):
    tok = body["access_token"]
    return client.get("/api/v1/academics/profile",
                      headers={"Authorization": f"Bearer {tok}", "X-Company-Slug": body["user"]["slug"]}).json()


def test_register_saves_institution_type_and_slug(client):
    slug = f"gvs-{uuid.uuid4().hex[:6]}"
    r = _register(client, institution_type="college", slug=slug.upper(), learners="501-2000",
                  country="India", timezone="Asia/Kolkata")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["slug"] == slug
    prof = _profile(client, body)
    assert prof["type"] == "college"
    assert prof["terms"]["program"] == "Programme"


def test_register_without_type_or_slug_still_works(client):
    r = _register(client)
    assert r.status_code == 200, r.text
    assert r.json()["user"]["slug"]
    assert _profile(client, r.json())["type"] == "school"


def test_register_rejects_taken_reserved_and_bad_slugs(client):
    slug = f"taken-{uuid.uuid4().hex[:6]}"
    assert _register(client, slug=slug).status_code == 200
    r = _register(client, slug=slug)
    assert r.status_code == 400 and "taken" in r.json()["detail"]
    assert _register(client, slug="admin").status_code == 400
    assert _register(client, slug="no spaces").status_code == 400
    assert _register(client, institution_type="spaceship").status_code == 400


def test_slug_available(client):
    slug = f"free-{uuid.uuid4().hex[:6]}"
    r = client.get("/api/v1/public/slug-available", params={"slug": slug})
    assert r.status_code == 200
    assert r.json()["available"] is True and r.json()["reason"] is None

    assert _register(client, slug=slug).status_code == 200
    r = client.get("/api/v1/public/slug-available", params={"slug": slug}).json()
    assert r["available"] is False and "taken" in r["reason"]
    assert r["suggestion"] and r["suggestion"] != slug

    r = client.get("/api/v1/public/slug-available", params={"slug": "dashboard"}).json()
    assert r["available"] is False and "reserved" in r["reason"]
    r = client.get("/api/v1/public/slug-available", params={"slug": "-bad-"}).json()
    assert r["available"] is False
    r = client.get("/api/v1/public/slug-available", params={"slug": ""}).json()
    assert r["available"] is False
