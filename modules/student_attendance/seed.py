"""Student attendance defaults (statuses) and demo data: registers for the last 30 days for
every section with realistic absences, a few chronically absent learners, approved and
pending leave requests (one pending for Aarav Kumar, waiting for his class teacher)."""
import logging
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

log = logging.getLogger("hrms")


def setup_company(db: Session, company_id: int) -> None:
    try:
        with db.begin_nested():
            from modules.student_attendance import service
            service.ensure_statuses(db, company_id)
    except Exception as exc:  # defaults must never block sign-up or start-up
        log.warning("student attendance defaults failed for company %s: %s", company_id, exc)


def seed_demo(db: Session, company_id: int) -> None:
    from core import context
    from core.timeutil import today_for_company_id
    from modules.academics import service as academics
    from modules.student_attendance import service
    from modules.student_attendance.models import (AttendanceMark, AttendanceRegister, StudentLeaveRequest)
    from modules.students import seed as students_seed
    from modules.students import service as students
    setup_company(db, company_id)
    # Learners first (this module sorts before "students").
    students_seed.seed_demo(db, company_id)
    db.flush()
    if db.query(AttendanceRegister.id).filter(AttendanceRegister.company_id == company_id).first():
        return
    if service.mode(db, company_id) != "daily":
        return
    rng = random.Random(42)
    today = today_for_company_id(db, company_id)
    codes = service._status_by_code(db, company_id)
    sections = service.current_sections(db, company_id)
    if not sections:
        return
    ctx = context.current()
    start = today - timedelta(days=30)
    leave_days: dict[tuple, StudentLeaveRequest] = {}
    roster = {s.id: students.section_students(db, company_id, s.id) for s in sections}
    all_students = [(sec, st) for sec in sections for st, _ in roster[sec.id]]
    # A few approved past leaves.
    picks = rng.sample(all_students, min(6, len(all_students)))
    for i, (sec, st) in enumerate(picks):
        f = start + timedelta(days=rng.randint(3, 22))
        t = f + timedelta(days=rng.choice([0, 1, 2]))
        code = "ML" if i % 3 == 0 else "EX"
        reason = "Fever and cold, doctor advised rest" if code == "ML" else rng.choice(
            ["Family wedding out of town", "Grandmother's house warming", "Travelling for a family function"])
        r = StudentLeaveRequest(company_id=company_id, student_id=st.id, section_id=sec.id, from_date=f, to_date=t,
                                days=(t - f).days + 1, status_code=code, reason=reason, status="approved",
                                requested_by_kind="parent", requested_by_name="Parent",
                                decided_by_name=(f"{sec.class_teacher.first_name} {sec.class_teacher.last_name}"
                                                 if sec.class_teacher else "Class teacher"),
                                decided_at=datetime.now(timezone.utc) - timedelta(days=(today - f).days + 1))
        db.add(r)
        db.flush()
        d = f
        while d <= t:
            leave_days[(st.id, d)] = r
            d += timedelta(days=1)
    chronic = {st.id for _, st in rng.sample(all_students, min(5, len(all_students)))}
    aarav = next((st for _, st in all_students if st.email == "student@test.com"), None)
    if aarav:
        chronic.discard(aarav.id)
    cal_cache = {}
    d = start
    while d <= today:
        for sec in sections:
            if sec.program_id not in cal_cache:
                cal_cache[sec.program_id] = service.calendar_map(db, company_id, start, today, sec.program_id)
            if not cal_cache[sec.program_id][d][0]:
                continue
            if d == today and rng.random() < 0.35:
                continue  # some registers still to be marked today
            teacher = sec.class_teacher
            marked_at = datetime(d.year, d.month, d.day, 3, rng.randint(5, 40), tzinfo=timezone.utc)
            reg = AttendanceRegister(company_id=company_id, section_id=sec.id, date=d, slot="day", period_no=0,
                                     subject_key=0, marked_by_id=teacher.id if teacher else None,
                                     marked_by_name=f"{teacher.first_name} {teacher.last_name}" if teacher else "Office",
                                     marked_at=marked_at,
                                     updated_by_name=f"{teacher.first_name} {teacher.last_name}" if teacher else "Office")
            db.add(reg)
            db.flush()
            total = present = absent = 0
            for st, _ in roster[sec.id]:
                lv = leave_days.get((st.id, d))
                late = None
                remarks = None
                if lv:
                    code, source = lv.status_code, "leave"
                else:
                    source = "manual"
                    x = rng.random()
                    p_abs = 0.22 if st.id in chronic else 0.035
                    if aarav and st.id == aarav.id:
                        p_abs = 0.06
                    if x < p_abs:
                        code = "A"
                    elif x < p_abs + 0.025:
                        code, late = "L", rng.choice([5, 10, 15, 20])
                        remarks = rng.choice([None, "Bus was late", "Traffic"])
                    elif x < p_abs + 0.035:
                        code, remarks = "HD", "Left after lunch"
                    elif x < p_abs + 0.04:
                        code, remarks = "OD", "Inter-school quiz"
                    else:
                        code = "P"
                st_row = codes[code]
                db.add(AttendanceMark(company_id=company_id, register_id=reg.id, student_id=st.id, date=d,
                                      section_id=sec.id, status_id=st_row.id, code=code, late_minutes=late,
                                      remarks=remarks, source=source, leave_request_id=lv.id if lv else None))
                total += 1
                if st_row.is_absent:
                    absent += 1
                elif float(st_row.counts_as) > 0:
                    present += 1
            reg.total, reg.present, reg.absent = total, present, absent
        d += timedelta(days=1)
    db.flush()
    # A pending request from the demo parent for Aarav (waits for his class teacher, Divya).
    if aarav:
        ctx.actor_name, ctx.actor_type = "Rajesh Kumar", "parent"
        try:
            nxt = today + timedelta(days=3)
            service.create_leave(db, company_id, {"role": "parent", "portal_id": None}, aarav.id, nxt,
                                 nxt + timedelta(days=1), "Cousin's wedding in Mysuru", "EX")
        except Exception as exc:
            log.warning("demo leave request failed: %s", exc)
        ctx.actor_name, ctx.actor_type = None, None
    # One more pending request in another section.
    others = [(sec, st) for sec, st in all_students if not aarav or st.id != aarav.id]
    if others:
        sec, st = others[len(others) // 3]
        ctx.actor_name, ctx.actor_type = "Parent", "parent"
        nxt = today + timedelta(days=1)
        while not service.day_info(db, company_id, nxt, sec.program_id)["working"] and nxt < today + timedelta(days=10):
            nxt += timedelta(days=1)
        try:
            service.create_leave(db, company_id, {"role": "parent", "portal_id": None}, st.id, nxt, nxt,
                                 "Dental appointment", "EX")
        except Exception as exc:
            log.warning("demo leave request failed: %s", exc)
        ctx.actor_name, ctx.actor_type = None, None
    db.flush()
