import json
import os
import sqlite3
import threading
import time
from config import EVENT_DB_PATH

RETENTION_DAYS = 90

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


# Opens the event database, creates the table if needed, and prunes old events
def init_db() -> None:
    global _conn
    os.makedirs(os.path.dirname(os.path.abspath(EVENT_DB_PATH)), exist_ok=True)
    _conn = sqlite3.connect(EVENT_DB_PATH, check_same_thread=False)
    with _lock:
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                type TEXT NOT NULL,
                actor TEXT,
                ip TEXT,
                detail TEXT
            )"""
        )
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts)")
        _conn.execute("DELETE FROM events WHERE ts < ?", (time.time() - RETENTION_DAYS * 86400,))
        _conn.commit()
    print(f"Event log: {EVENT_DB_PATH}")


# Records an event. Never raises — logging must not break spa control.
def log_event(type: str, actor: str | None = None, ip: str | None = None, detail: dict | str | None = None) -> None:
    if isinstance(detail, dict):
        detail = json.dumps(detail)
    print(f"EVENT {type} actor={actor} ip={ip} {detail or ''}")
    if _conn is None:
        return
    try:
        with _lock:
            _conn.execute(
                "INSERT INTO events (ts, type, actor, ip, detail) VALUES (?, ?, ?, ?, ?)",
                (time.time(), type, actor, ip, detail),
            )
            _conn.commit()
    except sqlite3.Error as e:
        print(f"Event log write failed: {e}")


# Returns the most recent events, newest first, optionally filtered by type
def recent(limit: int = 200, type: str | None = None) -> list[dict]:
    if _conn is None:
        return []
    sql = "SELECT id, ts, type, actor, ip, detail FROM events"
    args: list = []
    if type:
        sql += " WHERE type = ?"
        args.append(type)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(limit, 1000)))
    with _lock:
        rows = _conn.execute(sql, args).fetchall()
    return [dict(zip(("id", "ts", "type", "actor", "ip", "detail"), r)) for r in rows]


def event_types() -> list[str]:
    if _conn is None:
        return []
    with _lock:
        return [r[0] for r in _conn.execute("SELECT DISTINCT type FROM events ORDER BY type")]
