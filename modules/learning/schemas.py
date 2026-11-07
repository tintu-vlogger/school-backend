"""Request bodies for the learning API."""
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _In(BaseModel):
    model_config = ConfigDict(extra="ignore")


class SettingsIn(_In):
    certificate_title: str | None = None
    certificate_text: str | None = None
    certificate_signatory: str | None = None
    reminder_days_before_due: int | None = None


class LessonIn(_In):
    uuid: str | None = None
    title: str
    kind: str = "text"
    body: str | None = None
    url: str | None = None
    file_name: str | None = None
    assessment_id: int | None = None
    duration_minutes: int | None = None


class CourseIn(_In):
    title: str = Field(max_length=200)
    summary: str | None = Field(None, max_length=400)
    description: str | None = None
    category_id: int | None = None
    level: str | None = None
    delivery: str | None = None
    cover: str | None = None
    cover_image_url: str | None = None
    duration_minutes: int | None = None
    sequential: bool | None = None
    self_enrol: bool | None = None
    enrol_approval: bool | None = None
    mandatory: bool | None = None
    due_days: int | None = None
    recert_months: int | None = None
    assessment_id: int | None = None
    certificate: bool | None = None
    status: str | None = None
    tags: list[str] | None = None
    skill_ids: list[int] | None = None
    lessons: list[LessonIn] | None = None


class StatusIn(_In):
    status: str


class AssignIn(_In):
    employee_ids: list[int]
    due_date: date | None = None
    mandatory: bool | None = None


class RuleIn(_In):
    name: str
    course_id: int | None = None
    path_id: int | None = None
    criteria: dict = {}
    mandatory: bool = True
    due_days: int | None = None
    active: bool = True


class PathIn(_In):
    title: str
    description: str | None = None
    kind: str = "path"
    items: list[dict]
    sequential: bool = False
    due_days: int | None = None
    status: str | None = None


class BatchIn(_In):
    course_id: int | None = None
    name: str
    trainer_employee_id: int | None = None
    trainer_name: str | None = None
    mode: str = "classroom"
    venue: str | None = None
    meeting_link: str | None = None
    capacity: int = 20
    sessions: list[dict] = []


class SeatIn(_In):
    employee_id: int | None = None


class AttendanceIn(_In):
    session_id: str
    present: list[str] = []


class CompleteBatchIn(_In):
    min_attendance_pct: int = 100


class QuestionIn(_In):
    category: str | None = None
    kind: str
    text: str
    options: list[dict] = []
    answer: Any = None
    accepted_answers: list[str] = []
    marks: Decimal | None = None
    difficulty: str | None = None
    explanation: str | None = None
    active: bool = True


class AssessmentIn(_In):
    title: str
    description: str | None = None
    selection: str = "fixed"
    question_ids: list[int] = []
    random_rule: dict = {}
    time_limit_minutes: int | None = None
    max_attempts: int | None = None
    pass_pct: Decimal | None = None
    negative_marking: str = "none"
    negative_value: Decimal | None = None
    shuffle: bool = True
    show_answers: str = "after_submit"
    final_score: str = "highest"
    status: str | None = None


class AnswersIn(_In):
    answers: dict = {}


class RequestIn(_In):
    title: str
    reason: str | None = None
    link: str | None = None
    provider: str | None = None
    estimated_cost: Decimal | None = None


class FulfilIn(_In):
    course_id: int
