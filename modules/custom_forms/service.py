"""Custom services: business rules for services, form versions, records, approval,
statuses, comments, saved views, exports and the menu.

Who may do what
- Design services (create, change the form, install templates): `settings.services.manage`.
- Handle a service's records (see all, edit allowed fields, move statuses, export): the
  company owner, HR admins, holders of `people.services.records`, the service's manager
  roles and the people named as its managers.
- Submit: everyone, or the roles / teams / departments / locations the service names.
- See one record: its requester, the service's handlers and anyone with an approval task on it.
Field access (hidden / view / edit) is set per role on each field (see fields.py).
"""
from __future__ import annotations

import csv
import io
import re
import time as _time
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from core import audit, context, numbering
from core.errors import BadRequest, Forbidden, NotFound
from core.timeutil import iso_utc, today_for_company_id
from modules.custom_forms import fields as F
from modules.custom_forms import templates as T
from modules.custom_forms.models import (CustomFormComment, CustomFormRecord, CustomFormVersion, CustomFormView,
                                         CustomService)

MANAGE = "settings.services.manage"
RECORDS = "people.services.records"
SUBJECT = "custom_form_record"

NAV_SECTIONS = {"home": "Home", "people": "People", "time": "Time", "pay": "Payroll", "talent": "Talent",
                "engage": "Engage"}
ICONS = ["clipboard-list", "graduation-cap", "id-card", "contact", "badge-check", "key-round", "laptop", "car",
         "plane", "shirt", "utensils", "home", "heart-pulse", "wrench", "package", "file-text", "calendar-check",
         "megaphone", "gift", "shield-check", "building", "phone", "mail", "printer", "coffee", "bus", "ticket",
         "briefcase", "book-open", "hand-coins"]
TONES = ("neutral", "accent", "good", "warn", "bad", "info")
SYSTEM_STATUSES = {
    "draft": ("Draft", "neutral"),
    "pending": ("In approval", "warn"),
    "rejected": ("Rejected", "bad"),
    "cancelled": ("Withdrawn", "neutral"),
}
CLOSED_SYSTEM = {"rejected", "cancelled"}
ROLE_LABELS = {"admin": "HR admin", "hr": "HR", "manager": "Manager", "employee": "Employee"}


def request_type_key(company_id: int, code: str) -> str:
    return f"form.{company_id}.{code}"


def series_key(code: str) -> str:
    return f"form.{code}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _actor() -> str:
    return context.current().actor_name or "System"


# ── Who is asking ───────────────────────────────────────────────────

class Viewer:
    """The signed-in person with everything the rules need, loaded once per request."""

    def __init__(self, db: Session, user: dict):
        from core.security import _extra_role_names, _get_role_permissions
        from modules.employees.models import Employee
        self.db = db
        self.company_id: int = user["company_id"]
        self.employee_id: int | None = user.get("employee_id")
        self.is_owner: bool = bool(user.get("is_owner"))
        self.role: str = user["role"]
        self.perms = set(_get_role_permissions(self.company_id, self.role, db))
        roles = {self.role}
        if self.employee_id:
            roles |= set(_extra_role_names(self.company_id, self.employee_id, db))
        self.roles = roles
        self.emp = None
        if self.employee_id:
            self.emp = db.query(Employee).filter(Employee.company_id == self.company_id,
                                                 Employee.id == self.employee_id).first()
        self._teams: set[int] | None = None
        self._is_manager: bool | None = None

    @property
    def is_admin(self) -> bool:
        return self.is_owner or "admin" in self.roles

    @property
    def can_design(self) -> bool:
        return self.is_admin or MANAGE in self.perms

    @property
    def handles_all(self) -> bool:
        return self.is_admin or RECORDS in self.perms

    @property
    def team_ids(self) -> set[int]:
        if self._teams is None:
            self._teams = set()
            if self.employee_id:
                try:
                    from modules.organization.models import TeamMember
                    self._teams = {t for (t,) in self.db.query(TeamMember.team_id).filter(
                        TeamMember.company_id == self.company_id, TeamMember.employee_id == self.employee_id).all()}
                except Exception:  # pragma: no cover - organisation module missing
                    self._teams = set()
        return self._teams

    @property
    def is_manager(self) -> bool:
        if self._is_manager is None:
            from modules.employees.models import Employee
            self._is_manager = bool(self.employee_id) and self.db.query(Employee.id).filter(
                Employee.company_id == self.company_id, Employee.reporting_manager_id == self.employee_id,
                Employee.active.is_(True)).first() is not None
        return self._is_manager

    @property
    def owner_key(self) -> str:
        return f"employee:{self.employee_id}" if self.employee_id else "owner"

    def requester_vars(self, today: date) -> dict:
        return requester_vars_for(self.emp, today, is_manager=self.is_manager)

    def can_handle(self, s: CustomService) -> bool:
        return (self.handles_all or bool(self.roles & set(s.manager_roles or []))
                or (self.employee_id is not None and self.employee_id in (s.manager_employee_ids or [])))

    def can_submit(self, s: CustomService) -> bool:
        if not s.active or s.archived_at is not None:
            return False
        if self.is_admin:
            return True
        acc = s.submit_access or {}
        if acc.get("everyone", True):
            return True
        if self.roles & set(acc.get("roles") or []):
            return True
        if self.team_ids & set(acc.get("team_ids") or []):
            return True
        if self.emp is not None:
            if self.emp.department_id and self.emp.department_id in (acc.get("department_ids") or []):
                return True
            if getattr(self.emp, "location_id", None) and self.emp.location_id in (acc.get("location_ids") or []):
                return True
        return False

    def field_full(self) -> bool:
        return self.is_owner


def requester_vars_for(emp, today: date, is_manager: bool = False) -> dict:
    if emp is None:
        return {"role": "admin", "is_owner": True, "today": today, "is_manager": False}
    return {"id": emp.id, "role": emp.role, "department_id": emp.department_id,
            "location_id": getattr(emp, "location_id", None), "designation_id": emp.designation_id,
            "employee_code": emp.employee_code, "is_manager": is_manager, "today": today}


class DbLookups(F.Lookups):
    def __init__(self, db: Session, company_id: int):
        self.db, self.company_id = db, company_id

    def master_ids(self, master: str, ids: list[int]) -> None:
        from core.masters import validate_value_ids
        try:
            validate_value_ids(self.db, self.company_id, master, ids)
        except NotFound:
            raise BadRequest("A list used on this form no longer exists.")

    def entity_ids(self, kind: str, ids: list[int]) -> None:
        model, extra = _entity_model(kind)
        q = self.db.query(model.id).filter(model.company_id == self.company_id, model.id.in_(ids))
        if extra is not None:
            q = q.filter(extra)
        found = {i for (i,) in q.all()}
        if any(i not in found for i in ids):
            raise BadRequest(f"Pick a {kind} from the list.")

    def files(self, uuids: list) -> list[dict]:
        from modules.documents import service as docs
        return docs.attachments_from(self.db, self.company_id, uuids, limit=F.MAX_FILES)


def _entity_model(kind: str):
    if kind == "employee":
        from modules.employees.models import Employee
        return Employee, None
    if kind == "department":
        from modules.departments.models import Department
        return Department, None
    if kind == "location":
        from modules.organization.models import Location
        return Location, None
    raise BadRequest("Unknown kind of list.")


# ── Run-time registration: approval request types ───────────────────

_TYPE_CACHE: dict[str, tuple[float, object]] = {}
_COMPANY_TYPES: dict[int, tuple[float, list]] = {}
CACHE_SECONDS = 120


def _build_type(s: CustomService, definition: dict | None):
    from core.workflow.registry import RequestType
    from modules.custom_forms import hooks
    variables = {"request.service": "Service code"}
    for f in F.input_fields(definition or {}):
        if f["type"] in ("file",):
            continue
        variables[f"request.{f['key']}"] = f["label"]
    return RequestType(
        key=request_type_key(s.company_id, s.code), label=s.name,
        description=(s.description or "A request made with a custom service.")
        + " Fields of the form can be used in conditions, e.g. request.<field> > 1000.",
        on_approved=hooks.on_approved, on_rejected=hooks.on_rejected, on_cancelled=hooks.on_cancelled,
        on_revoked=hooks.on_revoked, variables=variables, summarize=hooks.summarize,
    )


def remember_type(db: Session, s: CustomService) -> None:
    """Make the service's request type known in this process at once (after a change)."""
    ver = current_version(db, s)
    _TYPE_CACHE[request_type_key(s.company_id, s.code)] = (_time.monotonic(), _build_type(s, ver.definition if ver else None))
    _COMPANY_TYPES.pop(s.company_id, None)


def forget_company(company_id: int) -> None:
    _COMPANY_TYPES.pop(company_id, None)
    _SOURCE_CACHE.pop(company_id, None)


def type_for_key(key: str):
    """Request type for `form.<company>.<code>` (registry provider)."""
    if not key.startswith("form."):
        return None
    hit = _TYPE_CACHE.get(key)
    if hit and _time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    parts = key.split(".", 2)
    if len(parts) != 3 or not parts[1].isdigit():
        return None
    from core.db import system_session
    db = system_session()
    try:
        s = db.query(CustomService).filter(CustomService.company_id == int(parts[1]),
                                           CustomService.code == parts[2]).first()
        if s is None:
            return hit[1] if hit else None
        ver = current_version(db, s)
        rt = _build_type(s, ver.definition if ver else None)
    finally:
        db.close()
    _TYPE_CACHE[key] = (_time.monotonic(), rt)
    return rt


def types_for_company(company_id: int) -> list:
    """A company's services that need approval (registry provider; shown on Approval flows)."""
    hit = _COMPANY_TYPES.get(company_id)
    if hit and _time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    from core.db import system_session
    db = system_session()
    try:
        rows = db.query(CustomService).filter(CustomService.company_id == company_id,
                                              CustomService.archived_at.is_(None),
                                              CustomService.needs_approval.is_(True)).all()
        versions = _current_versions(db, rows)
        out = [_build_type(s, versions.get(s.id)) for s in rows]
    finally:
        db.close()
    for rt in out:
        _TYPE_CACHE[rt.key] = (_time.monotonic(), rt)
    _COMPANY_TYPES[company_id] = (_time.monotonic(), out)
    return out


def _current_versions(db: Session, services: list[CustomService]) -> dict[int, dict]:
    if not services:
        return {}
    rows = db.query(CustomFormVersion).filter(
        CustomFormVersion.company_id == services[0].company_id,
        CustomFormVersion.service_id.in_([s.id for s in services])).all()
    want = {s.id: s.current_version for s in services}
    return {v.service_id: v.definition for v in rows if want.get(v.service_id) == v.version}


# ── Report sources (one per service) ────────────────────────────────

_SOURCE_CACHE: dict[int, tuple[float, list]] = {}


def report_sources() -> list:
    """Report sources for the company in the request context (reports registry provider)."""
    company_id = context.current().company_id
    if not company_id:
        return []
    hit = _SOURCE_CACHE.get(company_id)
    if hit and _time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    from core.db import system_session
    from modules.custom_forms.report_sources import source_for
    db = system_session()
    try:
        rows = db.query(CustomService).filter(CustomService.company_id == company_id,
                                              CustomService.archived_at.is_(None)).order_by(CustomService.name).all()
        versions = _current_versions(db, rows)
        out = [source_for(s, versions.get(s.id) or {"fields": []}) for s in rows]
    finally:
        db.close()
    _SOURCE_CACHE[company_id] = (_time.monotonic(), out)
    return out


# ── Services ────────────────────────────────────────────────────────

def get_service(db: Session, company_id: int, code_or_uuid: str) -> CustomService:
    q = db.query(CustomService).filter(CustomService.company_id == company_id)
    row = None
    if re.fullmatch(r"[0-9a-fA-F-]{36}", str(code_or_uuid)):
        row = q.filter(CustomService.uuid == code_or_uuid).first()
    if row is None:
        row = q.filter(CustomService.code == str(code_or_uuid)).first()
    if row is None:
        raise NotFound("Service")
    return row


def current_version(db: Session, s: CustomService) -> CustomFormVersion | None:
    return db.query(CustomFormVersion).filter(CustomFormVersion.company_id == s.company_id,
                                              CustomFormVersion.service_id == s.id,
                                              CustomFormVersion.version == s.current_version).first()


def _code_from(db: Session, company_id: int, name: str, wanted: str | None = None) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", (wanted or name or "").lower()).strip("_")[:26] or "service"
    if not base[0].isalpha():
        base = f"s_{base}"[:26]
    have = {c for (c,) in db.query(CustomService.code).filter(CustomService.company_id == company_id,
                                                               CustomService.code.like(f"{base}%")).all()}
    if base not in have:
        return base
    for i in range(2, 100):
        if f"{base}_{i}" not in have:
            return f"{base}_{i}"
    raise BadRequest("Choose a different name.")


def _check_name(db: Session, company_id: int, name: str, exclude_id: int | None = None) -> str:
    name = (name or "").strip()
    if not name:
        raise BadRequest("Give the service a name.")
    if len(name) > 50:
        raise BadRequest("The name can be at most 50 characters.")
    q = db.query(CustomService.id).filter(CustomService.company_id == company_id,
                                          func.lower(CustomService.name) == name.lower())
    if exclude_id:
        q = q.filter(CustomService.id != exclude_id)
    if q.first():
        raise BadRequest(f"There is already a service called “{name}”.")
    return name


def clean_statuses(raw) -> list[dict]:
    items = raw or []
    if not isinstance(items, list) or not items:
        raise BadRequest("Add at least one status.")
    if len(items) > 12:
        raise BadRequest("Up to 12 statuses.")
    out, keys = [], set()
    for i, s in enumerate(items, start=1):
        label = str((s or {}).get("label") or "").strip()
        if not label:
            raise BadRequest(f"Status {i}: give it a name.")
        if len(label) > 40:
            raise BadRequest(f"Status {i}: at most 40 characters.")
        key = str(s.get("key") or "").strip() or F.slug_key(label)[:30]
        key = re.sub(r"[^a-z0-9_]", "_", key.lower())[:30] or f"status_{i}"
        if key in SYSTEM_STATUSES:
            key = f"{key}_{i}"
        if key in keys:
            raise BadRequest(f"Status {i}: “{label}” is listed twice.")
        keys.add(key)
        out.append({"key": key, "label": label, "tone": s.get("tone") if s.get("tone") in TONES else "neutral",
                    "closed": bool(s.get("closed"))})
    if all(s["closed"] for s in out):
        raise BadRequest("At least one status must be open (not final).")
    if out[0]["closed"]:
        raise BadRequest("The first status is where new records start, so it can't be a final one.")
    return out


def _clean_access(db: Session, company_id: int, raw: dict | None) -> dict:
    raw = raw or {}
    if raw.get("everyone", True):
        return {"everyone": True}
    out = {"everyone": False}
    for k in ("roles",):
        out[k] = sorted({str(x) for x in raw.get(k) or [] if str(x).strip()})
    for k in ("team_ids", "department_ids", "location_ids"):
        vals = []
        for x in raw.get(k) or []:
            try:
                vals.append(int(x))
            except (TypeError, ValueError):
                continue
        out[k] = sorted(set(vals))
    if not any(out.get(k) for k in ("roles", "team_ids", "department_ids", "location_ids")):
        raise BadRequest("Choose who may use this service, or let everyone use it.")
    return out


def _clean_employee_ids(db: Session, company_id: int, ids) -> list[int]:
    out = []
    for x in ids or []:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    if out:
        DbLookups(db, company_id).entity_ids("employee", out)
    return sorted(set(out))


def apply_settings(db: Session, s: CustomService, data: dict) -> None:
    cid = s.company_id
    if "name" in data and data["name"] is not None:
        s.name = _check_name(db, cid, data["name"], s.id)
    if "description" in data:
        d = (data.get("description") or "").strip()
        if len(d) > 300:
            raise BadRequest("The description can be at most 300 characters.")
        s.description = d or None
    if data.get("icon"):
        s.icon = data["icon"] if data["icon"] in ICONS else "clipboard-list"
    if data.get("nav_section"):
        if data["nav_section"] not in NAV_SECTIONS:
            raise BadRequest("Pick where the service shows in the menu.")
        s.nav_section = data["nav_section"]
    for flag in ("active", "needs_approval", "allow_drafts", "allow_cancel"):
        if data.get(flag) is not None:
            setattr(s, flag, bool(data[flag]))
    if "submit_access" in data and data["submit_access"] is not None:
        s.submit_access = _clean_access(db, cid, data["submit_access"])
    if "manager_roles" in data and data["manager_roles"] is not None:
        s.manager_roles = sorted({str(r) for r in data["manager_roles"] if str(r).strip()})
    if "manager_employee_ids" in data and data["manager_employee_ids"] is not None:
        s.manager_employee_ids = _clean_employee_ids(db, cid, data["manager_employee_ids"])
    if "statuses" in data and data["statuses"] is not None:
        new = clean_statuses(data["statuses"])
        in_use = {st for (st,) in db.query(CustomFormRecord.status).filter(
            CustomFormRecord.company_id == cid, CustomFormRecord.service_id == s.id).distinct().all()} if s.id else set()
        missing = [x for x in in_use if x not in SYSTEM_STATUSES and x not in {n["key"] for n in new}]
        if missing:
            old = {o["key"]: o["label"] for o in s.statuses or []}
            raise BadRequest(f"The status “{old.get(missing[0], missing[0])}” is used by records, so it can't be "
                             "removed. Rename it instead.")
        s.statuses = new
    if "notify" in data and data["notify"] is not None:
        n = data["notify"] or {}
        s.notify = {"managers_on_submit": bool(n.get("managers_on_submit", True)),
                    "requester_on_status": bool(n.get("requester_on_status", True)),
                    "requester_on_comment": bool(n.get("requester_on_comment", True))}
    if "title_field" in data:
        s.title_field = (data.get("title_field") or None)


def _ensure_series(db: Session, s: CustomService, prefix: str | None = None, pattern: str | None = None) -> None:
    from core.numbering import NumberSeries
    key = series_key(s.code)
    if numbering.get_series(db, s.company_id, key) is not None:
        return
    pre = (prefix or re.sub(r"[^A-Z]", "", s.name.upper())[:3] or "REQ")[:30]
    db.add(NumberSeries(company_id=s.company_id, key=key, name=f"{s.name} records", prefix=pre,
                        pattern=pattern or "{PREFIX}-{YY}{MM}-{SEQ:4}", reset="monthly", active=True,
                        allow_manual=False, description=f"Numbers for records of the {s.name} service."))
    db.flush()


def _ensure_flow(db: Session, s: CustomService, steps: list[dict] | None = None) -> None:
    """Give the service a default approval flow when it needs approval and has none."""
    from core.workflow import service as wf
    from core.workflow.models import WorkflowDefinition
    key = request_type_key(s.company_id, s.code)
    if db.query(WorkflowDefinition.id).filter(WorkflowDefinition.company_id == s.company_id,
                                              WorkflowDefinition.request_type == key).first():
        return
    remember_type(db, s)
    wf.create_definition(db, s.company_id, {"request_type": key, "name": s.name,
                                            "steps": steps or [T.MANAGER_STEP],
                                            "change_note": "Default flow", "active": True})


def _master_keys(db: Session, company_id: int) -> set[str]:
    from core.masters import MasterType
    return {k for (k,) in db.query(MasterType.key).filter(MasterType.company_id == company_id).all()}


def _new_version(db: Session, s: CustomService, definition: dict, note: str | None, version: int) -> CustomFormVersion:
    row = CustomFormVersion(company_id=s.company_id, service_id=s.id, version=version, definition=definition,
                            change_note=(note or None) and note[:300], created_by_name=_actor())
    db.add(row)
    db.flush()
    return row


def create_service(db: Session, company_id: int, data: dict, *, definition: dict | None = None,
                   template: dict | None = None, flow: list[dict] | None = None) -> CustomService:
    name = _check_name(db, company_id, data.get("name") or "")
    code = _code_from(db, company_id, name, data.get("code"))
    defn = F.clean_definition(definition or {"fields": [
        {"key": "details", "type": "textarea", "label": "Details", "required": True}]},
        master_keys=_master_keys(db, company_id))
    s = CustomService(company_id=company_id, code=code, name=name, template_key=(template or {}).get("key"),
                      statuses=T.DEFAULT_STATUSES, manager_roles=["hr"], manager_employee_ids=[],
                      submit_access={"everyone": True}, current_version=1, created_by_name=_actor(),
                      notify={"managers_on_submit": True, "requester_on_status": True, "requester_on_comment": True},
                      needs_approval=True, allow_drafts=True, allow_cancel=True, active=True, icon="clipboard-list",
                      nav_section="home")
    db.add(s)
    db.flush()
    apply_settings(db, s, {k: v for k, v in data.items() if k not in ("name", "code")})
    if s.title_field and s.title_field not in F.all_keys(defn):
        s.title_field = None
    _new_version(db, s, defn, "First version", 1)
    _ensure_series(db, s, data.get("prefix"))
    if s.needs_approval:
        _ensure_flow(db, s, flow)
    remember_type(db, s)
    forget_company(company_id)
    audit.event(db, "custom_form.service_created", "custom_services", s.id, {"code": s.code, "template": s.template_key},
                entity_uuid=str(s.uuid), entity_label=s.name)
    return s


def update_service(db: Session, s: CustomService, data: dict) -> CustomService:
    apply_settings(db, s, data)
    ver = current_version(db, s)
    if s.title_field and ver and s.title_field not in F.all_keys(ver.definition):
        raise BadRequest("The title field must be a field of the form.")
    series = numbering.get_series(db, s.company_id, series_key(s.code))
    if series is not None:
        if data.get("prefix") is not None:
            series.prefix = str(data["prefix"]).strip()[:30]
        if data.get("pattern"):
            numbering.validate_pattern(data["pattern"])
            series.pattern = data["pattern"]
    if s.needs_approval:
        _ensure_flow(db, s)
    db.flush()
    remember_type(db, s)
    forget_company(s.company_id)
    return s


def _type_signature(f: dict) -> str:
    return f"{f['type']}{'*' if f.get('multiple') else ''}"


def save_form(db: Session, s: CustomService, raw: dict, note: str | None) -> CustomFormVersion:
    """Save the designer's form as a new version (nothing happens when it is unchanged)."""
    defn = F.clean_definition(raw, master_keys=_master_keys(db, s.company_id))
    # A field keeps its type across versions so old records always read correctly.
    earlier: dict[str, tuple[str, str]] = {}
    for (d,) in db.query(CustomFormVersion.definition).filter(CustomFormVersion.company_id == s.company_id,
                                                              CustomFormVersion.service_id == s.id).all():
        for f in F.input_fields(d):
            earlier.setdefault(f["key"], (_type_signature(f), f["label"]))
    for f in F.input_fields(defn):
        old = earlier.get(f["key"])
        if old and old[0] != _type_signature(f):
            raise BadRequest(f"“{f['label']}” was saved before as a different kind of field. Its kind can't change, "
                             "so old records stay readable. Add a new field instead.")
    cur = current_version(db, s)
    if cur is not None and cur.definition == defn:
        return cur
    row = _new_version(db, s, defn, note, s.current_version + 1)
    s.current_version = row.version
    if s.title_field and s.title_field not in F.all_keys(defn):
        s.title_field = None
    remember_type(db, s)
    forget_company(s.company_id)
    audit.event(db, "custom_form.form_saved", "custom_services", s.id, {"version": row.version, "note": note},
                entity_uuid=str(s.uuid), entity_label=s.name)
    return row


def install_template(db: Session, company_id: int, key: str) -> CustomService:
    t = T.BY_KEY.get(key)
    if t is None:
        raise NotFound("Template")
    existing = db.query(CustomService).filter(CustomService.company_id == company_id,
                                              CustomService.template_key == key).first()
    if existing is not None:
        if existing.archived_at is not None:
            raise BadRequest(f"“{existing.name}” was added before and is archived. Restore it instead.")
        raise BadRequest(f"“{existing.name}” is already added.")
    name = t["name"]
    if db.query(CustomService.id).filter(CustomService.company_id == company_id,
                                         func.lower(CustomService.name) == name.lower()).first():
        name = f"{name} (template)"[:50]
    data = {"name": name, "code": key, "description": t["description"], "icon": t["icon"],
            "nav_section": t["nav_section"], "needs_approval": t["needs_approval"], "statuses": t["statuses"],
            "title_field": t.get("title_field"), "prefix": t["prefix"]}
    return create_service(db, company_id, data, definition={"fields": t["fields"]}, template=t, flow=t.get("flow"))


def copy_service(db: Session, s: CustomService, name: str) -> CustomService:
    ver = current_version(db, s)
    data = {"name": name, "description": s.description, "icon": s.icon, "nav_section": s.nav_section,
            "needs_approval": s.needs_approval, "allow_drafts": s.allow_drafts, "allow_cancel": s.allow_cancel,
            "submit_access": s.submit_access, "manager_roles": s.manager_roles,
            "manager_employee_ids": s.manager_employee_ids, "statuses": s.statuses, "notify": s.notify,
            "title_field": s.title_field}
    return create_service(db, s.company_id, data, definition=ver.definition if ver else None)


def archive_service(db: Session, s: CustomService, archive: bool) -> CustomService:
    if archive:
        s.archived_at, s.archived_by_name, s.active = _now(), _actor(), False
    else:
        s.archived_at, s.archived_by_name, s.active = None, None, True
    forget_company(s.company_id)
    remember_type(db, s)
    return s


def series_info(db: Session, s: CustomService) -> dict | None:
    series = numbering.get_series(db, s.company_id, series_key(s.code))
    if series is None:
        return None
    try:
        nxt = numbering.preview(db, series, today_for_company_id(db, s.company_id))
    except Exception:
        nxt = None
    return {"uuid": str(series.uuid), "prefix": series.prefix, "pattern": series.pattern, "reset": series.reset,
            "active": series.active, "next": nxt}


def flow_count(db: Session, s: CustomService) -> int:
    from core.workflow.models import WorkflowDefinition
    return db.query(func.count(WorkflowDefinition.id)).filter(
        WorkflowDefinition.company_id == s.company_id, WorkflowDefinition.is_current.is_(True),
        WorkflowDefinition.request_type == request_type_key(s.company_id, s.code)).scalar() or 0


def record_counts(db: Session, company_id: int) -> dict[int, dict]:
    rows = db.query(CustomFormRecord.service_id, CustomFormRecord.status, func.count(CustomFormRecord.id)).filter(
        CustomFormRecord.company_id == company_id).group_by(CustomFormRecord.service_id, CustomFormRecord.status).all()
    out: dict[int, dict] = {}
    for sid, st, n in rows:
        d = out.setdefault(sid, {"total": 0, "pending": 0, "draft": 0})
        if st != "draft":
            d["total"] += n
        if st in ("pending", "draft"):
            d[st] += n
    return out


# ── Statuses ────────────────────────────────────────────────────────

def status_info(s: CustomService, status: str) -> dict:
    if status in SYSTEM_STATUSES:
        label, tone = SYSTEM_STATUSES[status]
        return {"key": status, "label": label, "tone": tone, "closed": status in CLOSED_SYSTEM, "system": True}
    for st in s.statuses or []:
        if st["key"] == status:
            return {**st, "system": False}
    return {"key": status, "label": status.replace("_", " ").capitalize(), "tone": "neutral", "closed": False,
            "system": False}


def all_statuses(s: CustomService) -> list[dict]:
    first = [status_info(s, "draft"), status_info(s, "pending")]
    return first + [status_info(s, x["key"]) for x in s.statuses or []] + \
        [status_info(s, "rejected"), status_info(s, "cancelled")]


def _first_status(s: CustomService) -> str:
    return (s.statuses or T.DEFAULT_STATUSES)[0]["key"]


def _is_work_status(s: CustomService, status: str) -> bool:
    return status not in SYSTEM_STATUSES


def _closed(s: CustomService, status: str) -> bool:
    return bool(status_info(s, status)["closed"])


# ── Records ─────────────────────────────────────────────────────────

def get_record(db: Session, company_id: int, uuid) -> CustomFormRecord:
    try:
        row = db.query(CustomFormRecord).filter(CustomFormRecord.company_id == company_id,
                                                CustomFormRecord.uuid == str(uuid)).first()
    except Exception:
        row = None
    if row is None:
        raise NotFound("Record")
    return row


def is_requester(v: Viewer, r: CustomFormRecord) -> bool:
    if r.requester_employee_id is None:
        return v.is_owner and not v.employee_id
    return v.employee_id is not None and r.requester_employee_id == v.employee_id


def is_approver(db: Session, v: Viewer, r: CustomFormRecord) -> bool:
    if not r.workflow_instance_id:
        return False
    from core.workflow.models import WorkflowTask
    q = db.query(WorkflowTask.id).filter(WorkflowTask.company_id == r.company_id,
                                         WorkflowTask.instance_id == r.workflow_instance_id)
    if v.is_owner:
        conds = [WorkflowTask.approver_kind == "owner"]
        if v.employee_id:
            conds.append(WorkflowTask.approver_employee_id == v.employee_id)
        return q.filter(or_(*conds)).first() is not None
    if not v.employee_id:
        return False
    return q.filter(or_(WorkflowTask.approver_employee_id == v.employee_id,
                        WorkflowTask.delegated_from_employee_id == v.employee_id)).first() is not None


def can_view(db: Session, v: Viewer, r: CustomFormRecord) -> bool:
    if is_requester(v, r):
        return True
    if r.status == "draft":
        return False
    return v.can_handle(r.service) or is_approver(db, v, r)


def require_view(db: Session, v: Viewer, r: CustomFormRecord) -> None:
    if not can_view(db, v, r):
        raise NotFound("Record")


def _requester_vars_for_record(db: Session, r: CustomFormRecord, today: date) -> dict:
    from modules.employees.models import Employee
    emp = None
    if r.requester_employee_id:
        emp = db.query(Employee).filter(Employee.company_id == r.company_id, Employee.id == r.requester_employee_id).first()
    return requester_vars_for(emp, today)


def _title(s: CustomService, definition: dict, values: dict, labels: dict | None = None) -> str:
    if s.title_field and values.get(s.title_field) not in (None, "", []):
        f = next((x for x in F.input_fields(definition) if x["key"] == s.title_field), None)
        if f is not None:
            text = display_text(f, values[s.title_field], labels or {})
            if text:
                return f"{s.name}: {text}"[:250]
    return s.name


def _check_unique(db: Session, s: CustomService, r: CustomFormRecord, definition: dict, values: dict) -> None:
    for f in F.unique_fields(definition):
        v = values.get(f["key"])
        if v in (None, ""):
            continue
        other = db.query(CustomFormRecord.reference_no).filter(
            CustomFormRecord.company_id == s.company_id, CustomFormRecord.service_id == s.id,
            CustomFormRecord.id != r.id, CustomFormRecord.status.notin_(("draft", "cancelled", "rejected")),
            func.lower(CustomFormRecord.values[f["key"]].astext) == str(v).lower()).first()
        if other is not None:
            raise BadRequest(f"{f['label']} “{v}” is already used on {other[0] or 'another record'}. "
                             "It must be different on every record.")


def _activity(db: Session, r: CustomFormRecord, kind: str, body: str | None = None, data: dict | None = None,
              attachments: list | None = None) -> CustomFormComment:
    ctx = context.current()
    row = CustomFormComment(company_id=r.company_id, record_id=r.id, kind=kind, body=body,
                            author_employee_id=ctx.employee_id, author_name=ctx.actor_name or "System",
                            data=data or {}, attachments=attachments or [])
    db.add(row)
    return row


def _notify(db: Session, company_id: int, recipients: list, title: str, r: CustomFormRecord, kind: str) -> None:
    if not recipients:
        return
    try:
        from modules.notifications.service import notify
    except Exception:  # pragma: no cover
        return
    ctx = context.current()
    notify(db, company_id, recipients, kind=kind, title=title,
           body=f"{title} ({r.reference_no})" if r.reference_no else title,
           link=f"/dashboard/services/{r.service.code}?view={r.uuid}", actor_name=ctx.actor_name,
           actor_employee_id=ctx.employee_id, skip={ctx.employee_id} if ctx.employee_id else None)


def _requester_recipient(r: CustomFormRecord) -> list:
    return [r.requester_employee_id] if r.requester_employee_id else [None]


def _summary_lines(definition: dict, values: dict, labels: dict) -> list[str]:
    lines = []
    candidates = [f for f in F.input_fields(definition) if f.get("summary")] or F.input_fields(definition)
    for f in candidates:
        if (f.get("permissions") or {}).get("manager") == "hidden" or (f.get("permissions") or {}).get("employee") == "hidden":
            continue
        if f["type"] in ("table", "file") or values.get(f["key"]) in (None, "", []):
            continue
        lines.append(f"{f['label']}: {display_text(f, values[f['key']], labels)}"[:200])
        if len(lines) >= 6:
            break
    return lines


def create_record(db: Session, v: Viewer, s: CustomService, submitted: dict, *, draft: bool) -> CustomFormRecord:
    if not v.can_submit(s):
        raise Forbidden("You can't use this service.")
    if draft and not s.allow_drafts:
        raise BadRequest("This service doesn't keep drafts. Submit the form instead.")
    ver = current_version(db, s)
    today = today_for_company_id(db, s.company_id)
    values = F.process(ver.definition, submitted, roles=v.roles, full=v.field_full(), requester=v.requester_vars(today),
                       lookups=DbLookups(db, s.company_id), check_required=not draft, today=today, new=True)
    emp = v.emp
    r = CustomFormRecord(company_id=s.company_id, service_id=s.id, version_id=ver.id, values=values, status="draft",
                         requester_employee_id=v.employee_id,
                         requester_name=(f"{emp.first_name} {emp.last_name}".strip() if emp else _actor()),
                         created_by_name=_actor(), title=s.name)
    r.service = s
    r.version = ver
    db.add(r)
    db.flush()
    _activity(db, r, "system", "Saved as a draft" if draft else None)
    if not draft:
        submit_record(db, v, r)
    return r


def submit_record(db: Session, v: Viewer, r: CustomFormRecord) -> CustomFormRecord:
    s = r.service
    if r.status != "draft":
        raise BadRequest("This record was already submitted.")
    if not is_requester(v, r):
        raise Forbidden("Only the person who made this draft can submit it.")
    if not s.active or s.archived_at is not None:
        raise BadRequest("This service is switched off.")
    ver = r.version
    if ver.version != s.current_version:
        # A draft made on an older form is moved to the current one before it is sent.
        newer = current_version(db, s)
        known = set(F.all_keys(newer.definition))
        r.values = {k: val for k, val in (r.values or {}).items() if k in known}
        r.version_id, r.version, ver = newer.id, newer, newer
    today = today_for_company_id(db, s.company_id)
    values = F.process(ver.definition, {}, existing=r.values, roles=v.roles, full=v.field_full(),
                       requester=v.requester_vars(today), lookups=DbLookups(db, s.company_id), check_required=True,
                       today=today)
    r.values = values
    _check_unique(db, s, r, ver.definition, values)
    labels = resolve_labels(db, s.company_id, [(ver.definition, values)])
    r.reference_no = numbering.next_number(db, s.company_id, series_key(s.code), today) or f"{s.code.upper()[:6]}-{r.id}"
    r.title = _title(s, ver.definition, values, labels)
    r.submitted_at = _now()
    r.status_changed_at = r.submitted_at
    if s.needs_approval:
        from core.workflow import service as wf
        remember_type(db, s)
        r.status = "pending"
        payload = {"service": s.code, "record_uuid": str(r.uuid), "reference_no": r.reference_no,
                   "summary": _summary_lines(ver.definition, values, labels)}
        for f in F.input_fields(ver.definition):
            if f["key"] in values and f["key"] not in ("service", "summary", "record_uuid", "reference_no"):
                payload[f["key"]] = F.to_payload_value(f, values[f["key"]])
        inst = wf.submit(db, s.company_id, request_type_key(s.company_id, s.code), payload, r.title,
                         subject_type=SUBJECT, subject_id=r.id)
        r.workflow_instance_id = inst.id
    else:
        r.status = _first_status(s)
    _activity(db, r, "status", "Submitted", {"to": r.status})
    if (s.notify or {}).get("managers_on_submit", True):
        _notify(db, s.company_id, list(s.manager_employee_ids or []), f"New {s.name.lower()} from {r.requester_name}",
                r, "custom_form.submitted")
    audit.event(db, "custom_form.submitted", "custom_form_records", r.id,
                {"service": s.code, "reference": r.reference_no, "status": r.status},
                entity_uuid=str(r.uuid), entity_label=r.title)
    db.flush()
    return r


def update_record(db: Session, v: Viewer, r: CustomFormRecord, submitted: dict) -> CustomFormRecord:
    s, ver = r.service, r.version
    today = today_for_company_id(db, s.company_id)
    if r.status == "draft":
        if not is_requester(v, r):
            raise NotFound("Record")
        values = F.process(ver.definition, submitted, existing=r.values, roles=v.roles, full=v.field_full(),
                           requester=v.requester_vars(today), lookups=DbLookups(db, s.company_id),
                           check_required=False, today=today)
        r.values = values
        return r
    if not v.can_handle(s):
        raise Forbidden("Only the people who handle this service can change a submitted record.")
    if r.status in CLOSED_SYSTEM:
        raise BadRequest("A rejected or withdrawn record can't be changed.")
    before = dict(r.values or {})
    values = F.process(ver.definition, submitted, existing=before, roles=v.roles, full=v.field_full(),
                       requester=_requester_vars_for_record(db, r, today), lookups=DbLookups(db, s.company_id),
                       check_required=True, today=today)
    changed = F.changed_labels(ver.definition, before, values)
    if not changed:
        return r
    _check_unique(db, s, r, ver.definition, values)
    r.values = values
    labels = resolve_labels(db, s.company_id, [(ver.definition, values)])
    r.title = _title(s, ver.definition, values, labels)
    _activity(db, r, "edit", "Changed " + ", ".join(changed[:8]) + ("…" if len(changed) > 8 else ""),
              {"fields": changed})
    audit.event(db, "custom_form.updated", "custom_form_records", r.id, {"fields": changed},
                entity_uuid=str(r.uuid), entity_label=r.title)
    return r


def change_status(db: Session, v: Viewer, r: CustomFormRecord, status: str, note: str | None) -> CustomFormRecord:
    s = r.service
    if not v.can_handle(s):
        raise Forbidden("Only the people who handle this service can change its status.")
    if not _is_work_status(s, r.status):
        raise BadRequest("The status can be changed once the record is approved.")
    keys = [x["key"] for x in s.statuses or []]
    if status not in keys:
        raise BadRequest("Pick one of the service's statuses.")
    if status == r.status:
        return r
    old = r.status
    r.status = status
    r.status_changed_at = _now()
    r.closed_at = _now() if _closed(s, status) else None
    label = status_info(s, status)["label"]
    _activity(db, r, "status", (note or "").strip()[:2000] or None, {"from": old, "to": status})
    if (s.notify or {}).get("requester_on_status", True):
        _notify(db, s.company_id, _requester_recipient(r), f"{r.title}: {label}", r, "custom_form.status")
    audit.event(db, "custom_form.status_changed", "custom_form_records", r.id, {"from": old, "to": status},
                entity_uuid=str(r.uuid), entity_label=r.title)
    return r


def can_cancel(v: Viewer, r: CustomFormRecord) -> bool:
    s = r.service
    if r.status in CLOSED_SYSTEM or (_is_work_status(s, r.status) and _closed(s, r.status)):
        return False
    if is_requester(v, r):
        if r.status == "draft":
            return True
        if not s.allow_cancel:
            return False
        return r.status == "pending" or r.status == _first_status(s)
    return v.can_handle(s) and _is_work_status(s, r.status)


def cancel_record(db: Session, v: Viewer, r: CustomFormRecord, reason: str | None) -> CustomFormRecord:
    if not can_cancel(v, r):
        raise BadRequest("This record can't be withdrawn now.")
    reason = (reason or "").strip()[:500] or None
    if r.status == "pending" and r.workflow_instance_id:
        from core.workflow import service as wf
        from core.workflow.models import WorkflowInstance
        inst = db.query(WorkflowInstance).filter(WorkflowInstance.company_id == r.company_id,
                                                 WorkflowInstance.id == r.workflow_instance_id).first()
        if inst is not None and inst.status == "pending":
            wf.cancel(db, r.company_id, inst, reason)  # runs on_cancelled, which closes the record
            return r
    old = r.status
    r.status, r.closed_at, r.status_changed_at = "cancelled", _now(), _now()
    _activity(db, r, "status", reason, {"from": old, "to": "cancelled"})
    if not is_requester(v, r):
        _notify(db, r.company_id, _requester_recipient(r), f"{r.title} was withdrawn", r, "custom_form.status")
    audit.event(db, "custom_form.cancelled", "custom_form_records", r.id, {"reason": reason},
                entity_uuid=str(r.uuid), entity_label=r.title)
    return r


def delete_draft(db: Session, v: Viewer, r: CustomFormRecord) -> None:
    """Drafts are the only records that may be removed (by the person who made them)."""
    if r.status != "draft" or not is_requester(v, r):
        raise BadRequest("Only your own drafts can be deleted.")
    db.query(CustomFormComment).filter(CustomFormComment.company_id == r.company_id,
                                       CustomFormComment.record_id == r.id).delete()
    audit.event(db, "custom_form.draft_deleted", "custom_form_records", r.id, {"service": r.service.code},
                entity_uuid=str(r.uuid), entity_label=r.title)
    db.delete(r)


def add_comment(db: Session, v: Viewer, r: CustomFormRecord, body: str | None, attachment_uuids: list | None):
    body = (body or "").strip()
    if not body and not attachment_uuids:
        raise BadRequest("Write a comment or add a file.")
    if len(body) > 5000:
        raise BadRequest("A comment can be at most 5,000 characters.")
    files = []
    if attachment_uuids:
        from modules.documents import service as docs
        files = docs.attachments_from(db, r.company_id, attachment_uuids, limit=10)
    row = _activity(db, r, "comment", body or None, attachments=files)
    s = r.service
    if is_requester(v, r):
        _notify(db, r.company_id, list(s.manager_employee_ids or []), f"New comment on {r.title}", r,
                "custom_form.comment")
    elif (s.notify or {}).get("requester_on_comment", True):
        _notify(db, r.company_id, _requester_recipient(r), f"New comment on {r.title}", r, "custom_form.comment")
    db.flush()
    return row


# Workflow handlers (called from hooks.py)

def for_instance(db: Session, instance) -> CustomFormRecord | None:
    if instance.subject_type != SUBJECT or not instance.subject_id:
        return None
    return db.query(CustomFormRecord).filter(CustomFormRecord.company_id == instance.company_id,
                                             CustomFormRecord.id == instance.subject_id).first()


def on_decided(db: Session, instance, outcome: str) -> None:
    r = for_instance(db, instance)
    if r is None:
        return
    old = r.status
    if outcome == "approved":
        if r.status != "pending":
            return
        r.status = _first_status(r.service)
        r.closed_at = None
        body = "Approved"
    else:
        if r.status in CLOSED_SYSTEM:
            return
        r.status = outcome
        r.closed_at = _now()
        body = {"rejected": "Rejected", "cancelled": "Withdrawn"}.get(outcome, outcome)
        if instance.result_note:
            body = f"{body}: {instance.result_note}"[:2000]
    r.status_changed_at = _now()
    _activity(db, r, "status", body, {"from": old, "to": r.status, "via": "approval"})


# ── Lists ───────────────────────────────────────────────────────────

FILTERABLE = {"single_choice", "boolean", "master", "department", "location", "employee", "multi_choice"}


def records_query(db: Session, v: Viewer, s: CustomService | None, *, scope: str, status: list[str] | None = None,
                  q: str | None = None, date_from: date | None = None, date_to: date | None = None,
                  field_filters: dict | None = None):
    query = db.query(CustomFormRecord).filter(CustomFormRecord.company_id == v.company_id)
    if s is not None:
        query = query.filter(CustomFormRecord.service_id == s.id)
    if scope == "mine":
        if v.employee_id:
            query = query.filter(CustomFormRecord.requester_employee_id == v.employee_id)
        else:
            query = query.filter(CustomFormRecord.requester_employee_id.is_(None))
    else:
        if s is None or not v.can_handle(s):
            raise Forbidden("You don't handle this service.")
        mine = (CustomFormRecord.requester_employee_id == v.employee_id) if v.employee_id else \
            CustomFormRecord.requester_employee_id.is_(None)
        query = query.filter(or_(CustomFormRecord.status != "draft", mine))
    if status:
        groups = set(status)
        conds = []
        if "open" in groups and s is not None:
            open_keys = [x["key"] for x in s.statuses or [] if not x.get("closed")] + ["pending"]
            conds.append(CustomFormRecord.status.in_(open_keys))
            groups.discard("open")
        if groups:
            conds.append(CustomFormRecord.status.in_(groups))
        query = query.filter(or_(*conds))
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(CustomFormRecord.reference_no.ilike(like), CustomFormRecord.title.ilike(like),
                                 CustomFormRecord.requester_name.ilike(like)))
    if date_from:
        query = query.filter(func.date(CustomFormRecord.created_at) >= date_from)
    if date_to:
        query = query.filter(func.date(CustomFormRecord.created_at) <= date_to)
    if field_filters and s is not None:
        ver = current_version(db, s)
        fmap = {f["key"]: f for f in F.input_fields(ver.definition)} if ver else {}
        for key, val in field_filters.items():
            f = fmap.get(key)
            if f is None or f["type"] not in FILTERABLE or val in (None, ""):
                continue
            col = CustomFormRecord.values[key]
            if f["type"] == "multi_choice" or f.get("multiple"):
                import json
                parsed = int(val) if f["type"] in F.LOOKUPS and str(val).isdigit() else val
                query = query.filter(col.contains(json.dumps([parsed])))  # jsonb @>
            elif f["type"] == "boolean":
                query = query.filter(col.astext == ("true" if str(val).lower() in ("true", "yes", "1") else "false"))
            else:
                query = query.filter(col.astext == str(val))
    return query


SORTS = {"created_at": CustomFormRecord.created_at, "updated_at": CustomFormRecord.updated_at,
         "reference_no": CustomFormRecord.reference_no, "status": CustomFormRecord.status,
         "requester_name": CustomFormRecord.requester_name, "title": CustomFormRecord.title}


def sort_query(query, sort: str | None):
    key, _, direction = (sort or "created_at:desc").partition(":")
    col = SORTS.get(key, CustomFormRecord.created_at)
    return query.order_by(col.asc() if direction == "asc" else col.desc(), CustomFormRecord.id.desc())


# ── Display ─────────────────────────────────────────────────────────

def _collect_ids(definition: dict, values: dict, acc: dict[str, set]) -> None:
    def take(f, v):
        t = f["type"]
        if t not in F.LOOKUPS or v in (None, ""):
            return
        kind = f"master:{f['master']}" if t == "master" else t
        for i in (v if isinstance(v, list) else [v]):
            if isinstance(i, int) or str(i).isdigit():
                acc.setdefault(kind, set()).add(int(i))
    for f in F.input_fields(definition):
        v = (values or {}).get(f["key"])
        if f["type"] == "table" and isinstance(v, list):
            for row in v:
                for c in f.get("columns") or []:
                    take(c, row.get(c["key"]))
        else:
            take(f, v)


def resolve_labels(db: Session, company_id: int, pairs: list[tuple[dict, dict]]) -> dict[str, str]:
    """Names for ids in values: {"employee:12": "Ravi Kumar", "master:5": "O+", "department:3": "Finance"}."""
    acc: dict[str, set] = {}
    for definition, values in pairs:
        _collect_ids(definition, values, acc)
    out: dict[str, str] = {}
    master_ids = set()
    for kind, ids in acc.items():
        if kind.startswith("master:"):
            master_ids |= ids
    if master_ids:
        from core.masters import labels_for
        for i, label in labels_for(db, company_id, master_ids).items():
            out[f"master:{i}"] = label
    if acc.get("employee"):
        from modules.employees.models import Employee
        for e in db.query(Employee.id, Employee.first_name, Employee.last_name, Employee.employee_code).filter(
                Employee.company_id == company_id, Employee.id.in_(acc["employee"])).all():
            out[f"employee:{e.id}"] = f"{e.first_name} {e.last_name}".strip()
    if acc.get("department"):
        from modules.departments.models import Department
        for i, n in db.query(Department.id, Department.department_name).filter(
                Department.company_id == company_id, Department.id.in_(acc["department"])).all():
            out[f"department:{i}"] = n
    if acc.get("location"):
        from modules.organization.models import Location
        for i, n in db.query(Location.id, Location.name).filter(
                Location.company_id == company_id, Location.id.in_(acc["location"])).all():
            out[f"location:{i}"] = n
    return out


def _label_key(f: dict, i) -> str:
    return f"master:{i}" if f["type"] == "master" else f"{f['type']}:{i}"


def _money(v) -> str:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return str(v)
    neg, d = d < 0, abs(d)
    whole, frac = f"{d:.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return f"{'-' if neg else ''}₹{whole}.{frac}"


def display_text(f: dict, v, labels: dict, tz=None) -> str:
    """A stored value as plain text (exports, summaries, titles)."""
    if v is None or v == "" or v == []:
        return ""
    t = f["type"] if f["type"] != "formula" else f.get("result", "number")
    if t in F.LOOKUPS:
        items = v if isinstance(v, list) else [v]
        return ", ".join(labels.get(_label_key(f, i), f"#{i}") for i in items)
    if t == "boolean":
        return "Yes" if v else "No"
    if t == "currency":
        return _money(v)
    if t == "multi_choice":
        return ", ".join(str(x) for x in v)
    if t == "date":
        try:
            return date.fromisoformat(str(v)[:10]).strftime("%d %b %Y")
        except ValueError:
            return str(v)
    if t == "datetime":
        try:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            if tz is not None:
                dt = dt.astimezone(tz)
            return dt.strftime("%d %b %Y %H:%M")
        except ValueError:
            return str(v)
    if t == "table":
        n = len(v) if isinstance(v, list) else 0
        return f"{n} row{'s' if n != 1 else ''}"
    if t == "file":
        return ", ".join(x.get("name") or "file" for x in v) if isinstance(v, list) else ""
    return str(v)


def record_row(v: Viewer, r: CustomFormRecord, labels: dict) -> dict:
    s, ver = r.service, r.version
    full = v.field_full()
    values = F.strip_hidden(ver.definition, r.values or {}, v.roles, full=full)
    st = status_info(s, r.status)
    return {"uuid": str(r.uuid), "reference_no": r.reference_no, "title": r.title, "status": r.status,
            "status_label": st["label"], "status_tone": st["tone"], "closed": st["closed"],
            "service": {"code": s.code, "name": s.name, "icon": s.icon},
            "requester_name": r.requester_name, "requester_employee_id": r.requester_employee_id,
            "version": ver.version, "values": values,
            "labels": {k: labels[k] for k in _keys_for(ver.definition, values) if k in labels},
            "created_at": iso_utc(r.created_at), "submitted_at": iso_utc(r.submitted_at),
            "updated_at": iso_utc(r.updated_at), "closed_at": iso_utc(r.closed_at),
            "mine": is_requester(v, r)}


def _keys_for(definition: dict, values: dict) -> list[str]:
    acc: dict[str, set] = {}
    _collect_ids(definition, values, acc)
    out = []
    for kind, ids in acc.items():
        prefix = "master" if kind.startswith("master:") else kind
        out += [f"{prefix}:{i}" for i in ids]
    return out


def record_detail(db: Session, v: Viewer, r: CustomFormRecord) -> dict:
    s, ver = r.service, r.version
    full = v.field_full()
    labels = resolve_labels(db, r.company_id, [(ver.definition, r.values or {})])
    row = record_row(v, r, labels)
    values = row["values"]
    # fresh links for files
    from modules.documents import service as docs
    files = {}
    for f in F.input_fields(ver.definition):
        if f["type"] == "file" and values.get(f["key"]):
            files[f["key"]] = docs.attachments_with_links(db, r.company_id, values[f["key"]])
    row["files"] = files
    handler = v.can_handle(s)
    mine = is_requester(v, r)
    definition = F.visible_definition(ver.definition, v.roles, full=full)
    if not ((r.status == "draft" and mine) or (handler and r.status not in CLOSED_SYSTEM and r.status != "draft")):
        for f in definition["fields"]:
            if f.get("access") == "edit":
                f["access"] = "view"
    row.update({
        "definition": definition,
        "version_is_current": ver.version == s.current_version,
        "service": {**row["service"], "uuid": str(s.uuid), "needs_approval": s.needs_approval,
                    "allow_drafts": s.allow_drafts},
        "can_edit": (r.status == "draft" and mine) or (handler and r.status not in CLOSED_SYSTEM and r.status != "draft"),
        "can_submit": r.status == "draft" and mine,
        "can_delete": r.status == "draft" and mine,
        "can_cancel": can_cancel(v, r),
        "can_change_status": handler and _is_work_status(s, r.status),
        "can_comment": r.status != "draft",
        "is_handler": handler,
        "statuses": [status_info(s, x["key"]) for x in s.statuses or []],
        "activity": activity(db, r),
        "approval": approval_steps(db, r),
    })
    return row


def activity(db: Session, r: CustomFormRecord) -> list[dict]:
    rows = db.query(CustomFormComment).filter(CustomFormComment.company_id == r.company_id,
                                              CustomFormComment.record_id == r.id).order_by(CustomFormComment.id).all()
    from modules.documents import service as docs
    ctx = context.current()
    out = []
    for c in rows:
        data = dict(c.data or {})
        if data.get("to"):
            data["to_label"] = status_info(r.service, data["to"])["label"]
        if data.get("from"):
            data["from_label"] = status_info(r.service, data["from"])["label"]
        out.append({"uuid": str(c.uuid), "kind": c.kind, "author_name": c.author_name, "body": c.body,
                    "attachments": docs.attachments_with_links(db, r.company_id, c.attachments) if c.attachments else [],
                    "data": data, "mine": c.author_employee_id is not None and c.author_employee_id == ctx.employee_id,
                    "created_at": iso_utc(c.created_at)})
    return out


def approval_steps(db: Session, r: CustomFormRecord) -> dict | None:
    if not r.workflow_instance_id:
        return None
    from core.workflow.models import WorkflowInstance, WorkflowTask
    inst = db.query(WorkflowInstance).filter(WorkflowInstance.company_id == r.company_id,
                                             WorkflowInstance.id == r.workflow_instance_id).first()
    if inst is None:
        return None
    tasks = db.query(WorkflowTask).filter(WorkflowTask.company_id == r.company_id,
                                          WorkflowTask.instance_id == inst.id).order_by(WorkflowTask.id).all()
    return {"uuid": str(inst.uuid), "status": inst.status, "reference_no": inst.reference_no,
            "result_note": inst.result_note,
            "tasks": [{"step_name": t.step_name, "approver_name": t.approver_name, "status": t.status,
                       "comment": t.comment, "decided_by_name": t.decided_by_name, "decided_at": iso_utc(t.decided_at),
                       "created_at": iso_utc(t.created_at)} for t in tasks]}


# ── Export ──────────────────────────────────────────────────────────

def export(db: Session, v: Viewer, s: CustomService, rows: list[CustomFormRecord], fmt: str) -> tuple[bytes, str, str]:
    from zoneinfo import ZoneInfo
    from core import csv_safe
    from core.timeutil import effective_timezone
    tz = ZoneInfo(effective_timezone(v.emp))
    ver = current_version(db, s)
    full = v.field_full()
    cols = [f for f in F.input_fields(ver.definition) if F.access_for(f, v.roles, full=full) != "hidden"]
    defs = {}
    pairs = []
    for r in rows:
        defs[r.version_id] = r.version.definition
        pairs.append((r.version.definition, r.values or {}))
    labels = resolve_labels(db, s.company_id, pairs)
    header = ["Reference", "Status", "Requested by", "Submitted on", *[f["label"] for f in cols]]
    data = []
    for r in rows:
        own = {f["key"]: f for f in F.input_fields(r.version.definition)}
        vals = F.strip_hidden(r.version.definition, r.values or {}, v.roles, full=full)
        line = [r.reference_no or "Draft", status_info(s, r.status)["label"], r.requester_name or "",
                r.submitted_at.astimezone(tz).strftime("%d %b %Y %H:%M") if r.submitted_at else ""]
        for f in cols:
            ff = own.get(f["key"], f)
            line.append(display_text(ff, vals.get(f["key"]), labels, tz))
        data.append(line)
    stamp = datetime.now(tz).strftime("%Y%m%d")
    name = f"{s.code}-{stamp}"
    audit.event(db, "export.downloaded", "custom_services", s.id, {"format": fmt, "rows": len(rows)},
                entity_uuid=str(s.uuid), entity_label=s.name)
    if fmt == "xlsx":
        from openpyxl import Workbook
        from openpyxl.styles import Font
        wb = Workbook()
        ws = wb.active
        ws.title = s.name[:31] or "Records"
        ws.append(header)
        for c in ws[1]:
            c.font = Font(bold=True)
        for line in data:
            ws.append([("'" + x) if isinstance(x, str) and x.startswith(("=", "+", "-", "@")) else x for x in line])
        for i, h in enumerate(header, start=1):
            width = max([len(str(h))] + [len(str(line[i - 1])) for line in data[:200]]) + 2
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = min(width, 50)
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue(), f"{name}.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    buf = io.StringIO()
    w = csv_safe.writer(buf)
    w.writerow(header)
    w.writerows(data)
    return ("﻿" + buf.getvalue()).encode("utf-8"), f"{name}.csv", "text/csv; charset=utf-8"


# ── Saved views ─────────────────────────────────────────────────────

def views(db: Session, v: Viewer, s: CustomService) -> list[CustomFormView]:
    return db.query(CustomFormView).filter(CustomFormView.company_id == v.company_id, CustomFormView.service_id == s.id,
                                           or_(CustomFormView.owner_key == v.owner_key,
                                               CustomFormView.shared.is_(True))).order_by(CustomFormView.name).all()


def save_view(db: Session, v: Viewer, s: CustomService, data: dict, row: CustomFormView | None = None) -> CustomFormView:
    name = (data.get("name") or "").strip()
    if not name:
        raise BadRequest("Give the view a name.")
    if len(name) > 100:
        raise BadRequest("The name can be at most 100 characters.")
    if row is None:
        row = CustomFormView(company_id=v.company_id, service_id=s.id, owner_key=v.owner_key)
        db.add(row)
    elif row.owner_key != v.owner_key:
        raise Forbidden("Only the person who saved this view can change it.")
    row.name = name
    row.filters = data.get("filters") or {}
    row.columns = [str(c) for c in (data.get("columns") or [])][:40]
    row.shared = bool(data.get("shared")) and v.can_handle(s)
    db.flush()
    return row


# ── Menu and catalogue ──────────────────────────────────────────────

def usable_services(db: Session, v: Viewer) -> list[tuple[CustomService, bool, bool]]:
    rows = db.query(CustomService).filter(CustomService.company_id == v.company_id,
                                          CustomService.archived_at.is_(None)).order_by(
        CustomService.sort_order, CustomService.name).all()
    out = []
    for s in rows:
        sub, handle = v.can_submit(s), v.can_handle(s)
        if s.active and (sub or handle):
            out.append((s, sub, handle))
    return out


def lookup_options(db: Session, company_id: int, kind: str, q: str | None, ids: list[int] | None,
                   limit: int = 20) -> list[dict]:
    if kind == "employee":
        from modules.employees.models import Employee
        query = db.query(Employee).filter(Employee.company_id == company_id)
        if ids:
            query = query.filter(Employee.id.in_(ids))
        else:
            query = query.filter(Employee.active.is_(True))
            if q:
                like = f"%{q.strip()}%"
                query = query.filter(or_(func.concat_ws(" ", Employee.first_name, Employee.last_name).ilike(like),
                                         Employee.employee_code.ilike(like), Employee.email.ilike(like)))
        rows = query.order_by(Employee.first_name, Employee.last_name).limit(limit).all()
        return [{"value": e.id, "label": f"{e.first_name} {e.last_name}".strip(),
                 "hint": " · ".join(x for x in (e.employee_code,) if x) or None} for e in rows]
    if kind == "role":
        from modules.roles.models import Role
        rows = db.query(Role).filter(Role.company_id == company_id).order_by(Role.display_name).all()
        return [{"value": r.role_name, "label": r.display_name} for r in rows]
    if kind == "team":
        from modules.organization.models import Team
        query = db.query(Team).filter(Team.company_id == company_id)
        if ids:
            query = query.filter(Team.id.in_(ids))
        elif q:
            query = query.filter(Team.name.ilike(f"%{q.strip()}%"))
        return [{"value": t.id, "label": t.name} for t in query.order_by(Team.name).limit(limit).all()]
    if kind in ("department", "location"):
        model, _ = _entity_model(kind)
        name_col = model.department_name if kind == "department" else model.name
        query = db.query(model.id, name_col).filter(model.company_id == company_id)
        if ids:
            query = query.filter(model.id.in_(ids))
        else:
            if hasattr(model, "active"):
                query = query.filter(model.active.is_(True))
            if q:
                query = query.filter(name_col.ilike(f"%{q.strip()}%"))
        return [{"value": i, "label": n} for i, n in query.order_by(name_col).limit(limit).all()]
    raise BadRequest("Unknown kind of list.")


# ── Live form logic for the browser ─────────────────────────────────

def evaluate(db: Session, v: Viewer, s: CustomService, submitted: dict, r: CustomFormRecord | None = None) -> dict:
    """Which fields show and what the formulas give for values typed so far (lenient: values
    that are not valid yet are ignored)."""
    ver = r.version if r is not None else current_version(db, s)
    today = today_for_company_id(db, s.company_id)
    requester = _requester_vars_for_record(db, r, today) if (r is not None and not is_requester(v, r)) \
        else v.requester_vars(today)
    fields = {f["key"]: f for f in F.input_fields(ver.definition)}
    values: dict = {}
    for key, raw in (submitted or {}).items():
        f = fields.get(key)
        if f is None or f["type"] == "formula" or F._empty(raw):
            continue
        try:
            if f["type"] == "table":
                rows = []
                for row in raw if isinstance(raw, list) else []:
                    clean = {}
                    for c in f.get("columns") or []:
                        if c["type"] == "formula" or F._empty((row or {}).get(c["key"])):
                            continue
                        try:
                            clean[c["key"]] = F.clean_value(c, row[c["key"]], lookups=None)
                        except Exception:
                            continue
                    rows.append(clean)
                values[key] = rows
            elif f["type"] == "file":
                values[key] = raw if isinstance(raw, list) else [raw]
            else:
                values[key] = F.clean_value(f, raw, lookups=None)
        except Exception:
            continue
    out, visible = F.compute(ver.definition, values, requester, today)
    computed = {}
    for key, f in fields.items():
        if f["type"] == "formula":
            computed[key] = out.get(key)
        elif f["type"] == "table" and any(c["type"] == "formula" for c in f.get("columns") or []):
            computed[key] = out.get(key) or []
    return {"visible": sorted(visible), "computed": computed}
