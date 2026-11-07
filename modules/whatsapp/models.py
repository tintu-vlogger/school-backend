from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from core.db import Base


class WhatsAppConnection(Base):
    """A company's WhatsApp number, for employee self-service messages."""
    __tablename__ = "whatsapp_connections"

    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    connection_name = Column(String(100), nullable=False)
    instance_name = Column(String(100), nullable=False)
    phone_number = Column(String(20), nullable=True)
    status = Column(String(20), nullable=False)
    config = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    company = relationship("Company", back_populates="whatsapp_connections")
