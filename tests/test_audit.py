"""Every change is audited with old and new values; secrets are masked; the log is append-only."""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from core.db import tenant_session


def _entries(company, **params):
    r = company.get("/audit/", params={"size": 200, **params})
    assert r.status_code == 200, r.text
    return r.json()["items"]


def test_create_and_update_are_recorded_with_field_changes(company_a):
    emp = company_a.add_employee("Deepa", "Nair")
    r = company_a.put(f"/employees/{emp['uuid']}", json={"first_name": "Deepika"})
    assert r.status_code == 200
    rows = _entries(company_a, entity_type="employees", entity_uuid=emp["uuid"])
    actions = [row["action"] for row in rows]
    assert "created" in actions and "updated" in actions
    upd = next(row for row in rows if row["action"] == "updated")
    assert upd["changes"]["first_name"] == {"old": "Deepa", "new": "Deepika"}
    assert upd["actor_type"] == "owner" and upd["actor_name"]
    assert upd["request_id"]
    created = next(row for row in rows if row["action"] == "created")
    assert created["changes"]["password"]["new"] == "••••••"  # never the hash


def test_sign_in_is_an_event(client, company_a):
    r = client.post("/api/v1/auth/login", json={"email": company_a.email, "password": "Secret123#"},
                    headers={"X-Company-Slug": company_a.slug})
    assert r.status_code == 200, r.text
    rows = _entries(company_a, action="auth.signed_in")
    assert rows and rows[0]["details"]["method"] == "password"


def test_filters_by_date_and_person(company_a):
    company_a.add_employee("Esha")
    filters = company_a.get("/audit/filters").json()
    assert any(t["value"] == "employees" for t in filters["entity_types"])
    assert any(a["value"] == "owner" for a in filters["actors"])
    assert _entries(company_a, actor="owner")
    assert _entries(company_a, date_from="2000-01-01", date_to="2000-01-02") == []


def test_audit_log_is_append_only(company_a):
    company_a.add_employee("Farah")
    db = tenant_session(company_a.id)
    try:
        with pytest.raises(DBAPIError):
            db.execute(text("UPDATE audit_logs SET action = 'x' WHERE company_id = :c"), {"c": company_a.id})
        db.rollback()
        with pytest.raises(DBAPIError):
            db.execute(text("DELETE FROM audit_logs WHERE company_id = :c"), {"c": company_a.id})
        db.rollback()
    finally:
        db.close()


def test_other_company_cannot_see_audit(company_a, company_b):
    emp = company_b.add_employee("Gita")
    assert _entries(company_a, entity_uuid=emp["uuid"]) == []
