"""Payroll through the API: salary assignment, a full pay run (inputs, checks, background
calculation, approval through the workflow engine, lock, bank and PF files, payslips),
arrears on a back-dated revision, ESI contribution periods, final settlement, expenses,
loans, permissions and tenant isolation.
"""
import io
import uuid

import pytest

from tests.conftest import login_as


def _emp(company, first="Ravi", **extra):
    base = {"date_of_joining": "2020-01-01", "pan": f"ABCP{chr(65 + uuid.uuid4().int % 26)}{uuid.uuid4().int % 9000 + 1000}F",
            "uan": str(100000000000 + uuid.uuid4().int % 899999999999), "date_of_birth": "1990-05-10"}
    base.update(extra)
    e = company.add_employee(first, "Kumar", **base)
    acct = str(10 ** 11 + uuid.uuid4().int % (9 * 10 ** 11))
    r = company.post(f"/employees/{e['uuid']}/details/bank-accounts",
                     json={"account_holder": f"{first} Kumar", "account_number": acct, "ifsc": "SBIN0001234"})
    assert r.status_code == 201, r.text
    e["account_number"] = acct
    e["uan"] = base["uan"]
    return e


def _approve_all(company, request_type):
    for _ in range(6):
        tasks = company.get(f"/workflow/inbox?request_type={request_type}").json()["items"]
        if not tasks:
            return
        for t in tasks:
            company.post(f"/workflow/tasks/{t['uuid']}/decision", json={"decision": "approve"})


def _structure(company, code="STAFF"):
    return next(s for s in company.get("/payroll/structures").json() if s["code"] == code)


def _assign(company, emp, ctc, start="2026-04-01", code="STAFF"):
    r = company.post(f"/payroll/salaries/{emp['uuid']}/assign",
                     json={"structure_uuid": _structure(company, code)["uuid"], "annual_ctc": ctc, "effective_from": start})
    assert r.status_code == 201, r.text
    return r.json()


def _open_run(company, month, emps):
    r = company.post("/payroll/runs", json={"month": month})
    assert r.status_code == 201, r.text
    run = r.json()
    for e in emps:  # pay full months: no punches in a test company would read as absence
        assert company.post(f"/payroll/runs/{run['uuid']}/inputs",
                            json={"employee_uuid": e["uuid"], "kind": "lop_days", "days": 0}).status_code == 201
    return run


def _calculate(company, run):
    r = company.post(f"/payroll/runs/{run['uuid']}/calculate")
    assert r.status_code == 200, r.text
    run = company.get(f"/payroll/runs/{run['uuid']}").json()
    assert run["status"] == "review", run
    return run


def _approve_and_lock(company, run):
    r = company.post(f"/payroll/runs/{run['uuid']}/submit")
    assert r.status_code == 200, r.text
    run = r.json()
    if run["status"] == "pending_approval":
        task = next(s for s in run["approval"]["steps"] if s["status"] == "pending")
        assert company.post(f"/workflow/tasks/{task['uuid']}/decision", json={"decision": "approve"}).status_code == 200
    run = company.get(f"/payroll/runs/{run['uuid']}").json()
    assert run["status"] == "approved", run
    r = company.post(f"/payroll/runs/{run['uuid']}/lock")
    assert r.status_code == 200, r.text
    return r.json()


def _slip(company, run, emp):
    rows = company.get(f"/payroll/runs/{run['uuid']}/payslips").json()["items"]
    row = next(x for x in rows if x["employee_id"] == emp["id"])
    return company.get(f"/payroll/payslips/{row['uuid']}").json()


def _line(slip, code):
    return sum(float(x["amount"]) for x in slip["lines"] if x["code"] == code)


def test_salary_split_from_ctc(company_a):
    e = _emp(company_a)
    r = company_a.post(f"/payroll/salaries/{e['uuid']}/preview",
                       json={"structure_uuid": _structure(company_a)["uuid"], "annual_ctc": 600000})
    out = r.json()
    parts = {c["code"]: float(c["monthly"]) for c in out["components"]}
    # 50,000 a month: Basic 50% = 25,000; HRA 40% of basic (not metro) = 10,000; employer PF 1,800 inside CTC;
    # special allowance balances: 50,000 - 25,000 - 10,000 - 1,800 = 13,200
    assert parts["BASIC"] == 25000 and parts["HRA"] == 10000 and parts["PF_ER"] == 1800 and parts["SPECIAL"] == 13200
    assert out["monthly_gross"] == "48200.00" and out["wage_rule"]["compliant"]
    # a CTC too low for the structure is refused
    r = company_a.post(f"/payroll/salaries/{e['uuid']}/assign",
                       json={"structure_uuid": _structure(company_a, "MGMT")["uuid"], "annual_ctc": 100000,
                             "effective_from": "2026-04-01"})
    assert r.status_code == 400 and "negative" in r.json()["detail"]


def test_full_pay_run_files_and_payslip(company_a):
    e = _emp(company_a, "Meera")
    company_a.put(f"/payroll/profiles/{e['uuid']}", json={"pt_state": "KA"})
    _assign(company_a, e, 600000)
    run = _open_run(company_a, "2026-06-01", [e])
    checks = company_a.post(f"/payroll/runs/{run['uuid']}/checks").json()["checks"]
    assert not any(c["level"] == "block" for c in checks)
    run = _calculate(company_a, run)
    slip = _slip(company_a, run, e)
    assert (_line(slip, "BASIC"), _line(slip, "HRA"), _line(slip, "SPECIAL")) == (25000, 10000, 13200)
    assert _line(slip, "PF_EE") == 1800 and _line(slip, "PF_ER") == 550 and _line(slip, "EPS_ER") == 1250
    assert _line(slip, "PT") == 200 and _line(slip, "ESI_EE") == 0 and _line(slip, "TDS") == 0  # 5.78 lakh: rebate
    assert slip["net"] == "46200.00"
    # employee cannot see payroll before it is published
    me = login_as(company_a, e)
    assert me.get("/payroll/runs").status_code == 403
    assert me.get(f"/payroll/payslips/{slip['uuid']}").status_code == 403
    run = _approve_and_lock(company_a, run)
    assert run["status"] == "locked"
    # bank files
    r = company_a.get(f"/payroll/runs/{run['uuid']}/files/bank?format=generic_neft")
    lines = r.content.decode().strip().splitlines()
    assert lines[0].startswith("Payment mode,Beneficiary name,Account number") and len(lines) == 2
    assert e["account_number"] in lines[1] and "46200.00" in lines[1] and lines[1].startswith("NEFT")
    hdfc = company_a.get(f"/payroll/runs/{run['uuid']}/files/bank?format=hdfc").content.decode().strip().splitlines()
    assert len(hdfc) == 1 and hdfc[0].split(",")[0] == "N" and hdfc[0].split(",")[3] == "46200.00"
    sbi = company_a.get(f"/payroll/runs/{run['uuid']}/files/bank?format=sbi").content.decode().strip()
    assert sbi.split("|")[:3] == [e["account_number"], "SBIN0001234", "46200.00"]
    # PF ECR: 11 fields separated by #~#, whole rupees
    ecr = company_a.get(f"/payroll/runs/{run['uuid']}/files/ecr").content.decode().strip().splitlines()
    fields = ecr[0].split("#~#")
    assert len(fields) == 11
    assert fields[0] == e["uan"] and fields[1] == "MEERA KUMAR"
    assert fields[2:] == ["48200", "15000", "15000", "15000", "1800", "1250", "550", "0", "0"]
    # journal balances
    rows = company_a.get(f"/payroll/runs/{run['uuid']}/journal").json()
    assert abs(sum(float(x["debit"]) for x in rows) - sum(float(x["credit"]) for x in rows)) < 0.01
    tally = company_a.get(f"/payroll/runs/{run['uuid']}/files/tally").content.decode()
    assert "<VOUCHER VCHTYPE=\"Journal\"" in tally and "Salary payable" in tally
    # publish; the employee sees a password-protected PDF
    assert company_a.post(f"/payroll/runs/{run['uuid']}/publish?email=false").json()["published"] == 1
    mine = me.get("/payroll/me/payslips").json()["items"]
    assert len(mine) == 1 and mine[0]["net"] == "46200.00"
    r = me.get(f"/payroll/payslips/{mine[0]['uuid']}/pdf")
    assert r.status_code == 200 and r.content.startswith(b"%PDF") and b"/Encrypt" in r.content
    # locked runs cannot change; reopening needs a reason
    assert company_a.post(f"/payroll/runs/{run['uuid']}/inputs", json={"employee_uuid": e["uuid"], "kind": "earning",
                                                                       "component_code": "BONUS", "amount": 100}).status_code == 400
    assert company_a.post(f"/payroll/runs/{run['uuid']}/reopen", json={"reason": " "}).status_code == 400
    r = company_a.post(f"/payroll/runs/{run['uuid']}/reopen", json={"reason": "Bank details changed"})
    assert r.status_code == 200 and r.json()["status"] == "review"
    assert any(h["stage"] == "reopened" and h["note"] == "Bank details changed" for h in r.json()["history"])


def test_arrears_on_back_dated_revision(company_a):
    e = _emp(company_a, "Arun")
    _assign(company_a, e, 600000)
    run = _calculate(company_a, _open_run(company_a, "2026-06-01", [e]))
    _approve_and_lock(company_a, run)
    # raise to 7.2 lakh from 1 June, after June was paid
    company_a.post(f"/payroll/salaries/{e['uuid']}/assign",
                   json={"structure_uuid": _structure(company_a)["uuid"], "annual_ctc": 720000, "effective_from": "2026-06-01"})
    july = _calculate(company_a, _open_run(company_a, "2026-07-01", [e]))
    slip = _slip(company_a, july, e)
    # new month: basic 30,000, HRA 12,000, LTA 2,500, special 13,700 -> June arrears 5,000 + 2,000 + 2,500 + 500
    arrears = {a["code"]: float(a["amount"]) for a in slip["arrears"]}
    assert arrears == {"BASIC": 5000, "HRA": 2000, "LTA": 2500, "SPECIAL": 500}
    assert _line(slip, "ARREARS") == 10000 and _line(slip, "BASIC") == 30000
    # calculating again does not pay the arrears twice
    july = _calculate(company_a, july)
    assert _line(_slip(company_a, july, e), "ARREARS") == 10000


def test_esi_coverage_holds_for_the_contribution_period(company_a):
    e = _emp(company_a, "Sunil")
    _assign(company_a, e, 258000, start="2026-04-01", code="WORK")       # about 19,000 gross: covered
    sep = _calculate(company_a, _open_run(company_a, "2026-09-01", [e]))
    assert _line(_slip(company_a, sep, e), "ESI_EE") > 0
    _approve_and_lock(company_a, sep)
    oct_run = _calculate(company_a, _open_run(company_a, "2026-10-01", [e]))
    assert _line(_slip(company_a, oct_run, e), "ESI_EE") > 0
    _approve_and_lock(company_a, oct_run)
    # raise above the ceiling in November: still covered until March (period October–March)
    _assign(company_a, e, 360000, start="2026-11-01", code="WORK")
    nov = _calculate(company_a, _open_run(company_a, "2026-11-01", [e]))
    slip = _slip(company_a, nov, e)
    assert float(slip["gross"]) > 21000 and _line(slip, "ESI_EE") > 0
    assert slip["statutory"]["esi"]["covered"] is True


def test_minimum_wage_blocks_until_acknowledged(company_a):
    e = _emp(company_a, "Bala")
    company_a.put(f"/payroll/profiles/{e['uuid']}", json={"min_wage_state": "DL", "skill": "skilled"})
    company_a.post(f"/payroll/salaries/{e['uuid']}/assign",
                   json={"annual_ctc": 216000, "effective_from": "2026-04-01",
                         "overrides": {"BASIC": 10000, "HRA": 4000, "SPECIAL": 4000}})
    run = _calculate(company_a, _open_run(company_a, "2026-06-01", [e]))
    assert "calc_below_min_wage" in run["blocking"]
    r = company_a.post(f"/payroll/runs/{run['uuid']}/submit")
    assert r.status_code == 400 and "minimum wage" in r.json()["detail"].lower()
    company_a.post(f"/payroll/runs/{run['uuid']}/acknowledge", json={"key": "calc_below_min_wage", "reason": "Revision pending"})
    assert company_a.post(f"/payroll/runs/{run['uuid']}/submit").status_code == 200


def test_bulk_inputs_from_excel(company_a):
    from openpyxl import Workbook
    e = _emp(company_a, "Kiran", employee_code=f"K{uuid.uuid4().hex[:5]}")
    _assign(company_a, e, 600000)
    run = _open_run(company_a, "2026-06-01", [e])
    wb = Workbook()
    ws = wb.active
    ws.append(["employee_code", "kind", "component", "amount", "days", "note"])
    ws.append([e["employee_code"], "earning", "INCENTIVE", 5000, None, "Q1"])
    ws.append([e["employee_code"], "lop_days", None, None, 2, None])
    ws.append(["NOPE", "earning", "INCENTIVE", 1, None, None])
    buf = io.BytesIO()
    wb.save(buf)
    r = company_a.post(f"/payroll/runs/{run['uuid']}/inputs/import",
                       files={"file": ("inputs.xlsx", buf.getvalue(), "application/octet-stream")})
    assert r.json()["imported"] == 2 and len(r.json()["errors"]) == 1
    run = _calculate(company_a, run)
    slip = _slip(company_a, run, e)
    assert _line(slip, "INCENTIVE") == 5000 and slip["lop_days"] == "2.00"
    assert _line(slip, "BASIC") == round(25000 * 28 / 30)


def test_final_settlement_retrenchment(company_a):
    e = _emp(company_a, "Gopal", date_of_joining="2020-01-01")
    _assign(company_a, e, 600000, start="2025-04-01")
    lt = next(t for t in company_a.get("/loans/types").json() if t["code"] == "EMERGENCY")
    loan = company_a.post("/loans", json={"loan_type_uuid": lt["uuid"], "amount": 24000, "instalments": 12,
                                          "employee_uuid": e["uuid"], "start_month": "2026-07-01"}).json()
    _approve_all(company_a, "loan.request")
    company_a.post(f"/loans/{loan['uuid']}/disburse", json={"reference": "NEFT1"})
    r = company_a.post("/payroll/settlements", json={"employee_uuid": e["uuid"], "exit_kind": "retrenchment",
                                                     "last_working_day": "2026-06-30", "inputs": {"leave_days": 10}})
    assert r.status_code == 201, r.text
    s = r.json()
    lines = {x["code"]: float(x["amount"]) for x in s["lines"]}
    # wages 25,000 (basic); 6 years 6 months -> 6 years
    assert lines["GRATUITY_PAY"] == 86538                      # 25,000 x 15/26 x 6
    assert lines["RETRENCH"] == 86538                       # 15 days per year
    assert lines["RESKILL"] == 14423                        # 15 days' wages to the re-skilling fund
    assert lines["LEAVE_ENC"] == 9615                       # 10 days at basic / 26
    assert lines["NOTICE_PAY"] == 48200                     # 30 days' notice not given
    assert lines["LOAN"] == 24000 and lines["SALARY"] == 48200
    assert s["due_date"] == "2026-07-02"                    # 2 working days after Tuesday 30 June
    assert s["countdown"]["state"] == "overdue"
    # approve and pay; the loan is closed by the settlement
    s = company_a.post(f"/payroll/settlements/{s['uuid']}/submit").json()
    for step in (s.get("approval") or {}).get("steps", []):
        if step["status"] == "pending":
            company_a.post(f"/workflow/tasks/{step['uuid']}/decision", json={"decision": "approve"})
    s = company_a.get(f"/payroll/settlements/{s['uuid']}").json()
    assert s["status"] == "approved"
    s = company_a.post(f"/payroll/settlements/{s['uuid']}/pay", json={"reference": "UTR123", "paid_on": "2026-07-02"}).json()
    assert s["status"] == "paid" and s["countdown"]["state"] == "on_time"
    assert company_a.get(f"/loans/{loan['uuid']}").json()["status"] == "closed"
    pdf = company_a.get(f"/payroll/settlements/{s['uuid']}/pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


def test_expense_claim_paid_with_salary(company_a):
    e = _emp(company_a, "Nisha")
    _assign(company_a, e, 600000)
    me = login_as(company_a, e)
    cats = {c["code"]: c for c in me.get("/expenses/categories").json()}
    r = me.post("/expenses/claims", json={"title": "Site visit", "submit": True, "lines": [
        {"category_uuid": cats["FUEL"]["uuid"], "expense_date": "2026-06-05", "distance_km": 50, "vehicle": "car"},
        {"category_uuid": cats["FOOD"]["uuid"], "expense_date": "2026-06-05", "amount": 400, "gstin": "BADGSTIN"}]})
    assert r.status_code == 201, r.text
    claim = r.json()
    assert claim["total"] == "1000.00"                     # 50 km x 12 + 400
    assert any("GSTIN" in f["message"] for f in claim["policy_flags"])
    _approve_all(company_a, "expense.claim")
    assert me.get(f"/expenses/claims/{claim['uuid']}").json()["status"] == "approved"
    run = _calculate(company_a, _open_run(company_a, "2026-06-01", [e]))
    slip = _slip(company_a, run, e)
    assert _line(slip, "REIMB") == 1000
    _approve_and_lock(company_a, run)
    assert me.get(f"/expenses/claims/{claim['uuid']}").json()["status"] == "paid"


def test_loan_instalments_deducted(company_a):
    e = _emp(company_a, "Varun")
    _assign(company_a, e, 600000)
    me = login_as(company_a, e)
    lt = next(t for t in me.get("/loans/types").json() if t["code"] == "PERSONAL")
    pv = me.post("/loans/preview", json={"loan_type_uuid": lt["uuid"], "amount": 60000, "instalments": 6,
                                         "start_month": "2026-06-01"}).json()
    assert len(pv["schedule"]) == 6 and pv["problems"] == []
    loan = me.post("/loans", json={"loan_type_uuid": lt["uuid"], "amount": 60000, "instalments": 6,
                                   "start_month": "2026-06-01", "purpose": "Medical"}).json()
    _approve_all(company_a, "loan.request")
    assert company_a.post(f"/loans/{loan['uuid']}/disburse", json={"reference": "N1"}).json()["status"] == "active"
    run = _calculate(company_a, _open_run(company_a, "2026-06-01", [e]))
    assert _line(_slip(company_a, run, e), "LOAN") == float(pv["emi"])
    _approve_and_lock(company_a, run)
    detail = me.get(f"/loans/{loan['uuid']}").json()
    assert detail["schedule"][0]["status"] == "deducted"
    assert float(detail["outstanding"]) == 60000 - float(detail["schedule"][0]["principal"])


def test_tax_declaration_and_regime(company_a):
    e = _emp(company_a, "Tara")
    _assign(company_a, e, 1500000)
    me = login_as(company_a, e)
    d = me.get("/payroll/me/declaration?fy=2026-27").json()
    assert d["regime"] == "new"
    r = me.put("/payroll/me/declaration?fy=2026-27", json={
        "regime": "old", "items": [{"key": "ppf", "declared": 150000}, {"key": "health_self", "declared": 25000}],
        "hra": {"months": [{"month": "2026-04", "rent": 30000}] * 12, "landlord_name": "X", "metro": True}})
    assert r.status_code == 400 and "PAN" in r.json()["detail"]   # rent above 1 lakh needs the landlord's PAN
    r = me.put("/payroll/me/declaration?fy=2026-27", json={
        "regime": "old", "items": [{"key": "ppf", "declared": 150000}, {"key": "health_self", "declared": 25000}],
        "hra": {"months": [{"month": "2026-04", "rent": 30000}] * 12, "landlord_name": "X", "landlord_pan": "ABCPX1234K",
                "metro": True}, "submit": True})
    assert r.status_code == 200 and r.json()["status"] == "submitted"
    cmp = me.get("/payroll/me/tax/compare?fy=2026-27").json()
    assert set(cmp) >= {"new", "old", "better"}
    assert any(x["key"] == "hra" for x in cmp["old"]["exemptions"])
    ws = me.get("/payroll/me/tax/worksheet?fy=2026-27").json()
    assert ws["worksheet"]["regime"] == "old"
    # HR verifies: partly accepts PPF
    hr = company_a.get(f"/payroll/tax/declarations/{d['uuid']}").json()
    r = company_a.post(f"/payroll/tax/declarations/{hr['uuid']}/verify", json={
        "items": [{"key": "ppf", "status": "partial", "approved": 100000, "comment": "One receipt missing"},
                  {"key": "health_self", "status": "accepted"}], "complete": True})
    out = r.json()
    assert out["status"] == "verified" and out["approved_total"] == "125000.00"


def test_statutory_override_rules(company_a):
    r = company_a.post("/payroll/statutory/overrides", json={"kind": "esi", "effective_from": "2026-04-01",
                                                             "data": {"employer_rate": 3}, "reason": "Try lower"})
    assert r.status_code == 400
    r = company_a.post("/payroll/statutory/overrides", json={"kind": "pt", "scope": "KA", "effective_from": "2026-10-01",
                                                             "data": {"slabs": [{"min": 0, "max": 29999.99, "amount": 0},
                                                                                {"min": 30000, "max": None, "amount": 200}]},
                                                             "reason": "New Karnataka notification"})
    assert r.status_code == 201
    eff = company_a.get("/payroll/statutory/effective?kind=pt&scope=KA&on=2026-11-01").json()["data"]
    assert eff["slabs"][1]["min"] == 30000 and eff["annual_cap"] == 2500
    old = company_a.get("/payroll/statutory/effective?kind=pt&scope=KA&on=2026-06-01").json()["data"]
    assert old["slabs"][1]["min"] == 25000            # earlier months keep the old slabs


def test_payroll_tables_are_isolated(company_a, company_b):
    e = _emp(company_a, "Iris")
    _assign(company_a, e, 600000)
    run = _calculate(company_a, _open_run(company_a, "2026-06-01", [e]))
    slip = _slip(company_a, run, e)
    assert company_b.get(f"/payroll/runs/{run['uuid']}").status_code == 404
    assert company_b.get(f"/payroll/payslips/{slip['uuid']}").status_code == 404
    assert all(r["uuid"] != run["uuid"] for r in company_b.get("/payroll/runs").json()["items"])
    s = _structure(company_a)
    assert company_b.get(f"/payroll/structures/{s['uuid']}").status_code == 404
    assert company_b.get(f"/payroll/salaries/{e['uuid']}").status_code == 404
    assert company_b.post(f"/payroll/runs/{run['uuid']}/lock").status_code == 404
    assert all(x.get("name") != "Iris Kumar" for x in company_b.get("/payroll/salaries").json()["items"])


@pytest.mark.parametrize("path", ["/payroll/salaries", "/payroll/components", "/payroll/dashboard", "/payroll/settlements",
                                  "/payroll/tax/declarations", "/expenses/claims?scope=all", "/loans?scope=all"])
def test_employee_cannot_open_admin_lists(company_a, path):
    me = login_as(company_a, company_a.add_employee("Plain"))
    assert me.get(path).status_code == 403
