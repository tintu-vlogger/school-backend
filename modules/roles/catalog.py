"""How permissions are presented and scoped: groups for the permission matrix, which
permissions take a data scope, the sensitive fields whose visibility a role controls, and
the defaults for the system roles."""

GROUPS = [
    ("academics", "Academics", "Sessions, programs and classes, sections, subjects, timetable"),
    ("students", "Students", "Students, guardians, enrolment, promotion, admissions and certificates"),
    ("learning", "Teaching & learning", "Student attendance, homework, materials, online classes and quizzes"),
    ("exams", "Exams & results", "Exams, grading schemes, marks, report cards and transcripts"),
    ("fees", "Fees & finance", "Fee structures, invoices, collections, concessions and refunds"),
    ("campus", "Campus services", "Library, transport, hostel, health, discipline and visitors"),
    ("comms", "Communication", "Notices, messages, events and parent-teacher meetings"),
    ("portal", "Parent & student portal", "What parents and students can see and do"),
    ("people", "Staff", "Staff, departments, documents, letters, tasks and assets"),
    ("time", "Time", "Attendance, shifts, timesheets and leave"),
    ("payroll", "Payroll", "Pay runs, salaries, tax, statutory, expenses and loans"),
    ("talent", "Talent", "Hiring, onboarding, probation, performance, learning and exits"),
    ("engage", "Engage", "Buzz, announcements, surveys, help desk and grievances"),
    ("insights", "Insights", "Reports, dashboards, data and the audit log"),
    ("settings", "Settings", "Company set-up, roles, lists, flows and integrations"),
]

# Keys whose group is not given by their prefix.
_GROUP_OF = {
    "overview.view": "people",
    "workplace.files.manage": "people",
    "workplace.letters.manage": "people",
    "workplace.tasks.manage": "people",
    "workplace.assets.manage": "people",
    "workplace.helpdesk.manage": "engage",
    "workplace.grievances.manage": "engage",
    "expenses.manage": "payroll",
    "loans.manage": "payroll",
    "settings.leave.manage": "time",
}
_PREFIX_GROUP = {"academics": "academics", "timetable": "academics", "students": "students",
                 "admissions": "students", "attendance": "learning", "lms": "learning", "exams": "exams",
                 "fees": "fees", "library": "campus", "transport": "campus", "hostel": "campus",
                 "wellbeing": "campus", "comms": "comms", "portal": "portal",
                 "people": "people", "time": "time", "payroll": "payroll", "talent": "talent",
                 "engage": "engage", "insights": "insights", "settings": "settings"}


def group_of(key: str) -> str:
    if key in _GROUP_OF:
        return _GROUP_OF[key]
    return _PREFIX_GROUP.get(key.split(".", 1)[0], "settings")


SCOPES = [
    ("own", "Own records"),
    ("direct_reports", "Direct reports"),
    ("all_reports", "All reports (whole team below)"),
    ("department", "Own department"),
    ("location", "Own location"),
    ("legal_entity", "Own legal entity"),
    ("everyone", "Everyone"),
]
SCOPE_KEYS = [k for k, _ in SCOPES]
# Order used when a person holds several roles: the widest scope wins.
SCOPE_RANK = {k: i for i, k in enumerate(SCOPE_KEYS)}

# Permissions about other people's records, where "whose records" makes sense.
SCOPABLE = {
    "people.employees.view", "people.employees.manage", "people.documents.verify", "people.sensitive.view",
    "time.roster.view", "time.roster.manage", "time.attendance.view", "time.attendance.manage",
    "time.timesheets.manage", "time.leave.view", "time.leave.approve", "time.leave.manage",
    "payroll.view", "payroll.salaries.manage", "expenses.manage", "loans.manage",
    "talent.performance.manage", "talent.probation.manage", "talent.onboarding.manage", "talent.exits.manage",
    "talent.learning.manage", "workplace.tasks.manage", "workplace.assets.manage",
    "insights.reports.view", "insights.reports.all_data",
}

FIELDS = [
    ("salary", "Salary and pay", "CTC, pay slips and salary revisions"),
    ("bank", "Bank details", "Account numbers and IFSC"),
    ("pan", "PAN", "Permanent account number"),
    ("aadhaar", "Aadhaar", "Aadhaar number"),
]
FIELD_KEYS = [k for k, _, _ in FIELDS]
FIELD_LEVELS = [("hidden", "Hidden"), ("masked", "Masked"), ("full", "Full")]
FIELD_RANK = {"hidden": 0, "masked": 1, "full": 2}

DEFAULT_SCOPES = {
    "admin": {},
    "hr": {},
    "manager": {
        "time.roster.manage": "all_reports", "time.leave.approve": "all_reports",
        "talent.performance.manage": "all_reports", "insights.reports.view": "all_reports",
        "time.attendance.view": "all_reports", "time.leave.view": "all_reports",
    },
    "employee": {"time.attendance.view": "own", "time.leave.view": "own", "time.roster.view": "department"},
}

DEFAULT_FIELD_ACCESS = {
    "admin": {f: "full" for f in FIELD_KEYS},
    "hr": {f: "full" for f in FIELD_KEYS},
    "manager": {"salary": "hidden", "bank": "hidden", "pan": "masked", "aadhaar": "masked"},
    "employee": {"salary": "hidden", "bank": "hidden", "pan": "hidden", "aadhaar": "hidden"},
}
_STAFF_OWN = {"time.attendance.view": "own", "time.leave.view": "own", "time.roster.view": "department"}
_HIDDEN = {f: "hidden" for f in FIELD_KEYS}
for _role in ("teacher", "accountant", "librarian"):
    DEFAULT_SCOPES[_role] = dict(_STAFF_OWN)
    DEFAULT_FIELD_ACCESS[_role] = dict(_HIDDEN)
DEFAULT_SCOPES["principal"] = {}
DEFAULT_FIELD_ACCESS["principal"] = {"salary": "hidden", "bank": "hidden", "pan": "masked", "aadhaar": "masked"}
for _role in ("parent", "student"):
    DEFAULT_SCOPES[_role] = {}
    DEFAULT_FIELD_ACCESS[_role] = dict(_HIDDEN)

# A custom role starts with nothing sensitive visible.
NEW_ROLE_FIELD_ACCESS = {"salary": "hidden", "bank": "hidden", "pan": "masked", "aadhaar": "masked"}


def catalog() -> dict:
    """Everything the roles screen needs to draw the permission matrix."""
    from modules.roles.models import ALL_PERMISSIONS
    groups = {k: {"key": k, "label": label, "hint": hint, "permissions": []} for k, label, hint in GROUPS}
    for key, label in ALL_PERMISSIONS.items():
        groups[group_of(key)]["permissions"].append({"key": key, "label": label, "scopable": key in SCOPABLE})
    return {
        "groups": [g for g in groups.values() if g["permissions"]],
        "scopes": [{"key": k, "label": label} for k, label in SCOPES],
        "fields": [{"key": k, "label": label, "hint": hint} for k, label, hint in FIELDS],
        "field_levels": [{"key": k, "label": label} for k, label in FIELD_LEVELS],
        "total": len(ALL_PERMISSIONS),
    }
