"""Employee rules: create, update, reporting lines, timezones, statutory IDs, duplicate
checks, effective-dated job changes, lifecycle stages, archive and rehire."""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.orm.attributes import flag_modified

from core import context, custom_fields
from core.errors import BadRequest, NotFound
from core.security import hash_password
from core.timeutil import is_valid_timezone
from modules.employees import sensitive, validators as v
from modules.employees.models import Employee, EmployeeSettings, JobChange
from modules.employees.work_arrangement import DEFAULT_WORK_ARRANGEMENT

BACKGROUND_FIELDS = {"qualification", "experience"}

STATUSES = {
    "pre_joining": "Pre-joining",
    "probation": "On probation",
    "confirmed": "Confirmed",
    "notice": "On notice",
    "exited": "Exited",
    "archived": "Archived",
}
INACTIVE_STATUSES = {"exited", "archived"}
# Allowed moves between lifecycle stages (archive and rehire have their own actions).
TRANSITIONS = {
    "pre_joining": {"probation", "confirmed"},
    "probation": {"confirmed", "notice", "exited"},
    "confirmed": {"notice", "exited", "probation"},
    "notice": {"confirmed", "exited"},
    "exited": set(),
    "archived": set(),
}
WORK_MODES = {"office": "Office", "remote": "Remote", "hybrid": "Hybrid", "field": "Field"}

# Job fields whose changes are kept in the effective-dated history.
JOB_FIELDS = {
    "designation_id": "Designation",
    "department_id": "Department",
    "reporting_manager_id": "Reporting manager",
    "secondary_manager_id": "Secondary manager",
    "location_id": "Location",
    "legal_entity_id": "Legal entity",
    "grade_id": "Grade",
    "employment_type_id": "Employment type",
    "cost_centre_id": "Cost centre",
    "work_mode": "Work mode",
    "notice_period_days": "Notice period",
}
CHANGE_TYPES = {
    "joining": "Joined",
    "transfer": "Transfer",
    "promotion": "Promotion",
    "redesignation": "Designation change",
    "department_change": "Department change",
    "manager_change": "Manager change",
    "entity_transfer": "Company transfer",
    "grade_change": "Grade change",
    "type_change": "Employment type change",
    "status": "Stage change",
    "confirmation": "Confirmation",
    "exit": "Exit",
    "rehire": "Rehired",
    "revoke": "Change revoked",
    "update": "Job details updated",
    "other": "Other change",
}

# Personal fields stored as master value ids, and the list each comes from.
MASTER_FIELDS = {
    "gender_id": "gender",
    "marital_status_id": "marital_status",
    "blood_group_id": "blood_group",
    "nationality_id": "nationality",
    "employment_type_id": "employment_type",
    "employee_status_id": "employee_status",
}
ADDRESS_KEYS = ("line1", "line2", "city", "district", "state_id", "pincode", "country_id")

PLAIN_FIELDS = {
    "greeting", "first_name", "last_name", "middle_name", "preferred_name", "email", "phone", "role", "date_of_birth",
    "marriage_date", "father_name", "spouse_name", "personal_email", "alternate_phone", "work_phone", "extension",
    "passport_expiry", "driving_licence_expiry", "date_of_joining", "probation_end_date", "confirmation_date",
    "notice_period_days", "work_mode", "timezone", "designation_id", "department_id", "reporting_manager_id",
    "secondary_manager_id", "location_id", "legal_entity_id", "grade_id", "cost_centre_id",
    *MASTER_FIELDS,
}


def today() -> date:
    """Today's calendar date in India (dates without time are company-local)."""
    return datetime.now(timezone(timedelta(hours=5, minutes=30))).date()


def actor_name() -> str | None:
    return context.current().actor_name


def base_query(db: Session, company_id: int):
    return (
        db.query(Employee)
        .options(joinedload(Employee.designation), joinedload(Employee.department), joinedload(Employee.reporting_manager),
                 joinedload(Employee.location), joinedload(Employee.legal_entity), joinedload(Employee.grade))
        .filter(Employee.company_id == company_id)
    )


def search(query, term: str | None):
    if term:
        like = f"%{term.strip()}%"
        query = query.filter(or_(
            Employee.first_name.ilike(like),
            Employee.last_name.ilike(like),
            Employee.preferred_name.ilike(like),
            (Employee.first_name + " " + Employee.last_name).ilike(like),
            Employee.email.ilike(like),
            Employee.employee_code.ilike(like),
            Employee.phone.ilike(like),
        ))
    return query


def get(db: Session, company_id: int, uuid) -> Employee:
    emp = base_query(db, company_id).filter(Employee.uuid == uuid).first()
    if not emp:
        raise NotFound("Employee")
    return emp


def get_by_id(db: Session, company_id: int, employee_id: int) -> Employee | None:
    return db.query(Employee).filter(Employee.company_id == company_id, Employee.id == employee_id).first()


def display_name(emp: Employee | None) -> str | None:
    return f"{emp.first_name} {emp.last_name}".strip() if emp else None


# ── Checks ───────────────────────────────────────────────────────────

def _check_manager(db: Session, company_id: int, emp: Employee | None, manager_id: int | None,
                   label: str = "reporting manager") -> None:
    if manager_id is None:
        return
    manager = get_by_id(db, company_id, manager_id)
    if manager is None:
        raise BadRequest(f"The {label} was not found.")
    if emp is not None:
        if manager.id == emp.id:
            raise BadRequest("An employee can't report to themselves.")
        if label != "reporting manager":
            return
        # walk up from the new manager: the employee must not appear (no loops)
        seen, cursor = set(), manager
        while cursor is not None and cursor.reporting_manager_id and cursor.id not in seen:
            seen.add(cursor.id)
            if cursor.reporting_manager_id == emp.id:
                raise BadRequest(f"{manager.first_name} already reports to {emp.first_name}, so this would make a loop.")
            cursor = get_by_id(db, company_id, cursor.reporting_manager_id)


def _check_timezone(tz: str | None) -> str | None:
    tz = (tz or "").strip() or None
    if tz and not is_valid_timezone(tz):
        raise BadRequest("That timezone isn't recognised. Pick one from the list.")
    return tz


def _check_masters(db: Session, company_id: int, data: dict) -> None:
    from core.masters import validate_value_ids
    for field, key in MASTER_FIELDS.items():
        if data.get(field) is not None:
            validate_value_ids(db, company_id, key, [int(data[field])])


def _check_org_refs(db: Session, company_id: int, data: dict) -> None:
    """Location, legal entity, grade, cost centre, department and designation must be this company's."""
    from modules.organization import service as org
    org.check_refs(db, company_id, {k: data.get(k) for k in
                                    ("location_id", "legal_entity_id", "grade_id", "cost_centre_id",
                                     "department_id", "designation_id") if data.get(k) is not None})


def _clean_address(db: Session, company_id: int, value) -> dict:
    if not value:
        return {}
    if not isinstance(value, dict):
        raise BadRequest("Address should have line, city, state and PIN code.")
    out = {}
    for k in ADDRESS_KEYS:
        x = value.get(k)
        if isinstance(x, str):
            x = x.strip() or None
        if x is not None:
            out[k] = x
    if "pincode" in out:
        out["pincode"] = v.clean_pincode(out["pincode"])
    from core.masters import validate_value_ids
    if out.get("state_id") is not None:
        out["state_id"] = int(out["state_id"])
        validate_value_ids(db, company_id, "state", [out["state_id"]])
    if out.get("country_id") is not None:
        out["country_id"] = int(out["country_id"])
        validate_value_ids(db, company_id, "country", [out["country_id"]])
    return out


def _clean_statutory(data: dict) -> dict:
    """Normalise and check every statutory field present in `data` (in place)."""
    checks = {"pan": v.clean_pan, "uan": v.clean_uan, "esi_number": v.clean_esi, "passport_number": v.clean_passport,
              "driving_licence": v.clean_driving_licence, "aadhaar": v.clean_aadhaar}
    for field, fn in checks.items():
        if field in data:
            data[field] = fn(data[field])
    for field, label in (("phone", "Mobile number"), ("alternate_phone", "Alternate phone"), ("work_phone", "Work phone")):
        if field in data:
            data[field] = v.clean_phone(data[field], label)
    return data


def _check_dates(emp_like: dict) -> None:
    dob, doj = emp_like.get("date_of_birth"), emp_like.get("date_of_joining")
    if dob and dob > today():
        raise BadRequest("Date of birth can't be in the future.")
    if dob and doj and (doj - dob).days < 14 * 365:
        raise BadRequest("The employee would be under 14 on the joining date. Check the dates.")
    pe = emp_like.get("probation_end_date")
    if pe and doj and pe < doj:
        raise BadRequest("Probation can't end before the joining date.")
    cd = emp_like.get("confirmation_date")
    if cd and doj and cd < doj:
        raise BadRequest("Confirmation can't be before the joining date.")
    if emp_like.get("notice_period_days") is not None and not 0 <= int(emp_like["notice_period_days"]) <= 365:
        raise BadRequest("Notice period should be between 0 and 365 days.")
    if emp_like.get("work_mode") and emp_like["work_mode"] not in WORK_MODES:
        raise BadRequest("Work mode should be office, remote, hybrid or field.")


def find_duplicates(db: Session, company_id: int, *, email=None, phone=None, pan=None, aadhaar=None, uan=None,
                    exclude_id: int | None = None) -> list[dict]:
    """Other employees holding the same email, phone, PAN, Aadhaar or UAN."""
    checks = []
    if email:
        checks.append(("email", "Email", Employee.email.ilike(email.strip())))
    if phone:
        digits = "".join(ch for ch in phone if ch.isdigit())[-10:]
        if len(digits) >= 8:
            from sqlalchemy import func
            checks.append(("phone", "Mobile number",
                           func.regexp_replace(Employee.phone, r"\D", "", "g").like(f"%{digits}")))
    if pan:
        checks.append(("pan", "PAN", Employee.pan == pan))
    if aadhaar:
        checks.append(("aadhaar", "Aadhaar", Employee.aadhaar_hash == sensitive.lookup_hash(aadhaar)))
    if uan:
        checks.append(("uan", "UAN", Employee.uan == uan))
    out = []
    for field, label, cond in checks:
        q = db.query(Employee).filter(Employee.company_id == company_id, cond)
        if exclude_id:
            q = q.filter(Employee.id != exclude_id)
        for other in q.limit(3).all():
            out.append({"field": field, "label": label, "employee_id": other.id, "uuid": str(other.uuid),
                        "name": display_name(other), "employee_code": other.employee_code, "status": other.status,
                        "can_rehire": other.status in INACTIVE_STATUSES})
    return out


def _refuse_duplicates(db: Session, company_id: int, data: dict, exclude_id: int | None = None) -> None:
    dups = find_duplicates(db, company_id, email=data.get("email"), pan=data.get("pan"), aadhaar=data.get("aadhaar"),
                           uan=data.get("uan"), exclude_id=exclude_id)
    if not dups:
        return
    d = dups[0]
    if d["field"] == "email":
        raise BadRequest("An employee with this email already exists in your company")
    hint = " They have left: rehire them instead of adding a new record." if d["can_rehire"] else ""
    raise BadRequest(f"This {d['label']} already belongs to {d['name']}"
                     f"{' (' + d['employee_code'] + ')' if d['employee_code'] else ''}.{hint}")


def _employee_code(db: Session, company_id: int, typed: str | None, current: Employee | None = None,
                   legal_entity_id: int | None = None) -> str | None:
    """Employee ID: typed by hand, or the next number from the employee series when the
    company has switched it on."""
    from core import numbering
    typed = (typed or "").strip() or None
    series = numbering.get_series(db, company_id, "employee")
    auto = series is not None and series.active
    if typed is None:
        if current is not None:
            return current.employee_code
        return numbering.next_number(db, company_id, "employee", legal_entity_id=legal_entity_id) if auto else None
    if auto and not series.allow_manual and (current is None or typed != current.employee_code):
        raise BadRequest("Employee IDs are given automatically. Leave the ID empty.")
    dup = db.query(Employee.id).filter(Employee.company_id == company_id, Employee.employee_code == typed)
    if current is not None:
        dup = dup.filter(Employee.id != current.id)
    if dup.first():
        raise BadRequest(f"Employee ID {typed} is already used by someone else.")
    return typed


def _prepare(db: Session, company_id: int, data: dict, emp: Employee | None) -> dict:
    """Validate and normalise incoming fields (create or partial update)."""
    _clean_statutory(data)
    for key in ("current_address", "permanent_address"):
        if key in data:
            data[key] = _clean_address(db, company_id, data[key])
    if "timezone" in data:
        data["timezone"] = _check_timezone(data["timezone"])
    if "reporting_manager_id" in data:
        _check_manager(db, company_id, emp, data["reporting_manager_id"])
    if "secondary_manager_id" in data:
        _check_manager(db, company_id, emp, data["secondary_manager_id"], "secondary manager")
        if data["secondary_manager_id"] and data["secondary_manager_id"] == data.get(
                "reporting_manager_id", emp.reporting_manager_id if emp else None):
            raise BadRequest("The secondary manager should be someone other than the reporting manager.")
    if "personal_email" in data and data["personal_email"]:
        if "@" not in data["personal_email"]:
            raise BadRequest("The personal email address doesn't look right.")
    _check_masters(db, company_id, data)
    _check_org_refs(db, company_id, data)
    merged = {k: getattr(emp, k, None) for k in ("date_of_birth", "date_of_joining", "probation_end_date",
                                                  "confirmation_date", "notice_period_days", "work_mode")} if emp else {}
    merged.update({k: data[k] for k in merged.keys() | set(data) if k in data})
    _check_dates(merged)
    for name_field in ("first_name", "last_name"):
        if name_field in data and not (data[name_field] or "").strip():
            raise BadRequest("First and last name can't be empty.")
    return data


# ── Create and update ────────────────────────────────────────────────

def create(db: Session, company_id: int, data: dict) -> Employee:
    data = {k: val for k, val in data.items()}
    data["email"] = (data.get("email") or "").strip().lower()
    _prepare(db, company_id, data, None)
    _refuse_duplicates(db, company_id, data)
    custom = custom_fields.clean_values(db, company_id, "employee", data.get("custom"))
    code = _employee_code(db, company_id, data.get("employee_code"), legal_entity_id=data.get("legal_entity_id"))
    status = data.get("status") or ("probation" if data.get("probation_end_date") else "confirmed")
    if status not in STATUSES or status in INACTIVE_STATUSES:
        raise BadRequest("A new employee starts as pre-joining, on probation or confirmed.")
    emp = Employee(
        company_id=company_id,
        employee_code=code,
        password=hash_password(data.get("password") or _random_password()),
        status=status,
        active=True,
        role=data.get("role") or "employee",
        custom={k: val for k, val in custom.items() if val is not None},
        date_of_joining=data.get("date_of_joining") or today(),
    )
    for key in PLAIN_FIELDS - {"role", "date_of_joining"}:
        if key in data and data[key] is not None:
            setattr(emp, key, data[key].strip() if isinstance(data[key], str) else data[key])
    for key in ("current_address", "permanent_address"):
        if data.get(key):
            setattr(emp, key, data[key])
    if "aadhaar" in data:
        sensitive.set_aadhaar(emp, data["aadhaar"])
    for key in ("pan", "uan", "esi_number", "passport_number", "driving_licence"):
        if data.get(key):
            setattr(emp, key, data[key])
    db.add(emp)
    db.flush()
    config = dict(DEFAULT_WORK_ARRANGEMENT)
    background = {k: data.get(k) for k in BACKGROUND_FIELDS if data.get(k) not in (None, "")}
    if background:
        config["background"] = background
    db.add(EmployeeSettings(company_id=company_id, employee_id=emp.id, config=config))
    db.add(JobChange(company_id=company_id, employee_id=emp.id, change_type="joining",
                     effective_date=emp.date_of_joining, status="applied", applied_at=datetime.now(timezone.utc),
                     changes={k: {"old": None, "new": getattr(emp, k)} for k in JOB_FIELDS if getattr(emp, k) is not None},
                     reason=None, created_by_name=actor_name()))
    if data.get("team_ids"):
        from modules.organization import service as org
        org.set_employee_teams(db, company_id, emp.id, data["team_ids"])
    if data.get("tag_ids"):
        from modules.organization import service as org
        org.set_tags(db, company_id, "employees", emp.id, data["tag_ids"])
    return emp


def _random_password() -> str:
    import secrets
    return secrets.token_urlsafe(16)


def _infer_change_type(fields: set[str]) -> str:
    if fields == {"reporting_manager_id"} or fields == {"secondary_manager_id"} or fields == {"reporting_manager_id", "secondary_manager_id"}:
        return "manager_change"
    if fields == {"designation_id"}:
        return "redesignation"
    if fields == {"department_id"}:
        return "department_change"
    if "legal_entity_id" in fields:
        return "entity_transfer"
    if "location_id" in fields:
        return "transfer"
    if fields == {"grade_id"}:
        return "grade_change"
    if fields == {"employment_type_id"}:
        return "type_change"
    return "update"


def update(db: Session, company_id: int, emp: Employee, data: dict, *, change_type: str | None = None,
           reason: str | None = None, effective_date: date | None = None) -> Employee:
    """Apply a partial update. Job field changes are recorded in the job history."""
    data = dict(data)
    if "email" in data and data["email"]:
        data["email"] = data["email"].strip().lower()
    _prepare(db, company_id, data, emp)
    dup_fields = {k: data.get(k) for k in ("email", "pan", "aadhaar", "uan")
                  if k in data and data.get(k) and (k == "aadhaar" or data.get(k) != getattr(emp, k, None))}
    if dup_fields:
        _refuse_duplicates(db, company_id, dup_fields, exclude_id=emp.id)
    if "employee_code" in data:
        data["employee_code"] = _employee_code(db, company_id, data["employee_code"], emp)
    if "custom" in data:
        cleaned = custom_fields.clean_values(db, company_id, "employee", data["custom"], emp.custom, partial=True)
        merged = {**(emp.custom or {}), **cleaned}
        data["custom"] = {k: val for k, val in merged.items() if val is not None}
    background = {k: data.pop(k) for k in list(data) if k in BACKGROUND_FIELDS}
    if "aadhaar" in data:
        sensitive.set_aadhaar(emp, data.pop("aadhaar"))
    team_ids, tag_ids = data.pop("team_ids", None), data.pop("tag_ids", None)
    if "active" in data:
        active = data.pop("active")
        if active is False and emp.status not in INACTIVE_STATUSES:
            set_status(db, company_id, emp, "exited", reason="Marked inactive", effective=today())
        elif active is True and emp.status in INACTIVE_STATUSES:
            rehire(db, company_id, emp, {"date_of_joining": emp.date_of_joining or today(), "status": "confirmed",
                                         "reason": "Marked active again", "keep_joining_date": True})
    data.pop("status", None)  # stages change through set_status
    job_changes = {}
    for key, value in data.items():
        if isinstance(value, str):
            value = value.strip()
            if key not in ("first_name", "last_name", "email"):
                value = value or None
        if key in JOB_FIELDS and getattr(emp, key) != value:
            job_changes[key] = {"old": getattr(emp, key), "new": value}
        setattr(emp, key, value)
    if job_changes:
        db.add(JobChange(company_id=company_id, employee_id=emp.id,
                         change_type=change_type or _infer_change_type(set(job_changes)),
                         effective_date=effective_date or today(), changes=job_changes, reason=reason,
                         status="applied", applied_at=datetime.now(timezone.utc), created_by_name=actor_name()))
    if background:
        if not emp.settings:
            emp.settings = EmployeeSettings(company_id=company_id, employee_id=emp.id, config=dict(DEFAULT_WORK_ARRANGEMENT))
        config = dict(emp.settings.config or {})
        config["background"] = {**(config.get("background") or {}), **background}
        emp.settings.config = config
        flag_modified(emp.settings, "config")
    if team_ids is not None or tag_ids is not None:
        from modules.organization import service as org
        if team_ids is not None:
            org.set_employee_teams(db, company_id, emp.id, team_ids)
        if tag_ids is not None:
            org.set_tags(db, company_id, "employees", emp.id, tag_ids)
    return emp


# ── Effective-dated job changes ──────────────────────────────────────

def record_job_change(db: Session, company_id: int, emp: Employee, change_type: str, effective_date: date,
                      new_values: dict, reason: str | None, remarks: str | None = None) -> JobChange:
    """A transfer, promotion or other job change. Dated today or earlier it applies now;
    dated later it is scheduled and applied on that date."""
    if change_type not in CHANGE_TYPES or change_type in ("joining", "rehire", "exit", "status"):
        raise BadRequest("Pick what kind of change this is.")
    unknown = [k for k in new_values if k not in JOB_FIELDS]
    if unknown:
        raise BadRequest(f"“{unknown[0]}” can't be changed this way.")
    if not new_values:
        raise BadRequest("Choose at least one new value.")
    if emp.date_of_joining and effective_date < emp.date_of_joining:
        raise BadRequest("The change can't take effect before the joining date.")
    if emp.status in INACTIVE_STATUSES:
        raise BadRequest("This person has left. Rehire them before changing their job.")
    data = dict(new_values)
    _prepare(db, company_id, data, emp)
    changes = {k: {"old": getattr(emp, k), "new": val} for k, val in data.items() if getattr(emp, k) != val}
    if not changes:
        raise BadRequest("Nothing is different from the current job details.")
    clash = (db.query(JobChange.id).filter(JobChange.company_id == company_id, JobChange.employee_id == emp.id,
                                           JobChange.status == "scheduled", JobChange.effective_date == effective_date)
             .first())
    if clash:
        raise BadRequest("A change is already scheduled for this date. Cancel it first or pick another date.")
    row = JobChange(company_id=company_id, employee_id=emp.id, change_type=change_type, effective_date=effective_date,
                    changes=changes, reason=(reason or "").strip() or None, remarks=remarks,
                    status="scheduled", created_by_name=actor_name())
    db.add(row)
    db.flush()
    if effective_date <= today():
        _apply(db, emp, row)
    return row


def _apply(db: Session, emp: Employee, row: JobChange) -> None:
    for key, change in (row.changes or {}).items():
        setattr(emp, key, change.get("new"))
    row.status = "applied"
    row.applied_at = datetime.now(timezone.utc)


def apply_due_changes(db: Session, company_id: int) -> int:
    """Apply scheduled job changes whose date has come. Cheap when there are none."""
    rows = (db.query(JobChange).filter(JobChange.company_id == company_id, JobChange.status == "scheduled",
                                       JobChange.effective_date <= today())
            .order_by(JobChange.effective_date, JobChange.id).all())
    for row in rows:
        emp = get_by_id(db, company_id, row.employee_id)
        if emp is not None and emp.status not in INACTIVE_STATUSES:
            _apply(db, emp, row)
        else:
            row.status = "cancelled"
    return len(rows)


def cancel_job_change(db: Session, company_id: int, row: JobChange) -> None:
    if row.status != "scheduled":
        raise BadRequest("Only changes that haven't taken effect yet can be cancelled.")
    row.status = "cancelled"


# ── Lifecycle ────────────────────────────────────────────────────────

def set_status(db: Session, company_id: int, emp: Employee, status: str, *, reason: str | None = None,
               effective: date | None = None, extra: dict | None = None) -> Employee:
    """Move an employee to another lifecycle stage, recording it in the history."""
    extra = extra or {}
    if status not in STATUSES:
        raise BadRequest("Unknown stage.")
    if status == emp.status:
        raise BadRequest(f"They are already {STATUSES[status].lower()}.")
    if status not in TRANSITIONS.get(emp.status, set()):
        raise BadRequest(f"Someone {STATUSES[emp.status].lower()} can't be moved to {STATUSES[status].lower()}.")
    effective = effective or today()
    changes = {"status": {"old": emp.status, "new": status}}

    def put(field, value):
        if getattr(emp, field) != value:
            changes[field] = {"old": getattr(emp, field), "new": value}
            setattr(emp, field, value)

    if status == "confirmed":
        put("confirmation_date", extra.get("confirmation_date") or effective)
        if emp.status == "notice":
            put("resignation_date", None)
            put("exit_date", None)
    elif status == "probation":
        if extra.get("probation_end_date"):
            put("probation_end_date", extra["probation_end_date"])
    elif status == "notice":
        resigned = extra.get("resignation_date") or effective
        last_day = extra.get("exit_date") or (resigned + timedelta(days=emp.notice_period_days or 30))
        if last_day < resigned:
            raise BadRequest("The last working day can't be before the resignation date.")
        put("resignation_date", resigned)
        put("exit_date", last_day)
    elif status == "exited":
        put("exit_date", extra.get("exit_date") or emp.exit_date or effective)
        put("active", False)
    emp.status = status
    if status not in INACTIVE_STATUSES:
        emp.active = True
    kind = {"confirmed": "confirmation", "exited": "exit"}.get(status, "status")
    db.add(JobChange(company_id=company_id, employee_id=emp.id, change_type=kind, effective_date=effective,
                     changes=_jsonable(changes), reason=(reason or "").strip() or None, status="applied",
                     applied_at=datetime.now(timezone.utc), created_by_name=actor_name()))
    return emp


def archive(db: Session, company_id: int, emp: Employee, reason: str | None = None) -> Employee:
    """No delete: a record is archived. Only people who have left (or never joined) can be archived."""
    if emp.status == "archived":
        raise BadRequest("This record is already archived.")
    if emp.status not in ("exited", "pre_joining"):
        raise BadRequest("Record their exit first. Only people who have left or never joined can be archived.")
    old = emp.status
    emp.status = "archived"
    emp.active = False
    emp.archived_at = datetime.now(timezone.utc)
    emp.archived_by = actor_name()
    db.add(JobChange(company_id=company_id, employee_id=emp.id, change_type="status", effective_date=today(),
                     changes={"status": {"old": old, "new": "archived"}}, reason=reason, status="applied",
                     applied_at=datetime.now(timezone.utc), created_by_name=actor_name()))
    return emp


def rehire(db: Session, company_id: int, emp: Employee, data: dict) -> Employee:
    """Bring a former employee back on the same record: their history, UAN and documents
    stay; the new stint starts with its own joining date."""
    if emp.status not in INACTIVE_STATUSES:
        raise BadRequest("Only people who have left can be rehired.")
    doj = data.get("date_of_joining") or today()
    if emp.exit_date and doj <= emp.exit_date and not data.get("keep_joining_date"):
        raise BadRequest("The new joining date should be after their last exit date.")
    status = data.get("status") or "probation"
    if status not in ("pre_joining", "probation", "confirmed"):
        raise BadRequest("A rehired person starts as pre-joining, on probation or confirmed.")
    changes = {"status": {"old": emp.status, "new": status},
               "date_of_joining": {"old": emp.date_of_joining, "new": doj},
               "exit_date": {"old": emp.exit_date, "new": None}}
    emp.status = status
    emp.active = True
    emp.archived_at = None
    emp.archived_by = None
    emp.date_of_joining = doj
    emp.exit_date = None
    emp.resignation_date = None
    emp.confirmation_date = doj if status == "confirmed" else None
    emp.probation_end_date = data.get("probation_end_date")
    if not data.get("keep_joining_date"):
        emp.rehire_count = (emp.rehire_count or 0) + 1
    job = {k: data[k] for k in JOB_FIELDS if data.get(k) is not None}
    if job:
        _prepare(db, company_id, job, emp)
        for k, val in job.items():
            if getattr(emp, k) != val:
                changes[k] = {"old": getattr(emp, k), "new": val}
                setattr(emp, k, val)
    db.add(JobChange(company_id=company_id, employee_id=emp.id, change_type="rehire", effective_date=doj,
                     changes=_jsonable(changes), reason=(data.get("reason") or "").strip() or None, status="applied",
                     applied_at=datetime.now(timezone.utc), created_by_name=actor_name()))
    return emp


def _jsonable(changes: dict) -> dict:
    def conv(x):
        return x.isoformat() if isinstance(x, (date, datetime)) else x
    return {k: {"old": conv(c.get("old")), "new": conv(c.get("new"))} for k, c in changes.items()}
