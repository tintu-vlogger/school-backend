"""Approval request types and background jobs of the documents module.

documents.letter_request: an employee asks for a letter (salary certificate, address
proof, experience letter …) from a self-service template. When approved, the letter is
generated, filed in the employee's documents and the employee is told.
"""
from sqlalchemy.orm import Session

from core import periodic
from core.errors import BadRequest
from core.workflow.registry import RequestType, register


def _on_submit(db: Session, instance) -> None:
    from modules.documents import service
    from modules.documents import settings as doc_settings
    from modules.documents.models import LetterTemplate
    payload = dict(instance.payload or {})
    cid = instance.company_id
    if not doc_settings.get(db, cid).get("self_service_letters", True):
        raise BadRequest("Letter requests are switched off. Contact HR.")
    t = (db.query(LetterTemplate).filter(LetterTemplate.company_id == cid,
                                         LetterTemplate.id == payload.get("template_id")).first())
    if t is None or not t.active or not t.self_service:
        raise BadRequest("This letter can't be requested. Pick one from the list.")
    emp = service.employee(db, cid, payload.get("employee_id"))
    if emp is None:
        raise BadRequest("Only employees can request letters.")
    reason = None
    if payload.get("reason_id"):
        from core.masters import labels_for
        reason = labels_for(db, cid, [payload["reason_id"]]).get(payload["reason_id"])
    if (reason or "").lower() == "other" and not (payload.get("reason_text") or "").strip():
        raise BadRequest("Say why you need the letter.")
    payload.update({"template_name": t.name, "letter_type": t.letter_type, "reason": reason,
                    "employee_name": service.display_name(emp)})
    instance.payload = payload
    instance.subject_type, instance.subject_id = "employees", emp.id


def _on_approved(db: Session, instance) -> None:
    from modules.documents import service
    from modules.documents.models import LetterTemplate
    p = instance.payload or {}
    t = db.query(LetterTemplate).filter(LetterTemplate.company_id == instance.company_id,
                                        LetterTemplate.id == p.get("template_id")).first()
    emp = service.employee(db, instance.company_id, p.get("employee_id"))
    if t is None or emp is None:
        raise BadRequest("The template or the employee no longer exists.")
    values = {"letter.purpose": p.get("purpose") or "To whom it may concern",
              "letter.reason": p.get("reason_text") or p.get("reason"), "letter.details": p.get("details")}
    letter = service.generate_letter(db, instance.company_id, t, emp, {k: v for k, v in values.items() if v},
                                     workflow_instance_id=instance.id)
    instance.payload = {**p, "letter_uuid": str(letter.uuid)}


def _summarize(instance) -> list[str]:
    p = instance.payload or {}
    lines = [p.get("template_name") or "Letter"]
    if p.get("reason") or p.get("reason_text"):
        lines.append(f"Reason: {p.get('reason_text') or p.get('reason')}")
    if p.get("purpose"):
        lines.append(f"Addressed to: {p['purpose']}")
    return lines


register(RequestType(
    key="documents.letter_request",
    label="Letter request",
    description="An employee asks for a letter such as a salary certificate, address proof or experience letter.",
    on_submit=_on_submit,
    on_approved=_on_approved,
    summarize=_summarize,
    variables={"request.letter_type": "Kind of letter", "request.reason": "Reason given"},
    default_steps=[{"name": "HR approval", "approver_type": "role", "approver_value": "hr", "mode": "any"}],
))


def _reminders(db: Session) -> int:
    from modules.documents import service
    return service.run_reminders(db)


periodic.register("documents.reminders", _reminders)
