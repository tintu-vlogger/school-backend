"""Group insurance and benefit plans: plans, eligibility, enrolment windows, premiums and the
employee's share deducted in payroll, auto-enrolment and the monthly insurer file.

Payroll calls `payroll_deductions(db, company_id, employee_ids, start, end)` while syncing a
pay run's inputs; each enrolment becomes one "INS_PREM" deduction, pro-rated for part months.

Premiums are yearly per policy. A tier says who is covered: the employee, a spouse, up to N
children and up to N parents (or parents-in-law). Parental plans can price each parent by
age band instead of a flat tier premium. The employer pays a percentage of the premium or a
flat yearly amount; the rest is the employee's share, spread over the months of the policy.
"""
from __future__ import annotations

import io
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.orm import Session

from core import audit, expressions
from core.errors import BadRequest, Forbidden, NotFound
from core.timeutil import today_for_company_id
from modules.benefits.models import BenefitEnrolment, BenefitPlan

ZERO = Decimal("0")
KINDS = {"gmc": "Group health", "gpa": "Group personal accident", "gtl": "Group term life", "topup": "Top-up health",
         "parental": "Parental cover", "other": "Other benefit"}
STATUS = {"enrolled": "Enrolled", "waived": "Waived", "terminated": "Ended"}
STARTS = {"on_joining": "From the joining date", "first_of_month": "From the 1st of the joining month",
          "first_of_next_month": "From the 1st of the next month"}
RULE_FIELDS = {"grade", "grade_rank", "department", "location", "employment_type", "age", "months_of_service",
               "annual_ctc", "gender", "designation"}
PREMIUM_CODE = "INS_PREM"


def d(v) -> Decimal:
    if v is None or v == "":
        return ZERO
    return v if isinstance(v, Decimal) else Decimal(str(v))


def rnd(v) -> Decimal:
    return d(v).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def money(v) -> str:
    return str(d(v).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _now():
    return datetime.now(timezone.utc)


def _actor() -> str:
    from core import context
    return context.current().actor_name or "System"


def _emp_model():
    from modules.employees.models import Employee
    return Employee


def name_of(e) -> str:
    return f"{e.first_name} {e.last_name}".strip() if e else ""


# ── Plans ──────────────────────────────────────────────────────────

def plans(db: Session, company_id: int, active_only: bool = False) -> list[BenefitPlan]:
    q = db.query(BenefitPlan).filter(BenefitPlan.company_id == company_id)
    if active_only:
        q = q.filter(BenefitPlan.active.is_(True))
    return q.order_by(BenefitPlan.policy_start.desc(), BenefitPlan.name).all()


def get_plan(db: Session, company_id: int, uuid) -> BenefitPlan:
    p = db.query(BenefitPlan).filter(BenefitPlan.company_id == company_id, BenefitPlan.uuid == uuid).first()
    if not p:
        raise NotFound("Benefit plan")
    return p


def _clean_tiers(tiers: list[dict]) -> list[dict]:
    out, seen = [], set()
    for t in tiers or []:
        code = (t.get("code") or "").strip().lower() or None
        label = (t.get("label") or "").strip()
        if not label:
            raise BadRequest("Give every coverage tier a name.")
        code = code or "_".join(label.lower().split())[:30]
        if code in seen:
            raise BadRequest(f"Two tiers use the code {code}.")
        seen.add(code)
        children, parents = int(t.get("children") or 0), int(t.get("parents") or 0)
        if children < 0 or parents < 0 or children > 6 or parents > 4:
            raise BadRequest("Children can be 0 to 6 and parents 0 to 4.")
        if d(t.get("premium")) < 0:
            raise BadRequest("A premium cannot be negative.")
        out.append({"code": code, "label": label, "spouse": bool(t.get("spouse")), "children": children, "parents": parents,
                    "premium": float(d(t.get("premium")))})
    if not out:
        raise BadRequest("Add at least one coverage tier.")
    return out


def _clean_bands(bands: list[dict]) -> list[dict]:
    out = sorted(({"min_age": int(b.get("min_age") or 0), "max_age": int(b.get("max_age") or 0),
                   "premium": float(d(b.get("premium")))} for b in bands or []), key=lambda b: b["min_age"])
    for i, b in enumerate(out):
        if b["max_age"] < b["min_age"]:
            raise BadRequest("An age band ends before it starts.")
        if i and b["min_age"] != out[i - 1]["max_age"] + 1:
            raise BadRequest("Age bands must follow each other without gaps or overlaps.")
    return out


PLAN_FIELDS = ("name", "kind", "insurer", "policy_number", "tpa", "broker", "contact_email", "contact_phone", "website",
               "description", "policy_start", "policy_end", "sum_insured_basis", "sum_insured", "salary_multiple",
               "min_sum_insured", "max_sum_insured", "employer_share_type", "employer_share_value", "deduct_in_payroll",
               "eligibility_rule", "waiting_days", "cover_starts", "mandatory", "auto_enrol", "joining_window_days",
               "renewal_window_from", "renewal_window_to", "active")


def save_plan(db: Session, company_id: int, data: dict, p: BenefitPlan | None = None) -> BenefitPlan:
    new = p is None
    if new:
        p = BenefitPlan(company_id=company_id, created_by_name=_actor(), kind="gmc", sum_insured_basis="fixed",
                        sum_insured=ZERO, employer_share_type="percent", employer_share_value=Decimal(100),
                        deduct_in_payroll=True, waiting_days=0, cover_starts="on_joining", mandatory=False, auto_enrol=False,
                        joining_window_days=30, active=True, tiers=[], age_bands=[])
    for f in PLAN_FIELDS:
        if f in data and not (data[f] is None and f in ("kind", "sum_insured_basis", "employer_share_type", "cover_starts",
                                                         "deduct_in_payroll", "mandatory", "auto_enrol", "active",
                                                         "waiting_days", "joining_window_days", "employer_share_value")):
            setattr(p, f, data[f])
    if "tiers" in data:
        p.tiers = _clean_tiers(data["tiers"])
    if "age_bands" in data:
        p.age_bands = _clean_bands(data["age_bands"])
    if not (p.name or "").strip():
        raise BadRequest("Give the plan a name.")
    if p.kind not in KINDS:
        raise BadRequest("Choose the kind of plan.")
    if not p.policy_start or not p.policy_end or p.policy_end <= p.policy_start:
        raise BadRequest("The policy must end after it starts.")
    if p.sum_insured_basis not in ("fixed", "salary_multiple"):
        raise BadRequest("Choose a fixed sum insured or a multiple of salary.")
    if p.sum_insured_basis == "salary_multiple" and d(p.salary_multiple) <= 0:
        raise BadRequest("Enter the multiple of annual salary.")
    if p.sum_insured_basis == "fixed" and d(p.sum_insured) <= 0:
        raise BadRequest("Enter the sum insured.")
    if p.employer_share_type not in ("percent", "flat"):
        raise BadRequest("Choose how the employer's share is set.")
    if p.employer_share_type == "percent" and not (0 <= d(p.employer_share_value) <= 100):
        raise BadRequest("The employer's share must be between 0% and 100%.")
    if p.cover_starts not in STARTS:
        raise BadRequest("Choose when cover starts.")
    if p.eligibility_rule:
        try:
            expressions.validate(p.eligibility_rule, RULE_FIELDS)
        except expressions.ExpressionError as e:
            raise BadRequest(f"Eligibility rule: {e}")
    if (p.renewal_window_from is None) != (p.renewal_window_to is None) or (
            p.renewal_window_from and p.renewal_window_to < p.renewal_window_from):
        raise BadRequest("Give the renewal window a start and an end.")
    if not p.tiers:
        p.tiers = _clean_tiers([{"code": "employee", "label": "Employee only", "premium": 0}])
    if new:
        db.add(p)
    db.flush()
    return p


def plan_out(p: BenefitPlan, counts: dict | None = None, today: date | None = None) -> dict:
    today = today or date.today()
    c = (counts or {}).get(p.id, {})
    return {"uuid": str(p.uuid), "name": p.name, "kind": p.kind, "kind_label": KINDS.get(p.kind, p.kind), "insurer": p.insurer,
            "policy_number": p.policy_number, "tpa": p.tpa, "broker": p.broker, "contact_email": p.contact_email,
            "contact_phone": p.contact_phone, "website": p.website, "description": p.description,
            "policy_start": p.policy_start.isoformat(), "policy_end": p.policy_end.isoformat(),
            "sum_insured_basis": p.sum_insured_basis, "sum_insured": money(p.sum_insured),
            "salary_multiple": str(p.salary_multiple) if p.salary_multiple is not None else None,
            "min_sum_insured": money(p.min_sum_insured) if p.min_sum_insured is not None else None,
            "max_sum_insured": money(p.max_sum_insured) if p.max_sum_insured is not None else None,
            "tiers": p.tiers or [], "age_bands": p.age_bands or [], "employer_share_type": p.employer_share_type,
            "employer_share_value": str(p.employer_share_value), "deduct_in_payroll": p.deduct_in_payroll,
            "eligibility_rule": p.eligibility_rule, "waiting_days": p.waiting_days, "cover_starts": p.cover_starts,
            "mandatory": p.mandatory, "auto_enrol": p.auto_enrol, "joining_window_days": p.joining_window_days,
            "renewal_window_from": p.renewal_window_from.isoformat() if p.renewal_window_from else None,
            "renewal_window_to": p.renewal_window_to.isoformat() if p.renewal_window_to else None,
            "renewal_open": bool(p.renewal_window_from and p.renewal_window_from <= today <= p.renewal_window_to),
            "active": p.active, "enrolled": c.get("enrolled", 0), "waived": c.get("waived", 0),
            "employee_premium_total": money(c.get("employee_share", 0)), "premium_total": money(c.get("premium", 0))}


def plan_counts(db: Session, company_id: int) -> dict:
    out: dict = defaultdict(lambda: defaultdict(lambda: ZERO))
    for e in db.query(BenefitEnrolment).filter(BenefitEnrolment.company_id == company_id,
                                               BenefitEnrolment.status.in_(("enrolled", "waived"))).all():
        out[e.plan_id][e.status] += 1
        if e.status == "enrolled":
            out[e.plan_id]["premium"] += d(e.annual_premium)
            out[e.plan_id]["employee_share"] += d(e.employee_share)
    return {k: {kk: (int(vv) if kk in ("enrolled", "waived") else vv) for kk, vv in v.items()} for k, v in out.items()}


# ── Eligibility, cover and premium ─────────────────────────────────

def _age(dob: date | None, on: date) -> int | None:
    if not dob:
        return None
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def _annual_ctc(db: Session, company_id: int, employee_id: int) -> Decimal:
    try:
        from modules.payroll import service as payroll
        return d(payroll.current_ctc(db, company_id, employee_id) or 0)
    except ImportError:  # pragma: no cover
        return ZERO


def employee_vars(db: Session, company_id: int, e, on: date) -> dict:
    from core.masters import MasterValue
    labels = {}
    ids = [x for x in (e.employment_type_id, e.gender_id) if x]
    if ids:
        labels = {m.id: (m.code or m.label) for m in db.query(MasterValue).filter(MasterValue.id.in_(ids)).all()}
    grade = getattr(e, "grade", None)
    dept = getattr(e, "department", None)
    loc = getattr(e, "location", None)
    desg = getattr(e, "designation", None)
    doj = e.date_of_joining
    return {"grade": (grade.code or grade.name) if grade else None, "grade_rank": grade.rank if grade else None,
            "department": dept.department_name if dept else None, "location": loc.name if loc else None,
            "designation": desg.designation_name if desg else None,
            "employment_type": labels.get(e.employment_type_id), "gender": labels.get(e.gender_id),
            "age": _age(e.date_of_birth, on),
            "months_of_service": ((on.year - doj.year) * 12 + on.month - doj.month) if doj else 0,
            "annual_ctc": _annual_ctc(db, company_id, e.id)}


def eligible(db: Session, company_id: int, p: BenefitPlan, e, on: date) -> tuple[bool, str | None]:
    if not p.active or on > p.policy_end:
        return False, "The plan is not running."
    if e.exit_date and e.exit_date < max(on, p.policy_start):
        return False, "Leaving before cover starts."
    if not p.eligibility_rule:
        return True, None
    try:
        ok = expressions.check_condition(p.eligibility_rule, employee_vars(db, company_id, e, on), today=on)
    except expressions.ExpressionError:
        ok = False
    return (True, None) if ok else (False, "Not eligible under the plan's rule.")


def sum_insured(p: BenefitPlan, annual_ctc: Decimal) -> Decimal:
    if p.sum_insured_basis == "salary_multiple":
        si = rnd(d(annual_ctc) * d(p.salary_multiple))
        if p.min_sum_insured is not None:
            si = max(si, d(p.min_sum_insured))
        if p.max_sum_insured is not None:
            si = min(si, d(p.max_sum_insured))
        return si
    return d(p.sum_insured)


def _relation_kind(label: str | None) -> str:
    x = (label or "").lower()
    if x in ("spouse", "wife", "husband"):
        return "spouse"
    if x in ("son", "daughter", "child"):
        return "child"
    if x in ("father", "mother", "father-in-law", "mother-in-law", "parent"):
        return "parent"
    return "other"


def family(db: Session, company_id: int, employee_id: int) -> list[dict]:
    from core.masters import MasterValue
    from modules.employees.models import FamilyMember
    rows = db.query(FamilyMember).filter(FamilyMember.company_id == company_id, FamilyMember.employee_id == employee_id).all()
    ids = [x for r in rows for x in (r.relation_id, r.gender_id) if x]
    labels = {m.id: m.label for m in db.query(MasterValue).filter(MasterValue.id.in_(ids or [-1])).all()}
    return [{"id": r.id, "uuid": str(r.uuid), "name": r.name, "relation": labels.get(r.relation_id),
             "relation_kind": _relation_kind(labels.get(r.relation_id)), "gender": labels.get(r.gender_id),
             "date_of_birth": r.date_of_birth.isoformat() if r.date_of_birth else None} for r in rows]


def premium_for(p: BenefitPlan, tier_code: str, dependents: list[dict]) -> Decimal:
    tier = next((t for t in p.tiers or [] if t["code"] == tier_code), None)
    if tier is None:
        raise BadRequest("Choose a coverage tier of this plan.")
    counts = defaultdict(int)
    for x in dependents:
        counts[x.get("relation_kind")] += 1
    if counts["spouse"] > (1 if tier.get("spouse") else 0):
        raise BadRequest(f"{tier['label']} does not cover a spouse.")
    if counts["child"] > int(tier.get("children") or 0):
        raise BadRequest(f"{tier['label']} covers up to {tier.get('children') or 0} children.")
    if counts["parent"] > int(tier.get("parents") or 0):
        raise BadRequest(f"{tier['label']} covers up to {tier.get('parents') or 0} parents.")
    if counts["other"]:
        raise BadRequest("Only a spouse, children and parents can be covered.")
    bands = p.age_bands or []
    if bands and counts["parent"]:
        total = ZERO
        for x in dependents:
            if x.get("relation_kind") != "parent":
                continue
            if not x.get("date_of_birth"):
                raise BadRequest(f"Add {x['name']}'s date of birth to price the cover.")
            age = _age(date.fromisoformat(x["date_of_birth"]), p.policy_start)
            band = next((b for b in bands if b["min_age"] <= age <= b["max_age"]), None)
            if band is None:
                raise BadRequest(f"{x['name']} ({age}) is outside the plan's age bands.")
            total += d(band["premium"])
        return total + (d(tier.get("premium")) if not tier.get("parents") else ZERO)
    return d(tier.get("premium"))


def split(p: BenefitPlan, premium: Decimal) -> tuple[Decimal, Decimal]:
    """(employer share, employee share) of a yearly premium."""
    if p.employer_share_type == "flat":
        er = min(d(p.employer_share_value), premium)
    else:
        er = rnd(premium * d(p.employer_share_value) / 100)
    return er, premium - er


def _policy_months(p: BenefitPlan) -> int:
    end = p.policy_end + timedelta(days=1)
    return max(1, (end.year - p.policy_start.year) * 12 + end.month - p.policy_start.month)


def cover_start(p: BenefitPlan, e, on: date) -> date:
    doj = e.date_of_joining or on
    base = doj + timedelta(days=int(p.waiting_days or 0))
    if p.cover_starts == "first_of_month":
        base = base.replace(day=1)
    elif p.cover_starts == "first_of_next_month":
        base = (base.replace(day=28) + timedelta(days=4)).replace(day=1) if base.day != 1 else base
    return max(base, p.policy_start)


def windows(p: BenefitPlan, e, today: date) -> dict:
    doj = e.date_of_joining
    joining_end = (doj + timedelta(days=int(p.joining_window_days or 0))) if doj else None
    joining = bool(doj and doj <= today <= joining_end and doj >= p.policy_start - timedelta(days=60))
    renewal = bool(p.renewal_window_from and p.renewal_window_from <= today <= p.renewal_window_to)
    closes = joining_end if joining else (p.renewal_window_to if renewal else None)
    return {"open": joining or renewal, "kind": "joining" if joining else "renewal" if renewal else None,
            "closes_on": closes.isoformat() if closes else None,
            "days_left": (closes - today).days if closes else None}


# ── Enrolment ──────────────────────────────────────────────────────

def get_enrolment(db: Session, company_id: int, uuid) -> BenefitEnrolment:
    x = db.query(BenefitEnrolment).filter(BenefitEnrolment.company_id == company_id, BenefitEnrolment.uuid == uuid).first()
    if not x:
        raise NotFound("Enrolment")
    return x


def current_enrolment(db: Session, company_id: int, plan_id: int, employee_id: int) -> BenefitEnrolment | None:
    return (db.query(BenefitEnrolment).filter(BenefitEnrolment.company_id == company_id, BenefitEnrolment.plan_id == plan_id,
                                              BenefitEnrolment.employee_id == employee_id,
                                              BenefitEnrolment.status.in_(("enrolled", "waived")))
            .order_by(BenefitEnrolment.id.desc()).first())


def _history(x: BenefitEnrolment, what: str, detail: dict | None = None) -> None:
    x.history = list(x.history or []) + [{"what": what, "by": _actor(), "at": _now().isoformat(), **(detail or {})}]


def enrol(db: Session, company_id: int, p: BenefitPlan, e, *, tier: str | None, dependent_ids: list[int] | None,
          source: str, by_hr: bool) -> BenefitEnrolment:
    today = today_for_company_id(db, company_id)
    ok, why = eligible(db, company_id, p, e, today)
    if not ok:
        raise BadRequest(why or "Not eligible for this plan.")
    existing = current_enrolment(db, company_id, p.id, e.id)
    if not by_hr and source != "auto" and not windows(p, e, today)["open"]:
        raise BadRequest("Enrolment is open at joining and at renewal only. Ask HR to change your cover.")
    fam = {f["id"]: f for f in family(db, company_id, e.id)}
    deps = []
    for i in dependent_ids or []:
        f = fam.get(int(i))
        if f is None:
            raise BadRequest("Choose family members from your profile.")
        deps.append({"family_member_id": f["id"], "name": f["name"], "relation": f["relation"],
                     "relation_kind": f["relation_kind"], "gender": f["gender"], "date_of_birth": f["date_of_birth"]})
    tier = tier or (p.tiers or [{}])[0].get("code")
    premium = premium_for(p, tier, deps)
    er, ee = split(p, premium)
    si = sum_insured(p, _annual_ctc(db, company_id, e.id))
    monthly = rnd(ee / _policy_months(p)) if p.deduct_in_payroll else ZERO
    if existing is not None and existing.status == "enrolled":
        x = existing
        _history(x, "changed", {"tier": tier, "dependents": len(deps)})
    else:
        x = BenefitEnrolment(company_id=company_id, plan_id=p.id, employee_id=e.id, source=source,
                             start_date=cover_start(p, e, today) if not existing else max(today, p.policy_start))
        if existing is not None:      # waived before, joining now
            existing.status, existing.end_date = "terminated", today
        db.add(x)
        _history(x, "enrolled", {"tier": tier, "source": source})
    x.tier, x.dependents, x.status = tier, deps, "enrolled"
    x.sum_insured, x.annual_premium, x.employer_share, x.employee_share, x.monthly_deduction = si, premium, er, ee, monthly
    x.end_date = None if not e.exit_date else min(e.exit_date, p.policy_end)
    db.flush()
    audit.event(db, "benefits.enrolled", "benefit_enrolments", x.id, {"plan": p.name, "tier": tier, "premium": money(premium)},
                entity_uuid=str(x.uuid), entity_label=name_of(e))
    return x


def waive(db: Session, company_id: int, p: BenefitPlan, e, reason: str, *, by_hr: bool) -> BenefitEnrolment:
    if p.mandatory:
        raise BadRequest("This cover is compulsory and cannot be waived.")
    if not (reason or "").strip():
        raise BadRequest("Say why you are opting out.")
    today = today_for_company_id(db, company_id)
    if not by_hr and not windows(p, e, today)["open"]:
        raise BadRequest("You can opt out at joining or at renewal only.")
    x = current_enrolment(db, company_id, p.id, e.id)
    if x is not None and x.status == "enrolled":
        x.status, x.end_date = "terminated", max(today, x.start_date)
        _history(x, "ended", {"reason": "Waived"})
    w = BenefitEnrolment(company_id=company_id, plan_id=p.id, employee_id=e.id, status="waived", source="employee",
                         start_date=today, waiver_reason=reason.strip(), tier="none")
    _history(w, "waived", {"reason": reason.strip()})
    db.add(w)
    db.flush()
    return w


def terminate(db: Session, company_id: int, x: BenefitEnrolment, end_date: date, reason: str | None) -> None:
    if x.status != "enrolled":
        raise BadRequest("This enrolment is not running.")
    x.status, x.end_date = "terminated", max(end_date, x.start_date)
    _history(x, "ended", {"reason": reason})
    db.flush()


def auto_enrol(db: Session, company_id: int) -> dict:
    """New joiners into auto-enrol plans; cover ended for leavers and people no longer eligible."""
    today = today_for_company_id(db, company_id)
    Employee = _emp_model()
    emps = db.query(Employee).filter(Employee.company_id == company_id, Employee.archived_at.is_(None)).all()
    added = ended = 0
    for p in plans(db, company_id, active_only=True):
        if p.policy_end < today:
            continue
        for e in emps:
            cur = current_enrolment(db, company_id, p.id, e.id)
            if cur is not None and cur.status == "enrolled":
                if e.exit_date and e.exit_date < today:
                    terminate(db, company_id, cur, e.exit_date, "Left the company")
                    ended += 1
                elif p.eligibility_rule and not eligible(db, company_id, p, e, today)[0]:
                    terminate(db, company_id, cur, today, "No longer eligible")
                    ended += 1
                continue
            if cur is not None or not p.auto_enrol or not e.active or e.status in ("pre_joining", "exited"):
                continue
            if not eligible(db, company_id, p, e, today)[0]:
                continue
            try:
                with db.begin_nested():
                    enrol(db, company_id, p, e, tier=None, dependent_ids=[], source="auto", by_hr=True)
                added += 1
            except BadRequest:
                continue
    return {"enrolled": added, "ended": ended}


def enrolment_out(x: BenefitEnrolment, p: BenefitPlan | None = None, e=None) -> dict:
    tier = next((t for t in (p.tiers if p else []) or [] if t["code"] == x.tier), None)
    return {"uuid": str(x.uuid), "plan_uuid": str(p.uuid) if p else None, "plan_name": p.name if p else None,
            "plan_kind": p.kind if p else None, "employee_id": x.employee_id, "employee_uuid": str(e.uuid) if e else None,
            "employee_name": name_of(e) if e else None, "employee_code": e.employee_code if e else None,
            "tier": x.tier, "tier_label": tier["label"] if tier else ("Opted out" if x.status == "waived" else x.tier),
            "dependents": x.dependents or [], "sum_insured": money(x.sum_insured), "annual_premium": money(x.annual_premium),
            "employer_share": money(x.employer_share), "employee_share": money(x.employee_share),
            "monthly_deduction": money(x.monthly_deduction), "status": x.status, "status_label": STATUS.get(x.status, x.status),
            "source": x.source, "start_date": x.start_date.isoformat(), "end_date": x.end_date.isoformat() if x.end_date else None,
            "waiver_reason": x.waiver_reason, "history": x.history or []}


# ── Payroll ─────────────────────────────────────────────────────────

def payroll_deductions(db: Session, company_id: int, employee_ids: list[int], start: date, end: date) -> list[dict]:
    """The employee's share of each running cover in a pay period, pro-rated by days covered."""
    if not employee_ids:
        return []
    rows = (db.query(BenefitEnrolment, BenefitPlan).join(BenefitPlan, BenefitPlan.id == BenefitEnrolment.plan_id)
            .filter(BenefitEnrolment.company_id == company_id, BenefitEnrolment.employee_id.in_(employee_ids),
                    BenefitEnrolment.status.in_(("enrolled", "terminated")), BenefitPlan.deduct_in_payroll.is_(True),
                    BenefitEnrolment.start_date <= end, BenefitPlan.policy_start <= end, BenefitPlan.policy_end >= start)
            .all())
    out = []
    days = (end - start).days + 1
    for x, p in rows:
        if x.end_date and x.end_date < start:
            continue
        if d(x.monthly_deduction) <= 0:
            continue
        a, b = max(start, x.start_date, p.policy_start), min(end, x.end_date or end, p.policy_end)
        if a > b:
            continue
        covered = (b - a).days + 1
        amount = d(x.monthly_deduction) if covered >= days else rnd(d(x.monthly_deduction) * covered / days)
        if amount <= 0:
            continue
        out.append({"employee_id": x.employee_id, "code": PREMIUM_CODE, "amount": amount, "ref": str(x.uuid),
                    "note": f"{p.name}" + (f" ({covered} of {days} days)" if covered < days else "")})
    return out


# ── Insurer file ───────────────────────────────────────────────────

INSURER_COLUMNS = ["Action", "Effective date", "Employee ID", "Member name", "Relation", "Gender", "Date of birth", "Age",
                   "Sum insured", "Tier", "Policy number", "Employee email"]


def insurer_rows(db: Session, company_id: int, p: BenefitPlan, month: date, *, mark_sent: bool = False) -> list[list]:
    """Additions (cover starting in the month) and deletions (cover ending in the month), one
    row per insured person: the employee and each covered family member."""
    a = month.replace(day=1)
    b = (a.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    Employee = _emp_model()
    rows = []
    q = db.query(BenefitEnrolment).filter(BenefitEnrolment.company_id == company_id, BenefitEnrolment.plan_id == p.id)
    adds = q.filter(BenefitEnrolment.status.in_(("enrolled", "terminated")), BenefitEnrolment.start_date >= a,
                    BenefitEnrolment.start_date <= b).all()
    dels = q.filter(BenefitEnrolment.status == "terminated", BenefitEnrolment.end_date >= a, BenefitEnrolment.end_date <= b).all()
    emps = {e.id: e for e in db.query(Employee).filter(Employee.company_id == company_id,
                                                       Employee.id.in_([x.employee_id for x in adds + dels] or [-1])).all()}
    from core.masters import MasterValue
    gl = {m.id: m.label for m in db.query(MasterValue).filter(
        MasterValue.id.in_([e.gender_id for e in emps.values() if e.gender_id] or [-1])).all()}
    tiers = {t["code"]: t["label"] for t in p.tiers or []}
    for action, items, when in (("Addition", adds, "start_date"), ("Deletion", dels, "end_date")):
        for x in sorted(items, key=lambda r: getattr(r, when)):
            e = emps.get(x.employee_id)
            if e is None:
                continue
            on = getattr(x, when)
            people = [{"name": name_of(e), "relation": "Self", "gender": gl.get(e.gender_id),
                       "date_of_birth": e.date_of_birth.isoformat() if e.date_of_birth else None}] + list(x.dependents or [])
            for m in people:
                dob = m.get("date_of_birth")
                rows.append([action, on.strftime("%d/%m/%Y"), e.employee_code or "", m["name"], m.get("relation") or "",
                             m.get("gender") or "", date.fromisoformat(dob).strftime("%d/%m/%Y") if dob else "",
                             _age(date.fromisoformat(dob), on) if dob else "", float(d(x.sum_insured)),
                             tiers.get(x.tier, x.tier), p.policy_number or "", e.email])
            if mark_sent:
                if action == "Addition":
                    x.addition_sent_on = date.today()
                else:
                    x.deletion_sent_on = date.today()
    return rows


def insurer_file(db: Session, company_id: int, p: BenefitPlan, month: date) -> bytes:
    from openpyxl import Workbook
    rows = insurer_rows(db, company_id, p, month, mark_sent=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Endorsements"
    ws.append(INSURER_COLUMNS)
    for r in rows:
        ws.append(r)
    for col, width in zip("ABCDEFGHIJKL", (10, 13, 12, 26, 12, 9, 13, 6, 13, 22, 18, 28)):
        ws.column_dimensions[col].width = width
    buf = io.BytesIO()
    wb.save(buf)
    audit.event(db, "export.downloaded", "benefit_plans", p.id, {"report": "insurer_file", "month": month.isoformat(),
                                                                  "rows": len(rows)}, entity_uuid=str(p.uuid))
    return buf.getvalue()


# ── Self-service ───────────────────────────────────────────────────

def my_benefits(db: Session, company_id: int, e) -> dict:
    today = today_for_company_id(db, company_id)
    out = []
    for p in plans(db, company_id, active_only=True):
        if p.policy_end < today:
            continue
        ok, why = eligible(db, company_id, p, e, today)
        cur = current_enrolment(db, company_id, p.id, e.id)
        if not ok and cur is None:
            continue
        ctc = _annual_ctc(db, company_id, e.id)
        tiers = []
        for t in p.tiers or []:
            prem = d(t.get("premium"))
            er, ee = split(p, prem)
            tiers.append({**t, "employer_share": money(er), "employee_share": money(ee),
                          "monthly": money(rnd(ee / _policy_months(p))) if p.deduct_in_payroll else "0.00"})
        out.append({"plan": plan_out(p, today=today), "eligible": ok, "reason": why, "window": windows(p, e, today),
                    "enrolment": enrolment_out(cur, p, e) if cur else None, "tiers": tiers,
                    "sum_insured": money(sum_insured(p, ctc))})
    return {"plans": out, "family": family(db, company_id, e.id)}


def require_own(user: dict) -> int:
    if not user.get("employee_id"):
        raise Forbidden("Sign in as an employee to see your benefits.")
    return user["employee_id"]
