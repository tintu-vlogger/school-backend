import re
from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator


class DesignationCreate(BaseModel):
    designation_name: str
    code: Optional[str] = None
    description: Optional[str] = None
    grade_id: Optional[int] = None
    custom: Optional[dict] = None

    @field_validator("designation_name")
    @classmethod
    def sanitize_name(cls, v):
        if v:
            v = re.sub(r"<[^>]+>", "", v).strip()
        return v


class DesignationUpdate(BaseModel):
    designation_name: Optional[str] = None
    code: Optional[str] = None
    description: Optional[str] = None
    grade_id: Optional[int] = None
    custom: Optional[dict] = None


class DesignationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    uuid: UUID
    designation_name: str
    code: Optional[str] = None
    description: Optional[str] = None
    grade_id: Optional[int] = None
    grade_name: Optional[str] = None
    employee_count: int = 0
    created_at: Optional[datetime] = None
    custom: dict = {}


class SuggestedDesignation(BaseModel):
    name: str
    category: str
