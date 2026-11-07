"""The data assistant: answer questions about HR data the person may see, with the
company's AI model when one is set up, else with the built-in question matcher. Either
way the data comes only from the safe tools (modules/assistant/tools.py)."""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core import audit
from core.errors import BadRequest, Forbidden, NotFound
from modules.assistant import integrations, intents, providers, tools
from modules.assistant.models import AccessToken, AssistantThread
from modules.reports.registry import Principal

MAX_HISTORY = 12
TOKEN_PREFIX = "ngh_pat_"


def now() -> datetime:
    return datetime.now(timezone.utc)


def model_config(db: Session, company_id: int) -> dict | None:
    cfg = integrations.get_config(db, company_id, "ai_model")
    if not cfg or not cfg.get("model"):
        return None
    return cfg


def status(db: Session, p: Principal) -> dict:
    cfg = model_config(db, p.company_id)
    label = None
    if cfg:
        label = {"openai": "OpenAI-compatible", "anthropic": "Anthropic", "gemini": "Google Gemini"}.get(
            cfg.get("provider"), "Model") + f" · {cfg.get('model')}"
    return {"mode": "model" if cfg else "builtin", "model_label": label, "examples": intents.EXAMPLES,
            "tools": [{"name": t["name"], "description": t["description"]} for t in tools.TOOLS],
            "can_configure": p.is_owner or p.role == "admin" or p.can("settings.integrations.manage")}


def _system_prompt(db: Session, p: Principal) -> str:
    from core.timeutil import today_for_company_id
    from modules.company.models import Company
    c = db.query(Company).filter(Company.id == p.company_id).first()
    return (
        f"You are the HR data assistant of {c.company_name if c else 'the company'}, an Indian company using "
        f"Campusly. Today is {today_for_company_id(db, p.company_id):%A %d %B %Y}. You are talking to "
        f"{p.name or 'the company owner'}.\n"
        "Answer questions about people, attendance, leave, pay and other HR data using only the tools. The tools "
        "return only what this person is allowed to see; never guess or invent numbers, names or policies. If a "
        "tool returns an error or no rows, say so plainly. If some values are hidden, say the person does not "
        "have access to them. Keep answers short and plain, use Indian formats (₹, DD Mon YYYY), and do not "
        "repeat whole tables: the app shows the tool's table below your answer."
    )


def _blocks_from(result: dict | None, tool_name: str | None) -> list[dict]:
    if not isinstance(result, dict) or result.get("error"):
        return []
    blocks = []
    rows = result.get("rows") or []
    if result.get("value") is not None and len(rows) <= 1 and tool_name in ("headcount", "joiners_leavers",
                                                                            "who_is_on_leave", "pending_approvals"):
        blocks.append({"type": "kpi", "value": result["value"], "label": result.get("summary")})
    if rows and not (len(rows) == 1 and tool_name == "headcount"):
        blocks.append({"type": "table", "columns": result.get("columns") or list(rows[0].keys()), "rows": rows,
                       "note": result.get("note"), "totals": result.get("totals")})
        chart = result.get("chart")
        if chart and len(result.get("columns") or []) >= 2 and 1 < len(rows) <= 30:
            cols = result["columns"]
            blocks.append({"type": "chart", "chart": chart.get("type") if chart.get("type") in (
                "bar", "line", "donut", "pie") else "bar", "x": cols[0], "y": cols[-1], "rows": rows})
    if result.get("scope"):
        blocks.append({"type": "scope", "text": result["scope"]})
    return blocks


def ask(db: Session, p: Principal, question: str, thread_uuid: str | None = None) -> dict:
    question = (question or "").strip()
    if not question:
        raise BadRequest("Type a question.")
    if len(question) > 2000:
        raise BadRequest("Keep the question under 2,000 characters.")
    thread = None
    if thread_uuid:
        thread = get_thread(db, p, thread_uuid)
    if thread is None:
        thread = AssistantThread(company_id=p.company_id, employee_id=p.employee_id, title=question[:200], messages=[])
        db.add(thread)
        db.flush()
    history = [{"role": m["role"], "content": m["text"]} for m in (thread.messages or [])[-MAX_HISTORY:]
               if m.get("text")]
    cfg = model_config(db, p.company_id)
    calls: list[dict] = []
    last: dict = {"result": None, "name": None}

    def run_tool(name: str, args: dict) -> dict:
        res = tools.call(db, p, name, args)
        calls.append({"name": name, "args": args, "rows": res.get("row_count") if isinstance(res, dict) else None,
                      "error": res.get("error") if isinstance(res, dict) else None})
        last["result"], last["name"] = res, name
        return res

    mode = "builtin"
    if cfg:
        mode = "model"
        try:
            text, _log = providers.chat(cfg, _system_prompt(db, p), history + [{"role": "user", "content": question}],
                                        tools.specs(), run_tool)
        except BadRequest as exc:
            text = f"The AI model could not answer ({exc.detail}). Here is what I can tell you directly."
            mode = "builtin"
            m = intents.match(question)
            if m:
                res = run_tool(*m)
                text += "\n\n" + (res.get("summary") or res.get("error") or "")
    else:
        m = intents.match(question)
        if m is None:
            text = ("I can answer common questions without an AI model, such as:\n• " + "\n• ".join(intents.EXAMPLES)
                    + "\n\nTo ask anything in your own words, connect an AI model in Settings → Integrations.")
        else:
            res = run_tool(*m)
            text = res.get("error") and f"I couldn't do that: {res['error']}" or res.get("summary") or "Done."
    blocks = _blocks_from(last["result"], last["name"])
    stamp = now().isoformat()
    msgs = list(thread.messages or [])
    msgs.append({"role": "user", "text": question, "at": stamp})
    msgs.append({"role": "assistant", "text": text, "blocks": blocks, "tools": calls, "mode": mode, "at": stamp})
    thread.messages = msgs[-60:]
    flag_modified(thread, "messages")
    db.flush()
    return {"thread_uuid": str(thread.uuid), "answer": {"text": text, "blocks": blocks, "tools": calls, "mode": mode}}


def get_thread(db: Session, p: Principal, uuid) -> AssistantThread:
    t = db.query(AssistantThread).filter(AssistantThread.company_id == p.company_id, AssistantThread.uuid == uuid).first()
    if t is None or t.employee_id != p.employee_id:
        raise NotFound("Conversation")
    return t


def threads(db: Session, p: Principal) -> list[dict]:
    rows = db.query(AssistantThread).filter(AssistantThread.company_id == p.company_id,
                                            AssistantThread.employee_id.is_(None) if p.employee_id is None
                                            else AssistantThread.employee_id == p.employee_id
                                            ).order_by(AssistantThread.updated_at.desc()).limit(30).all()
    return [{"uuid": str(t.uuid), "title": t.title, "updated_at": t.updated_at, "messages": len(t.messages or [])}
            for t in rows]


# ── Personal access tokens (MCP) ───────────────────────────────────

def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def token_out(t: AccessToken) -> dict:
    state = "revoked" if t.revoked_at else ("expired" if t.expires_at and t.expires_at < now() else "active")
    return {"uuid": str(t.uuid), "name": t.name, "prefix": t.prefix, "scopes": t.scopes or [], "state": state,
            "owner_name": t.owner_name, "created_at": t.created_at, "expires_at": t.expires_at,
            "last_used_at": t.last_used_at, "use_count": t.use_count, "revoked_at": t.revoked_at}


def can_connect(p: Principal) -> bool:
    return p.is_owner or p.role == "admin" or p.can("insights.mcp.connect")


def create_token(db: Session, p: Principal, data: dict) -> tuple[AccessToken, str]:
    if not can_connect(p):
        raise Forbidden("You can't connect outside assistants. Ask HR for access.")
    name = (data.get("name") or "").strip()
    if not name:
        raise BadRequest("Name the token, for example the assistant you will use it with.")
    scopes = data.get("scopes") or ["*"]
    unknown = [s for s in scopes if s != "*" and s not in tools.BY_NAME]
    if unknown:
        raise BadRequest(f"Unknown tool {unknown[0]}.")
    days = data.get("expires_in_days")
    if days not in (None, "", 0):
        days = int(days)
        if not 1 <= days <= 365:
            raise BadRequest("A token can last from 1 to 365 days.")
    active = db.query(AccessToken.id).filter(AccessToken.company_id == p.company_id,
                                             AccessToken.employee_id == p.employee_id if p.employee_id
                                             else AccessToken.employee_id.is_(None),
                                             AccessToken.revoked_at.is_(None)).count()
    if active >= 10:
        raise BadRequest("You already have 10 tokens. Revoke one you no longer use.")
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    t = AccessToken(company_id=p.company_id, employee_id=p.employee_id, owner_name=p.name or "Company owner",
                    name=name[:100], token_hash=hash_token(raw), prefix=raw[:12], scopes=scopes,
                    expires_at=now() + timedelta(days=days) if days else None)
    db.add(t)
    db.flush()
    audit.event(db, "mcp.token_created", "access_tokens", t.id, {"name": t.name, "scopes": scopes,
                                                                 "expires_at": t.expires_at},
                entity_uuid=str(t.uuid), entity_label=t.name)
    return t, raw


def tokens_for(db: Session, p: Principal, everyone: bool = False) -> list[dict]:
    q = db.query(AccessToken).filter(AccessToken.company_id == p.company_id)
    if not everyone:
        q = q.filter(AccessToken.employee_id == p.employee_id if p.employee_id else AccessToken.employee_id.is_(None))
    return [token_out(t) for t in q.order_by(AccessToken.created_at.desc()).all()]


def revoke_token(db: Session, p: Principal, uuid) -> AccessToken:
    t = db.query(AccessToken).filter(AccessToken.company_id == p.company_id, AccessToken.uuid == uuid).first()
    admin = p.is_owner or p.role == "admin" or p.can("settings.integrations.manage")
    if t is None or (t.employee_id != p.employee_id and not admin):
        raise NotFound("Token")
    if t.revoked_at is None:
        t.revoked_at = now()
        audit.event(db, "mcp.token_revoked", "access_tokens", t.id, {"name": t.name}, entity_uuid=str(t.uuid),
                    entity_label=t.name)
    return t
