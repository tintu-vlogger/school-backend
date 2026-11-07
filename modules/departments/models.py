from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from core.db import Base


class Department(Base):
    __tablename__ = "departments"
    __table_args__ = (
        UniqueConstraint("company_id", "department_name", name="uq_departments_company_name"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)  # NULL = shared template
    department_name = Column(String(200), nullable=False)
    department_code = Column(String(20), nullable=True)  # e.g. "FIN", "HR"
    description = Column(Text, nullable=True)
    head_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    parent_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    cost_centre_id = Column(Integer, ForeignKey("cost_centres.id"), nullable=True)
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=True)
    config = Column(JSONB, nullable=True)
    custom = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))  # custom field values
    is_global = Column(Boolean, nullable=False, server_default=text("false"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    company = relationship("Company", back_populates="departments")
    head_employee = relationship("Employee", foreign_keys=[head_employee_id])
    parent = relationship("Department", remote_side=[id])
    cost_centre = relationship("CostCentre")
    location = relationship("Location")
    employees = relationship("Employee", back_populates="department", foreign_keys="Employee.department_id")
