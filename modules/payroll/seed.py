"""Company setup and development demo data for payroll, expenses and loans.

setup_company(db, company_id)  the statutory library (once), payroll settings, salary components,
                               structures, a monthly pay group with this year's periods, number
                               series, expense categories and loan types. Only adds what is missing.
seed_demo(db, company_id)      the demo company: salaries for everyone, payroll profiles across
                               Karnataka, Maharashtra and Tamil Nadu, declarations, a locked past
                               pay run with payslips, an open current run, expenses and a loan.
"""
from __future__ import annotations

import logging
import random
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from core.context import RequestContext, use_context
from core.timeutil import today_for_company_id

log = logging.getLogger("hrms")
_LIBRARY_DONE = False


def setup_company(db: Session, company_id: int) -> None:
    global _LIBRARY_DONE
    from modules.payroll import calc, defaults, statutory
    from modules.payroll import service as svc
    from modules.payroll.models import PayGroup, SalaryComponent, SalaryStructure
    if not _LIBRARY_DONE:
        statutory.ensure_library(db)
        _LIBRARY_DONE = True
    svc.settings_row(db, company_id)
    have = {c for (c,) in db.query(SalaryComponent.code).filter(SalaryComponent.company_id == company_id).all()}
    for (code, name, hi, kind, calc_, formula, wage_class, pf, esi, prorate, taxability, exemption, one_time, sort,
         extra) in defaults.COMPONENTS:
        if code in have:
            continue
        db.add(SalaryComponent(company_id=company_id, code=code, name=name, name_hi=hi, kind=kind, calc=calc_, formula=formula,
                               wage_class=wage_class, pf_wage=pf, esi_wage=esi, prorate=prorate, taxability=taxability,
                               tax_exemption=exemption, one_time=one_time, sort_order=sort, is_system=True,
                               in_ctc=extra.get("in_ctc", True), fbp=extra.get("fbp", False), fbp_max=extra.get("fbp_max"),
                               show_on_payslip=extra.get("show_on_payslip", True),
                               fbp_tax_free_regimes=extra.get("fbp_regimes", ["old", "new"])))
    db.flush()
    if not db.query(SalaryStructure.id).filter(SalaryStructure.company_id == company_id).first():
        import uuid as uuid_mod
        for s in defaults.STRUCTURES:
            db.add(SalaryStructure(company_id=company_id, lineage_id=uuid_mod.uuid4(), version=1, is_current=True,
                                   effective_from=date(2025, 4, 1), name=s["name"], code=s["code"], description=s["description"],
                                   min_ctc=s["min_ctc"], max_ctc=s["max_ctc"], lines=s["lines"], created_by_name="System"))
    if not db.query(PayGroup.id).filter(PayGroup.company_id == company_id).first():
        for g in defaults.PAY_GROUPS:
            db.add(PayGroup(company_id=company_id, **g))
        db.flush()
    today = today_for_company_id(db, company_id)
    fy_start = calc.fy_bounds(calc.fy_of(today))[0]
    for g in db.query(PayGroup).filter(PayGroup.company_id == company_id, PayGroup.active.is_(True)).all():
        svc.ensure_periods(db, company_id, g, fy_start)
    _series(db, company_id)
    _expense_and_loan_defaults(db, company_id)
    db.flush()


def _series(db: Session, company_id: int) -> None:
    from core.numbering import NumberSeries
    have = {k for (k,) in db.query(NumberSeries.key).filter(NumberSeries.company_id == company_id).all()}
    for key, name, prefix, pattern, desc in (
            ("pay_run", "Pay runs", "PR", "{PREFIX}/{FY}/{SEQ:3}", "Numbers for monthly and off-cycle pay runs."),
            ("settlement", "Final settlements", "FNF", "{PREFIX}/{FY}/{SEQ:3}", "Numbers for full and final settlements.")):
        if key not in have:
            db.add(NumberSeries(company_id=company_id, key=key, name=name, prefix=prefix, pattern=pattern,
                                reset="financial_year", active=True, description=desc, is_system=True))


def _expense_and_loan_defaults(db: Session, company_id: int) -> None:
    from modules.expenses.models import ExpenseCategory
    from modules.loans.models import LoanType
    from modules.payroll import defaults
    if not db.query(ExpenseCategory.id).filter(ExpenseCategory.company_id == company_id).first():
        for i, c in enumerate(defaults.EXPENSE_CATEGORIES):
            db.add(ExpenseCategory(company_id=company_id, sort_order=(i + 1) * 10, **c))
    if not db.query(LoanType.id).filter(LoanType.company_id == company_id).first():
        for t in defaults.LOAN_TYPES:
            db.add(LoanType(company_id=company_id, **t))


# ── Demo data ───────────────────────────────────────────────────────

def _owner_ctx(company_id: int) -> RequestContext:
    return RequestContext(company_id=company_id, actor_type="owner", actor_name="Company owner", role="admin")


def _emp_ctx(company_id: int, e) -> RequestContext:
    return RequestContext(company_id=company_id, employee_id=e.id, actor_type="employee", role=e.role,
                          actor_name=f"{e.first_name} {e.last_name}".strip())


CTC_BY_GRADE = {"G1": (360000, 480000), "G2": (540000, 900000), "G3": (1000000, 1600000), "G4": (1600000, 2400000),
                "G5": (2400000, 3200000), "G6": (3600000, 4800000), "G7": (5000000, 6500000)}


def seed_demo(db: Session, company_id: int) -> None:
    _seed_demo_first(db, company_id)
    from modules.company.models import Company
    company = db.query(Company).filter(Company.id == company_id).first()
    if company and company.slug == "campusly-demo":
        from modules.payroll import demo_second
        demo_second.seed(db, company_id)


def _seed_demo_first(db: Session, company_id: int) -> None:
    from modules.company.models import Company
    from modules.payroll import calc, runs, structures
    from modules.payroll import service as svc
    from modules.payroll.models import EmployeeSalary, PayRun
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company or company.slug != "campusly-demo":
        return
    if db.query(EmployeeSalary.id).filter(EmployeeSalary.company_id == company_id).first():
        return
    db.info["company_id"] = company_id
    rng = random.Random(42)
    today = today_for_company_id(db, company_id)
    Employee = svc.employee_model()
    emps = db.query(Employee).filter(Employee.company_id == company_id, Employee.active.is_(True)).order_by(Employee.id).all()
    structs = {s.code: s for s in structures.structures(db, company_id)}
    from modules.payroll import adapters
    fy_start = calc.fy_bounds(calc.fy_of(today))[0]
    with use_context(_owner_ctx(company_id)):
        for i, e in enumerate(emps):
            grade = adapters.grade_code(db, e.grade_id) or "G2"
            lo, hi = CTC_BY_GRADE.get(grade, (500000, 900000))
            ctc = Decimal(round(rng.uniform(lo, hi) / 12000) * 12000)
            code = "MGMT" if grade in ("G6", "G7") else "STAFF"
            if i in (3, 7):          # two workers covered by ESI
                code, ctc = "WORK", Decimal(252000 + i * 1500)
            p = svc.profile_for(db, company_id, e)
            p.metro = e.location_id in (1, 2)
            p.skill = "semi_skilled" if i in (3, 7) else "skilled"
            if i == 5:
                p.vpf_rate = Decimal("5")
            start = max(e.date_of_joining or fy_start, fy_start) if (e.date_of_joining or fy_start) <= today else None
            if start is None:
                continue
            try:
                svc.assign_salary(db, company_id, e, {"structure_uuid": structs[code].uuid, "annual_ctc": ctc,
                                                     "effective_from": start, "reason": "first_salary"})
            except Exception as exc:
                log.warning("Demo salary for %s failed: %s", e.id, exc)
        db.flush()
        _demo_declarations(db, company_id, emps[:6], calc.fy_of(today))
        _demo_expenses_loans(db, company_id, emps)
        group = svc.default_pay_group(db, company_id)
        last_month = calc.add_months(today.replace(day=1), -1)
        past = runs.create_run(db, company_id, month=last_month, group=group)
        # attendance before go-live has no punches; pay the demo's first month in full
        from modules.payroll.models import PayRunInput
        db.query(PayRunInput).filter(PayRunInput.pay_run_id == past.id, PayRunInput.kind == "lop_days").delete()
        _demo_opening(db, company_id, emps, last_month)
        runs.pre_checks(db, company_id, past)
        runs.calculate(db, company_id, past)
        past.status = "approved"
        runs._stage(past, "approved", "Demo data")
        runs.lock(db, company_id, past)
        from modules.payroll.models import Payslip
        for p in db.query(Payslip).filter(Payslip.pay_run_id == past.id).all():
            p.published = True
        runs.mark_paid(db, company_id, past, reference="UTR-DEMO-0001")
        # a mid-year raise back-dated into the paid month, so the current run shows arrears
        e = next((x for x in emps[8:] if x.date_of_joining and x.date_of_joining < last_month), None)
        if e is not None:
            cur = svc.current_salary(db, company_id, e.id, today)
            if cur:
                st = structures.structure_by_id(db, company_id, cur.structure_id)
                svc.assign_salary(db, company_id, e, {"structure_uuid": st.uuid if st else None,
                                                     "annual_ctc": d2(cur.annual_ctc * Decimal("1.1")),
                                                     "effective_from": last_month, "reason": "annual_raise",
                                                     "note": "Demo: back-dated raise"})
        current = runs.create_run(db, company_id, month=today.replace(day=1), group=group)
        if emps:
            runs.add_input(db, company_id, current, emps[1], {"kind": "earning", "component_code": "INCENTIVE",
                                                              "amount": 7500, "note": "Quarterly incentive"})
        runs.pre_checks(db, company_id, current)
        runs.calculate(db, company_id, current)
        db.query(PayRun).filter(PayRun.id == current.id).first()
    db.commit()


def _demo_opening(db: Session, company_id: int, emps, first_month: date) -> None:
    """Pay from April up to the first month run here, as if imported from the previous system."""
    from modules.payroll import calc, statutory, taxsvc
    from modules.payroll import service as svc
    fy = calc.fy_of(first_month)
    fy_start = calc.fy_bounds(fy)[0]
    for e in emps:
        s = svc.current_salary(db, company_id, e.id, first_month)
        if not s:
            continue
        start = max(fy_start, (e.date_of_joining or fy_start).replace(day=1))
        months = max(0, (first_month.year - start.year) * 12 + first_month.month - start.month)
        if not months:
            continue
        comp = {c["code"]: Decimal(str(c["monthly"])) for c in s.components or []}
        gross = Decimal(str(s.monthly_gross))
        pf = min(comp.get("BASIC", Decimal(0)), Decimal(15000)) * Decimal("0.12")
        rules = statutory.rule(db, company_id, "income_tax", "new", first_month)
        annual_tax = Decimal(calc.income_tax(gross * 12 - 75000, rules)["total"])
        decl = taxsvc.declaration(db, company_id, e.id, fy)
        decl.opening_ytd = {"gross": str(gross * months), "taxable": str(gross * months), "pf": str(round(pf * months)),
                            "pt": str(200 * months), "basic_da": str((comp.get("BASIC", 0) + comp.get("DA", 0)) * months),
                            "hra": str(comp.get("HRA", 0) * months), "tds": str(round(annual_tax * months / 12))}


def d2(x) -> Decimal:
    return Decimal(str(round(float(x) / 1200) * 1200))


def _demo_declarations(db: Session, company_id: int, emps, fy: str) -> None:
    from modules.payroll import taxsvc
    for i, e in enumerate(emps):
        decl = taxsvc.declaration(db, company_id, e.id, fy)
        old = i % 2 == 1
        decl.regime = "old" if old else "new"
        taxsvc.save(db, company_id, decl, {
            "items": [{"key": "ppf", "declared": 60000 + i * 10000}, {"key": "life_insurance", "declared": 25000},
                      {"key": "health_self", "declared": 18000}, {"key": "health_parents", "declared": 32000, "senior": True}]
            if old else [{"key": "ppf", "declared": 50000}],
            "hra": {"months": [{"month": f"{fy[:4]}-{m:02d}", "rent": 22000} for m in range(4, 13)] +
                    [{"month": f"{int(fy[:4]) + 1}-{m:02d}", "rent": 22000} for m in range(1, 4)],
                    "landlord_name": "R. Srinivasan", "landlord_pan": "ABCPS1234K", "metro": True} if old else {},
            "submit": True}, by_hr=True)


def _demo_expenses_loans(db: Session, company_id: int, emps) -> None:
    from core.workflow import service as workflow
    from core.workflow.models import WorkflowInstance
    from modules.expenses import service as expenses
    from modules.loans import service as loans
    if len(emps) < 4:
        return
    today = today_for_company_id(db, company_id)
    cats = {c.code: c for c in expenses.categories(db, company_id)}
    e1, e2, e3 = emps[1], emps[2], emps[min(10, len(emps) - 1)]
    with use_context(_emp_ctx(company_id, e1)):
        c1 = expenses.save_claim(db, company_id, e1, {"title": "Client visit to Mumbai", "lines": [
            {"category_uuid": cats["TRAVEL"].uuid, "expense_date": date(today.year, today.month, 1), "amount": 6400,
             "merchant": "IndiGo", "description": "Flight BLR-BOM", "gstin": "29AABCI1234F1Z5", "invoice_number": "6E-88123",
             "gst_amount": 305},
            {"category_uuid": cats["FOOD"].uuid, "expense_date": date(today.year, today.month, 2), "amount": 950,
             "merchant": "Hotel Sahara", "description": "Dinner with client"}]})
        expenses.submit(db, company_id, c1, e1)
    with use_context(_emp_ctx(company_id, e2)):
        c2 = expenses.save_claim(db, company_id, e2, {"title": "Fuel for site visits", "lines": [
            {"category_uuid": cats["FUEL"].uuid, "expense_date": date(today.year, today.month, 3), "distance_km": 84,
             "vehicle": "car", "from_place": "Pune office", "to_place": "Chakan plant"}]})
        expenses.submit(db, company_id, c2, e2)
    # approve the first claim so it is paid with this month's salary
    with use_context(_owner_ctx(company_id)):
        inst = db.query(WorkflowInstance).filter(WorkflowInstance.id == c1.workflow_instance_id).first()
        if inst and inst.status == "pending":
            workflow._finish(db, inst, "approved", "Demo")
    types = {t.code: t for t in loans.types(db, company_id)}
    with use_context(_owner_ctx(company_id)):
        loan = loans.request_loan(db, company_id, e3, types["PERSONAL"],
                                  {"amount": 120000, "instalments": 12, "purpose": "Home repairs",
                                   "start_month": date(today.year, today.month, 1), "override_eligibility": True},
                                  today, by_hr=True)
        inst = db.query(WorkflowInstance).filter(WorkflowInstance.id == loan.workflow_instance_id).first()
        if inst and inst.status == "pending":
            workflow._finish(db, inst, "approved", "Demo")
        db.refresh(loan)
        if loan.status == "approved":
            loans.disburse(db, company_id, loan, today.replace(day=1), "NEFT-DEMO-LOAN")
