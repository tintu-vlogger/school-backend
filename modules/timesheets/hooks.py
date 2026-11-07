"""Weekly timesheet approval and timesheet reminders."""
from core.workflow.registry import RequestType, register
from modules.timesheets import service


def _summarize(instance) -> list[str]:
    p = instance.payload or {}
    return [f"Week: {p.get('week_start')} to {p.get('week_end')}", f"{p.get('hours')} hours in {p.get('lines')} entries",
            "Open Timesheets > Approvals to approve line by line"]


register(RequestType(
    key="timesheets.timesheet", label="Timesheet", description="A week of time logged against projects.",
    on_approved=service.on_approved, on_rejected=service.on_rejected, on_cancelled=service.on_cancelled,
    summarize=_summarize, variables={"request.hours": "Hours in the week", "request.lines": "Number of entries"},
    default_steps=[{"name": "Manager approval", "approver_type": "reporting_manager", "mode": "any", "sla_hours": 72}],
))

try:
    from core import periodic
    periodic.register("timesheets.reminders", service.reminders_job)
except ImportError:
    pass
