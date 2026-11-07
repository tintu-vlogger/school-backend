"""Every soft-deletable model registered with the recycle bin.

Models that existed before the bin are registered here; new modules register their own in
their hooks.py. A model with an archived_at / deleted_at / active column that is neither
registered nor listed in EXCLUDED is picked up automatically by `discover()` (shown under
"Other", restorable, never purged until someone reviews it), so nothing soft-deleted is
ever missing from the bin.

Purging (removing for good) is allowed only for set-up records that no law asks us to
keep. Records with legal or audit value are kept for good (`purgeable=False`), with the
reason shown on screen:
  - payroll, salary, tax and statutory set-up (Code on Wages, Income-tax Act: books and
    registers for at least 8 years; EPF/ESI records)
  - attendance, leave and shift rules (registers under the Shops and Establishments Acts
    and the OSH Code; past days are worked out with them)
  - employee records and documents, POSH committee records, asset register entries
"""
from __future__ import annotations

import importlib
import logging
import re

from core import recycle_bin as rb

logger = logging.getLogger("hrms")

PAYROLL = "Payroll history uses it (kept as part of wage and tax records)"
TIME = "Attendance and leave history uses it (kept for statutory registers)"
PEOPLE = "Employee records are kept for the legal retention period"
APPROVALS = "Past approvals refer to it"

# Tables with a soft-delete column that are NOT deletions, with the reason.
EXCLUDED = {
    "companies": "The company itself (platform administration)",
    "platform_users": "Platform staff accounts",
    "exits": "Exit cases are archived through the exit process and stay in the exit records",
    "employee_bank_accounts": "Old bank accounts are history, not deletions",
    "perquisites": "Payroll records",
    "statutory_overrides": "Payroll records",
    "grievance_committee_members": "Committee membership history (POSH records)",
    "ts_billing_rates": "Rate history for billing",
    "workflow_delegations": "Ended delegations are history",
}


def _model(path: str):
    mod, _, name = path.rpartition(".")
    try:
        return getattr(importlib.import_module(mod), name)
    except (ImportError, AttributeError):
        return None


def _reg(key: str, label: str, group: str, path: str, flag: str, **kw) -> None:
    m = _model(path)
    if m is None:
        return
    rb.register(rb.BinKind(key=key, label=label, group=group, model=m, flag=flag, **kw))


def _current(q):
    return q.filter(q.column_descriptions[0]["entity"].is_current.is_(True))


def _employee_restore(db, row) -> None:
    row.active = True
    row.archived_at = None
    row.archived_by = None


def _employee_where(q):
    from modules.employees.models import Employee
    # Only people archived before they ever joined; leavers are archived through exits.
    return q.filter(Employee.exit_date.is_(None))


def _name(row) -> str:
    first, last = getattr(row, "first_name", "") or "", getattr(row, "last_name", "") or ""
    return f"{first} {last}".strip()


def _short(text: str | None) -> str:
    t = re.sub(r"<[^>]+>", " ", text or "")
    t = " ".join(t.split())
    return t[:80] + ("…" if len(t) > 80 else "")


def register_all() -> None:
    org, lists, docs, time, pay, talent, engage, auto = (
        "Organisation", "Lists and fields", "Documents and letters", "Time and leave", "Payroll and expenses",
        "Talent", "Engagement and help desk", "Reports and automation")

    # People
    _reg("employee", "Employee (never joined)", "People", "modules.employees.models.Employee", "status",
         status_values=("archived",), restore_status="pre_joining", title=_name, subtitle=lambda r: r.employee_code,
         purgeable=False, keep_reason=PEOPLE, where=_employee_where, on_restore=_employee_restore,
         unique_fields=("employee_code",),
         hidden_fields=("password", "pan", "aadhaar", "passport_number", "uan", "esic_number", "custom"))
    _reg("employee_document", "Employee document", "People", "modules.employees.models.EmployeeDocument", "archived_at",
         purgeable=False, keep_reason=PEOPLE)

    # Organisation
    _reg("department", "Department", org, "modules.departments.models.Department", "active",
         title=lambda r: r.department_name, subtitle=lambda r: r.department_code,
         where=lambda q: q.filter(q.column_descriptions[0]["entity"].is_global.is_(False)),
         unique_fields=("department_name", "department_code"))
    _reg("legal_entity", "Legal entity", org, "modules.organization.models.LegalEntity", "active",
         purgeable=False, keep_reason="Statutory registrations and payroll history use it")
    _reg("region", "Region", org, "modules.organization.models.Region", "active")
    _reg("location", "Location", org, "modules.organization.models.Location", "active")
    _reg("cost_centre", "Cost centre", org, "modules.organization.models.CostCentre", "active")
    _reg("grade", "Grade", org, "modules.organization.models.Grade", "active")
    _reg("team", "Team", org, "modules.organization.models.Team", "active")
    _reg("tag", "Tag", org, "modules.organization.models.Tag", "active", unique_fields=())
    _reg("skill", "Skill", org, "modules.organization.models.Skill", "active", unique_fields=())

    # Lists and fields
    _reg("master_type", "List", lists, "core.masters.MasterType", "active", unique_fields=("key",))
    _reg("master_value", "List value", lists, "core.masters.MasterValue", "active",
         subtitle=lambda r: r.code, unique_fields=())
    _reg("custom_field_section", "Custom field section", lists, "core.custom_fields.CustomFieldSection", "active",
         unique_fields=())
    _reg("custom_field", "Custom field", lists, "core.custom_fields.CustomFieldDef", "active",
         subtitle=lambda r: r.key, unique_fields=())
    _reg("number_series", "Number series", lists, "core.numbering.NumberSeries", "active", unique_fields=())
    _reg("notification_template", "Notification template", lists,
         "modules.notifications.models.NotificationTemplate", "active",
         title=lambda r: r.subject or r.key, subtitle=lambda r: r.key, unique_fields=())
    _reg("hr_process_type", "HR process type", lists, "modules.hr_processes.models.HrProcessType", "active")

    # Documents and letters
    _reg("doc_folder", "Folder", docs, "modules.documents.models.DocFolder", "archived_at", unique_fields=())
    _reg("doc_file", "Company file", docs, "modules.documents.models.DocFile", "archived_at", unique_fields=())
    _reg("policy", "Policy document", docs, "modules.documents.models.Policy", "active", unique_fields=(),
         purgeable=False, keep_reason="Employees' acknowledgements of the policy are kept")
    _reg("letter_template", "Letter template", docs, "modules.documents.models.LetterTemplate", "active",
         unique_fields=())
    _reg("letterhead", "Letterhead", docs, "modules.documents.models.Letterhead", "active", unique_fields=())

    # Time and leave
    _reg("leave_type", "Leave type", time, "modules.leave.models.LeaveType", "active", purgeable=False, keep_reason=TIME)
    _reg("holiday_calendar", "Holiday calendar", time, "modules.leave.models.HolidayCalendar", "active",
         purgeable=False, keep_reason=TIME, unique_fields=())
    _reg("work_week", "Work week", time, "modules.leave.models.WorkWeekPattern", "active", purgeable=False,
         keep_reason=TIME, unique_fields=())
    _reg("shift", "Shift", time, "modules.shifts.models.Shift", "active", purgeable=False, keep_reason=TIME)
    _reg("shift_pattern", "Shift pattern", time, "modules.shifts.models.ShiftPattern", "active", purgeable=False,
         keep_reason=TIME, unique_fields=())
    _reg("attendance_rule", "Attendance rule", time, "modules.attendance.models.AttendanceRule", "active",
         where=_current, purgeable=False, keep_reason=TIME, unique_fields=())
    _reg("attendance_device", "Attendance device", time, "modules.attendance.models.Device", "active", unique_fields=())
    _reg("attendance_kiosk", "Attendance kiosk", time, "modules.attendance.models.Kiosk", "active", unique_fields=())
    _reg("geofence", "Office area (geofence)", time, "modules.attendance.models.Geofence", "active", unique_fields=())
    _reg("timesheet_client", "Timesheet client", time, "modules.timesheets.models.TsClient", "active",
         purgeable=False, keep_reason="Invoices and timesheets refer to it", unique_fields=())

    # Payroll and expenses
    _reg("salary_component", "Salary component", pay, "modules.payroll.models.SalaryComponent", "active",
         purgeable=False, keep_reason=PAYROLL)
    _reg("salary_structure", "Salary structure", pay, "modules.payroll.models.SalaryStructure", "active",
         where=_current, purgeable=False, keep_reason=PAYROLL)
    _reg("pay_group", "Pay group", pay, "modules.payroll.models.PayGroup", "active", purgeable=False, keep_reason=PAYROLL)
    _reg("expense_category", "Expense category", pay, "modules.expenses.models.ExpenseCategory", "active",
         purgeable=False, keep_reason="Expense claims refer to it (kept with the accounts)")
    _reg("loan_type", "Loan type", pay, "modules.loans.models.LoanType", "active", purgeable=False, keep_reason=PAYROLL)
    _reg("benefit_plan", "Benefit plan", pay, "modules.benefits.models.BenefitPlan", "active", purgeable=False,
         keep_reason=PAYROLL, unique_fields=())

    # Talent
    _reg("probation_policy", "Probation policy", talent, "modules.probation.models.ProbationPolicy", "active",
         unique_fields=())
    _reg("onboarding_flow", "Onboarding flow", talent, "modules.onboarding.models.OnboardingFlow", "active",
         unique_fields=())
    _reg("exit_flow", "Exit flow", talent, "modules.offboarding.models.ExitFlow", "active", unique_fields=())
    _reg("interview_kit", "Interview kit", talent, "modules.recruitment.models.InterviewKit", "active", unique_fields=())
    _reg("rating_scale", "Rating scale", talent, "modules.performance.models.RatingScale", "active", unique_fields=())
    _reg("competency", "Competency", talent, "modules.performance.models.Competency", "active", unique_fields=())
    _reg("kra", "Key result area", talent, "modules.performance.models.Kra", "active", unique_fields=())
    _reg("review_template", "Review template", talent, "modules.performance.models.ReviewTemplate", "active",
         unique_fields=())
    _reg("objective", "Goal", talent, "modules.performance.models.Objective", "archived_at", unique_fields=())
    _reg("learning_rule", "Learning rule", talent, "modules.learning.models.EnrolmentRule", "active", unique_fields=())
    _reg("learning_question", "Quiz question", talent, "modules.learning.models.Question", "active",
         title=lambda r: _short(r.body), unique_fields=())

    # Engagement and help desk
    _reg("announcement", "Announcement", engage, "modules.engagement.models.Announcement", "status",
         status_values=("archived",), restore_status="published", unique_fields=())
    _reg("buzz_post", "Buzz post", engage, "modules.engagement.models.BuzzPost", "deleted_at",
         title=lambda r: _short(r.body) or "Post", unique_fields=())
    _reg("buzz_comment", "Buzz comment", engage, "modules.engagement.models.BuzzComment", "deleted_at",
         title=lambda r: _short(r.body) or "Comment", unique_fields=())
    _reg("kudos_value", "Kudos value", engage, "modules.engagement.models.KudosValue", "active", unique_fields=())
    _reg("engage_kiosk", "Kiosk screen", engage, "modules.engagement.models.Kiosk", "active", unique_fields=())
    _reg("grievance_committee", "Grievance committee", engage, "modules.grievances.models.GrievanceCommittee", "active",
         purgeable=False, keep_reason="Committee records under the POSH Act are kept", unique_fields=())
    _reg("helpdesk_category", "Help desk category", engage, "modules.helpdesk.models.HelpdeskCategory", "active",
         unique_fields=())
    _reg("helpdesk_reply", "Saved reply", engage, "modules.helpdesk.models.HelpdeskCannedResponse", "active",
         unique_fields=())
    _reg("helpdesk_sla", "Response time rule", engage, "modules.helpdesk.models.HelpdeskSla", "active",
         title=lambda r: f"{(r.priority or '').capitalize()} priority: reply in {r.response_minutes} min",
         unique_fields=())
    _reg("asset", "Asset", "Assets", "modules.assets.models.Asset", "status", status_values=("retired",),
         restore_status="available", subtitle=lambda r: r.asset_tag, purgeable=False,
         keep_reason="Kept in the fixed asset register", unique_fields=("asset_tag",))

    # Reports and automation
    _reg("dashboard", "Dashboard", auto, "modules.analytics.models.Dashboard", "archived_at", unique_fields=())
    _reg("report", "Saved report", auto, "modules.reports.models.Report", "archived_at", unique_fields=())
    _reg("report_schedule", "Report schedule", auto, "modules.reports.models.ReportSchedule", "active", unique_fields=())
    _reg("automation_rule", "Automation rule", auto, "modules.automation.models.AutomationRule", "archived_at",
         unique_fields=())
    _reg("webhook", "Webhook", auto, "modules.automation.models.AutomationWebhook", "archived_at", unique_fields=())
    _reg("approval_flow", "Approval flow", auto, "core.workflow.models.WorkflowDefinition", "active", where=_current,
         purgeable=False, keep_reason=APPROVALS, unique_fields=())


def _humanise(name: str) -> str:
    words = re.sub(r"(?<!^)(?=[A-Z])", " ", name).lower()
    return words[:1].upper() + words[1:]


def discover() -> list[str]:
    """Register (as "Other") every soft-deletable model that has not declared itself.
    Returns the tables added."""
    from core.db import Base
    known = set(rb.by_table()) | set(EXCLUDED)
    added = []
    for mapper in Base.registry.mappers:
        t = mapper.local_table
        cols = {c.name for c in t.c}
        if t.name in known or not {"company_id", "uuid", "id"} <= cols:
            continue
        flag = next((f for f in ("archived_at", "deleted_at", "active") if f in cols), None)
        if not flag:
            continue
        key = f"auto_{t.name}"
        rb.register(rb.BinKind(key=key, label=_humanise(mapper.class_.__name__), group="Other", model=mapper.class_,
                               flag=flag, purgeable=False, keep_reason="Not yet reviewed for removal",
                               where=_current if "is_current" in cols else None, unique_fields=()))
        added.append(t.name)
    return added
