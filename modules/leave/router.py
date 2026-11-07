"""Leave API under /api/v1/leave.

Self-service   GET  /leave/access, /leave/me/summary, /leave/me/holidays
               POST /leave/requests/preview, /leave/requests
               GET  /leave/requests?scope=mine|team|all, /leave/requests/{uuid}
               PUT  /leave/requests/{uuid}                 change a pending request
               POST /leave/requests/{uuid}/cancel          withdraw, cancel all or some days
               POST /leave/requests/{uuid}/extend
               GET  /leave/requests/{uuid}/attachment
               GET/POST /leave/comp-offs
Team           GET  /leave/team/calendar?month=YYYY-MM
Admin          GET  /leave/balances, /leave/balances/{employee}/ledger, /leave/balances/{employee}
               POST /leave/balances/adjust, /leave/balances/import
               POST /leave/runs/accrual, /leave/runs/expiry, /leave/runs/year-end (preview flag)
               GET  /leave/runs
Settings       /leave/settings, /leave/types, /leave/holiday-calendars, /leave/holidays, /leave/work-weeks
Reports        /leave/reports/balances, /taken, /lop, /absentees  (?format=csv)
Calendar       GET  /leave/calendar/days?employee=&from=&to=    (working days, holidays)
"""
from __future__ import annotations

import base64
import datetime as _dt
from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core import audit
from core.db import get_db
from core.errors import BadRequest, Forbidden
from core.security import get_current_user, require_permission
from core.timeutil import iso_utc, today_for_company_id
from modules.leave import defaults, holidays as hol, ledger, policy, reports, service, views
from modules.leave.calendar import PRESETS, WEEKDAYS, WorkCalendar
from modules.leave.facts import EXPRESSION_FIELDS
from modules.leave.models import LeaveRun

router = APIRouter(prefix="/leave", tags=["Leave"])

VIEW = require_permission("time.leave.view")
MANAGE = require_permission("time.leave.manage")
CONFIGURE = require_permission("settings.leave.manage")


def _acc(db: Session, user: dict) -> dict:
    return views.access(db, user)


def _me(db: Session, user: dict):
    if not user.get("employee_id"):
        raise BadRequest("Sign in as an employee to see your own leave.")
    return service.get_employee(db, user["company_id"], user["employee_id"])


def _csv(text: str, name: str) -> Response:
    return Response(content=text, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})


def _date(v: str | None, default: date | None = None) -> date | None:
    if not v:
        return default
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise BadRequest("Dates must look like 2026-11-07.")


# ── Schemas ─────────────────────────────────────────────────────────

class ApplyIn(BaseModel):
    leave_type_uuid: UUID
    from_date: date
    to_date: date | None = None
    unit: str = "day"
    half: str | None = None
    hours: float | None = None
    reason: str | None = Field(None, max_length=2000)
    grant_option: str | None = None
    attachment: str | None = None
    attachment_name: str | None = None
    employee_uuid: UUID | None = None  # HR applying for someone


class ModifyIn(BaseModel):
    from_date: date | None = None
    to_date: date | None = None
    unit: str | None = None
    half: str | None = None
    hours: float | None = None
    reason: str | None = Field(None, max_length=2000)
    grant_option: str | None = None
    attachment: str | None = None
    attachment_name: str | None = None


class CancelIn(BaseModel):
    dates: list[str] | None = None
    reason: str | None = Field(None, max_length=500)


class ExtendIn(BaseModel):
    to_date: date
    reason: str | None = Field(None, max_length=1000)


class CompOffIn(BaseModel):
    worked_date: date
    unit: str = "day"
    hours: float | None = None
    reason: str | None = Field(None, max_length=2000)
    employee_uuid: UUID | None = None


class AdjustIn(BaseModel):
    employee_uuid: UUID
    leave_type_uuid: UUID
    days: float
    reason: str = Field(..., max_length=300)
    year: int | None = None
    kind: str = "adjustment"  # adjustment | opening


class ImportIn(BaseModel):
    csv: str = Field(..., max_length=2_000_000)
    dry_run: bool = False


class RunIn(BaseModel):
    as_of: date | None = None
    year: int | None = None
    apply: bool = False
    leave_type_uuids: list[UUID] | None = None


class TypeIn(BaseModel):
    code: str | None = None
    name: str | None = None
    color: str | None = None
    icon: str | None = None
    description: str | None = None
    kind: str | None = None
    sort_order: int | None = None
    rules: dict[str, Any] | None = None
    effective_from: date | None = None
    change_note: str | None = Field(None, max_length=300)


class CalendarIn(BaseModel):
    name: str | None = None
    description: str | None = None
    applicability: dict[str, Any] | None = None
    is_default: bool | None = None
    priority: int | None = None
    optional_limit: int | None = None
    active: bool | None = None
    location_ids: list[int] | None = None
    copy_from_uuid: UUID | None = None


class HolidayIn(BaseModel):
    name: str | None = None
    date: _dt.date | None = None
    end_date: _dt.date | None = None
    kind: str | None = None
    description: str | None = None
    calendar_uuids: list[UUID] | None = None


class PatternIn(BaseModel):
    name: str | None = None
    description: str | None = None
    days: list[dict[str, Any]] | None = None
    applicability: dict[str, Any] | None = None
    is_default: bool | None = None
    priority: int | None = None
    active: bool | None = None


# ── Access and self-service ─────────────────────────────────────────

@router.get("/access")
def get_access(db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    acc = _acc(db, user)
    s = policy.get_settings(db, user["company_id"])
    return {**acc, "settings": {"comp_off": s["comp_off"]["enabled"], "extension": s["extension"]["enabled"],
                                "partial_cancel": s["cancellation"]["partial"],
                                "cancel_reason_required": s["cancellation"]["reason_required"],
                                "extension_reason_required": s["extension"]["reason_required"]},
            "today": today_for_company_id(db, user["company_id"]).isoformat()}


@router.get("/me/summary")
def my_summary(year: int | None = None, employee_uuid: UUID | None = None, db: Session = Depends(get_db),
               user: dict = Depends(VIEW)):
    acc = _acc(db, user)
    if employee_uuid:
        emp = service.get_employee_by_uuid(db, user["company_id"], employee_uuid)
        ids = views.visible_ids(db, user["company_id"], acc)
        if ids is not None and emp.id not in ids:
            raise Forbidden("You can't see this person's leave.")
    else:
        emp = _me(db, user)
    return views.my_summary(db, user["company_id"], emp, year, include_hidden=acc["can_manage"])


@router.get("/me/holidays")
def my_holidays(year: int | None = None, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    if not user.get("employee_id"):
        cals = hol.list_calendars(db, user["company_id"])
        default = next((c for c in cals if c.is_default), None)
        y = year or today_for_company_id(db, user["company_id"]).year
        return {"calendar": {"uuid": str(default.uuid), "name": default.name} if default else None,
                "optional_limit": default.optional_limit if default else 0, "optional_taken": 0,
                "holidays": [h for h in hol.list_holidays(db, user["company_id"], default, y)] if default else []}
    emp = _me(db, user)
    return hol.my_holidays(db, user["company_id"], emp, year or today_for_company_id(db, user["company_id"]).year)


def _target(db: Session, user: dict, acc: dict, employee_uuid) -> tuple[Any, bool]:
    if employee_uuid and (not user.get("employee_id") or str(employee_uuid) != str(
            service.get_employee(db, user["company_id"], user["employee_id"]).uuid)):
        if not acc["can_manage"]:
            raise Forbidden("Only HR can apply for someone else.")
        return service.get_employee_by_uuid(db, user["company_id"], employee_uuid), True
    return _me(db, user), False


@router.post("/requests/preview")
def preview(payload: ApplyIn, request_uuid: UUID | None = None, db: Session = Depends(get_db),
            user: dict = Depends(VIEW)):
    cid = user["company_id"]
    acc = _acc(db, user)
    exclude, parent = None, None
    if request_uuid:
        req = service.get_request(db, cid, request_uuid)
        emp, on_behalf = req.employee, not (acc["employee_id"] == req.employee_id)
        exclude = req.id
    else:
        emp, on_behalf = _target(db, user, acc, payload.employee_uuid)
    t = policy.get_type(db, cid, payload.leave_type_uuid)
    data = payload.model_dump()
    data["has_attachment"] = bool(payload.attachment)
    try:
        ev = service.evaluate(db, cid, emp, t, data, exclude_id=exclude, enforce=False, parent=parent)
    except BadRequest as e:
        return {"ok": False, "errors": [e.detail], "warnings": [], "days": 0, "items": []}
    team = views.team_on_leave(db, cid, emp, ev["from_date"], ev["to_date"])
    errors = ev["errors"]
    if on_behalf:
        errors = [e for e in errors if "already have" in e or "available" in e]
    return {
        "ok": not errors, "errors": errors, "warnings": ev["warnings"],
        "days": float(ev["days"]), "paid_days": float(ev["paid_days"]), "lop_days": float(ev["lop_days"]),
        "sandwich_days": float(ev["sandwich_days"]), "document_needed": ev["document_needed"],
        "items": service._items_json(ev["items"]), "balance": ev["balance"], "team_on_leave": team,
        "on_behalf": on_behalf,
    }


@router.post("/requests", status_code=201)
def apply_leave(payload: ApplyIn, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    cid = user["company_id"]
    acc = _acc(db, user)
    emp, on_behalf = _target(db, user, acc, payload.employee_uuid)
    t = policy.get_type(db, cid, payload.leave_type_uuid)
    req = service.apply(db, cid, emp, t, payload.model_dump(), on_behalf=on_behalf)
    db.commit()
    return views.request_detail(db, cid, service.get_request(db, cid, req.uuid), acc)


@router.get("/requests")
def list_requests(scope: str = "mine", status: str | None = None, leave_type: str | None = None,
                  q: str | None = None, from_date: str | None = Query(None, alias="from"),
                  to_date: str | None = Query(None, alias="to"), employee_uuid: UUID | None = None,
                  department_id: int | None = None, page: int = Query(1, ge=1), size: int = Query(25, ge=1, le=200),
                  db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    acc = _acc(db, user)
    if scope == "all" and not acc["can_manage"]:
        scope = "team"
    if scope == "team" and not (acc["can_approve"] or acc["can_manage"]):
        raise Forbidden("You don't have a team to see.")
    return views.list_requests(
        db, user["company_id"], acc, scope=scope, status=[s for s in (status or "").split(",") if s] or None,
        type_codes=[c for c in (leave_type or "").split(",") if c] or None, q=q, start=_date(from_date),
        end=_date(to_date), employee_uuid=employee_uuid, department_id=department_id, page=page, size=size)


def _visible_request(db: Session, user: dict, acc: dict, uuid: UUID):
    req = service.get_request(db, user["company_id"], uuid)
    ids = views.visible_ids(db, user["company_id"], acc)
    if ids is not None and req.employee_id not in ids:
        # an approver with a task on it may see it too
        if not views.my_task_map(db, user["company_id"], [req.workflow_instance_id, req.cancel_instance_id]):
            raise Forbidden("You can't see this leave request.")
    return req


@router.get("/requests/{uuid}")
def get_request(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    acc = _acc(db, user)
    return views.request_detail(db, user["company_id"], _visible_request(db, user, acc, uuid), acc)


@router.put("/requests/{uuid}")
def modify_request(uuid: UUID, payload: ModifyIn, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    acc = _acc(db, user)
    req = _visible_request(db, user, acc, uuid)
    service.modify(db, user["company_id"], req, payload.model_dump(exclude_none=True), can_manage=acc["can_manage"])
    db.commit()
    return views.request_detail(db, user["company_id"], service.get_request(db, user["company_id"], uuid), acc)


@router.post("/requests/{uuid}/cancel")
def cancel_request(uuid: UUID, payload: CancelIn, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    acc = _acc(db, user)
    req = _visible_request(db, user, acc, uuid)
    service.cancel(db, user["company_id"], req, payload.dates, payload.reason, can_manage=acc["can_manage"])
    db.commit()
    return views.request_detail(db, user["company_id"], service.get_request(db, user["company_id"], uuid), acc)


@router.post("/requests/{uuid}/extend", status_code=201)
def extend_request(uuid: UUID, payload: ExtendIn, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    acc = _acc(db, user)
    req = _visible_request(db, user, acc, uuid)
    child = service.extend(db, user["company_id"], req, payload.to_date, payload.reason, can_manage=acc["can_manage"])
    db.commit()
    return views.request_detail(db, user["company_id"], service.get_request(db, user["company_id"], child.uuid), acc)


@router.get("/requests/{uuid}/attachment")
def get_attachment(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    acc = _acc(db, user)
    req = _visible_request(db, user, acc, uuid)
    if not req.attachment:
        raise BadRequest("This request has no attachment.")
    head, _, data = req.attachment.partition(",")
    mime = head[5:].split(";")[0] or "application/octet-stream"
    return Response(content=base64.b64decode(data), media_type=mime,
                    headers={"Content-Disposition": f'inline; filename="{req.attachment_name or "document"}"'})


# ── Comp off ────────────────────────────────────────────────────────

@router.get("/comp-offs")
def list_comp_offs(scope: str = "mine", page: int = Query(1, ge=1), size: int = Query(25, ge=1, le=200),
                   db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    return views.comp_off_rows(db, user["company_id"], _acc(db, user), scope, page, size)


@router.post("/comp-offs", status_code=201)
def ask_comp_off(payload: CompOffIn, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    acc = _acc(db, user)
    emp, on_behalf = _target(db, user, acc, payload.employee_uuid)
    row = service.request_comp_off(db, user["company_id"], emp, payload.model_dump(),
                                   source="admin" if on_behalf else "request", approved=on_behalf)
    db.commit()
    return {"uuid": str(row.uuid), "status": row.status, "days": float(row.days),
            "expires_on": row.expires_on.isoformat() if row.expires_on else None}


# ── Team ────────────────────────────────────────────────────────────

@router.get("/team/calendar")
def team_calendar(month: str | None = None, department_id: int | None = None, q: str | None = None,
                  db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    today = today_for_company_id(db, user["company_id"])
    start, end = reports.month_bounds(month) if month else reports.month_bounds(f"{today.year}-{today.month}")
    return views.team_calendar(db, user["company_id"], _acc(db, user), start, end, department_id=department_id, q=q)


@router.get("/calendar/days")
def calendar_days(from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to"),
                  employee_uuid: UUID | None = None, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    acc = _acc(db, user)
    emp, _ = _target(db, user, acc, employee_uuid) if employee_uuid else (_me(db, user), False)
    start, end = _date(from_date), _date(to_date)
    if (end - start).days > 400:
        raise BadRequest("Ask for at most 400 days at a time.")
    cal = WorkCalendar(db, user["company_id"])
    return {"days": [{"date": x["date"].isoformat(), "kind": x["kind"], "holiday": x.get("holiday"),
                      "optional": x.get("optional") or [], "value": float(x["value"])} for x in cal.days(emp, start, end)],
            "working_days": float(cal.working_days(emp, start, end))}


# ── Admin: balances and runs ────────────────────────────────────────

@router.get("/balances")
def balances(year: int | None = None, q: str | None = None, department_id: int | None = None,
             page: int = Query(1, ge=1), size: int = Query(25, ge=1, le=200), db: Session = Depends(get_db),
             user: dict = Depends(VIEW)):
    acc = _acc(db, user)
    if not (acc["can_manage"] or acc["can_approve"]):
        raise Forbidden("You can't see other people's balances.")
    ids = views.visible_ids(db, user["company_id"], acc)
    return views.balances_table(db, user["company_id"], year=year, q=q, department_id=department_id, page=page,
                                size=size, ids=ids)


@router.get("/balances/{employee_uuid}/ledger")
def balance_ledger(employee_uuid: UUID, leave_type_uuid: UUID | None = None, year: int | None = None,
                   db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    acc = _acc(db, user)
    emp = service.get_employee_by_uuid(db, user["company_id"], employee_uuid)
    ids = views.visible_ids(db, user["company_id"], acc)
    if ids is not None and emp.id not in ids:
        raise Forbidden("You can't see this person's leave.")
    tid = policy.get_type(db, user["company_id"], leave_type_uuid).id if leave_type_uuid else None
    return {"items": views.ledger_rows(db, user["company_id"], emp.id, tid, year)}


@router.post("/balances/adjust", status_code=201)
def adjust_balance(payload: AdjustIn, db: Session = Depends(get_db), user: dict = Depends(MANAGE)):
    cid = user["company_id"]
    emp = service.get_employee_by_uuid(db, cid, payload.employee_uuid)
    t = policy.get_type(db, cid, payload.leave_type_uuid)
    kind = "opening" if payload.kind == "opening" else "adjustment"
    on = None
    if kind == "opening" and payload.year:
        on = policy.year_range(policy.rules_on(t, date(payload.year, 6, 1)), payload.year)[0]
    row = ledger.adjust(db, cid, emp, t, payload.days, payload.reason, year=payload.year, on=on, kind=kind)
    db.commit()
    return {"uuid": str(row.uuid), "days": float(row.days), "year": row.year}


@router.post("/balances/import")
def import_balances(payload: ImportIn, db: Session = Depends(get_db), user: dict = Depends(MANAGE)):
    res = ledger.import_opening(db, user["company_id"], payload.csv, dry_run=payload.dry_run)
    db.commit()
    return res


@router.get("/balances/import/template")
def import_template(user: dict = Depends(MANAGE)):
    return _csv("employee,leave_type,days,year,note\nEMP001,EL,12.5,2026,Carried from old system\n", "opening-balances")


def _type_ids(db: Session, cid: int, uuids) -> list[int] | None:
    return [policy.get_type(db, cid, u).id for u in uuids] if uuids else None


@router.post("/runs/accrual")
def run_accrual(payload: RunIn, db: Session = Depends(get_db), user: dict = Depends(MANAGE)):
    cid = user["company_id"]
    as_of = payload.as_of or today_for_company_id(db, cid)
    res = ledger.run_accruals(db, cid, as_of)
    db.commit()
    return res


@router.post("/runs/expiry")
def run_expiry(payload: RunIn, db: Session = Depends(get_db), user: dict = Depends(MANAGE)):
    cid = user["company_id"]
    res = ledger.run_expiry(db, cid, payload.as_of or today_for_company_id(db, cid))
    db.commit()
    return res


@router.post("/runs/year-end")
def run_year_end(payload: RunIn, db: Session = Depends(get_db), user: dict = Depends(MANAGE)):
    cid = user["company_id"]
    year = payload.year or today_for_company_id(db, cid).year - 1
    res = ledger.year_end(db, cid, year, apply=payload.apply, type_ids=_type_ids(db, cid, payload.leave_type_uuids))
    if payload.apply:
        db.commit()
    else:
        db.rollback()
    return res


@router.get("/runs")
def list_runs(page: int = Query(1, ge=1), size: int = Query(25, ge=1, le=200), db: Session = Depends(get_db),
              user: dict = Depends(MANAGE)):
    q = db.query(LeaveRun).filter(LeaveRun.company_id == user["company_id"])
    total = q.count()
    rows = q.order_by(LeaveRun.created_at.desc()).offset((page - 1) * size).limit(size).all()
    return {"items": [{"uuid": str(r.uuid), "kind": r.kind, "label": r.label, "entries": r.entries,
                       "as_of": r.as_of.isoformat() if r.as_of else None, "year": r.year, "result": r.result,
                       "ran_by_name": r.ran_by_name, "created_at": iso_utc(r.created_at)} for r in rows],
            "total": total, "page": page, "size": size}


# ── Settings ────────────────────────────────────────────────────────

@router.get("/settings")
def get_settings(db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    return policy.get_settings(db, user["company_id"])


@router.put("/settings")
def put_settings(payload: dict[str, Any], db: Session = Depends(get_db), user: dict = Depends(CONFIGURE)):
    res = policy.update_settings(db, user["company_id"], payload)
    db.commit()
    return res


@router.get("/options")
def options(db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    """What the policy editor can pick from: which employee details exist, rule fields,
    icons, work week presets."""
    available = {"departments": True, "designations": True}
    try:
        from modules.organization import models as org  # noqa: F401
        available.update({"locations": True, "grades": True, "tags": True})
    except ImportError:
        available.update({"locations": False, "grades": False, "tags": False})
    return {"available": available, "expression_fields": EXPRESSION_FIELDS, "icons": list(policy.ICONS),
            "presets": [{"key": k, "name": v[0], "days": v[1]} for k, v in PRESETS.items()], "weekdays": WEEKDAYS}


@router.get("/options/{kind}")
def option_list(kind: str, q: str | None = None, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    """Options for the applicability pickers: locations, grades, tags, departments, designations, leave_types."""
    cid = user["company_id"]
    like = f"%{q.strip()}%" if q and q.strip() else None
    try:
        if kind == "leave_types":
            return [{"value": t.code, "label": f"{t.name} ({t.code})"} for t in policy.list_types(db, cid)]
        if kind == "departments":
            from modules.departments.models import Department
            qq = db.query(Department.id, Department.department_name).filter(Department.company_id == cid)
            if like:
                qq = qq.filter(Department.department_name.ilike(like))
            return [{"value": i, "label": n} for i, n in qq.order_by(Department.department_name).limit(200).all()]
        if kind == "designations":
            from modules.designations.models import Designation
            col = getattr(Designation, "designation_name", None) or getattr(Designation, "name")
            qq = db.query(Designation.id, col).filter((Designation.company_id == cid) | (Designation.company_id.is_(None)))
            if like:
                qq = qq.filter(col.ilike(like))
            return [{"value": i, "label": n} for i, n in qq.order_by(col).limit(200).all()]
        if kind in ("locations", "grades", "tags"):
            from modules.organization import models as org
            model = {"locations": org.Location, "grades": org.Grade, "tags": org.Tag}[kind]
            with db.begin_nested():
                qq = db.query(model.id, model.name).filter(model.company_id == cid)
                if like:
                    qq = qq.filter(model.name.ilike(like))
                return [{"value": i, "label": n} for i, n in qq.order_by(model.name).limit(200).all()]
    except Exception:
        return []
    raise BadRequest("Unknown list.")


@router.get("/types")
def list_types(include_inactive: bool = True, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    acc = _acc(db, user)
    rows = policy.list_types(db, user["company_id"], include_inactive=include_inactive and acc["can_configure"])
    if acc["can_configure"]:
        return [views.type_full(t) for t in rows]
    return [views.type_brief(t) for t in rows]


@router.get("/types/{uuid}")
def get_type(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(CONFIGURE)):
    return views.type_full(policy.get_type(db, user["company_id"], uuid))


@router.post("/types", status_code=201)
def create_type(payload: TypeIn, db: Session = Depends(get_db), user: dict = Depends(CONFIGURE)):
    data = payload.model_dump()
    t = policy.create_type(db, user["company_id"], data, effective_from=payload.effective_from or date(2000, 1, 1))
    db.commit()
    return views.type_full(policy.get_type(db, user["company_id"], t.uuid))


@router.put("/types/{uuid}")
def update_type(uuid: UUID, payload: TypeIn, db: Session = Depends(get_db), user: dict = Depends(CONFIGURE)):
    t = policy.get_type(db, user["company_id"], uuid)
    policy.update_type(db, user["company_id"], t, payload.model_dump(exclude_unset=True))
    db.commit()
    return views.type_full(policy.get_type(db, user["company_id"], uuid))


@router.post("/types/{uuid}/active")
def set_type_active(uuid: UUID, payload: dict[str, Any], db: Session = Depends(get_db), user: dict = Depends(CONFIGURE)):
    t = policy.get_type(db, user["company_id"], uuid)
    policy.set_active(db, t, bool(payload.get("active")))
    db.commit()
    return views.type_full(policy.get_type(db, user["company_id"], uuid))


@router.post("/types/validate-rule")
def validate_rule(payload: dict[str, Any], user: dict = Depends(CONFIGURE)):
    from core import expressions
    text = (payload.get("rule") or "").strip()
    if not text:
        return {"ok": True}
    try:
        expressions.parse(text)
        return {"ok": True}
    except expressions.ExpressionError as e:
        return {"ok": False, "detail": str(e)}


@router.post("/defaults")
def restore_defaults(db: Session = Depends(get_db), user: dict = Depends(CONFIGURE)):
    """Add the standard leave types, work weeks and holiday calendars that are missing."""
    defaults.ensure_leave_defaults(db, user["company_id"])
    db.commit()
    return {"ok": True}


# ── Holiday calendars ───────────────────────────────────────────────

@router.get("/holiday-calendars")
def list_calendars(year: int | None = None, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    return [hol.calendar_out(db, c, year) for c in hol.list_calendars(db, user["company_id"])]


@router.post("/holiday-calendars", status_code=201)
def create_calendar(payload: CalendarIn, db: Session = Depends(get_db), user: dict = Depends(CONFIGURE)):
    cid = user["company_id"]
    data = payload.model_dump(exclude_unset=True)
    if payload.copy_from_uuid:
        c = hol.copy_calendar(db, cid, hol.get_calendar(db, cid, payload.copy_from_uuid), payload.name or "Copy")
        c = hol.save_calendar(db, cid, {k: v for k, v in data.items() if k != "copy_from_uuid"}, c)
    else:
        c = hol.save_calendar(db, cid, data)
    db.commit()
    return hol.calendar_out(db, c)


@router.put("/holiday-calendars/{uuid}")
def update_calendar(uuid: UUID, payload: CalendarIn, db: Session = Depends(get_db), user: dict = Depends(CONFIGURE)):
    cid = user["company_id"]
    c = hol.save_calendar(db, cid, payload.model_dump(exclude_unset=True), hol.get_calendar(db, cid, uuid))
    db.commit()
    return hol.calendar_out(db, c)


@router.post("/holiday-calendars/{uuid}/import")
def import_calendar(uuid: UUID, payload: ImportIn, db: Session = Depends(get_db), user: dict = Depends(CONFIGURE)):
    cid = user["company_id"]
    res = hol.import_holidays(db, cid, hol.get_calendar(db, cid, uuid), payload.csv)
    db.commit()
    return res


@router.get("/holidays")
def list_holidays(calendar_uuid: UUID | None = None, year: int | None = None, db: Session = Depends(get_db),
                  user: dict = Depends(VIEW)):
    cid = user["company_id"]
    cal = hol.get_calendar(db, cid, calendar_uuid) if calendar_uuid else None
    return hol.list_holidays(db, cid, cal, year or today_for_company_id(db, cid).year)


@router.post("/holidays", status_code=201)
def add_holiday(payload: HolidayIn, db: Session = Depends(get_db), user: dict = Depends(CONFIGURE)):
    cid = user["company_id"]
    cals = [hol.get_calendar(db, cid, u) for u in (payload.calendar_uuids or [])]
    rows = hol.add_holiday(db, cid, cals, payload.model_dump())
    db.commit()
    return {"added": len(rows)}


@router.put("/holidays/{uuid}")
def update_holiday(uuid: UUID, payload: HolidayIn, db: Session = Depends(get_db), user: dict = Depends(CONFIGURE)):
    cid = user["company_id"]
    data = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in payload.model_dump(exclude_unset=True).items()}
    h = hol.update_holiday(db, cid, hol.get_holiday(db, cid, uuid), data)
    db.commit()
    return hol.holiday_out(h)


@router.delete("/holidays/{uuid}")
def delete_holiday(uuid: UUID, db: Session = Depends(get_db), user: dict = Depends(CONFIGURE)):
    cid = user["company_id"]
    hol.delete_holiday(db, cid, hol.get_holiday(db, cid, uuid))
    db.commit()
    return {"ok": True}


# ── Work weeks ──────────────────────────────────────────────────────

@router.get("/work-weeks")
def list_patterns(db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    return [hol.pattern_out(p) for p in hol.list_patterns(db, user["company_id"])]


@router.post("/work-weeks", status_code=201)
def create_pattern(payload: PatternIn, db: Session = Depends(get_db), user: dict = Depends(CONFIGURE)):
    p = hol.save_pattern(db, user["company_id"], payload.model_dump(exclude_unset=True))
    db.commit()
    return hol.pattern_out(p)


@router.put("/work-weeks/{uuid}")
def update_pattern(uuid: UUID, payload: PatternIn, db: Session = Depends(get_db), user: dict = Depends(CONFIGURE)):
    cid = user["company_id"]
    p = hol.save_pattern(db, cid, payload.model_dump(exclude_unset=True), hol.get_pattern(db, cid, uuid))
    db.commit()
    return hol.pattern_out(p)


# ── Reports ─────────────────────────────────────────────────────────

def _report_ids(db: Session, user: dict):
    acc = _acc(db, user)
    if not (acc["can_manage"] or acc["can_approve"]):
        raise Forbidden("You don't have access to leave reports.")
    return views.visible_ids(db, user["company_id"], acc)


@router.get("/reports/balances")
def report_balances(year: int | None = None, format: str | None = None, db: Session = Depends(get_db),
                    user: dict = Depends(VIEW)):
    res = reports.balance_report(db, user["company_id"], year, _report_ids(db, user))
    if format == "csv":
        audit.event(db, "export.downloaded", "leave_ledger", None, {"report": "leave balances"})
        db.commit()
        return _csv(reports.to_csv(res["rows"], [
            ("employee_code", "Employee ID"), ("employee_name", "Name"), ("department", "Department"),
            ("leave_type", "Leave type"), ("year", "Year"), ("opening", "Opening"), ("earned", "Earned"),
            ("taken", "Taken"), ("adjusted", "Adjusted"), ("encashed", "Encashed"), ("lapsed", "Lapsed or carried"),
            ("balance", "Balance")]), "leave-balances")
    return res


@router.get("/reports/taken")
def report_taken(from_date: str | None = Query(None, alias="from"), to_date: str | None = Query(None, alias="to"),
                 format: str | None = None, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    today = today_for_company_id(db, user["company_id"])
    start = _date(from_date, date(today.year, 1, 1))
    end = _date(to_date, today)
    res = reports.taken_report(db, user["company_id"], start, end, _report_ids(db, user))
    if format == "csv":
        rows = [{"department": d["department"], **{c: d["by_type"].get(c, 0) for c in [t["code"] for t in res["types"]]},
                 "total": d["total"]} for d in res["by_department"]]
        return _csv(reports.to_csv(rows, [("department", "Department"), *[(t["code"], t["name"]) for t in res["types"]],
                                          ("total", "Total")]), "leave-taken")
    return res


@router.get("/reports/lop")
def report_lop(period: str | None = None, format: str | None = None, db: Session = Depends(get_db),
               user: dict = Depends(VIEW)):
    today = today_for_company_id(db, user["company_id"])
    try:
        start, end = reports.month_bounds(period or f"{today.year}-{today.month}")
    except (ValueError, TypeError):
        raise BadRequest("The pay period looks like 2026-11.")
    res = reports.lop_summary(db, user["company_id"], start, end, _report_ids(db, user))
    if format == "csv":
        return _csv(reports.to_csv(res["rows"], [("employee_code", "Employee ID"), ("employee_name", "Name"),
                                                 ("department", "Department"), ("lop_days", "LOP days"),
                                                 ("pending_days", "Pending LOP days")]), f"lop-{period or 'period'}")
    return res


@router.get("/reports/absentees")
def report_absentees(day: str | None = None, db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    d = _date(day, today_for_company_id(db, user["company_id"]))
    return reports.absentees(db, user["company_id"], d, _report_ids(db, user))


@router.get("/reports/encashments")
def report_encashments(from_date: str | None = Query(None, alias="from"), to_date: str | None = Query(None, alias="to"),
                       db: Session = Depends(get_db), user: dict = Depends(MANAGE)):
    today = today_for_company_id(db, user["company_id"])
    return {"items": reports.encashments(db, user["company_id"], _date(from_date, date(today.year - 1, 1, 1)),
                                         _date(to_date, today))}


# ── Bradford score and expiry reminders ─────────────────────────────

@router.get("/reports/bradford")
def report_bradford(as_of: str | None = None, format: str | None = None, db: Session = Depends(get_db),
                    user: dict = Depends(VIEW)):
    from modules.leave import bradford
    d = _date(as_of, today_for_company_id(db, user["company_id"]))
    res = bradford.report(db, user["company_id"], d, _report_ids(db, user))
    if format == "csv":
        audit.event(db, "export.downloaded", "leave_requests", None, {"report": "bradford"})
        db.commit()
        return _csv(reports.to_csv(res["rows"], [
            ("employee_code", "Employee ID"), ("employee_name", "Name"), ("department", "Department"),
            ("spells", "Spells"), ("days", "Days"), ("score", "Bradford score"), ("label", "Level")]), "bradford")
    return res


@router.get("/me/bradford")
def my_bradford(db: Session = Depends(get_db), user: dict = Depends(VIEW)):
    from modules.leave import bradford
    emp = _me(db, user)
    cid = user["company_id"]
    s = policy.get_settings(db, cid)["bradford"]
    if not s.get("enabled"):
        return {"enabled": False}
    return {"enabled": True, "score": bradford.employee_score(db, cid, emp.id, today_for_company_id(db, cid))}


@router.post("/bradford/alerts/run")
def run_bradford_alerts(db: Session = Depends(get_db), user: dict = Depends(MANAGE)):
    from modules.leave import bradford
    cid = user["company_id"]
    n = bradford.run_alerts(db, cid, today_for_company_id(db, cid))
    db.commit()
    return {"alerts": n}


@router.get("/expiry-reminders/due")
def expiry_due(db: Session = Depends(get_db), user: dict = Depends(MANAGE)):
    from modules.leave import reminders
    cid = user["company_id"]
    items = reminders.due(db, cid, today_for_company_id(db, cid))
    names = {e.id: f"{e.first_name} {e.last_name}".strip() for e in
             db.query(service._employee_model()).filter(
                 service._employee_model().id.in_([i["entry"].employee_id for i in items] or [-1])).all()}
    return {"items": [{"employee_id": i["entry"].employee_id, "employee_name": names.get(i["entry"].employee_id),
                       "leave_type": i["type"].name, "days": float(i["remaining"]),
                       "expires_on": i["entry"].expires_on.isoformat(), "days_left": i["days_left"]} for i in items]}


@router.post("/expiry-reminders/send")
def expiry_send(db: Session = Depends(get_db), user: dict = Depends(MANAGE)):
    from modules.leave import reminders
    cid = user["company_id"]
    n = reminders.send_due(db, cid, today_for_company_id(db, cid))
    audit.event(db, "leave.expiry_reminders_sent", "leave_expiry_reminders", None, {"employees": n})
    db.commit()
    return {"employees": n}
