"""Merge fields for letters: {{employee.full_name}}, {{salary.ctc|money}}, {{letter.date|date}}.

Values are looked up in a flat dict ("employee.full_name" -> "Priya Sharma") built from
the employee, the company, the legal entity, the job and anything typed at generation
time. Every value is HTML-escaped, except the few image fields (company.logo) which are
built here. Unknown or empty fields render as nothing and are reported as missing.

Filters: upper, lower, title, date (7 November 2026), money (₹1,23,456.00),
words (amount in Indian words: "One lakh twenty-three thousand ...").

Other modules can add values (for example payroll adds salary.*) with
`register_provider(fn)` where fn(db, company_id, employee) -> {"salary.ctc": "..."}.
"""
from __future__ import annotations

import html
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Callable

FIELD_RE = re.compile(r"\{\{\s*([a-zA-Z_]\w*(?:\.\w+)*)\s*(?:\|\s*([a-z_]+)\s*)?\}\}")
RAW_FIELDS = {"company.logo", "company.logo_url_img", "signatory.signature"}

CATALOGUE: list[dict] = [
    {"group": "employee", "label": "Employee", "fields": [
        ("employee.full_name", "Full name", "Priya Sharma"),
        ("employee.title", "Title (Mr., Ms.)", "Ms."),
        ("employee.first_name", "First name", "Priya"),
        ("employee.last_name", "Last name", "Sharma"),
        ("employee.employee_code", "Employee ID", "EMP0042"),
        ("employee.email", "Email", "priya.sharma@example.com"),
        ("employee.phone", "Phone", "98450 12345"),
        ("employee.address", "Address", "12, MG Road, Bengaluru 560001"),
        ("employee.date_of_birth", "Date of birth", "1994-03-12"),
        ("employee.pan", "PAN", "ABCPS1234K"),
    ]},
    {"group": "job", "label": "Job", "fields": [
        ("job.designation", "Designation", "Senior Analyst"),
        ("job.department", "Department", "Finance"),
        ("job.manager", "Reporting manager", "Rahul Verma"),
        ("job.location", "Work location", "Bengaluru"),
        ("job.employment_type", "Employment type", "Permanent"),
        ("job.joining_date", "Date of joining", "2024-06-03"),
        ("job.confirmation_date", "Confirmation date", "2024-12-03"),
        ("job.probation_months", "Probation (months)", "6"),
        ("job.notice_period_days", "Notice period (days)", "60"),
        ("job.last_working_day", "Last working day", "2026-12-31"),
        ("job.new_designation", "New designation", "Lead Analyst"),
        ("job.new_department", "New department", "Strategy"),
        ("job.new_location", "New location", "Pune"),
        ("job.effective_date", "Effective date", "2026-12-01"),
        ("job.tenure", "Length of service", "2 years 5 months"),
    ]},
    {"group": "salary", "label": "Salary", "fields": [
        ("salary.ctc", "Annual CTC", "1200000"),
        ("salary.ctc_monthly", "Monthly CTC", "100000"),
        ("salary.basic", "Basic (monthly)", "50000"),
        ("salary.hra", "HRA (monthly)", "20000"),
        ("salary.special_allowance", "Special allowance (monthly)", "24000"),
        ("salary.gross_monthly", "Gross (monthly)", "94000"),
        ("salary.net_monthly", "Take-home (monthly)", "86500"),
        ("salary.previous_ctc", "Previous annual CTC", "1050000"),
        ("salary.increment_percent", "Increment %", "14.3"),
        ("salary.effective_from", "Revised salary from", "2026-04-01"),
    ]},
    {"group": "company", "label": "Company", "fields": [
        ("company.name", "Company name", "Next Gen HRMS Demo"),
        ("company.address", "Address", "4th Floor, Prestige Tower, Bengaluru"),
        ("company.city", "City", "Bengaluru"),
        ("company.state", "State", "Karnataka"),
        ("company.pincode", "PIN code", "560001"),
        ("company.email", "Email", "hr@example.com"),
        ("company.phone", "Phone", "080 4000 1234"),
        ("company.logo", "Logo (image)", ""),
    ]},
    {"group": "legal_entity", "label": "Legal entity", "fields": [
        ("legal_entity.name", "Registered name", "Next Gen HRMS Demo Private Limited"),
        ("legal_entity.address", "Registered address", "4th Floor, Prestige Tower, Bengaluru 560001"),
        ("legal_entity.cin", "CIN", "U72900KA2020PTC123456"),
        ("legal_entity.pan", "PAN", "AABCN1234F"),
        ("legal_entity.tan", "TAN", "BLRN12345E"),
        ("legal_entity.gstin", "GSTIN", "29AABCN1234F1Z5"),
    ]},
    {"group": "letter", "label": "Letter", "fields": [
        ("letter.date", "Letter date", "2026-11-07"),
        ("letter.reference_no", "Reference number", "LTR/2627/0012"),
        ("letter.signatory_name", "Signatory name", "Anita Rao"),
        ("letter.signatory_designation", "Signatory designation", "Head of HR"),
        ("letter.purpose", "Purpose / addressed to", "To whom it may concern"),
        ("letter.reason", "Reason", "Bank loan"),
        ("letter.details", "Extra details", ""),
    ]},
]

MONEY_FIELDS = {"salary.ctc", "salary.ctc_monthly", "salary.basic", "salary.hra", "salary.special_allowance",
                "salary.gross_monthly", "salary.net_monthly", "salary.previous_ctc"}
DATE_FIELDS = {"employee.date_of_birth", "job.joining_date", "job.confirmation_date", "job.last_working_day",
               "job.effective_date", "salary.effective_from", "letter.date"}


def sample_values() -> dict[str, str]:
    return {key: sample for group in CATALOGUE for key, _label, sample in group["fields"] if sample}


def catalogue() -> list[dict]:
    return [{"group": g["group"], "label": g["label"],
             "fields": [{"key": k, "label": label, "sample": s} for k, label, s in g["fields"]]} for g in CATALOGUE]


_PROVIDERS: list[Callable] = []


def register_provider(fn: Callable) -> None:
    _PROVIDERS.append(fn)


def providers() -> list[Callable]:
    return list(_PROVIDERS)


# ── Formatting ──────────────────────────────────────────────────────

def _to_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def format_date(value) -> str:
    d = _to_date(value)
    return f"{d.day} {d.strftime('%B %Y')}" if d else str(value)


def _decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", "").replace("₹", "").strip())
    except (InvalidOperation, ValueError):
        return None


def indian_grouping(n: int) -> str:
    s = str(abs(n))
    if len(s) <= 3:
        out = s
    else:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        out = ",".join(parts) + "," + tail
    return ("-" if n < 0 else "") + out


def format_money(value) -> str:
    d = _decimal(value)
    if d is None:
        return str(value)
    d = d.quantize(Decimal("0.01"))
    whole = int(d)
    paise = abs(int((d - whole) * 100))
    return f"₹{indian_grouping(whole)}.{paise:02d}"


_ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
         "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def _two(n: int) -> str:
    if n < 20:
        return _ONES[n]
    return _TENS[n // 10] + ("-" + _ONES[n % 10] if n % 10 else "")


def _three(n: int) -> str:
    h, rest = divmod(n, 100)
    parts = []
    if h:
        parts.append(_ONES[h] + " hundred")
    if rest:
        parts.append(_two(rest))
    return " ".join(parts)


def number_in_words(n: int) -> str:
    """Indian system: crore, lakh, thousand."""
    if n == 0:
        return "zero"
    parts = []
    crore, n = divmod(n, 10_000_000)
    lakh, n = divmod(n, 100_000)
    thousand, n = divmod(n, 1000)
    if crore:
        parts.append(number_in_words(crore) + " crore")
    if lakh:
        parts.append(_two(lakh) + " lakh")
    if thousand:
        parts.append(_two(thousand) + " thousand")
    if n:
        parts.append(_three(n))
    return " ".join(parts)


def amount_in_words(value) -> str:
    d = _decimal(value)
    if d is None:
        return str(value)
    d = d.quantize(Decimal("0.01"))
    rupees = int(d)
    paise = int((d - rupees) * 100)
    text = "Rupees " + number_in_words(rupees)
    if paise:
        text += " and " + number_in_words(paise) + " paise"
    return (text + " only").capitalize().replace("rupees", "Rupees")


def apply_filter(value, name: str | None, key: str = "") -> str:
    if name is None:
        if key in DATE_FIELDS and _to_date(value):
            return format_date(value)
        return str(value)
    if name == "upper":
        return str(value).upper()
    if name == "lower":
        return str(value).lower()
    if name == "title":
        return str(value).title()
    if name == "date":
        return format_date(value)
    if name == "money":
        return format_money(value)
    if name == "words":
        return amount_in_words(value)
    return str(value)


def render(template: str, values: dict, *, escape: bool = True) -> tuple[str, list[str]]:
    """Fill {{fields}}. Returns the text and the sorted list of fields that had no value."""
    missing: set[str] = set()

    def sub(m: re.Match) -> str:
        key, filt = m.group(1), m.group(2)
        value = values.get(key)
        if value is None or value == "":
            missing.add(key)
            return ""
        if key in RAW_FIELDS:
            return str(value)
        out = apply_filter(value, filt, key)
        return html.escape(out) if escape else out

    return FIELD_RE.sub(sub, template or ""), sorted(missing)


def fields_used(template: str) -> list[str]:
    return sorted({m.group(1) for m in FIELD_RE.finditer(template or "")})
