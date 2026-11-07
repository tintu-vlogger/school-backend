"""Request bodies for the engagement API. Responses are plain dicts built by the
services (they mix several records into one card)."""
from datetime import date, datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class Audience(BaseModel):
    all: Optional[bool] = None
    match: Optional[str] = None
    rules: Optional[dict[str, list[str]]] = None

    def as_dict(self) -> dict:
        return self.model_dump(exclude_none=True)


class PollIn(BaseModel):
    options: list[str] = Field(..., min_length=2, max_length=8)
    multiple: bool = False
    closes_at: Optional[datetime] = None
    show_voters: bool = True


class PostIn(BaseModel):
    body: Optional[str] = Field(None, max_length=5000)
    attachments: list[UUID] = Field(default_factory=list, max_length=10)
    link_preview: Optional[dict[str, Any]] = None
    audience: Optional[Audience] = None
    poll: Optional[PollIn] = None
    allow_comments: bool = True


class PostUpdate(BaseModel):
    body: Optional[str] = Field(None, max_length=5000)
    attachments: Optional[list[UUID]] = None
    link_preview: Optional[dict[str, Any]] = None
    audience: Optional[Audience] = None
    allow_comments: Optional[bool] = None


class ReactIn(BaseModel):
    emoji: Optional[str] = Field(None, max_length=16)


class CommentIn(BaseModel):
    body: str = Field(..., max_length=2000)
    parent: Optional[UUID] = None


class CommentUpdate(BaseModel):
    body: str = Field(..., max_length=2000)


class VoteIn(BaseModel):
    options: list[UUID] = Field(default_factory=list, max_length=8)


class ReportIn(BaseModel):
    reason: str
    note: Optional[str] = Field(None, max_length=1000)
    comment: Optional[UUID] = None


class PinIn(BaseModel):
    pinned: bool


class HideIn(BaseModel):
    hidden: bool
    reason: Optional[str] = Field(None, max_length=500)


class WishIn(BaseModel):
    message: Optional[str] = Field(None, max_length=2000)
    emoji: Optional[str] = Field("🎉", max_length=16)


class CelebrationPrefs(BaseModel):
    show_birthday: Optional[bool] = None
    show_work_anniversary: Optional[bool] = None
    show_wedding_anniversary: Optional[bool] = None
    celebration_emails: Optional[bool] = None
    wedding_date: Optional[date] = None


class KudosIn(BaseModel):
    recipients: list[UUID] = Field(..., min_length=1, max_length=10)
    value: Optional[UUID] = None
    message: str = Field(..., max_length=1000)


class KudosValueIn(BaseModel):
    name: Optional[str] = Field(None, max_length=80)
    description: Optional[str] = Field(None, max_length=300)
    emoji: Optional[str] = Field(None, max_length=16)
    color: Optional[str] = None
    points: Optional[int] = None
    active: Optional[bool] = None


class ReorderIn(BaseModel):
    uuids: list[UUID]


class AnnouncementIn(BaseModel):
    title: Optional[str] = Field(None, max_length=200)
    kind: Optional[str] = None
    summary: Optional[str] = Field(None, max_length=400)
    body_html: Optional[str] = Field(None, max_length=200_000)
    cover: Optional[dict[str, Any]] = None
    audience: Optional[Audience] = None
    publish_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    requires_ack: Optional[bool] = None
    ack_due_on: Optional[date] = None
    pinned: Optional[bool] = None
    show_on_kiosk: Optional[bool] = None
    allow_comments: Optional[bool] = None
    send_email: Optional[bool] = None
    action: Optional[str] = None  # draft | publish


class QuestionIn(BaseModel):
    uuid: Optional[UUID] = None
    qtype: str
    text: str = Field(..., max_length=500)
    help_text: Optional[str] = Field(None, max_length=300)
    required: bool = True
    options: list[str] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=dict)


class SurveyIn(BaseModel):
    title: Optional[str] = Field(None, max_length=200)
    kind: Optional[str] = None
    description: Optional[str] = Field(None, max_length=4000)
    thank_you: Optional[str] = Field(None, max_length=500)
    cover: Optional[dict[str, Any]] = None
    identity: Optional[str] = None
    min_responses: Optional[int] = None
    audience: Optional[Audience] = None
    mandatory: Optional[bool] = None
    reminder_every_days: Optional[int] = None
    opens_at: Optional[datetime] = None
    duration_days: Optional[int] = None
    recurrence: Optional[str] = None
    recurrence_count: Optional[int] = None
    post_to_buzz: Optional[bool] = None
    questions: Optional[list[QuestionIn]] = None
    action: Optional[str] = None


class SurveyAnswerIn(BaseModel):
    answers: dict[str, Any]
    show_name: bool = False


class ExtendIn(BaseModel):
    days: int = Field(..., ge=1, le=30)


class KioskIn(BaseModel):
    name: Optional[str] = Field(None, max_length=120)
    slides: Optional[list[dict[str, Any]]] = None
    seconds_per_slide: Optional[int] = None
    audience: Optional[Audience] = None
    theme: Optional[str] = None
    welcome_text: Optional[str] = Field(None, max_length=200)
    active: Optional[bool] = None


class AudiencePreviewIn(BaseModel):
    audience: Optional[Audience] = None


class EmailPreviewIn(BaseModel):
    kind: str = "birthday"
    subject: Optional[str] = None
    body: Optional[str] = None


def dump(model: BaseModel) -> dict:
    """Fields the client actually sent, with the audience as a plain dict."""
    data = model.model_dump(exclude_unset=True)
    if "audience" in data and data["audience"] is not None:
        data["audience"] = {k: v for k, v in data["audience"].items() if v is not None}
    if "questions" in data and data["questions"] is not None:
        data["questions"] = [{**q, "uuid": str(q["uuid"]) if q.get("uuid") else None} for q in data["questions"]]
    return data
