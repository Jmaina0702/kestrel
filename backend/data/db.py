"""
backend/data/db.py

Owns the SQLite schema and connection management for Kestrel. Every
table and column name here is the Section 6 data contract -- Instance B
and Instance C both read/write against this schema directly, so nothing
here gets renamed without flagging it as a breaking change in
PROGRESS.md first.

Why WAL mode: default SQLite journaling locks the whole file on any
write, which causes "database is locked" failures the moment one process
reads while another writes. That's exactly Kestrel's situation -- the
backend writes continuously (signals, trades, balance snapshots) while
the frontend mostly reads. PRAGMA journal_mode=WAL allows concurrent
readers alongside a single writer without lock contention. It's set once
at init and again on every connection this module hands out, since WAL
is a per-connection pragma in some SQLite builds.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

try:
    from .. import config
except ImportError:  # pragma: no cover - fallback for running this file
    # directly as a script (`python backend/data/db.py`) rather than as
    # part of the package (`python -m backend.data.db`). Relative imports
    # only resolve when Python knows this file is part of a package, which
    # it doesn't when invoked as a bare script -- this makes the "run it
    # directly to verify" instruction actually work for Joy.
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from backend import config


def utc_now_iso() -> str:
    """
    Returns the current UTC time as an ISO 8601 string, e.g.
    '2026-08-13T14:03:22Z'. This is the one and only timestamp format
    used anywhere in the database (Section 5's coding standard) -- every
    Instance A module imports this instead of formatting timestamps its
    own way, so there's no risk of two modules drifting into slightly
    different formats that break sorting or comparison later.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS zones (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL CHECK(kind IN ('support','resistance')),
    price           REAL NOT NULL,
    upper           REAL NOT NULL,
    lower           REAL NOT NULL,
    formed_at       TEXT NOT NULL,
    mitigated       INTEGER NOT NULL DEFAULT 0,
    mitigated_at    TEXT,
    touched         INTEGER NOT NULL DEFAULT 0,
    first_touch_at  TEXT
);

CREATE TABLE IF NOT EXISTS signals_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc   TEXT NOT NULL,
    bias            TEXT NOT NULL CHECK(bias IN ('bullish','bearish','range')),
    zone_id         INTEGER REFERENCES zones(id),
    k_value         REAL,
    d_value         REAL,
    fired           INTEGER NOT NULL DEFAULT 0,
    direction       TEXT CHECK(direction IN ('BUY','SELL')),
    score           INTEGER,
    classification  TEXT CHECK(classification IN ('normal','perfect')),
    reason          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trade_groups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id         INTEGER NOT NULL REFERENCES zones(id),
    direction       TEXT NOT NULL CHECK(direction IN ('BUY','SELL')),
    classification  TEXT NOT NULL CHECK(classification IN ('normal','perfect')),
    score           INTEGER NOT NULL,
    opened_at       TEXT NOT NULL,
    closed_at       TEXT,
    status          TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
    entry_price     REAL NOT NULL,
    sl_price        REAL NOT NULL,
    tp_price        REAL NOT NULL,
    total_pnl       REAL
);

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_group_id  INTEGER NOT NULL REFERENCES trade_groups(id),
    mt5_ticket      INTEGER NOT NULL UNIQUE,
    lot_size        REAL NOT NULL,
    entry_price     REAL NOT NULL,
    sl_price        REAL NOT NULL,
    tp_price        REAL NOT NULL,
    opened_at       TEXT NOT NULL,
    closed_at       TEXT,
    close_price     REAL,
    close_reason    TEXT CHECK(close_reason IN ('TP','SL','manual','kill_switch')),
    pnl             REAL,
    status          TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed'))
);

CREATE TABLE IF NOT EXISTS balance_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc   TEXT NOT NULL,
    balance         REAL NOT NULL,
    equity          REAL NOT NULL,
    open_pnl        REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS control (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc   TEXT NOT NULL,
    level           TEXT NOT NULL CHECK(level IN ('INFO','WARNING','ERROR')),
    message         TEXT NOT NULL
);
"""


def get_connection() -> sqlite3.Connection:
    """
    Returns a new SQLite connection to config.DB_PATH with WAL mode
    active and row_factory set to sqlite3.Row, so callers read columns
    by name (row['balance']) instead of brittle positional indexing.

    A fresh connection is returned on every call rather than a shared
    module-level singleton: sqlite3.Connection objects are not safe to
    share across threads without check_same_thread tuning, and Kestrel's
    backend runs multiple threads (engine loop, balance recorder). Each
    caller opening its own short-lived connection is the simpler, safer
    default for this workload's scale -- this is not a high-throughput
    system where connection-pooling overhead would matter.
    """
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """
    Creates every table from the Section 6 schema if it doesn't already
    exist, and seeds the required `control` rows. Safe to call on every
    backend startup: CREATE TABLE IF NOT EXISTS plus an existence-checked
    INSERT make this fully idempotent, so restarting the backend never
    wipes or duplicates existing data.
    """
    conn = get_connection()
    try:
        conn.executescript(_SCHEMA)

        now = utc_now_iso()
        existing = {
            row["key"] for row in conn.execute("SELECT key FROM control").fetchall()
        }

        seed_rows = [
            ("bot_state", "stopped"),
            ("kill_switch_requested_at", ""),
        ]
        for key, value in seed_rows:
            if key not in existing:
                conn.execute(
                    "INSERT INTO control (key, value, updated_at) VALUES (?, ?, ?)",
                    (key, value, now),
                )

        conn.commit()
    finally:
        conn.close()


def log_event(level: str, message: str) -> None:
    """
    Writes one row to system_events. This is the single write path every
    other module uses instead of print() for anything meant to persist
    (Section 5's coding standard): connects, disconnects, order attempts,
    kill-switch triggers, offset recalculation, engine errors.

    level must be 'INFO', 'WARNING', or 'ERROR' -- the table's CHECK
    constraint enforces this, so an invalid level raises
    sqlite3.IntegrityError immediately rather than silently storing junk
    that would only surface as confusion later when someone reads the log.
    """
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO system_events (timestamp_utc, level, message) VALUES (?, ?, ?)",
            (utc_now_iso(), level, message),
        )
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    # Manual verification path for Joy: run `python backend/data/db.py`
    # (or `python -m backend.data.db` from the repo root). It should
    # create kestrel.db with no errors. Opening the file in any free
    # SQLite browser should then show all seven tables, with 'control'
    # already holding the two seed rows.
    init_db()
    print(f"kestrel.db initialized at: {config.DB_PATH}")
