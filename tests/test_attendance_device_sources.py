"""Device connections without the API: field mapping (JSON, XML, text), the ADMS protocol
parsing, incremental database reads (SQLite), vendor API paging and sign-in (mock HTTP),
and the ZKTeco LAN pull (a fake device)."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from core.errors import BadRequest
from modules.attendance import device_adms, device_api, device_db, device_mapping as fm, device_zk
from modules.attendance.device_common import MASK, masked, store_secrets, with_secrets

IST = ZoneInfo("Asia/Kolkata")


# ── Mapping ─────────────────────────────────────────────────────────

def test_json_nested_records_and_fields():
    body = json.dumps({"EventCollection": {"rows": [
        {"datetime": "2026-11-02T03:58:00.00Z", "user_id": {"user_id": "101", "name": "A"}, "device_id": {"id": "5"}},
        {"datetime": "2026-11-02T12:31:10Z", "user_id": {"user_id": "102"}, "device_id": {"id": "5"}},
    ]}})
    m = fm.clean({"format": "json", "records_path": "EventCollection.rows", "employee": "user_id.user_id",
                  "timestamp": "datetime", "timestamp_format": "iso", "device": "device_id.id"})
    rows = fm.extract(body.encode(), m)
    assert len(rows) == 2 and rows[0]["user_id.user_id"] == "101"
    recs = fm.to_records(rows, m, IST)
    assert recs[0]["device_user_id"] == "101"
    assert recs[0]["timestamp"] == datetime(2026, 11, 2, 3, 58, tzinfo=timezone.utc)
    assert recs[1]["_terminal"] == "5"


def test_local_times_custom_format_timezone_and_direction_values():
    rows = [{"userid": "7", "eventdatetime": "02/11/2026 09:15:00", "entryexittype": "0"},
            {"userid": "7", "eventdatetime": "02/11/2026 18:40:05", "entryexittype": "1"},
            {"userid": "7", "eventdatetime": "02/11/2026 19:00:00", "entryexittype": "9"}]
    m = fm.clean({"employee": "UserID", "timestamp": "EventDateTime", "timestamp_format": "%d/%m/%Y %H:%M:%S",
                  "direction": "EntryExitType", "in_values": "0", "out_values": "1"})
    recs = fm.to_records(rows, m, IST)  # field names match without regard to case
    assert recs[0]["timestamp"] == datetime(2026, 11, 2, 3, 45, tzinfo=timezone.utc)
    assert [r["direction"] for r in recs] == ["in", "out", None]
    # a mapping timezone overrides the device's
    m2 = {**m, "timezone": "UTC"}
    assert fm.to_records(rows[:1], m2, IST)[0]["timestamp"] == datetime(2026, 11, 2, 9, 15, tzinfo=timezone.utc)


def test_epoch_and_separate_date_time():
    m = fm.clean({"employee": "pin", "timestamp": "ts", "timestamp_format": "epoch_ms"})
    recs = fm.to_records([{"pin": 5, "ts": 1793600000000}], m, IST)
    assert recs[0]["device_user_id"] == "5"
    assert recs[0]["timestamp"] == datetime.fromtimestamp(1793600000, tz=timezone.utc)
    m = fm.clean({"employee": "code", "date": "d", "time": "t"})
    recs = fm.to_records([{"code": "E1", "d": "2026-11-03", "t": "09:00:00"}], m, IST)
    assert recs[0]["timestamp"] == datetime(2026, 11, 3, 3, 30, tzinfo=timezone.utc)


def test_xml_records_and_xml_with_delimited_text():
    xml = """<?xml version="1.0"?><Response xmlns="urn:x"><Logs>
      <Log><EmpCode>11</EmpCode><Time>2026-11-02 09:00:00</Time><Dir>IN</Dir></Log>
      <Log><EmpCode>12</EmpCode><Time>2026-11-02 09:05:00</Time><Dir>OUT</Dir></Log></Logs></Response>"""
    m = fm.clean({"format": "xml", "records_path": "Log", "employee": "EmpCode", "timestamp": "Time", "direction": "Dir"})
    recs = fm.to_records(fm.extract(xml, m), m, IST)
    assert [(r["device_user_id"], r["direction"]) for r in recs] == [("11", "in"), ("12", "out")]
    soap = """<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>
      <GetTransactionsLogResponse xmlns="http://tempuri.org/"><GetTransactionsLogResult>true</GetTransactionsLogResult>
      <strDataList>101\t2026-11-02 09:01:00\r\n102\t2026-11-02 09:03:30\r\n</strDataList>
      </GetTransactionsLogResponse></soap:Body></soap:Envelope>"""
    m = fm.clean({"format": "xml", "text_field": "strDataList", "text_delimiter": "\\t", "employee": "1", "timestamp": "2"})
    recs = fm.to_records(fm.extract(soap, m), m, IST)
    assert [r["device_user_id"] for r in recs] == ["101", "102"]
    assert recs[1]["timestamp"] == datetime(2026, 11, 2, 3, 33, 30, tzinfo=timezone.utc)


def test_xml_with_entities_is_refused_and_bad_json_explained():
    with pytest.raises(BadRequest):
        fm.extract('<!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>', {"format": "xml"})
    with pytest.raises(BadRequest):
        fm.extract(b"not json", {"format": "json"})


def test_whole_body_is_one_record_and_guess():
    hik = {"dateTime": "2026-11-02T09:10:00+05:30", "eventType": "AccessControllerEvent",
           "AccessControllerEvent": {"employeeNoString": "88", "attendanceStatus": "checkIn", "serialNo": 41}}
    m = fm.clean({"records_path": ".", "employee": "AccessControllerEvent.employeeNoString", "timestamp": "dateTime",
                  "direction": "AccessControllerEvent.attendanceStatus", "in_values": "checkIn", "out_values": "checkOut"})
    recs = fm.to_records(fm.extract(json.dumps(hik), m), m, IST)
    assert recs[0]["device_user_id"] == "88" and recs[0]["direction"] == "in"
    assert recs[0]["timestamp"] == datetime(2026, 11, 2, 3, 40, tzinfo=timezone.utc)
    g = fm.guess(["DeviceLogId", "DownloadDate", "DeviceId", "UserId", "LogDate", "Direction"])
    assert g["employee"] == "UserId" and g["timestamp"] == "LogDate" and g["id"] == "DeviceLogId"
    assert g["direction"] == "Direction" and g["device"] == "DeviceId"


def test_mapping_needs_employee_and_time():
    with pytest.raises(BadRequest):
        fm.clean({"timestamp": "t"})
    with pytest.raises(BadRequest):
        fm.clean({"employee": "e", "date": "d"})


def test_secrets_are_encrypted_masked_and_kept():
    cfg = store_secrets({"host": "h"}, ("password",), {"password": "s3cret"}, None)
    assert "s3cret" not in json.dumps(cfg)
    assert with_secrets(cfg)["password"] == "s3cret"
    assert masked(cfg, ("password",))["password"] == MASK
    again = store_secrets({"host": "h2"}, ("password",), {"password": MASK}, cfg)  # unchanged form keeps it
    assert with_secrets(again)["password"] == "s3cret"


# ── ADMS protocol ───────────────────────────────────────────────────

def test_adms_attlog_operlog_info_and_commands():
    body = "1001\t2026-11-02 09:01:05\t0\t1\t0\t0\t0\r\n1002\t2026-11-02 18:30:00\t1\t15\t\t\t\n\n"
    recs = device_adms.parse_attlog(body)
    assert [(r["device_user_id"], r["timestamp"], r["direction"]) for r in recs] == [
        ("1001", "2026-11-02 09:01:05", None), ("1002", "2026-11-02 18:30:00", None)]
    assert [r["direction"] for r in device_adms.parse_attlog(body, use_status=True)] == ["in", "out"]
    op = device_adms.parse_operlog("USER PIN=1001\tName=Ravi\tPri=0\tPasswd=\tCard=[0000]\tGrp=1\n"
                                   "OPLOG 4\t0\t2026-11-02 09:00:00\t0\t0\t0\t0\nFP PIN=1001\tFID=6\tSize=1\tValid=1\tTMP=x\n")
    assert op["users"] == [{"pin": "1001", "name": "Ravi", "card": "[0000]", "privilege": "0"}] and op["other"] == 2
    info = device_adms.parse_info("Ver 6.60 Apr 2 2016,12,10,4512,192.168.1.201,10,7,0,0")
    assert info["firmware"].startswith("Ver 6.60") and info["users"] == "12" and info["ip"] == "192.168.1.201"
    text = device_adms.options_text("ABC123", {"adms": {"att_stamp": "999"}}, IST)
    assert "GET OPTION FROM: ABC123" in text and "ATTLOGStamp=999" in text and "TimeZone=330" in text
    state = device_adms.queue_command({}, "INFO")
    state = device_adms.queue_command(state, device_adms.reload_command(IST, 7))
    cmds, state = device_adms.pending_commands(state)
    assert [c["id"] for c in cmds] == [1, 2] and cmds[1]["cmd"].startswith("DATA QUERY ATTLOG StartTime=")
    assert device_adms.pending_commands(state)[0] == []  # sent once
    state = device_adms.command_results("ID=1&Return=0&CMD=INFO\nID=2&Return=-1&CMD=DATA", state)
    assert [c["status"] for c in state["adms"]["commands"]] == ["done", "failed"]


# ── Database reads (SQLite) ─────────────────────────────────────────

def _sqlite(tmp_path, rows, table="punch_log"):
    path = tmp_path / "att.db"
    con = sqlite3.connect(path)
    con.execute(f"CREATE TABLE IF NOT EXISTS {table} (id INTEGER PRIMARY KEY, emp TEXT, punch_time TIMESTAMP, io TEXT)")
    con.executemany(f"INSERT INTO {table} (id, emp, punch_time, io) VALUES (?, ?, ?, ?)", rows)
    con.commit()
    con.close()
    return str(path)


def _db_cfg(path, **kw):
    return with_secrets(device_db.clean({"engine": "sqlite", "file_path": path, "mode": "table", "table": "punch_log",
                                         "incremental": "time", "lookback_days": 3650, **kw}, None))


MAP = fm.clean({"employee": "emp", "timestamp": "punch_time", "direction": "io", "in_values": "I", "out_values": "O",
                "id": "id"})


def test_incremental_db_read_by_time(tmp_path):
    path = _sqlite(tmp_path, [(1, "E1", "2026-11-02 09:00:00", "I"), (2, "E1", "2026-11-02 18:00:00", "O")])
    cfg = _db_cfg(path)
    rows, recs, state, _ = device_db.read(cfg, MAP, {}, IST)
    assert len(recs) == 2 and recs[0]["direction"] == "in"
    assert recs[0]["timestamp"] == datetime(2026, 11, 2, 3, 30, tzinfo=timezone.utc)
    assert state["last_time"] == "2026-11-02T18:00:00"
    _sqlite(tmp_path, [(3, "E2", "2026-11-03 09:10:00", "I")])
    rows, recs, state2, _ = device_db.read(cfg, MAP, state, IST)
    # the last punch time is read again (>=) and dropped later as a duplicate; older rows aren't
    assert [r["device_user_id"] for r in recs] == ["E1", "E2"]
    assert state2["last_time"] == "2026-11-03T09:10:00"


def test_incremental_db_read_by_id_and_batches(tmp_path):
    path = _sqlite(tmp_path, [(i, f"E{i}", f"2026-11-02 09:{i:02d}:00", "I") for i in range(1, 8)])
    cfg = _db_cfg(path, incremental="id", batch=100)
    cfg["batch"] = 3
    _, recs, state, _ = device_db.read(cfg, MAP, {}, IST)
    assert [r["device_user_id"] for r in recs] == ["E1", "E2", "E3"] and state["last_id"] == 3 and state["more"]
    _, recs, state, _ = device_db.read(cfg, MAP, state, IST)
    assert [r["device_user_id"] for r in recs] == ["E4", "E5", "E6"]
    _, recs, state, _ = device_db.read(cfg, MAP, state, IST)
    assert [r["device_user_id"] for r in recs] == ["E7"] and not state["more"]


def test_custom_query_with_since_and_read_only_checks(tmp_path):
    path = _sqlite(tmp_path, [(1, "E1", "2026-11-02 09:00:00", "I"), (2, "E9", "2026-11-04 09:00:00", "I")])
    cfg = with_secrets(device_db.clean({"engine": "sqlite", "file_path": path, "mode": "query", "incremental": "time",
                                        "query": "SELECT emp, punch_time, io FROM punch_log WHERE punch_time >= :since ORDER BY punch_time"}, None))
    m = fm.clean({"employee": "emp", "timestamp": "punch_time"})
    _, recs, state, _ = device_db.read(cfg, m, {"last_time": "2026-11-03T00:00:00"}, IST)
    assert [r["device_user_id"] for r in recs] == ["E9"]
    for bad in ("DELETE FROM punch_log", "SELECT 1; DROP TABLE punch_log", "SELECT * INTO x FROM punch_log",
                "SELECT 1 EXEC xp_cmdshell 'dir'", "UPDATE punch_log SET emp = 'x'"):
        with pytest.raises(BadRequest):
            device_db.check_query(bad)
    assert device_db.check_query("WITH t AS (SELECT * FROM punch_log) SELECT REPLACE(emp, 'E', '') FROM t;")


def test_monthly_tables(tmp_path):
    now = datetime(2026, 11, 7, 6, 0, tzinfo=timezone.utc)
    names = device_db.tables_for("DeviceLogs_{M}_{YYYY}", datetime(2026, 9, 20), now)
    assert names == ["DeviceLogs_9_2026", "DeviceLogs_10_2026", "DeviceLogs_11_2026"]
    path = _sqlite(tmp_path, [(1, "A", "2026-10-30 09:00:00", "I")], table="DeviceLogs_10_2026")
    _sqlite(tmp_path, [(1, "B", "2026-11-02 09:00:00", "I")], table="DeviceLogs_11_2026")
    cfg = _db_cfg(path, table="DeviceLogs_{M}_{YYYY}")
    _, recs, state, notes = device_db.read(cfg, MAP, {"last_time": "2026-09-15T00:00:00"}, IST, now=now)
    assert [r["device_user_id"] for r in recs] == ["A", "B"]  # September's table doesn't exist: skipped
    assert any("not found" in n for n in notes)


def test_unknown_engine_and_missing_driver_message():
    with pytest.raises(BadRequest):
        device_db.clean({"engine": "nosuch", "host": "x", "table": "t"}, None)
    cfg = with_secrets(device_db.clean({"engine": "oracle", "host": "db.example.com", "table": "t"}, None))
    try:
        import oracledb  # noqa: F401
    except ImportError:
        with pytest.raises(BadRequest) as e:
            device_db.url_for(cfg)
        assert "oracledb" in e.value.detail
    with pytest.raises(BadRequest):
        device_db.url_for({**cfg, "engine": "access"})


# ── Vendor API (mock HTTP) ──────────────────────────────────────────

def test_api_login_paging_and_window():
    calls = []

    def handler(request: httpx.Request):
        calls.append(request)
        if request.url.path == "/jwt-api-token-auth/":
            assert json.loads(request.content) == {"username": "hr", "password": "pw\"1"}
            return httpx.Response(200, json={"token": "T0K"})
        assert request.headers["Authorization"] == "JWT T0K"
        page = int(request.url.params.get("page", "1"))
        if page == 1:
            data = [{"id": 1, "emp_code": "101", "punch_time": "2026-11-02 09:00:00", "punch_state": "0", "terminal_sn": "S1"},
                    {"id": 2, "emp_code": "102", "punch_time": "2026-11-02 09:02:00", "punch_state": "0", "terminal_sn": "S1"}]
            return httpx.Response(200, json={"data": data, "next": "https://bio.example.com/iclock/api/transactions/?page=2"})
        return httpx.Response(200, json={"data": [{"id": 3, "emp_code": "101", "punch_time": "2026-11-02 18:00:00",
                                                   "punch_state": "1", "terminal_sn": "S1"}], "next": None})

    cfg = with_secrets(device_api.clean({
        "url": "https://bio.example.com/iclock/api/transactions/", "method": "GET",
        "params": [{"name": "start_time", "value": "{from:%Y-%m-%d %H:%M:%S}"}, {"name": "end_time", "value": "{to}"}],
        "auth_type": "login", "login_url": "https://bio.example.com/jwt-api-token-auth/",
        "login_body": '{"username": "{username}", "password": "{password}"}', "token_from": "json:token",
        "token_header": "Authorization", "token_prefix": "JWT ", "username": "hr", "password": 'pw"1',
        "paging": "next", "next_path": "next", "lookback_days": 2}, None))
    m = fm.clean({"records_path": "data", "employee": "emp_code", "timestamp": "punch_time", "direction": "punch_state",
                  "in_values": "0", "out_values": "1", "device": "terminal_sn", "id": "id"})
    now = datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc)
    rows, recs, state, notes = device_api.read(cfg, m, {}, IST, None, httpx.MockTransport(handler), now)
    assert len(recs) == 3 and recs[2]["direction"] == "out" and state["last_id"] == 3
    assert state["last_time"] == "2026-11-02T18:00:00"
    first = [c for c in calls if c.url.path.endswith("/transactions/")][0]
    assert first.url.params["start_time"] == "2026-11-01 11:30:00"  # 2 days back, India time
    assert len(notes) == 2
    # the next run asks from the last punch, less the overlap
    start, end, capped = device_api.window(cfg, state, IST, now)
    assert start == datetime(2026, 11, 2, 17, 30) and not capped


def test_api_basic_auth_page_numbers_and_errors():
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        assert request.headers["Authorization"].startswith("Basic ")
        page = int(request.url.params["page"])
        items = [{"u": str(page * 10 + i), "t": "2026-11-02 09:00:00"} for i in range(2 if page < 3 else 1)]
        return httpx.Response(200, json={"items": items})

    cfg = with_secrets(device_api.clean({"url": "https://api.example.com/logs?from={from:%d%m%Y}", "auth_type": "basic",
                                         "username": "u", "password": "p", "paging": "page", "page_size": 2,
                                         "size_param": "limit"}, None))
    m = fm.clean({"records_path": "items", "employee": "u", "timestamp": "t"})
    _, recs, _, _ = device_api.read(cfg, m, {}, IST, None, httpx.MockTransport(handler))
    assert len(recs) == 5 and [s["page"] for s in seen] == ["1", "2", "3"] and seen[0]["limit"] == "2"
    deny = httpx.MockTransport(lambda r: httpx.Response(401, text="nope"))
    with pytest.raises(BadRequest) as e:
        device_api.read(cfg, m, {}, IST, None, deny)
    assert "sign-in" in e.value.detail


def test_api_oauth_and_soap_body_escaping():
    bodies = []

    def handler(request):
        if request.url.path == "/token":
            assert b"grant_type=client_credentials" in request.content
            return httpx.Response(200, json={"access_token": "abc"})
        assert request.headers["Authorization"] == "Bearer abc"
        bodies.append(request.content.decode())
        return httpx.Response(200, text="<r><strDataList>5\t2026-11-02 09:00:00\n</strDataList></r>")

    cfg = with_secrets(device_api.clean({"url": "https://x.example.com/svc", "method": "POST", "auth_type": "oauth2",
                                         "token_url": "https://x.example.com/token", "client_id": "c", "client_secret": "s",
                                         "content_type": "text/xml", "username": "a<b",
                                         "body": "<q><u>{username}</u><s>{serial}</s></q>"}, None))
    m = fm.clean({"format": "xml", "text_field": "strDataList", "text_delimiter": "\\t", "employee": "1", "timestamp": "2"})
    _, recs, _, _ = device_api.read(cfg, m, {}, IST, "SN9", httpx.MockTransport(handler))
    assert recs[0]["device_user_id"] == "5"
    assert "<u>a&lt;b</u><s>SN9</s>" in bodies[0]


def test_api_window_is_capped_and_moves_on():
    cfg = with_secrets(device_api.clean({"url": "https://a.example.com", "date_window_days": 5}, None))
    now = datetime(2026, 11, 30, tzinfo=timezone.utc)
    start, end, capped = device_api.window(cfg, {"last_time": "2026-11-01T00:00:00"}, IST, now)
    assert capped and (end - start).days == 5
    empty = httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
    _, _, state, _ = device_api.read(cfg, fm.clean({"employee": "a", "timestamp": "b"}),
                                     {"last_time": "2026-11-01T00:00:00"}, IST, None, empty, now)
    assert state["more"] and state["last_time"] > "2026-11-05"


def test_api_address_must_be_web():
    with pytest.raises(BadRequest):
        device_api.clean({"url": "ftp://x"}, None)


# ── ZKTeco LAN pull (fake device) ───────────────────────────────────

class _Att:
    def __init__(self, uid, ts, punch):
        self.user_id, self.timestamp, self.punch, self.status = uid, ts, punch, 1


class _FakeConn:
    def __init__(self, logs):
        self.logs = logs
        self.users, self.records = 3, len(logs)

    def disable_device(self): pass
    def enable_device(self): pass
    def disconnect(self): pass
    def get_serialnumber(self): return "ZK123"
    def get_firmware_version(self): return "Ver 6.60"
    def read_sizes(self): pass
    def get_attendance(self): return self.logs


def _factory(logs):
    class FakeZK:
        def __init__(self, ip, **kw):
            assert kw["port"] == 4370 and kw["password"] == 1234
        def connect(self):
            return _FakeConn(logs)
    return FakeZK


def test_zk_reads_only_new_punches():
    logs = [_Att("1", datetime(2026, 11, 2, 9, 0), 0), _Att("1", datetime(2026, 11, 2, 18, 0), 1)]
    cfg = with_secrets(device_zk.clean({"ip": "203.0.113.5", "password": "1234", "use_status": True, "lookback_days": 3650}, None))
    cfg["_skip_host_check"] = True
    recs, state, info = device_zk.read(cfg, {}, IST, _factory(logs))
    assert [r["direction"] for r in recs] == ["in", "out"] and info["serial"] == "ZK123"
    logs.append(_Att("2", datetime(2026, 11, 3, 9, 0), 0))
    recs, state, _ = device_zk.read(cfg, state, IST, _factory(logs))
    assert [r["device_user_id"] for r in recs] == ["1", "2"]  # the last one again (dropped later) and the new one
    with pytest.raises(BadRequest):
        device_zk.clean({"ip": "x", "password": "abc"}, None)


def test_window_default_lookback():
    cfg = with_secrets(device_api.clean({"url": "https://a.example.com", "lookback_days": 1}, None))
    now = datetime(2026, 11, 7, 6, 30, tzinfo=timezone.utc)
    start, end, _ = device_api.window(cfg, {}, IST, now)
    assert end == datetime(2026, 11, 7, 12, 0) and start == end - timedelta(days=1)
