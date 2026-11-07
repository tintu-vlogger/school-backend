"""Communication defaults for every institution and sample data for the demo:
notices (some for Grade 8 - A parents), conversations between parent@test.com and Divya Menon,
calendar events this month and next, a PTM day with a booking, and a few diary notes."""
import logging
from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

logger = logging.getLogger("hrms")


def setup_company(db: Session, company_id: int) -> None:
    try:
        from modules.comms.models import CommsSettings
        if not db.query(CommsSettings.id).filter(CommsSettings.company_id == company_id).first():
            db.add(CommsSettings(company_id=company_id, config={}))
            db.flush()
    except Exception as exc:  # never block start-up
        logger.warning("Communication defaults for company %s failed: %s", company_id, exc)


def seed_demo(db: Session, company_id: int) -> None:
    from modules.comms.models import Notice
    if db.query(Notice.id).filter(Notice.company_id == company_id).first():
        return
    # The demo classes and learners come first (both seeds skip themselves when already done).
    from modules.academics import seed as academics_seed
    from modules.students import seed as students_seed
    academics_seed.seed_demo(db, company_id)
    students_seed.seed_demo(db, company_id)

    from modules.academics.models import Program, Section
    from modules.comms import notices, ptm, service
    from modules.comms.models import (DiaryEntry, Event, Message, NoticeRecipient, PtmBooking, PtmDay, PtmSlot,
                                      PtmTeacher, Thread)
    from modules.employees.models import Employee
    from modules.students.models import PortalAccount

    def emp(email):
        return db.query(Employee).filter(Employee.company_id == company_id, Employee.email == email).first()

    divya = emp("divya.menon@example.com")
    principal = emp("kavitha.raghavan@example.com")
    parent = db.query(PortalAccount).filter(PortalAccount.company_id == company_id,
                                            PortalAccount.login == "parent@test.com").first()
    student_acc = db.query(PortalAccount).filter(PortalAccount.company_id == company_id,
                                                 PortalAccount.login == "student@test.com").first()
    g8 = db.query(Program).filter(Program.company_id == company_id, Program.name == "Grade 8").first()
    sec_a = db.query(Section).filter(Section.company_id == company_id, Section.program_id == (g8.id if g8 else -1),
                                     Section.name == "A").first() if g8 else None
    sec_b = db.query(Section).filter(Section.company_id == company_id, Section.program_id == (g8.id if g8 else -1),
                                     Section.name == "B").first() if g8 else None
    if not (divya and parent and sec_a):
        return
    now = service.now()
    tz = service.company_tz(db, company_id)
    author = principal or divya
    a_name = service.employee_name(author)
    aarav_id = student_acc.student_id if student_acc else None

    # ── Notices ──
    g8a = {"learners": {"parents": True, "scope": "sections", "section_ids": [sec_a.id]}}
    specs = [
        ("Diwali holidays", "holiday", {"everyone": True},
         "The institution stays closed from **Monday to Wednesday** for Diwali.\n\nClasses resume on Thursday. "
         "Wishing all families a safe and happy festival.", True, False, a_name, 6),
        ("Term 2 fee due by 15th", "fee", {"learners": {"parents": True, "scope": "all"}},
         "Term 2 fees are due by the **15th** of this month. Pay online from the Fees page or at the office "
         "(9 am to 1 pm).\n\nA late fee applies after the due date.", False, True, a_name, 5),
        ("Grade 8 - A: Science project submission", "academic", g8a,
         "Please help your child finish the **water conservation model**. Submission is on Friday in the "
         "science lab.\n\n- Use recycled material\n- Add a short write-up (one page)", False, False,
         service.employee_name(divya), 4),
        ("Grade 8 - A: Field trip to the planetarium", "event", g8a,
         "We are visiting the planetarium next Tuesday. Please sign and return the consent slip and send a "
         "packed lunch.\n\nBus leaves at **8:15 am**.", False, True, service.employee_name(divya), 3),
        ("Mid-term exam timetable", "exam", {"learners": {"parents": True, "students": True, "scope": "all"}},
         "The mid-term examinations start on the 24th. The full timetable is attached in the Exams page.\n\n"
         "Learners must carry their hall tickets.", True, False, a_name, 2),
        ("Staff meeting on Saturday", "general", {"staff": {"scope": "all"}},
         "All staff are requested to attend the monthly staff meeting on **Saturday at 12:30 pm** in the "
         "conference hall.", False, True, a_name, 2),
        ("Grade 8 - A: Parent-teacher meeting", "academic", g8a,
         "Parent-teacher meetings for Grade 8 - A are open for booking. Please pick a slot with each teacher "
         "from the Meetings page.", False, False, service.employee_name(divya), 1),
        ("Heavy rain: early dismissal today", "emergency", {"everyone": True},
         "Due to heavy rain, classes end at **12:30 pm** today. Buses leave at 12:45 pm. Parents picking up "
         "children, please come to Gate 2.", False, False, a_name, 9),
    ]
    made = []
    for title, cat, aud, body, pinned, ack, by, days_ago in specs:
        n = Notice(company_id=company_id, title=title, category=cat, body=body, pinned=pinned, require_ack=ack,
                   author_name=by, author_employee_id=(divya.id if by == service.employee_name(divya) else
                                                       (author.id if author else None)),
                   channels=["in_app"], audience=service.clean_audience(aud),
                   audience_label=service.audience_label(db, company_id, aud), status="draft",
                   published_at=now - timedelta(days=days_ago, hours=2))
        if cat == "emergency":
            n.expires_at = now - timedelta(days=days_ago - 1)
        db.add(n)
        db.flush()
        try:
            notices.publish(db, n)
        except Exception as exc:
            logger.warning("Demo notice %s not sent: %s", title, exc)
        made.append(n)
    # The demo parent has read a few; the first teacher notice is acknowledged.
    key = f"g:{parent.guardian_id}"
    for n in made[:4]:
        r = db.query(NoticeRecipient).filter(NoticeRecipient.notice_id == n.id, NoticeRecipient.recipient_key == key).first()
        if r:
            r.read_at = n.published_at + timedelta(hours=3)
            if n.require_ack:
                r.acknowledged_at = r.read_at
    db.flush()

    # ── Conversations between parent@test.com and Divya Menon ──
    p_name = parent.guardian.full_name if parent.guardian else "Parent"
    d_name = service.employee_name(divya)
    convs = [
        ("Aarav's maths homework", "parent", [
            ("parent", "Good morning ma'am. Aarav found the algebra worksheet difficult. Could you suggest some practice material?", 50),
            ("staff", "Good morning! I'll share a short practice set tomorrow. He did well in class today.", 46),
            ("parent", "Thank you so much, that helps.", 45),
        ]),
        ("Leave on Friday", "parent", [
            ("parent", "Aarav will be absent on Friday for a family function. Kindly excuse him.", 20),
            ("staff", "Noted, thank you for letting me know. I'll send him the notes on Monday.", 18),
        ]),
        ("Science project materials", "staff", [
            ("staff", "Hello! For the science project Aarav needs a shoebox and two plastic bottles by Thursday.", 6),
            ("parent", "Sure, we'll send them on Wednesday. Should he bring glue as well?", 3),
        ]),
    ]
    for subject, started, msgs in convs:
        t = Thread(company_id=company_id, subject=subject, staff_employee_id=divya.id, party_kind="parent",
                   guardian_id=parent.guardian_id, about_student_id=aarav_id, section_id=sec_a.id, started_by=started)
        db.add(t)
        db.flush()
        for who, body, hours_ago in msgs:
            at = now - timedelta(hours=hours_ago)
            db.add(Message(company_id=company_id, thread_id=t.id, sender_kind="staff" if who == "staff" else "parent",
                           sender_employee_id=divya.id if who == "staff" else None,
                           sender_portal_id=None if who == "staff" else parent.id,
                           sender_name=d_name if who == "staff" else p_name, body=body, created_at=at))
        t.message_count = len(msgs)
        t.last_message_at = now - timedelta(hours=msgs[-1][2])
        t.last_message_preview = msgs[-1][1][:200]
        t.staff_unread = 1 if msgs[-1][0] == "parent" and hours_ago < 10 else 0
        t.party_unread = 0
    db.flush()

    # ── Events this month and next ──
    first = date.today().replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)

    def d(base, day):
        import calendar
        return base.replace(day=min(day, calendar.monthrange(base.year, base.month)[1]))

    ev_specs = [
        ("Diwali holidays", "holiday", d(first, 10), d(first, 12), None, None, None, {"everyone": True}, False),
        ("Science exhibition", "event", d(first, 14), None, "10:00", "13:00", "Main hall", {"everyone": True}, True),
        ("Grade 8 - A planetarium trip", "trip", d(first, 18), None, "08:15", "15:00", "City planetarium", g8a, True),
        ("Inter-house football final", "sports", d(first, 21), None, "15:30", "17:00", "Sports ground", {"everyone": True}, False),
        ("Mid-term exams begin", "exam", d(first, 24), d(first, 29), None, None, None,
         {"learners": {"parents": True, "students": True, "scope": "all"}}, False),
        ("Monthly staff meeting", "meeting", d(first, 22), None, "12:30", "13:30", "Conference hall",
         {"staff": {"scope": "all"}}, False),
        ("Children's Day celebrations", "cultural", d(first, 14), None, "09:00", "12:00", "Auditorium", {"everyone": True}, False),
        ("Annual sports day", "sports", d(nxt, 6), None, "08:30", "14:00", "Sports ground", {"everyone": True}, True),
        ("Christmas break", "holiday", d(nxt, 24), d(nxt, 31), None, None, None, {"everyone": True}, False),
        ("Annual day rehearsal", "cultural", d(nxt, 15), None, "14:00", "16:30", "Auditorium",
         {"learners": {"students": True, "scope": "all"}}, False),
    ]
    for title, cat, sd, ed, st, en, loc, aud, rsvp in ev_specs:
        e = Event(company_id=company_id, title=title, category=cat, location=loc, rsvp_required=rsvp,
                  all_day=st is None, start_date=sd, end_date=ed or sd, created_by=a_name,
                  audience=service.clean_audience(aud), audience_label=service.audience_label(db, company_id, aud))
        if st:
            e.start_at = datetime.combine(sd, time.fromisoformat(st), tzinfo=tz)
            e.end_at = datetime.combine(sd, time.fromisoformat(en), tzinfo=tz)
        db.add(e)
    db.flush()

    # ── A PTM day for Grade 8 with a booking by parent@test.com ──
    day = date.today() + timedelta(days=9)
    if day.isoweekday() == 7:
        day += timedelta(days=1)
    pd = PtmDay(company_id=company_id, title="Grade 8 parent-teacher meeting", date=day, start_time=time(9, 0),
                end_time=time(12, 30), slot_minutes=10, gap_minutes=0,
                breaks=[{"start": "10:30", "end": "10:45", "label": "Tea break"}], mode="both",
                location="Grade 8 classrooms", meeting_link="https://meet.example.com/grade8-ptm",
                section_ids=[s.id for s in (sec_a, sec_b) if s], status="draft", created_by=a_name,
                instructions="Please be on time. Each meeting is 10 minutes. Bring the term report card.",
                notes_visible_default=True)
    db.add(pd)
    db.flush()
    ptm.set_teachers(db, pd, ptm.suggested_teachers(db, company_id, pd.section_ids))
    ptm.open_day(db, pd, a_name)
    pt = db.query(PtmTeacher).filter(PtmTeacher.ptm_day_id == pd.id, PtmTeacher.employee_id == divya.id).first()
    if pt and aarav_id:
        slot = db.query(PtmSlot).filter(PtmSlot.ptm_teacher_id == pt.id).order_by(PtmSlot.start_at).offset(2).first()
        if slot:
            db.add(PtmBooking(company_id=company_id, ptm_day_id=pd.id, ptm_teacher_id=pt.id, slot_id=slot.id,
                              student_id=aarav_id, guardian_id=parent.guardian_id, booked_by=p_name, mode="in_person",
                              parent_note="Would like to discuss maths progress.", notes_visible=True))
    db.flush()

    # ── Daily diary for Grade 8 - A ──
    notes = ["Maths: finish exercise 4.2 (Q1-10). English: read chapter 5.",
             "Science: bring the project materials. Hindi: learn the poem.",
             "Social science: map work on rivers of India. Carry an atlas tomorrow.",
             "Computer science: practise the Python loops worksheet."]
    for i, body in enumerate(notes):
        dt_ = date.today() - timedelta(days=i)
        if dt_.isoweekday() == 7:
            continue
        db.add(DiaryEntry(company_id=company_id, section_id=sec_a.id, date=dt_, body=body,
                          author_employee_id=divya.id, author_name=d_name))
    db.flush()
