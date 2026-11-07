import re
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=6)
    full_name: str
    company_name: str
    industry: Optional[str] = Field(None, max_length=100)
    ref_code: Optional[str] = Field(None, max_length=12)
    # Institution details asked for on the sign-up form (all optional for older clients).
    institution_type: Optional[str] = Field(None, max_length=20)
    learners: Optional[str] = Field(None, max_length=20)  # approximate number of learners, e.g. "101-500"
    country: Optional[str] = Field(None, max_length=100)
    timezone: Optional[str] = Field(None, max_length=64)
    phone: Optional[str] = Field(None, max_length=20)
    slug: Optional[str] = Field(None, max_length=60)  # the web address asked for; used when free

    @field_validator("full_name", "company_name")
    @classmethod
    def sanitize_strings(cls, v):
        if v:
            v = re.sub(r"<[^>]+>", "", v).strip()
        return v


class LoginRequest(BaseModel):
    # An email address, or for parents and students also a username such as the admission number.
    email: str = Field(..., min_length=1, max_length=255)
    password: str

    @field_validator("email")
    @classmethod
    def _clean_login(cls, v):
        return v.strip()


class ForgotPasswordRequest(BaseModel):
    email: str = Field(..., min_length=1, max_length=255)


class ResetPasswordRequest(BaseModel):
    email: str = Field(..., min_length=1, max_length=255)
    code: str
    new_password: str = Field(..., min_length=6)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict  # email, role, company_id, company_name, full_name, slug, employee_id


class UserProfile(BaseModel):
    """The signed-in person, as the app needs it on every screen."""
    model_config = ConfigDict(from_attributes=True)

    email: str
    role: str
    company_id: int
    company_name: str
    full_name: str
    slug: Optional[str] = None
    # False until the company has a name and a web address; the app sends the owner to setup.
    setup_complete: bool = True
    picture: Optional[str] = None
    # The signed-in person's employee record (the company owner has one too; is_owner tells them apart).
    employee_id: Optional[int] = None
    employee_uuid: Optional[str] = None
    is_owner: bool = False
    features: Optional[dict] = None
    subscription: Optional[dict] = None
    # Effective timezone (own setting, else the company's) and where it came from.
    timezone: str = "Asia/Kolkata"
    timezone_source: str = "company"  # "own" or "company"
    company_timezone: str = "Asia/Kolkata"
    # Parent / student portal sign-ins.
    portal_id: Optional[int] = None
    student_id: Optional[int] = None
    guardian_id: Optional[int] = None
    must_change_password: bool = False


class TimezoneUpdate(BaseModel):
    timezone: Optional[str] = Field(None, max_length=64)  # empty/None = follow the company
