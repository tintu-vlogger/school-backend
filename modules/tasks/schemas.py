from datetime import date
from uuid import UUID

from pydantic import BaseModel, Field


class Recurrence(BaseModel):
    freq: str
    interval: int = Field(1, ge=1, le=12)
    until: date | None = None


class TaskIn(BaseModel):
    title: str = Field(..., max_length=200)
    description: str | None = Field(None, max_length=20000)
    assignee_employee_id: int | None = None
    priority: str = "medium"
    start_date: date | None = None
    due_date: date | None = None
    labels: list[str] = []
    checklist: list[str] = []
    attachments: list[UUID] = []
    recurrence: Recurrence | None = None
    remind_days_before: int = Field(1, ge=0, le=30)


class TaskUpdate(BaseModel):
    title: str | None = Field(None, max_length=200)
    description: str | None = Field(None, max_length=20000)
    assignee_employee_id: int | None = None
    priority: str | None = None
    start_date: date | None = None
    due_date: date | None = None
    labels: list[str] | None = None
    recurrence: Recurrence | None = None
    remind_days_before: int | None = Field(None, ge=0, le=30)


class StatusIn(BaseModel):
    status: str


class MoveIn(BaseModel):
    status: str
    before_uuid: UUID | None = None
    after_uuid: UUID | None = None


class ChecklistIn(BaseModel):
    title: str = Field(..., max_length=300)


class ChecklistUpdate(BaseModel):
    done: bool | None = None
    title: str | None = Field(None, max_length=300)


class CommentIn(BaseModel):
    body: str = Field("", max_length=10000)
    attachments: list[UUID] = []


class BulkIn(BaseModel):
    uuids: list[UUID] = Field(..., max_length=200)
    status: str | None = None
    assignee_employee_id: int | None = None
