"""Attendance API, second part: attendance-to-leave links, loss of pay per pay period,
clock-in kiosks and PINs, and statutory registers. Mounted by router.py."""
from __future__ import annotations

import io
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core import audit, csv_safe
from core.db import get_db, set_tenant
from core.errors import BadRequest, NotFound
from core.security import require_permission
from modules.attendance import kiosk, leave_link, lop, registers, service
from modules.attendance.models import Kiosk, LeaveLink, LopEntry

router = APIRouter(tags=["Attendance"])  # mounted under /attendance by router.py

VIEW = require_permission("time.attendance.view")
MANAGE = require_permission("time.attendance.manage")
SETTINGS = require_permission("time.attendance.settings")


# ── Attendance and leave ────────────────────────────────────────────

@router.get("/leave-links")
def list_leave_links(start: date | None = None, end: date | None = None, kind: str | None = None,
                     user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    cid = user["company_id"]
    td = service.today(db, cid)
    start, end = start or td - timedelta(days=60), end or td
    if end < start or (end - start).days > 400:
        raise BadRequest("Choose a range of up to a year.")
    ids = service.visible_ids(db, user)
    return {"items": leave_link.list_links(db, cid, start, end, kind, ids)}


class ReverseIn(BaseModel):
    reason: str = Field(..., min_length=3, max_length=300)


@router.post("/leave-links/{uuid}/reverse")
def reverse_leave_link(uuid: UUID, req: ReverseIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = user["company_id"]
    link = db.query(LeaveLink).filter(LeaveLink.company_id == cid, LeaveLink.uuid == uuid).first()
    if link is None:
        raise NotFound("Record")
    if link.status not in leave_link.ACTIVE:
        raise BadRequest("This has already been undone.")
    if service.is_locked(db, cid, link.date):
        raise BadRequest("This day is in a locked pay period.")
    leave_link.reverse(db, link, req.reason)
    if link.kind == "absence":
        # keep it from being converted again at the next run
        link.status = "rejected"
    service.recompute(db, cid, [link.employee_id], link.date, link.date, hooks=False)
    db.commit()
    return {"ok": True}


# ── Loss of pay per pay period ──────────────────────────────────────

def _period(db: Session, cid: int, start: date | None, end: date | None) -> tuple[date, date]:
    if start and end:
        if end < start or (end - start).days > 62:
            raise BadRequest("Choose one pay period.")
        return start, end
    td = service.today(db, cid)
    return service.pay_period_for(db, cid, start or (td.replace(day=1) - timedelta(days=1)))


@router.get("/lop")
def lop_report(start: date | None = None, end: date | None = None, format: str = "json",
               user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    """Pre-payroll loss-of-pay review for a pay period."""
    cid = user["company_id"]
    start, end = _period(db, cid, start, end)
    ids = [e.id for e in service.employees_query(db, cid, active_only=False).all()]
    if start <= service.today(db, cid):
        active = [e.id for e in service.employees_query(db, cid).all()]
        service.recompute(db, cid, active, start, min(end, service.today(db, cid)))
    figs = lop.period_rows(db, cid, start, end, ids)
    emps = {e.id: e for e in service.employees_query(db, cid, list(figs), active_only=False).all()}
    rows = []
    for eid, f in figs.items():
        e = emps.get(eid)
        if e is None:
            continue
        rows.append({"employee_id": eid, "employee_uuid": str(e.uuid), "employee_code": e.employee_code,
                     "name": service.name_of(e), **{k: (str(v) if isinstance(v, Decimal) else v) for k, v in f.items()},
                     "payroll_lop": str(f["payable"] + f["reduced_pay_days"])})
    rows.sort(key=lambda r: (-Decimal(r["total"]), r["name"]))
    cfg = lop.config(db, cid, start)
    locked = service.lock_for(service.locks(db, cid), start) is not None
    if format == "csv":
        buf = io.StringIO()
        w = csv_safe.writer(buf)
        w.writerow(["Employee ID", "Name", "Attendance LOP", "Carried in", "Manual", "Total", "Payable LOP",
                    "Carried to next period", "Waived", "Reduced-pay days", "LOP sent to payroll"])
        for r in rows:
            w.writerow([r["employee_code"], r["name"], r["attendance_lop"], r["carried_in"], r["manual"], r["total"],
                        r["payable"], r["carried_out"], r["waived"], r["reduced_pay_days"], r["payroll_lop"]])
        audit.event(db, "export.downloaded", "lop_period_entries", None, {"report": "lop review"})
        db.commit()
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="lop-{start}.csv"'})
    db.commit()
    return {"start": start.isoformat(), "end": end.isoformat(), "locked": locked, "cap_days": cfg.get("cap_days"),
            "carry_excess": cfg.get("carry_excess", True), "rows": rows,
            "totals": {k: str(sum((Decimal(r[k]) for r in rows), Decimal("0")))
                       for k in ("attendance_lop", "total", "payable", "carried_out", "reduced_pay_days", "payroll_lop")}}


class LopAdjustIn(BaseModel):
    employee_uuid: UUID
    start: date
    end: date
    days: Decimal = Field(..., ge=-31, le=31)
    note: str = Field(..., min_length=3, max_length=300)


@router.post("/lop/adjustments", status_code=201)
def lop_adjust(req: LopAdjustIn, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = user["company_id"]
    emp = service.employee_by_uuid(db, cid, req.employee_uuid)
    e = lop.add_manual(db, cid, emp.id, req.start, req.end, req.days, req.note)
    db.commit()
    return {"uuid": str(e.uuid)}


@router.delete("/lop/adjustments/{uuid}")
def lop_adjust_remove(uuid: UUID, user: dict = Depends(MANAGE), db: Session = Depends(get_db)):
    cid = user["company_id"]
    e = db.query(LopEntry).filter(LopEntry.company_id == cid, LopEntry.uuid == uuid, LopEntry.void.is_(False)).first()
    if e is None:
        raise NotFound("Adjustment")
    lop.void_manual(db, cid, e)
    db.commit()
    return {"ok": True}


# ── Kiosks (admin) ──────────────────────────────────────────────────

class KioskIn(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    location_note: str | None = Field(None, max_length=200)
    photo: str = "off"
    allowed_ips: list[str] = Field(default_factory=list)
    active: bool | None = None


@router.get("/kiosks")
def list_kiosks(user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    cid = user["company_id"]
    rows = db.query(Kiosk).filter(Kiosk.company_id == cid).order_by(Kiosk.created_at).all()
    ids = [e.id for e in service.employees_query(db, cid).all()]
    with_pin = kiosk.pins_set(db, cid)
    return {"items": [kiosk.kiosk_out(k) for k in rows],
            "pins": {"employees": len(ids), "with_pin": len([i for i in ids if i in with_pin])}}


@router.post("/kiosks", status_code=201)
def create_kiosk(req: KioskIn, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    k, token = kiosk.save(db, user["company_id"], req.model_dump())
    audit.event(db, "attendance.kiosk_created", "attendance_kiosks", k.id, {"name": k.name}, entity_uuid=str(k.uuid))
    db.commit()
    return {**kiosk.kiosk_out(k), "token": token}


@router.put("/kiosks/{uuid}")
def update_kiosk(uuid: UUID, req: KioskIn, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    k = kiosk.get(db, user["company_id"], uuid)
    kiosk.save(db, user["company_id"], req.model_dump(), k)
    db.commit()
    return kiosk.kiosk_out(k)


@router.post("/kiosks/{uuid}/link")
def new_kiosk_link(uuid: UUID, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    k = kiosk.get(db, user["company_id"], uuid)
    token = kiosk.new_token(k)
    audit.event(db, "attendance.kiosk_link_changed", "attendance_kiosks", k.id, {"name": k.name},
                entity_uuid=str(k.uuid))
    db.commit()
    return {**kiosk.kiosk_out(k), "token": token}


class PinIn(BaseModel):
    pin: str = Field(..., min_length=4, max_length=6)


@router.get("/kiosk-pins")
def kiosk_pins(q: str | None = None, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    cid = user["company_id"]
    with_pin = kiosk.pins_set(db, cid)
    emps = service.employees_query(db, cid, q=q).limit(200).all()
    return {"items": [{"employee_uuid": str(e.uuid), "employee_code": e.employee_code, "name": service.name_of(e),
                       "has_pin": e.id in with_pin} for e in emps]}


@router.put("/kiosk-pins/{employee_uuid}")
def set_kiosk_pin(employee_uuid: UUID, req: PinIn, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    cid = user["company_id"]
    emp = service.employee_by_uuid(db, cid, employee_uuid)
    kiosk.set_pin(db, cid, emp.id, req.pin, by_hr=True)
    db.commit()
    return kiosk.pin_status(db, cid, emp.id)


@router.delete("/kiosk-pins/{employee_uuid}")
def clear_kiosk_pin(employee_uuid: UUID, user: dict = Depends(SETTINGS), db: Session = Depends(get_db)):
    cid = user["company_id"]
    emp = service.employee_by_uuid(db, cid, employee_uuid)
    kiosk.clear_pin(db, cid, emp.id)
    db.commit()
    return kiosk.pin_status(db, cid, emp.id)


@router.get("/me/kiosk-pin")
def my_kiosk_pin(user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    if not user.get("employee_id"):
        raise BadRequest("The company owner doesn't punch at the kiosk.")
    cid = user["company_id"]
    emp = service.employee(db, cid, user["employee_id"])
    return {**kiosk.pin_status(db, cid, emp.id), "employee_code": emp.employee_code}


@router.put("/me/kiosk-pin")
def set_my_kiosk_pin(req: PinIn, user: dict = Depends(VIEW), db: Session = Depends(get_db)):
    if not user.get("employee_id"):
        raise BadRequest("The company owner doesn't punch at the kiosk.")
    cid = user["company_id"]
    kiosk.set_pin(db, cid, user["employee_id"], req.pin)
    db.commit()
    return kiosk.pin_status(db, cid, user["employee_id"])


# ── Kiosk (public, opened at the kiosk link) ────────────────────────

def _kiosk(db: Session, token: str) -> Kiosk:
    k = kiosk.kiosk_for_token(db, token)
    if k is None:
        raise NotFound("Kiosk")
    from core import context
    ctx = context.current()
    ctx.actor_type, ctx.actor_name = "system", f"Kiosk: {k.name}"
    set_tenant(db, k.company_id)
    return k


@router.get("/kiosk/{token}")
def kiosk_info(token: str, db: Session = Depends(get_db)):
    k = _kiosk(db, token)
    out = kiosk.info(db, k)
    db.commit()
    return out


class KioskPunchIn(BaseModel):
    employee_code: str = Field(..., min_length=1, max_length=30)
    pin: str = Field(..., min_length=1, max_length=12)
    photo: str | None = None
    at: datetime | None = None
    client_id: str | None = Field(None, max_length=60)


@router.post("/kiosk/{token}/punch")
def kiosk_punch(token: str, req: KioskPunchIn, db: Session = Depends(get_db)):
    k = _kiosk(db, token)
    try:
        out = kiosk.punch(db, k, req.model_dump())
    except BadRequest:
        db.commit()  # keep the failed-PIN count
        raise
    db.commit()
    return out


class KioskSyncIn(BaseModel):
    punches: list[KioskPunchIn] = Field(..., max_length=200)


@router.post("/kiosk/{token}/sync")
def kiosk_sync(token: str, req: KioskSyncIn, db: Session = Depends(get_db)):
    """Punches kept on the tablet while it was offline. Each one is checked on its own."""
    k = _kiosk(db, token)
    results: list[dict[str, Any]] = []
    for p in req.punches:
        try:
            with db.begin_nested():
                r = kiosk.punch(db, k, p.model_dump())
            results.append({"client_id": p.client_id, **r})
        except BadRequest as exc:
            results.append({"client_id": p.client_id, "status": "error", "detail": exc.detail})
        db.commit()
        set_tenant(db, k.company_id)
    return {"results": results}


# ── Statutory registers ─────────────────────────────────────────────

@router.get("/registers/formats")
def register_formats(user: dict = Depends(MANAGE)):
    return {"items": registers.formats()}


@router.get("/registers")
def register_file(format: str = "generic", kind: str = "attendance", year: int | None = None,
                  month: int | None = None, type: str = "xlsx", user: dict = Depends(MANAGE),
                  db: Session = Depends(get_db)):
    cid = user["company_id"]
    if kind not in ("attendance", "overtime"):
        raise BadRequest("Choose the register of attendance or of overtime.")
    if type not in ("xlsx", "pdf", "json"):
        raise BadRequest("Choose Excel or PDF.")
    td = service.today(db, cid)
    year, month = year or td.year, month or td.month
    if not (1 <= month <= 12 and 2000 <= year <= 2100):
        raise BadRequest("Choose a month.")
    start, end = service.month_range(year, month)
    if start <= td:
        service.recompute(db, cid, [e.id for e in service.employees_query(db, cid).all()], start, min(end, td))
    data = registers.build(db, cid, format, kind, start, end, service.visible_ids(db, user))
    audit.event(db, "export.downloaded", "attendance_days", None,
                {"report": f"register {format} {kind}", "period": f"{year}-{month:02d}", "type": type})
    db.commit()
    if type == "json":
        return {**{k: v for k, v in data.items() if k not in ("days", "start", "end")},
                "start": start.isoformat(), "end": end.isoformat()}
    name = f"{kind}-register-{format.lower()}-{year}-{month:02d}"
    if type == "pdf":
        return Response(registers.to_pdf(data), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{name}.pdf"'})
    return Response(registers.to_xlsx(data),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'})
