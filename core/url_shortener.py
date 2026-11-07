"""
Simple URL shortener using our own backend.
Stores short code → long URL mapping in Redis.
"""

import hashlib

import redis

from core.config import get_settings
from core.cache import get_redis

settings = get_settings()
_redis = None


def _get_redis():
    global _redis
    if _redis is None:
        _redis = get_redis()
    return _redis


def shorten_url(long_url: str, base_url: str = "") -> str:
    if not base_url:
        base_url = settings.BACKEND_URL
    """Create a short URL. Returns the full short URL."""
    r = _get_redis()
    # Generate a short code from the URL hash
    code = hashlib.md5(long_url.encode()).hexdigest()[:8]
    key = f"url:{code}"
    # Store for 90 days
    r.setex(key, 90 * 86400, long_url)
    return f"{base_url}/s/{code}"
