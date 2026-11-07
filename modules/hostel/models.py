"""Hostel: blocks, rooms and beds, allocation, night roll-call, leave and out-passes, visitors, mess.

- HostelBlock       a building for boys, girls or both, with its warden(s)
- HostelRoom        a room on a floor: type, number of beds, fee
- HostelBed         one bed in a room (created from the room's bed count)
- HostelMessPlan    an optional meal plan with its fee
- HostelAllocation  a boarder in a bed from check-in to check-out
- HostelRollCall    night (or morning) roll-call mark per boarder and day
- HostelLeave       leave or an out-pass for a boarder (asked by a parent or the warden)
- HostelVisitor     visitors to boarders
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
    return (Column(DateTime(timezone=True), server_default=func.now()),
            Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now()))


class HostelBlock(Base):
    __tablename__ = "hostel_blocks"
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_hostel_blocks_name"),
                      Index("ix_hostel_blocks_company", "company_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(80), nullable=False)
    code = Column(String(20), nullable=True)
    gender = Column(String(10), nullable=False, server_default=text("'mixed'"))  # boys | girls | mixed
    warden_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    assistant_ids = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))  # more wardens (employee ids)
    floors = Column(Integer, nullable=True)
    phone = Column(String(20), nullable=True)
    address = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()

    warden = relationship("Employee")


class HostelRoom(Base):
    __tablename__ = "hostel_rooms"
    __table_args__ = (UniqueConstraint("block_id", "room_no", name="uq_hostel_rooms_no"),
                      Index("ix_hostel_rooms_company", "company_id", "block_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    block_id = Column(Integer, ForeignKey("hostel_blocks.id"), nullable=False)
    floor = Column(String(20), nullable=True)
    room_no = Column(String(20), nullable=False)
    room_type = Column(String(20), nullable=False, server_default=text("'double'"))  # single | double | triple | dorm
    beds = Column(Integer, nullable=False, server_default=text("2"))
    fee = Column(Numeric(12, 2), nullable=True)
    amenities = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    status = Column(String(20), nullable=False, server_default=text("'open'"))  # open | maintenance
    notes = Column(Text, nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()

    block = relationship("HostelBlock")
    bed_rows = relationship("HostelBed", back_populates="room", order_by="HostelBed.label")


class HostelBed(Base):
    __tablename__ = "hostel_beds"
    __table_args__ = (UniqueConstraint("room_id", "label", name="uq_hostel_beds_label"),
                      Index("ix_hostel_beds_company", "company_id", "room_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    room_id = Column(Integer, ForeignKey("hostel_rooms.id"), nullable=False)
    label = Column(String(10), nullable=False)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()

    room = relationship("HostelRoom", back_populates="bed_rows")


class HostelMessPlan(Base):
    __tablename__ = "hostel_mess_plans"
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_hostel_mess_plans_name"),
                      Index("ix_hostel_mess_plans_company", "company_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(80), nullable=False)
    fee = Column(Numeric(12, 2), nullable=False, server_default=text("0"))
    description = Column(Text, nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class HostelAllocation(Base):
    __tablename__ = "hostel_allocations"
    __table_args__ = (Index("ix_hostel_allocations_company", "company_id", "status"),
                      Index("ix_hostel_allocations_bed", "company_id", "bed_id", "status"),
                      Index("ix_hostel_allocations_student", "company_id", "student_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    block_id = Column(Integer, ForeignKey("hostel_blocks.id"), nullable=False)
    room_id = Column(Integer, ForeignKey("hostel_rooms.id"), nullable=False)
    bed_id = Column(Integer, ForeignKey("hostel_beds.id"), nullable=False)
    mess_plan_id = Column(Integer, ForeignKey("hostel_mess_plans.id"), nullable=True)
    check_in = Column(Date, nullable=False)
    check_out = Column(Date, nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'active'"))  # active | checked_out
    fee_amount = Column(Numeric(12, 2), nullable=True)
    fee_ref = Column(String(80), nullable=True)
    notes = Column(Text, nullable=True)
    created_at, updated_at = _stamps()

    student = relationship("Student")
    block = relationship("HostelBlock")
    room = relationship("HostelRoom")
    bed = relationship("HostelBed")
    mess_plan = relationship("HostelMessPlan")


class HostelRollCall(Base):
    __tablename__ = "hostel_roll_calls"
    __table_args__ = (UniqueConstraint("student_id", "day", "session", name="uq_hostel_roll_calls"),
                      Index("ix_hostel_roll_calls_company", "company_id", "block_id", "day"))
    id = Column(Integer, primary_key=True)
    company_id = _company()
    block_id = Column(Integer, ForeignKey("hostel_blocks.id"), nullable=False)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    day = Column(Date, nullable=False)
    session = Column(String(10), nullable=False, server_default=text("'night'"))  # night | morning
    status = Column(String(10), nullable=False)  # present | absent | leave
    remark = Column(String(200), nullable=True)
    marked_by = Column(String(200), nullable=True)
    marked_at = Column(DateTime(timezone=True), server_default=func.now())


class HostelLeave(Base):
    __tablename__ = "hostel_leaves"
    __table_args__ = (Index("ix_hostel_leaves_company", "company_id", "status"),
                      Index("ix_hostel_leaves_student", "company_id", "student_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    kind = Column(String(10), nullable=False, server_default=text("'leave'"))  # leave (nights away) | outpass (same day)
    from_at = Column(DateTime(timezone=True), nullable=False)
    to_at = Column(DateTime(timezone=True), nullable=False)
    reason = Column(Text, nullable=True)
    escort = Column(String(200), nullable=True)  # who takes the boarder out
    requested_by = Column(String(200), nullable=True)
    requested_via = Column(String(10), nullable=False, server_default=text("'staff'"))  # portal | staff
    # pending | approved | rejected | cancelled | out | returned
    status = Column(String(20), nullable=False, server_default=text("'pending'"))
    decided_by = Column(String(200), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decision_note = Column(Text, nullable=True)
    out_at = Column(DateTime(timezone=True), nullable=True)
    returned_at = Column(DateTime(timezone=True), nullable=True)
    created_at, updated_at = _stamps()

    student = relationship("Student")


class HostelVisitor(Base):
    __tablename__ = "hostel_visitors"
    __table_args__ = (Index("ix_hostel_visitors_company", "company_id", "in_at"),)
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    block_id = Column(Integer, ForeignKey("hostel_blocks.id"), nullable=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=True)
    name = Column(String(200), nullable=False)
    relation = Column(String(40), nullable=True)
    phone = Column(String(20), nullable=True)
    id_proof = Column(String(80), nullable=True)
    purpose = Column(String(200), nullable=True)
    in_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    out_at = Column(DateTime(timezone=True), nullable=True)
    logged_by = Column(String(200), nullable=True)
    created_at, updated_at = _stamps()

    student = relationship("Student")
    block = relationship("HostelBlock")


TABLES = ["hostel_blocks", "hostel_rooms", "hostel_beds", "hostel_mess_plans", "hostel_allocations",
          "hostel_roll_calls", "hostel_leaves", "hostel_visitors"]
