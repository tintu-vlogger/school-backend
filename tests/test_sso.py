"""Google and Microsoft sign-in with the provider calls replaced (no network).

Covers: platform set-up, company switches and domains, verified-email matching of owners and
employees on a company address and on the main site, refusals (not enabled, other domain,
unknown person, unverified email, guest), the signed state + cookie, and the one-time code.
"""
import uuid
from urllib.parse import parse_qs, urlparse

import pytest

from modules.sso import providers, router as sso_router
from modules.sso.providers import Identity, SsoError

API = "/api/v1"


@pytest.fixture(scope="module")
def platform_headers(client):
    from core.db import system_session
    from core.security import hash_password
    from modules.platform.models import PlatformUser
    from modules.platform.security import create_platform_token
    db = system_session()
    try:
        u = PlatformUser(email=f"sso-admin-{uuid.uuid4().hex[:6]}@platform.test", password=hash_password("Secret123#x"),
                         name="SSO Admin", role="admin", is_active=True, token_version=0)
        db.add(u)
        db.commit()
        db.refresh(u)
        return {"Authorization": f"Bearer {create_platform_token(u)}"}
    finally:
        db.close()


@pytest.fixture(scope="module")
def providers_ready(client, platform_headers):
    r = client.put(f"{API}/platform/admin/sso/google", headers=platform_headers,
                   json={"enabled": True, "client_id": "g-client.apps.googleusercontent.com", "client_secret": "g-secret"})
    assert r.status_code == 200, r.text
    r = client.put(f"{API}/platform/admin/sso/microsoft", headers=platform_headers,
                   json={"enabled": True, "client_id": "11111111-2222-3333-4444-555555555555", "client_secret": "m-secret",
                         "tenant": "organizations"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["google"]["ready"] and body["microsoft"]["ready"]
    assert body["google"]["has_secret"] and "client_secret" not in body["google"]
    return body


@pytest.fixture
def fake_identity(monkeypatch):
    """Replace the provider call: whatever is put in `who` is returned."""
    who = {}

    def fake(provider, code, redirect_uri, client_id, client_secret, tenant=None):
        if who.get("error"):
            raise SsoError(who["error"])
        return Identity(provider, "sub-1", who["email"].lower(), who.get("verified", True), "Test Person")

    monkeypatch.setattr(providers, "fetch_identity", fake)
    return who


def _enable(company, **body):
    r = company.put("/sso/settings", json={"google_enabled": True, "microsoft_enabled": True, **body})
    assert r.status_code == 200, r.text
    return r.json()


def _sign_in(client, provider, slug=None):
    """Run start -> provider -> callback. Returns the final redirect address (parsed)."""
    client.cookies.clear()
    r = client.get(f"{API}/auth/sso/{provider}/start", params={"slug": slug} if slug else {}, follow_redirects=False)
    assert r.status_code == 302, r.text
    loc = urlparse(r.headers["location"])
    if loc.path.endswith("/login"):
        return loc
    state = parse_qs(loc.query)["state"][0]
    r = client.get(f"{API}/auth/sso/{provider}/callback", params={"code": "abc", "state": state}, follow_redirects=False)
    assert r.status_code == 302, r.text
    return urlparse(r.headers["location"])


def _exchange(client, loc):
    code = parse_qs(loc.query)["code"][0]
    r = client.post(f"{API}/auth/sso/exchange", json={"code": code})
    assert r.status_code == 200, r.text
    return r.json()


def test_platform_settings_need_platform_admin(client):
    assert client.get(f"{API}/platform/admin/sso").status_code == 401


def test_switching_on_needs_platform_setup_and_valid_domains(client, company_a, providers_ready):
    out = _enable(company_a, allowed_domains=["@AlphaWorks.in", "alphaworks.in"])
    assert out["allowed_domains"] == ["alphaworks.in"]
    assert out["providers"]["google"]["enabled"] and out["providers"]["microsoft"]["enabled"]
    r = company_a.put("/sso/settings", json={"allowed_domains": ["not a domain"]})
    assert r.status_code == 400
    r = client.get(f"{API}/auth/sso/options", headers={"X-Company-Slug": company_a.slug})
    assert r.json() == {"google": True, "microsoft": True}


def test_employee_signs_in_on_company_address(client, company_a, providers_ready, fake_identity):
    _enable(company_a, allowed_domains=[])
    emp = company_a.add_employee("Sona")
    fake_identity["email"] = emp["email"].upper()  # matching ignores case
    loc = _sign_in(client, "google", company_a.slug)
    assert loc.path == "/auth/callback" and loc.hostname.startswith(company_a.slug + ".")
    body = _exchange(client, loc)
    assert body["user"]["company_id"] == company_a.id and body["user"]["employee_uuid"] == emp["uuid"]
    me = client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {body['access_token']}", "X-Company-Slug": company_a.slug})
    assert me.status_code == 200, me.text
    # the code works once only (when Redis is there) and never after tampering
    r = client.post(f"{API}/auth/sso/exchange", json={"code": parse_qs(loc.query)["code"][0][:-3] + "abc"})
    assert r.status_code == 400


def test_owner_signs_in_with_microsoft_from_main_site(client, company_b, providers_ready, fake_identity):
    _enable(company_b)
    fake_identity["email"] = company_b.email
    loc = _sign_in(client, "microsoft")
    assert loc.path == "/auth/callback" and loc.hostname.startswith(company_b.slug + ".")
    body = _exchange(client, loc)
    assert body["user"]["is_owner"] and body["user"]["company_id"] == company_b.id


def test_main_site_finds_the_employees_company(client, company_a, providers_ready, fake_identity):
    _enable(company_a)
    emp = company_a.add_employee("Ira")
    fake_identity["email"] = emp["email"]
    loc = _sign_in(client, "google")
    assert loc.hostname.startswith(company_a.slug + ".")
    assert _exchange(client, loc)["user"]["company_id"] == company_a.id


def test_same_email_in_two_companies_asks_for_company_address(client, company_a, company_b, providers_ready, fake_identity):
    email = f"two-{uuid.uuid4().hex[:6]}@example.com"
    for c in (company_a, company_b):
        _enable(c)
        assert c.post("/employees/", json={"first_name": "Two", "last_name": "Places", "email": email,
                                           "password": "Secret123#"}).status_code == 201
    fake_identity["email"] = email
    loc = _sign_in(client, "google")
    q = parse_qs(loc.query)
    assert loc.path == "/login" and q["sso_error"] == ["many_companies"]
    # from one company's address it works
    loc = _sign_in(client, "google", company_a.slug)
    assert _exchange(client, loc)["user"]["company_id"] == company_a.id


def test_refusals(client, company_a, company_b, providers_ready, fake_identity):
    emp = company_a.add_employee("Ravi")
    # switched off at the company
    company_a.put("/sso/settings", json={"google_enabled": False, "microsoft_enabled": True})
    assert parse_qs(_sign_in(client, "google", company_a.slug).query)["sso_error"] == ["not_enabled"]
    fake_identity["email"] = emp["email"]
    assert parse_qs(_sign_in(client, "google").query)["sso_error"] == ["not_enabled"]
    # other email domain than the company allows
    company_a.put("/sso/settings", json={"google_enabled": True, "allowed_domains": ["alphaworks.in"]})
    assert parse_qs(_sign_in(client, "google", company_a.slug).query)["sso_error"] == ["domain_not_allowed"]
    company_a.put("/sso/settings", json={"allowed_domains": []})
    # unverified email
    fake_identity["verified"] = False
    assert parse_qs(_sign_in(client, "google", company_a.slug).query)["sso_error"] == ["email_not_verified"]
    fake_identity["verified"] = True
    # nobody with that email: no account is created
    fake_identity["email"] = f"stranger-{uuid.uuid4().hex[:6]}@example.com"
    assert parse_qs(_sign_in(client, "google", company_a.slug).query)["sso_error"] == ["no_account"]
    assert parse_qs(_sign_in(client, "google").query)["sso_error"] == ["no_account"]
    # an employee of company A cannot sign in on company B's address
    _enable(company_b)
    fake_identity["email"] = emp["email"]
    assert parse_qs(_sign_in(client, "google", company_b.slug).query)["sso_error"] == ["no_account"]
    # provider problems (guest accounts, failed exchange) come back as a message code
    fake_identity["error"] = "guest_account"
    assert parse_qs(_sign_in(client, "microsoft", company_a.slug).query)["sso_error"] == ["guest_account"]


def test_inactive_employee_is_refused(client, company_a, providers_ready, fake_identity):
    _enable(company_a)
    emp = company_a.add_employee("Gone")
    from core.db import system_session
    from modules.employees.models import Employee
    db = system_session()
    try:
        db.query(Employee).filter(Employee.email == emp["email"]).update({"active": False})
        db.commit()
    finally:
        db.close()
    fake_identity["email"] = emp["email"]
    assert parse_qs(_sign_in(client, "google", company_a.slug).query)["sso_error"] == ["no_account"]


def test_forged_or_expired_state_is_refused(client, company_a, providers_ready, fake_identity):
    _enable(company_a)
    emp = company_a.add_employee("Forge")
    fake_identity["email"] = emp["email"]
    client.cookies.clear()
    # a state made by someone else (no matching cookie in this browser)
    state = sso_router._sign_state("google", company_a.slug, "someone-elses-nonce")
    r = client.get(f"{API}/auth/sso/google/callback", params={"code": "x", "state": state}, follow_redirects=False)
    assert parse_qs(urlparse(r.headers["location"]).query)["sso_error"] == ["expired"]
    r = client.get(f"{API}/auth/sso/google/callback", params={"code": "x", "state": "garbage"}, follow_redirects=False)
    assert parse_qs(urlparse(r.headers["location"]).query)["sso_error"] == ["expired"]


def test_company_settings_need_permission_and_are_per_company(client, company_a, company_b, providers_ready):
    from tests.conftest import login_as
    emp = company_a.add_employee("Nopriv")
    assert login_as(company_a, emp).get("/sso/settings").status_code == 403
    _enable(company_a, allowed_domains=["alpha.example.in"])
    company_b.put("/sso/settings", json={"google_enabled": False, "allowed_domains": []})
    assert company_b.get("/sso/settings").json()["allowed_domains"] == []
    assert company_a.get("/sso/settings").json()["allowed_domains"] == ["alpha.example.in"]


def test_microsoft_identity_uses_sign_in_name_not_mail(monkeypatch):
    """The real Microsoft exchange, with the HTTP calls mocked."""
    import httpx

    def handler(request: httpx.Request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "at", "id_token": "e30.eyJ0aWQiOiJ0MSIsIm9pZCI6Im8xIn0.sig"})
        if request.url.host == "graph.microsoft.com":
            return httpx.Response(200, json={"id": "o1", "displayName": "Asha", "userPrincipalName": "Asha@Contoso.in",
                                             "mail": "ceo@victim.example"})
        return httpx.Response(404)

    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    ident = providers.fetch_identity("microsoft", "c", "http://x/cb", "id", "secret", "organizations")
    assert ident.email == "asha@contoso.in" and ident.email_verified and ident.tenant == "t1"

    def guest(request: httpx.Request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "at"})
        return httpx.Response(200, json={"userPrincipalName": "bob_gmail.com#EXT#@contoso.onmicrosoft.com"})

    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(guest), **kw))
    with pytest.raises(SsoError):
        providers.fetch_identity("microsoft", "c", "http://x/cb", "id", "secret", None)


def test_google_identity_requires_verified_email(monkeypatch):
    import httpx

    def handler(request: httpx.Request):
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "at"})
        return httpx.Response(200, json={"sub": "1", "email": "Meera@Example.in", "email_verified": False})

    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    ident = providers.fetch_identity("google", "c", "http://x/cb", "id", "secret")
    assert ident.email == "meera@example.in" and ident.email_verified is False
