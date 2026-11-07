"""Shared rules of the communication module: settings, audiences, who may message whom.

Functions other parts of the module (and other modules) use:
    get_settings(db, company_id)                    -> dict with defaults filled in
    resolve_audience(db, company_id, audience)      -> Resolved (staff, guardians, students)
    audience_label(db, company_id, audience)        -> "Parents of Grade 8 - A"
    check_teacher_audience(db, company_id, employee_id, audience)  raises when outside own sections
    portal_context(db, user)                        -> the portal person's children, sections, programs
    portal_matches(audience, ctx)                   -> bool, for events and other audience-filtered items
    messageable_staff(db, company_id, user)         -> staff a parent / student may write to
    staff_can_message_student(db, user, student_id) -> bool
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core.security import _get_role_permissions
from modules.academics import service as academics
from modules.academics.models import Program, Section, SectionSubject
from modules.comms.models import CommsSettings
from modules.employees.models import Employee
from modules.students.models import Enrollment, Guardian, PortalAccount, Student, StudentGuardian

NOTICE_CATEGORIES = {
    "general": "General", "academic": "Academic", "event": "Event", "holiday": "Holiday", "fee": "Fees",
    "exam": "Exams", "emergency": "Emergency",
}
EVENT_CATEGORIES = {
    "event": "Event", "holiday": "Holiday", "exam": "Exam", "ptm": "Parent-teacher meeting", "sports": "Sports",
    "trip": "Trip", "meeting": "Meeting", "cultural": "Cultural", "other": "Other",
}
CHANNELS = ("in_app", "email", "sms", "whatsapp")
STAFF_ROLES = ("admin", "principal", "hr", "manager", "teacher", "accountant", "librarian", "employee")

DEFAULT_SETTINGS = {
    "office_hours": {"enabled": True, "start": "08:00", "end": "16:00", "days": [1, 2, 3, 4, 5, 6]},
    "parents_can_message": True,
    "students_can_message": True,
    "office_inbox": True,
    "subject_teachers": True,
    "default_channels": ["in_app"],
    "emergency_channels": ["in_app", "email", "sms"],
    "diary_enabled": True,
    "ptm_reminder_hours": 24,
}


def now() -> datetime:
    return datetime.now(timezone.utc)


def perms(db: Session, user: dict) -> set[str]:
    return set(_get_role_permissions(user["company_id"], user["role"], db))


def has(db: Session, user: dict, *keys: str) -> bool:
    p = perms(db, user)
    return any(k in p for k in keys)


def require(db: Session, user: dict, *keys: str) -> None:
    if not has(db, user, *keys):
        raise HTTPException(status_code=403, detail="You don't have access to this.")


def employee_name(e: Employee | None) -> str:
    return f"{e.first_name} {e.last_name or ''}".strip() if e else ""


def actor_name(db: Session, user: dict) -> str:
    if user.get("employee_id"):
        e = db.query(Employee).filter(Employee.id == user["employee_id"]).first()
        if e:
            return employee_name(e)
    if user.get("portal_id"):
        acc = db.query(PortalAccount).filter(PortalAccount.id == user["portal_id"]).first()
        if acc and acc.guardian_id and acc.guardian:
            return acc.guardian.full_name
        if acc and acc.student_id and acc.student:
            return acc.student.full_name
    return "Administrator" if user.get("is_owner") else user.get("email", "")


def company_tz(db: Session, company_id: int) -> ZoneInfo:
    from modules.company.models import Company
    name = db.query(Company.timezone).filter(Company.id == company_id).scalar() or "Asia/Kolkata"
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Asia/Kolkata")


# ── Settings ─────────────────────────────────────────────────────────

def get_settings(db: Session, company_id: int) -> dict:
    row = db.query(CommsSettings).filter(CommsSettings.company_id == company_id).first()
    cfg = dict(row.config or {}) if row else {}
    out = {**DEFAULT_SETTINGS, **cfg}
    out["office_hours"] = {**DEFAULT_SETTINGS["office_hours"], **(cfg.get("office_hours") or {})}
    return out


def save_settings(db: Session, company_id: int, data: dict) -> dict:
    row = db.query(CommsSettings).filter(CommsSettings.company_id == company_id).first()
    if row is None:
        row = CommsSettings(company_id=company_id, config={})
        db.add(row)
    cfg = dict(row.config or {})
    for key in DEFAULT_SETTINGS:
        if key in data and data[key] is not None:
            cfg[key] = data[key]
    for key in ("default_channels", "emergency_channels"):
        if key in cfg:
            cfg[key] = [c for c in cfg[key] if c in CHANNELS] or ["in_app"]
    oh = cfg.get("office_hours")
    if isinstance(oh, dict):
        for k in ("start", "end"):
            if k in oh:
                _parse_hhmm(oh[k])
    row.config = cfg
    flag_modified(row, "config")
    db.flush()
    return get_settings(db, company_id)


def _parse_hhmm(v: str) -> time:
    try:
        h, m = str(v).split(":")[:2]
        return time(int(h), int(m))
    except Exception:
        raise HTTPException(status_code=400, detail="Times must look like 08:30.")


def office_hours_state(db: Session, company_id: int, at: datetime | None = None) -> dict:
    s = get_settings(db, company_id)["office_hours"]
    if not s.get("enabled"):
        return {"enabled": False, "within": True, "label": ""}
    local = (at or now()).astimezone(company_tz(db, company_id))
    start, end = _parse_hhmm(s.get("start") or "08:00"), _parse_hhmm(s.get("end") or "16:00")
    days = s.get("days") or [1, 2, 3, 4, 5, 6]
    within = local.isoweekday() in days and start <= local.time() <= end
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    ds = sorted(int(d) for d in days if 1 <= int(d) <= 7)
    if ds and ds == list(range(ds[0], ds[-1] + 1)) and len(ds) > 1:
        dlabel = f"{names[ds[0] - 1]}–{names[ds[-1] - 1]}"
    else:
        dlabel = ", ".join(names[d - 1] for d in ds)
    return {"enabled": True, "within": within, "label": f"{dlabel} {s.get('start')}–{s.get('end')}",
            "start": s.get("start"), "end": s.get("end"), "days": ds}


# ── Audiences ────────────────────────────────────────────────────────

def clean_audience(a: dict | None) -> dict:
    a = dict(a or {})
    staff = dict(a.get("staff") or {})
    learners = dict(a.get("learners") or {})
    ints = lambda xs: sorted({int(x) for x in (xs or []) if str(x).lstrip("-").isdigit()})  # noqa: E731
    out = {
        "everyone": bool(a.get("everyone")),
        "staff": {"scope": staff.get("scope") if staff.get("scope") in ("none", "all", "filter") else "none",
                  "department_ids": ints(staff.get("department_ids")),
                  "roles": sorted({r for r in (staff.get("roles") or []) if r in STAFF_ROLES}),
                  "employee_ids": ints(staff.get("employee_ids"))},
        "learners": {"parents": bool(learners.get("parents")), "students": bool(learners.get("students")),
                     "scope": learners.get("scope") if learners.get("scope") in ("all", "programs", "sections",
                                                                                 "students") else "all",
                     "program_ids": ints(learners.get("program_ids")),
                     "section_ids": ints(learners.get("section_ids")),
                     "student_ids": ints(learners.get("student_ids"))},
    }
    return out


def audience_empty(a: dict) -> bool:
    a = clean_audience(a)
    if a["everyone"]:
        return False
    st, le = a["staff"], a["learners"]
    staff_any = st["scope"] == "all" or (st["scope"] == "filter" and (st["department_ids"] or st["roles"] or
                                                                        st["employee_ids"]))
    learner_any = (le["parents"] or le["students"]) and (
        le["scope"] == "all" or (le["scope"] == "programs" and le["program_ids"]) or
        (le["scope"] == "sections" and le["section_ids"]) or (le["scope"] == "students" and le["student_ids"]))
    return not (staff_any or learner_any)


@dataclass
class Resolved:
    staff: dict[int, Employee] = field(default_factory=dict)
    guardians: dict[int, set[int]] = field(default_factory=dict)  # guardian id -> learners that put them in
    students: set[int] = field(default_factory=set)

    @property
    def count(self) -> int:
        return len(self.staff) + len(self.guardians) + len(self.students)


def _live_learners(db: Session, company_id: int):
    sess = academics.current_session(db, company_id)
    q = (db.query(Enrollment.student_id).join(Student, Student.id == Enrollment.student_id)
         .filter(Enrollment.company_id == company_id, Enrollment.status == "enrolled", Student.status == "active",
                 Student.archived_at.is_(None)))
    if sess:
        q = q.filter(Enrollment.session_id == sess.id)
    return q


def resolve_audience(db: Session, company_id: int, audience: dict) -> Resolved:
    a = clean_audience(audience)
    out = Resolved()
    st, le = a["staff"], a["learners"]
    # Staff
    eq = db.query(Employee).filter(Employee.company_id == company_id, Employee.active.is_(True))
    if a["everyone"] or st["scope"] == "all":
        rows = eq.all()
    elif st["scope"] == "filter":
        from sqlalchemy import or_
        conds = []
        if st["department_ids"]:
            conds.append(Employee.department_id.in_(st["department_ids"]))
        if st["roles"]:
            conds.append(Employee.role.in_(st["roles"]))
        if st["employee_ids"]:
            conds.append(Employee.id.in_(st["employee_ids"]))
        rows = eq.filter(or_(*conds)).all() if conds else []
    else:
        rows = []
    out.staff = {e.id: e for e in rows}
    # Learners and their families
    parents, students = (True, True) if a["everyone"] else (le["parents"], le["students"])
    if parents or students:
        if a["everyone"] or le["scope"] == "all":
            ids = {i for (i,) in _live_learners(db, company_id).all()}
        elif le["scope"] == "programs":
            ids = {i for (i,) in _live_learners(db, company_id).filter(Enrollment.program_id.in_(le["program_ids"] or [-1])).all()}
        elif le["scope"] == "sections":
            ids = {i for (i,) in _live_learners(db, company_id).filter(Enrollment.section_id.in_(le["section_ids"] or [-1])).all()}
        else:
            ids = {i for (i,) in db.query(Student.id).filter(Student.company_id == company_id,
                                                              Student.id.in_(le["student_ids"] or [-1])).all()}
        if students:
            out.students = set(ids)
        if parents and ids:
            for sid, gid in db.query(StudentGuardian.student_id, StudentGuardian.guardian_id).filter(
                    StudentGuardian.company_id == company_id, StudentGuardian.student_id.in_(ids),
                    StudentGuardian.receives_notifications.is_(True)).all():
                out.guardians.setdefault(gid, set()).add(sid)
    return out


def audience_label(db: Session, company_id: int, audience: dict) -> str:
    a = clean_audience(audience)
    if a["everyone"]:
        return "Everyone"
    parts = []
    st, le = a["staff"], a["learners"]
    if st["scope"] == "all":
        parts.append("All staff")
    elif st["scope"] == "filter":
        bits = []
        if st["department_ids"]:
            from modules.departments.models import Department
            names = [n for (n,) in db.query(Department.department_name).filter(Department.id.in_(st["department_ids"])).all()]
            bits.append(", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else ""))
        if st["roles"]:
            bits.append(", ".join(r.title() for r in st["roles"]) + " staff")
        if st["employee_ids"]:
            n = len(st["employee_ids"])
            bits.append(f"{n} staff member{'s' if n != 1 else ''}")
        if bits:
            parts.append("Staff: " + "; ".join(bits))
    who = " and ".join(x for x, on in (("Parents", le["parents"]), ("students", le["students"])) if on)
    if who:
        who = who[0].upper() + who[1:]
        if le["scope"] == "all":
            parts.append(f"All {who.lower()}")
        elif le["scope"] == "programs":
            names = [p.name for p in db.query(Program).filter(Program.company_id == company_id,
                                                              Program.id.in_(le["program_ids"] or [-1])).order_by(Program.sort_order).all()]
            parts.append(f"{who} of " + _list(names))
        elif le["scope"] == "sections":
            secs = db.query(Section).filter(Section.company_id == company_id, Section.id.in_(le["section_ids"] or [-1])).all()
            parts.append(f"{who} of " + _list(sorted(academics.section_label(s) for s in secs)))
        else:
            n = len(le["student_ids"])
            parts.append(f"{who} of {n} learner{'s' if n != 1 else ''}")
    return "; ".join(parts)[:300] or "No one"


def _list(names: list[str]) -> str:
    if len(names) <= 3:
        return ", ".join(names)
    return ", ".join(names[:3]) + f" and {len(names) - 3} more"


def check_teacher_audience(db: Session, company_id: int, employee_id: int | None, audience: dict) -> None:
    """Teachers without comms.notices.manage may only reach families of their own sections."""
    a = clean_audience(audience)
    if a["everyone"] or a["staff"]["scope"] != "none":
        raise HTTPException(status_code=403, detail="You can send notices only to the parents and students of your own sections.")
    mine = academics.teacher_section_ids(db, company_id, employee_id)
    le = a["learners"]
    if le["scope"] == "sections" and le["section_ids"] and set(le["section_ids"]) <= mine:
        return
    if le["scope"] == "students" and le["student_ids"]:
        sess = academics.current_session(db, company_id)
        q = db.query(Enrollment.student_id).filter(Enrollment.company_id == company_id,
                                                   Enrollment.student_id.in_(le["student_ids"]),
                                                   Enrollment.section_id.in_(mine or {-1}))
        if sess:
            q = q.filter(Enrollment.session_id == sess.id)
        if {i for (i,) in q.all()} >= set(le["student_ids"]):
            return
    raise HTTPException(status_code=403, detail="You can send notices only to the parents and students of your own sections.")


# ── The portal person and what reaches them ──────────────────────────

@dataclass
class PortalCtx:
    role: str
    guardian_id: int | None
    student_id: int | None          # the signed-in learner (student role)
    children: list[int]             # learners this person may see
    sections: dict[int, int | None]  # learner -> section
    programs: dict[int, int | None]  # learner -> program

    @property
    def key(self) -> str:
        return f"g:{self.guardian_id}" if self.role == "parent" else f"s:{self.student_id}"


def portal_context(db: Session, user: dict) -> PortalCtx:
    from modules.students import service as students
    if not user.get("portal_id"):
        raise HTTPException(status_code=403, detail="This page is for parents and students.")
    ids = sorted(students.visible_student_ids(db, user) or [])
    sess = academics.current_session(db, user["company_id"])
    sections, programs = {}, {}
    if ids:
        q = db.query(Enrollment).filter(Enrollment.company_id == user["company_id"], Enrollment.student_id.in_(ids))
        if sess:
            q = q.filter(Enrollment.session_id == sess.id)
        for e in q.all():
            sections[e.student_id], programs[e.student_id] = e.section_id, e.program_id
    return PortalCtx(role=user["role"], guardian_id=user.get("guardian_id"), student_id=user.get("student_id"),
                     children=ids, sections=sections, programs=programs)


def portal_matches(audience: dict, ctx: PortalCtx, student_id: int | None = None) -> bool:
    """Does an audience include this parent / learner (optionally: through this child)?"""
    a = clean_audience(audience)
    if a["everyone"]:
        return True
    le = a["learners"]
    if ctx.role == "parent" and not le["parents"]:
        return False
    if ctx.role == "student" and not le["students"]:
        return False
    kids = [student_id] if student_id else ctx.children
    for k in kids:
        if le["scope"] == "all":
            return True
        if le["scope"] == "programs" and ctx.programs.get(k) in le["program_ids"]:
            return True
        if le["scope"] == "sections" and ctx.sections.get(k) in le["section_ids"]:
            return True
        if le["scope"] == "students" and k in le["student_ids"]:
            return True
    return False


# ── Who may message whom ─────────────────────────────────────────────

def staff_for_section(db: Session, company_id: int, section_id: int, subject_teachers: bool = True) -> dict[int, list[str]]:
    """Staff linked to a section: {employee id: ["Class teacher", "Mathematics", ...]}."""
    out: dict[int, list[str]] = {}
    sec = db.query(Section).filter(Section.company_id == company_id, Section.id == section_id).first()
    if sec is None:
        return out
    if sec.class_teacher_id:
        out.setdefault(sec.class_teacher_id, []).append("Class teacher")
    if subject_teachers:
        for a in db.query(SectionSubject).filter(SectionSubject.company_id == company_id,
                                                 SectionSubject.section_id == section_id).all():
            name = a.subject.name if a.subject else "Subject"
            for t in [a.teacher_id, *(a.assistant_ids or [])]:
                if t and name not in out.setdefault(t, []):
                    out[t].append(name)
    return out


def messageable_staff(db: Session, company_id: int, ctx: PortalCtx, student_id: int | None = None) -> list[dict]:
    """The staff a parent (about one child, or any child) or a learner may write to."""
    settings = get_settings(db, company_id)
    kids = [student_id] if student_id else ctx.children
    found: dict[int, dict] = {}
    for k in kids:
        sec = ctx.sections.get(k)
        if not sec:
            continue
        for emp_id, roles in staff_for_section(db, company_id, sec, settings.get("subject_teachers", True)).items():
            item = found.setdefault(emp_id, {"employee_id": emp_id, "roles": [], "student_ids": []})
            for r in roles:
                if r not in item["roles"]:
                    item["roles"].append(r)
            if k not in item["student_ids"]:
                item["student_ids"].append(k)
    if not found:
        return []
    emps = {e.id: e for e in db.query(Employee).filter(Employee.company_id == company_id, Employee.id.in_(found),
                                                       Employee.active.is_(True)).all()}
    out = []
    for emp_id, item in found.items():
        e = emps.get(emp_id)
        if e is None:
            continue
        out.append({**item, "name": employee_name(e), "photo_url": e.profile_image_url
                    if (e.profile_image_url or "").startswith("http") else None,
                    "is_class_teacher": "Class teacher" in item["roles"]})
    out.sort(key=lambda x: (not x["is_class_teacher"], x["name"]))
    return out


def can_portal_message(db: Session, company_id: int, ctx: PortalCtx, employee_id: int | None,
                       to_office: bool = False, student_id: int | None = None) -> bool:
    settings = get_settings(db, company_id)
    if ctx.role == "student" and not settings.get("students_can_message", True):
        return False
    if ctx.role == "parent" and not settings.get("parents_can_message", True):
        return False
    if to_office:
        return bool(settings.get("office_inbox", True))
    return any(s["employee_id"] == employee_id for s in messageable_staff(db, company_id, ctx, student_id))


def staff_can_message_student(db: Session, user: dict, student_id: int) -> bool:
    """Staff may write to a learner's family when they teach the learner's section, or with
    comms.messages.all."""
    if has(db, user, "comms.messages.all"):
        return True
    from modules.students import service as students
    sec = students.student_section_id(db, user["company_id"], student_id)
    return bool(sec and sec in academics.teacher_section_ids(db, user["company_id"], user.get("employee_id")))


def guardians_of(db: Session, company_id: int, student_id: int) -> list[tuple[Guardian, StudentGuardian]]:
    return [(l.guardian, l) for l in db.query(StudentGuardian).filter(
        StudentGuardian.company_id == company_id, StudentGuardian.student_id == student_id).all()]


def portal_ids_for(db: Session, company_id: int, guardian_ids=(), student_ids=()) -> dict[str, int]:
    """{"g:<id>" | "s:<id>": portal account id} for active sign-ins."""
    out: dict[str, int] = {}
    if guardian_ids:
        for pid, gid in db.query(PortalAccount.id, PortalAccount.guardian_id).filter(
                PortalAccount.company_id == company_id, PortalAccount.guardian_id.in_(list(guardian_ids)),
                PortalAccount.active.is_(True)).all():
            out[f"g:{gid}"] = pid
    if student_ids:
        for pid, sid in db.query(PortalAccount.id, PortalAccount.student_id).filter(
                PortalAccount.company_id == company_id, PortalAccount.student_id.in_(list(student_ids)),
                PortalAccount.kind == "student", PortalAccount.active.is_(True)).all():
            out[f"s:{sid}"] = pid
    return out
