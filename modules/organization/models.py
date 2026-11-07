"""Organisation structure tables. Every table is company-owned (company_id + RLS)."""
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


class LegalEntity(Base):
    """A registered company in the group, with its own PAN, TAN, PF, ESI, PT and LWF
    registrations, bank accounts and authorised signatory. Payroll and filings run per entity."""
    __tablename__ = "legal_entities"
    __table_args__ = (Index("ix_legal_entities_company", "company_id", "active"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(200), nullable=False)          # short name used in lists
    legal_name = Column(String(250), nullable=True)     # as registered
    code = Column(String(20), nullable=True)
    pan = Column(String(10), nullable=True)
    tan = Column(String(10), nullable=True)
    cin = Column(String(21), nullable=True)
    gstin = Column(String(15), nullable=True)
    pf_code = Column(String(30), nullable=True)         # PF establishment code
    esi_code = Column(String(20), nullable=True)        # ESI employer code (17 digits)
    lin = Column(String(20), nullable=True)             # Labour identification number
    incorporation_date = Column(Date, nullable=True)
    address_line1 = Column(String(250), nullable=True)
    address_line2 = Column(String(250), nullable=True)
    city = Column(String(100), nullable=True)
    state_id = Column(Integer, ForeignKey("master_values.id"), nullable=True)
    pincode = Column(String(6), nullable=True)
    email = Column(String(255), nullable=True)
    phone = Column(String(20), nullable=True)
    # [{kind: "pt"|"lwf"|"shops", state_id, number, valid_from, valid_to}]
    registrations = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    # [{bank_id, account_name, account_number, ifsc, branch, purpose, primary}]
    bank_accounts = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    # {name, designation, pan, email, phone, employee_id}
    signatory = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    is_primary = Column(Boolean, nullable=False, server_default=text("false"))
    custom = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))  # custom field values
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class Region(Base):
    __tablename__ = "regions"
    __table_args__ = (Index("ix_regions_company", "company_id", "active"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(150), nullable=False)
    code = Column(String(20), nullable=True)
    description = Column(Text, nullable=True)
    head_employee_id = Column(Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()

    head = relationship("Employee", foreign_keys=[head_employee_id])


class Location(Base):
    """A branch or office. Its state decides PT, LWF, holidays and shop rules; the geo
    point and radius are the attendance boundary."""
    __tablename__ = "locations"
    __table_args__ = (
        Index("ix_locations_company", "company_id", "active"),
        Index("ix_locations_company_region", "company_id", "region_id"),
        Index("ix_locations_company_entity", "company_id", "legal_entity_id"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(150), nullable=False)
    code = Column(String(20), nullable=True)
    kind = Column(String(20), nullable=False, server_default=text("'branch'"))  # head_office|branch|factory|warehouse|site|remote
    region_id = Column(Integer, ForeignKey("regions.id"), nullable=True)
    legal_entity_id = Column(Integer, ForeignKey("legal_entities.id"), nullable=True)
    address_line1 = Column(String(250), nullable=True)
    address_line2 = Column(String(250), nullable=True)
    city = Column(String(100), nullable=True)
    state_id = Column(Integer, ForeignKey("master_values.id"), nullable=True)
    pincode = Column(String(6), nullable=True)
    timezone = Column(String(64), nullable=True)
    latitude = Column(Numeric(9, 6), nullable=True)
    longitude = Column(Numeric(9, 6), nullable=True)
    radius_m = Column(Integer, nullable=True)
    holiday_calendar_id = Column(Integer, nullable=True)  # set by the holidays module
    phone = Column(String(20), nullable=True)
    email = Column(String(255), nullable=True)
    custom = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))  # custom field values
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()

    region = relationship("Region")
    legal_entity = relationship("LegalEntity")


class Grade(Base):
    """Grades / bands used by pay and policy rules. `rank` orders them (1 = most junior)."""
    __tablename__ = "grades"
    __table_args__ = (Index("ix_grades_company", "company_id", "rank"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(100), nullable=False)
    code = Column(String(20), nullable=True)
    band = Column(String(60), nullable=True)
    rank = Column(Integer, nullable=False, server_default=text("0"))
    min_ctc = Column(Numeric(14, 2), nullable=True)
    max_ctc = Column(Numeric(14, 2), nullable=True)
    description = Column(Text, nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class CostCentre(Base):
    __tablename__ = "cost_centres"
    __table_args__ = (Index("ix_cost_centres_company", "company_id", "active"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(150), nullable=False)
    code = Column(String(30), nullable=True)
    description = Column(Text, nullable=True)
    legal_entity_id = Column(Integer, ForeignKey("legal_entities.id"), nullable=True)
    owner_employee_id = Column(Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()

    legal_entity = relationship("LegalEntity")
    owner = relationship("Employee", foreign_keys=[owner_employee_id])


class Team(Base):
    """A working group that can cut across departments."""
    __tablename__ = "teams"
    __table_args__ = (Index("ix_teams_company", "company_id", "active"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(150), nullable=False)
    code = Column(String(20), nullable=True)
    description = Column(Text, nullable=True)
    lead_employee_id = Column(Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    color = Column(String(20), nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()

    lead = relationship("Employee", foreign_keys=[lead_employee_id])
    members = relationship("TeamMember", back_populates="team", cascade="all, delete-orphan")


class TeamMember(Base):
    __tablename__ = "team_members"
    __table_args__ = (
        UniqueConstraint("team_id", "employee_id", name="uq_team_members_team_employee"),
        Index("ix_team_members_company_employee", "company_id", "employee_id"),
        Index("ix_team_members_company_team", "company_id", "team_id"),
    )

    id = Column(Integer, primary_key=True)
    company_id = _company()
    team_id = Column(Integer, ForeignKey("teams.id"), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    role = Column(String(60), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    team = relationship("Team", back_populates="members")
    employee = relationship("Employee")


class Tag(Base):
    """A label with optional sub-tags (parent_id). `applies_to` limits which kinds of record
    it can be put on (empty = any)."""
    __tablename__ = "tags"
    __table_args__ = (
        Index("ix_tags_company_parent", "company_id", "parent_id"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    parent_id = Column(Integer, ForeignKey("tags.id"), nullable=True)
    name = Column(String(100), nullable=False)
    color = Column(String(20), nullable=True)
    description = Column(Text, nullable=True)
    applies_to = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    sort_order = Column(Integer, nullable=False, server_default=text("0"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()

    parent = relationship("Tag", remote_side=[id])


class TagLink(Base):
    """A tag put on one record of any kind (entity_type is the table name, e.g. employees)."""
    __tablename__ = "tag_links"
    __table_args__ = (
        UniqueConstraint("tag_id", "entity_type", "entity_id", name="uq_tag_links_tag_entity"),
        Index("ix_tag_links_company_entity", "company_id", "entity_type", "entity_id"),
        Index("ix_tag_links_company_tag", "company_id", "tag_id"),
    )

    id = Column(Integer, primary_key=True)
    company_id = _company()
    tag_id = Column(Integer, ForeignKey("tags.id"), nullable=False)
    entity_type = Column(String(50), nullable=False)
    entity_id = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    tag = relationship("Tag")


class Skill(Base):
    __tablename__ = "skills"
    __table_args__ = (Index("ix_skills_company_category", "company_id", "category_id"),)

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(120), nullable=False)
    category_id = Column(Integer, ForeignKey("master_values.id"), nullable=True)  # skill_category master
    description = Column(Text, nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class EmployeeSkill(Base):
    """A skill an employee has, with level 1 (beginner) to 5 (expert)."""
    __tablename__ = "employee_skills"
    __table_args__ = (
        UniqueConstraint("employee_id", "skill_id", name="uq_employee_skills_employee_skill"),
        Index("ix_employee_skills_company_employee", "company_id", "employee_id"),
        Index("ix_employee_skills_company_skill", "company_id", "skill_id"),
    )

    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    skill_id = Column(Integer, ForeignKey("skills.id"), nullable=False)
    level = Column(Integer, nullable=False, server_default=text("3"))
    years = Column(Numeric(4, 1), nullable=True)
    verified = Column(Boolean, nullable=False, server_default=text("false"))
    verified_by_name = Column(String(200), nullable=True)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    notes = Column(Text, nullable=True)
    created_at, updated_at = _stamps()

    skill = relationship("Skill")
