"""The company owner's own employee record.

The owner signs in with the company's email and password and keeps full admin rights,
but also needs an employee record so self-service (my leave, my attendance, my pay, my
goals ...) works for them like for anyone else. The record is created when the company
signs up, and once for older companies at start-up (idempotent)."""
from sqlalchemy.orm import Session

OWNER_DESIGNATION = "Managing Director"
_LEADERSHIP_DEPARTMENTS = ("leadership", "management", "executive", "directors", "board")


def owner_employee_id(db: Session, company_id: int) -> int | None:
    from modules.company.models import Company
    return db.query(Company.owner_employee_id).filter(Company.id == company_id).scalar()


def _split_name(full: str) -> tuple[str, str]:
    full = " ".join((full or "").split())
    if not full:
        return "Owner", ""
    first, _, last = full.partition(" ")
    return first, last


def _designation(db: Session, company_id: int) -> int:
    from modules.designations.models import Designation
    row = (db.query(Designation).filter(Designation.company_id == company_id,
                                        Designation.designation_name.ilike(OWNER_DESIGNATION)).first())
    if row is None:
        row = Designation(company_id=company_id, designation_name=OWNER_DESIGNATION,
                          description="Leads the company")
        db.add(row)
        db.flush()
    return row.id


def _department(db: Session, company_id: int) -> int | None:
    from modules.departments.models import Department
    rows = (db.query(Department).filter(Department.company_id == company_id, Department.active.is_(True))
            .order_by(Department.id).all())
    for name in _LEADERSHIP_DEPARTMENTS:
        for d in rows:
            if (d.department_name or "").strip().lower() == name:
                return d.id
    return None


def ensure_owner_employee(db: Session, company_id: int):
    """Link (or create) the owner's employee record. Returns the employee, or None when the
    company does not exist. Safe to run any number of times."""
    from modules.company.models import Company
    from modules.employees.models import Employee

    company = db.query(Company).filter(Company.id == company_id).first()
    if company is None:
        return None
    if company.owner_employee_id:
        emp = db.query(Employee).filter(Employee.id == company.owner_employee_id,
                                        Employee.company_id == company_id).first()
        if emp is not None:
            return emp

    email = (company.email or "").strip().lower()
    emp = (db.query(Employee).filter(Employee.company_id == company_id, Employee.email.ilike(email))
           .order_by(Employee.id).first())
    if emp is None:
        from modules.employees import service
        config = company.config if isinstance(company.config, dict) else {}
        first, last = _split_name(config.get("owner_name") or "")
        emp = service.create(db, company_id, {
            "first_name": first, "last_name": last or "Owner", "email": email, "role": "admin",
            "designation_id": _designation(db, company_id), "department_id": _department(db, company_id),
        })
        if not last:
            emp.last_name = ""  # a one-word name stays one word
        # Same password as the owner's sign-in; the owner always signs in as the owner.
        emp.password = company.password
    elif emp.role != "admin":
        emp.role = "admin"
    company.owner_employee_id = emp.id
    db.flush()
    return emp


def sync_owner_password(db: Session, company_id: int, hashed: str) -> None:
    """Keep the owner's employee record on the same password as the owner sign-in."""
    from core.db import set_tenant
    from modules.employees.models import Employee
    set_tenant(db, company_id)
    emp_id = owner_employee_id(db, company_id)
    if emp_id:
        emp = db.query(Employee).filter(Employee.id == emp_id).first()
        if emp is not None:
            emp.password = hashed
