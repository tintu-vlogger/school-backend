"""HR process builder: company-defined change processes and their requests.

A process type says which employee fields change (job fields such as location or
designation, the probation and confirmation dates, and employee custom fields), which one
is the key field, the stage it moves people to (confirmation), the letters to issue and
whether it may be revoked. Every request goes through the approval engine as the
`hr.process` request type (flows can target one process with
`request.process_code == 'TRF'`).

On approval the change is applied when its effective date has come, otherwise it waits as
"scheduled" and the periodic job applies it on the day. Applying writes one entry in the
employee's job history and keeps the previous values on the request, so a revoke puts
them back (and writes its own history entry).
"""
from __future__ import annotations

import uuid as uuid_mod
from datetime import date, datetime, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from core import audit, context, custom_fields
from core.errors import BadRequest, NotFound
from modules.hr_processes.models import HrProcessRequest, HrProcessType

REQUEST_TYPE = "hr.process"
EXTRA_FIELDS = {"probation_end_date": "Probation ends", "confirmation_date": "Confirmation date"}
ID_FIELDS = {"designation_id", "department_id", "location_id", "legal_entity_id", "grade_id", "cost_centre_id",
             "employment_type_id", "reporting_manager_id", "secondary_manager_id"}
DATE_FIELDS = set(EXTRA_FIELDS)
TARGET_STATUSES = {"confirmed": "Confirmed"}
MAX_FIELDS = 10
OPEN_STATUSES = ("pending", "scheduled", "applied")
STATUS_LABELS = {"pending": "Waiting for approval", "scheduled": "Scheduled", "applied": "Applied",
                 "rejected": "Rejected", "cancelled": "Withdrawn", "revoked": "Revoked"}

DEFAULT_TYPES = [
    # code, name, change type, fields, key field, target status, description
    ("TRF", "Transfer", "transfer", ["location_id", "department_id", "reporting_manager_id", "cost_centre_id"],
     "location_id", None, "Move someone to another location, with their team and manager if those change too."),
    ("PRM", "Promotion", "promotion", ["designation_id", "grade_id"], "designation_id", None,
     "A new designation, usually with a higher grade."),
    ("CNF", "Confirmation", "confirmation", ["grade_id"], None, "confirmed",
     "Confirm employment at the end of probation."),
    ("DPT", "Department change", "department_change", ["department_id", "reporting_manager_id", "cost_centre_id"],
     "department_id", None, "Move someone to another department."),
    ("MGR", "Manager change", "manager_change", ["reporting_manager_id", "secondary_manager_id"],
     "reporting_manager_id", None, "Change who someone reports to."),
]


def _emp_service():
    from modules.employees import service
    return service


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today(db: Session, company_id: int) -> date:
    from core.timeutil import today_for_company_id
    return today_for_company_id(db, company_id)


def actor() -> str | None:
    return context.current().actor_name


# ── Fields ──────────────────────────────────────────────────────────

def field_catalog(db: Session, company_id: int) -> dict[str, dict]:
    """Every employee field a process may change: {key: {label, kind}}."""
    es = _emp_service()
    kinds = {"designation_id": "designation", "department_id": "department", "location_id": "location",
             "legal_entity_id": "legal_entity", "grade_id": "grade", "cost_centre_id": "cost_centre",
             "employment_type_id": "employment_type", "reporting_manager_id": "employee",
             "secondary_manager_id": "employee", "work_mode": "work_mode", "notice_period_days": "number"}
    out = {k: {"label": label, "kind": kinds.get(k, "text")} for k, label in es.JOB_FIELDS.items()}
    out.update({k: {"label": label, "kind": "date"} for k, label in EXTRA_FIELDS.items()})
    for d in custom_fields.definitions(db, company_id, "employee"):
        out[f"custom.{d.key}"] = {"label": d.label, "kind": "custom", "field_type": d.field_type,
                                  "master_type_key": d.master_type_key, "options": d.options or []}
    return out


def _as_date(value):
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise BadRequest("Dates should be in the form YYYY-MM-DD.")


def _plain(v):
    return v.isoformat() if isinstance(v, (date, datetime)) else v


def clean_values(db: Session, company_id: int, ptype: HrProcessType, emp, values: dict) -> dict:
    """Check new values against the process and the employee; returns JSON-safe values."""
    values = dict(values or {})
    unknown = [k for k in (values or {}) if k not in (ptype.fields or [])]
    if unknown:
        raise BadRequest(f"{ptype.name} doesn't change “{unknown[0]}”.")
    if ptype.key_field and values.get(ptype.key_field) in (None, ""):
        label = field_catalog(db, company_id).get(ptype.key_field, {}).get("label", ptype.key_field)
        raise BadRequest(f"Give the new {label.lower()}.")
    job, custom = {}, {}
    for k, v in values.items():
        if k.startswith("custom."):
            custom[k[7:]] = v
            continue
        if v in ("",):
            v = None
        if k in ID_FIELDS and v is not None:
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise BadRequest("Pick a value from the list.")
        elif k == "notice_period_days" and v is not None:
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise BadRequest("The notice period must be a whole number of days.")
            if v < 0 or v > 365:
                raise BadRequest("The notice period must be between 0 and 365 days.")
        elif k == "work_mode" and v is not None and v not in _emp_service().WORK_MODES:
            raise BadRequest("Pick a work mode from the list.")
        elif k in DATE_FIELDS:
            v = _as_date(v)
        job[k] = v
    if job:
        _emp_service()._prepare(db, company_id, job, emp)
    out = {k: _plain(v) for k, v in job.items()}
    if custom:
        cleaned = custom_fields.clean_values(db, company_id, "employee", custom, emp.custom, partial=True)
        out.update({f"custom.{k}": _plain(v) for k, v in cleaned.items()})
    return out


def current_values(emp, fields: list[str]) -> dict:
    out = {}
    for f in fields:
        if f.startswith("custom."):
            out[f] = (emp.custom or {}).get(f[7:])
        else:
            out[f] = _plain(getattr(emp, f, None))
    return out


def readable(db: Session, company_id: int, pairs: list[dict]) -> list[dict]:
    """Turn [{field: value}] dicts into display labels; returns the same shape with names."""
    from core.masters import labels_for
    from modules.employees import profile
    as_changes = [{f: {"old": v, "new": None} for f, v in p.items()} for p in pairs]
    names = profile.resolve_values(db, company_id, as_changes)
    catalog = field_catalog(db, company_id)
    master_ids = set()
    for p in pairs:
        for f, v in p.items():
            c = catalog.get(f) or {}
            if c.get("master_type_key") and v not in (None, ""):
                master_ids.update(v if isinstance(v, list) else [v])
    mlabels = labels_for(db, company_id, master_ids) if master_ids else {}
    out = []
    for p in pairs:
        row = {}
        for f, v in p.items():
            c = catalog.get(f) or {}
            if f.startswith("custom."):
                if v in (None, ""):
                    row[f] = None
                elif c.get("master_type_key"):
                    vals = v if isinstance(v, list) else [v]
                    row[f] = ", ".join(mlabels.get(int(x), str(x)) if str(x).isdigit() else str(x) for x in vals)
                elif isinstance(v, bool):
                    row[f] = "Yes" if v else "No"
                elif isinstance(v, list):
                    row[f] = ", ".join(str(x) for x in v)
                else:
                    row[f] = str(v)
            else:
                row[f] = profile._readable(f, v, names)
        out.append(row)
    return out


# ── Process types ───────────────────────────────────────────────────

def ensure_defaults(db: Session, company_id: int) -> None:
    have = {c for (c,) in db.query(HrProcessType.code).filter(HrProcessType.company_id == company_id).all()}
    for i, (code, name, ctype, fields, key, target, desc) in enumerate(DEFAULT_TYPES):
        if code in have:
            continue
        db.add(HrProcessType(company_id=company_id, code=code, name=name, change_type=ctype, fields=fields,
                             key_field=key, target_status=target, description=desc, is_system=True,
                             sort_order=(i + 1) * 10))
    db.flush()


def list_types(db: Session, company_id: int, active_only: bool = False) -> list[HrProcessType]:
    q = db.query(HrProcessType).filter(HrProcessType.company_id == company_id)
    if active_only:
        q = q.filter(HrProcessType.active.is_(True))
    return q.order_by(HrProcessType.sort_order, HrProcessType.name).all()


def get_type(db: Session, company_id: int, uuid) -> HrProcessType:
    row = db.query(HrProcessType).filter(HrProcessType.company_id == company_id, HrProcessType.uuid == uuid).first()
    if row is None:
        raise NotFound("Process")
    return row


def _letter_templates(db: Session, company_id: int, uuids: list) -> list:
    if not uuids:
        return []
    try:
        from modules.documents.models import LetterTemplate
    except ImportError:  # pragma: no cover
        return []
    clean = []
    for u in uuids:
        try:
            clean.append(uuid_mod.UUID(str(u)))
        except ValueError:
            raise BadRequest("A letter template was not found.")
    rows = db.query(LetterTemplate).filter(LetterTemplate.company_id == company_id, LetterTemplate.uuid.in_(clean)).all()
    return rows


def save_type(db: Session, company_id: int, data: dict, row: HrProcessType | None = None) -> HrProcessType:
    es = _emp_service()
    catalog = field_catalog(db, company_id)
    merged = {
        "code": row.code if row else None, "name": row.name if row else None,
        "description": row.description if row else None, "change_type": row.change_type if row else "other",
        "fields": row.fields if row else [], "key_field": row.key_field if row else None,
        "target_status": row.target_status if row else None,
        "letter_template_uuids": row.letter_template_uuids if row else [],
        "revocable": row.revocable if row else True, "needs_approval": row.needs_approval if row else True,
        "active": row.active if row else True, "sort_order": row.sort_order if row else 100,
    }
    merged.update({k: v for k, v in data.items() if k in merged})
    code = (merged["code"] or "").strip().upper()
    if not code or len(code) > 10 or not code.replace("_", "").isalnum():
        raise BadRequest("Give the process a short code of up to 10 letters or digits, e.g. TRF.")
    clash = db.query(HrProcessType.id).filter(HrProcessType.company_id == company_id, HrProcessType.code == code)
    if row is not None:
        clash = clash.filter(HrProcessType.id != row.id)
    if clash.first():
        raise BadRequest("Another process already uses this code.")
    name = (merged["name"] or "").strip()
    if not name:
        raise BadRequest("Give the process a name.")
    fields = list(dict.fromkeys(merged["fields"] or []))
    bad = [f for f in fields if f not in catalog]
    if bad:
        raise BadRequest(f"“{bad[0]}” is not a field a process can change.")
    if len(fields) > MAX_FIELDS:
        raise BadRequest(f"A process can change at most {MAX_FIELDS} fields.")
    target = merged["target_status"] or None
    if target and target not in TARGET_STATUSES:
        raise BadRequest("Pick a stage from the list.")
    if not fields and not target:
        raise BadRequest("Pick at least one field the process changes.")
    key = merged["key_field"] or None
    if key and key not in fields:
        raise BadRequest("The key field must be one of the fields the process changes.")
    ctype = merged["change_type"] or "other"
    if ctype not in es.CHANGE_TYPES or ctype in ("joining", "rehire", "exit", "status", "revoke"):
        raise BadRequest("Pick how the change shows in the job history.")
    letters = [str(u) for u in (merged["letter_template_uuids"] or [])]
    found = {str(t.uuid) for t in _letter_templates(db, company_id, letters)}
    if any(u not in found for u in letters):
        raise BadRequest("A letter template was not found.")
    if row is None:
        row = HrProcessType(company_id=company_id)
        db.add(row)
    row.code, row.name, row.description = code, name[:100], (merged["description"] or None)
    row.change_type, row.fields, row.key_field, row.target_status = ctype, fields, key, target
    row.letter_template_uuids = letters
    row.revocable, row.needs_approval = bool(merged["revocable"]), bool(merged["needs_approval"])
    row.active, row.sort_order = bool(merged["active"]), int(merged["sort_order"] or 0)
    db.flush()
    return row


# ── Requests ────────────────────────────────────────────────────────

def get_request(db: Session, company_id: int, uuid) -> HrProcessRequest:
    row = (db.query(HrProcessRequest).options(joinedload(HrProcessRequest.process_type))
           .filter(HrProcessRequest.company_id == company_id, HrProcessRequest.uuid == uuid).first())
    if row is None:
        raise NotFound("Change request")
    return row


def _employee(db: Session, company_id: int, employee_id):
    es = _emp_service()
    emp = es.get_by_id(db, company_id, int(employee_id)) if str(employee_id).isdigit() else None
    if emp is None:
        raise BadRequest("Employee not found.")
    return emp


def _summary(db: Session, company_id: int, ptype: HrProcessType, emp, new_values: dict, effective: date) -> list[str]:
    catalog = field_catalog(db, company_id)
    cur = current_values(emp, list(new_values))
    old_r, new_r = readable(db, company_id, [cur, new_values])
    lines = [f"{catalog.get(f, {}).get('label', f)}: {old_r.get(f) or 'Not set'} → {new_r.get(f) or 'cleared'}"
             for f in new_values]
    if ptype.target_status:
        lines.append(f"Stage: {TARGET_STATUSES[ptype.target_status]}")
    lines.append(f"Takes effect on {effective.isoformat()}")
    return lines


def create_requests(db: Session, company_id: int, ptype: HrProcessType, employee_ids: list, effective_date: date,
                    values: dict, reason: str | None, remarks: str | None) -> list[HrProcessRequest]:
    """Raise the change for one or many employees. Every employee is checked first, so
    either all requests are raised or none."""
    from core.workflow import service as wf
    es = _emp_service()
    if not ptype.active:
        raise BadRequest("This process is switched off.")
    ids = list(dict.fromkeys(int(i) for i in employee_ids or []))
    if not ids:
        raise BadRequest("Pick at least one employee.")
    if len(ids) > 200:
        raise BadRequest("Raise the change for up to 200 people at a time.")
    if not (reason or "").strip():
        raise BadRequest("Say why the change is made.")
    prepared = []
    for eid in ids:
        emp = _employee(db, company_id, eid)
        who = es.display_name(emp)
        if emp.status in es.INACTIVE_STATUSES:
            raise BadRequest(f"{who} has left.")
        if not emp.date_of_joining:
            raise BadRequest(f"{who} has no joining date yet.")
        if effective_date < emp.date_of_joining:
            raise BadRequest(f"The change can't take effect before {who} joined.")
        if ptype.target_status and ptype.target_status != emp.status \
                and ptype.target_status not in es.TRANSITIONS.get(emp.status, set()):
            raise BadRequest(f"{who} is {es.STATUSES.get(emp.status, emp.status).lower()} and can't be moved to "
                             f"{TARGET_STATUSES[ptype.target_status].lower()}.")
        if ptype.target_status and emp.status == ptype.target_status:
            raise BadRequest(f"{who} is already {TARGET_STATUSES[ptype.target_status].lower()}.")
        clean = clean_values(db, company_id, ptype, emp, values)
        if not ptype.target_status:
            cur = current_values(emp, list(clean))
            if not clean or all(cur.get(k) == v for k, v in clean.items()):
                raise BadRequest(f"Nothing is different from {who}'s current details.")
        dup = (db.query(HrProcessRequest.id)
               .filter(HrProcessRequest.company_id == company_id, HrProcessRequest.employee_id == emp.id,
                       HrProcessRequest.process_type_id == ptype.id,
                       HrProcessRequest.effective_date == effective_date,
                       HrProcessRequest.status.in_(OPEN_STATUSES)).first())
        if dup:
            raise BadRequest(f"{who} already has a {ptype.name.lower()} for this date.")
        prepared.append((emp, clean))
    batch = uuid_mod.uuid4() if len(prepared) > 1 else None
    out = []
    for emp, clean in prepared:
        req = HrProcessRequest(company_id=company_id, process_type_id=ptype.id, employee_id=emp.id, batch_id=batch,
                               effective_date=effective_date, new_values=clean, reason=(reason or "").strip()[:300],
                               remarks=(remarks or "").strip() or None, status="pending", created_by_name=actor())
        req.process_type = ptype
        db.add(req)
        db.flush()
        payload = {"process_code": ptype.code, "process_name": ptype.name, "hr_process_request_id": req.id,
                   "employee_id": emp.id, "employee_name": es.display_name(emp), "manager_id": emp.reporting_manager_id,
                   "department_id": emp.department_id, "location_id": emp.location_id,
                   "effective_date": effective_date.isoformat(), "fields": list(clean),
                   "summary": _summary(db, company_id, ptype, emp, clean, effective_date)}
        if not ptype.needs_approval:
            audit.event(db, "hr_process.raised", "hr_process_requests", req.id,
                        {"process": ptype.name, "employee": es.display_name(emp)}, entity_uuid=str(req.uuid),
                        entity_label=f"{ptype.name}: {es.display_name(emp)}")
            approve(db, req)
        else:
            inst = wf.submit(db, company_id, REQUEST_TYPE, payload, f"{ptype.name}: {es.display_name(emp)}",
                             subject_type="hr_process_request", subject_id=req.id)
            req.workflow_instance_id = inst.id
        out.append(req)
    db.flush()
    return out


def for_instance(db: Session, instance) -> HrProcessRequest | None:
    rid = instance.subject_id if instance.subject_type == "hr_process_request" else None
    rid = rid or (instance.payload or {}).get("hr_process_request_id")
    if not rid:
        return None
    return (db.query(HrProcessRequest).options(joinedload(HrProcessRequest.process_type))
            .filter(HrProcessRequest.company_id == instance.company_id, HrProcessRequest.id == int(rid)).first())


def approve(db: Session, req: HrProcessRequest) -> None:
    """Approved: issue the letters, then apply now or wait for the effective date."""
    if req.status not in ("pending",):
        return
    req.status = "scheduled"
    _issue_letters(db, req)
    if req.effective_date <= _today(db, req.company_id):
        apply(db, req)


def close(db: Session, req: HrProcessRequest, status: str) -> None:
    if req.status == "pending":
        req.status = status


def apply(db: Session, req: HrProcessRequest) -> None:
    """Put the new values on the employee and write the job history entry."""
    from modules.employees.models import JobChange
    es = _emp_service()
    if req.status != "scheduled":
        return
    emp = es.get_by_id(db, req.company_id, req.employee_id)
    if emp is None or emp.status in es.INACTIVE_STATUSES:
        req.status = "cancelled"
        req.note = "Not applied: the employee had left by the effective date."
        return
    ptype = req.process_type
    new = dict(req.new_values or {})
    if ptype.target_status and emp.status != ptype.target_status:
        new["status"] = ptype.target_status
        if ptype.target_status == "confirmed" and "confirmation_date" not in new:
            new["confirmation_date"] = req.effective_date.isoformat()
    old = current_values(emp, list(new))
    changes = {}
    custom = dict(emp.custom or {})
    for f, v in new.items():
        if old.get(f) == v:
            continue
        changes[f] = {"old": old.get(f), "new": v}
        if f.startswith("custom."):
            if v is None:
                custom.pop(f[7:], None)
            else:
                custom[f[7:]] = v
        else:
            setattr(emp, f, _as_date(v) if f in DATE_FIELDS else v)
    if any(f.startswith("custom.") for f in changes):
        emp.custom = custom
    if "status" in changes:
        emp.active = True
    req.old_values = old
    jc = JobChange(company_id=req.company_id, employee_id=emp.id, change_type=ptype.change_type,
                   effective_date=req.effective_date, changes=changes, reason=req.reason,
                   remarks=f"{ptype.name} ({ptype.code})" + (f". {req.remarks}" if req.remarks else ""),
                   status="applied", applied_at=_now(), created_by_name=req.created_by_name)
    db.add(jc)
    db.flush()
    req.job_change_id = jc.id
    req.status = "applied"
    req.applied_at = _now()
    audit.event(db, "hr_process.applied", "hr_process_requests", req.id,
                {"process": ptype.name, "employee": es.display_name(emp), "fields": list(changes)},
                company_id=req.company_id, entity_uuid=str(req.uuid), entity_label=f"{ptype.name}: {es.display_name(emp)}")
    try:
        from modules.notifications.service import notify
        notify(db, req.company_id, [emp.id], kind="hr_process", title=f"Your {ptype.name.lower()} has taken effect",
               body="Your details have been updated. See your profile for the history.",
               link="/dashboard/profile")
    except Exception:  # a notification problem never blocks the change
        pass


def _issue_letters(db: Session, req: HrProcessRequest) -> None:
    uuids = list(req.process_type.letter_template_uuids or [])
    if not uuids:
        return
    from modules.documents import service as docs
    emp = _emp_service().get_by_id(db, req.company_id, req.employee_id)
    made, skipped = [], []
    templates = {str(t.uuid): t for t in _letter_templates(db, req.company_id, uuids)}
    for u in uuids:
        t = templates.get(str(u))
        if t is None or not t.active:
            skipped.append("a letter template that no longer exists or is switched off")
            continue
        try:
            with db.begin_nested():
                letter = docs.generate_letter(db, req.company_id, t, emp, {"effective_date": req.effective_date.isoformat()},
                                              workflow_instance_id=req.workflow_instance_id)
                made.append(str(letter.uuid))
        except Exception as exc:  # never block the change because of a letter
            detail = getattr(exc, "detail", None) or str(exc)
            skipped.append(f"{t.name} ({str(detail)[:120]})")
    req.letter_uuids = made
    if skipped:
        req.note = "Letters not issued: " + "; ".join(skipped)


def apply_due(db: Session, company_id: int | None = None) -> int:
    """Apply approved changes whose date has come. Idempotent."""
    q = (db.query(HrProcessRequest).options(joinedload(HrProcessRequest.process_type))
         .filter(HrProcessRequest.status == "scheduled"))
    if company_id is not None:
        q = q.filter(HrProcessRequest.company_id == company_id)
    n = 0
    for req in q.order_by(HrProcessRequest.effective_date, HrProcessRequest.id).limit(500).all():
        if req.effective_date <= _today(db, req.company_id):
            apply(db, req)
            n += 1
    return n


def revoke(db: Session, req: HrProcessRequest, reason: str | None) -> HrProcessRequest:
    """Undo an approved change: a scheduled one simply won't happen; an applied one puts
    the previous values back and records that in the job history."""
    from modules.employees.models import JobChange
    es = _emp_service()
    if not req.process_type.revocable:
        raise BadRequest("This process can't be revoked.")
    if req.status not in ("scheduled", "applied"):
        raise BadRequest("Only approved changes can be revoked.")
    emp = es.get_by_id(db, req.company_id, req.employee_id)
    if req.status == "applied" and emp is not None:
        later = (db.query(HrProcessRequest.id)
                 .filter(HrProcessRequest.company_id == req.company_id, HrProcessRequest.employee_id == req.employee_id,
                         HrProcessRequest.status == "applied", HrProcessRequest.id != req.id,
                         HrProcessRequest.applied_at > req.applied_at).first())
        if later:
            raise BadRequest("A later change for this person has been applied. Revoke that one first.")
        old = dict(req.old_values or {})
        cur = current_values(emp, list(old))
        changes = {}
        custom = dict(emp.custom or {})
        for f, v in old.items():
            if cur.get(f) == v:
                continue
            changes[f] = {"old": cur.get(f), "new": v}
            if f.startswith("custom."):
                if v is None:
                    custom.pop(f[7:], None)
                else:
                    custom[f[7:]] = v
            else:
                setattr(emp, f, _as_date(v) if f in DATE_FIELDS else v)
        if any(f.startswith("custom.") for f in changes):
            emp.custom = custom
        if "status" in changes:
            emp.active = emp.status not in es.INACTIVE_STATUSES
        jc = JobChange(company_id=req.company_id, employee_id=emp.id, change_type="revoke",
                       effective_date=_today(db, req.company_id), changes=changes,
                       reason=(reason or "").strip()[:300] or f"{req.process_type.name} revoked",
                       remarks=f"Revokes the {req.process_type.name.lower()} dated {req.effective_date.isoformat()}",
                       status="applied", applied_at=_now(), created_by_name=actor())
        db.add(jc)
        db.flush()
        req.revoke_job_change_id = jc.id
        if req.job_change_id:
            orig = db.query(JobChange).filter(JobChange.id == req.job_change_id).first()
            if orig is not None:
                orig.remarks = ((orig.remarks or "") + " (revoked)").strip()
    req.status = "revoked"
    req.revoked_at = _now()
    req.revoked_by_name = actor()
    req.revoke_reason = (reason or "").strip()[:300] or None
    audit.event(db, "hr_process.revoked", "hr_process_requests", req.id,
                {"process": req.process_type.name, "employee": es.display_name(emp), "reason": req.revoke_reason},
                company_id=req.company_id, entity_uuid=str(req.uuid),
                entity_label=f"{req.process_type.name}: {es.display_name(emp)}")
    db.flush()
    return req


def list_query(db: Session, company_id: int, *, status: str | None = None, type_uuid: str | None = None,
               q: str | None = None, employee_id: int | None = None):
    from modules.employees.models import Employee
    query = (db.query(HrProcessRequest).options(joinedload(HrProcessRequest.process_type))
             .join(Employee, Employee.id == HrProcessRequest.employee_id)
             .filter(HrProcessRequest.company_id == company_id))
    if status:
        query = query.filter(HrProcessRequest.status == status)
    if type_uuid:
        query = query.join(HrProcessType, HrProcessType.id == HrProcessRequest.process_type_id) \
            .filter(HrProcessType.uuid == type_uuid)
    if employee_id:
        query = query.filter(HrProcessRequest.employee_id == employee_id)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Employee.first_name.ilike(like), Employee.last_name.ilike(like),
                                 Employee.employee_code.ilike(like), HrProcessRequest.reason.ilike(like)))
    return query.order_by(HrProcessRequest.created_at.desc(), HrProcessRequest.id.desc())
