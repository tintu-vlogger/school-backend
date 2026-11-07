"""Local storage in one SQLite file: punches waiting to be sent (so nothing is lost while
the internet is down) and how far each device has been read."""
from __future__ import annotations

import json
import sqlite3
import threading
import time


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_key TEXT NOT NULL,
                punch TEXT NOT NULL,
                created REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_try REAL NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS ix_queue_key ON queue (device_key, id);
            CREATE TABLE IF NOT EXISTS cursors (
                device_key TEXT PRIMARY KEY,
                last_time TEXT,
                last_id INTEGER,
                last_read_at TEXT,
                last_error TEXT
            );
        """)
        self.db.commit()

    # ── queue ──
    def add(self, device_key: str, punches: list[dict]) -> int:
        now = time.time()
        with self._lock:
            self.db.executemany("INSERT INTO queue (device_key, punch, created) VALUES (?, ?, ?)",
                                [(device_key, json.dumps(p, default=str), now) for p in punches])
            self.db.commit()
        return len(punches)

    def due(self, device_key: str, limit: int = 500) -> list[tuple[int, dict]]:
        with self._lock:
            rows = self.db.execute("SELECT id, punch FROM queue WHERE device_key = ? AND next_try <= ? ORDER BY id LIMIT ?",
                                   (device_key, time.time(), limit)).fetchall()
        return [(i, json.loads(p)) for i, p in rows]

    def done(self, ids: list[int]) -> None:
        if not ids:
            return
        with self._lock:
            self.db.executemany("DELETE FROM queue WHERE id = ?", [(i,) for i in ids])
            self.db.commit()

    def retry_later(self, ids: list[int]) -> None:
        """Back off: 1, 2, 4 ... minutes, at most an hour between tries."""
        with self._lock:
            for i in ids:
                row = self.db.execute("SELECT attempts FROM queue WHERE id = ?", (i,)).fetchone()
                attempts = (row[0] if row else 0) + 1
                wait = min(60 * (2 ** (attempts - 1)), 3600)
                self.db.execute("UPDATE queue SET attempts = ?, next_try = ? WHERE id = ?", (attempts, time.time() + wait, i))
            self.db.commit()

    def size(self, device_key: str | None = None) -> int:
        with self._lock:
            if device_key:
                return self.db.execute("SELECT COUNT(*) FROM queue WHERE device_key = ?", (device_key,)).fetchone()[0]
            return self.db.execute("SELECT COUNT(*) FROM queue").fetchone()[0]

    # ── cursors ──
    def cursor(self, device_key: str) -> dict:
        with self._lock:
            row = self.db.execute("SELECT last_time, last_id, last_read_at, last_error FROM cursors WHERE device_key = ?",
                                  (device_key,)).fetchone()
        if not row:
            return {}
        return {k: v for k, v in zip(("last_time", "last_id", "last_read_at", "last_error"), row) if v is not None}

    def save_cursor(self, device_key: str, **values) -> None:
        cur = self.cursor(device_key)
        cur.update({k: v for k, v in values.items()})
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO cursors (device_key, last_time, last_id, last_read_at, last_error) "
                            "VALUES (?, ?, ?, ?, ?)", (device_key, cur.get("last_time"), cur.get("last_id"),
                                                       cur.get("last_read_at"), cur.get("last_error")))
            self.db.commit()

    def close(self) -> None:
        self.db.close()
