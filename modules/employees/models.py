from sqlalchemy import (Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, LargeBinary, Numeric, String, Text,
                        UniqueConstraint, text)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from core.db import Base


class Employee(Base):
    __tablename__ = "employees"
    __table_args__ = (
        UniqueConstraint("company_id", "email", name="uq_employees_company_email"),
        Index("ix_employees_company_active", "company_id", "active"),
        Index("ix_employees_company_designation", "company_id", "designation_id"),
        Index("ix_employees_company_manager", "company_id", "reporting_manager_id"),
        Index("ix_employees_company_department", "company_id", "department_id"),
        Index("ix_employees_company_status", "company_id", "status"),
        Index("ix_employees_company_location", "company_id", "location_id"),
        Index("ix_employees_company_entity", "company_id", "legal_entity_id"),
        Index("ix_employees_company_pan", "company_id", "pan"),
        Index("ix_employees_company_uan", "company_id", "uan"),
        Index("ix_employees_company_aadhaar", "company_id", "aadhaar_hash"),
        Index("uq_employees_company_code", "company_id", "employee_code", unique=True,
              postgresql_where=text("employee_code IS NOT NULL")),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    designation_id = Column(Integer, ForeignKey("designations.id"), nullable=True)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    employee_code = Column(String(30), nullable=True)
    greeting = Column(String(20), nullable=True)
    first_name = Column(String(100), nullable=False)
    last_name = Column(String(100), nullable=False)
    email = Column(String(255), nullable=False)
    phone = Column(String(20), nullable=True)
    password = Column(String(255), nullable=False)
    password_reset_token = Column(String(255), nullable=True)
    role = Column(String(20), nullable=False, server_default=text("'employee'"))
    profile_image_url = Column(Text, nullable=True)  # data URL (base64) or absolute URL
    signature_url = Column(String(500), nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    # IANA name such as Asia/Kolkata; NULL means the company's timezone.
    timezone = Column(String(64), nullable=True)
    reporting_manager_id = Column(Integer, ForeignKey("employees.id", name="fk_employees_reporting_manager_id",
                                                      ondelete="SET NULL"), nullable=True)
    # Values of custom fields (see core.custom_fields), keyed by field key.
    custom = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))

    # ── Personal ──
    middle_name = Column(String(100), nullable=True)
    preferred_name = Column(String(100), nullable=True)
    gender_id = Column(Integer, ForeignKey("master_values.id"), nullable=True)
    date_of_birth = Column(Date, nullable=True)
    marital_status_id = Column(Integer, ForeignKey("master_values.id"), nullable=True)
    marriage_date = Column(Date, nullable=True)
    blood_group_id = Column(Integer, ForeignKey("master_values.id"), nullable=True)
    nationality_id = Column(Integer, ForeignKey("master_values.id"), nullable=True)
    father_name = Column(String(200), nullable=True)
    spouse_name = Column(String(200), nullable=True)
    # ── Contact ──
    personal_email = Column(String(255), nullable=True)
    alternate_phone = Column(String(20), nullable=True)
    work_phone = Column(String(20), nullable=True)
    extension = Column(String(10), nullable=True)
    # {line1, line2, city, district, state_id, pincode, country_id}
    current_address = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    permanent_address = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    # ── Statutory IDs ── (Aadhaar: encrypted, a keyed hash for duplicate checks, last 4 shown)
    pan = Column(String(10), nullable=True)
    aadhaar_encrypted = Column(Text, nullable=True)
    aadhaar_hash = Column(String(64), nullable=True)
    aadhaar_last4 = Column(String(4), nullable=True)
    uan = Column(String(12), nullable=True)
    esi_number = Column(String(17), nullable=True)
    passport_number = Column(String(20), nullable=True)
    passport_expiry = Column(Date, nullable=True)
    driving_licence = Column(String(20), nullable=True)
    driving_licence_expiry = Column(Date, nullable=True)
    # ── Job ──
    legal_entity_id = Column(Integer, ForeignKey("legal_entities.id"), nullable=True)
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=True)
    grade_id = Column(Integer, ForeignKey("grades.id"), nullable=True)
    cost_centre_id = Column(Integer, ForeignKey("cost_centres.id"), nullable=True)
    employment_type_id = Column(Integer, ForeignKey("master_values.id"), nullable=True)
    # Lifecycle stage: pre_joining | probation | confirmed | notice | exited | archived
    status = Column(String(20), nullable=False, server_default=text("'confirmed'"))
    # Optional company-defined sub-status (employee_status master), e.g. suspended, long leave
    employee_status_id = Column(Integer, ForeignKey("master_values.id"), nullable=True)
    date_of_joining = Column(Date, nullable=True)
    probation_end_date = Column(Date, nullable=True)
    confirmation_date = Column(Date, nullable=True)
    notice_period_days = Column(Integer, nullable=True)
    resignation_date = Column(Date, nullable=True)
    exit_date = Column(Date, nullable=True)
    secondary_manager_id = Column(Integer, ForeignKey("employees.id", name="fk_employees_secondary_manager_id",
                                                      ondelete="SET NULL"), nullable=True)
    work_mode = Column(String(20), nullable=True)  # office | remote | hybrid | field
    rehire_count = Column(Integer, nullable=False, server_default=text("0"))
    archived_at = Column(DateTime(timezone=True), nullable=True)
    archived_by = Column(String(200), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    company = relationship("Company", back_populates="employees", foreign_keys=[company_id])
    reporting_manager = relationship("Employee", remote_side=[id], foreign_keys=[reporting_manager_id])
    secondary_manager = relationship("Employee", remote_side=[id], foreign_keys=[secondary_manager_id])
    legal_entity = relationship("LegalEntity", foreign_keys=[legal_entity_id])
    location = relationship("Location", foreign_keys=[location_id])
    grade = relationship("Grade", foreign_keys=[grade_id])
    cost_centre = relationship("CostCentre", foreign_keys=[cost_centre_id])
    designation = relationship("Designation", back_populates="employees")
    department = relationship("Department", back_populates="employees", foreign_keys=[department_id])
    settings = relationship("EmployeeSettings", back_populates="employee", uselist=False)
    roster_entries = relationship("RosterEntry", back_populates="employee")


class EmployeeSettings(Base):
    """Per-employee settings, such as the work arrangement (working days and shift timings)."""
    __tablename__ = "employee_settings"
    __table_args__ = (Index("ix_employee_settings_company_employee", "company_id", "employee_id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    company_id = Column(Integer, ForeignKey("companies.id", name="fk_employee_settings_company_id"), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id"), unique=True, nullable=False)
    config = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    employee = relationship("Employee", back_populates="settings")


# ── Employee details (one row per item) ─────────────────────────────

def _uuid():
    return Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))


def _company():
    return Column(Integer, ForeignKey("companies.id"), nullable=False)


def _employee():
    return Column(Integer, ForeignKey("employees.id"), nullable=False)


def _stamps():
    return (Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
            Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False))


class EmergencyContact(Base):
    """Emergency contacts / next of kin."""
    __tablename__ = "employee_emergency_contacts"
    __table_args__ = (Index("ix_employee_emergency_contacts_company_employee", "company_id", "employee_id"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = _employee()
    name = Column(String(200), nullable=False)
    relation_id = Column(Integer, ForeignKey("master_values.id"), nullable=True)
    phone = Column(String(20), nullable=False)
    alternate_phone = Column(String(20), nullable=True)
    email = Column(String(255), nullable=True)
    address = Column(Text, nullable=True)
    is_primary = Column(Boolean, nullable=False, server_default=text("false"))
    created_at, updated_at = _stamps()


class FamilyMember(Base):
    __tablename__ = "employee_family_members"
    __table_args__ = (Index("ix_employee_family_members_company_employee", "company_id", "employee_id"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = _employee()
    name = Column(String(200), nullable=False)
    relation_id = Column(Integer, ForeignKey("master_values.id"), nullable=True)
    gender_id = Column(Integer, ForeignKey("master_values.id"), nullable=True)
    date_of_birth = Column(Date, nullable=True)
    occupation = Column(String(120), nullable=True)
    phone = Column(String(20), nullable=True)
    is_dependent = Column(Boolean, nullable=False, server_default=text("false"))
    lives_with_employee = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class Nominee(Base):
    """A nominee for PF, EPS, gratuity or insurance. Shares per kind add up to 100%."""
    __tablename__ = "employee_nominees"
    __table_args__ = (Index("ix_employee_nominees_company_employee", "company_id", "employee_id", "kind"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = _employee()
    kind = Column(String(20), nullable=False)  # pf | eps | gratuity | insurance | other
    family_member_id = Column(Integer, ForeignKey("employee_family_members.id"), nullable=True)
    name = Column(String(200), nullable=False)
    relation_id = Column(Integer, ForeignKey("master_values.id"), nullable=True)
    date_of_birth = Column(Date, nullable=True)
    share_percent = Column(Numeric(5, 2), nullable=False)
    guardian_name = Column(String(200), nullable=True)  # when the nominee is a minor
    address = Column(Text, nullable=True)
    created_at, updated_at = _stamps()


class BankAccount(Base):
    __tablename__ = "employee_bank_accounts"
    __table_args__ = (Index("ix_employee_bank_accounts_company_employee", "company_id", "employee_id"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = _employee()
    bank_id = Column(Integer, ForeignKey("master_values.id"), nullable=True)
    account_holder = Column(String(200), nullable=False)
    account_number = Column(String(20), nullable=False)
    ifsc = Column(String(11), nullable=False)
    branch = Column(String(150), nullable=True)
    account_type = Column(String(20), nullable=False, server_default=text("'savings'"))
    is_primary = Column(Boolean, nullable=False, server_default=text("false"))
    verified = Column(Boolean, nullable=False, server_default=text("false"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class Education(Base):
    __tablename__ = "employee_education"
    __table_args__ = (Index("ix_employee_education_company_employee", "company_id", "employee_id"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = _employee()
    level_id = Column(Integer, ForeignKey("master_values.id"), nullable=True)
    degree = Column(String(150), nullable=False)
    specialisation = Column(String(150), nullable=True)
    institution = Column(String(200), nullable=True)
    university = Column(String(200), nullable=True)
    start_year = Column(Integer, nullable=True)
    end_year = Column(Integer, nullable=True)
    score = Column(String(30), nullable=True)  # e.g. 8.2 CGPA, 78%
    created_at, updated_at = _stamps()


class PreviousEmployment(Base):
    """A job before joining. Income and tax deducted by that employer in the current
    financial year feed this year's TDS."""
    __tablename__ = "employee_previous_employment"
    __table_args__ = (Index("ix_employee_previous_employment_company_employee", "company_id", "employee_id"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = _employee()
    company_name = Column(String(200), nullable=False)
    designation = Column(String(150), nullable=True)
    from_date = Column(Date, nullable=True)
    to_date = Column(Date, nullable=True)
    last_ctc = Column(Numeric(14, 2), nullable=True)
    reason_for_leaving = Column(String(300), nullable=True)
    is_relevant = Column(Boolean, nullable=False, server_default=text("true"))
    financial_year = Column(String(7), nullable=True)  # e.g. 2026-27
    fy_taxable_income = Column(Numeric(14, 2), nullable=True)
    fy_tds = Column(Numeric(14, 2), nullable=True)
    fy_pf = Column(Numeric(14, 2), nullable=True)
    fy_pt = Column(Numeric(14, 2), nullable=True)
    previous_pf_member_id = Column(String(30), nullable=True)
    created_at, updated_at = _stamps()


class JobChange(Base):
    """Effective-dated job history: joining, transfer, promotion, designation, department,
    manager or other changes, with the values before and after and the reason. A change
    dated in the future is 'scheduled' and applied on that date."""
    __tablename__ = "employee_job_history"
    __table_args__ = (
        Index("ix_employee_job_history_company_employee", "company_id", "employee_id", "effective_date"),
        Index("ix_employee_job_history_company_status", "company_id", "status", "effective_date"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = _employee()
    change_type = Column(String(30), nullable=False)
    effective_date = Column(Date, nullable=False)
    changes = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))  # {field: {old, new}}
    reason = Column(String(300), nullable=True)
    remarks = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'applied'"))  # scheduled|applied|cancelled
    applied_at = Column(DateTime(timezone=True), nullable=True)
    created_by_name = Column(String(200), nullable=True)
    created_at, updated_at = _stamps()


class StoredFile(Base):
    """An uploaded file (document scan), kept in the database. Not audited row by row:
    the document that points to it is."""
    __tablename__ = "employee_files"
    __audit__ = False
    __table_args__ = (Index("ix_employee_files_company_employee", "company_id", "employee_id"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = _employee()
    filename = Column(String(255), nullable=False)
    content_type = Column(String(120), nullable=False)
    size = Column(Integer, nullable=False)
    data = Column(LargeBinary, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class EmployeeDocument(Base):
    __tablename__ = "employee_documents"
    __table_args__ = (
        Index("ix_employee_documents_company_employee", "company_id", "employee_id"),
        Index("ix_employee_documents_company_expiry", "company_id", "expiry_date"),
        Index("ix_employee_documents_company_status", "company_id", "verification_status"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = _employee()
    document_type_id = Column(Integer, ForeignKey("master_values.id"), nullable=True)
    title = Column(String(200), nullable=True)
    document_number = Column(String(60), nullable=True)
    issue_date = Column(Date, nullable=True)
    expiry_date = Column(Date, nullable=True)
    issued_by = Column(String(150), nullable=True)
    file_id = Column(Integer, ForeignKey("employee_files.id"), nullable=True)
    verification_status = Column(String(20), nullable=False, server_default=text("'pending'"))  # pending|verified|rejected
    verified_by_name = Column(String(200), nullable=True)
    verified_by_employee_id = Column(Integer, nullable=True)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    remarks = Column(Text, nullable=True)
    remind_days_before = Column(Integer, nullable=False, server_default=text("30"))
    reminder_sent_at = Column(DateTime(timezone=True), nullable=True)
    visible_to_employee = Column(Boolean, nullable=False, server_default=text("true"))
    archived_at = Column(DateTime(timezone=True), nullable=True)
    created_at, updated_at = _stamps()

    file = relationship("StoredFile")


class SavedView(Base):
    """A saved set of filters (and columns) on a list, private or shared with the company."""
    __tablename__ = "saved_views"
    __table_args__ = (Index("ix_saved_views_company_entity", "company_id", "entity", "owner_key"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    entity = Column(String(40), nullable=False)  # e.g. employees
    owner_key = Column(String(40), nullable=False)  # "employee:<id>" or "owner"
    name = Column(String(100), nullable=False)
    filters = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    columns = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    shared = Column(Boolean, nullable=False, server_default=text("false"))
    created_at, updated_at = _stamps()
