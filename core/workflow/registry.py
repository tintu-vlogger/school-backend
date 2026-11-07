"""Request types and their handlers. A module registers its request types in its
hooks.py, e.g.

    from core.workflow.registry import RequestType, register
    register(RequestType(key="leave.request", label="Leave request", on_approved=apply_leave, ...))

Handlers receive (db, instance) and run inside the same transaction as the decision.
`on_submit` may raise BadRequest to refuse a request.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class RequestType:
    key: str
    label: str
    description: str = ""
    on_submit: Callable | None = None
    on_approved: Callable | None = None
    on_rejected: Callable | None = None
    on_cancelled: Callable | None = None
    # Undo an already-approved request (cancel after approval). Only request types that
    # set it can be cancelled once approved; see service.revoke().
    on_revoked: Callable | None = None
    # Fields a condition may use, shown on the approval flow screen: {"request.days": "Number of days"}
    variables: dict[str, str] = field(default_factory=dict)
    # Steps of the flow every company gets by default
    default_steps: list[dict] = field(default_factory=list)
    # Short lines describing a request for the inbox: (instance) -> list[str]
    summarize: Callable | None = None


_REGISTRY: dict[str, RequestType] = {}
# Request types a company creates on screen (e.g. custom services), supplied by a module:
# (get(key) -> RequestType | None, for_company(company_id) -> list[RequestType]).
_PROVIDERS: list[tuple[Callable, Callable]] = []


def register(rt: RequestType) -> RequestType:
    _REGISTRY[rt.key] = rt
    return rt


def add_provider(get_fn: Callable, list_fn: Callable) -> None:
    """Add request types that belong to one company and are defined at run time. `get_fn(key)`
    finds one by key (any company; used when a request is decided), `list_fn(company_id)` lists
    a company's own (shown only to that company)."""
    _PROVIDERS.append((get_fn, list_fn))


def get(key: str) -> RequestType | None:
    rt = _REGISTRY.get(key)
    if rt is None:
        for get_fn, _ in _PROVIDERS:
            rt = get_fn(key)
            if rt is not None:
                break
    return rt


def all_types() -> list[RequestType]:
    """Every built-in request type plus the signed-in company's own."""
    out = list(_REGISTRY.values())
    if _PROVIDERS:
        from core import context
        company_id = context.current().company_id
        if company_id:
            for _, list_fn in _PROVIDERS:
                out.extend(list_fn(company_id))
    return sorted(out, key=lambda r: r.label)


COMMON_VARIABLES = {
    "requester.role": "Requester's role (admin, hr, manager, employee)",
    "requester.department_id": "Requester's department",
    "requester.designation_id": "Requester's designation",
    "requester.employee_code": "Requester's employee ID",
    "request_type": "Kind of request",
}
