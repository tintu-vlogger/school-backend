"""Format checks for Indian identity numbers and bank codes. Each `clean_*` returns the
normalised value (upper case, no spaces) or None for empty input, and raises BadRequest
with a plain message when the value is wrong."""
import re

from core.errors import BadRequest

# Verhoeff tables (Aadhaar check digit)
_D = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
      [3, 4, 0, 1, 2, 8, 9, 5, 6, 7], [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
      [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3], [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
      [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]]
_P = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
      [8, 9, 1, 6, 0, 4, 3, 5, 2, 7], [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
      [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8]]
_INV = [0, 4, 3, 2, 1, 5, 6, 7, 8, 9]


def verhoeff_ok(number: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(number)):
        c = _D[c][_P[i % 8][int(ch)]]
    return c == 0


def verhoeff_digit(number: str) -> str:
    c = 0
    for i, ch in enumerate(reversed(number)):
        c = _D[c][_P[(i + 1) % 8][int(ch)]]
    return str(_INV[c])


def _strip(v) -> str | None:
    if v is None:
        return None
    v = re.sub(r"[\s-]", "", str(v)).upper()
    return v or None


PAN_RE = re.compile(r"^[A-Z]{3}[ABCFGHLJPTK][A-Z][0-9]{4}[A-Z]$")


def clean_pan(v, *, person: bool = True) -> str | None:
    v = _strip(v)
    if v is None:
        return None
    if not PAN_RE.match(v):
        raise BadRequest("PAN should look like ABCPE1234F: 5 letters, 4 digits and a letter.")
    if person and v[3] != "P":
        raise BadRequest("This PAN is not an individual's PAN (the 4th letter should be P).")
    return v


def clean_aadhaar(v) -> str | None:
    v = _strip(v)
    if v is None:
        return None
    if not re.fullmatch(r"[2-9][0-9]{11}", v) or not verhoeff_ok(v):
        raise BadRequest("This Aadhaar number isn't valid. Check the 12 digits.")
    return v


def clean_uan(v) -> str | None:
    v = _strip(v)
    if v is None:
        return None
    if not re.fullmatch(r"[0-9]{12}", v):
        raise BadRequest("UAN has 12 digits.")
    return v


def clean_esi(v) -> str | None:
    v = _strip(v)
    if v is None:
        return None
    if not re.fullmatch(r"[0-9]{10}|[0-9]{17}", v):
        raise BadRequest("ESI number (IP number) has 10 digits.")
    return v


def clean_ifsc(v) -> str | None:
    v = _strip(v)
    if v is None:
        return None
    if not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", v):
        raise BadRequest("IFSC should look like HDFC0001234: 4 letters, a zero and 6 letters or digits.")
    return v


def clean_account_number(v) -> str | None:
    v = _strip(v)
    if v is None:
        return None
    if not re.fullmatch(r"[0-9A-Z]{6,20}", v):
        raise BadRequest("Bank account numbers have 6 to 20 digits.")
    return v


def clean_passport(v) -> str | None:
    v = _strip(v)
    if v is None:
        return None
    if not re.fullmatch(r"[A-Z][0-9]{7}", v):
        raise BadRequest("Indian passport numbers look like K1234567: a letter and 7 digits.")
    return v


def clean_driving_licence(v) -> str | None:
    v = _strip(v)
    if v is None:
        return None
    if not re.fullmatch(r"[A-Z]{2}[0-9A-Z]{8,16}", v):
        raise BadRequest("The driving licence number should start with the state code, e.g. KA0120190012345.")
    return v


def clean_pincode(v) -> str | None:
    v = _strip(v)
    if v is None:
        return None
    if not re.fullmatch(r"[1-9][0-9]{5}", v):
        raise BadRequest("PIN codes have 6 digits.")
    return v


def clean_phone(v, label: str = "Phone number") -> str | None:
    if v is None:
        return None
    v = str(v).strip()
    if not v:
        return None
    digits = re.sub(r"\D", "", v)
    if len(digits) < 8 or len(digits) > 15 or not re.fullmatch(r"\+?[0-9 ()-]+", v):
        raise BadRequest(f"{label} doesn't look right.")
    return v


def clean_gstin(v) -> str | None:
    v = _strip(v)
    if v is None:
        return None
    if not re.fullmatch(r"[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]", v):
        raise BadRequest("GSTIN has 15 characters, e.g. 29ABCDE1234F1Z5.")
    return v


def clean_tan(v) -> str | None:
    v = _strip(v)
    if v is None:
        return None
    if not re.fullmatch(r"[A-Z]{4}[0-9]{5}[A-Z]", v):
        raise BadRequest("TAN should look like BLRA12345B: 4 letters, 5 digits and a letter.")
    return v


def clean_cin(v) -> str | None:
    v = _strip(v)
    if v is None:
        return None
    if not re.fullmatch(r"[LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}", v):
        raise BadRequest("CIN has 21 characters, e.g. U72200KA2015PTC012345.")
    return v


def mask(v: str | None, keep: int = 4) -> str | None:
    if not v:
        return None
    return "•" * max(len(v) - keep, 2) + v[-keep:]
