"""The statutory library: platform rules, company overrides and resolution by date.

    rule(db, company_id, kind, scope, on)      library version in force on `on`, merged with the
                                               company's override in force on that date
    RuleBook(db, company_id, on)               the same, cached for a pay run
    ensure_library(db)                         write the shipped values when missing (start-up)
    validate_override(kind, base, data)        companies may be more generous than the law, never less

Library rows have no company_id (shared by every company); only platform admins change them.
"""
from __future__ import annotations

import copy
from datetime import date

from sqlalchemy import or_
from sqlalchemy.orm import Session

from core.errors import BadRequest
from modules.payroll import statutory_data
from modules.payroll.calc import d
from modules.payroll.models import StatutoryOverride, StatutoryRule

KIND_LABELS = {
    "epf": "Provident fund", "esi": "ESI", "pt": "Professional Tax", "lwf": "Labour Welfare Fund",
    "min_wage": "Minimum wages", "income_tax": "Income tax slabs", "tax_sections": "Tax deductions",
    "perquisites": "Perquisites", "gratuity": "Gratuity", "bonus": "Statutory bonus", "retrenchment": "Retrenchment",
    "leave_encashment": "Leave encashment", "wage_rule": "Code on Wages limits", "floor_wage": "National floor wage",
    "due_dates": "Due dates", "forms": "Form names",
}


def ensure_library(db: Session) -> int:
    """Insert shipped library rows that are not there yet. Existing rows (possibly edited by
    the platform team) are never overwritten."""
    have = {(r.kind, r.scope, r.effective_from) for r in db.query(StatutoryRule.kind, StatutoryRule.scope,
                                                                   StatutoryRule.effective_from).all()}
    added = 0
    for kind, scope, start, data, note in statutory_data.library():
        key = (kind, scope, date.fromisoformat(start))
        if key in have:
            continue
        db.add(StatutoryRule(kind=kind, scope=scope, effective_from=key[2], data=data, note=note,
                             created_by_name="Statutory library"))
        added += 1
    if added:
        db.flush()
    return added


def deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _library_row(db: Session, kind: str, scope: str | None, on: date) -> StatutoryRule | None:
    q = db.query(StatutoryRule).filter(StatutoryRule.kind == kind, StatutoryRule.status == "live",
                                       StatutoryRule.effective_from <= on)
    q = q.filter(StatutoryRule.scope == scope) if scope is not None else q.filter(StatutoryRule.scope.is_(None))
    return q.order_by(StatutoryRule.effective_from.desc()).first()


def _override_row(db: Session, company_id: int, kind: str, scope: str | None, on: date) -> StatutoryOverride | None:
    q = db.query(StatutoryOverride).filter(StatutoryOverride.company_id == company_id, StatutoryOverride.kind == kind,
                                           StatutoryOverride.active.is_(True), StatutoryOverride.effective_from <= on)
    q = q.filter(StatutoryOverride.scope == scope) if scope is not None else q.filter(StatutoryOverride.scope.is_(None))
    return q.order_by(StatutoryOverride.effective_from.desc(), StatutoryOverride.id.desc()).first()


def rule(db: Session, company_id: int | None, kind: str, scope: str | None, on: date) -> dict | None:
    """The rule in force on `on`: the library version merged with the company override (an
    override that starts later than a newer library version is ignored for that version)."""
    lib = _library_row(db, kind, scope, on)
    ov = _override_row(db, company_id, kind, scope, on) if company_id else None
    if lib is None and ov is None:
        return None
    if lib is None:
        return copy.deepcopy(ov.data)
    if ov is None or ov.effective_from < lib.effective_from:
        return copy.deepcopy(lib.data)
    return deep_merge(lib.data, ov.data)


class RuleBook:
    """Rules for one company on one date, cached (one pay run, one settlement)."""

    def __init__(self, db: Session, company_id: int, on: date):
        self.db, self.company_id, self.on = db, company_id, on
        self._cache: dict = {}

    def get(self, kind: str, scope: str | None = None, on: date | None = None) -> dict | None:
        key = (kind, scope, on or self.on)
        if key not in self._cache:
            self._cache[key] = rule(self.db, self.company_id, kind, scope, on or self.on)
        return self._cache[key]

    def need(self, kind: str, scope: str | None = None, on: date | None = None) -> dict:
        r = self.get(kind, scope, on)
        if r is None:
            raise BadRequest(f"The statutory library has no {KIND_LABELS.get(kind, kind)} rule for "
                             f"{(on or self.on).isoformat()}. Ask the platform team to add it.")
        return r


def validate_override(kind: str, base: dict | None, data: dict) -> list[str]:
    """Blocks a company override that goes below the law; returns plain messages."""
    if kind in statutory_data.NO_OVERRIDE:
        return [f"{KIND_LABELS.get(kind, kind)} is set by law and cannot be changed by a company."]
    errors = []
    base = base or {}
    for f in statutory_data.FLOOR_FIELDS.get(kind, []):
        if f in data and f in base and d(data[f]) < d(base[f]):
            errors.append(f"{f.replace('_', ' ').capitalize()} cannot be lower than the legal {base[f]}.")
    for f in statutory_data.CEILING_FIELDS.get(kind, []):
        if f in data and f in base and d(data[f]) > d(base[f]):
            errors.append(f"{f.replace('_', ' ').capitalize()} cannot be higher than the legal {base[f]}.")
    if kind == "pt":
        cap = (base or {}).get("annual_cap", 2500)
        if d(data.get("annual_cap", cap)) > d(2500):
            errors.append("Professional Tax cannot be more than ₹2,500 a year.")
    return errors


def library_rows(db: Session, kind: str | None = None, scope: str | None = None) -> list[StatutoryRule]:
    q = db.query(StatutoryRule)
    if kind:
        q = q.filter(StatutoryRule.kind == kind)
    if scope:
        q = q.filter(StatutoryRule.scope == scope)
    return q.order_by(StatutoryRule.kind, StatutoryRule.scope, StatutoryRule.effective_from.desc()).all()


def overrides(db: Session, company_id: int, kind: str | None = None) -> list[StatutoryOverride]:
    q = db.query(StatutoryOverride).filter(StatutoryOverride.company_id == company_id)
    if kind:
        q = q.filter(StatutoryOverride.kind == kind)
    return q.order_by(StatutoryOverride.kind, StatutoryOverride.scope, StatutoryOverride.effective_from.desc()).all()


def states_with(db: Session, kind: str, on: date) -> list[str]:
    rows = db.query(StatutoryRule.scope).filter(StatutoryRule.kind == kind, StatutoryRule.effective_from <= on,
                                                or_(StatutoryRule.scope.isnot(None))).distinct().all()
    return sorted(r[0] for r in rows if r[0])


def rule_out(r) -> dict:
    return {"uuid": str(r.uuid), "kind": r.kind, "kind_label": KIND_LABELS.get(r.kind, r.kind), "scope": r.scope,
            "effective_from": r.effective_from.isoformat(), "data": r.data,
            "note": getattr(r, "note", None) or getattr(r, "reason", None), "source": getattr(r, "source", None),
            "status": getattr(r, "status", "live"), "active": getattr(r, "active", True),
            "created_by_name": r.created_by_name, "updated_at": r.updated_at.isoformat() if r.updated_at else None}


def import_min_wages(rows: list[dict]) -> dict[str, dict]:
    """Rows from a CSV/Excel import: state, zone, skill, monthly (daily optional) -> data per state."""
    from modules.payroll.statutory_data import SKILLS
    out: dict[str, dict] = {}
    for i, r in enumerate(rows, start=2):
        st = str(r.get("state") or "").strip().upper()
        skill = str(r.get("skill") or "").strip().lower().replace("-", "_").replace(" ", "_")
        if not st or skill not in SKILLS:
            raise BadRequest(f"Row {i}: give a state code and a skill ({', '.join(SKILLS)}).")
        try:
            monthly = d(r.get("monthly"))
        except Exception:
            raise BadRequest(f"Row {i}: the monthly wage must be a number.")
        if monthly <= 0:
            raise BadRequest(f"Row {i}: the monthly wage must be more than zero.")
        daily = d(r.get("daily")) if r.get("daily") not in (None, "") else (monthly / 26).quantize(d("0.01"))
        out.setdefault(st, {"rates": []})["rates"].append(
            {"zone": str(r.get("zone") or "all").strip().lower().replace(" ", "_"), "skill": skill,
             "monthly": float(monthly), "daily": float(daily)})
    return out


def minimum_wage(book: RuleBook, state: str | None, zone: str | None, skill: str | None) -> dict | None:
    """Monthly minimum wage for a state, zone and skill (the zone falls back to 'all' or the
    lowest zone listed); never below the national floor wage."""
    floor = book.get("floor_wage") or {}
    floor_m = d(floor.get("monthly"))
    data = book.get("min_wage", state) if state else None
    if not data:
        return {"monthly": floor_m, "source": "National floor wage", "state": state} if floor_m else None
    rates = [r for r in data.get("rates", []) if r.get("skill") == (skill or "unskilled")]
    pick = next((r for r in rates if r.get("zone") == (zone or "")), None) \
        or next((r for r in rates if r.get("zone") == "all"), None) \
        or (min(rates, key=lambda r: d(r["monthly"])) if rates else None)
    if not pick:
        return {"monthly": floor_m, "source": "National floor wage", "state": state} if floor_m else None
    return {"monthly": max(d(pick["monthly"]), floor_m), "source": f"{state} {pick['zone']} {pick['skill']}",
            "state": state, "zone": pick["zone"], "skill": pick["skill"]}
