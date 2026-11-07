"""Fees: permissions, number series, approval request types, portal paths and the daily
reminder job.

fees.concession  a concession or scholarship for a learner; default flow: principal
fees.refund      a refund of advance, deposit or a receipt; default flow: principal
"""
from core.numbering import register_series
from core.security import register_portal_prefix
from core.workflow.registry import RequestType, register
from modules.roles.models import register_permissions

register_permissions(
    {
        "fees.view": "See fees: invoices, receipts, dues and reports",
        "fees.collect": "Collect fees at the counter and issue receipts",
        "fees.manage": "Set up fee heads and structures, make invoices, concessions, adjustments and the cash book",
        "fees.refund": "Request and pay out fee refunds; cancel receipts and mark cheques bounced",
        "fees.settings": "Change fee settings, late fee rules and online payment keys",
    },
    {
        "principal": ["fees.view"],
        "accountant": ["fees.view", "fees.collect", "fees.manage", "fees.refund", "fees.settings"],
        "hr": ["fees.view"],
    },
)

register_portal_prefix("/fees/portal")

register_series("fee_invoice", "Fee invoices", "INV", "{PREFIX}/{FY}/{SEQ:5}", "financial_year",
                description="Numbers on fee invoices (demands).")
register_series("fee_receipt", "Fee receipts", "RCT", "{PREFIX}/{FY}/{SEQ:5}", "financial_year",
                description="Numbers on fee receipts.")
register_series("fee_credit_note", "Fee credit notes", "CN", "{PREFIX}/{FY}/{SEQ:4}", "financial_year",
                description="Credit notes that reduce a fee invoice.")
register_series("fee_debit_note", "Fee debit notes", "DN", "{PREFIX}/{FY}/{SEQ:4}", "financial_year",
                description="Debit notes that add to a fee invoice.")
register_series("fee_refund", "Fee refunds", "RFD", "{PREFIX}/{FY}/{SEQ:4}", "financial_year",
                description="Fee refund vouchers.")
register_series("fee_voucher", "Cash book vouchers", "VCH", "{PREFIX}/{FY}/{SEQ:4}", "financial_year",
                description="Vouchers for other income and expenses in the cash book.")


def _svc():
    from modules.fees import service
    return service


def _summ_concession(instance) -> list[str]:
    p = instance.payload or {}
    lines = [f"{p.get('student')} ({p.get('admission_no')})", f"{p.get('name')}: {p.get('label')}"]
    if p.get("notes"):
        lines.append(f"Note: {p['notes']}")
    return lines


def _summ_refund(instance) -> list[str]:
    p = instance.payload or {}
    src = {"wallet": "advance balance", "deposit": "refundable deposit", "payment": "a receipt"}.get(p.get("source"), "")
    return [f"{p.get('student')} ({p.get('admission_no')})", f"Refund {p.get('amount')} from {src}",
            f"Reason: {p.get('reason')}"]


register(RequestType(
    key="fees.concession",
    label="Fee concession",
    description="A concession or scholarship given to a learner.",
    on_approved=lambda db, i: _svc().concession_decided(db, i, "approved"),
    on_rejected=lambda db, i: _svc().concession_decided(db, i, "rejected"),
    on_cancelled=lambda db, i: _svc().concession_decided(db, i, "cancelled"),
    summarize=_summ_concession,
    variables={"request.value": "Percentage or amount", "request.kind": "percent or fixed",
               "request.reason": "sibling, merit, staff_ward, need, sports, rte, other"},
    default_steps=[{"name": "Principal approval", "approver_type": "role", "approver_value": "principal", "mode": "any",
                    "sla_hours": 72}],
))

register(RequestType(
    key="fees.refund",
    label="Fee refund",
    description="A refund of advance, a refundable deposit or a receipt.",
    on_approved=lambda db, i: _svc().refund_decided(db, i, "approved"),
    on_rejected=lambda db, i: _svc().refund_decided(db, i, "rejected"),
    on_cancelled=lambda db, i: _svc().refund_decided(db, i, "cancelled"),
    summarize=_summ_refund,
    variables={"request.amount": "Refund amount", "request.source": "wallet, deposit or payment"},
    default_steps=[{"name": "Principal approval", "approver_type": "role", "approver_value": "principal", "mode": "any",
                    "sla_hours": 72}],
))


# ── Daily job: automatic fee reminders ──────────────────────────────
_LAST_RUN: dict[int, object] = {}


def daily_fee_job(db) -> int:
    """Once a day per institution (the periodic loop calls it every 15 minutes)."""
    from core.timeutil import now_for_company_id, today_for_company_id
    from modules.company.models import Company
    from modules.fees.models import FeeInvoice
    done = 0
    cids = {c for (c,) in db.query(FeeInvoice.company_id).distinct().all()}
    for (cid,) in db.query(Company.id).filter(Company.active.is_(True), Company.id.in_(cids or [0])).all():
        on = today_for_company_id(db, cid)
        if _LAST_RUN.get(cid) == on or now_for_company_id(db, cid).hour < 8:  # send in the morning, not at midnight
            continue
        db.info["company_id"] = cid
        try:
            with db.begin_nested():
                done += _svc().auto_reminders(db, cid, on)
        except Exception as exc:  # one institution never blocks the others
            import logging
            logging.getLogger("hrms").warning("Fee reminders failed for company %s: %s", cid, exc)
        _LAST_RUN[cid] = on
    db.info.pop("company_id", None)
    return done


try:
    from core import periodic
    periodic.register("fees.reminders", daily_fee_job)
except ImportError:  # pragma: no cover
    pass
