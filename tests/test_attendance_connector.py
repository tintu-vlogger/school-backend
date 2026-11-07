"""The office connector program: reading a database incrementally (SQLite), the offline
queue with retries, a ZKTeco device (fake), and a full cycle against the real API."""
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "connector"))

from hrms_connector import readers  # noqa: E402
from hrms_connector.cli import Connector  # noqa: E402
from hrms_connector.server import ServerError  # noqa: E402
from hrms_connector.store import Store  # noqa: E402

MAP = {"employee": "emp", "timestamp": "punch_time", "direction": "io", "in_values": "I", "out_values": "O", "id": "id",
       "device": "door"}


def _db(tmp_path, rows):
    path = tmp_path / "att.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY, emp TEXT, punch_time TIMESTAMP, io TEXT, door TEXT)")
    con.executemany("INSERT INTO logs VALUES (?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()
    return str(path)


def test_reader_incremental_by_time_and_id(tmp_path):
    path = _db(tmp_path, [(1, "E1", "2026-11-02 09:00:00", "I", "D1"), (2, "E1", "2026-11-02 18:00:00", "O", "D1")])
    s = {"engine": "sqlite", "file_path": path, "mode": "table", "table": "logs", "incremental": "time", "lookback_days": 3650}
    rows, punches, cur = readers.read_database(s, MAP, {}, datetime(2026, 11, 5))
    assert punches[0] == {"device_user_id": "E1", "timestamp": "2026-11-02T09:00:00", "direction": "in", "terminal": "D1"}
    assert cur["last_time"] == "2026-11-02T18:00:00"
    _db(tmp_path, [(3, "E2", "2026-11-03 09:00:00", "I", "D1")])
    _, punches, cur = readers.read_database(s, MAP, cur, datetime(2026, 11, 5))
    assert [p["device_user_id"] for p in punches] == ["E1", "E2"]
    s_id = {**s, "incremental": "id", "batch": 2}
    _, punches, cur = readers.read_database(s_id, MAP, {}, datetime(2026, 11, 5))
    assert [p["device_user_id"] for p in punches] == ["E1", "E1"] and cur["last_id"] == 2 and cur["more"]
    _, punches, cur = readers.read_database(s_id, MAP, cur, datetime(2026, 11, 5))
    assert [p["device_user_id"] for p in punches] == ["E2"] and not cur["more"]
    with pytest.raises(readers.ReadError):
        readers.read_database({**s, "mode": "query", "query": "DELETE FROM logs"}, MAP, {}, datetime(2026, 11, 5))


class FakeServer:
    def __init__(self):
        self.pushed, self.beats, self.down = [], [], False

    def config(self, key):
        return {"connection": "database", "settings": self.settings, "mapping": MAP, "timezone": "Asia/Kolkata",
                "every_minutes": 15, "state": {"last_time": "2026-11-01T00:00:00"}, "config_version": "v1",
                "device": {"name": "COSEC"}}

    def push(self, key, punches):
        if self.down:
            raise ServerError("Can't reach the server")
        self.pushed += punches
        return {"inserted": len(punches)}

    def heartbeat(self, key, data):
        self.beats.append(data)
        return {"ok": True, "config_version": "v1"}


def test_offline_queue_retries_and_heartbeat(tmp_path):
    path = _db(tmp_path, [(1, "E1", "2026-11-02 09:00:00", "I", None)])
    fake = FakeServer()
    fake.settings = {"engine": "sqlite", "file_path": path, "mode": "table", "table": "logs", "incremental": "time"}
    store = Store(str(tmp_path / "data.db"))
    c = Connector({"server": "https://x", "devices": [{"name": "A", "key": "dev_k"}]}, store, fake)
    fake.down = True
    c.cycle(force_read=True)
    assert store.size() == 1 and fake.pushed == []  # kept while offline
    assert store.cursor("dev_k")["last_time"] == "2026-11-02T09:00:00"  # started from the server's position
    fake.down = False
    c.send(c.jobs[0])
    assert fake.pushed == []  # waiting for its retry time
    store.db.execute("UPDATE queue SET next_try = 0")
    store.db.commit()
    c.send(c.jobs[0])
    assert [p["device_user_id"] for p in fake.pushed] == ["E1"] and store.size() == 0
    c.heartbeat(c.jobs[0], force=True)
    assert fake.beats[-1]["queue"] == 0 and fake.beats[-1]["cursor"]["last_time"] == "2026-11-02T09:00:00"


def test_zk_reader_with_fake_device():
    class Att:
        def __init__(self, uid, ts, punch):
            self.user_id, self.timestamp, self.punch = uid, ts, punch

    class Conn:
        def disable_device(self): pass
        def enable_device(self): pass
        def disconnect(self): pass
        def get_serialnumber(self): return "SN1"
        def get_attendance(self):
            return [Att("5", datetime(2026, 11, 2, 9, 0), 0), Att("5", datetime(2026, 11, 2, 18, 0), 1)]

    class ZK:
        def __init__(self, ip, **kw): pass
        def connect(self): return Conn()

    punches, cur, info = readers.read_zk({"ip": "10.0.0.5", "use_status": True}, {"last_time": "2026-11-02T10:00:00"},
                                         datetime(2026, 11, 3), ZK)
    assert punches == [{"device_user_id": "5", "timestamp": "2026-11-02T18:00:00", "direction": "out"}]
    assert cur["last_time"] == "2026-11-02T18:00:00" and info["serial"] == "SN1"


def test_full_cycle_against_the_api(client, company_a, tmp_path):
    """The connector downloads its settings with the device key, reads the database and
    pushes; the punches land in the company and the device shows the connector online."""
    d = date.today() - timedelta(days=2)
    path = _db(tmp_path, [(1, "31", f"{d} 09:00:00", "I", None), (2, "31", f"{d} 18:00:00", "O", None)])
    cfg = {"engine": "sqlite", "file_path": path, "mode": "table", "table": "logs", "incremental": "id",
           "mapping": {"employee": "emp", "timestamp": "punch_time", "direction": "io", "in_values": "I",
                       "out_values": "O", "id": "id"}}
    r = company_a.post("/attendance/devices", json={"name": "Office DB", "connection": "database", "runs_on": "connector",
                                                    "source_config": cfg})
    assert r.status_code == 201, r.text
    dev, key = r.json(), r.json()["api_key"]
    emp = company_a.add_employee("Cycle", "Kumar", date_of_joining="2025-01-01")
    company_a.post("/attendance/enrolments", json={"device_user_id": "31", "employee_id": emp["id"]})

    class TestServer:
        """The connector's Server, but through the test client."""
        def _call(self, method, path, key, body=None):
            resp = client.request(method, "/api/v1" + path, json=body, headers={"X-Device-Key": key})
            if resp.status_code >= 400:
                raise ServerError(resp.text, resp.status_code)
            return resp.json()

        def config(self, key): return self._call("GET", "/attendance/connector/config", key)
        def push(self, key, punches): return self._call("POST", "/attendance/devices/push", key, {"punches": punches})
        def heartbeat(self, key, data): return self._call("POST", "/attendance/connector/heartbeat", key, data)

    store = Store(str(tmp_path / "data.db"))
    c = Connector({"server": "http://test", "devices": [{"key": key}]}, store, TestServer())
    c.cycle(force_read=True)
    assert store.size() == 0
    row = company_a.get(f"/attendance/devices/{dev['uuid']}").json()
    assert row["connector"]["online"] and row["state"]["last_id"] == 2
    logs = company_a.get("/attendance/sync-logs", params={"device_uuid": dev["uuid"]}).json()["items"]
    assert logs[0]["inserted"] == 2 and logs[0]["kind"] == "connector"
