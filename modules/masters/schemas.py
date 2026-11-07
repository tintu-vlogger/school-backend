from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class MasterTypeCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    key: Optional[str] = Field(None, max_length=60)
    description: Optional[str] = Field(None, max_length=500)
    hierarchical: bool = False


class MasterTypeUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    description: Optional[str] = Field(None, max_length=500)
    hierarchical: Optional[bool] = None
    active: Optional[bool] = None
    sort_order: Optional[int] = None


class MasterTypeOut(BaseModel):
    uuid: UUID
    key: str
    name: str
    description: Optional[str] = None
    hierarchical: bool
    is_system: bool
    active: bool
    sort_order: int
    value_count: int = 0


class MasterValueCreate(BaseModel):
    label: str = Field(..., min_length=1, max_length=200)
    code: Optional[str] = Field(None, max_length=60)
    parent_id: Optional[int] = None
    sort_order: Optional[int] = None
    attributes: Optional[dict] = None
    active: bool = True


class MasterValueUpdate(BaseModel):
    label: Optional[str] = Field(None, min_length=1, max_length=200)
    code: Optional[str] = Field(None, max_length=60)
    parent_id: Optional[int] = None
    sort_order: Optional[int] = None
    attributes: Optional[dict] = None
    active: Optional[bool] = None


class MasterValueOut(BaseModel):
    id: int
    uuid: UUID
    code: Optional[str] = None
    label: str
    parent_id: Optional[int] = None
    parent_label: Optional[str] = None
    attributes: dict = {}
    is_system: bool
    active: bool
    sort_order: int
    child_count: int = 0
    updated_at: Optional[datetime] = None


class ReorderRequest(BaseModel):
    ids: list[int] = Field(..., max_length=2000)
