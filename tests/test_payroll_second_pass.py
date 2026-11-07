"""Payroll second pass: full and final as an off-cycle pay run, the flexible benefit plan,
pay-review revisions with arrears, the new payroll reports (pay equity maths), Form 16
signing, group insurance premiums deducted in payroll, and tenant isolation of the new tables.
"""
import csv
import io
import uuid
from datetime import date
from decimal import Decimal

from tests.conftest import login_as
from tests.test_payroll_api import _approve_and_lock, _assign, _calculate, _emp, _line, _open_run, _slip, _structure


def _approve_settlement(company, s):
    s = company.post(f"/payroll/settlements/{s['uuid']}/submit").json()
    for step in (s.get("approval") or {}).get("steps", []):
        if step["status"] == "pending":
            assert company.post(f"/workflow/tasks/{step['uuid']}/decision", json={"decision": "approve"}).status_code == 200
    return company.get(f"/payroll/settlements/{s['uuid']}").json()


# ── 1. Full and final as an off-cycle run ──────────────────────────

def test_settlement_becomes_an_off_cycle_run_with_bank_file(company_a):
    e = _emp(company_a, "Farhan", date_of_joining="2020-01-01")
    company_a.put(f"/payroll/profiles/{e['uuid']}", json={"pt_state": "KA"})
    _assign(company_a, e, 600000, start="2025-04-01")
    s = company_a.post("/payroll/settlements", json={"employee_uuid": e["uuid"], "exit_kind": "resignation",
                                                     "last_working_day": "2026-06-30", "resignation_date": "2026-05-31",
                                                     "inputs": {"leave_days": 10}}).json()
    lines = {x["code"]: float(x["amount"]) for x in s["lines"]}
    assert lines["SALARY"] == 48200 and lines["GRATUITY_PAY"] == 86538 and lines["LEAVE_ENC"] == 9615
    assert lines["PF_EE"] == 1800 and lines["PT"] == 200      # statutory deductions of the final month
    assert s["due_date"] == "2026-07-02"
    s = _approve_settlement(company_a, s)
    assert s["status"] == "approved" and s["pay_run"] and s["pay_run"]["status"] == "approved"
    run = company_a.get(f"/payroll/runs/{s['pay_run']['uuid']}").json()
    assert run["kind"] == "settlement" and run["pay_date"] == s["due_date"] and run["settlement"]["uuid"] == s["uuid"]
    assert run["totals"]["employees"] == 1
    slip = _slip(company_a, run, e)
    # the payslip carries the settlement's totals and its statutory effects
    assert slip["net"] == s["net"] and slip["gross"] == s["total_earnings"]
    assert _line(slip, "BASIC") == 25000 and _line(slip, "GRATUITY_PAY") == 86538 and _line(slip, "PF_EE") == 1800
    assert _line(slip, "PF_ER") == 550 and _line(slip, "EPS_ER") == 1250       # employer PF for the final month
    assert _line(slip, "TDS") == float(s["tax"])
    assert float(run["totals"]["pf_employee"]) == 1800 and float(run["totals"]["tds"]) == float(s["tax"])
    # a regular June run leaves the leaver out: the settlement pays June
    june = company_a.post("/payroll/runs", json={"month": "2026-06-01"}).json()
    assert all(x["employee_id"] != e["id"] for x in company_a.get(f"/payroll/runs/{june['uuid']}/payslips").json()["items"])
    # lock, bank file with one row, payslip-style statement
    r = company_a.post(f"/payroll/runs/{run['uuid']}/lock")
    assert r.status_code == 200, r.text
    bank = company_a.get(f"/payroll/runs/{run['uuid']}/files/bank?format=generic_neft")
    assert bank.status_code == 200
    rows = list(csv.reader(io.StringIO(bank.content.decode())))
    assert len(rows) == 2 and rows[1][2] == e["account_number"] and Decimal(rows[1][4]) == Decimal(s["net"])
    assert "settlement" in rows[1][7].lower()
    ecr = company_a.get(f"/payroll/runs/{run['uuid']}/files/ecr")
    assert ecr.status_code == 200 and e["uan"] in ecr.text
    pdf = company_a.get(f"/payroll/settlements/{s['uuid']}/pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    # paying the run closes the settlement (loans, leave ...) like a normal run
    r = company_a.post(f"/payroll/runs/{run['uuid']}/paid", json={"reference": "UTR-FNF-1"})
    assert r.status_code == 200 and r.json()["status"] == "paid"
    s = company_a.get(f"/payroll/settlements/{s['uuid']}").json()
    assert s["status"] == "paid" and s["payment_ref"] == "UTR-FNF-1"


def test_settlement_pay_locks_and_pays_its_run(company_a):
    e = _emp(company_a, "Ishaan", date_of_joining="2024-01-01")
    _assign(company_a, e, 480000, start="2025-04-01")
    s = company_a.post("/payroll/settlements", json={"employee_uuid": e["uuid"], "exit_kind": "termination",
                                                     "last_working_day": "2026-06-15"}).json()
    assert not any(x["code"] == "GRATUITY_PAY" for x in s["lines"])        # under 5 years
    s = _approve_settlement(company_a, s)
    s = company_a.post(f"/payroll/settlements/{s['uuid']}/pay", json={"reference": "UTR9", "paid_on": "2026-06-17"}).json()
    assert s["status"] == "paid" and s["countdown"]["state"] == "on_time"
    run = company_a.get(f"/payroll/runs/{s['pay_run']['uuid']}").json()
    assert run["status"] == "paid"


# ── 2. Flexible benefit plan ───────────────────────────────────────

def _fbp_employee(company, first):
    company.put("/payroll/settings", json={"fbp": {"declaration_from": "04-01", "declaration_to": "03-31"}})
    e = _emp(company, first)
    _assign(company, e, 2400000, code="MGMT")                    # FUEL 3,000 and PHONE 2,000 a month
    return e


def test_fbp_declaration_bills_and_taxability(company_a):
    e = _fbp_employee(company_a, "Kavya")
    me = login_as(company_a, e)
    plan = me.get("/payroll/me/fbp?fy=2026-27").json()
    assert plan["basket"] == "60000.00"
    r = me.put("/payroll/me/fbp?fy=2026-27", json={"allocation": {"FUEL": 30000, "PHONE": 30000}, "submit": True})
    assert r.status_code == 400 and "24,000" in r.json()["detail"]         # phone limit
    r = me.put("/payroll/me/fbp?fy=2026-27", json={"allocation": {"FUEL": 30000, "PHONE": 24000, "MEAL": 6000}, "submit": True})
    assert r.status_code == 200, r.text
    assert r.json()["declaration"]["status"] == "submitted"
    # bills (photo upload from a phone)
    def bill(code, amount, when="2026-05-10"):
        return me.post("/payroll/me/fbp/claims", data={"component_code": code, "bill_date": when, "amount": str(amount),
                                                        "bill_number": f"B{amount}"},
                       files=[("files", ("bill.jpg", b"\xff\xd8\xff fake jpeg", "image/jpeg"))])
    r = bill("MEAL", 7000)
    assert r.status_code == 400                                        # more than the 6,000 allocated
    fuel, meal = bill("FUEL", 10000).json(), bill("MEAL", 4000).json()
    assert fuel["status"] == "submitted"
    assert me.get(f"/payroll/fbp/files/{fuel['file_ids'][0]}").status_code == 200
    # HR verifies
    assert company_a.post(f"/payroll/fbp/claims/{fuel['uuid']}/verify", json={"approved_amount": 10000}).json()["status"] == "approved"
    r = company_a.post(f"/payroll/fbp/claims/{meal['uuid']}/verify", json={"approved_amount": 3000, "comment": "One bill unclear"})
    assert r.json()["status"] == "partly_approved"
    # June pay: nothing flexible paid as salary; bills paid up to what is due so far (3 of 12 months)
    run = _calculate(company_a, _open_run(company_a, "2026-06-01", [e]))
    slip = _slip(company_a, run, e)
    assert slip["statutory"]["fbp_accrued"] == {"FUEL": "3000.00", "PHONE": "2000.00"}
    fuel_line = next(x for x in slip["lines"] if x["code"] == "FUEL")
    meal_line = next(x for x in slip["lines"] if x["code"] == "MEAL")
    assert float(fuel_line["amount"]) == 7500 and fuel_line["kind"] == "reimbursement"   # 30,000 x 3/12
    assert float(meal_line["amount"]) == 1500                                              # 6,000 x 3/12
    # new regime: fuel bills are tax-free, meal cards are not
    inputs = company_a.get(f"/payroll/runs/{run['uuid']}/inputs").json()["items"]
    fb = {i["component_code"]: i for i in inputs if i["source"] == "fbp"}
    assert fb["FUEL"]["days"] in ("0", "0.00") and float(fb["MEAL"]["days"]) == 1500
    salary_taxable = float(slip["gross"]) - 7500 - 1500
    assert float(slip["tax"]["month"]["taxable"]) == salary_taxable + 1500
    _approve_and_lock(company_a, run)
    claims = {c["component_code"]: c for c in me.get("/payroll/me/fbp/claims").json()["items"]}
    assert claims["FUEL"]["paid_amount"] == "7500.00" and claims["FUEL"]["status"] == "approved"
    assert claims["MEAL"]["paid_amount"] == "1500.00"
    summary = me.get("/payroll/me/fbp?fy=2026-27").json()
    assert summary["accrued"] == "5000.00"


def test_fbp_unclaimed_balance_paid_as_taxable_at_year_end(company_a):
    e = _fbp_employee(company_a, "Rohan")
    run = _calculate(company_a, _open_run(company_a, "2027-03-01", [e]))
    slip = _slip(company_a, run, e)
    assert _line(slip, "FUEL") == 0 and _line(slip, "FBP_BAL") == 5000     # March accrual, never claimed
    one_time = next(x for x in slip["lines"] if x["code"] == "FBP_BAL")
    assert one_time["kind"] == "earning"


# ── 3. Increments from pay reviews -> revisions with arrears ──────

def test_pay_review_increment_becomes_revision_with_arrears(company_a):
    from core.context import RequestContext, use_context
    from core.db import tenant_session
    from modules.payroll import service as payroll
    e = _emp(company_a, "Anand")
    _assign(company_a, e, 600000)
    _approve_and_lock(company_a, _calculate(company_a, _open_run(company_a, "2026-06-01", [e])))
    # what the pay review does when the increment takes effect
    db = tenant_session(company_a.id)
    with use_context(RequestContext(company_id=company_a.id, actor_type="system", actor_name="Pay review")):
        payroll.apply_salary_revision(db, company_a.id, e["id"], Decimal("720000"), date(2026, 6, 1), source_ref="proposal-1")
        payroll.apply_salary_revision(db, company_a.id, e["id"], Decimal("720000"), date(2026, 6, 1), source_ref="proposal-1")
        db.commit()
    db.close()
    revs = company_a.get("/payroll/revisions?status=pending").json()
    mine = [r for r in revs["items"] if r["employee_id"] == e["id"]]
    assert len(mine) == 1                                    # the same proposal is queued once
    rev = mine[0]
    assert rev["source"] == "pay_review" and rev["current_ctc"] == "600000.00" and rev["arrears_estimate"] == "10000.00"
    detail = company_a.get(f"/payroll/revisions/{rev['uuid']}").json()
    assert detail["estimate"]["total"] == "10000.00" and len(detail["estimate"]["months"]) == 1
    # nothing changes until payroll applies it
    july = _calculate(company_a, _open_run(company_a, "2026-07-01", [e]))
    assert _line(_slip(company_a, july, e), "BASIC") == 25000
    r = company_a.post(f"/payroll/revisions/{rev['uuid']}/apply", json={})
    assert r.status_code == 200 and r.json()["status"] == "applied"
    july = _calculate(company_a, july)
    slip = _slip(company_a, july, e)
    assert _line(slip, "BASIC") == 30000 and _line(slip, "ARREARS") == 10000
    assert company_a.post(f"/payroll/revisions/{rev['uuid']}/apply", json={}).status_code == 400


# ── 4. Reports: pay equity maths and the standard reports ─────────

def test_pay_equity_maths():
    from modules.payroll.insights import pay_equity
    people = ([{"designation": "Engineer", "grade": "G2", "gender": "Male", "pay": p} for p in (1000000, 1200000)] +
              [{"designation": "Engineer", "grade": "G2", "gender": "Female", "pay": 900000}] +
              [{"designation": "Analyst", "grade": "G1", "gender": g, "pay": p}
               for g, p in (("Male", 600000), ("Female", 590000), ("Female", 600000))] +
              [{"designation": "Driver", "grade": "G0", "gender": "Male", "pay": 300000}])
    rows = {r["designation"]: r for r in pay_equity(people, threshold=5)}
    eng = rows["Engineer"]
    assert eng["average_men"] == 1100000 and eng["average_women"] == 900000
    assert eng["gap_percent"] == 18.2 and eng["flagged"] and eng["flag"].startswith("Review: women")
    assert rows["Analyst"]["gap_percent"] == 0.8 and not rows["Analyst"]["flagged"]      # 600,000 vs 595,000
    assert rows["Analyst"]["median_gap_percent"] == 0.8
    assert rows["Driver"]["flag"] == "Not comparable" and rows["Driver"]["gap_percent"] is None


def test_new_payroll_reports_run(company_a):
    e = _emp(company_a, "Lata")
    _assign(company_a, e, 600000)
    _approve_and_lock(company_a, _calculate(company_a, _open_run(company_a, "2026-06-01", [e])))
    std = {r["key"] for r in company_a.get("/reports/?category=pay").json()["items"]} \
        if isinstance(company_a.get("/reports/?category=pay").json(), dict) else \
        {r["key"] for r in company_a.get("/reports/?category=pay").json()}
    assert {"pay_equity", "cost_budget_department", "payroll_variance", "statutory_liability", "ctc"} <= std
    for source, params in (("pay_equity", {}), ("ctc_breakup", {}), ("payroll_variance", {"fy": "2026-27"}),
                           ("statutory_liability", {"fy": "2026-27"}), ("payroll_cost_budget", {"fy": "2026-27"})):
        r = company_a.post("/reports/preview", json={"source": source, "config": {"params": params}})
        assert r.status_code == 200, (source, r.text)
    liab = company_a.post("/reports/preview", json={"source": "statutory_liability", "config": {"params": {"fy": "2026-27"}}}).json()
    pf = [x for x in liab["rows"] if x["kind"] == "Provident fund" and x["month"] == "2026-06-01"]
    assert pf and pf[0]["liability"] == 3750                 # 1,800 employee + 1,800 employer + EDLI 75 + admin 75
    var = company_a.post("/reports/preview", json={"source": "payroll_variance", "config": {"params": {"fy": "2026-27"}}}).json()
    assert any(x["month"] == "2026-06-01" and x["employees"] >= 1 for x in var["rows"])
    # budgets feed cost against budget
    assert company_a.post("/departments/", json={"department_name": f"Finance {uuid.uuid4().hex[:4]}"}).status_code == 201
    depts = company_a.get("/payroll/budgets?fy=2026-27&dimension=department").json()["items"]
    assert depts
    r = company_a.put("/payroll/budgets", json={"fy": "2026-27", "dimension": "department",
                                                "items": [{"ref_id": depts[0]["ref_id"], "amount": 1200000}]})
    assert r.status_code == 200 and any(x["amount"] == "1200000.00" for x in r.json()["items"])


# ── 5. Form 16 digital signing ─────────────────────────────────────

def _pfx(password: str) -> bytes:
    from modules.payroll import signing
    data, pwd = signing.stub_pfx("Alpha Works Director")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12
    key, cert, _ = pkcs12.load_key_and_certificates(data, pwd.encode())
    return pkcs12.serialize_key_and_certificates(b"dsc", key, cert, None,
                                                 serialization.BestAvailableEncryption(password.encode()))


def test_form16_signing_stub_and_certificate(company_a):
    from modules.payroll import signing
    e = _emp(company_a, "Neha")
    _assign(company_a, e, 1500000)
    _approve_and_lock(company_a, _calculate(company_a, _open_run(company_a, "2026-06-01", [e])))
    info = company_a.get("/payroll/tax/signing").json()
    assert info["mode"] == "stub" and not info["has_certificate"]
    r = company_a.post("/payroll/tax/form16/sign", json={"fy": "2026-27"})
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "stub" and r.json()["signed"] >= 1
    pdf = company_a.get(f"/payroll/tax/form16/{e['uuid']}?fy=2026-27").content
    sigs = signing.signature_info(pdf)
    assert len(sigs) == 1 and sigs[0]["intact"] and "test signature" in sigs[0]["signer"]
    # a real certificate: the password opens it at upload and again at signing, and is never kept
    pfx = _pfx("S3cret-dsc")
    bad = company_a.post("/payroll/tax/signing/certificate", data={"password": "wrong"},
                         files={"file": ("dsc.pfx", pfx, "application/x-pkcs12")})
    assert bad.status_code == 400
    ok = company_a.post("/payroll/tax/signing/certificate", data={"password": "S3cret-dsc", "place": "Bengaluru"},
                        files={"file": ("dsc.pfx", pfx, "application/x-pkcs12")})
    assert ok.status_code == 200 and ok.json()["mode"] == "certificate"
    assert "S3cret-dsc" not in str(company_a.get("/payroll/settings").json())
    assert company_a.post("/payroll/tax/form16/sign", json={"fy": "2026-27"}).status_code == 400
    assert company_a.post("/payroll/tax/form16/sign", json={"fy": "2026-27", "password": "nope"}).status_code == 400
    r = company_a.post("/payroll/tax/form16/sign", json={"fy": "2026-27", "password": "S3cret-dsc",
                                                         "employee_uuids": [e["uuid"]]})
    assert r.status_code == 200 and r.json()["mode"] == "certificate"
    sigs = signing.signature_info(company_a.get(f"/payroll/tax/form16/{e['uuid']}?fy=2026-27").content)
    assert sigs[0]["signer"] == "Alpha Works Director (test signature)" and sigs[0]["intact"]
    assert login_as(company_a, e).get("/payroll/me/form16?fy=2026-27").content.startswith(b"%PDF")


# ── 6. Group insurance: premium deducted in payroll ────────────────

def _plan(company, **extra):
    body = {"name": "Group health", "kind": "gmc", "insurer": "Star Health", "policy_number": "GMC-1",
            "policy_start": "2026-04-01", "policy_end": "2027-03-31", "sum_insured_basis": "fixed", "sum_insured": 500000,
            "tiers": [{"code": "self", "label": "Employee only", "premium": 12000},
                      {"code": "family", "label": "Employee and spouse", "spouse": True, "premium": 18000}],
            "employer_share_type": "percent", "employer_share_value": 50, "deduct_in_payroll": True, **extra}
    r = company.post("/benefits/plans", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_insurance_premium_deducted_in_payroll(company_a):
    e = _emp(company_a, "Pooja")
    _assign(company_a, e, 600000)
    plan = _plan(company_a)
    r = company_a.post("/benefits/enrolments", json={"plan_uuid": plan["uuid"], "employee_uuid": e["uuid"], "tier": "self"})
    assert r.status_code == 201, r.text
    x = r.json()
    assert x["annual_premium"] == "12000.00" and x["employee_share"] == "6000.00" and x["monthly_deduction"] == "500.00"
    assert x["start_date"] == "2026-04-01"
    # a spouse needs the family tier
    r = company_a.post("/benefits/enrolments", json={"plan_uuid": plan["uuid"], "employee_uuid": e["uuid"], "tier": "family",
                                                     "dependent_ids": [999999]})
    assert r.status_code == 400
    run = _calculate(company_a, _open_run(company_a, "2026-06-01", [e]))
    slip = _slip(company_a, run, e)
    assert _line(slip, "INS_PREM") == 500
    assert float(slip["net"]) == 46400 - 500           # no PT state here
    # cover ends mid-month: the deduction is pro-rated
    company_a.post(f"/benefits/enrolments/{x['uuid']}/terminate", json={"end_date": "2026-07-15", "reason": "Left"})
    _approve_and_lock(company_a, run)
    july = _calculate(company_a, _open_run(company_a, "2026-07-01", [e]))
    assert _line(_slip(company_a, july, e), "INS_PREM") == 242            # 500 x 15/31
    # the insurer's monthly file lists the addition (April) and the deletion (July)
    adds = company_a.get(f"/benefits/plans/{plan['uuid']}/insurer-file?month=2026-04-01&format=json").json()
    dels = company_a.get(f"/benefits/plans/{plan['uuid']}/insurer-file?month=2026-07-01&format=json").json()
    assert adds["additions"] == 1 and dels["deletions"] == 1
    xl = company_a.get(f"/benefits/plans/{plan['uuid']}/insurer-file?month=2026-07-01")
    assert xl.status_code == 200 and xl.content[:2] == b"PK"


def test_auto_enrol_salary_multiple_and_my_benefits(company_a):
    e = _emp(company_a, "Sunil", date_of_joining="2026-10-20")
    _assign(company_a, e, 1200000, start="2026-10-20")
    plan = _plan(company_a, name="Term life", kind="gtl", sum_insured_basis="salary_multiple", salary_multiple=3,
                 min_sum_insured=1000000, tiers=[{"code": "self", "label": "Employee", "premium": 2400}],
                 employer_share_value=100, mandatory=True, auto_enrol=True, cover_starts="first_of_next_month")
    out = company_a.post("/benefits/auto-enrol").json()
    assert out["enrolled"] >= 1
    rows = company_a.get(f"/benefits/enrolments?plan={plan['uuid']}").json()["items"]
    mine = next(x for x in rows if x["employee_id"] == e["id"])
    assert mine["sum_insured"] == "3600000.00" and mine["monthly_deduction"] == "0.00" and mine["start_date"] == "2026-11-01"
    me = login_as(company_a, e)
    my = me.get("/benefits/me").json()
    p = next(x for x in my["plans"] if x["plan"]["uuid"] == plan["uuid"])
    assert p["enrolment"]["status"] == "enrolled"
    assert me.post("/benefits/me/waive", json={"plan_uuid": plan["uuid"], "reason": "Have my own"}).status_code == 400
    assert me.get("/benefits/plans").status_code == 403


# ── 7. Isolation and access ────────────────────────────────────────

def test_second_pass_tables_are_isolated(company_a, company_b):
    e = _fbp_employee(company_a, "Tanvi")
    me = login_as(company_a, e)
    me.put("/payroll/me/fbp?fy=2026-27", json={"allocation": {"FUEL": 30000}, "submit": True})
    c = me.post("/payroll/me/fbp/claims", data={"component_code": "FUEL", "bill_date": "2026-05-02", "amount": "500"},
                files=[("files", ("b.pdf", b"%PDF-1.4 fake", "application/pdf"))]).json()
    plan = _plan(company_a)
    r = company_a.post("/payroll/revisions", json={"employee_uuid": e["uuid"], "new_ctc": 2600000, "effective_from": "2026-10-01"})
    assert r.status_code == 201
    rev = r.json()
    assert company_b.get(f"/payroll/fbp/claims/{c['uuid']}").status_code == 404
    assert company_b.post(f"/payroll/fbp/claims/{c['uuid']}/verify", json={"approved_amount": 1}).status_code == 404
    assert company_b.get(f"/payroll/fbp/files/{c['file_ids'][0]}").status_code == 404
    assert company_b.get(f"/payroll/revisions/{rev['uuid']}").status_code == 404
    assert company_b.post(f"/payroll/revisions/{rev['uuid']}/apply", json={}).status_code == 404
    assert company_b.get(f"/benefits/plans/{plan['uuid']}").status_code == 404
    assert all(x["uuid"] != plan["uuid"] for x in company_b.get("/benefits/plans").json())
    assert all(x["uuid"] != c["uuid"] for x in company_b.get("/payroll/fbp/claims").json()["items"])
    # employees cannot verify bills, see others' bills or run revisions
    other = login_as(company_a, company_a.add_employee("Nosy"))
    assert other.get("/payroll/fbp/claims").status_code == 403
    assert other.get(f"/payroll/fbp/files/{c['file_ids'][0]}").status_code == 403
    assert other.post(f"/payroll/revisions/{rev['uuid']}/apply", json={}).status_code == 403
    assert other.post("/payroll/tax/form16/sign", json={"fy": "2026-27"}).status_code == 403
    _ = uuid
