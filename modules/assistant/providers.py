"""Talking to the company's chosen model, with tool calling, in three wire formats:
OpenAI-compatible chat completions, Anthropic messages and Google Gemini.

`chat(cfg, system, history, tools, call_tool)` runs the tool loop: the model may ask for
tools; each request goes through `call_tool(name, args)` (which applies the asking
person's access) and the result is sent back, up to MAX_ROUNDS times.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx

from core.errors import BadRequest

MAX_ROUNDS = 6
TIMEOUT = 60.0


def _post(url: str, headers: dict, body: dict) -> dict:
    try:
        r = httpx.post(url, headers=headers, json=body, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise BadRequest(f"Couldn't reach the AI model: {exc}")
    if r.status_code >= 400:
        try:
            detail = r.json()
            detail = detail.get("error", detail)
            if isinstance(detail, dict):
                detail = detail.get("message") or json.dumps(detail)[:200]
        except Exception:
            detail = r.text[:200]
        raise BadRequest(f"The AI model returned an error ({r.status_code}): {detail}")
    return r.json()


def _temp(cfg: dict) -> float:
    try:
        return float(cfg.get("temperature") if cfg.get("temperature") not in (None, "") else 0.2)
    except (TypeError, ValueError):
        return 0.2


# ── OpenAI-compatible ──────────────────────────────────────────────

def _openai(cfg, system, history, tools, call_tool, log):
    url = (cfg.get("base_url") or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    msgs = [{"role": "system", "content": system}] + [{"role": m["role"], "content": m["content"]} for m in history]
    spec = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["input_schema"]}} for t in tools]
    for _ in range(MAX_ROUNDS):
        body = {"model": cfg["model"], "messages": msgs, "temperature": _temp(cfg)}
        if spec:
            body["tools"] = spec
        data = _post(url, headers, body)
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        calls = msg.get("tool_calls") or []
        if not calls:
            return msg.get("content") or ""
        msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for c in calls:
            fn = c.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = call_tool(fn.get("name"), args)
            log.append({"name": fn.get("name"), "args": args})
            msgs.append({"role": "tool", "tool_call_id": c.get("id"), "content": json.dumps(result, default=str)[:60000]})
    return "I couldn't finish that in a few steps. Try a narrower question."


# ── Anthropic ──────────────────────────────────────────────────────

def _anthropic(cfg, system, history, tools, call_tool, log):
    url = (cfg.get("base_url") or "https://api.anthropic.com").rstrip("/")
    url = url + ("/messages" if url.endswith("/v1") else "/v1/messages")
    headers = {"Content-Type": "application/json", "x-api-key": cfg.get("api_key") or "",
               "anthropic-version": "2023-06-01"}
    msgs = [{"role": m["role"], "content": m["content"]} for m in history]
    spec = [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]} for t in tools]
    for _ in range(MAX_ROUNDS):
        body = {"model": cfg["model"], "system": system, "messages": msgs, "max_tokens": 2048,
                "temperature": _temp(cfg)}
        if spec:
            body["tools"] = spec
        data = _post(url, headers, body)
        content = data.get("content") or []
        uses = [b for b in content if b.get("type") == "tool_use"]
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        if not uses:
            return text
        msgs.append({"role": "assistant", "content": content})
        results = []
        for u in uses:
            result = call_tool(u.get("name"), u.get("input") or {})
            log.append({"name": u.get("name"), "args": u.get("input") or {}})
            results.append({"type": "tool_result", "tool_use_id": u.get("id"),
                            "content": json.dumps(result, default=str)[:60000]})
        msgs.append({"role": "user", "content": results})
    return "I couldn't finish that in a few steps. Try a narrower question."


# ── Gemini ─────────────────────────────────────────────────────────

def _gemini_schema(s):
    """Gemini accepts a subset of JSON schema."""
    if isinstance(s, dict):
        return {k: _gemini_schema(v) for k, v in s.items() if k not in ("additionalProperties", "default", "$schema")}
    if isinstance(s, list):
        return [_gemini_schema(x) for x in s]
    return s


def _gemini(cfg, system, history, tools, call_tool, log):
    base = (cfg.get("base_url") or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    url = f"{base}/models/{cfg['model']}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": cfg.get("api_key") or ""}
    contents = [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                for m in history]
    spec = [{"functionDeclarations": [{"name": t["name"], "description": t["description"],
                                       "parameters": _gemini_schema(t["input_schema"])} for t in tools]}] if tools else []
    for _ in range(MAX_ROUNDS):
        body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents,
                "generationConfig": {"temperature": _temp(cfg)}}
        if spec:
            body["tools"] = spec
        data = _post(url, headers, body)
        parts = (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts")) or []
        calls = [p["functionCall"] for p in parts if p.get("functionCall")]
        if not calls:
            return "".join(p.get("text", "") for p in parts)
        contents.append({"role": "model", "parts": parts})
        responses = []
        for c in calls:
            result = call_tool(c.get("name"), c.get("args") or {})
            log.append({"name": c.get("name"), "args": c.get("args") or {}})
            responses.append({"functionResponse": {"name": c.get("name"),
                                                   "response": {"result": json.loads(json.dumps(result, default=str))}}})
        contents.append({"role": "user", "parts": responses})
    return "I couldn't finish that in a few steps. Try a narrower question."


PROVIDERS = {"openai": _openai, "anthropic": _anthropic, "gemini": _gemini}


def chat(cfg: dict, system: str, history: list[dict], tools: list[dict],
         call_tool: Callable[[str, dict], dict]) -> tuple[str, list[dict]]:
    fn = PROVIDERS.get(cfg.get("provider") or "openai")
    if fn is None:
        raise BadRequest("Unknown AI provider.")
    if not cfg.get("model"):
        raise BadRequest("Choose the model name in Settings → Integrations → AI model.")
    log: list[dict] = []
    text = fn(cfg, system, history, tools, call_tool, log)
    return text, log


def complete_text(cfg: dict, prompt: str) -> str:
    text, _ = chat(cfg, "You are a helpful assistant.", [{"role": "user", "content": prompt}], [], lambda n, a: {})
    return text.strip()
