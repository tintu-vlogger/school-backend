from uuid import UUID

from pydantic import BaseModel, Field


class AgentIn(BaseModel):
    employee_id: int
    location_id: int | None = None


class CategoryIn(BaseModel):
    name: str = Field(..., max_length=120)
    parent_id: int | None = None
    description: str | None = Field(None, max_length=500)
    icon: str | None = Field(None, max_length=40)
    assignment_mode: str | None = None
    owner_employee_id: int | None = None
    escalate_to_employee_id: int | None = None
    require_subcategory: bool | None = None
    feedback_mandatory: bool | None = None
    default_priority: str | None = None
    active: bool | None = None
    sort_order: int | None = None
    agents: list[AgentIn] | None = None


class SlaIn(BaseModel):
    category_id: int | None = None
    priority: str
    response_minutes: int = Field(..., ge=1, le=60 * 24 * 60)
    resolution_minutes: int = Field(..., ge=1, le=60 * 24 * 90)
    escalate_to_employee_id: int | None = None
    raise_priority: bool | None = True
    active: bool | None = True


class CaseIn(BaseModel):
    category_id: int
    subcategory_id: int | None = None
    case_type_id: int | None = None
    location_id: int | None = None
    subject: str = Field(..., max_length=250)
    description: str = Field(..., max_length=20000)
    priority: str | None = None
    source: str | None = None
    requester_employee_id: int | None = None
    attachments: list[UUID] = []
    custom: dict | None = None


class CaseUpdate(BaseModel):
    priority: str | None = None
    category_id: int | None = None
    subcategory_id: int | None = None
    case_type_id: int | None = None
    custom: dict | None = None


class MessageIn(BaseModel):
    kind: str = "reply"
    body: str = Field("", max_length=20000)
    attachments: list[UUID] = []


class StatusIn(BaseModel):
    status: str
    note: str | None = Field(None, max_length=20000)


class AssignIn(BaseModel):
    employee_id: int | None = None


class RateIn(BaseModel):
    rating: int = Field(..., ge=1, le=5)
    comment: str | None = Field(None, max_length=1000)


class ReopenIn(BaseModel):
    reason: str | None = Field(None, max_length=2000)


class BulkIn(BaseModel):
    uuids: list[UUID] = Field(..., max_length=100)
    action: str  # pick | assign | status
    employee_id: int | None = None
    status: str | None = None
    note: str | None = None


class CannedIn(BaseModel):
    title: str = Field(..., max_length=150)
    body: str
    category_id: int | None = None
    active: bool | None = True


class ArticleIn(BaseModel):
    title: str = Field(..., max_length=250)
    body: str
    category_id: int | None = None
    tags: list[str] = []
    published: bool | None = True


class HelpfulIn(BaseModel):
    helpful: bool
