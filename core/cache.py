"""One Redis client for the whole process, plus a tiny JSON cache.

Before this, request handlers opened a fresh Redis connection each time they
needed one (permission checks on every request included). A shared pool keeps
connections warm; `cached` wraps any JSON-serialisable computation with a TTL,
and `invalidate` drops keys by prefix after a write.
"""
import json
import logging
from typing import Any, Callable

import redis
from fastapi.encoders import jsonable_encoder

from core.config import get_settings

logger = logging.getLogger("hrms.cache")
_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(get_settings().redis_url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2,
                                       health_check_interval=30, max_connections=50)
    return _client


def cached(key: str, ttl: int, compute: Callable[[], Any]) -> Any:
    """Return the cached JSON value for `key`, or compute, store for `ttl` seconds and return.
    Redis being down never breaks a request: the value is simply computed."""
    try:
        raw = get_redis().get(key)
        if raw is not None:
            return json.loads(raw)
    except Exception:
        pass
    value = compute()
    value = jsonable_encoder(value)  # pydantic models and dates become plain JSON, same shape warm or cold
    try:
        get_redis().setex(key, ttl, json.dumps(jsonable_encoder(value)))
    except Exception:
        pass
    return value


def invalidate(*prefixes: str) -> None:
    """Delete every key that starts with any of the prefixes (SCAN, never KEYS)."""
    try:
        r = get_redis()
        for prefix in prefixes:
            for k in r.scan_iter(match=f"{prefix}*", count=200):
                r.delete(k)
    except Exception as e:
        logger.debug("cache invalidate skipped: %s", e)
