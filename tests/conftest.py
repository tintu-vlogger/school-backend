"""Test setup: a fresh database for every test run, migrated to head, dropped at the end.

Each run gets its own database (school_test_<pid>), so parallel runs never collide and test
data (companies, seeds, audit rows) never accumulates on disk. Set TEST_DATABASE_URL to use
a fixed database instead (it is then kept).

Companies are created through the real API (/auth/register), so every test goes through
authentication, the request context and row-level security exactly like production.
"""
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
_FIXED_DB = os.environ.get("TEST_DATABASE_URL")
TEST_DB_URL = _FIXED_DB or f"postgresql://tintucraju@localhost:5432/school_test_{os.getpid()}"
os.environ["DATABASE_URL"] = TEST_DB_URL
os.environ.setdefault("APP_ENV", "development")


def _ensure_database():
    import psycopg2
    from urllib.parse import urlparse

    url = urlparse(TEST_DB_URL)
    name = url.path.lstrip("/")
    conn = psycopg2.connect(dbname="postgres", user=url.username, host=url.hostname, port=url.port or 5432,
                            password=url.password)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
        if not cur.fetchone():
            cur.execute(f'CREATE DATABASE "{name}"')
    conn.close()


def _drop_database():
    import psycopg2
    from urllib.parse import urlparse

    url = urlparse(TEST_DB_URL)
    try:
        from core.db import engine  # release pooled connections first
        engine.dispose()
    except Exception:
        pass
    conn = psycopg2.connect(dbname="postgres", user=url.username, host=url.hostname, port=url.port or 5432,
                            password=url.password)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{url.path.lstrip("/")}" WITH (FORCE)')
    conn.close()


def pytest_sessionfinish(session, exitstatus):
    if not _FIXED_DB:
        _drop_database()


_ensure_database()
subprocess.run(
    [sys.executable, "-m", "alembic", "upgrade", "heads"],
    cwd=BACKEND, check=True, env={**os.environ, "PYTHONPATH": str(BACKEND)}, capture_output=True,
)

from fastapi.testclient import TestClient  # noqa: E402

from main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    return TestClient(app)


class Company:
    def __init__(self, client, name):
        self.client = client
        email = f"owner-{uuid.uuid4().hex[:10]}@example.com"
        r = client.post("/api/v1/auth/register", json={
            "email": email, "password": "Secret123#", "full_name": f"{name} Owner", "company_name": name,
        })
        assert r.status_code == 200, r.text
        body = r.json()
        self.token = body["access_token"]
        self.id = body["user"]["company_id"]
        self.slug = body["user"]["slug"]
        self.email = email

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.token}", "X-Company-Slug": self.slug}

    def get(self, path, **kw):
        return self.client.get(f"/api/v1{path}", headers=self.headers, **kw)

    def post(self, path, json=None, **kw):
        return self.client.post(f"/api/v1{path}", headers=self.headers, json=json, **kw)

    def put(self, path, json=None, **kw):
        return self.client.put(f"/api/v1{path}", headers=self.headers, json=json, **kw)

    def patch(self, path, json=None, **kw):
        return self.client.patch(f"/api/v1{path}", headers=self.headers, json=json, **kw)

    def delete(self, path, **kw):
        return self.client.delete(f"/api/v1{path}", headers=self.headers, **kw)

    def add_employee(self, first="Ravi", last="Kumar", **extra):
        payload = {"first_name": first, "last_name": last, "password": "Secret123#",
                   "email": f"{first.lower()}-{uuid.uuid4().hex[:8]}@example.com", **extra}
        r = self.post("/employees/", json=payload)
        assert r.status_code == 201, r.text
        return r.json()


@pytest.fixture
def company_a(client):
    return Company(client, f"Alpha Works {uuid.uuid4().hex[:6]}")


@pytest.fixture
def company_b(client):
    return Company(client, f"Beta Traders {uuid.uuid4().hex[:6]}")


class EmployeeSession:
    """Signed in as one of a company's employees."""

    def __init__(self, company: "Company", employee: dict, password: str = "Secret123#"):
        r = company.client.post("/api/v1/auth/login", json={"email": employee["email"], "password": password},
                                headers={"X-Company-Slug": company.slug})
        assert r.status_code == 200, r.text
        self.client, self.slug, self.token, self.employee = company.client, company.slug, r.json()["access_token"], employee

    headers = Company.headers
    get, post, put, patch, delete = Company.get, Company.post, Company.put, Company.patch, Company.delete


def login_as(company, employee):
    return EmployeeSession(company, employee)
