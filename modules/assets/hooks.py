"""assets.request: an employee asks for an asset (a laptop, a phone, an access card).
When approved, the request waits for the asset team to hand over an asset."""
from sqlalchemy.orm import Session

from core.workflow.registry import RequestType, register


def _request(db: Session, instance):
    from modules.assets.models import AssetRequest
    return db.query(AssetRequest).filter(AssetRequest.company_id == instance.company_id,
                                         AssetRequest.id == (instance.payload or {}).get("request_id")).first()


def _set(status: str):
    def handler(db: Session, instance) -> None:
        req = _request(db, instance)
        if req is not None and req.status == "pending":
            req.status = status
            if status == "approved":
                from modules.employees.models import Employee
                from modules.notifications.models import Notification
                from datetime import datetime, timezone
                for (eid,) in db.query(Employee.id).filter(Employee.company_id == instance.company_id,
                                                           Employee.role.in_(["hr", "admin"]),
                                                           Employee.active.is_(True)).all():
                    db.add(Notification(company_id=instance.company_id, employee_id=eid, channel="in_app",
                                        message_type="asset", status="sent", sent_at=datetime.now(timezone.utc),
                                        message_content=f"Approved: {instance.title}. Hand over an asset from the register."))
    return handler


def _summarize(instance) -> list[str]:
    p = instance.payload or {}
    out = [f"Needs: {p.get('category') or 'an asset'}"]
    if p.get("details"):
        out.append(p["details"])
    if p.get("needed_by"):
        out.append(f"Needed by {p['needed_by']}")
    return out


register(RequestType(
    key="assets.request",
    label="Asset request",
    description="An employee asks for a laptop, phone, access card or other company asset.",
    on_approved=_set("approved"),
    on_rejected=_set("rejected"),
    on_cancelled=_set("cancelled"),
    summarize=_summarize,
    variables={"request.category": "What is needed"},
    default_steps=[{"name": "Manager approval", "approver_type": "reporting_manager", "mode": "any"},
                   {"name": "HR approval", "approver_type": "role", "approver_value": "hr", "mode": "any"}],
))
