"""Saved reports: who can see and change them, running, exporting (always recorded) and
email schedules."""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import or_
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core import audit, context
from core.errors import BadRequest, Forbidden, NotFound
from modules.reports import engine, registry, render
from modules.reports.models import ExportLog, Report, ReportSchedule
from modules.reports.registry import Principal

logger = logging.getLogger("hrms")

VIEW = "insights.reports.view"
MANAGE = "insights.reports.manage"
EXPORT_KEEP = timedelta(hours=24)
FREQUENCIES = {"daily": "Every day", "weekly": "Every week", "monthly": "Every month"}


def now() -> datetime:
    return datetime.now(timezone.utc)


# ── Access ─────────────────────────────────────────────────────────

def can_view(db: Session, p: Principal, r: Report) -> bool:
    src = registry.get(r.source)
    if src is None or registry.max_scope(db, p, src) is None:
        return False
    if p.is_owner or p.role == "admin" or p.can(MANAGE):
        return True
    if r.owner_employee_id and r.owner_employee_id == p.employee_id:
        return True
    if p.role in (r.shared_roles or []) or (p.employee_id and p.employee_id in (r.shared_employee_ids or [])):
        return True
    return bool(r.is_standard and p.can(VIEW))


def can_edit(p: Principal, r: Report) -> bool:
    if r.is_standard:
        return False
    if p.is_owner or p.role == "admin":
        return True
    return bool(r.owner_employee_id and r.owner_employee_id == p.employee_id)


def can_share(p: Principal) -> bool:
    return p.is_owner or p.role == "admin" or p.can(MANAGE)


def get(db: Session, p: Principal, uuid) -> Report:
    r = db.query(Report).filter(Report.company_id == p.company_id, Report.uuid == uuid,
                                Report.archived_at.is_(None)).first()
    if r is None or not can_view(db, p, r):
        raise NotFound("Report")
    return r


def get_by_key(db: Session, p: Principal, key: str) -> Report:
    r = db.query(Report).filter(Report.company_id == p.company_id, Report.key == key,
                                Report.archived_at.is_(None)).first()
    if r is None or not can_view(db, p, r):
        raise NotFound("Report")
    return r


def report_out(db: Session, p: Principal, r: Report, schedules: int = 0) -> dict:
    src = registry.get(r.source)
    return {
        "uuid": str(r.uuid), "key": r.key, "name": r.name, "description": r.description, "category": r.category,
        "source": r.source, "source_label": src.label if src else r.source, "config": r.config, "chart": r.chart,
        "scope": r.scope, "is_standard": r.is_standard, "owner_name": r.owner_name or "Company owner",
        "mine": bool(r.owner_employee_id and r.owner_employee_id == p.employee_id) or (
            p.is_owner and not r.owner_employee_id and not r.is_standard),
        "shared_roles": r.shared_roles or [], "shared_employee_ids": r.shared_employee_ids or [],
        "can_edit": can_edit(p, r), "can_share": can_share(p) and (can_edit(p, r) or r.is_standard),
        "schedules": schedules, "sensitive": bool(src and src.sensitive),
        "updated_at": r.updated_at,
    }


def list_reports(db: Session, p: Principal, *, category: str | None = None, q: str | None = None,
                 which: str | None = None) -> list[dict]:
    rows = db.query(Report).filter(Report.company_id == p.company_id, Report.archived_at.is_(None))
    if category:
        rows = rows.filter(Report.category == category)
    if q:
        rows = rows.filter(or_(Report.name.ilike(f"%{q}%"), Report.description.ilike(f"%{q}%")))
    counts = dict(db.query(ReportSchedule.report_id, ReportSchedule.id).filter(
        ReportSchedule.company_id == p.company_id).all())
    sched: dict[int, int] = {}
    for rid in counts:
        sched[rid] = sched.get(rid, 0) + 1
    out = []
    for r in rows.order_by(Report.is_standard.desc(), Report.name).all():
        if not can_view(db, p, r):
            continue
        d = report_out(db, p, r, sched.get(r.id, 0))
        if which == "standard" and not r.is_standard:
            continue
        if which == "mine" and not d["mine"]:
            continue
        if which == "shared" and (r.is_standard or d["mine"]):
            continue
        out.append(d)
    return out


def _check_source(db: Session, p: Principal, key: str) -> registry.Source:
    src = registry.get(key)
    if src is None:
        raise BadRequest("Choose what the report is about.")
    if registry.max_scope(db, p, src) is None:
        raise Forbidden(f"You don't have access to {src.label.lower()}.")
    return src


def _clean_sharing(db: Session, p: Principal, data: dict) -> tuple[list, list]:
    roles = [str(x) for x in data.get("shared_roles") or []]
    people = [int(x) for x in data.get("shared_employee_ids") or []]
    if (roles or people) and not can_share(p):
        raise Forbidden("Only HR can share reports with others.")
    if people:
        from modules.employees.models import Employee
        found = {i for (i,) in db.query(Employee.id).filter(Employee.company_id == p.company_id,
                                                             Employee.id.in_(people)).all()}
        people = [i for i in people if i in found]
    return roles, people


def save(db: Session, p: Principal, data: dict, r: Report | None = None) -> Report:
    name = (data.get("name") or (r.name if r else "")).strip()
    if not name:
        raise BadRequest("Give the report a name.")
    if len(name) > 120:
        raise BadRequest("Keep the name under 120 characters.")
    source_key = data.get("source") or (r.source if r else None)
    src = _check_source(db, p, source_key)
    config = data.get("config") if "config" in data else (r.config if r else {})
    engine.clean_config(src, config, engine.resolve_params(src, (config or {}).get("params"), date.today())
                        if src.params else {})
    scope = data.get("scope") or (r.scope if r else "auto")
    if scope not in ("auto", *registry.SCOPES):
        raise BadRequest("Choose whose data the report shows.")
    dup = db.query(Report.id).filter(Report.company_id == p.company_id, Report.name == name,
                                     Report.archived_at.is_(None))
    if r is not None:
        dup = dup.filter(Report.id != r.id)
    if dup.first():
        raise BadRequest("There is already a report with this name.")
    if r is None:
        r = Report(company_id=p.company_id, owner_employee_id=p.employee_id, owner_name=p.name or "Company owner",
                   is_standard=False)
        db.add(r)
    elif not can_edit(p, r):
        raise Forbidden("Only the person who made this report can change it. Copy it to make your own.")
    r.name, r.source, r.config, r.scope = name, src.key, config or {}, scope
    r.description = (data.get("description") or "").strip() or None if "description" in data else r.description
    r.category = src.category
    if "chart" in data:
        r.chart = data.get("chart")
    if "shared_roles" in data or "shared_employee_ids" in data:
        r.shared_roles, r.shared_employee_ids = _clean_sharing(db, p, data)
    flag_modified(r, "config")
    db.flush()
    return r


def share(db: Session, p: Principal, r: Report, data: dict) -> Report:
    if not can_share(p):
        raise Forbidden("Only HR can share reports with others.")
    r.shared_roles, r.shared_employee_ids = _clean_sharing(db, p, data)
    db.flush()
    audit.event(db, "report.shared", "reports", r.id, {"roles": r.shared_roles, "people": r.shared_employee_ids},
                entity_uuid=str(r.uuid), entity_label=r.name)
    return r


def clone(db: Session, p: Principal, r: Report, name: str | None = None) -> Report:
    base = (name or f"{r.name} (copy)").strip()
    n, i = base, 2
    while db.query(Report.id).filter(Report.company_id == p.company_id, Report.name == n,
                                     Report.archived_at.is_(None)).first():
        n, i = f"{base} {i}", i + 1
    return save(db, p, {"name": n, "description": r.description, "source": r.source, "config": dict(r.config),
                        "chart": r.chart, "scope": r.scope})


def archive(db: Session, p: Principal, r: Report) -> None:
    if not can_edit(p, r):
        raise Forbidden("Only the person who made this report can remove it.")
    if db.query(ReportSchedule.id).filter(ReportSchedule.report_id == r.id, ReportSchedule.active.is_(True)).first():
        raise BadRequest("This report is emailed on a schedule. Stop the schedule first.")
    r.archived_at = now()


# ── Running ────────────────────────────────────────────────────────

def merged_config(r_config: dict, overrides: dict | None) -> dict:
    """Run-time choices on top of a saved report: extra filters and parameters."""
    c = dict(r_config or {})
    o = overrides or {}
    if o.get("filters"):
        c["filters"] = list(c.get("filters") or []) + list(o["filters"])
    if o.get("replace_filters") is not None:
        c["filters"] = list(o["replace_filters"])
    if o.get("params"):
        c["params"] = {**(c.get("params") or {}), **o["params"]}
    for k in ("sort", "group_by", "columns", "measures"):
        if o.get(k) is not None:
            c[k] = o[k]
    return c


def run_report(db: Session, p: Principal, r: Report, overrides: dict | None = None) -> dict:
    src = registry.get(r.source)
    if src is None:
        raise BadRequest("This report's data is not available.")
    return engine.run(db, p, src, merged_config(r.config, overrides), scope_limit=r.scope)


def run_adhoc(db: Session, p: Principal, source_key: str, config: dict, scope: str | None = None) -> dict:
    src = _check_source(db, p, source_key)
    return engine.run(db, p, src, config, scope_limit=scope)


# ── Exports (always recorded) ───────────────────────────────────────

def _filters_text(result: dict) -> list[str]:
    src = registry.get(result["source"])
    fm = engine.fields_for(src, {}) if src else {}
    out = []
    for f in result["config"].get("filters") or []:
        label = fm[f["field"]].label if f["field"] in fm else f["field"]
        val = f.get("value")
        if isinstance(val, list):
            val = ", ".join(str(x) for x in val)
        out.append(f"{label} {engine.OPS.get(f['op'], f['op'])}{'' if val in (None, '') else ' ' + str(val)}")
    for k, v in (result.get("params") or {}).items():
        if v not in (None, ""):
            out.append(f"{k}: {v}")
    return out


def meta_lines(db: Session, p: Principal, result: dict, title: str) -> list[str]:
    zone = ZoneInfo(result.get("timezone") or "Asia/Kolkata")
    lines = [f"{result['source_label']} · {result['scope_label']} · {result['row_count']} rows",
             f"Made by {p.name or 'Company owner'} on {now().astimezone(zone):%d %b %Y %H:%M}"]
    f = _filters_text(result)
    if f:
        lines.append("Filters: " + "; ".join(f))
    if result.get("masked"):
        lines.append("Some values are hidden because you may not see them.")
    return lines


def is_sensitive(result: dict) -> bool:
    src = registry.get(result["source"])
    if src and src.sensitive:
        return True
    fm = src.field_map() if src else {}
    return any(fm.get(c["key"]) and fm[c["key"]].sensitive and c["key"] not in (result.get("masked") or [])
               for c in result["columns"])


def record_export(db: Session, p: Principal, *, what: str, fmt: str, content: bytes, file_name: str,
                  content_type: str, rows: int, channel: str = "download", report: Report | None = None,
                  source: str | None = None, scope: str | None = None, details: dict | None = None,
                  sensitive: bool = False, protected: bool = False, keep: bool = True) -> ExportLog:
    """Write the export record and an audit event. Every export in the product goes through here."""
    ctx = context.current()
    log = ExportLog(company_id=p.company_id, employee_id=p.employee_id, actor_name=p.name or "Company owner",
                    channel=channel, report_id=report.id if report else None, what=what[:200], source=source,
                    format=fmt, row_count=rows, scope=scope, details=details or {}, sensitive=sensitive,
                    protected=protected, file_name=file_name, content_type=content_type,
                    content=content if keep else None, expires_at=now() + EXPORT_KEEP if keep else None,
                    ip_address=getattr(ctx, "ip_address", None) or getattr(ctx, "ip", None))
    db.add(log)
    db.flush()
    audit.event(db, "export.created", "export_logs", log.id,
                {"what": what, "format": fmt, "rows": rows, "channel": channel, "scope": scope,
                 "protected": protected, "sensitive": sensitive, "source": source, **(details or {})},
                entity_uuid=str(log.uuid), entity_label=what)
    return log


def export(db: Session, p: Principal, result: dict, *, title: str, fmt: str, protect: bool = False,
           report: Report | None = None, channel: str = "download") -> tuple[ExportLog, str | None]:
    sensitive = is_sensitive(result)
    password = render.new_password() if protect else None
    data, ctype, ext = render.render(result, fmt, title, meta_lines(db, p, result, title),
                                     result.get("timezone") or "Asia/Kolkata", password)
    stamp = now().strftime("%Y%m%d-%H%M")
    file_name = f"{render.safe_name(title)}-{stamp}.{ext}"
    log = record_export(db, p, what=title, fmt=ext, content=data, file_name=file_name, content_type=ctype,
                        rows=result["row_count"], channel=channel, report=report, source=result["source"],
                        scope=result["scope"], sensitive=sensitive, protected=bool(password),
                        details={"filters": _filters_text(result)})
    return log, password


def download(db: Session, p: Principal, uuid) -> ExportLog:
    log = db.query(ExportLog).filter(ExportLog.company_id == p.company_id, ExportLog.uuid == uuid).first()
    if log is None or log.employee_id != p.employee_id:
        raise NotFound("Export")
    if log.content is None or (log.expires_at and log.expires_at < now()):
        raise BadRequest("This file is no longer available. Export it again.")
    log.downloaded_at = log.downloaded_at or now()
    audit.event(db, "export.downloaded", "export_logs", log.id, {"what": log.what, "format": log.format},
                entity_uuid=str(log.uuid), entity_label=log.what)
    return log


def export_history(db: Session, p: Principal, *, everyone: bool, channel: str | None = None):
    q = db.query(ExportLog).filter(ExportLog.company_id == p.company_id)
    if not everyone:
        q = q.filter(ExportLog.employee_id == p.employee_id) if p.employee_id else q.filter(
            ExportLog.employee_id.is_(None))
    if channel:
        q = q.filter(ExportLog.channel == channel)
    return q.order_by(ExportLog.created_at.desc())


def export_out(x: ExportLog) -> dict:
    return {"uuid": str(x.uuid), "what": x.what, "channel": x.channel, "format": x.format, "rows": x.row_count,
            "scope": x.scope, "actor_name": x.actor_name, "sensitive": x.sensitive, "protected": x.protected,
            "file_name": x.file_name, "created_at": x.created_at, "downloaded_at": x.downloaded_at,
            "available": bool(x.content is not None and x.expires_at and x.expires_at > now()),
            "details": x.details or {}}


def purge_expired_exports(db: Session) -> int:
    rows = db.query(ExportLog).filter(ExportLog.content.isnot(None), ExportLog.expires_at < now()).all()
    for x in rows:
        x.content = None
    return len(rows)


# ── Schedules ──────────────────────────────────────────────────────

def _company_zone(db: Session, company_id: int) -> ZoneInfo:
    return ZoneInfo(engine._company_tz(db, company_id))


def next_run(s: ReportSchedule, zone: ZoneInfo, after: datetime | None = None) -> datetime:
    after = (after or now()).astimezone(zone)
    day = after.date()
    for _ in range(400):
        ok = (s.frequency == "daily" or (s.frequency == "weekly" and day.weekday() == (s.weekday or 0))
              or (s.frequency == "monthly" and day.day == (s.day_of_month or 1)))
        if ok:
            at = datetime.combine(day, time(hour=s.hour or 0), tzinfo=zone)
            if at > after:
                return at.astimezone(timezone.utc)
        day += timedelta(days=1)
    raise BadRequest("Couldn't work out when this schedule runs next.")


def schedule_out(db: Session, s: ReportSchedule, report: Report | None = None) -> dict:
    from modules.employees.models import Employee
    names = {}
    if s.recipient_employee_ids:
        names = {e.id: (f"{e.first_name} {e.last_name}".strip(), e.email) for e in db.query(Employee).filter(
            Employee.company_id == s.company_id, Employee.id.in_(s.recipient_employee_ids)).all()}
    return {"uuid": str(s.uuid), "name": s.name, "report_uuid": str(report.uuid) if report else None,
            "report_name": report.name if report else None, "frequency": s.frequency,
            "frequency_label": FREQUENCIES.get(s.frequency), "weekday": s.weekday, "day_of_month": s.day_of_month,
            "hour": s.hour, "format": s.format, "recipient_employee_ids": s.recipient_employee_ids or [],
            "recipients": [{"id": i, "name": names[i][0], "email": names[i][1]} for i in s.recipient_employee_ids or []
                           if i in names],
            "recipient_emails": s.recipient_emails or [], "active": s.active, "next_run_at": s.next_run_at,
            "last_run_at": s.last_run_at, "last_status": s.last_status, "last_message": s.last_message,
            "created_by_name": s.created_by_name}


def save_schedule(db: Session, p: Principal, data: dict, s: ReportSchedule | None = None) -> ReportSchedule:
    if not can_share(p):
        raise Forbidden("Only HR can schedule reports.")
    if s is None:
        r = get(db, p, data.get("report_uuid"))
        s = ReportSchedule(company_id=p.company_id, report_id=r.id, run_as_employee_id=p.employee_id, active=True,
                           created_by_name=p.name or "Company owner")
        db.add(s)
    name = (data.get("name") or s.name or "").strip()
    if not name:
        raise BadRequest("Give the schedule a name.")
    freq = data.get("frequency", s.frequency or "weekly")
    if freq not in FREQUENCIES:
        raise BadRequest("Choose daily, weekly or monthly.")
    hour = int(data.get("hour", s.hour if s.hour is not None else 9))
    if not 0 <= hour <= 23:
        raise BadRequest("Choose an hour between 0 and 23.")
    fmt = data.get("format", s.format or "xlsx")
    if fmt not in render.FORMATS:
        raise BadRequest("Choose Excel, PDF or CSV.")
    weekday = data.get("weekday", s.weekday)
    dom = data.get("day_of_month", s.day_of_month)
    if freq == "weekly" and (weekday is None or not 0 <= int(weekday) <= 6):
        raise BadRequest("Choose the day of the week.")
    if freq == "monthly" and (dom is None or not 1 <= int(dom) <= 28):
        raise BadRequest("Choose a day of the month from 1 to 28.")
    people = [int(x) for x in data.get("recipient_employee_ids", s.recipient_employee_ids or [])]
    emails = [str(x).strip().lower() for x in data.get("recipient_emails", s.recipient_emails or []) if str(x).strip()]
    for e in emails:
        if "@" not in e or " " in e:
            raise BadRequest(f"“{e}” is not an email address.")
    if not people and not emails:
        raise BadRequest("Add at least one person to send it to.")
    s.name, s.frequency, s.hour, s.format = name[:120], freq, hour, fmt
    s.weekday = int(weekday) if freq == "weekly" else None
    s.day_of_month = int(dom) if freq == "monthly" else None
    s.recipient_employee_ids, s.recipient_emails = people, emails
    if "active" in data:
        s.active = bool(data["active"])
    s.next_run_at = next_run(s, _company_zone(db, p.company_id)) if s.active else None
    db.flush()
    return s


def run_schedule(db: Session, s: ReportSchedule) -> str:
    """Run one schedule now: build the file with the access of the person who set it up and
    email it. Returns the status."""
    from modules.employees.models import Employee
    r = db.query(Report).filter(Report.id == s.report_id).first()
    zone = _company_zone(db, s.company_id)
    s.last_run_at = now()
    try:
        if r is None or r.archived_at:
            raise PermissionError("The report was removed.")
        p = registry.principal_for_employee(db, s.company_id, s.run_as_employee_id, "schedule")
        if not can_view(db, p, r):
            raise PermissionError("The person who set this up can no longer see the report.")
        result = run_report(db, p, r)
        log, _pw = export(db, p, result, title=r.name, fmt=s.format, report=r, channel="schedule")
        emails = list(s.recipient_emails or [])
        if s.recipient_employee_ids:
            emails += [e for (e,) in db.query(Employee.email).filter(Employee.company_id == s.company_id,
                                                                    Employee.id.in_(s.recipient_employee_ids),
                                                                    Employee.active.is_(True)).all()]
        log.details = {**(log.details or {}), "recipients": emails}
        from modules.reports import mailer
        ok, msg = mailer.send(db, s.company_id, emails, f"{r.name} — {now().astimezone(zone):%d %b %Y}",
                              f"Hello,\n\nAttached is the report “{r.name}” ({result['row_count']} rows, "
                              f"{result['scope_label'].lower()} of {p.name or 'the company owner'}).\n\n"
                              f"This email was sent by the schedule “{s.name}”.",
                              [(log.file_name, log.content, log.content_type)])
        s.last_status, s.last_message = ("sent" if ok else "not_sent"), msg[:300]
    except Exception as exc:
        s.last_status, s.last_message = "failed", str(exc)[:300]
        logger.warning("Report schedule %s failed: %s", s.id, exc)
    s.next_run_at = next_run(s, zone, now()) if s.active else None
    return s.last_status


def run_due(db: Session) -> int:
    """Periodic job: send every schedule that is due (each in its own company context)."""
    rows = db.query(ReportSchedule).filter(ReportSchedule.active.is_(True), ReportSchedule.next_run_at <= now()).all()
    n = 0
    for s in rows:
        prev = db.info.get("company_id")
        db.info["company_id"] = s.company_id
        context.current().company_id = s.company_id
        try:
            with db.begin_nested():
                run_schedule(db, s)
                n += 1
        finally:
            db.info["company_id"] = prev
    return n + purge_expired_exports(db)


# ── Standard reports ───────────────────────────────────────────────

def ensure_standard(db: Session, company_id: int) -> int:
    """Add missing standard reports and keep existing ones in step with the definitions."""
    from modules.reports.standard import STANDARD
    existing = {r.key: r for r in db.query(Report).filter(Report.company_id == company_id,
                                                         Report.is_standard.is_(True)).all()}
    added = 0
    for d in STANDARD:
        src = registry.get(d["source"])
        if src is None:
            continue
        r = existing.get(d["key"])
        if r is None:
            db.add(Report(company_id=company_id, key=d["key"], name=d["name"], description=d["description"],
                          category=d["category"], source=d["source"], config=d["config"], chart=d["chart"],
                          is_standard=True, owner_name="Next Gen HRMS", scope="auto"))
            added += 1
            continue
        for attr in ("name", "description", "category", "source", "config", "chart"):
            if getattr(r, attr) != d[attr]:
                setattr(r, attr, d[attr])
    db.flush()
    return added
