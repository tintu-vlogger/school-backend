"""Ready-made settings for common attendance software and devices. A preset only fills the
connection form: every value stays editable, and a test shows the real fields before saving.

Field and table names come from the vendors' published software and from installations we
have seen; versions differ, so the test step is where an admin confirms them.
"""
from __future__ import annotations

# ── Vendor / middleware API (we call it on a schedule) ─────────────

_ESSL_SOAP = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <GetTransactionsLog xmlns="http://tempuri.org/">
      <FromDateTime>{from:%Y-%m-%d %H:%M}</FromDateTime>
      <ToDateTime>{to:%Y-%m-%d %H:%M}</ToDateTime>
      <SerialNumber>{serial}</SerialNumber>
      <UserName>{username}</UserName>
      <UserPassword>{password}</UserPassword>
      <strDataList></strDataList>
    </GetTransactionsLog>
  </soap:Body>
</soap:Envelope>"""

_BIOSTAR_SEARCH = ('{"Query": {"limit": {size}, "offset": {offset}, "conditions": [{"column": "datetime", "operator": 3, '
                   '"values": ["{from_utc:%Y-%m-%dT%H:%M:%S.000Z}", "{to_utc:%Y-%m-%dT%H:%M:%S.000Z}"]}], '
                   '"orders": [{"column": "datetime", "descending": false}]}}')

API_PRESETS = {
    "custom": {
        "label": "Custom", "vendor": None,
        "hint": "Any service that returns punches as JSON, XML or text.",
        "config": {"method": "GET", "url": "", "params": [{"name": "from", "value": "{from:%Y-%m-%d %H:%M:%S}"},
                                                         {"name": "to", "value": "{to:%Y-%m-%d %H:%M:%S}"}],
                   "headers": [], "body": "", "content_type": "application/json", "auth_type": "none",
                   "paging": "none"},
        "mapping": {"format": "json", "records_path": "", "employee": "", "timestamp": "", "timestamp_format": ""},
    },
    "essl_etimetracklite": {
        "label": "eSSL eTimeTrackLite (web API)", "vendor": "eSSL",
        "hint": "The WebAPIService of eTimeTrackLite. Enter the software's address, the device serial number and the "
                "web API user. Punches come back as tab-separated lines: user id, then time.",
        "config": {"method": "POST", "url": "http://your-server:80/iclock/WebAPIService.asmx", "params": [],
                   "headers": [{"name": "SOAPAction", "value": "http://tempuri.org/GetTransactionsLog"}],
                   "body": _ESSL_SOAP, "content_type": "text/xml; charset=utf-8", "auth_type": "body",
                   "paging": "none", "date_window_days": 7},
        "mapping": {"format": "xml", "text_field": "strDataList", "text_delimiter": "\\t", "text_header": False,
                    "employee": "1", "timestamp": "2", "timestamp_format": "", "direction": ""},
    },
    "matrix_cosec": {
        "label": "Matrix COSEC (web API)", "vendor": "Matrix",
        "hint": "COSEC CENTRA's event API. Use a COSEC API user. Field names can differ by version: check them in the test.",
        "config": {"method": "GET",
                   "url": "http://your-server/COSEC/api.svc/v2/event-ta?action=get;date-range={from:%d%m%Y}-{to:%d%m%Y};format=json",
                   "params": [], "headers": [], "body": "", "content_type": "application/json", "auth_type": "basic",
                   "paging": "none"},
        "mapping": {"format": "json", "records_path": "event-ta", "employee": "userid", "timestamp": "eventdatetime",
                    "timestamp_format": "%d/%m/%Y %H:%M:%S", "direction": "entryexittype", "in_values": "0",
                    "out_values": "1", "device": "mid", "id": "indexno"},
    },
    "realtime_etimeoffice": {
        "label": "Realtime eTimeOffice (web API)", "vendor": "Realtime",
        "hint": "Realtime's cloud attendance. User name is CorporateID:UserName and password is Password:true, as "
                "Realtime's API asks.",
        "config": {"method": "GET", "url": "https://api.etimeoffice.com/api/DownloadPunchData",
                   "params": [{"name": "Empcode", "value": "ALL"},
                              {"name": "FromDate", "value": "{from:%d/%m/%Y_%H:%M}"},
                              {"name": "ToDate", "value": "{to:%d/%m/%Y_%H:%M}"}],
                   "headers": [], "body": "", "content_type": "application/json", "auth_type": "basic",
                   "paging": "none"},
        "mapping": {"format": "json", "records_path": "PunchData", "employee": "Empcode", "timestamp": "PunchDate",
                    "timestamp_format": "%d/%m/%Y %H:%M:%S", "direction": "", "device": "mcid"},
    },
    "zkbiotime": {
        "label": "ZKTeco ZKBioTime (web API)", "vendor": "ZKTeco",
        "hint": "ZKBioTime 8 or later. Use a BioTime user; we sign in and read the transactions page by page.",
        "config": {"method": "GET", "url": "https://your-biotime:8081/iclock/api/transactions/",
                   "params": [{"name": "start_time", "value": "{from:%Y-%m-%d %H:%M:%S}"},
                              {"name": "end_time", "value": "{to:%Y-%m-%d %H:%M:%S}"},
                              {"name": "page_size", "value": "{size}"}],
                   "headers": [], "body": "", "content_type": "application/json", "auth_type": "login",
                   "login_url": "https://your-biotime:8081/jwt-api-token-auth/",
                   "login_body": '{"username": "{username}", "password": "{password}"}',
                   "token_from": "json:token", "token_header": "Authorization", "token_prefix": "JWT ",
                   "paging": "next", "next_path": "next", "page_size": 500},
        "mapping": {"format": "json", "records_path": "data", "employee": "emp_code", "timestamp": "punch_time",
                    "timestamp_format": "", "direction": "punch_state", "in_values": "0", "out_values": "1",
                    "device": "terminal_sn", "id": "id"},
    },
    "suprema_biostar2": {
        "label": "Suprema BioStar 2 (web API)", "vendor": "Suprema",
        "hint": "BioStar 2's event search. Use a BioStar operator account; times are read in UTC.",
        "config": {"method": "POST", "url": "https://your-biostar/api/events/search", "params": [], "headers": [],
                   "body": _BIOSTAR_SEARCH, "content_type": "application/json", "auth_type": "login",
                   "login_url": "https://your-biostar/api/login",
                   "login_body": '{"User": {"login_id": "{username}", "password": "{password}"}}',
                   "token_from": "header:bs-session-id", "token_header": "bs-session-id", "token_prefix": "",
                   "paging": "offset", "page_size": 500},
        "mapping": {"format": "json", "records_path": "EventCollection.rows", "employee": "user_id.user_id",
                    "timestamp": "datetime", "timestamp_format": "iso", "direction": "", "device": "device_id.id",
                    "id": "id"},
    },
}

# ── Vendor push in any format (the device calls our address) ───────

WEBHOOK_PRESETS = {
    "custom": {
        "label": "Custom JSON", "vendor": None,
        "hint": "Any system that can post JSON. Map its fields once; every post is read the same way.",
        "mapping": {"format": "json", "records_path": "", "employee": "", "timestamp": ""},
    },
    "hikvision_isapi": {
        "label": "Hikvision (ISAPI event push)", "vendor": "Hikvision",
        "hint": "On the device: Network, Advanced, HTTP Listening. Enter our address and port, and the path shown "
                "below. Only events with an employee number are kept.",
        "mapping": {"format": "json", "records_path": ".", "employee": "AccessControllerEvent.employeeNoString",
                    "timestamp": "dateTime", "timestamp_format": "iso", "direction": "AccessControllerEvent.attendanceStatus",
                    "in_values": "checkIn,breakIn,overtimeIn", "out_values": "checkOut,breakOut,overtimeOut",
                    "device": "AccessControllerEvent.serialNo"},
    },
}

# ── Direct database read ───────────────────────────────────────────

ENGINES = {
    "mssql": {"label": "SQL Server", "port": 1433, "driver": "pymssql", "package": "pymssql"},
    "mysql": {"label": "MySQL / MariaDB", "port": 3306, "driver": "pymysql", "package": "pymysql"},
    "postgresql": {"label": "PostgreSQL", "port": 5432, "driver": "psycopg2", "package": "psycopg2-binary"},
    "oracle": {"label": "Oracle", "port": 1521, "driver": "oracledb", "package": "oracledb"},
    "access": {"label": "MS Access (office connector)", "port": None, "driver": "pyodbc", "package": "pyodbc",
               "connector_only": True},
}

DB_PRESETS = {
    "custom": {
        "label": "Custom", "vendor": None, "hint": "Any table or view with a user id and a punch time.",
        "config": {"engine": "mssql", "port": 1433, "mode": "table", "table": "", "incremental": "time"},
        "mapping": {"employee": "", "timestamp": ""},
    },
    "matrix_cosec": {
        "label": "Matrix COSEC (SQL Server)", "vendor": "Matrix",
        "hint": "The COSEC database (often named COSEC). Punches are in Mx_ATDEventTrn; MID is the door controller. "
                "Very old COSEC servers (SQL Server 2000) need tds_version=7.0 in the extra options.",
        "config": {"engine": "mssql", "port": 1433, "database": "COSEC", "mode": "query", "incremental": "time",
                   "query": "SELECT et.UserID, et.Edatetime AS PunchTime, et.EntryExitType, et.MID AS MasterControllerID\n"
                            "FROM Mx_ATDEventTrn et\nWHERE et.Edatetime >= :since\nORDER BY et.Edatetime"},
        "mapping": {"employee": "UserID", "timestamp": "PunchTime", "direction": "EntryExitType", "in_values": "0",
                    "out_values": "1", "device": "MasterControllerID"},
    },
    "essl_etimetracklite": {
        "label": "eSSL eTimeTrackLite (SQL Server)", "vendor": "eSSL",
        "hint": "eTimeTrackLite keeps one table per month (DeviceLogs_11_2026). We read each month's table from the "
                "last punch onwards.",
        "config": {"engine": "mssql", "port": 1433, "database": "etimetracklite1", "mode": "table",
                   "table": "DeviceLogs_{M}_{YYYY}", "incremental": "time"},
        "mapping": {"employee": "UserId", "timestamp": "LogDate", "direction": "Direction", "in_values": "in",
                    "out_values": "out", "device": "DeviceId", "id": "DeviceLogId"},
    },
    "zkbiotime": {
        "label": "ZKTeco ZKBioTime (database)", "vendor": "ZKTeco",
        "hint": "ZKBioTime's own PostgreSQL (port 7496 by default; older versions use MySQL or SQL Server). "
                "Punches are in iclock_transaction.",
        "config": {"engine": "postgresql", "port": 7496, "database": "biotime", "mode": "table",
                   "table": "iclock_transaction", "incremental": "id"},
        "mapping": {"employee": "emp_code", "timestamp": "punch_time", "direction": "punch_state", "in_values": "0",
                    "out_values": "1", "device": "terminal_sn", "id": "id"},
    },
    "zk_access": {
        "label": "ZKTeco ZKTime / ZKAccess (att2000.mdb)", "vendor": "ZKTeco",
        "hint": "The Access file of ZKTime 5 / ZKAccess (att2000.mdb or access.mdb). Read on the office PC by the "
                "connector. Badgenumber is the user id shown on the device.",
        "config": {"engine": "access", "mode": "query", "incremental": "time", "file_path": "C:\\Program Files (x86)\\ZKTime5.0\\att2000.mdb",
                   "query": "SELECT u.Badgenumber, c.CHECKTIME, c.CHECKTYPE, c.SENSORID\n"
                            "FROM CHECKINOUT c INNER JOIN USERINFO u ON c.USERID = u.USERID\n"
                            "WHERE c.CHECKTIME >= :since\nORDER BY c.CHECKTIME", "runs_on": "connector"},
        "mapping": {"employee": "Badgenumber", "timestamp": "CHECKTIME", "direction": "CHECKTYPE", "in_values": "I",
                    "out_values": "O", "device": "SENSORID"},
    },
}


def public() -> dict:
    """Presets for the web (labels, hints and the values they fill)."""
    from modules.attendance.device_common import driver_available
    engines = {k: {**v, "installed": driver_available(v["driver"])} for k, v in ENGINES.items()}
    return {"api": API_PRESETS, "webhook": WEBHOOK_PRESETS, "database": DB_PRESETS, "engines": engines,
            "zk_installed": driver_available("zk")}
