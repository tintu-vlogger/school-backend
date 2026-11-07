from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class NotificationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    uuid: UUID
    channel: str
    message_type: str
    message_content: str
    status: str
    sent_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    employee_id: Optional[int] = None
    title: Optional[str] = None
    link: Optional[str] = None
    actor_name: Optional[str] = None
    read_at: Optional[datetime] = None
