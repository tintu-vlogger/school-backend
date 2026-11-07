"""Shift change and shift swap requests."""
from core.workflow.registry import RequestType, register
from modules.shifts import requests as shift_requests

register(RequestType(
    key="shifts.change", label="Shift change", description="An employee asks to work another shift on some dates.",
    on_approved=shift_requests.on_change_approved, summarize=shift_requests.summarize_change,
    variables={"request.shift_name": "Shift asked for", "request.start_date": "First date"},
    default_steps=[{"name": "Manager approval", "approver_type": "reporting_manager", "mode": "any", "sla_hours": 48}],
))

register(RequestType(
    key="shifts.swap", label="Shift swap", description="Two colleagues swap their shifts on a day.",
    on_approved=shift_requests.on_swap_approved, summarize=shift_requests.summarize_swap,
    variables={"request.counterpart_employee_id": "Colleague swapping", "request.date": "Date"},
    default_steps=[
        {"name": "Colleague accepts", "approver_type": "field", "approver_value": "request.counterpart_employee_id",
         "mode": "any", "sla_hours": 24},
        {"name": "Manager approval", "approver_type": "reporting_manager", "mode": "any", "sla_hours": 48},
    ],
))
