"""Pay reviews: an increment cycle with budgets per department, a guideline matrix
(rating x compa-ratio -> suggested %), manager proposals in a grid, approval through the
workflow engine (`performance.increment`), revision letters on release, and
effective-dated job changes written to the employee's history (designation applied on
the date; the salary goes to payroll when a payroll service is present)."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from core import audit
from core.errors import BadRequest, Forbidden, NotFound
from modules.performance import calc
from modules.performance.common import (
    INCREMENTS, Actor, config, emp_model, employee, is_manager_of, name_of, notify, now, person, reportee_ids, today,
)
from modules.performance.models import (
    IncrementBudget, IncrementCycle, IncrementProposal, JobChange, ReviewParticipant,
)


def _require(actor: Actor) -> None:
    if not actor.can(INCREMENTS):
        raise Forbidden("Only people who run pay reviews can do this.")


def _payroll():
    """The payroll module's service, when it exists."""
    try:
        from modules.payroll import service as payroll  # type: ignore
        return payroll
    except ImportError:
        return None


def current_ctc(db: Session, company_id: int, employee_id: int) -> Decimal:
    payroll = _payroll()
    fn = getattr(payroll, "current_ctc", None) if payroll else None
    if fn:
        try:
            v = fn(db, company_id, employee_id)
            if v:
                return calc.money(v)
        except Exception:
            pass
    last = (db.query(JobChange).filter(JobChange.company_id == company_id, JobChange.employee_id == employee_id,
                                       JobChange.status == "applied", JobChange.kind == "increment")
            .order_by(JobChange.effective_date.desc()).first())
    if last and (last.after or {}).get("ctc"):
        return calc.money(last.after["ctc"])
    return Decimal("0.00")


# ── Cycles ──────────────────────────────────────────────────────────

def clean_guidelines(g: dict) -> dict:
    bands = []
    for b in (g or {}).get("bands") or []:
        lo = b.get("min")
        hi = b.get("max")
        bands.append({"label": (b.get("label") or "").strip()[:40] or None,
                      "min": float(lo) if lo not in (None, "") else None, "max": float(hi) if hi not in (None, "") else None})
    rows = []
    for r in (g or {}).get("rows") or []:
        if r.get("rating") in (None, ""):
            continue
        pcts = [float(x or 0) for x in (r.get("pcts") or [])]
        if len(pcts) != len(bands):
            raise BadRequest("Every rating row needs one % per compa-ratio band.")
        if any(p < 0 or p > 100 for p in pcts):
            raise BadRequest("Guideline % must be between 0 and 100.")
        rows.append({"rating": float(r["rating"]), "pcts": pcts})
    return {"bands": bands, "rows": sorted(rows, key=lambda r: -r["rating"])}


def cycle_out(db: Session, c: IncrementCycle, with_budget: bool = True) -> dict:
    out = {"uuid": str(c.uuid), "id": c.id, "name": c.name, "status": c.status,
           "review_cycle": {"id": c.review_cycle.id, "uuid": str(c.review_cycle.uuid), "name": c.review_cycle.name} if c.review_cycle else None,
           "effective_date": c.effective_date.isoformat(), "guidelines": c.guidelines or {}, "pay_ranges": c.pay_ranges or {},
           "letter_template": c.letter_template, "released_at": c.released_at.isoformat() if c.released_at else None}
    if with_budget:
        out["budgets"] = budgets(db, c)
        counts = dict(db.query(IncrementProposal.status, func.count(IncrementProposal.id))
                      .filter(IncrementProposal.company_id == c.company_id, IncrementProposal.cycle_id == c.id)
                      .group_by(IncrementProposal.status).all())
        out["counts"] = counts
        out["total_people"] = sum(counts.values())
    return out


def get_cycle(db: Session, company_id: int, uuid) -> IncrementCycle:
    c = db.query(IncrementCycle).options(joinedload(IncrementCycle.review_cycle)).filter(
        IncrementCycle.company_id == company_id, IncrementCycle.uuid == uuid).first()
    if c is None:
        raise NotFound("Pay review")
    return c


def list_cycles(db: Session, actor: Actor) -> list[dict]:
    q = db.query(IncrementCycle).options(joinedload(IncrementCycle.review_cycle)).filter(IncrementCycle.company_id == actor.company_id)
    if not actor.can(INCREMENTS):
        q = q.filter(IncrementCycle.status.in_(("open", "released")))
        if not reportee_ids(db, actor.company_id, actor.employee_id):
            return []
    return [cycle_out(db, c) for c in q.order_by(IncrementCycle.effective_date.desc()).all()]


def save_cycle(db: Session, actor: Actor, data: dict, c: IncrementCycle | None = None) -> IncrementCycle:
    _require(actor)
    name = (data.get("name") or "").strip()
    if not name or not data.get("effective_date"):
        raise BadRequest("Give the pay review a name and an effective date.")
    if c is not None and c.status in ("released", "closed"):
        raise BadRequest("A released pay review can't be changed.")
    if c is None:
        c = IncrementCycle(company_id=actor.company_id, status="draft")
        db.add(c)
    c.name, c.effective_date = name[:200], data["effective_date"]
    if "review_cycle_id" in data:
        c.review_cycle_id = data.get("review_cycle_id") or None
    if "guidelines" in data:
        c.guidelines = clean_guidelines(data.get("guidelines") or {})
    if "pay_ranges" in data:
        pr = {}
        for k, v in (data.get("pay_ranges") or {}).items():
            if v and v.get("mid"):
                pr[str(k)] = {x: float(v.get(x) or 0) for x in ("min", "mid", "max")}
        c.pay_ranges = pr
    if "letter_template" in data:
        c.letter_template = data.get("letter_template") or None
    db.flush()
    if "budgets" in data:
        set_budgets(db, actor, c, data.get("budgets") or [])
    return c


def set_budgets(db: Session, actor: Actor, c: IncrementCycle, items: list[dict]) -> None:
    _require(actor)
    have = {b.department_id: b for b in db.query(IncrementBudget).filter(IncrementBudget.company_id == c.company_id,
                                                                          IncrementBudget.cycle_id == c.id).all()}
    for it in items:
        dep = it.get("department_id") or None
        b = have.get(dep)
        if b is None:
            b = IncrementBudget(company_id=c.company_id, cycle_id=c.id, department_id=dep)
            db.add(b)
            have[dep] = b
        b.budget_pct = Decimal(str(it["budget_pct"])) if it.get("budget_pct") not in (None, "") else None
        b.budget_amount = calc.money(it.get("budget_amount") or 0)
    db.flush()
    _refresh_budget_amounts(db, c)


def _refresh_budget_amounts(db: Session, c: IncrementCycle) -> None:
    """Budgets set as a % become an amount: % of the department's current pay."""
    base = dict(db.query(IncrementProposal.department_id, func.sum(IncrementProposal.current_ctc))
                .filter(IncrementProposal.company_id == c.company_id, IncrementProposal.cycle_id == c.id)
                .group_by(IncrementProposal.department_id).all())
    for b in db.query(IncrementBudget).filter(IncrementBudget.company_id == c.company_id, IncrementBudget.cycle_id == c.id).all():
        if b.budget_pct is not None:
            b.budget_amount = calc.money(Decimal(str(base.get(b.department_id) or 0)) * b.budget_pct / 100)
    db.flush()


def budgets(db: Session, c: IncrementCycle, department_ids: set | None = None) -> list[dict]:
    from modules.departments.models import Department
    spend = {}
    for dep, status, base, inc in (db.query(IncrementProposal.department_id, IncrementProposal.status,
                                            func.sum(IncrementProposal.current_ctc), func.sum(IncrementProposal.increment_amount))
                                   .filter(IncrementProposal.company_id == c.company_id, IncrementProposal.cycle_id == c.id)
                                   .group_by(IncrementProposal.department_id, IncrementProposal.status).all()):
        s = spend.setdefault(dep, {"base": Decimal(0), "proposed": Decimal(0), "approved": Decimal(0)})
        s["base"] += base or 0
        if status != "rejected":
            s["proposed"] += inc or 0
        if status in ("approved", "released"):
            s["approved"] += inc or 0
    rows = {b.department_id: b for b in db.query(IncrementBudget).filter(IncrementBudget.company_id == c.company_id,
                                                                         IncrementBudget.cycle_id == c.id).all()}
    names = dict(db.query(Department.id, Department.department_name).filter(Department.company_id == c.company_id).all())
    out = []
    for dep in sorted(set(spend) | set(rows), key=lambda d: names.get(d, "~")):
        if department_ids is not None and dep not in department_ids:
            continue
        s = spend.get(dep, {"base": Decimal(0), "proposed": Decimal(0), "approved": Decimal(0)})
        b = rows.get(dep)
        amount = b.budget_amount if b else Decimal(0)
        out.append({"department_id": dep, "department": names.get(dep, "No department"),
                    "budget_pct": float(b.budget_pct) if b and b.budget_pct is not None else None,
                    "budget": float(amount), "current_payroll": float(s["base"]), "proposed": float(s["proposed"]),
                    "approved": float(s["approved"]), "remaining": float(amount - s["proposed"]),
                    "used_pct": calc.r2(float(s["proposed"]) / float(amount) * 100) if amount else None})
    return out


def _rating_for(db: Session, c: IncrementCycle, employee_id: int) -> Decimal | None:
    if not c.review_cycle_id:
        return None
    p = db.query(ReviewParticipant).filter(ReviewParticipant.company_id == c.company_id, ReviewParticipant.cycle_id == c.review_cycle_id,
                                           ReviewParticipant.employee_id == employee_id).first()
    if p is None:
        return None
    for v in (p.final_rating, p.calibrated_rating, p.proposed_rating):
        if v is not None:
            return v
    return None


def _guide(c: IncrementCycle, p: IncrementProposal, designation_id: int | None) -> None:
    rng = (c.pay_ranges or {}).get(str(designation_id)) if designation_id else None
    p.compa_ratio = Decimal(str(calc.compa_ratio(p.current_ctc, rng["mid"]))) if rng and p.current_ctc else None
    g = calc.guideline_pct(float(p.rating) if p.rating is not None else None,
                           float(p.compa_ratio) if p.compa_ratio is not None else None, c.guidelines or {})
    p.guideline_pct = Decimal(str(g)) if g is not None else None


def _recalc(p: IncrementProposal) -> None:
    amount, new = calc.increment(p.current_ctc, p.proposed_pct)
    p.increment_amount, p.new_ctc = amount, new


def open_cycle(db: Session, actor: Actor, c: IncrementCycle) -> int:
    """Create a proposal for everyone (the review cycle's people when linked, else every
    active employee), pre-filled from the guideline matrix."""
    _require(actor)
    if c.status not in ("draft", "open"):
        raise BadRequest("This pay review is already released.")
    E = emp_model()
    if c.review_cycle_id:
        ids = [i for (i,) in db.query(ReviewParticipant.employee_id).filter(
            ReviewParticipant.company_id == c.company_id, ReviewParticipant.cycle_id == c.review_cycle_id).all()]
        emps = db.query(E).filter(E.company_id == c.company_id, E.id.in_(ids or [0]), E.active.is_(True)).all()
    else:
        emps = db.query(E).filter(E.company_id == c.company_id, E.active.is_(True)).all()
    have = {i for (i,) in db.query(IncrementProposal.employee_id).filter(IncrementProposal.company_id == c.company_id,
                                                                         IncrementProposal.cycle_id == c.id).all()}
    n = 0
    for e in emps:
        if e.id in have:
            continue
        p = IncrementProposal(company_id=c.company_id, cycle_id=c.id, employee_id=e.id, manager_employee_id=e.reporting_manager_id,
                              department_id=e.department_id, current_designation_id=e.designation_id,
                              current_ctc=current_ctc(db, c.company_id, e.id), rating=_rating_for(db, c, e.id))
        _guide(c, p, e.designation_id)
        p.proposed_pct = p.guideline_pct or Decimal(0)
        _recalc(p)
        db.add(p)
        n += 1
    c.status = "open"
    db.flush()
    _refresh_budget_amounts(db, c)
    for mgr in {e.reporting_manager_id for e in emps if e.reporting_manager_id}:
        notify(db, c.company_id, mgr, f"{c.name} is open. Please propose increments for your team.")
    audit.event(db, "increment_cycle.opened", "perf_increment_cycles", c.id, {"proposals": n}, entity_uuid=str(c.uuid))
    return n


def _visible(db: Session, actor: Actor, c: IncrementCycle) -> set[int] | None:
    """None = everyone (pay review admins); otherwise the employee ids a manager may see."""
    if actor.can(INCREMENTS):
        return None
    return reportee_ids(db, actor.company_id, actor.employee_id, deep=True)


def proposal_out(p: IncrementProposal, designations: dict, scale_labels: dict | None = None) -> dict:
    fl = lambda x: float(x) if x is not None else None  # noqa: E731
    deviation = None
    if p.guideline_pct is not None:
        deviation = calc.r2(float(p.proposed_pct) - float(p.guideline_pct))
    return {
        "uuid": str(p.uuid), "employee": person(p.employee), "manager_employee_id": p.manager_employee_id,
        "department_id": p.department_id, "rating": fl(p.rating), "rating_label": (scale_labels or {}).get(fl(p.rating)),
        "current_ctc": fl(p.current_ctc), "compa_ratio": fl(p.compa_ratio), "guideline_pct": fl(p.guideline_pct),
        "proposed_pct": fl(p.proposed_pct), "increment_amount": fl(p.increment_amount), "new_ctc": fl(p.new_ctc),
        "deviation": deviation, "promote": p.promote,
        "current_designation": designations.get(p.current_designation_id), "current_designation_id": p.current_designation_id,
        "new_designation_id": p.new_designation_id, "new_designation": designations.get(p.new_designation_id),
        "justification": p.justification, "status": p.status, "letter_no": p.letter_no, "has_letter": bool(p.letter_html),
        "released_at": p.released_at.isoformat() if p.released_at else None,
    }


def grid(db: Session, actor: Actor, c: IncrementCycle, department_id: int | None = None, status: str | None = None) -> dict:
    from modules.designations.models import Designation
    E = emp_model()
    vis = _visible(db, actor, c)
    if vis is not None and not vis:
        raise Forbidden("Pay reviews are for managers and pay review administrators.")
    q = (db.query(IncrementProposal).options(joinedload(IncrementProposal.employee).joinedload(E.designation),
                                            joinedload(IncrementProposal.employee).joinedload(E.department))
         .filter(IncrementProposal.company_id == c.company_id, IncrementProposal.cycle_id == c.id))
    if vis is not None:
        q = q.filter(IncrementProposal.employee_id.in_(vis))
    if department_id:
        q = q.filter(IncrementProposal.department_id == department_id)
    if status:
        q = q.filter(IncrementProposal.status == status)
    rows = q.all()
    designations = dict(db.query(Designation.id, Designation.designation_name).filter(
        (Designation.company_id == c.company_id) | (Designation.company_id.is_(None))).all())
    labels = {}
    if c.review_cycle_id:
        from modules.performance.models import ReviewCycle
        from modules.performance.reviews import _scale
        rc = db.get(ReviewCycle, c.review_cycle_id)
        sc = _scale(db, rc) if rc else None
        labels = {float(pt["value"]): pt["label"] for pt in (sc.points if sc else [])}
    items = sorted((proposal_out(p, designations, labels) for p in rows), key=lambda r: (r["employee"]["department"] or "~", r["employee"]["name"]))
    deps = None if vis is None else {p.department_id for p in rows}
    return {"cycle": cycle_out(db, c, with_budget=False), "proposals": items, "budgets": budgets(db, c, deps),
            "can_admin": actor.can(INCREMENTS),
            "totals": {"current": sum(i["current_ctc"] or 0 for i in items), "increase": sum(i["increment_amount"] or 0 for i in items),
                       "promotions": sum(1 for i in items if i["promote"])}}


def get_proposal(db: Session, actor: Actor, uuid) -> IncrementProposal:
    p = (db.query(IncrementProposal).options(joinedload(IncrementProposal.cycle), joinedload(IncrementProposal.employee))
         .filter(IncrementProposal.company_id == actor.company_id, IncrementProposal.uuid == uuid).first())
    if p is None:
        raise NotFound("Proposal")
    return p


def update_proposal(db: Session, actor: Actor, p: IncrementProposal, data: dict) -> IncrementProposal:
    admin = actor.can(INCREMENTS)
    if not admin and not is_manager_of(db, actor, p.employee_id):
        raise Forbidden("You can only propose for your own team.")
    if p.cycle.status != "open":
        raise BadRequest("This pay review is not open for proposals.")
    if p.status not in ("draft", "rejected") and not (admin and p.status == "submitted"):
        raise BadRequest("This proposal is waiting for approval or already approved.")
    if "current_ctc" in data and data["current_ctc"] is not None:
        if not admin:
            raise Forbidden("Only pay review administrators can change current pay.")
        p.current_ctc = calc.money(data["current_ctc"])
        _guide(p.cycle, p, p.current_designation_id)
    if data.get("increment_amount") is not None and p.current_ctc:
        amt = calc.money(data["increment_amount"])
        p.proposed_pct = Decimal(str(round(float(amt) / float(p.current_ctc) * 100, 2)))
    elif data.get("proposed_pct") is not None:
        pct = Decimal(str(data["proposed_pct"]))
        if pct < 0 or pct > 200:
            raise BadRequest("The increment must be between 0% and 200%.")
        p.proposed_pct = pct
    if "promote" in data:
        p.promote = bool(data["promote"])
        if not p.promote:
            p.new_designation_id = None
    if data.get("new_designation_id"):
        if data["new_designation_id"] == p.current_designation_id:
            raise BadRequest("Pick a different designation for a promotion.")
        p.new_designation_id, p.promote = data["new_designation_id"], True
    if p.promote and not p.new_designation_id:
        raise BadRequest("Pick the new designation for the promotion.")
    if "justification" in data:
        p.justification = (data.get("justification") or "").strip() or None
    if p.status == "rejected":
        p.status = "draft"
    _recalc(p)
    db.flush()
    return p


def submit(db: Session, actor: Actor, c: IncrementCycle, uuids: list[str]) -> dict:
    """Send proposals for approval as one request (one per submission)."""
    from core.workflow import service as workflow
    admin = actor.can(INCREMENTS)
    if c.status != "open":
        raise BadRequest("This pay review is not open.")
    rows = db.query(IncrementProposal).options(joinedload(IncrementProposal.employee)).filter(
        IncrementProposal.company_id == c.company_id, IncrementProposal.cycle_id == c.id,
        IncrementProposal.uuid.in_(uuids or [])).all()
    rows = [p for p in rows if p.status in ("draft", "rejected")]
    if not rows:
        raise BadRequest("Pick proposals that are still drafts.")
    for p in rows:
        if not admin and not is_manager_of(db, actor, p.employee_id):
            raise Forbidden("You can only submit proposals for your own team.")
        if p.promote and not p.new_designation_id:
            raise BadRequest(f"{name_of(p.employee)}: pick the new designation.")
    total = sum((p.increment_amount for p in rows), Decimal(0))
    base = sum((p.current_ctc for p in rows), Decimal(0))
    payload = {"cycle_id": c.id, "cycle_name": c.name, "proposal_ids": [p.id for p in rows], "people": len(rows),
               "total_increase": float(total), "avg_pct": calc.r2(float(total) / float(base) * 100) if base else 0.0,
               "promotions": sum(1 for p in rows if p.promote),
               "lines": [f"{name_of(p.employee)}: {float(p.proposed_pct):g}%{' + promotion' if p.promote else ''}" for p in rows[:15]]}
    title = f"Pay review: {len(rows)} {'person' if len(rows) == 1 else 'people'} in {c.name}"
    inst = workflow.submit(db, c.company_id, "performance.increment", payload, title, subject_type="perf_increment_cycles", subject_id=c.id)
    for p in rows:
        p.workflow_instance_id = inst.id
        if p.status in ("draft", "rejected"):
            p.status = "submitted" if inst.status == "pending" else p.status
    db.flush()
    return {"request": inst.reference_no, "status": inst.status, "count": len(rows)}


def on_decision(db: Session, instance, status: str) -> None:
    ids = (instance.payload or {}).get("proposal_ids") or []
    rows = db.query(IncrementProposal).filter(IncrementProposal.company_id == instance.company_id,
                                              IncrementProposal.id.in_(ids or [0])).all()
    for p in rows:
        if p.status in ("submitted", "draft"):
            p.status = {"approved": "approved", "rejected": "rejected", "cancelled": "draft"}[status]
    db.flush()


def release(db: Session, actor: Actor, c: IncrementCycle) -> dict:
    """Letters for every approved proposal and job changes effective on the cycle date."""
    _require(actor)
    if c.status != "open":
        raise BadRequest("Only an open pay review can be released.")
    rows = db.query(IncrementProposal).options(joinedload(IncrementProposal.employee)).filter(
        IncrementProposal.company_id == c.company_id, IncrementProposal.cycle_id == c.id, IncrementProposal.status == "approved").all()
    if not rows:
        raise BadRequest("Nothing is approved yet.")
    from core import numbering
    from modules.company.models import Company
    from modules.designations.models import Designation
    company = db.get(Company, c.company_id)
    t = today(db, c.company_id)
    tmpl = c.letter_template or config(db, c.company_id)["letters"]["increment_template"]
    for p in rows:
        new_desig = db.get(Designation, p.new_designation_id) if p.promote and p.new_designation_id else None
        vals = {
            "employee_name": name_of(p.employee), "cycle_name": c.name,
            "current_ctc": f"₹{float(p.current_ctc):,.0f}", "new_ctc": f"₹{float(p.new_ctc):,.0f}",
            "increment_pct": f"{float(p.proposed_pct):g}", "increment_amount": f"₹{float(p.increment_amount):,.0f}",
            "effective_date": c.effective_date.strftime("%d %B %Y"),
            "promotion_line": f" We are also delighted to promote you to {new_desig.designation_name}." if new_desig else "",
            "new_designation": new_desig.designation_name if new_desig else "", "company_name": company.company_name if company else "",
        }
        body = tmpl
        for k, v in vals.items():
            body = body.replace("{" + k + "}", str(v))
        p.letter_no = numbering.next_number(db, c.company_id, "letter", t)
        p.letter_html, p.released_at, p.status = body, now(), "released"
        if p.proposed_pct and float(p.proposed_pct) > 0:
            db.add(JobChange(company_id=c.company_id, employee_id=p.employee_id, kind="increment", effective_date=c.effective_date,
                             before={"ctc": float(p.current_ctc)}, after={"ctc": float(p.new_ctc), "pct": float(p.proposed_pct)},
                             source=str(p.uuid), note=c.name))
        if new_desig:
            _promote(db, c, p, new_desig)
        notify(db, c.company_id, p.employee_id, f"Your revision letter for {c.name} is ready.")
    c.status, c.released_at = "released", now()
    db.flush()
    applied = apply_due_changes(db, c.company_id)
    audit.event(db, "increment_cycle.released", "perf_increment_cycles", c.id, {"letters": len(rows), "applied_now": applied},
                entity_uuid=str(c.uuid), entity_label=c.name)
    return {"released": len(rows), "applied_now": applied}


def _promote(db: Session, c: IncrementCycle, p: IncrementProposal, new_desig) -> None:
    """Promotions go into the employee's job history (effective-dated) through the
    employees service; if that is refused (e.g. another change on the same date) the
    promotion is kept here and applied on the date instead."""
    from modules.employees import service as employees
    record = getattr(employees, "record_job_change", None)
    if record is not None:
        try:
            with db.begin_nested():
                record(db, c.company_id, p.employee, "promotion", c.effective_date, {"designation_id": new_desig.id},
                       reason=f"Promoted in {c.name}", remarks=p.justification)
            return
        except BadRequest:
            pass
    db.add(JobChange(company_id=c.company_id, employee_id=p.employee_id, kind="promotion", effective_date=c.effective_date,
                     before={"designation_id": p.current_designation_id},
                     after={"designation_id": new_desig.id, "designation": new_desig.designation_name},
                     source=str(p.uuid), note=c.name))


def apply_due_changes(db: Session, company_id: int) -> int:
    """Apply scheduled job changes whose date has come: designation on the employee record;
    salary through the payroll service when there is one."""
    t = today(db, company_id)
    rows = db.query(JobChange).filter(JobChange.company_id == company_id, JobChange.status == "scheduled",
                                      JobChange.effective_date <= t).all()
    payroll = _payroll()
    for ch in rows:
        emp = employee(db, company_id, ch.employee_id, required=False)
        if emp is None:
            ch.status = "cancelled"
            continue
        if ch.kind == "promotion" and (ch.after or {}).get("designation_id"):
            from modules.employees import service as employees
            employees.update(db, company_id, emp, {"designation_id": ch.after["designation_id"]}, change_type="promotion",
                             reason=ch.note, effective_date=ch.effective_date)
        if ch.kind == "increment" and payroll and getattr(payroll, "apply_salary_revision", None):
            try:
                payroll.apply_salary_revision(db, company_id, ch.employee_id, Decimal(str(ch.after["ctc"])), ch.effective_date)
            except Exception as exc:  # keep the change scheduled so it is retried
                ch.note = f"{ch.note or ''} (payroll: {exc})"[:500]
                continue
        ch.status, ch.applied_at = "applied", now()
        notify(db, company_id, ch.employee_id, f"Your {ch.kind} effective {ch.effective_date.strftime('%d %b %Y')} is now in your record.")
    db.flush()
    return len(rows)


def job_history(db: Session, actor: Actor, employee_id: int) -> list[dict]:
    if not (actor.can(INCREMENTS) or actor.employee_id == employee_id or is_manager_of(db, actor, employee_id)):
        raise Forbidden()
    rows = (db.query(JobChange).filter(JobChange.company_id == actor.company_id, JobChange.employee_id == employee_id)
            .order_by(JobChange.effective_date.desc(), JobChange.id.desc()).all())
    return [{"uuid": str(r.uuid), "kind": r.kind, "effective_date": r.effective_date.isoformat(), "before": r.before,
             "after": r.after, "status": r.status, "note": r.note,
             "applied_at": r.applied_at.isoformat() if r.applied_at else None} for r in rows]


def my_letters(db: Session, actor: Actor) -> list[dict]:
    if actor.employee_id is None:
        return []
    rows = (db.query(IncrementProposal).options(joinedload(IncrementProposal.cycle))
            .filter(IncrementProposal.company_id == actor.company_id, IncrementProposal.employee_id == actor.employee_id,
                    IncrementProposal.status == "released").order_by(IncrementProposal.released_at.desc()).all())
    return [{"uuid": str(p.uuid), "cycle": p.cycle.name, "effective_date": p.cycle.effective_date.isoformat(),
             "letter_no": p.letter_no, "new_ctc": float(p.new_ctc), "pct": float(p.proposed_pct), "promote": p.promote,
             "released_at": p.released_at.isoformat() if p.released_at else None} for p in rows]


def letter(db: Session, actor: Actor, p: IncrementProposal) -> dict:
    if not (actor.can(INCREMENTS) or actor.employee_id == p.employee_id):
        raise Forbidden()
    if not p.letter_html:
        raise NotFound("Letter")
    return {"letter_no": p.letter_no, "body": p.letter_html, "employee": name_of(p.employee), "cycle": p.cycle.name,
            "released_at": p.released_at.isoformat() if p.released_at else None}
