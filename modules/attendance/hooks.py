"""Approval request types of the attendance module and its periodic jobs."""
from core.workflow.registry import RequestType, register
from modules.attendance import requests as att_requests

_MANAGER = [{"name": "Manager approval", "approver_type": "reporting_manager", "mode": "any", "sla_hours": 48,
             "skip_if_requester": True}]
_VARIABLES = {"request.days": "Number of days", "request.minutes": "Minutes (permission and overtime)",
              "request.date": "Date (YYYY-MM-DD)", "request.duration": "full_day, first_half, second_half or hours"}

for kind, label, description in (
    ("regularisation", "Attendance correction", "Fix a missed or wrong punch."),
    ("on_duty", "On duty", "Working away from the office: client visit, field work, training."),
    ("wfh", "Work from home", "Working from home for a day, a half day or a few days."),
    ("permission", "Permission hours", "A short time off in hours, within the monthly limit."),
    ("overtime", "Overtime pre-approval", "Approval and the worker's consent before working overtime."),
):
    register(RequestType(
        key=f"attendance.{kind}", label=label, description=description,
        on_approved=att_requests.on_approved, on_rejected=att_requests.on_rejected,
        on_cancelled=att_requests.on_cancelled, summarize=att_requests.summarize,
        variables=_VARIABLES, default_steps=_MANAGER,
    ))

def _summ_absence(instance) -> list[str]:
    p = instance.payload or {}
    return [f"Absent on {p.get('date')} ({p.get('days')} day)",
            "When approved, it is covered by leave from the balance, else kept as loss of pay."]


register(RequestType(
    key="attendance.absence_leave", label="Absence to leave",
    description="An absent day turned into leave from the employee's balance, or loss of pay.",
    on_approved=lambda db, i: _leave_link().on_absence_approved(db, i),
    on_rejected=lambda db, i: _leave_link().on_absence_closed(db, i),
    on_cancelled=lambda db, i: _leave_link().on_absence_closed(db, i),
    summarize=_summ_absence, variables=_VARIABLES, default_steps=_MANAGER,
))


def _leave_link():
    from modules.attendance import leave_link
    return leave_link


try:  # shared periodic job runner (every 15 minutes in the API process)
    from core import periodic
    from modules.attendance import devices, service

    periodic.register("attendance.daily", service.process_yesterday)
    periodic.register("attendance.ftp_pull", devices.poll_due_devices)
    from modules.attendance import device_sources
    periodic.register("attendance.device_pull", device_sources.poll_due)
    periodic.register("attendance.selfie_retention", service.purge_old_selfies)
    periodic.register("attendance.leave_link", lambda db: _leave_link().daily_job(db))
except ImportError:
    pass
