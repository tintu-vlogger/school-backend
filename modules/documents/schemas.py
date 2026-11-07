from datetime import date
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class Audience(BaseModel):
    all: bool = True
    department_ids: list[int] = []
    roles: list[str] = []
    employee_ids: list[int] = []


class FolderIn(BaseModel):
    name: str = Field(..., max_length=150)
    area: str = "company"
    department_id: int | None = None
    description: str | None = Field(None, max_length=300)


class FileIn(BaseModel):
    folder_uuid: UUID
    upload_uuid: UUID
    title: str | None = Field(None, max_length=250)
    description: str | None = Field(None, max_length=500)
    employee_id: int | None = None
    audience: Audience | None = None
    visible_to_employee: bool = True
    allow_download: bool = True
    expires_on: date | None = None
    tags: list[str] = []


class FileUpdate(BaseModel):
    title: str | None = Field(None, max_length=250)
    description: str | None = Field(None, max_length=500)
    audience: Audience | None = None
    visible_to_employee: bool | None = None
    allow_download: bool | None = None
    expires_on: date | None = None
    tags: list[str] | None = None
    folder_uuid: UUID | None = None


class VersionIn(BaseModel):
    upload_uuid: UUID
    note: str | None = Field(None, max_length=300)


class RestoreIn(BaseModel):
    version: int


class PolicyIn(BaseModel):
    title: str = Field(..., max_length=200)
    category_id: int | None = None
    description: str | None = None
    audience: Audience | None = None
    requires_ack: bool = True
    ack_due_days: int | None = Field(None, ge=1, le=365)
    reminder_every_days: int | None = Field(None, ge=1, le=30)
    is_posh: bool = False
    review_on: date | None = None
    owner_name: str | None = Field(None, max_length=200)
    active: bool = True


class PolicyVersionIn(BaseModel):
    upload_uuid: UUID | None = None
    body_html: str | None = None
    effective_from: date | None = None
    change_note: str | None = Field(None, max_length=300)
    ask_again: bool = True


class LetterheadIn(BaseModel):
    name: str = Field(..., max_length=120)
    kind: str = "header"
    html: str = ""
    height_mm: int | None = Field(None, ge=5, le=120)
    is_default: bool | None = None
    active: bool | None = None


class TemplateIn(BaseModel):
    name: str = Field(..., max_length=150)
    letter_type: str
    description: str | None = Field(None, max_length=300)
    subject: str | None = Field(None, max_length=250)
    body_html: str | None = None
    header_id: int | None = None
    footer_id: int | None = None
    page_size: str | None = None
    orientation: str | None = None
    margin_top: int | None = None
    margin_right: int | None = None
    margin_bottom: int | None = None
    margin_left: int | None = None
    font_family: str | None = None
    font_size: int | None = None
    extra_fields: list[dict] | None = None
    self_service: bool | None = None
    esign_required: bool | None = None
    signers: list[dict] | None = None
    signing_order: bool | None = None
    is_default: bool | None = None
    active: bool | None = None


class PreviewIn(BaseModel):
    template_uuid: UUID | None = None
    template: TemplateIn | None = None   # unsaved changes from the designer
    employee_id: int | None = None
    values: dict[str, Any] = {}
    header_id: int | None = None
    footer_id: int | None = None


class GenerateIn(BaseModel):
    template_uuid: UUID
    employee_id: int
    values: dict[str, Any] = {}
    header_id: int | None = None
    footer_id: int | None = None
    use_header: bool = True
    use_footer: bool = True


class BulkIn(BaseModel):
    template_uuid: UUID
    employee_ids: list[int]
    values: dict[str, Any] = {}
    header_id: int | None = None
    footer_id: int | None = None


class SignerIn(BaseModel):
    kind: str  # employee | manager | specific | external
    employee_id: int | None = None
    name: str | None = None
    email: str | None = None
    label: str | None = None


class EsignIn(BaseModel):
    signers: list[SignerIn] = []
    signing_order: bool = True
    expires_days: int = Field(14, ge=1, le=90)
    message: str | None = Field(None, max_length=500)
    provider: str = "in_app_otp"


class SignIn(BaseModel):
    code: str = Field(..., max_length=10)
    signature_image: str | None = None
    typed_name: str | None = Field(None, max_length=200)


class DeclineIn(BaseModel):
    reason: str = Field(..., max_length=500)


class LetterRequestIn(BaseModel):
    template_uuid: UUID
    reason_id: int | None = None
    reason_text: str | None = Field(None, max_length=500)
    purpose: str | None = Field(None, max_length=200)
    details: str | None = Field(None, max_length=1000)
