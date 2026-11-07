"""Transport demo: 4 routes with stops, vehicles, drivers and attendants, zones and about 40
riders. Aarav Kumar (the demo student) rides route 1."""
import random
from datetime import time, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

ROUTES = [
    ("Route 1 - Indiranagar", "R1", "KA-01-AB-1234", "Bus 1", ["100 Feet Road", "CMH Road", "Indiranagar Metro", "Domlur Flyover", "HAL Old Airport Road"]),
    ("Route 2 - Koramangala", "R2", "KA-01-AC-5678", "Bus 2", ["Sony World Signal", "Forum Mall", "St. John's Hospital", "Koramangala 1st Block"]),
    ("Route 3 - Jayanagar", "R3", "KA-05-MD-2468", "Bus 3", ["Jayanagar 4th Block", "South End Circle", "Lalbagh West Gate", "Wilson Garden", "Shanti Nagar"]),
    ("Route 4 - Whitefield", "R4", "KA-53-EF-9753", "Van 4", ["ITPL Main Gate", "Hope Farm", "Kadugodi", "Marathahalli Bridge"]),
]
DRIVERS = [("Ramesh Gowda", "+91 98450 11223"), ("Suresh Naik", "+91 98860 44556"), ("Manjunath K", "+91 97400 77889"),
           ("Iqbal Pasha", "+91 99000 33221")]
ATTENDANTS = [("Lakshmi Devi", "+91 90080 12345"), ("Shobha R", "+91 90350 67890"), ("Kamala B", "+91 81050 24680"),
              ("Revathi S", "+91 96320 13579")]


def seed_demo(db: Session, company_id: int) -> None:
    from modules.students.models import Enrollment, Student
    from modules.transport import service
    from modules.transport.models import TransportCrew, TransportRoute, TransportStop, TransportVehicle, TransportZone
    if db.query(TransportRoute.id).filter(TransportRoute.company_id == company_id).first():
        return
    from modules.library.seed import _foundation
    _foundation(db, company_id)
    if not db.query(Student.id).filter(Student.company_id == company_id).first():
        return
    rng = random.Random(23)
    on = service.today(db, company_id)
    zones = []
    for name, fee in [("Zone A (up to 5 km)", 9000), ("Zone B (5-10 km)", 12000), ("Zone C (over 10 km)", 15000)]:
        z = TransportZone(company_id=company_id, name=name, fee=Decimal(fee))
        db.add(z)
        zones.append(z)
    db.flush()
    routes = []
    for i, (name, code, reg, vname, stops) in enumerate(ROUTES):
        v = TransportVehicle(company_id=company_id, registration_no=reg, name=vname, kind="van" if "Van" in vname else "bus",
                             make_model="Force Traveller" if "Van" in vname else "Tata Starbus 40",
                             capacity=14 if "Van" in vname else 40, fuel="Diesel",
                             insurance_expiry=on + timedelta(days=[20, 200, 320, 90][i]),
                             permit_expiry=on + timedelta(days=[400, 15, 250, 300][i]),
                             fitness_expiry=on + timedelta(days=[180, 220, -5, 150][i]),
                             pollution_expiry=on + timedelta(days=[60, 90, 120, 25][i]),
                             gps_device_id=f"GPS-{1000 + i}")
        d = TransportCrew(company_id=company_id, kind="driver", name=DRIVERS[i][0], phone=DRIVERS[i][1],
                          licence_no=f"KA0{i + 1}2019000{i + 1}{rng.randint(100, 999)}",
                          licence_expiry=on + timedelta(days=[700, 400, 28, 900][i]), badge_no=f"DB-{i + 11}")
        a = TransportCrew(company_id=company_id, kind="attendant", name=ATTENDANTS[i][0], phone=ATTENDANTS[i][1])
        db.add_all([v, d, a])
        db.flush()
        r = TransportRoute(company_id=company_id, name=name, code=code, vehicle_id=v.id, driver_id=d.id,
                           attendant_id=a.id, start_point="School campus")
        db.add(r)
        db.flush()
        for k, sname in enumerate(stops):
            pick = 7 * 60 + 5 + k * 9 - len(stops) * 2
            drop = 15 * 60 + 40 + k * 9
            db.add(TransportStop(company_id=company_id, route_id=r.id, name=sname, sequence=k + 1,
                                 pickup_time=time(pick // 60, pick % 60), drop_time=time(drop // 60, drop % 60),
                                 distance_km=Decimal(str(round(2.5 + k * 2.2 + i, 1))),
                                 zone_id=zones[min(2, (k + i) // 2)].id))
        db.flush()
        db.refresh(r)
        routes.append(r)
    students = (db.query(Student).join(Enrollment, Enrollment.student_id == Student.id)
                .filter(Student.company_id == company_id, Student.status == "active").order_by(Student.id).all())
    aarav = next((s for s in students if s.email == "student@test.com"), None)
    start = on - timedelta(days=60)
    if aarav is not None:
        r = routes[0]
        service.allocate(db, company_id, student_id=aarav.id, route=r, stop=r.stops[2], start_date=start, notify=False)
    pool = [s for s in students if s.uses_transport and (aarav is None or s.id != aarav.id)]
    pool += [s for s in students if not s.uses_transport and s not in pool]
    count = 0
    for s in pool:
        if count >= 39:
            break
        r = routes[count % 4]
        stops = [x for x in r.stops if x.active]
        try:
            service.allocate(db, company_id, student_id=s.id, route=r, stop=rng.choice(stops),
                             start_date=start + timedelta(days=rng.randint(0, 20)), notify=False)
            count += 1
        except Exception:
            continue
    # Learners marked as using transport without an allocation are cleared by the sync.
    for s in students:
        service._sync_flag(db, company_id, s.id)
    db.flush()
