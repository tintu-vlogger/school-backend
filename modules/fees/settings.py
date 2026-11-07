"""Fee settings of an institution, kept in companies.config["fees"] with defaults.

    sibling_discount     automatic concession for younger siblings (shared guardian)
    concession_approval  concessions go through the approval flow fees.concession
    allow_advance        money paid above the dues is kept as advance (wallet) instead of refused
    allow_partial_online parents may pay part of the dues online (pick instalments)
    reminders            automatic reminders N days before / after the due date
    bounce_charge        default charge billed when a cheque bounces
    receipt_note         printed at the bottom of receipts
"""
from __future__ import annotations

import copy
from decimal import Decimal

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core.errors import BadRequest

DEFAULTS: dict = {
    "sibling_discount": {"enabled": False, "percent": 10, "head_codes": ["TUITION"]},
    "concession_approval": True,
    "allow_advance": True,
    "allow_partial_online": True,
    "online_payments": True,
    "reminders": {"enabled": True, "days_before": [3], "days_after": [1, 7, 15], "email": True},
    "bounce_charge": 500,
    "receipt_note": "This is a computer-generated receipt and does not need a signature.",
}


def _company(db: Session, company_id: int):
    from modules.company.models import Company
    return db.query(Company).filter(Company.id == company_id).first()


def currency(db: Session, company_id: int) -> str:
    from modules.company.models import Company
    return db.query(Company.currency).filter(Company.id == company_id).scalar() or "INR"


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def get(db: Session, company_id: int) -> dict:
    c = _company(db, company_id)
    cfg = c.config if c is not None and isinstance(c.config, dict) else {}
    return _merge(DEFAULTS, cfg.get("fees") or {})


def _days(values, label: str) -> list[int]:
    out = []
    for v in values or []:
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise BadRequest(f"{label}: enter whole numbers of days.")
        if n < 0 or n > 365:
            raise BadRequest(f"{label}: use 0 to 365 days.")
        out.append(n)
    return sorted(set(out))[:6]


def save(db: Session, company_id: int, data: dict) -> dict:
    c = _company(db, company_id)
    current = get(db, company_id)
    merged = _merge(current, {k: v for k, v in (data or {}).items() if k in DEFAULTS})
    sd = merged["sibling_discount"]
    try:
        pct = Decimal(str(sd.get("percent") or 0))
    except Exception:
        raise BadRequest("Sibling discount: enter a percentage.")
    if pct < 0 or pct > 100:
        raise BadRequest("Sibling discount must be between 0 and 100 percent.")
    merged["sibling_discount"] = {"enabled": bool(sd.get("enabled")), "percent": float(pct),
                                  "head_codes": [str(x).strip().upper() for x in (sd.get("head_codes") or []) if str(x).strip()]}
    rem = merged["reminders"]
    merged["reminders"] = {"enabled": bool(rem.get("enabled")), "email": bool(rem.get("email", True)),
                           "days_before": _days(rem.get("days_before"), "Days before"),
                           "days_after": _days(rem.get("days_after"), "Days after")}
    try:
        merged["bounce_charge"] = float(Decimal(str(merged.get("bounce_charge") or 0)))
    except Exception:
        raise BadRequest("Cheque bounce charge should be an amount.")
    for k in ("concession_approval", "allow_advance", "allow_partial_online", "online_payments"):
        merged[k] = bool(merged.get(k))
    merged["receipt_note"] = str(merged.get("receipt_note") or "")[:400]
    cfg = dict(c.config) if isinstance(c.config, dict) else {}
    cfg["fees"] = merged
    c.config = cfg
    flag_modified(c, "config")
    db.flush()
    return merged
