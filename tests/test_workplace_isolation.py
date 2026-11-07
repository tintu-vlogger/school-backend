"""Workplace services: company A never reads or changes company B's cases, grievances,
files, letters, tasks or assets, through the API or directly in the database."""
from sqlalchemy import text

from core.db import tenant_session
from tests.conftest import login_as

TABLES = ["helpdesk_cases", "helpdesk_categories", "grievances", "grievance_committees", "doc_folders", "doc_files",
          "stored_files", "policies", "policy_acknowledgements", "letter_templates", "letterheads", "letters",
          "esign_requests", "tasks", "assets", "asset_assignments"]


def test_api_isolation(company_a, company_b):
    b_emp = company_b.add_employee("Bimal")
    b = login_as(company_b, b_emp)
    cat = next(c for c in company_b.get("/helpdesk/categories").json()["items"] if c["parent_id"] is None)
    case = b.post("/helpdesk/cases", json={"category_id": cat["id"], "subject": "B case", "description": "x"}).json()
    task = company_b.post("/tasks", json={"title": "B task", "assignee_employee_id": b_emp["id"]}).json()
    asset = company_b.post("/assets", json={"name": "B laptop"}).json()
    tpl = company_b.get("/letters/templates").json()["items"][0]
    pol = company_b.post("/documents/policies", json={"title": "B policy"}).json()
    g = b.post("/grievances", json={"subject": "B grievance", "description": "x"}).json()

    assert company_a.get(f"/helpdesk/cases/{case['uuid']}").status_code == 404
    assert company_a.post(f"/helpdesk/cases/{case['uuid']}/messages", json={"body": "hi"}).status_code == 404
    assert all(c["uuid"] != case["uuid"] for c in company_a.get("/helpdesk/cases", params={"queue": "all"}).json()["items"])
    assert company_a.post("/helpdesk/cases", json={"category_id": cat["id"], "subject": "x", "description": "y",
                                                   "requester_employee_id": b_emp["id"]}).status_code in (400, 403)
    assert company_a.get(f"/tasks/{task['uuid']}").status_code == 404
    assert company_a.put(f"/tasks/{task['uuid']}", json={"title": "x"}).status_code == 404
    assert company_a.get(f"/assets/{asset['uuid']}").status_code == 404
    assert company_a.post(f"/assets/{asset['uuid']}/assign", json={"employee_id": b_emp["id"]}).status_code == 404
    assert all(x["uuid"] != asset["uuid"] for x in company_a.get("/assets").json()["items"])
    assert company_a.get(f"/letters/templates/{tpl['uuid']}").status_code == 404
    assert company_a.post("/letters/generate", json={"template_uuid": tpl["uuid"], "employee_id": b_emp["id"]}).status_code == 404
    assert company_a.get(f"/documents/policies/{pol['uuid']}").status_code == 404
    assert company_a.get(f"/grievances/{g['uuid']}").status_code == 404
    assert company_a.post("/grievances/track", json={"code": g["tracking_code"]}).status_code == 404


def test_database_isolation(company_a, company_b):
    company_b.post("/tasks", json={"title": "B only"})
    company_b.post("/assets", json={"name": "B only"})
    db = tenant_session(company_a.id)
    try:
        for t in TABLES:
            n = db.execute(text(f"SELECT count(*) FROM {t} WHERE company_id = :b"), {"b": company_b.id}).scalar()
            assert n == 0, t
    finally:
        db.close()
