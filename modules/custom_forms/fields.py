"""Form definitions and values: field types, checking a form built in the designer, and
cleaning what people submit (types, limits, required, show-if conditions, formulas and
per-role field access).

A form is {"fields": [field, ...]} where a field is

    {"key": "start_date", "type": "date", "label": "Start date", "help": "…", "placeholder": "…",
     "required": true, "unique": false, "default": "today", "width": "half",
     "options": ["Internal", "External"],            # single_choice / multi_choice
     "display": "dropdown" | "radio",                 # single_choice
     "master": "blood_group", "multiple": false,      # master / employee / department / location
     "min": 0, "max": 100, "min_length": 2, "max_length": 200, "decimals": 2,
     "formula": "fee + travel_cost", "result": "currency",   # formula
     "show_if": "training_type != 'Internal'",
     "permissions": {"employee": "view", "manager": "hidden"},  # by role; others may edit
     "columns": [field, ...], "min_rows": 1, "max_rows": 20,    # table (line items)
     "accept": "any" | "images" | "documents", "max_files": 3,  # file
     "summary": true}                                  # shown as a column in lists

Section headings ("section") have a label and help text and may carry a show-if that hides
every field up to the next section. Conditions and formulas use the safe expression
language in core/expressions.py; field keys are the variable names, `requester.*` gives
the person filling the form, and a table's key gives its totals (`items.amount`, `items.count`).
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from core import expressions
from core.errors import BadRequest

FIELD_TYPES: dict[str, str] = {
    "text": "Short text", "textarea": "Long text", "number": "Whole number", "decimal": "Decimal number",
    "currency": "Amount (₹)", "date": "Date", "datetime": "Date and time", "time": "Time", "boolean": "Yes / no",
    "single_choice": "Choose one", "multi_choice": "Choose many", "master": "List from masters",
    "employee": "Employee", "department": "Department", "location": "Location", "file": "File upload",
    "email": "Email", "phone": "Phone", "url": "Web link", "formula": "Formula", "section": "Section heading",
    "table": "Table (line items)",
}
GROUPS = [
    ("Basic", ["text", "textarea", "number", "decimal", "currency", "boolean"]),
    ("Date and time", ["date", "datetime", "time"]),
    ("Choices and lookups", ["single_choice", "multi_choice", "master", "employee", "department", "location"]),
    ("Contact and files", ["email", "phone", "url", "file"]),
    ("Layout and logic", ["section", "formula", "table"]),
]
TABLE_COLUMN_TYPES = {"text", "textarea", "number", "decimal", "currency", "date", "time", "boolean", "single_choice",
                      "master", "employee", "email", "phone", "url", "formula"}
NUMERIC = {"number", "decimal", "currency"}
LOOKUPS = {"master", "employee", "department", "location"}
LAYOUT = {"section"}
UNIQUE_TYPES = {"text", "number", "email", "phone", "url", "date", "employee"}
FORMULA_RESULTS = {"number": "Number", "currency": "Amount (₹)", "text": "Text", "date": "Date", "boolean": "Yes / no"}
ACCESS = ("hidden", "view", "edit")
ACCESS_RANK = {a: i for i, a in enumerate(ACCESS)}
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
RESERVED = {"requester", "today", "request", "status", "reference_no", "row", "and", "or", "not", "in", "true",
            "false", "null", "none", "between", "if"}
REQUESTER_VARS = {
    "requester.role": "Role of the person filling the form",
    "requester.department_id": "Their department",
    "requester.location_id": "Their location",
    "requester.designation_id": "Their designation",
    "requester.employee_code": "Their employee ID",
    "requester.is_manager": "Whether they manage anyone",
}
MAX_FIELDS = 150
MAX_COLUMNS = 25
MAX_OPTIONS = 200
MAX_ROWS = 100
MAX_FILES = 10
TEXT_LIMIT = 255
TEXTAREA_LIMIT = 5000


# ── Checking a form from the designer ───────────────────────────────

def slug_key(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (label or "").lower()).strip("_")
    if not s or not s[0].isalpha():
        s = f"f_{s}" if s else "field"
    return s[:40]


def _num_or_none(v, what: str, label: str):
    if v in (None, ""):
        return None
    try:
        return float(Decimal(str(v)))
    except (InvalidOperation, ValueError):
        raise BadRequest(f"{label}: {what} must be a number.")


def _clean_field(raw: dict, *, in_table: bool, seen: set[str], index: int) -> dict:
    if not isinstance(raw, dict):
        raise BadRequest("Each field must be an object.")
    ftype = raw.get("type")
    label = str(raw.get("label") or "").strip()
    where = f"Field {index + 1}" + (f" ({label})" if label else "")
    if ftype not in FIELD_TYPES:
        raise BadRequest(f"{where}: pick a field type.")
    if in_table and ftype not in TABLE_COLUMN_TYPES:
        raise BadRequest(f"{where}: a {FIELD_TYPES[ftype].lower()} can't be used inside a table.")
    if not label:
        raise BadRequest(f"Field {index + 1}: give it a label.")
    if len(label) > 120:
        raise BadRequest(f"{where}: the label can be at most 120 characters.")
    key = str(raw.get("key") or "").strip() or slug_key(label)
    if not KEY_RE.match(key) or key in RESERVED:
        raise BadRequest(f"{where}: the field name “{key}” must start with a letter and use only small letters, "
                         "numbers and _ (up to 40).")
    if key in seen:
        raise BadRequest(f"{where}: two fields are called “{key}”. Field names must be different.")
    seen.add(key)
    f: dict = {"key": key, "type": ftype, "label": label}
    help_text = str(raw.get("help") or "").strip()
    if len(help_text) > 1000:
        raise BadRequest(f"{where}: help text can be at most 1,000 characters.")
    if help_text:
        f["help"] = help_text
    if ftype == "section":
        if raw.get("show_if"):
            f["show_if"] = str(raw["show_if"]).strip()
        return f
    placeholder = str(raw.get("placeholder") or "").strip()[:120]
    if placeholder:
        f["placeholder"] = placeholder
    if ftype != "formula":
        f["required"] = bool(raw.get("required"))
    if raw.get("unique"):
        if in_table or ftype not in UNIQUE_TYPES:
            raise BadRequest(f"{where}: only text, number, email, phone, link, date or employee fields can be unique.")
        f["unique"] = True
    if raw.get("width") in ("half", "full"):
        f["width"] = raw["width"]
    if raw.get("summary"):
        f["summary"] = True
    if raw.get("show_if") and not in_table:
        f["show_if"] = str(raw["show_if"]).strip()

    if ftype in ("text", "textarea", "email", "phone", "url"):
        limit = TEXTAREA_LIMIT if ftype == "textarea" else TEXT_LIMIT
        mn = _num_or_none(raw.get("min_length"), "the shortest length", label)
        mx = _num_or_none(raw.get("max_length"), "the longest length", label)
        if mn is not None:
            f["min_length"] = max(0, int(mn))
        if mx is not None:
            f["max_length"] = max(1, min(limit, int(mx)))
        if mn is not None and mx is not None and f["min_length"] > f["max_length"]:
            raise BadRequest(f"{where}: the shortest length is more than the longest.")
    if ftype in NUMERIC:
        mn = _num_or_none(raw.get("min"), "the smallest value", label)
        mx = _num_or_none(raw.get("max"), "the largest value", label)
        if mn is not None:
            f["min"] = mn
        if mx is not None:
            f["max"] = mx
        if mn is not None and mx is not None and mn > mx:
            raise BadRequest(f"{where}: the smallest value is more than the largest.")
        if ftype == "decimal":
            d = raw.get("decimals")
            f["decimals"] = max(0, min(6, int(d))) if d not in (None, "") else 2
    if ftype in ("single_choice", "multi_choice"):
        opts = raw.get("options") or []
        if isinstance(opts, str):
            opts = opts.splitlines()
        clean, lower = [], set()
        for o in opts:
            o = str(o.get("label") if isinstance(o, dict) else o).strip()
            if not o:
                continue
            if len(o) > 120:
                raise BadRequest(f"{where}: an option can be at most 120 characters.")
            if o.lower() in lower:
                raise BadRequest(f"{where}: the option “{o}” is listed twice.")
            lower.add(o.lower())
            clean.append(o)
        if not clean:
            raise BadRequest(f"{where}: add at least one option.")
        if len(clean) > MAX_OPTIONS:
            raise BadRequest(f"{where}: up to {MAX_OPTIONS} options.")
        f["options"] = clean
        if ftype == "single_choice":
            f["display"] = "radio" if raw.get("display") == "radio" else "dropdown"
    if ftype == "master":
        master = str(raw.get("master") or "").strip()
        if not master:
            raise BadRequest(f"{where}: pick the master list it uses.")
        f["master"] = master
    if ftype in LOOKUPS and not in_table:
        f["multiple"] = bool(raw.get("multiple"))
    if ftype == "file":
        f["accept"] = raw.get("accept") if raw.get("accept") in ("any", "images", "documents") else "any"
        mf = raw.get("max_files")
        f["max_files"] = max(1, min(MAX_FILES, int(mf))) if mf not in (None, "") else 1
    if ftype == "formula":
        expr = str(raw.get("formula") or "").strip()
        if not expr:
            raise BadRequest(f"{where}: write the formula.")
        f["formula"] = expr
        f["result"] = raw.get("result") if raw.get("result") in FORMULA_RESULTS else "number"
        if raw.get("decimals") not in (None, ""):
            f["decimals"] = max(0, min(6, int(raw["decimals"])))
    if ftype == "table":
        cols_raw = raw.get("columns") or []
        if not cols_raw:
            raise BadRequest(f"{where}: add at least one column to the table.")
        if len(cols_raw) > MAX_COLUMNS:
            raise BadRequest(f"{where}: a table can have up to {MAX_COLUMNS} columns.")
        col_seen: set[str] = set()
        f["columns"] = [_clean_field(c, in_table=True, seen=col_seen, index=i) for i, c in enumerate(cols_raw)]
        mn = int(raw.get("min_rows") or 0)
        mx = int(raw.get("max_rows") or 20)
        f["min_rows"] = max(0, min(MAX_ROWS, mn))
        f["max_rows"] = max(1, min(MAX_ROWS, mx))
        if f["min_rows"] > f["max_rows"]:
            raise BadRequest(f"{where}: the fewest rows is more than the most rows.")
        if f.get("required") and f["min_rows"] == 0:
            f["min_rows"] = 1
    if raw.get("default") not in (None, "", []) and ftype not in ("formula", "table", "file"):
        f["default"] = raw["default"]
    perms = raw.get("permissions") or {}
    if isinstance(perms, dict):
        clean_perms = {str(r): a for r, a in perms.items() if a in ACCESS and str(r).strip()}
        if clean_perms and not in_table:
            f["permissions"] = clean_perms
    return f


def input_fields(definition: dict) -> list[dict]:
    """Fields that hold a value (no section headings)."""
    return [f for f in (definition or {}).get("fields", []) if f.get("type") not in LAYOUT]


def all_keys(definition: dict) -> list[str]:
    return [f["key"] for f in input_fields(definition)]


def clean_definition(raw: dict | list | None, *, master_keys: set[str] | None = None) -> dict:
    """Check a form from the designer and return the stored shape. Raises BadRequest with a
    plain message naming the field."""
    items = raw.get("fields") if isinstance(raw, dict) else raw
    items = items or []
    if not isinstance(items, list):
        raise BadRequest("The form must be a list of fields.")
    if len(items) > MAX_FIELDS:
        raise BadRequest(f"A form can have up to {MAX_FIELDS} fields.")
    seen: set[str] = set()
    fields = [_clean_field(f, in_table=False, seen=seen, index=i) for i, f in enumerate(items)]
    if not any(f["type"] not in LAYOUT for f in fields):
        raise BadRequest("Add at least one field to the form.")
    definition = {"fields": fields}
    keys = set(all_keys(definition))
    allowed = keys | set(REQUESTER_VARS)
    for f in fields:
        if master_keys is not None:
            for x in [f, *(f.get("columns") or [])]:
                if x.get("type") == "master" and x.get("master") not in master_keys:
                    raise BadRequest(f"{x['label']}: the master list “{x.get('master')}” doesn't exist.")
        for attr in ("show_if", "formula"):
            if f.get(attr):
                _check_expr(f, attr, f[attr], allowed)
        for c in f.get("columns") or []:
            if c.get("formula"):
                _check_expr(c, "formula", c["formula"], allowed | {x["key"] for x in f["columns"]})
        if f.get("default") is not None:
            try:
                clean_value(f, _resolve_default(f, None, f["default"]), lookups=None)
            except BadRequest as e:
                raise BadRequest(f"{f['label']}: the starting value is not valid ({e.detail})")
    return definition


def _check_expr(f: dict, attr: str, text: str, allowed: set[str]) -> None:
    what = "condition" if attr == "show_if" else "formula"
    try:
        expressions.validate(text, allowed)
    except expressions.ExpressionError as e:
        raise BadRequest(f"{f['label']}: the {what} has a mistake. {e}")


# ── Who may see and change a field ──────────────────────────────────

def access_for(field: dict, roles: set[str], *, full: bool = False) -> str:
    """"edit", "view" or "hidden" for a person holding `roles`. Roles not listed on the field
    may edit; with several roles the widest wins. `full` (the company owner) always edits."""
    if full:
        return "edit"
    perms = field.get("permissions") or {}
    if not perms:
        return "edit"
    best = None
    for r in roles or {"employee"}:
        a = perms.get(r, "edit")
        if best is None or ACCESS_RANK[a] > ACCESS_RANK[best]:
            best = a
    return best or "edit"


def visible_definition(definition: dict, roles: set[str], *, full: bool = False) -> dict:
    """The form as one person sees it: hidden fields removed, each field marked with its access."""
    out = []
    for f in (definition or {}).get("fields", []):
        if f.get("type") in LAYOUT:
            out.append(dict(f))
            continue
        a = access_for(f, roles, full=full)
        if a == "hidden":
            continue
        g = {k: v for k, v in f.items() if k != "permissions"}
        g["access"] = a
        out.append(g)
    # drop sections left with no fields under them
    cleaned = []
    for i, f in enumerate(out):
        if f.get("type") == "section":
            nxt = out[i + 1] if i + 1 < len(out) else None
            if nxt is None or nxt.get("type") == "section":
                continue
        cleaned.append(f)
    return {"fields": cleaned}


# ── Values ──────────────────────────────────────────────────────────

class Lookups:
    """Checks ids of employees, departments, locations and master values for one company.
    Built by the service; tests can pass their own."""

    def master_ids(self, master: str, ids: list[int]) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def entity_ids(self, kind: str, ids: list[int]) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def files(self, uuids: list) -> list[dict]:  # pragma: no cover - interface
        raise NotImplementedError


def _empty(v) -> bool:
    return v is None or v == "" or v == [] or v == {}


def _decimal(value, label: str) -> Decimal:
    if isinstance(value, bool):
        raise BadRequest(f"{label} must be a number.")
    try:
        d = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        raise BadRequest(f"{label} must be a number.")
    if not d.is_finite():
        raise BadRequest(f"{label} must be a number.")
    return d


def _ids(value, label: str, multiple: bool) -> list[int]:
    items = value if isinstance(value, list) else [value]
    out = []
    for i in items:
        if isinstance(i, dict):
            i = i.get("value", i.get("id"))
        try:
            out.append(int(i))
        except (TypeError, ValueError):
            raise BadRequest(f"Pick {label} from the list.")
    if not multiple and len(out) != 1:
        raise BadRequest(f"Pick one value for {label}.")
    return out


def clean_value(f: dict, value, *, lookups: Lookups | None):
    """Check and normalise one non-empty value. Numbers with decimals and amounts are kept as
    text ("1500.00") so money stays exact; whole numbers as integers."""
    t, label = f["type"], f["label"]
    if t in ("text", "textarea", "email", "phone", "url"):
        if isinstance(value, (dict, list)):
            raise BadRequest(f"{label} must be text.")
        s = str(value).strip()
        if f.get("min_length") and len(s) < int(f["min_length"]):
            raise BadRequest(f"{label} must be at least {f['min_length']} characters.")
        limit = int(f.get("max_length") or (TEXTAREA_LIMIT if t == "textarea" else TEXT_LIMIT))
        if len(s) > limit:
            raise BadRequest(f"{label} can be at most {limit} characters.")
        if t == "email" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", s):
            raise BadRequest(f"{label} must be an email address.")
        if t == "phone" and not re.fullmatch(r"\+?[\d][\d\s-]{6,19}", s):
            raise BadRequest(f"{label} must be a phone number.")
        if t == "url" and not re.match(r"https?://[^\s]+$", s):
            raise BadRequest(f"{label} must be a web link starting with http:// or https://.")
        return s
    if t in NUMERIC:
        d = _decimal(value, label)
        if t == "number" and d != d.to_integral_value():
            raise BadRequest(f"{label} must be a whole number.")
        if f.get("min") is not None and d < Decimal(str(f["min"])):
            raise BadRequest(f"{label} must be at least {_fmt_num(f['min'])}.")
        if f.get("max") is not None and d > Decimal(str(f["max"])):
            raise BadRequest(f"{label} must be at most {_fmt_num(f['max'])}.")
        if t == "number":
            if abs(d) >= 10 ** 15:
                raise BadRequest(f"{label} is too large.")
            return int(d)
        places = 2 if t == "currency" else int(f.get("decimals", 2))
        q = Decimal(1).scaleb(-places)
        if d != d.quantize(q, rounding=ROUND_HALF_UP):
            raise BadRequest(f"{label} can have at most {places} decimal places." if places else
                             f"{label} must be a whole number.")
        if abs(d) >= 10 ** 13:
            raise BadRequest(f"{label} is too large.")
        return str(d.quantize(q))
    if t == "date":
        try:
            return date.fromisoformat(str(value)[:10]).isoformat()
        except ValueError:
            raise BadRequest(f"{label} must be a date.")
    if t == "datetime":
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            raise BadRequest(f"{label} must be a date and time.")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if t == "time":
        try:
            return time.fromisoformat(str(value)[:8]).strftime("%H:%M")
        except ValueError:
            raise BadRequest(f"{label} must be a time such as 09:30.")
    if t == "boolean":
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("true", "yes", "1", "on"):
            return True
        if s in ("false", "no", "0", "off"):
            return False
        raise BadRequest(f"{label} must be yes or no.")
    if t in ("single_choice", "multi_choice"):
        items = value if isinstance(value, list) else [value]
        allowed = {o.lower(): o for o in f.get("options") or []}
        out = []
        for i in items:
            o = allowed.get(str(i).strip().lower())
            if o is None:
                raise BadRequest(f"Pick a value for {label} from the list.")
            if o not in out:
                out.append(o)
        if t == "single_choice":
            if len(out) != 1:
                raise BadRequest(f"Pick one value for {label}.")
            return out[0]
        return out
    if t in LOOKUPS:
        multiple = bool(f.get("multiple"))
        ids = _ids(value, label, multiple)
        if lookups is not None:
            if t == "master":
                lookups.master_ids(f["master"], ids)
            else:
                lookups.entity_ids(t, ids)
        return ids if multiple else ids[0]
    if t == "file":
        items = value if isinstance(value, list) else [value]
        uuids = [i.get("uuid") if isinstance(i, dict) else i for i in items]
        uuids = [u for u in uuids if u]
        limit = int(f.get("max_files") or 1)
        if len(uuids) > limit:
            raise BadRequest(f"{label} takes up to {limit} file{'s' if limit != 1 else ''}.")
        if lookups is None:
            return [{"uuid": str(u)} for u in uuids]
        files = lookups.files(uuids)
        accept = f.get("accept") or "any"
        for fl in files:
            ct = (fl.get("content_type") or "").lower()
            if accept == "images" and not ct.startswith("image/"):
                raise BadRequest(f"{label} takes images only (JPG or PNG).")
            if accept == "documents" and not (ct.startswith("image/") or "pdf" in ct or "word" in ct
                                              or "sheet" in ct or "excel" in ct or ct.startswith("text/")):
                raise BadRequest(f"{label} takes documents only (PDF, Word, Excel or images).")
        return files
    raise BadRequest(f"{label} can't be filled in.")


def _fmt_num(v) -> str:
    d = Decimal(str(v))
    return str(int(d)) if d == d.to_integral_value() else str(d)


def _resolve_default(f: dict, requester: dict | None, default):
    """Starting values may be tokens: "today" / "now" for dates, "me" for an employee field,
    "my" for the person's own department or location."""
    if default == "today" and f["type"] == "date":
        return date.today().isoformat() if requester is None else requester.get("today", date.today()).isoformat()
    if default == "now" and f["type"] == "datetime":
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if default in ("me", "my"):
        if requester is None:
            return 1 if f["type"] in ("employee", "department", "location") else default
        ref = {"employee": "id", "department": "department_id", "location": "location_id"}.get(f["type"])
        v = requester.get(ref) if ref else None
        return ([v] if f.get("multiple") else v) if v else None
    return default


def defaults_for(definition: dict, requester: dict | None) -> dict:
    """Starting values of a new form (tokens resolved for the person filling it)."""
    out = {}
    for f in input_fields(definition):
        if f.get("default") is not None:
            v = _resolve_default(f, requester, f["default"])
            if v not in (None, "", []):
                out[f["key"]] = v
    return out


def _expr_value(f: dict, v):
    """A stored value as the expression language sees it."""
    if v is None:
        return None
    t = f["type"] if f["type"] != "formula" else f.get("result", "number")
    if t in NUMERIC or t == "number":
        try:
            return Decimal(str(v))
        except (InvalidOperation, ValueError):
            return None
    if t == "date":
        try:
            return date.fromisoformat(str(v)[:10])
        except ValueError:
            return None
    if t == "table":
        rows = v if isinstance(v, list) else []
        summary: dict = {"count": Decimal(len(rows))}
        for c in f.get("columns") or []:
            ctype = c["type"] if c["type"] != "formula" else c.get("result", "number")
            if ctype in NUMERIC or ctype == "number":
                total = Decimal(0)
                for r in rows:
                    try:
                        total += Decimal(str(r.get(c["key"]))) if r.get(c["key"]) not in (None, "") else 0
                    except (InvalidOperation, ValueError):
                        pass
                summary[c["key"]] = total
        return summary
    if t == "file":
        return Decimal(len(v)) if isinstance(v, list) else None
    return v


def variables(definition: dict, values: dict, requester: dict | None) -> dict:
    out = {}
    for f in input_fields(definition):
        out[f["key"]] = _expr_value(f, values.get(f["key"]))
    out["requester"] = {k: v for k, v in (requester or {}).items() if k not in ("today",)}
    return out


def _formula_value(f: dict, result):
    """Turn a formula result into the stored shape for its result type."""
    if result is None:
        return None
    kind = f.get("result", "number")
    if kind in ("number", "currency"):
        try:
            d = Decimal(str(result)) if not isinstance(result, Decimal) else result
        except (InvalidOperation, ValueError):
            return None
        if isinstance(result, bool):
            return None
        places = 2 if kind == "currency" else int(f.get("decimals", 2))
        d = d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        return str(d)
    if kind == "date":
        if isinstance(result, datetime):
            return result.date().isoformat()
        if isinstance(result, date):
            return result.isoformat()
        return None
    if kind == "boolean":
        return bool(result)
    return str(expressions.to_json(result))[:TEXTAREA_LIMIT]


def _safe_eval(text: str, variables_: dict, today: date | None):
    try:
        return expressions.evaluate(text, variables_, today=today)
    except (expressions.ExpressionError, ArithmeticError, TypeError, ValueError):
        return None


def _safe_check(text: str | None, variables_: dict, today: date | None) -> bool:
    if not text:
        return True
    try:
        return bool(expressions.check_condition(text, variables_, today=today))
    except (expressions.ExpressionError, ArithmeticError, TypeError, ValueError):
        return False


def compute(definition: dict, values: dict, requester: dict | None, today: date | None = None) -> tuple[dict, set[str]]:
    """Formulas and show-if: returns (values with formulas filled in and hidden fields
    removed, keys of visible fields). Runs twice so a formula may use one further down."""
    vals = dict(values)
    visible: set[str] = set()
    for _ in range(2):
        visible = set()
        section_shown = True
        for f in definition.get("fields", []):
            if f["type"] == "section":
                section_shown = _safe_check(f.get("show_if"), variables(definition, vals, requester), today)
                continue
            shown = section_shown and _safe_check(f.get("show_if"), variables(definition, vals, requester), today)
            if not shown:
                vals.pop(f["key"], None)
                continue
            visible.add(f["key"])
            if f["type"] == "table" and isinstance(vals.get(f["key"]), list):
                vals[f["key"]] = _row_formulas(definition, f, vals, requester, today)
            if f["type"] == "formula":
                res = _formula_value(f, _safe_eval(f["formula"], variables(definition, vals, requester), today))
                if res is None:
                    vals.pop(f["key"], None)
                else:
                    vals[f["key"]] = res
    return vals, visible


def _row_formulas(definition: dict, table: dict, vals: dict, requester, today) -> list[dict]:
    cols = [c for c in table.get("columns") or [] if c["type"] == "formula"]
    rows = vals.get(table["key"]) or []
    if not cols:
        return rows
    base = variables(definition, vals, requester)
    out = []
    for r in rows:
        r = dict(r)
        for c in cols:
            row_vars = {**base, **{x["key"]: _expr_value(x, r.get(x["key"])) for x in table["columns"]}}
            res = _formula_value(c, _safe_eval(c["formula"], row_vars, today))
            if res is None:
                r.pop(c["key"], None)
            else:
                r[c["key"]] = res
        out.append(r)
    return out


def _clean_table(f: dict, value, lookups: Lookups | None, check_required: bool) -> list[dict]:
    if not isinstance(value, list):
        raise BadRequest(f"{f['label']} must be a list of rows.")
    rows = []
    for i, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise BadRequest(f"{f['label']}: row {i + 1} is not valid.")
        row = {}
        for c in f["columns"]:
            if c["type"] == "formula":
                continue
            v = raw.get(c["key"])
            if _empty(v):
                if check_required and c.get("required"):
                    raise BadRequest(f"{f['label']}, row {i + 1}: {c['label']} is required.")
                continue
            try:
                row[c["key"]] = clean_value(c, v, lookups=lookups)
            except BadRequest as e:
                raise BadRequest(f"{f['label']}, row {i + 1}: {e.detail}")
        if row:
            rows.append(row)
    if check_required and len(rows) < int(f.get("min_rows") or 0):
        n = int(f["min_rows"])
        raise BadRequest(f"{f['label']} needs at least {n} row{'s' if n != 1 else ''}.")
    if len(rows) > int(f.get("max_rows") or MAX_ROWS):
        raise BadRequest(f"{f['label']} can have at most {f['max_rows']} rows.")
    return rows


def process(definition: dict, submitted: dict | None, *, existing: dict | None = None, roles: set[str],
            full: bool = False, requester: dict | None = None, lookups: Lookups | None = None,
            check_required: bool = True, today: date | None = None, new: bool = False) -> dict:
    """Check what a person submitted against a form and return the values to store.

    - Only fields this person may edit are taken from `submitted`; others keep their
      existing value (or the starting value on a new form). Unknown keys are refused.
    - Values are cleaned by type; hidden fields (show-if) are dropped; formulas are worked out.
    - Required fields must have a value when they are shown and the person may edit them
      (skipped for drafts with check_required=False).
    """
    submitted = submitted or {}
    fields = {f["key"]: f for f in input_fields(definition)}
    unknown = [k for k in submitted if k not in fields]
    if unknown:
        raise BadRequest(f"Unknown field: {unknown[0]}.")
    values = dict(existing or {})
    if new:
        for k, v in defaults_for(definition, requester).items():
            values.setdefault(k, v)
    editable = {k for k, f in fields.items() if f["type"] != "formula" and access_for(f, roles, full=full) == "edit"}
    for key in editable:
        if key not in submitted:
            continue
        f, v = fields[key], submitted[key]
        if _empty(v) or (f["type"] == "table" and v == []):
            values.pop(key, None)
            continue
        if f["type"] == "table":
            values[key] = _clean_table(f, v, lookups, check_required)
        else:
            values[key] = clean_value(f, v, lookups=lookups)
    # Starting values set on fields this person can't change are still checked.
    for key, f in fields.items():
        if key in editable or key not in values or f["type"] in ("formula", "table", "file"):
            continue
        if existing and key in existing:
            continue
        try:
            values[key] = clean_value(f, values[key], lookups=lookups)
        except BadRequest:
            values.pop(key, None)
    values, visible = compute(definition, values, requester, today)
    if check_required:
        for key in editable:
            f = fields[key]
            if key in visible and f.get("required") and _empty(values.get(key)):
                raise BadRequest(f"{f['label']} is required.")
            if key in visible and f["type"] == "table" and values.get(key) is not None:
                _clean_table(f, values[key], None, True)
    return {k: v for k, v in values.items() if k in fields and not _empty(v)}


def strip_hidden(definition: dict, values: dict, roles: set[str], *, full: bool = False) -> dict:
    """Values a person may see."""
    out = {}
    for f in input_fields(definition):
        if f["key"] in values and access_for(f, roles, full=full) != "hidden":
            out[f["key"]] = values[f["key"]]
    return out


def unique_fields(definition: dict) -> list[dict]:
    return [f for f in input_fields(definition) if f.get("unique")]


def changed_labels(definition: dict, before: dict, after: dict) -> list[str]:
    out = []
    for f in input_fields(definition):
        if before.get(f["key"]) != after.get(f["key"]):
            out.append(f["label"])
    return out


def to_payload_value(f: dict, v):
    """A value as plain JSON for approval-flow conditions (numbers as numbers)."""
    if v is None:
        return None
    t = f["type"] if f["type"] != "formula" else f.get("result", "number")
    if t in NUMERIC or t == "number":
        try:
            d = Decimal(str(v))
            return int(d) if d == d.to_integral_value() else float(d)
        except (InvalidOperation, ValueError):
            return None
    if t == "table":
        return [dict(r) for r in v] if isinstance(v, list) else None
    if t == "file":
        return len(v) if isinstance(v, list) else 0
    return v
