"""The full employee profile in one response (header facts, every tab's data and the job
history with readable values). Sensitive values are masked unless the viewer may see them."""
from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session, joinedload

from core.masters import labels_for
from modules.employees import details, documents, service, validators as v
from modules.employees.models import Employee, EmployeeDocument, JobChange

ADDRESS_FIELDS = ("current_address", "permanent_address")


def _person(e: Employee | None) -> dict | None:
    if e is None:
        return None
    return {"id": e.id, "uuid": str(e.uuid), "name": f"{e.first_name} {e.last_name}".strip(), "photo": e.profile_image_url,
            "designation": e.designation.designation_name if e.designation else None, "code": e.employee_code,
            "status": e.status}


def _years_between(start: date | None, end: date | None) -> float | None:
    if not start:
        return None
    end = end or service.today()
    return round(max((end - start).days, 0) / 365.25, 1)


def resolve_values(db: Session, company_id: int, changes_list: list[dict]) -> dict[str, dict]:
    """Readable names for ids found in job history values, keyed by field then id."""
    from modules.departments.models import Department
    from modules.designations.models import Designation
    from modules.organization.models import CostCentre, Grade, LegalEntity, Location
    wanted: dict[str, set] = {}
    for ch in changes_list:
        for field, c in (ch or {}).items():
            for side in ("old", "new"):
                val = (c or {}).get(side)
                if isinstance(val, int) and not isinstance(val, bool):
                    wanted.setdefault(field, set()).add(val)
    out: dict[str, dict] = {}
    simple = {"designation_id": (Designation, Designation.designation_name), "department_id": (Department, Department.department_name),
              "location_id": (Location, Location.name), "legal_entity_id": (LegalEntity, LegalEntity.name),
              "grade_id": (Grade, Grade.name), "cost_centre_id": (CostCentre, CostCentre.name)}
    for field, ids in wanted.items():
        if field in simple:
            model, col = simple[field]
            out[field] = dict(db.query(model.id, col).filter(model.id.in_(ids)).all())
        elif field in ("reporting_manager_id", "secondary_manager_id"):
            out[field] = {e.id: f"{e.first_name} {e.last_name}" for e in
                          db.query(Employee).filter(Employee.company_id == company_id, Employee.id.in_(ids))}
        elif field in ("employment_type_id", "employee_status_id"):
            out[field] = labels_for(db, company_id, ids)
    return out


def _readable(field: str, value, names: dict) -> str | None:
    if value is None or value == "":
        return None
    if field in names:
        return names[field].get(value, str(value))
    if field == "status":
        return service.STATUSES.get(value, value)
    if field == "work_mode":
        return service.WORK_MODES.get(value, value)
    if field == "notice_period_days":
        return f"{value} days"
    if field == "active":
        return "Yes" if value else "No"
    return str(value)


FIELD_LABELS = {**service.JOB_FIELDS, "status": "Stage", "date_of_joining": "Joining date", "exit_date": "Last working day",
                "resignation_date": "Resignation date", "confirmation_date": "Confirmation date",
                "probation_end_date": "Probation ends", "active": "Can sign in"}


def history(db: Session, company_id: int, employee_id: int) -> list[dict]:
    rows = (db.query(JobChange).filter(JobChange.company_id == company_id, JobChange.employee_id == employee_id)
            .order_by(JobChange.effective_date.desc(), JobChange.id.desc()).all())
    names = resolve_values(db, company_id, [r.changes for r in rows])
    out = []
    for r in rows:
        items = [{"field": f, "label": FIELD_LABELS.get(f, f.replace("_", " ").capitalize()),
                  "old": _readable(f, (c or {}).get("old"), names), "new": _readable(f, (c or {}).get("new"), names)}
                 for f, c in (r.changes or {}).items()]
        out.append({"uuid": str(r.uuid), "change_type": r.change_type,
                    "change_label": service.CHANGE_TYPES.get(r.change_type, r.change_type),
                    "effective_date": r.effective_date, "status": r.status, "reason": r.reason, "remarks": r.remarks,
                    "items": items, "created_by_name": r.created_by_name, "created_at": r.created_at})
    return out


def _address(addr: dict | None, labels: dict) -> dict:
    addr = dict(addr or {})
    if addr.get("state_id"):
        addr["state_name"] = labels.get(addr["state_id"])
    if addr.get("country_id"):
        addr["country_name"] = labels.get(addr["country_id"])
    return addr


def build(db: Session, company_id: int, emp: Employee, *, sensitive: bool, can_edit: bool, can_verify: bool,
          is_self: bool) -> dict:
    from modules.organization import service as org
    service.apply_due_changes(db, company_id)
    master_ids = [getattr(emp, f) for f in service.MASTER_FIELDS if getattr(emp, f)]
    for f in ADDRESS_FIELDS:
        addr = getattr(emp, f) or {}
        master_ids += [addr.get("state_id"), addr.get("country_id")]
    labels = labels_for(db, company_id, [i for i in master_ids if i])
    manager = emp.reporting_manager
    secondary = (db.query(Employee).options(joinedload(Employee.designation))
                 .filter(Employee.id == emp.secondary_manager_id).first()) if emp.secondary_manager_id else None
    reportees = (db.query(Employee).options(joinedload(Employee.designation))
                 .filter(Employee.company_id == company_id, Employee.reporting_manager_id == emp.id,
                         Employee.status.notin_(("exited", "archived"))).order_by(Employee.first_name).all())
    cost_centre = emp.cost_centre
    doc_rows = (db.query(EmployeeDocument).options(joinedload(EmployeeDocument.file))
                .filter(EmployeeDocument.company_id == company_id, EmployeeDocument.employee_id == emp.id)
                .order_by(EmployeeDocument.archived_at.isnot(None), EmployeeDocument.created_at.desc()).all())
    if is_self and not can_edit:
        doc_rows = [d for d in doc_rows if d.visible_to_employee]
    experience_rows = details.list_rows(db, company_id, emp.id, "experience")
    prev_years = sum(_years_between(r.from_date, r.to_date) or 0 for r in experience_rows if r.is_relevant)
    tenure = _years_between(emp.date_of_joining, emp.exit_date)
    pan = emp.pan if sensitive or is_self else v.mask(emp.pan)

    data = {
        "id": emp.id, "uuid": str(emp.uuid), "employee_code": emp.employee_code, "greeting": emp.greeting,
        "first_name": emp.first_name, "middle_name": emp.middle_name, "last_name": emp.last_name,
        "preferred_name": emp.preferred_name, "full_name": service.display_name(emp), "email": emp.email,
        "phone": emp.phone, "role": emp.role, "active": emp.active, "status": emp.status,
        "status_label": service.STATUSES.get(emp.status, emp.status), "profile_image_url": emp.profile_image_url,
        "timezone": emp.timezone, "custom": emp.custom or {}, "config": emp.settings.config if emp.settings else None,
        # personal
        "gender_id": emp.gender_id, "gender_name": labels.get(emp.gender_id), "date_of_birth": emp.date_of_birth,
        "age": int(_years_between(emp.date_of_birth, None)) if emp.date_of_birth else None,
        "marital_status_id": emp.marital_status_id, "marital_status_name": labels.get(emp.marital_status_id),
        "marriage_date": emp.marriage_date, "blood_group_id": emp.blood_group_id,
        "blood_group_name": labels.get(emp.blood_group_id), "nationality_id": emp.nationality_id,
        "nationality_name": labels.get(emp.nationality_id), "father_name": emp.father_name, "spouse_name": emp.spouse_name,
        # contact
        "personal_email": emp.personal_email, "alternate_phone": emp.alternate_phone, "work_phone": emp.work_phone,
        "extension": emp.extension, "current_address": _address(emp.current_address, labels),
        "permanent_address": _address(emp.permanent_address, labels),
        # statutory
        "pan": pan, "aadhaar_masked": f"XXXX XXXX {emp.aadhaar_last4}" if emp.aadhaar_last4 else None,
        "has_aadhaar": bool(emp.aadhaar_last4), "uan": emp.uan if sensitive or is_self else v.mask(emp.uan),
        "esi_number": emp.esi_number if sensitive or is_self else v.mask(emp.esi_number),
        "passport_number": emp.passport_number if sensitive or is_self else v.mask(emp.passport_number),
        "passport_expiry": emp.passport_expiry,
        "driving_licence": emp.driving_licence if sensitive or is_self else v.mask(emp.driving_licence),
        "driving_licence_expiry": emp.driving_licence_expiry,
        # job
        "legal_entity_id": emp.legal_entity_id, "legal_entity_name": emp.legal_entity.name if emp.legal_entity else None,
        "location_id": emp.location_id, "location_name": emp.location.name if emp.location else None,
        "location_city": emp.location.city if emp.location else None,
        "department_id": emp.department_id, "department_name": emp.department.department_name if emp.department else None,
        "designation_id": emp.designation_id,
        "designation_name": emp.designation.designation_name if emp.designation else None,
        "grade_id": emp.grade_id, "grade_name": emp.grade.name if emp.grade else None,
        "cost_centre_id": emp.cost_centre_id, "cost_centre_name": cost_centre.name if cost_centre else None,
        "employment_type_id": emp.employment_type_id, "employment_type_name": labels.get(emp.employment_type_id),
        "employee_status_id": emp.employee_status_id, "employee_status_name": labels.get(emp.employee_status_id),
        "date_of_joining": emp.date_of_joining, "probation_end_date": emp.probation_end_date,
        "confirmation_date": emp.confirmation_date, "notice_period_days": emp.notice_period_days,
        "resignation_date": emp.resignation_date, "exit_date": emp.exit_date, "work_mode": emp.work_mode,
        "work_mode_label": service.WORK_MODES.get(emp.work_mode) if emp.work_mode else None,
        "rehire_count": emp.rehire_count, "archived_at": emp.archived_at,
        "reporting_manager_id": emp.reporting_manager_id, "reporting_manager": _person(manager),
        "secondary_manager_id": emp.secondary_manager_id, "secondary_manager": _person(secondary),
        "reportees": [_person(r) for r in reportees],
        "tenure_years": tenure, "previous_experience_years": round(prev_years, 1),
        "total_experience_years": round((tenure or 0) + prev_years, 1),
        "teams": org.teams_of(db, company_id, [emp.id]).get(emp.id, []),
        "tags": org.tags_of(db, company_id, "employees", [emp.id]).get(emp.id, []),
        "skills": org.skills_of(db, company_id, emp.id),
        "documents": documents.out(db, company_id, doc_rows),
        "history": history(db, company_id, emp.id),
        "nominee_totals": details.nominee_totals(db, company_id, emp.id),
        "created_at": emp.created_at, "updated_at": emp.updated_at,
        "permissions": {"can_edit": can_edit, "can_verify": can_verify, "sensitive": sensitive, "is_self": is_self},
    }
    for kind, key in (("emergency-contacts", "emergency_contacts"), ("family", "family"), ("nominees", "nominees"),
                      ("bank-accounts", "bank_accounts"), ("education", "education")):
        data[key] = details.out_rows(db, company_id, kind, details.list_rows(db, company_id, emp.id, kind),
                                     full_bank=sensitive)
    data["experience"] = details.out_rows(db, company_id, "experience", experience_rows)
    bg = ((emp.settings.config or {}).get("background") if emp.settings else None) or {}
    data["qualification"] = bg.get("qualification")
    return data
