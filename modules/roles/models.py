from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from core.db import Base


class Role(Base):
    __tablename__ = "roles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    role_name = Column(String(50), nullable=False)  # "admin", "hr", "manager", "employee" or custom
    display_name = Column(String(100), nullable=False)  # "HR admin", "Manager", ...
    description = Column(String(300), nullable=True)
    permissions = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))  # list of permission keys
    is_system = Column(Boolean, nullable=False, server_default=text("false"))  # system roles can't be deleted
    # Data scope per permission key ("own", "direct_reports", "all_reports", "department", "location",
    # "legal_entity", "everyone"). A key without an entry means "everyone".
    scopes = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    # Sensitive fields of other people: {"salary": "hidden" | "masked" | "full", "bank": ..., "pan": ..., "aadhaar": ...}
    field_access = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    # A system role whose permissions were changed by the company: start-up no longer resets it.
    customised = Column(Boolean, nullable=False, server_default=text("false"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class EmployeeRole(Base):
    """Extra roles held by an employee, on top of the base role in employees.role."""
    __tablename__ = "employee_roles"
    __table_args__ = (
        UniqueConstraint("employee_id", "role_id", name="uq_employee_roles_employee_role"),
        Index("ix_employee_roles_company_id", "company_id"),
        Index("ix_employee_roles_role_id", "role_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    role_id = Column(Integer, ForeignKey("roles.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# Every permission in the product, grouped by module (people, time, payroll, talent,
# engage, insights, settings). Keys are checked by the API; labels are shown on screen.
ALL_PERMISSIONS = {
    # Overview
    "overview.view": "See the overview page",

    # People
    "people.employees.view": "See employees",
    "people.employees.manage": "Add, edit and deactivate employees",
    "people.departments.view": "See departments",
    "people.departments.manage": "Add and edit departments",
    "people.designations.view": "See designations",
    "people.designations.manage": "Add and edit designations",
    "people.employees.export": "Download the employee list",
    "people.sensitive.view": "See full PAN, Aadhaar, bank and ID numbers",
    "people.documents.verify": "Verify employee documents",
    "people.teams.manage": "Add and edit teams",
    "people.tags.manage": "Manage tags and tag people",
    "people.skills.manage": "Manage the skills library and confirm skills",

    # Time
    "time.roster.view": "See shifts and roster",
    "time.roster.manage": "Change shifts and roster",
    "time.attendance.view": "See attendance",
    "time.attendance.manage": "Correct and lock attendance",
    "time.attendance.settings": "Change shifts, attendance rules, devices and time settings",
    "time.face.manage": "Face attendance: enrol faces, review flagged face punches and change face settings",
    "time.timesheets.manage": "Manage clients and projects, and see everyone's timesheets",
    "time.leave.view": "See leave",
    "time.leave.approve": "Approve leave",

    # Payroll
    "payroll.view": "See payroll",
    "payroll.run": "Run payroll",
    "payroll.approve": "Approve payroll",

    # Talent
    "talent.hiring.manage": "Manage hiring",
    "talent.performance.manage": "Manage performance reviews",
    "talent.performance.admin": "Run review cycles, calibration, goals and performance settings",
    "talent.increments.manage": "Run pay reviews: increments and promotions",
    "talent.learning.manage": "Manage courses, assessments and training",

    # Engage
    "engage.announcements.manage": "Post announcements",

    # Workplace services (help desk, grievances, files, letters, tasks, assets)
    "workplace.helpdesk.manage": "Set up the help desk and see every case",
    "workplace.grievances.manage": "Set up grievance committees and handle grievances (not POSH)",
    "workplace.files.manage": "Manage company files, folders and policies",
    "workplace.letters.manage": "Design letter templates, generate letters and send them for signature",
    "workplace.tasks.manage": "See and manage everyone's tasks",
    "workplace.assets.manage": "Manage the asset register, assignments and returns",

    # Insights
    "insights.reports.view": "See reports",
    "insights.audit.view": "See the audit log",

    # Settings
    "settings.company.manage": "Change company settings",
    "settings.roles.manage": "Manage roles and access",
    "settings.notifications.manage": "Manage notifications and WhatsApp",
    "settings.masters.manage": "Manage lists used in dropdowns (masters)",
    "settings.organization.manage": "Manage legal entities, locations, grades and cost centres",
    "settings.custom_fields.manage": "Manage custom fields",
    "settings.numbering.manage": "Manage number series",
    "settings.workflows.manage": "Manage approval flows",
    "settings.rules.manage": "Manage rules and formulas",
}

_EVERYONE = ["overview.view", "time.roster.view", "time.attendance.view", "time.leave.view"]

# Default permissions for each system role.
DEFAULT_ROLE_PERMISSIONS = {
    "admin": list(ALL_PERMISSIONS.keys()),  # HR admin gets everything
    "hr": [
        *_EVERYONE,
        "people.employees.view", "people.employees.manage",
        "people.departments.view", "people.departments.manage",
        "people.designations.view", "people.designations.manage",
        "people.employees.export", "people.sensitive.view", "people.documents.verify",
        "people.teams.manage", "people.tags.manage", "people.skills.manage",
        "time.roster.manage", "time.attendance.manage", "time.leave.approve",
        "time.attendance.settings", "time.timesheets.manage", "time.face.manage",
        "payroll.view", "talent.hiring.manage", "talent.performance.manage",
        "talent.performance.admin", "talent.increments.manage", "talent.learning.manage",
        "engage.announcements.manage", "insights.reports.view", "insights.audit.view",
        "settings.masters.manage", "settings.custom_fields.manage",
        "workplace.helpdesk.manage", "workplace.grievances.manage", "workplace.files.manage",
        "workplace.letters.manage", "workplace.tasks.manage", "workplace.assets.manage",
    ],
    "manager": [
        *_EVERYONE,
        "people.employees.view", "people.departments.view", "people.designations.view",
        "time.roster.manage", "time.leave.approve", "talent.performance.manage",
        "insights.reports.view",
    ],
    "employee": [*_EVERYONE, "people.departments.view", "people.designations.view"],
}

SYSTEM_ROLES = {
    "admin": ("HR admin", "Full access to everything"),
    "hr": ("HR", "Runs people, time and payroll operations"),
    "manager": ("Manager", "Looks after a team: approvals, roster and reviews"),
    "employee": ("Employee", "Self-service: own profile, attendance and leave"),
}
