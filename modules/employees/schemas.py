import re
from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

ROLES = ("admin", "principal", "hr", "manager", "teacher", "accountant", "librarian", "employee")


def _no_tags(v):
    if v:
        v = re.sub(r"<[^>]+>", "", v).strip()
    return v


class Address(BaseModel):
    line1: Optional[str] = Field(None, max_length=250)
    line2: Optional[str] = Field(None, max_length=250)
    city: Optional[str] = Field(None, max_length=100)
    district: Optional[str] = Field(None, max_length=100)
    state_id: Optional[int] = None
    pincode: Optional[str] = None
    country_id: Optional[int] = None


class _EmployeeFields(BaseModel):
    """Fields shared by create and update (all optional here)."""
    greeting: Optional[str] = Field(None, max_length=20)
    middle_name: Optional[str] = Field(None, max_length=100)
    preferred_name: Optional[str] = Field(None, max_length=100)
    phone: Optional[str] = None
    employee_code: Optional[str] = Field(None, max_length=30)
    designation_id: Optional[int] = None
    department_id: Optional[int] = None
    qualification: Optional[str] = None
    experience: Optional[int] = None
    reporting_manager_id: Optional[int] = None
    secondary_manager_id: Optional[int] = None
    timezone: Optional[str] = None
    custom: Optional[dict] = None
    # personal
    gender_id: Optional[int] = None
    date_of_birth: Optional[date] = None
    marital_status_id: Optional[int] = None
    marriage_date: Optional[date] = None
    blood_group_id: Optional[int] = None
    nationality_id: Optional[int] = None
    father_name: Optional[str] = Field(None, max_length=200)
    spouse_name: Optional[str] = Field(None, max_length=200)
    # contact
    personal_email: Optional[str] = Field(None, max_length=255)
    alternate_phone: Optional[str] = None
    work_phone: Optional[str] = None
    extension: Optional[str] = Field(None, max_length=10)
    current_address: Optional[Address] = None
    permanent_address: Optional[Address] = None
    # statutory
    pan: Optional[str] = None
    aadhaar: Optional[str] = None
    uan: Optional[str] = None
    esi_number: Optional[str] = None
    passport_number: Optional[str] = None
    passport_expiry: Optional[date] = None
    driving_licence: Optional[str] = None
    driving_licence_expiry: Optional[date] = None
    # job
    legal_entity_id: Optional[int] = None
    location_id: Optional[int] = None
    grade_id: Optional[int] = None
    cost_centre_id: Optional[int] = None
    employment_type_id: Optional[int] = None
    employee_status_id: Optional[int] = None
    date_of_joining: Optional[date] = None
    probation_end_date: Optional[date] = None
    confirmation_date: Optional[date] = None
    notice_period_days: Optional[int] = None
    work_mode: Optional[str] = None
    team_ids: Optional[list[int]] = None
    tag_ids: Optional[list[int]] = None

    @field_validator("father_name", "spouse_name", "preferred_name", "middle_name")
    @classmethod
    def _plain_text(cls, v):
        return _no_tags(v)


class EmployeeCreate(_EmployeeFields):
    first_name: str
    last_name: str
    email: EmailStr
    password: Optional[str] = None
    role: str = "employee"
    status: Optional[str] = None

    @field_validator("first_name", "last_name")
    @classmethod
    def sanitize_name(cls, v):
        return _no_tags(v)

    @field_validator("role")
    @classmethod
    def known_role(cls, v):
        return _check_role(v)


def _check_role(v):
    if v is not None and v not in ROLES:
        raise ValueError("Role must be admin, hr, manager or employee")
    return v


class EmployeeUpdate(_EmployeeFields):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[EmailStr] = None
    role: Optional[str] = None
    active: Optional[bool] = None

    @field_validator("role")
    @classmethod
    def known_role(cls, v):
        return _check_role(v)


class WorkArrangementUpdate(BaseModel):
    model_config = ConfigDict(extra="allow")

    config: dict


class EmployeeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    uuid: UUID
    employee_code: Optional[str] = None
    greeting: Optional[str] = None
    first_name: str
    last_name: str
    preferred_name: Optional[str] = None
    email: str
    phone: Optional[str] = None
    role: str
    active: bool
    status: str = "confirmed"
    status_label: Optional[str] = None
    designation_id: Optional[int] = None
    designation_name: Optional[str] = None
    department_id: Optional[int] = None
    department_name: Optional[str] = None
    location_id: Optional[int] = None
    location_name: Optional[str] = None
    legal_entity_id: Optional[int] = None
    legal_entity_name: Optional[str] = None
    grade_id: Optional[int] = None
    grade_name: Optional[str] = None
    employment_type_id: Optional[int] = None
    employment_type_name: Optional[str] = None
    date_of_joining: Optional[date] = None
    reporting_manager_id: Optional[int] = None
    reporting_manager_uuid: Optional[UUID] = None
    reporting_manager_name: Optional[str] = None
    work_mode: Optional[str] = None
    timezone: Optional[str] = None
    custom: dict = {}
    tags: list[dict] = []
    profile_image_url: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class EmployeeDetailResponse(EmployeeResponse):
    config: Optional[dict] = None


# ── Lifecycle and job changes ──

class StatusChange(BaseModel):
    status: str
    effective_date: Optional[date] = None
    reason: Optional[str] = Field(None, max_length=300)
    resignation_date: Optional[date] = None
    exit_date: Optional[date] = None
    confirmation_date: Optional[date] = None
    probation_end_date: Optional[date] = None


class RehireIn(BaseModel):
    date_of_joining: date
    status: str = "probation"
    probation_end_date: Optional[date] = None
    reason: Optional[str] = Field(None, max_length=300)
    designation_id: Optional[int] = None
    department_id: Optional[int] = None
    location_id: Optional[int] = None
    legal_entity_id: Optional[int] = None
    grade_id: Optional[int] = None
    reporting_manager_id: Optional[int] = None


class JobChangeIn(BaseModel):
    change_type: str
    effective_date: date
    reason: Optional[str] = Field(None, max_length=300)
    remarks: Optional[str] = Field(None, max_length=2000)
    changes: dict


class BulkManager(BaseModel):
    employee_uuids: list[str] = Field(..., min_length=1, max_length=500)
    manager_id: Optional[int] = None
    effective_date: Optional[date] = None
    reason: Optional[str] = Field(None, max_length=300)


class DuplicateCheck(BaseModel):
    email: Optional[str] = None
    phone: Optional[str] = None
    pan: Optional[str] = None
    aadhaar: Optional[str] = None
    uan: Optional[str] = None
    exclude_uuid: Optional[UUID] = None


class ArchiveIn(BaseModel):
    reason: Optional[str] = Field(None, max_length=300)


class SavedViewIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    filters: dict = Field(default_factory=dict)
    columns: list[str] = Field(default_factory=list)
    shared: bool = False


# ── Detail rows ──

class EmergencyContactIn(BaseModel):
    name: Optional[str] = Field(None, max_length=200)
    relation_id: Optional[int] = None
    phone: Optional[str] = None
    alternate_phone: Optional[str] = None
    email: Optional[str] = Field(None, max_length=255)
    address: Optional[str] = Field(None, max_length=1000)
    is_primary: Optional[bool] = None


class FamilyMemberIn(BaseModel):
    name: Optional[str] = Field(None, max_length=200)
    relation_id: Optional[int] = None
    gender_id: Optional[int] = None
    date_of_birth: Optional[date] = None
    occupation: Optional[str] = Field(None, max_length=120)
    phone: Optional[str] = None
    is_dependent: Optional[bool] = None
    lives_with_employee: Optional[bool] = None


class NomineeIn(BaseModel):
    kind: Optional[str] = None
    family_member_id: Optional[int] = None
    name: Optional[str] = Field(None, max_length=200)
    relation_id: Optional[int] = None
    date_of_birth: Optional[date] = None
    share_percent: Optional[Decimal] = Field(None, gt=0, le=100)
    guardian_name: Optional[str] = Field(None, max_length=200)
    address: Optional[str] = Field(None, max_length=1000)


class BankAccountIn(BaseModel):
    bank_id: Optional[int] = None
    account_holder: Optional[str] = Field(None, max_length=200)
    account_number: Optional[str] = None
    ifsc: Optional[str] = None
    branch: Optional[str] = Field(None, max_length=150)
    account_type: Optional[str] = None
    is_primary: Optional[bool] = None
    verified: Optional[bool] = None
    active: Optional[bool] = None


class EducationIn(BaseModel):
    level_id: Optional[int] = None
    degree: Optional[str] = Field(None, max_length=150)
    specialisation: Optional[str] = Field(None, max_length=150)
    institution: Optional[str] = Field(None, max_length=200)
    university: Optional[str] = Field(None, max_length=200)
    start_year: Optional[int] = Field(None, ge=1950, le=2100)
    end_year: Optional[int] = Field(None, ge=1950, le=2100)
    score: Optional[str] = Field(None, max_length=30)


class PreviousEmploymentIn(BaseModel):
    company_name: Optional[str] = Field(None, max_length=200)
    designation: Optional[str] = Field(None, max_length=150)
    from_date: Optional[date] = None
    to_date: Optional[date] = None
    last_ctc: Optional[Decimal] = Field(None, ge=0)
    reason_for_leaving: Optional[str] = Field(None, max_length=300)
    is_relevant: Optional[bool] = None
    financial_year: Optional[str] = None
    fy_taxable_income: Optional[Decimal] = Field(None, ge=0)
    fy_tds: Optional[Decimal] = Field(None, ge=0)
    fy_pf: Optional[Decimal] = Field(None, ge=0)
    fy_pt: Optional[Decimal] = Field(None, ge=0)
    previous_pf_member_id: Optional[str] = Field(None, max_length=30)


class DocumentIn(BaseModel):
    document_type_id: Optional[int] = None
    title: Optional[str] = Field(None, max_length=200)
    document_number: Optional[str] = Field(None, max_length=60)
    issue_date: Optional[date] = None
    expiry_date: Optional[date] = None
    issued_by: Optional[str] = Field(None, max_length=150)
    remind_days_before: Optional[int] = Field(None, ge=0, le=365)
    visible_to_employee: Optional[bool] = None
    remarks: Optional[str] = Field(None, max_length=1000)


class DocumentVerify(BaseModel):
    status: str  # verified | rejected | pending
    remarks: Optional[str] = Field(None, max_length=1000)


class SelfServiceUpdate(BaseModel):
    """Details an employee may change directly (recorded in the audit log)."""
    preferred_name: Optional[str] = Field(None, max_length=100)
    personal_email: Optional[str] = Field(None, max_length=255)
    alternate_phone: Optional[str] = None
    blood_group_id: Optional[int] = None
    current_address: Optional[Address] = None
    permanent_address: Optional[Address] = None
