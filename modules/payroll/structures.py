"""Salary components, structures (versioned) and the CTC split.

compute_salary(...) turns an annual CTC into monthly components using a structure:
lines are evaluated in order (fixed, % of CTC, or a formula over `ctc`, `monthly_ctc`,
`metro` and earlier components by lower-case code); employer PF / ESI / gratuity that sit
inside the CTC are worked out from the earnings; the balancing line (usually the special
allowance) takes whatever is left so the parts add up to the CTC exactly. The Code on Wages
50% check and the structure's salary range are reported as warnings.
"""
from __future__ import annotations

import uuid as uuid_mod
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from core import expressions
from core.errors import BadRequest, NotFound
from modules.payroll import calc
from modules.payroll.calc import ZERO, d, money, rnd
from modules.payroll.models import SalaryComponent, SalaryStructure

KINDS = {"earning": "Earning", "deduction": "Deduction", "employer": "Employer contribution",
         "reimbursement": "Reimbursement"}
CALCS = {"fixed": "Fixed amount", "formula": "Formula", "balance": "Balancing figure", "manual": "Typed in each month",
         "system": "Worked out by payroll"}


# ── Components ──────────────────────────────────────────────────────

def components(db: Session, company_id: int, active_only: bool = False) -> list[SalaryComponent]:
    q = db.query(SalaryComponent).filter(SalaryComponent.company_id == company_id)
    if active_only:
        q = q.filter(SalaryComponent.active.is_(True))
    return q.order_by(SalaryComponent.sort_order, SalaryComponent.name).all()


def component_map(db: Session, company_id: int) -> dict[str, SalaryComponent]:
    return {c.code: c for c in components(db, company_id)}


def get_component(db: Session, company_id: int, uuid) -> SalaryComponent:
    c = db.query(SalaryComponent).filter(SalaryComponent.company_id == company_id, SalaryComponent.uuid == uuid).first()
    if not c:
        raise NotFound("Salary component")
    return c


FORMULA_FIELDS = {"ctc", "monthly_ctc", "metro", "gross", "hours", "basic", "da"}


def check_formula(text: str | None, codes: set[str]) -> None:
    if not text:
        return
    try:
        expressions.validate(text, FORMULA_FIELDS | {c.lower() for c in codes})
        expressions.evaluate(text, {**{f: Decimal(1) for f in FORMULA_FIELDS}, "metro": True,
                                    **{c.lower(): Decimal(1) for c in codes}})
    except expressions.ExpressionError as e:
        raise BadRequest(f"Formula: {e}")


COMPONENT_FIELDS = ("name", "name_hi", "kind", "calc", "formula", "one_time", "taxability", "tax_exemption", "wage_class",
                    "pf_wage", "esi_wage", "prorate", "rounding", "in_ctc", "fbp", "fbp_max", "fbp_tax_free_regimes", "show_on_payslip",
                    "show_if_zero", "gl_code", "sort_order", "active", "description")


def save_component(db: Session, company_id: int, data: dict, existing: SalaryComponent | None = None) -> SalaryComponent:
    codes = set(component_map(db, company_id))
    if existing is None:
        code = (data.get("code") or "").strip().upper()
        if not code or not code.replace("_", "").isalnum():
            raise BadRequest("Give the component a short code using letters, numbers and _.")
        if code in codes:
            raise BadRequest(f"A component with code {code} already exists.")
        existing = SalaryComponent(company_id=company_id, code=code)
        db.add(existing)
    elif existing.is_system:
        # system components keep their role; labels, display, GL and taxability may change
        for f in ("kind", "calc"):
            if f in data and data[f] != getattr(existing, f) and existing.calc == "system":
                raise BadRequest("This component is worked out by payroll; its type cannot change.")
    if data.get("kind") and data["kind"] not in KINDS:
        raise BadRequest("Choose earning, deduction, employer contribution or reimbursement.")
    if data.get("calc") and data["calc"] not in CALCS:
        raise BadRequest("Choose how the component is worked out.")
    if data.get("fbp_tax_free_regimes") is not None:
        data["fbp_tax_free_regimes"] = [r for r in data["fbp_tax_free_regimes"] if r in ("old", "new")]
    if data.get("calc") == "formula":
        check_formula(data.get("formula"), codes)
    for f in COMPONENT_FIELDS:
        if f in data:
            setattr(existing, f, data[f])
    if not existing.name:
        raise BadRequest("Give the component a name.")
    db.flush()
    return existing


def component_out(c: SalaryComponent) -> dict:
    return {"uuid": str(c.uuid), "code": c.code, "name": c.name, "name_hi": c.name_hi, "kind": c.kind,
            "kind_label": KINDS.get(c.kind, c.kind), "calc": c.calc, "calc_label": CALCS.get(c.calc, c.calc),
            "formula": c.formula, "one_time": c.one_time, "taxability": c.taxability, "tax_exemption": c.tax_exemption,
            "wage_class": c.wage_class, "pf_wage": c.pf_wage, "esi_wage": c.esi_wage, "prorate": c.prorate,
            "rounding": c.rounding, "in_ctc": c.in_ctc, "fbp": c.fbp, "fbp_max": money(c.fbp_max) if c.fbp_max is not None else None,
            "fbp_tax_free_regimes": list(c.fbp_tax_free_regimes or []),
            "show_on_payslip": c.show_on_payslip, "show_if_zero": c.show_if_zero, "gl_code": c.gl_code,
            "sort_order": c.sort_order, "is_system": c.is_system, "active": c.active, "description": c.description}


# ── Structures ──────────────────────────────────────────────────────

def structures(db: Session, company_id: int, current_only: bool = True) -> list[SalaryStructure]:
    q = db.query(SalaryStructure).filter(SalaryStructure.company_id == company_id)
    if current_only:
        q = q.filter(SalaryStructure.is_current.is_(True))
    return q.order_by(SalaryStructure.name, SalaryStructure.version.desc()).all()


def get_structure(db: Session, company_id: int, uuid) -> SalaryStructure:
    s = db.query(SalaryStructure).filter(SalaryStructure.company_id == company_id, SalaryStructure.uuid == uuid).first()
    if not s:
        raise NotFound("Salary structure")
    return s


def structure_by_id(db: Session, company_id: int, sid: int | None) -> SalaryStructure | None:
    if not sid:
        return None
    return db.query(SalaryStructure).filter(SalaryStructure.company_id == company_id, SalaryStructure.id == sid).first()


def current_of(db: Session, company_id: int, s: SalaryStructure) -> SalaryStructure:
    cur = db.query(SalaryStructure).filter(SalaryStructure.company_id == company_id, SalaryStructure.lineage_id == s.lineage_id,
                                           SalaryStructure.is_current.is_(True)).first()
    return cur or s


def clean_lines(db: Session, company_id: int, lines: list[dict]) -> list[dict]:
    comps = component_map(db, company_id)
    out, seen, balances = [], set(), 0
    for i, ln in enumerate(lines or []):
        code = (ln.get("component") or "").upper()
        c = comps.get(code)
        if not c:
            raise BadRequest(f"Line {i + 1}: choose a salary component.")
        if code in seen:
            raise BadRequest(f"{c.name} is in the structure twice.")
        if c.kind not in ("earning", "reimbursement", "employer"):
            raise BadRequest(f"{c.name} is a deduction; deductions are worked out by payroll, not the structure.")
        seen.add(code)
        mode = ln.get("calc") or "fixed"
        if mode not in ("fixed", "formula", "balance", "percent_ctc"):
            raise BadRequest(f"{c.name}: choose fixed, % of CTC, formula or balancing.")
        row = {"component": code, "calc": mode}
        if mode == "fixed":
            row["value"] = float(d(ln.get("value")))
        elif mode == "percent_ctc":
            row["value"] = float(d(ln.get("value")))
        elif mode == "formula":
            check_formula(ln.get("formula"), set(comps))
            row["formula"] = ln.get("formula")
        else:
            balances += 1
            if c.kind != "earning":
                raise BadRequest("Only an earning can be the balancing figure.")
        out.append(row)
    if balances > 1:
        raise BadRequest("Only one line can be the balancing figure.")
    if not any(r["component"] == "BASIC" for r in out):
        raise BadRequest("A structure needs Basic.")
    return out


STRUCTURE_FIELDS = ("name", "code", "description", "grade_ids", "min_ctc", "max_ctc", "include_employer_pf_in_ctc",
                    "include_gratuity_in_ctc", "active", "effective_from")


def create_structure(db: Session, company_id: int, data: dict, actor: str | None) -> SalaryStructure:
    if not (data.get("name") or "").strip():
        raise BadRequest("Give the structure a name.")
    s = SalaryStructure(company_id=company_id, lineage_id=uuid_mod.uuid4(), version=1, is_current=True,
                        effective_from=data.get("effective_from") or date(2025, 4, 1), created_by_name=actor)
    for f in STRUCTURE_FIELDS:
        if f in data and data[f] is not None:
            setattr(s, f, data[f])
    s.lines = clean_lines(db, company_id, data.get("lines") or [])
    _check_range(s)
    db.add(s)
    db.flush()
    return s


def new_version(db: Session, company_id: int, current: SalaryStructure, data: dict, actor: str | None) -> SalaryStructure:
    """Editing a structure keeps the old version for salaries already assigned with it."""
    current = current_of(db, company_id, current)
    current.is_current = False
    s = SalaryStructure(company_id=company_id, lineage_id=current.lineage_id, version=current.version + 1, is_current=True,
                        created_by_name=actor, change_note=data.get("change_note"))
    for f in STRUCTURE_FIELDS:
        setattr(s, f, data[f] if f in data and data[f] is not None else getattr(current, f))
    s.lines = clean_lines(db, company_id, data["lines"]) if "lines" in data else current.lines
    _check_range(s)
    db.add(s)
    db.flush()
    return s


def _check_range(s: SalaryStructure) -> None:
    if s.min_ctc is not None and s.max_ctc is not None and d(s.min_ctc) > d(s.max_ctc):
        raise BadRequest("The minimum CTC must not be more than the maximum.")


def structure_out(s: SalaryStructure, grade_names: dict | None = None, preview: dict | None = None) -> dict:
    return {"uuid": str(s.uuid), "lineage_id": str(s.lineage_id), "version": s.version, "is_current": s.is_current,
            "effective_from": s.effective_from.isoformat(), "name": s.name, "code": s.code, "description": s.description,
            "grade_ids": s.grade_ids or [], "grades": [grade_names.get(g) for g in s.grade_ids or [] if grade_names and g in grade_names],
            "min_ctc": money(s.min_ctc) if s.min_ctc is not None else None,
            "max_ctc": money(s.max_ctc) if s.max_ctc is not None else None,
            "lines": s.lines or [], "include_employer_pf_in_ctc": s.include_employer_pf_in_ctc,
            "include_gratuity_in_ctc": s.include_gratuity_in_ctc, "active": s.active, "change_note": s.change_note,
            "created_by_name": s.created_by_name, "preview": preview}


# ── CTC split ───────────────────────────────────────────────────────

def compute_salary(*, lines: list[dict], comps: dict[str, SalaryComponent], annual_ctc, metro: bool,
                   rules: dict, overrides: dict | None = None, pf_opts: dict | None = None,
                   include_employer_pf: bool = True, include_gratuity: bool = False,
                   min_ctc=None, max_ctc=None, esi_applicable: bool = True, disabled: bool = False,
                   wage_share=50) -> dict:
    """Monthly and annual amounts per component for an annual CTC. `rules` holds the resolved
    epf, esi and gratuity rules. Raises BadRequest when the balancing figure goes negative."""
    annual = d(annual_ctc)
    monthly_ctc = annual / 12
    overrides = {k.upper(): d(v) for k, v in (overrides or {}).items() if v not in (None, "")}
    pf_opts = pf_opts or {}
    balance_code = next((ln["component"] for ln in lines if ln["calc"] == "balance"), None)

    def evaluate_lines(balance: Decimal) -> tuple[dict[str, Decimal], dict]:
        vars_ = {"ctc": annual, "monthly_ctc": monthly_ctc, "metro": bool(metro), "basic": ZERO, "da": ZERO}
        amounts: dict[str, Decimal] = {}
        for ln in lines:
            code = ln["component"]
            c = comps.get(code)
            if code in overrides:
                amt = overrides[code]
            elif ln["calc"] == "fixed":
                amt = d(ln.get("value"))
            elif ln["calc"] == "percent_ctc":
                amt = calc.pct(monthly_ctc, ln.get("value"))
            elif ln["calc"] == "formula":
                try:
                    amt = d(expressions.evaluate(ln.get("formula") or "0", vars_))
                except expressions.ExpressionError as e:
                    raise BadRequest(f"{c.name if c else code}: {e}")
            else:
                amt = balance
            amt = rnd(amt, (c.rounding if c else "nearest"))
            amounts[code] = amt
            vars_[code.lower()] = amt
        return amounts, vars_

    def employer_parts(amounts: dict[str, Decimal]) -> dict[str, Decimal]:
        out = {}
        gross = sum((v for k, v in amounts.items() if comps.get(k) and comps[k].kind in ("earning", "reimbursement")), ZERO)
        if include_employer_pf and pf_opts.get("applicable", True):
            pf_wage = sum((v for k, v in amounts.items() if comps.get(k) and comps[k].pf_wage), ZERO)
            r = calc.pf(pf_wage, rules["epf"], employee_basis=pf_opts.get("employee_basis", "ceiling"),
                        employer_basis=pf_opts.get("employer_basis", "ceiling"),
                        eps_eligible=pf_opts.get("eps_eligible", True))
            if r.employer_total:
                out["PF_ER"] = r.employer_total
        esi_wage = sum((v for k, v in amounts.items() if comps.get(k) and comps[k].esi_wage), ZERO)
        if esi_applicable and calc.esi_covered(esi_wage, rules["esi"], disabled=disabled):
            _, er = calc.esi(esi_wage, rules["esi"], covered=True)
            if er:
                out["ESI_ER"] = er
        if include_gratuity:
            wage = sum((v for k, v in amounts.items() if comps.get(k) and comps[k].wage_class == "wage"), ZERO)
            out["GRATUITY"] = calc.gratuity_provision(wage, rules["gratuity"])
        _ = gross
        return out

    balance = ZERO
    amounts, employer = {}, {}
    for _ in range(8):
        amounts, _vars = evaluate_lines(balance)
        employer = employer_parts(amounts)
        in_ctc = sum((v for k, v in amounts.items() if comps.get(k) and comps[k].in_ctc), ZERO) + sum(employer.values(), ZERO)
        gap = rnd(monthly_ctc) - in_ctc
        if not balance_code or abs(gap) < 1:
            break
        balance += gap
    if balance_code and amounts.get(balance_code, ZERO) < 0:
        name = comps[balance_code].name if balance_code in comps else balance_code
        raise BadRequest(f"{name} would be negative ({calc.inr(amounts[balance_code])} a month): the CTC is too low "
                         f"for this structure. Lower the other parts or raise the CTC.")
    rows = []
    for code, amt in list(amounts.items()) + list(employer.items()):
        c = comps.get(code)
        rows.append({"code": code, "name": c.name if c else code, "kind": c.kind if c else "employer",
                     "monthly": money(amt), "annual": money(amt * 12), "in_ctc": bool(c.in_ctc) if c else True})
    order = {c.code: (c.sort_order, c.name) for c in comps.values()}
    rows.sort(key=lambda r: order.get(r["code"], (999, r["code"])))
    earnings = [r for r in rows if r["kind"] in ("earning", "reimbursement")]
    gross = sum((d(r["monthly"]) for r in earnings), ZERO)
    core = sum((d(r["monthly"]) for r in earnings if comps.get(r["code"]) and comps[r["code"]].wage_class == "wage"), ZERO)
    excl = sum((d(r["monthly"]) for r in earnings if comps.get(r["code"]) and comps[r["code"]].wage_class == "exclusion"), ZERO)
    wr = calc.wage_rule(core, excl, wage_share)
    warnings = []
    if not wr["compliant"]:
        warnings.append({"code": "wage_rule", "message": f"Basic and DA are {wr['core_share']}% of pay, below 50%. "
                         f"{calc.inr(wr['add_back'])} a month will be added back to wages for PF, gratuity and bonus."})
    if min_ctc is not None and annual < d(min_ctc):
        warnings.append({"code": "below_range", "message": f"CTC is below this structure's range (minimum {calc.inr(min_ctc)})."})
    if max_ctc is not None and annual > d(max_ctc):
        warnings.append({"code": "above_range", "message": f"CTC is above this structure's range (maximum {calc.inr(max_ctc)})."})
    return {"annual_ctc": money(annual), "monthly_ctc": money(monthly_ctc), "monthly_gross": money(gross),
            "annual_gross": money(gross * 12), "components": rows, "wage_rule": wr, "warnings": warnings,
            "employer_monthly": money(sum(employer.values(), ZERO))}
