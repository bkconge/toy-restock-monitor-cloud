import io
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
import unittest.mock

from src import main as main_mod


VALID_EMPTY_WATCHES_YAML = textwrap.dedent("""
    notifier: ntfy
    ntfy_topic_url: "https://ntfy.sh/brian-restocks-abc123"
    redsky_key: "abcdef0123456789abcdef0123456789abcdef01"
    poll_interval_seconds: 180
    cooldown_minutes: 60
    parse_fail_threshold: 3
    http_read_timeout_seconds: 30
    tick_wall_clock_cap_seconds: 120
    log_level: INFO
    per_source_overrides: {}
    watches: []
""").strip()

VALID_THREE_WATCHES_YAML = textwrap.dedent("""
    notifier: ntfy
    ntfy_topic_url: "https://ntfy.sh/brian-restocks-abc123"
    redsky_key: "abcdef0123456789abcdef0123456789abcdef01"
    poll_interval_seconds: 180
    cooldown_minutes: 60
    parse_fail_threshold: 3
    http_read_timeout_seconds: 30
    tick_wall_clock_cap_seconds: 120
    log_level: INFO
    per_source_overrides: {}
    watches:
      - id: w1
        name: "w1"
        msrp_cap: 6.99
        sources:
          - https://www.target.com/p/foo/-/A-1
""").strip()

EXPECTED_INDEXES = {
    "snapshots_watch_url_idx",
    "snapshots_fetched_at_idx",
    "alerts_watch_url_fired_idx",
    "alerts_kind_url_fired_idx",
}


def _named_indexes(db_path: pathlib.Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name NOT LIKE 'sqlite_autoindex_%'"
        ).fetchall()
        return {row[0] for row in rows}
    finally:
        conn.close()


class MainBootstrapTests(unittest.TestCase):
    def test_fresh_checkout_creates_db_with_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = pathlib.Path(tmp_str)
            cfg_path = tmp / "config.yaml"
            cfg_path.write_text(VALID_EMPTY_WATCHES_YAML, encoding="utf-8")
            db_path = tmp / "data" / "state.db"
            log_path = tmp / "logs" / "app.log"
            self.assertFalse(db_path.exists())
            rc = main_mod.main([
                "--once",
                "--config", str(cfg_path),
                "--db-path", str(db_path),
                "--log-file", str(log_path),
            ])
            self.assertEqual(rc, 0)
            self.assertTrue(db_path.exists())
            self.assertEqual(_named_indexes(db_path), EXPECTED_INDEXES)

    def test_missing_config_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = pathlib.Path(tmp_str)
            cfg_path = tmp / "config-does-not-exist.yaml"
            db_path = tmp / "data" / "state.db"
            log_path = tmp / "logs" / "app.log"
            stderr_buf = io.StringIO()
            with unittest.mock.patch("sys.stderr", stderr_buf):
                rc = main_mod.main([
                    "--once",
                    "--config", str(cfg_path),
                    "--db-path", str(db_path),
                    "--log-file", str(log_path),
                ])
            self.assertNotEqual(rc, 0)
            self.assertFalse(db_path.exists())
            self.assertIn(str(cfg_path), stderr_buf.getvalue())

    def test_malformed_config_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = pathlib.Path(tmp_str)
            cfg_path = tmp / "config.yaml"
            cfg_path.write_text(
                # missing cooldown_minutes — required key absent
                VALID_EMPTY_WATCHES_YAML.replace("cooldown_minutes: 60\n", ""),
                encoding="utf-8",
            )
            db_path = tmp / "data" / "state.db"
            log_path = tmp / "logs" / "app.log"
            stderr_buf = io.StringIO()
            with unittest.mock.patch("sys.stderr", stderr_buf):
                rc = main_mod.main([
                    "--once",
                    "--config", str(cfg_path),
                    "--db-path", str(db_path),
                    "--log-file", str(log_path),
                ])
            self.assertNotEqual(rc, 0)
            self.assertFalse(db_path.exists())
            self.assertIn("cooldown_minutes", stderr_buf.getvalue())

    def test_three_watches_phase1_short_circuits_zero(self) -> None:
        # Phase 1 stub: a non-empty watches list must still exit 0 because
        # orchestrator isn't implemented yet. Confirms the explicit decision
        # documented in main.py's TODO.
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = pathlib.Path(tmp_str)
            cfg_path = tmp / "config.yaml"
            cfg_path.write_text(VALID_THREE_WATCHES_YAML, encoding="utf-8")
            db_path = tmp / "data" / "state.db"
            log_path = tmp / "logs" / "app.log"
            rc = main_mod.main([
                "--once",
                "--config", str(cfg_path),
                "--db-path", str(db_path),
                "--log-file", str(log_path),
            ])
            self.assertEqual(rc, 0)
            self.assertTrue(db_path.exists())

    def test_missing_env_var_fails_fast(self) -> None:
        # Spec §6.1 + AC #5: env: prefixed config values must fail fast at
        # startup with the missing var name in stderr.
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = pathlib.Path(tmp_str)
            cfg_path = tmp / "config.yaml"
            cfg_path.write_text(
                VALID_EMPTY_WATCHES_YAML.replace(
                    'ntfy_topic_url: "https://ntfy.sh/brian-restocks-abc123"',
                    "ntfy_topic_url: env:MISSING_NTFY_XYZ_TEST",
                ),
                encoding="utf-8",
            )
            db_path = tmp / "data" / "state.db"
            log_path = tmp / "logs" / "app.log"

            env = {k: v for k, v in os.environ.items() if k != "MISSING_NTFY_XYZ_TEST"}
            project_root = pathlib.Path(__file__).resolve().parents[1]
            result = subprocess.run(
                [
                    sys.executable, "-m", "src.main",
                    "--once",
                    "--config", str(cfg_path),
                    "--db-path", str(db_path),
                    "--log-file", str(log_path),
                ],
                cwd=str(project_root),
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("MISSING_NTFY_XYZ_TEST", result.stderr)


if __name__ == "__main__":
    unittest.main()
