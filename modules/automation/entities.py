"""Record types a rule can watch: how to load them, which fields they have, which person a
record belongs to, and the values a condition or message can use."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Callable
from uuid import UUID

from sqlalchemy.orm import Session


def plain(v):
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, UUID):
        return str(v)
    if isinstance(v, dict):
        return {k: plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [plain(x) for x in v]
    return v


@dataclass
class Entity:
    key: str
    label: str
    model: Callable[[], type]                 # lazy import of the SQLAlchemy model
    fields: dict[str, str]                    # field -> label (watchable, usable in conditions)
    date_fields: dict[str, str] = field(default_factory=dict)
    triggers: tuple = ("created", "updated", "field_changed", "date")
    employee_attr: str | None = None          # attribute holding the person the record is about
    label_of: Callable = lambda r: str(getattr(r, "id", ""))
    active_filter: Callable | None = None     # (query, Model) -> query for date rules


def _employee():
    from modules.employees.models import Employee
    return Employee


def _department():
    from modules.departments.models import Department
    return Department


def _document():
    from modules.employees.models import EmployeeDocument
    return EmployeeDocument


def _case():
    from modules.helpdesk.models import HelpdeskCase
    return HelpdeskCase


def _candidate():
    from modules.recruitment.models import Candidate
    return Candidate


def _instance():
    from core.workflow.models import WorkflowInstance
    return WorkflowInstance


EMPLOYEE_FIELDS = {
    "first_name": "First name", "last_name": "Last name", "email": "Work email", "phone": "Phone",
    "employee_code": "Employee ID", "role": "Access role", "status": "Stage", "department_id": "Department",
    "designation_id": "Designation", "reporting_manager_id": "Reporting manager", "location_id": "Location",
    "legal_entity_id": "Legal entity", "grade_id": "Grade", "cost_centre_id": "Cost centre",
    "employment_type_id": "Employment type", "work_mode": "Work mode", "notice_period_days": "Notice period",
    "date_of_birth": "Date of birth", "date_of_joining": "Joining date", "probation_end_date": "Probation end",
    "confirmation_date": "Confirmation date", "resignation_date": "Resignation date", "exit_date": "Last working day",
    "passport_expiry": "Passport expiry", "driving_licence_expiry": "Driving licence expiry", "active": "Active",
}
EMPLOYEE_DATES = {
    "date_of_birth": "Date of birth", "date_of_joining": "Joining date", "probation_end_date": "Probation end",
    "confirmation_date": "Confirmation date", "exit_date": "Last working day", "passport_expiry": "Passport expiry",
    "driving_licence_expiry": "Driving licence expiry",
}

ENTITIES: dict[str, Entity] = {
    "employee": Entity(
        "employee", "Employee", _employee, EMPLOYEE_FIELDS, EMPLOYEE_DATES, employee_attr="id",
        label_of=lambda r: f"{r.first_name} {r.last_name}".strip(),
        active_filter=lambda q, M: q.filter(M.active.is_(True))),
    "department": Entity(
        "department", "Department", _department,
        {"department_name": "Name", "head_employee_id": "Head", "active": "Active"},
        triggers=("created", "updated", "field_changed"), employee_attr="head_employee_id",
        label_of=lambda r: r.department_name),
    "employee_document": Entity(
        "employee_document", "Employee document", _document,
        {"title": "Title", "document_number": "Number", "issue_date": "Issue date", "expiry_date": "Expiry date",
         "verification_status": "Verification"},
        {"expiry_date": "Expiry date"}, employee_attr="employee_id",
        label_of=lambda r: r.title or "Document",
        active_filter=lambda q, M: q.filter(M.archived_at.is_(None))),
    "helpdesk_case": Entity(
        "helpdesk_case", "Help desk case", _case,
        {"subject": "Subject", "priority": "Priority", "status": "Status", "assignee_employee_id": "Assigned to",
         "category_id": "Category", "resolution_breached": "Resolution late"},
        triggers=("created", "updated", "field_changed"), employee_attr="requester_employee_id",
        label_of=lambda r: f"{r.reference_no or ''} {r.subject}".strip()),
    "candidate": Entity(
        "candidate", "Candidate", _candidate,
        {"first_name": "First name", "last_name": "Last name", "email": "Email", "status": "Status",
         "source": "Source", "talent_pool": "In talent pool", "follow_up_on": "Follow-up date"},
        {"follow_up_on": "Follow-up date"}, employee_attr="owner_employee_id",
        label_of=lambda r: f"{r.first_name} {r.last_name or ''}".strip()),
    "request": Entity(
        "request", "Request (any approval)", _instance,
        {"request_type": "Kind of request", "status": "Status", "title": "Title"},
        triggers=("approved", "rejected"), employee_attr="requester_employee_id",
        label_of=lambda r: f"{r.reference_no or ''} {r.title}".strip()),
}


def available() -> dict[str, Entity]:
    """Entities whose module is installed."""
    from core.db import Base
    out = {}
    for k, e in ENTITIES.items():
        try:
            m = e.model()
        except Exception:
            continue
        if m.__tablename__ in Base.metadata.tables:
            out[k] = e
    return out


def entity_for_model(obj) -> Entity | None:
    name = getattr(obj, "__tablename__", None)
    for e in ENTITIES.values():
        try:
            if e.model().__tablename__ == name and e.key != "request":
                return e
        except Exception:
            continue
    return None


def record_values(obj, ent: Entity) -> dict:
    out = {}
    for k in list(ent.fields) + ["id", "uuid", "company_id"]:
        if hasattr(obj, k):
            out[k] = plain(getattr(obj, k))
    if ent.key == "request":
        out.update({"reference_no": obj.reference_no, "requester_employee_id": obj.requester_employee_id,
                    "requester_name": obj.requester_name, "payload": plain(obj.payload or {})})
    if hasattr(obj, "custom"):
        out["custom"] = plain(getattr(obj, "custom") or {})
    return out


def employee_values(db: Session, company_id: int, employee_id) -> dict:
    """The person a record is about, with names for the ids people care about."""
    if not employee_id:
        return {}
    Employee = _employee()
    emp = db.query(Employee).filter(Employee.company_id == company_id, Employee.id == employee_id).first()
    if emp is None:
        return {}
    vals = record_values(emp, ENTITIES["employee"])
    vals["name"] = f"{emp.first_name} {emp.last_name}".strip()
    if emp.reporting_manager_id:
        mgr = db.query(Employee).filter(Employee.company_id == company_id, Employee.id == emp.reporting_manager_id).first()
        vals["manager_name"] = f"{mgr.first_name} {mgr.last_name}".strip() if mgr else None
    if emp.department_id:
        D = _department()
        d = db.query(D.department_name).filter(D.id == emp.department_id).scalar()
        vals["department_name"] = d
    return vals


def variables(db: Session, ent: Entity, obj, *, trigger: str, changes: dict | None = None) -> dict:
    rec = record_values(obj, ent)
    emp_id = getattr(obj, ent.employee_attr, None) if ent.employee_attr else None
    emp = employee_values(db, obj.company_id, emp_id)
    v = {"record": rec, "employee": emp, "changes": plain(changes or {}), "trigger": trigger}
    v[ent.key] = {**rec, **emp} if ent.key == "employee" else rec
    if ent.key == "request":
        v["request"] = {**rec, **(rec.get("payload") or {})}
    return v


def condition_fields(ent: Entity) -> list[dict]:
    out = [{"name": f"record.{k}", "label": v} for k, v in ent.fields.items()]
    out += [{"name": f"employee.{k}", "label": f"Person: {v}"} for k, v in EMPLOYEE_FIELDS.items()
            if k in ("department_id", "designation_id", "status", "role", "location_id", "grade_id", "date_of_joining",
                     "probation_end_date", "work_mode")]
    if ent.key == "request":
        out.append({"name": "request.request_type", "label": "Kind of request"})
    return out
