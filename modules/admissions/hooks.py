"""Permissions, number series and the offer approval of the admissions module.

admissions.offer   an admission offer to an applicant. No default flow: without a flow set up
                   in Settings > Approval flows the offer goes out at once.
"""
from core.numbering import register_series
from core.workflow.registry import RequestType, register
from modules.roles.models import register_permissions

register_permissions(
    {
        "admissions.view": "See admission enquiries, applications and the admissions dashboard",
        "admissions.manage": "Add and work on enquiries and applications: follow-ups, tests, interviews, scores, offers",
        "admissions.enrol": "Turn accepted applicants into students and enrol them",
        "admissions.settings.manage": "Set up admissions: programs open, seats, fees, documents, online form",
        "admissions.certificates.view": "See the certificate register",
        "admissions.certificates.manage": "Issue and cancel certificates and edit certificate templates",
    },
    {
        "principal": ["admissions.view", "admissions.manage", "admissions.enrol", "admissions.settings.manage",
                      "admissions.certificates.view", "admissions.certificates.manage"],
        "hr": ["admissions.view", "admissions.manage", "admissions.enrol", "admissions.settings.manage",
               "admissions.certificates.view", "admissions.certificates.manage"],
        "accountant": ["admissions.view"],
        "manager": ["admissions.view", "admissions.certificates.view"],
    },
)

register_series("enquiry", "Admission enquiries", "ENQ", "{PREFIX}{YY}{SEQ:4}", "yearly", True,
                "Numbers for admission enquiries (leads).")
register_series("admission_application", "Admission applications", "APP", "{PREFIX}{YYYY}{SEQ:4}", "yearly", True,
                "Application numbers given to every admission application, online or at the office.")


def _svc():
    from modules.admissions import service
    return service


def _summ_offer(instance) -> list[str]:
    p = instance.payload or {}
    lines = [f"{p.get('applicant')}: {p.get('program')} ({p.get('session')})"]
    if p.get("merit_rank"):
        lines.append(f"Merit rank {p['merit_rank']}" + (f", score {p['merit_score']}" if p.get("merit_score") else ""))
    if p.get("seats"):
        lines.append(f"Seats: {p.get('seats_taken')} of {p['seats']} taken")
    if p.get("note"):
        lines.append(p["note"])
    return lines


register(RequestType(
    key="admissions.offer",
    label="Admission offer",
    description="An offer of admission to an applicant.",
    on_approved=lambda db, i: _svc().on_offer_approved(db, i),
    on_rejected=lambda db, i: _svc().on_offer_closed(db, i, "rejected"),
    on_cancelled=lambda db, i: _svc().on_offer_closed(db, i, "cancelled"),
    summarize=_summ_offer,
    variables={"request.merit_rank": "Merit rank", "request.merit_score": "Merit score",
               "request.program_id": "Program id", "request.fee": "Application fee"},
    default_steps=[],
))
