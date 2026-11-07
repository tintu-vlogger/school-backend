"""Payroll set-up services: settings, employee payroll profiles, pay groups and periods,
and effective-dated employee salaries (assign, revise, arrears detection).

Other modules use:
    current_salary(db, company_id, employee_id, on) -> EmployeeSalary | None
    monthly_gross(db, company_id, employee_id, on) -> Decimal
    profile_for(db, company_id, employee) -> EmployeePayrollProfile
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from core import audit, context
from core.errors import BadRequest, NotFound
from core.timeutil import today_for_company_id
from modules.payroll import calc, statutory, structures
from modules.payroll.calc import ZERO, d, money
from modules.payroll.defaults import DEFAULT_SETTINGS
from modules.payroll.models import (EmployeePayrollProfile, EmployeeSalary, PayGroup, PayPeriod, PayrollSettings,
                                    PayRun, Payslip)
from modules.payroll.statutory import deep_merge


def actor() -> str:
    return context.current().actor_name or "System"


def employee_model():
    from modules.employees.models import Employee
    return Employee


def name_of(e) -> str:
    return f"{e.first_name} {e.last_name}".strip() if e else ""


def get_employee(db: Session, company_id: int, employee_id: int):
    Employee = employee_model()
    e = db.query(Employee).filter(Employee.company_id == company_id, Employee.id == employee_id).first()
    if not e:
        raise NotFound("Employee")
    return e


def employee_by_uuid(db: Session, company_id: int, uuid):
    Employee = employee_model()
    e = db.query(Employee).filter(Employee.company_id == company_id, Employee.uuid == uuid).first()
    if not e:
        raise NotFound("Employee")
    return e


def gender_of(db: Session, e) -> str | None:
    if not getattr(e, "gender_id", None):
        return None
    from core.masters import MasterValue
    v = db.query(MasterValue.code, MasterValue.label).filter(MasterValue.id == e.gender_id).first()
    if not v:
        return None
    code = (v[0] or v[1] or "").lower()
    return "female" if code in ("f", "female") else "male" if code in ("m", "male") else "other"


def is_fixed_term(db: Session, e) -> bool:
    if not getattr(e, "employment_type_id", None):
        return False
    from core.masters import MasterValue
    v = db.query(MasterValue.code).filter(MasterValue.id == e.employment_type_id).first()
    return bool(v and (v[0] or "").upper() == "FTC")


# ── Settings ────────────────────────────────────────────────────────

def settings_row(db: Session, company_id: int) -> PayrollSettings:
    row = db.query(PayrollSettings).filter(PayrollSettings.company_id == company_id).first()
    if row is None:
        row = PayrollSettings(company_id=company_id, config={})
        db.add(row)
        db.flush()
    return row


def get_settings(db: Session, company_id: int) -> dict:
    return deep_merge(DEFAULT_SETTINGS, settings_row(db, company_id).config or {})


def update_settings(db: Session, company_id: int, data: dict) -> dict:
    row = settings_row(db, company_id)
    merged = deep_merge(row.config or {}, data or {})
    if merged.get("rounding") and merged["rounding"] not in ("nearest", "up", "down", "none"):
        raise BadRequest("Choose nearest, up, down or no rounding.")
    if merged.get("proration_basis") and merged["proration_basis"] not in ("calendar", "fixed_30", "fixed_26", "working_days"):
        raise BadRequest("Choose how part months are paid.")
    if "overtime_formula" in (data or {}):
        structures.check_formula(data["overtime_formula"], {"basic", "da"})
    rate = d(deep_merge(DEFAULT_SETTINGS, merged).get("bonus_rate"))
    if rate < d("8.33") or rate > 20:
        raise BadRequest("The bonus rate must be between 8.33% and 20%.")
    row.config = merged
    db.flush()
    return get_settings(db, company_id)


# ── Profiles ────────────────────────────────────────────────────────

def profile_for(db: Session, company_id: int, employee, create: bool = True) -> EmployeePayrollProfile | None:
    p = db.query(EmployeePayrollProfile).filter(EmployeePayrollProfile.company_id == company_id,
                                                EmployeePayrollProfile.employee_id == employee.id).first()
    if p is None and create:
        group = default_pay_group(db, company_id)
        p = EmployeePayrollProfile(company_id=company_id, employee_id=employee.id, pay_group_id=group.id if group else None,
                                   fixed_term=is_fixed_term(db, employee))
        db.add(p)
        db.flush()
    return p


def profiles_for(db: Session, company_id: int, employee_ids: list[int]) -> dict[int, EmployeePayrollProfile]:
    rows = db.query(EmployeePayrollProfile).filter(EmployeePayrollProfile.company_id == company_id,
                                                   EmployeePayrollProfile.employee_id.in_(employee_ids or [-1])).all()
    return {r.employee_id: r for r in rows}


PROFILE_FIELDS = ("payment_mode", "pf_applicable", "pf_employee_basis", "pf_employer_basis", "eps_applicable", "vpf_rate",
                  "vpf_amount", "esi_applicable", "disability", "pt_applicable", "pt_state", "lwf_applicable", "lwf_state",
                  "metro", "min_wage_state", "min_wage_zone", "skill", "fixed_term", "pf_number", "previous_pf_member",
                  "cost_centre_code", "notes")


def update_profile(db: Session, company_id: int, employee, data: dict) -> EmployeePayrollProfile:
    p = profile_for(db, company_id, employee)
    if "pay_group_uuid" in data:
        g = get_pay_group(db, company_id, data["pay_group_uuid"]) if data["pay_group_uuid"] else None
        p.pay_group_id = g.id if g else None
    for f in PROFILE_FIELDS:
        if f in data:
            setattr(p, f, data[f])
    if p.pf_employee_basis not in ("ceiling", "actual") or p.pf_employer_basis not in ("ceiling", "actual"):
        raise BadRequest("PF can be on the wage ceiling or on actual wages.")
    if p.payment_mode not in ("bank", "cheque", "cash"):
        raise BadRequest("Pay by bank, cheque or cash.")
    if p.skill not in ("unskilled", "semi_skilled", "skilled", "highly_skilled"):
        raise BadRequest("Choose a skill level for the minimum wage.")
    db.flush()
    return p


def profile_out(db: Session, p: EmployeePayrollProfile | None) -> dict | None:
    if p is None:
        return None
    g = db.query(PayGroup).filter(PayGroup.id == p.pay_group_id).first() if p.pay_group_id else None
    out = {f: (money(getattr(p, f)) if isinstance(getattr(p, f), Decimal) else getattr(p, f)) for f in PROFILE_FIELDS}
    return {**out, "uuid": str(p.uuid), "pay_group_uuid": str(g.uuid) if g else None, "pay_group_name": g.name if g else None}


# ── Pay groups and periods ─────────────────────────────────────────

def pay_groups(db: Session, company_id: int) -> list[PayGroup]:
    return db.query(PayGroup).filter(PayGroup.company_id == company_id).order_by(PayGroup.is_default.desc(), PayGroup.name).all()


def default_pay_group(db: Session, company_id: int) -> PayGroup | None:
    q = db.query(PayGroup).filter(PayGroup.company_id == company_id, PayGroup.active.is_(True))
    return q.filter(PayGroup.is_default.is_(True)).first() or q.order_by(PayGroup.id).first()


def get_pay_group(db: Session, company_id: int, uuid) -> PayGroup:
    g = db.query(PayGroup).filter(PayGroup.company_id == company_id, PayGroup.uuid == uuid).first()
    if not g:
        raise NotFound("Pay group")
    return g


GROUP_FIELDS = ("name", "code", "frequency", "legal_entity_id", "proration_basis", "attendance_cutoff_day", "pay_day",
                "pay_day_same_month", "is_default", "active", "description")


def save_pay_group(db: Session, company_id: int, data: dict, g: PayGroup | None = None) -> PayGroup:
    if g is None:
        g = PayGroup(company_id=company_id)
        db.add(g)
    for f in GROUP_FIELDS:
        if f in data:
            setattr(g, f, data[f])
    if not (g.name or "").strip():
        raise BadRequest("Give the pay group a name.")
    if g.frequency != "monthly":
        raise BadRequest("Pay groups are paid monthly for now.")
    if not (1 <= int(g.attendance_cutoff_day or 0) <= 31) or not (1 <= int(g.pay_day or 0) <= 31):
        raise BadRequest("Cut-off and pay days must be between 1 and 31.")
    if g.is_default:
        for other in pay_groups(db, company_id):
            if other is not g and other.is_default:
                other.is_default = False
    db.flush()
    return g


def pay_group_out(g: PayGroup, counts: dict | None = None) -> dict:
    return {"uuid": str(g.uuid), "name": g.name, "code": g.code, "frequency": g.frequency, "legal_entity_id": g.legal_entity_id,
            "proration_basis": g.proration_basis, "attendance_cutoff_day": g.attendance_cutoff_day, "pay_day": g.pay_day,
            "pay_day_same_month": g.pay_day_same_month, "is_default": g.is_default, "active": g.active,
            "description": g.description, "employees": (counts or {}).get(g.id, 0)}


def _clamp_day(y: int, m: int, day: int) -> date:
    last = calc.month_end(date(y, m, 1)).day
    return date(y, m, min(day, last))


def period_dates(g: PayGroup, month_start: date) -> dict:
    start = month_start.replace(day=1)
    end = calc.month_end(start)
    cutoff = _clamp_day(start.year, start.month, g.attendance_cutoff_day)
    if g.pay_day_same_month:
        pay = _clamp_day(start.year, start.month, g.pay_day)
    else:
        nxt = calc.add_months(start, 1)
        pay = _clamp_day(nxt.year, nxt.month, g.pay_day)
    return {"start": start, "end": end, "cutoff": cutoff, "pay": pay, "label": start.strftime("%B %Y")}


def ensure_periods(db: Session, company_id: int, g: PayGroup, fy_start: date) -> list[PayPeriod]:
    """Create the twelve monthly periods of a financial year for a pay group."""
    have = {p.start_date for p in db.query(PayPeriod).filter(PayPeriod.pay_group_id == g.id).all()}
    out = []
    for i in range(12):
        m = calc.add_months(fy_start, i)
        if m in have:
            continue
        pd = period_dates(g, m)
        p = PayPeriod(company_id=company_id, pay_group_id=g.id, label=pd["label"], start_date=pd["start"],
                      end_date=pd["end"], cutoff_date=pd["cutoff"], pay_date=pd["pay"], status="upcoming")
        db.add(p)
        out.append(p)
    db.flush()
    return out


def period_for(db: Session, company_id: int, g: PayGroup, month_start: date) -> PayPeriod:
    p = db.query(PayPeriod).filter(PayPeriod.pay_group_id == g.id, PayPeriod.start_date == month_start.replace(day=1)).first()
    if p is None:
        fy = calc.fy_of(month_start)
        ensure_periods(db, company_id, g, calc.fy_bounds(fy)[0])
        p = db.query(PayPeriod).filter(PayPeriod.pay_group_id == g.id, PayPeriod.start_date == month_start.replace(day=1)).first()
    return p


def periods(db: Session, company_id: int, g: PayGroup | None = None, fy: str | None = None) -> list[PayPeriod]:
    q = db.query(PayPeriod).filter(PayPeriod.company_id == company_id)
    if g:
        q = q.filter(PayPeriod.pay_group_id == g.id)
    if fy:
        a, b = calc.fy_bounds(fy)
        q = q.filter(PayPeriod.start_date >= a, PayPeriod.start_date <= b)
    return q.order_by(PayPeriod.start_date).all()


def period_out(p: PayPeriod, runs: dict | None = None) -> dict:
    r = (runs or {}).get(p.id)
    return {"uuid": str(p.uuid), "label": p.label, "start_date": p.start_date.isoformat(), "end_date": p.end_date.isoformat(),
            "cutoff_date": p.cutoff_date.isoformat(), "pay_date": p.pay_date.isoformat(), "status": p.status,
            "run_uuid": str(r.uuid) if r else None, "run_status": r.status if r else None}


# ── Salaries ────────────────────────────────────────────────────────

def salaries_of(db: Session, company_id: int, employee_id: int, include_cancelled: bool = False) -> list[EmployeeSalary]:
    q = db.query(EmployeeSalary).filter(EmployeeSalary.company_id == company_id, EmployeeSalary.employee_id == employee_id)
    if not include_cancelled:
        q = q.filter(EmployeeSalary.status != "cancelled")
    return q.order_by(EmployeeSalary.effective_from.desc(), EmployeeSalary.id.desc()).all()


def current_salary(db: Session, company_id: int, employee_id: int, on: date) -> EmployeeSalary | None:
    return (db.query(EmployeeSalary)
            .filter(EmployeeSalary.company_id == company_id, EmployeeSalary.employee_id == employee_id,
                    EmployeeSalary.status != "cancelled", EmployeeSalary.effective_from <= on)
            .order_by(EmployeeSalary.effective_from.desc(), EmployeeSalary.id.desc()).first())


def salaries_in(db: Session, company_id: int, employee_ids: list[int], start: date, end: date) -> dict[int, list[EmployeeSalary]]:
    """Salaries overlapping a period, per employee, oldest first."""
    rows = (db.query(EmployeeSalary)
            .filter(EmployeeSalary.company_id == company_id, EmployeeSalary.employee_id.in_(employee_ids or [-1]),
                    EmployeeSalary.status != "cancelled", EmployeeSalary.effective_from <= end)
            .order_by(EmployeeSalary.employee_id, EmployeeSalary.effective_from, EmployeeSalary.id).all())
    out: dict[int, list[EmployeeSalary]] = {}
    for r in rows:
        if r.effective_to and r.effective_to < start:
            continue
        out.setdefault(r.employee_id, []).append(r)
    # only the latest one starting on or before the period start, plus later ones
    for eid, lst in out.items():
        before = [s for s in lst if s.effective_from <= start]
        later = [s for s in lst if s.effective_from > start]
        out[eid] = ([before[-1]] if before else []) + later
    return out


def monthly_gross(db: Session, company_id: int, employee_id: int, on: date) -> Decimal:
    s = current_salary(db, company_id, employee_id, on)
    return d(s.monthly_gross) if s else ZERO


def rules_for_ctc(db: Session, company_id: int, on: date) -> dict:
    book = statutory.RuleBook(db, company_id, on)
    return {"epf": book.need("epf"), "esi": book.need("esi"), "gratuity": book.need("gratuity"),
            "wage_rule": book.get("wage_rule") or {"min_wage_share_percent": 50}}


def preview_salary(db: Session, company_id: int, employee, data: dict) -> dict:
    """Split a CTC with a structure for one employee (nothing saved)."""
    on = data.get("effective_from") or today_for_company_id(db, company_id)
    comps = structures.component_map(db, company_id)
    profile = profile_for(db, company_id, employee)
    s = structures.get_structure(db, company_id, data["structure_uuid"]) if data.get("structure_uuid") else None
    rules = rules_for_ctc(db, company_id, on)
    settings = get_settings(db, company_id)
    if s is None:
        # no structure: the typed amounts are the salary
        lines = [{"component": k.upper(), "calc": "fixed", "value": float(d(v))} for k, v in (data.get("overrides") or {}).items()]
        if not lines:
            raise BadRequest("Choose a salary structure or type the amounts.")
        lines = structures.clean_lines(db, company_id, lines)
        overrides = {}
        min_ctc = max_ctc = None
        inc_pf, inc_grat = True, False
    else:
        lines, overrides = s.lines, data.get("overrides") or {}
        min_ctc, max_ctc = s.min_ctc, s.max_ctc
        inc_pf, inc_grat = s.include_employer_pf_in_ctc, s.include_gratuity_in_ctc
    metro = data.get("metro") if data.get("metro") is not None else profile.metro
    age = calc.age_on(employee.date_of_birth, on)
    res = structures.compute_salary(
        lines=lines, comps=comps, annual_ctc=data["annual_ctc"], metro=bool(metro), rules=rules, overrides=overrides,
        pf_opts={"applicable": profile.pf_applicable, "employee_basis": profile.pf_employee_basis,
                 "employer_basis": settings["pf"].get("employer_basis") if profile.pf_employer_basis == "ceiling" else "actual",
                 "eps_eligible": calc.eps_eligible(age, rules["epf"]) and profile.eps_applicable},
        include_employer_pf=inc_pf, include_gratuity=inc_grat, min_ctc=min_ctc, max_ctc=max_ctc,
        esi_applicable=profile.esi_applicable, disabled=profile.disability,
        wage_share=rules["wage_rule"].get("min_wage_share_percent", 50))
    # minimum wage check on the full month
    book = statutory.RuleBook(db, company_id, on)
    company = _company(db, company_id)
    from modules.payroll import adapters
    state = profile.min_wage_state or adapters.employee_state(db, employee, company, settings)
    mw = statutory.minimum_wage(book, state, profile.min_wage_zone, profile.skill)
    if mw and d(res["wage_rule"]["wages"]) < d(mw["monthly"]):
        res["warnings"].append({"code": "below_min_wage", "message": f"Wages of {calc.inr(res['wage_rule']['wages'])} a month are below "
                                f"the minimum wage of {calc.inr(mw['monthly'])} ({mw['source']})."})
    res["minimum_wage"] = {**mw, "monthly": money(mw["monthly"])} if mw else None
    return res


def _company(db: Session, company_id: int):
    from modules.company.models import Company
    return db.query(Company).filter(Company.id == company_id).first()


def last_locked_period_end(db: Session, company_id: int, employee_id: int) -> date | None:
    row = (db.query(Payslip.period_end).join(PayRun, PayRun.id == Payslip.pay_run_id)
           .filter(Payslip.company_id == company_id, Payslip.employee_id == employee_id, PayRun.kind == "regular",
                   PayRun.status.in_(("locked", "paid"))).order_by(Payslip.period_end.desc()).first())
    return row[0] if row else None


def assign_salary(db: Session, company_id: int, employee, data: dict) -> EmployeeSalary:
    """Assign or revise a salary from a date. The salary before it ends the day before; a
    date inside an already paid month makes the next pay run add arrears."""
    eff: date = data["effective_from"]
    if d(data.get("annual_ctc")) <= 0:
        raise BadRequest("Enter the annual CTC.")
    if employee.date_of_joining and eff < employee.date_of_joining:
        raise BadRequest("A salary cannot start before the joining date.")
    res = preview_salary(db, company_id, employee, data)
    s = structures.get_structure(db, company_id, data["structure_uuid"]) if data.get("structure_uuid") else None
    reasons = {r["value"] for r in get_settings(db, company_id).get("revision_reasons", [])}
    reason = data.get("reason") or ("first_salary" if not salaries_of(db, company_id, employee.id) else "annual_raise")
    if reasons and reason not in reasons:
        raise BadRequest("Choose a reason for the revision.")
    for old in salaries_of(db, company_id, employee.id):
        if old.effective_from == eff:
            old.status = "cancelled"
        elif old.effective_from < eff and (old.effective_to is None or old.effective_to >= eff):
            old.effective_to = eff - timedelta(days=1)
            old.status = "superseded"
        elif old.effective_from > eff:
            raise BadRequest("A later salary already exists; revise from a date after it or cancel it first.")
    row = EmployeeSalary(
        company_id=company_id, employee_id=employee.id, structure_id=s.id if s else None, effective_from=eff,
        annual_ctc=d(data["annual_ctc"]), monthly_gross=d(res["monthly_gross"]), components=res["components"],
        overrides={k.upper(): float(d(v)) for k, v in (data.get("overrides") or {}).items() if v not in (None, "")},
        reason=reason, note=data.get("note"), warnings=res["warnings"], status="active", created_by_name=actor())
    db.add(row)
    db.flush()
    locked_until = last_locked_period_end(db, company_id, employee.id)
    if locked_until and eff <= locked_until:
        row.warnings = list(row.warnings or []) + [{"code": "arrears", "message":
                                                    f"Back-dated to {eff:%d %b %Y}: arrears for paid months will be added to the next pay run."}]
    audit.event(db, "payroll.salary_assigned", "employee_salaries", row.id,
                {"employee_id": employee.id, "effective_from": eff.isoformat(), "annual_ctc": money(row.annual_ctc),
                 "reason": reason}, entity_uuid=str(row.uuid), entity_label=name_of(employee))
    return row


def cancel_salary(db: Session, company_id: int, row: EmployeeSalary) -> None:
    locked_until = last_locked_period_end(db, company_id, row.employee_id)
    if locked_until and row.effective_from <= locked_until:
        raise BadRequest("This salary has been paid in a locked pay run; revise it instead of cancelling.")
    row.status = "cancelled"
    prev = [s for s in salaries_of(db, company_id, row.employee_id) if s.effective_from < row.effective_from]
    if prev and prev[0].effective_to and prev[0].effective_to == row.effective_from - timedelta(days=1):
        prev[0].effective_to = None
        prev[0].status = "active"
    db.flush()


def salary_out(s: EmployeeSalary, structure_name: str | None = None) -> dict:
    return {"uuid": str(s.uuid), "effective_from": s.effective_from.isoformat(),
            "effective_to": s.effective_to.isoformat() if s.effective_to else None, "annual_ctc": money(s.annual_ctc),
            "monthly_gross": money(s.monthly_gross), "components": s.components or [], "overrides": s.overrides or {},
            "reason": s.reason, "note": s.note, "warnings": s.warnings or [], "status": s.status,
            "structure_name": structure_name, "created_by_name": s.created_by_name,
            "created_at": s.created_at.isoformat() if s.created_at else None}


# ── Called by other modules ─────────────────────────────────────────

def current_ctc(db: Session, company_id: int, employee_id: int) -> Decimal | None:
    """The employee's annual CTC today (pay reviews use it as the base)."""
    s = current_salary(db, company_id, employee_id, today_for_company_id(db, company_id))
    return d(s.annual_ctc) if s else None


def apply_salary_revision(db: Session, company_id: int, employee_id: int, ctc, effective_date: date, *,
                          reason: str | None = None, source: str = "pay_review", source_ref: str | None = None,
                          note: str | None = None):
    """An increment or promotion from a pay review. It is queued for payroll's review (or
    applied at once when `revisions.auto_apply` is on); arrears follow in the next pay run."""
    from modules.payroll import revisions
    return revisions.request(db, company_id, employee_id, d(ctc), effective_date, source=source, source_ref=source_ref,
                             reason=reason, note=note)
