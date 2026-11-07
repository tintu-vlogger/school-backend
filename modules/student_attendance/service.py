"""Learner attendance rules.

- Modes come from the institution profile (`attendance_mode`): "daily" (one register per day,
  or morning and afternoon when the setting asks for two sessions) and "period" (one register
  per timetable period, marked by the subject teacher or a substitute).
- Working days: the institution's week days, minus staff holidays of the default holiday
  calendar (setting), with the institution's own calendar days on top (extra holidays and
  extra working days, for everyone or some programs).
- Percentages: each mark counts `status.counts_as` (1 present, 0.5 half day, 0 absent) out of
  1, for statuses that count in the total. Two sessions a day are two units, periods are one
  unit each, so the same formula works in every mode.
- Locking: a register can be changed by its teacher until `lock_after_days` days after its
  date; later changes need attendance.correct and a reason and are logged as corrections.
- Leave: parents and learners ask for leave from the portal; the class teacher approves it
  through the workflow engine; approved days pre-fill the register with the leave status and
  absent marks already given for those days are turned into leave.
- Alerts: guardians hear when a learner is marked absent (at once or after a set time) and
  once a week when attendance falls below the threshold.
"""
from __future__ import annotations

import io
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.orm.attributes import flag_modified

from core import audit, context
from core.errors import BadRequest, Forbidden, NotFound
from core.timeutil import iso_utc, today_for_company_id
from modules.academics import service as academics
from modules.academics.models import AcademicSession, Program, ProgramSubject, Section, SectionSubject, Subject, Term
from modules.student_attendance.models import (AttendanceAlert, AttendanceCorrection, AttendanceMark, AttendanceRegister,
                                               AttendanceSettings, AttendanceStatus, CalendarDay, StudentLeaveRequest)
from modules.students import service as students
from modules.students.models import Student

REQUEST_TYPE = "attendance.student_leave"

# code, name, counts_as, in_total, is_absent, is_late, is_leave, colour
DEFAULT_STATUSES = [
    ("P", "Present", "1", True, False, False, False, "#30a46c"),
    ("A", "Absent", "0", True, True, False, False, "#e5484d"),
    ("L", "Late", "1", True, False, True, False, "#f59e0b"),
    ("HD", "Half day", "0.5", True, False, False, False, "#8e4ec6"),
    ("EX", "Leave", "0", True, False, False, True, "#3e63dd"),
    ("ML", "Medical leave", "0", True, False, False, True, "#0091ff"),
    ("OD", "On duty", "1", True, False, False, False, "#12a594"),
]

DEFAULTS = {
    "daily_sessions": 1,              # daily mode: 1 = once a day, 2 = morning and afternoon
    "lock_after_days": 3,             # teachers may change a register for this many days after its date
    "class_teacher_only": True,       # daily mode: only the class teacher (and office) marks
    "absence_alerts": True,           # tell guardians when a learner is marked absent
    "absence_alert_when": "on_mark",  # on_mark | after_time
    "absence_alert_time": "11:00",    # local time for after_time
    "alert_on_late": False,
    "low_attendance_threshold": 75,   # percent
    "low_attendance_alerts": True,    # weekly alert to guardians below the threshold
    "low_alert_weekday": 1,           # ISO weekday the weekly alert goes out
    "low_alert_min_units": 10,        # need this many marked days/periods before alerting
    "notify_class_teacher_low": True,
    "use_staff_holidays": True,       # staff holidays (default holiday calendar) are holidays for learners too
    "leave_max_days": 15,
    "leave_past_days": 7,             # how far back parents may ask for leave
    "leave_attachment_after_days": 3,  # a note or certificate is needed for longer leave (0 = never)
}

WEEKDAYS = ["", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _name(e) -> str | None:
    return f"{e.first_name} {e.last_name}".strip() if e else None


# ── Settings and statuses ────────────────────────────────────────────

def get_settings(db: Session, company_id: int) -> dict:
    row = db.query(AttendanceSettings).filter(AttendanceSettings.company_id == company_id).first()
    return {**DEFAULTS, **((row.config if row else None) or {})}


def save_settings(db: Session, company_id: int, data: dict) -> dict:
    row = db.query(AttendanceSettings).filter(AttendanceSettings.company_id == company_id).first()
    if row is None:
        row = AttendanceSettings(company_id=company_id, config={})
        db.add(row)
    cfg = dict(row.config or {})
    for key, default in DEFAULTS.items():
        if key not in data or data[key] is None:
            continue
        val = data[key]
        if isinstance(default, bool):
            cfg[key] = bool(val)
        elif isinstance(default, int):
            try:
                cfg[key] = int(val)
            except (TypeError, ValueError):
                raise BadRequest("Use whole numbers for days and limits.")
        else:
            cfg[key] = str(val)
    if cfg.get("daily_sessions", 1) not in (1, 2):
        raise BadRequest("Daily attendance is taken once or twice a day.")
    if not 0 <= cfg.get("lock_after_days", 3) <= 365:
        raise BadRequest("Lock after 0 to 365 days.")
    if not 1 <= cfg.get("low_attendance_threshold", 75) <= 100:
        raise BadRequest("The threshold is a percentage between 1 and 100.")
    if cfg.get("absence_alert_when", "on_mark") not in ("on_mark", "after_time"):
        raise BadRequest("Send absence alerts when marked or after a set time.")
    try:
        _parse_hm(cfg.get("absence_alert_time", "11:00"))
    except ValueError:
        raise BadRequest("The alert time must look like 11:00.")
    row.config = cfg
    flag_modified(row, "config")
    db.flush()
    return get_settings(db, company_id)


def _parse_hm(v: str) -> time:
    h, m = str(v).split(":")[:2]
    return time(int(h), int(m))


def ensure_statuses(db: Session, company_id: int) -> None:
    have = {c for (c,) in db.query(AttendanceStatus.code).filter(AttendanceStatus.company_id == company_id)}
    for i, (code, name, counts, in_total, absent, late, leave, color) in enumerate(DEFAULT_STATUSES, 1):
        if code in have:
            continue
        db.add(AttendanceStatus(company_id=company_id, code=code, name=name, counts_as=Decimal(counts), in_total=in_total,
                                is_absent=absent, is_late=late, is_leave=leave, color=color, sort_order=i, is_system=True))
    db.flush()


def list_statuses(db: Session, company_id: int, active_only: bool = True) -> list[AttendanceStatus]:
    q = db.query(AttendanceStatus).filter(AttendanceStatus.company_id == company_id)
    if active_only:
        q = q.filter(AttendanceStatus.active.is_(True))
    return q.order_by(AttendanceStatus.sort_order, AttendanceStatus.code).all()


def status_out(s: AttendanceStatus) -> dict:
    return {"id": s.id, "uuid": str(s.uuid), "code": s.code, "name": s.name, "counts_as": float(s.counts_as),
            "in_total": s.in_total, "is_absent": s.is_absent, "is_late": s.is_late, "is_leave": s.is_leave,
            "color": s.color, "sort_order": s.sort_order, "is_system": s.is_system, "active": s.active}


def _status_by_code(db: Session, company_id: int) -> dict[str, AttendanceStatus]:
    return {s.code: s for s in db.query(AttendanceStatus).filter(AttendanceStatus.company_id == company_id)}


def present_code(db: Session, company_id: int) -> str:
    codes = _status_by_code(db, company_id)
    if "P" in codes and codes["P"].active:
        return "P"
    for s in list_statuses(db, company_id):
        if float(s.counts_as) >= 1 and not s.is_late and not s.is_leave:
            return s.code
    raise BadRequest("Add a present status first (Settings).")


def absent_code(db: Session, company_id: int) -> str:
    for s in list_statuses(db, company_id):
        if s.is_absent:
            return s.code
    return "A"


# ── Mode, calendar and working days ──────────────────────────────────

def _company(db: Session, company_id: int):
    from modules.company.models import Company
    return db.query(Company).filter(Company.id == company_id).first()


def profile(db: Session, company_id: int) -> dict:
    return academics.profile(_company(db, company_id))


def mode(db: Session, company_id: int) -> str:
    m = profile(db, company_id).get("attendance_mode") or "daily"
    return "period" if m == "period" else "daily"


def daily_sessions(db: Session, company_id: int) -> int:
    if profile(db, company_id).get("attendance_mode") == "session":
        return 2
    return 2 if get_settings(db, company_id).get("daily_sessions") == 2 else 1


def _staff_holidays(db: Session, company_id: int, start: date, end: date) -> dict[date, str]:
    try:
        from modules.leave.models import Holiday, HolidayCalendar
        with db.begin_nested():
            cal = (db.query(HolidayCalendar).filter(HolidayCalendar.company_id == company_id,
                                                    HolidayCalendar.active.is_(True))
                   .order_by(HolidayCalendar.is_default.desc(), HolidayCalendar.priority).first())
            if cal is None:
                return {}
            rows = db.query(Holiday.date, Holiday.name).filter(
                Holiday.company_id == company_id, Holiday.calendar_id == cal.id, Holiday.kind != "optional",
                Holiday.date >= start, Holiday.date <= end).all()
        return {d: n for d, n in rows}
    except Exception:
        return {}


def calendar_map(db: Session, company_id: int, start: date, end: date,
                 program_id: int | None = None) -> dict[date, tuple[bool, str | None]]:
    """Every date in the range -> (is a working day, why not)."""
    week = set(profile(db, company_id).get("week_days") or [1, 2, 3, 4, 5, 6])
    staff = _staff_holidays(db, company_id, start, end) if get_settings(db, company_id)["use_staff_holidays"] else {}
    own: dict[date, list[CalendarDay]] = defaultdict(list)
    for c in db.query(CalendarDay).filter(CalendarDay.company_id == company_id, CalendarDay.active.is_(True),
                                          CalendarDay.date >= start, CalendarDay.date <= end):
        if not c.program_ids or (program_id and program_id in c.program_ids):
            own[c.date].append(c)
    out = {}
    d = start
    while d <= end:
        days = own.get(d, [])
        hol = next((c for c in days if c.kind == "holiday"), None)
        work = next((c for c in days if c.kind == "working"), None)
        if hol:
            out[d] = (False, hol.name)
        elif work:
            out[d] = (True, None)
        elif d in staff:
            out[d] = (False, staff[d])
        elif d.isoweekday() not in week:
            out[d] = (False, "Weekly off")
        else:
            out[d] = (True, None)
        d += timedelta(days=1)
    return out


def day_info(db: Session, company_id: int, d: date, program_id: int | None = None) -> dict:
    working, why = calendar_map(db, company_id, d, d, program_id)[d]
    return {"date": d.isoformat(), "working": working, "reason": why}


def working_days_between(db: Session, company_id: int, start: date, end: date, program_id: int | None = None) -> int:
    return sum(1 for ok, _ in calendar_map(db, company_id, start, end, program_id).values() if ok)


# ── Slots (what registers a section has on a date) ───────────────────

def slots_for(db: Session, company_id: int, section: Section, d: date) -> list[dict]:
    if mode(db, company_id) == "daily":
        if daily_sessions(db, company_id) == 2:
            return [{"slot": "am", "period_no": 0, "subject_id": None, "label": "Morning", "teacher_id": section.class_teacher_id},
                    {"slot": "pm", "period_no": 0, "subject_id": None, "label": "Afternoon", "teacher_id": section.class_teacher_id}]
        return [{"slot": "day", "period_no": 0, "subject_id": None, "label": "Day", "teacher_id": section.class_teacher_id}]
    try:
        from modules.timetable import service as timetable
    except ImportError:
        return []
    out = []
    for p in timetable.section_day(db, company_id, section.id, d):
        sub = p.get("substitution")
        if sub and sub.get("kind") == "cancelled":
            continue
        out.append({"slot": "period", "period_no": p["period_no"], "subject_id": p["subject_id"],
                    "subject_name": p["subject_name"], "label": f"P{p['period_no']} · {p['subject_name'] or 'Free'}",
                    "start": p.get("start"), "end": p.get("end"), "entry_id": p["id"],
                    "teacher_id": (sub or {}).get("teacher_id") or p.get("teacher_id"),
                    "teacher_name": (sub or {}).get("teacher_name") or p.get("teacher_name")})
    return out


def _find_slot(db: Session, company_id: int, section: Section, d: date, slot: str, period_no: int,
               subject_id: int | None) -> dict:
    for s in slots_for(db, company_id, section, d):
        if s["slot"] == slot and s["period_no"] == (period_no or 0) and (slot != "period" or s["subject_id"] == subject_id):
            return s
    if slot == "period":
        raise BadRequest("That period is not in the timetable for this day.")
    raise BadRequest("Pick a register for this day.")


# ── Who may mark ─────────────────────────────────────────────────────

def can_mark(db: Session, company_id: int, user: dict, perms: list[str], section: Section, slot: dict) -> bool:
    if user.get("role") == "admin" or "attendance.mark.any" in perms:
        return True
    if "attendance.mark" not in perms:
        return False
    emp = user.get("employee_id")
    if not emp:
        return False
    if slot["slot"] != "period":
        if section.class_teacher_id == emp:
            return True
        if not get_settings(db, company_id)["class_teacher_only"] or section.class_teacher_id is None:
            return section.id in academics.teacher_section_ids(db, company_id, emp)
        return False
    if slot.get("teacher_id") == emp:
        return True
    return db.query(SectionSubject.id).filter(
        SectionSubject.company_id == company_id, SectionSubject.section_id == section.id,
        SectionSubject.subject_id == slot.get("subject_id"),
        or_(SectionSubject.teacher_id == emp, SectionSubject.assistant_ids.contains([emp]))).first() is not None


def is_locked(db: Session, company_id: int, d: date, today: date | None = None) -> bool:
    today = today or today_for_company_id(db, company_id)
    return (today - d).days > get_settings(db, company_id)["lock_after_days"]


# ── Registers ────────────────────────────────────────────────────────

def _section(db: Session, company_id: int, section_id: int) -> Section:
    s = (db.query(Section).options(joinedload(Section.program))
         .filter(Section.company_id == company_id, Section.id == section_id).first())
    if s is None:
        raise NotFound("Section")
    return s


def _roster(db: Session, company_id: int, section: Section, subject_id: int | None) -> list[tuple[Student, object]]:
    rows = students.section_students(db, company_id, section.id)
    if subject_id:
        elective = db.query(ProgramSubject.id).filter(ProgramSubject.company_id == company_id,
                                                      ProgramSubject.program_id == section.program_id,
                                                      ProgramSubject.subject_id == subject_id,
                                                      ProgramSubject.is_elective.is_(True)).first()
        if elective:
            rows = [(s, e) for s, e in rows if subject_id in (e.elective_subject_ids or [])]
    return rows


def _find_register(db: Session, company_id: int, section_id: int, d: date, slot: str, period_no: int,
                   subject_id: int | None) -> AttendanceRegister | None:
    return db.query(AttendanceRegister).filter(
        AttendanceRegister.company_id == company_id, AttendanceRegister.section_id == section_id,
        AttendanceRegister.date == d, AttendanceRegister.slot == slot,
        AttendanceRegister.period_no == (period_no or 0),
        AttendanceRegister.subject_key == ((subject_id or 0) if slot == "period" else 0)).first()


def approved_leave(db: Session, company_id: int, student_ids: list[int], start: date, end: date) -> dict[int, list[StudentLeaveRequest]]:
    out: dict[int, list] = defaultdict(list)
    if not student_ids:
        return out
    for r in db.query(StudentLeaveRequest).filter(
            StudentLeaveRequest.company_id == company_id, StudentLeaveRequest.student_id.in_(student_ids),
            StudentLeaveRequest.status.in_(("approved", "pending")), StudentLeaveRequest.from_date <= end,
            StudentLeaveRequest.to_date >= start):
        out[r.student_id].append(r)
    return out


def register_out(r: AttendanceRegister | None) -> dict | None:
    if r is None:
        return None
    return {"uuid": str(r.uuid), "date": r.date.isoformat(), "slot": r.slot, "period_no": r.period_no,
            "subject_id": r.subject_id, "marked_by": r.marked_by_name, "marked_at": iso_utc(r.marked_at),
            "updated_by": r.updated_by_name, "updated_at": iso_utc(r.updated_at),
            "total": r.total, "present": r.present, "absent": r.absent}


def load_register(db: Session, company_id: int, user: dict, perms: list[str], section_id: int, d: date,
                  slot: str | None = None, period_no: int = 0, subject_id: int | None = None) -> dict:
    """Everything the marking screen needs: learners with their marks (or the pre-filled leave),
    the statuses, whether the day is a working day and whether this person may change it."""
    section = _section(db, company_id, section_id)
    today = today_for_company_id(db, company_id)
    day = day_info(db, company_id, d, section.program_id)
    slots = slots_for(db, company_id, section, d) if day["working"] else []
    slot_info = None
    if slot:
        slot_info = next((s for s in slots if s["slot"] == slot and s["period_no"] == (period_no or 0)
                          and (slot != "period" or s["subject_id"] == subject_id)), None)
    elif slots:
        slot_info = slots[0]
    reg = None
    rows: list[dict] = []
    if slot_info:
        reg = _find_register(db, company_id, section.id, d, slot_info["slot"], slot_info["period_no"], slot_info["subject_id"])
        roster = _roster(db, company_id, section, slot_info["subject_id"] if slot_info["slot"] == "period" else None)
        marks = {m.student_id: m for m in reg.marks} if reg else {}
        leave = approved_leave(db, company_id, [s.id for s, _ in roster], d, d)
        for s, e in roster:
            m = marks.get(s.id)
            lv = next((l for l in leave.get(s.id, []) if l.status == "approved"), None)
            pending = next((l for l in leave.get(s.id, []) if l.status == "pending"), None)
            rows.append({
                "student_id": s.id, "uuid": str(s.uuid), "name": s.full_name, "roll_no": e.roll_no,
                "photo_url": s.photo_url, "admission_no": s.admission_no,
                "code": m.code if m else (lv.status_code if lv else None),
                "late_minutes": m.late_minutes if m else None, "remarks": m.remarks if m else None,
                "source": m.source if m else ("leave" if lv else None),
                "leave": {"uuid": str(lv.uuid), "reason": lv.reason, "status_code": lv.status_code} if lv else None,
                "pending_leave": {"uuid": str(pending.uuid), "reason": pending.reason} if pending else None,
            })
    locked = is_locked(db, company_id, d, today)
    may_mark = bool(slot_info) and can_mark(db, company_id, user, perms, section, slot_info)
    can_correct = user.get("role") == "admin" or "attendance.correct" in perms
    future = d > today
    can_edit = bool(slot_info) and day["working"] and not future and ((may_mark and not locked) or can_correct)
    return {
        "section": {"id": section.id, "uuid": str(section.uuid), "label": academics.section_label(section),
                    "class_teacher_id": section.class_teacher_id},
        "date": d.isoformat(), "today": today.isoformat(), "mode": mode(db, company_id), "day": day,
        "slots": [{**s, "marked": _find_register(db, company_id, section.id, d, s["slot"], s["period_no"], s["subject_id"]) is not None}
                  for s in slots],
        "slot": slot_info, "register": register_out(reg), "students": rows,
        "statuses": [status_out(s) for s in list_statuses(db, company_id)],
        "locked": locked, "future": future, "can_mark": may_mark, "can_correct": can_correct, "can_edit": can_edit,
        "needs_reason": locked and can_edit and not (may_mark and not locked),
        "lock_after_days": get_settings(db, company_id)["lock_after_days"],
    }


def save_register(db: Session, company_id: int, user: dict, perms: list[str], data: dict) -> AttendanceRegister:
    section = _section(db, company_id, int(data["section_id"]))
    d: date = data["date"]
    today = today_for_company_id(db, company_id)
    if d > today:
        raise BadRequest("Attendance can't be marked for a future date.")
    day = day_info(db, company_id, d, section.program_id)
    if not day["working"]:
        raise BadRequest(f"{d:%d %b %Y} is not a working day ({day['reason']}).")
    slot = _find_slot(db, company_id, section, d, data.get("slot") or "day", int(data.get("period_no") or 0),
                      data.get("subject_id"))
    may_mark = can_mark(db, company_id, user, perms, section, slot)
    can_correct = user.get("role") == "admin" or "attendance.correct" in perms
    locked = is_locked(db, company_id, d, today)
    correcting = False
    if locked or not may_mark:
        if not can_correct:
            if not may_mark:
                raise Forbidden("You can't mark this register.")
            raise Forbidden(f"This register is locked ({get_settings(db, company_id)['lock_after_days']} days after the date). "
                            "Ask the office to correct it.")
        correcting = locked
    reason = (data.get("reason") or "").strip()
    if correcting and not reason:
        raise BadRequest("Say why this locked register is being changed.")
    codes = _status_by_code(db, company_id)
    roster = {s.id: s for s, _ in _roster(db, company_id, section, slot["subject_id"] if slot["slot"] == "period" else None)}
    reg = _find_register(db, company_id, section.id, d, slot["slot"], slot["period_no"], slot["subject_id"])
    existing = {m.student_id: m for m in reg.marks} if reg else {}
    allowed = set(roster) | set(existing)
    incoming = data.get("marks") or []
    if not incoming:
        raise BadRequest("Mark at least one learner.")
    for m in incoming:
        if m["student_id"] not in allowed:
            raise BadRequest("Some learners are not in this register.")
        st = codes.get(m["code"])
        if st is None or (not st.active and (m["student_id"] not in existing or existing[m["student_id"]].code != m["code"])):
            raise BadRequest(f"Unknown status {m['code']}.")
    missing = set(roster) - {m["student_id"] for m in incoming} - set(existing)
    if missing and reg is None:
        raise BadRequest("Mark every learner before saving.")
    ctx = context.current()
    now = _now()
    if reg is None:
        reg = AttendanceRegister(company_id=company_id, section_id=section.id, date=d, slot=slot["slot"],
                                 period_no=slot["period_no"], subject_id=slot["subject_id"] if slot["slot"] == "period" else None,
                                 subject_key=(slot["subject_id"] or 0) if slot["slot"] == "period" else 0,
                                 timetable_entry_id=slot.get("entry_id"), marked_by_id=ctx.employee_id,
                                 marked_by_name=ctx.actor_name, marked_at=now)
        db.add(reg)
        db.flush()
    newly_absent, newly_late = [], []
    changed = 0
    for m in incoming:
        st = codes[m["code"]]
        late = int(m.get("late_minutes") or 0) if st.is_late else None
        remarks = (m.get("remarks") or "").strip()[:300] or None
        old = existing.get(m["student_id"])
        if old is None:
            src = "leave" if (m.get("source") == "leave" and st.is_leave) else ("correction" if correcting else "manual")
            mark = AttendanceMark(company_id=company_id, register_id=reg.id, student_id=m["student_id"], date=d,
                                  section_id=section.id, subject_id=reg.subject_id, status_id=st.id, code=st.code,
                                  late_minutes=late or None, remarks=remarks, source=src,
                                  leave_request_id=m.get("leave_request_id"))
            db.add(mark)
            reg.marks.append(mark)
            changed += 1
            if st.is_absent:
                newly_absent.append(m["student_id"])
            elif st.is_late:
                newly_late.append(m["student_id"])
            if correcting:
                db.add(AttendanceCorrection(company_id=company_id, register_id=reg.id, student_id=m["student_id"], date=d,
                                            old_code=None, new_code=st.code, reason=reason, by_employee_id=ctx.employee_id,
                                            by_name=ctx.actor_name))
            continue
        if old.code == st.code and (old.late_minutes or None) == (late or None) and (old.remarks or None) == remarks:
            continue
        if old.code != st.code:
            if correcting:
                db.add(AttendanceCorrection(company_id=company_id, register_id=reg.id, student_id=m["student_id"], date=d,
                                            old_code=old.code, new_code=st.code, reason=reason,
                                            by_employee_id=ctx.employee_id, by_name=ctx.actor_name))
            if st.is_absent:
                newly_absent.append(m["student_id"])
            elif st.is_late:
                newly_late.append(m["student_id"])
            old.source = "correction" if correcting else "manual"
        old.status_id, old.code, old.late_minutes, old.remarks = st.id, st.code, late or None, remarks
        changed += 1
    db.flush()
    _recount(db, reg)
    reg.updated_by_name = ctx.actor_name
    if correcting:
        audit.event(db, "student_attendance.corrected", "student_attendance_registers", reg.id,
                    {"date": d.isoformat(), "section": academics.section_label(section), "changes": changed, "reason": reason},
                    entity_uuid=str(reg.uuid), entity_label=f"{academics.section_label(section)} {d.isoformat()}")
    db.flush()
    cfg = get_settings(db, company_id)
    if cfg["absence_alerts"] and cfg["absence_alert_when"] == "on_mark" and d == today:
        if newly_absent:
            send_alerts(db, company_id, d, newly_absent, "absence")
        if newly_late and cfg["alert_on_late"]:
            send_alerts(db, company_id, d, newly_late, "late")
    return reg


def _recount(db: Session, reg: AttendanceRegister) -> None:
    total = present = absent = 0
    for m in reg.marks:
        st = m.status or db.query(AttendanceStatus).filter(AttendanceStatus.id == m.status_id).first()
        total += 1
        if st.is_absent:
            absent += 1
        elif float(st.counts_as) > 0:
            present += 1
    reg.total, reg.present, reg.absent = total, present, absent


def mark_all_present(db: Session, company_id: int, user: dict, perms: list[str], section_id: int, d: date,
                     slot: str = "day", period_no: int = 0, subject_id: int | None = None) -> AttendanceRegister:
    """Quick action: everyone present except approved leave."""
    info = load_register(db, company_id, user, perms, section_id, d, slot, period_no, subject_id)
    p = present_code(db, company_id)
    marks = [{"student_id": r["student_id"], "code": r["code"] if r["source"] == "leave" else p,
              "source": r["source"], "leave_request_id": None} for r in info["students"]]
    return save_register(db, company_id, user, perms, {"section_id": section_id, "date": d, "slot": slot,
                                                       "period_no": period_no, "subject_id": subject_id, "marks": marks})


# ── Percentages ──────────────────────────────────────────────────────

def compute_percentage(marks: list[tuple[float, bool]]) -> dict:
    """[(counts_as, in_total)] -> {present, total, pct}. Present is the sum of what each mark
    counts as; marks that do not count in the total are left out."""
    total = sum(1 for _, inc in marks if inc)
    present = sum(float(c) for c, inc in marks if inc)
    return {"present": round(present, 2), "total": total, "pct": round(present * 100 / total, 1) if total else None}


def percentages(db: Session, company_id: int, student_ids: list[int], start: date, end: date,
                subject_id: int | None = None, by_subject: bool = False) -> dict:
    """student_id (or (student_id, subject_id) when by_subject) -> {present, total, pct, absent, late, leave}."""
    if not student_ids:
        return {}
    q = (db.query(AttendanceMark.student_id, AttendanceMark.subject_id, AttendanceStatus.counts_as,
                  AttendanceStatus.in_total, AttendanceStatus.is_absent, AttendanceStatus.is_late, AttendanceStatus.is_leave)
         .join(AttendanceStatus, AttendanceStatus.id == AttendanceMark.status_id)
         .filter(AttendanceMark.company_id == company_id, AttendanceMark.student_id.in_(student_ids),
                 AttendanceMark.date >= start, AttendanceMark.date <= end))
    if subject_id:
        q = q.filter(AttendanceMark.subject_id == subject_id)
    acc: dict = defaultdict(lambda: {"marks": [], "absent": 0, "late": 0, "leave": 0})
    for sid, subj, counts, inc, is_abs, is_late, is_leave in q.all():
        key = (sid, subj) if by_subject else sid
        a = acc[key]
        a["marks"].append((float(counts), inc))
        a["absent"] += 1 if is_abs else 0
        a["late"] += 1 if is_late else 0
        a["leave"] += 1 if is_leave else 0
    out = {}
    for key, a in acc.items():
        out[key] = {**compute_percentage(a["marks"]), "absent": a["absent"], "late": a["late"], "leave": a["leave"]}
    return out


def range_for(db: Session, company_id: int, term_id: int | None = None, date_from: date | None = None,
              date_to: date | None = None) -> tuple[date, date, str]:
    """A date range: given dates, a term, or the current session up to today."""
    today = today_for_company_id(db, company_id)
    if date_from and date_to:
        if date_to < date_from:
            raise BadRequest("The end date must be after the start date.")
        return date_from, date_to, f"{date_from:%d %b %Y} to {date_to:%d %b %Y}"
    if term_id:
        t = db.query(Term).filter(Term.company_id == company_id, Term.id == term_id).first()
        if t is None:
            raise NotFound("Term")
        return t.start_date, min(t.end_date, today), t.name
    sess = academics.current_session(db, company_id)
    if sess is None:
        return today.replace(day=1), today, "This month"
    return sess.start_date, min(sess.end_date, today), sess.name


# ── Overviews and reports ────────────────────────────────────────────

def current_sections(db: Session, company_id: int, section_ids: set[int] | None = None) -> list[Section]:
    sess = academics.current_session(db, company_id)
    if sess is None:
        return []
    q = (db.query(Section).options(joinedload(Section.program), joinedload(Section.class_teacher))
         .join(Program, Program.id == Section.program_id)
         .filter(Section.company_id == company_id, Section.session_id == sess.id, Section.active.is_(True)))
    if section_ids is not None:
        q = q.filter(Section.id.in_(section_ids or {0}))
    return q.order_by(Program.sort_order, Program.name, Section.name).all()


def day_overview(db: Session, company_id: int, d: date, section_ids: set[int] | None = None) -> dict:
    """Per section: marked or not, present %, absentees; and totals."""
    sections = current_sections(db, company_id, section_ids)
    regs = db.query(AttendanceRegister).filter(AttendanceRegister.company_id == company_id,
                                               AttendanceRegister.date == d,
                                               AttendanceRegister.section_id.in_([s.id for s in sections] or [0])).all()
    by_section: dict[int, list] = defaultdict(list)
    for r in regs:
        by_section[r.section_id].append(r)
    marks = (db.query(AttendanceMark.section_id, AttendanceMark.student_id, AttendanceStatus.code,
                      AttendanceStatus.counts_as, AttendanceStatus.in_total, AttendanceStatus.is_absent,
                      AttendanceStatus.is_late, AttendanceStatus.is_leave)
             .join(AttendanceStatus, AttendanceStatus.id == AttendanceMark.status_id)
             .filter(AttendanceMark.company_id == company_id, AttendanceMark.date == d).all())
    per: dict[int, dict] = defaultdict(lambda: {"marks": [], "absent": set(), "late": set(), "leave": set()})
    for sec_id, sid, code, counts, inc, is_abs, is_late, is_leave in marks:
        p = per[sec_id]
        p["marks"].append((float(counts), inc))
        if is_abs:
            p["absent"].add(sid)
        if is_late:
            p["late"].add(sid)
        if is_leave:
            p["leave"].add(sid)
    names = {s.id: s.full_name for s in db.query(Student).filter(
        Student.id.in_({sid for p in per.values() for sid in p["absent"]} or {0}))}
    strength = dict(db.query(students.Enrollment.section_id, func.count(students.Enrollment.id)).filter(
        students.Enrollment.company_id == company_id, students.Enrollment.status == "enrolled",
        students.Enrollment.section_id.in_([s.id for s in sections] or [0])).group_by(students.Enrollment.section_id).all())
    cal_cache: dict[int | None, tuple[bool, str | None]] = {}
    m = mode(db, company_id)
    rows = []
    for s in sections:
        if s.program_id not in cal_cache:
            cal_cache[s.program_id] = calendar_map(db, company_id, d, d, s.program_id)[d]
        working, why = cal_cache[s.program_id]
        expected = len(slots_for(db, company_id, s, d)) if working else 0
        done = len(by_section.get(s.id, []))
        p = per.get(s.id)
        pct = compute_percentage(p["marks"])["pct"] if p else None
        rows.append({"section_id": s.id, "section_uuid": str(s.uuid), "label": academics.section_label(s),
                     "class_teacher": _name(s.class_teacher), "class_teacher_id": s.class_teacher_id,
                     "strength": strength.get(s.id, 0), "working": working, "reason": why,
                     "expected": expected, "marked": done, "complete": working and expected > 0 and done >= expected,
                     "present_pct": pct, "absent": len(p["absent"]) if p else 0, "late": len(p["late"]) if p else 0,
                     "leave": len(p["leave"]) if p else 0,
                     "absentees": sorted(names.get(i, "") for i in (p["absent"] if p else set()))})
    working_rows = [r for r in rows if r["working"] and r["expected"]]
    all_marks = [x for p in per.values() for x in p["marks"]]
    return {"date": d.isoformat(), "mode": m, "sections": rows,
            "totals": {"sections": len(working_rows), "marked": sum(1 for r in working_rows if r["complete"]),
                       "partly": sum(1 for r in working_rows if 0 < r["marked"] < r["expected"]),
                       "present_pct": compute_percentage(all_marks)["pct"],
                       "absent": sum(r["absent"] for r in rows), "late": sum(r["late"] for r in rows),
                       "leave": sum(r["leave"] for r in rows), "strength": sum(r["strength"] for r in working_rows)}}


def my_registers(db: Session, company_id: int, user: dict, perms: list[str], d: date) -> list[dict]:
    """The registers the signed-in teacher should mark on a date."""
    emp = user.get("employee_id")
    if not emp:
        return []
    out = []
    if mode(db, company_id) == "daily":
        sess = academics.current_session(db, company_id)
        secs = [s for s in current_sections(db, company_id) if s.class_teacher_id == emp] if sess else []
        for s in secs:
            if not day_info(db, company_id, d, s.program_id)["working"]:
                continue
            for sl in slots_for(db, company_id, s, d):
                reg = _find_register(db, company_id, s.id, d, sl["slot"], 0, None)
                out.append({"section_id": s.id, "label": academics.section_label(s), "slot": sl["slot"], "period_no": 0,
                            "subject_id": None, "subject_name": None, "slot_label": sl["label"], "start": None,
                            "end": None, "marked": reg is not None, "present": reg.present if reg else None,
                            "total": reg.total if reg else None, "is_substitution": False})
        return out
    try:
        from modules.timetable import service as timetable
    except ImportError:
        return []
    for c in timetable.teacher_day(db, company_id, emp, d):
        s = _section(db, company_id, c["section_id"])
        if not day_info(db, company_id, d, s.program_id)["working"]:
            continue
        reg = _find_register(db, company_id, s.id, d, "period", c["period_no"], c["subject_id"])
        out.append({"section_id": s.id, "label": c["section_label"], "slot": "period", "period_no": c["period_no"],
                    "subject_id": c["subject_id"], "subject_name": c["subject_name"],
                    "slot_label": f"P{c['period_no']} · {c['subject_name'] or ''}", "start": c.get("start"),
                    "end": c.get("end"), "marked": reg is not None, "present": reg.present if reg else None,
                    "total": reg.total if reg else None, "is_substitution": c.get("is_substitution", False)})
    return out


def monthly_register(db: Session, company_id: int, section_id: int, year: int, month: int) -> dict:
    """Learner x day grid for a month with totals."""
    section = _section(db, company_id, section_id)
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    cal = calendar_map(db, company_id, start, end, section.program_id)
    roster = students.section_students(db, company_id, section.id)
    ids = [s.id for s, _ in roster]
    marks = (db.query(AttendanceMark.student_id, AttendanceMark.date, AttendanceMark.code, AttendanceStatus.counts_as,
                      AttendanceStatus.in_total, AttendanceMark.subject_id)
             .join(AttendanceStatus, AttendanceStatus.id == AttendanceMark.status_id)
             .filter(AttendanceMark.company_id == company_id, AttendanceMark.section_id == section.id,
                     AttendanceMark.date >= start, AttendanceMark.date <= end).all())
    grid: dict[int, dict[date, list]] = defaultdict(lambda: defaultdict(list))
    for sid, d, code, counts, inc, _ in marks:
        grid[sid][d].append((code, float(counts), inc))
    m = mode(db, company_id)
    days = [{"date": d.isoformat(), "day": d.day, "weekday": d.strftime("%a"), "working": cal[d][0], "reason": cal[d][1]}
            for d in sorted(cal)]
    rows = []
    for s, e in roster:
        cells = {}
        all_marks = []
        for d in sorted(cal):
            items = grid[s.id].get(d, [])
            if not items:
                continue
            all_marks += [(c, inc) for _, c, inc in items]
            if m == "period":
                tot = sum(1 for _, _, inc in items if inc)
                pres = sum(c for _, c, inc in items if inc)
                cells[d.isoformat()] = f"{pres:g}/{tot}"
            else:
                cells[d.isoformat()] = "/".join(code for code, _, _ in items)
        rows.append({"student_id": s.id, "name": s.full_name, "roll_no": e.roll_no, "admission_no": s.admission_no,
                     "cells": cells, **compute_percentage(all_marks)})
    return {"section": {"id": section.id, "label": academics.section_label(section)}, "month": f"{year:04d}-{month:02d}",
            "mode": m, "days": days, "rows": rows,
            "working_days": sum(1 for d in days if d["working"]),
            "legend": [status_out(st) for st in list_statuses(db, company_id, active_only=False)]}


def monthly_register_csv(data: dict) -> str:
    from core import csv_safe
    buf = io.StringIO()
    w = csv_safe.writer(buf)
    w.writerow(["Roll no", "Admission no", "Name"] + [str(d["day"]) for d in data["days"]] + ["Present", "Total", "%"])
    for r in data["rows"]:
        w.writerow([r["roll_no"] or "", r["admission_no"], r["name"]] +
                   [r["cells"].get(d["date"], "" if d["working"] else "H") for d in data["days"]] +
                   [r["present"], r["total"], "" if r["pct"] is None else r["pct"]])
    return buf.getvalue()


def learner_percentages(db: Session, company_id: int, section_id: int, start: date, end: date) -> list[dict]:
    section = _section(db, company_id, section_id)
    roster = students.section_students(db, company_id, section.id)
    pct = percentages(db, company_id, [s.id for s, _ in roster], start, end)
    threshold = get_settings(db, company_id)["low_attendance_threshold"]
    out = []
    for s, e in roster:
        p = pct.get(s.id, {"present": 0, "total": 0, "pct": None, "absent": 0, "late": 0, "leave": 0})
        out.append({"student_id": s.id, "uuid": str(s.uuid), "name": s.full_name, "roll_no": e.roll_no, **p,
                    "below": p["pct"] is not None and p["pct"] < threshold})
    return out


def subject_percentages(db: Session, company_id: int, section_id: int, start: date, end: date) -> dict:
    section = _section(db, company_id, section_id)
    roster = students.section_students(db, company_id, section.id)
    pct = percentages(db, company_id, [s.id for s, _ in roster], start, end, by_subject=True)
    subj_ids = sorted({k[1] for k in pct if k[1]})
    names = {i: n for i, n in db.query(Subject.id, Subject.name).filter(Subject.id.in_(subj_ids or [0]))}
    subjects = [{"id": i, "name": names.get(i, "")} for i in subj_ids]
    subjects.sort(key=lambda x: x["name"])
    rows = []
    for s, e in roster:
        cells = {str(i): pct.get((s.id, i), {}).get("pct") for i in subj_ids}
        rows.append({"student_id": s.id, "name": s.full_name, "roll_no": e.roll_no, "cells": cells})
    return {"subjects": subjects, "rows": rows}


def defaulters(db: Session, company_id: int, start: date, end: date, threshold: float | None = None,
               section_ids: set[int] | None = None) -> list[dict]:
    threshold = threshold if threshold is not None else get_settings(db, company_id)["low_attendance_threshold"]
    sections = current_sections(db, company_id, section_ids)
    out = []
    for sec in sections:
        roster = students.section_students(db, company_id, sec.id)
        pct = percentages(db, company_id, [s.id for s, _ in roster], start, end)
        for s, e in roster:
            p = pct.get(s.id)
            if p and p["pct"] is not None and p["pct"] < threshold:
                out.append({"student_id": s.id, "uuid": str(s.uuid), "name": s.full_name, "roll_no": e.roll_no,
                            "section_id": sec.id, "section_label": academics.section_label(sec), **p})
    out.sort(key=lambda r: r["pct"])
    return out


# ── Alerts ───────────────────────────────────────────────────────────

def send_alerts(db: Session, company_id: int, d: date, student_ids: list[int], kind: str = "absence") -> int:
    """Tell guardians (and the learner) once per learner per day."""
    from modules.notifications.service import notify_portal
    sent = 0
    already = {sid for (sid,) in db.query(AttendanceAlert.student_id).filter(
        AttendanceAlert.company_id == company_id, AttendanceAlert.kind == kind, AttendanceAlert.ref == d.isoformat(),
        AttendanceAlert.student_id.in_(student_ids or [0]))}
    for s in db.query(Student).filter(Student.company_id == company_id, Student.id.in_(set(student_ids) - already or {0})):
        accounts = students.portal_account_ids_for_students(db, company_id, [s.id])
        title = (f"{s.first_name} was marked absent on {d:%d %b}" if kind == "absence"
                 else f"{s.first_name} was late on {d:%d %b}")
        body = (f"{s.full_name} was marked absent today. If this is planned, please send a leave request."
                if kind == "absence" else f"{s.full_name} reached late today.")
        notify_portal(db, company_id, accounts, kind=f"attendance.{kind}", title=title, body=body,
                      link="/dashboard/my/attendance", data={"student_id": s.id, "date": d.isoformat()})
        db.add(AttendanceAlert(company_id=company_id, kind=kind, student_id=s.id, ref=d.isoformat(), recipients=len(accounts)))
        sent += 1
    db.flush()
    return sent


def notify_low_attendance(db: Session, company_id: int, rows: list[dict], ref: str, threshold: float) -> int:
    """Alert guardians of learners below the threshold (once per ref) and tell class teachers."""
    from modules.notifications.service import notify, notify_portal
    already = {sid for (sid,) in db.query(AttendanceAlert.student_id).filter(
        AttendanceAlert.company_id == company_id, AttendanceAlert.kind == "low", AttendanceAlert.ref == ref)}
    per_teacher: dict[int, list[str]] = defaultdict(list)
    sent = 0
    cfg = get_settings(db, company_id)
    for r in rows:
        if r["student_id"] in already:
            continue
        accounts = students.portal_account_ids_for_students(db, company_id, [r["student_id"]])
        notify_portal(db, company_id, accounts, kind="attendance.low", title=f"Attendance is {r['pct']:g}%",
                      body=f"{r['name']}'s attendance is {r['pct']:g}%, below the required {threshold:g}%.",
                      link="/dashboard/my/attendance", data={"student_id": r["student_id"]})
        db.add(AttendanceAlert(company_id=company_id, kind="low", student_id=r["student_id"], ref=ref, recipients=len(accounts)))
        sent += 1
        sec = db.query(Section).filter(Section.id == r["section_id"]).first()
        if sec and sec.class_teacher_id:
            per_teacher[sec.class_teacher_id].append(f"{r['name']} ({r['pct']:g}%)")
    if cfg["notify_class_teacher_low"]:
        for tid, names in per_teacher.items():
            notify(db, company_id, [tid], kind="attendance.low", title=f"{len(names)} learner(s) below {threshold:g}% attendance",
                   body=", ".join(names[:10]) + (" and more" if len(names) > 10 else ""),
                   link="/dashboard/student-attendance/reports?tab=defaulters")
    db.flush()
    return sent


def run_due_alerts(db: Session, company_id: int, now_local: datetime) -> int:
    """Periodic: absence alerts after the set time, and the weekly low-attendance alert. Idempotent."""
    cfg = get_settings(db, company_id)
    today = now_local.date()
    done = 0
    if cfg["absence_alerts"] and cfg["absence_alert_when"] == "after_time":
        if now_local.time() >= _parse_hm(cfg["absence_alert_time"]):
            ids = [sid for (sid,) in db.query(AttendanceMark.student_id)
                   .join(AttendanceStatus, AttendanceStatus.id == AttendanceMark.status_id)
                   .filter(AttendanceMark.company_id == company_id, AttendanceMark.date == today,
                           AttendanceStatus.is_absent.is_(True)).distinct()]
            if ids:
                done += send_alerts(db, company_id, today, ids, "absence")
            if cfg["alert_on_late"]:
                late = [sid for (sid,) in db.query(AttendanceMark.student_id)
                        .join(AttendanceStatus, AttendanceStatus.id == AttendanceMark.status_id)
                        .filter(AttendanceMark.company_id == company_id, AttendanceMark.date == today,
                                AttendanceStatus.is_late.is_(True)).distinct()]
                if late:
                    done += send_alerts(db, company_id, today, late, "late")
    if cfg["low_attendance_alerts"] and today.isoweekday() == cfg["low_alert_weekday"] and now_local.hour >= 9:
        iso = today.isocalendar()
        ref = f"{iso[0]}-W{iso[1]:02d}"
        if not db.query(AttendanceAlert.id).filter(AttendanceAlert.company_id == company_id, AttendanceAlert.kind == "low",
                                                   AttendanceAlert.ref == ref).first():
            start, end, _ = range_for(db, company_id)
            rows = [r for r in defaulters(db, company_id, start, end) if r["total"] >= cfg["low_alert_min_units"]]
            done += notify_low_attendance(db, company_id, rows, ref, cfg["low_attendance_threshold"])
    return done


def alerts_job(db: Session) -> int:
    from core.timeutil import now_for
    from modules.company.models import Company
    done = 0
    for company in db.query(Company).filter(Company.active.is_(True)).all():
        db.info["company_id"] = company.id
        try:
            with db.begin_nested():
                if db.query(AttendanceMark.id).filter(AttendanceMark.company_id == company.id).first() is None:
                    continue
                done += run_due_alerts(db, company.id, now_for(company))
        except Exception as exc:  # one institution never blocks the others
            import logging
            logging.getLogger("hrms").warning("student attendance alerts failed for company %s: %s", company.id, exc)
    db.info.pop("company_id", None)
    return done


# ── Leave requests ───────────────────────────────────────────────────

def leave_out(r: StudentLeaveRequest, db: Session | None = None, links: bool = False) -> dict:
    s, sec = r.student, r.section
    attachments = r.attachments or []
    if links and db is not None and attachments:
        from modules.documents import storage
        attachments = storage.with_links(db, r.company_id, attachments)
    return {"uuid": str(r.uuid), "id": r.id, "student_id": r.student_id, "student_uuid": str(s.uuid) if s else None,
            "student_name": s.full_name if s else None, "section_id": r.section_id,
            "section_label": academics.section_label(sec) if sec else None,
            "from_date": r.from_date.isoformat(), "to_date": r.to_date.isoformat(), "days": r.days,
            "status_code": r.status_code, "reason": r.reason, "attachments": attachments, "status": r.status,
            "requested_by_kind": r.requested_by_kind, "requested_by_name": r.requested_by_name,
            "decided_by_name": r.decided_by_name, "decided_at": iso_utc(r.decided_at), "decision_note": r.decision_note,
            "created_at": iso_utc(r.created_at), "workflow_instance_id": r.workflow_instance_id}


def leave_query(db: Session, company_id: int):
    return (db.query(StudentLeaveRequest)
            .options(joinedload(StudentLeaveRequest.student), joinedload(StudentLeaveRequest.section).joinedload(Section.program))
            .filter(StudentLeaveRequest.company_id == company_id))


def get_leave(db: Session, company_id: int, uuid) -> StudentLeaveRequest:
    r = leave_query(db, company_id).filter(StudentLeaveRequest.uuid == uuid).first()
    if r is None:
        raise NotFound("Leave request")
    return r


def create_leave(db: Session, company_id: int, user: dict, student_id: int, from_date: date, to_date: date,
                 reason: str, status_code: str | None = None, attachments: list | None = None,
                 by_staff: bool = False) -> StudentLeaveRequest:
    cfg = get_settings(db, company_id)
    today = today_for_company_id(db, company_id)
    s = db.query(Student).filter(Student.company_id == company_id, Student.id == student_id).first()
    if s is None:
        raise NotFound("Student")
    if to_date < from_date:
        raise BadRequest("The last day must be on or after the first day.")
    span = (to_date - from_date).days + 1
    if span > cfg["leave_max_days"]:
        raise BadRequest(f"Leave can be at most {cfg['leave_max_days']} days at a time. Contact the office for longer leave.")
    if not by_staff and from_date < today - timedelta(days=cfg["leave_past_days"]):
        raise BadRequest(f"Leave can be asked for at most {cfg['leave_past_days']} days back.")
    reason = (reason or "").strip()
    if not reason:
        raise BadRequest("Add the reason for leave.")
    codes = _status_by_code(db, company_id)
    code = status_code or next((st.code for st in list_statuses(db, company_id) if st.is_leave), None)
    if not code or code not in codes or not codes[code].is_leave or not codes[code].active:
        raise BadRequest("Pick a kind of leave.")
    overlap = db.query(StudentLeaveRequest.id).filter(
        StudentLeaveRequest.company_id == company_id, StudentLeaveRequest.student_id == student_id,
        StudentLeaveRequest.status.in_(("pending", "approved")), StudentLeaveRequest.from_date <= to_date,
        StudentLeaveRequest.to_date >= from_date).first()
    if overlap:
        raise BadRequest("There is already a leave request for some of these days.")
    enr = students.current_enrollment(db, company_id, student_id)
    section = enr.section if enr else None
    days = working_days_between(db, company_id, from_date, to_date, section.program_id if section else None)
    if days == 0:
        raise BadRequest("All of these days are holidays; no leave is needed.")
    need_file = cfg["leave_attachment_after_days"]
    from modules.documents import storage
    files = storage.attachments_from(db, company_id, attachments or [])
    if need_file and days > need_file and not files and not by_staff:
        raise BadRequest(f"Attach a note or medical certificate for leave longer than {need_file} days.")
    ctx = context.current()
    kind = "staff" if by_staff else ("student" if user.get("role") == "student" else "parent")
    row = StudentLeaveRequest(company_id=company_id, student_id=student_id, section_id=section.id if section else None,
                              from_date=from_date, to_date=to_date, days=days, status_code=code, reason=reason[:2000],
                              attachments=files, status="pending", requested_by_kind=kind,
                              requested_by_portal_id=user.get("portal_id"), requested_by_name=ctx.actor_name)
    db.add(row)
    db.flush()
    db.refresh(row)
    if by_staff:
        row.status = "approved"
        row.decided_by_name = ctx.actor_name
        row.decided_at = _now()
        row.decision_note = "Recorded by the office"
        apply_leave_to_registers(db, company_id, row)
        audit.event(db, "student_leave.recorded", "student_leave_requests", row.id,
                    {"from": from_date.isoformat(), "to": to_date.isoformat(), "days": days},
                    entity_uuid=str(row.uuid), entity_label=s.full_name)
        _tell_family(db, company_id, row, "approved")
        return row
    from core.workflow import service as workflow
    payload = {"leave_id": row.id, "student_id": s.id, "student_name": s.full_name,
               "section_label": academics.section_label(section) if section else "",
               "class_teacher_id": section.class_teacher_id if section else None,
               "from_date": from_date.isoformat(), "to_date": to_date.isoformat(), "days": days, "reason": reason[:500],
               "status_code": code, "status_name": codes[code].name, "requested_by": ctx.actor_name}
    title = f"Leave for {s.full_name}: {from_date:%d %b}" + (f" to {to_date:%d %b}" if to_date != from_date else "")
    instance = workflow.submit(db, company_id, REQUEST_TYPE, payload, title, subject_type="student_leave_requests",
                               subject_id=row.id)
    row.workflow_instance_id = instance.id
    db.flush()
    return row


def _leave_from_instance(db: Session, instance) -> StudentLeaveRequest | None:
    lid = (instance.payload or {}).get("leave_id")
    return db.query(StudentLeaveRequest).filter(StudentLeaveRequest.company_id == instance.company_id,
                                                StudentLeaveRequest.id == lid).first() if lid else None


def on_leave_approved(db: Session, instance) -> None:
    row = _leave_from_instance(db, instance)
    if row is None or row.status != "pending":
        return
    row.status = "approved"
    row.decided_at = _now()
    row.decided_by_name = context.current().actor_name if instance.result_note != "No approval needed" else "Automatic"
    row.decision_note = instance.result_note
    db.flush()
    apply_leave_to_registers(db, row.company_id, row)
    _tell_family(db, row.company_id, row, "approved")


def on_leave_rejected(db: Session, instance) -> None:
    row = _leave_from_instance(db, instance)
    if row is None or row.status != "pending":
        return
    row.status = "rejected"
    row.decided_at = _now()
    row.decided_by_name = context.current().actor_name
    row.decision_note = instance.result_note
    db.flush()
    _tell_family(db, row.company_id, row, "rejected")


def on_leave_cancelled(db: Session, instance) -> None:
    row = _leave_from_instance(db, instance)
    if row is None or row.status != "pending":
        return
    row.status = "cancelled"
    row.decided_at = _now()
    db.flush()


def _tell_family(db: Session, company_id: int, row: StudentLeaveRequest, outcome: str) -> None:
    from modules.notifications.service import notify_portal
    s = row.student or db.query(Student).filter(Student.id == row.student_id).first()
    accounts = students.portal_account_ids_for_students(db, company_id, [row.student_id])
    span = f"{row.from_date:%d %b}" + (f" to {row.to_date:%d %b}" if row.to_date != row.from_date else "")
    title = f"Leave {outcome} for {s.first_name}"
    body = f"Leave for {s.full_name} ({span}) was {outcome}." + (f" Note: {row.decision_note}" if outcome == "rejected" and row.decision_note else "")
    notify_portal(db, company_id, accounts, kind=f"attendance.leave_{outcome}", title=title, body=body,
                  link="/dashboard/my/leave", actor_name=row.decided_by_name)


def apply_leave_to_registers(db: Session, company_id: int, row: StudentLeaveRequest) -> int:
    """Turn absent marks already given for approved leave days into the leave status."""
    codes = _status_by_code(db, company_id)
    st = codes.get(row.status_code)
    if st is None:
        return 0
    marks = (db.query(AttendanceMark).join(AttendanceStatus, AttendanceStatus.id == AttendanceMark.status_id)
             .filter(AttendanceMark.company_id == company_id, AttendanceMark.student_id == row.student_id,
                     AttendanceMark.date >= row.from_date, AttendanceMark.date <= row.to_date,
                     AttendanceStatus.is_absent.is_(True)).all())
    regs = set()
    for m in marks:
        m.status_id, m.code, m.source, m.leave_request_id = st.id, st.code, "leave", row.id
        regs.add(m.register_id)
    db.flush()
    for reg in db.query(AttendanceRegister).filter(AttendanceRegister.id.in_(regs or {0})):
        db.refresh(reg)
        _recount(db, reg)
    db.flush()
    return len(marks)


def revoke_leave(db: Session, company_id: int, row: StudentLeaveRequest, reason: str | None) -> None:
    """Office cancels approved leave: leave marks go back to absent."""
    if row.status != "approved":
        raise BadRequest("Only approved leave can be cancelled here.")
    a = _status_by_code(db, company_id).get(absent_code(db, company_id))
    regs = set()
    for m in db.query(AttendanceMark).filter(AttendanceMark.company_id == company_id,
                                             AttendanceMark.leave_request_id == row.id, AttendanceMark.source == "leave"):
        if a is not None:
            m.status_id, m.code, m.source = a.id, a.code, "manual"
            regs.add(m.register_id)
    row.status = "cancelled"
    row.decision_note = (reason or "").strip() or "Cancelled by the office"
    row.decided_by_name = context.current().actor_name
    row.decided_at = _now()
    db.flush()
    for reg in db.query(AttendanceRegister).filter(AttendanceRegister.id.in_(regs or {0})):
        db.refresh(reg)
        _recount(db, reg)
    audit.event(db, "student_leave.cancelled", "student_leave_requests", row.id, {"reason": row.decision_note},
                entity_uuid=str(row.uuid))
    db.flush()


def withdraw_leave(db: Session, company_id: int, row: StudentLeaveRequest) -> None:
    """The family withdraws a pending request."""
    if row.status != "pending":
        raise BadRequest("Only pending requests can be withdrawn.")
    from core.workflow import service as workflow
    from core.workflow.models import WorkflowInstance
    inst = db.query(WorkflowInstance).filter(WorkflowInstance.company_id == company_id,
                                             WorkflowInstance.id == row.workflow_instance_id).first()
    if inst is not None and inst.status == "pending":
        audit.event(db, "workflow.cancelled", "workflow_instances", inst.id, {"reason": "Withdrawn by the family"},
                    entity_uuid=str(inst.uuid), entity_label=inst.title)
        workflow._finish(db, inst, "cancelled", "Withdrawn by the family")
    row.status = "cancelled"
    row.decided_at = _now()
    db.flush()


def decide_leave(db: Session, company_id: int, row: StudentLeaveRequest, decision: str, comment: str | None) -> None:
    """Approve or reject from the leave page: uses the signed-in person's open approval task."""
    from core.workflow import service as workflow
    from core.workflow.models import WorkflowTask
    if row.status != "pending":
        raise BadRequest("This request has already been decided.")
    tasks = db.query(WorkflowTask).filter(WorkflowTask.company_id == company_id,
                                          WorkflowTask.instance_id == row.workflow_instance_id,
                                          WorkflowTask.status == "pending").all()
    mine = next((t for t in tasks if workflow.is_my_task(t)), None)
    if mine is None:
        who = ", ".join(t.approver_name for t in tasks) or "someone else"
        raise Forbidden(f"This request is waiting for {who}.")
    workflow.decide(db, company_id, mine, decision, comment)


def pending_task_names(db: Session, company_id: int, instance_ids: list[int]) -> dict[int, list[dict]]:
    from core.workflow import service as workflow
    from core.workflow.models import WorkflowTask
    out: dict[int, list] = defaultdict(list)
    for t in db.query(WorkflowTask).filter(WorkflowTask.company_id == company_id,
                                           WorkflowTask.instance_id.in_(instance_ids or [0]),
                                           WorkflowTask.status == "pending"):
        out[t.instance_id].append({"name": t.approver_name, "mine": workflow.is_my_task(t)})
    return out


# ── Portal views ─────────────────────────────────────────────────────

def student_calendar(db: Session, company_id: int, student_id: int, year: int, month: int) -> dict:
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    enr = students.current_enrollment(db, company_id, student_id)
    program_id = enr.program_id if enr else None
    cal = calendar_map(db, company_id, start, end, program_id)
    today = today_for_company_id(db, company_id)
    marks = (db.query(AttendanceMark.date, AttendanceMark.code, AttendanceMark.subject_id, AttendanceMark.late_minutes,
                      AttendanceMark.remarks, AttendanceStatus.counts_as, AttendanceStatus.in_total,
                      AttendanceStatus.is_absent, AttendanceStatus.is_leave, AttendanceStatus.is_late, AttendanceStatus.name)
             .join(AttendanceStatus, AttendanceStatus.id == AttendanceMark.status_id)
             .filter(AttendanceMark.company_id == company_id, AttendanceMark.student_id == student_id,
                     AttendanceMark.date >= start, AttendanceMark.date <= end).all())
    by_day: dict[date, list] = defaultdict(list)
    for m in marks:
        by_day[m.date].append(m)
    days = []
    for d in sorted(cal):
        items = by_day.get(d, [])
        state = "none"
        value = None
        if items:
            r = compute_percentage([(float(m.counts_as), m.in_total) for m in items])
            value = r["pct"]
            if all(m.is_leave for m in items):
                state = "leave"
            elif value is not None and value >= 100:
                state = "late" if any(m.is_late for m in items) else "present"
            elif value == 0 or value is None:
                state = "absent" if any(m.is_absent for m in items) else "leave"
            else:
                state = "partial"
        elif not cal[d][0]:
            state = "holiday"
        elif d > today:
            state = "future"
        days.append({"date": d.isoformat(), "working": cal[d][0], "holiday": cal[d][1], "state": state, "value": value,
                     "codes": [m.code for m in items], "names": sorted({m.name for m in items}),
                     "late_minutes": max((m.late_minutes or 0 for m in items), default=0) or None,
                     "remarks": "; ".join(m.remarks for m in items if m.remarks) or None})
    pct = percentages(db, company_id, [student_id], start, end).get(student_id)
    return {"month": f"{year:04d}-{month:02d}", "days": days,
            "summary": pct or {"present": 0, "total": 0, "pct": None, "absent": 0, "late": 0, "leave": 0}}


def student_absences(db: Session, company_id: int, student_id: int, start: date, end: date) -> list[dict]:
    rows = (db.query(AttendanceMark, AttendanceStatus)
            .join(AttendanceStatus, AttendanceStatus.id == AttendanceMark.status_id)
            .filter(AttendanceMark.company_id == company_id, AttendanceMark.student_id == student_id,
                    AttendanceMark.date >= start, AttendanceMark.date <= end,
                    or_(AttendanceStatus.is_absent.is_(True), AttendanceStatus.is_leave.is_(True),
                        AttendanceStatus.is_late.is_(True), AttendanceStatus.counts_as < 1))
            .order_by(AttendanceMark.date.desc()).limit(200).all())
    subj = {i: n for i, n in db.query(Subject.id, Subject.name).filter(
        Subject.id.in_({m.subject_id for m, _ in rows if m.subject_id} or {0}))}
    reg_slots = {r.id: r.slot for r in db.query(AttendanceRegister).filter(
        AttendanceRegister.id.in_({m.register_id for m, _ in rows} or {0}))}
    return [{"date": m.date.isoformat(), "code": m.code, "name": st.name, "color": st.color,
             "kind": "absent" if st.is_absent else "leave" if st.is_leave else "late" if st.is_late else "partial",
             "subject": subj.get(m.subject_id), "slot": reg_slots.get(m.register_id),
             "late_minutes": m.late_minutes, "remarks": m.remarks} for m, st in rows]


def student_summary(db: Session, company_id: int, student_id: int) -> dict:
    today = today_for_company_id(db, company_id)
    month_start = today.replace(day=1)
    start, end, label = range_for(db, company_id)
    month = percentages(db, company_id, [student_id], month_start, today).get(student_id)
    session = percentages(db, company_id, [student_id], start, end).get(student_id)
    enr = students.current_enrollment(db, company_id, student_id)
    info = day_info(db, company_id, today, enr.program_id if enr else None)
    todays = (db.query(AttendanceMark.code, AttendanceStatus.name, AttendanceStatus.color, AttendanceStatus.is_absent,
                       AttendanceStatus.is_leave, AttendanceStatus.is_late, AttendanceMark.late_minutes)
              .join(AttendanceStatus, AttendanceStatus.id == AttendanceMark.status_id)
              .filter(AttendanceMark.company_id == company_id, AttendanceMark.student_id == student_id,
                      AttendanceMark.date == today).all())
    today_state = None
    if todays:
        if all(t.is_leave for t in todays):
            today_state = "leave"
        elif any(t.is_absent for t in todays):
            today_state = "absent" if all(t.is_absent for t in todays) else "partial"
        elif any(t.is_late for t in todays):
            today_state = "late"
        else:
            today_state = "present"
    return {
        "date": today.isoformat(), "threshold": get_settings(db, company_id)["low_attendance_threshold"],
        "month": month or {"present": 0, "total": 0, "pct": None, "absent": 0, "late": 0, "leave": 0},
        "session": session or {"present": 0, "total": 0, "pct": None, "absent": 0, "late": 0, "leave": 0},
        "session_label": label,
        "today": {"working": info["working"], "reason": info["reason"], "marked": bool(todays), "state": today_state,
                  "codes": [t.code for t in todays], "names": sorted({t.name for t in todays}),
                  "late_minutes": max((t.late_minutes or 0 for t in todays), default=0) or None},
    }
