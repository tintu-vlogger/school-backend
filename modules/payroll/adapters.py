"""What payroll reads from other modules, each behind a small adapter so payroll keeps
working when a module is not installed (or is still being built):

attendance   payable days, LOP, overtime hours and shift allowance per employee per period;
             falls back to approved loss-of-pay leave plus the leave work calendar
organisation legal entity (PAN, TAN, PF and ESI codes, PT/LWF registrations, bank accounts)
             and the employee's work state; falls back to company fields and the State master
assets       recovery amounts for damaged or unreturned assets (final settlement)
leave        LOP days, leave balances, encashment on exit, working days
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from modules.payroll.calc import ZERO, d

log = logging.getLogger("hrms")


def _try(fn, default=None):
    try:
        return fn()
    except ImportError:
        return default


# ── Attendance and leave ────────────────────────────────────────────

def attendance_summary(db: Session, company_id: int, start: date, end: date, employee_ids: list[int]) -> dict[int, dict] | None:
    """{employee_id: {payable_days, lop_days, overtime_hours, shift_allowance_amount, ...}} or
    None when the attendance module is not there."""
    try:
        from modules.attendance import service as att
    except ImportError:
        return None
    if not hasattr(att, "payroll_summary"):
        return None
    try:
        with db.begin_nested():
            rows = att.payroll_summary(db, company_id, start, end, employee_ids or None)
    except Exception as exc:  # attendance problems must not stop payroll; fall back to leave
        log.warning("Attendance summary failed for company %s: %s", company_id, exc)
        return None
    return {r["employee_id"]: r for r in rows}


def leave_lop(db: Session, company_id: int, start: date, end: date, employee_ids: list[int]) -> dict[int, dict]:
    """{employee_id: {lop_days, pending_days}} from approved (and pending) leave."""
    try:
        from modules.leave import reports
    except ImportError:
        return {}
    s = reports.lop_summary(db, company_id, start, end, ids=set(employee_ids) if employee_ids else None)
    return {r["employee_id"]: {"lop_days": d(r["lop_days"]), "pending_days": d(r["pending_days"])} for r in s["rows"]}


def working_days(db: Session, employee, start: date, end: date) -> Decimal | None:
    try:
        from modules.leave.calendar import working_days as wd
    except ImportError:
        return None
    try:
        return d(wd(db, employee, start, end))
    except Exception:
        return None


def is_working_day_fn(db: Session, employee):
    try:
        from modules.leave.calendar import is_working_day
    except ImportError:
        return lambda day: day.weekday() < 5
    return lambda day: bool(is_working_day(db, employee, day))


def pending_approvals(db: Session, company_id: int, start: date, end: date) -> dict:
    """Leave and attendance requests still waiting in the period, for the pre-checks."""
    out = {"leave": [], "attendance": []}
    try:
        from modules.leave.models import LeaveRequest
        rows = db.query(LeaveRequest.employee_id, LeaveRequest.reference_no).filter(
            LeaveRequest.company_id == company_id, LeaveRequest.status.in_(("pending", "cancel_pending")),
            LeaveRequest.from_date <= end, LeaveRequest.to_date >= start).all()
        out["leave"] = [{"employee_id": r[0], "reference": r[1]} for r in rows]
    except ImportError:
        pass
    try:
        from modules.attendance.models import AttendanceRequest
        rows = db.query(AttendanceRequest.employee_id, AttendanceRequest.kind).filter(
            AttendanceRequest.company_id == company_id, AttendanceRequest.status == "pending",
            AttendanceRequest.date <= end, AttendanceRequest.date >= start).all()
        out["attendance"] = [{"employee_id": r[0], "kind": r[1]} for r in rows]
    except ImportError:
        pass
    return out


def attendance_locked(db: Session, company_id: int, start: date, end: date) -> bool | None:
    try:
        from modules.attendance import service as att
    except ImportError:
        return None
    try:
        return bool(att.is_locked(db, company_id, start) and att.is_locked(db, company_id, end))
    except Exception:
        return None


def leave_balances(db: Session, company_id: int, employee_id: int) -> list[dict]:
    try:
        from modules.leave import reports
    except ImportError:
        return []
    try:
        rep = reports.balance_report(db, company_id, None, ids={employee_id})
    except Exception:
        return []
    return [{"code": r["leave_type"], "name": r["leave_type_name"], "balance": r["balance"]} for r in rep["rows"]]


def encashable_leave(db: Session, company_id: int, employee, on: date) -> list[dict]:
    """Days that would be encashed on exit (preview; nothing is written)."""
    try:
        from modules.leave import ledger, policy
    except ImportError:
        return []
    out = []
    for t in policy.list_types(db, company_id):
        try:
            rules = policy.rules_on(t, on)
            if not rules["year_end"]["encashment"]["on_exit"]:
                continue
            year, _, _ = policy.year_bounds(rules, on)
            bal = d(ledger.balance(db, company_id, employee.id, t, year))
        except Exception:
            continue
        if bal > 0:
            out.append({"code": t.code, "name": t.name, "days": bal})
    return out


def encash_leave_on_exit(db: Session, company_id: int, employee, on: date) -> list[dict]:
    try:
        from modules.leave import ledger
    except ImportError:
        return []
    return ledger.encash_on_exit(db, company_id, employee, on)


# ── Organisation ───────────────────────────────────────────────────

def _master_code(db: Session, value_id: int | None) -> str | None:
    if not value_id:
        return None
    from core.masters import MasterValue
    v = db.query(MasterValue).filter(MasterValue.id == value_id).first()
    return v.code if v else None


def _state_code_from_name(db: Session, company_id: int, name: str | None) -> str | None:
    if not name:
        return None
    from modules.payroll.statutory_data import PT, LWF  # noqa: F401
    from core.master_seed import STATES
    n = name.strip().lower()
    for st_name, code, _gst, _kind in STATES:
        if st_name.lower() == n or code.lower() == n:
            return code
    return None


def employee_state(db: Session, employee, company, settings: dict) -> str | None:
    """Work state for PT and LWF: the employee's location, else their legal entity, else
    the payroll default state, else the company address."""
    try:
        from modules.organization.models import LegalEntity, Location
        if getattr(employee, "location_id", None):
            loc = db.query(Location).filter(Location.id == employee.location_id).first()
            code = _master_code(db, loc.state_id) if loc else None
            if code:
                return code
        if getattr(employee, "legal_entity_id", None):
            le = db.query(LegalEntity).filter(LegalEntity.id == employee.legal_entity_id).first()
            code = _master_code(db, le.state_id) if le else None
            if code:
                return code
    except ImportError:
        pass
    return settings.get("default_state") or _state_code_from_name(db, company.id, company.state)


def employee_city(db: Session, employee) -> str | None:
    try:
        from modules.organization.models import Location
        if getattr(employee, "location_id", None):
            loc = db.query(Location).filter(Location.id == employee.location_id).first()
            return loc.city if loc else None
    except ImportError:
        pass
    return None


def legal_entity(db: Session, company, entity_id: int | None) -> dict:
    """Registration details for files and payslips."""
    base = {"id": 0, "name": company.company_name, "legal_name": company.company_name, "pan": company.pan,
            "tan": company.tan, "pf_code": (company.config or {}).get("pf_code"),
            "esi_code": (company.config or {}).get("esi_code"), "address": ", ".join(
                x for x in [company.address, company.city, company.state, company.pincode] if x),
            "registrations": [], "bank_accounts": [], "logo_url": company.logo_url, "state": None,
            "signatory": {}}
    try:
        from modules.organization.models import LegalEntity
    except ImportError:
        return base
    q = db.query(LegalEntity).filter(LegalEntity.company_id == company.id, LegalEntity.active.is_(True))
    le = q.filter(LegalEntity.id == entity_id).first() if entity_id else None
    le = le or q.filter(LegalEntity.is_primary.is_(True)).first() or q.order_by(LegalEntity.id).first()
    if not le:
        return base
    return {"id": le.id, "name": le.name, "legal_name": le.legal_name or le.name, "pan": le.pan or company.pan,
            "tan": le.tan or company.tan, "pf_code": le.pf_code or base["pf_code"], "esi_code": le.esi_code or base["esi_code"],
            "address": ", ".join(x for x in [le.address_line1, le.address_line2, le.city, le.pincode] if x) or base["address"],
            "registrations": le.registrations or [], "bank_accounts": le.bank_accounts or [], "logo_url": company.logo_url,
            "state": _master_code(db, le.state_id), "signatory": le.signatory or {}}


def legal_entities(db: Session, company) -> list[dict]:
    try:
        from modules.organization.models import LegalEntity
    except ImportError:
        return [legal_entity(db, company, None)]
    rows = db.query(LegalEntity).filter(LegalEntity.company_id == company.id, LegalEntity.active.is_(True)).all()
    return [legal_entity(db, company, r.id) for r in rows] or [legal_entity(db, company, None)]


def grade_code(db: Session, grade_id: int | None) -> str | None:
    if not grade_id:
        return None
    try:
        from modules.organization.models import Grade
    except ImportError:
        return None
    g = db.query(Grade).filter(Grade.id == grade_id).first()
    return (g.code or g.name) if g else None


def grade_names(db: Session, company_id: int) -> dict[int, str]:
    try:
        from modules.organization.models import Grade
    except ImportError:
        return {}
    return {g.id: g.name for g in db.query(Grade).filter(Grade.company_id == company_id).all()}


def cost_centre(db: Session, cost_centre_id: int | None) -> dict | None:
    if not cost_centre_id:
        return None
    try:
        from modules.organization.models import CostCentre
    except ImportError:
        return None
    c = db.query(CostCentre).filter(CostCentre.id == cost_centre_id).first()
    return {"code": c.code or str(c.id), "name": c.name} if c else None


# ── Assets ──────────────────────────────────────────────────────────

def asset_recoveries(db: Session, company_id: int, employee_id: int) -> list[dict]:
    try:
        from modules.assets import service as assets
    except ImportError:
        return []
    out = []
    try:
        res = assets.recoveries(db, company_id, employee_id=employee_id, status="pending")
        for r in res["items"]:
            out.append({"ref": r["assignment_uuid"], "label": f"{r['asset_name']} ({r['asset_tag']})",
                        "amount": d(r["amount"]), "reason": r.get("reason")})
        if hasattr(assets, "outstanding_assets"):
            for a in assets.outstanding_assets(db, company_id, employee_id) or []:
                out.append({"ref": None, "label": f"Not returned: {a.get('name') or a.get('asset_name')}",
                            "amount": ZERO, "pending_return": True})
    except Exception as exc:
        log.warning("Asset recoveries failed: %s", exc)
    return out


def mark_assets_recovered(db: Session, company_id: int, refs: list[str], settlement_ref: str) -> None:
    try:
        from modules.assets import service as assets
    except ImportError:
        return
    for r in refs:
        if r:
            try:
                assets.mark_recovered(db, company_id, r, settlement_ref)
            except Exception as exc:
                log.warning("Could not mark asset recovered: %s", exc)
