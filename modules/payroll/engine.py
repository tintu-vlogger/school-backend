"""One employee's pay for one period, without the database.

`compute(EmployeeInput)` returns the payslip: earnings (prorated for joiners, leavers,
mid-month revisions and loss of pay), overtime and shift allowance, arrears, one-time
pay, reimbursements, the Code on Wages "wages" with the 50% add-back, PF, ESI, PT, LWF,
loan instalments, other deductions, TDS, net pay, employer contributions and warnings.
The pay-run service gathers the inputs; tests call this directly with worked examples.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from core import expressions
from modules.payroll import calc, taxcalc
from modules.payroll.calc import ZERO, d, money, rnd


@dataclass
class Segment:
    """A salary in force for part (or all) of the period: monthly amounts by component."""
    start: date
    end: date
    amounts: dict[str, Decimal]


@dataclass
class EmployeeInput:
    employee_id: int
    period_start: date
    period_end: date
    segments: list[Segment]
    comps: dict[str, dict]                    # code -> component attributes
    rules: dict                               # epf, esi, gratuity, wage_rule
    settings: dict
    join_date: date | None = None
    exit_date: date | None = None
    lop_days: Decimal = ZERO
    basis: str = "calendar"
    working_days: Decimal | None = None       # in the period, for the working-days basis
    overtime_hours: Decimal = ZERO
    shift_amount: Decimal = ZERO
    earnings: list[dict] = field(default_factory=list)       # [{code, amount, note}] one-time / typed in
    deductions: list[dict] = field(default_factory=list)     # [{code, amount, note}]
    arrears: list[dict] = field(default_factory=list)        # [{code, amount, period}]
    reimbursements: list[dict] = field(default_factory=list)  # [{amount, taxable, ref, code}]
    loans: list[dict] = field(default_factory=list)          # [{code, amount, ref}]
    profile: dict = field(default_factory=dict)
    esi_covered: bool = False
    pt_rules: dict | None = None
    pt_paid_fy: Decimal = ZERO
    pt_half_year_gross: Decimal | None = None   # gross of the half-year so far (half-yearly states)
    pt_annual_gross: Decimal | None = None
    lwf_rules: dict | None = None
    tax: taxcalc.TaxInputs | None = None
    min_wage_monthly: Decimal | None = None
    previous_net: Decimal | None = None
    skip_statutory: bool = False             # off-cycle runs that only pay a one-time amount
    accrue_codes: set = field(default_factory=set)   # flexible-benefit components held back (paid on bills)


def _days(a: date, b: date) -> int:
    return (b - a).days + 1


def _employment(inp: EmployeeInput) -> tuple[date, date] | None:
    start = max(inp.period_start, inp.join_date or inp.period_start)
    end = min(inp.period_end, inp.exit_date or inp.period_end)
    return (start, end) if start <= end else None


def compute(inp: EmployeeInput) -> dict:
    comps = inp.comps
    period_days = _days(inp.period_start, inp.period_end)
    employed = _employment(inp)
    warnings: list[dict] = []
    lines: list[dict] = []

    def comp(code: str) -> dict:
        return comps.get(code) or {"code": code, "name": code, "kind": "earning", "wage_class": "none",
                                   "pf_wage": False, "esi_wage": False, "prorate": False, "rounding": "nearest",
                                   "taxability": "taxable"}

    def add(code: str, amount, *, note: str | None = None, extra: dict | None = None, kind: str | None = None):
        amount = d(amount)
        c = comp(code)
        if amount == 0 and not c.get("show_if_zero"):
            return
        lines.append({"code": code, "name": c.get("name", code), "name_hi": c.get("name_hi"), "kind": kind or c["kind"],
                      "amount": rnd(amount, c.get("rounding", "nearest")), "note": note, **(extra or {})})

    # ── days ────────────────────────────────────────────────────────
    if employed is None:
        eligible_days = 0
    else:
        eligible_days = _days(*employed)
    lop = min(d(inp.lop_days), d(eligible_days))
    basis_days = {"fixed_30": d(30), "fixed_26": d(26)}.get(inp.basis)
    if inp.basis == "working_days" and inp.working_days:
        basis_days = d(inp.working_days)
    if basis_days is None:
        basis_days = d(period_days)
    payable_days = max(d(eligible_days) - lop, ZERO)

    # ── salary earnings, segment by segment ────────────────────────
    earned: dict[str, Decimal] = {}
    full_month: dict[str, Decimal] = {}
    if employed:
        segs = []
        for s in inp.segments:
            a, b = max(s.start, employed[0]), min(s.end, employed[1])
            if a <= b:
                segs.append((s, _days(a, b)))
        total_seg_days = sum(n for _, n in segs) or 1
        for s, n in segs:
            share_full = basis_days * d(n) / d(period_days)          # basis days this segment covers
            lop_share = lop * d(n) / d(total_seg_days)
            factor = calc.proration_factor(basis_days - share_full + lop_share, basis_days)
            employ_factor = d(n) / d(period_days)
            for code, monthly in s.amounts.items():
                c = comp(code)
                if c["kind"] not in ("earning", "reimbursement", "employer"):
                    continue
                f = factor if c.get("prorate", True) else employ_factor
                earned[code] = earned.get(code, ZERO) + d(monthly) * f
        if inp.segments:
            full_month = {k: d(v) for k, v in inp.segments[-1].amounts.items()}
    for code in list(earned):
        earned[code] = rnd(earned[code], comp(code).get("rounding", "nearest"))
    # flexible benefits: earned but not paid now; they build up and are paid against bills
    accrued = {code: earned.pop(code) for code in list(earned) if code in inp.accrue_codes}

    regular_earning_codes = [c for c in earned if comp(c)["kind"] in ("earning", "reimbursement")]
    for code in sorted(regular_earning_codes, key=lambda c: comp(c).get("sort_order", 100)):
        add(code, earned[code])

    # overtime and shift allowance
    ot_amount = ZERO
    if d(inp.overtime_hours) > 0 and not inp.skip_statutory:
        formula = inp.settings.get("overtime_formula") or "round(2 * (basic + da) / 26 / 8 * hours, 2)"
        vars_ = {"basic": full_month.get("BASIC", ZERO), "da": full_month.get("DA", ZERO), "hours": d(inp.overtime_hours),
                 "gross": sum((v for k, v in full_month.items() if comp(k)["kind"] == "earning"), ZERO)}
        try:
            ot_amount = rnd(d(expressions.evaluate(formula, vars_)))
        except expressions.ExpressionError:
            warnings.append({"code": "overtime_formula", "level": "warn", "message": "The overtime formula has a mistake."})
        ot_code = inp.settings.get("overtime_component") or "OT"
        add(ot_code, ot_amount, note=f"{d(inp.overtime_hours)} hours")
        earned[ot_code] = earned.get(ot_code, ZERO) + ot_amount
    if d(inp.shift_amount) > 0:
        add("SHIFT", inp.shift_amount)
        earned["SHIFT"] = earned.get("SHIFT", ZERO) + rnd(inp.shift_amount)

    # arrears from back-dated revisions
    arrears_total = ZERO
    arrears_pf = ZERO
    arrears_esi = ZERO
    for a in inp.arrears:
        arrears_total += d(a["amount"])
        if comp(a["code"]).get("pf_wage"):
            arrears_pf += d(a["amount"])
        if comp(a["code"]).get("esi_wage"):
            arrears_esi += d(a["amount"])
    if arrears_total:
        add("ARREARS", arrears_total, extra={"breakdown": [{**a, "amount": money(a["amount"])} for a in inp.arrears]})

    # one-time and typed-in earnings
    one_time_taxable = ZERO
    for e in inp.earnings:
        c = comp(e["code"])
        add(e["code"], e["amount"], note=e.get("note"))
        earned[e["code"]] = earned.get(e["code"], ZERO) + rnd(e["amount"])
        if c.get("one_time") and c.get("taxability", "taxable") != "exempt":
            one_time_taxable += rnd(e["amount"])

    # reimbursements (expense claims paid with salary)
    reimb_taxable = ZERO
    for r in inp.reimbursements:
        add(r.get("code") or "REIMB", r["amount"], note=r.get("note"), extra={"ref": r.get("ref"), "bill": True},
            kind="reimbursement")
        reimb_taxable += d(r.get("taxable", 0))

    earning_lines = [ln for ln in lines if ln["kind"] in ("earning", "reimbursement")]
    gross = sum((ln["amount"] for ln in earning_lines), ZERO)
    gross_wages_like = sum((ln["amount"] for ln in earning_lines if ln["kind"] == "earning"), ZERO)

    # ── Code on Wages: wages and the 50% add-back ──────────────────
    core = sum((v for k, v in earned.items() if comp(k).get("wage_class") == "wage"), ZERO)
    excl = sum((v for k, v in earned.items() if comp(k).get("wage_class") == "exclusion" and comp(k)["kind"] == "earning"), ZERO)
    share = (inp.rules.get("wage_rule") or {}).get("min_wage_share_percent", 50)
    wr = calc.wage_rule(core, excl, share)
    wages = d(wr["wages"])
    full_core = sum((v for k, v in full_month.items() if comp(k).get("wage_class") == "wage"), ZERO)
    full_excl = sum((v for k, v in full_month.items() if comp(k).get("wage_class") == "exclusion" and comp(k)["kind"] == "earning"), ZERO)
    full_wages = d(calc.wage_rule(full_core, full_excl, share)["wages"])

    statutory: dict = {"wages": money(wages), "wage_add_back": wr["add_back"]}
    deductions: list[dict] = []
    employer: list[dict] = []

    def ded(code, amount, note=None, extra=None):
        amount = d(amount)
        if amount <= 0:
            return
        c = comp(code)
        deductions.append({"code": code, "name": c.get("name", code), "name_hi": c.get("name_hi"), "kind": "deduction",
                           "amount": rnd(amount, "none") if amount != rnd(amount) else rnd(amount), "note": note, **(extra or {})})

    def emp_line(code, amount, note=None):
        amount = d(amount)
        if amount <= 0:
            return
        c = comp(code)
        employer.append({"code": code, "name": c.get("name", code), "name_hi": c.get("name_hi"), "kind": "employer",
                         "amount": amount, "note": note, "show": c.get("show_on_payslip", True)})

    p = inp.profile
    if not inp.skip_statutory:
        # ── PF ──────────────────────────────────────────────────────
        pf_wage = sum((v for k, v in earned.items() if comp(k).get("pf_wage")), ZERO) + arrears_pf
        if inp.settings.get("pf", {}).get("apply_wage_rule", True):
            pf_wage += d(wr["add_back"])
        pf = calc.pf(pf_wage, inp.rules["epf"], applicable=p.get("pf_applicable", True),
                     employee_basis=p.get("pf_employee_basis", "ceiling"), employer_basis=p.get("pf_employer_basis", "ceiling"),
                     eps_eligible=p.get("eps_eligible", True), vpf_rate=p.get("vpf_rate", 0), vpf_amount=p.get("vpf_amount", 0))
        statutory["pf"] = {**pf.as_dict(), "pf_wage": money(pf_wage), "ncp_days": money(lop + d(period_days - eligible_days))}
        ded("PF_EE", pf.employee)
        ded("VPF", pf.vpf)
        emp_line("PF_ER", pf.employer_epf)
        emp_line("EPS_ER", pf.employer_eps)
        emp_line("EDLI", pf.edli)
        emp_line("PF_ADMIN", pf.admin)

        # ── ESI ─────────────────────────────────────────────────────
        esi_wage = sum((v for k, v in earned.items() if comp(k).get("esi_wage")), ZERO) + arrears_esi
        if not inp.settings.get("esi_include_overtime", True):
            esi_wage -= ot_amount
        covered = bool(inp.esi_covered and p.get("esi_applicable", True))
        ee, er = calc.esi(esi_wage, inp.rules["esi"], covered=covered, days_paid=payable_days)
        statutory["esi"] = {"covered": covered, "wage": money(esi_wage if covered else ZERO), "employee": money(ee),
                            "employer": money(er), "days": money(payable_days)}
        ded("ESI_EE", ee)
        emp_line("ESI_ER", er)

        # ── PT ──────────────────────────────────────────────────────
        pt = ZERO
        if p.get("pt_applicable", True) and inp.pt_rules:
            pt = calc.professional_tax(inp.pt_rules, month=inp.period_end.month, gross=gross_wages_like,
                                       gender=p.get("gender"), paid_this_year=inp.pt_paid_fy,
                                       half_year_gross=(d(inp.pt_half_year_gross) + gross_wages_like) if inp.pt_half_year_gross is not None else None,
                                       annual_gross=inp.pt_annual_gross)
        statutory["pt"] = {"state": p.get("pt_state"), "amount": money(pt), "gross": money(gross_wages_like)}
        ded("PT", pt)

        # ── LWF ─────────────────────────────────────────────────────
        lwf_ee, lwf_er = (calc.lwf(inp.lwf_rules, month=inp.period_end.month, gross=gross_wages_like)
                          if p.get("lwf_applicable", True) and inp.lwf_rules else (ZERO, ZERO))
        statutory["lwf"] = {"state": p.get("lwf_state"), "employee": money(lwf_ee), "employer": money(lwf_er)}
        ded("LWF_EE", lwf_ee)
        emp_line("LWF_ER", lwf_er)

        # ── gratuity provision ────────────────────────────────────
        grat = calc.gratuity_provision(wages, inp.rules["gratuity"]) if inp.rules.get("gratuity") else ZERO
        emp_line("GRATUITY", grat)
        statutory["gratuity_provision"] = money(grat)

    # employer parts of the structure (e.g. employer NPS)
    for code, v in earned.items():
        if comp(code)["kind"] == "employer" and code not in ("PF_ER", "ESI_ER", "GRATUITY"):
            emp_line(code, v)

    # loans and other deductions
    for ln in inp.loans:
        ded(ln.get("code") or "LOAN", ln["amount"], ln.get("note"), {"ref": ln.get("ref")})
    for x in inp.deductions:
        ded(x["code"], x["amount"], x.get("note"))

    # ── TDS ────────────────────────────────────────────────────────
    tax_out = None
    if inp.tax is not None:
        t = inp.tax
        taxable_regular = sum((ln["amount"] for ln in earning_lines
                               if comp(ln["code"]).get("taxability", "taxable") != "exempt"
                               and not comp(ln["code"]).get("one_time") and ln["code"] != "REIMB"
                               and not ln.get("bill")), ZERO)
        # expense claims: only the taxable part of claims paid with salary is income
        t.regular_taxable = taxable_regular + reimb_taxable
        t.one_time_taxable = one_time_taxable + (arrears_total if comp("ARREARS").get("one_time", True) else ZERO)
        t.basic_da = earned.get("BASIC", ZERO) + earned.get("DA", ZERO)
        t.hra = earned.get("HRA", ZERO)
        t.pf = sum((x["amount"] for x in deductions if x["code"] in ("PF_EE", "VPF")), ZERO)
        t.pt = sum((x["amount"] for x in deductions if x["code"] == "PT"), ZERO)
        t.employer_nps = earned.get("NPS_ER", ZERO)
        tax_out = taxcalc.monthly_tds(t)
        ded("TDS", d(tax_out["tds"]))

    total_deductions = sum((x["amount"] for x in deductions), ZERO)
    net_raw = gross - total_deductions
    net = rnd(net_raw, inp.settings.get("rounding", "nearest"))
    employer_total = sum((x["amount"] for x in employer), ZERO)

    # ── warnings ───────────────────────────────────────────────────
    if net < 0:
        warnings.append({"code": "negative_net", "level": "block", "message": f"Take-home pay is negative ({calc.inr(net)})."})
    limit = (inp.rules.get("wage_rule") or {}).get("max_deduction_percent", 50)
    statutory_ded = sum((x["amount"] for x in deductions if x["code"] != "TDS"), ZERO)
    if wages > 0 and statutory_ded > calc.pct(wages, limit):
        warnings.append({"code": "deductions_over_limit", "level": "warn",
                         "message": f"Deductions are {(statutory_ded / wages * 100).quantize(d('0.1'))}% of wages, above the "
                                    f"{limit}% limit in the Code on Wages."})
    if not wr["compliant"]:
        warnings.append({"code": "wage_rule", "level": "info",
                         "message": f"Basic and DA are {wr['core_share']}% of pay; {calc.inr(wr['add_back'])} added back to wages."})
    if inp.min_wage_monthly is not None and full_wages and full_wages < d(inp.min_wage_monthly):
        warnings.append({"code": "below_min_wage", "level": "block",
                         "message": f"Wages of {calc.inr(full_wages)} a month are below the minimum wage of "
                                    f"{calc.inr(inp.min_wage_monthly)}."})
    if inp.previous_net is not None and d(inp.previous_net) > 0:
        change = (net - d(inp.previous_net)) / d(inp.previous_net) * 100
        threshold = d(inp.settings.get("variance_percent", 20))
        if abs(change) >= threshold:
            warnings.append({"code": "variance", "level": "warn",
                             "message": f"Take-home pay changed {change.quantize(d('0.1'))}% from last month."})

    for ln in lines + deductions:
        ln["amount"] = money(ln["amount"])
    for ln in employer:
        ln["amount"] = money(ln["amount"])
    return {
        "employee_id": inp.employee_id,
        "days_in_period": money(period_days), "eligible_days": money(eligible_days),
        "payable_days": money(payable_days), "lop_days": money(lop), "basis_days": money(basis_days),
        "earnings": lines, "deductions": deductions, "employer": employer,
        "gross": money(gross), "total_deductions": money(total_deductions), "net": money(net),
        "net_rounding": money(net - net_raw), "employer_contributions": money(employer_total),
        "employer_cost": money(gross + employer_total), "wages": money(wages), "full_month_wages": money(full_wages),
        "wage_rule": wr, "statutory": statutory, "tax": tax_out, "warnings": warnings,
        "fbp_accrued": {k: money(v) for k, v in accrued.items() if v},
    }
