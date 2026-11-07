"""The statutory library as shipped: current Indian rates, limits and slabs.

These are only the *seed* values. At start-up they are written to the platform-level
`statutory_rules` table (one row per kind + scope + effective date) when missing; from then
on the platform team edits them on screen (a new version with a start date) and companies
may add their own overrides where the law allows. Nothing in the calculation code reads
this file directly.

Each entry: (kind, scope, effective_from, data, note). `scope` is a state code for state
laws (PT, LWF, minimum wages), the regime for income tax ("new" / "old"), else None.

Values were compiled from the official notifications known at the time of writing and must
be checked by the payroll and labour-law consultant before launch (requirements 2.3);
state Professional Tax, LWF and minimum wages in particular change often.
"""
from __future__ import annotations

FY2025 = "2025-04-01"
FY2026 = "2026-04-01"
LABOUR_CODES = "2025-11-21"

# ── Provident fund (Code on Social Security, 2020; EPF scheme) ──────
EPF = {
    "employee_rate": 12, "employer_rate": 12, "eps_rate": 8.33,
    "wage_ceiling": 15000, "eps_wage_ceiling": 15000, "eps_max": 1250,
    "edli_rate": 0.5, "edli_wage_ceiling": 15000,
    "admin_rate": 0.5, "admin_min": 500, "admin_min_no_members": 75,
    "eps_max_age": 58, "reduced_rate": 10,
    "rounding": "nearest",
}

# ── ESI ─────────────────────────────────────────────────────────────
ESI = {
    "employee_rate": 0.75, "employer_rate": 3.25,
    "wage_ceiling": 21000, "disability_wage_ceiling": 25000,
    "exempt_daily_wage": 176,           # employee share is nil at or below this average daily wage
    "contribution_periods": [[4, 9], [10, 3]],
    "rounding": "up",                   # contributions are rounded up to the next rupee
}

# ── Professional Tax by state ───────────────────────────────────────
# basis monthly: slab on the month's gross; half_yearly: on the half-year's gross, deducted
# in deduct_months; annual: on the year's gross, spread over the months (balance in
# balance_month). gender "female"/"male" limits a slab; feb_amount replaces the amount in February.
def _m(*rows, **kw):
    return {"basis": "monthly", "slabs": [dict(zip(("min", "max", "amount"), r[:3]), **(r[3] if len(r) > 3 else {}))
                                          for r in rows], "annual_cap": 2500, **kw}


PT = {
    "MH": _m((0, 7500, 0, {"gender": "male"}), (7500.01, 10000, 175, {"gender": "male"}),
             (10000.01, None, 200, {"gender": "male", "feb_amount": 300}),
             (0, 25000, 0, {"gender": "female"}), (25000.01, None, 200, {"gender": "female", "feb_amount": 300}),
             due_day=30, return_frequency="monthly"),
    "KA": _m((0, 24999.99, 0), (25000, None, 200, {"feb_amount": 300}), due_day=20, return_frequency="monthly"),
    "AP": _m((0, 15000, 0), (15000.01, 20000, 150), (20000.01, None, 200), due_day=10, return_frequency="monthly"),
    "TS": _m((0, 15000, 0), (15000.01, 20000, 150), (20000.01, None, 200), due_day=10, return_frequency="monthly"),
    "WB": _m((0, 10000, 0), (10000.01, 15000, 110), (15000.01, 25000, 130), (25000.01, 40000, 150),
             (40000.01, None, 200), due_day=21, return_frequency="monthly"),
    "GJ": _m((0, 11999.99, 0), (12000, None, 200), due_day=15, return_frequency="monthly"),
    "AS": _m((0, 15000, 0), (15000.01, 25000, 180), (25000.01, None, 208), due_day=28, return_frequency="monthly"),
    "OD": _m((0, 13304, 0), (13304.01, 25000, 125), (25000.01, None, 200, {"feb_amount": 300}),
             due_day=10, return_frequency="monthly"),
    "SK": _m((0, 20000, 0), (20000.01, 30000, 125), (30000.01, 40000, 150), (40000.01, None, 200),
             due_day=10, return_frequency="monthly"),
    "TR": _m((0, 7500, 0), (7500.01, 15000, 150), (15000.01, None, 208), due_day=10, return_frequency="monthly"),
    "GA": _m((0, 15000, 0), (15000.01, 25000, 150), (25000.01, None, 200), due_day=30, return_frequency="monthly"),
    "PB": _m((0, 20833, 0), (20833.01, None, 200), due_day=30, return_frequency="monthly"),
    "MZ": _m((0, 5000, 0), (5000.01, 8000, 75), (8000.01, 10000, 120), (10000.01, 12000, 150),
             (12000.01, 15000, 180), (15000.01, None, 208), due_day=30, return_frequency="monthly"),
    "NL": _m((0, 4000, 0), (4000.01, 5000, 35), (5000.01, 7000, 75), (7000.01, 9000, 110),
             (9000.01, 12000, 180), (12000.01, None, 208), due_day=30, return_frequency="monthly"),
    "ML": _m((0, 4166, 0), (4166.01, 6250, 16.5), (6250.01, 8333, 25), (8333.01, 12500, 41.5),
             (12500.01, 16666, 62.5), (16666.01, 20833, 83.33), (20833.01, 25000, 104.16), (25000.01, 29166, 125),
             (29166.01, 33333, 150), (33333.01, 37500, 175), (37500.01, 41666, 200), (41666.01, None, 208),
             due_day=30, return_frequency="monthly"),
    "TN": {"basis": "half_yearly", "deduct_months": [9, 3], "annual_cap": 2500, "due_day": 30,
           "return_frequency": "half_yearly",
           "slabs": [{"min": 0, "max": 21000, "amount": 0}, {"min": 21000.01, "max": 30000, "amount": 180},
                     {"min": 30000.01, "max": 45000, "amount": 425}, {"min": 45000.01, "max": 60000, "amount": 930},
                     {"min": 60000.01, "max": 75000, "amount": 1025}, {"min": 75000.01, "max": None, "amount": 1250}]},
    "KL": {"basis": "half_yearly", "deduct_months": [8, 2], "annual_cap": 2500, "due_day": 30,
           "return_frequency": "half_yearly",
           "slabs": [{"min": 0, "max": 11999.99, "amount": 0}, {"min": 12000, "max": 17999.99, "amount": 120},
                     {"min": 18000, "max": 29999.99, "amount": 180}, {"min": 30000, "max": 44999.99, "amount": 300},
                     {"min": 45000, "max": 59999.99, "amount": 450}, {"min": 60000, "max": 74999.99, "amount": 600},
                     {"min": 75000, "max": 99999.99, "amount": 750}, {"min": 100000, "max": 124999.99, "amount": 1000},
                     {"min": 125000, "max": None, "amount": 1250}]},
    "PY": {"basis": "half_yearly", "deduct_months": [9, 3], "annual_cap": 2500, "due_day": 30,
           "return_frequency": "half_yearly",
           "slabs": [{"min": 0, "max": 99999.99, "amount": 0}, {"min": 100000, "max": 200000, "amount": 250},
                     {"min": 200000.01, "max": 300000, "amount": 500}, {"min": 300000.01, "max": 400000, "amount": 750},
                     {"min": 400000.01, "max": 500000, "amount": 1000}, {"min": 500000.01, "max": None, "amount": 1250}]},
    "MP": {"basis": "annual", "balance_month": 3, "annual_cap": 2500, "due_day": 10, "return_frequency": "monthly",
           "slabs": [{"min": 0, "max": 225000, "amount": 0}, {"min": 225000.01, "max": 300000, "amount": 1500},
                     {"min": 300000.01, "max": 400000, "amount": 2000}, {"min": 400000.01, "max": None, "amount": 2500}]},
    "BR": {"basis": "annual", "balance_month": 3, "annual_cap": 2500, "due_day": 30, "return_frequency": "annual",
           "slabs": [{"min": 0, "max": 300000, "amount": 0}, {"min": 300000.01, "max": 500000, "amount": 1000},
                     {"min": 500000.01, "max": 1000000, "amount": 2000}, {"min": 1000000.01, "max": None, "amount": 2500}]},
    "JH": {"basis": "annual", "balance_month": 3, "annual_cap": 2500, "due_day": 30, "return_frequency": "annual",
           "slabs": [{"min": 0, "max": 300000, "amount": 0}, {"min": 300000.01, "max": 500000, "amount": 1200},
                     {"min": 500000.01, "max": 800000, "amount": 1800}, {"min": 800000.01, "max": 1000000, "amount": 2100},
                     {"min": 1000000.01, "max": None, "amount": 2500}]},
    "MN": {"basis": "annual", "balance_month": 3, "annual_cap": 2500, "due_day": 30, "return_frequency": "annual",
           "slabs": [{"min": 0, "max": 50000, "amount": 0}, {"min": 50000.01, "max": 75000, "amount": 1200},
                     {"min": 75000.01, "max": 100000, "amount": 2000}, {"min": 100000.01, "max": None, "amount": 2500}]},
}

# ── Labour Welfare Fund by state ────────────────────────────────────
def _lwf(freq, months, ee, er, due_day=15, **kw):
    return {"frequency": freq, "months": months, "employee": ee, "employer": er, "due_day": due_day, **kw}


LWF = {
    "MH": _lwf("half_yearly", [6, 12], {"amount": 25}, {"amount": 75}, due_day=15),
    "KA": _lwf("annual", [12], {"amount": 50}, {"amount": 100}, due_day=15),
    "TN": _lwf("annual", [12], {"amount": 20}, {"amount": 40}, due_day=31),
    "KL": _lwf("monthly", list(range(1, 13)), {"amount": 50}, {"amount": 50}, due_day=15),
    "GJ": _lwf("half_yearly", [6, 12], {"amount": 6}, {"amount": 12}, due_day=31),
    "HR": _lwf("monthly", list(range(1, 13)), {"rate": 0.2, "max": 34}, {"multiple_of_employee": 2}, due_day=15),
    "PB": _lwf("monthly", list(range(1, 13)), {"amount": 5}, {"amount": 20}, due_day=15),
    "CH": _lwf("monthly", list(range(1, 13)), {"amount": 5}, {"amount": 20}, due_day=15),
    "DL": _lwf("half_yearly", [6, 12], {"amount": 0.75}, {"amount": 2.25}, due_day=15),
    "WB": _lwf("half_yearly", [6, 12], {"amount": 3}, {"amount": 15}, due_day=15),
    "AP": _lwf("annual", [12], {"amount": 30}, {"amount": 70}, due_day=31),
    "TS": _lwf("annual", [12], {"amount": 2}, {"amount": 5}, due_day=31),
    "MP": _lwf("half_yearly", [6, 12], {"amount": 10}, {"amount": 30}, due_day=15),
    "CG": _lwf("half_yearly", [6, 12], {"amount": 15}, {"amount": 45}, due_day=15),
    "OD": _lwf("half_yearly", [6, 12], {"amount": 20}, {"amount": 40}, due_day=15),
    "GA": _lwf("half_yearly", [6, 12], {"amount": 60}, {"amount": 180}, due_day=31),
}

# ── Minimum wages: a representative set (state × zone × skill), monthly rupees ──
# Import the current notification for each state the company works in (Settings >
# Statutory > Minimum wages > Import) before relying on the check.
def _mw(zone_rates: dict) -> dict:
    rates = []
    for zone, skills in zone_rates.items():
        for skill, monthly in skills.items():
            rates.append({"zone": zone, "skill": skill, "monthly": monthly, "daily": round(monthly / 26, 2)})
    return {"rates": rates}


MIN_WAGES = {
    "DL": _mw({"all": {"unskilled": 18456, "semi_skilled": 20371, "skilled": 22411, "highly_skilled": 24356}}),
    "MH": _mw({"zone_1": {"unskilled": 13500, "semi_skilled": 14250, "skilled": 15000, "highly_skilled": 15750},
               "zone_2": {"unskilled": 12800, "semi_skilled": 13500, "skilled": 14250, "highly_skilled": 15000},
               "zone_3": {"unskilled": 12100, "semi_skilled": 12800, "skilled": 13500, "highly_skilled": 14250}}),
    "KA": _mw({"zone_1": {"unskilled": 14500, "semi_skilled": 15400, "skilled": 16300, "highly_skilled": 17300},
               "zone_2": {"unskilled": 13800, "semi_skilled": 14600, "skilled": 15500, "highly_skilled": 16400}}),
    "TN": _mw({"zone_a": {"unskilled": 12200, "semi_skilled": 12900, "skilled": 13700, "highly_skilled": 14500},
               "zone_b": {"unskilled": 11700, "semi_skilled": 12400, "skilled": 13100, "highly_skilled": 13900}}),
    "TS": _mw({"zone_1": {"unskilled": 12700, "semi_skilled": 13600, "skilled": 14500, "highly_skilled": 15400}}),
    "GJ": _mw({"zone_1": {"unskilled": 12400, "semi_skilled": 12900, "skilled": 13500, "highly_skilled": 14000},
               "zone_2": {"unskilled": 12100, "semi_skilled": 12600, "skilled": 13100, "highly_skilled": 13600}}),
    "HR": _mw({"all": {"unskilled": 11275, "semi_skilled": 12425, "skilled": 13700, "highly_skilled": 14350}}),
    "UP": _mw({"all": {"unskilled": 11313, "semi_skilled": 12446, "skilled": 13940, "highly_skilled": 14500}}),
    "WB": _mw({"zone_a": {"unskilled": 9800, "semi_skilled": 10780, "skilled": 11860, "highly_skilled": 13050}}),
    "KL": _mw({"all": {"unskilled": 13000, "semi_skilled": 13600, "skilled": 14300, "highly_skilled": 15000}}),
}
FLOOR_WAGE = {"daily": 178, "monthly": 4628}
SKILLS = ["unskilled", "semi_skilled", "skilled", "highly_skilled"]

# ── Income tax ─────────────────────────────────────────────────────
_NEW_SLABS = [{"upto": 400000, "rate": 0}, {"upto": 800000, "rate": 5}, {"upto": 1200000, "rate": 10},
              {"upto": 1600000, "rate": 15}, {"upto": 2000000, "rate": 20}, {"upto": 2400000, "rate": 25},
              {"upto": None, "rate": 30}]
_OLD_SLABS = [{"upto": 250000, "rate": 0}, {"upto": 500000, "rate": 5}, {"upto": 1000000, "rate": 20},
              {"upto": None, "rate": 30}]


def _tax(regime: str, act: str) -> dict:
    new = regime == "new"
    return {
        "act": act,
        "standard_deduction": 75000 if new else 50000,
        "slabs": _NEW_SLABS if new else _OLD_SLABS,
        "senior_slabs": None if new else [{"upto": 300000, "rate": 0}, {"upto": 500000, "rate": 5},
                                          {"upto": 1000000, "rate": 20}, {"upto": None, "rate": 30}],
        "super_senior_slabs": None if new else [{"upto": 500000, "rate": 0}, {"upto": 1000000, "rate": 20},
                                                {"upto": None, "rate": 30}],
        "rebate": {"income_limit": 1200000, "max_rebate": 60000} if new else {"income_limit": 500000, "max_rebate": 12500},
        "rebate_marginal_relief": new,
        "surcharge": ([{"above": 5000000, "rate": 10}, {"above": 10000000, "rate": 15}, {"above": 20000000, "rate": 25}]
                      if new else
                      [{"above": 5000000, "rate": 10}, {"above": 10000000, "rate": 15}, {"above": 20000000, "rate": 25},
                       {"above": 50000000, "rate": 37}]),
        "cess_rate": 4,
        "no_pan_rate": 20,
        "employer_nps_percent": 14 if new else 10,
        # Deductions that may be claimed in this regime (keys of tax_sections)
        "allowed": (["employer_nps", "standard_deduction"] if new else
                    ["standard_deduction", "professional_tax", "hra", "lta", "80C", "80CCD1B", "80D_self",
                     "80D_parents", "80E", "80G", "80TTA", "24b", "employer_nps"]),
    }


# Deduction sections. `ref_2025` is the matching provision of the Income-tax Act, 2025
# (shown next to the familiar 1961 Act name); check with the tax consultant.
TAX_SECTIONS = {
    "80C": {"label": "Investments (80C): PF, PPF, ELSS, life insurance, tuition fees, home loan principal",
            "cap": 150000, "ref_2025": "s.123"},
    "80CCD1B": {"label": "Own NPS contribution (80CCD(1B))", "cap": 50000, "ref_2025": "s.124"},
    "80D_self": {"label": "Health insurance: self, spouse, children (80D)", "cap": 25000, "cap_senior": 50000,
                 "ref_2025": "s.126"},
    "80D_parents": {"label": "Health insurance: parents (80D)", "cap": 25000, "cap_senior": 50000, "ref_2025": "s.126"},
    "80E": {"label": "Education loan interest (80E)", "cap": None, "ref_2025": "s.129"},
    "80G": {"label": "Donations (80G), qualifying amount", "cap": None, "ref_2025": "s.133"},
    "80TTA": {"label": "Savings account interest (80TTA)", "cap": 10000, "ref_2025": "s.135"},
    "24b": {"label": "Home loan interest, self-occupied (24(b))", "cap": 200000, "ref_2025": "s.22"},
    "hra": {"label": "House rent allowance exemption", "landlord_pan_above": 100000, "metro_percent": 50,
            "non_metro_percent": 40, "ref_2025": "Schedule II"},
    "lta": {"label": "Leave travel allowance (two journeys in four years)", "cap": None, "ref_2025": "Schedule II"},
    "other_income": {"label": "Other income declared", "cap": None},
}

PERQUISITES = {
    "car": {"engine_cc_limit": 1600, "own_small": 8000, "own_large": 10000, "driver": 3000},
    "accommodation": {"population_above_40_lakh": 10, "population_15_to_40_lakh": 7.5, "other": 5},
    "loan": {"reference_rate": 9.25, "exempt_aggregate": 200000},
}
PERQUISITES_2025 = {
    "car": {"engine_cc_limit": 1600, "own_small": 1800, "own_large": 2400, "driver": 900},
    "accommodation": {"population_above_40_lakh": 10, "population_15_to_40_lakh": 7.5, "other": 5},
    "loan": {"reference_rate": 9.25, "exempt_aggregate": 20000},
}

GRATUITY = {
    "days_per_year": 15, "divisor": 26, "min_years": 5, "min_years_fixed_term": 1, "part_year_months": 6,
    "four_years_240_days": True, "max_amount": 2000000, "tax_exempt_limit": 2000000,
    "monthly_provision_percent": 4.81,
}
BONUS = {"min_rate": 8.33, "max_rate": 20, "eligibility_wage": 21000, "calculation_ceiling": 7000,
         "min_working_days": 30, "use_min_wage_if_higher": True}
RETRENCHMENT = {"days_per_year": 15, "reskilling_days": 15, "part_year_months": 6, "divisor": 26, "tax_exempt_limit": 500000}
LEAVE_ENCASHMENT = {"tax_exempt_limit": 2500000}
WAGE_RULE = {"min_wage_share_percent": 50, "max_deduction_percent": 50, "settlement_working_days": 2}

DUE_DATES = {
    "pf": {"day": 15, "label": "PF payment and ECR"},
    "esi": {"day": 15, "label": "ESI contribution"},
    "tds": {"day": 7, "march_day": 30, "label": "TDS deposit"},
    "salary": {"day": 7, "label": "Salary payment (Code on Wages)"},
    "tds_return": {"quarters": {"1": "07-31", "2": "10-31", "3": "01-31", "4": "05-31"}, "label": "Quarterly TDS return"},
    "certificate": {"date": "06-15", "label": "Annual tax certificate to employees"},
}
FORMS_2025 = {"certificate": "Form 16", "quarterly_return": "Form 24Q"}
FORMS_2026 = {"certificate": "Form 16 (Form 130)", "quarterly_return": "Form 24Q (Form 138)"}


def library() -> list[tuple[str, str | None, str, dict, str]]:
    out: list[tuple[str, str | None, str, dict, str]] = [
        ("epf", None, FY2025, EPF, "EPF scheme rates and the ₹15,000 wage ceiling"),
        ("esi", None, FY2025, ESI, "ESI rates; ₹21,000 ceiling (₹25,000 for persons with disability)"),
        ("income_tax", "new", FY2025, _tax("new", "Income-tax Act, 1961"), "New regime, Finance Act 2025"),
        ("income_tax", "old", FY2025, _tax("old", "Income-tax Act, 1961"), "Old regime"),
        ("income_tax", "new", FY2026, _tax("new", "Income-tax Act, 2025"), "New regime under the Income-tax Act, 2025"),
        ("income_tax", "old", FY2026, _tax("old", "Income-tax Act, 2025"), "Old regime under the Income-tax Act, 2025"),
        ("tax_sections", None, FY2025, TAX_SECTIONS, "Deduction sections and limits"),
        ("perquisites", None, FY2025, PERQUISITES_2025, "Perquisite values (Rule 3)"),
        ("perquisites", None, FY2026, PERQUISITES, "Perquisite values under the Income-tax Rules, 2026"),
        ("gratuity", None, FY2025, {**GRATUITY, "min_years_fixed_term": 5}, "Payment of Gratuity Act, 1972"),
        ("bonus", None, FY2025, BONUS, "Payment of Bonus Act, 1965"),
        ("retrenchment", None, FY2025, {**RETRENCHMENT, "reskilling_days": 0}, "Industrial Disputes Act, 1947"),
        ("wage_rule", None, FY2025, {**WAGE_RULE, "min_wage_share_percent": 0}, "Before the Labour Codes: no 50% wage rule"),
        ("gratuity", None, LABOUR_CODES, GRATUITY, "Code on Social Security: 5 years (1 for fixed-term), 15/26"),
        ("bonus", None, LABOUR_CODES, BONUS, "Code on Wages: 8.33% to 20%"),
        ("retrenchment", None, LABOUR_CODES, RETRENCHMENT, "IR Code: 15 days per year plus re-skilling fund"),
        ("leave_encashment", None, FY2025, LEAVE_ENCASHMENT, "Tax-free leave encashment on retirement"),
        ("wage_rule", None, LABOUR_CODES, WAGE_RULE, "Code on Wages: 50% wages, 50% deductions, 2-day settlement"),
        ("floor_wage", None, FY2025, FLOOR_WAGE, "National floor wage"),
        ("due_dates", None, FY2025, DUE_DATES, "Statutory due dates"),
        ("forms", None, FY2025, FORMS_2025, "Form names under the Income-tax Act, 1961"),
        ("forms", None, FY2026, FORMS_2026, "Form names under the Income-tax Act, 2025"),
    ]
    out += [("pt", code, FY2025, data, f"Professional Tax, {code}") for code, data in PT.items()]
    out += [("lwf", code, "2025-01-01", data, f"Labour Welfare Fund, {code}") for code, data in LWF.items()]
    out += [("min_wage", code, FY2025, data, f"Minimum wages, {code} (sample, import the current notification)")
            for code, data in MIN_WAGES.items()]
    return out


# Fields a company override may never set below the library value (law is the floor).
FLOOR_FIELDS = {
    "epf": ["employee_rate", "employer_rate"],
    "esi": ["employee_rate", "employer_rate", "wage_ceiling", "disability_wage_ceiling"],
    "gratuity": ["days_per_year", "max_amount"],
    "bonus": ["min_rate"],
    "retrenchment": ["days_per_year", "reskilling_days"],
}
# Fields a company override may never set above the library value.
CEILING_FIELDS = {
    "gratuity": ["divisor", "min_years", "min_years_fixed_term"],
    "wage_rule": ["max_deduction_percent", "settlement_working_days"],
    "bonus": ["min_working_days"],
}
# Kinds a company may never override (tax law is not a company choice).
NO_OVERRIDE = {"income_tax", "tax_sections", "forms", "floor_wage", "leave_encashment"}
