from datetime import date
from typing import Any, Optional

from pydantic import BaseModel, Field


class StepIn(BaseModel):
    name: Optional[str] = Field(None, max_length=150)
    approver_type: str
    approver_value: Optional[Any] = None
    mode: str = "any"
    condition: Optional[str] = Field(None, max_length=2000)
    sla_hours: Optional[int] = None
    escalate_to: Optional[dict] = None
    timeout_action: Optional[str] = None
    remind_every_hours: Optional[int] = None
    skip_if_requester: bool = True


class DefinitionIn(BaseModel):
    request_type: str
    name: str = Field(..., min_length=1, max_length=150)
    description: Optional[str] = Field(None, max_length=1000)
    applies_when: Optional[str] = Field(None, max_length=2000)
    priority: int = Field(100, ge=0, le=10000)
    steps: list[StepIn]
    active: bool = True
    change_note: Optional[str] = Field(None, max_length=300)


class DefinitionUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=150)
    description: Optional[str] = Field(None, max_length=1000)
    applies_when: Optional[str] = Field(None, max_length=2000)
    priority: Optional[int] = Field(None, ge=0, le=10000)
    steps: Optional[list[StepIn]] = None
    active: Optional[bool] = None
    change_note: Optional[str] = Field(None, max_length=300)


class DecisionIn(BaseModel):
    decision: str  # approve | reject
    comment: Optional[str] = Field(None, max_length=2000)


class BulkDecisionIn(DecisionIn):
    task_uuids: list[str] = Field(..., min_length=1, max_length=200)


class ReassignIn(BaseModel):
    to_employee_id: int
    comment: Optional[str] = Field(None, max_length=2000)


class CancelIn(BaseModel):
    reason: Optional[str] = Field(None, max_length=2000)


class DelegationIn(BaseModel):
    to_employee_id: int
    start_date: date
    end_date: date
    request_type: Optional[str] = None
    note: Optional[str] = Field(None, max_length=300)
    from_employee_id: Optional[int] = None  # admins may set up delegation for someone else


class CommentIn(BaseModel):
    body: Optional[str] = Field(None, max_length=4000)
    attachment_uuids: Optional[list[str]] = Field(None, max_length=10)


class RevokeIn(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2000)


class BulkReassignIn(BaseModel):
    to_employee_id: int
    task_uuids: Optional[list[str]] = Field(None, max_length=200)
    from_employee_id: Optional[int] = None
    comment: Optional[str] = Field(None, max_length=2000)


class EmailActionIn(BaseModel):
    comment: Optional[str] = Field(None, max_length=2000)


class SettingsIn(BaseModel):
    no_approver: Optional[dict] = None
    on_exit: Optional[dict] = None
    fallback: Optional[dict] = None
    nudge_cooldown_hours: Optional[int] = None
    email_actions: Optional[bool] = None
    email_link_hours: Optional[int] = None
    revoke: Optional[dict] = None
