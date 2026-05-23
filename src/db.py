"""SQLite connection helper and schema bootstrap.

See spec §7 for table definitions. All connections enable WAL so live
`sqlite3` CLI inspections during a tick don't block, and busy_timeout=5000
covers brief writer contention between the orchestrator and CLI.
"""

from __future__ import annotations

import pathlib
import sqlite3


_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY,
  watch_id TEXT NOT NULL,
  source_url TEXT NOT NULL,
  sku TEXT,
  fetched_at TEXT NOT NULL,
  in_stock INTEGER NOT NULL,
  price REAL,
  title TEXT,
  http_status INTEGER,
  error TEXT
);
CREATE INDEX IF NOT EXISTS snapshots_watch_url_idx
  ON snapshots(watch_id, source_url, fetched_at);
CREATE INDEX IF NOT EXISTS snapshots_fetched_at_idx
  ON snapshots(fetched_at);

CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY,
  watch_id TEXT,
  source_url TEXT NOT NULL,
  sku TEXT,
  kind TEXT NOT NULL DEFAULT 'stock',
  fired_at TEXT NOT NULL,
  price REAL,
  message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS alerts_watch_url_fired_idx
  ON alerts(watch_id, source_url, fired_at);
CREATE INDEX IF NOT EXISTS alerts_kind_url_fired_idx
  ON alerts(kind, source_url, fired_at);

CREATE TABLE IF NOT EXISTS parse_fail_counters (
  watch_id TEXT NOT NULL,
  source_url TEXT NOT NULL,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  last_updated TEXT NOT NULL,
  PRIMARY KEY (watch_id, source_url)
);
"""


def connect(path: pathlib.Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def bootstrap(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()
