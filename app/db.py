"""SQLite。スキーマは SPEC 第 11 章どおり。"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_write_lock = threading.Lock()


def connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.commit()


def ensure_db(path: str) -> sqlite3.Connection:
    conn = connect(path)
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='items'"
    ).fetchone()
    if exists is None:
        init_db(conn)
    else:
        try:
            conn.execute("ALTER TABLE items ADD COLUMN sebas_pushed_at TEXT")
            conn.commit()
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
    return conn


def write_lock() -> threading.Lock:
    return _write_lock
