import re
from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator


class DepartmentCreate(BaseModel):
    department_name: str
    department_code: Optional[str] = None
    description: Optional[str] = None
    head_employee_id: Optional[int] = None
    parent_id: Optional[int] = None
    cost_centre_id: Optional[int] = None
    location_id: Optional[int] = None
    custom: Optional[dict] = None

    @field_validator("department_name")
    @classmethod
    def sanitize_name(cls, v):
        if v:
            v = re.sub(r"<[^>]+>", "", v).strip()
        return v


class DepartmentUpdate(BaseModel):
    department_name: Optional[str] = None
    department_code: Optional[str] = None
    description: Optional[str] = None
    head_employee_id: Optional[int] = None
    parent_id: Optional[int] = None
    cost_centre_id: Optional[int] = None
    location_id: Optional[int] = None
    active: Optional[bool] = None
    custom: Optional[dict] = None

    @field_validator("department_name")
    @classmethod
    def sanitize_name(cls, v):
        if v:
            v = re.sub(r"<[^>]+>", "", v).strip()
        return v


class DepartmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    uuid: UUID
    department_name: str
    department_code: Optional[str] = None
    description: Optional[str] = None
    head_employee_id: Optional[int] = None
    head_employee_name: Optional[str] = None
    active: bool = True
    employee_count: int = 0
    created_at: Optional[datetime] = None
    parent_id: Optional[int] = None
    parent_name: Optional[str] = None
    cost_centre_id: Optional[int] = None
    cost_centre_name: Optional[str] = None
    location_id: Optional[int] = None
    location_name: Optional[str] = None
    child_count: int = 0
    custom: dict = {}
