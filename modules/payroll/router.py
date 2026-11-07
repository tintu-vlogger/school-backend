"""Payroll API under /api/v1/payroll (set-up). Other parts live in api_*.py and are mounted
through `extra_routers`.

Access        GET  /payroll/access
Dashboard     GET  /payroll/dashboard
Settings      GET/PUT /payroll/settings, GET /payroll/bank-formats
Components    GET/POST /payroll/components, PUT /payroll/components/{uuid}
Structures    GET/POST /payroll/structures, GET/PUT /payroll/structures/{uuid}, POST /payroll/structures/{uuid}/preview
Pay groups    GET/POST /payroll/pay-groups, PUT /payroll/pay-groups/{uuid}, GET /payroll/pay-periods
Salaries      GET /payroll/salaries, GET /payroll/salaries/{employee_uuid}, POST .../preview, POST .../assign,
              POST /payroll/salaries/entries/{uuid}/cancel, PUT /payroll/profiles/{employee_uuid}
Statutory     GET /payroll/statutory/rules, GET/POST /payroll/statutory/overrides, POST .../overrides/{uuid}/deactivate,
              POST /payroll/statutory/minimum-wages/import, GET /payroll/statutory/preview
"""
from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from core import audit
from core.db import get_db
from core.errors import BadRequest
from core.security import _get_role_permissions, get_current_user, require_permission
from core.timeutil import today_for_company_id
from modules.payroll import adapters, calc, compliance, exports, statutory, structures
from modules.payroll import service as svc
from modules.payroll.calc import ZERO, d, money
from modules.payroll.models import EmployeePayrollProfile, EmployeeSalary, PayRun, StatutoryOverride

router = APIRouter(prefix="/payroll", tags=["Payroll"])

VIEW = require_permission("payroll.view")
SETTINGS = require_permission("payroll.settings")
SALARIES = require_permission("payroll.salaries.manage")


def perms(db: Session, user: dict) -> set[str]:
    return set(_get_role_permissions(user["company_id"], user["role"], db))


@router.get("/access")
def access(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    p = perms(db, user)
    keys = ["payroll.view", "payroll.run", "payroll.approve", "payroll.reopen", "payroll.settings", "payroll.salaries.manage",
            "payroll.tax.manage", "payroll.statutory.manage", "payroll.settlements.manage", "expenses.manage", "loans.manage",
            "benefits.manage", "payroll.bank_files"]
    return {"permissions": {k: k in p for k in keys}, "is_employee": bool(user.get("employee_id")),
            "employee_id": user.get("employee_id")}


# ── Dashboard ───────────────────────────────────────────────────────

@router.get("/dashboard")
def dashboard(user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    cid = user["company_id"]
    today = today_for_company_id(db, cid)
    regular = (db.query(PayRun).filter(PayRun.company_id == cid, PayRun.kind == "regular", PayRun.status != "cancelled")
               .order_by(PayRun.period_start.desc()).limit(12).all())
    trend = [{"period": r.period_start.isoformat(), "label": r.period_start.strftime("%b %y"), "status": r.status,
              "gross": (r.totals or {}).get("gross", "0.00"), "net": (r.totals or {}).get("net", "0.00"),
              "employer_cost": (r.totals or {}).get("employer_cost", "0.00"), "employees": (r.totals or {}).get("employees", 0)}
             for r in reversed(regular)]
    last_locked = next((r for r in regular if r.status in ("locked", "paid", "approved")), None)
    t = (last_locked.totals or {}) if last_locked else {}
    liabilities = []
    if last_locked:
        for key, label, codes in (("pf", "Provident fund", ("pf_employee", "vpf", "pf_employer", "eps", "edli", "pf_admin")),
                                  ("esi", "ESI", ("esi_employee", "esi_employer")), ("pt", "Professional Tax", ("pt",)),
                                  ("lwf", "Labour Welfare Fund", ("lwf_employee", "lwf_employer")), ("tds", "Income tax (TDS)", ("tds",))):
            liabilities.append({"key": key, "label": label, "amount": money(sum((d(t.get(c)) for c in codes), ZERO)),
                                "period": last_locked.period_start.strftime("%b %Y")})
    cal = compliance.calendar(db, cid, calc.add_months(today.replace(day=1), -1), 3)
    upcoming = [c for c in cal if c["status"] in ("pending", "late") and c["days_left"] <= 45][:8]
    current = next((r for r in regular if r.status not in ("locked", "paid")), None)
    Employee = svc.employee_model()
    active = db.query(Employee.id).filter(Employee.company_id == cid, Employee.active.is_(True)).all()
    with_salary = {r[0] for r in db.query(EmployeeSalary.employee_id).filter(EmployeeSalary.company_id == cid,
                                                                              EmployeeSalary.status != "cancelled").distinct().all()}
    from modules.payroll.models import FinalSettlement, TaxDeclaration
    alerts = []
    missing = [i for (i,) in active if i not in with_salary]
    if missing:
        alerts.append({"key": "no_salary", "tone": "warn", "text": f"{len(missing)} active employee(s) have no salary",
                       "href": "/dashboard/payroll/salaries?filter=missing"})
    fnf = db.query(FinalSettlement).filter(FinalSettlement.company_id == cid,
                                           FinalSettlement.status.in_(("draft", "pending_approval", "approved"))).all()
    soon = [s for s in fnf if (s.due_date - today).days <= 1]
    if soon:
        alerts.append({"key": "fnf", "tone": "bad", "text": f"{len(soon)} final settlement(s) due within a working day",
                       "href": "/dashboard/settlements"})
    to_verify = db.query(func.count(TaxDeclaration.id)).filter(TaxDeclaration.company_id == cid,
                                                               TaxDeclaration.status == "proofs_submitted").scalar()
    if to_verify:
        alerts.append({"key": "proofs", "tone": "info", "text": f"{to_verify} tax proof submission(s) to verify",
                       "href": "/dashboard/tax?tab=proofs"})
    try:
        from modules.expenses.models import ExpenseClaim
        direct = db.query(func.count(ExpenseClaim.id)).filter(ExpenseClaim.company_id == cid, ExpenseClaim.status == "approved",
                                                               ExpenseClaim.pay_via == "direct").scalar()
        if direct:
            alerts.append({"key": "expenses", "tone": "info", "text": f"{direct} approved claim(s) to pay by transfer",
                           "href": "/dashboard/expenses?status=approved"})
    except ImportError:
        pass
    late = [c for c in cal if c["status"] == "late"]
    if late:
        alerts.append({"key": "late", "tone": "bad", "text": f"{len(late)} statutory payment(s) or filing(s) late",
                       "href": "/dashboard/statutory?tab=calendar"})
    return {"trend": trend, "liabilities": liabilities, "upcoming": upcoming, "alerts": alerts,
            "current_run": {"uuid": str(current.uuid), "title": current.title, "status": current.status,
                            "totals": current.totals or {}} if current else None,
            "last_run": {"uuid": str(last_locked.uuid), "title": last_locked.title, "totals": t} if last_locked else None,
            "headcount": len(active), "with_salary": len([i for (i,) in active if i in with_salary])}


# ── Settings ────────────────────────────────────────────────────────

@router.get("/settings")
def get_settings(user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    return svc.get_settings(db, user["company_id"])


@router.put("/settings")
def put_settings(body: dict, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    out = svc.update_settings(db, user["company_id"], body)
    db.commit()
    return out


@router.get("/bank-formats")
def bank_formats(user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    fm = exports.bank_formats(svc.get_settings(db, user["company_id"]))
    return [{"key": k, "label": v["label"], "delimiter": v.get("delimiter"), "extension": v.get("extension"),
             "header": v.get("header"), "columns": v["columns"]} for k, v in fm.items()]


# ── Components ──────────────────────────────────────────────────────

class ComponentIn(BaseModel):
    code: str | None = None
    name: str | None = None
    name_hi: str | None = None
    kind: str | None = None
    calc: str | None = None
    formula: str | None = None
    one_time: bool | None = None
    taxability: str | None = None
    tax_exemption: str | None = None
    wage_class: str | None = None
    pf_wage: bool | None = None
    esi_wage: bool | None = None
    prorate: bool | None = None
    rounding: str | None = None
    in_ctc: bool | None = None
    fbp: bool | None = None
    fbp_max: Decimal | None = None
    fbp_tax_free_regimes: list[str] | None = None
    show_on_payslip: bool | None = None
    show_if_zero: bool | None = None
    gl_code: str | None = None
    sort_order: int | None = None
    active: bool | None = None
    description: str | None = None


@router.get("/components")
def list_components(user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    return [structures.component_out(c) for c in structures.components(db, user["company_id"])]


@router.post("/components", status_code=201)
def create_component(body: ComponentIn, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    c = structures.save_component(db, user["company_id"], body.model_dump(exclude_unset=True))
    db.commit()
    return structures.component_out(c)


@router.put("/components/{uuid}")
def update_component(uuid: UUID, body: ComponentIn, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    c = structures.get_component(db, user["company_id"], uuid)
    data = body.model_dump(exclude_unset=True)
    data.pop("code", None)
    structures.save_component(db, user["company_id"], data, c)
    db.commit()
    return structures.component_out(c)


# ── Structures ──────────────────────────────────────────────────────

class StructureIn(BaseModel):
    name: str | None = None
    code: str | None = None
    description: str | None = None
    grade_ids: list[int] | None = None
    min_ctc: Decimal | None = None
    max_ctc: Decimal | None = None
    lines: list[dict] | None = None
    include_employer_pf_in_ctc: bool | None = None
    include_gratuity_in_ctc: bool | None = None
    active: bool | None = None
    effective_from: date | None = None
    change_note: str | None = None


def _preview_for(db: Session, cid: int, s, ctc, metro=True) -> dict | None:
    try:
        rules = svc.rules_for_ctc(db, cid, today_for_company_id(db, cid))
        return structures.compute_salary(lines=s.lines, comps=structures.component_map(db, cid), annual_ctc=ctc, metro=metro,
                                         rules=rules, include_employer_pf=s.include_employer_pf_in_ctc,
                                         include_gratuity=s.include_gratuity_in_ctc, min_ctc=s.min_ctc, max_ctc=s.max_ctc)
    except BadRequest as e:
        return {"error": e.detail}


@router.get("/structures")
def list_structures(history: bool = False, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    cid = user["company_id"]
    names = adapters.grade_names(db, cid)
    counts = dict(db.query(EmployeeSalary.structure_id, func.count(func.distinct(EmployeeSalary.employee_id)))
                  .filter(EmployeeSalary.company_id == cid, EmployeeSalary.status == "active", EmployeeSalary.effective_to.is_(None))
                  .group_by(EmployeeSalary.structure_id).all())
    out = []
    for s in structures.structures(db, cid, current_only=not history):
        row = structures.structure_out(s, names)
        ids = [x.id for x in structures.structures(db, cid, current_only=False) if x.lineage_id == s.lineage_id]
        row["employees"] = sum(counts.get(i, 0) for i in ids)
        mid = (d(s.min_ctc) + d(s.max_ctc)) / 2 if s.min_ctc and s.max_ctc else d(600000)
        row["sample"] = _preview_for(db, cid, s, mid)
        out.append(row)
    return out


@router.post("/structures", status_code=201)
def create_structure(body: StructureIn, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    s = structures.create_structure(db, user["company_id"], body.model_dump(exclude_unset=True), svc.actor())
    db.commit()
    return structures.structure_out(s, adapters.grade_names(db, user["company_id"]))


@router.get("/structures/{uuid}")
def get_structure(uuid: UUID, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    cid = user["company_id"]
    s = structures.get_structure(db, cid, uuid)
    versions = [structures.structure_out(v) for v in structures.structures(db, cid, current_only=False)
                if v.lineage_id == s.lineage_id]
    return {**structures.structure_out(s, adapters.grade_names(db, cid)), "versions": versions}


@router.put("/structures/{uuid}")
def update_structure(uuid: UUID, body: StructureIn, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    cid = user["company_id"]
    s = structures.get_structure(db, cid, uuid)
    new = structures.new_version(db, cid, s, body.model_dump(exclude_unset=True), svc.actor())
    db.commit()
    return structures.structure_out(new, adapters.grade_names(db, cid))


class PreviewIn(BaseModel):
    annual_ctc: Decimal
    metro: bool = True
    lines: list[dict] | None = None
    include_employer_pf_in_ctc: bool | None = None
    include_gratuity_in_ctc: bool | None = None
    min_ctc: Decimal | None = None
    max_ctc: Decimal | None = None


@router.post("/structures/preview")
def preview_structure(body: PreviewIn, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    """Try a structure (saved or being edited) on a CTC."""
    cid = user["company_id"]
    lines = structures.clean_lines(db, cid, body.lines or [])
    rules = svc.rules_for_ctc(db, cid, today_for_company_id(db, cid))
    return structures.compute_salary(lines=lines, comps=structures.component_map(db, cid), annual_ctc=body.annual_ctc,
                                     metro=body.metro, rules=rules,
                                     include_employer_pf=body.include_employer_pf_in_ctc is not False,
                                     include_gratuity=bool(body.include_gratuity_in_ctc), min_ctc=body.min_ctc,
                                     max_ctc=body.max_ctc)


# ── Pay groups and periods ─────────────────────────────────────────

class PayGroupIn(BaseModel):
    name: str | None = None
    code: str | None = None
    frequency: str | None = None
    legal_entity_id: int | None = None
    proration_basis: str | None = None
    attendance_cutoff_day: int | None = None
    pay_day: int | None = None
    pay_day_same_month: bool | None = None
    is_default: bool | None = None
    active: bool | None = None
    description: str | None = None


def _group_counts(db: Session, cid: int) -> dict:
    return dict(db.query(EmployeePayrollProfile.pay_group_id, func.count(EmployeePayrollProfile.id))
                .filter(EmployeePayrollProfile.company_id == cid).group_by(EmployeePayrollProfile.pay_group_id).all())


@router.get("/pay-groups")
def list_pay_groups(user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    counts = _group_counts(db, user["company_id"])
    return [svc.pay_group_out(g, counts) for g in svc.pay_groups(db, user["company_id"])]


@router.post("/pay-groups", status_code=201)
def create_pay_group(body: PayGroupIn, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    cid = user["company_id"]
    g = svc.save_pay_group(db, cid, body.model_dump(exclude_unset=True))
    svc.ensure_periods(db, cid, g, calc.fy_bounds(calc.fy_of(today_for_company_id(db, cid)))[0])
    db.commit()
    return svc.pay_group_out(g)


@router.put("/pay-groups/{uuid}")
def update_pay_group(uuid: UUID, body: PayGroupIn, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    cid = user["company_id"]
    g = svc.get_pay_group(db, cid, uuid)
    svc.save_pay_group(db, cid, body.model_dump(exclude_unset=True), g)
    from modules.payroll.models import PayPeriod
    for p in db.query(PayPeriod).filter(PayPeriod.pay_group_id == g.id, PayPeriod.status == "upcoming").all():
        pd = svc.period_dates(g, p.start_date)
        p.cutoff_date, p.pay_date = pd["cutoff"], pd["pay"]
    db.commit()
    return svc.pay_group_out(g, _group_counts(db, cid))


@router.get("/pay-periods")
def list_periods(group: UUID | None = None, fy: str | None = None, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    cid = user["company_id"]
    g = svc.get_pay_group(db, cid, group) if group else svc.default_pay_group(db, cid)
    fy = fy or calc.fy_of(today_for_company_id(db, cid))
    if g:
        svc.ensure_periods(db, cid, g, calc.fy_bounds(fy)[0])
        db.commit()
    rows = svc.periods(db, cid, g, fy)
    run_map = {r.pay_period_id: r for r in db.query(PayRun).filter(PayRun.company_id == cid, PayRun.kind == "regular",
                                                                   PayRun.status != "cancelled").all()}
    return [svc.period_out(p, run_map) for p in rows]


# ── Employee salaries ──────────────────────────────────────────────

@router.get("/salaries")
def list_salaries(q: str | None = None, filter: str | None = None, structure: UUID | None = None, page: int = 1,
                  size: int = Query(25, le=200), user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    cid = user["company_id"]
    today = today_for_company_id(db, cid)
    Employee = svc.employee_model()
    eq = db.query(Employee).filter(Employee.company_id == cid, Employee.archived_at.is_(None))
    if q:
        like = f"%{q.strip()}%"
        eq = eq.filter(or_(Employee.first_name.ilike(like), Employee.last_name.ilike(like), Employee.employee_code.ilike(like),
                           (Employee.first_name + " " + Employee.last_name).ilike(like)))
    emps = eq.order_by(Employee.active.desc(), Employee.first_name).all()
    current = {}
    for s in (db.query(EmployeeSalary).filter(EmployeeSalary.company_id == cid, EmployeeSalary.status != "cancelled",
                                              EmployeeSalary.effective_from <= today)
              .order_by(EmployeeSalary.employee_id, EmployeeSalary.effective_from, EmployeeSalary.id).all()):
        current[s.employee_id] = s
    future = {s.employee_id: s for s in db.query(EmployeeSalary).filter(EmployeeSalary.company_id == cid,
                                                                        EmployeeSalary.status != "cancelled",
                                                                        EmployeeSalary.effective_from > today).all()}
    snames = {s.id: s.name for s in structures.structures(db, cid, current_only=False)}
    rows = []
    for e in emps:
        s = current.get(e.id) or future.get(e.id)
        if filter == "missing" and s:
            continue
        if filter == "assigned" and not s:
            continue
        if structure and (not s or snames.get(s.structure_id) != snames.get(structures.get_structure(db, cid, structure).id)):
            continue
        warn = [w for w in (s.warnings or [])] if s else []
        rows.append({"employee_uuid": str(e.uuid), "employee_id": e.id, "name": svc.name_of(e), "employee_code": e.employee_code,
                     "active": e.active, "designation": e.designation.designation_name if getattr(e, "designation", None) else None,
                     "date_of_joining": e.date_of_joining.isoformat() if e.date_of_joining else None,
                     "annual_ctc": money(s.annual_ctc) if s else None, "monthly_gross": money(s.monthly_gross) if s else None,
                     "effective_from": s.effective_from.isoformat() if s else None,
                     "structure_name": snames.get(s.structure_id) if s else None, "warnings": warn,
                     "upcoming": bool(future.get(e.id) and current.get(e.id))})
    total = len(rows)
    return {"items": rows[(page - 1) * size: page * size], "total": total, "page": page, "size": size}


@router.get("/salaries/{employee_uuid}")
def employee_salary(employee_uuid: UUID, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    cid = user["company_id"]
    e = svc.employee_by_uuid(db, cid, employee_uuid)
    today = today_for_company_id(db, cid)
    snames = {s.id: s.name for s in structures.structures(db, cid, current_only=False)}
    hist = svc.salaries_of(db, cid, e.id, include_cancelled=True)
    cur = svc.current_salary(db, cid, e.id, today)
    p = svc.profile_for(db, cid, e)
    db.commit()
    company = svc._company(db, cid)
    return {"employee": {"uuid": str(e.uuid), "id": e.id, "name": svc.name_of(e), "employee_code": e.employee_code,
                         "date_of_joining": e.date_of_joining.isoformat() if e.date_of_joining else None,
                         "pan": e.pan, "uan": e.uan, "grade": adapters.grade_code(db, e.grade_id),
                         "state": adapters.employee_state(db, e, company, svc.get_settings(db, cid))},
            "current": svc.salary_out(cur, snames.get(cur.structure_id)) if cur else None,
            "history": [svc.salary_out(s, snames.get(s.structure_id)) for s in hist],
            "profile": svc.profile_out(db, p)}


class SalaryIn(BaseModel):
    structure_uuid: UUID | None = None
    annual_ctc: Decimal = Field(gt=0)
    effective_from: date | None = None
    overrides: dict[str, Any] | None = None
    reason: str | None = None
    note: str | None = None
    metro: bool | None = None


@router.post("/salaries/{employee_uuid}/preview")
def preview_salary(employee_uuid: UUID, body: SalaryIn, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    cid = user["company_id"]
    e = svc.employee_by_uuid(db, cid, employee_uuid)
    out = svc.preview_salary(db, cid, e, body.model_dump())
    db.rollback()
    return out


@router.post("/salaries/{employee_uuid}/assign", status_code=201)
def assign_salary(employee_uuid: UUID, body: SalaryIn, user: dict = Depends(SALARIES), db: Session = Depends(get_db)):
    cid = user["company_id"]
    e = svc.employee_by_uuid(db, cid, employee_uuid)
    data = body.model_dump()
    data["effective_from"] = data["effective_from"] or today_for_company_id(db, cid)
    row = svc.assign_salary(db, cid, e, data)
    db.commit()
    return svc.salary_out(row)


@router.post("/salaries/entries/{uuid}/cancel")
def cancel_salary(uuid: UUID, user: dict = Depends(SALARIES), db: Session = Depends(get_db)):
    cid = user["company_id"]
    row = db.query(EmployeeSalary).filter(EmployeeSalary.company_id == cid, EmployeeSalary.uuid == uuid).first()
    if not row:
        raise BadRequest("Salary not found.")
    svc.cancel_salary(db, cid, row)
    db.commit()
    return {"ok": True}


class ProfileIn(BaseModel):
    pay_group_uuid: UUID | None = None
    payment_mode: str | None = None
    pf_applicable: bool | None = None
    pf_employee_basis: str | None = None
    pf_employer_basis: str | None = None
    eps_applicable: bool | None = None
    vpf_rate: Decimal | None = None
    vpf_amount: Decimal | None = None
    esi_applicable: bool | None = None
    disability: bool | None = None
    pt_applicable: bool | None = None
    pt_state: str | None = None
    lwf_applicable: bool | None = None
    lwf_state: str | None = None
    metro: bool | None = None
    min_wage_state: str | None = None
    min_wage_zone: str | None = None
    skill: str | None = None
    fixed_term: bool | None = None
    pf_number: str | None = None
    previous_pf_member: bool | None = None
    cost_centre_code: str | None = None
    notes: str | None = None


@router.put("/profiles/{employee_uuid}")
def update_profile(employee_uuid: UUID, body: ProfileIn, user: dict = Depends(SALARIES), db: Session = Depends(get_db)):
    cid = user["company_id"]
    e = svc.employee_by_uuid(db, cid, employee_uuid)
    data = body.model_dump(exclude_unset=True)
    if "pay_group_uuid" in data and data["pay_group_uuid"] is not None:
        data["pay_group_uuid"] = data["pay_group_uuid"]
    p = svc.update_profile(db, cid, e, data)
    db.commit()
    return svc.profile_out(db, p)


# ── Statutory library and overrides ────────────────────────────────

@router.get("/statutory/rules")
def statutory_rules(kind: str | None = None, scope: str | None = None, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    cid = user["company_id"]
    lib = [statutory.rule_out(r) for r in statutory.library_rows(db, kind, scope)]
    ovs = [statutory.rule_out(o) for o in statutory.overrides(db, cid, kind)]
    return {"library": lib, "overrides": ovs, "kinds": statutory.KIND_LABELS}


@router.get("/statutory/effective")
def statutory_effective(kind: str, scope: str | None = None, on: date | None = None, user: dict = Depends(VIEW),
                        db: Session = Depends(get_db)):
    cid = user["company_id"]
    return {"kind": kind, "scope": scope, "on": (on or today_for_company_id(db, cid)).isoformat(),
            "data": statutory.rule(db, cid, kind, scope, on or today_for_company_id(db, cid))}


class OverrideIn(BaseModel):
    kind: str
    scope: str | None = None
    effective_from: date
    data: dict
    reason: str = Field(min_length=3)


@router.post("/statutory/overrides", status_code=201)
def create_override(body: OverrideIn, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    cid = user["company_id"]
    base = statutory.rule(db, None, body.kind, body.scope, body.effective_from)
    errors = statutory.validate_override(body.kind, base, body.data)
    if errors:
        raise BadRequest(" ".join(errors))
    o = StatutoryOverride(company_id=cid, kind=body.kind, scope=(body.scope or None), effective_from=body.effective_from,
                          data=body.data, reason=body.reason, created_by_name=svc.actor())
    db.add(o)
    db.flush()
    audit.event(db, "payroll.statutory_override", "statutory_overrides", o.id,
                {"kind": body.kind, "scope": body.scope, "from": body.effective_from.isoformat(), "reason": body.reason},
                entity_uuid=str(o.uuid))
    db.commit()
    return statutory.rule_out(o)


@router.post("/statutory/overrides/{uuid}/deactivate")
def deactivate_override(uuid: UUID, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    o = db.query(StatutoryOverride).filter(StatutoryOverride.company_id == user["company_id"], StatutoryOverride.uuid == uuid).first()
    if not o:
        raise BadRequest("Override not found.")
    o.active = False
    db.commit()
    return statutory.rule_out(o)


@router.post("/statutory/minimum-wages/import")
async def import_min_wages(effective_from: date = Query(...), file: UploadFile = File(...), user: dict = Depends(SETTINGS),
                           db: Session = Depends(get_db)):
    """CSV or Excel with columns state, zone, skill, monthly (daily optional). Each state in
    the file becomes a company version of the minimum wage table from the given date."""
    cid = user["company_id"]
    content = await file.read()
    rows: list[dict] = []
    if (file.filename or "").lower().endswith(".xlsx"):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        it = wb.worksheets[0].iter_rows(values_only=True)
        header = [str(h or "").strip().lower() for h in next(it, [])]
        rows = [dict(zip(header, r)) for r in it if any(v not in (None, "") for v in r)]
    else:
        rows = [{(k or "").strip().lower(): v for k, v in r.items()}
                for r in csv.DictReader(io.StringIO(content.decode("utf-8-sig", errors="replace")))]
    if not rows:
        raise BadRequest("The file has no rows.")
    per_state = statutory.import_min_wages(rows)
    for st, data in per_state.items():
        db.add(StatutoryOverride(company_id=cid, kind="min_wage", scope=st, effective_from=effective_from, data=data,
                                 reason=f"Imported from {file.filename}", created_by_name=svc.actor()))
    audit.event(db, "payroll.min_wages_imported", "statutory_overrides", None,
                {"states": sorted(per_state), "rows": len(rows), "from": effective_from.isoformat()})
    db.commit()
    return {"states": sorted(per_state), "rows": len(rows)}


@router.get("/states")
def states(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    from core.master_seed import STATES
    on = today_for_company_id(db, user["company_id"])
    pt = set(statutory.states_with(db, "pt", on))
    lwf = set(statutory.states_with(db, "lwf", on))
    mw = set(statutory.states_with(db, "min_wage", on))
    return [{"code": c, "name": n, "pt": c in pt, "lwf": c in lwf, "min_wage": c in mw} for n, c, _g, _k in STATES]


from modules.payroll import api_runs, api_statutory, api_tax, api_settlements, api_me, api_more  # noqa: E402

extra_routers = [api_runs.router, api_statutory.router, api_statutory.platform_router, api_tax.router,
                 api_settlements.router, api_me.router, api_more.router]
