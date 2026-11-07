"""Platform area: admin screens (companies, payments, executives, payouts, settings),
the marketing executive portal (their companies, earnings, redeem), and the public
referral-code check used by the registration form.

Money is manual on purpose: a company pays the platform UPI id, an admin records
the payment here, and the record extends the company's paid period and creates the
executive's commission in the same transaction.
"""
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import redis
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from core.config import get_settings
from core.db import enable_bypass, get_db
from core.security import hash_password, verify_password
from modules.platform.security import (LOCK_MINUTES, MAX_FAILED_LOGINS, client_ip, create_platform_token,
                                      current_platform_user, platform_admin, platform_executive)
from modules.company.models import Company
from modules.employees.models import Employee
from modules.platform.models import Commission, PaymentCode, Payout, PlatformSetting, PlatformUser, SubscriptionPayment
from modules.platform.subscription import (find_period, find_plan, get_periods, get_plans, plan_label, platform_settings, quote,
                                           subscription_state)
from core.cache import get_redis

router = APIRouter(prefix="/platform", tags=["Platform"])
settings = get_settings()

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L


def _redis():
    return get_redis()


def _limit(key: str, max_hits: int, window_s: int) -> None:
    """Simple fixed-window limiter; raises 429 past the limit."""
    try:
        r = _redis()
        n = r.incr(key)
        if n == 1:
            r.expire(key, window_s)
        if n > max_hits:
            raise HTTPException(status_code=429, detail="Too many attempts. Please try again later.")
    except redis.RedisError:
        pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_code(db: Session) -> str:
    for _ in range(20):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        if not db.query(PlatformUser.id).filter(PlatformUser.ref_code == code).scalar():
            return code
    raise HTTPException(status_code=500, detail="Could not allocate a referral code")


def _user_out(u: PlatformUser) -> dict:
    return {"id": u.id, "name": u.name, "email": u.email, "phone": u.phone, "role": u.role, "ref_code": u.ref_code,
            "upi_id": u.upi_id, "is_active": u.is_active, "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
            "created_at": u.created_at.isoformat() if u.created_at else None}


# ───────────────────────────── auth ─────────────────────────────

class PlatformLogin(BaseModel):
    email: EmailStr
    password: str = Field(..., max_length=200)


class ChangePassword(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=10, max_length=200)


@router.post("/auth/login")
def platform_login(req: PlatformLogin, request: Request, db: Session = Depends(get_db)):
    # the platform area works across companies (like every signed-in platform request), so the
    # sign-in's own audit entry, which belongs to no company, can be written
    enable_bypass(db)
    _limit(f"platform:login:ip:{client_ip(request)}", 20, 900)
    generic = HTTPException(status_code=401, detail="Incorrect email or password")
    user = db.query(PlatformUser).filter(func.lower(PlatformUser.email) == req.email.lower()).first()
    if not user or not user.is_active:
        raise generic
    if user.locked_until and user.locked_until > _now():
        raise HTTPException(status_code=423, detail="Account locked after repeated failures. Try again in a few minutes.")
    if not verify_password(req.password, user.password):
        user.failed_logins = (user.failed_logins or 0) + 1
        if user.failed_logins >= MAX_FAILED_LOGINS:
            user.locked_until = _now() + timedelta(minutes=LOCK_MINUTES)
            user.failed_logins = 0
        db.commit()
        raise generic
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = _now()
    from core import audit, context
    ctx = context.current()
    ctx.platform_user_id, ctx.actor_type, ctx.actor_name = user.id, "platform", user.name or user.email
    audit.event(db, "auth.platform_signed_in", "platform_users", user.id, company_id=None, entity_label=user.email)
    db.commit()
    return {"access_token": create_platform_token(user), "token_type": "bearer", "user": _user_out(user)}


@router.get("/auth/me")
def platform_me(user: PlatformUser = Depends(current_platform_user)):
    return _user_out(user)


@router.post("/auth/change-password")
def platform_change_password(req: ChangePassword, user: PlatformUser = Depends(current_platform_user), db: Session = Depends(get_db)):
    if not verify_password(req.current_password, user.password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    user.password = hash_password(req.new_password)
    user.token_version = (user.token_version or 0) + 1  # every other session is logged out
    db.commit()
    return {"access_token": create_platform_token(user), "token_type": "bearer"}


@router.post("/auth/logout")
def platform_logout(user: PlatformUser = Depends(current_platform_user), db: Session = Depends(get_db)):
    user.token_version = (user.token_version or 0) + 1
    db.commit()
    return {"ok": True}


# ───────────────────────── public referral check ─────────────────────────

@router.get("/referral/{code}")
def check_referral(code: str, request: Request, db: Session = Depends(get_db)):
    """Used by the registration form. Rate limited so codes cannot be enumerated."""
    _limit(f"platform:ref:ip:{client_ip(request)}", 30, 900)
    code = re.sub(r"[^A-Z0-9]", "", code.upper())[:12]
    u = db.query(PlatformUser).filter(PlatformUser.ref_code == code, PlatformUser.is_active == True, PlatformUser.role == "executive").first()
    if not u:
        return {"valid": False}
    first = (u.name or "").split()[0] if u.name else ""
    return {"valid": True, "name": first}


def resolve_referral(db: Session, code: str | None) -> int | None:
    """Called by registration. Returns the executive id or raises if the code is wrong."""
    if not code or not code.strip():
        return None
    clean = re.sub(r"[^A-Z0-9]", "", code.upper())[:12]
    u = db.query(PlatformUser).filter(PlatformUser.ref_code == clean, PlatformUser.is_active == True, PlatformUser.role == "executive").first()
    if not u:
        raise HTTPException(status_code=400, detail="Referral code not found. Leave it blank or check the code.")
    return u.id


# ───────────────────────────── public: pricing ─────────────────────────────

@router.get("/pricing")
def public_pricing(db: Session = Depends(get_db)):
    """Plans and payment choices for the public landing page: the same ones companies see on
    their Subscription page, switched-on ones only. No coupons or payment details."""
    from modules.platform.subscription import trial_plan, paid_plans
    data = platform_settings(db)
    tp = trial_plan(data)
    return {
        "trial_days": int(tp.get("trial_days") or 0) if tp else 0,
        "trial": ({k: tp.get(k) for k in ("id", "name", "tagline", "features", "whatsapp", "trial_days", "badge")} if tp else None),
        "plans": [{k: p.get(k) for k in ("id", "name", "tagline", "price", "per_learner", "max_learners", "features", "whatsapp", "badge")} for p in paid_plans(data)],
        "periods": [{k: p.get(k) for k in ("id", "label", "months", "discount")} for p in get_periods(data, active_only=True)],
    }


# ───────────────────────────── admin: settings ─────────────────────────────

class PlanIn(BaseModel):
    id: str = Field(..., min_length=2, max_length=30, pattern=r"^[a-z0-9_]+$")
    name: str = Field(..., min_length=1, max_length=60)
    tagline: str = Field("", max_length=120)
    price: int = Field(..., ge=0, le=1000000)        # per month (what the tier bills)
    per_learner: int = Field(0, ge=0, le=100000)     # ₹ per learner per month, shown on price cards
    max_learners: int = Field(0, ge=0, le=1000000)   # learner limit of the tier (0 = no limit)
    features: list[str] = Field(default_factory=list, max_length=20)
    whatsapp: bool = False                            # switches on WhatsApp self-service
    active: bool = True
    order: int = 0
    trial: bool = False                               # the free trial new companies start on
    trial_days: int = Field(0, ge=0, le=365)
    badge: str = Field("", max_length=30)             # e.g. "Most popular": shown on the card and highlights it


class PeriodIn(BaseModel):
    id: str = Field(..., min_length=2, max_length=30, pattern=r"^[a-z0-9_]+$")
    label: str = Field(..., min_length=1, max_length=40)
    months: int = Field(..., ge=1, le=36)
    discount: int = Field(0, ge=0, le=90)            # % off
    coupons: bool = False                             # coupon codes work on this period
    active: bool = True
    order: int = 0


class UpiAccountIn(BaseModel):
    id: str = Field(..., min_length=1, max_length=40)
    label: str = Field("", max_length=60)        # e.g. "SBI ··7557"
    upi_id: str = Field(..., min_length=3, max_length=100)
    upi_name: str = Field("", max_length=100)


class CouponIn(BaseModel):
    code: str = Field(..., min_length=2, max_length=30, pattern=r"^[A-Za-z0-9_-]+$")
    percent: int = Field(..., ge=1, le=90)
    applies_to: str = Field("yearly", pattern="^yearly$")  # coupons only apply to yearly payment
    starts_on: str = Field("", max_length=10)   # "" = works from today
    expires_on: str = Field("", max_length=10)  # "" = never ends
    active: bool = True


class SettingsUpdate(BaseModel):
    price_standard: int = Field(3000, ge=0, le=100000)
    price_standard_whatsapp: int = Field(10000, ge=0, le=100000)
    first_payment_bonus: int = Field(300, ge=0, le=100000)
    percent: int = Field(10, ge=0, le=100)
    percent_months: int = Field(6, ge=0, le=120)
    min_payout: int = Field(500, ge=0, le=100000)
    upi_id: str = Field("", max_length=100)
    upi_name: str = Field("", max_length=100)
    support_phone: str = Field("", max_length=20)
    discount_12: int = Field(20, ge=0, le=90)
    trial_days: int = Field(30, ge=0, le=365)
    coupons: list[CouponIn] = Field(default_factory=list, max_length=200)
    upi_accounts: list[UpiAccountIn] = Field(default_factory=list, max_length=20)
    plans: list[PlanIn] = Field(default_factory=list, max_length=20)
    periods: list[PeriodIn] = Field(default_factory=list, max_length=12)
    active_upi: str = Field("", max_length=40)


def _settings_out(db: Session) -> dict:
    data = platform_settings(db)
    data["plans"], data["periods"] = get_plans(data), get_periods(data)  # defaults shown as editable rows
    return data


@router.get("/admin/settings")
def get_platform_settings(_: PlatformUser = Depends(platform_admin), db: Session = Depends(get_db)):
    return _settings_out(db)


@router.put("/admin/settings")
def put_platform_settings(req: SettingsUpdate, _: PlatformUser = Depends(platform_admin), db: Session = Depends(get_db)):
    row = db.query(PlatformSetting).filter(PlatformSetting.id == 1).first()
    if not row:
        row = PlatformSetting(id=1, data={})
        db.add(row)
    data = req.model_dump()
    # one entry per code, stored upper-case
    seen, coupons = set(), []
    for c in data["coupons"]:
        c["code"] = c["code"].strip().upper()
        c["applies_to"] = "yearly"
        if c["code"] in seen:
            raise HTTPException(status_code=400, detail=f"Coupon {c['code']} is listed twice")
        seen.add(c["code"])
        coupons.append(c)
    data["coupons"] = coupons
    # plans and periods: unique ids, at least one of each switched on
    for key, what in (("plans", "plan"), ("periods", "payment choice")):
        items = data.get(key) or []
        ids = [x["id"] for x in items]
        if len(ids) != len(set(ids)):
            raise HTTPException(status_code=400, detail=f"Two {what}s have the same id")
        if items and not any(x["active"] for x in items):
            raise HTTPException(status_code=400, detail=f"Keep at least one {what} switched on")
        for x in items:
            if key == "plans":
                x["features"] = [f.strip() for f in x["features"] if f.strip()]
    trials = [x for x in data.get("plans") or [] if x.get("trial") and x.get("active")]
    if len(trials) > 1:
        raise HTTPException(status_code=400, detail="Only one plan can be the free trial")
    for x in data.get("plans") or []:
        if x.get("trial"):
            x["price"] = 0
            if x.get("active") and x.get("trial_days", 0) < 1:
                raise HTTPException(status_code=400, detail="Set how many days the free trial lasts")
    if data.get("plans") and not any(x.get("active") and not x.get("trial") for x in data["plans"]):
        raise HTTPException(status_code=400, detail="Keep at least one paid plan switched on")
    for x in data.get("plans") or []:
        x["badge"] = (x.get("badge") or "").strip()
    months = [x["months"] for x in data.get("periods") or []]
    if len(months) != len(set(months)):
        raise HTTPException(status_code=400, detail="Two payment choices have the same number of months")
    # keep the old single-price fields in step (partner portal and older screens read them)
    for x in data.get("plans") or []:
        if x["id"] == "standard":
            data["price_standard"] = x["price"]
        if x["id"] == "standard_whatsapp":
            data["price_standard_whatsapp"] = x["price"]
    for x in data.get("periods") or []:
        if x["months"] == 12:
            data["discount_12"] = x["discount"]
    # keep upi_id / upi_name in step with the chosen account
    accts = data.get("upi_accounts") or []
    if accts:
        chosen = next((x for x in accts if x["id"] == data.get("active_upi")), accts[0])
        data["active_upi"] = chosen["id"]
        data["upi_id"], data["upi_name"] = chosen["upi_id"].strip(), chosen["upi_name"].strip()
    row.data = data
    db.commit()
    return _settings_out(db)


# ───────────────────────────── admin: companies ─────────────────────────────

@router.get("/admin/companies")
def admin_companies(_: PlatformUser = Depends(platform_admin), db: Session = Depends(get_db)):
    rows = db.query(Company).order_by(Company.created_at.desc()).all()
    employee_counts = dict(db.query(Employee.company_id, func.count(Employee.id)).filter(Employee.active == True).group_by(Employee.company_id).all())  # noqa: E712
    execs = {u.id: u for u in db.query(PlatformUser).filter(PlatformUser.role == "executive").all()}
    paid_totals = dict(db.query(SubscriptionPayment.company_id, func.coalesce(func.sum(SubscriptionPayment.amount), 0)).group_by(SubscriptionPayment.company_id).all())
    out = []
    for c in rows:
        cfg = c.config or {}
        if cfg.get("test_account") and not cfg.get("demo_company"):
            continue
        ref = execs.get(c.referred_by_id) if c.referred_by_id else None
        out.append({
            "id": c.id, "name": c.company_name, "slug": c.slug, "email": c.email, "phone": c.phone, "city": c.city,
            "industry": c.industry,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "employees": employee_counts.get(c.id, 0),
            "subscription": subscription_state(c),
            "paid_total": float(paid_totals.get(c.id, 0) or 0),
            "referred_by": {"id": ref.id, "name": ref.name, "ref_code": ref.ref_code} if ref else None,
            "demo": bool(cfg.get("demo_company")),
            "active": bool(c.active),
        })
    return out


def _month_keys(n: int) -> list[str]:
    """The last n months as 'YYYY-MM', oldest first, ending with this month."""
    d = date.today().replace(day=1)
    keys = []
    for _ in range(n):
        keys.append(d.strftime("%Y-%m"))
        d = (d - timedelta(days=1)).replace(day=1)
    return keys[::-1]


@router.get("/admin/analytics")
def admin_analytics(_: PlatformUser = Depends(platform_admin), db: Session = Depends(get_db)):
    """Numbers for the admin dashboard: growth, money, usage, partners and what needs attention."""
    today = date.today()
    month_start = today.replace(day=1)
    last_month_start = (month_start - timedelta(days=1)).replace(day=1)
    companies = [b for b in db.query(Company).all() if not ((b.config or {}).get("test_account") and not (b.config or {}).get("demo_company"))]
    ids = [b.id for b in companies]
    by_id = {b.id: b for b in companies}
    states = {b.id: subscription_state(b) for b in companies}
    months = _month_keys(12)

    # ── companies ──
    status_counts = {"active": 0, "trial": 0, "grace": 0, "expired": 0}
    for st in states.values():
        status_counts[st["status"]] = status_counts.get(st["status"], 0) + 1
    disabled = sum(1 for b in companies if not b.active)
    signups = {m: 0 for m in months}
    for b in companies:
        if b.created_at:
            k = b.created_at.strftime("%Y-%m")
            if k in signups:
                signups[k] += 1
    running, cum = 0, []
    before = sum(1 for b in companies if b.created_at and b.created_at.strftime("%Y-%m") < months[0])
    running = before
    for m in months:
        running += signups[m]
        cum.append(running)

    plan_mix: dict[str, int] = {}
    for st in states.values():
        if st["status"] == "active":
            name = st.get("plan_name") or st.get("plan") or "Starter"
            plan_mix[name] = plan_mix.get(name, 0) + 1
    cities: dict[str, int] = {}
    for b in companies:
        c = (b.city or "").strip().title() or "Not set"
        cities[c] = cities.get(c, 0) + 1

    # ── money ──
    pays = db.query(SubscriptionPayment).filter(SubscriptionPayment.company_id.in_(ids)).all() if ids else []
    rev = {m: 0.0 for m in months}
    count = {m: 0 for m in months}
    by_plan: dict[str, float] = {}
    by_period: dict[str, int] = {}
    payers = set()
    mrr = 0.0
    for p in pays:
        amt = float(p.amount or 0)
        k = p.paid_at.strftime("%Y-%m") if p.paid_at else ""
        if k in rev:
            rev[k] += amt
            count[k] += 1
        d = p.details or {}
        pname = (d.get("plan") or {}).get("name") or plan_label(p.plan)
        by_plan[pname] = by_plan.get(pname, 0) + amt
        per = (d.get("period") or {}).get("label") or ("Yearly" if p.months >= 12 else "Monthly" if p.months == 1 else f"{p.months} months")
        by_period[per] = by_period.get(per, 0) + 1
        payers.add(p.company_id)
        ps, pe = p.period_start.date() if p.period_start else None, p.period_end.date() if p.period_end else None
        if ps and pe and ps <= today <= pe:
            mrr += amt / max(1, p.months or 1)
    rev_total = sum(float(p.amount or 0) for p in pays)
    rev_this = rev.get(months[-1], 0.0)
    rev_last = rev.get(months[-2], 0.0)

    # ── usage ──
    employee_counts = dict(db.query(Employee.company_id, func.count(Employee.id)).filter(Employee.company_id.in_(ids), Employee.active == True).group_by(Employee.company_id).all()) if ids else {}  # noqa: E712
    top = sorted(employee_counts.items(), key=lambda kv: -kv[1])[:8]

    # ── partners ──
    execs = db.query(PlatformUser).filter(PlatformUser.role == "executive").all()
    paid_by_company: dict[int, float] = {}
    for p in pays:
        paid_by_company[p.company_id] = paid_by_company.get(p.company_id, 0) + float(p.amount or 0)
    earned = dict(db.query(Commission.platform_user_id, func.coalesce(func.sum(Commission.amount), 0)).group_by(Commission.platform_user_id).all())
    partners = []
    for e in execs:
        mine = [b for b in companies if b.referred_by_id == e.id]
        partners.append({"name": e.name, "companies": len(mine), "paying": sum(1 for b in mine if states[b.id]["status"] == "active"),
                         "revenue": round(sum(paid_by_company.get(b.id, 0) for b in mine)), "earned": float(earned.get(e.id, 0) or 0)})
    partners.sort(key=lambda x: (-x["revenue"], -x["companies"]))

    # ── needs attention ──
    expiring = sorted(
        [{"id": b.id, "name": b.company_name or b.email, "status": states[b.id]["status"], "days_left": states[b.id]["days_left"]}
         for b in companies if b.active and states[b.id]["days_left"] is not None and states[b.id]["days_left"] <= 14 and states[b.id]["status"] in ("trial", "active", "grace")],
        key=lambda x: x["days_left"])[:8]
    pending_codes = db.query(func.count(PaymentCode.id)).filter(PaymentCode.status == "pending").scalar() or 0
    pending_payouts = db.query(func.count(Payout.id)).filter(Payout.status == "requested").scalar() or 0

    return {
        "kpis": {
            "companies": len(companies), "new_this_month": signups[months[-1]], "new_last_month": signups[months[-2]],
            "paying": status_counts["active"], "trial": status_counts["trial"], "grace": status_counts["grace"], "expired": status_counts["expired"], "disabled": disabled,
            "revenue_total": round(rev_total), "revenue_this_month": round(rev_this), "revenue_last_month": round(rev_last), "mrr": round(mrr),
            "arpc": round(rev_total / len(payers)) if payers else 0, "conversion": round(100 * len(payers) / len(companies)) if companies else 0,
            "employees_total": sum(employee_counts.values()),
            "pending_codes": pending_codes, "pending_payouts": pending_payouts,
        },
        "months": [{"month": m, "revenue": round(rev[m]), "payments": count[m], "signups": signups[m], "total_companies": cum[i]} for i, m in enumerate(months)],
        "status": [{"status": k, "count": v} for k, v in status_counts.items()],
        "plan_mix": [{"plan": k, "count": v} for k, v in sorted(plan_mix.items(), key=lambda kv: -kv[1])],
        "revenue_by_plan": [{"plan": k, "revenue": round(v)} for k, v in sorted(by_plan.items(), key=lambda kv: -kv[1])],
        "period_mix": [{"period": k, "count": v} for k, v in sorted(by_period.items(), key=lambda kv: -kv[1])],
        "cities": [{"city": k, "count": v} for k, v in sorted(cities.items(), key=lambda kv: -kv[1])[:8]],
        "top_companies": [{"name": by_id[i].company_name or by_id[i].email, "city": by_id[i].city, "employees": n} for i, n in top],
        "partners": partners[:8],
        "expiring": expiring,
    }


@router.get("/admin/infra")
def admin_infra(refresh: bool = False, _: PlatformUser = Depends(platform_admin)):
    """Railway usage and cost for the dashboard (cached a few minutes)."""
    from modules.platform.railway_stats import snapshot
    return snapshot(force=refresh)


class RecordPayment(BaseModel):
    amount: int = Field(..., ge=1, le=1000000)
    plan: str = Field(..., min_length=2, max_length=30)
    months: int = Field(1, ge=1, le=36)
    coupon: str | None = Field(None, max_length=30)
    paid_at: str | None = None       # ISO date, defaults to today
    reference: str | None = Field(None, max_length=100)
    note: str | None = Field(None, max_length=500)


def _add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    last = [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, last))


def apply_payment(db: Session, b: Company, admin_id: int | None, amount: int, plan: str, months: int,
                  paid_on: date, reference: str | None, note: str | None,
                  period: tuple[date, date] | None = None, details: dict | None = None) -> tuple[SubscriptionPayment, date, dict | None]:
    """Record one confirmed payment: extend paid-until, set the plan, earn commission. Does not commit.
    `period` pins the exact dates paid for (from a payment code); otherwise they follow paid-until."""
    cfg = dict(b.config or {})
    current_until = None
    if cfg.get("paid_until"):
        try:
            current_until = date.fromisoformat(str(cfg["paid_until"])[:10])
        except ValueError:
            current_until = None
    start = current_until + timedelta(days=1) if current_until and current_until >= date.today() else paid_on
    end = _add_months(start, months) - timedelta(days=1)
    if period:
        start, end = period

    pay = SubscriptionPayment(
        company_id=b.id, amount=Decimal(amount), plan=plan, months=months,
        period_start=datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc),
        period_end=datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc),
        paid_at=datetime.combine(paid_on, datetime.min.time(), tzinfo=timezone.utc),
        reference=(reference or "").strip() or None, note=note, recorded_by=admin_id, details=details,
    )
    db.add(pay)
    db.flush()

    if not current_until or end > current_until:
        cfg["paid_until"] = end.isoformat()
    cfg["plan"] = plan
    if details:
        # the deal this company now has; later changes to plans or prices don't touch it
        cfg["plan_details"] = {**details, "period_start": start.isoformat(), "period_end": end.isoformat(), "paid_on": paid_on.isoformat()}
    feats = dict(cfg.get("features") or {})
    feats["whatsapp"] = bool(details["plan"]["whatsapp"]) if details else plan == "standard_whatsapp"
    cfg["features"] = feats
    b.config = cfg
    b.subscription_plan = "paid"

    commission = None
    if b.referred_by_id:
        ex = db.query(PlatformUser).filter(PlatformUser.id == b.referred_by_id, PlatformUser.role == "executive").first()
        if ex and ex.is_active:
            rules = platform_settings(db)
            prior = db.query(func.count(SubscriptionPayment.id)).filter(SubscriptionPayment.company_id == b.id, SubscriptionPayment.id != pay.id).scalar() or 0
            rows = []
            if prior == 0 and rules["first_payment_bonus"] > 0:
                rows.append(Commission(platform_user_id=ex.id, company_id=b.id, payment_id=pay.id, kind="bonus", amount=Decimal(rules["first_payment_bonus"])))
            # percentage applies to the first N months of paid service for this company
            months_before = db.query(func.coalesce(func.sum(SubscriptionPayment.months), 0)).filter(SubscriptionPayment.company_id == b.id, SubscriptionPayment.id != pay.id).scalar() or 0
            eligible = max(0, min(months, rules["percent_months"] - int(months_before)))
            if eligible > 0 and rules["percent"] > 0:
                share = Decimal(amount) * Decimal(eligible) / Decimal(months) * Decimal(rules["percent"]) / Decimal(100)
                rows.append(Commission(platform_user_id=ex.id, company_id=b.id, payment_id=pay.id, kind="percent", amount=share.quantize(Decimal("0.01"))))
            for r in rows:
                db.add(r)
            commission = {"executive": ex.name, "amount": float(sum((r.amount for r in rows), Decimal(0)))}
    return pay, end, commission


@router.post("/admin/companies/{company_id}/payments")
def record_payment(company_id: int, req: RecordPayment, admin: PlatformUser = Depends(platform_admin), db: Session = Depends(get_db)):
    """Confirm a UPI payment: extends the paid period, sets the plan, earns commission."""
    b = db.query(Company).filter(Company.id == company_id).first()
    if not b:
        raise HTTPException(status_code=404, detail="Institution not found")
    if req.reference:
        dup = db.query(SubscriptionPayment.id).filter(SubscriptionPayment.company_id == b.id, SubscriptionPayment.reference == req.reference.strip()).scalar()
        if dup:
            raise HTTPException(status_code=409, detail="A payment with this reference is already recorded")
    try:
        paid_on = date.fromisoformat(req.paid_at[:10]) if req.paid_at else date.today()
    except ValueError:
        raise HTTPException(status_code=400, detail="paid_at must be a date")
    if paid_on > date.today():
        raise HTTPException(status_code=400, detail="Payment date cannot be in the future")
    rules = platform_settings(db)
    plan = find_plan(rules, req.plan)
    if not plan:
        raise HTTPException(status_code=400, detail="Unknown plan")
    per = find_period(rules, months=req.months) or {"id": f"m{req.months}", "label": f"{req.months} months", "months": req.months, "discount": 0, "coupons": False}
    details = quote(rules, plan, per, req.coupon)["details"]
    details["amount"] = req.amount  # what was actually received
    _, end, commission = apply_payment(db, b, admin.id, req.amount, req.plan, req.months, paid_on, req.reference, req.note, details=details)
    db.commit()
    return {"ok": True, "paid_until": end.isoformat(), "plan": req.plan, "commission": commission, "subscription": subscription_state(b)}


# ───────────────────────────── admin: payment codes ─────────────────────────────

def _norm_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def _period_label(start: date, end: date, months: int) -> str:
    """'November 2026' when the period is a calendar month, else '6 Nov – 5 Dec 2026'."""
    if months == 1 and start.day == 1 and (end + timedelta(days=1)).day == 1:
        return start.strftime("%B %Y")
    if months == 12 and start.day == 1 and start.month == 1:
        return f"Year {start.year}"
    fmt = lambda d, y: f"{d.day} {d.strftime('%b')}" + (f" {d.year}" if y else "")
    return f"{fmt(start, start.year != end.year)} – {fmt(end, True)}"


def _code_view(db: Session, pc: PaymentCode) -> dict:
    b = db.query(Company).filter(Company.id == pc.company_id).first()
    state = subscription_state(b) if b else None
    cfg = (b.config or {}) if b else {}
    current_until = None
    if cfg.get("paid_until"):
        try:
            current_until = date.fromisoformat(str(cfg["paid_until"])[:10])
        except ValueError:
            current_until = None
    pay = db.query(SubscriptionPayment).filter(SubscriptionPayment.id == pc.payment_id).first() if pc.payment_id else None
    ref = db.query(PlatformUser).filter(PlatformUser.id == b.referred_by_id).first() if b and b.referred_by_id else None
    return {
        "code": pc.code, "status": pc.status,
        "company": {"id": b.id, "name": b.company_name, "slug": b.slug, "phone": b.phone, "email": b.email, "city": b.city} if b else None,
        "plan": pc.plan, "months": pc.months,
        "plan_name": ((pc.details or {}).get("plan") or {}).get("name") or plan_label(pc.plan),
        "period": ((pc.details or {}).get("period") or {}).get("label") or ("Yearly" if pc.months == 12 else "Monthly" if pc.months == 1 else f"{pc.months} months"),
        "details": pc.details,
        "discount_percent": pc.discount_percent, "coupon_code": pc.coupon_code, "coupon_percent": pc.coupon_percent,
        "amount": float(pc.amount), "created_at": pc.created_at.isoformat() if pc.created_at else None,
        "paid_at": pc.paid_at.isoformat() if pc.paid_at else None,
        "current_paid_until": current_until.isoformat() if current_until else None,
        "subscription": state,
        "period_start": pc.period_start.isoformat(), "period_end": pc.period_end.isoformat(),
        "period_label": _period_label(pc.period_start, pc.period_end, pc.months),
        "already_covered": bool(pc.status == "pending" and current_until and pc.period_end <= current_until),
        "payment_id": pay.id if pay else None,
        "referred_by": ref.name if ref else None,
    }


@router.get("/admin/payment-codes/{code}")
def lookup_payment_code(code: str, _: PlatformUser = Depends(platform_admin), db: Session = Depends(get_db)):
    """Everything about a payment code seen in the UPI payment note."""
    pc = db.query(PaymentCode).filter(PaymentCode.code == _norm_code(code)).first()
    if not pc:
        raise HTTPException(status_code=404, detail="No payment with this code")
    return _code_view(db, pc)


@router.get("/admin/payment-codes")
def recent_payment_codes(status: str = "pending", _: PlatformUser = Depends(platform_admin), db: Session = Depends(get_db)):
    """Latest codes (default: still waiting), for the Find payment page."""
    q = db.query(PaymentCode)
    if status in ("pending", "paid"):
        q = q.filter(PaymentCode.status == status)
    return [_code_view(db, pc) for pc in q.order_by(PaymentCode.created_at.desc()).limit(30).all()]


class ConfirmCode(BaseModel):
    paid_at: str | None = None                        # ISO date, defaults to today
    amount: int | None = Field(None, ge=1, le=1000000)  # only when the company paid a different amount
    utr: str | None = Field(None, max_length=40)


@router.post("/admin/payment-codes/{code}/confirm")
def confirm_payment_code(code: str, req: ConfirmCode, admin: PlatformUser = Depends(platform_admin), db: Session = Depends(get_db)):
    """The money with this code arrived: record it for the company and activate the plan."""
    pc = db.query(PaymentCode).filter(PaymentCode.code == _norm_code(code)).with_for_update().first()
    if not pc:
        raise HTTPException(status_code=404, detail="No payment with this code")
    if pc.status == "paid":
        raise HTTPException(status_code=409, detail="This payment is already confirmed")
    b = db.query(Company).filter(Company.id == pc.company_id).first()
    if not b:
        raise HTTPException(status_code=404, detail="Institution not found")
    try:
        paid_on = date.fromisoformat(req.paid_at[:10]) if req.paid_at else date.today()
    except ValueError:
        raise HTTPException(status_code=400, detail="paid_at must be a date")
    if paid_on > date.today():
        raise HTTPException(status_code=400, detail="Payment date cannot be in the future")
    amount = req.amount or int(pc.amount)
    bits = [f"Code {pc.code}"]
    if pc.coupon_code:
        bits.append(f"coupon {pc.coupon_code} ({pc.coupon_percent}%)")
    if req.utr:
        bits.append(f"UTR {req.utr.strip()}")
    bits.insert(1, _period_label(pc.period_start, pc.period_end, pc.months))
    pay, end, commission = apply_payment(db, b, admin.id, amount, pc.plan, pc.months, paid_on, pc.code, " · ".join(bits),
                                         period=(pc.period_start, pc.period_end),
                                         details={**(pc.details or {}), "amount": amount} if pc.details else None)
    pc.status = "paid"
    pc.payment_id = pay.id
    pc.paid_at = datetime.combine(paid_on, datetime.min.time(), tzinfo=timezone.utc)
    db.commit()
    return {"ok": True, "paid_until": end.isoformat(), "commission": commission, "payment": _code_view(db, pc)}


class NewCompany(BaseModel):
    company_name: str = Field(..., min_length=2, max_length=255)
    slug: str = Field(..., min_length=3, max_length=60)
    owner_name: str = Field(..., min_length=2, max_length=120)
    email: EmailStr
    phone: str | None = Field(None, max_length=20)
    password: str | None = Field(None, max_length=128)  # empty: one is generated and returned once
    referred_by_id: int | None = None                    # partner (executive) who brought the company in


@router.get("/admin/slug-check")
def admin_slug_check(slug: str = "", _: PlatformUser = Depends(platform_admin), db: Session = Depends(get_db)):
    """Is this web address free? (for the New company form)"""
    from modules.company.router import _validate_slug
    from modules.company.slugs import unique_slug
    try:
        return {"slug": _validate_slug(slug, 0, db), "available": True}
    except HTTPException as e:
        out = {"slug": (slug or "").strip().lower(), "available": False, "reason": e.detail}
        if "taken" in str(e.detail) or "reserved" in str(e.detail):
            out["suggestion"] = unique_slug(db, slug)
        return out


@router.post("/admin/companies")
def admin_create_company(req: NewCompany, admin: PlatformUser = Depends(platform_admin), db: Session = Depends(get_db)):
    """Create a company on behalf of its owner, the same way sign-up does: free trial and
    Starter plan. Returns the login; a generated password is shown once."""
    from modules.company.router import _validate_slug
    from seed.seed_data import setup_company
    email = req.email.strip().lower()
    if db.query(Company.id).filter(func.lower(Company.email) == email).first():
        raise HTTPException(status_code=400, detail="An institution with this email already exists.")
    slug = _validate_slug(req.slug, 0, db)
    if req.password and len(req.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    password = req.password or secrets.token_urlsafe(9)
    referred_by_id = None
    if req.referred_by_id:
        ex = db.query(PlatformUser).filter(PlatformUser.id == req.referred_by_id, PlatformUser.role == "executive").first()
        if not ex:
            raise HTTPException(status_code=400, detail="Unknown partner")
        referred_by_id = ex.id
    c = Company(
        company_name=req.company_name.strip(), slug=slug, email=email,
        phone=(req.phone or "").strip() or None, password=hash_password(password),
        config={"owner_name": req.owner_name.strip(), "plan": "standard", "created_by_admin": admin.id},
        referred_by_id=referred_by_id,
    )
    from modules.platform.subscription import trial_days as _trial_days
    c.trial_ends_at = _now() + timedelta(days=_trial_days(db))
    db.add(c)
    db.commit()
    db.refresh(c)
    setup_company(db, c.id)
    host = settings.FRONTEND_URL.split("://", 1)
    login_url = f"{host[0]}://{slug}.{host[1]}/login" if len(host) == 2 else "/login"
    return {"ok": True, "id": c.id, "name": c.company_name, "slug": slug, "email": email,
            "login_url": login_url, "password": None if req.password else password}


class RemoveCompany(BaseModel):
    confirm_name: str = Field(..., min_length=1, max_length=255)


@router.post("/admin/companies/{company_id}/remove")
def remove_company_endpoint(company_id: int, req: RemoveCompany, admin: PlatformUser = Depends(platform_admin), db: Session = Depends(get_db)):
    """Permanently delete a company and all its data (for clearing test companies). The admin
    must type the company's exact name; the screen asks twice before sending this."""
    from core.security import forget_company_status
    from modules.platform.company_removal import remove_company
    b = db.query(Company).filter(Company.id == company_id).first()
    if not b:
        raise HTTPException(status_code=404, detail="Institution not found")
    expected = (b.company_name or "").strip() or (b.email or "")
    if req.confirm_name.strip() != expected:
        raise HTTPException(status_code=400, detail="The name you typed does not match this institution.")
    name, slug = expected, b.slug
    try:
        removed = remove_company(db, b.id)
        from core import audit
        audit.event(db, "platform.company_removed", "companies", None,
                    {"company_id": company_id, "name": name, "slug": slug, "rows": sum(removed.values())},
                    company_id=None)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Could not remove the institution; nothing was deleted. ({type(e).__name__})")
    forget_company_status(company_id)
    return {"ok": True, "removed": name, "rows": sum(removed.values()), "tables": removed}


class CompanyPassword(BaseModel):
    password: str = Field(..., min_length=8, max_length=128)


@router.post("/admin/companies/{company_id}/password")
def set_company_password(company_id: int, req: CompanyPassword, _: PlatformUser = Depends(platform_admin), db: Session = Depends(get_db)):
    """Support: set a company owner's login password (e.g. a Google sign-up that wants email login too)."""
    b = db.query(Company).filter(Company.id == company_id).first()
    if not b:
        raise HTTPException(status_code=404, detail="Institution not found")
    b.password = hash_password(req.password)
    db.commit()
    return {"ok": True}


@router.get("/admin/companies/{company_id}/payments")
def company_payments(company_id: int, _: PlatformUser = Depends(platform_admin), db: Session = Depends(get_db)):
    rows = db.query(SubscriptionPayment).filter(SubscriptionPayment.company_id == company_id).order_by(SubscriptionPayment.paid_at.desc()).all()
    return [{"id": p.id, "amount": float(p.amount), "plan": p.plan, "months": p.months, "period_start": p.period_start.date().isoformat(),
             "period_end": p.period_end.date().isoformat(), "paid_at": p.paid_at.date().isoformat(), "reference": p.reference, "note": p.note, "details": p.details} for p in rows]


class CompanyUpdate(BaseModel):
    plan: str | None = Field(None, min_length=2, max_length=30)
    extend_trial_days: int | None = Field(None, ge=1, le=90)
    active: bool | None = None


@router.put("/admin/companies/{company_id}")
def update_company(company_id: int, req: CompanyUpdate, _: PlatformUser = Depends(platform_admin), db: Session = Depends(get_db)):
    b = db.query(Company).filter(Company.id == company_id).first()
    if not b:
        raise HTTPException(status_code=404, detail="Institution not found")
    cfg = dict(b.config or {})
    if req.plan:
        plan = find_plan(platform_settings(db), req.plan)
        if not plan:
            raise HTTPException(status_code=400, detail="Unknown plan")
        cfg["plan"] = req.plan
        feats = dict(cfg.get("features") or {})
        feats["whatsapp"] = bool(plan.get("whatsapp"))
        cfg["features"] = feats
    if req.extend_trial_days:
        base = b.trial_ends_at or _now()
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
        b.trial_ends_at = max(base, _now()) + timedelta(days=req.extend_trial_days)
    if req.active is not None:
        b.active = req.active
        from core.security import forget_company_status
        forget_company_status(b.id)  # takes effect at once for its logins
    b.config = cfg
    db.commit()
    return {"ok": True, "subscription": subscription_state(b), "active": b.active}


# ───────────────────────────── admin: platform users ─────────────────────────────

class UserCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    email: EmailStr
    phone: str | None = Field(None, max_length=20)
    role: str = Field(..., pattern="^(admin|executive)$")
    password: str = Field(..., min_length=10, max_length=200)


class UserUpdate(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=120)
    phone: str | None = Field(None, max_length=20)
    is_active: bool | None = None
    new_password: str | None = Field(None, min_length=10, max_length=200)


def _strip_html(v: str) -> str:
    return re.sub(r"<[^>]+>", "", v or "").strip()


@router.get("/admin/users")
def admin_users(_: PlatformUser = Depends(platform_admin), db: Session = Depends(get_db)):
    users = db.query(PlatformUser).order_by(PlatformUser.role, PlatformUser.created_at.desc()).all()
    referred = dict(db.query(Company.referred_by_id, func.count(Company.id)).filter(Company.referred_by_id.isnot(None)).group_by(Company.referred_by_id).all())
    earned = dict(db.query(Commission.platform_user_id, func.coalesce(func.sum(Commission.amount), 0)).group_by(Commission.platform_user_id).all())
    pending = dict(db.query(Commission.platform_user_id, func.coalesce(func.sum(Commission.amount), 0)).filter(Commission.status == "pending").group_by(Commission.platform_user_id).all())
    out = []
    for u in users:
        d = _user_out(u)
        d.update({"companies": referred.get(u.id, 0), "earned": float(earned.get(u.id, 0) or 0), "pending": float(pending.get(u.id, 0) or 0)})
        out.append(d)
    return out


@router.post("/admin/users")
def create_user(req: UserCreate, admin: PlatformUser = Depends(platform_admin), db: Session = Depends(get_db)):
    if db.query(PlatformUser.id).filter(func.lower(PlatformUser.email) == req.email.lower()).scalar():
        raise HTTPException(status_code=409, detail="A platform user with this email already exists")
    u = PlatformUser(name=_strip_html(req.name), email=req.email.lower(), phone=req.phone, role=req.role,
                     password=hash_password(req.password), created_by=admin.id,
                     ref_code=_new_code(db) if req.role == "executive" else None)
    db.add(u)
    db.commit()
    return _user_out(u)


@router.put("/admin/users/{user_id}")
def update_user(user_id: int, req: UserUpdate, admin: PlatformUser = Depends(platform_admin), db: Session = Depends(get_db)):
    u = db.query(PlatformUser).filter(PlatformUser.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    if req.is_active is False:
        if u.id == admin.id:
            raise HTTPException(status_code=400, detail="You cannot deactivate your own account")
        if u.role == "admin" and db.query(func.count(PlatformUser.id)).filter(PlatformUser.role == "admin", PlatformUser.is_active == True).scalar() <= 1:
            raise HTTPException(status_code=400, detail="At least one active admin must remain")
    if req.name is not None:
        u.name = _strip_html(req.name)
    if req.phone is not None:
        u.phone = req.phone
    if req.is_active is not None:
        u.is_active = req.is_active
        if not req.is_active:
            u.token_version = (u.token_version or 0) + 1
    if req.new_password:
        u.password = hash_password(req.new_password)
        u.token_version = (u.token_version or 0) + 1
        u.failed_logins = 0
        u.locked_until = None
    db.commit()
    return _user_out(u)


# ───────────────────────────── admin: payouts ─────────────────────────────

class PayoutSettle(BaseModel):
    status: str = Field(..., pattern="^(paid|rejected)$")
    reference: str | None = Field(None, max_length=100)
    note: str | None = Field(None, max_length=500)


@router.get("/admin/payouts")
def admin_payouts(_: PlatformUser = Depends(platform_admin), db: Session = Depends(get_db)):
    rows = db.query(Payout, PlatformUser).join(PlatformUser, PlatformUser.id == Payout.platform_user_id).order_by(Payout.requested_at.desc()).all()
    return [{"id": p.id, "executive": {"id": u.id, "name": u.name, "email": u.email}, "amount": float(p.amount), "upi_id": p.upi_id, "status": p.status,
             "reference": p.reference, "note": p.note, "requested_at": p.requested_at.isoformat() if p.requested_at else None,
             "settled_at": p.settled_at.isoformat() if p.settled_at else None} for p, u in rows]


@router.put("/admin/payouts/{payout_id}")
def settle_payout(payout_id: int, req: PayoutSettle, admin: PlatformUser = Depends(platform_admin), db: Session = Depends(get_db)):
    p = db.query(Payout).filter(Payout.id == payout_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Payout not found")
    if p.status != "requested":
        raise HTTPException(status_code=400, detail="This payout is already settled")
    p.status = req.status
    p.reference = req.reference
    p.note = req.note
    p.settled_at = _now()
    p.settled_by = admin.id
    new_status = "paid" if req.status == "paid" else "pending"
    for c in db.query(Commission).filter(Commission.payout_id == p.id).all():
        c.status = new_status
        if req.status == "rejected":
            c.payout_id = None
    db.commit()
    return {"ok": True, "status": p.status}


# ───────────────────────────── executive portal ─────────────────────────────

def _exec_balances(db: Session, uid: int) -> dict:
    sums = dict(db.query(Commission.status, func.coalesce(func.sum(Commission.amount), 0)).filter(Commission.platform_user_id == uid).group_by(Commission.status).all())
    pending = float(sums.get("pending", 0) or 0)
    requested = float(sums.get("requested", 0) or 0)
    paid = float(sums.get("paid", 0) or 0)
    return {"pending": pending, "requested": requested, "paid": paid, "earned": pending + requested + paid}


@router.get("/me/summary")
def exec_summary(user: PlatformUser = Depends(platform_executive), db: Session = Depends(get_db)):
    companies = db.query(Company).filter(Company.referred_by_id == user.id).all()
    paid_ids = {r[0] for r in db.query(SubscriptionPayment.company_id).filter(SubscriptionPayment.company_id.in_([c.id for c in companies] or [0])).distinct().all()}
    rules = platform_settings(db)
    return {
        "user": _user_out(user),
        "ref_code": user.ref_code,
        "share_url": f"{settings.FRONTEND_URL.rstrip('/')}/register?ref={user.ref_code}",
        "leads": sum(1 for c in companies if c.id not in paid_ids),
        "converted": len(paid_ids),
        "balances": _exec_balances(db, user.id),
        "rules": {"first_payment_bonus": rules["first_payment_bonus"], "percent": rules["percent"], "percent_months": rules["percent_months"], "min_payout": rules["min_payout"]},
        "prices": {"standard": rules["price_standard"], "standard_whatsapp": rules["price_standard_whatsapp"]},
        "plans": [{"id": p["id"], "name": p["name"], "price": p["price"]} for p in get_plans(rules, active_only=True)],
    }


@router.get("/me/companies")
def exec_companies(user: PlatformUser = Depends(platform_executive), db: Session = Depends(get_db)):
    """Only what the executive needs: the company's name, city, when it joined and whether it pays. No contact data."""
    companies = db.query(Company).filter(Company.referred_by_id == user.id).order_by(Company.created_at.desc()).all()
    first_paid = dict(db.query(SubscriptionPayment.company_id, func.min(SubscriptionPayment.paid_at)).group_by(SubscriptionPayment.company_id).all())
    earned = dict(db.query(Commission.company_id, func.coalesce(func.sum(Commission.amount), 0)).filter(Commission.platform_user_id == user.id).group_by(Commission.company_id).all())
    out = []
    for c in companies:
        st = subscription_state(c)
        out.append({"id": c.id, "name": c.company_name, "city": c.city, "joined_at": c.created_at.date().isoformat() if c.created_at else None,
                    "status": st["status"], "plan": st["plan"], "first_paid_at": first_paid[c.id].date().isoformat() if c.id in first_paid else None,
                    "earned": float(earned.get(c.id, 0) or 0)})
    return out


@router.get("/me/commissions")
def exec_commissions(user: PlatformUser = Depends(platform_executive), db: Session = Depends(get_db)):
    rows = (db.query(Commission, Company.company_name, SubscriptionPayment.paid_at)
            .join(Company, Company.id == Commission.company_id)
            .join(SubscriptionPayment, SubscriptionPayment.id == Commission.payment_id)
            .filter(Commission.platform_user_id == user.id).order_by(Commission.created_at.desc()).all())
    return [{"id": c.id, "company": name, "kind": c.kind, "amount": float(c.amount), "status": c.status, "date": paid_at.date().isoformat()} for c, name, paid_at in rows]


@router.get("/me/payouts")
def exec_payouts(user: PlatformUser = Depends(platform_executive), db: Session = Depends(get_db)):
    rows = db.query(Payout).filter(Payout.platform_user_id == user.id).order_by(Payout.requested_at.desc()).all()
    return [{"id": p.id, "amount": float(p.amount), "upi_id": p.upi_id, "status": p.status, "reference": p.reference, "note": p.note,
             "requested_at": p.requested_at.isoformat() if p.requested_at else None, "settled_at": p.settled_at.isoformat() if p.settled_at else None} for p in rows]


class RedeemRequest(BaseModel):
    upi_id: str = Field(..., min_length=5, max_length=100)


@router.post("/me/payouts")
def exec_redeem(req: RedeemRequest, user: PlatformUser = Depends(platform_executive), db: Session = Depends(get_db)):
    upi = req.upi_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{2,}@[A-Za-z]{2,}", upi):
        raise HTTPException(status_code=400, detail="Enter a valid UPI id, like name@bank")
    if db.query(Payout.id).filter(Payout.platform_user_id == user.id, Payout.status == "requested").scalar():
        raise HTTPException(status_code=400, detail="A payout request is already waiting")
    pending = db.query(Commission).filter(Commission.platform_user_id == user.id, Commission.status == "pending").with_for_update().all()
    total = sum((c.amount for c in pending), Decimal(0))
    rules = platform_settings(db)
    if total < Decimal(rules["min_payout"]):
        raise HTTPException(status_code=400, detail=f"Minimum payout is ₹{rules['min_payout']}")
    p = Payout(platform_user_id=user.id, amount=total, upi_id=upi)
    db.add(p)
    db.flush()
    for c in pending:
        c.status = "requested"
        c.payout_id = p.id
    user.upi_id = upi
    db.commit()
    return {"ok": True, "amount": float(total)}


class ExecProfile(BaseModel):
    phone: str | None = Field(None, max_length=20)
    upi_id: str | None = Field(None, max_length=100)


@router.put("/me/profile")
def exec_profile(req: ExecProfile, user: PlatformUser = Depends(platform_executive), db: Session = Depends(get_db)):
    if req.phone is not None:
        user.phone = req.phone.strip()
    if req.upi_id is not None:
        user.upi_id = req.upi_id.strip()
    db.commit()
    return _user_out(user)
