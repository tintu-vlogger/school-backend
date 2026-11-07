from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from core import custom_fields
from core.db import get_db
from core.security import require_permission
from modules.departments.models import Department
from modules.employees.models import Employee
from modules.departments.schemas import (
    DepartmentCreate,
    DepartmentResponse,
    DepartmentUpdate,
)

router = APIRouter(prefix="/departments", tags=["Departments"])


def _active_counts(db: Session, company_id: int, ids: list[int]) -> dict[int, int]:
    """Active employees per department, in one query."""
    if not ids:
        return {}
    return dict(
        db.query(Employee.department_id, func.count(Employee.id))
        .filter(Employee.company_id == company_id, Employee.active.is_(True), Employee.department_id.in_(ids))
        .group_by(Employee.department_id).all()
    )


def _check_links(db: Session, company_id: int, dept: Department | None, data: dict) -> None:
    """Parent (no loops), cost centre and location must be this company's."""
    from core.errors import BadRequest
    from modules.organization import service as org
    from modules.organization.models import CostCentre, Location
    if data.get("parent_id"):
        parent = db.query(Department).filter(Department.id == data["parent_id"],
                                             or_(Department.company_id == company_id, Department.is_global.is_(True))).first()
        if parent is None:
            raise BadRequest("The parent department was not found.")
        cursor = parent
        while cursor is not None:
            if dept is not None and cursor.id == dept.id:
                raise BadRequest("A department can't sit under itself or one of its sub-departments.")
            cursor = cursor.parent
    if data.get("cost_centre_id") and not org._by_id(db, CostCentre, company_id, data["cost_centre_id"]):
        raise BadRequest("The cost centre was not found.")
    if data.get("location_id") and not org._by_id(db, Location, company_id, data["location_id"]):
        raise BadRequest("The location was not found.")


def _children(db: Session, ids: list[int]) -> dict[int, int]:
    if not ids:
        return {}
    return dict(db.query(Department.parent_id, func.count(Department.id))
                .filter(Department.parent_id.in_(ids), Department.active.is_(True)).group_by(Department.parent_id).all())


def _department_to_response(dept: Department, db: Session, company_id: int, counts: dict | None = None,
                            children: dict | None = None) -> DepartmentResponse:
    head_employee_name = None
    if dept.head_employee:
        head_employee_name = f"{dept.head_employee.first_name} {dept.head_employee.last_name}"
    if counts is None:
        counts = _active_counts(db, company_id, [dept.id])

    return DepartmentResponse(
        parent_id=dept.parent_id,
        parent_name=dept.parent.department_name if dept.parent else None,
        cost_centre_id=dept.cost_centre_id,
        cost_centre_name=dept.cost_centre.name if dept.cost_centre else None,
        location_id=dept.location_id,
        location_name=dept.location.name if dept.location else None,
        child_count=(children or {}).get(dept.id, 0),
        id=dept.id,
        uuid=dept.uuid,
        department_name=dept.department_name,
        department_code=dept.department_code,
        description=dept.description,
        head_employee_id=dept.head_employee_id,
        head_employee_name=head_employee_name,
        active=dept.active,
        employee_count=counts.get(dept.id, 0),
        created_at=dept.created_at,
        custom=dept.custom or {},
    )


@router.get("/", response_model=list[DepartmentResponse])
def list_departments(
    active: bool | None = Query(None),
    search: str | None = Query(None),
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("people.departments.view")),
):
    company_id = current_user["company_id"]
    query = db.query(Department).options(joinedload(Department.head_employee), joinedload(Department.parent),
                                         joinedload(Department.cost_centre), joinedload(Department.location)).filter(
        or_(
            Department.is_global.is_(True),
            Department.company_id == company_id,
        )
    )

    if active is not None:
        query = query.filter(Department.active == active)

    if search:
        search_term = f"%{search}%"
        query = query.filter(
            or_(
                Department.department_name.ilike(search_term),
                Department.department_code.ilike(search_term),
            )
        )

    departments = query.order_by(Department.is_global.desc(), Department.department_name).all()
    counts = _active_counts(db, company_id, [d.id for d in departments])
    children = _children(db, [d.id for d in departments])
    return [_department_to_response(d, db, company_id, counts, children) for d in departments]


@router.post("/", response_model=DepartmentResponse, status_code=status.HTTP_201_CREATED)
def create_department(
    payload: DepartmentCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("people.departments.manage")),
):
    company_id = current_user["company_id"]
    name = payload.department_name.strip()

    # Names must be unique among shared templates and this company's departments
    existing = (
        db.query(Department)
        .filter(
            or_(
                Department.is_global.is_(True),
                Department.company_id == company_id,
            ),
            Department.department_name.ilike(name),
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Department with this name already exists",
        )

    _check_links(db, company_id, None, payload.model_dump())
    department = Department(
        company_id=company_id,
        department_name=name,
        department_code=payload.department_code.upper().strip() if payload.department_code else None,
        description=payload.description,
        head_employee_id=payload.head_employee_id,
        parent_id=payload.parent_id,
        cost_centre_id=payload.cost_centre_id,
        location_id=payload.location_id,
        is_global=False,
        custom=custom_fields.merge_values(db, company_id, "department", payload.custom),
    )
    db.add(department)
    db.commit()
    db.refresh(department)
    return _department_to_response(department, db, company_id, children=_children(db, [department.id]))


@router.get("/{uuid}", response_model=DepartmentResponse)
def get_department(
    uuid: UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("people.departments.view")),
):
    company_id = current_user["company_id"]
    department = (
        db.query(Department)
        .filter(
            Department.uuid == uuid,
            or_(
                Department.is_global.is_(True),
                Department.company_id == company_id,
            ),
        )
        .first()
    )
    if not department:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")

    return _department_to_response(department, db, company_id)


@router.put("/{uuid}", response_model=DepartmentResponse)
def update_department(
    uuid: UUID,
    payload: DepartmentUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("people.departments.manage")),
):
    company_id = current_user["company_id"]
    department = db.query(Department).filter(Department.uuid == uuid).first()

    if not department:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")

    if department.is_global:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot edit default departments")

    if department.company_id != company_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")

    if payload.department_name is not None:
        name = payload.department_name.strip()
        duplicate = (
            db.query(Department)
            .filter(
                or_(
                    Department.is_global.is_(True),
                    Department.company_id == company_id,
                ),
                Department.department_name.ilike(name),
                Department.id != department.id,
            )
            .first()
        )
        if duplicate:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Department with this name already exists",
            )
        department.department_name = name

    if payload.department_code is not None:
        department.department_code = payload.department_code.upper().strip() if payload.department_code else None
    if payload.description is not None:
        department.description = payload.description
    if payload.head_employee_id is not None:
        department.head_employee_id = payload.head_employee_id
    if payload.active is not None:
        department.active = payload.active
    if payload.custom is not None:
        department.custom = custom_fields.merge_values(db, company_id, "department", payload.custom,
                                                       department.custom, partial=True)
    links = payload.model_dump(exclude_unset=True)
    _check_links(db, company_id, department, links)
    for key in ("parent_id", "cost_centre_id", "location_id"):
        if key in links:
            setattr(department, key, links[key])

    db.commit()
    db.refresh(department)
    return _department_to_response(department, db, company_id)


@router.delete("/{uuid}", status_code=status.HTTP_200_OK)
def delete_department(
    uuid: UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("people.departments.manage")),
):
    company_id = current_user["company_id"]
    department = db.query(Department).filter(Department.uuid == uuid).first()

    if not department:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")

    if department.is_global:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot delete default departments")

    if department.company_id != company_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")

    # Soft delete
    department.active = False
    db.commit()
    return {"detail": "Department deactivated successfully"}


@router.get("/{uuid}/employees")
def list_department_employees(
    uuid: UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("people.departments.view")),
):
    company_id = current_user["company_id"]
    department = (
        db.query(Department)
        .filter(
            Department.uuid == uuid,
            or_(
                Department.is_global.is_(True),
                Department.company_id == company_id,
            ),
        )
        .first()
    )
    if not department:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")

    employees = (
        db.query(Employee)
        .filter(
            Employee.department_id == department.id,
            Employee.company_id == company_id,
            Employee.active.is_(True),
        )
        .order_by(Employee.first_name)
        .all()
    )
    return [
        {
            "id": u.id,
            "uuid": str(u.uuid),
            "first_name": u.first_name,
            "last_name": u.last_name,
            "email": u.email,
            "role": u.role,
            "profile_image_url": u.profile_image_url,
        }
        for u in employees
    ]
