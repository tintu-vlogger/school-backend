"""Timetable defaults (a bell schedule every institution starts with) and the demo timetable:
rooms, realistic weekly periods, a generated and published timetable for Grades 6-10 (every
section Divya Menon teaches, including Grade 8 - A) and a few substitutions."""
import logging
from datetime import time, timedelta

from sqlalchemy.orm import Session

log = logging.getLogger("hrms")

WEEKDAY_PERIODS = [
    ("Assembly", "assembly", "08:15", "08:30"),
    ("Period 1", "class", "08:30", "09:15"), ("Period 2", "class", "09:15", "10:00"),
    ("Period 3", "class", "10:00", "10:45"), ("Short break", "break", "10:45", "11:00"),
    ("Period 4", "class", "11:00", "11:45"), ("Period 5", "class", "11:45", "12:30"),
    ("Lunch", "break", "12:30", "13:10"),
    ("Period 6", "class", "13:10", "13:55"), ("Period 7", "class", "13:55", "14:40"),
    ("Period 8", "class", "14:40", "15:20"),
]
SATURDAY_PERIODS = [
    ("Period 1", "class", "08:30", "09:10"), ("Period 2", "class", "09:10", "09:50"),
    ("Break", "break", "09:50", "10:05"),
    ("Period 3", "class", "10:05", "10:45"), ("Period 4", "class", "10:45", "11:25"),
]
# Weekly periods per subject code for the demo (44 slots a week: 5 x 8 + 4).
DEMO_PERIODS = {"MAT": 7, "SCI": 7, "ENG": 6, "SST": 6, "HIN": 5, "CS": 3, "PE": 3, "ART": 2, "MUS": 2}
DEMO_GRADES = ("Grade 6", "Grade 7", "Grade 8", "Grade 9", "Grade 10")


def _periods(rows):
    return [{"name": n, "kind": k, "start": s, "end": e} for n, k, s, e in rows]


def setup_company(db: Session, company_id: int) -> None:
    """Every institution gets a starting bell schedule for its working days (once)."""
    try:
        with db.begin_nested():
            from modules.timetable import service
            from modules.timetable.models import BellSchedule
            if db.query(BellSchedule.id).filter(BellSchedule.company_id == company_id).first():
                return
            days = service.week_days(db, company_id)
            weekdays = [d for d in days if d <= 5] or days
            service.save_schedule(db, company_id, {"name": "Regular day", "weekdays": weekdays, "is_default": True,
                                                   "periods": _periods(WEEKDAY_PERIODS)})
            if 6 in days:
                service.save_schedule(db, company_id, {"name": "Saturday", "weekdays": [6],
                                                       "periods": _periods(SATURDAY_PERIODS)})
    except Exception as exc:  # defaults must never block sign-up or start-up
        log.warning("timetable defaults failed for company %s: %s", company_id, exc)


def seed_demo(db: Session, company_id: int) -> None:
    from core import context
    from modules.academics import service as academics
    from modules.academics.models import Room, Section, SectionSubject, Subject
    from modules.timetable import service
    from modules.timetable.models import TimetableEntry
    setup_company(db, company_id)
    if db.query(TimetableEntry.id).filter(TimetableEntry.company_id == company_id).first():
        return
    sess = academics.current_session(db, company_id)
    if sess is None:
        return
    context.current().actor_name = context.current().actor_name or "Campusly demo"
    sections = [s for s in db.query(Section).filter(Section.company_id == company_id, Section.session_id == sess.id).all()
                if s.program and s.program.name in DEMO_GRADES]
    if not sections:
        return
    sections.sort(key=lambda s: (DEMO_GRADES.index(s.program.name), s.name))

    def room(name, kind="classroom", building="Main block", floor=None, capacity=40):
        r = db.query(Room).filter(Room.company_id == company_id, Room.name == name).first()
        if r is None:
            r = Room(company_id=company_id, name=name, kind=kind, building=building, floor=floor, capacity=capacity)
            db.add(r)
            db.flush()
        return r

    for s in sections:
        grade = s.program.name.split()[-1]
        r = room(f"Room {grade}{s.name}", floor="1" if int(grade) <= 8 else "2")
        if s.room_id is None:
            s.room_id = r.id
    special = {"CS": room("Computer Lab", "lab", "Science block", "1", 36), "SCI": room("Science Lab", "lab", "Science block", "G", 36),
               "MUS": room("Music Room", "classroom", "Activity block", "G", 40),
               "ART": room("Art Room", "classroom", "Activity block", "G", 40),
               "PE": room("Playground", "sports", "Grounds", None, 200)}
    codes = {sub.id: sub.code for sub in db.query(Subject).filter(Subject.company_id == company_id)}
    for a in db.query(SectionSubject).filter(SectionSubject.company_id == company_id,
                                             SectionSubject.section_id.in_([s.id for s in sections])):
        want = DEMO_PERIODS.get(codes.get(a.subject_id))
        if want and (a.weekly_periods in (None, 5)):
            a.weekly_periods = want
    db.flush()
    service.generate(db, company_id, [s.id for s in sections])
    # Labs and activity rooms where free.
    used = set()
    rows = db.query(TimetableEntry).filter(TimetableEntry.company_id == company_id, TimetableEntry.active.is_(True)).all()
    for e in sorted(rows, key=lambda x: (x.weekday, x.period_no, x.section_id)):
        code = codes.get(e.subject_id)
        r = special.get(code)
        if r is None:
            continue
        if code == "SCI" and (e.weekday + e.period_no) % 3:
            continue  # science is mostly in the classroom; some periods in the lab
        if (r.id, e.weekday, e.period_no) in used and code != "PE":
            continue
        e.room_id = r.id
        used.add((r.id, e.weekday, e.period_no))
    db.flush()
    service.publish(db, company_id, [s.id for s in sections])
    # A couple of substitutions today (or the last working day) for one teacher's classes.
    from core.timeutil import today_for_company_id
    day = today_for_company_id(db, company_id)
    days = service.week_days(db, company_id)
    while day.isoweekday() not in days:
        day -= timedelta(days=1)
    entries = [e for e in rows if e.active and e.weekday == day.isoweekday() and e.teacher_id]
    if entries:
        from collections import Counter
        busiest = Counter(e.teacher_id for e in entries).most_common(1)[0][0]
        for e in [x for x in entries if x.teacher_id == busiest][:2]:
            free = service.free_teachers(db, company_id, day, e.period_no, e)
            if free:
                try:
                    service.assign_substitution(db, company_id, e, day, "substitute", free[0]["employee_id"],
                                                "Teacher on leave", force=True)
                except Exception:
                    pass
    db.flush()
