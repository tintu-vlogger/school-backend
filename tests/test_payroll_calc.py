"""Worked examples for the statutory and payroll arithmetic (no database).

Every expected figure is worked out by hand from the rule in the comment, using the
shipped statutory library values.
"""
from datetime import date
from decimal import Decimal as D

import pytest

from modules.payroll import calc, engine, statutory_data as lib, taxcalc
from modules.payroll.statutory import deep_merge, validate_override

EPF, ESI, PT, LWF = lib.EPF, lib.ESI, lib.PT, lib.LWF
NEW, OLD = lib._tax("new", "x"), lib._tax("old", "x")


# ── PF ──────────────────────────────────────────────────────────────

def test_pf_restricted_to_ceiling():
    # 12% of 15,000 = 1,800 each side; EPS 8.33% of 15,000 = 1,249.5 -> 1,250 (max); EPF ER = 1,800 - 1,250
    r = calc.pf(30000, EPF)
    assert (r.epf_wage, r.employee, r.employer_eps, r.employer_epf) == (15000, 1800, 1250, 550)
    assert (r.edli_wage, r.edli, r.admin) == (15000, 75, 75)


def test_pf_unrestricted_actual_wage():
    # 12% of 30,000 = 3,600 both sides; EPS still capped at 1,250 on the 15,000 EPS ceiling
    r = calc.pf(30000, EPF, employee_basis="actual", employer_basis="actual")
    assert (r.employee, r.employer_eps, r.employer_epf, r.eps_wage) == (3600, 1250, 2350, 15000)
    assert r.admin == 150  # 0.5% of 30,000


def test_pf_below_ceiling():
    # 12% of 12,000 = 1,440; EPS 8.33% = 999.6 -> 1,000; EPF ER 440
    r = calc.pf(12000, EPF)
    assert (r.employee, r.employer_eps, r.employer_epf) == (1440, 1000, 440)


def test_pf_age_58_no_pension():
    assert calc.eps_eligible(58, EPF) is False and calc.eps_eligible(57, EPF) is True
    r = calc.pf(20000, EPF, eps_eligible=False)
    assert (r.employer_eps, r.employer_epf, r.eps_wage) == (0, 1800, 0)


def test_pf_vpf_and_not_applicable():
    r = calc.pf(20000, EPF, vpf_rate=10)
    assert r.vpf == 1500  # 10% of 15,000
    assert calc.pf(20000, EPF, applicable=False).employee == 0


def test_pf_admin_minimum():
    assert calc.pf_admin_charges(D("120"), 2, EPF) == 500
    assert calc.pf_admin_charges(D("900"), 30, EPF) == 900


# ── ESI ─────────────────────────────────────────────────────────────

def test_esi_rates_rounded_up():
    # 0.75% of 20,000 = 150; 3.25% = 650
    assert calc.esi(20000, ESI, covered=True, days_paid=30) == (150, 650)
    # 0.75% of 18,333 = 137.4975 -> 138 (always rounded up); 3.25% = 595.82 -> 596
    assert calc.esi(18333, ESI, covered=True, days_paid=30) == (138, 596)


def test_esi_thresholds_and_disability():
    assert calc.esi_covered(21000, ESI) is True
    assert calc.esi_covered(21001, ESI) is False
    assert calc.esi_covered(24000, ESI, disabled=True) is True
    assert calc.esi_covered(25001, ESI, disabled=True) is False


def test_esi_contribution_periods():
    assert calc.esi_period(date(2026, 2, 10), ESI) == (date(2025, 10, 1), date(2026, 3, 31))
    assert calc.esi_period(date(2026, 4, 1), ESI) == (date(2026, 4, 1), date(2026, 9, 30))
    assert calc.esi_period(date(2026, 11, 7), ESI) == (date(2026, 10, 1), date(2027, 3, 31))


def test_esi_low_daily_wage_employee_exempt():
    # 4,000 over 30 days = 133/day, at or below 176: the employee pays nothing, the employer 3.25% = 130
    assert calc.esi(4000, ESI, covered=True, days_paid=30) == (0, 130)


def test_esi_not_covered_pays_nothing():
    assert calc.esi(25000, ESI, covered=False) == (0, 0)


# ── Professional Tax ───────────────────────────────────────────────

def test_pt_maharashtra_february_and_gender():
    mh = PT["MH"]
    assert calc.professional_tax(mh, month=5, gross=30000, gender="male") == 200
    assert calc.professional_tax(mh, month=2, gross=30000, gender="male") == 300   # February amount
    assert calc.professional_tax(mh, month=5, gross=9000, gender="male") == 175
    assert calc.professional_tax(mh, month=5, gross=7000, gender="male") == 0
    assert calc.professional_tax(mh, month=5, gross=20000, gender="female") == 0   # women up to 25,000 pay nothing
    assert calc.professional_tax(mh, month=2, gross=30000, gender="female") == 300


def test_pt_karnataka():
    ka = PT["KA"]
    assert calc.professional_tax(ka, month=6, gross=24999, gender="male") == 0
    assert calc.professional_tax(ka, month=6, gross=25000, gender="male") == 200
    assert calc.professional_tax(ka, month=2, gross=40000, gender="male") == 300
    # 11 x 200 + 300 = 2,500 for the year


def test_pt_tamil_nadu_half_yearly():
    tn = PT["TN"]
    assert calc.professional_tax(tn, month=8, gross=20000, half_year_gross=120000) == 0   # not a deduction month
    assert calc.professional_tax(tn, month=9, gross=20000, half_year_gross=120000) == 1250
    assert calc.professional_tax(tn, month=3, gross=6000, half_year_gross=36000) == 425
    assert calc.professional_tax(tn, month=9, gross=3000, half_year_gross=18000) == 0


def test_pt_annual_cap_and_annual_states():
    assert calc.professional_tax(PT["MH"], month=2, gross=30000, gender="male", paid_this_year=2400) == 100
    mp = PT["MP"]  # 3.5 lakh a year -> 2,000: 166 a month, March gets the balance
    assert calc.professional_tax(mp, month=6, gross=0, annual_gross=350000) == 166
    assert calc.professional_tax(mp, month=3, gross=0, annual_gross=350000, paid_this_year=166 * 11) == 174
    assert calc.professional_tax(None, month=5, gross=50000) == 0


# ── LWF ─────────────────────────────────────────────────────────────

def test_lwf_by_state_and_month():
    assert calc.lwf(LWF["MH"], month=6, gross=30000) == (25, 75)
    assert calc.lwf(LWF["MH"], month=7, gross=30000) == (0, 0)
    assert calc.lwf(LWF["MH"], month=12, gross=30000) == (25, 75)
    assert calc.lwf(LWF["KA"], month=12, gross=30000) == (50, 100)
    # Haryana: 0.2% of 30,000 = 60, capped at 34; employer twice the employee
    assert calc.lwf(LWF["HR"], month=4, gross=30000) == (34, 68)
    assert calc.lwf(LWF["HR"], month=4, gross=10000) == (20, 40)


# ── Income tax ─────────────────────────────────────────────────────

def test_new_regime_rebate_up_to_12_lakh():
    r = calc.income_tax(1200000, NEW)
    assert (r["tax_on_income"], r["rebate"], r["total"]) == ("60000.00", "60000.00", "0.00")


def test_new_regime_marginal_relief_above_12_lakh():
    # Slab tax on 12,10,000 = 61,500 but tax cannot exceed the 10,000 above 12 lakh; cess 4% -> 10,400
    r = calc.income_tax(1210000, NEW)
    assert (r["rebate_marginal_relief"], r["total"]) == ("51500.00", "10400.00")
    # At 12,75,000 the slab tax (71,250) is already below the excess (75,000): no relief
    r = calc.income_tax(1275000, NEW)
    assert (r["rebate_marginal_relief"], r["total"]) == ("0.00", "74100.00")


def test_old_regime_rebate_and_slabs():
    assert calc.income_tax(500000, OLD)["total"] == "0.00"
    # 12,500 + 20% of 5 lakh = 1,12,500 + 4% cess = 1,17,000
    assert calc.income_tax(1000000, OLD)["total"] == "117000.00"
    # Senior citizen exemption up to 3 lakh: 10,000 + 20% of 5 lakh -> 1,10,000 + cess = 1,14,400
    assert calc.income_tax(1000000, OLD, age=62)["total"] == "114400.00"


def test_surcharge_with_marginal_relief():
    # 51 lakh: slab tax 11,10,000; 10% surcharge 1,11,000, limited so tax + surcharge rises by at most the
    # 1 lakh above 50 lakh (10,80,000 + 1,00,000): surcharge 70,000; cess 4% on 11,80,000 = 47,200
    r = calc.income_tax(5100000, NEW)
    assert (r["surcharge"], r["surcharge_marginal_relief"], r["total"]) == ("70000.00", "41000.00", "1227200.00")


def test_hra_exemption_least_of_three():
    sec = lib.TAX_SECTIONS["hra"]
    v = calc.hra_exemption(hra_received=240000, rent_paid=300000, basic_da=600000, metro=True, section=sec)
    assert v == 240000
    v = calc.hra_exemption(hra_received=240000, rent_paid=120000, basic_da=600000, metro=False, section=sec)
    assert v == 60000  # rent - 10% of salary


def _tax_inputs(**kw):
    base = dict(regime="new", regime_rules=NEW, sections=lib.TAX_SECTIONS, months_left=12)
    base.update(kw)
    return taxcalc.TaxInputs(**base)


def test_monthly_tds_spread_over_the_year():
    # 18 lakh a year: 17,25,000 after standard deduction -> 1,45,000 + cess 5,800 = 1,50,800; / 12 = 12,567
    t = _tax_inputs(regular_taxable=D(150000), future_taxable=D(150000))
    out = taxcalc.monthly_tds(t)
    assert out["worksheet"]["annual_tax"] == "150800.00"
    assert out["tds"] == "12567.00"


def test_one_time_payment_taxed_in_its_month():
    t = _tax_inputs(regular_taxable=D(150000), future_taxable=D(150000), one_time_taxable=D(100000))
    out = taxcalc.monthly_tds(t)
    # bonus of 1 lakh falls in the 20% slab: 20,000 + 4% = 20,800 deducted now in full
    assert out["one_time"] == "20800.00"
    assert out["regular"] == "12567.00"


def test_old_vs_new_comparison():
    t = _tax_inputs(regular_taxable=D(100000), future_taxable=D(100000), regime="old", regime_rules=OLD,
                    deductions={"80C": D(150000), "80D_self": D(25000)}, ytd_pf=D(0), pf=D(0))
    res = taxcalc.compare(t, {"new": NEW, "old": OLD})
    # new: 12 lakh - 75,000 = 11,25,000 -> rebate -> 0
    assert res["new"]["annual_tax"] == "0.00"
    assert D(res["old"]["annual_tax"]) > 0 and res["better"] == "new"


def test_higher_tds_without_pan():
    t = _tax_inputs(regular_taxable=D(50000), future_taxable=D(50000), has_pan=False)
    ws = taxcalc.annual_worksheet(t)
    # 6 lakh - 75,000 = 5,25,000; normal tax is nil after rebate, but without PAN 20% applies
    assert ws["annual_tax"] == "105000.00" and ws["no_pan_rate_applied"]


# ── Code on Wages 50% rule ─────────────────────────────────────────

def test_wage_rule_add_back():
    r = calc.wage_rule(40000, 60000)
    assert (r["add_back"], r["wages"], r["compliant"]) == ("10000.00", "50000.00", False)
    assert calc.wage_rule(50000, 50000)["add_back"] == "0.00"


# ── Engine: proration, LOP, revisions, add-back into PF ────────────

COMPS = {
    "BASIC": {"code": "BASIC", "name": "Basic", "kind": "earning", "wage_class": "wage", "pf_wage": True, "esi_wage": True,
              "prorate": True, "rounding": "nearest", "taxability": "taxable"},
    "HRA": {"code": "HRA", "name": "HRA", "kind": "earning", "wage_class": "exclusion", "pf_wage": False, "esi_wage": True,
            "prorate": True, "rounding": "nearest", "taxability": "taxable"},
    "SPECIAL": {"code": "SPECIAL", "name": "Special", "kind": "earning", "wage_class": "exclusion", "pf_wage": False,
                "esi_wage": True, "prorate": True, "rounding": "nearest", "taxability": "taxable"},
    "PHONE": {"code": "PHONE", "name": "Phone", "kind": "reimbursement", "wage_class": "none", "pf_wage": False,
              "esi_wage": False, "prorate": False, "rounding": "nearest", "taxability": "partly"},
}
RULES = {"epf": EPF, "esi": ESI, "gratuity": lib.GRATUITY, "wage_rule": lib.WAGE_RULE}


def _inp(amounts, start=date(2026, 6, 1), end=date(2026, 6, 30), **kw):
    seg = engine.Segment(start=date(2025, 4, 1), end=date(2030, 1, 1), amounts={k: D(v) for k, v in amounts.items()})
    return engine.EmployeeInput(employee_id=1, period_start=start, period_end=end, segments=kw.pop("segments", [seg]),
                                comps=COMPS, rules=RULES, settings={"pf": {"apply_wage_rule": True}}, **kw)


def _line(res, code):
    return next((D(x["amount"]) for x in res["earnings"] + res["deductions"] + res["employer"] if x["code"] == code), D(0))


def test_proration_with_lop():
    res = engine.compute(_inp({"BASIC": 30000, "HRA": 15000}, lop_days=D(3)))
    assert _line(res, "BASIC") == 27000 and _line(res, "HRA") == 13500
    assert res["payable_days"] == "27.00"


def test_joiner_paid_for_days_worked():
    res = engine.compute(_inp({"BASIC": 30000, "PHONE": 2000}, join_date=date(2026, 6, 16)))
    assert _line(res, "BASIC") == 15000          # 15 of 30 days
    assert _line(res, "PHONE") == 1000           # not cut for LOP, but only for the days employed


def test_leaver_and_fixed_30_basis():
    res = engine.compute(_inp({"BASIC": 31000}, start=date(2026, 7, 1), end=date(2026, 7, 31), lop_days=D(1),
                              basis="fixed_30"))
    assert _line(res, "BASIC") == 29967          # 29/30 of 31,000 = 29,966.67
    res = engine.compute(_inp({"BASIC": 30000}, exit_date=date(2026, 6, 10)))
    assert _line(res, "BASIC") == 10000


def test_mid_month_revision_split():
    segs = [engine.Segment(date(2025, 1, 1), date(2026, 6, 15), {"BASIC": D(30000)}),
            engine.Segment(date(2026, 6, 16), date(2030, 1, 1), {"BASIC": D(36000)})]
    res = engine.compute(_inp({}, segments=segs))
    assert _line(res, "BASIC") == 33000          # 15,000 + 18,000


def test_wage_rule_add_back_feeds_pf():
    # basic 10,000 of 30,000: add back 5,000 -> PF wage 15,000 -> employee PF 1,800 (not 1,200)
    res = engine.compute(_inp({"BASIC": 10000, "SPECIAL": 20000}))
    assert res["wage_rule"]["add_back"] == "5000.00"
    assert _line(res, "PF_EE") == 1800


def test_negative_net_and_deduction_limit_flagged():
    res = engine.compute(_inp({"BASIC": 20000}, deductions=[{"code": "OTHER_DED", "amount": D(25000)}]))
    codes = {w["code"] for w in res["warnings"]}
    assert "negative_net" in codes and "deductions_over_limit" in codes


def test_below_minimum_wage_flagged():
    res = engine.compute(_inp({"BASIC": 9000}, min_wage_monthly=D(12000)))
    assert any(w["code"] == "below_min_wage" and w["level"] == "block" for w in res["warnings"])


def test_esi_and_pt_in_engine():
    res = engine.compute(_inp({"BASIC": 12000, "HRA": 6000}, esi_covered=True, pt_rules=PT["KA"],
                              profile={"pt_state": "KA"}))
    assert _line(res, "ESI_EE") == 135 and _line(res, "ESI_ER") == 585   # 0.75% / 3.25% of 18,000
    assert _line(res, "PT") == 0                                          # below 25,000 in Karnataka


# ── Gratuity, bonus, retrenchment ──────────────────────────────────

def test_gratuity_five_year_rule_and_amount():
    g = calc.gratuity(30000, date(2020, 1, 1), date(2024, 6, 30), lib.GRATUITY)
    assert g["eligible"] is False                                     # 4 years 6 months
    g = calc.gratuity(30000, date(2020, 1, 1), date(2024, 8, 31), lib.GRATUITY)
    assert g["eligible"] is True and g["years_counted"] == 5          # 4 years 8 months: 240 days in the fifth year
    g = calc.gratuity(30000, date(2019, 1, 1), date(2024, 7, 31), lib.GRATUITY)
    # 5 years 7 months -> 6 years; 30,000 x 15/26 x 6 = 1,03,846
    assert (g["years_counted"], g["amount"]) == (6, "103846.00")


def test_gratuity_fixed_term_death_and_cap():
    g = calc.gratuity(30000, date(2025, 1, 1), date(2026, 2, 28), lib.GRATUITY, fixed_term=True)
    assert g["eligible"] and g["years_counted"] == 1 and g["amount"] == "17308.00"
    assert calc.gratuity(30000, date(2025, 1, 1), date(2025, 9, 30), lib.GRATUITY, death_or_disablement=True)["eligible"]
    g = calc.gratuity(500000, date(1990, 1, 1), date(2026, 1, 1), lib.GRATUITY)
    assert g["amount"] == "2000000.00" and g["taxable"] == "0.00"


def test_statutory_bonus():
    months = [{"wage": 15000, "days_worked": 30}] * 12
    b = calc.bonus(months, lib.BONUS, rate=D("8.33"))
    assert b["eligible"] and b["amount"] == "6997.00"           # 8.33% of 7,000 x 12
    assert calc.bonus(months, lib.BONUS, rate=D("8.33"), min_wage_monthly=12000)["amount"] == "11995.00"
    assert calc.bonus([{"wage": 25000, "days_worked": 30}] * 12, lib.BONUS, rate=20)["eligible"] is False
    assert calc.bonus(months, lib.BONUS, rate=30)["rate"] == "20.00"  # capped at 20%


def test_retrenchment_and_reskilling():
    r = calc.retrenchment(26000, date(2018, 1, 1), date(2026, 3, 31), lib.RETRENCHMENT)
    # 8 years 3 months -> 8 years; 1,000 a day x 15 x 8 = 1,20,000; re-skilling 15 days = 15,000
    assert (r["years_counted"], r["compensation"], r["reskilling_fund"]) == (8, "120000.00", "15000.00")


# ── Loans, deadlines and helpers ───────────────────────────────────

def test_emi_schedule_reducing_and_flat():
    rows = calc.emi_schedule(100000, 12, 12, date(2026, 1, 1))
    assert rows[0]["emi"] == 8885 and sum(r["principal"] for r in rows) == 100000
    flat = calc.emi_schedule(12000, 0, 6, date(2026, 1, 1))
    assert all(r["emi"] == 2000 for r in flat)


def test_loan_perquisite():
    rules = lib.PERQUISITES["loan"]
    assert calc.loan_perquisite(150000, 0, rules) == 0                  # within the 2 lakh exemption
    assert calc.loan_perquisite(300000, 5, rules) == 1063               # 3 lakh x (9.25 - 5)% / 12
    assert calc.loan_perquisite(300000, 0, rules, medical=True) == 0


def test_settlement_deadline_two_working_days():
    weekdays = lambda d: d.weekday() < 5  # noqa: E731
    assert calc.add_working_days(date(2026, 11, 6), 2, weekdays) == date(2026, 11, 10)   # Friday -> Tuesday
    assert calc.add_working_days(date(2026, 11, 9), 2, weekdays) == date(2026, 11, 11)


def test_helpers():
    assert calc.fy_of(date(2026, 3, 31)) == "2025-26" and calc.fy_of(date(2026, 4, 1)) == "2026-27"
    assert calc.months_left_in_fy(date(2026, 11, 1)) == 5
    assert calc.amount_in_words(125000) == "One lakh twenty-five thousand rupees only"


# ── Statutory library rules ────────────────────────────────────────

def test_override_cannot_go_below_law():
    assert validate_override("esi", ESI, {"employer_rate": 3})
    assert not validate_override("esi", ESI, {"employer_rate": 4})
    assert validate_override("income_tax", NEW, {"cess_rate": 0})
    assert validate_override("gratuity", lib.GRATUITY, {"min_years": 6})
    assert deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"b": 5}}) == {"a": {"b": 5, "c": 2}}


@pytest.mark.parametrize("state", sorted(PT))
def test_every_pt_state_respects_the_constitutional_cap(state):
    rules = PT[state]
    paid = D(0)
    for m in [4, 5, 6, 7, 8, 9, 10, 11, 12, 1, 2, 3]:
        paid += calc.professional_tax(rules, month=m, gross=500000, gender="male", half_year_gross=3000000,
                                      annual_gross=6000000, paid_this_year=paid)
    assert paid <= 2500
