"""Number series: formats, resets and employee IDs."""
from datetime import date

from core.numbering import financial_year, period_key, render


def test_render_tokens():
    d = date(2026, 11, 7)
    assert render("{PREFIX}{YYYY}{MM}{SEQ:5}", "EMP", 42, d) == "EMP20261100042"
    assert render("{PREFIX}/{FY}/{SEQ:3}", "LTR", 7, d) == "LTR/2627/007"
    assert render("{PREFIX}-{YY}{DD}-{SEQ}", "X", 12, d) == "X-2607-12"
    assert financial_year(date(2027, 2, 1)) == "2627"
    assert period_key("monthly", d) == "2026-11" and period_key("financial_year", date(2027, 3, 31)) == "FY2026"
    assert period_key("never", d) == "all"


def test_default_series_and_preview(company_a):
    data = company_a.get("/number-series/").json()
    keys = {s["key"]: s for s in data["items"]}
    assert {"employee", "request", "letter"} <= set(keys)
    assert keys["employee"]["active"] is False
    r = company_a.post("/number-series/preview", json={"prefix": "E", "pattern": "{PREFIX}{SEQ:3}", "start_number": 5})
    assert r.json()["examples"] == ["E005", "E006", "E007"]
    bad = company_a.post("/number-series/preview", json={"pattern": "{PREFIX}{FOO}{SEQ}"})
    assert bad.status_code == 400
    bad = company_a.post("/number-series/preview", json={"pattern": "{PREFIX}"})
    assert bad.status_code == 400 and "counter" in bad.json()["detail"]


def test_employee_codes_generated_when_enabled(company_a):
    series = next(s for s in company_a.get("/number-series/").json()["items"] if s["key"] == "employee")
    assert company_a.add_employee("Manual", employee_code="M-1")["employee_code"] == "M-1"
    assert company_a.add_employee("NoCode")["employee_code"] is None
    r = company_a.put(f"/number-series/{series['uuid']}", json={"active": True, "prefix": "AW", "pattern": "{PREFIX}{SEQ:4}",
                                                                 "allow_manual": False})
    assert r.status_code == 200 and r.json()["next"] == "AW0001"
    assert company_a.add_employee("First")["employee_code"] == "AW0001"
    assert company_a.add_employee("Second")["employee_code"] == "AW0002"
    r = company_a.post("/employees/", json={"first_name": "T", "last_name": "U", "email": "tu@example.com",
                                            "password": "Secret123#", "employee_code": "HAND-1"})
    assert r.status_code == 400


def test_duplicate_employee_code_refused(company_a):
    company_a.add_employee("Dup", employee_code="D-1")
    r = company_a.post("/employees/", json={"first_name": "V", "last_name": "W", "email": "vw@example.com",
                                            "password": "Secret123#", "employee_code": "D-1"})
    assert r.status_code == 400 and "already used" in r.json()["detail"]


def test_counters_are_per_company(company_a, company_b):
    for c in (company_a, company_b):
        s = next(s for s in c.get("/number-series/").json()["items"] if s["key"] == "employee")
        c.put(f"/number-series/{s['uuid']}", json={"active": True})
    assert company_a.add_employee("A1")["employee_code"] == "EMP0001"
    assert company_b.add_employee("B1")["employee_code"] == "EMP0001"
