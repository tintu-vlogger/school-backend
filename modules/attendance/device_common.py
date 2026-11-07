"""Small helpers shared by the device connections: secrets kept encrypted inside a
device's settings, masking them for the web, the host safety check for server-side pulls,
and optional-driver errors with plain messages."""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import socket

from cryptography.fernet import Fernet

from core.config import get_settings
from core.errors import BadRequest

MASK = "••••••"
SECRETS_KEY = "_secrets"


def _fernet() -> Fernet:
    secret = (get_settings().SECRET_KEY or "dev").encode()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"device-secrets-v1:" + secret).digest()))


def seal(values: dict) -> str:
    return _fernet().encrypt(json.dumps(values).encode()).decode()


def unseal(token: str | None) -> dict:
    if not token:
        return {}
    try:
        return json.loads(_fernet().decrypt(token.encode()).decode())
    except Exception:
        return {}


def store_secrets(cfg: dict, secret_keys: tuple[str, ...], incoming: dict, old: dict | None) -> dict:
    """Move secret fields of `incoming` into the encrypted blob of `cfg`. An empty or
    masked value keeps the saved secret; the literal "-" clears it."""
    saved = unseal((old or {}).get(SECRETS_KEY))
    out = dict(saved)
    for k in secret_keys:
        v = incoming.get(k)
        if v in (None, "", MASK):
            continue
        out[k] = "" if v == "-" else str(v)
        cfg.pop(k, None)
    for k in secret_keys:
        cfg.pop(k, None)
    cfg[SECRETS_KEY] = seal({k: v for k, v in out.items() if v})
    return cfg


def with_secrets(cfg: dict | None) -> dict:
    """The settings with secrets decrypted (for running a pull, never for the web)."""
    cfg = dict(cfg or {})
    secrets = unseal(cfg.pop(SECRETS_KEY, None))
    cfg.update(secrets)
    return cfg


def masked(cfg: dict | None, secret_keys: tuple[str, ...]) -> dict:
    cfg = dict(cfg or {})
    secrets = unseal(cfg.pop(SECRETS_KEY, None))
    for k in secret_keys:
        cfg[k] = MASK if secrets.get(k) else ""
    return cfg


def allow_private_hosts() -> bool:
    if os.environ.get("DEVICE_ALLOW_PRIVATE_HOSTS", "").lower() in ("1", "true", "yes"):
        return True
    return not get_settings().is_production


def check_public_host(host: str) -> None:
    """Server-side pulls go to the internet only: a private, loopback or link-local
    address is refused (it belongs on the office connector). Development allows it."""
    host = (host or "").strip()
    if not host:
        raise BadRequest("Enter the server address.")
    if allow_private_hosts():
        return
    try:
        addrs = {i[4][0] for i in socket.getaddrinfo(host, None)}
    except OSError:
        raise BadRequest(f"The address {host} can't be found.")
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise BadRequest(f"{host} is on a private network. Our server can't reach it; run this on the office connector instead.")


class MissingDriver(BadRequest):
    pass


def need(module: str, package: str, what: str):
    """Import an optional driver or fail with a message an admin can act on."""
    import importlib
    try:
        return importlib.import_module(module)
    except ImportError:
        raise MissingDriver(f"{what} needs the '{package}' package, which isn't installed on this server. "
                            f"Install it (pip install {package}) or run this on the office connector.")


def driver_available(module: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False
