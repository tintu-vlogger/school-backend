"""Talking to the HR system: download a device's settings, send punches, report status.
Only the standard library is used, so the program stays small."""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request


class ServerError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Server:
    def __init__(self, base_url: str, timeout: int = 60, verify_tls: bool = True):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.ctx = None if verify_tls else ssl._create_unverified_context()  # noqa: S323 (only when asked)

    def _call(self, method: str, path: str, key: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "X-Device-Key": key, "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": "hrms-attendance-connector"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as resp:
                text = resp.read().decode("utf-8", "replace")
                return json.loads(text) if text.strip() else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            try:
                detail = json.loads(detail).get("detail") or detail
            except ValueError:
                pass
            raise ServerError(f"{exc.code}: {detail}", exc.code)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ServerError(f"Can't reach {self.base}: {getattr(exc, 'reason', exc)}")

    def config(self, key: str) -> dict:
        return self._call("GET", "/attendance/connector/config", key)

    def push(self, key: str, punches: list[dict]) -> dict:
        return self._call("POST", "/attendance/devices/push", key, {"punches": punches})

    def heartbeat(self, key: str, data: dict) -> dict:
        return self._call("POST", "/attendance/connector/heartbeat", key, data)
