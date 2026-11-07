from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from core.db import Base


class Designation(Base):
    __tablename__ = "designations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)  # NULL = shared template
    designation_name = Column(String(100), nullable=False)
    code = Column(String(20), nullable=True)
    description = Column(Text, nullable=True)
    grade_id = Column(Integer, ForeignKey("grades.id"), nullable=True)
    custom = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))  # custom field values
    is_global = Column(Boolean, nullable=False, server_default=text("false"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    company = relationship("Company", back_populates="designations")
    grade = relationship("Grade")
    employees = relationship("Employee", back_populates="designation")
