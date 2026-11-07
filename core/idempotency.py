"""Idempotency for writes made by the offline mobile app.

The app saves changes on the phone while offline and sends them later. A request can
reach the server and still look failed to the phone (the connection dropped before the
answer came back), so the app sends it again. Each write carries an `Idempotency-Key`
header; the first successful answer for that key is stored for 7 days and replayed for
any repeat, so a change is applied exactly once.
"""
import hashlib
import json

import redis.asyncio as aioredis

from core.config import get_settings

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
TTL = 7 * 24 * 3600
LOCK_TTL = 60

_redis = None


def _client():
    global _redis
    if _redis is None:
        _redis = aioredis.Redis.from_url(get_settings().redis_url)
    return _redis


class IdempotencyMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in WRITE_METHODS:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        key = headers.get(b"idempotency-key")
        if not key or len(key) > 200:
            return await self.app(scope, receive, send)

        digest = hashlib.sha256(b"|".join([scope["method"].encode(), scope["path"].encode(), key])).hexdigest()
        store_key, lock_key = f"idem:{digest}", f"idem-lock:{digest}"
        try:
            r = _client()
            cached = await r.get(store_key)
        except Exception:
            # Redis down: behave like a normal request rather than failing the write.
            return await self.app(scope, receive, send)

        if cached:
            saved = json.loads(cached)
            out_headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in saved["headers"]]
            out_headers.append((b"idempotent-replay", b"true"))
            await send({"type": "http.response.start", "status": saved["status"], "headers": out_headers})
            await send({"type": "http.response.body", "body": saved["body"].encode("latin-1")})
            return

        if not await r.set(lock_key, "1", nx=True, ex=LOCK_TTL):
            body = b'{"detail":"This change is already being saved. Try again in a moment."}'
            await send({"type": "http.response.start", "status": 409, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return

        captured = {"status": 500, "headers": [], "body": bytearray()}

        async def capture(message):
            if message["type"] == "http.response.start":
                captured["status"] = message["status"]
                captured["headers"] = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in message.get("headers", [])]
            elif message["type"] == "http.response.body":
                captured["body"].extend(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            try:
                if 200 <= captured["status"] < 300:
                    keep = [(k, v) for k, v in captured["headers"] if k.lower() in ("content-type", "content-encoding")]
                    await r.set(store_key, json.dumps({
                        "status": captured["status"], "headers": keep, "body": bytes(captured["body"]).decode("latin-1"),
                    }), ex=TTL)
                await r.delete(lock_key)
            except Exception:
                pass
