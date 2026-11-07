"""Device connections through the API: ADMS cloud push (iclock), a database device read on
the server (SQLite), a vendor API pull (mock HTTP), vendor push (Hikvision), the office
connector (settings, configuration, heartbeat, push) and tenant isolation."""
import json
import sqlite3
from datetime import date, timedelta

import httpx

from core.db import tenant_session
from modules.attendance import device_api
from modules.attendance.models import Punch


def hire(company, first):
    return company.add_employee(first, "Kumar", date_of_joining="2025-01-01")


def enrol(company, emp, uid, device_uuid=None):
    r = company.post("/attendance/enrolments", json={"device_user_id": uid, "employee_id": emp["id"],
                                                     "device_uuid": device_uuid})
    assert r.status_code == 201, r.text


def punches(company, emp_id):
    db = tenant_session(company.id)
    try:
        return [(p.punched_at, p.direction, p.source, p.device_id) for p in
                db.query(Punch).filter(Punch.employee_id == emp_id).order_by(Punch.punched_at).all()]
    finally:
        db.close()


def recent_day(n=3) -> date:
    return date.today() - timedelta(days=n)


def add(company, **body):
    r = company.post("/attendance/devices", json={"kind": "biometric", **body})
    assert r.status_code == 201, r.text
    return r.json()


# ── ADMS ────────────────────────────────────────────────────────────

def test_adms_handshake_punches_commands_and_status(client, company_a, company_b):
    sn = f"CQZ{company_a.id}A7"
    dev = add(company_a, name="Gate ADMS", vendor="ZKTeco", connection="adms", serial_number=sn)
    assert dev["api_key"] is None  # recognised by serial number, no key
    emp = hire(company_a, "Adms")
    enrol(company_a, emp, "1001")
    # the same serial can't be registered by another company
    r = company_b.post("/attendance/devices", json={"name": "Copy", "connection": "adms", "serial_number": sn})
    assert r.status_code == 400 and "serial" in r.json()["detail"]
    assert company_a.post("/attendance/devices", json={"name": "No SN", "connection": "adms"}).status_code == 400

    assert client.get("/iclock/cdata", params={"SN": "UNKNOWN1", "options": "all"}).status_code == 404
    r = client.get("/iclock/cdata", params={"SN": sn, "options": "all", "pushver": "2.4.1"})
    assert r.status_code == 200 and f"GET OPTION FROM: {sn}" in r.text and "ATTLOGStamp=0" in r.text

    d = recent_day()
    body = f"1001\t{d} 09:02:00\t0\t1\t0\t0\n1001\t{d} 18:10:00\t1\t1\t0\t0\n9999\t{d} 10:00:00\t0\t1\n"
    r = client.post("/iclock/cdata", params={"SN": sn, "table": "ATTLOG", "Stamp": "4321"}, content=body)
    assert r.status_code == 200 and r.text == "OK: 3"
    got = punches(company_a, emp["id"])
    assert len(got) == 2 and got[0][2] == "device" and got[0][1] is None  # device states ignored by default
    # re-sent lines are duplicates, the handshake now carries the stamp
    client.post("/iclock/cdata.aspx", params={"SN": sn, "table": "ATTLOG"}, content=body)
    assert len(punches(company_a, emp["id"])) == 2
    assert "ATTLOGStamp=4321" in client.get("/iclock/cdata", params={"SN": sn}).text

    r = client.post("/iclock/cdata", params={"SN": sn, "table": "OPERLOG"},
                    content="USER PIN=1001\tName=Adms Kumar\tPri=0\tCard=\nOPLOG 4\t0\t2026-11-02 09:00:00\t0\t0\t0\t0\n")
    assert r.text == "OK: 2"
    assert company_a.get(f"/attendance/devices/{dev['uuid']}/device-users").json()["items"][0]["pin"] == "1001"

    assert client.get("/iclock/getrequest", params={"SN": sn, "INFO": "Ver 6.60,1,1,3,10.0.0.9"}).text == "OK"
    r = company_a.post(f"/attendance/devices/{dev['uuid']}/command", json={"action": "reload", "days": 7})
    assert r.status_code == 200, r.text
    r = client.get("/iclock/getrequest", params={"SN": sn})
    assert r.text.startswith("C:1:DATA QUERY ATTLOG StartTime=")
    assert client.get("/iclock/getrequest", params={"SN": sn}).text == "OK"
    assert client.post("/iclock/devicecmd", params={"SN": sn}, content="ID=1&Return=0&CMD=DATA").text == "OK"

    row = next(x for x in company_a.get("/attendance/devices").json()["items"] if x["uuid"] == dev["uuid"])
    assert row["online"] is True and row["state"]["adms"]["info"]["firmware"] == "Ver 6.60"
    assert row["state"]["commands"][0]["status"] == "done" and row["state"]["device_users"] == 1
    # tenant isolation: B sees nothing of A's device and its punches stay in A
    assert company_b.get(f"/attendance/devices/{dev['uuid']}").status_code == 404
    assert all(x["uuid"] != dev["uuid"] for x in company_b.get("/attendance/devices").json()["items"])
    assert company_b.post(f"/attendance/devices/{dev['uuid']}/command", json={"action": "info"}).status_code == 404

    # switched off: the device is refused
    company_a.put(f"/attendance/devices/{dev['uuid']}", json={"active": False})
    assert client.get("/iclock/cdata", params={"SN": sn}).status_code == 404


def test_adms_trusts_device_state_when_asked(client, company_a):
    sn = f"STATE{company_a.id}"
    add(company_a, name="Stateful", connection="adms", serial_number=sn, source_config={"use_status": True})
    emp = hire(company_a, "State")
    enrol(company_a, emp, "77")
    d = recent_day(4)
    client.post("/iclock/cdata", params={"SN": sn, "table": "ATTLOG"}, content=f"77\t{d} 09:00:00\t0\t1\n77\t{d} 17:00:00\t1\t1\n")
    assert [p[1] for p in punches(company_a, emp["id"])] == ["in", "out"]


# ── Database read on the server (SQLite stands in for SQL Server) ──

def test_database_device_test_pull_and_incremental(client, company_a, company_b, tmp_path):
    d = recent_day(2)
    path = str(tmp_path / "cosec.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE Mx_ATDEventTrn (IndexNo INTEGER PRIMARY KEY, UserID TEXT, Edatetime TIMESTAMP, "
                "EntryExitType INTEGER, MID INTEGER)")
    con.executemany("INSERT INTO Mx_ATDEventTrn VALUES (?, ?, ?, ?, ?)",
                    [(1, "501", f"{d} 09:01:00", 0, 3), (2, "501", f"{d} 18:05:00", 1, 3), (3, "777", f"{d} 09:30:00", 0, 3)])
    con.commit()
    con.close()
    cfg = {"preset": "matrix_cosec", "engine": "sqlite", "file_path": path, "mode": "query", "incremental": "time",
           "lookback_days": 10, "password": "dbpass",
           "query": "SELECT et.UserID, et.Edatetime AS PunchTime, et.EntryExitType, et.MID AS MasterControllerID "
                    "FROM Mx_ATDEventTrn et WHERE et.Edatetime >= :since ORDER BY et.Edatetime",
           "mapping": {"employee": "UserID", "timestamp": "PunchTime", "direction": "EntryExitType", "in_values": "0",
                       "out_values": "1", "device": "MasterControllerID"}}
    r = company_a.post("/attendance/devices/test", json={"connection": "database", "config": cfg})
    body = r.json()
    assert r.status_code == 200 and body["ok"], body
    assert body["sample"]["columns"][:2] == ["UserID", "PunchTime"] and len(body["sample"]["punches"]) == 3
    assert body["sample"]["punches"][0]["direction"] == "in"

    door = add(company_a, name="Door 3", connection="push", serial_number="3")  # MID 3 is this door
    dev = add(company_a, name="COSEC", vendor="Matrix", connection="database", source_config=cfg)
    assert dev["source_config"]["password"] == "••••••" and "dbpass" not in json.dumps(dev)
    emp = hire(company_a, "Cosec")
    enrol(company_a, emp, "501")
    r = company_a.post(f"/attendance/devices/{dev['uuid']}/pull")
    assert r.status_code == 200, r.text
    logs = r.json()["logs"]
    assert logs[0]["inserted"] == 2 and logs[0]["unmatched"] == 1
    got = punches(company_a, emp["id"])
    assert [p[1] for p in got] == ["in", "out"] and got[0][2] == "database" and got[0][3] == door["id"]

    con = sqlite3.connect(path)
    con.execute("INSERT INTO Mx_ATDEventTrn VALUES (4, '501', ?, 0, 3)", (f"{recent_day(1)} 09:00:00",))
    con.commit()
    con.close()
    r = company_a.post(f"/attendance/devices/{dev['uuid']}/pull")
    log = r.json()["logs"][0]
    assert log["inserted"] == 1 and log["duplicates"] == 1  # only the last punch time is read again
    # saved secrets fill an empty password on edit; B can't test or pull A's device
    r = company_a.put(f"/attendance/devices/{dev['uuid']}", json={"source_config": {**cfg, "password": ""}})
    assert r.status_code == 200
    r = company_b.post("/attendance/devices/test", json={"connection": "database", "config": cfg, "device_uuid": dev["uuid"]})
    assert r.status_code == 404
    assert company_b.post(f"/attendance/devices/{dev['uuid']}/pull").status_code == 404


def test_database_bad_query_and_missing_table(company_a, tmp_path):
    path = str(tmp_path / "x.db")
    sqlite3.connect(path).execute("CREATE TABLE logs (id INTEGER, emp TEXT)").connection.commit()
    bad = {"engine": "sqlite", "file_path": path, "mode": "query", "query": "DELETE FROM logs",
           "mapping": {"employee": "emp", "timestamp": "t"}}
    r = company_a.post("/attendance/devices", json={"name": "Bad", "connection": "database", "source_config": bad})
    assert r.status_code == 400 and "SELECT" in r.json()["detail"]
    r = company_a.post("/attendance/devices/test", json={"connection": "database", "config": {
        "engine": "sqlite", "file_path": path, "mode": "table", "table": "nosuch"}})
    body = r.json()
    assert body["ok"] is False and "logs" in body["tables"]


# ── Vendor API pull ────────────────────────────────────────────────

def test_api_device_pull(client, company_a, monkeypatch):
    d = recent_day(2)

    def handler(request):
        assert request.headers.get("X-API-Key") == "k-123"
        return httpx.Response(200, json={"PunchData": [
            {"Empcode": "0042", "PunchDate": f"{d:%d/%m/%Y} 09:15:00", "mcid": "1"},
            {"Empcode": "0042", "PunchDate": f"{d:%d/%m/%Y} 18:15:00", "mcid": "1"}]})

    real = device_api._client
    monkeypatch.setattr(device_api, "_client", lambda cfg, transport=None: real(cfg, httpx.MockTransport(handler)))
    emp = company_a.add_employee("Api", "Kumar", date_of_joining="2025-01-01", employee_code="0042")
    cfg = {"preset": "realtime_etimeoffice", "url": "https://api.example.com/api/DownloadPunchData",
           "params": [{"name": "FromDate", "value": "{from:%d/%m/%Y_%H:%M}"}], "auth_type": "api_key",
           "auth_header": "X-API-Key", "api_key": "k-123", "lookback_days": 5,
           "mapping": {"records_path": "PunchData", "employee": "Empcode", "timestamp": "PunchDate",
                       "timestamp_format": "%d/%m/%Y %H:%M:%S", "match_by": "employee_code"}}
    r = company_a.post("/attendance/devices/test", json={"connection": "api", "config": cfg})
    assert r.json()["ok"] and len(r.json()["sample"]["punches"]) == 2, r.text
    dev = add(company_a, name="eTimeOffice", connection="api", source_config=cfg)
    assert dev["source_config"]["api_key"] == "••••••"
    r = company_a.post(f"/attendance/devices/{dev['uuid']}/pull")
    assert r.json()["logs"][0]["inserted"] == 2, r.text
    assert [p[2] for p in punches(company_a, emp["id"])] == ["api", "api"]
    logs = company_a.get("/attendance/sync-logs", params={"device_uuid": dev["uuid"]}).json()["items"]
    assert logs[0]["kind"] == "api"


# ── Vendor push (Hikvision) ────────────────────────────────────────

def test_hikvision_event_push(client, company_a, company_b):
    mapping = {"records_path": ".", "employee": "AccessControllerEvent.employeeNoString", "timestamp": "dateTime",
               "timestamp_format": "iso"}
    dev = add(company_a, name="Hik face", vendor="Hikvision", connection="webhook",
              source_config={"preset": "hikvision_isapi", "mapping": mapping})
    key = dev["api_key"]
    emp = hire(company_a, "Hik")
    enrol(company_a, emp, "88")
    d = recent_day(2)
    event = {"dateTime": f"{d}T09:10:00+05:30", "eventType": "AccessControllerEvent",
             "AccessControllerEvent": {"employeeNoString": "88", "majorEventType": 5, "subEventType": 75}}
    r = client.post(f"/api/v1/attendance/devices/hook/{key}", data={"event_log": json.dumps(event)},
                    files={"Picture": ("face.jpg", b"\xff\xd8", "image/jpeg")})
    assert r.status_code == 200 and r.json()["inserted"] == 1, r.text
    beat = {"dateTime": f"{d}T09:11:00+05:30", "eventType": "heartBeat"}
    assert client.post(f"/api/v1/attendance/devices/hook/{key}", json=beat).json()["inserted"] == 0
    assert client.post("/api/v1/attendance/devices/hook/dev_wrongwrongwrongwrongwrong", json=event).status_code == 401
    # the last post is kept as a sample for the mapping screen
    r = company_a.post("/attendance/devices/test", json={"connection": "webhook", "device_uuid": dev["uuid"],
                                                         "config": {"mapping": mapping}})
    assert r.json()["ok"] and r.json()["sample"]["rows"][0]["eventType"] == "heartBeat"
    assert len(punches(company_a, emp["id"])) == 1


# ── Office connector ───────────────────────────────────────────────

def test_connector_config_heartbeat_push_and_isolation(client, company_a, company_b):
    cfg = {"preset": "zk_access", "engine": "access", "file_path": "C:\\ZKTime5.0\\att2000.mdb", "mode": "query",
           "incremental": "time", "password": "mdbpw",
           "query": "SELECT u.Badgenumber, c.CHECKTIME, c.CHECKTYPE FROM CHECKINOUT c INNER JOIN USERINFO u "
                    "ON c.USERID = u.USERID WHERE c.CHECKTIME >= :since ORDER BY c.CHECKTIME",
           "mapping": {"employee": "Badgenumber", "timestamp": "CHECKTIME", "direction": "CHECKTYPE",
                       "in_values": "I", "out_values": "O"}}
    dev = add(company_a, name="ZKTime PC", vendor="ZKTeco", connection="database", source_config=cfg)
    assert dev["runs_on"] == "connector" and dev["api_key"]  # Access files are read on the office PC
    key = dev["api_key"]
    r = company_a.post(f"/attendance/devices/{dev['uuid']}/pull")
    assert r.status_code == 400  # the connector reads it, not us

    h = {"X-Device-Key": key}
    conf = client.get("/api/v1/attendance/connector/config", headers=h).json()
    assert conf["connection"] == "database" and conf["settings"]["password"] == "mdbpw"
    assert conf["mapping"]["employee"] == "Badgenumber" and conf["timezone"]
    assert client.get("/api/v1/attendance/connector/config").status_code == 401

    emp = hire(company_a, "Conn")
    enrol(company_a, emp, "12")
    d = recent_day(2)
    r = client.post("/api/v1/attendance/devices/push", headers=h, json={"punches": [
        {"device_user_id": "12", "timestamp": f"{d}T09:00:00", "direction": "in"},
        {"device_user_id": "12", "timestamp": f"{d}T18:00:00", "direction": "out"}]})
    assert r.json()["inserted"] == 2
    r = client.post("/api/v1/attendance/connector/heartbeat", headers=h, json={
        "version": "1.0.0", "host": "FRONTDESK-PC", "queue": 0, "cursor": {"last_time": f"{d}T18:00:00"},
        "last_read_at": "2026-11-07T10:00:00Z"})
    assert r.status_code == 200 and r.json()["ok"]
    row = company_a.get(f"/attendance/devices/{dev['uuid']}").json()
    assert row["connector"]["online"] and row["connector"]["host"] == "FRONTDESK-PC"
    assert row["state"]["last_time"] == f"{d}T18:00:00"
    sync = company_a.get("/attendance/sync-logs", params={"device_uuid": dev["uuid"]}).json()["items"]
    assert sync[0]["kind"] == "connector"

    # a settings file makes a new key; the old one stops working
    f = company_a.post(f"/attendance/devices/{dev['uuid']}/connector-file").json()
    new_key = f["content"]["devices"][0]["key"]
    assert new_key != key and f["content"]["server"].endswith("/api/v1")
    assert client.get("/api/v1/attendance/connector/config", headers=h).status_code == 401
    assert client.get("/api/v1/attendance/connector/config", headers={"X-Device-Key": new_key}).status_code == 200
    # company B can't make a file for A's device, and A's key never touches B's data
    assert company_b.post(f"/attendance/devices/{dev['uuid']}/connector-file").status_code == 404
    emp_b = hire(company_b, "Other")
    enrol(company_b, emp_b, "12")
    client.post("/api/v1/attendance/devices/push", headers={"X-Device-Key": new_key},
                json={"punches": [{"device_user_id": "12", "timestamp": f"{recent_day(1)}T09:00:00"}]})
    assert punches(company_b, emp_b["id"]) == []


def test_presets_and_permissions(client, company_a):
    from tests.conftest import login_as
    p = company_a.get("/attendance/device-presets").json()
    assert {"essl_etimetracklite", "matrix_cosec", "realtime_etimeoffice", "custom"} <= set(p["api"])
    assert {"matrix_cosec", "essl_etimetracklite", "zkbiotime", "zk_access"} <= set(p["database"])
    assert "hikvision_isapi" in p["webhook"] and "mssql" in p["engines"]
    emp = company_a.add_employee("Plain", "User")
    me = login_as(company_a, emp)
    assert me.get("/attendance/device-presets").status_code == 403
    assert me.post("/attendance/devices/test", json={"connection": "database", "config": {}}).status_code == 403
