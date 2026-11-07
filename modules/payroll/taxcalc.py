"""Income tax on salary: the yearly projection and the month's TDS (pure functions).

    annual_worksheet(...)   the year's income, exemptions, deductions and tax under one regime
    monthly_tds(...)        TDS for this month: the regular part spread over the months left,
                            one-time payments taxed in full in the month they are paid
    compare(...)            both regimes side by side for the employee

Amounts that come from declarations use the declared figures until HR verifies the proofs,
then the approved figures. Without a valid PAN, tax is at least 20% of taxable income
(and at the average rate when that is higher).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from modules.payroll import calc
from modules.payroll.calc import ZERO, d, money, rnd


@dataclass
class TaxInputs:
    regime: str                              # new | old
    regime_rules: dict
    sections: dict
    age: int | None = None
    has_pan: bool = True
    months_left: int = 12                    # including the current month
    # year to date (before this month), from earlier payslips in the year
    ytd_taxable: Decimal = ZERO              # taxable earnings paid so far (after bill-based exemptions)
    ytd_basic_da: Decimal = ZERO
    ytd_hra: Decimal = ZERO
    ytd_pf: Decimal = ZERO                   # employee PF + VPF
    ytd_pt: Decimal = ZERO
    ytd_tds: Decimal = ZERO
    ytd_employer_nps: Decimal = ZERO
    ytd_perquisites: Decimal = ZERO
    # this month
    regular_taxable: Decimal = ZERO
    one_time_taxable: Decimal = ZERO
    basic_da: Decimal = ZERO
    hra: Decimal = ZERO
    pf: Decimal = ZERO
    pt: Decimal = ZERO
    employer_nps: Decimal = ZERO
    perquisites: Decimal = ZERO
    # projection for each later month
    future_taxable: Decimal = ZERO
    future_basic_da: Decimal = ZERO
    future_hra: Decimal = ZERO
    future_pf: Decimal = ZERO
    future_pt: Decimal = ZERO
    future_employer_nps: Decimal = ZERO
    future_perquisites: Decimal = ZERO
    # declarations (already chosen: declared or approved)
    deductions: dict = field(default_factory=dict)   # {section key: amount}
    rent_paid: Decimal = ZERO                        # for the year
    metro: bool = False
    lta_exempt: Decimal = ZERO
    bills_exempt: Decimal = ZERO                     # flexible-benefit allowances backed by approved bills
    previous_income: Decimal = ZERO
    previous_tds: Decimal = ZERO
    previous_pf: Decimal = ZERO
    previous_pt: Decimal = ZERO
    other_income: Decimal = ZERO
    extra_tds: Decimal = ZERO
    tds_override: Decimal | None = None


def _cap(v, cap) -> Decimal:
    return min(d(v), d(cap)) if cap is not None else d(v)


def annual_worksheet(t: TaxInputs, *, include_one_time: bool = True) -> dict:
    rr, sec = t.regime_rules, t.sections
    later = max(t.months_left - 1, 0)
    salary = (t.ytd_taxable + t.regular_taxable + (t.one_time_taxable if include_one_time else ZERO)
              + t.future_taxable * later)
    perqs = t.ytd_perquisites + t.perquisites + t.future_perquisites * later
    basic_da = t.ytd_basic_da + t.basic_da + t.future_basic_da * later
    hra = t.ytd_hra + t.hra + t.future_hra * later
    pf = t.ytd_pf + t.pf + t.future_pf * later + t.previous_pf
    pt = t.ytd_pt + t.pt + t.future_pt * later + t.previous_pt
    nps_er = t.ytd_employer_nps + t.employer_nps + t.future_employer_nps * later
    gross = salary + perqs + t.previous_income
    allowed = set(rr.get("allowed") or [])
    exemptions: list[dict] = []
    if "hra" in allowed and hra > 0 and t.rent_paid > 0:
        ex = calc.hra_exemption(hra_received=hra, rent_paid=t.rent_paid, basic_da=basic_da, metro=t.metro,
                                section=sec.get("hra", {}))
        exemptions.append({"key": "hra", "label": "House rent allowance", "amount": rnd(ex)})
    if "lta" in allowed and t.lta_exempt > 0:
        exemptions.append({"key": "lta", "label": "Leave travel allowance", "amount": rnd(t.lta_exempt)})
    if t.bills_exempt > 0:
        exemptions.append({"key": "bills", "label": "Allowances backed by bills", "amount": rnd(t.bills_exempt)})
    std = min(d(rr.get("standard_deduction", 0)), gross)
    exemptions.append({"key": "standard_deduction", "label": "Standard deduction", "amount": std})
    if "professional_tax" in allowed and pt > 0:
        exemptions.append({"key": "professional_tax", "label": "Professional tax", "amount": pt})
    deductions: list[dict] = []
    if "employer_nps" in allowed and nps_er > 0:
        limit = calc.pct(basic_da, rr.get("employer_nps_percent", 10))
        deductions.append({"key": "employer_nps", "label": "Employer NPS (80CCD(2))", "amount": rnd(min(nps_er, limit))})
    if "80C" in allowed:
        v = _cap(d(t.deductions.get("80C")) + pf, sec.get("80C", {}).get("cap"))
        if v > 0:
            deductions.append({"key": "80C", "label": "80C (incl. your PF)", "amount": v})
    senior = t.age is not None and t.age >= 60
    for key in ("80CCD1B", "80D_self", "80D_parents", "80E", "80G", "80TTA", "24b"):
        if key not in allowed or not t.deductions.get(key):
            continue
        s = sec.get(key, {})
        cap = s.get("cap_senior") if senior and key == "80D_self" and s.get("cap_senior") else s.get("cap")
        if key == "80D_parents" and t.deductions.get("parents_senior") and s.get("cap_senior"):
            cap = s["cap_senior"]
        v = _cap(t.deductions[key], cap)
        if v > 0:
            deductions.append({"key": key, "label": s.get("label", key), "amount": v})
    total_ex = sum((e["amount"] for e in exemptions), ZERO)
    total_ded = sum((x["amount"] for x in deductions), ZERO)
    income = max(gross + t.other_income - total_ex - total_ded, ZERO)
    tax = calc.income_tax(income, rr, age=t.age)
    total = d(tax["total"])
    no_pan = False
    if not t.has_pan:
        floor = rnd(calc.pct(income, rr.get("no_pan_rate", 20)))
        if floor > total:
            total, no_pan = floor, True
    return {"regime": t.regime, "salary": money(salary), "perquisites": money(perqs),
            "previous_employer_income": money(t.previous_income), "other_income": money(t.other_income),
            "gross": money(gross + t.other_income), "exemptions": [{**e, "amount": money(e["amount"])} for e in exemptions],
            "deductions": [{**x, "amount": money(x["amount"])} for x in deductions],
            "taxable_income": money(income), "tax": tax, "annual_tax": money(total), "no_pan_rate_applied": no_pan}


def monthly_tds(t: TaxInputs) -> dict:
    """This month's TDS and the worksheet behind it."""
    full = annual_worksheet(t, include_one_time=True)
    deducted = t.ytd_tds + t.previous_tds
    if t.tds_override is not None:
        return {"worksheet": full, "tds": money(t.tds_override), "regular": money(t.tds_override), "one_time": "0.00",
                "extra": "0.00", "deducted_so_far": money(deducted), "override": True}
    one_time_tax = ZERO
    regular_total = d(full["annual_tax"])
    if t.one_time_taxable > 0:
        without = annual_worksheet(t, include_one_time=False)
        one_time_tax = max(d(full["annual_tax"]) - d(without["annual_tax"]), ZERO)
        regular_total = d(without["annual_tax"])
    remaining = regular_total - deducted
    regular = rnd(max(remaining, ZERO) / max(t.months_left, 1))
    tds = regular + rnd(one_time_tax) + rnd(t.extra_tds)
    return {"worksheet": full, "tds": money(tds), "regular": money(regular), "one_time": money(rnd(one_time_tax)),
            "extra": money(rnd(t.extra_tds)), "deducted_so_far": money(deducted),
            "balance_after": money(max(d(full["annual_tax"]) - deducted - tds, ZERO)), "override": False}


def compare(t: TaxInputs, rules_by_regime: dict[str, dict]) -> dict:
    out = {}
    for regime, rr in rules_by_regime.items():
        tt = TaxInputs(**{**t.__dict__, "regime": regime, "regime_rules": rr})
        out[regime] = annual_worksheet(tt)
    new, old = d(out["new"]["annual_tax"]), d(out["old"]["annual_tax"])
    out["better"] = "new" if new <= old else "old"
    out["saving"] = money(abs(new - old))
    return out
