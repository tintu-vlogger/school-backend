"""Demo data for benefits: group health (compulsory, auto-enrolled), group term life at three
times salary and an optional parental cover with age-band premiums whose window is open."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy.orm import Session


def setup_company(db: Session, company_id: int) -> None:
    """Nothing to add: companies create their own plans."""
    return None


def seed_demo(db: Session, company_id: int) -> None:
    from core.context import RequestContext, use_context
    from core.timeutil import today_for_company_id
    from modules.benefits import service
    from modules.benefits.models import BenefitPlan
    if db.query(BenefitPlan.id).filter(BenefitPlan.company_id == company_id).first():
        return
    today = today_for_company_id(db, company_id)
    fy_start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
    end = date(fy_start.year + 1, 3, 31)
    with use_context(RequestContext(company_id=company_id, actor_type="system", actor_name="Demo data")):
        gmc = service.save_plan(db, company_id, {
            "name": "Group health insurance", "kind": "gmc", "insurer": "Star Health and Allied Insurance",
            "policy_number": "GMC/2026/104582", "tpa": "Medi Assist", "broker": "Marsh India",
            "contact_email": "claims@mediassist.example", "contact_phone": "1800 425 9449",
            "description": "Hospitalisation cover for you and your family, cashless at network hospitals.",
            "policy_start": fy_start, "policy_end": end, "sum_insured_basis": "fixed", "sum_insured": 500000,
            "tiers": [{"code": "employee", "label": "Employee only", "premium": 9000},
                      {"code": "family", "label": "Employee, spouse and 2 children", "spouse": True, "children": 2,
                       "premium": 18000}],
            "employer_share_type": "percent", "employer_share_value": 100, "deduct_in_payroll": False,
            "cover_starts": "on_joining", "mandatory": True, "auto_enrol": True, "joining_window_days": 30,
            "renewal_window_from": end - timedelta(days=30), "renewal_window_to": end - timedelta(days=6)})
        service.save_plan(db, company_id, {
            "name": "Group term life", "kind": "gtl", "insurer": "HDFC Life", "policy_number": "GTL/2026/88214",
            "description": "Life cover of three times your annual CTC (at least 10 lakh).",
            "policy_start": fy_start, "policy_end": end, "sum_insured_basis": "salary_multiple", "salary_multiple": 3,
            "min_sum_insured": 1000000, "max_sum_insured": 10000000,
            "tiers": [{"code": "employee", "label": "Employee", "premium": 2400}],
            "employer_share_type": "percent", "employer_share_value": 100, "deduct_in_payroll": False,
            "mandatory": True, "auto_enrol": True})
        service.save_plan(db, company_id, {
            "name": "Parental cover", "kind": "parental", "insurer": "Star Health and Allied Insurance",
            "policy_number": "GMC-P/2026/104583", "tpa": "Medi Assist",
            "description": "Optional cover for your parents or parents-in-law. Your share is deducted from salary.",
            "policy_start": fy_start, "policy_end": end, "sum_insured_basis": "fixed", "sum_insured": 300000,
            "tiers": [{"code": "two_parents", "label": "Up to 2 parents", "parents": 2, "premium": 0}],
            "age_bands": [{"min_age": 0, "max_age": 45, "premium": 9000}, {"min_age": 46, "max_age": 60, "premium": 14000},
                          {"min_age": 61, "max_age": 80, "premium": 21000}],
            "employer_share_type": "percent", "employer_share_value": 25, "deduct_in_payroll": True,
            "mandatory": False, "auto_enrol": False,
            "renewal_window_from": today.replace(day=1), "renewal_window_to": today.replace(day=1) + timedelta(days=29),
            "eligibility_rule": "months_of_service >= 0"})
        service.auto_enrol(db, company_id)
    db.flush()
    _ = gmc
