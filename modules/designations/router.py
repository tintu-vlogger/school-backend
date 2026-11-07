from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session, joinedload

from core import custom_fields
from core.db import get_db
from core.security import require_permission
from modules.designations.models import Designation
from modules.employees.models import Employee
from modules.designations.schemas import (
    DesignationCreate,
    DesignationResponse,
    DesignationUpdate,
    SuggestedDesignation,
)

router = APIRouter(prefix="/designations", tags=["Designations"])


def _out(d: Designation, count: int) -> DesignationResponse:
    return DesignationResponse(id=d.id, uuid=d.uuid, designation_name=d.designation_name, code=d.code,
                               description=d.description, grade_id=d.grade_id,
                               grade_name=d.grade.name if d.grade else None, employee_count=count,
                               created_at=d.created_at, custom=d.custom or {})


def _check_grade(db: Session, company_id: int, grade_id) -> None:
    if grade_id:
        from modules.organization import service as org
        org.check_refs(db, company_id, {"grade_id": grade_id})


@router.get("/suggestions", response_model=list[SuggestedDesignation])
def get_suggestions(current_user: dict = Depends(require_permission("people.designations.view"))):
    from modules.designations.suggested import SUGGESTED_DESIGNATIONS
    return [{"name": n, "category": c} for n, c in SUGGESTED_DESIGNATIONS]


@router.get("/", response_model=list[DesignationResponse])
def list_designations(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("people.designations.view")),
):
    company_id = current_user["company_id"]
    # Shared templates plus this company's own designations
    designations = (
        db.query(Designation).options(joinedload(Designation.grade))
        .filter(
            or_(
                Designation.is_global == True,
                Designation.company_id == company_id,
            )
        )
        .order_by(Designation.is_global.desc(), Designation.designation_name)
        .all()
    )
    ids = [d.id for d in designations]
    counts = dict(
        db.query(Employee.designation_id, sa_func.count(Employee.id))
        .filter(Employee.company_id == company_id, Employee.active.is_(True), Employee.designation_id.in_(ids))
        .group_by(Employee.designation_id).all()
    ) if ids else {}
    results = []
    for d in designations:
        results.append(_out(d, counts.get(d.id, 0)))
    return results


@router.post("/", response_model=DesignationResponse, status_code=status.HTTP_201_CREATED)
def create_designation(
    payload: DesignationCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("people.designations.manage")),
):
    company_id = current_user["company_id"]
    name = payload.designation_name.strip()

    # Names must be unique among shared templates and this company's designations
    existing = (
        db.query(Designation)
        .filter(
            or_(
                Designation.is_global == True,
                Designation.company_id == company_id,
            ),
            Designation.designation_name.ilike(name),
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Designation with this name already exists",
        )
    _check_grade(db, company_id, payload.grade_id)
    designation = Designation(
        company_id=company_id,
        designation_name=name,
        code=(payload.code or "").strip().upper() or None,
        description=(payload.description or "").strip() or None,
        grade_id=payload.grade_id,
        is_global=False,
        custom=custom_fields.merge_values(db, company_id, "designation", payload.custom),
    )
    db.add(designation)
    db.commit()
    db.refresh(designation)
    return _out(designation, 0)


@router.put("/{uuid}", response_model=DesignationResponse)
def update_designation(
    uuid: UUID,
    payload: DesignationUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("people.designations.manage")),
):
    company_id = current_user["company_id"]
    designation = (
        db.query(Designation)
        .filter(Designation.uuid == uuid)
        .first()
    )
    if not designation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Designation not found")

    # Can't edit global designations
    if designation.is_global:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot edit default designations")

    if designation.company_id != company_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Designation not found")

    name = (payload.designation_name or designation.designation_name).strip()
    duplicate = (
        db.query(Designation)
        .filter(
            or_(
                Designation.is_global == True,
                Designation.company_id == company_id,
            ),
            Designation.designation_name.ilike(name),
            Designation.id != designation.id,
        )
        .first()
    )
    if duplicate:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Designation with this name already exists",
        )

    designation.designation_name = name
    data = payload.model_dump(exclude_unset=True)
    if "grade_id" in data:
        _check_grade(db, company_id, data["grade_id"])
        designation.grade_id = data["grade_id"]
    if "code" in data:
        designation.code = (data["code"] or "").strip().upper() or None
    if "description" in data:
        designation.description = (data["description"] or "").strip() or None
    if data.get("custom") is not None:
        designation.custom = custom_fields.merge_values(db, company_id, "designation", data["custom"],
                                                        designation.custom, partial=True)
    db.commit()
    db.refresh(designation)

    employee_count = (
        db.query(Employee)
        .filter(Employee.designation_id == designation.id, Employee.company_id == company_id, Employee.active.is_(True))
        .count()
    )
    return _out(designation, employee_count)


@router.delete("/{uuid}", status_code=status.HTTP_200_OK)
def delete_designation(
    uuid: UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("people.designations.manage")),
):
    company_id = current_user["company_id"]
    designation = (
        db.query(Designation)
        .filter(Designation.uuid == uuid)
        .first()
    )
    if not designation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Designation not found")

    if designation.is_global:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot delete default designations")

    if designation.company_id != company_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Designation not found")

    employee_count = (
        db.query(Employee)
        .filter(Employee.designation_id == designation.id, Employee.company_id == company_id)
        .count()
    )
    if employee_count > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot delete. {employee_count} employee(s) have this designation.",
        )

    db.delete(designation)
    db.commit()
    return {"detail": "Designation deleted successfully"}
