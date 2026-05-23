import pathlib
import sqlite3
import tempfile
import unittest

from src import db


EXPECTED_INDEXES = {
    "snapshots_watch_url_idx",
    "snapshots_fetched_at_idx",
    "alerts_watch_url_fired_idx",
    "alerts_kind_url_fired_idx",
}

EXPECTED_TABLES = {"snapshots", "alerts", "parse_fail_counters"}


def _named_indexes(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name NOT LIKE 'sqlite_autoindex_%'"
    ).fetchall()
    return {row[0] for row in rows}


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {row[0] for row in rows}


class DbBootstrapTests(unittest.TestCase):
    def test_bootstrap_creates_all_tables(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            db.bootstrap(conn)
            self.assertTrue(EXPECTED_TABLES.issubset(_tables(conn)))
        finally:
            conn.close()

    def test_bootstrap_creates_four_named_indexes(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            db.bootstrap(conn)
            self.assertEqual(_named_indexes(conn), EXPECTED_INDEXES)
        finally:
            conn.close()

    def test_bootstrap_is_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            db.bootstrap(conn)
            db.bootstrap(conn)
            self.assertEqual(_named_indexes(conn), EXPECTED_INDEXES)
            self.assertTrue(EXPECTED_TABLES.issubset(_tables(conn)))
        finally:
            conn.close()

    def test_connect_sets_wal_pragma(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "state.db"
            conn = db.connect(path)
            try:
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                self.assertEqual(mode.lower(), "wal")
                busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
                self.assertEqual(busy, 5000)
                fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
                self.assertEqual(fk, 1)
            finally:
                conn.close()

    def test_connect_then_bootstrap_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "state.db"
            conn = db.connect(path)
            try:
                db.bootstrap(conn)
                self.assertEqual(_named_indexes(conn), EXPECTED_INDEXES)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
