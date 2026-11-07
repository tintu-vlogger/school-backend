"""Employee detail rows: emergency contacts, family members, nominees, bank accounts,
education and previous employment. Each kind has its own checks; responses add master
labels. Bank accounts are switched off rather than deleted; other rows can be removed
(the audit log keeps what they held)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.errors import BadRequest, NotFound
from core.masters import labels_for, validate_value_ids
from modules.employees import validators as v
from modules.employees.models import (BankAccount, Education, EmergencyContact, FamilyMember, Nominee,
                                      PreviousEmployment)

NOMINEE_KINDS = {"pf": "Provident Fund", "eps": "Pension (EPS)", "gratuity": "Gratuity", "insurance": "Insurance",
                 "other": "Other"}
ACCOUNT_TYPES = {"savings": "Savings", "current": "Current", "salary": "Salary", "nre": "NRE", "nro": "NRO"}


def _fy_label(d: date) -> str:
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


def current_fy() -> str:
    from modules.employees.service import today
    return _fy_label(today())


# ── Per-kind cleaning ────────────────────────────────────────────────

def _req(data: dict, row, field: str, message: str):
    value = data.get(field, getattr(row, field, None) if row is not None else None)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise BadRequest(message)


def _masters(db: Session, company_id: int, data: dict, fields: dict[str, str]):
    for field, key in fields.items():
        if data.get(field) is not None:
            validate_value_ids(db, company_id, key, [int(data[field])])


def clean_emergency(db, company_id, employee_id, data, row):
    _req(data, row, "name", "Enter the contact's name.")
    _req(data, row, "phone", "Enter a phone number for the contact.")
    if "phone" in data:
        data["phone"] = v.clean_phone(data["phone"])
    if "alternate_phone" in data:
        data["alternate_phone"] = v.clean_phone(data["alternate_phone"], "Alternate phone")
    _masters(db, company_id, data, {"relation_id": "relation"})
    return data


def clean_family(db, company_id, employee_id, data, row):
    _req(data, row, "name", "Enter the family member's name.")
    if data.get("date_of_birth") and data["date_of_birth"] > date.today():
        raise BadRequest("Date of birth can't be in the future.")
    if "phone" in data:
        data["phone"] = v.clean_phone(data["phone"])
    _masters(db, company_id, data, {"relation_id": "relation", "gender_id": "gender"})
    return data


def clean_nominee(db, company_id, employee_id, data, row):
    kind = data.get("kind", row.kind if row else None)
    if kind not in NOMINEE_KINDS:
        raise BadRequest("Pick what the nomination is for: PF, EPS, gratuity or insurance.")
    if data.get("family_member_id"):
        fm = db.query(FamilyMember).filter(FamilyMember.company_id == company_id, FamilyMember.employee_id == employee_id,
                                           FamilyMember.id == data["family_member_id"]).first()
        if fm is None:
            raise BadRequest("The family member was not found.")
        data.setdefault("name", fm.name)
        data.setdefault("relation_id", fm.relation_id)
        data.setdefault("date_of_birth", fm.date_of_birth)
    _req(data, row, "name", "Enter the nominee's name.")
    _req(data, row, "share_percent", "Enter the nominee's share.")
    _masters(db, company_id, data, {"relation_id": "relation"})
    share = Decimal(str(data.get("share_percent", row.share_percent if row else 0)))
    q = db.query(func.coalesce(func.sum(Nominee.share_percent), 0)).filter(
        Nominee.company_id == company_id, Nominee.employee_id == employee_id, Nominee.kind == kind)
    if row is not None:
        q = q.filter(Nominee.id != row.id)
    others = Decimal(str(q.scalar() or 0))
    if others + share > 100:
        raise BadRequest(f"{NOMINEE_KINDS[kind]} shares would add up to {others + share}%. They can't be more than 100%.")
    dob = data.get("date_of_birth", row.date_of_birth if row else None)
    if dob and (date.today() - dob).days < 18 * 365 and not data.get("guardian_name", row.guardian_name if row else None):
        raise BadRequest("The nominee is a minor. Enter the guardian's name.")
    return data


def clean_bank(db, company_id, employee_id, data, row):
    _req(data, row, "account_holder", "Enter the account holder's name.")
    _req(data, row, "account_number", "Enter the account number.")
    _req(data, row, "ifsc", "Enter the IFSC.")
    if "account_number" in data:
        if row is not None and "•" in (data["account_number"] or ""):
            data.pop("account_number")  # a masked number sent back means "unchanged"
        else:
            data["account_number"] = v.clean_account_number(data["account_number"])
    if "ifsc" in data:
        data["ifsc"] = v.clean_ifsc(data["ifsc"])
    if data.get("account_type") and data["account_type"] not in ACCOUNT_TYPES:
        raise BadRequest("Account type should be savings, current, salary, NRE or NRO.")
    _masters(db, company_id, data, {"bank_id": "bank"})
    if data.get("bank_id") and data.get("ifsc"):
        from core.masters import MasterValue
        code = db.query(MasterValue.code).filter(MasterValue.id == data["bank_id"]).scalar()
        if code and len(code) == 4 and code.isalpha() and data["ifsc"][:4] != code.upper():
            raise BadRequest(f"This IFSC doesn't belong to the bank picked (it should start with {code.upper()}).")
    number = data.get("account_number")
    if number:
        dup = db.query(BankAccount.id).filter(BankAccount.company_id == company_id, BankAccount.account_number == number,
                                              BankAccount.active.is_(True))
        if row is not None:
            dup = dup.filter(BankAccount.id != row.id)
        if dup.first():
            raise BadRequest("This bank account is already on someone's record.")
    return data


def clean_education(db, company_id, employee_id, data, row):
    _req(data, row, "degree", "Enter the degree or course.")
    s, e = data.get("start_year", row.start_year if row else None), data.get("end_year", row.end_year if row else None)
    if s and e and e < s:
        raise BadRequest("The end year can't be before the start year.")
    _masters(db, company_id, data, {"level_id": "education_level"})
    return data


def clean_experience(db, company_id, employee_id, data, row):
    _req(data, row, "company_name", "Enter the employer's name.")
    f, t = data.get("from_date", row.from_date if row else None), data.get("to_date", row.to_date if row else None)
    if f and t and t < f:
        raise BadRequest("The end date can't be before the start date.")
    if data.get("financial_year"):
        import re
        if not re.fullmatch(r"20[0-9]{2}-[0-9]{2}", data["financial_year"]):
            raise BadRequest("Financial year looks like 2026-27.")
    elif (data.get("fy_taxable_income") or data.get("fy_tds")) and not (row and row.financial_year):
        data["financial_year"] = current_fy()
    income = data.get("fy_taxable_income", row.fy_taxable_income if row else None)
    tds = data.get("fy_tds", row.fy_tds if row else None)
    if income is not None and tds is not None and tds > income:
        raise BadRequest("Tax deducted can't be more than the income.")
    return data


def _ensure_one_primary(db: Session, model, company_id: int, employee_id: int, row) -> None:
    if row.is_primary:
        db.query(model).filter(model.company_id == company_id, model.employee_id == employee_id, model.id != row.id,
                               model.is_primary.is_(True)).update({"is_primary": False})
    elif not db.query(model.id).filter(model.company_id == company_id, model.employee_id == employee_id,
                                       model.is_primary.is_(True), model.id != row.id,
                                       *( [model.active.is_(True)] if hasattr(model, "active") else [])).first():
        row.is_primary = True


# ── Registry ─────────────────────────────────────────────────────────

KINDS = {
    "emergency-contacts": (EmergencyContact, clean_emergency, "Emergency contact"),
    "family": (FamilyMember, clean_family, "Family member"),
    "nominees": (Nominee, clean_nominee, "Nominee"),
    "bank-accounts": (BankAccount, clean_bank, "Bank account"),
    "education": (Education, clean_education, "Education"),
    "experience": (PreviousEmployment, clean_experience, "Previous job"),
}
# Kinds an employee may change on their own record (audited); the rest go through HR.
SELF_SERVICE_KINDS = {"emergency-contacts", "family", "education", "experience"}
ORDER = {
    "emergency-contacts": lambda m: (m.is_primary.desc(), m.id),
    "family": lambda m: (m.id,),
    "nominees": lambda m: (m.kind, m.id),
    "bank-accounts": lambda m: (m.active.desc(), m.is_primary.desc(), m.id),
    "education": lambda m: (m.end_year.desc().nulls_last(), m.id),
    "experience": lambda m: (m.from_date.desc().nulls_last(), m.id),
}


def kind_or_404(kind: str):
    if kind not in KINDS:
        raise NotFound("Section")
    return KINDS[kind]


def list_rows(db: Session, company_id: int, employee_id: int, kind: str):
    model, _, _ = kind_or_404(kind)
    return (db.query(model).filter(model.company_id == company_id, model.employee_id == employee_id)
            .order_by(*ORDER[kind](model)).all())


def get_row(db: Session, company_id: int, employee_id: int, kind: str, uuid):
    model, _, label = kind_or_404(kind)
    row = db.query(model).filter(model.company_id == company_id, model.employee_id == employee_id,
                                 model.uuid == uuid).first()
    if row is None:
        raise NotFound(label)
    return row


def save_row(db: Session, company_id: int, employee_id: int, kind: str, data: dict, row=None, *, can_verify=False):
    model, clean, _ = kind_or_404(kind)
    data = {k: (val.strip() or None) if isinstance(val, str) else val for k, val in data.items()}
    if kind == "bank-accounts" and not can_verify:
        data.pop("verified", None)
    data = clean(db, company_id, employee_id, data, row)
    if row is None:
        row = model(company_id=company_id, employee_id=employee_id)
        db.add(row)
    if kind == "bank-accounts" and row.id and ("account_number" in data or "ifsc" in data):
        if "verified" not in data:
            row.verified = False  # new details need checking again
    for key, val in data.items():
        if hasattr(model, key) and key not in ("id", "uuid", "company_id", "employee_id"):
            setattr(row, key, val)
    db.flush()
    if hasattr(model, "is_primary"):
        _ensure_one_primary(db, model, company_id, employee_id, row)
    return row


def remove_row(db: Session, company_id: int, employee_id: int, kind: str, row) -> None:
    if kind == "bank-accounts":
        if row.is_primary and db.query(BankAccount.id).filter(
                BankAccount.company_id == company_id, BankAccount.employee_id == employee_id,
                BankAccount.active.is_(True), BankAccount.id != row.id).first() is None:
            raise BadRequest("Salary is paid into this account. Add another account before switching this one off.")
        row.active = False
        if row.is_primary:
            row.is_primary = False
            other = db.query(BankAccount).filter(BankAccount.company_id == company_id, BankAccount.employee_id == employee_id,
                                                 BankAccount.active.is_(True), BankAccount.id != row.id).first()
            if other:
                other.is_primary = True
        return
    if kind == "family":
        db.query(Nominee).filter(Nominee.company_id == company_id, Nominee.family_member_id == row.id).update(
            {"family_member_id": None})
    db.delete(row)


# ── Output ───────────────────────────────────────────────────────────

def _plain(x):
    if isinstance(x, Decimal):
        return float(x)
    return x


def out_rows(db: Session, company_id: int, kind: str, rows, *, full_bank: bool = False) -> list[dict]:
    model, _, _ = kind_or_404(kind)
    master_cols = [c for c in ("relation_id", "gender_id", "bank_id", "level_id") if hasattr(model, c)]
    labels = labels_for(db, company_id, [getattr(r, c) for r in rows for c in master_cols if getattr(r, c)])
    out = []
    for r in rows:
        d = {c.key: _plain(getattr(r, c.key)) for c in model.__table__.columns
             if c.key not in ("id", "company_id", "employee_id")}
        d["uuid"] = str(r.uuid)
        d["id"] = r.id
        for c in master_cols:
            d[c.replace("_id", "_name")] = labels.get(getattr(r, c))
        if kind == "bank-accounts":
            d["account_number_masked"] = v.mask(r.account_number)
            if not full_bank:
                d["account_number"] = d["account_number_masked"]
            d["account_type_label"] = ACCOUNT_TYPES.get(r.account_type, r.account_type)
        if kind == "nominees":
            d["kind_label"] = NOMINEE_KINDS.get(r.kind, r.kind)
        out.append(d)
    return out


def nominee_totals(db: Session, company_id: int, employee_id: int) -> dict[str, float]:
    rows = (db.query(Nominee.kind, func.sum(Nominee.share_percent))
            .filter(Nominee.company_id == company_id, Nominee.employee_id == employee_id).group_by(Nominee.kind).all())
    return {k: float(t) for k, t in rows}
