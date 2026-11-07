from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class RosterEntryCreate(BaseModel):
    employee_uuid: str
    date: date
    status: str  # working, off, leave, half_day
    timing_override: Optional[list[list[int]]] = None
    note: Optional[str] = None


class RosterEntryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    employee_id: int
    date: date
    status: str
    timing_override: Optional[list[list[int]]] = None
    note: Optional[str] = None
    created_at: Optional[datetime] = None
    employee_name: Optional[str] = None


class RosterBulkResponse(BaseModel):
    entries: list[RosterEntryResponse]
    employees: dict[str, str]  # {employee uuid: name}
