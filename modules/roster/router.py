from datetime import date
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import extract
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from core.db import get_db
from core.security import require_permission
from modules.employees.models import Employee
from modules.roster.models import RosterEntry
from modules.roster.schemas import RosterBulkResponse, RosterEntryCreate, RosterEntryResponse

router = APIRouter(prefix="/roster", tags=["Roster"])

STATUSES = {"working", "off", "leave", "half_day"}


def _employee(db: Session, company_id: int, uuid) -> Employee:
    emp = db.query(Employee).filter(Employee.uuid == uuid, Employee.company_id == company_id).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    return emp


def _response(entry: RosterEntry, name: str) -> RosterEntryResponse:
    return RosterEntryResponse(
        id=entry.id,
        employee_id=entry.employee_id,
        date=entry.date,
        status=entry.status,
        timing_override=entry.timing_override,
        note=entry.note,
        created_at=entry.created_at,
        employee_name=name,
    )


@router.get("/", response_model=RosterBulkResponse)
def get_roster(
    start_date: date = Query(...),
    end_date: date = Query(...),
    employee_uuid: Optional[str] = None,
    current_user: dict = Depends(require_permission("time.roster.view")),
    db: Session = Depends(get_db),
):
    """Roster entries for a date range, for everyone or one employee."""
    query = (
        db.query(
            RosterEntry,
            (Employee.first_name + " " + Employee.last_name).label("employee_name"),
            Employee.uuid.label("employee_uuid"),
        )
        .join(Employee, RosterEntry.employee_id == Employee.id)
        .filter(
            Employee.company_id == current_user["company_id"],
            RosterEntry.date >= start_date,
            RosterEntry.date <= end_date,
        )
    )
    if employee_uuid:
        query = query.filter(Employee.uuid == employee_uuid)

    entries, names = [], {}
    for entry, name, emp_uuid in query.order_by(RosterEntry.date, Employee.first_name).all():
        entries.append(_response(entry, name))
        names[str(emp_uuid)] = name
    return RosterBulkResponse(entries=entries, employees=names)


@router.post("/", response_model=RosterEntryResponse)
def set_roster_entry(
    data: RosterEntryCreate,
    current_user: dict = Depends(require_permission("time.roster.manage")),
    db: Session = Depends(get_db),
):
    """Create or replace one employee's plan for one day."""
    if data.status not in STATUSES:
        raise HTTPException(status_code=400, detail="Status must be working, off, leave or half_day")
    emp = _employee(db, current_user["company_id"], data.employee_uuid)
    entry = db.query(RosterEntry).filter(RosterEntry.employee_id == emp.id, RosterEntry.date == data.date).first()
    if entry is None:
        entry = RosterEntry(employee_id=emp.id, date=data.date)
        db.add(entry)
    entry.status = data.status
    entry.timing_override = data.timing_override
    entry.note = data.note
    # set from the employee profile: live straight away
    entry.published = True
    from datetime import datetime, timezone
    entry.published_at = datetime.now(timezone.utc)
    db.flush()
    from modules.roster import service as roster_service
    roster_service._recompute(db, current_user["company_id"], {emp.id: [data.date]})
    db.commit()
    db.refresh(entry)
    return _response(entry, f"{emp.first_name} {emp.last_name}")


@router.delete("/{employee_uuid}/{roster_date}")
def delete_roster_entry(
    employee_uuid: UUID,
    roster_date: date,
    current_user: dict = Depends(require_permission("time.roster.manage")),
    db: Session = Depends(get_db),
):
    emp = _employee(db, current_user["company_id"], employee_uuid)
    entry = db.query(RosterEntry).filter(RosterEntry.employee_id == emp.id, RosterEntry.date == roster_date).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Roster entry not found")
    db.delete(entry)
    db.commit()
    return {"message": "Roster entry deleted"}


@router.get("/stats/{employee_uuid}")
def roster_stats(
    employee_uuid: UUID,
    year: int = Query(...),
    month: int = Query(..., ge=1, le=12),
    current_user: dict = Depends(require_permission("time.roster.view")),
    db: Session = Depends(get_db),
):
    """How many days of each status one employee has in a month."""
    emp = _employee(db, current_user["company_id"], employee_uuid)
    rows = (
        db.query(RosterEntry.status, sa_func.count(RosterEntry.id))
        .filter(
            RosterEntry.employee_id == emp.id,
            extract("year", RosterEntry.date) == year,
            extract("month", RosterEntry.date) == month,
        )
        .group_by(RosterEntry.status)
        .all()
    )
    return {
        "employee_uuid": str(employee_uuid),
        "employee_name": f"{emp.first_name} {emp.last_name}",
        "year": year,
        "month": month,
        "stats": {s: c for s, c in rows},
    }


# ── Shift roster (grid, assign, patterns, publish) ──────────────────

from datetime import timedelta  # noqa: E402

from pydantic import BaseModel, Field  # noqa: E402

from modules.attendance import service as att  # noqa: E402
from modules.roster import service as roster_service  # noqa: E402


def _ids(db, current_user):
    return None if att.has_perm(db, current_user, "time.attendance.manage") else \
        [current_user.get("employee_id"), *att.report_ids(db, current_user["company_id"], current_user.get("employee_id"))]


@router.get("/grid")
def roster_grid(start_date: date, end_date: date, department_id: Optional[int] = None, team_id: Optional[int] = None,
                q: Optional[str] = None, current_user: dict = Depends(require_permission("time.roster.view")),
                db: Session = Depends(get_db)):
    """Everyone's plan day by day: roster entries (drafts included) over their usual week."""
    can_manage = att.has_perm(db, current_user, "time.roster.manage")
    ids = _ids(db, current_user) if can_manage else [current_user.get("employee_id")]
    out = roster_service.grid(db, current_user["company_id"], start_date, end_date, ids, department_id, team_id, q,
                              published_only=not can_manage)
    from modules.shifts import adapters
    out["teams"] = adapters.teams(db, current_user["company_id"])
    out["can_manage"] = can_manage
    return out


class AssignIn(BaseModel):
    employee_ids: list[int] = Field(default_factory=list)
    team_ids: list[int] = Field(default_factory=list)
    department_ids: list[int] = Field(default_factory=list)
    dates: list[date] | None = None
    start_date: date | None = None
    end_date: date | None = None
    weekdays: list[int] | None = None
    shift_id: Optional[int] = None
    status: str = "working"
    mode: str = "replace"
    note: Optional[str] = Field(None, max_length=255)
    publish: bool = False


def _check_scope(db, current_user, data: dict):
    ids = _ids(db, current_user)
    if ids is not None:
        wanted = set(data.get("employee_ids") or [])
        if data.get("team_ids") or data.get("department_ids") or not wanted <= set(ids):
            raise HTTPException(status_code=403, detail="You can plan shifts only for your own team.")


@router.post("/assign")
def roster_assign(req: AssignIn, current_user: dict = Depends(require_permission("time.roster.manage")),
                  db: Session = Depends(get_db)):
    data = req.model_dump()
    _check_scope(db, current_user, data)
    out = roster_service.assign(db, current_user["company_id"], data, publish=req.publish)
    db.commit()
    return out


@router.post("/check")
def roster_check(req: AssignIn, current_user: dict = Depends(require_permission("time.roster.manage")),
                 db: Session = Depends(get_db)):
    """What assigning would do, without saving: blocked days and warnings."""
    data = req.model_dump()
    _check_scope(db, current_user, data)
    out = roster_service.assign(db, current_user["company_id"], data, dry_run=True)
    db.rollback()
    return out


class PatternApplyIn(BaseModel):
    pattern_uuid: str
    employee_ids: list[int] = Field(default_factory=list)
    team_ids: list[int] = Field(default_factory=list)
    department_ids: list[int] = Field(default_factory=list)
    start_date: date
    end_date: date
    mode: str = "replace"
    dry_run: bool = False


@router.post("/apply-pattern")
def roster_apply_pattern(req: PatternApplyIn, current_user: dict = Depends(require_permission("time.roster.manage")),
                         db: Session = Depends(get_db)):
    data = req.model_dump()
    _check_scope(db, current_user, data)
    out = roster_service.apply_pattern(db, current_user["company_id"], data, dry_run=req.dry_run)
    if req.dry_run:
        db.rollback()
    else:
        db.commit()
    return out


class RangeIn(BaseModel):
    start_date: date
    end_date: date
    employee_ids: list[int] = Field(default_factory=list)


@router.post("/clear")
def roster_clear(req: RangeIn, current_user: dict = Depends(require_permission("time.roster.manage")),
                 db: Session = Depends(get_db)):
    _check_scope(db, current_user, {"employee_ids": req.employee_ids})
    if not req.employee_ids:
        raise HTTPException(status_code=400, detail="Choose whose roster to clear.")
    n = roster_service.clear(db, current_user["company_id"], req.employee_ids, req.start_date, req.end_date)
    db.commit()
    return {"cleared": n}


@router.post("/publish")
def roster_publish(req: RangeIn, current_user: dict = Depends(require_permission("time.roster.manage")),
                   db: Session = Depends(get_db)):
    ids = _ids(db, current_user)
    emp_ids = req.employee_ids or ids
    if ids is not None and not set(emp_ids or []) <= set(ids):
        raise HTTPException(status_code=403, detail="You can publish only your own team's roster.")
    if (req.end_date - req.start_date).days > 92:
        raise HTTPException(status_code=400, detail="Publish up to three months at a time.")
    n = roster_service.publish(db, current_user["company_id"], req.start_date, req.end_date, emp_ids or None)
    db.commit()
    return {"published": n}
