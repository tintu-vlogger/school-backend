"""Signing methods. The in-app method (drawn or typed signature, confirmed with a
one-time code sent by email) is built in. Aadhaar eSign plugs in here later: implement
`start` (redirect the signer to the eSign service provider with the document hash) and
`complete` (verify the signed response and store the signature), then set `ready`.
"""
from __future__ import annotations

from core.errors import BadRequest


class Provider:
    key = ""
    label = ""

    def check_ready(self, db, company_id: int) -> None:
        """Raise BadRequest when the method can't be used for this company yet."""

    def start(self, db, signer) -> dict:  # pragma: no cover - interface
        raise NotImplementedError

    def complete(self, db, signer, payload: dict) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class InAppOtp(Provider):
    key = "in_app_otp"
    label = "Signature in the app, confirmed with a code sent by email"


class AadhaarEsign(Provider):
    """Placeholder for an ESP (eSign Service Provider) under the IT Act, second schedule."""
    key = "aadhaar"
    label = "Aadhaar eSign"

    def check_ready(self, db, company_id: int) -> None:
        raise BadRequest("Aadhaar eSign is not connected yet. Use signing in the app for now.")


_PROVIDERS = {p.key: p for p in (InAppOtp(), AadhaarEsign())}


def get(key: str) -> Provider:
    p = _PROVIDERS.get(key)
    if p is None:
        raise BadRequest("Unknown signing method.")
    return p


def available() -> list[dict]:
    return [{"key": p.key, "label": p.label, "ready": not isinstance(p, AadhaarEsign)} for p in _PROVIDERS.values()]
