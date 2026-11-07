from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field


class AssetIn(BaseModel):
    asset_tag: str | None = Field(None, max_length=40)
    name: str = Field(..., max_length=200)
    category_id: int | None = None
    brand: str | None = Field(None, max_length=100)
    model: str | None = Field(None, max_length=150)
    serial_number: str | None = Field(None, max_length=120)
    purchase_date: date | None = None
    purchase_value: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=2)
    vendor: str | None = Field(None, max_length=200)
    invoice_no: str | None = Field(None, max_length=80)
    warranty_until: date | None = None
    location_id: int | None = None
    location_note: str | None = Field(None, max_length=200)
    condition: str | None = None
    notes: str | None = None
    custom: dict | None = None


class StatusIn(BaseModel):
    status: str
    note: str | None = Field(None, max_length=300)


class AssignIn(BaseModel):
    employee_id: int
    assigned_on: date | None = None
    expected_return_on: date | None = None
    condition: str | None = None
    notes: str | None = Field(None, max_length=500)
    request_uuid: str | None = None


class ReturnIn(BaseModel):
    returned_on: date | None = None
    condition: str | None = None
    notes: str | None = Field(None, max_length=500)
    recovery_amount: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=2)
    recovery_reason: str | None = Field(None, max_length=300)
    next_status: str | None = None


class RecoveryIn(BaseModel):
    status: str
    note: str | None = Field(None, max_length=300)


class MaintenanceIn(BaseModel):
    kind: str = "repair"
    performed_on: date
    vendor: str | None = Field(None, max_length=200)
    cost: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=2)
    description: str | None = None
    status: str = "done"
    next_due_on: date | None = None


class RequestIn(BaseModel):
    category_id: int
    details: str = Field(..., max_length=1000)
    needed_by: date | None = None
