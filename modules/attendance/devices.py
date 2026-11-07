"""Attendance devices of any make (biometric, RFID card, face), and the ways their
punches reach us:

(a) push     the device, its middleware or our office connector posts punches to
             /attendance/devices/push with the device's own API key (header X-Device-Key)
(b) file     HR uploads a CSV or Excel export and maps its columns
(c) ftp      we pull export files from an FTP/SFTP folder on a schedule (poll_due_devices)
(d) api      we call the vendor's or middleware's web API on a schedule       (device_api)
(e) database we read the attendance software's database                        (device_db)
(f) zk       we read a ZKTeco-protocol device over the LAN, port 4370          (device_zk)
(g) adms     ZKTeco / eSSL "cloud push": the device calls /iclock/...          (device_adms)
(h) webhook  the device or software posts its own JSON (Hikvision and others)
(i) email    CSV / Excel attachments from a mailbox                            (device_email)

(d)-(i) live in device_sources; database and zk can also run on the office connector.

Device user ids are mapped to employees by enrolments (per device, or for every device).
Duplicate punches (same person, same second) are ignored. Every sync is logged.
"""
from __future__ import annotations

import csv
import fnmatch
import hashlib
import hmac
import io
import logging
import secrets
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from core import audit
from core.errors import BadRequest, NotFound
from core.timeutil import iso_utc, is_valid_timezone
from modules.attendance import service
from modules.attendance.models import Device, DeviceEnrolment, DeviceSyncLog, Punch

log = logging.getLogger("hrms.devices")

VENDORS = ["eSSL", "ZKTeco", "Realtime", "Matrix", "Suprema", "Mantra", "BioMax", "Hikvision", "Other"]
KINDS = {"biometric": "Fingerprint", "face": "Face recognition", "rfid": "RFID card", "other": "Other"}
CONNECTIONS = {
    "adms": "ADMS cloud push (ZKTeco, eSSL)",
    "push": "Direct push (our API)",
    "webhook": "Vendor push (Hikvision, any JSON)",
    "api": "Vendor software API",
    "database": "Database read",
    "zk": "LAN pull (ZKTeco, port 4370)",
    "ftp": "FTP / SFTP folder",
    "email": "Email attachments",
    "file": "File upload (CSV / Excel)",
}
NEEDS_KEY = ("push", "webhook")
# where a punch came from, by the kind of sync
SOURCE_OF_KIND = {"file": "file", "ftp": "ftp", "email": "file", "api": "api", "database": "database"}
MAX_BATCH = 5000

IN_WORDS = {"in", "i", "0", "checkin", "check-in", "check in", "c/in", "entry", "ci"}
OUT_WORDS = {"out", "o", "1", "checkout", "check-out", "check out", "c/out", "exit", "co"}
DATE_FORMATS = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
                "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M",
                "%d-%m-%Y %I:%M %p", "%d/%m/%Y %I:%M %p", "%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M"]


# ── Keys ────────────────────────────────────────────────────────────

def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_key(device: Device) -> str:
    key = "dev_" + secrets.token_urlsafe(30)
    device.api_key_prefix = key[:12]
    device.api_key_hash = _hash(key)
    return key


def device_for_key(db: Session, key: str | None) -> Device | None:
    """Find a device by its API key (across companies; RLS is bypassed for the lookup)."""
    from core.db import bypass_rls
    if not key or len(key) < 20:
        return None
    with bypass_rls(db):
        rows = db.query(Device).filter(Device.api_key_prefix == key[:12], Device.active.is_(True)).all()
        found = next((d for d in rows if d.api_key_hash and hmac.compare_digest(d.api_key_hash, _hash(key))), None)
    return found


# ── CRUD ────────────────────────────────────────────────────────────

def save(db: Session, company_id: int, data: dict, device: Device | None = None) -> tuple[Device, str | None]:
    name = (data.get("name") or (device.name if device else "")).strip()
    if not name:
        raise BadRequest("Give the device a name.")
    conn = data.get("connection", device.connection if device else "push")
    if conn not in CONNECTIONS:
        raise BadRequest("Choose how the device sends punches.")
    kind = data.get("kind", device.kind if device else "biometric")
    if kind not in KINDS:
        raise BadRequest("Choose the kind of device.")
    tz = (data.get("timezone") or "").strip() or None
    if tz and not is_valid_timezone(tz):
        raise BadRequest("That timezone isn't recognised.")
    from modules.attendance import device_sources
    created = device is None
    if created:
        device = Device(company_id=company_id)
        db.add(device)
    if device.connection != conn and not created:
        device.sync_state = None  # a new connection starts reading afresh
    device.name, device.connection, device.kind, device.timezone = name, conn, kind, tz
    for k in ("vendor", "model", "serial_number", "location_name"):
        if k in data:
            setattr(device, k, (data[k] or "").strip() or None)
    if conn == "adms":
        _check_adms_serial(db, device)
    runs_on = data.get("runs_on", device.runs_on or "server")
    device.runs_on = runs_on if (runs_on in ("server", "connector") and conn in device_sources.CONNECTOR_CAPABLE) else "server"
    if "source_config" in data and data["source_config"] is not None:
        old = device.source_config if device.connection == conn else None
        device.source_config = device_sources.clean(conn, data["source_config"], old)
        if device.source_config and device.source_config.get("engine") == "access":
            device.runs_on = "connector"
        device.next_pull_at = datetime.now(timezone.utc)
    if conn in device_sources.PULLED and not device.source_config:
        raise BadRequest("Fill in the connection settings.")
    if "active" in data:
        device.active = bool(data["active"])
    if "ftp_config" in data and data["ftp_config"] is not None:
        device.ftp_config = _clean_ftp(data["ftp_config"], device.ftp_config)
        device.next_pull_at = datetime.now(timezone.utc)
    if "column_mapping" in data and data["column_mapping"] is not None:
        device.column_mapping = _clean_mapping(data["column_mapping"])
    if conn == "ftp" and not (device.ftp_config or {}).get("host"):
        raise BadRequest("Enter the FTP server address.")
    key = None
    if (conn in NEEDS_KEY or device.runs_on == "connector") and not device.api_key_hash:
        key = new_key(device)
    db.flush()
    return device, key


MASK = "••••••"


def _unmasked(value):
    return None if value == MASK else value


def _check_adms_serial(db: Session, device: Device) -> None:
    """ADMS devices are recognised by serial number alone: it must be unique everywhere."""
    from core.db import bypass_rls
    sn = (device.serial_number or "").strip()
    if not sn:
        raise BadRequest("Enter the device's serial number (Menu, System info on the device): it is how we recognise it.")
    device.serial_number = sn
    with bypass_rls(db):
        q = db.query(Device.id).filter(Device.connection == "adms", Device.serial_number == sn)
        if device.id:
            q = q.filter(Device.id != device.id)
        taken = q.first()
    if taken:
        raise BadRequest("A device with this serial number is already connected. If it moved to your company, "
                         "ask the previous owner to remove it, or contact support.")


def _clean_ftp(cfg: dict, old: dict | None) -> dict:
    old = old or {}
    protocol = cfg.get("protocol") or "ftp"
    if protocol not in ("ftp", "ftps", "sftp"):
        raise BadRequest("Choose FTP, FTPS or SFTP.")
    host = (cfg.get("host") or "").strip()
    if not host:
        raise BadRequest("Enter the FTP server address.")
    every = int(cfg.get("every_minutes") or 30)
    if every < 5 or every > 1440:
        raise BadRequest("Check the folder every 5 to 1440 minutes.")
    out = {
        "protocol": protocol, "host": host,
        "port": int(cfg.get("port") or (22 if protocol == "sftp" else 21)),
        "username": (cfg.get("username") or "").strip(),
        # keep the saved password when the form leaves it empty
        "password": _unmasked(cfg.get("password")) or old.get("password") or "",
        # SFTP: a private key (PEM text) instead of or with the password, and the server's key
        # fingerprint ("SHA256:...") to refuse a server that isn't the expected one
        "private_key": _unmasked(cfg.get("private_key")) or old.get("private_key") or "",
        "host_key": (cfg.get("host_key") or old.get("host_key") or "").strip(),
        "path": (cfg.get("path") or "/").strip() or "/",
        "file_pattern": (cfg.get("file_pattern") or "*.csv").strip(),
        "every_minutes": every, "delete_after": bool(cfg.get("delete_after")),
        "processed": old.get("processed", []),
    }
    return out


def _clean_mapping(m: dict) -> dict:
    out = {k: (str(m.get(k)).strip() if m.get(k) not in (None, "") else None)
           for k in ("employee", "timestamp", "date", "time", "direction", "date_format")}
    out["match_by"] = m.get("match_by") if m.get("match_by") in ("device_user_id", "employee_code") else "device_user_id"
    out["has_header"] = bool(m.get("has_header", True))
    if not out["employee"]:
        raise BadRequest("Choose the column with the employee or device user id.")
    if not out["timestamp"] and not (out["date"] and out["time"]):
        raise BadRequest("Choose the date-and-time column, or separate date and time columns.")
    return out


def device_out(d: Device, enrolled: int = 0) -> dict:
    from modules.attendance import device_sources
    ftp = dict(d.ftp_config or {})
    if ftp:
        ftp["password"] = MASK if ftp.get("password") else ""
        ftp["private_key"] = MASK if ftp.get("private_key") else ""
        ftp.pop("processed", None)
    return {"uuid": str(d.uuid), "id": d.id, "name": d.name, "vendor": d.vendor, "model": d.model,
            "serial_number": d.serial_number, "kind": d.kind, "kind_label": KINDS.get(d.kind, d.kind),
            "location_name": d.location_name, "timezone": d.timezone, "connection": d.connection,
            "connection_label": CONNECTIONS.get(d.connection), "api_key_prefix": d.api_key_prefix,
            "ftp_config": ftp or None, "column_mapping": d.column_mapping, "active": d.active,
            "last_seen_at": iso_utc(d.last_seen_at), "last_sync_at": iso_utc(d.last_sync_at),
            "next_pull_at": iso_utc(d.next_pull_at), "enrolled": enrolled, "created_at": iso_utc(d.created_at),
            "source_config": device_sources.config_out(d), "runs_on": d.runs_on or "server",
            "state": device_sources.state_out(d), **device_sources.status_out(d)}


def get(db: Session, company_id: int, uuid) -> Device:
    d = db.query(Device).filter(Device.company_id == company_id, Device.uuid == uuid).first()
    if not d:
        raise NotFound("Device")
    return d


# ── Enrolments ──────────────────────────────────────────────────────

def enrol(db: Session, company_id: int, device_id: int | None, device_user_id: str, employee_id: int,
          card_number: str | None = None) -> DeviceEnrolment:
    duid = (device_user_id or "").strip()
    if not duid:
        raise BadRequest("Enter the user id on the device.")
    service.employee(db, company_id, employee_id)
    q = db.query(DeviceEnrolment).filter(DeviceEnrolment.company_id == company_id,
                                         DeviceEnrolment.device_user_id == duid)
    q = q.filter(DeviceEnrolment.device_id == device_id) if device_id else q.filter(DeviceEnrolment.device_id.is_(None))
    row = q.first()
    if row is None:
        row = DeviceEnrolment(company_id=company_id, device_id=device_id, device_user_id=duid, employee_id=employee_id)
        db.add(row)
    row.employee_id = employee_id
    row.card_number = (card_number or "").strip() or None
    db.flush()
    return row


def _resolver(db: Session, company_id: int, device: Device | None, match_by: str = "device_user_id"):
    rows = db.query(DeviceEnrolment).filter(DeviceEnrolment.company_id == company_id).all()
    specific = {r.device_user_id: r.employee_id for r in rows if device and r.device_id == device.id}
    general = {r.device_user_id: r.employee_id for r in rows if r.device_id is None}
    cards = {r.card_number: r.employee_id for r in rows if r.card_number}
    by_code = {}
    if match_by == "employee_code" or not rows:
        from modules.employees.models import Employee
        by_code = {c: i for i, c in db.query(Employee.id, Employee.employee_code).filter(
            Employee.company_id == company_id, Employee.employee_code.isnot(None)).all()}

    def resolve(uid: str) -> int | None:
        uid = (uid or "").strip()
        if match_by == "employee_code":
            return by_code.get(uid)
        return specific.get(uid) or general.get(uid) or cards.get(uid) or by_code.get(uid)
    return resolve


# ── Ingest ──────────────────────────────────────────────────────────

def _direction(value) -> str | None:
    v = str(value or "").strip().lower()
    if v in IN_WORDS:
        return "in"
    if v in OUT_WORDS:
        return "out"
    return None


def parse_timestamp(value, tz: ZoneInfo, fmt: str | None = None) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value or "").strip()
        if not s:
            raise ValueError("empty time")
        dt = None
        if fmt:
            dt = datetime.strptime(s, fmt)
        else:
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                for f in DATE_FORMATS:
                    try:
                        dt = datetime.strptime(s, f)
                        break
                    except ValueError:
                        continue
        if dt is None:
            raise ValueError(f"can't read the time “{s}”")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def ingest(db: Session, company_id: int, device: Device | None, records: list[dict], kind: str,
           file_name: str | None = None, match_by: str = "device_user_id") -> DeviceSyncLog:
    """Save punches: [{"device_user_id", "timestamp", "direction"?}] (timestamps may be
    strings or datetimes; without a zone they are the device's or the company's local time)."""
    tz_name = (device.timezone if device else None)
    tz = ZoneInfo(tz_name) if tz_name else service.company_tz(db, company_id)
    resolve = _resolver(db, company_id, device, match_by)
    entry = DeviceSyncLog(company_id=company_id, device_id=device.id if device else None, kind=kind,
                          file_name=file_name, received=len(records), status="ok", errors=[],
                          inserted=0, duplicates=0, unmatched=0)
    errors, unmatched_ids, parsed = [], set(), []
    for i, rec in enumerate(records[:MAX_BATCH]):
        uid = str(rec.get("device_user_id") or rec.get("user_id") or "").strip()
        try:
            ts = parse_timestamp(rec.get("timestamp"), tz, rec.get("_fmt"))
        except (ValueError, TypeError) as exc:
            errors.append({"row": rec.get("_row", i + 1), "message": str(exc)})
            continue
        emp_id = resolve(uid)
        if emp_id is None:
            unmatched_ids.add(uid)
            entry.unmatched += 1
            continue
        direction = rec.get("direction")
        parsed.append((emp_id, ts, direction if direction in ("in", "out", None) else _direction(direction), uid,
                       rec.get("_device_id")))
    if len(records) > MAX_BATCH:
        errors.append({"row": MAX_BATCH + 1, "message": f"Only the first {MAX_BATCH} punches are read at a time."})
    seen = set()
    if parsed:
        emp_ids = list({p[0] for p in parsed})
        lo, hi = min(p[1] for p in parsed), max(p[1] for p in parsed)
        seen = {(e, t) for e, t in db.query(Punch.employee_id, Punch.punched_at).filter(
            Punch.company_id == company_id, Punch.employee_id.in_(emp_ids),
            Punch.punched_at >= lo, Punch.punched_at <= hi).all()}
    new_items = []
    for emp_id, ts, direction, uid, dev_id in parsed:
        if (emp_id, ts) in seen:
            entry.duplicates += 1
            continue
        seen.add((emp_id, ts))
        db.add(Punch(company_id=company_id, employee_id=emp_id, punched_at=ts, direction=direction,
                     source=SOURCE_OF_KIND.get(kind, "device"),
                     device_id=dev_id or (device.id if device else None), device_user_id=uid))
        new_items.append((emp_id, ts))
        entry.inserted += 1
    if unmatched_ids:
        errors.append({"row": None, "message": "Not enrolled: " + ", ".join(sorted(unmatched_ids)[:20])
                       + ("…" if len(unmatched_ids) > 20 else "")})
    entry.errors = errors[:100]
    entry.status = "failed" if (records and not entry.inserted and not entry.duplicates) else (
        "partial" if errors else "ok")
    entry.message = (f"{entry.inserted} new, {entry.duplicates} already there, {entry.unmatched} not enrolled"
                     + (f", {len([e for e in errors if e['row']])} unreadable" if errors else ""))
    db.add(entry)
    if device is not None:
        device.last_sync_at = datetime.now(timezone.utc)
        if kind in ("push", "adms", "webhook", "connector"):
            device.last_seen_at = device.last_sync_at
    db.flush()
    service.recompute_for_punch_times(db, company_id, new_items)
    return entry


def log_out(l: DeviceSyncLog, device_name: str | None = None) -> dict:
    return {"uuid": str(l.uuid), "device_name": device_name, "kind": l.kind, "status": l.status, "file_name": l.file_name,
            "received": l.received, "inserted": l.inserted, "duplicates": l.duplicates, "unmatched": l.unmatched,
            "errors": l.errors or [], "message": l.message, "created_at": iso_utc(l.created_at)}


# ── Files (CSV / Excel) ─────────────────────────────────────────────

def read_table(content: bytes, file_name: str) -> list[list]:
    name = (file_name or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        try:
            wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        except Exception:
            raise BadRequest("This Excel file can't be read. Save it as .xlsx or CSV and try again.")
        ws = wb.worksheets[0]
        return [[c for c in row] for row in ws.iter_rows(values_only=True) if any(c not in (None, "") for c in row)]
    if name.endswith(".xls"):
        raise BadRequest("Old .xls files aren't supported. Save it as .xlsx or CSV.")
    text_ = None
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text_ = content.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    sample = text_[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [row for row in csv.reader(io.StringIO(text_), dialect) if any((c or "").strip() for c in row)]


def _guess(headers: list[str]) -> dict:
    low = [str(h or "").strip().lower() for h in headers]

    def find(*words):
        for w in words:
            for i, h in enumerate(low):
                if w in h:
                    return headers[i]
        return None
    ts = find("datetime", "date time", "punch time", "logtime", "log time", "timestamp", "checktime")
    return {"employee": find("enroll", "user id", "userid", "emp code", "empcode", "employee code", "badge", "card", "id"),
            "timestamp": ts, "date": None if ts else find("date"), "time": None if ts else find("time"),
            "direction": find("in/out", "inout", "direction", "status", "type", "state"),
            "date_format": None, "match_by": "employee_code" if find("emp code", "employee code", "empcode") else "device_user_id",
            "has_header": True}


def preview(content: bytes, file_name: str) -> dict:
    rows = read_table(content, file_name)
    if not rows:
        raise BadRequest("The file is empty.")
    headers = [str(h if h is not None else f"Column {i + 1}") for i, h in enumerate(rows[0])]
    sample = [[(c.isoformat(sep=" ") if isinstance(c, datetime) else c) for c in r] for r in rows[1:11]]
    return {"headers": headers, "rows": sample, "total_rows": len(rows) - 1, "guess": _guess(headers)}


def records_from_table(rows: list[list], mapping: dict) -> list[dict]:
    mapping = _clean_mapping(mapping)
    if mapping["has_header"]:
        headers = [str(h if h is not None else f"Column {i + 1}") for i, h in enumerate(rows[0])]
        body = rows[1:]
    else:
        headers = [f"Column {i + 1}" for i in range(len(rows[0]))]
        body = rows

    def col(name):
        if not name:
            return None
        if name not in headers:
            raise BadRequest(f"The file has no column called {name}.")
        return headers.index(name)
    ci, ct, cd, ctm, cdir = (col(mapping["employee"]), col(mapping["timestamp"]), col(mapping["date"]),
                             col(mapping["time"]), col(mapping["direction"]))
    out = []
    for n, r in enumerate(body, start=2 if mapping["has_header"] else 1):
        def at(i):
            return r[i] if i is not None and i < len(r) else None
        if ct is not None:
            ts = at(ct)
        else:
            dv, tv = at(cd), at(ctm)
            if isinstance(dv, datetime):
                dv = dv.date()
            if isinstance(dv, date) and isinstance(tv, (time, datetime)):
                ts = datetime.combine(dv, tv if isinstance(tv, time) else tv.time())
            else:
                ts = f"{dv.isoformat() if isinstance(dv, date) else dv} {tv.strftime('%H:%M:%S') if isinstance(tv, time) else tv}"
        uid = at(ci)
        if isinstance(uid, float) and uid.is_integer():
            uid = int(uid)
        out.append({"device_user_id": str(uid if uid is not None else ""), "timestamp": ts,
                    "direction": at(cdir), "_row": n, "_fmt": mapping.get("date_format")})
    return out


def import_file(db: Session, company_id: int, device: Device | None, content: bytes, file_name: str,
                mapping: dict, kind: str = "file") -> DeviceSyncLog:
    rows = read_table(content, file_name)
    if not rows:
        raise BadRequest("The file is empty.")
    records = records_from_table(rows, mapping)
    entry = ingest(db, company_id, device, records, kind, file_name, (mapping or {}).get("match_by") or "device_user_id")
    if device is not None and kind == "file" and mapping:
        device.column_mapping = _clean_mapping(mapping)
    audit.event(db, "attendance.punches_imported", "attendance_devices", device.id if device else None,
                {"file": file_name, "inserted": entry.inserted, "duplicates": entry.duplicates,
                 "unmatched": entry.unmatched}, entity_uuid=str(device.uuid) if device else None,
                entity_label=device.name if device else file_name)
    return entry


# ── FTP / SFTP pull ─────────────────────────────────────────────────

def _list_and_fetch(cfg: dict):
    """Yield (file name, bytes, remover) for files matching the pattern."""
    protocol = cfg.get("protocol", "ftp")
    if protocol == "sftp":
        yield from _sftp_files(cfg)
        return
    import ftplib
    ftp = ftplib.FTP_TLS() if protocol == "ftps" else ftplib.FTP()
    ftp.connect(cfg["host"], int(cfg.get("port") or 21), timeout=30)
    ftp.login(cfg.get("username") or "anonymous", cfg.get("password") or "")
    if protocol == "ftps":
        ftp.prot_p()
    try:
        ftp.cwd(cfg.get("path") or "/")
        for name in sorted(ftp.nlst()):
            if fnmatch.fnmatch(name, cfg.get("file_pattern") or "*"):
                buf = io.BytesIO()
                ftp.retrbinary(f"RETR {name}", buf.write)
                yield name, buf.getvalue(), (lambda n=name: ftp.delete(n))
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()


def _sftp_key(paramiko, text: str):
    """A private key from PEM text (Ed25519, ECDSA or RSA)."""
    last = None
    for cls in (getattr(paramiko, "Ed25519Key", None), getattr(paramiko, "ECDSAKey", None), paramiko.RSAKey):
        if cls is None:
            continue
        try:
            return cls.from_private_key(io.StringIO(text))
        except Exception as exc:  # wrong key type: try the next one
            last = exc
    raise RuntimeError(f"The SFTP private key could not be read: {last}")


def host_key_fingerprint(key) -> str:
    import base64
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


def _sftp_files(cfg: dict):
    """Yield (file name, bytes, remover) from an SFTP folder (paramiko)."""
    try:
        import paramiko  # type: ignore
    except ImportError:
        raise RuntimeError("SFTP needs the paramiko package on the server; use FTP or FTPS, or install it.")
    transport = paramiko.Transport((cfg["host"], int(cfg.get("port") or 22)))
    try:
        transport.banner_timeout = 30
        pkey = _sftp_key(paramiko, cfg["private_key"]) if cfg.get("private_key") else None
        transport.connect(username=cfg.get("username") or None, password=cfg.get("password") or None, pkey=pkey)
        expected = (cfg.get("host_key") or "").strip()
        if expected:
            got = host_key_fingerprint(transport.get_remote_server_key())
            if got.rstrip("=") != expected.rstrip("="):
                raise RuntimeError(f"The SFTP server's key ({got}) is not the one saved for this device.")
        sftp = paramiko.SFTPClient.from_transport(transport)
        try:
            path = cfg.get("path") or "/"
            for name in sorted(sftp.listdir(path)):
                if fnmatch.fnmatch(name, cfg.get("file_pattern") or "*"):
                    full = f"{path.rstrip('/')}/{name}"
                    buf = io.BytesIO()
                    sftp.getfo(full, buf)
                    yield name, buf.getvalue(), (lambda f=full: sftp.remove(f))
        finally:
            sftp.close()
    finally:
        transport.close()


def poll_device(db: Session, device: Device, fetch=_list_and_fetch) -> list[DeviceSyncLog]:
    """Read new export files from the device's FTP/SFTP folder. `fetch` can be replaced
    in tests. Files already read are remembered and skipped."""
    cfg = dict(device.ftp_config or {})
    processed = list(cfg.get("processed") or [])
    logs = []
    mapping = device.column_mapping or {"employee": "UserID", "timestamp": "DateTime", "match_by": "device_user_id"}
    try:
        for name, content, remove in fetch(cfg):
            key = f"{name}:{len(content)}"
            if key in processed:
                continue
            try:
                with db.begin_nested():
                    logs.append(import_file(db, device.company_id, device, content, name, mapping, kind="ftp"))
                processed.append(key)
                if cfg.get("delete_after"):
                    try:
                        remove()
                    except Exception as exc:
                        log.warning("could not delete %s: %s", name, exc)
            except Exception as exc:
                detail = getattr(exc, "detail", None) or str(exc)
                entry = DeviceSyncLog(company_id=device.company_id, device_id=device.id, kind="ftp", file_name=name,
                                      status="failed", errors=[{"row": None, "message": detail}], message=detail[:500])
                db.add(entry)
                logs.append(entry)
    except Exception as exc:
        entry = DeviceSyncLog(company_id=device.company_id, device_id=device.id, kind="ftp", status="failed",
                              errors=[{"row": None, "message": str(exc)}],
                              message=f"Couldn't reach the FTP folder: {exc}"[:500])
        db.add(entry)
        logs.append(entry)
    cfg["processed"] = processed[-300:]
    device.ftp_config = cfg
    now = datetime.now(timezone.utc)
    device.last_sync_at = now
    device.next_pull_at = now + timedelta(minutes=int(cfg.get("every_minutes") or 30))
    db.flush()
    return logs


def poll_due_devices(db: Session) -> int:
    """Periodic job: pull files for every FTP device that is due (all companies)."""
    now = datetime.now(timezone.utc)
    due = (db.query(Device).filter(Device.connection == "ftp", Device.active.is_(True))
           .filter((Device.next_pull_at.is_(None)) | (Device.next_pull_at <= now)).all())
    n = 0
    for d in due:
        db.info["company_id"] = d.company_id
        n += sum(l.inserted for l in poll_device(db, d))
    db.info.pop("company_id", None)
    return n
