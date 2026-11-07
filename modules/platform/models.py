"""Platform-side accounts and money: the people who run Campusly (admins), the
partners who bring companies in, subscription payments recorded by hand after a UPI
transfer, the commissions those payments earn, and payouts.

Platform users are entirely separate from company logins. They never carry a
company_id and their tokens carry scope="platform", so a company token cannot
reach these screens and vice versa.
"""
from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from core.db import Base


class PlatformUser(Base):
    __tablename__ = "platform_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    name = Column(String(120), nullable=False)
    email = Column(String(255), unique=True, nullable=False)
    phone = Column(String(20), nullable=True)
    password = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False)  # 'admin' | 'executive'
    ref_code = Column(String(12), unique=True, nullable=True)  # executives only
    upi_id = Column(String(100), nullable=True)  # where an executive wants payouts
    is_active = Column(Boolean, nullable=False, server_default=text("true"))
    failed_logins = Column(Integer, nullable=False, server_default=text("0"))
    locked_until = Column(DateTime(timezone=True), nullable=True)
    token_version = Column(Integer, nullable=False, server_default=text("0"))  # bump to log out everywhere
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    created_by = Column(Integer, ForeignKey("platform_users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SubscriptionPayment(Base):
    """One manual confirmation that a company paid (UPI, cash, anything)."""
    __tablename__ = "subscription_payments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    amount = Column(Numeric(10, 2), nullable=False)
    plan = Column(String(30), nullable=False)  # 'standard' | 'standard_whatsapp'
    months = Column(Integer, nullable=False, server_default=text("1"))
    period_start = Column(DateTime(timezone=True), nullable=False)
    period_end = Column(DateTime(timezone=True), nullable=False)
    paid_at = Column(DateTime(timezone=True), nullable=False)
    reference = Column(String(100), nullable=True)  # UPI transaction id or note
    note = Column(Text, nullable=True)
    details = Column(JSONB, nullable=True)  # the deal bought: plan, price, features, period, discount, coupon
    recorded_by = Column(Integer, ForeignKey("platform_users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    commissions = relationship("Commission", back_populates="payment")


class PaymentCode(Base):
    """A short code put in the UPI payment note when a company opens its Subscription page.
    It records exactly what the company chose (plan, period, coupon, amount), so the admin can
    type the code seen in the payment app and confirm the right payment for the right company."""
    __tablename__ = "payment_codes"
    __table_args__ = (Index("ix_payment_codes_period", "company_id", "period_start"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(16), unique=True, nullable=False)
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    plan = Column(String(30), nullable=False)
    months = Column(Integer, nullable=False)
    coupon_code = Column(String(30), nullable=True)
    coupon_percent = Column(Integer, nullable=False, server_default=text("0"))
    discount_percent = Column(Integer, nullable=False, server_default=text("0"))  # yearly discount at the time
    amount = Column(Numeric(10, 2), nullable=False)
    period_start = Column(Date, nullable=False)  # the exact period this payment pays for
    period_end = Column(Date, nullable=False)
    details = Column(JSONB, nullable=True)  # the deal shown to the company, frozen at that moment
    status = Column(String(20), nullable=False, server_default=text("'pending'"))  # pending | paid
    payment_id = Column(Integer, ForeignKey("subscription_payments.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    paid_at = Column(DateTime(timezone=True), nullable=True)


class Commission(Base):
    """What an executive earned from one payment, computed when the payment is recorded."""
    __tablename__ = "commissions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform_user_id = Column(Integer, ForeignKey("platform_users.id"), nullable=False, index=True)
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    payment_id = Column(Integer, ForeignKey("subscription_payments.id", ondelete="CASCADE"), nullable=False)
    kind = Column(String(20), nullable=False)  # 'bonus' | 'percent'
    amount = Column(Numeric(10, 2), nullable=False)
    status = Column(String(20), nullable=False, server_default=text("'pending'"))  # pending | requested | paid
    payout_id = Column(Integer, ForeignKey("payouts.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    payment = relationship("SubscriptionPayment", back_populates="commissions")


class Payout(Base):
    """An executive asking for their pending balance, and the admin paying it."""
    __tablename__ = "payouts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform_user_id = Column(Integer, ForeignKey("platform_users.id"), nullable=False, index=True)
    amount = Column(Numeric(10, 2), nullable=False)
    upi_id = Column(String(100), nullable=False)
    status = Column(String(20), nullable=False, server_default=text("'requested'"))  # requested | paid | rejected
    reference = Column(String(100), nullable=True)
    note = Column(Text, nullable=True)
    requested_at = Column(DateTime(timezone=True), server_default=func.now())
    settled_at = Column(DateTime(timezone=True), nullable=True)
    settled_by = Column(Integer, ForeignKey("platform_users.id"), nullable=True)


class PlatformSetting(Base):
    """Single-row JSON bag: prices, commission rules, the UPI id companies pay to."""
    __tablename__ = "platform_settings"

    id = Column(Integer, primary_key=True)
    data = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
