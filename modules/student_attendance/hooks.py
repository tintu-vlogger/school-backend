"""Permissions, the parent/student leave request type, portal paths and the alerts job of
the student attendance module."""
from core import periodic
from core.security import register_portal_prefix
from core.workflow.registry import RequestType, register
from modules.roles.models import register_permissions

register_permissions(
    {
        "attendance.view": "See learner attendance of every section and its reports",
        "attendance.mark": "Mark attendance for the sections and periods you teach",
        "attendance.mark.any": "Mark attendance for any section",
        "attendance.correct": "Change locked attendance registers (logged with a reason)",
        "attendance.leave.manage": "See all learner leave requests and record leave for learners",
        "attendance.settings": "Set up attendance statuses, rules, alerts and the institution calendar",
    },
    {
        "principal": ["attendance.view", "attendance.mark", "attendance.mark.any", "attendance.correct",
                      "attendance.leave.manage", "attendance.settings"],
        "hr": ["attendance.view", "attendance.mark", "attendance.mark.any", "attendance.leave.manage"],
        "manager": ["attendance.view", "attendance.mark"],
        "teacher": ["attendance.mark"],
    },
)

register_portal_prefix("/student-attendance/portal")


def _svc():
    from modules.student_attendance import service
    return service


def _summ(instance) -> list[str]:
    p = instance.payload or {}
    lines = [f"{p.get('student_name')} ({p.get('section_label') or 'no section'})",
             f"{p.get('status_name') or 'Leave'}: {p.get('from_date')} to {p.get('to_date')}, {p.get('days')} day(s)"]
    if p.get("reason"):
        lines.append(f"Reason: {p['reason']}")
    if p.get("requested_by"):
        lines.append(f"Asked by {p['requested_by']}")
    return lines


register(RequestType(
    key="attendance.student_leave",
    label="Learner leave request",
    description="A parent or learner asks for leave; the class teacher approves it.",
    on_approved=lambda db, i: _svc().on_leave_approved(db, i),
    on_rejected=lambda db, i: _svc().on_leave_rejected(db, i),
    on_cancelled=lambda db, i: _svc().on_leave_cancelled(db, i),
    summarize=_summ,
    variables={"request.days": "Number of school days", "request.status_code": "Kind of leave (EX, ML ...)",
               "request.class_teacher_id": "Class teacher of the learner's section"},
    default_steps=[{"name": "Class teacher", "approver_type": "field", "approver_value": "request.class_teacher_id",
                    "mode": "any", "sla_hours": 48}],
))

periodic.register("student_attendance.alerts", lambda db: _svc().alerts_job(db))
