"""HR processes: the `hr.process` approval request type, the permission and the job that
applies approved changes on their effective date."""
from sqlalchemy.orm import Session

from core import periodic
from core.workflow.registry import RequestType, register
from modules.roles.models import ALL_PERMISSIONS, DEFAULT_ROLE_PERMISSIONS

PERMISSIONS = {"people.processes.manage": "Set up HR processes and raise, revoke and see job changes"}
for _k, _v in PERMISSIONS.items():
    ALL_PERMISSIONS.setdefault(_k, _v)
for _role in ("admin", "hr"):
    _lst = DEFAULT_ROLE_PERMISSIONS.setdefault(_role, [])
    for _p in PERMISSIONS:
        if _p not in _lst:
            _lst.append(_p)


def _svc():
    from modules.hr_processes import service
    return service


def _on_approved(db: Session, instance) -> None:
    req = _svc().for_instance(db, instance)
    if req is not None:
        _svc().approve(db, req)


def _closer(status: str):
    def handler(db: Session, instance) -> None:
        req = _svc().for_instance(db, instance)
        if req is not None:
            _svc().close(db, req, status)
    return handler


def _on_revoked(db: Session, instance) -> None:
    req = _svc().for_instance(db, instance)
    if req is not None and req.status in ("scheduled", "applied"):
        _svc().revoke(db, req, instance.result_note or "Approved request cancelled")


def _summarize(instance) -> list[str]:
    return list((instance.payload or {}).get("summary") or [])


_rt = register(RequestType(
    key="hr.process",
    label="HR process (transfer, promotion …)",
    description="A change to an employee's job raised under an HR process. Target one process with "
                "request.process_code == 'TRF'.",
    on_approved=_on_approved,
    on_rejected=_closer("rejected"),
    on_cancelled=_closer("cancelled"),
    summarize=_summarize,
    variables={"request.process_code": "Process code (TRF, PRM, CNF …)",
               "request.employee_id": "Employee being changed",
               "request.manager_id": "That employee's reporting manager",
               "request.department_id": "That employee's department",
               "request.location_id": "That employee's location",
               "request.fields": "Fields that change"},
    default_steps=[
        {"name": "Employee's manager", "approver_type": "field", "approver_value": "request.manager_id", "mode": "any"},
        {"name": "HR approval", "approver_type": "role", "approver_value": "hr", "mode": "any"},
    ],
))
_rt.on_revoked = _on_revoked  # used by the engine's "cancel an approved request"


def _apply_due(db: Session) -> int:
    return _svc().apply_due(db)


periodic.register("hr_processes.apply_due", _apply_due)
