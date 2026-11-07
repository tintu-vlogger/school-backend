"""Travel requests, policy versions, travel desk bookings and settlement.

Flow of a trip
  draft -> pending (approval through the workflow engine, "travel.request") -> approved
  -> booked (the travel desk has recorded tickets and hotels) -> settling (an expense claim
  was made from the trip) -> settled (that claim was approved or paid).
  A trip can be rejected, or cancelled by the traveller before it is booked.

Policy
  The policy version in force on the trip's start date applies (versions are effective
  dated). Every leg, stay, the booking lead time and the advance are checked; the
  warnings travel with the approval request and `request.outside_policy` lets a flow add
  an extra step for exceptions.

Advance
  When the trip is approved with an advance, an approved expense advance is opened in
  Expenses. Finance (or the travel desk) pays it; the settlement claim is linked to it so
  the claim is set off against the advance and only the difference is paid with salary.
  Anything left over is returned by the employee or recovered in the final settlement.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session, selectinload

from core import audit
from core.errors import BadRequest, NotFound
from modules.travel import policy as pol
from modules.travel.models import TravelLeg, TravelPolicy, TravelRequest, TravelStay

ZERO = Decimal("0")
STATUS = {"draft": "Draft", "pending": "Waiting for approval", "approved": "Approved", "booked": "Booked",
          "settling": "Claim in progress", "settled": "Settled", "rejected": "Rejected", "cancelled": "Cancelled"}
MAX_FILE = 3_000_000  # characters of data URL (~2 MB)
FILE_PREFIXES = ("data:application/pdf;base64,", "data:image/png;base64,", "data:image/jpeg;base64,",
                 "data:image/jpg;base64,", "data:image/webp;base64,")


def now() -> datetime:
    return datetime.now(timezone.utc)


def money(v) -> str:
    return f"{pol.d(v):.2f}"


def _today(db: Session, company_id: int) -> date:
    from core.timeutil import today_for_company_id
    return today_for_company_id(db, company_id)


def _actor_name() -> str | None:
    from core import context
    return context.current().actor_name


# ── Policy versions ─────────────────────────────────────────────────

def policies(db: Session, company_id: int) -> list[TravelPolicy]:
    return (db.query(TravelPolicy).filter(TravelPolicy.company_id == company_id)
            .order_by(TravelPolicy.effective_from.desc(), TravelPolicy.version.desc()).all())


def policy_on(db: Session, company_id: int, on: date) -> TravelPolicy | None:
    """The version in force on a day: the latest effective_from on or before it (the
    earliest version when the day is before all of them)."""
    p = (db.query(TravelPolicy).filter(TravelPolicy.company_id == company_id, TravelPolicy.effective_from <= on)
         .order_by(TravelPolicy.effective_from.desc(), TravelPolicy.version.desc()).first())
    if p:
        return p
    return (db.query(TravelPolicy).filter(TravelPolicy.company_id == company_id)
            .order_by(TravelPolicy.effective_from, TravelPolicy.version).first())


def rules_on(db: Session, company_id: int, on: date) -> tuple[TravelPolicy | None, dict]:
    p = policy_on(db, company_id, on)
    return p, (p.rules if p and p.rules else pol.default_rules())


def save_policy(db: Session, company_id: int, data: dict) -> TravelPolicy:
    """A change is always a new version from a date; history stays for old trips."""
    eff = data.get("effective_from")
    if not eff:
        raise BadRequest("Choose the date the policy starts from.")
    try:
        rules = pol.clean_rules(data.get("rules") or {})
    except ValueError as exc:
        raise BadRequest(str(exc))
    last = db.query(TravelPolicy).filter(TravelPolicy.company_id == company_id).order_by(TravelPolicy.version.desc()).first()
    p = TravelPolicy(company_id=company_id, version=(last.version + 1) if last else 1,
                     name=(data.get("name") or "Travel policy").strip()[:150], effective_from=eff, rules=rules,
                     change_note=(data.get("change_note") or "").strip()[:300] or None, created_by_name=_actor_name())
    db.add(p)
    db.flush()
    return p


def policy_out(p: TravelPolicy, today: date | None = None, current_id: int | None = None) -> dict:
    return {"uuid": str(p.uuid), "version": p.version, "name": p.name, "effective_from": p.effective_from.isoformat(),
            "rules": p.rules or {}, "change_note": p.change_note, "created_by_name": p.created_by_name,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "in_force": current_id == p.id, "future": bool(today and p.effective_from > today)}


def ensure_defaults(db: Session, company_id: int) -> None:
    """Version 1 of the travel policy and the daily allowance expense category."""
    if not db.query(TravelPolicy.id).filter(TravelPolicy.company_id == company_id).first():
        db.add(TravelPolicy(company_id=company_id, version=1, name="Travel policy", effective_from=date(2000, 1, 1),
                            rules=pol.default_rules(), change_note="Default policy", created_by_name="System"))
    from modules.expenses import service as exp
    codes = {c.code for c in exp.categories(db, company_id)}
    if codes and "PERDIEM" not in codes:
        exp.save_category(db, company_id, {"code": "PERDIEM", "name": "Daily allowance (travel)", "icon": "wallet",
                                           "kind": "per_day", "receipt_required_above": None, "gst_applicable": False,
                                           "taxable": False, "pay_via": "payroll", "sort_order": 15,
                                           "limits": [], "mileage_rates": {}, "active": True})
    db.flush()


# ── Trips ───────────────────────────────────────────────────────────

def get_trip(db: Session, company_id: int, uuid) -> TravelRequest:
    t = (db.query(TravelRequest).options(selectinload(TravelRequest.legs), selectinload(TravelRequest.stays))
         .filter(TravelRequest.company_id == company_id, TravelRequest.uuid == uuid).first())
    if not t:
        raise NotFound("Trip")
    return t


def _grade(db: Session, emp) -> str | None:
    from modules.payroll import adapters
    return adapters.grade_code(db, getattr(emp, "grade_id", None))


def _project_id(db: Session, company_id: int, project_uuid) -> int | None:
    if not project_uuid:
        return None
    from modules.timesheets.models import TsProject
    p = db.query(TsProject).filter(TsProject.company_id == company_id, TsProject.uuid == project_uuid).first()
    if not p:
        raise BadRequest("Choose a project from the list.")
    return p.id


def _plain(trip: TravelRequest) -> dict:
    return {"travel_type": trip.travel_type, "start_date": trip.start_date, "end_date": trip.end_date,
            "advance_amount": trip.advance_amount, "estimated_total": trip.estimated_total,
            "legs": [{"mode": l.mode, "travel_class": l.travel_class, "from_place": l.from_place, "to_place": l.to_place,
                      "depart_date": l.depart_date, "estimated_cost": l.estimated_cost} for l in trip.legs],
            "stays": [{"city": s.city, "check_in": s.check_in, "check_out": s.check_out, "nightly_cost": s.nightly_cost}
                      for s in trip.stays]}


def evaluate(db: Session, company_id: int, trip: TravelRequest, emp, today: date | None = None) -> list[dict]:
    """Work out tiers, per diem, the estimate and the policy warnings for a trip."""
    today = today or _today(db, company_id)
    p, rules = rules_on(db, company_id, trip.start_date)
    grade = _grade(db, emp)
    intl = trip.travel_type == "international"
    for s in trip.stays:
        s.tier = pol.city_tier(rules, s.city, intl)
    plain = _plain(trip)
    est = pol.estimate(rules, grade, plain)
    trip.per_diem_days, trip.per_diem_rate, trip.estimated_total = est["per_diem_days"], est["per_diem_rate"], est["total"]
    plain["estimated_total"] = est["total"]
    flags = pol.check(rules, grade, plain, today)
    for i, l in enumerate(trip.legs, start=1):
        l.flags = [f["message"] for f in flags if f["where"] == f"Journey {i}"]
    for i, s in enumerate(trip.stays, start=1):
        s.flags = [f["message"] for f in flags if f["where"] == f"Stay {i}"]
    trip.policy_id = p.id if p else None
    trip.grade_code = grade
    trip.policy_flags = flags
    trip.outside_policy = bool(flags)
    return flags


def save_trip(db: Session, company_id: int, emp, data: dict, trip: TravelRequest | None = None) -> TravelRequest:
    if trip is not None and trip.status not in ("draft", "rejected"):
        raise BadRequest("Only a draft trip can be changed.")
    purpose = (data.get("purpose") or "").strip()
    if not purpose:
        raise BadRequest("Say what the trip is for.")
    start, end = data.get("start_date"), data.get("end_date")
    if not start or not end:
        raise BadRequest("Enter the trip dates.")
    if end < start:
        raise BadRequest("The trip cannot end before it starts.")
    if (end - start).days > 180:
        raise BadRequest("A trip can be at most 180 days long.")
    ttype = data.get("travel_type") or "domestic"
    if ttype not in ("domestic", "international"):
        raise BadRequest("Choose domestic or international.")
    legs = data.get("legs") or []
    if not legs:
        raise BadRequest("Add at least one journey.")
    if trip is None:
        trip = TravelRequest(company_id=company_id, employee_id=emp.id, status="draft")
        db.add(trip)
    trip.purpose, trip.start_date, trip.end_date, trip.travel_type = purpose[:300], start, end, ttype
    trip.project_id = _project_id(db, company_id, data.get("project_uuid"))
    trip.billable = bool(data.get("billable")) and trip.project_id is not None
    trip.accommodation_needed = bool(data.get("accommodation_needed", True))
    adv = pol.d(data.get("advance_amount"))
    if adv < 0:
        raise BadRequest("The advance cannot be negative.")
    trip.advance_amount = adv
    trip.notes = (data.get("notes") or "").strip() or None
    trip.exception_reason = (data.get("exception_reason") or "").strip() or None
    for old in list(trip.legs):
        trip.legs.remove(old)
    for old in list(trip.stays):
        trip.stays.remove(old)
    db.flush()
    for i, l in enumerate(legs, start=1):
        mode = l.get("mode")
        if mode not in pol.MODES:
            raise BadRequest(f"Journey {i}: choose how you will travel.")
        cls = l.get("travel_class") or None
        if cls and cls not in pol.MODES[mode][1]:
            raise BadRequest(f"Journey {i}: choose a class from the list.")
        if not (l.get("from_place") or "").strip() or not (l.get("to_place") or "").strip():
            raise BadRequest(f"Journey {i}: enter where from and where to.")
        if not l.get("depart_date"):
            raise BadRequest(f"Journey {i}: enter the date.")
        cost = pol.d(l.get("estimated_cost"))
        if cost < 0:
            raise BadRequest(f"Journey {i}: the cost cannot be negative.")
        trip.legs.append(TravelLeg(company_id=company_id, seq=i, mode=mode, travel_class=cls,
                                   from_place=l["from_place"].strip()[:150], to_place=l["to_place"].strip()[:150],
                                   depart_date=l["depart_date"], preferred_time=(l.get("preferred_time") or None),
                                   distance_km=pol.d(l["distance_km"]) if l.get("distance_km") else None,
                                   estimated_cost=cost, notes=(l.get("notes") or None)))
    if trip.accommodation_needed:
        for i, s in enumerate(data.get("stays") or [], start=1):
            if not (s.get("city") or "").strip():
                raise BadRequest(f"Stay {i}: enter the city.")
            if not s.get("check_in") or not s.get("check_out") or s["check_out"] < s["check_in"]:
                raise BadRequest(f"Stay {i}: check-out must be on or after check-in.")
            cost = pol.d(s.get("nightly_cost"))
            if cost < 0:
                raise BadRequest(f"Stay {i}: the cost cannot be negative.")
            trip.stays.append(TravelStay(company_id=company_id, seq=i, city=s["city"].strip()[:150], check_in=s["check_in"],
                                         check_out=s["check_out"], nightly_cost=cost, notes=(s.get("notes") or None)))
    db.flush()
    evaluate(db, company_id, trip, emp)
    db.flush()
    return trip


def submit(db: Session, company_id: int, trip: TravelRequest, emp) -> TravelRequest:
    if trip.status not in ("draft", "rejected"):
        raise BadRequest("This trip has already been sent.")
    flags = evaluate(db, company_id, trip, emp)
    if flags and not trip.exception_reason:
        raise BadRequest("This trip is outside the travel policy. Give a reason for the exception before sending it.")
    from core import numbering
    from core.workflow import service as workflow
    from modules.payroll.service import name_of
    today = _today(db, company_id)
    trip.number = trip.number or numbering.next_number(db, company_id, "request", today)
    trip.status = "pending"
    trip.submitted_at = now()
    trip.department_id = getattr(emp, "department_id", None)
    db.flush()
    payload = {"trip_uuid": str(trip.uuid), "purpose": trip.purpose, "employee_name": name_of(emp),
               "amount": float(trip.estimated_total), "advance": float(trip.advance_amount),
               "international": trip.travel_type == "international", "outside_policy": bool(flags),
               "flags": len(flags), "flag_text": [f["message"] for f in flags][:5], "billable": trip.billable,
               "start_date": trip.start_date.isoformat(), "end_date": trip.end_date.isoformat(),
               "route": " → ".join([trip.legs[0].from_place] + [l.to_place for l in trip.legs]) if trip.legs else "",
               "exception_reason": trip.exception_reason}
    inst = workflow.submit(db, company_id, "travel.request", payload,
                           f"Trip: {trip.purpose} ({trip.start_date.strftime('%d %b')})", subject_type="travel_request",
                           subject_id=trip.id)
    trip.workflow_instance_id = inst.id
    db.flush()
    return trip


def _trip_from(db: Session, instance) -> TravelRequest | None:
    return db.query(TravelRequest).filter(TravelRequest.company_id == instance.company_id,
                                          TravelRequest.uuid == (instance.payload or {}).get("trip_uuid")).first()


def on_approved(db: Session, instance) -> None:
    trip = _trip_from(db, instance)
    if not trip or trip.status != "pending":
        return
    trip.status = "approved"
    trip.decided_note = instance.result_note
    if pol.d(trip.advance_amount) > 0 and not trip.advance_id:
        from modules.expenses import service as exp
        adv = exp.create_advance(db, trip.company_id, trip.employee_id, trip.advance_amount,
                                 f"Travel advance: {trip.purpose}", trip.start_date, number=trip.number)
        trip.advance_id = adv.id


def on_closed(db: Session, instance) -> None:
    trip = _trip_from(db, instance)
    if trip and trip.status == "pending":
        trip.status = "rejected" if instance.status == "rejected" else "cancelled"
        trip.decided_note = instance.result_note


def cancel(db: Session, company_id: int, trip: TravelRequest) -> TravelRequest:
    """The traveller withdraws a trip. Booked trips are cancelled by the travel desk."""
    if trip.status == "draft":
        trip.status = "cancelled"
    elif trip.status == "pending" and trip.workflow_instance_id:
        from core.workflow import service as workflow
        from core.workflow.models import WorkflowInstance
        inst = db.query(WorkflowInstance).filter(WorkflowInstance.id == trip.workflow_instance_id).first()
        workflow.cancel(db, company_id, inst, "Withdrawn by the traveller")
    elif trip.status in ("approved", "booked"):
        if any(x.booking_status == "booked" for x in [*trip.legs, *trip.stays]):
            raise BadRequest("Tickets or hotels are already booked. Ask the travel desk to cancel them first.")
        from modules.expenses import service as exp
        adv = exp.advance_by_id(db, company_id, trip.advance_id)
        if adv:
            exp.cancel_advance(db, company_id, adv)
        trip.status = "cancelled"
        audit.event(db, "travel.cancelled", "travel_requests", trip.id, {"number": trip.number},
                    entity_uuid=str(trip.uuid), entity_label=trip.purpose)
    else:
        raise BadRequest("This trip can no longer be cancelled.")
    return trip


# ── Travel desk ─────────────────────────────────────────────────────

def _item(trip: TravelRequest, kind: str, uuid):
    items = trip.legs if kind == "leg" else trip.stays if kind == "stay" else None
    if items is None:
        raise NotFound("Booking")
    it = next((x for x in items if str(x.uuid) == str(uuid)), None)
    if not it:
        raise NotFound("Booking")
    return it


def record_booking(db: Session, company_id: int, trip: TravelRequest, kind: str, uuid, data: dict):
    if trip.status not in ("approved", "booked"):
        raise BadRequest("Bookings are made once the trip is approved.")
    it = _item(trip, kind, uuid)
    status = data.get("booking_status") or "booked"
    if status not in ("open", "booked", "cancelled"):
        raise BadRequest("Choose booked, cancelled or not booked yet.")
    paid_by = data.get("paid_by") or "company"
    if paid_by not in ("company", "employee"):
        raise BadRequest("Choose who paid: the company or the traveller.")
    cost = data.get("booked_cost")
    if status == "booked" and (cost is None or pol.d(cost) < 0):
        raise BadRequest("Enter what the booking cost.")
    f = data.get("file_data")
    if f:
        if len(f) > MAX_FILE:
            raise BadRequest("The ticket file is too large (2 MB at most).")
        if not f.startswith(FILE_PREFIXES):
            raise BadRequest("Tickets must be a PDF or a photo.")
        it.file_data, it.file_name = f, (data.get("file_name") or "ticket")[:255]
    elif data.get("remove_file"):
        it.file_data = it.file_name = None
    it.booking_status, it.paid_by = status, paid_by
    it.vendor = (data.get("vendor") or "").strip()[:150] or None
    it.booking_ref = (data.get("booking_ref") or "").strip()[:80] or None
    it.booked_cost = pol.d(cost) if cost is not None else None
    it.booked_by_name, it.booked_on = _actor_name(), now()
    audit.event(db, "travel.booking_recorded", "travel_requests", trip.id,
                {"item": kind, "status": status, "reference": it.booking_ref, "cost": str(it.booked_cost or "")},
                entity_uuid=str(trip.uuid), entity_label=trip.purpose)
    _refresh_booked(trip)
    db.flush()
    return it


def _refresh_booked(trip: TravelRequest) -> None:
    items = [*trip.legs, *trip.stays]
    open_items = [x for x in items if x.booking_status == "open" and not (isinstance(x, TravelLeg) and x.mode == "own_vehicle")]
    if trip.status == "approved" and items and not open_items:
        trip.status, trip.booked_at = "booked", now()
    elif trip.status == "booked" and open_items:
        trip.status, trip.booked_at = "approved", None


def mark_booked(db: Session, company_id: int, trip: TravelRequest) -> TravelRequest:
    """The desk is done with this trip (anything left open is booked by the traveller)."""
    if trip.status != "approved":
        raise BadRequest("Only an approved trip can be marked booked.")
    trip.status, trip.booked_at = "booked", now()
    audit.event(db, "travel.booked", "travel_requests", trip.id, {"number": trip.number}, entity_uuid=str(trip.uuid),
                entity_label=trip.purpose)
    return trip


def desk_cancel(db: Session, company_id: int, trip: TravelRequest, reason: str | None) -> TravelRequest:
    if trip.status not in ("approved", "booked"):
        raise BadRequest("Only an approved or booked trip can be cancelled here.")
    for x in [*trip.legs, *trip.stays]:
        if x.booking_status == "booked":
            x.booking_status = "cancelled"
    from modules.expenses import service as exp
    adv = exp.advance_by_id(db, company_id, trip.advance_id)
    if adv and adv.status in ("pending", "approved"):
        exp.cancel_advance(db, company_id, adv)
    trip.status = "cancelled"
    trip.decided_note = (reason or "").strip() or trip.decided_note
    audit.event(db, "travel.cancelled_by_desk", "travel_requests", trip.id, {"reason": reason},
                entity_uuid=str(trip.uuid), entity_label=trip.purpose)
    return trip


def pay_advance(db: Session, company_id: int, trip: TravelRequest, on: date, reference: str | None) -> None:
    from modules.expenses import service as exp
    adv = exp.advance_by_id(db, company_id, trip.advance_id)
    if not adv:
        raise BadRequest("This trip has no advance.")
    exp.pay_advance(db, company_id, adv, on, reference)


def return_advance(db: Session, company_id: int, trip: TravelRequest, reference: str | None) -> None:
    """The traveller paid back what was left of the advance after the claim."""
    from modules.expenses import service as exp
    adv = exp.advance_by_id(db, company_id, trip.advance_id)
    if not adv or adv.status != "paid":
        raise BadRequest("There is no paid advance to close.")
    left = pol.d(adv.amount) - pol.d(adv.settled_amount) - pol.d(adv.recovered_amount)
    if left <= 0:
        raise BadRequest("Nothing is left of this advance.")
    if trip.status not in ("settled", "settling", "cancelled"):
        raise BadRequest("Settle the trip first, so the claim is set off against the advance.")
    exp.recover_advances(db, company_id, [str(adv.uuid)])
    audit.event(db, "travel.advance_returned", "expense_advances", adv.id, {"amount": str(left), "reference": reference},
                entity_uuid=str(adv.uuid), entity_label=trip.purpose)


# ── Settlement ──────────────────────────────────────────────────────

MODE_WORD = {k: v[0] for k, v in pol.MODES.items()}


def settlement_lines(db: Session, company_id: int, trip: TravelRequest) -> list[dict]:
    """Expense claim lines for a trip: what the traveller paid themselves (journeys and
    hotels the company did not book) and the daily allowance. Company-paid bookings are
    never claimed."""
    from modules.expenses import service as exp
    _, rules = rules_on(db, company_id, trip.start_date)
    want = rules.get("claim_categories") or {}
    cats = {c.code: c for c in exp.categories(db, company_id, active_only=True)}

    def cat(key: str, fallback: str):
        c = cats.get(want.get(key) or fallback) or cats.get(fallback)
        if not c:
            raise BadRequest(f"Set up an active expense category for {key.replace('_', ' ')} (code {want.get(key) or fallback}).")
        return c

    lines = []
    for l in trip.legs:
        if l.booking_status == "cancelled" or (l.booking_status == "booked" and l.paid_by == "company"):
            continue
        amount = pol.d(l.booked_cost) if l.booking_status == "booked" and l.booked_cost is not None else pol.d(l.estimated_cost)
        fuel = cats.get("FUEL")
        if l.mode == "own_vehicle" and l.distance_km and fuel is not None and fuel.kind == "mileage":
            lines.append({"category_uuid": str(fuel.uuid), "expense_date": l.depart_date, "distance_km": l.distance_km,
                          "vehicle": l.travel_class or "car", "from_place": l.from_place, "to_place": l.to_place,
                          "description": f"{MODE_WORD.get(l.mode, l.mode)} {l.from_place} → {l.to_place}"})
            continue
        if amount <= 0:
            continue
        c = cat("ticket", "TRAVEL")
        lines.append({"category_uuid": str(c.uuid), "expense_date": l.depart_date, "amount": amount,
                      "merchant": l.vendor, "from_place": l.from_place, "to_place": l.to_place,
                      "invoice_number": l.booking_ref,
                      "description": f"{MODE_WORD.get(l.mode, l.mode)} {l.from_place} → {l.to_place}"})
    for s in trip.stays:
        if s.booking_status == "cancelled" or (s.booking_status == "booked" and s.paid_by == "company"):
            continue
        n = pol.nights({"check_in": s.check_in, "check_out": s.check_out})
        total = pol.d(s.booked_cost) if s.booking_status == "booked" and s.booked_cost is not None else pol.d(s.nightly_cost) * n
        if total <= 0 or n <= 0:
            continue
        c = cat("hotel", "HOTEL")
        per_night = (total / n).quantize(Decimal("0.01"))
        for i in range(n):
            amt = per_night if i < n - 1 else total - per_night * (n - 1)
            lines.append({"category_uuid": str(c.uuid), "expense_date": s.check_in + timedelta(days=i), "amount": amt,
                          "merchant": s.vendor, "invoice_number": s.booking_ref,
                          "description": f"Hotel in {s.city}, night {i + 1} of {n}"})
    rate = pol.d(trip.per_diem_rate)
    if rate > 0 and trip.per_diem_days:
        c = cat("per_diem", "PERDIEM")
        for i in range(trip.per_diem_days):
            lines.append({"category_uuid": str(c.uuid), "expense_date": trip.start_date + timedelta(days=i), "amount": rate,
                          "description": f"Daily allowance, day {i + 1} of {trip.per_diem_days}"})
    return lines


def settle(db: Session, company_id: int, trip: TravelRequest, emp) -> TravelRequest:
    """After the trip: open an expense claim filled with the trip's journeys, hotels and
    daily allowance, linked to the advance. The traveller adds bills and sends it."""
    if trip.status not in ("approved", "booked"):
        raise BadRequest("Only an approved trip can be settled." if trip.status not in ("settling", "settled")
                         else "This trip has already been settled.")
    if trip.start_date > _today(db, company_id):
        raise BadRequest("Settle the trip once it has started.")
    from modules.expenses import service as exp
    lines = settlement_lines(db, company_id, trip)
    adv = exp.advance_by_id(db, company_id, trip.advance_id)
    if adv and adv.status not in ("approved", "paid"):
        adv = None
    if not lines:
        trip.status, trip.settled_at = "settled", now()
        audit.event(db, "travel.settled", "travel_requests", trip.id, {"claim": None, "note": "Nothing to claim"},
                    entity_uuid=str(trip.uuid), entity_label=trip.purpose)
        return trip
    data = {"title": f"Trip: {trip.purpose}"[:200] + (f" ({trip.number})" if trip.number else ""),
            "trip_from": trip.start_date, "trip_to": trip.end_date, "lines": lines}
    if adv:
        data["advance_uuid"] = str(adv.uuid)
    claim = exp.save_claim(db, company_id, emp, data)
    trip.claim_id = claim.id
    trip.status = "settling"
    audit.event(db, "travel.settlement_started", "travel_requests", trip.id,
                {"claim_uuid": str(claim.uuid), "lines": len(lines), "total": str(claim.total)},
                entity_uuid=str(trip.uuid), entity_label=trip.purpose)
    db.flush()
    return trip


def sync(db: Session, company_id: int, trips: list[TravelRequest]) -> None:
    """Follow the settlement claim: approved or paid settles the trip; a withdrawn claim
    lets the traveller settle again."""
    from modules.expenses import service as exp
    waiting = [t for t in trips if t.status == "settling" and t.claim_id]
    if not waiting:
        return
    claims = exp.claims_by_ids(db, company_id, [t.claim_id for t in waiting])
    for t in waiting:
        c = claims.get(t.claim_id)
        if c is None or c.status == "cancelled":
            t.status = "booked" if t.booked_at else "approved"
            t.claim_id = None
        elif c.status in ("approved", "paid"):
            t.status, t.settled_at = "settled", now()


# ── Output ──────────────────────────────────────────────────────────

def _booking_out(x) -> dict:
    return {"booking_status": x.booking_status, "paid_by": x.paid_by, "vendor": x.vendor, "booking_ref": x.booking_ref,
            "booked_cost": money(x.booked_cost) if x.booked_cost is not None else None, "file_name": x.file_name,
            "has_file": bool(x.file_data), "booked_by_name": x.booked_by_name,
            "booked_on": x.booked_on.isoformat() if x.booked_on else None}


def company_paid(trip: TravelRequest) -> Decimal:
    return sum((pol.d(x.booked_cost) for x in [*trip.legs, *trip.stays]
                if x.booking_status == "booked" and x.paid_by == "company"), ZERO)


def actual_cost(trip: TravelRequest, claim) -> Decimal | None:
    """What the trip really cost: company bookings plus the approved claim (None until settled)."""
    if trip.status != "settled":
        return None
    claimed = pol.d(claim.approved_total) if claim is not None and claim.status in ("approved", "paid") else ZERO
    return company_paid(trip) + claimed


def trip_out(t: TravelRequest, *, names: dict | None = None, projects: dict | None = None, depts: dict | None = None,
             claim=None, advance=None, full: bool = False) -> dict:
    proj = (projects or {}).get(t.project_id) if t.project_id else None
    out = {"uuid": str(t.uuid), "number": t.number, "employee_id": t.employee_id,
           "employee_name": (names or {}).get(t.employee_id), "purpose": t.purpose, "travel_type": t.travel_type,
           "start_date": t.start_date.isoformat(), "end_date": t.end_date.isoformat(), "status": t.status,
           "status_label": STATUS.get(t.status, t.status),
           "route": " → ".join([t.legs[0].from_place] + [l.to_place for l in t.legs]) if t.legs else "",
           "modes": sorted({l.mode for l in t.legs}),
           "project_uuid": proj["uuid"] if proj else None, "project_name": proj["name"] if proj else None,
           "client_name": proj.get("client_name") if proj else None, "billable": t.billable,
           "department_name": (depts or {}).get(t.department_id),
           "estimated_total": money(t.estimated_total), "advance_amount": money(t.advance_amount),
           "per_diem_days": t.per_diem_days, "per_diem_rate": money(t.per_diem_rate),
           "outside_policy": t.outside_policy, "policy_flags": t.policy_flags or [],
           "open_bookings": sum(1 for x in [*t.legs, *t.stays] if x.booking_status == "open"
                                and not (isinstance(x, TravelLeg) and x.mode == "own_vehicle")),
           "company_paid": money(company_paid(t)),
           "submitted_at": t.submitted_at.isoformat() if t.submitted_at else None,
           "created_at": t.created_at.isoformat() if t.created_at else None}
    if full:
        out.update({
            "accommodation_needed": t.accommodation_needed, "notes": t.notes, "exception_reason": t.exception_reason,
            "decided_note": t.decided_note, "grade_code": t.grade_code,
            "legs": [{"uuid": str(l.uuid), "mode": l.mode, "travel_class": l.travel_class, "from_place": l.from_place,
                      "to_place": l.to_place, "depart_date": l.depart_date.isoformat(), "preferred_time": l.preferred_time,
                      "distance_km": str(l.distance_km) if l.distance_km is not None else None,
                      "estimated_cost": money(l.estimated_cost), "notes": l.notes, "flags": l.flags or [], **_booking_out(l)}
                     for l in t.legs],
            "stays": [{"uuid": str(s.uuid), "city": s.city, "tier": s.tier, "check_in": s.check_in.isoformat(),
                       "check_out": s.check_out.isoformat(), "nights": pol.nights({"check_in": s.check_in, "check_out": s.check_out}),
                       "nightly_cost": money(s.nightly_cost), "notes": s.notes, "flags": s.flags or [], **_booking_out(s)}
                      for s in t.stays],
            "claim": ({"uuid": str(claim.uuid), "number": claim.number, "status": claim.status, "total": money(claim.total),
                       "approved_total": money(claim.approved_total), "advance_adjusted": money(claim.advance_adjusted)}
                      if claim is not None else None),
            "advance": ({"uuid": str(advance.uuid), "status": advance.status, "amount": money(advance.amount),
                         "settled_amount": money(advance.settled_amount), "recovered_amount": money(advance.recovered_amount),
                         "balance": money(pol.d(advance.amount) - pol.d(advance.settled_amount) - pol.d(advance.recovered_amount)),
                         "paid_on": advance.paid_on.isoformat() if advance.paid_on else None,
                         "payment_ref": advance.payment_ref} if advance is not None else None),
            "actual_cost": (money(actual_cost(t, claim)) if t.status == "settled" else None),
        })
    return out


def lookups(db: Session, company_id: int, trips: list[TravelRequest]) -> dict:
    """Names of employees, projects and departments for a page of trips (one query each)."""
    from modules.payroll import service as psvc
    E = psvc.employee_model()
    emp_ids = {t.employee_id for t in trips}
    names = {e.id: psvc.name_of(e) for e in db.query(E).filter(E.company_id == company_id, E.id.in_(list(emp_ids) or [-1])).all()}
    projects = project_names(db, company_id, {t.project_id for t in trips if t.project_id})
    depts = department_names(db, company_id, {t.department_id for t in trips if t.department_id})
    return {"names": names, "projects": projects, "depts": depts}


def project_names(db: Session, company_id: int, ids) -> dict:
    if not ids:
        return {}
    from modules.timesheets.models import TsProject
    rows = db.query(TsProject).filter(TsProject.company_id == company_id, TsProject.id.in_(list(ids))).all()
    return {p.id: {"uuid": str(p.uuid), "name": p.name, "client_name": p.client.name if p.client else None} for p in rows}


def department_names(db: Session, company_id: int, ids) -> dict:
    if not ids:
        return {}
    from modules.departments.models import Department
    rows = db.query(Department).filter(Department.company_id == company_id, Department.id.in_(list(ids))).all()
    return {x.id: x.department_name for x in rows}


# ── Reports ─────────────────────────────────────────────────────────

def _in_range(db: Session, company_id: int, start: date, end: date) -> list[TravelRequest]:
    return (db.query(TravelRequest).options(selectinload(TravelRequest.legs), selectinload(TravelRequest.stays))
            .filter(TravelRequest.company_id == company_id, TravelRequest.start_date >= start, TravelRequest.start_date <= end,
                    TravelRequest.status.notin_(("draft", "cancelled", "rejected"))).all())


def spend_report(db: Session, company_id: int, start: date, end: date, by: str = "department") -> dict:
    """Trips and spend grouped by department, project, employee or mode. Spend is what the
    company paid for bookings plus approved claims; estimated cost covers trips not yet settled."""
    if by not in ("department", "project", "employee", "mode", "type"):
        raise BadRequest("Group by department, project, employee, mode or type.")
    trips = _in_range(db, company_id, start, end)
    from modules.expenses import service as exp
    claims = exp.claims_by_ids(db, company_id, [t.claim_id for t in trips])
    lk = lookups(db, company_id, trips)
    groups: dict = {}
    for t in trips:
        claim = claims.get(t.claim_id)
        if by == "department":
            keys = [(t.department_id, lk["depts"].get(t.department_id) or "No department")]
        elif by == "project":
            p = lk["projects"].get(t.project_id)
            keys = [(t.project_id, (p["name"] + (f" · {p['client_name']}" if p.get("client_name") else "")) if p else "No project")]
        elif by == "employee":
            keys = [(t.employee_id, lk["names"].get(t.employee_id) or "")]
        elif by == "type":
            keys = [(t.travel_type, "International" if t.travel_type == "international" else "Domestic")]
        else:
            keys = [(m, MODE_WORD.get(m, m)) for m in sorted({l.mode for l in t.legs})] or [("none", "No journeys")]
        for key, label in keys:
            g = groups.setdefault(key, {"key": str(key), "label": label, "trips": 0, "estimated": ZERO, "company_paid": ZERO,
                                        "claimed": ZERO, "actual": ZERO, "billable": ZERO, "outside_policy": 0})
            share = Decimal(1) / len(keys)
            g["trips"] += 1
            g["estimated"] += pol.d(t.estimated_total) * share
            g["company_paid"] += company_paid(t) * share
            cl = pol.d(claim.approved_total) if claim is not None and claim.status in ("approved", "paid") else ZERO
            g["claimed"] += cl * share
            spent = company_paid(t) + cl if t.status == "settled" else pol.d(t.estimated_total)
            g["actual"] += spent * share
            if t.billable:
                g["billable"] += spent * share
            g["outside_policy"] += 1 if t.outside_policy else 0
    rows = sorted(groups.values(), key=lambda g: -g["actual"])
    for g in rows:
        for k in ("estimated", "company_paid", "claimed", "actual", "billable"):
            g[k] = money(g[k])
    totals = {"trips": len(trips), "spend": money(sum((pol.d(r["actual"]) for r in rows), ZERO)),
              "billable": money(sum((pol.d(r["billable"]) for r in rows), ZERO)),
              "outside_policy": sum(1 for t in trips if t.outside_policy)}
    return {"by": by, "rows": rows, "totals": totals}


def breaches_report(db: Session, company_id: int, start: date, end: date) -> dict:
    """Trips sent outside the policy: what was broken, by whom and how often."""
    trips = [t for t in _in_range(db, company_id, start, end) if t.outside_policy]
    lk = lookups(db, company_id, trips)
    kinds: dict = {}
    labels = {"mode": "Mode not allowed", "class": "Class above grade", "hotel": "Hotel above limit",
              "lead_time": "Booked at short notice", "advance": "Advance above limit", "dates": "Dates outside the trip"}
    for t in trips:
        for f in t.policy_flags or []:
            k = f.get("code") or "other"
            kinds[k] = kinds.get(k, 0) + 1
    return {"by_kind": [{"code": k, "label": labels.get(k, k), "count": n} for k, n in sorted(kinds.items(), key=lambda x: -x[1])],
            "trips": [trip_out(t, names=lk["names"], projects=lk["projects"], depts=lk["depts"]) | {"exception_reason": t.exception_reason}
                      for t in sorted(trips, key=lambda x: x.start_date, reverse=True)]}
