"""Salary revisions waiting for payroll's review.

Pay reviews (modules/performance) call `service.apply_salary_revision(...)` when an
increment or promotion takes effect. Each call becomes a pending revision here: payroll
sees the new CTC split with the employee's structure, the arrears the change would pay for
months already locked, and applies it (an effective-dated salary; the next pay run adds the
arrears by itself) or rejects it with a reason. With `revisions.auto_apply` on, revisions
are applied at once.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from core import audit
from core.errors import BadRequest, NotFound
from modules.payroll import calc, engine, structures
from modules.payroll import service as svc
from modules.payroll.calc import ZERO, d, money, rnd
from modules.payroll.models import EmployeeSalary, PayRun, Payslip, SalaryRevisionRequest, SalaryStructure

STATUS = {"pending": "Waiting for review", "applied": "Applied", "rejected": "Rejected"}
SOURCES = {"pay_review": "Pay review", "manual": "Entered by payroll"}


def _now():
    return datetime.now(timezone.utc)


def get(db: Session, company_id: int, uuid) -> SalaryRevisionRequest:
    r = db.query(SalaryRevisionRequest).filter(SalaryRevisionRequest.company_id == company_id,
                                               SalaryRevisionRequest.uuid == uuid).first()
    if not r:
        raise NotFound("Salary revision")
    return r


def _is_promotion(db: Session, company_id: int, employee_id: int, on: date) -> bool:
    try:
        from modules.employees.models import JobChange
    except ImportError:  # pragma: no cover
        return False
    return bool(db.query(JobChange.id).filter(JobChange.company_id == company_id, JobChange.employee_id == employee_id,
                                              JobChange.change_type == "promotion", JobChange.effective_date == on,
                                              JobChange.status != "cancelled").first())


def request(db: Session, company_id: int, employee_id: int, new_ctc: Decimal, effective_from: date, *,
            source: str = "pay_review", source_ref: str | None = None, reason: str | None = None,
            note: str | None = None) -> SalaryRevisionRequest:
    """Queue a revision (once per source and date). Returns the existing one when repeated."""
    new_ctc = d(new_ctc)
    if new_ctc <= 0:
        raise BadRequest("The new CTC must be above zero.")
    q = db.query(SalaryRevisionRequest).filter(SalaryRevisionRequest.company_id == company_id,
                                               SalaryRevisionRequest.employee_id == employee_id,
                                               SalaryRevisionRequest.effective_from == effective_from,
                                               SalaryRevisionRequest.status != "rejected")
    existing = q.filter(SalaryRevisionRequest.source_ref == source_ref).first() if source_ref else \
        q.filter(SalaryRevisionRequest.new_ctc == new_ctc).first()
    if existing:
        return existing
    cur = svc.current_salary(db, company_id, employee_id, effective_from)
    if reason is None:
        reason = "promotion" if _is_promotion(db, company_id, employee_id, effective_from) else "annual_raise"
    r = SalaryRevisionRequest(company_id=company_id, employee_id=employee_id, source=source, source_ref=source_ref,
                              reason=reason, effective_from=effective_from, current_ctc=d(cur.annual_ctc) if cur else ZERO,
                              new_ctc=new_ctc, structure_id=cur.structure_id if cur else None, note=note, status="pending")
    db.add(r)
    db.flush()
    try:
        with db.begin_nested():
            r.arrears_estimate = d(estimate(db, company_id, r)["total"])
    except Exception:  # an estimate that cannot be made must not stop the request
        r.arrears_estimate = ZERO
    emp = svc.get_employee(db, company_id, employee_id)
    audit.event(db, "payroll.revision_requested", "salary_revision_requests", r.id,
                {"employee_id": employee_id, "new_ctc": money(new_ctc), "effective_from": effective_from.isoformat(),
                 "source": source}, entity_uuid=str(r.uuid), entity_label=svc.name_of(emp))
    if (svc.get_settings(db, company_id).get("revisions") or {}).get("auto_apply"):
        apply(db, company_id, r)
    return r


def _structure_uuid(db: Session, r: SalaryRevisionRequest, structure_uuid=None):
    if structure_uuid:
        return structure_uuid
    if r.structure_id:
        s = db.query(SalaryStructure).filter(SalaryStructure.id == r.structure_id).first()
        if s:
            cur = db.query(SalaryStructure).filter(SalaryStructure.lineage_id == s.lineage_id,
                                                   SalaryStructure.is_current.is_(True)).first()
            return str((cur or s).uuid)
    return None


def preview(db: Session, company_id: int, r: SalaryRevisionRequest, structure_uuid=None) -> dict:
    emp = svc.get_employee(db, company_id, r.employee_id)
    data = {"annual_ctc": r.new_ctc, "effective_from": r.effective_from,
            "structure_uuid": _structure_uuid(db, r, structure_uuid)}
    if not data["structure_uuid"]:
        cur = svc.current_salary(db, company_id, r.employee_id, r.effective_from)
        if cur is None:
            raise BadRequest("This employee has no salary yet; assign one first.")
        # no structure: scale the current amounts to the new CTC
        factor = d(r.new_ctc) / d(cur.annual_ctc) if d(cur.annual_ctc) else Decimal(1)
        data["overrides"] = {c["code"]: rnd(d(c["monthly"]) * factor) for c in cur.components or []
                             if c.get("kind") in ("earning", "reimbursement")}
    return svc.preview_salary(db, company_id, emp, data)


def estimate(db: Session, company_id: int, r: SalaryRevisionRequest, structure_uuid=None) -> dict:
    """Arrears the revision would pay: for every locked month on or after the effective date,
    what the new salary earns minus what was paid (same paid days)."""
    emp = svc.get_employee(db, company_id, r.employee_id)
    pv = preview(db, company_id, r, structure_uuid)
    new_amounts = {c["code"]: d(c["monthly"]) for c in pv["components"]
                   if c.get("kind") in ("earning", "reimbursement", "employer") and c["code"] not in ("PF_ER", "ESI_ER", "GRATUITY")}
    from modules.payroll import runs
    comps = runs.comp_attrs(db, company_id)
    slips = (db.query(Payslip).join(PayRun, PayRun.id == Payslip.pay_run_id)
             .filter(Payslip.company_id == company_id, Payslip.employee_id == r.employee_id, PayRun.kind == "regular",
                     PayRun.status.in_(("locked", "paid")), Payslip.period_end >= r.effective_from,
                     Payslip.status != "skipped").order_by(Payslip.period_start).all())
    months, total = [], ZERO
    settings = svc.get_settings(db, company_id)
    for p in slips:
        old = svc.salaries_in(db, company_id, [r.employee_id], p.period_start, p.period_end).get(r.employee_id) or []
        segs = [s for s in runs.segments_for(old, p.period_start, p.period_end) if s.start < r.effective_from]
        segs = [engine.Segment(start=s.start, end=min(s.end, r.effective_from - timedelta(days=1)), amounts=s.amounts) for s in segs]
        segs.append(engine.Segment(start=max(p.period_start, r.effective_from), end=p.period_end, amounts=new_amounts))
        res = engine.compute(engine.EmployeeInput(
            employee_id=r.employee_id, period_start=p.period_start, period_end=p.period_end, segments=segs, comps=comps,
            rules={"epf": {}, "esi": {}, "wage_rule": {}}, settings=settings, join_date=emp.date_of_joining,
            exit_date=emp.exit_date, lop_days=d(p.lop_days), skip_statutory=True))
        should = sum((d(x["amount"]) for x in res["earnings"] if comps.get(x["code"], {}).get("kind") == "earning"
                      and not comps.get(x["code"], {}).get("one_time") and x["code"] not in ("OT", "SHIFT")), ZERO)
        paid = sum((d(x["amount"]) for x in p.lines or [] if x.get("kind") == "earning"
                    and not comps.get(x["code"], {}).get("one_time") and x["code"] not in ("OT", "SHIFT", "ARREARS")), ZERO)
        diff = rnd(should - paid)
        if diff:
            months.append({"month": p.period_start.isoformat(), "should": money(should), "paid": money(paid),
                           "difference": money(diff)})
            total += diff
    return {"months": months, "total": money(total), "monthly_gross": pv["monthly_gross"], "components": pv["components"],
            "warnings": pv.get("warnings") or []}


def apply(db: Session, company_id: int, r: SalaryRevisionRequest, *, structure_uuid=None, note: str | None = None
          ) -> EmployeeSalary:
    if r.status != "pending":
        raise BadRequest("This revision has already been decided.")
    emp = svc.get_employee(db, company_id, r.employee_id)
    data = {"annual_ctc": r.new_ctc, "effective_from": r.effective_from, "reason": r.reason,
            "structure_uuid": _structure_uuid(db, r, structure_uuid),
            "note": note or r.note or ("From pay review" if r.source == "pay_review" else None)}
    if not data["structure_uuid"]:
        pv = preview(db, company_id, r)
        data["overrides"] = {c["code"]: c["monthly"] for c in pv["components"] if c.get("kind") in ("earning", "reimbursement")}
    reasons = {x["value"] for x in svc.get_settings(db, company_id).get("revision_reasons", [])}
    if reasons and data["reason"] not in reasons:
        data["reason"] = "annual_raise" if "annual_raise" in reasons else next(iter(reasons))
    row = svc.assign_salary(db, company_id, emp, data)
    r.status, r.salary_id, r.decided_by_name, r.decided_at, r.decision_note = "applied", row.id, svc.actor(), _now(), note
    if row.structure_id:
        r.structure_id = row.structure_id
    audit.event(db, "payroll.revision_applied", "salary_revision_requests", r.id,
                {"new_ctc": money(r.new_ctc), "effective_from": r.effective_from.isoformat()}, entity_uuid=str(r.uuid),
                entity_label=svc.name_of(emp))
    try:
        from modules.notifications.service import notify
        notify(db, company_id, [emp.id], kind="payroll", title="Your revised salary is in payroll",
               body=f"Your salary of {calc.inr(r.new_ctc)} a year from {r.effective_from:%d %b %Y} is now in payroll.",
               link="/dashboard/payroll/me?tab=salary")
    except Exception:  # notifications never block payroll
        pass
    db.flush()
    return row


def reject(db: Session, company_id: int, r: SalaryRevisionRequest, note: str) -> None:
    if r.status != "pending":
        raise BadRequest("This revision has already been decided.")
    if not (note or "").strip():
        raise BadRequest("Give a reason.")
    r.status, r.decided_by_name, r.decided_at, r.decision_note = "rejected", svc.actor(), _now(), note.strip()
    audit.event(db, "payroll.revision_rejected", "salary_revision_requests", r.id, {"reason": note.strip()},
                entity_uuid=str(r.uuid))
    db.flush()


def out(r: SalaryRevisionRequest, emp=None, structure_name: str | None = None) -> dict:
    cur, new = d(r.current_ctc), d(r.new_ctc)
    return {"uuid": str(r.uuid), "employee_id": r.employee_id, "employee_uuid": str(emp.uuid) if emp else None,
            "employee_name": svc.name_of(emp) if emp else None, "employee_code": emp.employee_code if emp else None,
            "source": r.source, "source_label": SOURCES.get(r.source, r.source), "source_ref": r.source_ref,
            "reason": r.reason, "effective_from": r.effective_from.isoformat(), "current_ctc": money(cur),
            "new_ctc": money(new), "change": money(new - cur),
            "change_percent": str(((new - cur) / cur * 100).quantize(d("0.1"))) if cur else None,
            "arrears_estimate": money(r.arrears_estimate), "structure_name": structure_name, "note": r.note,
            "status": r.status, "status_label": STATUS.get(r.status, r.status), "decided_by_name": r.decided_by_name,
            "decided_at": r.decided_at.isoformat() if r.decided_at else None, "decision_note": r.decision_note,
            "created_at": r.created_at.isoformat() if r.created_at else None}

