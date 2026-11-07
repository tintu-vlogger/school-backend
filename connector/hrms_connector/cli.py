"""The connector program.

    connector run              keep running: read devices on their schedule, send punches
    connector once             read and send once, then stop
    connector test             read each device and show what was found (nothing is sent)
    connector status           punches waiting to be sent and how far each device was read
    connector add <file>       add the devices from another settings file downloaded from the HR system

Settings live in connector.json next to the program (download it from the Devices page);
waiting punches and positions in connector-data.db; a log in connector.log.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import platform
import signal
import socket
import sys
import time
from datetime import datetime, timezone

from . import VERSION
from .readers import ReadError, read_database, read_zk
from .server import Server, ServerError
from .store import Store

log = logging.getLogger("connector")
TICK_SECONDS = 30
CONFIG_REFRESH_SECONDS = 15 * 60
HEARTBEAT_SECONDS = 60
PUSH_BATCH = 500


def home_dir() -> str:
    if getattr(sys, "frozen", False):  # the Windows .exe: next to the program
        return os.path.dirname(sys.executable)
    return os.getcwd()


def load_settings(path: str) -> dict:
    if not os.path.exists(path):
        raise SystemExit(f"Settings file not found: {path}\nDownload it from the HR system: Devices, open the device, "
                         f"'Connector settings file', and save it next to this program as connector.json.")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not data.get("server") or not data.get("devices"):
        raise SystemExit("connector.json needs 'server' and at least one device key.")
    return data


def _now_in(tz_name: str | None) -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz_name)).replace(tzinfo=None) if tz_name else datetime.now()
    except Exception:
        return datetime.now()


class DeviceJob:
    """One device: its settings from the server, when it was last read, its last error."""

    def __init__(self, name: str, key: str):
        self.name, self.key = name, key
        self.config: dict | None = None
        self.config_at = 0.0
        self.next_read = 0.0
        self.last_error: str | None = None
        self.last_beat = 0.0
        self.version = None
        self.blocked = False  # the key was refused: wait for a new settings file


class Connector:
    def __init__(self, settings: dict, store: Store, server: Server | None = None):
        self.settings = settings
        self.store = store
        self.server = server or Server(settings["server"], verify_tls=settings.get("verify_tls", True) is not False)
        self.jobs = [DeviceJob(d.get("name") or f"Device {i + 1}", d["key"]) for i, d in enumerate(settings["devices"])]
        self.stopping = False

    # ── one device ──
    def refresh_config(self, job: DeviceJob, force: bool = False) -> None:
        if not force and job.config and time.time() - job.config_at < CONFIG_REFRESH_SECONDS:
            return
        try:
            job.config = self.server.config(job.key)
            job.config_at = time.time()
            job.name = (job.config.get("device") or {}).get("name") or job.name
            job.blocked = False
            # first run on this PC: start where the server says reading got to
            if not self.store.cursor(job.key):
                st = job.config.get("state") or {}
                self.store.save_cursor(job.key, last_time=st.get("last_time"), last_id=st.get("last_id"))
        except ServerError as exc:
            if exc.status == 401:
                job.blocked = True
                job.last_error = "The device key was refused (a new settings file was made, or the device is off)."
            elif exc.status == 400:
                job.last_error = str(exc)
            else:
                job.last_error = str(exc)
            log.warning("%s: settings not loaded: %s", job.name, exc)

    def read(self, job: DeviceJob, now: datetime | None = None, zk_factory=None, db=None) -> int:
        """Read new punches into the waiting list. Returns how many were added."""
        cfg = job.config or {}
        s, m = cfg.get("settings") or {}, cfg.get("mapping") or {}
        cursor = self.store.cursor(job.key)
        now = now or _now_in(cfg.get("timezone"))
        try:
            if cfg.get("connection") == "database":
                _, punches, new = read_database(s, m, cursor, now, db=db)
            elif cfg.get("connection") == "zk":
                punches, new, _ = read_zk(s, cursor, now, zk_factory)
                new["more"] = False
            else:
                raise ReadError(f"This connector can't read '{cfg.get('connection')}' devices.")
        except ReadError as exc:
            job.last_error = str(exc)
            self.store.save_cursor(job.key, last_error=job.last_error)
            log.warning("%s: %s", job.name, exc)
            job.next_read = time.time() + 60 * int(cfg.get("every_minutes") or 15)
            return 0
        added = self.store.add(job.key, punches) if punches else 0
        more = new.pop("more", False)
        self.store.save_cursor(job.key, last_time=new.get("last_time"), last_id=new.get("last_id"),
                               last_read_at=datetime.now(timezone.utc).isoformat(), last_error=None)
        job.last_error = None
        job.next_read = time.time() + (5 if more else 60 * int(cfg.get("every_minutes") or 15))
        log.info("%s: read %s punches", job.name, added)
        return added

    def send(self, job: DeviceJob) -> int:
        """Send waiting punches in batches; keep them when the server can't be reached."""
        sent = 0
        while not self.stopping:
            batch = self.store.due(job.key, PUSH_BATCH)
            if not batch:
                break
            ids = [i for i, _ in batch]
            try:
                self.server.push(job.key, [p for _, p in batch])
            except ServerError as exc:
                if exc.status in (400, 422):  # the batch itself is wrong: try one by one, drop the bad ones
                    for i, p in batch:
                        try:
                            self.server.push(job.key, [p])
                            self.store.done([i])
                            sent += 1
                        except ServerError as one:
                            if one.status in (400, 422):
                                log.warning("%s: dropped a punch the server can't read: %s (%s)", job.name, p, one)
                                self.store.done([i])
                            else:
                                self.store.retry_later([i])
                                break
                    continue
                if exc.status == 401:
                    job.blocked = True
                job.last_error = str(exc)
                self.store.retry_later(ids)
                log.warning("%s: sending failed, will try again: %s", job.name, exc)
                break
            self.store.done(ids)
            sent += len(ids)
        return sent

    def heartbeat(self, job: DeviceJob, force: bool = False) -> None:
        if not force and time.time() - job.last_beat < HEARTBEAT_SECONDS:
            return
        cur = self.store.cursor(job.key)
        try:
            out = self.server.heartbeat(job.key, {
                "version": VERSION, "host": socket.gethostname(), "platform": platform.platform(terse=True),
                "queue": self.store.size(job.key), "last_error": job.last_error or cur.get("last_error"),
                "last_read_at": cur.get("last_read_at"),
                "cursor": {"last_time": cur.get("last_time"), "last_id": cur.get("last_id")}})
            job.last_beat = time.time()
            if job.config and out.get("config_version") and out["config_version"] != job.config.get("config_version"):
                job.config_at = 0  # settings changed on the server: load them again
        except ServerError as exc:
            log.info("%s: status not sent: %s", job.name, exc)

    def cycle(self, force_read: bool = False) -> None:
        for job in self.jobs:
            if self.stopping:
                return
            self.refresh_config(job)
            if job.blocked or not job.config:
                continue
            if force_read or time.time() >= job.next_read:
                self.read(job)
            self.send(job)
            self.heartbeat(job, force=force_read)

    def run_forever(self) -> None:
        log.info("Connector %s started with %s device(s)", VERSION, len(self.jobs))
        while not self.stopping:
            try:
                self.cycle()
            except Exception as exc:  # never stop the service
                log.exception("Unexpected error: %s", exc)
            for _ in range(TICK_SECONDS):
                if self.stopping:
                    break
                time.sleep(1)
        log.info("Connector stopped")


def _setup_logging(folder: str, verbose: bool) -> None:
    handler = logging.handlers.RotatingFileHandler(os.path.join(folder, "connector.log"), maxBytes=2_000_000,
                                                   backupCount=3, encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(handler)
    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(fmt)
    root.addHandler(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="connector", description="Sends attendance punches from the office to the HR system.")
    p.add_argument("command", nargs="?", default="run", choices=["run", "once", "test", "status", "add"])
    p.add_argument("file", nargs="?", help="settings file to add (with 'add')")
    p.add_argument("--folder", default=None, help="where connector.json and the data file are (default: next to the program)")
    p.add_argument("--verbose", action="store_true")
    a = p.parse_args(argv)
    folder = a.folder or home_dir()
    settings_path = os.path.join(folder, "connector.json")

    if a.command == "add":
        if not a.file:
            p.error("give the settings file to add")
        current = load_settings(settings_path) if os.path.exists(settings_path) else {"devices": []}
        with open(a.file, encoding="utf-8") as f:
            extra = json.load(f)
        current["server"] = current.get("server") or extra["server"]
        keys = {d["key"] for d in current["devices"]}
        current["devices"] += [d for d in extra.get("devices", []) if d["key"] not in keys]
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2)
        print(f"{len(current['devices'])} device(s) in {settings_path}")
        return 0

    _setup_logging(folder, a.verbose)
    settings = load_settings(settings_path)
    store = Store(os.path.join(folder, "connector-data.db"))
    c = Connector(settings, store)

    if a.command == "status":
        for job in c.jobs:
            cur = store.cursor(job.key)
            print(f"{job.name}: {store.size(job.key)} waiting; read up to {cur.get('last_time') or cur.get('last_id') or '-'}"
                  f"{'; last problem: ' + cur['last_error'] if cur.get('last_error') else ''}")
        return 0

    if a.command == "test":
        from .readers import read_database as rd, read_zk as rz
        ok = True
        for job in c.jobs:
            c.refresh_config(job, force=True)
            if not job.config:
                print(f"{job.name}: {job.last_error}")
                ok = False
                continue
            cfg = job.config
            try:
                if cfg["connection"] == "database":
                    rows, punches, _ = rd(cfg["settings"], cfg.get("mapping") or {}, {}, _now_in(cfg.get("timezone")), limit=10)
                    print(f"{job.name}: connected. {len(rows)} sample rows:")
                    for r in rows[:10]:
                        print("   ", {k: str(v) for k, v in r.items()})
                    print("  read as:", punches[:5])
                else:
                    punches, _, info = rz(cfg["settings"], {}, _now_in(cfg.get("timezone")))
                    print(f"{job.name}: connected {info}. {len(punches)} punches, last ones: {punches[-5:]}")
            except ReadError as exc:
                ok = False
                print(f"{job.name}: {exc}")
        return 0 if ok else 1

    if a.command == "once":
        c.cycle(force_read=True)
        print(f"Done. {store.size()} punches still waiting to be sent.")
        return 0

    def stop(*_):
        c.stopping = True
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    c.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
