"""Request bodies for the organisation API. All fields optional on update (PUT sends only
what changed); `exclude_unset` keeps the difference between "not sent" and "cleared"."""
from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class Registration(BaseModel):
    kind: str
    state_id: Optional[int] = None
    number: str
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None


class EntityBank(BaseModel):
    bank_id: Optional[int] = None
    account_name: Optional[str] = None
    account_number: str
    ifsc: str
    branch: Optional[str] = None
    purpose: Optional[str] = None
    primary: bool = False


class Signatory(BaseModel):
    name: Optional[str] = None
    designation: Optional[str] = None
    pan: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    employee_id: Optional[int] = None


class LegalEntityIn(BaseModel):
    name: Optional[str] = Field(None, max_length=200)
    legal_name: Optional[str] = Field(None, max_length=250)
    code: Optional[str] = Field(None, max_length=20)
    pan: Optional[str] = None
    tan: Optional[str] = None
    cin: Optional[str] = None
    gstin: Optional[str] = None
    pf_code: Optional[str] = None
    esi_code: Optional[str] = None
    lin: Optional[str] = Field(None, max_length=20)
    incorporation_date: Optional[date] = None
    address_line1: Optional[str] = Field(None, max_length=250)
    address_line2: Optional[str] = Field(None, max_length=250)
    city: Optional[str] = Field(None, max_length=100)
    state_id: Optional[int] = None
    pincode: Optional[str] = None
    email: Optional[str] = Field(None, max_length=255)
    phone: Optional[str] = Field(None, max_length=20)
    registrations: Optional[list[Registration]] = None
    bank_accounts: Optional[list[EntityBank]] = None
    signatory: Optional[Signatory] = None
    is_primary: Optional[bool] = None
    active: Optional[bool] = None
    custom: Optional[dict] = None


class RegionIn(BaseModel):
    name: Optional[str] = Field(None, max_length=150)
    code: Optional[str] = Field(None, max_length=20)
    description: Optional[str] = None
    head_employee_id: Optional[int] = None
    active: Optional[bool] = None


class LocationIn(BaseModel):
    name: Optional[str] = Field(None, max_length=150)
    code: Optional[str] = Field(None, max_length=20)
    kind: Optional[str] = None
    region_id: Optional[int] = None
    legal_entity_id: Optional[int] = None
    address_line1: Optional[str] = Field(None, max_length=250)
    address_line2: Optional[str] = Field(None, max_length=250)
    city: Optional[str] = Field(None, max_length=100)
    state_id: Optional[int] = None
    pincode: Optional[str] = None
    timezone: Optional[str] = None
    latitude: Optional[Decimal] = None
    longitude: Optional[Decimal] = None
    radius_m: Optional[int] = None
    holiday_calendar_id: Optional[int] = None
    phone: Optional[str] = Field(None, max_length=20)
    email: Optional[str] = Field(None, max_length=255)
    active: Optional[bool] = None
    custom: Optional[dict] = None


class GradeIn(BaseModel):
    name: Optional[str] = Field(None, max_length=100)
    code: Optional[str] = Field(None, max_length=20)
    band: Optional[str] = Field(None, max_length=60)
    rank: Optional[int] = Field(None, ge=0, le=1000)
    min_ctc: Optional[Decimal] = Field(None, ge=0, decimal_places=2)
    max_ctc: Optional[Decimal] = Field(None, ge=0, decimal_places=2)
    description: Optional[str] = None
    active: Optional[bool] = None


class CostCentreIn(BaseModel):
    name: Optional[str] = Field(None, max_length=150)
    code: Optional[str] = Field(None, max_length=30)
    description: Optional[str] = None
    legal_entity_id: Optional[int] = None
    owner_employee_id: Optional[int] = None
    active: Optional[bool] = None


class TeamIn(BaseModel):
    name: Optional[str] = Field(None, max_length=150)
    code: Optional[str] = Field(None, max_length=20)
    description: Optional[str] = None
    lead_employee_id: Optional[int] = None
    department_id: Optional[int] = None
    color: Optional[str] = Field(None, max_length=20)
    member_ids: Optional[list[int]] = None
    active: Optional[bool] = None


class TagIn(BaseModel):
    name: Optional[str] = Field(None, max_length=100)
    parent_id: Optional[int] = None
    color: Optional[str] = Field(None, max_length=20)
    description: Optional[str] = None
    applies_to: Optional[list[str]] = None
    sort_order: Optional[int] = None
    active: Optional[bool] = None


class TagAssign(BaseModel):
    entity_type: str = "employees"
    entity_ids: list[int] = Field(default_factory=list, max_length=1000)
    entity_uuids: list[str] = Field(default_factory=list, max_length=1000)
    add: list[int] = Field(default_factory=list)
    remove: list[int] = Field(default_factory=list)


class SkillIn(BaseModel):
    name: Optional[str] = Field(None, max_length=120)
    category_id: Optional[int] = None
    description: Optional[str] = None
    active: Optional[bool] = None


class EmployeeSkillIn(BaseModel):
    skill_id: Optional[int] = None
    level: Optional[int] = Field(None, ge=1, le=5)
    years: Optional[Decimal] = None
    verified: Optional[bool] = None
    notes: Optional[str] = Field(None, max_length=1000)
