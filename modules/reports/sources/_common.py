"""Shared pieces for source definitions: the employee joins most sources need, and small
helpers for computed fields."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import aliased

from modules.reports.registry import Field

STATUS_OPTIONS = [("pre_joining", "Pre-joining"), ("probation", "Probation"), ("confirmed", "Confirmed"),
                  ("notice", "On notice"), ("exited", "Exited"), ("archived", "Archived")]
WORK_MODES = [("office", "Office"), ("remote", "Remote"), ("hybrid", "Hybrid"), ("field", "Field")]
MONTHS = [(i, n) for i, n in enumerate(("January", "February", "March", "April", "May", "June", "July", "August",
                                         "September", "October", "November", "December"), start=1)]


def models():
    from modules.departments.models import Department
    from modules.designations.models import Designation
    from modules.employees.models import Employee
    from modules.organization.models import CostCentre, Grade, LegalEntity, Location
    from core.masters import MasterValue
    return Employee, Department, Designation, Location, LegalEntity, Grade, CostCentre, MasterValue


class EmpJoin:
    """Aliases for an employee and their department, designation, location and legal entity,
    joined onto any table that has an employee id column."""

    def __init__(self, name: str = "e"):
        Employee, Department, Designation, Location, LegalEntity, Grade, CostCentre, MasterValue = models()
        self.e = aliased(Employee, name=f"{name}_emp")
        self.d = aliased(Department, name=f"{name}_dept")
        self.ds = aliased(Designation, name=f"{name}_desg")
        self.l = aliased(Location, name=f"{name}_loc")
        self.le = aliased(LegalEntity, name=f"{name}_le")

    def join(self, base, emp_id_col, outer: bool = False):
        from sqlalchemy.orm import join as ojoin
        j = ojoin(base, self.e, self.e.id == emp_id_col, isouter=outer)
        j = j.outerjoin(self.d, self.d.id == self.e.department_id)
        j = j.outerjoin(self.ds, self.ds.id == self.e.designation_id)
        j = j.outerjoin(self.l, self.l.id == self.e.location_id)
        j = j.outerjoin(self.le, self.le.id == self.e.legal_entity_id)
        return j

    def fields(self, default: bool = True) -> list[Field]:
        e = self.e
        return [
            Field("employee_id", "Employee id", "number", expr=e.id, hidden=True),
            Field("employee_code", "Employee ID", "text", expr=e.employee_code, default=default),
            Field("employee_name", "Employee", "text", expr=func.concat_ws(" ", e.first_name, e.last_name),
                  default=default),
            Field("department", "Department", "text", expr=self.d.department_name, default=default),
            Field("designation", "Designation", "text", expr=self.ds.designation_name),
            Field("location", "Location", "text", expr=self.l.name),
            Field("legal_entity", "Legal entity", "text", expr=self.le.name),
            Field("employee_status", "Employee stage", "enum", expr=e.status, options=STATUS_OPTIONS),
        ]


def years_between(a: date | None, b: date | None) -> float | None:
    if not a or not b:
        return None
    return round((b - a).days / 365.25, 1)


def age_on(dob: date | None, on: date) -> int | None:
    if not dob:
        return None
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def next_birthday(dob: date | None, on: date) -> date | None:
    if not dob:
        return None
    for y in (on.year, on.year + 1):
        try:
            d = dob.replace(year=y)
        except ValueError:      # 29 February
            d = date(y, 3, 1)
        if d >= on:
            return d
    return None


def num(v) -> float:
    if v is None or v == "":
        return 0.0
    if isinstance(v, Decimal):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def band(value: float | None, bands: list[tuple[float, str]], last: str) -> str | None:
    if value is None:
        return None
    for limit, label in bands:
        if value < limit:
            return label
    return last
