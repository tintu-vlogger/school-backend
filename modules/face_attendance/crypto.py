"""Face templates at rest: encrypted with Fernet (AES with an HMAC check) using a key that is
different for every company (derived from the app secret and the company id), so one
company's key can never open another company's templates."""
from __future__ import annotations

import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken

from core.config import get_settings


def _key(company_id: int) -> bytes:
    secret = (get_settings().SECRET_KEY or "dev").encode()
    return base64.urlsafe_b64encode(hashlib.sha256(b"face-templates-v1:" + str(company_id).encode() + b":" + secret).digest())


def seal(company_id: int, vectors: list[list[float]]) -> str:
    payload = json.dumps([[round(x, 6) for x in v] for v in vectors], separators=(",", ":"))
    return Fernet(_key(company_id)).encrypt(payload.encode()).decode()


def open_(company_id: int, token: str | None) -> list[list[float]]:
    if not token:
        return []
    try:
        return json.loads(Fernet(_key(company_id)).decrypt(token.encode()).decode())
    except (InvalidToken, ValueError):
        return []
