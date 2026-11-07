"""Trial and subscription state for a company.

Stored on the company: subscription_plan ('trial' | 'paid'), trial_ends_at, and
config.paid_until (ISO date). Enforcement is deliberately soft: the app keeps working
through a short grace period after the trial ends.
"""
from datetime import date, datetime, timedelta, timezone

TRIAL_DAYS = 30
GRACE_DAYS = 7

PLANS = ("standard", "standard_whatsapp")

DEFAULT_PLATFORM_SETTINGS = {
    "price_standard": 1250, "price_standard_whatsapp": 2000,
    "first_payment_bonus": 300, "percent": 10, "percent_months": 6, "min_payout": 500,
    "upi_id": "", "upi_name": "", "support_phone": "",
    # free trial for every new company (sign-up, Google sign-up, created by admin)
    "trial_days": TRIAL_DAYS,
    # Bank accounts companies can pay to; the one whose id is active_upi is used on the Subscription page
    # [{id, label, upi_id, upi_name}]
    "upi_accounts": [], "active_upi": "",
    # % off when a company pays for a full year at once
    "discount_12": 20,
    # Plans and billing periods companies can choose. Empty = the built-in defaults below.
    # plans:   [{id, name, tagline, price (per month), features: [str], whatsapp: bool, active, order}]
    # periods: [{id, label, months, discount (%), coupons (bool), active, order}]
    "plans": [], "periods": [],
    # Coupon codes for an extra % off, on periods that allow coupons: [{code, percent, starts_on/expires_on: "YYYY-MM-DD"|"", active}]
    "coupons": [],
}


DEFAULT_FEATURES = {
    "standard": ["Employee records and documents", "Attendance, shifts and leave", "Payroll with PF, ESI, PT and TDS",
                 "Employee self-service on web and mobile", "Reports and compliance calendar"],
    "standard_whatsapp": ["Everything in Standard", "Employee self-service on WhatsApp", "Payslips and leave balance on WhatsApp",
                          "Uses your own company number"],
}


def get_plans(data: dict, active_only: bool = False) -> list[dict]:
    """Plans made in admin, or the two built-in ones (priced from the legacy price fields)."""
    saved = data.get("plans") or []
    plans = saved or [
        {"id": "standard", "name": "Standard", "tagline": "Everything to run HR and payroll", "price": int(data.get("price_standard") or 0),
         "features": DEFAULT_FEATURES["standard"], "whatsapp": False, "active": True, "order": 1},
        {"id": "standard_whatsapp", "name": "Standard + WhatsApp", "tagline": "Employees get answers on WhatsApp", "price": int(data.get("price_standard_whatsapp") or 0),
         "features": DEFAULT_FEATURES["standard_whatsapp"], "whatsapp": True, "active": True, "order": 2},
    ]
    # No plan flagged as the trial yet (older settings): show the trial as a plan row, using
    # the old trial_days setting, so it can be edited like any other plan.
    if not any(p.get("trial") for p in plans):
        plans = [{"id": "free_trial", "name": "Free trial", "tagline": "Try everything before you pay", "price": 0,
                  "features": ["Full access to every feature", "No card needed", "Pick a plan only when you're ready"],
                  "whatsapp": True, "active": True, "order": 0, "trial": True,
                  "trial_days": int(data.get("trial_days") or 30)}] + list(plans)
    plans = sorted(plans, key=lambda p: (p.get("order") or 0))
    return [p for p in plans if p.get("active", True)] if active_only else plans


def paid_plans(data: dict) -> list[dict]:
    """Plans a company can pay for (the trial plan is not for sale)."""
    return [p for p in get_plans(data, active_only=True) if not p.get("trial")]


def get_periods(data: dict, active_only: bool = False) -> list[dict]:
    """Billing periods made in admin, or Monthly + Yearly (yearly discount from the legacy field)."""
    periods = data.get("periods") or [
        {"id": "monthly", "label": "Monthly", "months": 1, "discount": 0, "coupons": False, "active": True, "order": 1},
        {"id": "yearly", "label": "Yearly", "months": 12, "discount": int(data.get("discount_12") or 0), "coupons": True, "active": True, "order": 2},
    ]
    periods = sorted(periods, key=lambda p: (p.get("months") or 0, p.get("order") or 0))
    return [p for p in periods if p.get("active", True)] if active_only else periods


def find_plan(data: dict, plan_id: str) -> dict | None:
    return next((p for p in get_plans(data) if p["id"] == plan_id), None)


def find_period(data: dict, period_id: str | None = None, months: int | None = None) -> dict | None:
    for p in get_periods(data):
        if (period_id and p["id"] == period_id) or (period_id is None and months is not None and int(p["months"]) == int(months)):
            return p
    return None


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    last = [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, last))


def next_period(company, months: int, today: date | None = None) -> tuple[date, date]:
    """The period a new payment would pay for: right after the paid-until date, or after the
    free trial while it is running, otherwise from today."""
    today = today or date.today()
    cfg = company.config or {}
    start = today
    paid_until = None
    if cfg.get("paid_until"):
        try:
            paid_until = date.fromisoformat(str(cfg["paid_until"])[:10])
        except ValueError:
            paid_until = None
    trial_end = company.trial_ends_at.date() if getattr(company, "trial_ends_at", None) and hasattr(company.trial_ends_at, "date") else None
    if paid_until and paid_until >= today:
        start = paid_until + timedelta(days=1)
    elif trial_end and trial_end >= today:
        start = trial_end + timedelta(days=1)
    return start, add_months(start, months) - timedelta(days=1)


def quote(data: dict, plan: dict, period: dict, coupon_code: str | None = None) -> dict:
    """What a company pays for a plan and period: monthly price × months, less the period's
    discount, less a coupon when the period allows coupons. Discounted totals round to the
    nearest 50. Also returns `details`, the copy of the deal stored with the subscription,
    so later price or plan changes never alter what a company already bought."""
    months = int(period["months"])
    base = int(plan["price"]) * months
    discount = int(period.get("discount") or 0)
    coupon = find_coupon(data, coupon_code, period) if coupon_code else None
    cp = coupon["percent"] if coupon else 0
    raw = base * (1 - discount / 100) * (1 - cp / 100)
    # clean round numbers for real prices; exact rupees for tiny ones (e.g. a ₹1 test plan)
    amount = base if not (discount or cp) else (int(round(raw / 50) * 50) if raw >= 500 else max(1, int(round(raw))))
    details = {
        "plan": {"id": plan["id"], "name": plan["name"], "tagline": plan.get("tagline", ""), "price": int(plan["price"]),
                 "features": list(plan.get("features") or []), "whatsapp": bool(plan.get("whatsapp"))},
        "period": {"id": period["id"], "label": period["label"], "months": months, "discount": discount},
        "coupon": {"code": coupon["code"], "percent": cp} if coupon else None,
        "base": base, "amount": amount,
    }
    return {"base": base, "discount_percent": discount, "coupon": coupon, "coupon_percent": cp, "amount": amount, "details": details}


def find_coupon(data: dict, code: str, period: dict | None = None) -> dict | None:
    """The active, unexpired coupon matching `code` (case-insensitive), or None.
    With `period`, also None when that period does not allow coupons."""
    code = (code or "").strip().upper()
    if not code:
        return None
    today = date.today().isoformat()
    for c in data.get("coupons") or []:
        if str(c.get("code", "")).strip().upper() != code or not c.get("active", True):
            continue
        if c.get("expires_on") and str(c["expires_on"])[:10] < today:
            return None
        if c.get("starts_on") and str(c["starts_on"])[:10] > today:
            return None
        if period is not None and not period.get("coupons"):
            return None
        return {"code": code, "percent": int(c.get("percent") or 0), "expires_on": c.get("expires_on") or ""}
    return None


def trial_plan(data: dict) -> dict | None:
    """The plan flagged as the free trial (switched on), if any."""
    return next((p for p in get_plans(data, active_only=True) if p.get("trial")), None)


def trial_days(db) -> int:
    """Free-trial length for a company registering now: the days on the trial plan, or 0
    when no trial plan is switched on. Companies already registered keep their end date."""
    tp = trial_plan(platform_settings(db))
    return int(tp.get("trial_days") or 0) if tp else 0


def platform_settings(db) -> dict:
    """Prices, commission rules and the UPI id companies pay to, with defaults filled in."""
    from modules.platform.models import PlatformSetting
    row = db.query(PlatformSetting).filter(PlatformSetting.id == 1).first()
    data = dict(DEFAULT_PLATFORM_SETTINGS)
    if row and isinstance(row.data, dict):
        data.update({k: v for k, v in row.data.items() if k in DEFAULT_PLATFORM_SETTINGS})
    # the chosen bank account decides where companies pay
    acct = next((a for a in data.get("upi_accounts") or [] if a.get("id") == data.get("active_upi")), None)
    if acct and acct.get("upi_id"):
        data["upi_id"], data["upi_name"] = acct["upi_id"], acct.get("upi_name") or data.get("upi_name", "")
    return data


def subscription_state(company) -> dict:
    now = datetime.now(timezone.utc)
    cfg = company.config or {}
    paid_until = None
    if cfg.get("paid_until"):
        try:
            paid_until = date.fromisoformat(str(cfg["paid_until"])[:10])
        except ValueError:
            paid_until = None
    trial_ends = company.trial_ends_at
    if trial_ends is not None and trial_ends.tzinfo is None:
        trial_ends = trial_ends.replace(tzinfo=timezone.utc)

    if paid_until and paid_until >= now.date():
        status, ends = "active", paid_until
    elif trial_ends is None:
        status, ends = "trial", None  # older company with no trial date: treat as trial
    elif now <= trial_ends:
        status, ends = "trial", trial_ends.date()
    elif now <= trial_ends + timedelta(days=GRACE_DAYS):
        status, ends = "grace", (trial_ends + timedelta(days=GRACE_DAYS)).date()
    else:
        status, ends = "expired", trial_ends.date()

    days_left = (ends - now.date()).days if ends else None
    plan = cfg.get("plan") or "standard"
    details = cfg.get("plan_details") if isinstance(cfg.get("plan_details"), dict) else None
    feats = cfg.get("features") or {}
    whatsapp = bool(feats["whatsapp"]) if "whatsapp" in feats else plan == "standard_whatsapp"
    return {
        "plan": plan,
        "plan_name": (details or {}).get("plan", {}).get("name") or ("Standard + WhatsApp" if plan == "standard_whatsapp" else "Standard"),
        "plan_details": details,  # the deal frozen at the last confirmed payment
        "paid": bool(paid_until and paid_until >= now.date()),
        "whatsapp": whatsapp,
        "status": status,
        "trial_ends_at": trial_ends.date().isoformat() if trial_ends else None,
        "paid_until": paid_until.isoformat() if paid_until else None,
        "days_left": days_left,
    }
