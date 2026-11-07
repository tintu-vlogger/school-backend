"""Talent lifecycle registrations: permissions for hiring, offers, onboarding, probation
and exits; master lists and number series they use; approval request types for hiring;
background jobs (interview reminders, follow-ups, offer expiry, referral bonuses,
candidate data purge).

recruitment.requisition  a manager's hiring request (default: department head, then HR)
recruitment.offer        an offer outside the salary band (default: HR admin)
"""
from core import periodic
from core.workflow.registry import RequestType, register
from modules.roles.models import ALL_PERMISSIONS, DEFAULT_ROLE_PERMISSIONS

TALENT_PERMISSIONS = {
    "talent.offers.manage": "Prepare, approve and send offers",
    "talent.onboarding.manage": "Run onboarding: pre-joining portal, joining documents and checklists",
    "talent.probation.manage": "Manage probation reviews, extensions and confirmations",
    "talent.exits.manage": "Manage exits: clearances, settlement handoff, letters and alumni access",
}
for _k, _v in TALENT_PERMISSIONS.items():
    ALL_PERMISSIONS.setdefault(_k, _v)
for _role, _perms in (("admin", list(TALENT_PERMISSIONS)), ("hr", list(TALENT_PERMISSIONS))):
    _lst = DEFAULT_ROLE_PERMISSIONS.setdefault(_role, [])
    for _p in _perms:
        if _p not in _lst:
            _lst.append(_p)

# Master lists (seeded for every company with the others)
try:
    from core.master_seed import MASTERS
    MASTERS.setdefault("rejection_reason", (
        "Candidate rejection reason", "Why a candidate does not move ahead", False,
        [("Skills do not match", "skills"), ("Not enough experience", "experience"), ("Salary expectation too high", "salary"),
         ("Notice period too long", "notice"), ("Did not clear the interview", "interview"),
         ("Candidate withdrew", "withdrew"), ("Position filled", "filled"), ("Other", "other")]))
    MASTERS.setdefault("offer_decline_reason", (
        "Offer decline reason", "Why a candidate declines an offer", False,
        [("Better offer elsewhere", "better_offer"), ("Counter-offer from current employer", "counter"),
         ("Salary", "salary"), ("Location", "location"), ("Role or growth", "role"), ("Personal reasons", "personal"),
         ("Other", "other")]))
    MASTERS.setdefault("clearance_department", (
        "Clearance department", "Departments that clear a leaver", False,
        [("IT", "it"), ("Admin", "admin"), ("Finance", "finance"), ("Reporting manager", "manager"),
         ("Assets", "assets"), ("HR", "hr")]))
except ImportError:  # pragma: no cover
    pass

# Number series
try:
    from core.numbering import DEFAULT_SERIES
    _have = {s[0] for s in DEFAULT_SERIES}
    for _row in (
        ("requisition", "Hiring requests", "HR", "{PREFIX}-{FY}-{SEQ:4}", "financial_year", True, "Hiring request numbers."),
        ("job", "Job openings", "JOB", "{PREFIX}{SEQ:4}", "never", True, "Job opening codes on the careers page."),
        ("offer", "Offers", "OFR", "{PREFIX}/{FY}/{SEQ:4}", "financial_year", True, "Offer reference numbers."),
        ("exit", "Exits", "EXT", "{PREFIX}/{FY}/{SEQ:4}", "financial_year", True, "Exit case numbers."),
    ):
        if _row[0] not in _have:
            DEFAULT_SERIES.append(_row)
except ImportError:  # pragma: no cover
    pass


def _svc():
    from modules.recruitment import service
    return service


def _summ_req(instance) -> list[str]:
    p = instance.payload or {}
    lines = [f"{p.get('openings')} opening(s) · {'Replacement' if p.get('kind') == 'replacement' else 'New position'}"]
    if p.get("department"):
        lines.append(p["department"])
    if p.get("budget_min") or p.get("budget_max"):
        lines.append(f"Budget ₹{p.get('budget_min') or '—'} to ₹{p.get('budget_max') or '—'} a year")
    if p.get("justification"):
        lines.append(p["justification"][:200])
    return lines


def _summ_offer(instance) -> list[str]:
    p = instance.payload or {}
    return [f"{p.get('candidate')} · {p.get('job')}", f"CTC ₹{p.get('annual_ctc')} a year",
            f"Band ₹{p.get('band_min') or '—'} to ₹{p.get('band_max') or '—'}", f"Joining {p.get('joining_date')}"]


register(RequestType(
    key="recruitment.requisition",
    label="Hiring request",
    description="A manager asks for a new or replacement position with a salary range.",
    on_approved=lambda db, i: _svc().on_requisition_decided(db, i, "approved"),
    on_rejected=lambda db, i: _svc().on_requisition_decided(db, i, "rejected"),
    on_cancelled=lambda db, i: _svc().on_requisition_decided(db, i, "cancelled"),
    summarize=_summ_req,
    variables={"request.openings": "Number of openings", "request.budget_max": "Maximum annual CTC",
               "request.kind": "new or replacement", "request.department_id": "Department"},
    default_steps=[{"name": "Department head", "approver_type": "department_head", "mode": "any",
                    "skip_if_requester": True},
                   {"name": "HR approval", "approver_type": "role", "approver_value": "hr", "mode": "any"}],
))

register(RequestType(
    key="recruitment.offer",
    label="Offer outside salary band",
    description="An offer whose CTC is outside the job's salary band needs approval before it is sent.",
    on_approved=lambda db, i: _svc().on_offer_decided(db, i, "approved"),
    on_rejected=lambda db, i: _svc().on_offer_decided(db, i, "rejected"),
    on_cancelled=lambda db, i: _svc().on_offer_decided(db, i, "cancelled"),
    summarize=_summ_offer,
    variables={"request.annual_ctc": "Offered annual CTC", "request.band_max": "Top of the band"},
    default_steps=[{"name": "HR admin approval", "approver_type": "role", "approver_value": "admin", "mode": "any"}],
))


periodic.register("recruitment.interview_reminders", lambda db: _svc().run_interview_reminders(db))
periodic.register("recruitment.follow_ups", lambda db: _svc().run_follow_ups(db))
periodic.register("recruitment.offer_expiry", lambda db: _svc().expire_offers(db))
periodic.register("recruitment.referral_bonus", lambda db: _svc().run_referral_eligibility(db))
periodic.register("recruitment.candidate_purge", lambda db: _svc().purge_due(db))
