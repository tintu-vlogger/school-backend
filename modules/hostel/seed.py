"""Hostel demo: one boys' block with rooms, about 20 boarders, last night's roll-call, a few
leave requests and visitors. The warden is a demo staff member."""
import random
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session


def seed_demo(db: Session, company_id: int) -> None:
    from modules.employees.models import Employee
    from modules.hostel import service
    from modules.hostel.models import HostelBed, HostelBlock, HostelLeave, HostelMessPlan, HostelRoom, HostelVisitor
    from modules.students.models import Enrollment, Student
    if db.query(HostelBlock.id).filter(HostelBlock.company_id == company_id).first():
        return
    from modules.library.seed import _foundation
    _foundation(db, company_id)
    if not db.query(Student.id).filter(Student.company_id == company_id).first():
        return
    rng = random.Random(31)
    on = service.today(db, company_id)
    staff = db.query(Employee).filter(Employee.company_id == company_id, Employee.active.is_(True)).order_by(Employee.id).all()
    warden = next((e for e in staff if e.role in ("employee", "teacher") and e.email != "divya.menon@example.com"), None)
    veg = HostelMessPlan(company_id=company_id, name="Vegetarian", fee=Decimal("36000"), description="Breakfast, lunch, snacks and dinner")
    nonveg = HostelMessPlan(company_id=company_id, name="Non-vegetarian", fee=Decimal("42000"), description="Non-veg four days a week")
    block = HostelBlock(company_id=company_id, name="Tagore House", code="TH", gender="boys",
                        warden_id=warden.id if warden else None, floors=2, phone="+91 80 4123 9876",
                        notes="Boys' hostel for Grade 6 and above")
    db.add_all([veg, nonveg, block])
    db.flush()
    rooms = []
    for floor in (1, 2):
        for n in range(1, 7):
            kind = "triple" if n <= 4 else "double"
            r = HostelRoom(company_id=company_id, block_id=block.id, floor=str(floor), room_no=f"{floor}0{n}",
                           room_type=kind, beds=3 if kind == "triple" else 2,
                           fee=Decimal("60000") if kind == "triple" else Decimal("72000"),
                           amenities=["Study table", "Wardrobe"] + (["Attached bath"] if kind == "double" else []))
            db.add(r)
            db.flush()
            service.sync_beds(db, r)
            rooms.append(r)
    rooms[-1].status = "maintenance"
    db.flush()
    boys = (db.query(Student).join(Enrollment, Enrollment.student_id == Student.id)
            .filter(Student.company_id == company_id, Student.status == "active", Student.gender == "male")
            .order_by(Student.id.desc()).all())
    boys = [s for s in boys if s.email != "student@test.com"][:20]
    beds = [b for r in rooms if r.status == "open" for b in db.query(HostelBed).filter(HostelBed.room_id == r.id).order_by(HostelBed.label).all()]
    allocs = []
    for s, bed in zip(boys, beds):
        allocs.append(service.allocate(db, company_id, student_id=s.id, bed=bed,
                                       check_in=on - timedelta(days=rng.randint(60, 200)),
                                       mess_plan=rng.choice([veg, veg, nonveg])))
    # Last night's roll-call: everyone present but one absent and one on leave.
    if allocs:
        marks = {a.student_id: "present" for a in allocs}
        marks[allocs[3].student_id] = "leave"
        marks[allocs[7].student_id] = "absent"
        service.save_roll_call(db, company_id, block, on - timedelta(days=1), "night", marks,
                               {allocs[7].student_id: "Came back late from sports practice"})
        now = datetime.now(timezone.utc)
        db.add(HostelLeave(company_id=company_id, student_id=allocs[3].student_id, kind="leave",
                           from_at=now - timedelta(days=1, hours=4), to_at=now + timedelta(days=1), reason="Family function",
                           escort="Father", requested_by="Parent", requested_via="portal", status="out",
                           decided_by="Warden", decided_at=now - timedelta(days=2), out_at=now - timedelta(days=1, hours=4)))
        db.add(HostelLeave(company_id=company_id, student_id=allocs[5].student_id, kind="outpass",
                           from_at=now + timedelta(days=2), to_at=now + timedelta(days=2, hours=5), reason="Dental appointment",
                           escort="Mother", requested_by="Parent", requested_via="portal", status="pending"))
        db.add(HostelLeave(company_id=company_id, student_id=allocs[9].student_id, kind="leave",
                           from_at=now + timedelta(days=5), to_at=now + timedelta(days=7), reason="Weekend home visit",
                           escort="Uncle", requested_by="Parent", requested_via="portal", status="pending"))
        for a in allocs[:3]:
            db.add(HostelVisitor(company_id=company_id, block_id=block.id, student_id=a.student_id,
                                 name=f"{rng.choice(['Suresh', 'Anitha', 'Prakash', 'Meena'])} {a.student.last_name}",
                                 relation=rng.choice(["Father", "Mother"]), phone=f"+91 9{rng.randint(100000000, 999999999)}",
                                 id_proof="Aadhaar", purpose="Weekend visit", in_at=now - timedelta(hours=rng.randint(2, 30)),
                                 out_at=now - timedelta(hours=1), logged_by="Warden"))
    db.flush()
