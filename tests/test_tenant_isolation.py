"""Company A must never read or change company B's data, through the API or directly
through a database session (row-level security)."""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from core.db import tenant_session
from modules.departments.models import Department
from modules.employees.models import Employee


def test_employee_lists_are_separate(company_a, company_b):
    a_emp = company_a.add_employee("Anil")
    b_emp = company_b.add_employee("Bina")
    a_ids = {e["uuid"] for e in company_a.get("/employees/").json()}
    b_ids = {e["uuid"] for e in company_b.get("/employees/").json()}
    assert a_emp["uuid"] in a_ids and b_emp["uuid"] not in a_ids
    assert b_emp["uuid"] in b_ids and a_emp["uuid"] not in b_ids


def test_cannot_read_or_change_other_company_employee(company_a, company_b):
    b_emp = company_b.add_employee("Bala")
    assert company_a.get(f"/employees/{b_emp['uuid']}").status_code == 404
    assert company_a.put(f"/employees/{b_emp['uuid']}", json={"first_name": "Hacked"}).status_code == 404
    assert company_a.delete(f"/employees/{b_emp['uuid']}").status_code == 404
    # still unchanged for its owner
    again = company_b.get(f"/employees/{b_emp['uuid']}").json()
    assert again["first_name"] == "Bala" and again["active"] is True


def test_departments_are_separate(company_a, company_b):
    r = company_b.post("/departments/", json={"department_name": "Secret Lab"})
    assert r.status_code == 201, r.text
    dept = r.json()
    names = [d["department_name"] for d in company_a.get("/departments/").json()]
    assert "Secret Lab" not in names
    assert company_a.get(f"/departments/{dept['uuid']}").status_code == 404
    assert company_a.put(f"/departments/{dept['uuid']}", json={"department_name": "Mine"}).status_code == 404
    # company A may use the same name for its own department
    assert company_a.post("/departments/", json={"department_name": "Secret Lab"}).status_code == 201


def test_roles_and_audit_are_separate(company_a, company_b):
    company_b.add_employee("Bhavna")
    a_roles = company_a.get("/roles/").json()
    assert a_roles and all(r.get("company_id", company_a.id) == company_a.id for r in a_roles)
    audit = company_a.get("/audit/").json()
    rows = audit.get("items", audit.get("data", []))
    assert all(r.get("company_id", company_a.id) == company_a.id for r in rows)


def test_session_cannot_see_other_company_rows(company_a, company_b):
    b_emp = company_b.add_employee("Bharat")
    db = tenant_session(company_a.id)
    try:
        assert db.query(Employee).filter(Employee.uuid == b_emp["uuid"]).first() is None
        assert all(e.company_id == company_a.id for e in db.query(Employee).all())
        # a raw query is filtered too, even without a WHERE on company_id
        count = db.execute(text("SELECT count(*) FROM employees WHERE company_id = :b"), {"b": company_b.id}).scalar()
        assert count == 0
        # updates silently match nothing
        changed = db.execute(text("UPDATE employees SET first_name = 'X' WHERE company_id = :b"), {"b": company_b.id}).rowcount
        assert changed == 0
        db.rollback()
    finally:
        db.close()


def test_session_cannot_insert_into_other_company(company_a, company_b):
    db = tenant_session(company_a.id)
    try:
        db.add(Department(company_id=company_b.id, department_name="Planted"))
        with pytest.raises(DBAPIError):
            db.flush()
        db.rollback()
    finally:
        db.close()


def test_session_without_company_sees_nothing(company_a):
    company_a.add_employee("Chitra")
    from core.db import SessionLocal
    db = SessionLocal()
    try:
        assert db.query(Employee).count() == 0
    finally:
        db.close()


def test_every_company_owned_table_has_forced_rls():
    """Guard for new modules: any table with a company_id column must have RLS on and forced."""
    from core.db import system_session
    db = system_session()
    try:
        rows = db.execute(text("""
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'r'
              AND EXISTS (SELECT 1 FROM information_schema.columns col
                          WHERE col.table_schema = 'public' AND col.table_name = c.relname AND col.column_name = 'company_id')
        """)).fetchall()
    finally:
        db.close()
    missing = [name for name, enabled, forced in rows if not (enabled and forced)]
    assert rows and missing == [], f"Tables without forced RLS: {missing}"
