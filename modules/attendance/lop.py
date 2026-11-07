"""Loss of pay per pay period (settings key `lop_control`).

For each employee and pay period:

    attendance LOP   absent days, unpaid leave, late-mark penalties still on pay, and
                     corrections from earlier locked periods paid in this one
    + carried in     days over the cap in an earlier period, carried into this one
    + manual         adjustments HR makes before payroll (plus or minus, with a note)
    = total
    payable LOP      min(total, cap) when a cap is set, else the total
    carried out      the rest, moved to the next period when `carry_excess` is on (else waived)

Reduced-pay leave (e.g. half-pay sick leave) is reported on its own (`reduced_pay_days`) and
added to the LOP sent to payroll; the cap does not apply to it.

Locking the attendance pay period freezes the figures: a `final` entry per employee and a
`carry_in` entry for the next period. Unlocking voids them. Manual adjustments can't be
made in a locked period.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from core import audit, context
from core.errors import BadRequest
from modules.attendance import settings_store
from modules.attendance.models import AttendanceAdjustment, AttendanceDay, LopEntry

ZERO = Decimal("0.00")
Q = Decimal("0.01")


def _q(x) -> Decimal:
    return Decimal(str(x or 0)).quantize(Q)


def config(db: Session, company_id: int, on: date) -> dict:
    return settings_store.get(db, company_id, "lop_control", on)


def entries(db: Session, company_id: int, start: date, end: date, employee_ids=None) -> list[LopEntry]:
    q = db.query(LopEntry).filter(LopEntry.company_id == company_id, LopEntry.period_start == start,
                                  LopEntry.period_end == end, LopEntry.void.is_(False))
    if employee_ids is not None:
        q = q.filter(LopEntry.employee_id.in_(list(employee_ids) or [-1]))
    return q.order_by(LopEntry.id).all()


def compute(cfg: dict, attendance_lop: Decimal, carried_in: Decimal, manual: Decimal) -> dict:
    """The period's figures for one employee (pure, for tests)."""
    total = max(_q(attendance_lop) + _q(carried_in) + _q(manual), ZERO)
    cap = cfg.get("cap_days")
    payable = total
    if cap not in (None, ""):
        payable = min(total, _q(cap))
    rest = total - payable
    carry = rest if cfg.get("carry_excess", True) else ZERO
    return {"total": total, "payable": payable, "carried_out": carry, "waived": rest - carry}


def period_rows(db: Session, company_id: int, start: date, end: date, employee_ids=None) -> dict[int, dict]:
    """{employee_id: figures} for every employee with attendance in the period. Uses the
    frozen figures when the period is locked."""
    cfg = config(db, company_id, start)
    q = db.query(AttendanceDay).filter(AttendanceDay.company_id == company_id, AttendanceDay.date >= start,
                                       AttendanceDay.date <= end)
    if employee_ids is not None:
        q = q.filter(AttendanceDay.employee_id.in_(list(employee_ids) or [-1]))
    att = defaultdict(lambda: ZERO)
    cut = defaultdict(lambda: ZERO)
    seen = set()
    for r in q.all():
        seen.add(r.employee_id)
        cut[r.employee_id] += _q(r.pay_cut_days)
        att[r.employee_id] += _q(r.lop_days) + _q(r.penalty_lop_days) - _q(r.pay_cut_days)
    aq = db.query(AttendanceAdjustment).filter(AttendanceAdjustment.company_id == company_id,
                                               AttendanceAdjustment.apply_from >= start,
                                               AttendanceAdjustment.apply_from <= end)
    if employee_ids is not None:
        aq = aq.filter(AttendanceAdjustment.employee_id.in_(list(employee_ids) or [-1]))
    adj = defaultdict(lambda: ZERO)
    for a in aq.all():
        seen.add(a.employee_id)
        adj[a.employee_id] += _q(a.lop_days_delta)
    carried, manual, final, notes = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO), {}, defaultdict(list)
    for e in entries(db, company_id, start, end, employee_ids):
        seen.add(e.employee_id)
        if e.kind == "carry_in":
            carried[e.employee_id] += _q(e.days)
        elif e.kind == "manual":
            manual[e.employee_id] += _q(e.days)
            notes[e.employee_id].append({"uuid": str(e.uuid), "days": str(_q(e.days)), "note": e.note,
                                         "by": e.created_by_name,
                                         "at": e.created_at.isoformat().replace("+00:00", "Z") if e.created_at else None})
        elif e.kind == "final":
            final[e.employee_id] = e
    out = {}
    for eid in seen:
        base = att[eid] + adj[eid]
        fig = compute(cfg, base, carried[eid], manual[eid])
        f = final.get(eid)
        if f is not None:
            m = f.meta or {}
            fig = {"total": _q(m.get("total")), "payable": _q(f.days), "carried_out": _q(m.get("carried_out")),
                   "waived": _q(m.get("waived"))}
        out[eid] = {"attendance_lop": base, "attendance_adjustment": adj[eid], "carried_in": carried[eid],
                    "manual": manual[eid], "reduced_pay_days": cut[eid], **fig, "locked": f is not None,
                    "manual_entries": notes[eid], "cap_days": cfg.get("cap_days")}
    return out


def _next_period(db: Session, company_id: int, end: date) -> tuple[date, date]:
    from modules.attendance.service import pay_period_for
    return pay_period_for(db, company_id, end + timedelta(days=1))


def finalise(db: Session, company_id: int, start: date, end: date, lock_uuid: str | None = None) -> int:
    """Freeze the period's figures when attendance is locked and carry the excess on."""
    rows = period_rows(db, company_id, start, end)
    ns, ne = _next_period(db, company_id, end)
    who = context.current().actor_name or "System"
    n = 0
    for eid, fig in rows.items():
        if fig["locked"]:
            continue
        db.add(LopEntry(company_id=company_id, employee_id=eid, period_start=start, period_end=end, kind="final",
                        days=fig["payable"], created_by_name=who, note="Frozen when the period was locked",
                        meta={"total": str(fig["total"]), "carried_out": str(fig["carried_out"]),
                              "waived": str(fig["waived"]), "reduced_pay_days": str(fig["reduced_pay_days"]),
                              "lock": lock_uuid}))
        if fig["carried_out"] > 0:
            db.add(LopEntry(company_id=company_id, employee_id=eid, period_start=ns, period_end=ne, kind="carry_in",
                            days=fig["carried_out"], created_by_name=who,
                            note=f"Over the cap in {start:%d %b} – {end:%d %b %Y}",
                            meta={"from_start": start.isoformat(), "lock": lock_uuid}))
        n += 1
    db.flush()
    return n


def unfinalise(db: Session, company_id: int, start: date, end: date) -> int:
    """Undo `finalise` when the period is unlocked (entries are voided, not deleted)."""
    n = 0
    for e in entries(db, company_id, start, end):
        if e.kind == "final":
            e.void = True
            n += 1
    ns, ne = _next_period(db, company_id, end)
    for e in entries(db, company_id, ns, ne):
        if e.kind == "carry_in" and (e.meta or {}).get("from_start") == start.isoformat():
            e.void = True
    db.flush()
    return n


def add_manual(db: Session, company_id: int, employee_id: int, start: date, end: date, days, note: str) -> LopEntry:
    from modules.attendance.service import lock_for, locks
    if lock_for(locks(db, company_id), start) or lock_for(locks(db, company_id), end):
        raise BadRequest("This pay period is locked. Unlock it to change loss of pay.")
    try:
        d = _q(days)
    except Exception:
        raise BadRequest("Enter the days as a number.")
    if d == 0 or abs(d) > 31:
        raise BadRequest("Enter between -31 and 31 days (not zero).")
    if not (note or "").strip():
        raise BadRequest("Say why the loss of pay is changed.")
    e = LopEntry(company_id=company_id, employee_id=employee_id, period_start=start, period_end=end, kind="manual",
                 days=d, note=note.strip()[:300], created_by_name=context.current().actor_name)
    db.add(e)
    db.flush()
    audit.event(db, "attendance.lop_adjusted", "lop_period_entries", e.id,
                {"employee_id": employee_id, "from": start.isoformat(), "to": end.isoformat(), "days": str(d),
                 "note": e.note}, entity_uuid=str(e.uuid))
    return e


def void_manual(db: Session, company_id: int, entry: LopEntry) -> None:
    from modules.attendance.service import lock_for, locks
    if entry.kind != "manual":
        raise BadRequest("Only manual changes can be removed.")
    if lock_for(locks(db, company_id), entry.period_start):
        raise BadRequest("This pay period is locked.")
    entry.void = True
    audit.event(db, "attendance.lop_adjustment_removed", "lop_period_entries", entry.id,
                {"employee_id": entry.employee_id, "days": str(entry.days)}, entity_uuid=str(entry.uuid))
    db.flush()
