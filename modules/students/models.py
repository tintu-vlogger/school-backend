"""Learners, their guardians, and enrolment in a section for a session.

- Student          one learner (admission number from the "student" number series)
- Guardian         a parent, guardian or emergency contact (one guardian can have several children)
- StudentGuardian  link with relation, primary contact, pickup and notification flags
- Enrollment       the learner's place in one session: program, section, roll number, status
- PortalAccount    sign-in for parents and students (staff sign in through employees)
- StudentDocument  files attached to a learner (birth certificate, transfer certificate, photo ID …)
"""
from sqlalchemy import (Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text,
                        UniqueConstraint, text)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from core.db import Base


class Student(Base):
    __tablename__ = "students"
    __table_args__ = (UniqueConstraint("company_id", "admission_no", name="uq_students_admission_no"),
                      Index("ix_students_company_status", "company_id", "status"),
                      Index("ix_students_company_name", "company_id", "first_name", "last_name"))

    id = Column(Integer, primary_key=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    admission_no = Column(String(40), nullable=False)
    first_name = Column(String(100), nullable=False)
    middle_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)
    gender = Column(String(20), nullable=True)  # male | female | other
    date_of_birth = Column(Date, nullable=True)
    blood_group = Column(String(10), nullable=True)
    email = Column(String(255), nullable=True)
    phone = Column(String(20), nullable=True)
    photo_url = Column(Text, nullable=True)
    address = Column(Text, nullable=True)
    city = Column(String(100), nullable=True)
    state = Column(String(100), nullable=True)
    pincode = Column(String(12), nullable=True)
    country = Column(String(100), nullable=True)
    nationality = Column(String(60), nullable=True)
    religion = Column(String(60), nullable=True)
    category = Column(String(40), nullable=True)  # social category (General, OBC, SC, ST, EWS …) or any local list
    mother_tongue = Column(String(60), nullable=True)
    national_id = Column(String(40), nullable=True)  # Aadhaar / national ID / passport (masked in audit)
    student_reg_id = Column(String(60), nullable=True)  # board / university / government student ID (APAAR, PEN …)
    previous_institution = Column(String(200), nullable=True)
    admission_date = Column(Date, nullable=True)
    house_id = Column(Integer, ForeignKey("academic_houses.id"), nullable=True)
    # active | alumni | transferred | left | suspended | graduated
    status = Column(String(20), nullable=False, server_default=text("'active'"))
    left_on = Column(Date, nullable=True)
    leaving_reason = Column(Text, nullable=True)
    medical_notes = Column(Text, nullable=True)
    allergies = Column(Text, nullable=True)
    is_boarder = Column(Boolean, nullable=False, server_default=text("false"))
    uses_transport = Column(Boolean, nullable=False, server_default=text("false"))
    # Student-specific concessions / scholarship tag used by fees.
    fee_category = Column(String(60), nullable=True)
    custom = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    archived_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    house = relationship("House")
    guardian_links = relationship("StudentGuardian", back_populates="student", cascade="all, delete-orphan")
    enrollments = relationship("Enrollment", back_populates="student", order_by="Enrollment.id.desc()")

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.middle_name, self.last_name) if p)


class Guardian(Base):
    __tablename__ = "guardians"
    __table_args__ = (Index("ix_guardians_company_phone", "company_id", "phone"),
                      Index("ix_guardians_company_email", "company_id", "email"))

    id = Column(Integer, primary_key=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    first_name = Column(String(100), nullable=False)
    last_name = Column(String(100), nullable=True)
    email = Column(String(255), nullable=True)
    phone = Column(String(20), nullable=True)
    alt_phone = Column(String(20), nullable=True)
    occupation = Column(String(120), nullable=True)
    organisation = Column(String(200), nullable=True)
    annual_income = Column(Numeric(14, 2), nullable=True)
    education = Column(String(120), nullable=True)
    address = Column(Text, nullable=True)
    photo_url = Column(Text, nullable=True)
    national_id = Column(String(40), nullable=True)
    custom = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    student_links = relationship("StudentGuardian", back_populates="guardian")

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p)


class StudentGuardian(Base):
    __tablename__ = "student_guardians"
    __table_args__ = (UniqueConstraint("student_id", "guardian_id", name="uq_student_guardians"),
                      Index("ix_student_guardians_company", "company_id"),
                      Index("ix_student_guardians_guardian", "guardian_id"))

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    student_id = Column(Integer, ForeignKey("students.id", ondelete="CASCADE"), nullable=False)
    guardian_id = Column(Integer, ForeignKey("guardians.id", ondelete="CASCADE"), nullable=False)
    relation = Column(String(30), nullable=False, server_default=text("'guardian'"))  # father | mother | guardian | …
    is_primary = Column(Boolean, nullable=False, server_default=text("false"))
    can_pickup = Column(Boolean, nullable=False, server_default=text("true"))
    receives_notifications = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    student = relationship("Student", back_populates="guardian_links")
    guardian = relationship("Guardian", back_populates="student_links")


class Enrollment(Base):
    __tablename__ = "student_enrollments"
    __table_args__ = (UniqueConstraint("student_id", "session_id", name="uq_student_enrollments_session"),
                      Index("ix_student_enrollments_section", "company_id", "section_id", "status"),
                      Index("ix_student_enrollments_session", "company_id", "session_id"))

    id = Column(Integer, primary_key=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    student_id = Column(Integer, ForeignKey("students.id", ondelete="CASCADE"), nullable=False)
    session_id = Column(Integer, ForeignKey("academic_sessions.id"), nullable=False)
    program_id = Column(Integer, ForeignKey("academic_programs.id"), nullable=False)
    section_id = Column(Integer, ForeignKey("academic_sections.id"), nullable=True)
    term_no = Column(Integer, nullable=True)  # semester/term number within the program, where it matters
    roll_no = Column(String(20), nullable=True)
    # enrolled | promoted | detained | transferred | completed | dropped
    status = Column(String(20), nullable=False, server_default=text("'enrolled'"))
    enrolled_on = Column(Date, nullable=True)
    left_on = Column(Date, nullable=True)
    # Chosen electives: list of subject ids.
    elective_subject_ids = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    remarks = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    student = relationship("Student", back_populates="enrollments")
    session = relationship("AcademicSession")
    program = relationship("Program")
    section = relationship("Section")


class PortalAccount(Base):
    """Sign-in for a parent (guardian_id) or a student (student_id). `login` is an email
    address or a username such as the admission number; it is unique within the institution."""
    __tablename__ = "portal_accounts"
    __table_args__ = (UniqueConstraint("company_id", "login", name="uq_portal_accounts_login"),
                      Index("ix_portal_accounts_guardian", "company_id", "guardian_id"),
                      Index("ix_portal_accounts_student", "company_id", "student_id"))

    id = Column(Integer, primary_key=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    kind = Column(String(10), nullable=False)  # parent | student
    guardian_id = Column(Integer, ForeignKey("guardians.id", ondelete="CASCADE"), nullable=True)
    student_id = Column(Integer, ForeignKey("students.id", ondelete="CASCADE"), nullable=True)
    login = Column(String(255), nullable=False)
    password = Column(String(255), nullable=False)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    must_change_password = Column(Boolean, nullable=False, server_default=text("true"))
    timezone = Column(String(50), nullable=True)
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    guardian = relationship("Guardian")
    student = relationship("Student")


class StudentDocument(Base):
    __tablename__ = "student_documents"
    __table_args__ = (Index("ix_student_documents_student", "company_id", "student_id"),)

    id = Column(Integer, primary_key=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    student_id = Column(Integer, ForeignKey("students.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(200), nullable=False)
    doc_type = Column(String(60), nullable=True)
    file_url = Column(Text, nullable=False)
    verified = Column(Boolean, nullable=False, server_default=text("false"))
    expires_on = Column(Date, nullable=True)
    uploaded_by = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


TABLES = ["students", "guardians", "student_guardians", "student_enrollments", "portal_accounts", "student_documents"]
