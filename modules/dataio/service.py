"""Import wizard and data exports.

Import: upload → map columns (auto-matched) → choose add / update / add-or-update and the
key to match on → validate (row-level errors, preview, nothing saved) → run in the
background with progress → download an error file. Exports produce the same columns as
the import template, so a file can be exported, edited and imported back.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from core import audit, context
from core.errors import BadRequest, Forbidden, NotFound
from modules.dataio import entities
from modules.dataio.models import DataJob
from modules.reports.registry import Principal

logger = logging.getLogger("hrms")

MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 20_000
CHUNK = 25
MODES = {"add": "Add new records", "update": "Update existing records", "upsert": "Add new and update existing"}
MODULE_PERMS = {
    "employees": ("people.employees.manage", "people.employees.export"),
    "salaries": ("payroll.run", "payroll.view"),
    "leave_balances": ("time.leave.manage", "time.leave.manage"),
    "attendance_punches": ("time.attendance.manage", "time.attendance.manage"),
    "holidays": ("settings.leave.manage", "time.leave.view"),
    "assets": ("workplace.assets.manage", "workplace.assets.manage"),
    "masters": ("settings.masters.manage", "settings.masters.manage"),
    "departments": ("people.departments.manage", "people.departments.view"),
    "designations": ("people.designations.manage", "people.designations.view"),
}


def now() -> datetime:
    return datetime.now(timezone.utc)


def check(p: Principal, entity_key: str, kind: str = "import") -> None:
    base = "insights.data.import" if kind == "import" else "insights.data.export"
    if not p.can(base):
        raise Forbidden("You can't import data." if kind == "import" else "You can't export data.")
    need = MODULE_PERMS.get(entity_key, (None, None))[0 if kind == "import" else 1]
    if need and not p.can(need):
        raise Forbidden("You don't have access to this kind of record.")


def entities_for(p: Principal) -> list[dict]:
    out = []
    for e in entities.available():
        imp = exp = True
        try:
            check(p, e.key, "import")
        except Forbidden:
            imp = False
        try:
            check(p, e.key, "export")
        except Forbidden:
            exp = False
        if imp or exp:
            out.append({**e.out(), "can_import": imp, "can_export": exp and e.export_rows is not None})
    return out


# ── Reading files ──────────────────────────────────────────────────

def read_table(content: bytes, file_name: str) -> list[list]:
    name = (file_name or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        try:
            wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        except Exception:
            raise BadRequest("This Excel file can't be read. Save it as .xlsx or CSV and try again.")
        ws = wb.worksheets[0]
        rows = []
        for row in ws.iter_rows(values_only=True):
            rows.append(list(row))
            if len(rows) > MAX_ROWS + 5:
                break
        while rows and not any(c not in (None, "") for c in rows[-1]):
            rows.pop()
        return rows
    if name.endswith(".xls"):
        raise BadRequest("Old .xls files aren't supported. Save it as .xlsx or CSV.")
    if not name.endswith((".csv", ".txt")):
        raise BadRequest("Upload an Excel (.xlsx) or CSV file.")
    text = None
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = content.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [row for row in csv.reader(io.StringIO(text), dialect)]


def _split(rows: list[list]) -> tuple[list[str], list[tuple[int, list]]]:
    """Header row and data rows with their row numbers in the file (blank rows dropped)."""
    start = next((i for i, r in enumerate(rows) if any(c not in (None, "") for c in r)), None)
    if start is None:
        raise BadRequest("The file is empty.")
    headers = [str(h).strip() if h not in (None, "") else f"Column {i + 1}" for i, h in enumerate(rows[start])]
    data = [(start + i + 2, r) for i, r in enumerate(rows[start + 1:]) if any(c not in (None, "") for c in r)]
    return headers, data


def _jsonable(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


# ── Jobs ───────────────────────────────────────────────────────────

def get_job(db: Session, p: Principal, uuid) -> DataJob:
    j = db.query(DataJob).filter(DataJob.company_id == p.company_id, DataJob.uuid == uuid).first()
    if j is None:
        raise NotFound("Import")
    if j.employee_id != p.employee_id and not (p.is_owner or p.role == "admin" or p.can("insights.data.import")):
        raise NotFound("Import")
    return j


def job_out(j: DataJob, full: bool = False) -> dict:
    e = entities.ENTITIES.get(j.entity)
    o = {"uuid": str(j.uuid), "kind": j.kind, "entity": j.entity, "entity_label": e.label if e else j.entity,
         "mode": j.mode, "mode_label": MODES.get(j.mode), "match_key": j.match_key, "file_name": j.file_name,
         "status": j.status, "total": j.total, "processed": j.processed, "added": j.added, "updated": j.updated,
         "skipped": j.skipped, "failed": j.failed, "message": j.message, "created_by_name": j.created_by_name,
         "created_at": j.created_at, "started_at": j.started_at, "finished_at": j.finished_at,
         "error_count": len(j.errors or []),
         "progress": round(j.processed * 100 / j.total) if j.total else (100 if j.status == "done" else 0),
         "export_uuid": (j.options or {}).get("export_uuid")}
    if full:
        o.update({"headers": j.headers, "mapping": j.mapping, "options": j.options, "preview": j.preview,
                  "errors": (j.errors or [])[:500], "entity_def": e.out() if e else None})
    return o


def create_import(db: Session, p: Principal, entity_key: str, file_name: str, content: bytes) -> DataJob:
    e = entities.get(entity_key)
    check(p, e.key)
    if not content:
        raise BadRequest("The file is empty.")
    if len(content) > MAX_BYTES:
        raise BadRequest("The file is larger than 10 MB. Split it into smaller files.")
    headers, data = _split(read_table(content, file_name))
    if not data:
        raise BadRequest("The file has a header row but no data rows.")
    if len(data) > MAX_ROWS:
        raise BadRequest(f"A file can have up to {MAX_ROWS:,} rows. Split it into smaller files.")
    mode = e.modes[0]
    j = DataJob(company_id=p.company_id, kind="import", entity=e.key, mode=mode,
                match_key=e.match_keys[0][0] if e.match_keys else None, file_name=file_name[:200],
                file_content=content, headers=headers, mapping=entities.auto_map(e, headers), total=len(data),
                employee_id=p.employee_id, created_by_name=p.name or "Company owner", status="uploaded",
                preview=[{"row": n, "values": {h: _jsonable(r[i]) if i < len(r) else None
                                              for i, h in enumerate(headers)}} for n, r in data[:5]])
    db.add(j)
    db.flush()
    return j


def configure(db: Session, p: Principal, j: DataJob, data: dict) -> DataJob:
    if j.status in ("queued", "running", "done"):
        raise BadRequest("This import has already run. Start a new one.")
    e = entities.get(j.entity)
    if "mapping" in data:
        mapping = {k: v for k, v in (data["mapping"] or {}).items() if v}
        cols = {c.key for c in e.columns}
        bad = [k for k in mapping if k not in cols]
        if bad:
            raise BadRequest(f"Unknown column {bad[0]}.")
        missing = [h for h in mapping.values() if h not in j.headers]
        if missing:
            raise BadRequest(f"The file has no column “{missing[0]}”.")
        j.mapping = mapping
    if data.get("mode"):
        if data["mode"] not in e.modes:
            raise BadRequest("Choose how to import.")
        j.mode = data["mode"]
    if "match_key" in data and data["match_key"]:
        if data["match_key"] not in {k for k, _ in e.match_keys}:
            raise BadRequest("Choose the column to match existing records on.")
        j.match_key = data["match_key"]
    j.status = "uploaded"
    db.flush()
    return j


def _mapped_rows(j: DataJob) -> list[tuple[int, dict, list]]:
    headers, data = _split(read_table(j.file_content, j.file_name))
    pos = {h: i for i, h in enumerate(headers)}
    out = []
    for n, r in data:
        vals = {k: (r[pos[h]] if pos.get(h) is not None and pos[h] < len(r) else None) for k, h in j.mapping.items()}
        out.append((n, vals, r))
    return out


def _context(db: Session, p: Principal, e: entities.Entity, j: DataJob, rows) -> dict:
    ctx = e.prepare(db, p.company_id) if e.prepare else {}
    ctx.update({"db": db, "company_id": p.company_id, "file_name": j.file_name})
    if e.key == "employees":
        refs = set()
        for _, v, _ in rows:
            for k in ("employee_code", "email"):
                if v.get(k):
                    refs.add(str(v[k]).strip().lower())
        ctx["_file_refs"] = refs
    if e.key == "departments":
        ctx["_file_names"] = {entities.norm(v.get("name")) for _, v, _ in rows if v.get("name")}
    return ctx


def _check_rows(db: Session, p: Principal, j: DataJob) -> tuple[list[dict], dict]:
    """Validate every row without saving. Returns per-row results and the context."""
    e = entities.get(j.entity)
    missing = [c.label for c in e.columns if c.required and c.key not in j.mapping]
    if missing:
        raise BadRequest("Match these required columns first: " + ", ".join(missing) + ".")
    rows = _mapped_rows(j)
    ctx = _context(db, p, e, j, rows)
    results, seen = [], {}
    for n, vals, raw in rows:
        clean, errs = e.validate(ctx, vals)
        errors = [{"row": n, "column": c, "message": m} for c, m in errs]
        existing = e.find(ctx, clean, j.match_key) if e.find and not errors else None
        action = "update" if existing is not None else "add"
        if not errors:
            if j.mode == "add" and existing is not None and e.match_keys:
                errors.append({"row": n, "column": j.match_key, "message": "Already exists (choose update to change it)"})
            elif j.mode == "update" and existing is None and e.key != "leave_balances":
                errors.append({"row": n, "column": j.match_key, "message": "No existing record matches this row"})
        key = None
        if e.match_keys and j.match_key and not errors:
            key = str(_key_of(clean, j.match_key))
            if key in seen and key not in ("None", ""):
                errors.append({"row": n, "column": j.match_key,
                               "message": f"Same record as row {seen[key]}; only the first is used"})
            else:
                seen[key] = n
        results.append({"row": n, "clean": clean, "existing": existing, "action": action, "errors": errors,
                        "raw": raw, "values": vals})
    return results, ctx


def _key_of(clean: dict, key: str):
    if key in clean:
        return clean.get(key)
    if key == "employee_effective":
        return (getattr(clean.get("_emp"), "id", None), clean.get("effective_from"))
    if key == "employee_type":
        return (getattr(clean.get("_emp"), "id", None), getattr(clean.get("_type"), "id", None))
    if key == "calendar_date_name":
        return (getattr(clean.get("_cal"), "id", None), clean.get("date"), (clean.get("name") or "").lower())
    if key == "calendar_date":
        return (getattr(clean.get("_cal"), "id", None), clean.get("date"))
    if key in ("list_label", "list_code"):
        return (getattr(clean.get("_type"), "id", None), clean.get("label" if key == "list_label" else "code"))
    return None


def validate(db: Session, p: Principal, j: DataJob) -> DataJob:
    check(p, j.entity)
    results, _ctx = _check_rows(db, p, j)
    errors = [err for r in results for err in r["errors"]]
    ok = [r for r in results if not r["errors"]]
    j.errors = errors[:2000]
    j.options = {**(j.options or {}), "would_add": sum(1 for r in ok if r["action"] == "add"),
                 "would_update": sum(1 for r in ok if r["action"] == "update"),
                 "rows_with_errors": len({e["row"] for e in errors})}
    j.preview = [{"row": r["row"], "action": "error" if r["errors"] else r["action"],
                  "values": {k: _jsonable(v) for k, v in r["values"].items()},
                  "errors": [e["message"] for e in r["errors"]]} for r in results[:50]]
    j.status = "validated"
    db.flush()
    return j


def queue(db: Session, p: Principal, j: DataJob) -> DataJob:
    check(p, j.entity)
    if j.status not in ("validated", "uploaded"):
        raise BadRequest("This import has already run.")
    j.status, j.processed, j.added, j.updated, j.skipped, j.failed = "queued", 0, 0, 0, 0, 0
    j.message = "Waiting to start"
    audit.event(db, "import.started", "data_jobs", j.id, {"entity": j.entity, "mode": j.mode, "rows": j.total,
                                                         "file": j.file_name}, entity_uuid=str(j.uuid),
                entity_label=f"Import {j.entity}")
    db.flush()
    return j


def run_job(company_id: int, job_id: int, employee_id: int | None, actor_name: str) -> None:
    """Background: apply every valid row, each in its own savepoint, committing progress
    every few rows. Rows that fail are kept with their reason for the error file."""
    ctx_ = context.RequestContext(company_id=company_id, employee_id=employee_id, actor_name=actor_name,
                                  actor_type="employee" if employee_id else "owner")
    with context.use_context(ctx_):
        _run_job(company_id, job_id, employee_id, actor_name)


def _run_job(company_id: int, job_id: int, employee_id: int | None, actor_name: str) -> None:
    from core.db import tenant_session
    from modules.reports.registry import principal_for_employee
    db = tenant_session(company_id)
    try:
        j = db.query(DataJob).filter(DataJob.id == job_id).first()
        if j is None or j.status != "queued":
            return
        p = principal_for_employee(db, company_id, employee_id, "web")
        p.name = actor_name
        j.status, j.started_at, j.message = "running", now(), "Checking the rows"
        db.commit()
        e = entities.get(j.entity)
        results, ctx = _check_rows(db, p, j)
        errors = []
        for i, r in enumerate(results, start=1):
            if r["errors"]:
                errors.extend(r["errors"])
                j.failed += 1
            else:
                try:
                    with db.begin_nested():
                        existing = e.find(ctx, r["clean"], j.match_key) if e.find else None
                        res = e.apply(ctx, r["clean"], existing, j.mode)
                    if res == "added":
                        j.added += 1
                    elif res == "updated":
                        j.updated += 1
                    else:
                        j.skipped += 1
                except Exception as exc:
                    msg = getattr(exc, "detail", None) or str(exc)
                    errors.append({"row": r["row"], "column": None, "message": str(msg)[:300]})
                    j.failed += 1
            j.processed = i
            if i % CHUNK == 0:
                j.errors = errors[:2000]
                j.message = f"{i} of {j.total} rows done"
                db.commit()
        j.errors = errors[:2000]
        j.status, j.finished_at = "done", now()
        j.message = (f"{j.added} added, {j.updated} updated, {j.skipped} unchanged, {j.failed} with problems")
        audit.event(db, "import.finished", "data_jobs", j.id,
                    {"entity": j.entity, "added": j.added, "updated": j.updated, "skipped": j.skipped,
                     "failed": j.failed}, entity_uuid=str(j.uuid), entity_label=f"Import {j.entity}")
        db.commit()
    except Exception as exc:  # pragma: no cover - unexpected failure
        logger.exception("Import %s failed", job_id)
        db.rollback()
        j = db.query(DataJob).filter(DataJob.id == job_id).first()
        if j is not None:
            j.status, j.finished_at = "failed", now()
            j.message = (getattr(exc, "detail", None) or str(exc))[:300]
            db.commit()
    finally:
        db.close()


def error_file(j: DataJob) -> bytes:
    """The rows that had problems, as in the upload, plus the row number and the reason."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    headers, data = _split(read_table(j.file_content, j.file_name)) if j.file_content else ([], [])
    by_row: dict = {}
    for err in j.errors or []:
        by_row.setdefault(err.get("row"), []).append(
            (f"{err['column']}: " if err.get("column") else "") + err["message"])
    wb = Workbook()
    ws = wb.active
    ws.title = "Rows to fix"
    ws.append(["Row", "Problem", *headers])
    for c in ws[1]:
        c.font = Font(color="9F1239")
    for n, r in data:
        if n in by_row:
            ws.append([n, "; ".join(by_row[n]), *[_jsonable(x) for x in r]])
    if None in by_row:
        ws.append(["", "; ".join(by_row[None])])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── Templates and exports ──────────────────────────────────────────

def _sheet(e: entities.Entity, rows: list[dict], with_help: bool) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    ws = wb.active
    ws.title = e.label[:31]
    ws.append([c.label for c in e.columns])
    for i, c in enumerate(e.columns, start=1):
        cell = ws.cell(row=1, column=i)
        cell.font = Font(color="2F3A8F" if not c.required else "9F1239")
        cell.fill = PatternFill("solid", fgColor="EEF0FB")
        ws.column_dimensions[get_column_letter(i)].width = max(14, len(c.label) + 4)
    for r in rows:
        vals = []
        for c in e.columns:
            v = r.get(c.key)
            if isinstance(v, Decimal):
                v = float(v)
            if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
                v = "'" + v
            vals.append(v)
        ws.append(vals)
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            if isinstance(cell.value, date):
                cell.number_format = "dd-mm-yyyy"
    ws.freeze_panes = "A2"
    if with_help:
        h = wb.create_sheet("How to fill")
        h.append(["Column", "Required", "What to write", "Example"])
        for c in e.columns:
            h.append([c.label, "Yes" if c.required else "", c.help or {"date": "A date, e.g. 14-02-2026",
                                                                       "number": "A number",
                                                                       "email": "An email address",
                                                                       "datetime": "Date and time, e.g. 2026-11-02 09:15",
                                                                       "bool": "Yes or No"}.get(c.type, ""),
                      c.example])
        h.column_dimensions["A"].width, h.column_dimensions["C"].width = 30, 60
        if e.note:
            h.append([])
            h.append([e.note])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def template(e: entities.Entity, fmt: str) -> tuple[bytes, str, str]:
    example = [{c.key: c.example for c in e.columns}]
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([c.label for c in e.columns])
        w.writerow([c.example for c in e.columns])
        return ("﻿" + buf.getvalue()).encode(), "text/csv; charset=utf-8", f"{e.key}-template.csv"
    return (_sheet(e, example, True),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", f"{e.key}-template.xlsx")


def export(db: Session, p: Principal, entity_key: str, fmt: str = "xlsx", protect: bool = False
           ) -> tuple[DataJob, str | None]:
    from modules.reports import render, service as reports
    e = entities.get(entity_key)
    check(p, e.key, "export")
    if e.export_rows is None:
        raise BadRequest("This data can't be exported yet.")
    rows = e.export_rows(db, p.company_id)
    if fmt == "csv" and not protect:
        buf = io.StringIO()
        from core import csv_safe
        w = csv_safe.writer(buf)
        w.writerow([c.label for c in e.columns])
        for r in rows:
            w.writerow([_jsonable(r.get(c.key)) for c in e.columns])
        data, ctype, ext = ("﻿" + buf.getvalue()).encode(), "text/csv; charset=utf-8", "csv"
    else:
        data, ctype, ext = _sheet(e, rows, False), render.CONTENT_TYPES["xlsx"], "xlsx"
    password = render.new_password() if protect else None
    if password:
        data = render.protect(data, "xlsx", password)
    sensitive = e.key in ("employees", "salaries")
    log = reports.record_export(db, p, what=f"{e.label} (import format)", fmt=ext, content=data,
                                file_name=f"{e.key}-{now():%Y%m%d-%H%M}.{ext}", content_type=ctype, rows=len(rows),
                                channel="data", source=e.key, scope="all", sensitive=sensitive,
                                protected=bool(password))
    j = DataJob(company_id=p.company_id, kind="export", entity=e.key, mode="add", status="done", total=len(rows),
                processed=len(rows), file_name=log.file_name, export_log_id=log.id, employee_id=p.employee_id,
                created_by_name=p.name or "Company owner", started_at=now(), finished_at=now(),
                message=f"{len(rows)} rows exported", options={"export_uuid": str(log.uuid)})
    db.add(j)
    db.flush()
    return j, password


def history(db: Session, p: Principal, kind: str | None = None):
    q = db.query(DataJob).filter(DataJob.company_id == p.company_id)
    if kind:
        q = q.filter(DataJob.kind == kind)
    if not (p.is_owner or p.role == "admin" or p.can("insights.data.import") or p.can("insights.data.export")):
        q = q.filter(DataJob.employee_id == p.employee_id)
    return q.order_by(DataJob.created_at.desc())
