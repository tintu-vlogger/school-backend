"""Business travel: policy by grade, trip requests with legs and stays, bookings by the
travel desk, the travel advance and the settlement into an expense claim.

travel_policies   versioned travel policy (effective from a date): modes and classes allowed,
                  hotel limits and per diem by city tier (X / Y / Z / international),
                  booking lead time and advance limit, per grade band
travel_requests   a trip: purpose, dates, domestic or international, billable project,
                  advance, policy warnings, approval, settlement claim
travel_legs       one journey of a trip (flight, train, bus, cab, own vehicle) with its booking
travel_stays      one hotel stay of a trip with its booking
"""
from sqlalchemy import (Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text,
                        UniqueConstraint, text)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from core.db import Base


def _uuid():
    return Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))


def _company():
    return Column(Integer, ForeignKey("companies.id"), nullable=False)


def _stamps():
    return (Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
            Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False))


class TravelPolicy(Base):
    __tablename__ = "travel_policies"
    __table_args__ = (
        UniqueConstraint("company_id", "version", name="uq_travel_policies_company_version"),
        Index("ix_travel_policies_company_from", "company_id", "effective_from"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    version = Column(Integer, nullable=False)
    name = Column(String(150), nullable=False, server_default=text("'Travel policy'"))
    effective_from = Column(Date, nullable=False)
    rules = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    change_note = Column(String(300), nullable=True)
    created_by_name = Column(String(200), nullable=True)
    created_at, updated_at = _stamps()


class TravelRequest(Base):
    __tablename__ = "travel_requests"
    __table_args__ = (
        Index("ix_travel_requests_company_employee", "company_id", "employee_id", "status"),
        Index("ix_travel_requests_company_status", "company_id", "status", "start_date"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)  # at submission, for reports
    number = Column(String(40), nullable=True)
    purpose = Column(String(300), nullable=False)
    travel_type = Column(String(20), nullable=False, server_default=text("'domestic'"))  # domestic | international
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    # draft | pending | approved | booked | settling | settled | rejected | cancelled
    status = Column(String(20), nullable=False, server_default=text("'draft'"))
    project_id = Column(Integer, ForeignKey("ts_projects.id"), nullable=True)
    billable = Column(Boolean, nullable=False, server_default=text("false"))
    accommodation_needed = Column(Boolean, nullable=False, server_default=text("true"))
    per_diem_days = Column(Integer, nullable=False, server_default=text("0"))
    per_diem_rate = Column(Numeric(14, 2), nullable=False, server_default=text("0"))
    estimated_total = Column(Numeric(14, 2), nullable=False, server_default=text("0"))
    advance_amount = Column(Numeric(14, 2), nullable=False, server_default=text("0"))
    advance_id = Column(Integer, ForeignKey("expense_advances.id"), nullable=True)
    policy_id = Column(Integer, ForeignKey("travel_policies.id"), nullable=True)
    grade_code = Column(String(30), nullable=True)
    policy_flags = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    outside_policy = Column(Boolean, nullable=False, server_default=text("false"))
    exception_reason = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    workflow_instance_id = Column(Integer, ForeignKey("workflow_instances.id"), nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    decided_note = Column(Text, nullable=True)
    booked_at = Column(DateTime(timezone=True), nullable=True)
    claim_id = Column(Integer, ForeignKey("expense_claims.id"), nullable=True)
    settled_at = Column(DateTime(timezone=True), nullable=True)
    created_at, updated_at = _stamps()

    legs = relationship("TravelLeg", back_populates="request", order_by="TravelLeg.seq", cascade="all, delete-orphan")
    stays = relationship("TravelStay", back_populates="request", order_by="TravelStay.seq", cascade="all, delete-orphan")


class _Booking:
    """Booking columns shared by legs and stays (filled in by the travel desk)."""
    booking_status = Column(String(20), nullable=False, server_default=text("'open'"))  # open | booked | cancelled
    paid_by = Column(String(20), nullable=False, server_default=text("'company'"))      # company | employee
    vendor = Column(String(150), nullable=True)          # airline, train no., hotel name
    booking_ref = Column(String(80), nullable=True)      # PNR / confirmation number
    booked_cost = Column(Numeric(14, 2), nullable=True)
    file_name = Column(String(255), nullable=True)
    file_data = Column(Text, nullable=True)              # ticket or voucher (data URL)
    booked_by_name = Column(String(200), nullable=True)
    booked_on = Column(DateTime(timezone=True), nullable=True)


class TravelLeg(_Booking, Base):
    __tablename__ = "travel_legs"
    __table_args__ = (Index("ix_travel_legs_company_request", "company_id", "request_id"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    request_id = Column(Integer, ForeignKey("travel_requests.id", ondelete="CASCADE"), nullable=False)
    seq = Column(Integer, nullable=False, server_default=text("1"))
    mode = Column(String(20), nullable=False)        # flight | train | bus | cab | own_vehicle
    travel_class = Column(String(30), nullable=True)
    from_place = Column(String(150), nullable=False)
    to_place = Column(String(150), nullable=False)
    depart_date = Column(Date, nullable=False)
    preferred_time = Column(String(40), nullable=True)
    distance_km = Column(Numeric(10, 2), nullable=True)  # own vehicle
    estimated_cost = Column(Numeric(14, 2), nullable=False, server_default=text("0"))
    notes = Column(String(300), nullable=True)
    flags = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    created_at, updated_at = _stamps()

    request = relationship("TravelRequest", back_populates="legs")


class TravelStay(_Booking, Base):
    __tablename__ = "travel_stays"
    __table_args__ = (Index("ix_travel_stays_company_request", "company_id", "request_id"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    request_id = Column(Integer, ForeignKey("travel_requests.id", ondelete="CASCADE"), nullable=False)
    seq = Column(Integer, nullable=False, server_default=text("1"))
    city = Column(String(150), nullable=False)
    tier = Column(String(20), nullable=True)           # X | Y | Z | international
    check_in = Column(Date, nullable=False)
    check_out = Column(Date, nullable=False)
    nightly_cost = Column(Numeric(14, 2), nullable=False, server_default=text("0"))
    notes = Column(String(300), nullable=True)
    flags = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    created_at, updated_at = _stamps()

    request = relationship("TravelRequest", back_populates="stays")

