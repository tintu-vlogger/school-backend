from datetime import date
from uuid import UUID

from pydantic import BaseModel, Field


class MemberIn(BaseModel):
    employee_id: int | None = None
    external_name: str | None = Field(None, max_length=200)
    external_email: str | None = Field(None, max_length=255)
    external_org: str | None = Field(None, max_length=200)
    role: str = "member"


class CommitteeIn(BaseModel):
    kind: str
    name: str = Field(..., max_length=150)
    description: str | None = Field(None, max_length=500)
    constituted_on: date | None = None
    valid_until: date | None = None
    active: bool | None = True
    members: list[MemberIn] | None = None


class GrievanceIn(BaseModel):
    kind: str = "grievance"
    category_id: int | None = None
    subject: str = Field(..., max_length=250)
    description: str = Field(..., max_length=20000)
    anonymous: bool = False
    respondent_employee_id: int | None = None
    respondent_name: str | None = Field(None, max_length=200)
    incident_date: date | None = None
    incident_location: str | None = Field(None, max_length=250)
    witnesses: str | None = Field(None, max_length=500)
    attachments: list[UUID] = []


class MessageIn(BaseModel):
    body: str = Field(..., max_length=20000)
    kind: str = "reply"
    attachments: list[UUID] = []


class ActionIn(BaseModel):
    action: str
    on: date | None = None
    reason: str | None = Field(None, max_length=500)
    outcome: str | None = None
    summary: str | None = Field(None, max_length=10000)
    action_taken: str | None = Field(None, max_length=10000)
    interim_relief: str | None = Field(None, max_length=500)
    employee_id: int | None = None


class TrackIn(BaseModel):
    code: str = Field(..., max_length=20)


class TrackMessageIn(BaseModel):
    code: str = Field(..., max_length=20)
    body: str = Field(..., max_length=20000)


class WithdrawIn(BaseModel):
    reason: str | None = Field(None, max_length=1000)


class ProgrammeIn(BaseModel):
    held_on: date
    title: str = Field(..., max_length=200)
    kind: str = "workshop"
    participants: int = Field(0, ge=0, le=100000)
    notes: str | None = Field(None, max_length=500)
