"""Offer CTC break-up. Uses the payroll module's salary structures when payroll is set up;
otherwise a simple Indian split (basic 50% of gross, HRA 40/50% of basic, employer PF on
basic up to ₹15,000, special allowance as the balance) so offers still work."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.orm import Session

from core.errors import BadRequest


def _d(x) -> Decimal:
    return Decimal(str(x or 0))


def _r(x: Decimal) -> Decimal:
    return x.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def structures(db: Session, company_id: int) -> list[dict]:
    try:
        from modules.payroll import structures as ps
        rows = ps.structures(db, company_id)
    except Exception:
        return []
    return [{"id": s.id, "uuid": str(s.uuid), "name": s.name, "min_ctc": s.min_ctc, "max_ctc": s.max_ctc}
            for s in rows if getattr(s, "active", True)]


def _from_payroll(db: Session, company_id: int, annual_ctc: Decimal, structure_id: int, on, metro: bool) -> dict | None:
    try:
        from modules.payroll import service as pservice, structures as ps
    except Exception:
        return None
    s = ps.structure_by_id(db, company_id, structure_id)
    if s is None:
        return None
    comps = ps.component_map(db, company_id)
    rules = pservice.rules_for_ctc(db, company_id, on)
    res = ps.compute_salary(lines=s.lines, comps=comps, annual_ctc=annual_ctc, metro=metro, rules=rules,
                            include_employer_pf=s.include_employer_pf_in_ctc, include_gratuity=s.include_gratuity_in_ctc,
                            min_ctc=s.min_ctc, max_ctc=s.max_ctc,
                            wage_share=rules["wage_rule"].get("min_wage_share_percent", 50))
    lines = [{"code": c["code"], "label": c["name"], "kind": c["kind"], "monthly": str(c["monthly"]),
              "annual": str(c["annual"])} for c in res["components"]]
    return {"source": "payroll", "structure": s.name, "lines": lines, "monthly_gross": str(res["monthly_gross"]),
            "monthly_ctc": str(res["monthly_ctc"]), "warnings": [w["message"] for w in res.get("warnings", [])]}


def simple_split(annual_ctc: Decimal, metro: bool = True) -> dict:
    monthly = annual_ctc / 12
    # employer PF is 12% of basic capped at ₹15,000 of basic → solve basic = 50% of gross
    gross = monthly
    for _ in range(6):
        basic = gross * Decimal("0.5")
        pf_er = min(basic, Decimal("15000")) * Decimal("0.12")
        gross = monthly - pf_er
    basic = _r(gross * Decimal("0.5"))
    pf_er = _r(min(basic, Decimal("15000")) * Decimal("0.12"))
    hra = _r(basic * (Decimal("0.5") if metro else Decimal("0.4")))
    gross = _r(monthly) - pf_er
    special = gross - basic - hra
    if special < 0:
        raise BadRequest("The CTC is too low to split. Check the amount.")
    rows = [("BASIC", "Basic", "earning", basic), ("HRA", "House rent allowance", "earning", hra),
            ("SPECIAL", "Special allowance", "earning", special), ("PF_ER", "Employer PF", "employer", pf_er)]
    lines = [{"code": c, "label": lbl, "kind": k, "monthly": str(v), "annual": str(v * 12)} for c, lbl, k, v in rows]
    return {"source": "standard", "structure": "Standard split", "lines": lines, "monthly_gross": str(gross),
            "monthly_ctc": str(_r(monthly)), "warnings": []}


def breakup(db: Session, company_id: int, annual_ctc, structure_id: int | None, on, metro: bool = True) -> dict:
    ctc = _d(annual_ctc)
    if ctc <= 0:
        raise BadRequest("Enter the annual CTC.")
    if structure_id:
        try:
            out = _from_payroll(db, company_id, ctc, structure_id, on, metro)
        except BadRequest:
            raise
        except Exception:
            out = None
        if out:
            return out
    return simple_split(ctc, metro)


def letter_values(b: dict) -> dict:
    """Merge values for the offer and appointment letter templates."""
    by = {ln["code"]: ln for ln in b.get("lines", [])}

    def m(*codes):
        for c in codes:
            if c in by:
                return by[c]["monthly"]
        return None
    special = m("SPECIAL", "SPL", "SPECIAL_ALLOWANCE", "SA")
    return {k: v for k, v in {
        "salary.basic": m("BASIC"), "salary.hra": m("HRA"), "salary.special_allowance": special,
        "salary.gross_monthly": b.get("monthly_gross"), "salary.ctc_monthly": b.get("monthly_ctc"),
    }.items() if v is not None}
