"""Throttle password guessing on the login endpoints.

Failed attempts are counted per email and per client IP in Redis. Once either
counter passes its limit, further attempts are refused for the rest of the
window, even with the right password. A successful login clears the email counter.
If Redis is unreachable the guard steps aside rather than locking everyone out.
"""
from fastapi import HTTPException, Request

from core.cache import get_redis

WINDOW_SECONDS = 15 * 60
MAX_FAILS_PER_EMAIL = 10
MAX_FAILS_PER_IP = 50


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "?")


def _keys(scope: str, email: str, request: Request) -> tuple[str, str]:
    return (
        f"login:{scope}:email:{(email or '').strip().lower()}",
        f"login:{scope}:ip:{_client_ip(request)}",
    )


def check(scope: str, email: str, request: Request) -> None:
    """Refuse the attempt if this email or IP has failed too often recently."""
    try:
        r = get_redis()
        email_key, ip_key = _keys(scope, email, request)
        if int(r.get(email_key) or 0) >= MAX_FAILS_PER_EMAIL or int(r.get(ip_key) or 0) >= MAX_FAILS_PER_IP:
            raise HTTPException(
                status_code=429,
                detail="Too many failed sign-in attempts. Please wait 15 minutes and try again.",
            )
    except HTTPException:
        raise
    except Exception:
        pass


def record_failure(scope: str, email: str, request: Request) -> None:
    try:
        r = get_redis()
        for key in _keys(scope, email, request):
            if r.incr(key) == 1:
                r.expire(key, WINDOW_SECONDS)
    except Exception:
        pass


def record_success(scope: str, email: str, request: Request) -> None:
    try:
        get_redis().delete(_keys(scope, email, request)[0])
    except Exception:
        pass
