"""The rules of a leave type version: their shape, defaults, cleaning and legal floors.

Rules are stored as JSON on `leave_policy_versions.rules`. Every key has a default, so a
version saved with only a few keys still works. `clean_rules()` fills defaults, checks
types and ranges and refuses anything below the legal minimum for statutory leave
(maternity under the Code on Social Security, annual leave under the OSH Code).

    paid            paid | unpaid | lop
    pay_percent     share of pay kept on a paid leave day (100; 50 for half-pay sick leave). The rest is
                    sent to payroll as loss of pay.
    statutory       true when the leave comes from law (shown in reports)
    legal_kind      none | annual | maternity | paternity | sick | casual  (decides the legal floor)
    unit            day | hour (hour types count hours; hours_per_day converts)
    allow_half_day, allow_quarter_day, allow_hourly, hours_per_day
    applicability   who gets it (see facts.applies)
    entitlement     mode: fixed | accrual | days_worked | grant | none
                    yearly_quota, frequency (yearly|half_yearly|quarterly|monthly), credit_at (start|end)
                    days_worked {per_days, credit, min_days_in_year, count_paid_leave, count_holidays}
                    proration {on_joining, on_exit, basis (days|months), joining_cutoff_day, rounding, round_mode}
                    probation {credit: full|none|reduced, reduced_quota, waiting_days}
                    experience_levels [{after_years, yearly_quota}]
                    max_balance
    year_end        year_start_month (1 = calendar, 4 = financial), reset (true/false)
                    carry_forward {enabled, max_days, percent, expire_after_months, max_accumulation}
                    encashment {enabled, max_days, percent, min_balance_after, on_exit}
    restrictions    min_days, max_days, max_consecutive, notice_days, notice_above_days, backdated_days,
                    future_days, count_weekends, count_holidays, sandwich {weekends, holidays, min_days, adjacent},
                    clubbing_blocked (codes), document_after_days, negative {mode: none|limit|year_end|lop, limit},
                    max_requests {count, period: month|quarter|year|tenure}, min_gap_days, only_optional_holidays,
                    hidden_from_employee
    grant           options [{key, label, days}], max_requests, period (year|tenure), min_gap_days
"""
from __future__ import annotations

import copy
from decimal import Decimal

from core.errors import BadRequest

FREQUENCIES = {"yearly": 1, "half_yearly": 2, "quarterly": 4, "monthly": 12}
MODES = ("fixed", "accrual", "days_worked", "grant", "none")
NEGATIVE_MODES = ("none", "limit", "year_end", "lop")
ROUNDINGS = {"none": None, "whole": Decimal("1"), "half": Decimal("0.5"), "quarter": Decimal("0.25")}
LEGAL_KINDS = ("none", "annual", "maternity", "paternity", "sick", "casual")

# Statutory floors (Code on Social Security 2020, OSH Code 2020). Values a policy may not go below.
MATERNITY_BIRTH_DAYS = 182          # 26 weeks for the first two children
MATERNITY_LATER_CHILD_DAYS = 84     # 12 weeks from the third child
MATERNITY_ADOPTION_DAYS = 84        # adoption of a child under 3 months; commissioning mother
ANNUAL_LEAVE_PER_DAYS = 20          # 1 day for every 20 days worked
ANNUAL_LEAVE_MIN_DAYS_WORKED = 180  # after 180 days of work in the year

DEFAULT_RULES: dict = {
    "paid": "paid",
    "pay_percent": 100,
    "statutory": False,
    "legal_kind": "none",
    "unit": "day",
    "allow_half_day": True,
    "allow_quarter_day": False,
    "allow_hourly": False,
    "hours_per_day": 8,
    "applicability": {
        "mode": "all", "genders": [], "marital_statuses": [], "employment_types": [], "locations": [], "states": [],
        "departments": [], "designations": [], "grades": [], "tags": [], "employees": [], "exclude_employees": [],
        "min_tenure_days": 0, "probation": "any", "rule": "",
    },
    "entitlement": {
        "mode": "fixed",
        "yearly_quota": 12,
        "frequency": "yearly",
        "credit_at": "start",
        "days_worked": {"per_days": 20, "credit": 1, "min_days_in_year": 180, "count_paid_leave": True,
                        "count_holidays": True},
        "proration": {"on_joining": True, "on_exit": True, "basis": "months", "joining_cutoff_day": 15,
                      "rounding": "half", "round_mode": "nearest"},
        "probation": {"credit": "full", "reduced_quota": 0, "waiting_days": 0},
        "experience_levels": [],
        "max_balance": None,
    },
    "year_end": {
        "year_start_month": 1,
        "reset": True,
        "carry_forward": {"enabled": False, "max_days": 0, "percent": 100, "expire_after_months": None,
                          "max_accumulation": None},
        "encashment": {"enabled": False, "max_days": 0, "percent": 100, "min_balance_after": 0, "on_exit": False},
    },
    "restrictions": {
        "min_days": None, "max_days": None, "max_consecutive": None,
        "notice_days": 0, "notice_above_days": 0,
        "backdated_days": 30, "future_days": 365,
        "count_weekends": False, "count_holidays": False,
        "sandwich": {"weekends": False, "holidays": False, "min_days": 0, "adjacent": False},
        "clubbing_blocked": [],
        "document_after_days": None,
        "negative": {"mode": "none", "limit": 0},
        "max_requests": {"count": None, "period": "year"},
        "min_gap_days": None,
        "only_optional_holidays": False,
        "hidden_from_employee": False,
    },
    "grant": {"options": [], "max_requests": None, "period": "tenure", "min_gap_days": None},
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def with_defaults(rules: dict | None) -> dict:
    return _merge(DEFAULT_RULES, rules or {})


def _num(value, label: str, *, minimum: float = 0, maximum: float = 100000, allow_none: bool = True, whole=False):
    if value in (None, ""):
        if allow_none:
            return None
        raise BadRequest(f"{label} is needed.")
    try:
        n = Decimal(str(value))
    except Exception:
        raise BadRequest(f"{label} must be a number.")
    if n < Decimal(str(minimum)) or n > Decimal(str(maximum)):
        raise BadRequest(f"{label} must be between {minimum} and {maximum}.")
    if whole:
        if n != n.to_integral_value():
            raise BadRequest(f"{label} must be a whole number.")
        return int(n)
    return float(n)


def _ids(values) -> list:
    out = []
    for v in values or []:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return sorted(set(out))


def clean_rules(rules: dict | None, *, code: str | None = None) -> dict:
    """Fill defaults, check every value and enforce the legal minimums."""
    r = with_defaults(rules)
    if r["paid"] not in ("paid", "unpaid", "lop"):
        raise BadRequest("Choose whether the leave is paid, unpaid or loss of pay.")
    if r["legal_kind"] not in LEGAL_KINDS:
        r["legal_kind"] = "none"
    if r["unit"] not in ("day", "hour"):
        raise BadRequest("Leave is counted in days or hours.")
    r["hours_per_day"] = _num(r["hours_per_day"], "Hours in a working day", minimum=1, maximum=24, allow_none=False)
    r["pay_percent"] = _num(r.get("pay_percent", 100), "Pay kept on leave (%)", minimum=0, maximum=100,
                            allow_none=False)
    if r["paid"] != "paid":
        r["pay_percent"] = 0
    for k in ("allow_half_day", "allow_quarter_day", "allow_hourly", "statutory"):
        r[k] = bool(r[k])

    a = r["applicability"]
    if a.get("mode") not in ("all", "pickers", "rule"):
        a["mode"] = "all"
    for k in ("genders", "marital_statuses", "employment_types", "locations", "states", "departments", "designations",
              "grades", "tags", "employees", "exclude_employees"):
        a[k] = _ids(a.get(k))
    a["min_tenure_days"] = _num(a.get("min_tenure_days") or 0, "Minimum service", maximum=20000, whole=True) or 0
    if a.get("probation") not in ("any", "only", "exclude"):
        a["probation"] = "any"
    a["rule"] = (a.get("rule") or "").strip()
    if a["rule"]:
        from core import expressions
        try:
            expressions.parse(a["rule"])
        except expressions.ExpressionError as e:
            raise BadRequest(f"Who gets this leave: {e}")

    e = r["entitlement"]
    if e["mode"] not in MODES:
        raise BadRequest("Choose how the leave is earned.")
    e["yearly_quota"] = _num(e.get("yearly_quota") or 0, "Days per year", maximum=400) or 0
    if e["frequency"] not in FREQUENCIES:
        raise BadRequest("Choose how often leave is credited.")
    if e["credit_at"] not in ("start", "end"):
        e["credit_at"] = "start"
    dw = e["days_worked"]
    dw["per_days"] = _num(dw.get("per_days"), "Days worked for each day of leave", minimum=1, maximum=365, allow_none=False)
    dw["credit"] = _num(dw.get("credit"), "Leave earned", minimum=0.25, maximum=30, allow_none=False)
    dw["min_days_in_year"] = _num(dw.get("min_days_in_year") or 0, "Days of work before leave is earned", maximum=366,
                                  whole=True) or 0
    dw["count_paid_leave"] = bool(dw.get("count_paid_leave", True))
    dw["count_holidays"] = bool(dw.get("count_holidays", True))
    p = e["proration"]
    p["on_joining"], p["on_exit"] = bool(p.get("on_joining")), bool(p.get("on_exit"))
    if p.get("basis") not in ("days", "months"):
        p["basis"] = "months"
    p["joining_cutoff_day"] = _num(p.get("joining_cutoff_day") or 0, "Joining cut-off day", maximum=31, whole=True) or 0
    if p.get("rounding") not in ROUNDINGS:
        p["rounding"] = "half"
    if p.get("round_mode") not in ("nearest", "down", "up"):
        p["round_mode"] = "nearest"
    pr = e["probation"]
    if pr.get("credit") not in ("full", "none", "reduced"):
        pr["credit"] = "full"
    pr["reduced_quota"] = _num(pr.get("reduced_quota") or 0, "Days per year during probation", maximum=400) or 0
    pr["waiting_days"] = _num(pr.get("waiting_days") or 0, "Waiting period", maximum=3650, whole=True) or 0
    levels = []
    for lv in e.get("experience_levels") or []:
        yrs = _num(lv.get("after_years"), "Years of service", maximum=60, allow_none=False)
        q = _num(lv.get("yearly_quota"), "Days per year", maximum=400, allow_none=False)
        levels.append({"after_years": yrs, "yearly_quota": q})
    e["experience_levels"] = sorted(levels, key=lambda x: x["after_years"])
    e["max_balance"] = _num(e.get("max_balance"), "Maximum balance", maximum=1000)

    y = r["year_end"]
    y["year_start_month"] = _num(y.get("year_start_month") or 1, "Leave year start month", minimum=1, maximum=12,
                                 whole=True)
    y["reset"] = bool(y.get("reset", True))
    cf = y["carry_forward"]
    cf["enabled"] = bool(cf.get("enabled"))
    cf["max_days"] = _num(cf.get("max_days") or 0, "Most days carried forward", maximum=1000) or 0
    cf["percent"] = _num(cf.get("percent") if cf.get("percent") is not None else 100, "Carry forward percentage",
                         maximum=100) or 0
    cf["expire_after_months"] = _num(cf.get("expire_after_months"), "Carried leave expires after", minimum=1, maximum=60,
                                     whole=True)
    cf["max_accumulation"] = _num(cf.get("max_accumulation"), "Most carried leave in total", maximum=1000)
    en = y["encashment"]
    en["enabled"] = bool(en.get("enabled"))
    en["on_exit"] = bool(en.get("on_exit"))
    en["max_days"] = _num(en.get("max_days") or 0, "Most days encashed", maximum=1000) or 0
    en["percent"] = _num(en.get("percent") if en.get("percent") is not None else 100, "Encashment percentage",
                         maximum=100) or 0
    en["min_balance_after"] = _num(en.get("min_balance_after") or 0, "Balance to keep after encashment", maximum=1000) or 0

    s = r["restrictions"]
    for k, label in (("min_days", "Least days per request"), ("max_days", "Most days per request"),
                     ("max_consecutive", "Most days in a row"), ("document_after_days", "Document needed after")):
        s[k] = _num(s.get(k), label, maximum=1000)
    for k, label in (("notice_days", "Notice"), ("notice_above_days", "Notice applies above"),
                     ("backdated_days", "Past dates allowed"), ("future_days", "Future dates allowed"),
                     ("min_gap_days", "Gap between requests")):
        s[k] = _num(s.get(k), label, maximum=3650, whole=True)
    if s["min_days"] and s["max_days"] and s["min_days"] > s["max_days"]:
        raise BadRequest("The least days per request can't be more than the most days.")
    s["count_weekends"], s["count_holidays"] = bool(s.get("count_weekends")), bool(s.get("count_holidays"))
    sw = s["sandwich"]
    sw["weekends"], sw["holidays"], sw["adjacent"] = bool(sw.get("weekends")), bool(sw.get("holidays")), bool(sw.get("adjacent"))
    sw["min_days"] = _num(sw.get("min_days") or 0, "Sandwich applies from", maximum=365) or 0
    s["clubbing_blocked"] = sorted({str(c).strip().upper() for c in (s.get("clubbing_blocked") or []) if str(c).strip()})
    neg = s["negative"]
    if neg.get("mode") not in NEGATIVE_MODES:
        neg["mode"] = "none"
    neg["limit"] = _num(neg.get("limit") or 0, "Negative balance limit", maximum=365) or 0
    mr = s["max_requests"]
    mr["count"] = _num(mr.get("count"), "Most requests", minimum=1, maximum=1000, whole=True)
    if mr.get("period") not in ("month", "quarter", "year", "tenure"):
        mr["period"] = "year"
    s["only_optional_holidays"] = bool(s.get("only_optional_holidays"))
    s["hidden_from_employee"] = bool(s.get("hidden_from_employee"))

    g = r["grant"]
    opts = []
    for o in g.get("options") or []:
        key = str(o.get("key") or "").strip() or f"option{len(opts) + 1}"
        label = str(o.get("label") or "").strip() or key
        days = _num(o.get("days"), f"Days for “{label}”", minimum=0.5, maximum=1000, allow_none=False)
        opts.append({"key": key[:40], "label": label[:100], "days": days})
    g["options"] = opts
    g["max_requests"] = _num(g.get("max_requests"), "Most grants", minimum=1, maximum=100, whole=True)
    if g.get("period") not in ("year", "tenure"):
        g["period"] = "tenure"
    g["min_gap_days"] = _num(g.get("min_gap_days"), "Gap between grants", maximum=3650, whole=True)

    check_legal_floor(r)
    return r


def check_legal_floor(r: dict) -> None:
    """Refuse statutory policies that give less than the law."""
    kind = r.get("legal_kind")
    e = r["entitlement"]
    if kind == "maternity":
        days = {o["key"]: o["days"] for o in r["grant"]["options"]}
        if e["mode"] == "grant":
            if days.get("birth") is not None and days["birth"] < MATERNITY_BIRTH_DAYS:
                raise BadRequest("Maternity leave for the first two children can't be less than 26 weeks (182 days).")
            if days.get("birth_third") is not None and days["birth_third"] < MATERNITY_LATER_CHILD_DAYS:
                raise BadRequest("Maternity leave from the third child can't be less than 12 weeks (84 days).")
            for k in ("adoption", "commissioning"):
                if days.get(k) is not None and days[k] < MATERNITY_ADOPTION_DAYS:
                    raise BadRequest("Maternity leave for adoption or a commissioning mother can't be less than 12 weeks.")
        if r["paid"] != "paid" or float(r.get("pay_percent", 100)) < 100:
            raise BadRequest("Maternity leave must be paid in full.")
    if kind == "annual" and e["mode"] == "days_worked":
        dw = e["days_worked"]
        if Decimal(str(dw["per_days"])) / Decimal(str(dw["credit"])) > ANNUAL_LEAVE_PER_DAYS:
            raise BadRequest("Annual leave can't be less than 1 day for every 20 days worked.")
        if dw["min_days_in_year"] > ANNUAL_LEAVE_MIN_DAYS_WORKED:
            raise BadRequest("Annual leave must start after at most 180 days of work in the year.")


def summary_lines(r: dict) -> list[str]:
    """Short plain sentences describing a policy (shown on cards and in history)."""
    r = with_defaults(r)
    e, y, s = r["entitlement"], r["year_end"], r["restrictions"]
    out = []
    if e["mode"] == "fixed":
        out.append(f"{_fmt(e['yearly_quota'])} days a year, credited at the start of the year")
    elif e["mode"] == "accrual":
        per = FREQUENCIES[e["frequency"]]
        out.append(f"{_fmt(e['yearly_quota'])} days a year, {_fmt(e['yearly_quota'] / per)} credited "
                   f"{e['frequency'].replace('_', '-')} at the {e['credit_at']} of each period")
    elif e["mode"] == "days_worked":
        dw = e["days_worked"]
        out.append(f"{_fmt(dw['credit'])} day for every {_fmt(dw['per_days'])} days worked"
                   + (f", after {dw['min_days_in_year']} days of work in the year" if dw["min_days_in_year"] else ""))
    elif e["mode"] == "grant":
        out.append("Credited when a request is approved")
        for o in r["grant"]["options"]:
            out.append(f"{o['label']}: {_fmt(o['days'])} days")
    if y["carry_forward"]["enabled"]:
        out.append(f"Carry forward up to {_fmt(y['carry_forward']['max_days'])} days")
    if y["encashment"]["enabled"]:
        out.append(f"Encash up to {_fmt(y['encashment']['max_days'])} days")
    if s["sandwich"]["weekends"] or s["sandwich"]["holidays"]:
        out.append("Weekends and holidays between leave days are counted")
    if r["paid"] == "paid" and float(r.get("pay_percent", 100)) < 100:
        out.append(f"Paid at {_fmt(r['pay_percent'])} % of the daily wage")
    if s["document_after_days"]:
        out.append(f"Document needed above {_fmt(s['document_after_days'])} days")
    return out


def _fmt(n) -> str:
    n = float(n or 0)
    return str(int(n)) if n == int(n) else f"{n:g}"
