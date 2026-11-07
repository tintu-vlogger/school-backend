"""Transport API: vehicles, crew, zones, routes with stops, riders, trip attendance, route
sheets (PDF), reports and the portal (/transport/portal/...)."""
import html
from datetime import date, time
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from core.crud import by_id_or_400, crud_router, get_or_404
from core.db import get_db
from core.pagination import PageParams, paginate
from core.security import get_current_user, require_permission
from modules.transport import service
from modules.transport.models import (TransportCrew, TransportRider, TransportRoute, TransportStop, TransportTripMark,
                                      TransportVehicle, TransportZone)

router = APIRouter(prefix="/transport", tags=["Transport"])
VIEW, MANAGE = "transport.view", "transport.manage"


def _cid(user: dict) -> int:
    return user["company_id"]


def _money(v) -> str | None:
    return None if v is None else f"{Decimal(v):.2f}"


def _t(v: time | None) -> str | None:
    return v.strftime("%H:%M") if v else None


# ── Zones (plain list) ───────────────────────────────────────────────

class ZoneIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    fee: Decimal = Field(Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    description: str | None = None
    active: bool = True


class ZonePatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    fee: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=2)
    description: str | None = None
    active: bool | None = None


router.include_router(crud_router(
    model=TransportZone, prefix="/zones", tag="Transport", create_schema=ZoneIn, update_schema=ZonePatch,
    view_perm=VIEW, manage_perm=MANAGE, label="Zone",
    out=lambda z: {"id": z.id, "uuid": str(z.uuid), "name": z.name, "fee": _money(z.fee), "description": z.description,
                   "active": z.active}))


# ── Vehicles ─────────────────────────────────────────────────────────

class VehicleIn(BaseModel):
    registration_no: str = Field(..., min_length=2, max_length=30)
    name: str | None = Field(None, max_length=60)
    kind: str = Field("bus", pattern="^(bus|minibus|van|car|auto|other)$")
    make_model: str | None = Field(None, max_length=120)
    capacity: int = Field(40, ge=1, le=200)
    fuel: str | None = Field(None, max_length=20)
    ownership: str = Field("own", pattern="^(own|contract)$")
    contractor: str | None = Field(None, max_length=200)
    insurance_expiry: date | None = None
    permit_expiry: date | None = None
    fitness_expiry: date | None = None
    pollution_expiry: date | None = None
    gps_device_id: str | None = Field(None, max_length=80)
    notes: str | None = None
    active: bool = True


class VehiclePatch(BaseModel):
    registration_no: str | None = Field(None, min_length=2, max_length=30)
    name: str | None = None
    kind: str | None = Field(None, pattern="^(bus|minibus|van|car|auto|other)$")
    make_model: str | None = None
    capacity: int | None = Field(None, ge=1, le=200)
    fuel: str | None = None
    ownership: str | None = Field(None, pattern="^(own|contract)$")
    contractor: str | None = None
    insurance_expiry: date | None = None
    permit_expiry: date | None = None
    fitness_expiry: date | None = None
    pollution_expiry: date | None = None
    gps_device_id: str | None = None
    notes: str | None = None
    active: bool | None = None


def _vehicle_out(v: TransportVehicle, on: date | None = None, routes: list[str] | None = None) -> dict:
    out = {"id": v.id, "uuid": str(v.uuid), **{k: getattr(v, k) for k in (
        "registration_no", "name", "kind", "make_model", "capacity", "fuel", "ownership", "contractor",
        "insurance_expiry", "permit_expiry", "fitness_expiry", "pollution_expiry", "gps_device_id", "notes", "active")}}
    if on is not None:
        dates = [d for d in (v.insurance_expiry, v.permit_expiry, v.fitness_expiry, v.pollution_expiry) if d]
        nxt = min(dates) if dates else None
        out["next_expiry"] = nxt
        out["expiry_state"] = None if nxt is None else ("expired" if nxt < on else
                                                       "soon" if (nxt - on).days <= service.REMIND_DAYS else "ok")
    if routes is not None:
        out["routes"] = routes
    return out


@router.get("/vehicles")
def list_vehicles(q: str | None = None, active: bool | None = Query(True), params: PageParams = Depends(),
                  db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    cid = _cid(user)
    query = db.query(TransportVehicle).filter(TransportVehicle.company_id == cid)
    if active is not None:
        query = query.filter(TransportVehicle.active.is_(active))
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(TransportVehicle.registration_no.ilike(like), TransportVehicle.name.ilike(like)))
    on = service.today(db, cid)
    routes: dict[int, list[str]] = {}
    for vid, name in db.query(TransportRoute.vehicle_id, TransportRoute.name).filter(
            TransportRoute.company_id == cid, TransportRoute.active.is_(True)).all():
        routes.setdefault(vid, []).append(name)
    return paginate(query.order_by(TransportVehicle.name, TransportVehicle.registration_no), params,
                    lambda v: _vehicle_out(v, on, routes.get(v.id, [])))


def _unique_reg(db: Session, cid: int, reg: str, skip: int | None = None) -> str:
    reg = reg.strip().upper()
    q = db.query(TransportVehicle.id).filter(TransportVehicle.company_id == cid,
                                             func.upper(TransportVehicle.registration_no) == reg)
    if skip:
        q = q.filter(TransportVehicle.id != skip)
    if q.first():
        raise HTTPException(status_code=409, detail=f"A vehicle with registration {reg} already exists.")
    return reg


@router.post("/vehicles", status_code=201)
def create_vehicle(payload: VehicleIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    data = payload.model_dump()
    data["registration_no"] = _unique_reg(db, cid, data["registration_no"])
    v = TransportVehicle(company_id=cid, **data)
    db.add(v)
    db.commit()
    return _vehicle_out(v, service.today(db, cid))


@router.get("/vehicles/{uuid}")
def get_vehicle(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    cid = _cid(user)
    v = get_or_404(db, TransportVehicle, cid, uuid, "Vehicle")
    names = [n for (n,) in db.query(TransportRoute.name).filter(TransportRoute.company_id == cid,
                                                                TransportRoute.vehicle_id == v.id).all()]
    return _vehicle_out(v, service.today(db, cid), names)


@router.put("/vehicles/{uuid}")
def update_vehicle(uuid: UUID, payload: VehiclePatch, db: Session = Depends(get_db),
                   user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    v = get_or_404(db, TransportVehicle, cid, uuid, "Vehicle")
    data = payload.model_dump(exclude_unset=True)
    if data.get("registration_no"):
        data["registration_no"] = _unique_reg(db, cid, data["registration_no"], v.id)
    for k, val in data.items():
        setattr(v, k, val)
    db.commit()
    return _vehicle_out(v, service.today(db, cid))


@router.delete("/vehicles/{uuid}")
def archive_vehicle(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    v = get_or_404(db, TransportVehicle, _cid(user), uuid, "Vehicle")
    v.active = False
    db.commit()
    return {"detail": "Switched off"}


# ── Crew (drivers and attendants) ────────────────────────────────────

class CrewIn(BaseModel):
    kind: str = Field("driver", pattern="^(driver|attendant)$")
    employee_id: int | None = None
    name: str | None = Field(None, max_length=200)
    phone: str | None = Field(None, max_length=20)
    licence_no: str | None = Field(None, max_length=40)
    licence_expiry: date | None = None
    badge_no: str | None = Field(None, max_length=40)
    photo_url: str | None = None
    notes: str | None = None
    active: bool = True


class CrewPatch(BaseModel):
    kind: str | None = Field(None, pattern="^(driver|attendant)$")
    employee_id: int | None = None
    name: str | None = Field(None, max_length=200)
    phone: str | None = None
    licence_no: str | None = None
    licence_expiry: date | None = None
    badge_no: str | None = None
    photo_url: str | None = None
    notes: str | None = None
    active: bool | None = None


def _crew_out(c: TransportCrew, on: date | None = None) -> dict:
    out = {"id": c.id, "uuid": str(c.uuid), **{k: getattr(c, k) for k in (
        "kind", "employee_id", "name", "licence_no", "licence_expiry", "badge_no", "photo_url", "notes", "active")}}
    out["phone"] = c.phone or (c.employee.phone if c.employee is not None else None)
    out["employee_name"] = f"{c.employee.first_name} {c.employee.last_name}".strip() if c.employee is not None else None
    if on is not None and c.licence_expiry:
        out["licence_state"] = "expired" if c.licence_expiry < on else (
            "soon" if (c.licence_expiry - on).days <= service.REMIND_DAYS else "ok")
    return out


def _crew_fill(db: Session, cid: int, data: dict) -> None:
    from modules.employees.models import Employee
    if data.get("employee_id"):
        e = by_id_or_400(db, Employee, cid, data["employee_id"], "Staff member")
        if not data.get("name"):
            data["name"] = f"{e.first_name} {e.last_name}".strip()
        if not data.get("phone"):
            data["phone"] = e.phone


@router.get("/crew")
def list_crew(kind: str | None = None, q: str | None = None, active: bool | None = Query(True),
              params: PageParams = Depends(), db: Session = Depends(get_db),
              user: dict = Depends(require_permission(VIEW))):
    cid = _cid(user)
    query = db.query(TransportCrew).options(joinedload(TransportCrew.employee)).filter(TransportCrew.company_id == cid)
    if kind:
        query = query.filter(TransportCrew.kind == kind)
    if active is not None:
        query = query.filter(TransportCrew.active.is_(active))
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(TransportCrew.name.ilike(like), TransportCrew.phone.ilike(like),
                                 TransportCrew.licence_no.ilike(like)))
    on = service.today(db, cid)
    return paginate(query.order_by(TransportCrew.kind, TransportCrew.name), params, lambda c: _crew_out(c, on))


@router.post("/crew", status_code=201)
def create_crew(payload: CrewIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    data = payload.model_dump()
    _crew_fill(db, cid, data)
    if not data.get("name"):
        raise HTTPException(status_code=400, detail="Give a name or pick a staff member.")
    c = TransportCrew(company_id=cid, **data)
    db.add(c)
    db.commit()
    return _crew_out(c, service.today(db, cid))


@router.put("/crew/{uuid}")
def update_crew(uuid: UUID, payload: CrewPatch, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    c = get_or_404(db, TransportCrew, cid, uuid, "Driver or attendant")
    data = payload.model_dump(exclude_unset=True)
    _crew_fill(db, cid, data)
    for k, v in data.items():
        setattr(c, k, v)
    db.commit()
    return _crew_out(c, service.today(db, cid))


@router.delete("/crew/{uuid}")
def archive_crew(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    c = get_or_404(db, TransportCrew, _cid(user), uuid, "Driver or attendant")
    c.active = False
    db.commit()
    return {"detail": "Switched off"}


# ── Routes and stops ─────────────────────────────────────────────────

def _stop_out(s: TransportStop, riders: int | None = None) -> dict:
    fee = service.stop_fee(s)
    return {"id": s.id, "uuid": str(s.uuid), "name": s.name, "sequence": s.sequence, "pickup_time": _t(s.pickup_time),
            "drop_time": _t(s.drop_time), "distance_km": None if s.distance_km is None else float(s.distance_km),
            "zone_id": s.zone_id, "zone_name": s.zone.name if s.zone else None, "fee": _money(s.fee),
            "effective_fee": _money(fee), "landmark": s.landmark, "active": s.active, "riders": riders}


def _route_out(db: Session, r: TransportRoute, on: date, full: bool = False) -> dict:
    count = service.riders_on(db, r.company_id, r.id, on)
    stops = [s for s in r.stops if s.active]
    out = {"id": r.id, "uuid": str(r.uuid), "name": r.name, "code": r.code, "start_point": r.start_point,
           "notes": r.notes, "active": r.active, "capacity": r.capacity, "seats": service.seats(r), "riders": count,
           "vehicle_id": r.vehicle_id, "vehicle": (r.vehicle.name or r.vehicle.registration_no) if r.vehicle else None,
           "vehicle_reg": r.vehicle.registration_no if r.vehicle else None,
           "driver_id": r.driver_id, "driver": r.driver.name if r.driver else None,
           "attendant_id": r.attendant_id, "attendant": r.attendant.name if r.attendant else None,
           "stop_count": len(stops),
           "first_pickup": _t(min((s.pickup_time for s in stops if s.pickup_time), default=None)),
           "distance_km": float(max((s.distance_km for s in stops if s.distance_km is not None), default=0) or 0)}
    if full:
        per_stop = dict(service._live(db.query(TransportRider.stop_id, func.count(TransportRider.id)).filter(
            TransportRider.company_id == r.company_id, TransportRider.route_id == r.id), on)
            .group_by(TransportRider.stop_id).all())
        out["stops"] = [_stop_out(s, per_stop.get(s.id, 0)) for s in stops]
        out["driver_detail"] = _crew_out(r.driver) if r.driver else None
        out["attendant_detail"] = _crew_out(r.attendant) if r.attendant else None
    return out


def _route_query(db: Session, cid: int):
    return db.query(TransportRoute).options(joinedload(TransportRoute.vehicle), joinedload(TransportRoute.driver),
                                            joinedload(TransportRoute.attendant),
                                            joinedload(TransportRoute.stops).joinedload(TransportStop.zone)).filter(
        TransportRoute.company_id == cid)


@router.get("/routes")
def list_routes(q: str | None = None, active: bool | None = Query(True), params: PageParams = Depends(),
                db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    cid = _cid(user)
    query = db.query(TransportRoute).filter(TransportRoute.company_id == cid)
    if active is not None:
        query = query.filter(TransportRoute.active.is_(active))
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(TransportRoute.name.ilike(like), TransportRoute.code.ilike(like)))
    on = service.today(db, cid)
    return paginate(query.order_by(TransportRoute.name), params, lambda r: _route_out(db, r, on))


class RouteIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    code: str | None = Field(None, max_length=20)
    vehicle_id: int | None = None
    driver_id: int | None = None
    attendant_id: int | None = None
    start_point: str | None = Field(None, max_length=200)
    capacity: int | None = Field(None, ge=1, le=300)
    notes: str | None = None
    active: bool = True


class RoutePatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    code: str | None = None
    vehicle_id: int | None = None
    driver_id: int | None = None
    attendant_id: int | None = None
    start_point: str | None = None
    capacity: int | None = Field(None, ge=1, le=300)
    notes: str | None = None
    active: bool | None = None
    notify_parents: bool = False


def _check_route_links(db: Session, cid: int, data: dict) -> None:
    by_id_or_400(db, TransportVehicle, cid, data.get("vehicle_id"), "Vehicle")
    by_id_or_400(db, TransportCrew, cid, data.get("driver_id"), "Driver")
    by_id_or_400(db, TransportCrew, cid, data.get("attendant_id"), "Attendant")
    name = data.get("name")
    if name is not None:
        data["name"] = name.strip()


@router.post("/routes", status_code=201)
def create_route(payload: RouteIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    data = payload.model_dump()
    _check_route_links(db, cid, data)
    if db.query(TransportRoute.id).filter(TransportRoute.company_id == cid, TransportRoute.name == data["name"]).first():
        raise HTTPException(status_code=409, detail="A route with this name already exists.")
    r = TransportRoute(company_id=cid, **data)
    db.add(r)
    db.commit()
    return _route_out(db, r, service.today(db, cid), full=True)


@router.get("/routes/{uuid}")
def get_route(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    cid = _cid(user)
    r = _route_query(db, cid).filter(TransportRoute.uuid == uuid).first()
    if r is None:
        raise HTTPException(status_code=404, detail="Route not found")
    return _route_out(db, r, service.today(db, cid), full=True)


@router.put("/routes/{uuid}")
def update_route(uuid: UUID, payload: RoutePatch, db: Session = Depends(get_db),
                 user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    r = get_or_404(db, TransportRoute, cid, uuid, "Route")
    data = payload.model_dump(exclude_unset=True)
    notify = data.pop("notify_parents", False)
    _check_route_links(db, cid, data)
    changed = [k for k in ("vehicle_id", "driver_id", "attendant_id") if k in data and data[k] != getattr(r, k)]
    if data.get("name") and db.query(TransportRoute.id).filter(TransportRoute.company_id == cid, TransportRoute.name == data["name"],
                                                               TransportRoute.id != r.id).first():
        raise HTTPException(status_code=409, detail="A route with this name already exists.")
    for k, v in data.items():
        setattr(r, k, v)
    db.flush()
    db.refresh(r)
    if notify and changed:
        bits = []
        if "vehicle_id" in changed and r.vehicle:
            bits.append(f"vehicle {r.vehicle.name or r.vehicle.registration_no}")
        if "driver_id" in changed and r.driver:
            bits.append(f"driver {r.driver.name}" + (f" ({r.driver.phone})" if r.driver.phone else ""))
        if "attendant_id" in changed and r.attendant:
            bits.append(f"attendant {r.attendant.name}")
        if bits:
            service.notify_route_riders(db, cid, r, f"{r.name}: transport update", "New " + ", ".join(bits) + ".")
    db.commit()
    return _route_out(db, r, service.today(db, cid), full=True)


@router.delete("/routes/{uuid}")
def archive_route(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    r = get_or_404(db, TransportRoute, cid, uuid, "Route")
    if service.riders_on(db, cid, r.id, service.today(db, cid)):
        raise HTTPException(status_code=400, detail="Learners still ride this route. Move or end their allocations first.")
    r.active = False
    db.commit()
    return {"detail": "Switched off"}


class StopIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    sequence: int | None = Field(None, ge=1, le=500)
    pickup_time: time | None = None
    drop_time: time | None = None
    distance_km: Decimal | None = Field(None, ge=0, max_digits=6, decimal_places=2)
    zone_id: int | None = None
    fee: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=2)
    landmark: str | None = Field(None, max_length=200)


class StopPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    sequence: int | None = Field(None, ge=1, le=500)
    pickup_time: time | None = None
    drop_time: time | None = None
    distance_km: Decimal | None = Field(None, ge=0, max_digits=6, decimal_places=2)
    zone_id: int | None = None
    fee: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=2)
    landmark: str | None = None
    notify_parents: bool = False


@router.post("/routes/{uuid}/stops", status_code=201)
def add_stop(uuid: UUID, payload: StopIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    r = get_or_404(db, TransportRoute, cid, uuid, "Route")
    data = payload.model_dump()
    by_id_or_400(db, TransportZone, cid, data.get("zone_id"), "Zone")
    if not data.get("sequence"):
        data["sequence"] = (db.query(func.max(TransportStop.sequence)).filter(
            TransportStop.route_id == r.id, TransportStop.active.is_(True)).scalar() or 0) + 1
    s = TransportStop(company_id=cid, route_id=r.id, **data)
    db.add(s)
    db.flush()
    _renumber(db, r.id, keep=s)
    db.commit()
    return _stop_out(s)


def _renumber(db: Session, route_id: int, keep: TransportStop | None = None) -> None:
    stops = db.query(TransportStop).filter(TransportStop.route_id == route_id, TransportStop.active.is_(True)).all()
    stops.sort(key=lambda s: (s.sequence, 0 if keep is not None and s.id == keep.id else 1, s.id))
    for i, s in enumerate(stops, start=1):
        if s.sequence != i:
            s.sequence = i


@router.put("/stops/{uuid}")
def update_stop(uuid: UUID, payload: StopPatch, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    s = get_or_404(db, TransportStop, cid, uuid, "Stop")
    data = payload.model_dump(exclude_unset=True)
    notify = data.pop("notify_parents", False)
    if "zone_id" in data:
        by_id_or_400(db, TransportZone, cid, data["zone_id"], "Zone")
    times_changed = any(k in data and data[k] != getattr(s, k) for k in ("pickup_time", "drop_time"))
    for k, v in data.items():
        setattr(s, k, v)
    db.flush()
    _renumber(db, s.route_id, keep=s if "sequence" in data else None)
    if notify and times_changed:
        on = service.today(db, cid)
        ids = [sid for (sid,) in service._live(db.query(TransportRider.student_id).filter(
            TransportRider.company_id == cid, TransportRider.stop_id == s.id), on).all()]
        service._tell_parents(db, cid, ids, "transport.stop_times", f"New timings at {s.name}",
                              f"{s.name}" + service._times(s) + ".")
    db.commit()
    return _stop_out(s)


@router.delete("/stops/{uuid}")
def remove_stop(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    s = get_or_404(db, TransportStop, cid, uuid, "Stop")
    if service._live(db.query(TransportRider.id).filter(TransportRider.company_id == cid, TransportRider.stop_id == s.id),
                     service.today(db, cid)).first():
        raise HTTPException(status_code=400, detail="Learners are allocated to this stop. Move them first.")
    s.active = False
    db.flush()
    _renumber(db, s.route_id)
    db.commit()
    return {"detail": "Stop removed"}


class ReorderIn(BaseModel):
    stop_uuids: list[UUID]


@router.post("/routes/{uuid}/reorder")
def reorder(uuid: UUID, payload: ReorderIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    r = get_or_404(db, TransportRoute, cid, uuid, "Route")
    stops = {s.uuid: s for s in db.query(TransportStop).filter(TransportStop.route_id == r.id, TransportStop.active.is_(True)).all()}
    for i, u in enumerate(payload.stop_uuids, start=1):
        if u in stops:
            stops[u].sequence = i
    db.flush()
    _renumber(db, r.id)
    db.commit()
    return get_route(uuid, db, user)


# ── Riders ───────────────────────────────────────────────────────────

def _rider_out(db: Session, r: TransportRider, on: date) -> dict:
    from modules.students import service as students
    s = r.student
    e = students.current_enrollment(db, r.company_id, s.id)
    live = r.status == "active" and r.start_date <= on and (r.end_date is None or r.end_date >= on)
    return {"id": r.id, "uuid": str(r.uuid), "student_id": s.id, "student_uuid": str(s.uuid), "student_name": s.full_name,
            "admission_no": s.admission_no, "photo_url": s.photo_url,
            "section_label": students.academics.section_label(e.section) if e and e.section else None,
            "route_uuid": str(r.route.uuid), "route": r.route.name, "stop_uuid": str(r.stop.uuid), "stop": r.stop.name,
            "pickup_time": _t(r.stop.pickup_time), "drop_time": _t(r.stop.drop_time), "direction": r.direction,
            "start_date": r.start_date, "end_date": r.end_date, "status": r.status,
            "state": "current" if live else ("upcoming" if r.status == "active" and r.start_date > on else "ended"),
            "fee_amount": _money(r.fee_amount), "fee_ref": r.fee_ref, "notes": r.notes}


@router.get("/riders")
def list_riders(q: str | None = None, route_uuid: UUID | None = None, stop_uuid: UUID | None = None,
                state: str | None = Query("current"), params: PageParams = Depends(), db: Session = Depends(get_db),
                user: dict = Depends(require_permission(VIEW))):
    from modules.students.models import Student
    cid = _cid(user)
    on = service.today(db, cid)
    query = (db.query(TransportRider).join(Student, Student.id == TransportRider.student_id)
             .join(TransportRoute, TransportRoute.id == TransportRider.route_id)
             .join(TransportStop, TransportStop.id == TransportRider.stop_id)
             .options(joinedload(TransportRider.student), joinedload(TransportRider.route), joinedload(TransportRider.stop))
             .filter(TransportRider.company_id == cid))
    if route_uuid:
        query = query.filter(TransportRoute.uuid == route_uuid)
    if stop_uuid:
        query = query.filter(TransportStop.uuid == stop_uuid)
    if state == "current":
        query = service._live(query, on)
    elif state == "upcoming":
        query = query.filter(TransportRider.status == "active", TransportRider.start_date > on)
    elif state == "ended":
        query = query.filter(or_(TransportRider.status == "ended", TransportRider.end_date < on))
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Student.first_name.ilike(like), Student.last_name.ilike(like),
                                 Student.admission_no.ilike(like)))
    query = query.order_by(TransportRoute.name, TransportStop.sequence, Student.first_name)
    return paginate(query, params, lambda r: _rider_out(db, r, on))


class RiderIn(BaseModel):
    student_id: int
    route_uuid: UUID
    stop_uuid: UUID
    start_date: date | None = None
    end_date: date | None = None
    direction: str = Field("both", pattern="^(both|pickup|drop)$")
    notes: str | None = None
    bill_fee: bool = False


@router.post("/riders", status_code=201)
def create_rider(payload: RiderIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    route = get_or_404(db, TransportRoute, cid, payload.route_uuid, "Route")
    stop = get_or_404(db, TransportStop, cid, payload.stop_uuid, "Stop")
    r = service.allocate(db, cid, student_id=payload.student_id, route=route, stop=stop,
                         start_date=payload.start_date or service.today(db, cid), end_date=payload.end_date,
                         direction=payload.direction, notes=payload.notes, bill=payload.bill_fee)
    db.commit()
    return _rider_out(db, r, service.today(db, cid))


class EndIn(BaseModel):
    end_date: date | None = None


@router.post("/riders/{uuid}/end")
def end_rider(uuid: UUID, payload: EndIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    r = get_or_404(db, TransportRider, cid, uuid, "Allocation")
    service.end_rider(db, cid, r, payload.end_date or service.today(db, cid))
    db.commit()
    return _rider_out(db, r, service.today(db, cid))


@router.get("/riders/for-student/{student_id}")
def rider_for_student(student_id: int, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    cid = _cid(user)
    rows = (db.query(TransportRider).options(joinedload(TransportRider.student), joinedload(TransportRider.route),
                                             joinedload(TransportRider.stop))
            .filter(TransportRider.company_id == cid, TransportRider.student_id == student_id)
            .order_by(TransportRider.start_date.desc()).all())
    on = service.today(db, cid)
    return {"current": service.rider_summary(db, cid, student_id), "history": [_rider_out(db, r, on) for r in rows]}


# ── Trip attendance ──────────────────────────────────────────────────

@router.get("/routes/{uuid}/trip")
def get_trip(uuid: UUID, day: date | None = None, trip: str = Query("pickup", pattern="^(pickup|drop)$"),
             db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    cid = _cid(user)
    r = get_or_404(db, TransportRoute, cid, uuid, "Route")
    day = day or service.today(db, cid)
    riders = (service._live(db.query(TransportRider).options(joinedload(TransportRider.student), joinedload(TransportRider.stop))
                            .filter(TransportRider.company_id == cid, TransportRider.route_id == r.id), day)
              .filter(TransportRider.direction.in_(("both", trip))).all())
    marks = {m.student_id: m for m in db.query(TransportTripMark).filter(
        TransportTripMark.company_id == cid, TransportTripMark.route_id == r.id, TransportTripMark.day == day,
        TransportTripMark.trip == trip).all()}
    riders.sort(key=lambda x: (x.stop.sequence, x.student.first_name))
    return {"route": r.name, "day": day, "trip": trip, "items": [
        {"student_id": x.student_id, "student_name": x.student.full_name, "admission_no": x.student.admission_no,
         "stop": x.stop.name, "time": _t(x.stop.pickup_time if trip == "pickup" else x.stop.drop_time),
         "boarded": marks[x.student_id].boarded if x.student_id in marks else None} for x in riders]}


class TripIn(BaseModel):
    day: date
    trip: str = Field(..., pattern="^(pickup|drop)$")
    marks: dict[int, bool]
    notify_absent: bool = False


@router.post("/routes/{uuid}/trip")
def save_trip(uuid: UUID, payload: TripIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = _cid(user)
    r = get_or_404(db, TransportRoute, cid, uuid, "Route")
    allowed = {sid for (sid,) in service._live(db.query(TransportRider.student_id).filter(
        TransportRider.company_id == cid, TransportRider.route_id == r.id), payload.day).all()}
    existing = {m.student_id: m for m in db.query(TransportTripMark).filter(
        TransportTripMark.company_id == cid, TransportTripMark.route_id == r.id, TransportTripMark.day == payload.day,
        TransportTripMark.trip == payload.trip).all()}
    missed = []
    for sid, boarded in payload.marks.items():
        if sid not in allowed:
            continue
        m = existing.get(sid)
        if m is None:
            m = TransportTripMark(company_id=cid, route_id=r.id, student_id=sid, day=payload.day, trip=payload.trip,
                                  boarded=boarded)
            db.add(m)
        m.boarded, m.marked_by = boarded, service.actor()
        if not boarded:
            missed.append(sid)
    if payload.notify_absent and missed:
        service._tell_parents(db, cid, missed, "transport.not_boarded", "Did not board the bus",
                              f"Not on {r.name} ({payload.trip}) on {payload.day:%d %b}.")
    db.commit()
    return {"saved": len(payload.marks)}


# ── Route sheet, reports, summary ────────────────────────────────────

@router.get("/routes/{uuid}/sheet.pdf")
def route_sheet(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    from modules.company.models import Company
    from modules.documents.pdf import render_pdf
    from modules.students import service as students
    from modules.students.models import Guardian, StudentGuardian
    cid = _cid(user)
    r = _route_query(db, cid).filter(TransportRoute.uuid == uuid).first()
    if r is None:
        raise HTTPException(status_code=404, detail="Route not found")
    on = service.today(db, cid)
    riders = service._live(db.query(TransportRider).options(joinedload(TransportRider.student)).filter(
        TransportRider.company_id == cid, TransportRider.route_id == r.id), on).all()
    by_stop: dict[int, list] = {}
    for x in riders:
        by_stop.setdefault(x.stop_id, []).append(x)
    phones = {}
    sids = [x.student_id for x in riders]
    if sids:
        for sid, ph in db.query(StudentGuardian.student_id, Guardian.phone).join(Guardian, Guardian.id == StudentGuardian.guardian_id).filter(
                StudentGuardian.company_id == cid, StudentGuardian.student_id.in_(sids), StudentGuardian.is_primary.is_(True)).all():
            phones[sid] = ph
    esc = html.escape
    company = db.get(Company, cid)
    rows = []
    for s in [s for s in r.stops if s.active]:
        rows.append(f'<tr class="stop"><td colspan="5">{s.sequence}. {esc(s.name)}'
                    f'<span>{esc(_t(s.pickup_time) or "")} / {esc(_t(s.drop_time) or "")}'
                    f'{" · " + esc(s.landmark) if s.landmark else ""}</span></td></tr>')
        for x in sorted(by_stop.get(s.id, []), key=lambda x: x.student.first_name):
            e = students.current_enrollment(db, cid, x.student_id)
            rows.append(f"<tr><td>{esc(x.student.full_name)}</td><td>{esc(x.student.admission_no)}</td>"
                        f"<td>{esc(students.academics.section_label(e.section) if e and e.section else '')}</td>"
                        f"<td>{esc(phones.get(x.student_id) or '')}</td><td class='box'></td></tr>")
    v, d, a = r.vehicle, r.driver, r.attendant
    head = " · ".join(p for p in [
        f"Vehicle {esc(v.name or '')} {esc(v.registration_no)}" if v else "",
        f"Driver {esc(d.name)} {esc(d.phone or '')}" if d else "",
        f"Attendant {esc(a.name)} {esc(a.phone or '')}" if a else "",
        f"{len(riders)} riders / {service.seats(r) or '-'} seats"] if p)
    css = """@page { size: A4; margin: 14mm; } body { font-family: Helvetica, Arial, sans-serif; font-size: 10pt; color: #222; }
    h1 { font-size: 16pt; font-weight: 400; margin: 0; } .inst { color: #777; font-size: 9pt; text-transform: uppercase; }
    .meta { color: #555; margin: 4px 0 12px; } table { width: 100%; border-collapse: collapse; }
    th { text-align: left; font-weight: 400; color: #777; border-bottom: 1px solid #999; padding: 4px; }
    td { border-bottom: 1px solid #e5e5e5; padding: 4px; } tr.stop td { background: #f4f4f6; padding-top: 7px; }
    tr.stop span { float: right; color: #666; } td.box { width: 18mm; }"""
    doc = (f"<html><head><meta charset='utf-8'><style>{css}</style></head><body>"
           f"<div class='inst'>{esc(company.company_name if company else '')}</div><h1>Route sheet: {esc(r.name)}</h1>"
           f"<div class='meta'>{head} · {on:%d %b %Y}</div><table><tr><th>Learner</th><th>Adm. no.</th><th>Class</th>"
           f"<th>Parent phone</th><th>Boarded</th></tr>{''.join(rows)}</table></body></html>")
    return Response(render_pdf(doc), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="route-{r.code or r.id}.pdf"'})


@router.get("/expiring")
def expiring(days: int = Query(30, ge=1, le=365), db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    return service.expiring(db, _cid(user), days)


@router.get("/summary")
def summary(db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    cid = _cid(user)
    on = service.today(db, cid)
    routes = db.query(TransportRoute).options(joinedload(TransportRoute.vehicle)).filter(
        TransportRoute.company_id == cid, TransportRoute.active.is_(True)).all()
    loads = []
    for r in routes:
        n, cap = service.riders_on(db, cid, r.id, on), service.seats(r)
        loads.append({"uuid": str(r.uuid), "name": r.name, "riders": n, "seats": cap})
    riders = service._live(db.query(func.count(TransportRider.id)).filter(TransportRider.company_id == cid), on).scalar() or 0
    vehicles = db.query(func.count(TransportVehicle.id)).filter(TransportVehicle.company_id == cid,
                                                                TransportVehicle.active.is_(True)).scalar() or 0
    exp = service.expiring(db, cid)
    return {"routes": len(routes), "vehicles": vehicles, "riders": riders,
            "seats": sum(l["seats"] or 0 for l in loads), "full_routes": sum(1 for l in loads if l["seats"] and l["riders"] >= l["seats"]),
            "expiring": len(exp), "expired": sum(1 for e in exp if e["days_left"] < 0), "loads": loads,
            "expiring_items": exp[:5]}


@router.get("/reports/stops")
def stop_report(db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    """Riders and fee per stop across routes (for planning and fee totals)."""
    cid = _cid(user)
    on = service.today(db, cid)
    rows = (service._live(db.query(TransportRoute.name, TransportStop.name, func.count(TransportRider.id),
                                   func.coalesce(func.sum(TransportRider.fee_amount), 0))
                          .select_from(TransportRider).join(TransportRoute, TransportRoute.id == TransportRider.route_id)
                          .join(TransportStop, TransportStop.id == TransportRider.stop_id)
                          .filter(TransportRider.company_id == cid), on)
            .group_by(TransportRoute.name, TransportStop.name, TransportStop.sequence)
            .order_by(TransportRoute.name, TransportStop.sequence).all())
    return [{"route": a, "stop": b, "riders": n, "fees": _money(f)} for a, b, n, f in rows]


# ── Portal ───────────────────────────────────────────────────────────

@router.get("/portal/summary")
def portal_summary(student_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    from modules.students.service import require_student_access
    if not user.get("portal_id"):
        raise HTTPException(status_code=403, detail="This page is for parents and students.")
    require_student_access(db, user, student_id)
    cid = user["company_id"]
    out = service.rider_summary(db, cid, student_id)
    if out is None:
        return {"rider": None}
    r = service.current_rider(db, cid, student_id)
    stops = [{"name": s.name, "pickup_time": _t(s.pickup_time), "drop_time": _t(s.drop_time), "mine": s.id == r.stop_id}
             for s in r.route.stops if s.active]
    return {"rider": out, "stops": stops}
