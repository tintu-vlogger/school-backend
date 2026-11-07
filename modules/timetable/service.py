"""Timetable rules: bell schedules, building the weekly grid, clash checks, copying,
publishing, a simple generator, and substitutions.

Functions other modules use:
    section_day(db, company_id, section_id, day)    -> the section's periods on a date (with substitutions)
    teacher_day(db, company_id, employee_id, day)   -> a teacher's classes on a date (with substitutions)
    schedule_for(db, company_id, program_id, weekday) -> BellSchedule | None
"""
from __future__ import annotations

import uuid as uuid_mod
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.orm.attributes import flag_modified

from core import audit, context
from core.errors import BadRequest, Conflict, NotFound
from modules.academics import service as academics
from modules.academics.models import Program, Room, Section, SectionSubject, Subject
from modules.timetable.models import (BellPeriod, BellSchedule, Substitution, TimetableEntry, TimetablePublication,
                                      TimetableSettings)

WEEKDAYS = ["", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
# Room kinds several classes can use at once (grounds, online rooms).
SHARED_ROOMS = ("online", "sports")
SHORT_DAYS = ["", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

DEFAULTS = {
    "max_periods_per_day": 7,     # a teacher's classes in one day before a warning
    "max_weekly_load": 32,        # a teacher's classes in a week before a warning
    "max_consecutive": 4,         # classes in a row before a warning
    "notify_on_publish": True,    # tell teachers, parents and learners when a timetable is published
    "notify_substitute": True,    # tell a teacher when they are given a substitution
    "show_teacher_in_portal": True,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _name(e) -> str | None:
    return f"{e.first_name} {e.last_name}".strip() if e else None


def _hm(t: time | None) -> str | None:
    return t.strftime("%H:%M") if t else None


# ── Settings ─────────────────────────────────────────────────────────

def _settings_row(db: Session, company_id: int) -> TimetableSettings:
    row = db.query(TimetableSettings).filter(TimetableSettings.company_id == company_id).first()
    if row is None:
        row = TimetableSettings(company_id=company_id, config={})
        db.add(row)
        db.flush()
    return row


def get_settings(db: Session, company_id: int) -> dict:
    row = db.query(TimetableSettings).filter(TimetableSettings.company_id == company_id).first()
    return {**DEFAULTS, **((row.config if row else None) or {})}


def save_settings(db: Session, company_id: int, data: dict) -> dict:
    row = _settings_row(db, company_id)
    cfg = dict(row.config or {})
    for key, default in DEFAULTS.items():
        if key not in data or data[key] is None:
            continue
        val = data[key]
        if isinstance(default, bool):
            cfg[key] = bool(val)
        else:
            try:
                val = int(val)
            except (TypeError, ValueError):
                raise BadRequest("Limits must be whole numbers.")
            if val < 1 or val > 80:
                raise BadRequest("Limits must be between 1 and 80.")
            cfg[key] = val
    row.config = cfg
    flag_modified(row, "config")
    db.flush()
    return get_settings(db, company_id)


def week_days(db: Session, company_id: int) -> list[int]:
    from modules.company.models import Company
    company = db.query(Company).filter(Company.id == company_id).first()
    days = academics.profile(company).get("week_days") or [1, 2, 3, 4, 5, 6]
    return sorted({int(d) for d in days if 1 <= int(d) <= 7})


# ── Bell schedules ───────────────────────────────────────────────────

def list_schedules(db: Session, company_id: int, active_only: bool = True) -> list[BellSchedule]:
    q = db.query(BellSchedule).options(joinedload(BellSchedule.periods)).filter(BellSchedule.company_id == company_id)
    if active_only:
        q = q.filter(BellSchedule.active.is_(True))
    return q.order_by(BellSchedule.is_default.desc(), BellSchedule.name).all()


def schedule_for(db: Session, company_id: int, program_id: int | None, weekday: int,
                 schedules: list[BellSchedule] | None = None) -> BellSchedule | None:
    """The bell schedule a program follows on a weekday: one made for that program wins over a
    general one; among general ones the default wins."""
    rows = schedules if schedules is not None else list_schedules(db, company_id)
    on_day = [s for s in rows if weekday in (s.weekdays or [])]
    if program_id:
        for s in on_day:
            if program_id in (s.program_ids or []):
                return s
    general = [s for s in on_day if not s.program_ids]
    general.sort(key=lambda s: (not s.is_default, s.name))
    return general[0] if general else None


def period_out(p: BellPeriod) -> dict:
    return {"uuid": str(p.uuid), "no": p.no, "name": p.name, "kind": p.kind, "start": _hm(p.start_time),
            "end": _hm(p.end_time), "sort_order": p.sort_order}


def schedule_out(s: BellSchedule, program_names: dict[int, str] | None = None) -> dict:
    periods = [period_out(p) for p in s.periods]
    teaching = [p for p in periods if p["kind"] == "class"]
    return {
        "uuid": str(s.uuid), "id": s.id, "name": s.name, "weekdays": s.weekdays or [], "program_ids": s.program_ids or [],
        "programs": [program_names.get(i, "") for i in (s.program_ids or [])] if program_names else [],
        "is_default": s.is_default, "active": s.active, "periods": periods,
        "teaching_periods": len(teaching),
        "starts": periods[0]["start"] if periods else None, "ends": periods[-1]["end"] if periods else None,
    }


def _parse_time(v) -> time:
    if isinstance(v, time):
        return v
    try:
        h, m = str(v).strip().split(":")[:2]
        return time(int(h), int(m))
    except (ValueError, TypeError):
        raise BadRequest("Times must look like 08:30.")


def save_schedule(db: Session, company_id: int, data: dict, row: BellSchedule | None = None) -> BellSchedule:
    name = (data.get("name") if data.get("name") is not None else (row.name if row else "")).strip()
    if not name:
        raise BadRequest("Give the bell schedule a name.")
    weekdays = data.get("weekdays") if data.get("weekdays") is not None else (row.weekdays if row else [1, 2, 3, 4, 5])
    weekdays = sorted({int(d) for d in weekdays if 1 <= int(d) <= 7})
    if not weekdays:
        raise BadRequest("Pick at least one day for this bell schedule.")
    program_ids = data.get("program_ids") if data.get("program_ids") is not None else (row.program_ids if row else [])
    program_ids = sorted({int(p) for p in program_ids})
    if program_ids:
        found = {i for (i,) in db.query(Program.id).filter(Program.company_id == company_id, Program.id.in_(program_ids))}
        if found != set(program_ids):
            raise BadRequest("Some of the programs were not found.")
    if row is None:
        row = BellSchedule(company_id=company_id)
        db.add(row)
    row.name = name[:80]
    row.weekdays = weekdays
    row.program_ids = program_ids
    if data.get("active") is not None:
        row.active = bool(data["active"])
    if data.get("is_default") is not None:
        row.is_default = bool(data["is_default"])
    if data.get("periods") is not None:
        periods = data["periods"]
        if not periods:
            raise BadRequest("Add the periods of the day.")
        if len(periods) > 20:
            raise BadRequest("A day can have at most 20 periods and breaks.")
        cleaned = []
        for i, p in enumerate(periods, 1):
            start, end = _parse_time(p.get("start")), _parse_time(p.get("end"))
            if end <= start:
                raise BadRequest(f"Row {i}: the end time must be after the start time.")
            kind = p.get("kind") or "class"
            if kind not in ("class", "break", "assembly"):
                raise BadRequest(f"Row {i}: pick class, break or assembly.")
            cleaned.append((start, end, kind, (p.get("name") or "").strip()))
        cleaned.sort(key=lambda x: x[0])
        for a, b in zip(cleaned, cleaned[1:]):
            if b[0] < a[1]:
                raise BadRequest(f"{a[3] or 'A period'} and {b[3] or 'the next one'} overlap.")
        for p in list(row.periods):
            row.periods.remove(p)
        db.flush()
        n = 0
        for i, (start, end, kind, pname) in enumerate(cleaned, 1):
            no = None
            if kind == "class":
                n += 1
                no = n
            row.periods.append(BellPeriod(company_id=company_id, no=no, kind=kind, start_time=start, end_time=end,
                                          sort_order=i, name=(pname or (f"Period {no}" if no else kind.title()))[:60]))
    if row.is_default:
        db.query(BellSchedule).filter(BellSchedule.company_id == company_id, BellSchedule.id != (row.id or 0),
                                      BellSchedule.is_default.is_(True)).update({"is_default": False})
    db.flush()
    return row


# ── Entries ──────────────────────────────────────────────────────────

def _session_id(db: Session, company_id: int) -> int | None:
    sess = academics.current_session(db, company_id)
    return sess.id if sess else None


def _section(db: Session, company_id: int, section_id: int) -> Section:
    s = (db.query(Section).options(joinedload(Section.program))
         .filter(Section.company_id == company_id, Section.id == section_id).first())
    if s is None:
        raise NotFound("Section")
    return s


def _entries_q(db: Session, company_id: int, session_id: int | None = None):
    q = (db.query(TimetableEntry)
         .options(joinedload(TimetableEntry.subject), joinedload(TimetableEntry.teacher), joinedload(TimetableEntry.room),
                  joinedload(TimetableEntry.section).joinedload(Section.program))
         .filter(TimetableEntry.company_id == company_id, TimetableEntry.active.is_(True)))
    if session_id:
        q = q.filter(TimetableEntry.session_id == session_id)
    return q


def entry_out(e: TimetableEntry, show_teacher: bool = True) -> dict:
    return {
        "uuid": str(e.uuid), "id": e.id, "section_id": e.section_id, "section_label": academics.section_label(e.section),
        "weekday": e.weekday, "period_no": e.period_no,
        "subject_id": e.subject_id, "subject_name": e.subject.name if e.subject else None,
        "subject_code": e.subject.code if e.subject else None, "subject_color": e.subject.color if e.subject else None,
        "teacher_id": e.teacher_id if show_teacher else None,
        "teacher_name": _name(e.teacher) if show_teacher else None,
        "room_id": e.room_id, "room_name": e.room.name if e.room else None,
        "group_key": e.group_key, "elective_group": e.elective_group, "note": e.note,
    }


def _mark_changed(db: Session, company_id: int, section_ids) -> None:
    for sid in set(section_ids):
        pub = db.query(TimetablePublication).filter(TimetablePublication.company_id == company_id,
                                                    TimetablePublication.section_id == sid).first()
        if pub is None:
            db.add(TimetablePublication(company_id=company_id, section_id=sid, version=0, changed=True))
        elif pub.published_at is not None:
            pub.changed = True
    db.flush()


def published_section_ids(db: Session, company_id: int) -> set[int]:
    return {sid for (sid,) in db.query(TimetablePublication.section_id).filter(
        TimetablePublication.company_id == company_id, TimetablePublication.published_at.isnot(None))}


def publication_out(pub: TimetablePublication | None) -> dict:
    if pub is None:
        return {"published": False, "published_at": None, "published_by": None, "version": 0, "changed": False}
    from core.timeutil import iso_utc
    return {"published": pub.published_at is not None, "published_at": iso_utc(pub.published_at),
            "published_by": pub.published_by_name, "version": pub.version, "changed": pub.changed}


def _day_grid(db: Session, company_id: int, program_id: int | None, schedules=None) -> list[dict]:
    rows = schedules if schedules is not None else list_schedules(db, company_id)
    out = []
    for wd in week_days(db, company_id):
        s = schedule_for(db, company_id, program_id, wd, rows)
        out.append({"weekday": wd, "label": WEEKDAYS[wd], "short": SHORT_DAYS[wd],
                    "schedule": s.name if s else None, "periods": [period_out(p) for p in s.periods] if s else []})
    return out


def allocation_summary(db: Session, company_id: int, section_id: int, entries: list[TimetableEntry]) -> list[dict]:
    """Subjects the section is taught (teacher allocation) with how many periods are placed."""
    allocs = (db.query(SectionSubject).options(joinedload(SectionSubject.subject), joinedload(SectionSubject.teacher))
              .filter(SectionSubject.company_id == company_id, SectionSubject.section_id == section_id).all())
    placed: dict[int, int] = defaultdict(int)
    for e in entries:
        if e.subject_id:
            placed[e.subject_id] += 1
    out = [{"subject_id": a.subject_id, "subject_name": a.subject.name if a.subject else "", "subject_code": a.subject.code if a.subject else None,
            "subject_color": a.subject.color if a.subject else None, "teacher_id": a.teacher_id,
            "teacher_name": _name(a.teacher), "weekly_periods": a.weekly_periods, "placed": placed.get(a.subject_id, 0)}
           for a in allocs]
    out.sort(key=lambda r: r["subject_name"])
    return out


def section_grid(db: Session, company_id: int, section_id: int) -> dict:
    section = _section(db, company_id, section_id)
    entries = _entries_q(db, company_id).filter(TimetableEntry.section_id == section.id).all()
    pub = db.query(TimetablePublication).filter(TimetablePublication.company_id == company_id,
                                                TimetablePublication.section_id == section.id).first()
    entry_ids = {e.id for e in entries}
    clashes = [c for c in find_clashes(db, company_id, section.session_id) if entry_ids & set(c["entry_ids"])]
    return {
        "section": {"id": section.id, "uuid": str(section.uuid), "label": academics.section_label(section),
                    "program_id": section.program_id, "room_id": section.room_id,
                    "class_teacher_id": section.class_teacher_id},
        "days": _day_grid(db, company_id, section.program_id),
        "entries": [entry_out(e) for e in entries],
        "clashes": clashes,
        "allocation": allocation_summary(db, company_id, section.id, entries),
        "publication": publication_out(pub),
    }


def _employee(db: Session, company_id: int, employee_id: int | None):
    if not employee_id:
        return None
    from modules.employees.models import Employee
    e = db.query(Employee).filter(Employee.company_id == company_id, Employee.id == employee_id).first()
    if e is None:
        raise BadRequest("The teacher was not found.")
    return e


def _cell_clashes(db: Session, company_id: int, session_id: int, weekday: int, period_no: int,
                  teacher_id: int | None, room_id: int | None, group_key: str | None,
                  ignore_ids: set[int]) -> list[str]:
    """Plain sentences for a teacher or room already busy in this weekday/period."""
    out = []
    if not teacher_id and not room_id:
        return out
    q = _entries_q(db, company_id, session_id).filter(TimetableEntry.weekday == weekday,
                                                      TimetableEntry.period_no == period_no)
    for e in q.all():
        if e.id in ignore_ids or (group_key and e.group_key == group_key):
            continue
        if teacher_id and e.teacher_id == teacher_id:
            out.append(f"{_name(e.teacher)} already teaches {academics.section_label(e.section)} in period {period_no} on {WEEKDAYS[weekday]}.")
        if room_id and e.room_id == room_id and not (e.room and e.room.kind in SHARED_ROOMS):
            out.append(f"{e.room.name} is already used by {academics.section_label(e.section)} in period {period_no} on {WEEKDAYS[weekday]}.")
    return out


def save_entry(db: Session, company_id: int, data: dict, row: TimetableEntry | None = None) -> list[TimetableEntry]:
    """Put a subject in a cell. `section_ids` with more than one section makes a combined class
    (one teacher and room for all of them). Without `elective_group`, whatever was in the cell is
    replaced. A teacher or room already busy is refused unless `force` is set."""
    force = bool(data.get("force"))
    if row is not None:
        section_ids = [row.section_id]
        weekday = int(data.get("weekday") or row.weekday)
        period_no = int(data.get("period_no") or row.period_no)
    else:
        section_ids = [int(s) for s in (data.get("section_ids") or [data.get("section_id")]) if s]
        if not section_ids:
            raise BadRequest("Pick the section.")
        weekday = int(data.get("weekday") or 0)
        period_no = int(data.get("period_no") or 0)
    sections = [_section(db, company_id, sid) for sid in section_ids]
    session_id = sections[0].session_id
    if any(s.session_id != session_id for s in sections):
        raise BadRequest("Combined classes must be in the same session.")
    if weekday not in week_days(db, company_id):
        raise BadRequest("That day is not a working day.")
    schedules = list_schedules(db, company_id)
    for s in sections:
        sched = schedule_for(db, company_id, s.program_id, weekday, schedules)
        if sched is None or period_no not in {p.no for p in sched.periods if p.no}:
            raise BadRequest(f"{academics.section_label(s)} has no period {period_no} on {WEEKDAYS[weekday]}. Check the bell schedule.")
    subject_id = data.get("subject_id", row.subject_id if row else None)
    if subject_id:
        if not db.query(Subject.id).filter(Subject.company_id == company_id, Subject.id == subject_id).first():
            raise BadRequest("The subject was not found.")
    teacher_id = data.get("teacher_id", row.teacher_id if row else None) if "teacher_id" in data or row else None
    if teacher_id is None and subject_id and "teacher_id" not in data:
        alloc = db.query(SectionSubject).filter(SectionSubject.company_id == company_id,
                                                SectionSubject.section_id == sections[0].id,
                                                SectionSubject.subject_id == subject_id).first()
        teacher_id = alloc.teacher_id if alloc else None
    _employee(db, company_id, teacher_id)
    room_id = data.get("room_id", row.room_id if row else None) if ("room_id" in data or row) else None
    if room_id is None and "room_id" not in data and len(sections) == 1:
        room_id = sections[0].room_id
    if room_id and not db.query(Room.id).filter(Room.company_id == company_id, Room.id == room_id).first():
        raise BadRequest("The room was not found.")
    elective_group = (data.get("elective_group", row.elective_group if row else None) or None)
    group_key = row.group_key if row else (uuid_mod.uuid4().hex if len(sections) > 1 else None)
    ignore = {row.id} if row else set()
    if not elective_group:
        # Replacing the cell: what is there now will go, so it does not clash.
        for s in sections:
            ignore |= {e.id for e in db.query(TimetableEntry).filter(
                TimetableEntry.company_id == company_id, TimetableEntry.section_id == s.id, TimetableEntry.active.is_(True),
                TimetableEntry.weekday == weekday, TimetableEntry.period_no == period_no)}
    problems = _cell_clashes(db, company_id, session_id, weekday, period_no, teacher_id, room_id, group_key, ignore)
    if problems and not force:
        raise Conflict(" ".join(problems))
    out = []
    if row is not None:
        if not elective_group:
            for e in db.query(TimetableEntry).filter(
                    TimetableEntry.company_id == company_id, TimetableEntry.section_id == row.section_id,
                    TimetableEntry.active.is_(True), TimetableEntry.weekday == weekday,
                    TimetableEntry.period_no == period_no, TimetableEntry.id != row.id):
                e.active = False
        row.weekday, row.period_no = weekday, period_no
        row.subject_id, row.teacher_id, row.room_id = subject_id, teacher_id, room_id
        row.elective_group = elective_group
        if "note" in data:
            row.note = (data.get("note") or None)
        out.append(row)
    else:
        for s in sections:
            if not elective_group:
                for e in db.query(TimetableEntry).filter(
                        TimetableEntry.company_id == company_id, TimetableEntry.section_id == s.id,
                        TimetableEntry.active.is_(True), TimetableEntry.weekday == weekday,
                        TimetableEntry.period_no == period_no):
                    e.active = False
            e = TimetableEntry(company_id=company_id, session_id=session_id, section_id=s.id, weekday=weekday,
                               period_no=period_no, subject_id=subject_id, teacher_id=teacher_id, room_id=room_id,
                               group_key=group_key, elective_group=elective_group, note=(data.get("note") or None))
            db.add(e)
            out.append(e)
    db.flush()
    _mark_changed(db, company_id, [s.id for s in sections])
    return out


def remove_entry(db: Session, company_id: int, row: TimetableEntry, whole_group: bool = True) -> int:
    rows = [row]
    if whole_group and row.group_key:
        rows = db.query(TimetableEntry).filter(TimetableEntry.company_id == company_id,
                                               TimetableEntry.group_key == row.group_key,
                                               TimetableEntry.active.is_(True)).all()
    for e in rows:
        e.active = False
    db.flush()
    _mark_changed(db, company_id, [e.section_id for e in rows])
    return len(rows)


def get_entry(db: Session, company_id: int, uuid) -> TimetableEntry:
    e = _entries_q(db, company_id).filter(TimetableEntry.uuid == uuid).first()
    if e is None:
        raise NotFound("Timetable entry")
    return e


# ── Clash checks ─────────────────────────────────────────────────────

def find_clashes(db: Session, company_id: int, session_id: int | None = None,
                 entries: list[TimetableEntry] | None = None) -> list[dict]:
    """Every problem in the session's timetable: a teacher or room in two places at once,
    two subjects in one section's period (unless they are electives), and teachers over
    their daily, weekly or back-to-back limits."""
    session_id = session_id or _session_id(db, company_id)
    rows = entries if entries is not None else _entries_q(db, company_id, session_id).all()
    cfg = get_settings(db, company_id)
    out: list[dict] = []

    def unit(e):  # a combined class counts once
        return e.group_key or f"e{e.id}"

    by_teacher: dict[tuple, list] = defaultdict(list)
    by_room: dict[tuple, list] = defaultdict(list)
    by_cell: dict[tuple, list] = defaultdict(list)
    for e in rows:
        if e.teacher_id:
            by_teacher[(e.teacher_id, e.weekday, e.period_no)].append(e)
        if e.room_id and not (e.room and e.room.kind in SHARED_ROOMS):
            by_room[(e.room_id, e.weekday, e.period_no)].append(e)
        by_cell[(e.section_id, e.weekday, e.period_no)].append(e)
    for (tid, wd, p), es in by_teacher.items():
        if len({unit(e) for e in es}) > 1:
            out.append({"kind": "teacher", "severity": "error", "weekday": wd, "period_no": p, "teacher_id": tid,
                        "message": f"{_name(es[0].teacher)} is in {len({unit(e) for e in es})} classes at once "
                                   f"({', '.join(sorted({academics.section_label(e.section) for e in es}))}) "
                                   f"in period {p} on {WEEKDAYS[wd]}.",
                        "entry_ids": [e.id for e in es], "entry_uuids": [str(e.uuid) for e in es]})
    for (rid, wd, p), es in by_room.items():
        if len({unit(e) for e in es}) > 1:
            out.append({"kind": "room", "severity": "error", "weekday": wd, "period_no": p, "room_id": rid,
                        "message": f"{es[0].room.name} is booked by "
                                   f"{', '.join(sorted({academics.section_label(e.section) for e in es}))} "
                                   f"in period {p} on {WEEKDAYS[wd]}.",
                        "entry_ids": [e.id for e in es], "entry_uuids": [str(e.uuid) for e in es]})
    for (sid, wd, p), es in by_cell.items():
        if len(es) > 1 and not all(e.elective_group for e in es):
            out.append({"kind": "section", "severity": "error", "weekday": wd, "period_no": p, "section_id": sid,
                        "message": f"{academics.section_label(es[0].section)} has {len(es)} subjects in period {p} on "
                                   f"{WEEKDAYS[wd]}. Mark them as electives or keep one.",
                        "entry_ids": [e.id for e in es], "entry_uuids": [str(e.uuid) for e in es]})
    # Loads.
    week: dict[int, set] = defaultdict(set)
    day: dict[tuple, set] = defaultdict(set)
    slots: dict[tuple, set] = defaultdict(set)
    names: dict[int, str] = {}
    ids_of: dict[int, list] = defaultdict(list)
    for e in rows:
        if not e.teacher_id:
            continue
        names[e.teacher_id] = _name(e.teacher) or ""
        week[e.teacher_id].add((e.weekday, e.period_no, unit(e)))
        day[(e.teacher_id, e.weekday)].add((e.period_no, unit(e)))
        slots[(e.teacher_id, e.weekday)].add(e.period_no)
        ids_of[e.teacher_id].append(e.id)
    for tid, items in week.items():
        n = len({(w, p) for w, p, _ in items})
        if n > cfg["max_weekly_load"]:
            out.append({"kind": "load_week", "severity": "warning", "teacher_id": tid, "weekday": None, "period_no": None,
                        "message": f"{names[tid]} has {n} classes a week (limit {cfg['max_weekly_load']}).",
                        "entry_ids": ids_of[tid], "entry_uuids": []})
    for (tid, wd), items in day.items():
        n = len({p for p, _ in items})
        if n > cfg["max_periods_per_day"]:
            out.append({"kind": "load_day", "severity": "warning", "teacher_id": tid, "weekday": wd, "period_no": None,
                        "message": f"{names[tid]} has {n} classes on {WEEKDAYS[wd]} (limit {cfg['max_periods_per_day']}).",
                        "entry_ids": [], "entry_uuids": []})
        ps = sorted(slots[(tid, wd)])
        run = best = 1 if ps else 0
        for a, b in zip(ps, ps[1:]):
            run = run + 1 if b == a + 1 else 1
            best = max(best, run)
        if best > cfg["max_consecutive"]:
            out.append({"kind": "consecutive", "severity": "warning", "teacher_id": tid, "weekday": wd, "period_no": None,
                        "message": f"{names[tid]} has {best} classes in a row on {WEEKDAYS[wd]} (limit {cfg['max_consecutive']}).",
                        "entry_ids": [], "entry_uuids": []})
    order = {"error": 0, "warning": 1}
    out.sort(key=lambda c: (order[c["severity"]], c.get("weekday") or 0, c.get("period_no") or 0))
    return out


def teacher_loads(db: Session, company_id: int, session_id: int | None = None) -> list[dict]:
    session_id = session_id or _session_id(db, company_id)
    rows = _entries_q(db, company_id, session_id).filter(TimetableEntry.teacher_id.isnot(None)).all()
    cfg = get_settings(db, company_id)
    per: dict[int, dict] = {}
    for e in rows:
        t = per.setdefault(e.teacher_id, {"teacher_id": e.teacher_id, "name": _name(e.teacher), "slots": set(), "days": defaultdict(set)})
        t["slots"].add((e.weekday, e.period_no))
        t["days"][e.weekday].add(e.period_no)
    out = []
    for t in per.values():
        out.append({"teacher_id": t["teacher_id"], "name": t["name"], "weekly": len(t["slots"]),
                    "by_day": {str(d): len(p) for d, p in sorted(t["days"].items())},
                    "over": len(t["slots"]) > cfg["max_weekly_load"]})
    out.sort(key=lambda r: (-r["weekly"], r["name"] or ""))
    return out


# ── Teacher and room views ───────────────────────────────────────────

def _default_days(db: Session, company_id: int) -> list[dict]:
    return _day_grid(db, company_id, None)


def teacher_week(db: Session, company_id: int, employee_id: int, published_only: bool = False) -> dict:
    q = _entries_q(db, company_id, _session_id(db, company_id)).filter(TimetableEntry.teacher_id == employee_id)
    rows = q.all()
    if published_only:
        pub = published_section_ids(db, company_id)
        rows = [e for e in rows if e.section_id in pub]
    emp = _employee(db, company_id, employee_id)
    return {"teacher": {"id": employee_id, "name": _name(emp)}, "days": _default_days(db, company_id),
            "entries": [entry_out(e) for e in rows], "weekly": len({(e.weekday, e.period_no) for e in rows})}


def room_week(db: Session, company_id: int, room_id: int) -> dict:
    room = db.query(Room).filter(Room.company_id == company_id, Room.id == room_id).first()
    if room is None:
        raise NotFound("Room")
    rows = _entries_q(db, company_id, _session_id(db, company_id)).filter(TimetableEntry.room_id == room_id).all()
    return {"room": {"id": room.id, "name": room.name, "kind": room.kind}, "days": _default_days(db, company_id),
            "entries": [entry_out(e) for e in rows]}


# ── Copy, publish ────────────────────────────────────────────────────

def copy_timetable(db: Session, company_id: int, from_section_id: int, to_section_ids: list[int],
                   teachers: str = "allocation") -> dict:
    """Copy one section's timetable to others. teachers: allocation (each target's own teacher
    for the subject), same (keep the teachers), none."""
    src = _section(db, company_id, from_section_id)
    src_rows = db.query(TimetableEntry).filter(TimetableEntry.company_id == company_id,
                                               TimetableEntry.section_id == src.id, TimetableEntry.active.is_(True)).all()
    if not src_rows:
        raise BadRequest("That timetable is empty; there is nothing to copy.")
    copied, skipped = 0, 0
    for tid in to_section_ids:
        if tid == src.id:
            continue
        dst = _section(db, company_id, tid)
        alloc = {a.subject_id: a.teacher_id for a in db.query(SectionSubject).filter(
            SectionSubject.company_id == company_id, SectionSubject.section_id == dst.id)}
        for e in db.query(TimetableEntry).filter(TimetableEntry.company_id == company_id,
                                                 TimetableEntry.section_id == dst.id, TimetableEntry.active.is_(True)):
            e.active = False
        for e in src_rows:
            teacher = e.teacher_id if teachers == "same" else (alloc.get(e.subject_id) if teachers == "allocation" else None)
            if teachers == "allocation" and e.subject_id and e.subject_id not in alloc:
                skipped += 1
                continue
            db.add(TimetableEntry(company_id=company_id, session_id=dst.session_id, section_id=dst.id, weekday=e.weekday,
                                  period_no=e.period_no, subject_id=e.subject_id, teacher_id=teacher,
                                  room_id=dst.room_id if e.room_id == src.room_id else e.room_id,
                                  elective_group=e.elective_group, note=e.note))
            copied += 1
        _mark_changed(db, company_id, [dst.id])
    db.flush()
    audit.event(db, "timetable.copied", "academic_sections", src.id,
                {"to": to_section_ids, "teachers": teachers, "entries": copied}, entity_label=academics.section_label(src))
    return {"copied": copied, "skipped": skipped}


def publish(db: Session, company_id: int, section_ids: list[int]) -> dict:
    """Publish the timetables of these sections: teachers, parents and learners see them, and
    (by setting) everyone affected is told."""
    cfg = get_settings(db, company_id)
    actor = context.current().actor_name
    done = []
    teacher_ids: set[int] = set()
    for sid in section_ids:
        s = _section(db, company_id, sid)
        rows = db.query(TimetableEntry).filter(TimetableEntry.company_id == company_id, TimetableEntry.section_id == s.id,
                                               TimetableEntry.active.is_(True)).all()
        if not rows:
            continue
        pub = db.query(TimetablePublication).filter(TimetablePublication.company_id == company_id,
                                                    TimetablePublication.section_id == s.id).first()
        if pub is None:
            pub = TimetablePublication(company_id=company_id, section_id=s.id, version=0)
            db.add(pub)
        pub.version = (pub.version or 0) + 1
        pub.published_at = _now()
        pub.published_by_name = actor
        pub.changed = False
        teacher_ids |= {e.teacher_id for e in rows if e.teacher_id}
        done.append(s)
        audit.event(db, "timetable.published", "academic_sections", s.id, {"version": pub.version, "entries": len(rows)},
                    entity_label=academics.section_label(s))
    db.flush()
    if done and cfg["notify_on_publish"]:
        from modules.notifications.service import notify, notify_portal
        from modules.students import service as students
        labels = ", ".join(academics.section_label(s) for s in done[:4]) + (" and more" if len(done) > 4 else "")
        notify(db, company_id, sorted(teacher_ids), kind="timetable.published", title="Timetable published",
               body=f"The timetable for {labels} is published. Check your classes.", link="/dashboard/timetable/mine",
               actor_name=actor)
        student_ids = []
        for s in done:
            student_ids += [st.id for st, _ in students.section_students(db, company_id, s.id)]
        notify_portal(db, company_id, students.portal_account_ids_for_students(db, company_id, student_ids),
                      kind="timetable.published", title="Timetable updated",
                      body="The class timetable has been published.", link="/dashboard/my/timetable", actor_name=actor)
    return {"published": len(done)}


# ── Generator ────────────────────────────────────────────────────────

def generate(db: Session, company_id: int, section_ids: list[int], replace: bool = False,
             periods_by_subject: dict[int, int] | None = None) -> dict:
    """Fill the timetables of these sections from the teacher allocation (weekly periods per
    subject), greedily: the busiest teachers first, each subject spread over the week, never a
    teacher or the section in two places at once, within the daily load limit. Existing
    entries are kept (and counted) unless `replace` is set."""
    cfg = get_settings(db, company_id)
    session_id = _session_id(db, company_id)
    schedules = list_schedules(db, company_id)
    sections = [_section(db, company_id, sid) for sid in section_ids]
    if replace:
        for s in sections:
            for e in db.query(TimetableEntry).filter(TimetableEntry.company_id == company_id,
                                                     TimetableEntry.section_id == s.id, TimetableEntry.active.is_(True)):
                e.active = False
        db.flush()
    existing = _entries_q(db, company_id, session_id).all()
    teacher_busy: set[tuple] = {(e.teacher_id, e.weekday, e.period_no) for e in existing if e.teacher_id}
    section_busy: set[tuple] = {(e.section_id, e.weekday, e.period_no) for e in existing}
    teacher_day: dict[tuple, int] = defaultdict(int)
    for t, wd, p in teacher_busy:
        teacher_day[(t, wd)] += 1
    subj_day: dict[tuple, int] = defaultdict(int)
    placed_count: dict[tuple, int] = defaultdict(int)
    for e in existing:
        if e.subject_id:
            subj_day[(e.section_id, e.subject_id, e.weekday)] += 1
            placed_count[(e.section_id, e.subject_id)] += 1
    slots_of: dict[int, list[tuple]] = {}
    for s in sections:
        slots = []
        for wd in week_days(db, company_id):
            sched = schedule_for(db, company_id, s.program_id, wd, schedules)
            if sched:
                slots += [(wd, p.no) for p in sched.periods if p.no]
        slots_of[s.id] = slots
    needs = []
    for s in sections:
        allocs = db.query(SectionSubject).options(joinedload(SectionSubject.subject)).filter(
            SectionSubject.company_id == company_id, SectionSubject.section_id == s.id).all()
        for a in allocs:
            want = (periods_by_subject or {}).get(a.subject_id, a.weekly_periods or 0)
            missing = want - placed_count[(s.id, a.subject_id)]
            if missing > 0:
                needs.append((s, a, missing))
    load = defaultdict(int)
    for s, a, m in needs:
        if a.teacher_id:
            load[a.teacher_id] += m
    needs.sort(key=lambda x: (-(load[x[1].teacher_id] if x[1].teacher_id else 0), -x[2], x[0].id, x[1].subject_id))
    # Interleave: one period of each need per round, so no subject takes all the good slots.
    rounds = max((m for _, _, m in needs), default=0)
    placed, unplaced = 0, defaultdict(int)
    max_day = cfg["max_periods_per_day"]
    new_rows = []
    for r in range(rounds):
        for s, a, m in needs:
            if r >= m:
                continue
            tid = a.teacher_id
            days_count = len({wd for wd, _ in slots_of[s.id]}) or 1
            best, best_score = None, None
            for wd, p in slots_of[s.id]:
                if (s.id, wd, p) in section_busy:
                    continue
                if tid and ((tid, wd, p) in teacher_busy or teacher_day[(tid, wd)] >= max_day):
                    continue
                same_day = subj_day[(s.id, a.subject_id, wd)]
                if same_day >= 1 and m <= days_count:
                    score = 100 + same_day * 50
                else:
                    score = same_day * 20
                # Avoid the same subject back to back, prefer lighter days for the teacher and the
                # morning for core subjects.
                if (s.id, wd, p - 1) in section_busy and any(
                        x.section_id == s.id and x.weekday == wd and x.period_no == p - 1 and x.subject_id == a.subject_id
                        for x in new_rows):
                    score += 30
                score += teacher_day[(tid, wd)] * 3 if tid else 0
                kind = a.subject.kind if a.subject else "theory"
                score += p if kind in ("theory", "theory_practical", "language") else max(0, 8 - p)
                score += (wd * 7 + p + s.id + a.subject_id) % 5 * 0.1  # stable tie-break
                if best_score is None or score < best_score:
                    best, best_score = (wd, p), score
            if best is None:
                unplaced[(s.id, a.subject_id)] += 1
                continue
            wd, p = best
            room = s.room_id
            if a.subject and a.subject.kind in ("activity",):
                room = None
            e = TimetableEntry(company_id=company_id, session_id=s.session_id, section_id=s.id, weekday=wd, period_no=p,
                               subject_id=a.subject_id, teacher_id=tid, room_id=room)
            db.add(e)
            new_rows.append(e)
            section_busy.add((s.id, wd, p))
            if tid:
                teacher_busy.add((tid, wd, p))
                teacher_day[(tid, wd)] += 1
            subj_day[(s.id, a.subject_id, wd)] += 1
            placed += 1
    db.flush()
    _mark_changed(db, company_id, [s.id for s in sections])
    names = {s.id: academics.section_label(s) for s in sections}
    subj_names = {i: n for i, n in db.query(Subject.id, Subject.name).filter(Subject.company_id == company_id)}
    audit.event(db, "timetable.generated", "timetable_entries", None,
                {"sections": section_ids, "placed": placed, "unplaced": sum(unplaced.values())})
    return {"placed": placed,
            "unplaced": [{"section_id": sid, "section_label": names.get(sid), "subject_id": subj,
                          "subject_name": subj_names.get(subj), "missing": n} for (sid, subj), n in unplaced.items()]}


# ── A day (for attendance, home cards and the portal) ────────────────

def _subs_on(db: Session, company_id: int, day: date):
    return {s.entry_id: s for s in db.query(Substitution).options(joinedload(Substitution.substitute)).filter(
        Substitution.company_id == company_id, Substitution.date == day, Substitution.status == "active")}


def _with_times(db: Session, company_id: int, program_id: int | None, weekday: int, schedules=None) -> dict[int, BellPeriod]:
    s = schedule_for(db, company_id, program_id, weekday, schedules)
    return {p.no: p for p in s.periods if p.no} if s else {}


def section_day(db: Session, company_id: int, section_id: int, day: date, published_only: bool = False) -> list[dict]:
    """The section's teaching periods on a date, with substitutions. Empty on days without classes."""
    section = _section(db, company_id, section_id)
    if published_only and section_id not in published_section_ids(db, company_id):
        return []
    wd = day.isoweekday()
    rows = _entries_q(db, company_id).filter(TimetableEntry.section_id == section_id, TimetableEntry.weekday == wd).all()
    times = _with_times(db, company_id, section.program_id, wd)
    subs = _subs_on(db, company_id, day)
    out = []
    for e in sorted(rows, key=lambda x: (x.period_no, x.elective_group or "")):
        p = times.get(e.period_no)
        sub = subs.get(e.id)
        d = entry_out(e)
        d.update({"start": _hm(p.start_time) if p else None, "end": _hm(p.end_time) if p else None,
                  "period_name": p.name if p else f"Period {e.period_no}",
                  "substitution": None})
        if sub:
            d["substitution"] = {"uuid": str(sub.uuid), "kind": sub.kind, "teacher_id": sub.substitute_teacher_id,
                                 "teacher_name": _name(sub.substitute), "note": sub.note}
        out.append(d)
    return out


def teacher_day(db: Session, company_id: int, employee_id: int, day: date) -> list[dict]:
    """A teacher's classes on a date: their own periods (unless someone else takes them that
    day) and the substitutions they were given."""
    wd = day.isoweekday()
    schedules = list_schedules(db, company_id)
    subs = _subs_on(db, company_id, day)
    own = _entries_q(db, company_id, _session_id(db, company_id)).filter(
        TimetableEntry.teacher_id == employee_id, TimetableEntry.weekday == wd).all()
    out = []
    seen_groups = set()
    for e in own:
        if e.group_key:
            if e.group_key in seen_groups:
                continue
            seen_groups.add(e.group_key)
        sub = subs.get(e.id)
        if sub and (sub.kind == "cancelled" or sub.substitute_teacher_id != employee_id):
            continue
        out.append((e, None))
    for sub in subs.values():
        if sub.substitute_teacher_id == employee_id and sub.kind == "substitute":
            e = get_entry_by_id(db, company_id, sub.entry_id)
            if e and e.teacher_id != employee_id:
                out.append((e, sub))
    res = []
    for e, sub in out:
        times = _with_times(db, company_id, e.section.program_id if e.section else None, wd, schedules)
        p = times.get(e.period_no)
        d = entry_out(e)
        if e.group_key:
            d["section_label"] = ", ".join(sorted({academics.section_label(x.section) for x in own if x.group_key == e.group_key}))
        d.update({"start": _hm(p.start_time) if p else None, "end": _hm(p.end_time) if p else None,
                  "period_name": p.name if p else f"Period {e.period_no}",
                  "is_substitution": sub is not None,
                  "absent_teacher_name": _name(e.teacher) if sub else None})
        res.append(d)
    res.sort(key=lambda r: (r["period_no"], r["section_label"]))
    return res


def get_entry_by_id(db: Session, company_id: int, entry_id: int) -> TimetableEntry | None:
    return _entries_q(db, company_id).filter(TimetableEntry.id == entry_id).first()


# ── Substitutions ────────────────────────────────────────────────────

def _on_leave(db: Session, company_id: int, day: date) -> dict[int, str]:
    """Staff on approved leave on a date -> leave type name (empty when the leave module is off)."""
    try:
        from modules.leave.models import LeaveRequest, LeaveType
        with db.begin_nested():
            rows = (db.query(LeaveRequest.employee_id, LeaveType.name)
                    .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id)
                    .filter(LeaveRequest.company_id == company_id, LeaveRequest.status.in_(("approved", "cancel_pending")),
                            LeaveRequest.from_date <= day, LeaveRequest.to_date >= day).all())
        return {eid: name for eid, name in rows}
    except Exception:
        return {}


def absent_teachers(db: Session, company_id: int, day: date) -> list[dict]:
    """Teachers on leave on a date who have classes that day, with how many are covered."""
    leave = _on_leave(db, company_id, day)
    wd = day.isoweekday()
    sid = _session_id(db, company_id)
    subs = _subs_on(db, company_id, day)
    rows = _entries_q(db, company_id, sid).filter(TimetableEntry.weekday == wd,
                                                  TimetableEntry.teacher_id.in_(list(leave) or [0])).all()
    per: dict[int, dict] = {}
    for e in rows:
        t = per.setdefault(e.teacher_id, {"employee_id": e.teacher_id, "name": _name(e.teacher), "reason": leave.get(e.teacher_id),
                                          "periods": 0, "covered": 0})
        t["periods"] += 1
        if e.id in subs:
            t["covered"] += 1
    return sorted(per.values(), key=lambda r: r["name"] or "")


def _teacher_pool(db: Session, company_id: int):
    """Everyone who teaches: has an allocation or a timetable entry."""
    from modules.employees.models import Employee
    ids = {i for (i,) in db.query(SectionSubject.teacher_id).filter(SectionSubject.company_id == company_id,
                                                                     SectionSubject.teacher_id.isnot(None))}
    ids |= {i for (i,) in db.query(TimetableEntry.teacher_id).filter(TimetableEntry.company_id == company_id,
                                                                      TimetableEntry.teacher_id.isnot(None))}
    return db.query(Employee).filter(Employee.company_id == company_id, Employee.id.in_(ids or {0}),
                                     Employee.active.is_(True)).order_by(Employee.first_name).all()


def free_teachers(db: Session, company_id: int, day: date, period_no: int, for_entry: TimetableEntry | None = None) -> list[dict]:
    """Teachers free in a period on a date, best first: those who teach the section, then the
    same subject, then those with the fewest classes that day."""
    wd = day.isoweekday()
    sid = _session_id(db, company_id)
    rows = _entries_q(db, company_id, sid).filter(TimetableEntry.weekday == wd).all()
    subs = _subs_on(db, company_id, day)
    leave = _on_leave(db, company_id, day)
    busy = {e.teacher_id for e in rows if e.period_no == period_no and e.teacher_id
            and not (e.id in subs and subs[e.id].substitute_teacher_id != e.teacher_id)}
    busy |= {s.substitute_teacher_id for s in subs.values() if s.period_no == period_no and s.substitute_teacher_id
             and (for_entry is None or s.entry_id != for_entry.id)}
    day_load: dict[int, int] = defaultdict(int)
    for e in rows:
        if e.teacher_id:
            day_load[e.teacher_id] += 1
    for s in subs.values():
        if s.substitute_teacher_id:
            day_load[s.substitute_teacher_id] += 1
    knows_section, knows_subject = set(), set()
    if for_entry is not None:
        knows_section = {i for (i,) in db.query(SectionSubject.teacher_id).filter(
            SectionSubject.company_id == company_id, SectionSubject.section_id == for_entry.section_id)}
        if for_entry.subject_id:
            knows_subject = {i for (i,) in db.query(SectionSubject.teacher_id).filter(
                SectionSubject.company_id == company_id, SectionSubject.subject_id == for_entry.subject_id)}
    cfg = get_settings(db, company_id)
    out = []
    for t in _teacher_pool(db, company_id):
        if t.id in busy or t.id in leave or (for_entry is not None and t.id == for_entry.teacher_id):
            continue
        reasons = []
        if t.id in knows_section:
            reasons.append("Teaches this class")
        if t.id in knows_subject:
            reasons.append("Teaches this subject")
        out.append({"employee_id": t.id, "name": _name(t), "classes_today": day_load.get(t.id, 0),
                    "at_limit": day_load.get(t.id, 0) >= cfg["max_periods_per_day"], "reasons": reasons,
                    "score": (0 if t.id in knows_section else 1) + (0 if t.id in knows_subject else 0.5) + day_load.get(t.id, 0) * 0.2})
    out.sort(key=lambda r: (r["at_limit"], r["score"], r["name"] or ""))
    return out


def teacher_periods_on(db: Session, company_id: int, employee_id: int, day: date) -> list[dict]:
    """An absent teacher's periods on a date, each with its substitution (if any) and suggestions."""
    wd = day.isoweekday()
    rows = _entries_q(db, company_id, _session_id(db, company_id)).filter(
        TimetableEntry.teacher_id == employee_id, TimetableEntry.weekday == wd).all()
    subs = _subs_on(db, company_id, day)
    schedules = list_schedules(db, company_id)
    out = []
    for e in sorted(rows, key=lambda x: x.period_no):
        times = _with_times(db, company_id, e.section.program_id if e.section else None, wd, schedules)
        p = times.get(e.period_no)
        sub = subs.get(e.id)
        d = entry_out(e)
        d.update({"start": _hm(p.start_time) if p else None, "end": _hm(p.end_time) if p else None,
                  "substitution": {"uuid": str(sub.uuid), "kind": sub.kind, "teacher_id": sub.substitute_teacher_id,
                                   "teacher_name": _name(sub.substitute), "note": sub.note} if sub else None,
                  "suggestions": free_teachers(db, company_id, day, e.period_no, e)[:8]})
        out.append(d)
    return out


def assign_substitution(db: Session, company_id: int, entry: TimetableEntry, day: date, kind: str,
                        substitute_id: int | None, note: str | None, force: bool = False) -> Substitution:
    if day.isoweekday() != entry.weekday:
        raise BadRequest(f"That class is on {WEEKDAYS[entry.weekday]}s, not on {WEEKDAYS[day.isoweekday()]}.")
    if kind not in ("substitute", "cancelled"):
        raise BadRequest("Pick a substitute or cancel the class.")
    sub_emp = None
    if kind == "substitute":
        if not substitute_id:
            raise BadRequest("Pick the substitute teacher.")
        sub_emp = _employee(db, company_id, substitute_id)
        if substitute_id == entry.teacher_id:
            raise BadRequest("Pick someone other than the absent teacher.")
        if not force and substitute_id not in {t["employee_id"] for t in free_teachers(db, company_id, day, entry.period_no, entry)}:
            raise Conflict(f"{_name(sub_emp)} is not free in period {entry.period_no} on that day.")
    row = db.query(Substitution).filter(Substitution.company_id == company_id, Substitution.entry_id == entry.id,
                                        Substitution.date == day).first()
    if row is None:
        row = Substitution(company_id=company_id, entry_id=entry.id, date=day)
        db.add(row)
    row.section_id, row.period_no, row.subject_id = entry.section_id, entry.period_no, entry.subject_id
    row.absent_teacher_id = entry.teacher_id
    row.substitute_teacher_id = substitute_id if kind == "substitute" else None
    row.kind, row.note, row.status = kind, (note or None), "active"
    row.created_by_name = context.current().actor_name
    db.flush()
    cfg = get_settings(db, company_id)
    if sub_emp is not None and cfg["notify_substitute"]:
        from modules.notifications.service import notify
        subj = entry.subject.name if entry.subject else "a class"
        notify(db, company_id, [sub_emp.id], kind="timetable.substitution", title="Substitution for you",
               body=f"{day:%a %d %b}: period {entry.period_no}, {subj} in {academics.section_label(entry.section)} "
                    f"(for {_name(entry.teacher) or 'an absent teacher'}).",
               link=f"/dashboard/timetable/mine?date={day.isoformat()}", actor_name=context.current().actor_name)
    return row


def substitution_out(s: Substitution) -> dict:
    e = s.entry
    return {"uuid": str(s.uuid), "date": s.date.isoformat(), "period_no": s.period_no, "kind": s.kind, "status": s.status,
            "section_id": s.section_id, "section_label": academics.section_label(e.section) if e else "",
            "subject_name": e.subject.name if e and e.subject else None,
            "absent_teacher_id": s.absent_teacher_id, "absent_teacher_name": _name(s.absent_teacher),
            "substitute_teacher_id": s.substitute_teacher_id, "substitute_teacher_name": _name(s.substitute),
            "note": s.note, "created_by": s.created_by_name}


def substitutions_query(db: Session, company_id: int, date_from: date | None, date_to: date | None,
                        teacher_id: int | None = None, status: str | None = "active"):
    q = (db.query(Substitution)
         .options(joinedload(Substitution.entry).joinedload(TimetableEntry.section).joinedload(Section.program),
                  joinedload(Substitution.entry).joinedload(TimetableEntry.subject),
                  joinedload(Substitution.substitute), joinedload(Substitution.absent_teacher))
         .filter(Substitution.company_id == company_id))
    if date_from:
        q = q.filter(Substitution.date >= date_from)
    if date_to:
        q = q.filter(Substitution.date <= date_to)
    if teacher_id:
        q = q.filter(or_(Substitution.substitute_teacher_id == teacher_id, Substitution.absent_teacher_id == teacher_id))
    if status:
        q = q.filter(Substitution.status == status)
    return q.order_by(Substitution.date.desc(), Substitution.period_no)


def substitution_counts(db: Session, company_id: int, date_from: date, date_to: date) -> list[dict]:
    """How many substitutions each teacher took in a range (fair sharing)."""
    rows = substitutions_query(db, company_id, date_from, date_to).filter(Substitution.kind == "substitute").all()
    per: dict[int, dict] = {}
    for s in rows:
        if not s.substitute_teacher_id:
            continue
        r = per.setdefault(s.substitute_teacher_id, {"employee_id": s.substitute_teacher_id, "name": _name(s.substitute), "count": 0})
        r["count"] += 1
    return sorted(per.values(), key=lambda r: -r["count"])


def week_dates(day: date) -> list[date]:
    start = day - timedelta(days=day.isoweekday() - 1)
    return [start + timedelta(days=i) for i in range(7)]
