"""Request bodies for the performance API. Lists and nested configuration are checked
again in the services (weights, scales, matrices)."""
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _In(BaseModel):
    model_config = ConfigDict(extra="ignore")


class SettingsIn(_In):
    okr: dict | None = None
    nine_box: dict | None = None
    reviews: dict | None = None
    feedback: dict | None = None
    letters: dict | None = None


class ScaleIn(_In):
    name: str = Field(max_length=120)
    description: str | None = None
    points: list[dict]
    is_default: bool = False
    active: bool = True


class KraIn(_In):
    title: str = Field(max_length=200)
    description: str | None = None
    measure: str | None = None
    weight: Decimal | None = None
    tags: dict | None = None
    active: bool = True


class EmployeeKrasIn(_In):
    items: list[dict]


class CompetencyIn(_In):
    name: str = Field(max_length=200)
    category: str | None = None
    description: str | None = None
    rating_scale_id: int | None = None
    weight: Decimal | None = None
    behaviours: list[dict] = []
    skill_ids: list[int] = []
    tags: dict | None = None
    active: bool = True


class TemplateIn(_In):
    name: str = Field(max_length=200)
    description: str | None = None
    rating_scale_id: int | None = None
    sections: list[dict]
    active: bool = True


class PeriodIn(_In):
    name: str = Field(max_length=120)
    kind: str = "quarter"
    start_date: date
    end_date: date
    status: str | None = None


class KeyResultIn(_In):
    uuid: str | None = None
    title: str
    kind: str = "number"
    unit: str | None = None
    start_value: Decimal | None = None
    target_value: Decimal | None = None
    current_value: Decimal | None = None
    milestones: list[dict] = []
    weight: Decimal | None = None
    owner_employee_id: int | None = None
    start_date: date | None = None
    due_date: date | None = None
    checkin_frequency: str | None = None


class ObjectiveIn(_In):
    period_id: int
    level: str
    title: str = Field(max_length=300)
    description: str | None = None
    owner_employee_id: int | None = None
    department_id: int | None = None
    parent_id: int | None = None
    weight: Decimal | None = None
    due_date: date | None = None
    visibility: str | None = None
    key_results: list[KeyResultIn] = []


class ObjectiveUpdate(_In):
    title: str | None = None
    description: str | None = None
    owner_employee_id: int | None = None
    department_id: int | None = None
    parent_id: int | None = None
    weight: Decimal | None = None
    due_date: date | None = None
    visibility: str | None = None
    status_key: str | None = None
    key_results: list[KeyResultIn] | None = None


class CheckinIn(_In):
    key_result_uuid: str | None = None
    value: Any = None
    milestones_done: list[str] = []
    confidence: int | None = None
    status_key: str | None = None
    note: str | None = None
    checkin_date: date | None = None


class CycleIn(_In):
    name: str = Field(max_length=200)
    kind: str = "annual"
    template_id: int
    rating_scale_id: int | None = None
    goal_period_id: int | None = None
    period_start: date
    period_end: date
    participant_rule: dict = {}
    stages: list[dict] = []
    weightage: dict = {}
    settings: dict = {}


class RuleIn(_In):
    participant_rule: dict = {}


class StatusIn(_In):
    status: str


class IdsIn(_In):
    employee_ids: list[int] = []


class UuidsIn(_In):
    uuids: list[str] = []


class FormIn(_In):
    answers: dict
    submit: bool = False


class DecisionIn(_In):
    approve: bool


class CalibrationIn(_In):
    rating: Decimal | None = None
    reason: str | None = None


class AckIn(_In):
    accept: bool
    note: str | None = None


class PotentialIn(_In):
    potential: int | None = None


class ResponseIn(_In):
    response: str
    note: str | None = None


class MeetingIn(_In):
    employee_id: int
    title: str | None = None
    scheduled_at: datetime
    duration_minutes: int = 30
    recurrence: str = "none"
    agenda: list[str] = []


class MeetingUpdate(_In):
    title: str | None = None
    scheduled_at: datetime | None = None
    duration_minutes: int | None = None
    recurrence: str | None = None
    shared_notes: str | None = None
    agenda: list[dict] | None = None
    action_items: list[dict] | None = None
    status: str | None = None


class NoteIn(_In):
    note: str | None = None


class FeedbackIn(_In):
    receiver_employee_id: int
    message: str
    kind: str = "feedback"
    visibility: str = "private"
    competency_id: int | None = None
    also_kudos: bool = False


class FeedbackRequestIn(_In):
    giver_employee_ids: list[int]
    about_employee_id: int | None = None
    prompt: str | None = None
    visibility: str = "private"


class FeedbackAnswerIn(_In):
    message: str | None = None
    decline: bool = False


class PipIn(_In):
    employee_id: int | None = None
    reason: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    goals: list[dict] | None = None
    support: str | None = None
    draft: bool = False


class PipCheckinIn(_In):
    note: str
    rating: str | None = None
    checkin_date: date | None = None
    goal_status: list[dict] = []


class PipCloseIn(_In):
    outcome: str
    note: str | None = None
    new_end_date: date | None = None


class IncrementCycleIn(_In):
    name: str = Field(max_length=200)
    effective_date: date
    review_cycle_id: int | None = None
    guidelines: dict | None = None
    pay_ranges: dict | None = None
    letter_template: str | None = None
    budgets: list[dict] | None = None


class BudgetsIn(_In):
    budgets: list[dict]


class ProposalIn(_In):
    current_ctc: Decimal | None = None
    proposed_pct: Decimal | None = None
    increment_amount: Decimal | None = None
    promote: bool | None = None
    new_designation_id: int | None = None
    justification: str | None = None
