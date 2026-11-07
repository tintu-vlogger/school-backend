"""Group insurance and other benefit plans.

benefit_plans        a plan for a policy year: kind (group health, accident, life, top-up,
                     parental), insurer, policy and TPA details, coverage tiers with premiums,
                     sum insured (fixed or a multiple of salary), employer/employee premium
                     split, eligibility rule, enrolment windows, mandatory/auto-enrol flags
benefit_enrolments   one employee in one plan: tier, covered family members, sum insured,
                     premium split, monthly deduction, dates and status, and when the
                     addition/deletion was sent to the insurer
"""
from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from core.db import Base


def _uuid():
    return Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))


def _money(nullable=False, default="0"):
    return Column(Numeric(14, 2), nullable=nullable, server_default=text(default) if default is not None else None)


def _json(default="'{}'::jsonb"):
    return Column(JSONB, nullable=False, server_default=text(default))


class BenefitPlan(Base):
    __tablename__ = "benefit_plans"
    __table_args__ = (Index("ix_benefit_plans_company", "company_id", "active", "policy_end"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    name = Column(String(150), nullable=False)
    # gmc (group health) | gpa (accident) | gtl (term life) | topup | parental | other
    kind = Column(String(20), nullable=False, server_default=text("'gmc'"))
    insurer = Column(String(150), nullable=True)
    policy_number = Column(String(80), nullable=True)
    tpa = Column(String(150), nullable=True)
    broker = Column(String(150), nullable=True)
    contact_email = Column(String(150), nullable=True)
    contact_phone = Column(String(30), nullable=True)
    website = Column(String(200), nullable=True)
    description = Column(Text, nullable=True)
    policy_start = Column(Date, nullable=False)
    policy_end = Column(Date, nullable=False)
    # fixed | salary_multiple
    sum_insured_basis = Column(String(20), nullable=False, server_default=text("'fixed'"))
    sum_insured = _money()
    salary_multiple = Column(Numeric(6, 2), nullable=True)
    min_sum_insured = _money(nullable=True, default=None)
    max_sum_insured = _money(nullable=True, default=None)
    # [{code, label, spouse: bool, children: int, parents: int, premium}]  yearly premium per tier
    tiers = _json("'[]'::jsonb")
    # parental cover: [{min_age, max_age, premium}] per parent, instead of a flat tier premium
    age_bands = _json("'[]'::jsonb")
    # employer share: percent of the premium or a flat yearly amount
    employer_share_type = Column(String(10), nullable=False, server_default=text("'percent'"))
    employer_share_value = Column(Numeric(14, 2), nullable=False, server_default=text("100"))
    deduct_in_payroll = Column(Boolean, nullable=False, server_default=text("true"))
    eligibility_rule = Column(Text, nullable=True)           # expression, e.g. grade in ['M1','M2'] and age < 65
    waiting_days = Column(Integer, nullable=False, server_default=text("0"))
    # on_joining | first_of_month | first_of_next_month
    cover_starts = Column(String(30), nullable=False, server_default=text("'on_joining'"))
    mandatory = Column(Boolean, nullable=False, server_default=text("false"))
    auto_enrol = Column(Boolean, nullable=False, server_default=text("false"))
    joining_window_days = Column(Integer, nullable=False, server_default=text("30"))
    renewal_window_from = Column(Date, nullable=True)
    renewal_window_to = Column(Date, nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_by_name = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class BenefitEnrolment(Base):
    __tablename__ = "benefit_enrolments"
    __table_args__ = (Index("ix_benefit_enrolments_company_plan", "company_id", "plan_id", "status"),
                      Index("ix_benefit_enrolments_company_employee", "company_id", "employee_id"))

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    plan_id = Column(Integer, ForeignKey("benefit_plans.id"), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    tier = Column(String(30), nullable=False, server_default=text("'employee'"))
    # [{family_member_id, name, relation, gender, date_of_birth}]
    dependents = _json("'[]'::jsonb")
    sum_insured = _money()
    annual_premium = _money()
    employer_share = _money()
    employee_share = _money()
    monthly_deduction = _money()
    # enrolled | waived | terminated
    status = Column(String(20), nullable=False, server_default=text("'enrolled'"))
    source = Column(String(20), nullable=False, server_default=text("'hr'"))      # auto | employee | hr
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=True)
    waiver_reason = Column(String(300), nullable=True)
    addition_sent_on = Column(Date, nullable=True)
    deletion_sent_on = Column(Date, nullable=True)
    history = _json("'[]'::jsonb")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
