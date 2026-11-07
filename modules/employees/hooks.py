"""Approval request types owned by the employees module.

employee.profile_change: an employee asks to change sensitive personal details (name,
date of birth, gender, PAN, Aadhaar, UAN, passport, phone, family names, custom fields)
or to add a new salary bank account. Nothing changes until the request is approved; then
the change is applied to the employee record (and audited like any other change).
Aadhaar is kept encrypted inside the request; only the last 4 digits are shown.
"""
from datetime import date

from sqlalchemy.orm import Session

from core import custom_fields
from core.errors import BadRequest
from core.workflow.registry import RequestType, register

PROFILE_FIELDS = {
    "greeting": "Title",
    "first_name": "First name",
    "middle_name": "Middle name",
    "last_name": "Last name",
    "phone": "Mobile number",
    "timezone": "Timezone",
    "date_of_birth": "Date of birth",
    "gender_id": "Gender",
    "marital_status_id": "Marital status",
    "marriage_date": "Marriage date",
    "nationality_id": "Nationality",
    "father_name": "Father's name",
    "spouse_name": "Spouse's name",
    "pan": "PAN",
    "aadhaar": "Aadhaar",
    "uan": "UAN",
    "esi_number": "ESI number",
    "passport_number": "Passport number",
    "passport_expiry": "Passport expiry",
    "driving_licence": "Driving licence",
    "driving_licence_expiry": "Driving licence expiry",
}
DATE_FIELDS = {"date_of_birth", "marriage_date", "passport_expiry", "driving_licence_expiry"}
MASTER_FIELDS = {"gender_id", "marital_status_id", "nationality_id"}
BANK_KEYS = ("bank_id", "account_holder", "account_number", "ifsc", "branch", "account_type")


def _employee(db: Session, instance):
    from modules.employees.models import Employee
    emp = db.query(Employee).filter(Employee.company_id == instance.company_id,
                                    Employee.id == instance.payload.get("employee_id")).first()
    if emp is None:
        raise BadRequest("Employee not found.")
    return emp


def _as_date(value):
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise BadRequest("Dates should be in the form YYYY-MM-DD.")


def _plain(v):
    return v.isoformat() if isinstance(v, date) else v


def _on_submit(db: Session, instance) -> None:
    from core.workflow.models import WorkflowInstance
    from modules.employees import details, sensitive, service, validators as val

    payload = dict(instance.payload or {})
    changes = dict(payload.get("changes") or {})
    custom = dict(changes.pop("custom", None) or {})
    bank = changes.pop("bank_account", None)
    if not changes and not custom and not bank:
        raise BadRequest("Change at least one detail.")
    unknown = [k for k in changes if k not in PROFILE_FIELDS]
    if unknown:
        raise BadRequest(f"“{unknown[0]}” can't be changed with a request.")
    emp = _employee(db, instance)
    for k in ("first_name", "last_name"):
        if k in changes and not str(changes[k] or "").strip():
            raise BadRequest(f"{PROFILE_FIELDS[k]} can't be empty.")
    if "timezone" in changes and changes["timezone"]:
        from core.timeutil import is_valid_timezone
        if not is_valid_timezone(changes["timezone"]):
            raise BadRequest("That timezone isn't recognised.")
    for k in DATE_FIELDS & set(changes):
        changes[k] = _as_date(changes[k])
    for k in MASTER_FIELDS & set(changes):
        changes[k] = int(changes[k]) if changes[k] not in (None, "") else None
    # format checks (PAN, UAN, phone ...) and duplicates, without applying anything
    checked = service._clean_statutory(dict(changes))
    service._check_masters(db, instance.company_id, checked)
    service._check_dates({"date_of_birth": checked.get("date_of_birth", emp.date_of_birth),
                          "date_of_joining": emp.date_of_joining})
    dup = {k: checked.get(k) for k in ("pan", "aadhaar", "uan") if checked.get(k)}
    if dup:
        service._refuse_duplicates(db, instance.company_id, dup, exclude_id=emp.id)
    changes.update({k: checked[k] for k in checked if k in changes})
    if custom:
        custom = custom_fields.clean_values(db, instance.company_id, "employee", custom, emp.custom, partial=True)
    before = {k: getattr(emp, k) for k in changes if k != "aadhaar"}
    if "aadhaar" in changes:
        before["aadhaar"] = f"XXXX XXXX {emp.aadhaar_last4}" if emp.aadhaar_last4 else None
    before_custom = {k: (emp.custom or {}).get(k) for k in custom}
    changes = {k: (v.strip() if isinstance(v, str) else v) for k, v in changes.items()
               if k == "aadhaar" or before.get(k) != v}
    custom = {k: v for k, v in custom.items() if before_custom.get(k) != v}
    if bank:
        bank = {k: bank.get(k) for k in BANK_KEYS if bank.get(k) not in (None, "")}
        bank = details.clean_bank(db, instance.company_id, emp.id, bank, None)
        bank.setdefault("account_type", "savings")
    if not changes and not custom and not bank:
        raise BadRequest("Nothing is different from the current details.")
    pending = (db.query(WorkflowInstance.id)
               .filter(WorkflowInstance.company_id == instance.company_id, WorkflowInstance.status == "pending",
                       WorkflowInstance.request_type == "employee.profile_change",
                       WorkflowInstance.subject_id == emp.id).first())
    if pending:
        raise BadRequest("A change request is already waiting for approval. Withdraw it first to send a new one.")
    if "aadhaar" in changes:
        number = changes.pop("aadhaar")
        payload["aadhaar_encrypted"] = sensitive.encrypt(number)
        changes["aadhaar"] = f"XXXX XXXX {number[-4:]}"
    payload["changes"] = {**{k: _plain(v) for k, v in changes.items()}, **({"custom": custom} if custom else {}),
                          **({"bank_account": bank} if bank else {})}
    payload["before"] = {**{k: _plain(before.get(k)) for k in changes},
                         **({"custom": {k: before_custom[k] for k in custom}} if custom else {})}
    payload["fields"] = sorted(list(changes) + [f"custom.{k}" for k in custom] + (["bank_account"] if bank else []))
    instance.payload = payload
    instance.subject_type, instance.subject_id = "employees", emp.id


def _on_approved(db: Session, instance) -> None:
    from modules.employees import details, sensitive, service
    emp = _employee(db, instance)
    data = dict(instance.payload.get("changes") or {})
    bank = data.pop("bank_account", None)
    if "aadhaar" in data:
        data.pop("aadhaar")
        number = sensitive.decrypt(instance.payload.get("aadhaar_encrypted"))
        if number:
            data["aadhaar"] = number
    for k in DATE_FIELDS & set(data):
        data[k] = _as_date(data[k])
    service.update(db, instance.company_id, emp, data)
    if bank:
        details.save_row(db, instance.company_id, emp.id, "bank-accounts", {**bank, "is_primary": True})


def _summarize(instance) -> list[str]:
    from modules.employees.validators import mask
    p = instance.payload or {}
    before, changes = p.get("before") or {}, p.get("changes") or {}
    lines = []
    for k, v in changes.items():
        if k == "custom":
            for ck, cv in (v or {}).items():
                lines.append(f"{ck.replace('_', ' ').capitalize()}: {(before.get('custom') or {}).get(ck) or '—'} → {cv or '—'}")
        elif k == "bank_account":
            lines.append(f"New salary account: {mask((v or {}).get('account_number'))} · IFSC {(v or {}).get('ifsc')}")
        elif k in MASTER_FIELDS:
            lines.append(f"{PROFILE_FIELDS.get(k, k)} changed")
        else:
            lines.append(f"{PROFILE_FIELDS.get(k, k)}: {before.get(k) or '—'} → {v or '—'}")
    if p.get("reason"):
        lines.append(f"Reason: {p['reason']}")
    return lines


register(RequestType(
    key="employee.profile_change",
    label="Change of personal details",
    description="An employee asks to change their name, date of birth, IDs, bank account or other sensitive details.",
    on_submit=_on_submit,
    on_approved=_on_approved,
    summarize=_summarize,
    variables={"request.fields": "Fields being changed (list)", "request.reason": "Reason given"},
    default_steps=[{"name": "HR approval", "approver_type": "role", "approver_value": "hr", "mode": "any"}],
))
