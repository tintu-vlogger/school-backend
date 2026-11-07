"""Probation rules: policies by audience, one probation per person with dates from the
policy, reviews that start automatically before the end date, multi-level reviewers plus
optional feedback, outcomes (confirm, extend within limits, terminate, on hold), the
confirmation letter and confirmation date written to the employee record, bulk actions
and reminders.

Functions other modules call:
    start_for(db, company_id, employee, policy=None, start_date=None) -> Probation
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.orm.attributes import flag_modified

from core import audit
from core.errors import BadRequest, Forbidden, NotFound
from modules.probation.models import Probation, ProbationPolicy, ProbationReview
from modules.recruitment import common as c

MANAGE = "talent.probation.manage"
STATUSES = {"active": "On probation", "in_review": "Under review", "extended": "Extended", "on_hold": "On hold",
            "confirmed": "Confirmed", "terminated": "Not confirmed", "cancelled": "Cancelled"}
OPEN = ("active", "in_review", "extended", "on_hold")
OUTCOMES = {"confirm": "Confirm", "extend": "Extend", "terminate": "End employment", "hold": "Put on hold"}
REVIEWER_TYPES = {"reporting_manager": "Reporting manager", "skip_level_manager": "Manager's manager",
                  "department_head": "Department head", "role": "Everyone with a role", "employee": "A specific person"}


def can_manage(db: Session, user: dict) -> bool:
    return c.has_perm(db, user, MANAGE)


def require_manage(db: Session, user: dict) -> None:
    if not can_manage(db, user):
        raise Forbidden("Only HR can manage probation.")


def add_period(start: date, value: int, unit: str) -> date:
    """End date (inclusive) of a period starting on `start`."""
    if unit == "days":
        return start + timedelta(days=int(value) - 1)
    return c.add_months(start, int(value)) - timedelta(days=1)


def months_between(a: date, b: date) -> float:
    return (b - a).days / 30.4375


# ── Policies ────────────────────────────────────────────────────────

def get_policy(db: Session, company_id: int, uuid) -> ProbationPolicy:
    p = db.query(ProbationPolicy).filter(ProbationPolicy.company_id == company_id, ProbationPolicy.uuid == uuid).first()
    if p is None:
        raise NotFound("Probation policy")
    return p


def _clean_reviewers(db: Session, company_id: int, items) -> list[dict]:
    out = []
    for i, r in enumerate(items or [], start=1):
        t = r.get("type")
        if t not in REVIEWER_TYPES:
            raise BadRequest(f"Reviewer {i}: pick who reviews.")
        val = r.get("value")
        if t == "role" and not val:
            raise BadRequest(f"Reviewer {i}: pick the role.")
        if t == "employee":
            if not val or c.employee(db, company_id, int(val)) is None:
                raise BadRequest(f"Reviewer {i}: pick the person.")
            val = int(val)
        out.append({"level": int(r.get("level") or i), "type": t, "value": val, "label": r.get("label") or REVIEWER_TYPES[t]})
    return out


def save_policy(db: Session, company_id: int, data: dict, p: ProbationPolicy | None = None) -> ProbationPolicy:
    p = p or ProbationPolicy(company_id=company_id)
    if "name" in data:
        p.name = (data["name"] or "").strip()[:150]
    if not p.name:
        raise BadRequest("Name the policy.")
    for k in ("description",):
        if k in data:
            p.description = (data[k] or "").strip() or None
    for k in ("duration_value", "review_before_days", "extension_value", "max_extensions", "priority"):
        if k in data and data[k] not in (None, ""):
            setattr(p, k, int(data[k]))
    if "max_total_months" in data:
        p.max_total_months = int(data["max_total_months"]) if data["max_total_months"] not in (None, "") else None
    for k in ("duration_unit", "extension_unit"):
        if k in data:
            if data[k] not in ("days", "months"):
                raise BadRequest("Use days or months.")
            setattr(p, k, data[k])
    for k in ("allow_extension", "auto_confirm", "confirmation_letter", "active"):
        if k in data:
            setattr(p, k, bool(data[k]))
    if "audience" in data:
        p.audience = c.clean_audience(data["audience"])
    if "reviewers" in data:
        p.reviewers = _clean_reviewers(db, company_id, data["reviewers"])
    if "feedback_from" in data:
        p.feedback_from = _clean_reviewers(db, company_id, data["feedback_from"])
    if (p.duration_value or 0) < 1:
        raise BadRequest("Probation lasts at least one day.")
    if p.duration_unit == "months" and p.duration_value > 24 or p.duration_unit == "days" and p.duration_value > 730:
        raise BadRequest("Probation can't be longer than two years.")
    if p.max_total_months is not None:
        base = p.duration_value if p.duration_unit == "months" else p.duration_value / 30.4375
        if base > p.max_total_months:
            raise BadRequest("The probation is already longer than the maximum allowed in total.")
    if not 0 <= (p.review_before_days or 0) <= 90:
        raise BadRequest("Start the review between 0 and 90 days before the end.")
    if not 0 <= (p.max_extensions or 0) <= 5:
        raise BadRequest("Allow up to 5 extensions.")
    if not p.reviewers:
        raise BadRequest("Add at least one reviewer.")
    db.add(p)
    db.flush()
    return p


def policy_out(db: Session, p: ProbationPolicy, counts: dict | None = None) -> dict:
    names = c.names_for(db, p.company_id, [r["value"] for r in (p.reviewers or []) + (p.feedback_from or [])
                                           if r.get("type") == "employee"])
    def r_out(r):
        return {**r, "name": names.get(r["value"]) if r.get("type") == "employee" else None}
    return {"uuid": str(p.uuid), "name": p.name, "description": p.description, "audience": p.audience or {},
            "priority": p.priority, "duration_value": p.duration_value, "duration_unit": p.duration_unit,
            "review_before_days": p.review_before_days, "allow_extension": p.allow_extension,
            "extension_value": p.extension_value, "extension_unit": p.extension_unit,
            "max_extensions": p.max_extensions, "max_total_months": p.max_total_months,
            "reviewers": [r_out(r) for r in p.reviewers or []], "feedback_from": [r_out(r) for r in p.feedback_from or []],
            "auto_confirm": p.auto_confirm, "confirmation_letter": p.confirmation_letter, "active": p.active,
            "in_use": (counts or {}).get(p.id, 0)}


def pick_policy(db: Session, company_id: int, emp) -> ProbationPolicy | None:
    for p in (db.query(ProbationPolicy).filter(ProbationPolicy.company_id == company_id, ProbationPolicy.active.is_(True))
              .order_by(ProbationPolicy.priority, ProbationPolicy.id).all()):
        if c.audience_match(p.audience, emp):
            return p
    return None


# ── Probations ──────────────────────────────────────────────────────

def _log(p: Probation, text: str, kind: str = "event", **extra) -> None:
    hist = list(p.history or [])
    hist.append(c.event(kind=kind, text=text, **extra))
    p.history = hist[-200:]
    flag_modified(p, "history")


def get(db: Session, company_id: int, uuid) -> Probation:
    p = (db.query(Probation).options(joinedload(Probation.policy), joinedload(Probation.reviews))
         .filter(Probation.company_id == company_id, Probation.uuid == uuid).first())
    if p is None:
        raise NotFound("Probation")
    return p


def start_for(db: Session, company_id: int, emp, policy: ProbationPolicy | None = None,
              start_date: date | None = None, end_date: date | None = None) -> Probation:
    live = (db.query(Probation).filter(Probation.company_id == company_id, Probation.employee_id == emp.id,
                                       Probation.status.in_(OPEN)).first())
    if live is not None:
        return live
    policy = policy or pick_policy(db, company_id, emp)
    start = start_date or emp.date_of_joining or c.today(db, company_id)
    if end_date is None:
        if emp.probation_end_date and emp.probation_end_date >= start:
            end_date = emp.probation_end_date
        elif policy is not None:
            end_date = add_period(start, policy.duration_value, policy.duration_unit)
        else:
            end_date = add_period(start, 6, "months")
    if end_date < start:
        raise BadRequest("Probation can't end before it starts.")
    p = Probation(company_id=company_id, employee_id=emp.id, policy_id=policy.id if policy else None,
                  start_date=start, original_end_date=end_date, end_date=end_date, status="active")
    p.policy = policy
    db.add(p)
    if emp.probation_end_date != end_date:
        emp.probation_end_date = end_date
    db.flush()
    _log(p, f"Probation started under “{policy.name if policy else 'standard'}”, ends {end_date.strftime('%d %b %Y')}", "start")
    return p


def _resolve(db: Session, company_id: int, emp, r: dict) -> list:
    E = c.Employee()
    t, val = r.get("type"), r.get("value")
    if t == "reporting_manager":
        m = c.employee(db, company_id, emp.reporting_manager_id)
        return [m] if m and m.active else []
    if t == "skip_level_manager":
        m = c.employee(db, company_id, emp.reporting_manager_id)
        top = c.employee(db, company_id, m.reporting_manager_id) if m else None
        return [top] if top and top.active else []
    if t == "department_head":
        if not emp.department_id:
            return []
        from modules.departments.models import Department
        d = db.query(Department).filter(Department.id == emp.department_id).first()
        h = c.employee(db, company_id, d.head_employee_id) if d else None
        return [h] if h and h.active else []
    if t == "role":
        return db.query(E).filter(E.company_id == company_id, E.role == str(val), E.active.is_(True),
                                  E.id != emp.id).order_by(E.id).limit(1).all()
    if t == "employee":
        e = c.employee(db, company_id, val)
        return [e] if e and e.active else []
    return []


def start_review(db: Session, p: Probation) -> Probation:
    if p.status not in ("active", "extended"):
        raise BadRequest("A review can start only for someone on probation.")
    emp = c.employee(db, p.company_id, p.employee_id)
    rnd = p.extensions + 1
    if any(r.round == rnd for r in p.reviews):
        p.status = "in_review"
        return p
    policy = p.policy
    reviewers = (policy.reviewers if policy and policy.reviewers else
                 [{"level": 1, "type": "reporting_manager"}, {"level": 2, "type": "role", "value": "hr"}])
    made = 0
    levels = sorted({int(r.get("level") or 1) for r in reviewers})
    first = levels[0] if levels else 1
    seen = set()
    for r in reviewers:
        for person in _resolve(db, p.company_id, emp, r):
            if person.id == emp.id or (r["level"], person.id) in seen:
                continue
            seen.add((r["level"], person.id))
            db.add(ProbationReview(company_id=p.company_id, probation_id=p.id, round=rnd, level=int(r["level"]),
                                   kind="review", reviewer_employee_id=person.id, reviewer_name=c.display_name(person),
                                   status="pending" if int(r["level"]) == first else "waiting"))
            if int(r["level"]) == first:
                c.notify(db, p.company_id, person.id, f"Please review the probation of {emp.first_name} {emp.last_name} "
                                                      f"(ends {p.end_date.strftime('%d %b %Y')}).", "probation")
            made += 1
    for r in (policy.feedback_from if policy else []) or []:
        for person in _resolve(db, p.company_id, emp, r):
            if person.id == emp.id:
                continue
            db.add(ProbationReview(company_id=p.company_id, probation_id=p.id, round=rnd, level=0, kind="feedback",
                                   reviewer_employee_id=person.id, reviewer_name=c.display_name(person), status="pending"))
            c.notify(db, p.company_id, person.id, f"Your feedback is requested on {emp.first_name} {emp.last_name}'s "
                                                  f"probation.", "probation")
    if made == 0:
        c.notify_hr(db, p.company_id, f"No reviewer found for {emp.first_name} {emp.last_name}'s probation. "
                                      f"HR can decide directly.", "probation")
    p.status, p.review_started_at, p.current_level = "in_review", c.now(), first
    _log(p, f"Review started (round {rnd})", "review")
    db.flush()
    db.refresh(p)
    return p


def submit_review(db: Session, user: dict, r: ProbationReview, data: dict) -> ProbationReview:
    p = r.probation
    if r.reviewer_employee_id != user.get("employee_id"):
        raise Forbidden("This review is assigned to someone else.")
    if r.status != "pending":
        raise BadRequest("This review is not open." if r.status != "waiting" else "An earlier reviewer has to finish first.")
    if p.status != "in_review":
        raise BadRequest("The review is closed.")
    rating = data.get("rating")
    if rating in (None, ""):
        raise BadRequest("Give a rating.")
    rating = int(rating)
    if not 1 <= rating <= 5:
        raise BadRequest("Ratings go from 1 to 5.")
    rec = data.get("recommendation")
    if r.kind == "review" and rec not in OUTCOMES:
        raise BadRequest("Recommend confirm, extend, end employment or hold.")
    if rec in ("terminate", "extend", "hold") and not (data.get("comments") or data.get("improvements") or "").strip():
        raise BadRequest("Explain your recommendation in the comments.")
    r.rating, r.recommendation = rating, rec if rec in OUTCOMES else None
    for k in ("strengths", "improvements", "comments"):
        if k in data:
            setattr(r, k, (data[k] or "").strip() or None)
    r.status, r.submitted_at = "submitted", c.now()
    _log(p, f"{r.reviewer_name} submitted a {'review' if r.kind == 'review' else 'feedback'}"
            + (f": {OUTCOMES.get(rec)}" if rec in OUTCOMES else ""), "review")
    db.flush()
    _advance(db, p)
    return r


def _advance(db: Session, p: Probation) -> None:
    rnd = p.extensions + 1
    reviews = [x for x in p.reviews if x.round == rnd and x.kind == "review"]
    if any(x.status == "pending" for x in reviews):
        return
    waiting = sorted({x.level for x in reviews if x.status == "waiting"})
    emp = c.employee(db, p.company_id, p.employee_id)
    if waiting:
        lvl = waiting[0]
        p.current_level = lvl
        for x in reviews:
            if x.level == lvl and x.status == "waiting":
                x.status = "pending"
                c.notify(db, p.company_id, x.reviewer_employee_id,
                         f"Please review the probation of {emp.first_name} {emp.last_name}.", "probation")
        return
    recs = [x.recommendation for x in reviews if x.status == "submitted"]
    c.notify_hr(db, p.company_id, f"All reviews are in for {emp.first_name} {emp.last_name}'s probation. Decide the outcome.",
                "probation")
    if p.policy and p.policy.auto_confirm and recs and all(x == "confirm" for x in recs):
        decide(db, p, "confirm", {"reason": "Every reviewer recommended confirmation"}, auto=True)


def _total_months(p: Probation) -> float:
    return months_between(p.start_date, p.end_date + timedelta(days=1))


def extension_check(p: Probation) -> dict:
    pol = p.policy
    if pol is None:
        return {"allowed": True, "left": 1, "new_end": add_period(p.end_date + timedelta(days=1), 3, "months")}
    new_end = add_period(p.end_date + timedelta(days=1), pol.extension_value, pol.extension_unit)
    reason = None
    if not pol.allow_extension:
        reason = "This policy does not allow extensions."
    elif p.extensions >= pol.max_extensions:
        reason = f"Probation has already been extended {p.extensions} time(s), the most this policy allows."
    elif pol.max_total_months and months_between(p.start_date, new_end + timedelta(days=1)) > pol.max_total_months + 0.01:
        reason = f"Probation can't be longer than {pol.max_total_months} months in total."
    return {"allowed": reason is None, "reason": reason, "left": max(0, pol.max_extensions - p.extensions),
            "new_end": new_end}


def decide(db: Session, p: Probation, outcome: str, data: dict, auto: bool = False) -> Probation:
    from modules.employees import service as people
    if outcome not in OUTCOMES:
        raise BadRequest("Pick confirm, extend, end employment or hold.")
    if p.status not in OPEN:
        raise BadRequest("This probation is already closed.")
    emp = c.employee(db, p.company_id, p.employee_id)
    reason = (data.get("reason") or "").strip()
    t0 = c.today(db, p.company_id)
    if outcome in ("terminate", "hold") and not reason:
        raise BadRequest("Give the reason.")
    if outcome == "confirm":
        cdate = data.get("confirmation_date")
        cdate = date.fromisoformat(str(cdate)[:10]) if cdate else p.end_date + timedelta(days=1)
        if cdate < p.start_date:
            raise BadRequest("Confirmation can't be before probation started.")
        p.status, p.confirmation_date = "confirmed", cdate
        if emp.status == "probation":
            people.set_status(db, p.company_id, emp, "confirmed", reason="Probation completed", effective=min(cdate, t0)
                              if cdate > t0 else cdate, extra={"confirmation_date": cdate})
        else:
            emp.confirmation_date = cdate
        if (p.policy is None or p.policy.confirmation_letter) and data.get("letter", True):
            _confirmation_letter(db, p, emp, cdate)
        c.notify(db, p.company_id, emp.id, f"Congratulations! Your employment is confirmed from {cdate.strftime('%d %b %Y')}.",
                 "probation")
    elif outcome == "extend":
        chk = extension_check(p)
        if not chk["allowed"]:
            raise BadRequest(chk["reason"])
        new_end = chk["new_end"]
        if data.get("new_end_date"):
            wanted = date.fromisoformat(str(data["new_end_date"])[:10])
            if wanted <= p.end_date:
                raise BadRequest("The new end date should be after the current one.")
            if wanted > new_end:
                raise BadRequest(f"The extension can run until {new_end.strftime('%d %b %Y')} at most.")
            new_end = wanted
        old = p.end_date
        p.extensions += 1
        p.end_date, p.status, p.review_started_at = new_end, "extended", None
        emp.probation_end_date = new_end
        c.notify(db, p.company_id, emp.id, f"Your probation has been extended to {new_end.strftime('%d %b %Y')}.", "probation")
        _log(p, f"Extended from {old.strftime('%d %b %Y')} to {new_end.strftime('%d %b %Y')}" + (f": {reason}" if reason else ""),
             "extend")
    elif outcome == "hold":
        p.status, p.hold_reason = "on_hold", reason[:500]
    elif outcome == "terminate":
        p.status = "terminated"
        try:
            from modules.offboarding import service as offboarding
            lwd = data.get("last_working_day")
            lwd = date.fromisoformat(str(lwd)[:10]) if lwd else None
            x = offboarding.start_exit(db, p.company_id, emp, "termination",
                                       {"reason_text": f"Probation not confirmed: {reason}", "last_working_day": lwd,
                                        "raised_by": "hr"})
            p.exit_id = x.id
        except ImportError:
            pass
    p.outcome, p.outcome_reason = outcome, reason or None
    p.decided_at, p.decided_by = c.now(), ("Automatic" if auto else c.actor_name())
    if outcome != "extend":
        _log(p, f"Outcome: {OUTCOMES[outcome]}" + (f" ({reason})" if reason else ""), "decision")
    for r in p.reviews:
        if r.status in ("pending", "waiting") and r.round == p.extensions + (0 if outcome == "extend" else 1):
            r.status = "skipped"
    audit.event(db, f"probation.{outcome}", "probations", p.id, {"employee_id": p.employee_id, "reason": reason},
                company_id=p.company_id, entity_uuid=str(p.uuid), entity_label=f"{emp.first_name} {emp.last_name}")
    db.flush()
    return p


def _confirmation_letter(db: Session, p: Probation, emp, cdate: date) -> None:
    from modules.documents import service as documents
    template = documents.default_template(db, p.company_id, "confirmation")
    if template is None:
        return
    try:
        with db.begin_nested():
            letter = documents.generate_letter(db, p.company_id, template, emp,
                                               {"job.confirmation_date": cdate.isoformat(), "job.effective_date": cdate.isoformat()})
            p.letter_id = letter.id
            _log(p, "Confirmation letter issued", "letter")
    except BadRequest:
        pass


def cancel(db: Session, p: Probation, reason: str | None) -> Probation:
    if p.status not in OPEN:
        raise BadRequest("This probation is already closed.")
    if not (reason or "").strip():
        raise BadRequest("Say why the probation is cancelled.")
    p.status, p.outcome_reason = "cancelled", reason.strip()
    for r in p.reviews:
        if r.status in ("pending", "waiting"):
            r.status = "skipped"
    _log(p, f"Cancelled: {reason.strip()}", "cancel")
    return p


def resume(db: Session, p: Probation) -> Probation:
    if p.status != "on_hold":
        raise BadRequest("Only probations on hold can be resumed.")
    p.status, p.hold_reason = "in_review", None
    _log(p, "Taken off hold", "review")
    return p


def change_end_date(db: Session, p: Probation, new_end: date, reason: str | None) -> Probation:
    """HR override of the end date (before any extension), clearly logged."""
    if p.status not in ("active",):
        raise BadRequest("The end date can be changed only before the review starts.")
    if p.extensions:
        raise BadRequest("The length can't be changed while an extension is running.")
    if new_end < p.start_date:
        raise BadRequest("Probation can't end before it starts.")
    pol = p.policy
    if pol and pol.max_total_months and months_between(p.start_date, new_end + timedelta(days=1)) > pol.max_total_months + 0.01:
        raise BadRequest(f"Probation can't be longer than {pol.max_total_months} months in total.")
    if not (reason or "").strip():
        raise BadRequest("Say why the end date changes.")
    old = p.end_date
    p.end_date = p.original_end_date = new_end
    emp = c.employee(db, p.company_id, p.employee_id)
    emp.probation_end_date = new_end
    _log(p, f"End date changed from {old.strftime('%d %b %Y')} to {new_end.strftime('%d %b %Y')}: {reason}", "override")
    return p


# ── Output ──────────────────────────────────────────────────────────

def review_out(r: ProbationReview) -> dict:
    return {"uuid": str(r.uuid), "round": r.round, "level": r.level, "kind": r.kind,
            "reviewer_employee_id": r.reviewer_employee_id, "reviewer_name": r.reviewer_name, "status": r.status,
            "rating": r.rating, "recommendation": r.recommendation, "recommendation_label": OUTCOMES.get(r.recommendation),
            "strengths": r.strengths, "improvements": r.improvements, "comments": r.comments,
            "submitted_at": r.submitted_at}


def out(db: Session, p: Probation, emp=None, labels: dict | None = None, full: bool = False) -> dict:
    emp = emp or c.employee(db, p.company_id, p.employee_id)
    labels = labels or c.org_labels(db, p.company_id, departments=[emp.department_id], designations=[emp.designation_id])
    t0 = c.today(db, p.company_id)
    rnd = p.extensions + 1
    cur = [r for r in p.reviews if r.round == rnd and r.kind == "review"]
    pol = p.policy
    review_on = p.end_date - timedelta(days=pol.review_before_days if pol else 15)
    o = {"uuid": str(p.uuid), "employee_id": p.employee_id, "employee_uuid": str(emp.uuid),
         "name": f"{emp.first_name} {emp.last_name}".strip(), "employee_code": emp.employee_code,
         "designation": labels["designation"].get(emp.designation_id),
         "department": labels["department"].get(emp.department_id), "manager_id": emp.reporting_manager_id,
         "date_of_joining": emp.date_of_joining, "start_date": p.start_date, "original_end_date": p.original_end_date,
         "end_date": p.end_date, "days_left": (p.end_date - t0).days, "review_starts_on": review_on,
         "status": p.status, "status_label": STATUSES.get(p.status), "extensions": p.extensions,
         "max_extensions": pol.max_extensions if pol else None, "policy_name": pol.name if pol else None,
         "outcome": p.outcome, "outcome_label": OUTCOMES.get(p.outcome), "outcome_reason": p.outcome_reason,
         "hold_reason": p.hold_reason, "confirmation_date": p.confirmation_date, "decided_at": p.decided_at,
         "decided_by": p.decided_by, "reviews_done": sum(1 for r in cur if r.status == "submitted"),
         "reviews_total": len(cur), "overdue": p.status in OPEN and p.end_date < t0}
    if full:
        chk = extension_check(p) if p.status in OPEN else {"allowed": False, "reason": "Closed"}
        o.update({"reviews": [review_out(r) for r in sorted(p.reviews, key=lambda r: (r.round, r.kind != "review", r.level))],
                  "history": list(reversed(p.history or [])),
                  "extension": {"allowed": chk["allowed"], "reason": chk.get("reason"),
                                "new_end": chk.get("new_end"), "left": chk.get("left")},
                  "policy": policy_out(db, pol) if pol else None})
        if p.letter_id:
            from modules.documents import service as documents
            from modules.documents.models import Letter
            letter = db.get(Letter, p.letter_id)
            if letter is not None:
                o["letter"] = {"uuid": str(letter.uuid), "status": letter.status,
                               "file": documents.file_link(db, p.company_id, letter.stored_file_id)}
    return o


def dashboard(db: Session, company_id: int) -> dict:
    t0 = c.today(db, company_id)
    by = dict(db.query(Probation.status, func.count(Probation.id)).filter(Probation.company_id == company_id)
              .group_by(Probation.status).all())
    due30 = db.query(func.count(Probation.id)).filter(Probation.company_id == company_id, Probation.status.in_(OPEN),
                                                      Probation.end_date <= t0 + timedelta(days=30),
                                                      Probation.end_date >= t0).scalar()
    overdue = db.query(func.count(Probation.id)).filter(Probation.company_id == company_id, Probation.status.in_(OPEN),
                                                        Probation.end_date < t0).scalar()
    pend = (db.query(ProbationReview.reviewer_employee_id, func.count(ProbationReview.id))
            .join(Probation, Probation.id == ProbationReview.probation_id)
            .filter(ProbationReview.company_id == company_id, ProbationReview.status == "pending",
                    Probation.status == "in_review").group_by(ProbationReview.reviewer_employee_id).all())
    names = c.names_for(db, company_id, [r for r, _ in pend])
    return {"by_status": {k: by.get(k, 0) for k in STATUSES}, "ending_in_30_days": due30, "overdue": overdue,
            "pending_by_reviewer": [{"employee_id": r, "name": names.get(r), "count": n} for r, n in pend]}


# ── Background jobs ─────────────────────────────────────────────────

def ensure_records(db: Session, company_id: int) -> int:
    """Everyone whose stage is 'on probation' gets a probation record."""
    E = c.Employee()
    have = {e for (e,) in db.query(Probation.employee_id).filter(Probation.company_id == company_id,
                                                                 Probation.status.in_(OPEN))}
    n = 0
    for emp in db.query(E).filter(E.company_id == company_id, E.status == "probation", E.active.is_(True)).all():
        if emp.id not in have:
            start_for(db, company_id, emp)
            n += 1
    return n


def run_daily(db: Session) -> int:
    """Create missing records, start reviews N days before the end, and send each
    reviewer one reminder a day listing their due reviews."""
    from modules.company.models import Company
    n = 0
    for (cid,) in db.query(Company.id).filter(Company.active.is_(True)).all():
        db.info["company_id"] = cid
        n += ensure_records(db, cid)
        t0 = c.today(db, cid)
        rows = (db.query(Probation).options(joinedload(Probation.policy), joinedload(Probation.reviews))
                .filter(Probation.company_id == cid, Probation.status.in_(("active", "extended"))).all())
        for p in rows:
            before = p.policy.review_before_days if p.policy else 15
            if p.end_date - timedelta(days=before) <= t0:
                start_review(db, p)
                n += 1
        pend = (db.query(ProbationReview).join(Probation, Probation.id == ProbationReview.probation_id)
                .filter(ProbationReview.company_id == cid, ProbationReview.status == "pending",
                        Probation.status == "in_review").all())
        by_reviewer: dict[int, list[ProbationReview]] = {}
        for r in pend:
            if r.reminded_on == t0 or not r.reviewer_employee_id:
                continue
            if r.created_at.date() == t0:
                continue
            by_reviewer.setdefault(r.reviewer_employee_id, []).append(r)
        for eid, items in by_reviewer.items():
            names = c.names_for(db, cid, [x.probation.employee_id for x in items])
            people = ", ".join(sorted({names.get(x.probation.employee_id, "") for x in items}))
            c.notify(db, cid, eid, f"{len(items)} probation review(s) waiting for you: {people}.", "probation")
            for x in items:
                x.reminded_on = t0
            n += 1
    db.info.pop("company_id", None)
    return n
