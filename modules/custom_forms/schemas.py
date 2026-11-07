from typing import Any

from pydantic import BaseModel, Field


class ServiceIn(BaseModel):
    name: str | None = Field(None, max_length=50)
    description: str | None = Field(None, max_length=300)
    icon: str | None = None
    nav_section: str | None = None
    active: bool | None = None
    needs_approval: bool | None = None
    allow_drafts: bool | None = None
    allow_cancel: bool | None = None
    submit_access: dict | None = None
    manager_roles: list[str] | None = None
    manager_employee_ids: list[int] | None = None
    statuses: list[dict] | None = None
    notify: dict | None = None
    title_field: str | None = None
    prefix: str | None = Field(None, max_length=30)
    pattern: str | None = Field(None, max_length=120)


class ServiceCreate(ServiceIn):
    name: str = Field(..., min_length=1, max_length=50)
    fields: list[dict] | None = None


class FormIn(BaseModel):
    fields: list[dict]
    change_note: str | None = Field(None, max_length=300)


class CopyIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)


class RecordIn(BaseModel):
    values: dict[str, Any] = {}
    draft: bool = False


class RecordUpdate(BaseModel):
    values: dict[str, Any] = {}


class EvaluateIn(BaseModel):
    values: dict[str, Any] = {}
    record_uuid: str | None = None


class StatusIn(BaseModel):
    status: str
    note: str | None = Field(None, max_length=2000)


class CancelIn(BaseModel):
    reason: str | None = Field(None, max_length=500)


class CommentIn(BaseModel):
    body: str | None = Field(None, max_length=5000)
    attachment_uuids: list[str] | None = None


class ViewIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    filters: dict = {}
    columns: list[str] = []
    shared: bool = False
