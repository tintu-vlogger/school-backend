"""Custom services: permissions, the approval request type of every service (registered at
run time, one per service, so its flow can be set up under Approval flows) and the
handlers that move a record when its approval is decided."""
from sqlalchemy.orm import Session

from core.workflow import registry
from modules.roles.models import ALL_PERMISSIONS, DEFAULT_ROLE_PERMISSIONS

PERMISSIONS = {
    "settings.services.manage": "Create and change custom services, their forms, statuses and templates",
    "people.services.records": "See and handle the records of every custom service",
}
for _k, _v in PERMISSIONS.items():
    ALL_PERMISSIONS.setdefault(_k, _v)
for _role, _perms in {"admin": list(PERMISSIONS), "hr": list(PERMISSIONS)}.items():
    _lst = DEFAULT_ROLE_PERMISSIONS.setdefault(_role, [])
    for _p in _perms:
        if _p not in _lst:
            _lst.append(_p)


def _svc():
    from modules.custom_forms import service
    return service


def on_approved(db: Session, instance) -> None:
    _svc().on_decided(db, instance, "approved")


def on_rejected(db: Session, instance) -> None:
    _svc().on_decided(db, instance, "rejected")


def on_cancelled(db: Session, instance) -> None:
    _svc().on_decided(db, instance, "cancelled")


def on_revoked(db: Session, instance) -> None:
    _svc().on_decided(db, instance, "cancelled")


def summarize(instance) -> list[str]:
    return list((instance.payload or {}).get("summary") or [])


def _get(key: str):
    if not key.startswith("form."):
        return None
    return _svc().type_for_key(key)


def _list(company_id: int) -> list:
    return _svc().types_for_company(company_id)


registry.add_provider(_get, _list)
