"""Request bodies for the hiring API. Fields are optional on update; services validate
the rules and give plain messages."""
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _In(BaseModel):
    model_config = ConfigDict(extra="ignore")


class RequisitionIn(_In):
    title: str | None = None
    department_id: int | None = None
    designation_id: int | None = None
    location_id: int | None = None
    employment_type_id: int | None = None
    openings: int | None = None
    kind: str | None = None
    replacement_for_id: int | None = None
    budget_min: Decimal | None = None
    budget_max: Decimal | None = None
    min_experience: float | None = None
    max_experience: float | None = None
    needed_by: date | None = None
    justification: str | None = None
    hiring_manager_id: int | None = None
    submit: bool = False


class StageIn(_In):
    key: str | None = None
    label: str
    kind: str


class QuestionIn(_In):
    key: str | None = None
    label: str
    type: str = "text"
    required: bool = False
    options: list[str] = []


class TeamIn(_In):
    employee_id: int
    role: str = "interviewer"


class JobIn(_In):
    requisition_uuid: UUID | None = None
    title: str | None = None
    slug: str | None = None
    department_id: int | None = None
    designation_id: int | None = None
    location_id: int | None = None
    location_text: str | None = None
    work_mode: str | None = None
    employment_type_id: int | None = None
    summary: str | None = None
    description: str | None = None
    skills: list[str] | None = None
    nice_skills: list[str] | None = None
    min_experience: float | None = None
    max_experience: float | None = None
    salary_min: Decimal | None = None
    salary_max: Decimal | None = None
    show_salary: bool | None = None
    openings: int | None = None
    closes_on: date | None = None
    hiring_manager_id: int | None = None
    team: list[TeamIn] | None = None
    stages: list[StageIn] | None = None
    questions: list[QuestionIn] | None = None
    screening: dict | None = None
    referral_bonus: Decimal | None = None


class ActionIn(_In):
    action: str


class CandidateIn(_In):
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    current_company: str | None = None
    current_title: str | None = None
    total_experience: float | None = None
    current_ctc: Decimal | None = None
    expected_ctc: Decimal | None = None
    notice_days: int | None = None
    skills: list[str] | None = None
    education: str | None = None
    linkedin_url: str | None = None
    source: str | None = None
    source_detail: str | None = None
    referred_by_employee_id: int | None = None
    tags: list[str] | None = None
    notes: str | None = None
    owner_employee_id: int | None = None
    follow_up_on: date | None = None
    follow_up_note: str | None = None
    talent_pool: bool | None = None
    resume_uuid: UUID | None = None
    job_uuid: UUID | None = None
    custom: dict | None = None
    allow_duplicate: bool = False


class ParseIn(_In):
    resume_uuid: UUID
    job_uuid: UUID | None = None


class MergeIn(_In):
    other_uuid: UUID


class ApplyIn(_In):
    job_uuid: UUID
    answers: dict = {}


class MoveIn(_In):
    stage: str
    before_uuid: UUID | None = None
    after_uuid: UUID | None = None


class RejectIn(_In):
    reason: str | None = None
    notify_candidate: bool = False


class NoteIn(_In):
    text: str


class OverrideIn(_In):
    recommendation: str | None = None
    note: str | None = None


class BgvIn(_In):
    bgv_status: str
    bgv_vendor: str | None = None
    bgv_notes: str | None = None
    consent_received: bool = False


class InterviewIn(_In):
    round_name: str | None = None
    mode: str | None = None
    starts_at: datetime | None = None
    duration_minutes: int | None = None
    panel: list[int] | None = None
    location: str | None = None
    meeting_link: str | None = None
    notes: str | None = None
    kit_uuid: UUID | None = None


class CancelIn(_In):
    reason: str | None = None


class StatusIn(_In):
    status: str


class ScorecardIn(_In):
    ratings: dict = {}
    overall: int | None = None
    recommendation: str | None = None
    strengths: str | None = None
    concerns: str | None = None
    notes: str | None = None


class KitIn(_In):
    name: str
    description: str | None = None
    competencies: list[dict] = []
    questions: list[str] = []
    duration_minutes: int = 45
    active: bool = True


class ReferenceIn(_In):
    referee_name: str
    referee_email: str | None = None
    referee_phone: str | None = None
    relationship: str | None = None
    referee_company: str | None = None
    send: bool = True


class OfferIn(_In):
    designation_id: int | None = None
    department_id: int | None = None
    location_id: int | None = None
    employment_type_id: int | None = None
    reporting_manager_id: int | None = None
    joining_date: date | None = None
    annual_ctc: Decimal | None = None
    joining_bonus: Decimal | None = None
    structure_id: int | None = None
    probation_months: int | None = None
    notice_days: int | None = None
    notes: str | None = None
    submit: bool = False


class BreakupIn(_In):
    annual_ctc: Decimal
    structure_id: int | None = None
    joining_date: date | None = None


class SendIn(_In):
    message: str | None = None


class ReferralIn(_In):
    job_uuid: UUID
    first_name: str
    last_name: str = ""
    email: str | None = None
    phone: str | None = None
    current_company: str | None = None
    current_title: str | None = None
    total_experience: float | None = None
    linkedin_url: str | None = None
    note: str | None = None
    resume_uuid: UUID | None = None


class ReferralStatusIn(_In):
    status: str
    payment_ref: str | None = None


class BoardIn(_In):
    board: str


class AcceptIn(_In):
    typed_name: str


class DeclineIn(_In):
    reason: str | None = None


class ReferenceAnswerIn(_In):
    answers: dict = Field(default_factory=dict)
    decline: bool = False


class BulkIn(_In):
    uuids: list[UUID]
    action: str
    stage: str | None = None
    reason: str | None = None
    tag: str | None = None
