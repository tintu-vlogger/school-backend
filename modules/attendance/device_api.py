"""Vendor or middleware API pull: we call the attendance software's web address on a
schedule and read the punches out of its JSON, XML or text answer.

Settings (source_config of a device with connection "api"):

    method GET | POST, url, params [{name, value}], headers [{name, value}], body, content_type
    auth_type none | api_key | basic | bearer | oauth2 | login | body
        api_key: auth_header (default X-API-Key) + api_key; api_key_in header | query
        basic: username + password       bearer: token
        oauth2 (client credentials): token_url, client_id, client_secret, scope
        login: login_url + login_body; token_from "json:<path>" | "header:<name>";
               token_header (default Authorization) + token_prefix
        body: username / password only go into the body template ({username}, {password})
    paging none | page | offset | next   (page_param, page_start, offset_param, page_size, next_path)
    lookback_days (first run), overlap_minutes, date_window_days (largest range asked at once)
    every_minutes, verify_tls

Templates (url, params, headers, body) take placeholders: {from} {to} (device local time),
{from_utc} {to_utc}, each with an optional strftime format ({from:%d%m%Y}), and {page}
{offset} {size} {serial} {username} {password} {since_id} {tzoffset} {next}.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlparse
from xml.sax.saxutils import escape as xml_escape
from zoneinfo import ZoneInfo

from core.errors import BadRequest
from modules.attendance import device_mapping as fm
from modules.attendance.device_common import check_public_host, store_secrets

SECRET_KEYS = ("password", "api_key", "token", "client_secret")
AUTH_TYPES = ("none", "api_key", "basic", "bearer", "oauth2", "login", "body")
PAGING = ("none", "page", "offset", "next")
MAX_PAGES = 50
MAX_BYTES = 20 * 1024 * 1024
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_PH = re.compile(r"\{(from_utc|to_utc|from|to|page|offset|size|serial|username|password|since_id|tzoffset|next)(?::([^{}]*))?\}")


def _pairs(v) -> list[dict]:
    out = []
    for p in v or []:
        name = str((p or {}).get("name") or "").strip()
        if name:
            out.append({"name": name[:100], "value": str(p.get("value") or "")[:2000]})
    return out[:30]


def clean(cfg: dict, old: dict | None) -> dict:
    url = (cfg.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise BadRequest("Enter the full web address of the service, starting with http:// or https://.")
    auth = cfg.get("auth_type") or "none"
    if auth not in AUTH_TYPES:
        raise BadRequest("Choose how we sign in to the service.")
    paging = cfg.get("paging") or "none"
    if paging not in PAGING:
        raise BadRequest("Choose how the service splits results into pages.")
    every = int(cfg.get("every_minutes") or 15)
    if not 5 <= every <= 1440:
        raise BadRequest("Check every 5 to 1440 minutes.")
    out = {
        "preset": (cfg.get("preset") or "custom")[:40],
        "method": "POST" if str(cfg.get("method") or "GET").upper() == "POST" else "GET",
        "url": url[:2000], "params": _pairs(cfg.get("params")), "headers": _pairs(cfg.get("headers")),
        "body": str(cfg.get("body") or "")[:20000],
        "content_type": (cfg.get("content_type") or "application/json").strip()[:100],
        "auth_type": auth,
        "auth_header": (cfg.get("auth_header") or "X-API-Key").strip()[:100],
        "api_key_in": "query" if cfg.get("api_key_in") == "query" else "header",
        "username": (cfg.get("username") or "").strip()[:200],
        "token_url": (cfg.get("token_url") or "").strip()[:2000],
        "client_id": (cfg.get("client_id") or "").strip()[:300],
        "scope": (cfg.get("scope") or "").strip()[:500],
        "login_url": (cfg.get("login_url") or "").strip()[:2000],
        "login_body": str(cfg.get("login_body") or "")[:5000],
        "token_from": (cfg.get("token_from") or "json:access_token").strip()[:200],
        "token_header": (cfg.get("token_header") or "Authorization").strip()[:100],
        "token_prefix": str(cfg.get("token_prefix") if cfg.get("token_prefix") is not None else "Bearer ")[:40],
        "paging": paging,
        "page_param": (cfg.get("page_param") or "page").strip()[:60],
        "page_start": int(cfg.get("page_start") if cfg.get("page_start") not in (None, "") else 1),
        "offset_param": (cfg.get("offset_param") or "offset").strip()[:60],
        "size_param": (cfg.get("size_param") or "").strip()[:60],
        "page_size": max(1, min(int(cfg.get("page_size") or 500), 5000)),
        "next_path": (cfg.get("next_path") or "next").strip()[:200],
        "next_param": (cfg.get("next_param") or "cursor").strip()[:60],
        "date_format": (cfg.get("date_format") or DEFAULT_DATE_FORMAT)[:60],
        "lookback_days": max(0, min(int(cfg.get("lookback_days") if cfg.get("lookback_days") not in (None, "") else 3), 90)),
        "overlap_minutes": max(0, min(int(cfg.get("overlap_minutes") if cfg.get("overlap_minutes") not in (None, "") else 30), 1440)),
        "date_window_days": max(1, min(int(cfg.get("date_window_days") or 31), 92)),
        "every_minutes": every,
        "verify_tls": cfg.get("verify_tls") is not False,
    }
    if auth == "oauth2" and not out["token_url"]:
        raise BadRequest("Enter the token address for OAuth2.")
    if auth == "login" and not out["login_url"]:
        raise BadRequest("Enter the sign-in address.")
    return store_secrets(out, SECRET_KEYS, cfg, old)


# ── Templates ───────────────────────────────────────────────────────

def _escaper(content_type: str):
    ct = (content_type or "").lower()
    if "json" in ct:
        return lambda s: json.dumps(s)[1:-1]
    if "xml" in ct:
        return xml_escape
    return lambda s: s


def render(template: str, ctx: dict, escape=None) -> str:
    def sub(m):
        name, fmt = m.group(1), m.group(2)
        v = ctx.get(name)
        if isinstance(v, datetime):
            s = v.strftime(fmt or ctx.get("_date_format") or DEFAULT_DATE_FORMAT)
        else:
            s = "" if v is None else str(v)
        return escape(s) if escape else s
    return _PH.sub(sub, template or "")


def window(cfg: dict, state: dict, tz: ZoneInfo, now: datetime | None = None) -> tuple[datetime, datetime, bool]:
    """The local time range to ask for: from the last punch (minus an overlap) or the
    look-back on the first run, to now, at most date_window_days long."""
    now_local = (now or datetime.now(timezone.utc)).astimezone(tz).replace(tzinfo=None, microsecond=0)
    last = state.get("last_time")
    if last:
        start = datetime.fromisoformat(last) - timedelta(minutes=int(cfg.get("overlap_minutes") or 0))
    else:
        start = now_local - timedelta(days=int(cfg.get("lookback_days") if cfg.get("lookback_days") is not None else 3))
    end = now_local
    capped = False
    limit = timedelta(days=int(cfg.get("date_window_days") or 31))
    if end - start > limit:
        end, capped = start + limit, True
    return start, end, capped


def _tzoffset(tz: ZoneInfo, at: datetime) -> str:
    off = at.replace(tzinfo=tz).utcoffset() or timedelta(0)
    mins = int(off.total_seconds() // 60)
    sign = "+" if mins >= 0 else "-"
    return f"{sign}{abs(mins) // 60:02d}:{abs(mins) % 60:02d}"


def context(cfg: dict, state: dict, tz: ZoneInfo, serial: str | None, start: datetime, end: datetime) -> dict:
    return {"from": start, "to": end,
            "from_utc": start.replace(tzinfo=tz).astimezone(timezone.utc), "to_utc": end.replace(tzinfo=tz).astimezone(timezone.utc),
            "size": cfg.get("page_size") or 500, "page": cfg.get("page_start", 1), "offset": 0,
            "serial": serial or "", "username": cfg.get("username") or "", "password": cfg.get("password") or "",
            "since_id": state.get("last_id") or 0, "tzoffset": _tzoffset(tz, start), "next": "",
            "_date_format": cfg.get("date_format") or DEFAULT_DATE_FORMAT}


# ── HTTP ────────────────────────────────────────────────────────────

def _client(cfg: dict, transport=None):
    import httpx
    return httpx.Client(timeout=httpx.Timeout(45.0, connect=15.0), verify=bool(cfg.get("verify_tls", True)),
                        follow_redirects=False, transport=transport)


def _check(url: str) -> None:
    host = urlparse(url).hostname
    check_public_host(host or "")


def _raise_for(resp, what="The service") -> None:
    if resp.status_code >= 400:
        snippet = (resp.text or "")[:200].strip().replace("\n", " ")
        if resp.status_code in (401, 403):
            raise BadRequest(f"{what} refused the sign-in ({resp.status_code}). Check the user name, password or key. {snippet}")
        raise BadRequest(f"{what} answered with an error ({resp.status_code}): {snippet}")
    if len(resp.content) > MAX_BYTES:
        raise BadRequest(f"{what} sent more than 20 MB at once. Ask for a smaller date range or use paging.")


def _auth(client, cfg: dict, ctx: dict) -> tuple[dict, dict, object]:
    """Headers, extra query params and an httpx auth object for the chosen sign-in."""
    import httpx
    t = cfg.get("auth_type") or "none"
    headers, params, auth = {}, {}, None
    if t == "api_key":
        if cfg.get("api_key_in") == "query":
            params[cfg.get("auth_header") or "api_key"] = cfg.get("api_key") or ""
        else:
            headers[cfg.get("auth_header") or "X-API-Key"] = cfg.get("api_key") or ""
    elif t == "basic":
        auth = httpx.BasicAuth(cfg.get("username") or "", cfg.get("password") or "")
    elif t == "bearer":
        headers["Authorization"] = f"Bearer {cfg.get('token') or ''}"
    elif t == "oauth2":
        _check(cfg["token_url"])
        r = client.post(cfg["token_url"], data={"grant_type": "client_credentials", "client_id": cfg.get("client_id") or "",
                                                "client_secret": cfg.get("client_secret") or "",
                                                **({"scope": cfg["scope"]} if cfg.get("scope") else {})})
        _raise_for(r, "The token service")
        try:
            token = r.json().get("access_token")
        except ValueError:
            token = None
        if not token:
            raise BadRequest("The token service didn't return an access_token.")
        headers["Authorization"] = f"Bearer {token}"
    elif t == "login":
        _check(cfg["login_url"])
        body = render(cfg.get("login_body") or "", ctx, _escaper("application/json"))
        r = client.post(cfg["login_url"], content=body.encode(), headers={"Content-Type": "application/json"})
        _raise_for(r, "The sign-in")
        src = cfg.get("token_from") or "json:access_token"
        token = None
        if src.startswith("header:"):
            token = r.headers.get(src[7:].strip())
        else:
            try:
                token = fm.get_path(r.json(), src.split(":", 1)[-1].strip())
            except ValueError:
                token = None
        if not token:
            raise BadRequest(f"Signed in, but no token was found at {src}.")
        headers[cfg.get("token_header") or "Authorization"] = f"{cfg.get('token_prefix') or ''}{token}"
    return headers, params, auth


def _uses_next(cfg: dict) -> bool:
    texts = [cfg.get("url") or "", cfg.get("body") or ""] + [p["value"] for p in cfg.get("params") or []]
    return any("{next" in t for t in texts)


def fetch(cfg: dict, mapping: dict, ctx: dict, transport=None, max_pages: int = MAX_PAGES) -> tuple[list[dict], list[str]]:
    """Rows from every page, and a short note per request (for the test screen)."""
    _check(cfg["url"])
    notes, rows = [], []
    with _client(cfg, transport) as client:
        a_headers, a_params, auth = _auth(client, cfg, ctx)
        size = int(cfg.get("page_size") or 500)
        paging = cfg.get("paging") or "none"
        next_url = None
        for n in range(max_pages):
            ctx = {**ctx, "page": int(cfg.get("page_start", 1)) + n, "offset": n * size}
            url_esc = lambda s: quote(s, safe="/:;,-_.~%@+=&?")  # noqa: E731
            if next_url:
                url, params = next_url, {}
            else:
                url = render(cfg["url"], ctx, url_esc)
                params = {p["name"]: render(p["value"], ctx) for p in cfg.get("params") or []}
                if paging == "page":
                    params.setdefault(cfg.get("page_param") or "page", str(ctx["page"]))
                elif paging == "offset" and cfg.get("offset_param") and "{offset}" not in (cfg.get("body") or ""):
                    params.setdefault(cfg["offset_param"], str(ctx["offset"]))
                if paging in ("page", "offset") and cfg.get("size_param"):
                    params.setdefault(cfg["size_param"], str(size))
                if paging == "next" and ctx.get("next") and not _uses_next(cfg):
                    params.setdefault(cfg.get("next_param") or "cursor", ctx["next"])
            _check(url)
            headers = {h["name"]: render(h["value"], ctx) for h in cfg.get("headers") or []}
            headers.update(a_headers)
            params.update(a_params)
            if cfg.get("method") == "POST":
                body = render(cfg.get("body") or "", ctx, _escaper(cfg.get("content_type") or ""))
                headers.setdefault("Content-Type", cfg.get("content_type") or "application/json")
                resp = client.post(url, params=params or None, content=body.encode(), headers=headers, auth=auth)
            else:
                resp = client.get(url, params=params or None, headers=headers, auth=auth)
            _raise_for(resp)
            page_rows = fm.extract(resp.content, mapping)
            notes.append(f"{cfg.get('method')} {str(resp.request.url)[:160]} → {resp.status_code}, {len(page_rows)} records")
            rows.extend(page_rows)
            if len(rows) >= fm.MAX_ROWS or paging == "none" or not page_rows:
                break
            if paging in ("page", "offset") and len(page_rows) < size:
                break
            if paging == "next":
                try:
                    nxt = fm.get_path(resp.json(), cfg.get("next_path") or "next")
                except ValueError:
                    nxt = None
                if not nxt:
                    break
                nxt = str(nxt)
                if nxt.startswith(("http://", "https://")):
                    next_url = nxt
                else:
                    ctx["next"] = nxt
                    next_url = None
    return rows, notes


def read(cfg: dict, mapping: dict, state: dict, tz: ZoneInfo, serial: str | None, transport=None,
         now: datetime | None = None, max_pages: int = MAX_PAGES) -> tuple[list[dict], list[dict], dict, list[str]]:
    """(rows, records, new state, notes) for one scheduled pull."""
    start, end, capped = window(cfg, state, tz, now)
    ctx = context(cfg, state, tz, serial, start, end)
    rows, notes = fetch(cfg, mapping, ctx, transport, max_pages)
    records = fm.to_records(rows, mapping, tz)
    state = dict(state)
    times = [r["timestamp"].astimezone(tz).replace(tzinfo=None) for r in records if isinstance(r.get("timestamp"), datetime)]
    newest = max(times) if times else None
    if capped and (newest is None or newest < end):
        newest = end  # nothing more in this range: move on to the next one
    if newest and (not state.get("last_time") or newest.isoformat() > state["last_time"]):
        state["last_time"] = newest.isoformat()
    ids = [r.get("_id") for r in records if isinstance(r.get("_id"), int)]
    if ids:
        state["last_id"] = max(ids + [int(state.get("last_id") or 0)])
    state["more"] = capped
    return rows, records, state, notes
