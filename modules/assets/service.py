"""Asset register, assignment with acknowledgement, return, maintenance, requests and
recovery for damage or loss.

For final settlement and exit clearance other modules call:
    outstanding_assets(db, company_id, employee_id) -> list[dict]  (not yet returned)
    recoveries(db, company_id, employee_id) -> {"items": [...], "total_pending": Decimal}
    mark_recovered(db, company_id, assignment_uuid, settlement_ref) -> None
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from core import audit, context
from core.errors import BadRequest, Forbidden, NotFound
from core.timeutil import today_for_company_id
from modules.assets.models import Asset, AssetAssignment, AssetMaintenance, AssetRequest

CONDITIONS = {"new": "New", "good": "Good", "fair": "Fair", "poor": "Poor", "damaged": "Damaged", "lost": "Lost"}
STATUSES = {"available": "Available", "assigned": "Assigned", "in_repair": "In repair", "lost": "Lost", "retired": "Retired"}
MAINT_KINDS = {"repair": "Repair", "service": "Service", "upgrade": "Upgrade", "inspection": "Inspection"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def actor_name() -> str:
    return context.current().actor_name or "System"


def _today(db: Session, company_id: int) -> date:
    return today_for_company_id(db, company_id)


def _employee_model():
    from modules.employees.models import Employee
    return Employee


def employee(db: Session, company_id: int, eid):
    if not eid:
        return None
    E = _employee_model()
    return db.query(E).filter(E.company_id == company_id, E.id == eid).first()


def names_for(db: Session, company_id: int, ids) -> dict[int, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    E = _employee_model()
    return {r.id: f"{r.first_name} {r.last_name}".strip()
            for r in db.query(E.id, E.first_name, E.last_name).filter(E.company_id == company_id, E.id.in_(ids))}


def notify(db: Session, company_id: int, employee_id, message: str) -> None:
    if not employee_id:
        return
    from modules.notifications.models import Notification
    db.add(Notification(company_id=company_id, employee_id=employee_id, channel="in_app", message_type="asset",
                        message_content=message[:1000], status="sent", sent_at=_now()))


def _money(v) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        d = Decimal(str(v)).quantize(Decimal("0.01"))
    except Exception:
        raise BadRequest("Enter amounts as numbers.")
    if d < 0:
        raise BadRequest("Amounts can't be negative.")
    return d


# ── Register ────────────────────────────────────────────────────────

def get(db: Session, company_id: int, uuid) -> Asset:
    row = (db.query(Asset).options(joinedload(Asset.assignments))
           .filter(Asset.company_id == company_id, Asset.uuid == uuid).first())
    if row is None:
        raise NotFound("Asset")
    return row


def current_assignment(a: Asset) -> AssetAssignment | None:
    return next((x for x in a.assignments if x.status in ("pending_ack", "active")), None)


def save(db: Session, company_id: int, data: dict, row: Asset | None = None) -> Asset:
    name = (data.get("name") or (row.name if row else "")).strip()
    if not name:
        raise BadRequest("Give the asset a name, such as “Dell Latitude 5440”.")
    tag = (data.get("asset_tag") or "").strip() or (row.asset_tag if row else None)
    from core import numbering
    if not tag:
        tag = numbering.next_number(db, company_id, "asset")
        if not tag:
            raise BadRequest("Type an asset tag, or switch on asset numbering in settings.")
    dup = db.query(Asset.id).filter(Asset.company_id == company_id, Asset.asset_tag == tag)
    if row is not None:
        dup = dup.filter(Asset.id != row.id)
    if dup.first():
        raise BadRequest(f"Asset tag {tag} is already used.")
    if "serial_number" in data:
        serial = (data.get("serial_number") or "").strip() or None
    else:
        serial = row.serial_number if row else None
    if serial:
        d2 = db.query(Asset.id).filter(Asset.company_id == company_id, Asset.serial_number == serial)
        if row is not None:
            d2 = d2.filter(Asset.id != row.id)
        if d2.first():
            raise BadRequest(f"Another asset already has serial number {serial}.")
    if data.get("category_id"):
        from core.masters import validate_value_ids
        validate_value_ids(db, company_id, "asset_category", [data["category_id"]])
    new = row is None
    if new or "custom" in data:
        from core import custom_fields
        custom = custom_fields.merge_values(db, company_id, "asset", data.get("custom"),
                                            None if new else row.custom, partial=not new)
    if row is None:
        row = Asset(company_id=company_id, asset_tag=tag, name=name, status="available")
        db.add(row)
    if new or "custom" in data:
        row.custom = custom
    row.asset_tag, row.name, row.serial_number = tag[:40], name[:200], serial
    for k in ("category_id", "brand", "model", "purchase_date", "vendor", "invoice_no", "warranty_until",
              "location_id", "location_note", "notes"):
        if k in data:
            v = data[k]
            setattr(row, k, v.strip() if isinstance(v, str) else v)
    if "purchase_value" in data:
        row.purchase_value = _money(data["purchase_value"])
    if data.get("condition"):
        if data["condition"] not in CONDITIONS:
            raise BadRequest("Unknown condition.")
        row.condition = data["condition"]
    if row.purchase_date and row.warranty_until and row.warranty_until < row.purchase_date:
        raise BadRequest("The warranty can't end before the purchase date.")
    db.flush()
    return row


def set_status(db: Session, a: Asset, status: str, note: str | None = None) -> Asset:
    """Mark an unassigned asset as available, in repair, lost or retired."""
    if status not in STATUSES or status == "assigned":
        raise BadRequest("Assign the asset to someone to mark it assigned.")
    if current_assignment(a):
        raise BadRequest("Take the asset back from the employee first.")
    a.status = status
    if status == "retired":
        a.retired_on = _today(db, a.company_id)
    audit.event(db, "asset.status", "assets", a.id, {"status": status, "note": note}, entity_uuid=str(a.uuid),
                entity_label=a.asset_tag)
    return a


# ── Assign, acknowledge, return ─────────────────────────────────────

def assign(db: Session, company_id: int, a: Asset, employee_id: int, *, assigned_on: date | None = None,
           expected_return_on: date | None = None, condition: str | None = None, notes: str | None = None,
           request: AssetRequest | None = None) -> AssetAssignment:
    if a.status != "available" or current_assignment(a):
        raise BadRequest(f"{a.asset_tag} is not available (it is {STATUSES.get(a.status, a.status).lower()}).")
    emp = employee(db, company_id, employee_id)
    if emp is None or not emp.active:
        raise BadRequest("Pick an active employee.")
    on = assigned_on or _today(db, company_id)
    if expected_return_on and expected_return_on < on:
        raise BadRequest("The return date can't be before the assignment date.")
    if condition and condition not in CONDITIONS:
        raise BadRequest("Unknown condition.")
    x = AssetAssignment(company_id=company_id, asset_id=a.id, employee_id=emp.id, status="pending_ack", assigned_on=on,
                        expected_return_on=expected_return_on, condition_out=condition or a.condition,
                        notes=(notes or "").strip()[:500] or None, assigned_by_name=actor_name(),
                        request_id=request.id if request else None)
    db.add(x)
    a.status = "assigned"
    if request is not None:
        request.status, request.fulfilled_at = "fulfilled", _now()
    db.flush()
    db.refresh(a)
    notify(db, company_id, emp.id, f"{a.name} ({a.asset_tag}) was given to you. Please confirm you received it.")
    return x


def get_assignment(db: Session, company_id: int, uuid) -> AssetAssignment:
    row = (db.query(AssetAssignment).options(joinedload(AssetAssignment.asset))
           .filter(AssetAssignment.company_id == company_id, AssetAssignment.uuid == uuid).first())
    if row is None:
        raise NotFound("Assignment")
    return row


def acknowledge(db: Session, user: dict, x: AssetAssignment) -> AssetAssignment:
    if x.employee_id != user.get("employee_id"):
        raise Forbidden("Only the person who received the asset can confirm it.")
    if x.status != "pending_ack":
        raise BadRequest("Already confirmed.")
    x.status, x.acknowledged_at = "active", _now()
    audit.event(db, "asset.acknowledged", "assets", x.asset_id, {"assignment": str(x.uuid)},
                entity_uuid=str(x.asset.uuid), entity_label=x.asset.asset_tag)
    return x


def return_asset(db: Session, company_id: int, x: AssetAssignment, *, returned_on: date | None = None,
                 condition: str | None = None, notes: str | None = None, recovery_amount=None,
                 recovery_reason: str | None = None, next_status: str | None = None) -> AssetAssignment:
    """Take an asset back. Damage or loss can carry a recovery amount, which final
    settlement deducts unless it is waived or recovered."""
    if x.status == "returned":
        raise BadRequest("This asset was already returned.")
    on = returned_on or _today(db, company_id)
    if on < x.assigned_on:
        raise BadRequest("The return date can't be before the assignment date.")
    condition = condition or x.asset.condition
    if condition not in CONDITIONS:
        raise BadRequest("Unknown condition.")
    amount = _money(recovery_amount)
    if amount and not (recovery_reason or "").strip():
        raise BadRequest("Say why the amount is recovered.")
    if amount and x.asset.purchase_value and amount > x.asset.purchase_value:
        raise BadRequest("The recovery can't be more than the purchase value.")
    x.status, x.returned_on, x.condition_in = "returned", on, condition
    x.return_notes = (notes or "").strip()[:500] or None
    x.received_by_name = actor_name()
    x.recovery_amount = amount
    x.recovery_reason = (recovery_reason or "").strip()[:300] or None
    x.recovery_status = "pending" if amount else "none"
    a = x.asset
    a.condition = "damaged" if condition == "lost" else condition
    status = next_status or ("lost" if condition == "lost" else ("in_repair" if condition == "damaged" else "available"))
    if status not in STATUSES or status == "assigned":
        raise BadRequest("Pick what happens to the asset next.")
    a.status = status
    if status == "retired":
        a.retired_on = on
    if amount:
        notify(db, company_id, x.employee_id, f"₹{amount} will be recovered for {a.name} ({a.asset_tag}): {x.recovery_reason}.")
    audit.event(db, "asset.returned", "assets", a.id, {"employee_id": x.employee_id, "condition": condition,
                                                       "recovery": str(amount) if amount else None},
                entity_uuid=str(a.uuid), entity_label=a.asset_tag)
    return x


def set_recovery(db: Session, x: AssetAssignment, status: str, note: str | None = None) -> AssetAssignment:
    if status not in ("pending", "recovered", "waived"):
        raise BadRequest("Recovery is pending, recovered or waived.")
    if not x.recovery_amount:
        raise BadRequest("There is no recovery on this return.")
    x.recovery_status = status
    audit.event(db, "asset.recovery", "assets", x.asset_id, {"status": status, "note": note, "amount": str(x.recovery_amount)},
                entity_uuid=str(x.asset.uuid), entity_label=x.asset.asset_tag)
    return x


def outstanding_assets(db: Session, company_id: int, employee_id: int) -> list[dict]:
    rows = (db.query(AssetAssignment).options(joinedload(AssetAssignment.asset))
            .filter(AssetAssignment.company_id == company_id, AssetAssignment.employee_id == employee_id,
                    AssetAssignment.status.in_(("pending_ack", "active"))).all())
    return [{"assignment_uuid": str(x.uuid), "asset_uuid": str(x.asset.uuid), "asset_tag": x.asset.asset_tag,
             "name": x.asset.name, "assigned_on": x.assigned_on, "purchase_value": x.asset.purchase_value} for x in rows]


def recoveries(db: Session, company_id: int, employee_id: int | None = None, status: str | None = "pending") -> dict:
    q = (db.query(AssetAssignment).options(joinedload(AssetAssignment.asset))
         .filter(AssetAssignment.company_id == company_id, AssetAssignment.recovery_amount.isnot(None)))
    if employee_id:
        q = q.filter(AssetAssignment.employee_id == employee_id)
    if status:
        q = q.filter(AssetAssignment.recovery_status == status)
    rows = q.order_by(AssetAssignment.returned_on.desc()).all()
    names = names_for(db, company_id, [x.employee_id for x in rows])
    items = [{"assignment_uuid": str(x.uuid), "employee_id": x.employee_id, "employee_name": names.get(x.employee_id),
              "asset_uuid": str(x.asset.uuid), "asset_tag": x.asset.asset_tag, "asset_name": x.asset.name,
              "amount": x.recovery_amount, "reason": x.recovery_reason, "status": x.recovery_status,
              "returned_on": x.returned_on, "condition_in": x.condition_in} for x in rows]
    total = sum((x.recovery_amount for x in rows if x.recovery_status == "pending"), Decimal("0.00"))
    return {"items": items, "total_pending": total}


def mark_recovered(db: Session, company_id: int, assignment_uuid, settlement_ref: str | None = None) -> None:
    x = get_assignment(db, company_id, assignment_uuid)
    set_recovery(db, x, "recovered", f"Recovered in final settlement {settlement_ref or ''}".strip())


# ── Maintenance ─────────────────────────────────────────────────────

def add_maintenance(db: Session, company_id: int, a: Asset, data: dict, row: AssetMaintenance | None = None) -> AssetMaintenance:
    kind = data.get("kind") or "repair"
    if kind not in MAINT_KINDS:
        raise BadRequest("Unknown kind of maintenance.")
    status = data.get("status") or "done"
    if status not in ("scheduled", "in_progress", "done"):
        raise BadRequest("Status is scheduled, in progress or done.")
    if not data.get("performed_on"):
        raise BadRequest("Pick the date.")
    if row is None:
        row = AssetMaintenance(company_id=company_id, asset_id=a.id, created_by_name=actor_name(),
                               performed_on=data["performed_on"])
        db.add(row)
    row.kind, row.status, row.performed_on = kind, status, data["performed_on"]
    row.vendor = (data.get("vendor") or "").strip()[:200] or None
    row.cost = _money(data.get("cost"))
    row.description = (data.get("description") or "").strip() or None
    row.next_due_on = data.get("next_due_on")
    if status == "in_progress" and a.status == "available":
        a.status = "in_repair"
    elif status == "done" and a.status == "in_repair":
        a.status = "available"
        if a.condition == "damaged":
            a.condition = "good"
    db.flush()
    return row


# ── Requests (through approvals) ────────────────────────────────────

def get_request(db: Session, company_id: int, uuid) -> AssetRequest:
    row = db.query(AssetRequest).filter(AssetRequest.company_id == company_id, AssetRequest.uuid == uuid).first()
    if row is None:
        raise NotFound("Request")
    return row


def submit_request(db: Session, user: dict, data: dict) -> AssetRequest:
    from core.workflow import service as workflow
    cid = user["company_id"]
    emp = employee(db, cid, user.get("employee_id"))
    if emp is None:
        raise BadRequest("Only employees can request assets.")
    if not data.get("category_id"):
        raise BadRequest("Pick what you need.")
    from core.masters import labels_for, validate_value_ids
    validate_value_ids(db, cid, "asset_category", [data["category_id"]])
    label = labels_for(db, cid, [data["category_id"]]).get(data["category_id"])
    if not (data.get("details") or "").strip():
        raise BadRequest("Say why you need it.")
    req = AssetRequest(company_id=cid, employee_id=emp.id, category_id=data["category_id"],
                       details=data["details"].strip()[:1000], needed_by=data.get("needed_by"), status="pending")
    db.add(req)
    db.flush()
    inst = workflow.submit(db, cid, "assets.request",
                           {"request_id": req.id, "employee_id": emp.id, "category": label, "details": req.details,
                            "needed_by": str(req.needed_by) if req.needed_by else None},
                           f"Asset request: {label} for {emp.first_name} {emp.last_name}",
                           subject_type="asset_requests", subject_id=req.id)
    req.workflow_instance_id = inst.id
    if inst.status == "approved" and req.status == "pending":
        req.status = "approved"
    return req


# ── Reports ─────────────────────────────────────────────────────────

def report(db: Session, company_id: int) -> dict:
    from core.masters import labels_for
    today = _today(db, company_id)
    assets = db.query(Asset).filter(Asset.company_id == company_id).all()
    cats = labels_for(db, company_id, [a.category_id for a in assets if a.category_id])
    by_cat: dict = {}
    for a in assets:
        b = by_cat.setdefault(a.category_id, {"category": cats.get(a.category_id, "Uncategorised"), "total": 0,
                                              "assigned": 0, "available": 0, "in_repair": 0, "lost": 0, "retired": 0,
                                              "value": Decimal("0.00")})
        b["total"] += 1
        b[a.status] = b.get(a.status, 0) + 1
        if a.status != "retired" and a.purchase_value:
            b["value"] += a.purchase_value
    warranty = sorted([a for a in assets if a.warranty_until and today <= a.warranty_until <= today + timedelta(days=60)
                       and a.status != "retired"], key=lambda a: a.warranty_until)
    pending_ack = (db.query(func.count(AssetAssignment.id)).filter(AssetAssignment.company_id == company_id,
                                                                   AssetAssignment.status == "pending_ack").scalar())
    overdue = (db.query(AssetAssignment).options(joinedload(AssetAssignment.asset))
               .filter(AssetAssignment.company_id == company_id, AssetAssignment.status.in_(("active", "pending_ack")),
                       AssetAssignment.expected_return_on < today).all())
    names = names_for(db, company_id, [x.employee_id for x in overdue])
    maint_cost = (db.query(func.coalesce(func.sum(AssetMaintenance.cost), 0))
                  .filter(AssetMaintenance.company_id == company_id,
                          AssetMaintenance.performed_on >= today.replace(month=1, day=1)).scalar())
    rec = recoveries(db, company_id)
    return {
        "total": len(assets), "value": sum((b["value"] for b in by_cat.values()), Decimal("0.00")),
        "by_status": {s: sum(1 for a in assets if a.status == s) for s in STATUSES},
        "by_category": sorted(by_cat.values(), key=lambda b: -b["total"]),
        "warranty_expiring": [{"uuid": str(a.uuid), "asset_tag": a.asset_tag, "name": a.name,
                               "warranty_until": a.warranty_until} for a in warranty],
        "pending_acknowledgement": pending_ack,
        "overdue_returns": [{"asset_tag": x.asset.asset_tag, "name": x.asset.name, "employee_name": names.get(x.employee_id),
                             "expected_return_on": x.expected_return_on} for x in overdue],
        "maintenance_cost_this_year": maint_cost, "recoveries_pending": rec["total_pending"],
        "recoveries_pending_count": len(rec["items"]),
    }
