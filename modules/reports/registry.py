"""Report data sources and who may see which rows.

A *source* is one kind of record a report can be built on (employees, attendance days,
leave requests, payslips …). It lists its fields (key, label, type), how to fetch rows and
which column says whose row it is (the employee), so row-level access can be applied.

Two kinds of source:

* query sources describe a SQL select (tables and joins) and a column expression per
  field. Filters on plain fields run in SQL; computed fields run in Python.
* function sources call a module's public report function (leave balances, muster roll,
  attrition …) and return ready rows. They may take parameters (a period, a year).

Sources live in `modules/reports/sources/` (one file per business area) and register
themselves with `register(...)`. A module that wants to add its own can ship
`modules/<name>/report_sources.py`; `load()` imports those too.

Row-level access (scope), from narrow to wide:

    self      only the person's own rows
    direct    their direct reports
    team      all their reports (direct and indirect)
    all       everyone in the company

A person's widest scope for a source comes from the existing permissions: the company
owner and anyone holding one of the source's `all_perms` get `all`; someone with reports
(reporting line) gets `team` when the source allows managers; everyone else gets `self`
when the source allows it. A report can narrow this further but never widen it.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi import Depends
from sqlalchemy.orm import Session

from core.db import get_db
from core.security import _get_role_permissions, get_current_user

SCOPES = ("self", "direct", "team", "all")
SCOPE_LABELS = {"self": "My data", "direct": "Me and my direct reports", "team": "Me and all my reports",
                "all": "Everyone"}
TYPES = ("text", "number", "money", "date", "datetime", "bool", "enum", "hours", "percent")
NUMERIC = ("number", "money", "hours", "percent")


@dataclass
class Field:
    key: str
    label: str
    type: str = "text"
    expr: Any = None                      # SQL column expression (query sources)
    compute: Callable[[dict], Any] | None = None   # derived from the row in Python
    needs: tuple = ()                     # hidden keys the compute needs (fetched, not shown)
    sensitive: str | None = None          # permission needed to see the value of other people
    options: list | None = None           # enum choices [(value, label)]
    default: bool = False                 # in the default column set
    hidden: bool = False                  # helper column, never offered
    agg: str | None = None                # default aggregate for numeric fields
    description: str | None = None

    @property
    def numeric(self) -> bool:
        return self.type in NUMERIC

    def out(self) -> dict:
        return {"key": self.key, "label": self.label, "type": self.type, "sensitive": bool(self.sensitive),
                "options": [{"value": v, "label": l} for v, l in (self.options or [])] or None,
                "default": self.default, "agg": self.agg or ("sum" if self.numeric else None),
                "description": self.description}


@dataclass
class Param:
    key: str
    label: str
    type: str = "date"                    # date | month | year | number | text
    default: str | None = None            # relative token such as @month_start
    required: bool = True


@dataclass
class Source:
    key: str
    label: str
    category: str                         # people | time | pay | talent | engage | workplace
    description: str = ""
    fields: list[Field] = field(default_factory=list)
    # query sources
    select_from: Callable[[], Any] | None = None   # returns a FROM clause (table with joins)
    company_col: Any = None
    employee_col: Any = None              # whose row it is; None means company-level data
    base_where: Callable[[], list] | None = None
    # function sources: fetch(db, company_id, ids or None, params) -> list[dict]
    fetch: Callable[..., list[dict]] | None = None
    dynamic_fields: Callable[..., list[Field]] | None = None   # extra columns that depend on params
    params: list[Param] = field(default_factory=list)
    per_employee: bool = False            # function source rows carry employee_id (scope applies)
    # access
    all_perms: tuple = ()                 # any of these gives company-wide rows
    allow_team: bool = True               # managers see their reports' rows
    allow_self: bool = True               # everyone sees their own rows
    sensitive: bool = False               # whole source is sensitive (exports may be protected)
    available: Callable[[], bool] = lambda: True
    date_field: str | None = None         # the main date, used for quick date filters

    def field_map(self) -> dict[str, Field]:
        return {f.key: f for f in self.fields}

    def out(self, include_fields: bool = True) -> dict:
        d = {"key": self.key, "label": self.label, "category": self.category, "description": self.description,
             "kind": "query" if self.select_from else "function", "sensitive": self.sensitive,
             "company_level": self.company_level,
             "date_field": self.date_field,
             "params": [{"key": p.key, "label": p.label, "type": p.type, "default": p.default, "required": p.required}
                        for p in self.params]}
        if include_fields:
            d["fields"] = [f.out() for f in self.fields if not f.hidden]
        return d

    @property
    def fetch_has_employee(self) -> bool:
        return self.fetch is not None and self.per_employee

    @property
    def company_level(self) -> bool:
        return self.employee_col is None and not self.per_employee


_SOURCES: dict[str, Source] = {}
_loaded = False
# Sources a company defines at run time (e.g. custom services): fn() -> list[Source] for the
# company in the current request context.
_PROVIDERS: list[Callable[[], list[Source]]] = []

CATEGORIES = {"people": "People", "time": "Time and leave", "pay": "Pay", "talent": "Talent",
              "engage": "Engagement", "workplace": "Workplace"}


def register(source: Source) -> Source:
    _SOURCES[source.key] = source
    return source


def add_provider(fn: Callable[[], list[Source]]) -> None:
    """Add sources that belong to the company in the request context (see _PROVIDERS)."""
    _PROVIDERS.append(fn)


def _provided() -> list[Source]:
    out: list[Source] = []
    for fn in _PROVIDERS:
        out.extend(fn() or [])
    return out


def load() -> None:
    """Import every source file once (ours and any module's report_sources.py)."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    from modules.reports.sources import ALL as ours
    for name in ours:
        importlib.import_module(f"modules.reports.sources.{name}")
    from modules import module_names
    for name in module_names():
        try:
            importlib.import_module(f"modules.{name}.report_sources")
        except ModuleNotFoundError as exc:
            if exc.name != f"modules.{name}.report_sources":
                raise


def get(key: str) -> Source | None:
    load()
    s = _SOURCES.get(key)
    if s is None:
        s = next((x for x in _provided() if x.key == key), None)
    if s is None or not s.available():
        return None
    return s


def all_sources() -> list[Source]:
    load()
    return [s for s in [*_SOURCES.values(), *_provided()] if s.available()]


# ── Who is asking ──────────────────────────────────────────────────

@dataclass
class Principal:
    company_id: int
    employee_id: int | None
    role: str
    name: str = ""
    perms: set = field(default_factory=set)
    via: str = "web"                       # web | schedule | assistant | mcp

    @property
    def is_owner(self) -> bool:
        return self.employee_id is None

    def can(self, perm: str) -> bool:
        return self.is_owner or self.role == "admin" or perm in self.perms

    def can_any(self, perms) -> bool:
        return any(self.can(p) for p in perms)

    def as_user(self) -> dict:
        return {"company_id": self.company_id, "employee_id": self.employee_id, "role": self.role}


def principal_for(db: Session, user: dict, via: str = "web") -> Principal:
    from core import context
    perms = set(_get_role_permissions(user["company_id"], user["role"], db))
    return Principal(company_id=user["company_id"], employee_id=user.get("employee_id"), role=user["role"],
                     name=context.current().actor_name or user.get("email") or "", perms=perms, via=via)


def principal_for_employee(db: Session, company_id: int, employee_id: int | None, via: str) -> Principal:
    """A principal for background work done on someone's behalf (schedules, tokens)."""
    from modules.employees.models import Employee
    if employee_id is None:
        return Principal(company_id=company_id, employee_id=None, role="admin", name="Company owner", via=via,
                         perms=set(_get_role_permissions(company_id, "admin", db)))
    e = db.query(Employee).filter(Employee.company_id == company_id, Employee.id == employee_id).first()
    if e is None or not e.active:
        raise PermissionError("This person no longer has access.")
    return Principal(company_id=company_id, employee_id=e.id, role=e.role, via=via,
                     name=f"{e.first_name} {e.last_name}".strip(),
                     perms=set(_get_role_permissions(company_id, e.role, db)))


def get_principal(user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> Principal:
    return principal_for(db, user)


# ── Row-level access ───────────────────────────────────────────────

def reports_of(db: Session, company_id: int, manager_id: int | None, deep: bool) -> set[int]:
    """Employee ids below a manager in the reporting line (active people)."""
    if not manager_id:
        return set()
    from modules.employees.models import Employee
    rows = db.query(Employee.id, Employee.reporting_manager_id).filter(
        Employee.company_id == company_id, Employee.reporting_manager_id.isnot(None)).all()
    children: dict[int, list[int]] = {}
    for eid, mid in rows:
        children.setdefault(mid, []).append(eid)
    out: set[int] = set()
    frontier = [manager_id]
    while frontier:
        nxt = []
        for m in frontier:
            for c in children.get(m, []):
                if c not in out and c != manager_id:
                    out.add(c)
                    nxt.append(c)
        frontier = nxt if deep else []
    return out


def max_scope(db: Session, p: Principal, source: Source) -> str | None:
    """The widest scope this person has on a source, or None for no access."""
    if p.is_owner or p.role == "admin" or (source.all_perms and p.can_any(source.all_perms)):
        return "all"
    if source.company_level:
        return None   # company-level data needs a company-wide permission
    if source.allow_team and reports_of(db, p.company_id, p.employee_id, deep=False):
        return "team"
    if source.allow_self and p.employee_id:
        return "self"
    return None


def narrow(a: str | None, b: str | None) -> str | None:
    """The narrower of two scopes (None = no access; 'auto' = no limit)."""
    if a is None:
        return None
    if b in (None, "auto", ""):
        return a
    return SCOPES[min(SCOPES.index(a), SCOPES.index(b))] if b in SCOPES else a


def scope_ids(db: Session, p: Principal, scope: str) -> set[int] | None:
    """Employee ids visible at a scope; None means everyone."""
    if scope == "all":
        return None
    if scope == "self":
        return {p.employee_id} if p.employee_id else set()
    ids = reports_of(db, p.company_id, p.employee_id, deep=(scope == "team"))
    if p.employee_id:
        ids.add(p.employee_id)      # a manager's team views include their own row
    return ids


def allowed_scopes(db: Session, p: Principal, source: Source) -> list[dict]:
    top = max_scope(db, p, source)
    if top is None:
        return []
    out = SCOPES[: SCOPES.index(top) + 1]
    if top == "all" and not p.employee_id:
        out = ("all",)
    return [{"value": s, "label": SCOPE_LABELS[s]} for s in out]
