"""Data assistant, integrations (Settings → Integrations) and personal access tokens.
The MCP server itself lives in modules/assistant/mcp.py."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.db import get_db
from core.errors import Forbidden
from modules.assistant import integrations, mcp, service, tools
from modules.reports.registry import Principal, get_principal

router = APIRouter(prefix="/assistant", tags=["Assistant"])
integrations_router = APIRouter(prefix="/integrations", tags=["Integrations"])


class AskBody(BaseModel):
    question: str
    thread_uuid: str | None = None


class IntegrationBody(BaseModel):
    enabled: bool | None = None
    config: dict = {}
    clear_secrets: list[str] | None = None


class TokenBody(BaseModel):
    name: str
    scopes: list[str] | None = None
    expires_in_days: int | None = None


# ── Assistant ──────────────────────────────────────────────────────

@router.get("/status")
def status(db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    return service.status(db, p)


@router.post("/ask")
def ask(body: AskBody, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    p.via = "assistant"
    res = service.ask(db, p, body.question, body.thread_uuid)
    db.commit()
    return res


@router.get("/threads")
def threads(db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    return service.threads(db, p)


@router.get("/threads/{uuid}")
def thread(uuid: UUID, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    t = service.get_thread(db, p, uuid)
    return {"uuid": str(t.uuid), "title": t.title, "messages": t.messages or [], "updated_at": t.updated_at}


@router.delete("/threads/{uuid}", status_code=204)
def delete_thread(uuid: UUID, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    db.delete(service.get_thread(db, p, uuid))
    db.commit()


# ── Integrations ───────────────────────────────────────────────────

def _admin(p: Principal) -> None:
    if not (p.is_owner or p.role == "admin" or p.can("settings.integrations.manage")):
        raise Forbidden("Only the company owner and HR admins can change integrations.")


@integrations_router.get("/")
def list_integrations(db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    _admin(p)
    return {"integrations": integrations.list_all(db, p.company_id),
            "mcp": {"path": "/api/v1/mcp", "sse_path": "/api/v1/mcp/sse",
                    "tools": [{"name": t["name"], "description": t["description"]} for t in tools.TOOLS]}}


@integrations_router.put("/{kind}")
def save_integration(kind: str, body: IntegrationBody, db: Session = Depends(get_db),
                     p: Principal = Depends(get_principal)):
    _admin(p)
    r = integrations.save(db, p.company_id, kind, body.model_dump(exclude_unset=True), p.name or "Company owner")
    db.commit()
    return integrations.out(kind, r)


@integrations_router.post("/{kind}/test")
def test_integration(kind: str, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    _admin(p)
    r = integrations.test(db, p.company_id, kind)
    db.commit()
    return integrations.out(kind, r)


@integrations_router.get("/tokens/mine")
def my_tokens(everyone: bool = False, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    if everyone:
        _admin(p)
    return {"tokens": service.tokens_for(db, p, everyone), "can_connect": service.can_connect(p),
            "tools": [{"name": t["name"], "description": t["description"]} for t in tools.TOOLS]}


@integrations_router.post("/tokens", status_code=201)
def create_token(body: TokenBody, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    t, raw = service.create_token(db, p, body.model_dump())
    db.commit()
    return {**service.token_out(t), "token": raw}


@integrations_router.delete("/tokens/{uuid}")
def revoke_token(uuid: UUID, db: Session = Depends(get_db), p: Principal = Depends(get_principal)):
    t = service.revoke_token(db, p, uuid)
    db.commit()
    return service.token_out(t)


extra_routers = [integrations_router, mcp.router]
