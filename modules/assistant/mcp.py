"""Model Context Protocol server at /api/v1/mcp.

Any MCP-capable assistant can connect with a personal access token (Settings →
Integrations → MCP). Transports:

* Streamable HTTP: POST /api/v1/mcp with a JSON-RPC message (or a batch); the answer is
  JSON, or a one-event SSE stream when the client only accepts text/event-stream.
* HTTP + SSE (older clients): GET /api/v1/mcp/sse opens a stream whose first event names
  the message address (/api/v1/mcp/messages?session_id=…); answers arrive on the stream.

Authentication: `Authorization: Bearer ngh_pat_…` on every request. The token acts as its
owner with their current permissions and row-level scope, limited to the tools chosen
for the token. Every tool call is audited.
"""
from __future__ import annotations

import asyncio
import json
import uuid as _uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy.orm import Session

from core import context
from core.db import bypass_rls, get_db, set_tenant
from modules.assistant import service, tools
from modules.assistant.models import AccessToken
from modules.reports.registry import Principal, principal_for_employee

router = APIRouter(prefix="/mcp", tags=["MCP"])
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "next-gen-hrms", "title": "Next Gen HRMS", "version": "1.0.0"}
INSTRUCTIONS = ("HR data of one company, limited to what the token's owner may see. Use list_reports to discover "
                "reports and run_report to run them; headcount, leave, attendance and approvals have their own tools.")


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.message, self.status = message, status


def authenticate(db: Session, header: str | None) -> tuple[AccessToken, Principal]:
    raw = (header or "").removeprefix("Bearer ").strip()
    if not raw.startswith(service.TOKEN_PREFIX):
        raise AuthError("Send a personal access token: Authorization: Bearer ngh_pat_…")
    with bypass_rls(db):
        t = db.query(AccessToken).filter(AccessToken.token_hash == service.hash_token(raw)).first()
    if t is None or t.revoked_at is not None:
        raise AuthError("This token is not valid or was revoked.")
    if t.expires_at and t.expires_at < datetime.now(timezone.utc):
        raise AuthError("This token has expired. Make a new one in Settings → Integrations.")
    from core.security import company_status
    set_tenant(db, t.company_id)
    if company_status(db, t.company_id) != "active":
        raise AuthError("This company is not active.", 403)
    try:
        p = principal_for_employee(db, t.company_id, t.employee_id, "mcp")
    except PermissionError as exc:
        raise AuthError(str(exc), 403)
    if not service.can_connect(p):
        raise AuthError("The token's owner no longer has access to outside assistants.", 403)
    ctx = context.current()
    ctx.company_id, ctx.employee_id, ctx.role = t.company_id, t.employee_id, p.role
    ctx.actor_type = "employee" if t.employee_id else "owner"
    ctx.actor_name = f"{p.name} (via MCP token {t.name})"
    t.last_used_at = datetime.now(timezone.utc)
    t.use_count = (t.use_count or 0) + 1
    return t, p


def _result(id_, result) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def handle(db: Session, t: AccessToken, p: Principal, msg: dict) -> dict | None:
    """One JSON-RPC message. Returns the response, or None for notifications."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid request")
    method, id_, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if id_ is None:              # notification (initialized, cancelled …)
        return None
    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return _result(id_, {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                             "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS})
    if method == "ping":
        return _result(id_, {})
    if method == "tools/list":
        return _result(id_, {"tools": [{"name": s["name"], "description": s["description"],
                                        "inputSchema": s["input_schema"]} for s in tools.specs(t.scopes or ["*"])]})
    if method == "tools/call":
        name = params.get("name")
        if name not in tools.BY_NAME:
            return _error(id_, -32602, f"Unknown tool: {name}")
        res = tools.call(db, p, name, params.get("arguments") or {}, allowed=t.scopes or ["*"])
        is_error = isinstance(res, dict) and bool(res.get("error"))
        text = res.get("error") if is_error else json.dumps(res, default=str, ensure_ascii=False)
        out = {"content": [{"type": "text", "text": text}], "isError": is_error}
        if not is_error:
            out["structuredContent"] = json.loads(json.dumps(res, default=str))
        return _result(id_, out)
    if method in ("resources/list", "prompts/list"):
        return _result(id_, {"resources": []} if method == "resources/list" else {"prompts": []})
    return _error(id_, -32601, f"Method not found: {method}")


def _process(db: Session, header: str | None, body) -> tuple[int, object]:
    try:
        t, p = authenticate(db, header)
    except AuthError as exc:
        db.rollback()
        return exc.status, {"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": exc.message}}
    if isinstance(body, list):
        out = [r for r in (handle(db, t, p, m) for m in body) if r is not None]
        db.commit()
        return (200, out) if out else (202, None)
    r = handle(db, t, p, body)
    db.commit()
    return (200, r) if r is not None else (202, None)


@router.post("")
@router.post("/")
async def mcp_post(request: Request, db: Session = Depends(get_db)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                            status_code=400)
    status, payload = await asyncio.to_thread(_process, db, request.headers.get("authorization"), body)
    if status == 202:
        return Response(status_code=202)
    headers = {"WWW-Authenticate": 'Bearer realm="mcp"'} if status == 401 else {}
    accept = request.headers.get("accept", "")
    if status == 200 and "text/event-stream" in accept and "application/json" not in accept:
        data = f"event: message\ndata: {json.dumps(payload, default=str)}\n\n"
        return Response(content=data, media_type="text/event-stream", headers=headers)
    return JSONResponse(payload, status_code=status, headers=headers)


# ── HTTP + SSE transport ───────────────────────────────────────────

_SESSIONS: dict[str, asyncio.Queue] = {}


@router.get("")
@router.get("/sse")
async def mcp_sse(request: Request, db: Session = Depends(get_db)):
    try:
        await asyncio.to_thread(authenticate, db, request.headers.get("authorization"))
        db.commit()
    except AuthError as exc:
        db.rollback()
        return JSONResponse({"detail": exc.message}, status_code=exc.status,
                            headers={"WWW-Authenticate": 'Bearer realm="mcp"'})
    sid = _uuid.uuid4().hex
    queue: asyncio.Queue = asyncio.Queue()
    _SESSIONS[sid] = queue
    base = str(request.url.path).rstrip("/").removesuffix("/sse")

    async def stream():
        try:
            yield f"event: endpoint\ndata: {base}/messages?session_id={sid}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"event: message\ndata: {json.dumps(item, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            _SESSIONS.pop(sid, None)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.post("/messages")
async def mcp_messages(request: Request, session_id: str, db: Session = Depends(get_db)):
    queue = _SESSIONS.get(session_id)
    if queue is None:
        return JSONResponse({"detail": "Unknown session; open /api/v1/mcp/sse first."}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Parse error"}, status_code=400)
    status, payload = await asyncio.to_thread(_process, db, request.headers.get("authorization"), body)
    if status in (401, 403):
        return JSONResponse(payload, status_code=status)
    if payload is not None:
        for item in payload if isinstance(payload, list) else [payload]:
            await queue.put(item)
    return Response(status_code=202)
