"""Transport rules: seat capacity, rider allocation and route changes, fees, expiry reminders.

Functions other modules may use:
    current_rider(db, company_id, student_id, on=None) -> TransportRider | None
    rider_summary(db, company_id, student_id)          -> dict for the portal / home card

Rules:
- A route's seats are its own capacity if set, else its vehicle's capacity. A learner can be
  allocated only while riders active on the start date (and any later day) are below it.
- A learner has at most one live allocation. Allocating again ends the earlier one the day
  before the new start (a route change) and tells the parents.
- The fee is the stop's own fee, else its zone's fee. It is billed through fees when that
  module is installed, otherwise only recorded on the allocation.
- Student.uses_transport follows whether the learner has a live allocation.
"""
from datetime import date, timedelta
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from core import audit
from core.context import current as current_ctx
from modules.transport.models import TransportCrew, TransportRider, TransportRoute, TransportStop, TransportVehicle

EXPIRY_FIELDS = {"insurance_expiry": "Insurance", "permit_expiry": "Permit", "fitness_expiry": "Fitness certificate",
                 "pollution_expiry": "Pollution certificate"}
REMIND_DAYS = 30


def actor() -> str:
    return current_ctx().actor_name or "System"


def today(db: Session, company_id: int) -> date:
    from core.timeutil import today_for_company_id
    return today_for_company_id(db, company_id)


def seats(route: TransportRoute) -> int | None:
    if route.capacity:
        return route.capacity
    return route.vehicle.capacity if route.vehicle is not None else None


def _live(q, on: date):
    return q.filter(TransportRider.status == "active", TransportRider.start_date <= on,
                    or_(TransportRider.end_date.is_(None), TransportRider.end_date >= on))


def riders_on(db: Session, company_id: int, route_id: int, on: date, exclude_student: int | None = None) -> int:
    q = db.query(func.count(TransportRider.id)).filter(TransportRider.company_id == company_id,
                                                       TransportRider.route_id == route_id)
    if exclude_student:
        q = q.filter(TransportRider.student_id != exclude_student)
    return _live(q, on).scalar() or 0


def peak_riders(db: Session, company_id: int, route_id: int, start: date, end: date | None,
                exclude_student: int | None = None) -> int:
    """Most riders on any day from start to end (changes only happen on start dates)."""
    q = db.query(TransportRider).filter(TransportRider.company_id == company_id, TransportRider.route_id == route_id,
                                        TransportRider.status == "active",
                                        or_(TransportRider.end_date.is_(None), TransportRider.end_date >= start))
    if end:
        q = q.filter(TransportRider.start_date <= end)
    if exclude_student:
        q = q.filter(TransportRider.student_id != exclude_student)
    rows = q.all()
    days = {start} | {r.start_date for r in rows if r.start_date > start}
    best = 0
    for d in days:
        n = sum(1 for r in rows if r.start_date <= d and (r.end_date is None or r.end_date >= d))
        best = max(best, n)
    return best


def stop_fee(stop: TransportStop) -> Decimal | None:
    if stop.fee is not None:
        return Decimal(stop.fee)
    if stop.zone is not None:
        return Decimal(stop.zone.fee)
    return None


def current_rider(db: Session, company_id: int, student_id: int, on: date | None = None) -> TransportRider | None:
    on = on or today(db, company_id)
    q = db.query(TransportRider).options(
        joinedload(TransportRider.route).joinedload(TransportRoute.vehicle),
        joinedload(TransportRider.route).joinedload(TransportRoute.driver),
        joinedload(TransportRider.route).joinedload(TransportRoute.attendant),
        joinedload(TransportRider.stop)).filter(TransportRider.company_id == company_id,
                                                TransportRider.student_id == student_id)
    row = _live(q, on).order_by(TransportRider.start_date.desc()).first()
    if row is None:  # an allocation starting soon
        row = q.filter(TransportRider.status == "active", TransportRider.start_date > on).order_by(
            TransportRider.start_date).first()
    return row


def _sync_flag(db: Session, company_id: int, student_id: int) -> None:
    from modules.students.models import Student
    s = db.query(Student).filter(Student.company_id == company_id, Student.id == student_id).first()
    if s is None:
        return
    on = today(db, company_id)
    live = db.query(TransportRider.id).filter(
        TransportRider.company_id == company_id, TransportRider.student_id == student_id,
        TransportRider.status == "active", or_(TransportRider.end_date.is_(None), TransportRider.end_date >= on)).first()
    if s.uses_transport != bool(live):
        s.uses_transport = bool(live)


def allocate(db: Session, company_id: int, *, student_id: int, route: TransportRoute, stop: TransportStop,
             start_date: date, end_date: date | None = None, direction: str = "both", notes: str | None = None,
             bill: bool = False, notify: bool = True) -> TransportRider:
    from modules.students.models import Student
    s = db.query(Student).filter(Student.company_id == company_id, Student.id == student_id).first()
    if s is None:
        raise HTTPException(status_code=400, detail="The student was not found.")
    if s.status != "active":
        raise HTTPException(status_code=400, detail=f"{s.full_name} is not an active student.")
    if not route.active:
        raise HTTPException(status_code=400, detail="This route is switched off.")
    if stop.route_id != route.id:
        raise HTTPException(status_code=400, detail="The stop is not on this route.")
    if end_date and end_date < start_date:
        raise HTTPException(status_code=400, detail="The end date is before the start date.")
    cap = seats(route)
    if cap is not None and peak_riders(db, company_id, route.id, start_date, end_date, exclude_student=student_id) >= cap:
        raise HTTPException(status_code=400, detail=f"{route.name} is full ({cap} seats). Pick another route or add seats.")
    earlier = db.query(TransportRider).options(joinedload(TransportRider.route), joinedload(TransportRider.stop)).filter(
        TransportRider.company_id == company_id, TransportRider.student_id == student_id,
        TransportRider.status == "active",
        or_(TransportRider.end_date.is_(None), TransportRider.end_date >= start_date)).all()
    change_from = None
    for r in earlier:
        if r.start_date >= start_date:
            r.status, r.end_date = "ended", r.start_date  # replaced before it began
        else:
            r.end_date = start_date - timedelta(days=1)
            if r.end_date < today(db, company_id):
                r.status = "ended"
        change_from = r
    fee = stop_fee(stop)
    rider = TransportRider(company_id=company_id, student_id=student_id, route_id=route.id, stop_id=stop.id,
                           direction=direction, start_date=start_date, end_date=end_date, fee_amount=fee, notes=notes)
    db.add(rider)
    db.flush()
    if bill and fee:
        from modules.library.service import bill_via_fees
        rider.fee_ref = bill_via_fees(db, company_id, student_id, "TRANSPORT", fee,
                                      f"Transport: {route.name} ({stop.name})", start_date)
    _sync_flag(db, company_id, student_id)
    audit.event(db, "transport.rider_allocated", "transport_riders", rider.id,
                {"route": route.name, "stop": stop.name, "from": str(start_date),
                 "changed_from": change_from.route.name if change_from else None}, entity_label=s.full_name)
    if notify:
        if change_from is not None and (change_from.route_id != route.id or change_from.stop_id != stop.id):
            _tell_parents(db, company_id, [student_id], "transport.route_changed", "Transport route changed",
                          f"From {start_date:%d %b}: {route.name}, stop {stop.name}" + _times(stop) + ".")
        elif change_from is None:
            _tell_parents(db, company_id, [student_id], "transport.allocated", "Transport allocated",
                          f"{s.first_name} rides {route.name} from {start_date:%d %b}, stop {stop.name}" + _times(stop) + ".")
    db.flush()
    return rider


def end_rider(db: Session, company_id: int, rider: TransportRider, end_date: date) -> TransportRider:
    if rider.status != "active":
        raise HTTPException(status_code=400, detail="This allocation has already ended.")
    if end_date < rider.start_date:  # ended before it began: cancel it
        rider.end_date, rider.status = rider.start_date, "ended"
    else:
        rider.end_date = end_date
        if end_date < today(db, company_id):
            rider.status = "ended"
    db.flush()
    _sync_flag(db, company_id, rider.student_id)
    return rider


def _times(stop: TransportStop) -> str:
    parts = []
    if stop.pickup_time:
        parts.append(f"pickup {stop.pickup_time:%H:%M}")
    if stop.drop_time:
        parts.append(f"drop {stop.drop_time:%H:%M}")
    return f" ({', '.join(parts)})" if parts else ""


def _tell_parents(db: Session, company_id: int, student_ids: list[int], kind: str, title: str, body: str) -> None:
    try:
        from modules.notifications.service import notify_portal
        from modules.students.service import portal_account_ids_for_students
        ids = portal_account_ids_for_students(db, company_id, student_ids)
        if ids:
            notify_portal(db, company_id, ids, kind=kind, title=title, body=body, link="/dashboard/my/transport")
    except Exception:
        pass


def notify_route_riders(db: Session, company_id: int, route: TransportRoute, title: str, body: str) -> int:
    on = today(db, company_id)
    ids = [sid for (sid,) in _live(db.query(TransportRider.student_id).filter(
        TransportRider.company_id == company_id, TransportRider.route_id == route.id), on).all()]
    if ids:
        _tell_parents(db, company_id, ids, "transport.route_update", title, body)
    return len(ids)


def close_finished(db: Session) -> int:
    """Mark allocations whose end date has passed as ended and keep the learner flag right."""
    n = 0
    for r in db.query(TransportRider).filter(TransportRider.status == "active", TransportRider.end_date.isnot(None)).all():
        if r.end_date < today(db, r.company_id):
            r.status = "ended"
            db.flush()
            _sync_flag(db, r.company_id, r.student_id)
            n += 1
    return n


def expiring(db: Session, company_id: int, within_days: int = REMIND_DAYS) -> list[dict]:
    on = today(db, company_id)
    limit = on + timedelta(days=within_days)
    out = []
    for v in db.query(TransportVehicle).filter(TransportVehicle.company_id == company_id, TransportVehicle.active.is_(True)).all():
        for field, label in EXPIRY_FIELDS.items():
            d = getattr(v, field)
            if d and d <= limit:
                out.append({"kind": "vehicle", "uuid": str(v.uuid), "name": v.name or v.registration_no,
                            "registration_no": v.registration_no, "document": label, "expires_on": d,
                            "days_left": (d - on).days})
    for c in db.query(TransportCrew).filter(TransportCrew.company_id == company_id, TransportCrew.active.is_(True),
                                            TransportCrew.licence_expiry.isnot(None)).all():
        if c.licence_expiry <= limit:
            out.append({"kind": "crew", "uuid": str(c.uuid), "name": c.name, "registration_no": None,
                        "document": "Driving licence" if c.kind == "driver" else "ID / licence",
                        "expires_on": c.licence_expiry, "days_left": (c.licence_expiry - on).days})
    return sorted(out, key=lambda x: x["expires_on"])


def send_expiry_reminders(db: Session) -> int:
    """Daily: tell transport managers about documents expiring within 30 days (once a day)."""
    from modules.company.models import Company
    sent = 0
    for (cid,) in db.query(Company.id).all():
        items = expiring(db, cid)
        if not items:
            continue
        on = today(db, cid)
        vehicles = db.query(TransportVehicle).filter(TransportVehicle.company_id == cid).all()
        if any(v.reminded_on == on for v in vehicles):
            continue
        for v in vehicles:
            v.reminded_on = on
        try:
            from modules.notifications.service import notify
            people = _managers(db, cid)
            lines = ", ".join(f"{i['name']} {i['document'].lower()} ({i['expires_on']:%d %b})" for i in items[:5])
            notify(db, cid, people or [None], kind="transport.expiry", title=f"{len(items)} transport document(s) expiring",
                   body=lines, link="/dashboard/transport/vehicles")
            sent += 1
        except Exception:
            pass
    db.flush()
    return sent


def _managers(db: Session, company_id: int) -> list[int]:
    from modules.employees.models import Employee
    from modules.roles.models import Role
    roles = [r.role_name for r in db.query(Role).filter(Role.company_id == company_id).all()
             if "transport.manage" in (r.permissions or [])]
    if not roles:
        return []
    return [e for (e,) in db.query(Employee.id).filter(Employee.company_id == company_id, Employee.active.is_(True),
                                                       Employee.role.in_(roles)).all()]


def crew_contact(c: TransportCrew | None) -> dict | None:
    if c is None:
        return None
    phone = c.phone or (c.employee.phone if c.employee is not None else None)
    return {"name": c.name, "phone": phone, "photo_url": c.photo_url}


def rider_summary(db: Session, company_id: int, student_id: int) -> dict | None:
    r = current_rider(db, company_id, student_id)
    if r is None:
        return None
    route, stop = r.route, r.stop
    v = route.vehicle
    return {
        "uuid": str(r.uuid), "route": route.name, "route_code": route.code, "stop": stop.name,
        "landmark": stop.landmark, "pickup_time": stop.pickup_time.strftime("%H:%M") if stop.pickup_time else None,
        "drop_time": stop.drop_time.strftime("%H:%M") if stop.drop_time else None, "direction": r.direction,
        "start_date": r.start_date, "end_date": r.end_date,
        "vehicle": {"name": v.name, "registration_no": v.registration_no, "kind": v.kind} if v else None,
        "driver": crew_contact(route.driver), "attendant": crew_contact(route.attendant),
        "fee_amount": f"{r.fee_amount:.2f}" if r.fee_amount is not None else None,
    }
