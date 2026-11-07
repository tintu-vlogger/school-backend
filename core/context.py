"""Request context: who is acting, for which company, from where.

A fresh `RequestContext` is put in a context variable at the start of every HTTP
request (see `RequestContextMiddleware`). Authentication fills it in from the token.
FastAPI runs sync dependencies and endpoints in worker threads with a *copy* of the
context, so the context holds one mutable object: every thread of the same request
sees the same object and the changes made to it.

Background jobs and scripts use `use_context(...)` to set their own context.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class RequestContext:
    company_id: int | None = None
    employee_id: int | None = None
    platform_user_id: int | None = None
    # Parent or student portal sign-in (portal_accounts.id).
    portal_account_id: int | None = None
    # "owner" (company owner login), "employee", "parent", "student", "platform", "system" or "anonymous"
    actor_type: str = "anonymous"
    actor_name: str | None = None
    role: str | None = None
    timezone: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    bypass_rls: bool = False


_current: ContextVar[RequestContext | None] = ContextVar("hrms_request_context", default=None)


def current() -> RequestContext:
    """The context of the running request or job (an empty one outside of both)."""
    ctx = _current.get()
    if ctx is None:
        ctx = RequestContext(actor_type="system")
        _current.set(ctx)
    return ctx


@contextmanager
def use_context(ctx: RequestContext):
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)


def _client_ip(scope) -> str | None:
    for name, value in scope.get("headers") or []:
        if name == b"x-forwarded-for":
            return value.decode("latin-1").split(",")[0].strip()
    client = scope.get("client")
    return client[0] if client else None


class RequestContextMiddleware:
    """Pure ASGI middleware: one RequestContext per request, and an X-Request-ID header
    on the response so a log line can be matched to an audit entry."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        incoming = headers.get(b"x-request-id", b"").decode("latin-1").strip()
        ctx = RequestContext(
            ip_address=_client_ip(scope),
            user_agent=(headers.get(b"user-agent", b"").decode("latin-1") or None),
        )
        if incoming and len(incoming) <= 64:
            ctx.request_id = incoming
        token = _current.set(ctx)

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + [(b"x-request-id", ctx.request_id.encode("latin-1"))]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            _current.reset(token)
