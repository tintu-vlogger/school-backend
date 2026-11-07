"""Pure statutory and payroll arithmetic. No database, no dates from the clock.

Every function takes the rule data (from the statutory library, as resolved for the
period) as an argument, so no statutory number lives in this file. Money is Decimal.

    pf(...)                 EPF / EPS / EDLI / admin for one month
    esi_covered(...), esi(...), esi_period(...)
    professional_tax(...)   monthly, half-yearly and annual slabs, February amount, yearly cap
    lwf(...)                Labour Welfare Fund for the month (employee, employer)
    income_tax(...)         slab tax, rebate with marginal relief, surcharge with marginal relief, cess
    hra_exemption(...)
    gratuity(...), retrenchment(...), bonus(...)
    proration_factor(...), wage_rule(...)
    emi_schedule(...), loan_perquisite(...)
    add_working_days(...)
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Callable, Iterable

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")
CENT = Decimal("0.01")


def d(x) -> Decimal:
    if x is None or x == "":
        return ZERO
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def rnd(x, mode: str = "nearest") -> Decimal:
    """Round money. nearest (half up) | up | down to whole rupees, or none (two decimals)."""
    x = d(x)
    if mode == "up":
        return x.quantize(ONE, rounding=ROUND_CEILING)
    if mode == "down":
        return x.quantize(ONE, rounding=ROUND_FLOOR)
    if mode in ("none", "2dp", None):
        return x.quantize(CENT, rounding=ROUND_HALF_UP)
    return x.quantize(ONE, rounding=ROUND_HALF_UP)


def pct(amount, rate) -> Decimal:
    return d(amount) * d(rate) / HUNDRED


def inr(x) -> str:
    """₹12,34,567 (Indian grouping, paise only when present) for messages."""
    v = d(x).quantize(CENT, rounding=ROUND_HALF_UP)
    neg, v = v < 0, abs(v)
    whole, frac = str(v).split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(([head] if head else []) + groups + [tail])
    return f"{'-' if neg else ''}₹{whole}" + (f".{frac}" if frac != "00" else "")


def money(x) -> str:
    return str(d(x).quantize(CENT, rounding=ROUND_HALF_UP))


# ── Provident fund ──────────────────────────────────────────────────

@dataclass
class PFResult:
    epf_wage: Decimal = ZERO      # wages on which the employee's EPF is paid (ECR "EPF wages")
    eps_wage: Decimal = ZERO
    edli_wage: Decimal = ZERO
    employee: Decimal = ZERO      # employee's 12%
    vpf: Decimal = ZERO           # voluntary PF on top
    employer_eps: Decimal = ZERO  # pension (8.33%, capped)
    employer_epf: Decimal = ZERO  # employer's share that goes to the PF account (12% minus EPS)
    edli: Decimal = ZERO
    admin: Decimal = ZERO
    applicable: bool = True

    @property
    def employer_total(self) -> Decimal:
        return self.employer_eps + self.employer_epf

    def as_dict(self) -> dict:
        out = {k: (money(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}
        out["employer_total"] = money(self.employer_total)
        return out


def pf(wage, rules: dict, *, applicable: bool = True, employee_basis: str = "ceiling",
       employer_basis: str = "ceiling", eps_eligible: bool = True, vpf_rate=0, vpf_amount=0) -> PFResult:
    """One month of PF on `wage` (the PF wages earned this month).

    employee_basis / employer_basis: "ceiling" pays on wages up to the ceiling (restricted),
    "actual" on the full wage (unrestricted). EPS is 8.33% of wages up to the EPS ceiling,
    never more than eps_max; members not eligible for EPS (58 or older, or who joined after
    the 2014 change with wages above the ceiling) put the whole employer share into EPF."""
    if not applicable:
        return PFResult(applicable=False)
    wage = d(wage)
    if wage <= 0:
        return PFResult()
    mode = rules.get("rounding", "nearest")
    ceiling = d(rules["wage_ceiling"])
    base_ee = wage if employee_basis == "actual" else min(wage, ceiling)
    base_er = wage if employer_basis == "actual" else min(wage, ceiling)
    employee = rnd(pct(base_ee, rules["employee_rate"]), mode)
    employer_total = rnd(pct(base_er, rules["employer_rate"]), mode)
    eps_wage = min(wage, d(rules.get("eps_wage_ceiling", ceiling))) if eps_eligible else ZERO
    eps = ZERO
    if eps_eligible:
        eps = min(rnd(pct(eps_wage, rules["eps_rate"]), mode), d(rules.get("eps_max") or 10 ** 9))
        eps = min(eps, employer_total)
    edli_wage = min(wage, d(rules.get("edli_wage_ceiling", ceiling)))
    vpf = rnd(pct(base_ee, vpf_rate), mode) + rnd(vpf_amount, mode) if (d(vpf_rate) > 0 or d(vpf_amount) > 0) else ZERO
    return PFResult(
        epf_wage=base_ee, eps_wage=eps_wage, edli_wage=edli_wage, employee=employee, vpf=vpf,
        employer_eps=eps, employer_epf=employer_total - eps,
        edli=rnd(pct(edli_wage, rules.get("edli_rate", 0)), mode),
        admin=rnd(pct(base_er, rules.get("admin_rate", 0)), mode),
    )


def pf_admin_charges(total_admin, members: int, rules: dict) -> Decimal:
    """Admin charges for the establishment for the month, with the minimum."""
    total_admin = d(total_admin)
    minimum = d(rules.get("admin_min", 0)) if members else d(rules.get("admin_min_no_members", 0))
    return max(total_admin, minimum)


def eps_eligible(age_years: int | None, rules: dict, *, joined_after_2014_above_ceiling: bool = False) -> bool:
    if joined_after_2014_above_ceiling:
        return False
    if age_years is not None and age_years >= int(rules.get("eps_max_age", 58)):
        return False
    return True


# ── ESI ─────────────────────────────────────────────────────────────

def esi_period(day: date, rules: dict) -> tuple[date, date]:
    """The six-month contribution period containing `day` (April–September, October–March)."""
    for start_m, end_m in rules.get("contribution_periods") or [[4, 9], [10, 3]]:
        if start_m <= end_m:
            if start_m <= day.month <= end_m:
                start = date(day.year, start_m, 1)
                end = date(day.year, end_m, 1)
                break
        else:
            if day.month >= start_m or day.month <= end_m:
                y0 = day.year if day.month >= start_m else day.year - 1
                start = date(y0, start_m, 1)
                end = date(y0 + 1, end_m, 1)
                break
    else:  # pragma: no cover - defensive
        start, end = date(day.year, day.month, 1), date(day.year, day.month, 1)
    nxt = (end.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, nxt - timedelta(days=1)


def esi_covered(monthly_wage, rules: dict, *, disabled: bool = False) -> bool:
    """Coverage is decided on the wages at the start of a contribution period (or at joining)
    and then holds for the whole period even if pay goes above the ceiling."""
    ceiling = d(rules["disability_wage_ceiling"] if disabled else rules["wage_ceiling"])
    return d(monthly_wage) <= ceiling


def esi(wage, rules: dict, *, covered: bool, days_paid=None) -> tuple[Decimal, Decimal]:
    """(employee, employer) ESI for the month's ESI wages. The employee pays nothing when the
    average daily wage is at or below the exempt limit; the employer still pays."""
    wage = d(wage)
    if not covered or wage <= 0:
        return ZERO, ZERO
    mode = rules.get("rounding", "up")
    employer = rnd(pct(wage, rules["employer_rate"]), mode)
    employee = rnd(pct(wage, rules["employee_rate"]), mode)
    days = d(days_paid) if days_paid else ZERO
    if days > 0 and rules.get("exempt_daily_wage") and wage / days <= d(rules["exempt_daily_wage"]):
        employee = ZERO
    return employee, employer


# ── Professional Tax ────────────────────────────────────────────────

def _slab(slabs: list[dict], amount: Decimal, gender: str | None) -> dict | None:
    g = (gender or "").lower()
    if g not in ("male", "female"):
        g = "male"
    gendered = [s for s in slabs if s.get("gender")]
    pool = [s for s in slabs if not s.get("gender") or s.get("gender") == g] if gendered else slabs
    if gendered and not any(s.get("gender") == g for s in slabs):
        pool = [s for s in slabs if not s.get("gender") or s.get("gender") == "male"]
    for s in pool:
        lo = d(s.get("min"))
        hi = s.get("max")
        if amount >= lo and (hi is None or amount <= d(hi)):
            return s
    return None


def professional_tax(rules: dict | None, *, month: int, gross, gender: str | None = None, half_year_gross=None,
                     annual_gross=None, paid_this_year=0) -> Decimal:
    """PT to deduct this month.

    monthly basis: slab on this month's gross (February amount where the state sets one).
    half_yearly: slab on the half-year's gross, deducted in the state's deduction months.
    annual: slab on the year's gross, spread evenly with the balance in balance_month.
    The yearly total never exceeds annual_cap (₹2,500 under the Constitution)."""
    if not rules:
        return ZERO
    basis = rules.get("basis", "monthly")
    slabs = rules.get("slabs") or []
    amount = ZERO
    if basis == "monthly":
        s = _slab(slabs, d(gross), gender)
        if s:
            amount = d(s.get("feb_amount")) if month == 2 and s.get("feb_amount") else d(s.get("amount"))
    elif basis == "half_yearly":
        if month in (rules.get("deduct_months") or []):
            s = _slab(slabs, d(half_year_gross if half_year_gross is not None else d(gross) * 6), gender)
            amount = d(s.get("amount")) if s else ZERO
    elif basis == "annual":
        s = _slab(slabs, d(annual_gross if annual_gross is not None else d(gross) * 12), gender)
        yearly = d(s.get("amount")) if s else ZERO
        if month == int(rules.get("balance_month", 3)):
            amount = max(yearly - d(paid_this_year), ZERO)
        else:
            amount = (yearly / 12).quantize(ONE, rounding=ROUND_FLOOR)
    cap = rules.get("annual_cap")
    if cap is not None:
        amount = max(min(amount, d(cap) - d(paid_this_year)), ZERO)
    return amount


# ── Labour Welfare Fund ─────────────────────────────────────────────

def lwf(rules: dict | None, *, month: int, gross) -> tuple[Decimal, Decimal]:
    """(employee, employer) for this month; zero outside the state's collection months."""
    if not rules or month not in (rules.get("months") or []):
        return ZERO, ZERO
    if rules.get("wage_max") is not None and d(gross) > d(rules["wage_max"]):
        return ZERO, ZERO

    def part(spec: dict, base_employee: Decimal | None = None) -> Decimal:
        if "amount" in spec:
            return d(spec["amount"])
        if "multiple_of_employee" in spec:
            return (base_employee or ZERO) * d(spec["multiple_of_employee"])
        v = pct(gross, spec.get("rate", 0))
        if spec.get("max") is not None:
            v = min(v, d(spec["max"]))
        return v.quantize(CENT, rounding=ROUND_HALF_UP)

    ee = part(rules.get("employee") or {})
    er = part(rules.get("employer") or {}, ee)
    return ee, er


# ── Income tax ──────────────────────────────────────────────────────

def slab_tax(income, slabs: list[dict]) -> Decimal:
    income = max(d(income), ZERO)
    tax, lower = ZERO, ZERO
    for s in slabs:
        upper = d(s["upto"]) if s.get("upto") is not None else None
        if income <= lower:
            break
        top = income if upper is None else min(income, upper)
        tax += pct(top - lower, s["rate"])
        if upper is None:
            break
        lower = upper
    return tax


def _slabs_for(rules: dict, age: int | None) -> list[dict]:
    if age is not None and age >= 80 and rules.get("super_senior_slabs"):
        return rules["super_senior_slabs"]
    if age is not None and age >= 60 and rules.get("senior_slabs"):
        return rules["senior_slabs"]
    return rules["slabs"]


def _surcharge_rate(income: Decimal, rules: dict) -> tuple[Decimal, Decimal]:
    rate, threshold = ZERO, ZERO
    for s in sorted(rules.get("surcharge") or [], key=lambda x: x["above"]):
        if income > d(s["above"]):
            rate, threshold = d(s["rate"]), d(s["above"])
    return rate, threshold


def income_tax(taxable_income, rules: dict, *, age: int | None = None) -> dict:
    """Tax on a year's taxable income under one regime.

    Rebate: full rebate up to the limit; above it (new regime) marginal relief keeps the tax
    from exceeding the income above the limit. Surcharge with marginal relief so tax plus
    surcharge never rises by more than the income above each threshold. Cess on top."""
    income = max(d(taxable_income).quantize(ONE, rounding=ROUND_HALF_UP), ZERO)
    slabs = _slabs_for(rules, age)
    base = slab_tax(income, slabs)
    rebate = ZERO
    reb = rules.get("rebate") or {}
    limit = d(reb.get("income_limit")) if reb else ZERO
    relief_rebate = ZERO
    if reb and income <= limit:
        rebate = min(base, d(reb.get("max_rebate")))
    elif reb and rules.get("rebate_marginal_relief") and income > limit:
        excess = income - limit
        if base > excess:
            relief_rebate = base - excess
    tax = base - rebate - relief_rebate
    rate, threshold = _surcharge_rate(income, rules)
    surcharge, relief_sur = ZERO, ZERO
    if rate > 0:
        surcharge = pct(tax, rate)
        # tax at the threshold, with the surcharge of the bracket below it
        lower_rate = ZERO
        for s in sorted(rules.get("surcharge") or [], key=lambda x: x["above"]):
            if d(s["above"]) < threshold:
                lower_rate = d(s["rate"])
        at_threshold = slab_tax(threshold, slabs)
        at_threshold += pct(at_threshold, lower_rate)
        allowed = at_threshold + (income - threshold)
        if tax + surcharge > allowed:
            relief_sur = tax + surcharge - allowed
            surcharge = max(surcharge - relief_sur, ZERO)
    cess = pct(tax + surcharge, rules.get("cess_rate", 0))
    total = rnd(tax + surcharge + cess)
    return {"taxable_income": money(income), "tax_on_income": money(base), "rebate": money(rebate),
            "rebate_marginal_relief": money(relief_rebate), "surcharge": money(surcharge),
            "surcharge_marginal_relief": money(relief_sur), "cess": money(cess), "total": money(total)}


def hra_exemption(*, hra_received, rent_paid, basic_da, metro: bool, section: dict) -> Decimal:
    """Least of: HRA received, rent paid minus 10% of salary, 50% (metro) / 40% of salary."""
    pct_city = section.get("metro_percent", 50) if metro else section.get("non_metro_percent", 40)
    return max(min(d(hra_received), d(rent_paid) - pct(basic_da, 10), pct(basic_da, pct_city)), ZERO)


# ── Service-based amounts ───────────────────────────────────────────

def service_length(start: date, end: date) -> tuple[int, int, int]:
    """Completed years, months and days from start to end (end inclusive)."""
    end = end + timedelta(days=1)
    y = end.year - start.year
    m = end.month - start.month
    dd = end.day - start.day
    if dd < 0:
        m -= 1
        prev = end.replace(day=1) - timedelta(days=1)
        dd += prev.day
    if m < 0:
        y -= 1
        m += 12
    return y, m, dd


def counted_years(start: date, end: date, part_year_months: int = 6) -> int:
    """Completed years, plus one for a part year of more than `part_year_months` months."""
    y, m, dd = service_length(start, end)
    if m > part_year_months or (m == part_year_months and dd > 0):
        y += 1
    return y


def gratuity(last_monthly_wage, start: date, end: date, rules: dict, *, fixed_term: bool = False,
             death_or_disablement: bool = False) -> dict:
    """Gratuity on exit: 15 days' wages (wage / 26 × 15) per counted year, after 5 years of
    continuous service (1 year for fixed-term; none for death or disablement)."""
    y, m, dd = service_length(start, end)
    total_days = (end - start).days + 1
    min_years = int(rules["min_years_fixed_term"] if fixed_term else rules["min_years"])
    eligible = death_or_disablement or y >= min_years
    if not eligible and not fixed_term and rules.get("four_years_240_days") and y == min_years - 1:
        # 4 years and 240 days of work in the fifth year counts as 5 years of continuous service
        eligible = total_days >= (min_years - 1) * 365 + 240
    years = counted_years(start, end, int(rules.get("part_year_months", 6)))
    amount = ZERO
    if eligible:
        amount = rnd(d(last_monthly_wage) * d(rules["days_per_year"]) / d(rules["divisor"]) * years)
        if rules.get("max_amount"):
            amount = min(amount, d(rules["max_amount"]))
    exempt = min(amount, d(rules.get("tax_exempt_limit") or amount))
    return {"eligible": eligible, "service": {"years": y, "months": m, "days": dd}, "years_counted": years if eligible else 0,
            "wage": money(last_monthly_wage), "amount": money(amount), "tax_exempt": money(exempt),
            "taxable": money(amount - exempt)}


def gratuity_provision(monthly_wage, rules: dict) -> Decimal:
    """Monthly cost set aside: 15/26 of a month's wages per year, i.e. per month / 12."""
    return rnd(d(monthly_wage) * d(rules["days_per_year"]) / d(rules["divisor"]) / 12)


def retrenchment(avg_monthly_wage, start: date, end: date, rules: dict) -> dict:
    years = counted_years(start, end, int(rules.get("part_year_months", 6)))
    day_wage = d(avg_monthly_wage) / d(rules.get("divisor", 26))
    comp = rnd(day_wage * d(rules["days_per_year"]) * years)
    reskill = rnd(day_wage * d(rules["reskilling_days"]))
    return {"years_counted": years, "compensation": money(comp), "reskilling_fund": money(reskill)}


def bonus(months: Iterable[dict], rules: dict, *, rate, min_wage_monthly=None) -> dict:
    """Statutory bonus for an accounting year. `months` = [{wage, days_worked}]. Eligible
    when the monthly wage is at or below the eligibility limit and at least 30 days were
    worked; the base per month is the wage capped at the calculation ceiling (or the
    minimum wage when higher)."""
    rate = d(rate)
    rate = min(max(rate, d(rules["min_rate"])), d(rules["max_rate"]))
    ceiling = d(rules["calculation_ceiling"])
    if rules.get("use_min_wage_if_higher") and min_wage_monthly:
        ceiling = max(ceiling, d(min_wage_monthly))
    months = list(months)
    worked = sum(d(m.get("days_worked", 0)) for m in months)
    eligible_months = [m for m in months if d(m["wage"]) <= d(rules["eligibility_wage"])]
    base = sum((min(d(m["wage"]), ceiling) for m in eligible_months), ZERO)
    eligible = worked >= d(rules.get("min_working_days", 30)) and bool(eligible_months)
    amount = rnd(pct(base, rate)) if eligible else ZERO
    return {"eligible": eligible, "rate": money(rate), "ceiling": money(ceiling), "base": money(base),
            "months_counted": len(eligible_months), "amount": money(amount)}


# ── Proration and the wage rule ─────────────────────────────────────

def proration_factor(unpaid_days, basis_days) -> Decimal:
    """Share of the month paid: (basis days − unpaid days) / basis days, between 0 and 1."""
    basis = d(basis_days)
    if basis <= 0:
        return ZERO
    return min(max((basis - d(unpaid_days)) / basis, ZERO), ONE)


def wage_rule(core, exclusions, share_percent=50) -> dict:
    """Code on Wages: basic + DA + retaining allowance must be at least 50% of total
    remuneration; the excess of the excluded allowances over 50% is added back to wages."""
    core, exclusions = d(core), d(exclusions)
    total = core + exclusions
    required = pct(total, share_percent)
    add_back = max(required - core, ZERO)
    add_back = add_back.quantize(CENT, rounding=ROUND_HALF_UP)
    share = (core / total * HUNDRED).quantize(CENT) if total > 0 else HUNDRED
    return {"total_remuneration": money(total), "core": money(core), "exclusions": money(exclusions),
            "core_share": str(share), "add_back": money(add_back), "wages": money(core + add_back),
            "compliant": add_back == 0}


# ── Loans ───────────────────────────────────────────────────────────

def add_months(day: date, n: int) -> date:
    y, m = divmod(day.month - 1 + n, 12)
    return date(day.year + y, m + 1, 1)


def emi_schedule(principal, annual_rate, instalments: int, start_month: date, method: str = "reducing") -> list[dict]:
    """Instalment plan. reducing: equal EMIs on the reducing balance; flat: interest on the
    full amount spread evenly; rate 0: principal split evenly. The last instalment absorbs
    rounding so principal always sums exactly."""
    p, n = d(principal), int(instalments)
    if n <= 0 or p <= 0:
        return []
    r = d(annual_rate) / HUNDRED / 12
    rows = []
    balance = p
    if r == 0 or method == "flat":
        interest_total = rnd(p * d(annual_rate) / HUNDRED * n / 12) if method == "flat" else ZERO
        prin_each = rnd(p / n)
        int_each = rnd(interest_total / n)
        for i in range(n):
            prin = prin_each if i < n - 1 else balance
            intr = int_each if i < n - 1 else interest_total - int_each * (n - 1)
            balance -= prin
            rows.append({"seq": i + 1, "month": add_months(start_month, i), "principal": prin, "interest": intr,
                         "emi": prin + intr, "balance": balance})
        return rows
    f = (1 + r) ** n
    emi = rnd(p * r * f / (f - 1))
    for i in range(n):
        intr = rnd(balance * r)
        prin = emi - intr if i < n - 1 else balance
        if i == n - 1:
            emi_i = prin + intr
        else:
            emi_i = emi
        balance -= prin
        rows.append({"seq": i + 1, "month": add_months(start_month, i), "principal": prin, "interest": intr,
                     "emi": emi_i, "balance": balance})
    return rows


def loan_perquisite(outstanding, charged_rate, rules: dict, *, aggregate_outstanding=None, medical: bool = False) -> Decimal:
    """Monthly taxable value of a concessional loan: interest saved against the reference
    rate, unless all loans together stay within the exempt limit (or it is for treatment)."""
    agg = d(aggregate_outstanding if aggregate_outstanding is not None else outstanding)
    if medical or agg <= d(rules.get("exempt_aggregate", 0)):
        return ZERO
    gap = d(rules.get("reference_rate", 0)) - d(charged_rate)
    if gap <= 0:
        return ZERO
    return rnd(d(outstanding) * gap / HUNDRED / 12)


# ── Calendar helpers ────────────────────────────────────────────────

def add_working_days(start: date, n: int, is_working: Callable[[date], bool]) -> date:
    """The date `n` working days after `start` (the start day itself does not count)."""
    day, left = start, n
    for _ in range(366):
        if left <= 0:
            return day
        day += timedelta(days=1)
        if is_working(day):
            left -= 1
    return day


def fy_of(day: date) -> str:
    """Financial (tax) year label, e.g. 2026-27."""
    y = day.year if day.month >= 4 else day.year - 1
    return f"{y}-{str(y + 1)[-2:]}"


def fy_bounds(fy: str) -> tuple[date, date]:
    y = int(fy[:4])
    return date(y, 4, 1), date(y + 1, 3, 31)


def months_left_in_fy(day: date) -> int:
    """Months from `day`'s month to March, both included."""
    end = date(day.year if day.month <= 3 else day.year + 1, 3, 1)
    return (end.year - day.year) * 12 + end.month - day.month + 1


def month_end(day: date) -> date:
    return add_months(day.replace(day=1), 1) - timedelta(days=1)


def age_on(dob: date | None, day: date) -> int | None:
    if not dob:
        return None
    return day.year - dob.year - ((day.month, day.day) < (dob.month, dob.day))


def amount_in_words(n) -> str:
    """Indian numbering (lakh, crore): 125000 -> 'One lakh twenty-five thousand rupees only'."""
    n = int(rnd(n))
    if n == 0:
        return "Zero rupees only"
    ones = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
            "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
    tens = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]

    def two(x):
        if x < 20:
            return ones[x]
        return tens[x // 10] + ("-" + ones[x % 10] if x % 10 else "")

    def three(x):
        h, r = divmod(x, 100)
        return (ones[h] + " hundred" + (" " if r else "") if h else "") + (two(r) if r else "")

    parts = []
    crore, n = divmod(n, 10 ** 7)
    lakh, n = divmod(n, 10 ** 5)
    thousand, n = divmod(n, 1000)
    if crore:
        parts.append((amount_in_words(crore * 1).replace(" rupees only", "").lower() if crore >= 100 else two(crore)) + " crore")
    if lakh:
        parts.append(two(lakh) + " lakh")
    if thousand:
        parts.append(two(thousand) + " thousand")
    if n:
        parts.append(three(n))
    text = " ".join(parts).strip()
    return text[0].upper() + text[1:] + " rupees only"


def ceil_div(a: int, b: int) -> int:
    return -(-a // b)

