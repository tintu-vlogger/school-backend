"""What can be imported and exported, and how each row is checked and saved.

Every entity lists its columns (with the names people commonly use, for auto-matching),
the keys an update can match on, and three steps:

    validate(ctx, row) -> (clean, errors)   checks one row without saving anything
    find(ctx, clean, key) -> existing | None
    apply(ctx, clean, existing, mode) -> "added" | "updated" | "skipped"

Saving always goes through the owning module's service functions, so the module's own
rules and the audit log apply exactly as when someone uses its screens.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.errors import BadRequest


@dataclass
class Col:
    key: str
    label: str
    required: bool = False
    type: str = "text"          # text | date | number | email | bool | datetime
    aliases: tuple = ()
    help: str = ""
    example: str = ""


@dataclass
class Entity:
    key: str
    label: str
    description: str
    columns: list[Col]
    match_keys: list[tuple[str, str]]
    modes: tuple = ("add", "update", "upsert")
    perm: str = "insights.data.import"
    prepare: Callable[[Session, int], dict] | None = None
    validate: Callable[[dict, dict], tuple[dict, list]] | None = None
    find: Callable[[dict, dict, str], Any] | None = None
    apply: Callable[[dict, dict, Any, str], str] | None = None
    export_rows: Callable[[Session, int], list[dict]] | None = None
    available: Callable[[], bool] = lambda: True
    note: str = ""

    def out(self) -> dict:
        return {"key": self.key, "label": self.label, "description": self.description, "note": self.note,
                "columns": [{"key": c.key, "label": c.label, "required": c.required, "type": c.type,
                             "help": c.help, "example": c.example} for c in self.columns],
                "match_keys": [{"value": k, "label": l} for k, l in self.match_keys], "modes": list(self.modes)}


# ── Value parsing ──────────────────────────────────────────────────

def norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def text_of(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    return s or None


def parse_date(v) -> date | None:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d %b %Y", "%d-%b-%Y", "%d %B %Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"“{s}” is not a date (use DD-MM-YYYY or YYYY-MM-DD)")


def parse_datetime(v) -> datetime | None:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M:%S",
                "%d/%m/%Y %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"“{s}” is not a date and time (use YYYY-MM-DD HH:MM)")


def parse_number(v) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v).replace(",", "").replace("₹", "").strip())
    except InvalidOperation:
        raise ValueError(f"“{v}” is not a number")


def parse_bool(v) -> bool | None:
    if v in (None, ""):
        return None
    s = str(v).strip().lower()
    if s in ("yes", "y", "true", "1", "active"):
        return True
    if s in ("no", "n", "false", "0", "inactive"):
        return False
    raise ValueError(f"“{v}” should be Yes or No")


def coerce(cols: list[Col], raw: dict) -> tuple[dict, list]:
    """Type-check the mapped values. Returns (values, [(column, message)])."""
    out, errors = {}, []
    for c in cols:
        v = raw.get(c.key)
        try:
            if c.type == "date":
                v = parse_date(v)
            elif c.type == "datetime":
                v = parse_datetime(v)
            elif c.type == "number":
                v = parse_number(v)
            elif c.type == "bool":
                v = parse_bool(v)
            else:
                v = text_of(v)
                if v and c.type == "email":
                    v = v.lower()
                    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
                        raise ValueError(f"“{v}” is not an email address")
        except ValueError as exc:
            errors.append((c.key, str(exc)))
            v = None
        if c.required and v in (None, ""):
            if not any(e[0] == c.key for e in errors):
                errors.append((c.key, f"{c.label} is required"))
        out[c.key] = v
    return out, errors


# ── Shared lookups ─────────────────────────────────────────────────

def _employees_index(db: Session, company_id: int) -> dict:
    from modules.employees.models import Employee
    rows = db.query(Employee).filter(Employee.company_id == company_id).all()
    idx = {}
    for e in rows:
        if e.employee_code:
            idx[("code", e.employee_code.lower())] = e
        idx[("email", e.email.lower())] = e
        if e.pan:
            idx[("pan", e.pan.upper())] = e
    return idx


def find_employee(ctx: dict, ref) -> Any:
    """An employee by ID or email (as typed in the file)."""
    ref = text_of(ref)
    if not ref:
        return None
    idx = ctx["emp"]
    return idx.get(("email", ref.lower())) if "@" in ref else idx.get(("code", ref.lower()))


def _by_name(rows, *attrs) -> dict:
    out = {}
    for r in rows:
        for a in attrs:
            v = getattr(r, a, None)
            if v:
                out[norm(v)] = r
    return out


def _masters(db: Session, company_id: int, type_key: str) -> dict:
    from core.masters import MasterType, MasterValue
    t = db.query(MasterType).filter(MasterType.company_id == company_id, MasterType.key == type_key).first()
    if t is None:
        return {}
    return _by_name(db.query(MasterValue).filter(MasterValue.type_id == t.id).all(), "label", "code")


def lookup(ctx: dict, table: str, value, label: str, errors: list, col: str):
    v = text_of(value)
    if not v:
        return None
    hit = ctx[table].get(norm(v))
    if hit is None:
        errors.append((col, f"{label} “{v}” was not found"))
        return None
    return hit.id


# ── Employees ──────────────────────────────────────────────────────

EMP_COLS = [
    Col("employee_code", "Employee ID", aliases=("emp id", "empid", "emp code", "employee number", "staff id"),
        example="NG0101"),
    Col("first_name", "First name", True, aliases=("firstname", "given name", "first"), example="Asha"),
    Col("last_name", "Last name", True, aliases=("lastname", "surname", "family name", "last"), example="Menon"),
    Col("email", "Work email", True, "email", aliases=("email", "email id", "official email", "mail"),
        example="asha.menon@example.com"),
    Col("phone", "Mobile", aliases=("mobile", "phone number", "mobile number", "contact"), example="9876543210"),
    Col("gender", "Gender", aliases=("sex",), example="Female"),
    Col("date_of_birth", "Date of birth", type="date", aliases=("dob", "birth date"), example="14-02-1994"),
    Col("date_of_joining", "Date of joining", type="date", aliases=("doj", "joining date", "joined on"),
        example="01-04-2024"),
    Col("department", "Department", aliases=("dept",), example="Finance"),
    Col("designation", "Designation", aliases=("title", "job title", "role"), example="Accountant"),
    Col("location", "Location", aliases=("office", "branch", "work location"), example="Bengaluru"),
    Col("legal_entity", "Legal entity", aliases=("company", "entity"), example=""),
    Col("grade", "Grade", aliases=("band", "level"), example=""),
    Col("employment_type", "Employment type", aliases=("type", "employee type"), example="Permanent"),
    Col("reporting_manager", "Reporting manager (ID or email)", aliases=("manager", "reports to", "manager id",
                                                                         "manager email"), example="NG0001"),
    Col("status", "Stage", aliases=("employee status",), help="Pre-joining, probation or confirmed",
        example="Confirmed"),
    Col("probation_end_date", "Probation ends", type="date", aliases=("probation end",), example=""),
    Col("work_mode", "Work mode", help="Office, remote, hybrid or field", example="Office"),
    Col("personal_email", "Personal email", type="email", aliases=("personal mail",),
        example=""),
    Col("pan", "PAN", aliases=("pan number", "pan no"), example="ABCPM1234K"),
    Col("uan", "UAN", aliases=("uan number", "pf uan"), example=""),
]
STATUSES = {"prejoining": "pre_joining", "probation": "probation", "confirmed": "confirmed", "active": "confirmed",
            "onprobation": "probation"}
WORK_MODES = {"office": "office", "remote": "remote", "hybrid": "hybrid", "field": "field", "wfh": "remote"}


def _emp_prepare(db: Session, company_id: int) -> dict:
    from modules.departments.models import Department
    from modules.designations.models import Designation
    from modules.organization.models import Grade, LegalEntity, Location
    return {
        "emp": _employees_index(db, company_id),
        "department": _by_name(db.query(Department).filter(Department.company_id == company_id).all(),
                               "department_name", "department_code"),
        "designation": _by_name(db.query(Designation).filter(Designation.company_id == company_id).all(),
                                "designation_name", "code"),
        "location": _by_name(db.query(Location).filter(Location.company_id == company_id).all(), "name", "code"),
        "legal_entity": _by_name(db.query(LegalEntity).filter(LegalEntity.company_id == company_id).all(),
                                 "name", "code", "legal_name"),
        "grade": _by_name(db.query(Grade).filter(Grade.company_id == company_id).all(), "name", "code"),
        "gender": _masters(db, company_id, "gender"),
        "employment_type": _masters(db, company_id, "employment_type"),
    }


def _emp_validate(ctx: dict, raw: dict) -> tuple[dict, list]:
    v, errors = coerce(EMP_COLS, raw)
    out = {k: v.get(k) for k in ("employee_code", "first_name", "last_name", "email", "phone", "date_of_birth",
                                 "date_of_joining", "probation_end_date", "personal_email")}
    if v.get("pan"):
        pan = v["pan"].upper().replace(" ", "")
        if not re.match(r"^[A-Z]{5}[0-9]{4}[A-Z]$", pan):
            errors.append(("pan", f"“{v['pan']}” is not a valid PAN"))
        out["pan"] = pan
    if v.get("uan"):
        uan = re.sub(r"\D", "", v["uan"])
        if len(uan) != 12:
            errors.append(("uan", "UAN has 12 digits"))
        out["uan"] = uan
    if v.get("phone"):
        ph = re.sub(r"[^\d+]", "", v["phone"])
        out["phone"] = ph
    for col, table, label in (("department", "department", "Department"), ("designation", "designation", "Designation"),
                              ("location", "location", "Location"), ("legal_entity", "legal_entity", "Legal entity"),
                              ("grade", "grade", "Grade")):
        out[f"{col}_id"] = lookup(ctx, table, v.get(col), label, errors, col)
    out["gender_id"] = lookup(ctx, "gender", v.get("gender"), "Gender", errors, "gender")
    out["employment_type_id"] = lookup(ctx, "employment_type", v.get("employment_type"), "Employment type", errors,
                                       "employment_type")
    if v.get("reporting_manager"):
        out["_manager_ref"] = v["reporting_manager"]
        if find_employee(ctx, v["reporting_manager"]) is None and v["reporting_manager"].lower() not in ctx.get(
                "_file_refs", set()):
            errors.append(("reporting_manager", f"Manager “{v['reporting_manager']}” was not found"))
    if v.get("status"):
        st = STATUSES.get(norm(v["status"]))
        if st is None:
            errors.append(("status", "Stage should be pre-joining, probation or confirmed"))
        out["status"] = st
    if v.get("work_mode"):
        wm = WORK_MODES.get(norm(v["work_mode"]))
        if wm is None:
            errors.append(("work_mode", "Work mode should be office, remote, hybrid or field"))
        out["work_mode"] = wm
    return {k: x for k, x in out.items() if x is not None}, errors


def _emp_find(ctx: dict, clean: dict, key: str):
    idx = ctx["emp"]
    if key == "email":
        return idx.get(("email", (clean.get("email") or "").lower()))
    if key == "pan":
        return idx.get(("pan", (clean.get("pan") or "").upper())) if clean.get("pan") else None
    return idx.get(("code", (clean.get("employee_code") or "").lower())) if clean.get("employee_code") else None


def _emp_apply(ctx: dict, clean: dict, existing, mode: str) -> str:
    from modules.employees import service as employees
    db, cid = ctx["db"], ctx["company_id"]
    data = {k: v for k, v in clean.items() if not k.startswith("_")}
    if clean.get("_manager_ref"):
        m = find_employee(ctx, clean["_manager_ref"])
        if m is None:
            raise BadRequest(f"Manager “{clean['_manager_ref']}” was not found")
        data["reporting_manager_id"] = m.id
    if existing is None:
        emp = employees.create(db, cid, data)
        result = "added"
    else:
        data.pop("status", None)   # stage changes go through the lifecycle screens
        if existing.email and data.get("email", "").lower() == existing.email.lower():
            data.pop("email")
        emp = employees.update(db, cid, existing, data)
        result = "updated"
    db.flush()
    idx = ctx["emp"]
    if emp.employee_code:
        idx[("code", emp.employee_code.lower())] = emp
    idx[("email", emp.email.lower())] = emp
    if emp.pan:
        idx[("pan", emp.pan.upper())] = emp
    return result


def _emp_export(db: Session, company_id: int) -> list[dict]:
    from core.masters import labels_for
    from modules.departments.models import Department
    from modules.designations.models import Designation
    from modules.employees.models import Employee
    from modules.organization.models import Grade, LegalEntity, Location
    emps = db.query(Employee).filter(Employee.company_id == company_id, Employee.archived_at.is_(None)).order_by(
        Employee.employee_code, Employee.first_name).all()

    def names(model, attr):
        return {r.id: getattr(r, attr) for r in db.query(model).filter(model.company_id == company_id).all()}
    dept, desg = names(Department, "department_name"), names(Designation, "designation_name")
    loc, le, gr = names(Location, "name"), names(LegalEntity, "name"), names(Grade, "name")
    masters = labels_for(db, company_id, {x for e in emps for x in (e.gender_id, e.employment_type_id) if x})
    by_id = {e.id: e for e in emps}
    rows = []
    for e in emps:
        m = by_id.get(e.reporting_manager_id)
        rows.append({"employee_code": e.employee_code, "first_name": e.first_name, "last_name": e.last_name,
                     "email": e.email, "phone": e.phone, "gender": masters.get(e.gender_id),
                     "date_of_birth": e.date_of_birth, "date_of_joining": e.date_of_joining,
                     "department": dept.get(e.department_id), "designation": desg.get(e.designation_id),
                     "location": loc.get(e.location_id), "legal_entity": le.get(e.legal_entity_id),
                     "grade": gr.get(e.grade_id), "employment_type": masters.get(e.employment_type_id),
                     "reporting_manager": (m.employee_code or m.email) if m else None,
                     "status": e.status.replace("_", "-") if e.status else None,
                     "probation_end_date": e.probation_end_date, "work_mode": e.work_mode,
                     "personal_email": e.personal_email, "pan": e.pan, "uan": e.uan})
    return rows


# ── Salaries ───────────────────────────────────────────────────────

SAL_COLS = [
    Col("employee", "Employee (ID or email)", True, aliases=("employee id", "emp id", "employee code", "email"),
        example="NG0101"),
    Col("effective_from", "Effective from", True, "date", aliases=("from", "effective date", "with effect from"),
        example="01-04-2026"),
    Col("annual_ctc", "Annual CTC", True, "number", aliases=("ctc", "annual salary", "yearly ctc"), example="720000"),
    Col("structure", "Salary structure", aliases=("structure name",), example=""),
    Col("reason", "Reason", help="first_salary, annual_raise, promotion … (from payroll settings)", example=""),
]


def _has_payroll() -> bool:
    try:
        import modules.payroll.service  # noqa: F401
        return True
    except Exception:
        return False


def _sal_prepare(db: Session, company_id: int) -> dict:
    from modules.payroll import structures
    return {"emp": _employees_index(db, company_id),
            "structure": _by_name(structures.structures(db, company_id), "name")}


def _sal_validate(ctx: dict, raw: dict) -> tuple[dict, list]:
    v, errors = coerce(SAL_COLS, raw)
    emp = find_employee(ctx, v.get("employee"))
    if v.get("employee") and emp is None:
        errors.append(("employee", f"Employee “{v['employee']}” was not found"))
    if v.get("annual_ctc") is not None and v["annual_ctc"] <= 0:
        errors.append(("annual_ctc", "Annual CTC must be more than zero"))
    st = None
    if v.get("structure"):
        st = ctx["structure"].get(norm(v["structure"]))
        if st is None:
            errors.append(("structure", f"Salary structure “{v['structure']}” was not found"))
    return {"_emp": emp, "effective_from": v.get("effective_from"), "annual_ctc": v.get("annual_ctc"),
            "structure_uuid": str(st.uuid) if st else None, "reason": v.get("reason")}, errors


def _sal_find(ctx: dict, clean: dict, key: str):
    from modules.payroll import service as payroll
    emp = clean.get("_emp")
    if emp is None:
        return None
    return next((s for s in payroll.salaries_of(ctx["db"], ctx["company_id"], emp.id)
                 if s.effective_from == clean.get("effective_from")), None)


def _sal_apply(ctx: dict, clean: dict, existing, mode: str) -> str:
    from modules.payroll import service as payroll
    data = {k: v for k, v in clean.items() if not k.startswith("_") and v is not None}
    payroll.assign_salary(ctx["db"], ctx["company_id"], clean["_emp"], data)
    return "updated" if existing else "added"


def _sal_export(db: Session, company_id: int) -> list[dict]:
    from modules.employees.models import Employee
    from modules.payroll.models import EmployeeSalary, SalaryStructure
    rows = (db.query(EmployeeSalary, Employee, SalaryStructure.name)
            .join(Employee, Employee.id == EmployeeSalary.employee_id)
            .outerjoin(SalaryStructure, SalaryStructure.id == EmployeeSalary.structure_id)
            .filter(EmployeeSalary.company_id == company_id, EmployeeSalary.status == "active")
            .order_by(Employee.employee_code).all())
    return [{"employee": e.employee_code or e.email, "effective_from": s.effective_from, "annual_ctc": s.annual_ctc,
             "structure": n, "reason": s.reason} for s, e, n in rows]


# ── Leave balances ─────────────────────────────────────────────────

LB_COLS = [
    Col("employee", "Employee (ID or email)", True, aliases=("employee id", "emp id", "employee code", "email"),
        example="NG0101"),
    Col("leave_type", "Leave type (code or name)", True, aliases=("leave", "type", "leave code"), example="EL"),
    Col("days", "Days", True, "number", aliases=("balance", "opening balance", "leave balance"), example="12"),
    Col("note", "Note", aliases=("remarks", "reason"), example="Opening balance from the old system"),
]


def _lb_prepare(db: Session, company_id: int) -> dict:
    from modules.leave import policy
    types = policy.list_types(db, company_id)
    return {"emp": _employees_index(db, company_id), "leave_type": _by_name(types, "code", "name")}


def _lb_validate(ctx: dict, raw: dict) -> tuple[dict, list]:
    v, errors = coerce(LB_COLS, raw)
    emp = find_employee(ctx, v.get("employee"))
    if v.get("employee") and emp is None:
        errors.append(("employee", f"Employee “{v['employee']}” was not found"))
    t = ctx["leave_type"].get(norm(v.get("leave_type"))) if v.get("leave_type") else None
    if v.get("leave_type") and t is None:
        errors.append(("leave_type", f"Leave type “{v['leave_type']}” was not found"))
    if v.get("days") is not None and abs(v["days"]) > 1000:
        errors.append(("days", "That is too many days"))
    return {"_emp": emp, "_type": t, "days": v.get("days"), "note": v.get("note")}, errors


def _current_balance(ctx: dict, emp, t) -> tuple[Decimal, int]:
    from core.timeutil import today_for_company_id
    from modules.leave import ledger, policy
    today = today_for_company_id(ctx["db"], ctx["company_id"])
    year = policy.year_bounds(policy.rules_on(t, today), today)[0]
    return ledger.balance(ctx["db"], ctx["company_id"], emp.id, t, year), year


def _lb_find(ctx: dict, clean: dict, key: str):
    if clean.get("_emp") is None or clean.get("_type") is None:
        return None
    return ("balance", clean["_emp"].id, clean["_type"].id)


def _lb_apply(ctx: dict, clean: dict, existing, mode: str) -> str:
    """add: add the days to the balance. update / upsert: make the balance equal to the days."""
    from modules.leave import ledger
    emp, t, days = clean["_emp"], clean["_type"], Decimal(clean["days"])
    note = clean.get("note") or "Imported balance"
    if mode == "add":
        ledger.adjust(ctx["db"], ctx["company_id"], emp, t, days, note)
        return "added"
    current, year = _current_balance(ctx, emp, t)
    diff = days - Decimal(current)
    if diff == 0:
        return "skipped"
    ledger.adjust(ctx["db"], ctx["company_id"], emp, t, diff, f"{note} (set to {days})", year=year)
    return "updated"


def _lb_export(db: Session, company_id: int) -> list[dict]:
    from modules.leave import reports
    out = reports.balance_report(db, company_id, None)
    return [{"employee": r.get("employee_code") or r.get("employee_name"), "leave_type": r["leave_type"],
             "days": r["balance"], "note": None} for r in out["rows"]]


# ── Attendance punches ─────────────────────────────────────────────

PUNCH_COLS = [
    Col("employee", "Employee ID", True, aliases=("employee id", "emp id", "employee code", "user id"),
        example="NG0101"),
    Col("timestamp", "Date and time", True, "datetime", aliases=("punch time", "datetime", "time", "log time"),
        example="2026-11-02 09:12"),
    Col("direction", "In or out", aliases=("type", "in out", "inout", "status"), example="in"),
]


def _punch_prepare(db: Session, company_id: int) -> dict:
    return {"emp": _employees_index(db, company_id), "batch": []}


def _punch_validate(ctx: dict, raw: dict) -> tuple[dict, list]:
    v, errors = coerce(PUNCH_COLS, raw)
    emp = find_employee(ctx, v.get("employee"))
    if v.get("employee") and emp is None:
        errors.append(("employee", f"Employee “{v['employee']}” was not found"))
    if emp is not None and not emp.employee_code:
        errors.append(("employee", "This employee has no employee ID, which punches are matched on"))
    d = (v.get("direction") or "").lower()
    if d and d not in ("in", "out", "i", "o", "0", "1", "checkin", "checkout", "check in", "check out"):
        errors.append(("direction", "Write in or out"))
    return {"_emp": emp, "timestamp": v.get("timestamp"), "direction": v.get("direction")}, errors


def _punch_apply(ctx: dict, clean: dict, existing, mode: str) -> str:
    from modules.attendance import devices
    log = devices.ingest(ctx["db"], ctx["company_id"], None,
                         [{"device_user_id": clean["_emp"].employee_code, "timestamp": clean["timestamp"],
                           "direction": clean.get("direction")}], "file", ctx.get("file_name"),
                         match_by="employee_code")
    if log.inserted:
        return "added"
    if log.duplicates:
        return "skipped"
    raise BadRequest((log.errors or [{}])[0].get("message") or "The punch could not be saved")


# ── Holidays ───────────────────────────────────────────────────────

HOL_COLS = [
    Col("calendar", "Holiday calendar", aliases=("calendar name",), help="Blank means the default calendar",
        example="All India"),
    Col("name", "Holiday", True, aliases=("holiday name", "occasion", "festival"), example="Diwali"),
    Col("date", "Date", True, "date", aliases=("holiday date", "on"), example="08-11-2026"),
    Col("kind", "Kind", aliases=("type",), help="national, festival or optional", example="festival"),
    Col("description", "Description", aliases=("notes",), example=""),
]


def _hol_prepare(db: Session, company_id: int) -> dict:
    from modules.leave import holidays
    cals = holidays.list_calendars(db, company_id)
    return {"calendar": _by_name(cals, "name"), "default_calendar": next((c for c in cals if c.is_default), None)}


def _hol_validate(ctx: dict, raw: dict) -> tuple[dict, list]:
    v, errors = coerce(HOL_COLS, raw)
    cal = ctx["calendar"].get(norm(v["calendar"])) if v.get("calendar") else ctx["default_calendar"]
    if cal is None:
        errors.append(("calendar", f"Calendar “{v.get('calendar') or 'default'}” was not found"))
    kind = (v.get("kind") or "festival").lower()
    if kind not in ("national", "festival", "optional"):
        errors.append(("kind", "Kind should be national, festival or optional"))
    return {"_cal": cal, "name": v.get("name"), "date": v.get("date"), "kind": kind,
            "description": v.get("description")}, errors


def _hol_find(ctx: dict, clean: dict, key: str):
    from modules.leave.models import Holiday
    cal = clean.get("_cal")
    if cal is None or clean.get("date") is None:
        return None
    q = ctx["db"].query(Holiday).filter(Holiday.company_id == ctx["company_id"], Holiday.calendar_id == cal.id,
                                        Holiday.date == clean["date"])
    if key == "calendar_date_name":
        q = q.filter(func.lower(Holiday.name) == (clean.get("name") or "").lower())
    return q.first()


def _hol_apply(ctx: dict, clean: dict, existing, mode: str) -> str:
    from modules.leave import holidays
    data = {"name": clean["name"], "date": clean["date"].isoformat(), "kind": clean["kind"],
            "description": clean.get("description")}
    if existing is not None:
        holidays.update_holiday(ctx["db"], ctx["company_id"], existing, data)
        return "updated"
    made = holidays.add_holiday(ctx["db"], ctx["company_id"], [clean["_cal"]], data)
    return "added" if made else "skipped"


def _hol_export(db: Session, company_id: int) -> list[dict]:
    from modules.leave.models import Holiday, HolidayCalendar
    rows = (db.query(Holiday, HolidayCalendar.name).join(HolidayCalendar, HolidayCalendar.id == Holiday.calendar_id)
            .filter(Holiday.company_id == company_id).order_by(HolidayCalendar.name, Holiday.date).all())
    return [{"calendar": c, "name": h.name, "date": h.date, "kind": h.kind, "description": h.description}
            for h, c in rows]


# ── Assets ─────────────────────────────────────────────────────────

ASSET_COLS = [
    Col("asset_tag", "Asset tag", aliases=("tag", "asset id", "asset code"), help="Blank to number automatically",
        example="AST-0101"),
    Col("name", "Asset name", True, aliases=("asset", "item", "description"), example="Dell Latitude 5440"),
    Col("category", "Category", aliases=("asset category", "type"), example="Laptop"),
    Col("brand", "Brand", aliases=("make", "manufacturer"), example="Dell"),
    Col("model", "Model", example="Latitude 5440"),
    Col("serial_number", "Serial number", aliases=("serial", "serial no", "s/n"), example="5CD1234XYZ"),
    Col("purchase_date", "Bought on", type="date", aliases=("purchase date", "bought"),
        example="15-06-2025"),
    Col("purchase_value", "Cost", type="number", aliases=("purchase value", "price", "value", "amount"),
        example="78000"),
    Col("vendor", "Vendor", aliases=("supplier",), example=""),
    Col("invoice_no", "Invoice number", aliases=("invoice", "bill no"), example=""),
    Col("warranty_until", "Warranty until", type="date", aliases=("warranty", "warranty end"), example=""),
    Col("condition", "Condition", help="new, good, fair, poor or damaged", example="good"),
    Col("notes", "Notes", example=""),
]


def _asset_prepare(db: Session, company_id: int) -> dict:
    from modules.assets.models import Asset
    rows = db.query(Asset).filter(Asset.company_id == company_id).all()
    return {"asset_tag": {norm(a.asset_tag): a for a in rows if a.asset_tag},
            "serial_number": {norm(a.serial_number): a for a in rows if a.serial_number},
            "category": _masters(db, company_id, "asset_category")}


def _asset_validate(ctx: dict, raw: dict) -> tuple[dict, list]:
    v, errors = coerce(ASSET_COLS, raw)
    out = {k: v.get(k) for k in ("asset_tag", "name", "brand", "model", "serial_number", "purchase_date",
                                 "purchase_value", "vendor", "invoice_no", "warranty_until", "notes")}
    out["category_id"] = lookup(ctx, "category", v.get("category"), "Category", errors, "category")
    if v.get("condition"):
        out["condition"] = v["condition"].lower()
    return {k: x for k, x in out.items() if x is not None}, errors


def _asset_find(ctx: dict, clean: dict, key: str):
    val = clean.get(key)
    return ctx[key].get(norm(val)) if val else None


def _asset_apply(ctx: dict, clean: dict, existing, mode: str) -> str:
    from modules.assets import service as assets
    a = assets.save(ctx["db"], ctx["company_id"], dict(clean), existing)
    ctx["asset_tag"][norm(a.asset_tag)] = a
    if a.serial_number:
        ctx["serial_number"][norm(a.serial_number)] = a
    return "updated" if existing else "added"


def _asset_export(db: Session, company_id: int) -> list[dict]:
    from core.masters import labels_for
    from modules.assets.models import Asset
    rows = db.query(Asset).filter(Asset.company_id == company_id).order_by(Asset.asset_tag).all()
    cats = labels_for(db, company_id, {a.category_id for a in rows if a.category_id})
    return [{"asset_tag": a.asset_tag, "name": a.name, "category": cats.get(a.category_id), "brand": a.brand,
             "model": a.model, "serial_number": a.serial_number, "purchase_date": a.purchase_date,
             "purchase_value": a.purchase_value, "vendor": a.vendor, "invoice_no": a.invoice_no,
             "warranty_until": a.warranty_until, "condition": a.condition, "notes": a.notes} for a in rows]


# ── Masters (dropdown lists), departments and designations ─────────

MASTER_COLS = [
    Col("list", "List", True, aliases=("master", "list name", "type"), help="The list's name or key",
        example="Asset category"),
    Col("label", "Value", True, aliases=("name", "value name"), example="Tablet"),
    Col("code", "Code", example="tablet"),
    Col("parent", "Under (parent value)", aliases=("parent value",), example=""),
    Col("active", "Active", type="bool", example="Yes"),
]


def _master_prepare(db: Session, company_id: int) -> dict:
    from core.masters import MasterType, MasterValue
    types = db.query(MasterType).filter(MasterType.company_id == company_id).all()
    vals: dict[int, dict] = {}
    for mv in db.query(MasterValue).filter(MasterValue.company_id == company_id).all():
        d = vals.setdefault(mv.type_id, {})
        d[("label", norm(mv.label))] = mv
        d[("code", norm(mv.code))] = mv
    return {"types": _by_name(types, "name", "key"), "values": vals}


def _master_validate(ctx: dict, raw: dict) -> tuple[dict, list]:
    v, errors = coerce(MASTER_COLS, raw)
    t = ctx["types"].get(norm(v.get("list"))) if v.get("list") else None
    if v.get("list") and t is None:
        errors.append(("list", f"List “{v['list']}” was not found"))
    parent = None
    if t is not None and v.get("parent"):
        parent = ctx["values"].get(t.id, {}).get(("label", norm(v["parent"])))
        if parent is None:
            errors.append(("parent", f"“{v['parent']}” is not in this list"))
    return {"_type": t, "_parent": parent, "label": v.get("label"), "code": v.get("code"),
            "active": v.get("active")}, errors


def _master_find(ctx: dict, clean: dict, key: str):
    t = clean.get("_type")
    if t is None:
        return None
    vals = ctx["values"].get(t.id, {})
    if key == "list_code":
        return vals.get(("code", norm(clean.get("code")))) if clean.get("code") else None
    return vals.get(("label", norm(clean.get("label"))))


def _master_apply(ctx: dict, clean: dict, existing, mode: str) -> str:
    from core import masters
    db, cid, t = ctx["db"], ctx["company_id"], clean["_type"]
    data = {"label": clean["label"], "parent_id": clean["_parent"].id if clean.get("_parent") else None}
    if clean.get("active") is not None:
        data["active"] = clean["active"]
    if existing is None:
        if clean.get("code"):
            data["code"] = clean["code"]
        mv = masters.create_value(db, cid, t, data)
        result = "added"
    else:
        if clean.get("code") and not existing.is_system:
            data["code"] = clean["code"]
        mv = masters.update_value(db, cid, existing, data)
        result = "updated"
    db.flush()
    d = ctx["values"].setdefault(t.id, {})
    d[("label", norm(mv.label))] = mv
    d[("code", norm(mv.code))] = mv
    return result


def _master_export(db: Session, company_id: int) -> list[dict]:
    from core.masters import MasterType, MasterValue
    types = {t.id: t for t in db.query(MasterType).filter(MasterType.company_id == company_id).all()}
    vals = db.query(MasterValue).filter(MasterValue.company_id == company_id).order_by(
        MasterValue.type_id, MasterValue.sort_order).all()
    by_id = {v.id: v for v in vals}
    return [{"list": types[v.type_id].name if v.type_id in types else "", "label": v.label, "code": v.code,
             "parent": by_id[v.parent_id].label if v.parent_id in by_id else None,
             "active": "Yes" if v.active else "No"} for v in vals]


DEPT_COLS = [
    Col("name", "Department", True, aliases=("department", "department name", "dept"), example="Finance"),
    Col("code", "Code", aliases=("department code",), example="FIN"),
    Col("parent", "Under (parent department)", aliases=("parent",), example=""),
    Col("description", "Description", example=""),
]


def _dept_prepare(db: Session, company_id: int) -> dict:
    from modules.departments.models import Department
    rows = db.query(Department).filter(Department.company_id == company_id).all()
    return {"name": _by_name(rows, "department_name"), "code": _by_name(rows, "department_code")}


def _dept_validate(ctx: dict, raw: dict) -> tuple[dict, list]:
    v, errors = coerce(DEPT_COLS, raw)
    parent = None
    if v.get("parent"):
        parent = ctx["name"].get(norm(v["parent"]))
        if parent is None and norm(v["parent"]) not in ctx.get("_file_names", set()):
            errors.append(("parent", f"Department “{v['parent']}” was not found"))
    return {"name": v.get("name"), "code": v.get("code"), "_parent_ref": v.get("parent"),
            "description": v.get("description")}, errors


def _dept_find(ctx: dict, clean: dict, key: str):
    return ctx["code" if key == "code" else "name"].get(norm(clean.get(key if key == "code" else "name")))


def _dept_apply(ctx: dict, clean: dict, existing, mode: str) -> str:
    from modules.departments.models import Department
    db = ctx["db"]
    parent = ctx["name"].get(norm(clean.get("_parent_ref"))) if clean.get("_parent_ref") else None
    if existing is None:
        existing = Department(company_id=ctx["company_id"], department_name=clean["name"][:200], active=True)
        db.add(existing)
        result = "added"
    else:
        existing.department_name = clean["name"][:200]
        result = "updated"
    if clean.get("code"):
        existing.department_code = clean["code"][:20]
    if clean.get("description"):
        existing.description = clean["description"]
    if parent is not None and parent is not existing:
        existing.parent_id = parent.id
    db.flush()
    ctx["name"][norm(existing.department_name)] = existing
    if existing.department_code:
        ctx["code"][norm(existing.department_code)] = existing
    return result


def _dept_export(db: Session, company_id: int) -> list[dict]:
    from modules.departments.models import Department
    rows = db.query(Department).filter(Department.company_id == company_id).order_by(Department.department_name).all()
    by_id = {d.id: d for d in rows}
    return [{"name": d.department_name, "code": d.department_code,
             "parent": by_id[d.parent_id].department_name if d.parent_id in by_id else None,
             "description": d.description} for d in rows]


DESG_COLS = [
    Col("name", "Designation", True, aliases=("designation", "title", "job title"), example="Senior accountant"),
    Col("code", "Code", example="SACC"),
    Col("grade", "Grade", aliases=("band", "level"), example=""),
    Col("description", "Description", example=""),
]


def _desg_prepare(db: Session, company_id: int) -> dict:
    from modules.designations.models import Designation
    from modules.organization.models import Grade
    rows = db.query(Designation).filter(Designation.company_id == company_id).all()
    return {"name": _by_name(rows, "designation_name"), "code": _by_name(rows, "code"),
            "grade": _by_name(db.query(Grade).filter(Grade.company_id == company_id).all(), "name", "code")}


def _desg_validate(ctx: dict, raw: dict) -> tuple[dict, list]:
    v, errors = coerce(DESG_COLS, raw)
    grade_id = lookup(ctx, "grade", v.get("grade"), "Grade", errors, "grade")
    return {"name": v.get("name"), "code": v.get("code"), "grade_id": grade_id,
            "description": v.get("description")}, errors


def _desg_apply(ctx: dict, clean: dict, existing, mode: str) -> str:
    from modules.designations.models import Designation
    db = ctx["db"]
    if existing is None:
        existing = Designation(company_id=ctx["company_id"], designation_name=clean["name"][:200])
        db.add(existing)
        result = "added"
    else:
        existing.designation_name = clean["name"][:200]
        result = "updated"
    for k in ("code", "grade_id", "description"):
        if clean.get(k) is not None:
            setattr(existing, k, clean[k])
    db.flush()
    ctx["name"][norm(existing.designation_name)] = existing
    if existing.code:
        ctx["code"][norm(existing.code)] = existing
    return result


def _desg_export(db: Session, company_id: int) -> list[dict]:
    from modules.designations.models import Designation
    from modules.organization.models import Grade
    grades = {g.id: g.name for g in db.query(Grade).filter(Grade.company_id == company_id).all()}
    rows = db.query(Designation).filter(Designation.company_id == company_id).order_by(
        Designation.designation_name).all()
    return [{"name": d.designation_name, "code": d.code, "grade": grades.get(d.grade_id),
             "description": d.description} for d in rows]


ENTITIES: dict[str, Entity] = {e.key: e for e in [
    Entity("employees", "Employees", "Add new people or update existing records.", EMP_COLS,
           [("employee_code", "Employee ID"), ("email", "Work email"), ("pan", "PAN")],
           prepare=_emp_prepare, validate=_emp_validate, find=_emp_find, apply=_emp_apply, export_rows=_emp_export,
           note="New people get a random password and can use “Forgot password” to sign in. Stage changes for "
                "existing people (confirmation, exit) are made on their profile, not by import."),
    Entity("salaries", "Salaries", "Assign or revise annual CTC from a date.", SAL_COLS,
           [("employee_effective", "Employee and effective date")], modes=("add", "upsert"),
           prepare=_sal_prepare, validate=_sal_validate, find=_sal_find, apply=_sal_apply, export_rows=_sal_export,
           available=_has_payroll,
           note="Each row is a salary from a date; the earlier salary ends the day before. A row with the same "
                "date as an existing salary replaces it."),
    Entity("leave_balances", "Leave balances", "Opening balances or corrections, per person and leave type.",
           LB_COLS, [("employee_type", "Employee and leave type")], modes=("add", "update"),
           prepare=_lb_prepare, validate=_lb_validate, find=_lb_find, apply=_lb_apply, export_rows=_lb_export,
           note="Add: the days are added to the balance. Update: the balance is set to the days in the file. "
                "Every change is kept in the leave ledger with its note."),
    Entity("attendance_punches", "Attendance punches", "Check-in and check-out times from a machine or old system.",
           PUNCH_COLS, [], modes=("add",), prepare=_punch_prepare, validate=_punch_validate,
           find=lambda ctx, clean, key: None, apply=_punch_apply,
           note="Punches already on record are skipped. Attendance is worked out again for the days imported."),
    Entity("holidays", "Holidays", "Holidays for a holiday calendar.", HOL_COLS,
           [("calendar_date_name", "Calendar, date and name"), ("calendar_date", "Calendar and date")],
           prepare=_hol_prepare, validate=_hol_validate, find=_hol_find, apply=_hol_apply, export_rows=_hol_export),
    Entity("assets", "Assets", "The asset register.", ASSET_COLS,
           [("asset_tag", "Asset tag"), ("serial_number", "Serial number")],
           prepare=_asset_prepare, validate=_asset_validate, find=_asset_find, apply=_asset_apply,
           export_rows=_asset_export),
    Entity("masters", "Dropdown lists", "Values for lists such as asset category, document type or bank.",
           MASTER_COLS, [("list_label", "List and value"), ("list_code", "List and code")],
           prepare=_master_prepare, validate=_master_validate, find=_master_find, apply=_master_apply,
           export_rows=_master_export),
    Entity("departments", "Departments", "Departments and sub-departments.", DEPT_COLS,
           [("name", "Name"), ("code", "Code")], prepare=_dept_prepare, validate=_dept_validate, find=_dept_find,
           apply=_dept_apply, export_rows=_dept_export),
    Entity("designations", "Designations", "Job titles with their grade.", DESG_COLS,
           [("name", "Name"), ("code", "Code")], prepare=_desg_prepare, validate=_desg_validate,
           find=lambda ctx, clean, key: ctx["code" if key == "code" else "name"].get(
               norm(clean.get("code" if key == "code" else "name"))),
           apply=_desg_apply, export_rows=_desg_export),
]}


def get(key: str) -> Entity:
    e = ENTITIES.get(key)
    if e is None or not e.available():
        raise BadRequest("Choose what to import.")
    return e


def available() -> list[Entity]:
    return [e for e in ENTITIES.values() if e.available()]


def auto_map(entity: Entity, headers: list[str]) -> dict:
    """Match file headers to columns by key, label or common names."""
    out, used = {}, set()
    normed = [norm(h) for h in headers]
    for c in entity.columns:
        names = {norm(c.key), norm(c.label), *(norm(a) for a in c.aliases)}
        for i, h in enumerate(normed):
            if h and h in names and i not in used:
                out[c.key] = headers[i]
                used.add(i)
                break
    return out


def utc_now():
    return datetime.now(timezone.utc)

