"""Timetable API: bell schedules, the builder grid, clash checks, teacher and room views,
copy / publish / generate, substitutions, home summaries and the parent / student portal."""
from datetime import date
from datetime import date as Date
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core import audit
from core.db import get_db
from core.errors import BadRequest, NotFound
from core.pagination import PageParams, paginate
from core.security import _get_role_permissions, get_current_user, require_permission, require_portal
from core.timeutil import today_for_company_id
from modules.academics import service as academics
from modules.academics.models import Program, Section
from modules.timetable import service
from modules.timetable.models import BellSchedule, Substitution

router = APIRouter(prefix="/timetable", tags=["Timetable"])
VIEW, MANAGE = "timetable.view", "timetable.manage"


def _perms(db: Session, user: dict) -> list[str]:
    return _get_role_permissions(user["company_id"], user["role"], db)


# ── Settings ─────────────────────────────────────────────────────────

class SettingsIn(BaseModel):
    max_periods_per_day: int | None = Field(None, ge=1, le=20)
    max_weekly_load: int | None = Field(None, ge=1, le=80)
    max_consecutive: int | None = Field(None, ge=1, le=20)
    notify_on_publish: bool | None = None
    notify_substitute: bool | None = None
    show_teacher_in_portal: bool | None = None


@router.get("/settings")
def get_settings(db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    return {**service.get_settings(db, user["company_id"]), "week_days": service.week_days(db, user["company_id"])}


@router.put("/settings")
def put_settings(payload: SettingsIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    out = service.save_settings(db, user["company_id"], payload.model_dump(exclude_unset=True))
    db.commit()
    return out


# ── Bell schedules ───────────────────────────────────────────────────

class PeriodIn(BaseModel):
    name: str | None = Field(None, max_length=60)
    kind: str = Field("class", pattern="^(class|break|assembly)$")
    start: str = Field(..., max_length=5)
    end: str = Field(..., max_length=5)


class ScheduleIn(BaseModel):
    name: str | None = Field(None, max_length=80)
    weekdays: list[int] | None = None
    program_ids: list[int] | None = None
    is_default: bool | None = None
    active: bool | None = None
    periods: list[PeriodIn] | None = None


def _program_names(db: Session, cid: int) -> dict[int, str]:
    return {i: n for i, n in db.query(Program.id, Program.name).filter(Program.company_id == cid)}


def _schedule(db: Session, cid: int, uuid: UUID) -> BellSchedule:
    row = db.query(BellSchedule).filter(BellSchedule.company_id == cid, BellSchedule.uuid == uuid).first()
    if row is None:
        raise NotFound("Bell schedule")
    return row


@router.get("/bell-schedules")
def list_schedules(include_inactive: bool = False, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Bell schedules with their periods. Open to every staff member (times are not private)."""
    cid = user["company_id"]
    if user.get("portal_id"):
        raise HTTPException(status_code=403, detail="Permission denied")
    names = _program_names(db, cid)
    return [service.schedule_out(s, names) for s in service.list_schedules(db, cid, active_only=not include_inactive)]


@router.post("/bell-schedules", status_code=201)
def create_schedule(payload: ScheduleIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    data = payload.model_dump(exclude_unset=True)
    if data.get("periods") is None:
        raise BadRequest("Add the periods of the day.")
    data["periods"] = [p for p in data["periods"]]
    row = service.save_schedule(db, user["company_id"], data)
    db.commit()
    db.refresh(row)
    return service.schedule_out(row, _program_names(db, user["company_id"]))


@router.put("/bell-schedules/{uuid}")
def update_schedule(uuid: UUID, payload: ScheduleIn, db: Session = Depends(get_db),
                    user: dict = Depends(require_permission(MANAGE))):
    row = _schedule(db, user["company_id"], uuid)
    row = service.save_schedule(db, user["company_id"], payload.model_dump(exclude_unset=True), row)
    db.commit()
    db.refresh(row)
    return service.schedule_out(row, _program_names(db, user["company_id"]))


@router.delete("/bell-schedules/{uuid}")
def archive_schedule(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    row = _schedule(db, user["company_id"], uuid)
    row.active = False
    db.commit()
    return {"detail": "Switched off"}


# ── Builder ──────────────────────────────────────────────────────────

@router.get("/sections")
def sections_overview(db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    """Every section of the current session with how full its timetable is and whether it is published."""
    from modules.timetable.models import TimetableEntry, TimetablePublication
    from sqlalchemy import func
    cid = user["company_id"]
    sess = academics.current_session(db, cid)
    if sess is None:
        return []
    secs = (db.query(Section).join(Program, Program.id == Section.program_id)
            .filter(Section.company_id == cid, Section.session_id == sess.id, Section.active.is_(True))
            .order_by(Program.sort_order, Program.name, Section.name).all())
    counts = dict(db.query(TimetableEntry.section_id, func.count(TimetableEntry.id)).filter(
        TimetableEntry.company_id == cid, TimetableEntry.active.is_(True), TimetableEntry.session_id == sess.id)
        .group_by(TimetableEntry.section_id).all())
    pubs = {p.section_id: p for p in db.query(TimetablePublication).filter(TimetablePublication.company_id == cid)}
    return [{"id": s.id, "uuid": str(s.uuid), "label": academics.section_label(s), "program_id": s.program_id,
             "entries": counts.get(s.id, 0), **service.publication_out(pubs.get(s.id))} for s in secs]


@router.get("/grid")
def section_grid(section_id: int, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    return service.section_grid(db, user["company_id"], section_id)


class EntryIn(BaseModel):
    section_id: int | None = None
    section_ids: list[int] | None = None  # several = a combined class
    weekday: int | None = Field(None, ge=1, le=7)
    period_no: int | None = Field(None, ge=1, le=20)
    subject_id: int | None = None
    teacher_id: int | None = None
    room_id: int | None = None
    elective_group: str | None = Field(None, max_length=60)
    note: str | None = Field(None, max_length=200)
    force: bool = False


@router.post("/entries", status_code=201)
def create_entry(payload: EntryIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    rows = service.save_entry(db, user["company_id"], payload.model_dump(exclude_unset=True))
    db.commit()
    return [service.entry_out(service.get_entry(db, user["company_id"], r.uuid)) for r in rows]


@router.put("/entries/{uuid}")
def update_entry(uuid: UUID, payload: EntryIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    row = service.get_entry(db, user["company_id"], uuid)
    service.save_entry(db, user["company_id"], payload.model_dump(exclude_unset=True), row)
    db.commit()
    return service.entry_out(service.get_entry(db, user["company_id"], uuid))


@router.delete("/entries/{uuid}")
def delete_entry(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    row = service.get_entry(db, user["company_id"], uuid)
    n = service.remove_entry(db, user["company_id"], row)
    db.commit()
    return {"removed": n}


@router.get("/clashes")
def clashes(db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    cid = user["company_id"]
    return {"clashes": service.find_clashes(db, cid), "loads": service.teacher_loads(db, cid)}


@router.get("/teacher")
def teacher_view(employee_id: int, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    return service.teacher_week(db, user["company_id"], employee_id)


@router.get("/room")
def room_view(room_id: int, db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    return service.room_week(db, user["company_id"], room_id)


@router.get("/teachers")
def teacher_options(db: Session = Depends(get_db), user: dict = Depends(require_permission(VIEW))):
    """Everyone who teaches (has an allocation or a period), for pickers."""
    return [{"id": t.id, "name": f"{t.first_name} {t.last_name}".strip()} for t in service._teacher_pool(db, user["company_id"])]


class CopyIn(BaseModel):
    from_section_id: int
    to_section_ids: list[int] = Field(..., min_length=1)
    teachers: str = Field("allocation", pattern="^(allocation|same|none)$")


@router.post("/copy")
def copy(payload: CopyIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    out = service.copy_timetable(db, user["company_id"], payload.from_section_id, payload.to_section_ids, payload.teachers)
    db.commit()
    return out


class SectionsIn(BaseModel):
    section_ids: list[int] = Field(..., min_length=1)
    replace: bool = False


@router.post("/publish")
def publish(payload: SectionsIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    out = service.publish(db, user["company_id"], payload.section_ids)
    db.commit()
    return out


@router.post("/generate")
def generate(payload: SectionsIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    out = service.generate(db, user["company_id"], payload.section_ids, replace=payload.replace)
    db.commit()
    return out


# ── My timetable (any teacher) ───────────────────────────────────────

@router.get("/mine")
def my_week(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """The signed-in staff member's weekly timetable (published sections only)."""
    if not user.get("employee_id"):
        if user.get("portal_id"):
            raise HTTPException(status_code=403, detail="Permission denied")
        return {"teacher": None, "days": service._default_days(db, user["company_id"]), "entries": [], "weekly": 0}
    return service.teacher_week(db, user["company_id"], user["employee_id"], published_only=True)


@router.get("/mine/day")
def my_day(on: date | None = None, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    if user.get("portal_id"):
        raise HTTPException(status_code=403, detail="Permission denied")
    cid = user["company_id"]
    on = on or today_for_company_id(db, cid)
    if not user.get("employee_id"):
        return {"date": on.isoformat(), "classes": []}
    return {"date": on.isoformat(), "classes": service.teacher_day(db, cid, user["employee_id"], on)}


@router.get("/summary")
def summary(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Home card: the signed-in teacher's classes today, and (for timetable managers) the
    number of uncovered periods of absent teachers."""
    if user.get("portal_id"):
        raise HTTPException(status_code=403, detail="Permission denied")
    cid = user["company_id"]
    today = today_for_company_id(db, cid)
    classes = service.teacher_day(db, cid, user["employee_id"], today) if user.get("employee_id") else []
    out = {"date": today.isoformat(), "classes": classes, "uncovered": None}
    if MANAGE in _perms(db, user):
        absent = service.absent_teachers(db, cid, today)
        out["uncovered"] = sum(a["periods"] - a["covered"] for a in absent)
        out["absent_teachers"] = len(absent)
    return out


# ── Substitutions ────────────────────────────────────────────────────

@router.get("/substitutions/absent")
def absent_teachers(on: date, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    return service.absent_teachers(db, user["company_id"], on)


@router.get("/substitutions/periods")
def periods_of(on: date, teacher_id: int, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    return service.teacher_periods_on(db, user["company_id"], teacher_id, on)


@router.get("/substitutions/free")
def free(on: date, period_no: int, entry_uuid: UUID | None = None, db: Session = Depends(get_db),
         user: dict = Depends(require_permission(MANAGE))):
    entry = service.get_entry(db, user["company_id"], entry_uuid) if entry_uuid else None
    return service.free_teachers(db, user["company_id"], on, period_no, entry)


class SubstitutionIn(BaseModel):
    entry_uuid: UUID
    date: Date
    kind: str = Field("substitute", pattern="^(substitute|cancelled)$")
    substitute_teacher_id: int | None = None
    note: str | None = Field(None, max_length=300)
    force: bool = False


@router.post("/substitutions", status_code=201)
def assign(payload: SubstitutionIn, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    cid = user["company_id"]
    entry = service.get_entry(db, cid, payload.entry_uuid)
    row = service.assign_substitution(db, cid, entry, payload.date, payload.kind, payload.substitute_teacher_id,
                                      payload.note, payload.force)
    db.commit()
    db.refresh(row)
    return service.substitution_out(row)


@router.get("/substitutions")
def register(date_from: date | None = None, date_to: date | None = None, teacher_id: int | None = None,
             q: str | None = None, params: PageParams = Depends(), db: Session = Depends(get_db),
             user: dict = Depends(require_permission(MANAGE))):
    query = service.substitutions_query(db, user["company_id"], date_from, date_to, teacher_id)
    if q:
        from modules.employees.models import Employee
        from sqlalchemy import func, or_
        like = f"%{q.strip()}%"
        ids = [i for (i,) in db.query(Employee.id).filter(
            Employee.company_id == user["company_id"],
            func.concat(Employee.first_name, " ", Employee.last_name).ilike(like))]
        query = query.filter(or_(Substitution.substitute_teacher_id.in_(ids or [0]),
                                 Substitution.absent_teacher_id.in_(ids or [0])))
    return paginate(query, params, service.substitution_out)


@router.get("/substitutions/counts")
def counts(date_from: date, date_to: date, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    return service.substitution_counts(db, user["company_id"], date_from, date_to)


@router.delete("/substitutions/{uuid}")
def cancel_substitution(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(require_permission(MANAGE))):
    row = db.query(Substitution).filter(Substitution.company_id == user["company_id"], Substitution.uuid == uuid).first()
    if row is None:
        raise NotFound("Substitution")
    row.status = "cancelled"
    audit.event(db, "timetable.substitution_removed", "timetable_substitutions", row.id, {"date": row.date.isoformat()})
    db.commit()
    return {"detail": "Removed"}


# ── Parent and student portal ────────────────────────────────────────

def _portal_section(db: Session, user: dict, student_id: int) -> int | None:
    from modules.students import service as students
    students.require_student_access(db, user, student_id)
    return students.student_section_id(db, user["company_id"], student_id)


@router.get("/portal/week")
def portal_week(student_id: int, db: Session = Depends(get_db), user: dict = Depends(require_portal("parent", "student"))):
    """A child's weekly timetable (published only), with today's weekday."""
    cid = user["company_id"]
    section_id = _portal_section(db, user, student_id)
    today = today_for_company_id(db, cid)
    if not section_id or section_id not in service.published_section_ids(db, cid):
        return {"published": False, "days": [], "entries": [], "today": today.isoformat(), "weekday": today.isoweekday()}
    grid = service.section_grid(db, cid, section_id)
    show = service.get_settings(db, cid)["show_teacher_in_portal"]
    entries = grid["entries"]
    if not show:
        for e in entries:
            e["teacher_id"], e["teacher_name"] = None, None
    for e in entries:
        e.pop("teacher_id", None)
    week = service.week_dates(today)
    return {"published": True, "section_label": grid["section"]["label"], "days": grid["days"], "entries": entries,
            "today": today.isoformat(), "weekday": today.isoweekday(),
            "dates": {str(d.isoweekday()): d.isoformat() for d in week},
            "published_at": grid["publication"]["published_at"]}


@router.get("/portal/summary")
def portal_summary(student_id: int, db: Session = Depends(get_db), user: dict = Depends(require_portal("parent", "student"))):
    """Home card: the child's periods today."""
    cid = user["company_id"]
    section_id = _portal_section(db, user, student_id)
    today = today_for_company_id(db, cid)
    periods = service.section_day(db, cid, section_id, today, published_only=True) if section_id else []
    show = service.get_settings(db, cid)["show_teacher_in_portal"]
    for p in periods:
        p.pop("teacher_id", None)
        if not show:
            p["teacher_name"] = None
        if p.get("substitution"):
            p["substitution"].pop("teacher_id", None)
            if not show:
                p["substitution"]["teacher_name"] = None
    return {"date": today.isoformat(), "periods": periods}
