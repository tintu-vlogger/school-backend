from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class CompanyProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    uuid: UUID
    company_name: str
    slug: Optional[str] = None
    email: str
    phone: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    pincode: Optional[str] = None
    country: Optional[str] = None
    timezone: str
    currency: str
    logo_url: Optional[str] = None
    industry: Optional[str] = None
    employee_count_band: Optional[str] = None
    pan: Optional[str] = None
    tan: Optional[str] = None
    gstin: Optional[str] = None
    cin: Optional[str] = None
    subscription_plan: str
    active: bool
    config: Optional[dict] = None
    created_at: datetime


class CompanyProfileUpdate(BaseModel):
    company_name: Optional[str] = None
    slug: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    pincode: Optional[str] = None
    country: Optional[str] = None
    timezone: Optional[str] = None
    currency: Optional[str] = None
    industry: Optional[str] = None
    employee_count_band: Optional[str] = None
    pan: Optional[str] = None
    tan: Optional[str] = None
    gstin: Optional[str] = None
    cin: Optional[str] = None
    config: Optional[dict] = None


class OnboardingRequest(BaseModel):
    """First-time company setup: name and web address are required, the rest optional."""
    name: Optional[str] = None
    slug: Optional[str] = None
    industry: Optional[str] = None
    employee_count_band: Optional[str] = None
    description: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    address_line_2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    pincode: Optional[str] = None
    country: Optional[str] = None
    timezone: Optional[str] = None
    currency: Optional[str] = None
    config: Optional[dict] = None
