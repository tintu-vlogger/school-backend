"""Payroll analytics behind the standard reports: pay equity, cost against budget, CTC
breakup, month-on-month variance and the statutory liability summary.

Pay equity (Code on Wages, 2019, section 3: no discrimination on the ground of gender in
wages for the same or similar work): people are compared within the same designation and
grade. For each group the average and median pay of men and women are worked out and the
gap is (men − women) / men × 100. A group is flagged when both genders are present (at the
minimum group size) and the gap in either measure is at or above the threshold.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from statistics import median

from sqlalchemy.orm import Session

from modules.payroll import calc, structures
from modules.payroll import service as svc
from modules.payroll.calc import ZERO, d
from modules.payroll.models import PayrollBudget, PayRun, Payslip

ACTIVE = ("probation", "confirmed", "notice")


def _gender_key(label: str | None) -> str:
    x = (label or "").strip().lower()
    if x in ("male", "man", "m"):
        return "men"
    if x in ("female", "woman", "f"):
        return "women"
    return "other" if x else "unknown"


def _pct(a: Decimal, b: Decimal) -> Decimal | None:
    """(a − b) / a × 100, one decimal."""
    if not a:
        return None
    return ((a - b) / a * 100).quantize(Decimal("0.1"))


def pay_equity(people: list[dict], *, threshold=5, min_size: int = 1) -> list[dict]:
    """people: [{designation, grade, gender, pay}] -> one row per designation and grade."""
    groups: dict[tuple, dict[str, list[Decimal]]] = defaultdict(lambda: defaultdict(list))
    for p in people:
        groups[(p.get("designation") or "No designation", p.get("grade") or "No grade")][_gender_key(p.get("gender"))].append(d(p["pay"]))
    rows = []
    threshold = d(threshold)
    for (desg, grade), g in sorted(groups.items()):
        men, women = g.get("men", []), g.get("women", [])
        avg_m = sum(men, ZERO) / len(men) if men else None
        avg_w = sum(women, ZERO) / len(women) if women else None
        med_m = Decimal(str(median(men))) if men else None
        med_w = Decimal(str(median(women))) if women else None
        comparable = len(men) >= min_size and len(women) >= min_size
        gap = _pct(avg_m, avg_w) if comparable else None
        med_gap = _pct(med_m, med_w) if comparable else None
        flagged = comparable and ((gap is not None and abs(gap) >= threshold) or (med_gap is not None and abs(med_gap) >= threshold))
        rows.append({
            "designation": desg, "grade": grade, "men": len(men), "women": len(women),
            "others": len(g.get("other", [])) + len(g.get("unknown", [])), "people": sum(len(v) for v in g.values()),
            "average_men": float(round(avg_m, 2)) if avg_m is not None else None,
            "average_women": float(round(avg_w, 2)) if avg_w is not None else None,
            "median_men": float(med_m) if med_m is not None else None,
            "median_women": float(med_w) if med_w is not None else None,
            "gap_percent": float(gap) if gap is not None else None,
            "median_gap_percent": float(med_gap) if med_gap is not None else None,
            "flag": ("Review: women paid less" if (gap or 0) > 0 or (med_gap or 0) > 0 else "Review: men paid less")
            if flagged else ("Within range" if comparable else "Not comparable"),
            "flagged": bool(flagged),
        })
    return rows


def equity_people(db: Session, company_id: int, on: date, basis: str = "annual_ctc", ids=None) -> list[dict]:
    from core.masters import MasterValue
    Employee = svc.employee_model()
    q = db.query(Employee).filter(Employee.company_id == company_id, Employee.archived_at.is_(None),
                                  Employee.status.in_(ACTIVE))
    if ids is not None:
        q = q.filter(Employee.id.in_(list(ids) or [-1]))
    emps = q.all()
    genders = {m.id: m.label for m in db.query(MasterValue).filter(
        MasterValue.id.in_([e.gender_id for e in emps if e.gender_id] or [-1])).all()}
    out = []
    for e in emps:
        s = svc.current_salary(db, company_id, e.id, on)
        if s is None:
            continue
        pay = d(s.monthly_gross) if basis == "monthly_gross" else d(s.annual_ctc)
        out.append({"employee_id": e.id, "designation": e.designation.designation_name if e.designation else None,
                    "grade": (e.grade.name if e.grade else None), "gender": genders.get(e.gender_id), "pay": pay})
    return out


# ── Cost against budget ────────────────────────────────────────────

def _slips_fy(db: Session, company_id: int, fy: str) -> list[Payslip]:
    a, b = calc.fy_bounds(fy)
    return (db.query(Payslip).join(PayRun, PayRun.id == Payslip.pay_run_id)
            .filter(Payslip.company_id == company_id, Payslip.period_start >= a, Payslip.period_start <= b,
                    PayRun.status.in_(("approved", "locked", "paid")), Payslip.status.in_(("calculated", "held"))).all())


def budget_names(db: Session, company_id: int, dimension: str) -> dict[int, str]:
    if dimension == "department":
        from modules.departments.models import Department
        return {x.id: x.department_name for x in db.query(Department).filter(
            (Department.company_id == company_id) | (Department.company_id.is_(None))).all()}
    from modules.organization.models import CostCentre
    return {x.id: x.name for x in db.query(CostCentre).filter(CostCentre.company_id == company_id).all()}


def cost_vs_budget(db: Session, company_id: int, fy: str, by: str, today: date) -> list[dict]:
    by = "cost_centre" if by in ("cost_centre", "cost centre", "cost-centre") else "department"
    names = budget_names(db, company_id, by)
    budgets = {names.get(b.ref_id, f"#{b.ref_id}"): b for b in db.query(PayrollBudget).filter(
        PayrollBudget.company_id == company_id, PayrollBudget.fy == fy, PayrollBudget.dimension == by).all()}
    actual = defaultdict(lambda: ZERO)
    gross = defaultdict(lambda: ZERO)
    people = defaultdict(set)
    months = set()
    for p in _slips_fy(db, company_id, fy):
        s = p.snapshot or {}
        key = (s.get("department") if by == "department" else (s.get("cost_centre") or {}).get("name")) or "Not assigned"
        actual[key] += d(p.employer_cost)
        gross[key] += d(p.gross)
        people[key].add(p.employee_id)
        months.add(p.period_start.strftime("%Y-%m"))
    a, b = calc.fy_bounds(fy)
    elapsed = max(1, len(months)) if months else 0
    rows = []
    for key in sorted(set(actual) | set(budgets)):
        budget = d(budgets[key].amount) if key in budgets else None
        act = actual.get(key, ZERO)
        projected = (act / elapsed * 12).quantize(Decimal("1")) if elapsed else ZERO
        used = float((act / budget * 100).quantize(Decimal("0.1"))) if budget else None
        rows.append({"name": key, "dimension": "Department" if by == "department" else "Cost centre",
                     "budget": float(budget) if budget is not None else None, "actual": float(act),
                     "gross": float(gross.get(key, ZERO)), "people": len(people.get(key, ())), "months": elapsed,
                     "projected": float(projected), "remaining": float(budget - act) if budget is not None else None,
                     "projected_variance": float(projected - budget) if budget is not None else None,
                     "used_percent": used,
                     "status": ("No budget" if budget is None else "Over budget" if projected > budget
                                else "On track")})
    return rows


# ── CTC breakup ────────────────────────────────────────────────────

def ctc_rows(db: Session, company_id: int, on: date, ids=None) -> list[dict]:
    Employee = svc.employee_model()
    q = db.query(Employee).filter(Employee.company_id == company_id, Employee.archived_at.is_(None))
    if ids is not None:
        q = q.filter(Employee.id.in_(list(ids) or [-1]))
    emps = {e.id: e for e in q.all()}
    comps = {c.code: c for c in structures.components(db, company_id)}
    out = []
    for eid, e in emps.items():
        s = svc.current_salary(db, company_id, eid, on)
        if s is None or (e.exit_date and e.exit_date < on):
            continue
        m = defaultdict(lambda: ZERO)
        for c in s.components or []:
            comp = comps.get(c["code"])
            kind = c.get("kind") or (comp.kind if comp else "earning")
            amt = d(c.get("monthly"))
            if c["code"] == "BASIC":
                m["basic"] += amt
            elif c["code"] == "DA":
                m["da"] += amt
            elif c["code"] == "HRA":
                m["hra"] += amt
            elif comp is not None and comp.fbp:
                m["flexible"] += amt
            elif kind == "earning":
                m["allowances"] += amt
            elif kind == "reimbursement":
                m["flexible"] += amt
            elif kind == "employer":
                m["employer"] += amt
        out.append({"employee_id": eid, "employee_code": e.employee_code, "employee_name": svc.name_of(e),
                    "department": e.department.department_name if e.department else None,
                    "designation": e.designation.designation_name if e.designation else None,
                    "grade": e.grade.name if e.grade else None,
                    "cost_centre": e.cost_centre.name if getattr(e, "cost_centre", None) else None,
                    "effective_from": s.effective_from, "annual_ctc": float(d(s.annual_ctc)),
                    "monthly_gross": float(d(s.monthly_gross)), "basic": float(m["basic"] * 12), "da": float(m["da"] * 12),
                    "hra": float(m["hra"] * 12), "allowances": float(m["allowances"] * 12),
                    "flexible": float(m["flexible"] * 12), "employer_contributions": float(m["employer"] * 12),
                    "reason": s.reason})
    return out


# ── Variance trend ─────────────────────────────────────────────────

def variance_trend(db: Session, company_id: int, fy: str, threshold) -> list[dict]:
    by_month: dict[str, dict] = {}
    for p in _slips_fy(db, company_id, fy):
        k = p.period_start.strftime("%Y-%m")
        m = by_month.setdefault(k, {"gross": ZERO, "net": ZERO, "cost": ZERO, "deductions": ZERO, "ids": set(),
                                    "regular_ids": set(), "off_cycle": ZERO})
        m["gross"] += d(p.gross)
        m["net"] += d(p.net)
        m["cost"] += d(p.employer_cost)
        m["deductions"] += d(p.deductions)
        m["ids"].add(p.employee_id)
    run_kind = {r.id: r.kind for r in db.query(PayRun).filter(PayRun.company_id == company_id).all()}
    for p in _slips_fy(db, company_id, fy):
        k = p.period_start.strftime("%Y-%m")
        if run_kind.get(p.pay_run_id) == "regular":
            by_month[k]["regular_ids"].add(p.employee_id)
        else:
            by_month[k]["off_cycle"] += d(p.employer_cost)
    rows, prev = [], None
    threshold = d(threshold)
    for k in sorted(by_month):
        m = by_month[k]
        change = (m["cost"] - prev["cost"]) if prev else None
        pct = ((change / prev["cost"]) * 100).quantize(Decimal("0.1")) if prev and prev["cost"] else None
        rows.append({"month": date.fromisoformat(k + "-01"), "employees": len(m["ids"]), "gross": float(m["gross"]),
                     "deductions": float(m["deductions"]), "net": float(m["net"]), "employer_cost": float(m["cost"]),
                     "off_cycle_cost": float(m["off_cycle"]),
                     "change": float(change) if change is not None else None,
                     "change_percent": float(pct) if pct is not None else None,
                     "joined": len(m["regular_ids"] - prev["regular_ids"]) if prev else None,
                     "left": len(prev["regular_ids"] - m["regular_ids"]) if prev else None,
                     "flag": "Big change" if pct is not None and abs(pct) >= threshold else ""})
        prev = m
    return rows


# ── Statutory liability ────────────────────────────────────────────

KIND_LABELS = {"pf": "Provident fund", "esi": "ESI", "tds": "TDS", "pt": "Professional Tax", "lwf": "Labour Welfare Fund"}


def statutory_liability(db: Session, company_id: int, fy: str, today: date) -> list[dict]:
    from modules.payroll import compliance
    from modules.payroll.models import StatutoryChallan
    a, b = calc.fy_bounds(fy)
    challans = {(c.kind, c.legal_entity_id, c.state, c.period): c for c in
                db.query(StatutoryChallan).filter(StatutoryChallan.company_id == company_id).all()}
    rows = []
    m = a
    last = min(b, today)
    while m <= last:
        for it in compliance.items_for_month(db, company_id, m):
            if it["kind"] not in KIND_LABELS:
                continue
            liability = d(it["amount"])
            c = challans.get((it["kind"], it["legal_entity_id"], it["state"], it["period"]))
            paid = d(c.amount) if c is not None and c.status == "paid" else ZERO
            status = c.status if c is not None else "pending"
            if c is not None and c.status == "paid" and not d(c.amount):
                paid = liability
            outstanding = max(liability - paid, ZERO) if status != "not_applicable" else ZERO
            late_by = None
            if status == "paid" and c.paid_on and c.paid_on > it["due_date"]:
                late_by = (c.paid_on - it["due_date"]).days
            elif status == "pending" and it["due_date"] < today and outstanding > 0:
                late_by = (today - it["due_date"]).days
            rows.append({"month": m, "kind": KIND_LABELS[it["kind"]], "state": it["state"] or None,
                         "entity": it.get("entity_name"), "due_date": it["due_date"], "liability": float(liability),
                         "paid": float(paid), "outstanding": float(outstanding),
                         "status": {"pending": "Overdue" if late_by else "Due", "paid": "Paid", "filed": "Filed",
                                    "not_applicable": "Not needed"}.get(status, status),
                         "paid_on": c.paid_on if c is not None else None, "reference": c.reference if c is not None else None,
                         "days_late": late_by})
        m = calc.add_months(m, 1)
    return rows

