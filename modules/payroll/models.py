"""Payroll tables.

Platform level (no company_id; maintained by the platform team, read by everyone):
statutory_rules          the statutory library: one row per kind + scope + effective date
                         (EPF, ESI, PT and LWF per state, minimum wages, income tax per regime,
                         gratuity, bonus, due dates ...). Old periods keep using old versions.

Company level:
statutory_overrides      a company's own version of a library rule where the law allows
payroll_settings         one row per company: rounding, proration, payslip, TDS windows,
                         bank formats, GL mapping, checks
salary_components        earnings, deductions, employer contributions, reimbursements, one-time
salary_structures        versioned templates by grade that split CTC into components
pay_groups, pay_periods  who is paid together, when; attendance cut-off and pay date
employee_payroll_profiles  per employee: pay group, PF/ESI/PT/LWF options, minimum-wage zone and skill
employee_salaries        effective-dated salary (CTC and monthly components) per employee
pay_runs                 one payroll run (regular or off-cycle) and its stage history
pay_run_inputs           inputs for a run: LOP, overtime, one-time pay, deductions, holds, skips
payslips                 one calculated result per employee per run
statutory_challans       payments of PF, ESI, PT, LWF and TDS with reference numbers
tax_declarations         regime choice, investment declarations and proofs per employee per year
perquisites              car, accommodation and other taxable benefits
payroll_files            uploaded files (Form 16 Part A, proofs)
final_settlements        full and final settlement on exit
"""
from sqlalchemy import (Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, LargeBinary, Numeric, String,
                        Text, UniqueConstraint, text)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from core.db import Base


def _uuid():
    return Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))


def _company():
    return Column(Integer, ForeignKey("companies.id"), nullable=False)


def _employee(nullable=False):
    return Column(Integer, ForeignKey("employees.id"), nullable=nullable)


def _json(default="'{}'::jsonb"):
    return Column(JSONB, nullable=False, server_default=text(default))


def _money(nullable=False, default="0"):
    return Column(Numeric(14, 2), nullable=nullable, server_default=text(default) if default is not None else None)


def _stamps():
    return (Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
            Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False))


class StatutoryRule(Base):
    __tablename__ = "statutory_rules"
    __table_args__ = (
        UniqueConstraint("kind", "scope", "effective_from", name="uq_statutory_rules_kind_scope_from"),
        Index("ix_statutory_rules_kind_scope", "kind", "scope", "effective_from"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    kind = Column(String(40), nullable=False)
    scope = Column(String(20), nullable=True)           # state code, regime, or NULL
    effective_from = Column(Date, nullable=False)
    data = _json()
    note = Column(String(300), nullable=True)
    source = Column(String(300), nullable=True)         # notification / circular reference
    status = Column(String(20), nullable=False, server_default=text("'live'"))  # live | draft
    created_by_name = Column(String(200), nullable=True)
    reviewed_by_name = Column(String(200), nullable=True)
    created_at, updated_at = _stamps()


class StatutoryOverride(Base):
    __tablename__ = "statutory_overrides"
    __table_args__ = (
        Index("ix_statutory_overrides_company_kind", "company_id", "kind", "scope", "effective_from"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    kind = Column(String(40), nullable=False)
    scope = Column(String(20), nullable=True)
    effective_from = Column(Date, nullable=False)
    data = _json()
    reason = Column(String(300), nullable=False)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_by_name = Column(String(200), nullable=True)
    created_at, updated_at = _stamps()


class PayrollSettings(Base):
    __tablename__ = "payroll_settings"
    __table_args__ = (UniqueConstraint("company_id", name="uq_payroll_settings_company"),)

    id = Column(Integer, primary_key=True)
    company_id = _company()
    config = _json()
    created_at, updated_at = _stamps()


class SalaryComponent(Base):
    __tablename__ = "salary_components"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_salary_components_company_code"),
        Index("ix_salary_components_company_kind", "company_id", "kind", "sort_order"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    code = Column(String(30), nullable=False)
    name = Column(String(120), nullable=False)
    name_hi = Column(String(120), nullable=True)          # Hindi label on payslips
    # earning | deduction | employer | reimbursement
    kind = Column(String(20), nullable=False)
    # fixed | formula | balance | manual (typed in each month) | system (PF, ESI, PT, LWF, TDS, loans ...)
    calc = Column(String(20), nullable=False, server_default=text("'fixed'"))
    formula = Column(Text, nullable=True)
    one_time = Column(Boolean, nullable=False, server_default=text("false"))
    # taxable | exempt | partly (exemption worked out by tax rules, e.g. HRA, LTA, reimbursements with bills)
    taxability = Column(String(20), nullable=False, server_default=text("'taxable'"))
    tax_exemption = Column(String(30), nullable=True)     # hra | lta | bills | employer_nps
    # Code on Wages: wage (basic, DA, retaining) | exclusion (allowances tested by the 50% rule) | none
    wage_class = Column(String(20), nullable=False, server_default=text("'exclusion'"))
    pf_wage = Column(Boolean, nullable=False, server_default=text("false"))
    esi_wage = Column(Boolean, nullable=False, server_default=text("false"))
    prorate = Column(Boolean, nullable=False, server_default=text("true"))
    rounding = Column(String(10), nullable=False, server_default=text("'nearest'"))
    in_ctc = Column(Boolean, nullable=False, server_default=text("true"))
    fbp = Column(Boolean, nullable=False, server_default=text("false"))   # can be chosen in the flexible benefit plan
    fbp_max = _money(nullable=True, default=None)                        # yearly limit when chosen
    fbp_tax_free_regimes = _json("'[\"old\", \"new\"]'::jsonb")         # regimes in which approved bills are tax-free
    show_on_payslip = Column(Boolean, nullable=False, server_default=text("true"))
    show_if_zero = Column(Boolean, nullable=False, server_default=text("false"))
    gl_code = Column(String(40), nullable=True)
    sort_order = Column(Integer, nullable=False, server_default=text("100"))
    is_system = Column(Boolean, nullable=False, server_default=text("false"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    description = Column(Text, nullable=True)
    created_at, updated_at = _stamps()


class SalaryStructure(Base):
    """A versioned template. Editing creates a new version (same lineage); salaries keep
    pointing at the version they were assigned with."""
    __tablename__ = "salary_structures"
    __table_args__ = (
        UniqueConstraint("lineage_id", "version", name="uq_salary_structures_lineage_version"),
        Index("ix_salary_structures_company", "company_id", "is_current", "active"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    lineage_id = Column(UUID(as_uuid=True), nullable=False)
    version = Column(Integer, nullable=False, server_default=text("1"))
    is_current = Column(Boolean, nullable=False, server_default=text("true"))
    effective_from = Column(Date, nullable=False)
    name = Column(String(150), nullable=False)
    code = Column(String(30), nullable=True)
    description = Column(Text, nullable=True)
    grade_ids = _json("'[]'::jsonb")
    min_ctc = _money(nullable=True, default=None)
    max_ctc = _money(nullable=True, default=None)
    # [{component: CODE, calc: fixed|formula|balance|percent_ctc, value, formula}]
    lines = _json("'[]'::jsonb")
    include_employer_pf_in_ctc = Column(Boolean, nullable=False, server_default=text("true"))
    include_gratuity_in_ctc = Column(Boolean, nullable=False, server_default=text("false"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    change_note = Column(String(300), nullable=True)
    created_by_name = Column(String(200), nullable=True)
    created_at, updated_at = _stamps()


class PayGroup(Base):
    __tablename__ = "pay_groups"
    __table_args__ = (Index("ix_pay_groups_company", "company_id", "active"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(120), nullable=False)
    code = Column(String(20), nullable=True)
    frequency = Column(String(20), nullable=False, server_default=text("'monthly'"))
    legal_entity_id = Column(Integer, nullable=True)
    # calendar | fixed_30 | fixed_26 | working_days
    proration_basis = Column(String(20), nullable=False, server_default=text("'calendar'"))
    attendance_cutoff_day = Column(Integer, nullable=False, server_default=text("25"))
    pay_day = Column(Integer, nullable=False, server_default=text("1"))           # day of the next month
    pay_day_same_month = Column(Boolean, nullable=False, server_default=text("false"))
    is_default = Column(Boolean, nullable=False, server_default=text("false"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    description = Column(Text, nullable=True)
    created_at, updated_at = _stamps()


class PayPeriod(Base):
    __tablename__ = "pay_periods"
    __table_args__ = (
        UniqueConstraint("pay_group_id", "start_date", name="uq_pay_periods_group_start"),
        Index("ix_pay_periods_company_group", "company_id", "pay_group_id", "start_date"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    pay_group_id = Column(Integer, ForeignKey("pay_groups.id"), nullable=False)
    label = Column(String(40), nullable=False)              # "November 2026"
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    cutoff_date = Column(Date, nullable=False)
    pay_date = Column(Date, nullable=False)
    status = Column(String(20), nullable=False, server_default=text("'upcoming'"))  # upcoming|open|locked
    created_at, updated_at = _stamps()


class EmployeePayrollProfile(Base):
    __tablename__ = "employee_payroll_profiles"
    __table_args__ = (UniqueConstraint("company_id", "employee_id", name="uq_employee_payroll_profiles_employee"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = _employee()
    pay_group_id = Column(Integer, ForeignKey("pay_groups.id"), nullable=True)
    payment_mode = Column(String(20), nullable=False, server_default=text("'bank'"))  # bank | cheque | cash
    pf_applicable = Column(Boolean, nullable=False, server_default=text("true"))
    pf_employee_basis = Column(String(10), nullable=False, server_default=text("'ceiling'"))  # ceiling | actual
    pf_employer_basis = Column(String(10), nullable=False, server_default=text("'ceiling'"))
    eps_applicable = Column(Boolean, nullable=False, server_default=text("true"))
    vpf_rate = Column(Numeric(5, 2), nullable=False, server_default=text("0"))
    vpf_amount = _money()
    esi_applicable = Column(Boolean, nullable=False, server_default=text("true"))
    disability = Column(Boolean, nullable=False, server_default=text("false"))
    pt_applicable = Column(Boolean, nullable=False, server_default=text("true"))
    pt_state = Column(String(4), nullable=True)            # override; else location, entity, company
    lwf_applicable = Column(Boolean, nullable=False, server_default=text("true"))
    lwf_state = Column(String(4), nullable=True)
    metro = Column(Boolean, nullable=False, server_default=text("false"))
    min_wage_state = Column(String(4), nullable=True)
    min_wage_zone = Column(String(30), nullable=True)
    skill = Column(String(20), nullable=False, server_default=text("'skilled'"))
    fixed_term = Column(Boolean, nullable=False, server_default=text("false"))
    pf_number = Column(String(30), nullable=True)
    previous_pf_member = Column(Boolean, nullable=False, server_default=text("true"))
    cost_centre_code = Column(String(40), nullable=True)
    notes = Column(Text, nullable=True)
    created_at, updated_at = _stamps()


class EmployeeSalary(Base):
    __tablename__ = "employee_salaries"
    __table_args__ = (
        Index("ix_employee_salaries_company_employee", "company_id", "employee_id", "effective_from"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = _employee()
    structure_id = Column(Integer, ForeignKey("salary_structures.id"), nullable=True)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    annual_ctc = _money()
    monthly_gross = _money()
    # [{code, name, kind, monthly, annual}]  as worked out from the structure (or typed in)
    components = _json("'[]'::jsonb")
    overrides = _json()                     # {CODE: monthly amount} typed over the structure
    reason = Column(String(40), nullable=False, server_default=text("'first_salary'"))
    note = Column(Text, nullable=True)
    warnings = _json("'[]'::jsonb")
    status = Column(String(20), nullable=False, server_default=text("'active'"))  # active | superseded | cancelled
    arrears_run_id = Column(Integer, nullable=True)   # the run that paid arrears for this revision
    created_by_name = Column(String(200), nullable=True)
    created_at, updated_at = _stamps()


class PayRun(Base):
    __tablename__ = "pay_runs"
    __table_args__ = (
        Index("ix_pay_runs_company_period", "company_id", "period_start", "kind"),
        Index("ix_pay_runs_company_status", "company_id", "status"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    number = Column(String(40), nullable=True)
    pay_group_id = Column(Integer, ForeignKey("pay_groups.id"), nullable=True)
    pay_period_id = Column(Integer, ForeignKey("pay_periods.id"), nullable=True)
    # regular | bonus | arrears | settlement | correction
    kind = Column(String(20), nullable=False, server_default=text("'regular'"))
    title = Column(String(200), nullable=False)
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    pay_date = Column(Date, nullable=True)
    # draft | inputs | checked | calculating | review | pending_approval | approved | locked | paid | cancelled
    status = Column(String(20), nullable=False, server_default=text("'draft'"))
    progress = Column(Integer, nullable=False, server_default=text("0"))
    progress_total = Column(Integer, nullable=False, server_default=text("0"))
    employee_ids = _json("'[]'::jsonb")         # off-cycle runs: who is in it (empty = whole pay group)
    totals = _json()
    checks = _json("'[]'::jsonb")              # pre-check findings
    acknowledgements = _json()                 # {check key: {by, at, reason}}
    assistant = _json("'[]'::jsonb")           # payroll check assistant flags
    history = _json("'[]'::jsonb")             # [{stage, by, at, note}]
    options = _json()                          # off-cycle: {component, amounts ...}
    workflow_instance_id = Column(Integer, ForeignKey("workflow_instances.id"), nullable=True)
    error = Column(Text, nullable=True)
    locked_at = Column(DateTime(timezone=True), nullable=True)
    paid_at = Column(DateTime(timezone=True), nullable=True)
    created_by_name = Column(String(200), nullable=True)
    created_at, updated_at = _stamps()


class PayRunInput(Base):
    __tablename__ = "pay_run_inputs"
    __table_args__ = (Index("ix_pay_run_inputs_company_run", "company_id", "pay_run_id", "employee_id"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    pay_run_id = Column(Integer, ForeignKey("pay_runs.id"), nullable=False)
    employee_id = _employee()
    # lop_days | payable_days | overtime_hours | earning | deduction | hold | skip | tds_override | arrears | note
    kind = Column(String(20), nullable=False)
    component_code = Column(String(30), nullable=True)
    amount = _money(nullable=True, default=None)
    days = Column(Numeric(6, 2), nullable=True)
    note = Column(String(300), nullable=True)
    source = Column(String(20), nullable=False, server_default=text("'manual'"))  # manual | excel | attendance | expense | loan
    source_ref = Column(String(60), nullable=True)
    created_by_name = Column(String(200), nullable=True)
    created_at, updated_at = _stamps()


class Payslip(Base):
    """A calculated result. Rows are derived (recalculated freely until the run locks), so
    they are not field-audited; run-level events are."""
    __tablename__ = "payslips"
    __audit__ = False
    __table_args__ = (
        UniqueConstraint("pay_run_id", "employee_id", name="uq_payslips_run_employee"),
        Index("ix_payslips_company_employee", "company_id", "employee_id", "period_start"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    pay_run_id = Column(Integer, ForeignKey("pay_runs.id"), nullable=False)
    employee_id = _employee()
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    fy = Column(String(7), nullable=False)
    status = Column(String(20), nullable=False, server_default=text("'calculated'"))  # calculated | held | skipped | error
    days_in_period = Column(Numeric(6, 2), nullable=False, server_default=text("0"))
    payable_days = Column(Numeric(6, 2), nullable=False, server_default=text("0"))
    lop_days = Column(Numeric(6, 2), nullable=False, server_default=text("0"))
    gross = _money()
    deductions = _money()
    net = _money()
    employer_cost = _money()
    lines = _json("'[]'::jsonb")             # [{code, name, name_hi, kind, amount, taxable, ytd}]
    statutory = _json()                      # pf/esi/pt/lwf details, wages
    tax = _json()                            # TDS worksheet
    wage_rule = _json()
    arrears = _json("'[]'::jsonb")
    warnings = _json("'[]'::jsonb")
    snapshot = _json()                       # employee, bank, entity details at calculation time
    hold_reason = Column(String(300), nullable=True)
    released_at = Column(DateTime(timezone=True), nullable=True)
    payment_status = Column(String(20), nullable=False, server_default=text("'pending'"))  # pending | paid | failed
    payment_ref = Column(String(80), nullable=True)
    published = Column(Boolean, nullable=False, server_default=text("false"))
    emailed_at = Column(DateTime(timezone=True), nullable=True)
    created_at, updated_at = _stamps()


class StatutoryChallan(Base):
    __tablename__ = "statutory_challans"
    __table_args__ = (
        UniqueConstraint("company_id", "kind", "legal_entity_id", "state", "period", name="uq_statutory_challans_key"),
        Index("ix_statutory_challans_company_due", "company_id", "due_date"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    kind = Column(String(20), nullable=False)        # pf | esi | pt | lwf | tds | tds_return | certificate | salary
    legal_entity_id = Column(Integer, nullable=False, server_default=text("0"))
    state = Column(String(4), nullable=False, server_default=text("''"))
    period = Column(String(10), nullable=False)      # 2026-11, 2026-27-Q2, 2026-H1, 2026-27
    due_date = Column(Date, nullable=False)
    amount = _money()
    status = Column(String(20), nullable=False, server_default=text("'pending'"))  # pending | paid | filed | not_applicable
    paid_on = Column(Date, nullable=True)
    reference = Column(String(80), nullable=True)
    notes = Column(Text, nullable=True)
    created_at, updated_at = _stamps()


class TaxDeclaration(Base):
    __tablename__ = "tax_declarations"
    __table_args__ = (UniqueConstraint("company_id", "employee_id", "fy", name="uq_tax_declarations_employee_fy"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = _employee()
    fy = Column(String(7), nullable=False)
    regime = Column(String(5), nullable=False, server_default=text("'new'"))
    regime_locked = Column(Boolean, nullable=False, server_default=text("false"))
    # draft | submitted | proofs_submitted | verified
    status = Column(String(20), nullable=False, server_default=text("'draft'"))
    # [{key, section, label, declared, proof_amount, approved, status, comment, file_ids}]
    items = _json("'[]'::jsonb")
    # {months: [{month, rent}], landlord_name, landlord_pan, landlord_address, metro}
    hra = _json()
    previous_employer = _json()          # {income, tds, pf, pt, employer_name, tan}
    other_income = _money()
    extra_tds_monthly = _money()
    fbp = _json()                        # {CODE: yearly amount chosen}
    # pay earlier in the year from a previous payroll system: {gross, taxable, tds, pf, pt, basic_da, hra}
    opening_ytd = _json()
    age = Column(Integer, nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    proofs_submitted_at = Column(DateTime(timezone=True), nullable=True)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    verified_by_name = Column(String(200), nullable=True)
    created_at, updated_at = _stamps()


class Perquisite(Base):
    __tablename__ = "perquisites"
    __table_args__ = (Index("ix_perquisites_company_employee", "company_id", "employee_id", "effective_from"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = _employee()
    kind = Column(String(20), nullable=False)          # car | accommodation | other
    details = _json()                                  # car: {engine_cc, driver, own}; accommodation: {city_band, rent_paid_by_employer, recovered}
    monthly_value = _money()                           # worked out (or typed for "other")
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    note = Column(String(300), nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class PayrollFile(Base):
    __tablename__ = "payroll_files"
    __audit__ = False
    __table_args__ = (Index("ix_payroll_files_company_employee", "company_id", "employee_id", "kind"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = _employee(nullable=True)
    kind = Column(String(30), nullable=False)          # form16_part_a | tax_proof | settlement_letter
    fy = Column(String(7), nullable=True)
    ref = Column(String(80), nullable=True)
    filename = Column(String(255), nullable=False)
    content_type = Column(String(100), nullable=False)
    size = Column(Integer, nullable=False, server_default=text("0"))
    content = Column(LargeBinary, nullable=False)
    uploaded_by_name = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class FinalSettlement(Base):
    __tablename__ = "final_settlements"
    __table_args__ = (Index("ix_final_settlements_company_status", "company_id", "status", "due_date"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = _employee()
    number = Column(String(40), nullable=True)
    # resignation | termination | retrenchment | retirement | death | contract_end | absconding | closure | layoff
    exit_kind = Column(String(20), nullable=False, server_default=text("'resignation'"))
    resignation_date = Column(Date, nullable=True)
    last_working_day = Column(Date, nullable=False)
    due_date = Column(Date, nullable=False)            # 2 working days after the last working day
    # draft | pending_approval | approved | paid | cancelled
    status = Column(String(20), nullable=False, server_default=text("'draft'"))
    notice = _json()                 # {required_days, served_days, waived, buyout, shortfall_days, excess_days}
    inputs = _json()                 # HR adjustments: {leave_days, bonus_rate, extra_earnings [], extra_deductions [], waive_notice ...}
    lines = _json("'[]'::jsonb")     # [{code, label, kind, amount, taxable, detail}]
    total_earnings = _money()
    total_deductions = _money()
    tax = _money()
    net = _money()
    workings = _json()               # gratuity, leave, bonus, tax worksheet
    workflow_instance_id = Column(Integer, ForeignKey("workflow_instances.id"), nullable=True)
    pay_run_id = Column(Integer, ForeignKey("pay_runs.id"), nullable=True)
    paid_on = Column(Date, nullable=True)
    payment_ref = Column(String(80), nullable=True)
    payment_mode = Column(String(20), nullable=True)
    alerts_sent = _json("'[]'::jsonb")
    created_by_name = Column(String(200), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    created_at, updated_at = _stamps()


# ── Second pass: revisions from pay reviews, flexible benefits, budgets ─────────

class SalaryRevisionRequest(Base):
    """A salary change waiting for payroll's review: from a pay review (increment or
    promotion) or typed in. Applying it writes an effective-dated EmployeeSalary; arrears for
    months already paid follow in the next pay run."""
    __tablename__ = "salary_revision_requests"
    __table_args__ = (Index("ix_salary_revision_requests_company_status", "company_id", "status", "effective_from"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = _employee()
    source = Column(String(30), nullable=False, server_default=text("'pay_review'"))   # pay_review | manual
    source_ref = Column(String(80), nullable=True)
    reason = Column(String(40), nullable=False, server_default=text("'annual_raise'"))
    effective_from = Column(Date, nullable=False)
    current_ctc = _money()
    new_ctc = _money()
    structure_id = Column(Integer, ForeignKey("salary_structures.id"), nullable=True)
    note = Column(Text, nullable=True)
    # pending | applied | rejected
    status = Column(String(20), nullable=False, server_default=text("'pending'"))
    arrears_estimate = _money()
    salary_id = Column(Integer, ForeignKey("employee_salaries.id"), nullable=True)
    decided_by_name = Column(String(200), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decision_note = Column(String(300), nullable=True)
    created_at, updated_at = _stamps()


class FbpDeclaration(Base):
    """An employee's split of the flexible benefit amount among FBP components for a year."""
    __tablename__ = "fbp_declarations"
    __table_args__ = (UniqueConstraint("company_id", "employee_id", "fy", name="uq_fbp_declarations_employee_fy"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = _employee()
    fy = Column(String(7), nullable=False)
    allocation = _json()                 # {CODE: yearly amount}
    total = _money()                     # the yearly FBP amount from the salary when declared
    status = Column(String(20), nullable=False, server_default=text("'draft'"))   # draft | submitted | locked
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    year_end_run_id = Column(Integer, nullable=True)      # run that paid the unclaimed balance
    created_at, updated_at = _stamps()


class FbpClaim(Base):
    """A bill against an FBP component. HR verifies; approved amounts are paid with salary
    (tax-free when the rules allow) up to what has accrued."""
    __tablename__ = "fbp_claims"
    __table_args__ = (Index("ix_fbp_claims_company_employee", "company_id", "employee_id", "fy", "status"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = _employee()
    fy = Column(String(7), nullable=False)
    component_code = Column(String(30), nullable=False)
    bill_date = Column(Date, nullable=False)
    bill_number = Column(String(60), nullable=True)
    vendor = Column(String(150), nullable=True)
    amount = _money()
    approved_amount = _money(nullable=True, default=None)
    paid_amount = _money()
    description = Column(String(300), nullable=True)
    file_ids = _json("'[]'::jsonb")
    # submitted | approved | partly_approved | rejected | paid | withdrawn
    status = Column(String(20), nullable=False, server_default=text("'submitted'"))
    hr_comment = Column(String(300), nullable=True)
    verified_by_name = Column(String(200), nullable=True)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    pay_run_id = Column(Integer, ForeignKey("pay_runs.id"), nullable=True)
    paid_on = Column(Date, nullable=True)
    created_at, updated_at = _stamps()


class PayrollBudget(Base):
    """Yearly payroll budget (employer cost) for a department or a cost centre."""
    __tablename__ = "payroll_budgets"
    __table_args__ = (UniqueConstraint("company_id", "fy", "dimension", "ref_id", name="uq_payroll_budgets_key"),
                      Index("ix_payroll_budgets_company_fy", "company_id", "fy"))

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    fy = Column(String(7), nullable=False)
    dimension = Column(String(20), nullable=False)          # department | cost_centre
    ref_id = Column(Integer, nullable=False)
    amount = _money()
    headcount = Column(Integer, nullable=True)
    note = Column(String(300), nullable=True)
    created_at, updated_at = _stamps()
