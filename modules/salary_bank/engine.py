"""Bank file engine: turns payees into a bank's bulk upload file from a layout ("spec").

A spec is plain data, the same for the ready-made bank layouts (formats.py) and the custom
layouts companies build in payroll settings:

  kind          "delimited" | "fixed"
  delimiter     "," "|" "^" "#" "~" ";" or "tab" (delimited only)
  quote         "minimal" (quote only when needed) | "all" | "none" (delimiters removed from values)
  extension     "csv" | "txt"
  line_ending   "crlf" | "lf"
  header_row    true = a first line with the column labels (delimited only)
  header_lines  extra lines before the payments, each a list of columns (control / batch header)
  trailer_lines lines after the payments (totals, hash or checksum)
  columns       the payment line: [{label, value, width, align, pad, max, case}]
                value   text with {tokens}, e.g. "{amount}" or "SAL{period}"
                width   fixed width files: the field width (values are cut or padded)
                align   "left" | "right"; pad " " or "0"
                max     longest value allowed (cut to fit), any kind of file
                case    "upper" turns the value to capitals
  date_format   strftime pattern for {date} (e.g. "%d/%m/%Y")
  amount        {"decimals": 2, "paise": false}: 1234.50 / 1234 / 123450 (paise)
  codes         transaction type codes: {"internal": "I", "neft": "N", "rtgs": "R", "imps": "M"}
  modes         modes the bank file accepts, e.g. ["internal", "neft", "rtgs"]
  variants      layout changes for one group of a split file, e.g. {"internal": {"columns": [...]}}
  split         "none" | "internal_vs_other" (own-bank and other-bank in separate files) |
                "by_mode" (one file per transaction type)
  clean         true = keep only letters, digits, space and . , - / ( ) & @ _ in text values
  file_name     name pattern, e.g. "{client_code}_SAL_{date:%d%m%Y}_{batch}" (extension added)

Payment line tokens
  {seq} {seq:5}        line number (zero padded to 5)
  {name} {account} {ifsc} {branch} (IFSC branch part) {bank_name} {email} {phone} {employee_code}
  {amount} {amount_paise} {amount_words}?  (no words)
  {mode} NEFT/RTGS/IMPS/FT   {mode_code} the bank's code for the mode
  {date} {date:%Y%m%d}  value (pay) date
  {narration} {payment_ref} {period} (YYYYMM) {month} (Nov 2026)
  {debit_account} {debit_ifsc} {debit_branch} {debit_name} {client_code} {corporate_id} {batch_ref}
Header and trailer tokens: the debit and date tokens above, plus
  {count} {total} {total_paise} {account_hash} (sum of the beneficiary account numbers, last 15 digits)
  {file_date} (today, date_format) {batch} (file number in this run)
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
ACCOUNT_RE = re.compile(r"^[A-Za-z0-9]{6,20}$")
_TOKEN = re.compile(r"\{(\w+)(?::([^}]*))?\}")
_UNSAFE = re.compile(r"[^A-Za-z0-9 .,\-/()&@_]")
DELIMITERS = {"comma": ",", "pipe": "|", "caret": "^", "hash": "#", "tilde": "~", "semicolon": ";", "tab": "\t"}

RTGS_MIN = Decimal("200000")      # RBI: RTGS is for ₹2 lakh and above
IMPS_MAX = Decimal("500000")      # RBI: IMPS limit per transaction ₹5 lakh

# Usual length of account numbers at the big banks (used for warnings only; old and special
# accounts can differ). Sources: banks' own account number formats as printed on passbooks
# and cheque books.
ACCOUNT_LENGTHS = {
    "SBIN": (11, 17), "HDFC": (13, 14), "ICIC": (12,), "UTIB": (15,), "KKBK": (10, 14), "YESB": (15,),
    "PUNB": (16,), "BARB": (14,), "UBIN": (15,), "FDRL": (14,), "SIBL": (16,), "CNRB": (13, 14, 16),
}

TOKENS_ROW = ["seq", "name", "account", "ifsc", "branch", "bank_name", "email", "phone", "employee_code", "amount", "amount_paise",
              "mode", "mode_code", "date", "narration", "payment_ref", "period", "month", "debit_account", "debit_ifsc",
              "debit_branch", "debit_name", "client_code", "corporate_id", "batch_ref"]
TOKENS_TOTAL = ["count", "total", "total_paise", "account_hash", "file_date", "batch", "date", "debit_account",
                "debit_ifsc", "debit_branch", "debit_name", "client_code", "corporate_id", "batch_ref", "period", "month", "narration"]


class SpecError(ValueError):
    pass


@dataclass
class Payee:
    name: str
    account: str
    ifsc: str
    amount: Decimal
    employee_code: str = ""
    email: str = ""
    phone: str = ""
    bank_name: str = ""
    payslip_id: int | None = None
    employee_id: int | None = None


@dataclass
class Debit:
    account: str = ""
    ifsc: str = ""
    name: str = ""
    client_code: str = ""
    corporate_id: str = ""
    bank_name: str = ""


@dataclass
class Problem:
    level: str            # error (left out) | warn
    code: str
    message: str
    name: str = ""
    employee_code: str = ""
    payslip_id: int | None = None


@dataclass
class Line:
    payee: Payee
    mode: str             # internal | neft | rtgs | imps


@dataclass
class Rendered:
    content: bytes
    lines: list[str] = field(default_factory=list)
    count: int = 0
    total: Decimal = Decimal("0")


# ── Values ───────────────────────────────────────────────────────────

def money(v) -> Decimal:
    return Decimal(str(v or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def fmt_amount(v: Decimal, spec: dict) -> str:
    a = spec.get("amount") or {}
    if a.get("paise"):
        return str(int((money(v) * 100).to_integral_value()))
    decimals = int(a.get("decimals", 2))
    q = Decimal(1).scaleb(-decimals) if decimals else Decimal(1)
    return f"{money(v).quantize(q, rounding=ROUND_HALF_UP):.{decimals}f}"


def clean_text(v: str) -> str:
    return re.sub(r"\s+", " ", _UNSAFE.sub(" ", v or "")).strip()


def _render(template: str, values: dict, spec: dict) -> str:
    def rep(m):
        key, f = m.group(1), m.group(2)
        v = values.get(key, "")
        if isinstance(v, date):
            return v.strftime(f or spec.get("date_format") or "%d/%m/%Y")
        if key in ("seq", "batch") and f and f.isdigit():
            return str(v).zfill(int(f))
        if f and f.isdigit() and isinstance(v, str):
            return v[: int(f)]
        return "" if v is None else str(v)
    return _TOKEN.sub(rep, template or "")


def _cell(col: dict, values: dict, spec: dict) -> str:
    v = _render(col.get("value") or "", values, spec)
    if spec.get("clean", True) and not _is_numeric_token(col.get("value")):
        v = clean_text(v)
    if col.get("case") == "upper":
        v = v.upper()
    mx = col.get("max")
    if mx:
        v = v[: int(mx)]
    if spec.get("kind") == "fixed":
        width = int(col.get("width") or 0)
        if width <= 0:
            raise SpecError(f"Column “{col.get('label') or col.get('value')}” needs a width.")
        if len(v) > width:
            if _is_numeric_token(col.get("value")):
                raise SpecError(f"“{v}” does not fit in {width} characters ({col.get('label')}).")
            v = v[:width]
        pad = (col.get("pad") or " ")[:1]
        v = v.rjust(width, pad) if (col.get("align") or "left") == "right" else v.ljust(width, pad)
    return v


def _is_numeric_token(template: str | None) -> bool:
    t = (template or "").strip()
    return t in ("{amount}", "{amount_paise}", "{total}", "{total_paise}", "{count}", "{account_hash}") or \
        t.startswith("{seq") or t.startswith("{batch")


def _join(cells: list[str], spec: dict) -> str:
    if spec.get("kind") == "fixed":
        return "".join(cells)
    delim = delimiter(spec)
    quote = spec.get("quote") or "minimal"
    if quote == "none":
        return delim.join(c.replace(delim, " ").replace("\n", " ") for c in cells)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delim, lineterminator="",
                   quoting=csv.QUOTE_ALL if quote == "all" else csv.QUOTE_MINIMAL)
    w.writerow(cells)
    return buf.getvalue()


def delimiter(spec: dict) -> str:
    d = spec.get("delimiter") or ","
    return DELIMITERS.get(d, d)


# ── Modes and checks ─────────────────────────────────────────────────

def mode_for(p: Payee, debit_ifsc: str, spec: dict, rules: dict) -> str:
    """internal when both accounts are at the same bank, otherwise NEFT, RTGS (₹2 lakh and up,
    when chosen) or IMPS (up to ₹5 lakh, when chosen), limited to what the bank file accepts."""
    modes = spec.get("modes") or ["internal", "neft", "rtgs"]
    # the debit account's bank decides; the layout's own bank only when the debit IFSC is unknown
    own = {debit_ifsc[:4].upper()} if debit_ifsc else {x.upper() for x in (spec.get("ifsc_prefixes") or [])}
    if "internal" in modes and own and p.ifsc[:4].upper() in own:
        return "internal"
    pref = rules.get("other_bank_mode") or "auto"
    rtgs_from = money(rules.get("rtgs_from") or RTGS_MIN)
    if pref == "imps" and "imps" in modes and p.amount <= IMPS_MAX:
        return "imps"
    if pref in ("auto", "rtgs", "imps") and "rtgs" in modes and p.amount >= max(rtgs_from, RTGS_MIN):
        return "rtgs"
    if "neft" in modes:
        return "neft"
    return modes[0]


def check_payee(p: Payee) -> list[Problem]:
    out: list[Problem] = []

    def bad(level, code, msg):
        out.append(Problem(level, code, msg, p.name, p.employee_code, p.payslip_id))

    acct = re.sub(r"[\s-]", "", p.account or "")
    ifsc = (p.ifsc or "").strip().upper()
    if not acct:
        bad("error", "no_account", "No bank account number.")
    elif not ACCOUNT_RE.match(acct):
        bad("error", "bad_account", "The account number should be 6 to 20 letters or digits.")
    elif not acct.isdigit():
        bad("warn", "account_letters", "The account number has letters; most Indian banks use digits only.")
    if not ifsc:
        bad("error", "no_ifsc", "No IFSC.")
    elif not IFSC_RE.match(ifsc):
        bad("error", "bad_ifsc", f"“{ifsc}” is not a valid IFSC (4 letters, a zero, then 6 letters or digits).")
    elif acct.isdigit() and ifsc[:4] in ACCOUNT_LENGTHS and len(acct) not in ACCOUNT_LENGTHS[ifsc[:4]]:
        bad("warn", "account_length", f"{ifsc[:4]} account numbers usually have "
            f"{' or '.join(str(n) for n in ACCOUNT_LENGTHS[ifsc[:4]])} digits; this one has {len(acct)}.")
    if not clean_text(p.name):
        bad("error", "no_name", "No account holder name.")
    if money(p.amount) <= 0:
        bad("error", "no_amount", "Nothing to pay.")
    return out


def uses(spec: dict, token: str) -> bool:
    """Whether the layout (any line or variant) prints this token."""
    rows = [spec.get("columns") or []] + list(spec.get("header_lines") or []) + list(spec.get("trailer_lines") or [])
    for v in (spec.get("variants") or {}).values():
        rows += [v.get("columns") or []] + list(v.get("header_lines") or []) + list(v.get("trailer_lines") or [])
    return any(re.search(r"\{" + token + r"(:[^}]*)?\}", c.get("value") or "") for row in rows for c in row)


def check_debit(d: Debit, spec: dict) -> list[tuple[str, str]]:
    """[(kind, message)]: kind "account" for the debit account itself, "requires" for codes the
    bank needs. Only what the layout prints (or needs) is checked."""
    errs = []
    if uses(spec, "debit_account"):
        if not d.account:
            errs.append(("account", "The company account to pay from has no account number."))
        elif not ACCOUNT_RE.match(re.sub(r"[\s-]", "", d.account)):
            errs.append(("account", "The company account number is not valid."))
    if (uses(spec, "debit_ifsc") or uses(spec, "debit_branch")) and not IFSC_RE.match((d.ifsc or "").upper()):
        errs.append(("account", "The company account's IFSC is not valid."))
    for need in spec.get("requires") or []:
        if not getattr(d, need, ""):
            errs.append(("requires", {
                "client_code": "This bank's file needs the client code the bank gave you (set it on the debit account).",
                "corporate_id": "This bank's file needs your corporate id (set it on the debit account)."}.get(need, need)))
    return errs


def for_group(spec: dict, group: str) -> dict:
    """The layout for one file of a split: the base layout with that group's changes."""
    variants = spec.get("variants") or {}
    v = variants.get(group) or (variants.get("other") if group in ("neft", "rtgs", "imps") else None) or {}
    out = {**spec, **v}
    out.pop("variants", None)
    return out


def group_of(mode: str, spec: dict) -> str:
    split = spec.get("split") or "none"
    if split == "internal_vs_other":
        return "internal" if mode == "internal" else "other"
    if split == "by_mode":
        return mode
    return "all"


# ── Rendering ────────────────────────────────────────────────────────

def render(spec: dict, lines: list[Line], debit: Debit, ctx: dict) -> Rendered:
    """ctx: date (value date), narration, period, month, batch, batch_ref, file_date."""
    validate_spec(spec)
    codes = spec.get("codes") or {}
    base = {"debit_account": debit.account, "debit_ifsc": (debit.ifsc or "").upper(), "debit_name": debit.name,
            "debit_branch": (debit.ifsc or "")[5:].upper(),
            "client_code": debit.client_code, "corporate_id": debit.corporate_id, **ctx}
    out: list[str] = []
    total = sum((money(ln.payee.amount) for ln in lines), Decimal("0"))
    acct_hash = sum(int(re.sub(r"\D", "", ln.payee.account) or 0) for ln in lines) % (10 ** 15)
    totals = {**base, "count": len(lines), "total": fmt_amount(total, spec), "total_paise": str(int(total * 100)),
              "account_hash": str(acct_hash)}
    for row in spec.get("header_lines") or []:
        out.append(_join([_cell(c, totals, spec) for c in row], spec))
    if spec.get("header_row") and spec.get("kind") != "fixed":
        out.append(_join([c.get("label") or "" for c in spec["columns"]], spec))
    for i, ln in enumerate(lines, start=1):
        p = ln.payee
        values = {**base, "seq": i, "name": p.name, "account": re.sub(r"[\s-]", "", p.account), "ifsc": p.ifsc.upper(),
                  "branch": p.ifsc[5:].upper(), "bank_name": p.bank_name, "email": p.email, "phone": p.phone, "employee_code": p.employee_code,
                  "amount": fmt_amount(p.amount, spec), "amount_paise": str(int(money(p.amount) * 100)),
                  "mode": {"internal": "FT", "neft": "NEFT", "rtgs": "RTGS", "imps": "IMPS"}[ln.mode],
                  "mode_code": codes.get(ln.mode, ""),
                  "payment_ref": (f"{p.employee_code}{ctx.get('period', '')}" or str(i))[:20]}
        out.append(_join([_cell(c, values, spec) for c in spec["columns"]], spec))
    for row in spec.get("trailer_lines") or []:
        out.append(_join([_cell(c, totals, spec) for c in row], spec))
    eol = "\n" if spec.get("line_ending") == "lf" else "\r\n"
    text = eol.join(out) + eol
    return Rendered(text.encode(spec.get("encoding") or "utf-8", errors="replace"), out, len(lines), total)


def file_name(spec: dict, key: str, ctx: dict, debit: Debit, group: str) -> str:
    pattern = spec.get("file_name") or "salary-{format}-{period}-{group}-{batch}"
    values = {**ctx, "format": key.replace("custom:", "custom-")[:20], "group": group, "client_code": debit.client_code,
              "corporate_id": debit.corporate_id, "debit_last4": (debit.account or "")[-4:]}
    name = _render(pattern, values, spec)
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "salary"
    return f"{name[:120]}.{spec.get('extension') or 'txt'}"


def validate_spec(spec: dict) -> None:
    for v in (spec.get("variants") or {}).values():
        validate_spec({**spec, **v, "variants": None})
    if spec.get("kind") not in ("delimited", "fixed"):
        raise SpecError("Choose a separated (CSV-like) or a fixed width file.")
    cols = spec.get("columns") or []
    if not cols:
        raise SpecError("Add at least one column to the payment line.")
    if len(cols) > 80:
        raise SpecError("A line can have at most 80 columns.")
    if spec["kind"] == "delimited" and not delimiter(spec):
        raise SpecError("Choose the separator.")
    allowed_row = set(TOKENS_ROW)
    allowed_total = set(TOKENS_TOTAL)
    for c in cols:
        _check_tokens(c, allowed_row, spec)
    for row in (spec.get("header_lines") or []) + (spec.get("trailer_lines") or []):
        for c in row:
            _check_tokens(c, allowed_total, spec)
    a = spec.get("amount") or {}
    if int(a.get("decimals", 2)) not in (0, 2):
        raise SpecError("Amounts have 0 or 2 decimals.")
    if (spec.get("split") or "none") not in ("none", "internal_vs_other", "by_mode"):
        raise SpecError("Unknown way to split files.")
    try:
        date(2026, 1, 31).strftime(spec.get("date_format") or "%d/%m/%Y")
    except Exception:
        raise SpecError("The date format is not valid.")


def _check_tokens(col: dict, allowed: set, spec: dict) -> None:
    for m in _TOKEN.finditer(col.get("value") or ""):
        if m.group(1) not in allowed:
            raise SpecError(f"“{{{m.group(1)}}}” cannot be used here.")
    if spec.get("kind") == "fixed" and not int(col.get("width") or 0):
        raise SpecError(f"Give column “{col.get('label') or col.get('value') or '?'}” a width.")
