from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class SectionIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: Optional[str] = Field(None, max_length=500)


class SectionUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    description: Optional[str] = Field(None, max_length=500)
    active: Optional[bool] = None
    sort_order: Optional[int] = None


class FieldIn(BaseModel):
    label: str = Field(..., min_length=1, max_length=120)
    key: Optional[str] = Field(None, max_length=60)
    field_type: str
    section_id: Optional[int] = None
    master_type_key: Optional[str] = None
    options: Optional[list[Any]] = None
    required: bool = False
    help_text: Optional[str] = Field(None, max_length=300)
    placeholder: Optional[str] = Field(None, max_length=120)
    validation: Optional[dict] = None
    visible_to_employee: bool = True
    editable_by_employee: bool = False


class FieldUpdate(BaseModel):
    label: Optional[str] = Field(None, min_length=1, max_length=120)
    field_type: Optional[str] = None
    section_id: Optional[int] = None
    master_type_key: Optional[str] = None
    options: Optional[list[Any]] = None
    required: Optional[bool] = None
    help_text: Optional[str] = Field(None, max_length=300)
    placeholder: Optional[str] = Field(None, max_length=120)
    validation: Optional[dict] = None
    visible_to_employee: Optional[bool] = None
    editable_by_employee: Optional[bool] = None
    active: Optional[bool] = None
    sort_order: Optional[int] = None


class FieldOut(BaseModel):
    id: int
    uuid: UUID
    key: str
    label: str
    field_type: str
    section_id: Optional[int] = None
    master_type_key: Optional[str] = None
    options: list[Any] = []
    required: bool
    help_text: Optional[str] = None
    placeholder: Optional[str] = None
    validation: dict = {}
    visible_to_employee: bool
    editable_by_employee: bool
    active: bool
    sort_order: int


class SectionOut(BaseModel):
    id: int
    uuid: UUID
    name: str
    description: Optional[str] = None
    active: bool
    sort_order: int


class Reorder(BaseModel):
    ids: list[int] = Field(..., max_length=500)
