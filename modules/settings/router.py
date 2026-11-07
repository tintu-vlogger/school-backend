"""Company subscription: current plan, what can be bought, and the UPI payment code."""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.db import get_db
from core.security import get_current_user
from modules.company.models import Company

router = APIRouter(prefix="/settings", tags=["Settings"])


def _get_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


@router.get("/subscription")
def get_subscription(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Trial / paid state for the banner, plus how to pay (prices and the platform UPI id)."""
    from modules.platform.subscription import subscription_state, platform_settings, get_periods, paid_plans, trial_days as _td
    company = _get_company(db, current_user["company_id"])
    ps = platform_settings(db)
    out = subscription_state(company)
    # what the company can buy now (only switched-on plans and payment choices)
    out["plans"] = [{k: p.get(k) for k in ("id", "name", "tagline", "price", "features", "whatsapp", "badge")} for p in paid_plans(ps)]
    out["periods"] = [{k: p.get(k) for k in ("id", "label", "months", "discount", "coupons")} for p in get_periods(ps, active_only=True)]
    out["prices"] = {"standard": ps["price_standard"], "standard_whatsapp": ps["price_standard_whatsapp"]}
    out["discounts"] = {"1": 0, "12": ps["discount_12"]}
    out["trial_days"] = _td(db)
    today = date.today().isoformat()
    out["has_coupons"] = any(
        c.get("active", True) and str(c.get("starts_on") or "")[:10] <= today and (not c.get("expires_on") or str(c["expires_on"])[:10] >= today)
        for c in ps.get("coupons") or []
    ) and any(p["coupons"] for p in out["periods"])
    out["pay_to"] = {"upi_id": ps["upi_id"], "name": ps["upi_name"], "support_phone": ps["support_phone"], "note": f"Next Gen HRMS {company.slug}"}
    return out


class PaymentCodeRequest(BaseModel):
    plan: str = Field(..., min_length=2, max_length=30)
    period: str = Field(..., min_length=2, max_length=30)
    coupon: str | None = Field(None, max_length=30)


_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L


@router.post("/subscription/payment-code")
def subscription_payment_code(
    req: PaymentCodeRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The code for the UPI payment note. One code = one company paying one amount for one
    exact period (e.g. 6 Nov – 5 Dec). Reopening the page for the same unpaid period gives the
    same code; once that period is paid, the next period gets a new code."""
    import secrets
    from modules.platform.models import PaymentCode
    from modules.platform.subscription import platform_settings, quote, next_period, find_plan, find_period
    ps = platform_settings(db)
    plan, period = find_plan(ps, req.plan), find_period(ps, req.period)
    if not plan or not plan.get("active", True) or plan.get("trial") or not period or not period.get("active", True):
        raise HTTPException(status_code=400, detail="This plan or payment choice is not available")
    months = int(period["months"])
    company = _get_company(db, current_user["company_id"])
    q = quote(ps, plan, period, req.coupon)
    coupon = q["coupon"]
    start, end = next_period(company, months)
    existing = (db.query(PaymentCode)
                .filter(PaymentCode.company_id == company.id, PaymentCode.status == "pending",
                        PaymentCode.plan == req.plan, PaymentCode.months == months,
                        PaymentCode.coupon_code == (coupon["code"] if coupon else None),
                        PaymentCode.amount == q["amount"], PaymentCode.period_start == start)
                .order_by(PaymentCode.created_at.desc()).first())
    if not existing:
        for _ in range(10):
            code = "HR" + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))
            if not db.query(PaymentCode.id).filter(PaymentCode.code == code).scalar():
                break
        existing = PaymentCode(code=code, company_id=company.id, plan=req.plan, months=months, details=q["details"],
                               coupon_code=coupon["code"] if coupon else None, coupon_percent=q["coupon_percent"],
                               discount_percent=q["discount_percent"], amount=q["amount"],
                               period_start=start, period_end=end)
        db.add(existing)
        db.commit()
        db.refresh(existing)
    return {"code": existing.code, "amount": float(existing.amount), "base": q["base"],
            "period_start": existing.period_start.isoformat(), "period_end": existing.period_end.isoformat(),
            "discount_percent": q["discount_percent"], "coupon_percent": q["coupon_percent"]}


@router.get("/subscription/coupon")
def check_coupon(
    code: str,
    period: str | None = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Look up a coupon code for the Subscription page. 404 when it does not exist or has expired."""
    from modules.platform.subscription import platform_settings, find_coupon, find_period, get_periods
    ps = platform_settings(db)
    c = find_coupon(ps, code)
    if not c:
        raise HTTPException(status_code=404, detail="This coupon code isn't valid or has expired")
    per = find_period(ps, period) if period else None
    c["works_on"] = [p["id"] for p in get_periods(ps, active_only=True) if p.get("coupons")]
    if per and not per.get("coupons"):
        c["note"] = f"Coupons don't work on {per['label']} payment"
    return c
