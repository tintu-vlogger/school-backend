from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from core.db import Base


class Company(Base):
    """A customer of the platform (one tenant). The owner signs in with the company's
    email and password; employees sign in with their own accounts."""
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_name = Column(String(255), nullable=False)
    slug = Column(String(100), unique=True, nullable=True)
    email = Column(String(255), unique=True, nullable=False)
    password = Column(String(255), nullable=False)
    password_reset_token = Column(String(255), nullable=True)
    phone = Column(String(20), nullable=True)
    address = Column(Text, nullable=True)
    city = Column(String(100), nullable=True)
    state = Column(String(100), nullable=True)
    pincode = Column(String(10), nullable=True)
    country = Column(String(100), nullable=True)
    timezone = Column(String(50), nullable=False, server_default=text("'Asia/Kolkata'"))
    currency = Column(String(10), nullable=False, server_default=text("'INR'"))
    logo_url = Column(Text, nullable=True)
    industry = Column(String(100), nullable=True)
    employee_count_band = Column(String(20), nullable=True)
    pan = Column(String(10), nullable=True)
    tan = Column(String(10), nullable=True)
    gstin = Column(String(15), nullable=True)
    cin = Column(String(21), nullable=True)
    subscription_plan = Column(String(20), nullable=False, server_default=text("'trial'"))
    trial_ends_at = Column(DateTime(timezone=True), nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    config = Column(JSONB, nullable=True)
    # Platform partner who brought this company in (for commissions).
    referred_by_id = Column(Integer, ForeignKey("platform_users.id", ondelete="SET NULL"), nullable=True, index=True)
    # The owner's own employee record, so the owner can use self-service like everyone else.
    owner_employee_id = Column(Integer, ForeignKey("employees.id", name="fk_companies_owner_employee_id",
                                                   ondelete="SET NULL", use_alter=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    designations = relationship("Designation", back_populates="company")
    departments = relationship("Department", back_populates="company")
    employees = relationship("Employee", back_populates="company", foreign_keys="Employee.company_id")
    whatsapp_connections = relationship("WhatsAppConnection", back_populates="company")
    notifications = relationship("Notification", back_populates="company")
    audit_logs = relationship("AuditLog", back_populates="company")
