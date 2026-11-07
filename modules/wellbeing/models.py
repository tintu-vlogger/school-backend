"""Wellbeing: health records, check-up camps, infirmary, discipline and merits, counselling, gate.

- WbHealthRecord     one per learner: latest measurements, vision, dental, immunisations, doctor
- WbCheckupCamp      a periodic health check-up camp (general, dental, eye …)
- WbCheckup          one learner's measurements at a camp or a one-off check-up
- WbInfirmaryVisit   a visit to the infirmary: complaint, treatment, medicine, sent home, parent told
- WbIncidentType     kinds of incidents with default demerit points (configurable)
- WbIncident         a discipline incident: severity, action, follow-up, confidential flag
- WbPoint            merit (+) or demerit (−) points; house totals use the learner's house
- WbCounsellingNote  restricted counselling notes (permission wellbeing.counselling)
- WbVisitor          the gate visitor log: purpose, person to meet, ID, photo, time in and out
- WbGatePass         early pickup of a learner by an authorised guardian
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


class WbHealthRecord(Base):
    __tablename__ = "wb_health_records"
    __table_args__ = (UniqueConstraint("company_id", "student_id", name="uq_wb_health_records_student"),
                      Index("ix_wb_health_records_company", "company_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    height_cm = Column(Numeric(5, 1), nullable=True)
    weight_kg = Column(Numeric(5, 1), nullable=True)
    vision_left = Column(String(20), nullable=True)
    vision_right = Column(String(20), nullable=True)
    dental = Column(String(200), nullable=True)
    hearing = Column(String(60), nullable=True)
    chronic_conditions = Column(Text, nullable=True)
    immunisations = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))  # [{vaccine, dose, date, notes}]
    doctor_name = Column(String(200), nullable=True)
    doctor_phone = Column(String(20), nullable=True)
    insurance = Column(String(200), nullable=True)
    notes = Column(Text, nullable=True)
    measured_on = Column(Date, nullable=True)
    created_at, updated_at = _stamps()

    student = relationship("Student")


class WbCheckupCamp(Base):
    __tablename__ = "wb_checkup_camps"
    __table_args__ = (Index("ix_wb_checkup_camps_company", "company_id", "held_on"),)
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(160), nullable=False)
    kind = Column(String(20), nullable=False, server_default=text("'general'"))  # general | dental | eye | other
    held_on = Column(Date, nullable=False)
    doctor = Column(String(200), nullable=True)
    agency = Column(String(200), nullable=True)
    notes = Column(Text, nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class WbCheckup(Base):
    __tablename__ = "wb_checkups"
    __table_args__ = (Index("ix_wb_checkups_company", "company_id", "student_id"),
                      Index("ix_wb_checkups_camp", "company_id", "camp_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    camp_id = Column(Integer, ForeignKey("wb_checkup_camps.id"), nullable=True)
    checked_on = Column(Date, nullable=False)
    height_cm = Column(Numeric(5, 1), nullable=True)
    weight_kg = Column(Numeric(5, 1), nullable=True)
    vision_left = Column(String(20), nullable=True)
    vision_right = Column(String(20), nullable=True)
    dental = Column(String(200), nullable=True)
    blood_pressure = Column(String(20), nullable=True)
    pulse = Column(Integer, nullable=True)
    findings = Column(Text, nullable=True)
    recommendations = Column(Text, nullable=True)
    examined_by = Column(String(200), nullable=True)
    created_at, updated_at = _stamps()

    student = relationship("Student")
    camp = relationship("WbCheckupCamp")


class WbInfirmaryVisit(Base):
    __tablename__ = "wb_infirmary_visits"
    __table_args__ = (Index("ix_wb_infirmary_visits_company", "company_id", "visit_at"),
                      Index("ix_wb_infirmary_visits_student", "company_id", "student_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    visit_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    complaint = Column(String(300), nullable=False)
    temperature = Column(Numeric(4, 1), nullable=True)
    treatment = Column(Text, nullable=True)
    medicine_given = Column(String(300), nullable=True)
    sent_home = Column(Boolean, nullable=False, server_default=text("false"))
    parent_informed = Column(Boolean, nullable=False, server_default=text("false"))
    follow_up = Column(Text, nullable=True)
    attended_by = Column(String(200), nullable=True)
    left_at = Column(DateTime(timezone=True), nullable=True)
    created_at, updated_at = _stamps()

    student = relationship("Student")


class WbIncidentType(Base):
    __tablename__ = "wb_incident_types"
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_wb_incident_types_name"),
                      Index("ix_wb_incident_types_company", "company_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(80), nullable=False)
    default_severity = Column(String(10), nullable=False, server_default=text("'low'"))
    default_points = Column(Integer, nullable=False, server_default=text("0"))  # demerits (negative) for this kind
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class WbIncident(Base):
    __tablename__ = "wb_incidents"
    __table_args__ = (Index("ix_wb_incidents_company", "company_id", "occurred_at"),
                      Index("ix_wb_incidents_student", "company_id", "student_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    type_id = Column(Integer, ForeignKey("wb_incident_types.id"), nullable=True)
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    location = Column(String(120), nullable=True)
    severity = Column(String(10), nullable=False, server_default=text("'low'"))  # low | medium | high | critical
    description = Column(Text, nullable=False)
    action_taken = Column(Text, nullable=True)
    follow_up_on = Column(Date, nullable=True)
    follow_up_notes = Column(Text, nullable=True)
    status = Column(String(10), nullable=False, server_default=text("'open'"))  # open | closed
    parent_informed = Column(Boolean, nullable=False, server_default=text("false"))
    confidential = Column(Boolean, nullable=False, server_default=text("false"))
    reported_by = Column(String(200), nullable=True)
    reported_by_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    created_at, updated_at = _stamps()

    student = relationship("Student")
    type = relationship("WbIncidentType")


class WbPoint(Base):
    __tablename__ = "wb_points"
    __table_args__ = (Index("ix_wb_points_company", "company_id", "awarded_on"),
                      Index("ix_wb_points_student", "company_id", "student_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    points = Column(Integer, nullable=False)  # + merit, − demerit
    reason = Column(String(300), nullable=False)
    category = Column(String(60), nullable=True)  # academics, sports, conduct, service …
    awarded_on = Column(Date, nullable=False)
    awarded_by = Column(String(200), nullable=True)
    incident_id = Column(Integer, ForeignKey("wb_incidents.id"), nullable=True)
    voided = Column(Boolean, nullable=False, server_default=text("false"))
    created_at, updated_at = _stamps()

    student = relationship("Student")


class WbCounsellingNote(Base):
    __tablename__ = "wb_counselling_notes"
    __table_args__ = (Index("ix_wb_counselling_notes_company", "company_id", "student_id"),)
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    session_on = Column(Date, nullable=False)
    counsellor = Column(String(200), nullable=True)
    counsellor_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    concern = Column(String(200), nullable=True)
    notes = Column(Text, nullable=False)
    plan = Column(Text, nullable=True)
    next_session_on = Column(Date, nullable=True)
    archived_at = Column(DateTime(timezone=True), nullable=True)
    created_at, updated_at = _stamps()

    student = relationship("Student")


class WbVisitor(Base):
    __tablename__ = "wb_visitors"
    __table_args__ = (Index("ix_wb_visitors_company", "company_id", "in_at"),)
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    pass_no = Column(String(40), nullable=True)
    name = Column(String(200), nullable=False)
    phone = Column(String(20), nullable=True)
    company_name = Column(String(200), nullable=True)  # where the visitor is from
    purpose = Column(String(200), nullable=False)
    person_to_meet = Column(String(200), nullable=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=True)
    id_kind = Column(String(40), nullable=True)
    id_number = Column(String(60), nullable=True)
    photo_url = Column(Text, nullable=True)
    vehicle_no = Column(String(20), nullable=True)
    people_count = Column(Integer, nullable=False, server_default=text("1"))
    in_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    out_at = Column(DateTime(timezone=True), nullable=True)
    logged_by = Column(String(200), nullable=True)
    notes = Column(Text, nullable=True)
    created_at, updated_at = _stamps()

    employee = relationship("Employee")
    student = relationship("Student")


class WbGatePass(Base):
    __tablename__ = "wb_gate_passes"
    __table_args__ = (Index("ix_wb_gate_passes_company", "company_id", "out_at"),
                      Index("ix_wb_gate_passes_student", "company_id", "student_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    pass_no = Column(String(40), nullable=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    guardian_id = Column(Integer, ForeignKey("guardians.id"), nullable=False)
    reason = Column(String(300), nullable=False)
    out_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    returning = Column(Boolean, nullable=False, server_default=text("false"))
    returned_at = Column(DateTime(timezone=True), nullable=True)
    id_checked = Column(Boolean, nullable=False, server_default=text("true"))
    issued_by = Column(String(200), nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'issued'"))  # issued | returned | cancelled
    notes = Column(Text, nullable=True)
    created_at, updated_at = _stamps()

    student = relationship("Student")
    guardian = relationship("Guardian")


TABLES = ["wb_health_records", "wb_checkup_camps", "wb_checkups", "wb_infirmary_visits", "wb_incident_types",
          "wb_incidents", "wb_points", "wb_counselling_notes", "wb_visitors", "wb_gate_passes"]
