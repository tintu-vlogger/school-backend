"""Number series per legal entity: the {ENTITY} token, and shared or per-entity counters."""
import uuid as uuidlib
from datetime import date

import pytest

from core.db import tenant_session
from core import numbering
from tests.conftest import Company


@pytest.fixture
def company_a(client):
    return Company(client, f"Num A {uuidlib.uuid4().hex[:8]}")


@pytest.fixture
def company_b(client):
    return Company(client, f"Num B {uuidlib.uuid4().hex[:8]}")


class _E:
    def __init__(self, name, code=None):
        self.name, self.code = name, code


def test_entity_code_and_render():
    assert numbering.entity_code(_E("Alpha Works Pvt Ltd", "ka-01")) == "KA-01"
    assert numbering.entity_code(_E("Alpha Works Private Limited")) == "AW"
    assert numbering.entity_code(None) == ""
    assert numbering.render("{ENTITY}/{FY}/{SEQ:3}", "", 7, date(2026, 11, 7), "MH") == "MH/2627/007"
    numbering.validate_pattern("{ENTITY}-{SEQ:4}")


def _series(company, key="employee"):
    return next(s for s in company.get("/number-series/").json()["items"] if s["key"] == key)


def _entities(company):
    a = company.post("/organization/legal-entities", json={"name": "Alpha Karnataka", "code": "KA"}).json()
    b = company.post("/organization/legal-entities", json={"name": "Alpha Maharashtra", "code": "MH"}).json()
    return a, b


def test_per_entity_needs_entity_token(company_a):
    s = _series(company_a)
    r = company_a.put(f"/number-series/{s['uuid']}", json={"counter_scope": "per_entity"})
    assert r.status_code == 400 and "{ENTITY}" in r.json()["detail"]
    r = company_a.put(f"/number-series/{s['uuid']}", json={"counter_scope": "everywhere"})
    assert r.status_code == 400


def test_shared_counter_with_entity_token(company_a):
    ka, mh = _entities(company_a)
    s = _series(company_a)
    r = company_a.put(f"/number-series/{s['uuid']}", json={"active": True, "pattern": "{ENTITY}-{SEQ:3}"})
    assert r.status_code == 200, r.text
    assert {e["code"]: e["next"] for e in r.json()["next_by_entity"]}["MH"] == "MH-001"
    e1 = company_a.add_employee("One", legal_entity_id=ka["id"])
    e2 = company_a.add_employee("Two", legal_entity_id=mh["id"])
    e3 = company_a.add_employee("Three", legal_entity_id=ka["id"])
    assert [e1["employee_code"], e2["employee_code"], e3["employee_code"]] == ["KA-001", "MH-002", "KA-003"]


def test_per_entity_counters(company_a):
    ka, mh = _entities(company_a)
    s = _series(company_a)
    r = company_a.put(f"/number-series/{s['uuid']}", json={"active": True, "pattern": "{ENTITY}{SEQ:3}",
                                                          "counter_scope": "per_entity"})
    assert r.status_code == 200 and r.json()["counter_scope"] == "per_entity"
    codes = [company_a.add_employee(n, legal_entity_id=e["id"])["employee_code"]
             for n, e in (("A", ka), ("B", mh), ("C", ka), ("D", mh), ("E", mh))]
    assert codes == ["KA001", "MH001", "KA002", "MH002", "MH003"]
    nxt = {e["code"]: e["next"] for e in _series(company_a)["next_by_entity"]}
    assert nxt["KA"] == "KA003" and nxt["MH"] == "MH004"
    # without an entity the main legal entity is used
    db = tenant_session(company_a.id)
    try:
        primary = db.execute(numbering.text("SELECT code, name FROM legal_entities WHERE company_id = :c AND is_primary"),
                             {"c": company_a.id}).first()
        value = numbering.next_number(db, company_a.id, "employee")
        db.rollback()
    finally:
        db.close()
    assert value.endswith("001") and value[:-3] == numbering.entity_code(_E(primary.name, primary.code))


def test_preview_shows_each_entity(company_a):
    _entities(company_a)
    r = company_a.post("/number-series/preview", json={"prefix": "L", "pattern": "{PREFIX}/{ENTITY}/{SEQ:2}"})
    assert r.status_code == 200
    examples = {x["example"] for x in r.json()["by_entity"]}
    assert {"L/KA/01", "L/MH/01"} <= examples


def test_entity_counters_are_per_company(company_a, company_b):
    made = {}
    for c in (company_a, company_b):
        made[c.id] = _entities(c)
        s = _series(c)
        c.put(f"/number-series/{s['uuid']}", json={"active": True, "pattern": "{ENTITY}{SEQ:2}", "counter_scope": "per_entity"})
    ka_a, ka_b = made[company_a.id][0], made[company_b.id][0]
    assert company_a.add_employee("X", legal_entity_id=ka_a["id"])["employee_code"] == "KA01"
    assert company_a.add_employee("X2", legal_entity_id=ka_a["id"])["employee_code"] == "KA02"
    assert company_b.add_employee("Y", legal_entity_id=ka_b["id"])["employee_code"] == "KA01"
    # another company's entity is refused, so its counter can never be touched
    r = company_b.post("/employees/", json={"first_name": "Z", "last_name": "Q", "email": f"z-{uuidlib.uuid4().hex[:6]}@example.com",
                                            "password": "Secret123#", "legal_entity_id": ka_a["id"]})
    assert r.status_code in (400, 404)
