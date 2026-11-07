"""Organisation structure, tags, skills and the full employee record: rules, permissions,
tenant isolation, duplicates, lifecycle, job history, documents and self service."""
import uuid as uuidlib
from datetime import date, timedelta

import pytest

from tests.conftest import Company, login_as
from modules.employees.validators import verhoeff_digit



@pytest.fixture
def company_a(client):
    # A unique name gives a unique web address (the shared names collide in a long-lived test database).
    return Company(client, f"Org A {uuidlib.uuid4().hex[:8]}")


@pytest.fixture
def company_b(client):
    return Company(client, f"Org B {uuidlib.uuid4().hex[:8]}")


def _master(company, key, label):
    items = company.get(f"/masters/options/{key}", params={"q": label}).json()["items"]
    return next(i["value"] for i in items if i["label"] == label)


def _aadhaar(seed: int) -> str:
    base = "4" + str(10_000_000_000 + seed)[-10:]
    return base + verhoeff_digit(base)


# ── Organisation ─────────────────────────────────────────────────────

def test_defaults_seeded_for_new_company(company_a):
    s = company_a.get("/organization/summary").json()
    assert s["grades"] == 7 and s["skills"] >= 20 and s["tags"] >= 5 and s["legal_entities"] == 1


def test_legal_entity_checks(company_a):
    ka = _master(company_a, "state", "Karnataka")
    r = company_a.post("/organization/legal-entities", json={"name": "Alpha Labs", "pan": "AABCA1234Z", "gstin": "29AABCX1234Z1Z5"})
    assert r.status_code == 400 and "PAN" in r.json()["detail"]
    r = company_a.post("/organization/legal-entities", json={
        "name": "Alpha Labs", "pan": "AABCA1234Z", "tan": "BLRA12345B", "gstin": "29AABCA1234Z1Z5", "pf_code": "KABNG1234567",
        "esi_code": "53000123450001001", "state_id": ka,
        "registrations": [{"kind": "pt", "state_id": ka, "number": "PT-1"}],
        "bank_accounts": [{"account_number": "50200012345678", "ifsc": "HDFC0000523"}],
        "signatory": {"name": "A Director", "pan": "ABCPD1234E"}})
    assert r.status_code == 201, r.text
    ent = r.json()
    assert ent["bank_accounts"][0]["primary"] is True and ent["registrations"][0]["state_name"] == "Karnataka"
    assert company_a.post("/organization/legal-entities", json={"name": "alpha labs"}).status_code == 400
    # a masked number sent back keeps the stored one
    upd = company_a.put(f"/organization/legal-entities/{ent['uuid']}", json={"bank_accounts": ent["bank_accounts"]})
    assert upd.status_code == 200 and upd.json()["bank_accounts"][0]["account_number"] == "50200012345678"


def test_location_needs_state_and_valid_geo(company_a):
    assert company_a.post("/organization/locations", json={"name": "Pune"}).status_code == 400
    mh = _master(company_a, "state", "Maharashtra")
    r = company_a.post("/organization/locations", json={"name": "Pune", "state_id": mh, "latitude": 18.5, "longitude": None})
    assert r.status_code == 400
    r = company_a.post("/organization/locations", json={"name": "Pune", "state_id": mh, "latitude": 18.55, "longitude": 73.94,
                                                       "radius_m": 150, "pincode": "411014"})
    assert r.status_code == 201 and r.json()["state_name"] == "Maharashtra"


def test_department_parent_loop_refused(company_a):
    a = company_a.post("/departments/", json={"department_name": "Eng"}).json()
    b = company_a.post("/departments/", json={"department_name": "Platform", "parent_id": a["id"]}).json()
    assert b["parent_name"] == "Eng"
    r = company_a.put(f"/departments/{a['uuid']}", json={"parent_id": b["id"]})
    assert r.status_code == 400 and "itself" in r.json()["detail"]


def test_tags_hierarchy_and_filter(company_a):
    parent = company_a.post("/organization/tags", json={"name": "Squad"}).json()
    child = company_a.post("/organization/tags", json={"name": "Alpha squad", "parent_id": parent["id"]}).json()
    e1, e2 = company_a.add_employee("Tara"), company_a.add_employee("Uday")
    r = company_a.post("/organization/tags/assign", json={"entity_type": "employees", "entity_ids": [e1["id"]], "add": [child["id"]]})
    assert r.status_code == 200 and r.json()["changed"] == 1
    # filtering by the parent tag finds people tagged with a sub-tag
    ids = {e["uuid"] for e in company_a.get("/employees/", params={"tag_id": parent["id"]}).json()}
    assert e1["uuid"] in ids and e2["uuid"] not in ids
    # no loops
    r = company_a.put(f"/organization/tags/{parent['uuid']}", json={"parent_id": child["id"]})
    assert r.status_code == 400


def test_team_members_and_lead(company_a):
    lead, m = company_a.add_employee("Vani"), company_a.add_employee("Wasim")
    r = company_a.post("/organization/teams", json={"name": "Squad X", "lead_employee_id": lead["id"], "member_ids": [m["id"]]})
    assert r.status_code == 201, r.text
    team = r.json()
    assert team["member_count"] == 2 and {x["is_lead"] for x in team["members"]} == {True, False}
    ids = {e["uuid"] for e in company_a.get("/employees/", params={"team_id": team["id"]}).json()}
    assert ids == {lead["uuid"], m["uuid"]}


def test_skills_and_verification(company_a):
    emp = company_a.add_employee("Xavier", password="Secret123#")
    skill = company_a.get("/organization/skills", params={"q": "Python"}).json()[0]
    me = login_as(company_a, emp)
    r = me.post(f"/employees/{emp['uuid']}/skills", json={"skill_id": skill["id"], "level": 4, "verified": True})
    assert r.status_code == 201 and r.json()[0]["verified"] is False  # employees can't verify themselves
    item = r.json()[0]["uuid"]
    r = company_a.put(f"/employees/{emp['uuid']}/skills/{item}", json={"verified": True})
    assert r.json()[0]["verified"] is True
    # changing the level yourself asks for a new check
    r = me.put(f"/employees/{emp['uuid']}/skills/{item}", json={"level": 5})
    assert r.json()[0]["verified"] is False
    assert company_a.post(f"/employees/{emp['uuid']}/skills", json={"skill_id": skill["id"]}).status_code == 400


def test_org_tables_are_tenant_isolated(company_a, company_b):
    ent = company_b.post("/organization/legal-entities", json={"name": "Beta Pvt"}).json()
    team = company_b.post("/organization/teams", json={"name": "Beta team"}).json()
    tag = company_b.post("/organization/tags", json={"name": "Beta tag"}).json()
    assert "Beta Pvt" not in [e["name"] for e in company_a.get("/organization/legal-entities").json()]
    assert company_a.get(f"/organization/legal-entities/{ent['uuid']}").status_code == 404
    assert company_a.put(f"/organization/teams/{team['uuid']}", json={"name": "Mine"}).status_code == 404
    assert company_a.put(f"/organization/tags/{tag['uuid']}", json={"name": "Mine"}).status_code == 404
    # another company's ids can't be used on our records
    emp = company_a.add_employee("Yamini")
    assert company_a.put(f"/employees/{emp['uuid']}", json={"legal_entity_id": ent["id"]}).status_code == 400
    assert company_a.post("/organization/tags/assign", json={"entity_ids": [emp["id"]], "add": [tag["id"]]}).status_code == 400


def test_org_changes_need_permission(company_a):
    emp = company_a.add_employee("Zoya")
    me = login_as(company_a, emp)
    assert me.post("/organization/grades", json={"name": "G9"}).status_code == 403
    assert me.post("/organization/tags", json={"name": "Mine"}).status_code == 403
    assert me.get("/organization/locations").status_code == 200  # lists feed pickers
    assert me.get("/employees/export").status_code == 403


# ── Employee record ──────────────────────────────────────────────────

def test_statutory_formats_and_masking(company_a):
    r = company_a.post("/employees/", json={"first_name": "A", "last_name": "B", "email": f"ab{uuidlib.uuid4().hex[:6]}@example.com",
                                            "pan": "ABCCE1234F"})
    assert r.status_code == 400 and "individual" in r.json()["detail"]
    r = company_a.post("/employees/", json={"first_name": "A", "last_name": "B", "email": f"ab{uuidlib.uuid4().hex[:6]}@example.com",
                                            "aadhaar": "234567890123"})
    assert r.status_code == 400 and "Aadhaar" in r.json()["detail"]
    number = _aadhaar(1)
    emp = company_a.add_employee("Bhanu", pan="ABCPB1234F", aadhaar=number, uan="100200300400")
    prof = company_a.get(f"/employees/{emp['uuid']}/profile").json()
    assert prof["aadhaar_masked"] == f"XXXX XXXX {number[-4:]}" and number not in str(prof)
    full = company_a.get(f"/employees/{emp['uuid']}/aadhaar").json()["aadhaar"]
    assert full.replace(" ", "") == number
    # the audit log never holds the full number
    logs = company_a.get("/audit/", params={"entity_type": "employees"}).json()
    assert number not in str(logs)


def test_duplicates_are_caught(company_a):
    number = _aadhaar(2)
    first = company_a.add_employee("Chitra", pan="ABCPC1111F", aadhaar=number, uan="100200300401", phone="+91 98450 00001")
    dups = company_a.post("/employees/duplicates", json={"pan": "abcpc1111f", "aadhaar": number, "phone": "9845000001"}).json()
    assert {d["field"] for d in dups} == {"pan", "aadhaar", "phone"} and dups[0]["uuid"] == first["uuid"]
    r = company_a.post("/employees/", json={"first_name": "C", "last_name": "D", "email": f"cd{uuidlib.uuid4().hex[:6]}@example.com",
                                            "aadhaar": number})
    assert r.status_code == 400 and "Chitra" in r.json()["detail"]
    r = company_a.post("/employees/", json={"first_name": "C", "last_name": "D", "email": first["email"].upper()})
    assert r.status_code == 400


def test_job_change_history_and_scheduled_change(company_a):
    boss = company_a.add_employee("Dev")
    emp = company_a.add_employee("Esha")
    loc = company_a.post("/organization/locations", json={"name": "Chennai", "state_id": _master(company_a, "state", "Tamil Nadu")}).json()
    # direct edit records history
    company_a.put(f"/employees/{emp['uuid']}", json={"reporting_manager_id": boss["id"]})
    hist = company_a.get(f"/employees/{emp['uuid']}/history").json()
    assert hist[0]["change_type"] == "manager_change" and hist[0]["items"][0]["new"].startswith("Dev")
    # a future transfer waits for its date
    later = (date.today() + timedelta(days=10)).isoformat()
    r = company_a.post(f"/employees/{emp['uuid']}/job-changes", json={
        "change_type": "transfer", "effective_date": later, "changes": {"location_id": loc["id"]}, "reason": "New office"})
    assert r.status_code == 201 and r.json()["status"] == "scheduled"
    assert company_a.get(f"/employees/{emp['uuid']}/profile").json()["location_id"] is None
    # a past-dated change applies at once
    r = company_a.post(f"/employees/{emp['uuid']}/job-changes", json={
        "change_type": "transfer", "effective_date": date.today().isoformat(), "changes": {"location_id": loc["id"]}})
    assert r.json()["status"] == "applied"
    assert company_a.get(f"/employees/{emp['uuid']}/profile").json()["location_name"] == "Chennai"


def test_lifecycle_no_delete_and_rehire(company_a):
    emp = company_a.add_employee("Faiz", status="probation", probation_end_date=(date.today() + timedelta(days=90)).isoformat())
    assert emp["status"] == "probation"
    # still working: "delete" only marks them as left
    assert company_a.delete(f"/employees/{emp['uuid']}").json()["detail"] == "Employee deactivated"
    prof = company_a.get(f"/employees/{emp['uuid']}/profile").json()
    assert prof["status"] == "exited" and prof["active"] is False
    # archived next, never removed
    assert company_a.post(f"/employees/{emp['uuid']}/archive", json={}).status_code == 200
    assert company_a.get(f"/employees/{emp['uuid']}").status_code == 200
    ids = {e["uuid"] for e in company_a.get("/employees/").json()}
    assert emp["uuid"] not in ids  # hidden from the normal list
    # rehire on the same record keeps the history
    r = company_a.post(f"/employees/{emp['uuid']}/rehire", json={"date_of_joining": (date.today() + timedelta(days=1)).isoformat(),
                                                                 "status": "probation"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "probation" and body["rehire_count"] == 1
    assert {h["change_type"] for h in body["history"]} >= {"joining", "exit", "status", "rehire"}
    # moves that make no sense are refused
    assert company_a.post(f"/employees/{emp['uuid']}/status", json={"status": "pre_joining"}).status_code == 400


def test_notice_sets_last_day(company_a):
    emp = company_a.add_employee("Gita", notice_period_days=60)
    r = company_a.post(f"/employees/{emp['uuid']}/status", json={"status": "notice", "resignation_date": "2026-11-01"})
    assert r.status_code == 200 and r.json()["exit_date"] == "2026-12-31"


def test_sections_rules(company_a):
    emp = company_a.add_employee("Hema")
    base = f"/employees/{emp['uuid']}/details"
    spouse = _master(company_a, "relation", "Spouse")
    fm = company_a.post(f"{base}/family", json={"name": "Raj", "relation_id": spouse}).json()
    assert company_a.post(f"{base}/nominees", json={"kind": "pf", "family_member_id": fm["id"], "share_percent": 60}).status_code == 201
    r = company_a.post(f"{base}/nominees", json={"kind": "pf", "name": "Someone", "share_percent": 50})
    assert r.status_code == 400 and "100" in r.json()["detail"]
    # bank: IFSC format and primary flag
    assert company_a.post(f"{base}/bank-accounts", json={"account_holder": "Hema", "account_number": "123456789", "ifsc": "HDFC123"}).status_code == 400
    a1 = company_a.post(f"{base}/bank-accounts", json={"account_holder": "Hema", "account_number": "123456789012", "ifsc": "HDFC0001234"}).json()
    assert a1["is_primary"] is True
    a2 = company_a.post(f"{base}/bank-accounts", json={"account_holder": "Hema", "account_number": "999456789012", "ifsc": "ICIC0001234", "is_primary": True}).json()
    rows = company_a.get(f"{base}/bank-accounts").json()
    assert [r["is_primary"] for r in rows if r["uuid"] == a1["uuid"]] == [False] and a2["is_primary"]
    # bank accounts are switched off, not removed
    company_a.delete(f"{base}/bank-accounts/{a1['uuid']}")
    assert any(r["uuid"] == a1["uuid"] and r["active"] is False for r in company_a.get(f"{base}/bank-accounts").json())
    # previous job with this year's income and tax
    r = company_a.post(f"{base}/experience", json={"company_name": "Infosys", "fy_taxable_income": 300000, "fy_tds": 400000})
    assert r.status_code == 400
    r = company_a.post(f"{base}/experience", json={"company_name": "Infosys", "fy_taxable_income": 300000, "fy_tds": 12000})
    assert r.status_code == 201 and r.json()["financial_year"]


def test_self_service(company_a):
    emp = company_a.add_employee("Indira", password="Secret123#")
    other = company_a.add_employee("Jai")
    me = login_as(company_a, emp)
    prof = me.get("/me/profile/full").json()
    assert prof["uuid"] == emp["uuid"] and prof["permissions"]["can_edit"] is False
    r = me.put("/me/profile/personal", json={"preferred_name": "Indu", "current_address": {"line1": "12 MG Road", "pincode": "560001"}})
    assert r.status_code == 200 and r.json()["preferred_name"] == "Indu"
    assert me.put("/me/profile/personal", json={"current_address": {"pincode": "12"}}).status_code == 400
    # own emergency contacts directly; bank details only through a request
    assert me.post(f"/employees/{emp['uuid']}/details/emergency-contacts", json={"name": "Mom", "phone": "+91 98450 12345"}).status_code == 201
    assert me.post(f"/employees/{emp['uuid']}/details/bank-accounts", json={"account_holder": "I", "account_number": "123456789012", "ifsc": "HDFC0001234"}).status_code == 403
    assert me.get(f"/employees/{other['uuid']}/details/family").status_code == 403
    assert me.get(f"/employees/{other['uuid']}/profile").status_code == 403
    # a sensitive change goes through approval (no flow: approved at once)
    r = me.post("/me/profile-change", json={"changes": {"pan": "ABCPI1234K", "date_of_birth": "1995-05-05",
                                                         "bank_account": {"account_holder": "Indira", "account_number": "555566667777", "ifsc": "SBIN0001234"}},
                                            "reason": "Corrections"})
    assert r.status_code == 201, r.text
    prof = company_a.get(f"/employees/{emp['uuid']}/profile").json()
    if r.json()["status"] == "approved":
        assert prof["pan"] == "ABCPI1234K" and prof["date_of_birth"] == "1995-05-05"
        assert any(b["ifsc"] == "SBIN0001234" and b["is_primary"] for b in prof["bank_accounts"])
    else:
        assert prof["pan"] is None


def test_documents_upload_verify_and_expiry(company_a):
    emp = company_a.add_employee("Kiran", password="Secret123#")
    passport = _master(company_a, "document_type", "Passport")
    me = login_as(company_a, emp)
    soon = (date.today() + timedelta(days=20)).isoformat()
    r = me.post(f"/employees/{emp['uuid']}/documents", data={"document_type_id": str(passport), "document_number": "K1234567",
                                                             "issue_date": "2020-01-01", "expiry_date": soon},
                files={"file": ("passport.pdf", b"%PDF-1.4 test", "application/pdf")}, json=None)
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["verification_status"] == "pending" and doc["file"]["filename"] == "passport.pdf" and doc["expiry_state"] == "expiring"
    # employees can't verify; HR can, and must give a reason to reject
    assert me.post(f"/employees/{emp['uuid']}/documents/{doc['uuid']}/verify", json={"status": "verified"}).status_code == 403
    assert company_a.post(f"/employees/{emp['uuid']}/documents/{doc['uuid']}/verify", json={"status": "rejected"}).status_code == 400
    v = company_a.post(f"/employees/{emp['uuid']}/documents/{doc['uuid']}/verify", json={"status": "verified"}).json()
    assert v["verification_status"] == "verified" and v["verified_by_name"]
    f = company_a.get(f"/employees/{emp['uuid']}/documents/{doc['uuid']}/file")
    assert f.status_code == 200 and f.content.startswith(b"%PDF")
    listing = company_a.get("/employees/documents", params={"expiry": "expiring"}).json()
    assert any(d["uuid"] == doc["uuid"] for d in listing["items"])
    bad = me.post(f"/employees/{emp['uuid']}/documents", data={"document_type_id": str(passport)},
                  files={"file": ("x.exe", b"MZ", "application/octet-stream")})
    assert bad.status_code == 400


def test_documents_and_details_are_tenant_isolated(company_a, company_b):
    emp_b = company_b.add_employee("Leela")
    assert company_a.get(f"/employees/{emp_b['uuid']}/documents").status_code == 404
    assert company_a.get(f"/employees/{emp_b['uuid']}/details/family").status_code == 404
    assert company_a.post(f"/employees/{emp_b['uuid']}/job-changes", json={"change_type": "transfer",
                                                                           "effective_date": "2026-01-01", "changes": {}}).status_code == 404


def test_export_and_bulk_manager(company_a):
    boss = company_a.add_employee("Mala")
    e1, e2 = company_a.add_employee("Naren"), company_a.add_employee("Om")
    r = company_a.post("/employees/bulk/manager", json={"employee_uuids": [e1["uuid"], e2["uuid"]], "manager_id": boss["id"],
                                                         "reason": "New team"})
    assert r.json()["changed"] == 2
    assert company_a.get(f"/employees/{e1['uuid']}").json()["reporting_manager_id"] == boss["id"]
    csv = company_a.get("/employees/export", params={"format": "csv", "uuids": f"{e1['uuid']},{e2['uuid']}"})
    assert csv.status_code == 200 and "Naren" in csv.text and "Mala" in csv.text
    xlsx = company_a.get("/employees/export", params={"format": "xlsx"})
    assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK"


def test_saved_views_are_personal(company_a):
    r = company_a.post("/employees/saved-views", json={"name": "Probation", "filters": {"status": ["probation"]}})
    assert r.status_code == 201
    assert [v["name"] for v in company_a.get("/employees/saved-views").json()] == ["Probation"]
