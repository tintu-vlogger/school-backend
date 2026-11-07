"""Aadhaar storage: the number is encrypted (Fernet, key derived from the app secret),
a keyed hash finds duplicates without decrypting, and only the last 4 digits are shown."""
import base64
import hashlib
import hmac

from cryptography.fernet import Fernet

from core.config import get_settings


def _key() -> bytes:
    secret = (get_settings().SECRET_KEY or "dev").encode()
    return base64.urlsafe_b64encode(hashlib.sha256(b"aadhaar-v1:" + secret).digest())


def encrypt(value: str) -> str:
    return Fernet(_key()).encrypt(value.encode()).decode()


def decrypt(token: str | None) -> str | None:
    if not token:
        return None
    try:
        return Fernet(_key()).decrypt(token.encode()).decode()
    except Exception:
        return None


def lookup_hash(value: str) -> str:
    secret = (get_settings().SECRET_KEY or "dev").encode()
    return hmac.new(secret, b"aadhaar:" + value.encode(), hashlib.sha256).hexdigest()


def set_aadhaar(emp, value: str | None) -> None:
    if value is None:
        emp.aadhaar_encrypted = emp.aadhaar_hash = emp.aadhaar_last4 = None
        return
    emp.aadhaar_encrypted = encrypt(value)
    emp.aadhaar_hash = lookup_hash(value)
    emp.aadhaar_last4 = value[-4:]
