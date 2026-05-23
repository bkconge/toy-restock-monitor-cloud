"""Orchestrator state-machine tests (plan.md task #8).

All tests use a FakeSource (no real HTTP) and a FakeNotifier (collects
calls). The orchestrator's clock is patched via the module-level `_now`
shim so cooldown / retention windows are deterministic.
"""

from __future__ import annotations

import fcntl
import json
import pathlib
import socket
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from src import db
from src.config import Config, Watch
from src.notify.base import NotifyResult
from src.sources.base import StockSnapshot
from src import orchestrator as orchestrator_module
from src.orchestrator import Orchestrator


# ---------- fakes ----------

class FakeNotifier:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def send(self, text: str) -> NotifyResult:
        self.calls.append(text)
        return NotifyResult(channel="fake", success=True, error=None)


class FakeSource:
    """Source double — emits a scripted sequence of snapshots and records
    every fetch URL. Optionally raises a configured exception once."""

    def __init__(self, snapshots: list[StockSnapshot]) -> None:
        self._snapshots = list(snapshots)
        self.fetches: list[str] = []
        self.raise_exc: BaseException | None = None

    def fetch(self, url, *, http_client, user_agent) -> StockSnapshot:
        self.fetches.append(url)
        if self.raise_exc is not None:
            exc = self.raise_exc
            self.raise_exc = None
            raise exc
        if not self._snapshots:
            return _ok(in_stock=False)
        return self._snapshots.pop(0)


class StubHttpClient:
    """No-op HttpClient stand-in that satisfies the orchestrator's
    `select_ua()` + `next_allowed_at()` surface. Returns a far-past
    timestamp so tests with a patched clock always pass the backoff gate."""

    _ALWAYS_ALLOWED = datetime(1970, 1, 1, tzinfo=timezone.utc)

    def __init__(self) -> None:
        self.visitor_id = "fake"

    def select_ua(self) -> str:
        return "fake-ua"

    def next_allowed_at(self, host: str, ua: str) -> datetime:
        return self._ALWAYS_ALLOWED


# ---------- helpers ----------

def _ok(*, in_stock: bool, price: float | None = None, sku: str | None = None) -> StockSnapshot:
    return StockSnapshot(
        in_stock=in_stock,
        price=price,
        title="t",
        sku=sku,
        http_status=200,
        error=None,
        parse_error=False,
    )


def _parse_err() -> StockSnapshot:
    return StockSnapshot(
        in_stock=False, price=None, title=None, sku=None,
        http_status=200, error="parse failed", parse_error=True,
    )


def _net_err(http_status: int | None = None) -> StockSnapshot:
    return StockSnapshot(
        in_stock=False, price=None, title=None, sku=None,
        http_status=http_status,
        error=f"http {http_status}" if http_status else "error",
        parse_error=False,
    )


def _make_config(
    *,
    watches: tuple[Watch, ...],
    cooldown_minutes: int = 60,
    poll_interval_seconds: int = 180,
    per_source_overrides: dict | None = None,
    parse_fail_threshold: int = 3,
) -> Config:
    return Config(
        notifier="ntfy",
        ntfy_topic_url="https://ntfy.sh/x",
        redsky_key="abcdef0123456789abcdef0123456789abcdef01",
        poll_interval_seconds=poll_interval_seconds,
        cooldown_minutes=cooldown_minutes,
        parse_fail_threshold=parse_fail_threshold,
        http_read_timeout_seconds=30,
        tick_wall_clock_cap_seconds=120,
        log_level="INFO",
        watches=watches,
        per_source_overrides=per_source_overrides or {},
    )


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    db.bootstrap(conn)
    return conn


class FrozenClock:
    """Patchable replacement for orchestrator._now."""
    def __init__(self, start: datetime) -> None:
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t = self.t + timedelta(seconds=seconds)


# ---------- tests ----------

class CooldownTests(unittest.TestCase):
    def test_cooldown_suppresses_second_alert_within_window(self):
        watch = Watch(id="w1", name="W", msrp_cap=6.99, sources=("https://www.target.com/p/x/-/A-1",))
        cfg = _make_config(watches=(watch,))
        conn = _make_conn()
        notifier = FakeNotifier()
        source = FakeSource([_ok(in_stock=True, price=5.99, sku="1"), _ok(in_stock=True, price=5.99, sku="1")])

        clock = FrozenClock(datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc))
        with tempfile.TemporaryDirectory() as td:
            tdp = pathlib.Path(td)
            orch = Orchestrator(
                cfg, conn, notifier, {"www.target.com": source},
                http_client=StubHttpClient(),
                data_dir=tdp / "data",
                logs_dir=tdp / "logs",
            )
            with mock.patch.object(orchestrator_module, "_now", clock):
                orch.run_once(dry_run=False)
                clock.advance(30 * 60)  # 30 minutes — inside the 60-minute cooldown
                orch.run_once(dry_run=False)

        alerts = conn.execute("SELECT COUNT(*) FROM alerts WHERE kind='stock'").fetchone()[0]
        self.assertEqual(alerts, 1)
        self.assertEqual(len(notifier.calls), 1)


class DryRunCooldownTests(unittest.TestCase):
    def test_dry_run_writes_jsonl_and_alert_row_without_notifier(self):
        watch = Watch(id="w1", name="W", msrp_cap=6.99, sources=("https://www.target.com/p/x/-/A-1",))
        cfg = _make_config(watches=(watch,))
        conn = _make_conn()
        notifier = FakeNotifier()
        source = FakeSource([_ok(in_stock=True, price=5.99, sku="1"), _ok(in_stock=True, price=5.99, sku="1")])

        clock = FrozenClock(datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc))
        with tempfile.TemporaryDirectory() as td:
            tdp = pathlib.Path(td)
            logs_dir = tdp / "logs"
            orch = Orchestrator(
                cfg, conn, notifier, {"www.target.com": source},
                http_client=StubHttpClient(),
                data_dir=tdp / "data",
                logs_dir=logs_dir,
            )
            with mock.patch.object(orchestrator_module, "_now", clock):
                orch.run_once(dry_run=True)
                clock.advance(30 * 60)
                orch.run_once(dry_run=True)

            jsonl_path = logs_dir / "dry-run.jsonl"
            self.assertTrue(jsonl_path.exists())
            lines = jsonl_path.read_text().strip().splitlines()
            self.assertEqual(len(lines), 1)
            entry = json.loads(lines[0])
            self.assertEqual(entry["watch_id"], "w1")
            self.assertEqual(entry["sku"], "1")
            self.assertEqual(entry["price"], 5.99)

        alerts = conn.execute("SELECT COUNT(*) FROM alerts WHERE kind='stock'").fetchone()[0]
        self.assertEqual(alerts, 1)
        self.assertEqual(notifier.calls, [])


class OosTests(unittest.TestCase):
    def test_oos_records_snapshot_and_no_alert(self):
        watch = Watch(id="w1", name="W", msrp_cap=6.99, sources=("https://www.target.com/p/x/-/A-1",))
        cfg = _make_config(watches=(watch,))
        conn = _make_conn()
        notifier = FakeNotifier()
        source = FakeSource([_ok(in_stock=False, sku="1")])
        with tempfile.TemporaryDirectory() as td:
            tdp = pathlib.Path(td)
            orch = Orchestrator(
                cfg, conn, notifier, {"www.target.com": source},
                http_client=StubHttpClient(),
                data_dir=tdp / "data", logs_dir=tdp / "logs",
            )
            orch.run_once(dry_run=False)

        rows = conn.execute("SELECT in_stock FROM snapshots").fetchall()
        self.assertEqual(rows, [(0,)])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 0)
        self.assertEqual(notifier.calls, [])


class PerSourceOverrideTests(unittest.TestCase):
    def test_override_url_fetched_twice_while_default_url_only_once(self):
        url_fast = "https://www.target.com/p/fast/-/A-1"
        url_slow = "https://www.target.com/p/slow/-/A-2"
        watch = Watch(id="w1", name="W", msrp_cap=6.99, sources=(url_fast, url_slow))
        cfg = _make_config(
            watches=(watch,),
            poll_interval_seconds=180,
            per_source_overrides={url_fast: {"poll_interval_seconds": 60}},
        )
        conn = _make_conn()
        notifier = FakeNotifier()
        source = FakeSource([
            _ok(in_stock=False, sku="1"),  # tick 1 fast
            _ok(in_stock=False, sku="2"),  # tick 1 slow
            _ok(in_stock=False, sku="1"),  # tick 2 fast (slow skipped)
        ])
        clock = FrozenClock(datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc))
        with tempfile.TemporaryDirectory() as td:
            tdp = pathlib.Path(td)
            orch = Orchestrator(
                cfg, conn, notifier, {"www.target.com": source},
                http_client=StubHttpClient(),
                data_dir=tdp / "data", logs_dir=tdp / "logs",
            )
            with mock.patch.object(orchestrator_module, "_now", clock):
                orch.run_once(dry_run=False)
                clock.advance(75)
                orch.run_once(dry_run=False)

        rows_fast = conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE source_url=?", (url_fast,)
        ).fetchone()[0]
        rows_slow = conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE source_url=?", (url_slow,)
        ).fetchone()[0]
        self.assertEqual(rows_fast, 2)
        self.assertEqual(rows_slow, 1)


class ParserBrokenDedupTests(unittest.TestCase):
    def test_four_consecutive_parse_errors_yield_one_alert(self):
        url = "https://www.target.com/p/x/-/A-1"
        watch = Watch(id="w1", name="W", msrp_cap=6.99, sources=(url,))
        cfg = _make_config(watches=(watch,), parse_fail_threshold=3)
        conn = _make_conn()
        notifier = FakeNotifier()
        source = FakeSource([_parse_err() for _ in range(4)])

        clock = FrozenClock(datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc))
        with tempfile.TemporaryDirectory() as td:
            tdp = pathlib.Path(td)
            orch = Orchestrator(
                cfg, conn, notifier, {"www.target.com": source},
                http_client=StubHttpClient(),
                data_dir=tdp / "data", logs_dir=tdp / "logs",
            )
            with mock.patch.object(orchestrator_module, "_now", clock):
                for _ in range(4):
                    orch.run_once(dry_run=False)
                    # Advance past the global poll interval so each tick fetches.
                    clock.advance(cfg.poll_interval_seconds + 5)

        alerts = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE kind='parser_broken'"
        ).fetchone()[0]
        self.assertEqual(alerts, 1)


class PidfileOverlapTests(unittest.TestCase):
    def test_second_tick_exits_immediately_when_lock_held(self):
        url = "https://www.target.com/p/x/-/A-1"
        watch = Watch(id="w1", name="W", msrp_cap=6.99, sources=(url,))
        cfg = _make_config(watches=(watch,))
        conn = _make_conn()
        notifier = FakeNotifier()
        source = FakeSource([_ok(in_stock=False, sku="1")])

        with tempfile.TemporaryDirectory() as td:
            tdp = pathlib.Path(td)
            data_dir = tdp / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            pid_path = data_dir / "orchestrator.pid"

            holder = open(pid_path, "a+")
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
            try:
                orch = Orchestrator(
                    cfg, conn, notifier, {"www.target.com": source},
                    http_client=StubHttpClient(),
                    data_dir=data_dir, logs_dir=tdp / "logs",
                )
                orch.run_once(dry_run=False)
            finally:
                fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
                holder.close()

        self.assertEqual(conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 0)
        self.assertEqual(source.fetches, [])


class HttpTimeoutTests(unittest.TestCase):
    def test_socket_timeout_records_snapshot_with_error_no_crash(self):
        url = "https://www.target.com/p/x/-/A-1"
        watch = Watch(id="w1", name="W", msrp_cap=6.99, sources=(url,))
        cfg = _make_config(watches=(watch,))
        conn = _make_conn()
        notifier = FakeNotifier()
        source = FakeSource([])
        source.raise_exc = socket.timeout("read timed out")

        with tempfile.TemporaryDirectory() as td:
            tdp = pathlib.Path(td)
            orch = Orchestrator(
                cfg, conn, notifier, {"www.target.com": source},
                http_client=StubHttpClient(),
                data_dir=tdp / "data", logs_dir=tdp / "logs",
            )
            orch.run_once(dry_run=False)

        rows = conn.execute(
            "SELECT in_stock, http_status, error FROM snapshots"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        in_stock, http_status, error = rows[0]
        self.assertEqual(in_stock, 0)
        self.assertIsNone(http_status)
        self.assertIn("timeout", (error or "").lower())


class ThreeXxWithoutLocationTests(unittest.TestCase):
    def test_recorded_and_tick_completes(self):
        url = "https://www.target.com/p/x/-/A-1"
        watch = Watch(id="w1", name="W", msrp_cap=6.99, sources=(url,))
        cfg = _make_config(watches=(watch,))
        conn = _make_conn()
        notifier = FakeNotifier()
        source = FakeSource([StockSnapshot(
            in_stock=False, price=None, title=None, sku=None,
            http_status=302, error="3xx without Location header (status=302)",
            parse_error=False,
        )])
        with tempfile.TemporaryDirectory() as td:
            tdp = pathlib.Path(td)
            orch = Orchestrator(
                cfg, conn, notifier, {"www.target.com": source},
                http_client=StubHttpClient(),
                data_dir=tdp / "data", logs_dir=tdp / "logs",
            )
            orch.run_once(dry_run=False)

        rows = conn.execute(
            "SELECT http_status, error FROM snapshots"
        ).fetchall()
        self.assertEqual(rows[0][0], 302)
        self.assertIn("Location", rows[0][1])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 0)


class Http500Tests(unittest.TestCase):
    def test_500_recorded_no_alert(self):
        url = "https://www.target.com/p/x/-/A-1"
        watch = Watch(id="w1", name="W", msrp_cap=6.99, sources=(url,))
        cfg = _make_config(watches=(watch,))
        conn = _make_conn()
        notifier = FakeNotifier()
        source = FakeSource([_net_err(http_status=500)])
        with tempfile.TemporaryDirectory() as td:
            tdp = pathlib.Path(td)
            orch = Orchestrator(
                cfg, conn, notifier, {"www.target.com": source},
                http_client=StubHttpClient(),
                data_dir=tdp / "data", logs_dir=tdp / "logs",
            )
            orch.run_once(dry_run=False)

        rows = conn.execute(
            "SELECT http_status, error FROM snapshots"
        ).fetchall()
        self.assertEqual(rows[0][0], 500)
        self.assertIsNotNone(rows[0][1])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 0)


class SecondWaveAfterCooldownTests(unittest.TestCase):
    def test_in_stock_oos_in_stock_after_cooldown_yields_two_alerts(self):
        url = "https://www.target.com/p/x/-/A-1"
        watch = Watch(id="w1", name="W", msrp_cap=6.99, sources=(url,))
        cfg = _make_config(watches=(watch,))
        conn = _make_conn()
        notifier = FakeNotifier()
        source = FakeSource([
            _ok(in_stock=True, price=5.99, sku="1"),
            _ok(in_stock=False, sku="1"),
            _ok(in_stock=True, price=5.99, sku="1"),
        ])

        clock = FrozenClock(datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc))
        with tempfile.TemporaryDirectory() as td:
            tdp = pathlib.Path(td)
            orch = Orchestrator(
                cfg, conn, notifier, {"www.target.com": source},
                http_client=StubHttpClient(),
                data_dir=tdp / "data", logs_dir=tdp / "logs",
            )
            with mock.patch.object(orchestrator_module, "_now", clock):
                orch.run_once(dry_run=False)
                clock.advance(cfg.poll_interval_seconds + 5)
                orch.run_once(dry_run=False)
                clock.advance(cfg.cooldown_minutes * 60 + 5)
                orch.run_once(dry_run=False)

        alerts = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE kind='stock'"
        ).fetchone()[0]
        self.assertEqual(alerts, 2)
        self.assertEqual(len(notifier.calls), 2)


class RetentionTests(unittest.TestCase):
    def test_31_day_old_snapshot_is_purged_recent_kept(self):
        url = "https://www.target.com/p/x/-/A-1"
        watch = Watch(id="w1", name="W", msrp_cap=6.99, sources=(url,))
        cfg = _make_config(watches=(watch,))
        conn = _make_conn()
        notifier = FakeNotifier()
        source = FakeSource([_ok(in_stock=False, sku="1")])

        old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        conn.execute(
            "INSERT INTO snapshots (watch_id, source_url, fetched_at, in_stock) "
            "VALUES (?, ?, ?, ?)",
            ("w1", url, old, 0),
        )
        conn.commit()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 1)

        with tempfile.TemporaryDirectory() as td:
            tdp = pathlib.Path(td)
            orch = Orchestrator(
                cfg, conn, notifier, {"www.target.com": source},
                http_client=StubHttpClient(),
                data_dir=tdp / "data", logs_dir=tdp / "logs",
            )
            orch.run_once(dry_run=False)

        remaining = conn.execute(
            "SELECT fetched_at FROM snapshots"
        ).fetchall()
        self.assertEqual(len(remaining), 1)
        # The remaining row should be the fresh one (today's), not the 31-day-old one.
        self.assertNotEqual(remaining[0][0], old)


class TestForceInStockOverrideTests(unittest.TestCase):
    # Spec §6.2 — the override path must fire the notifier without writing an
    # `alerts` row, so smoke tests cannot poison the 60-min cooldown for a
    # real restock of the same watch_id.

    def _orch(self, watch, conn, notifier, source, td):
        cfg = _make_config(watches=(watch,))
        tdp = pathlib.Path(td)
        return Orchestrator(
            cfg, conn, notifier,
            {"www.target.com": source} if source is not None else {},
            http_client=StubHttpClient(),
            data_dir=tdp / "data",
            logs_dir=tdp / "logs",
        )

    def test_override_fires_notifier_and_skips_alerts_row(self):
        watch = Watch(
            id="squeeezy-cheese", name="Squeeezy Cheese",
            msrp_cap=6.99,
            sources=("https://www.target.com/p/sunny-days-squeezy-cheese-block/-/A-1003785284",),
        )
        conn = _make_conn()
        notifier = FakeNotifier()
        # OOS fake — real fetch would NOT produce an alert; only the override should.
        source = FakeSource([_ok(in_stock=False)])
        with tempfile.TemporaryDirectory() as td:
            orch = self._orch(watch, conn, notifier, source, td)
            with mock.patch.dict(__import__("os").environ, {"TEST_FORCE_IN_STOCK": "squeeezy-cheese"}, clear=False):
                orch.run_once(dry_run=False)
        self.assertEqual(len(notifier.calls), 1)
        self.assertIn("\U0001F7E2 IN STOCK", notifier.calls[0])
        alerts = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE kind='stock'"
        ).fetchone()[0]
        self.assertEqual(alerts, 0)
        forced_snaps = conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE sku='forced'"
        ).fetchone()[0]
        self.assertEqual(forced_snaps, 1)

    def test_no_env_var_behaves_normally(self):
        watch = Watch(
            id="squeeezy-cheese", name="Squeeezy Cheese", msrp_cap=6.99,
            sources=("https://www.target.com/p/x/-/A-1",),
        )
        conn = _make_conn()
        notifier = FakeNotifier()
        source = FakeSource([_ok(in_stock=False)])
        env = {k: v for k, v in __import__("os").environ.items() if k != "TEST_FORCE_IN_STOCK"}
        with tempfile.TemporaryDirectory() as td:
            orch = self._orch(watch, conn, notifier, source, td)
            with mock.patch.dict(__import__("os").environ, env, clear=True):
                orch.run_once(dry_run=False)
        self.assertEqual(len(notifier.calls), 0)
        forced_snaps = conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE sku='forced'"
        ).fetchone()[0]
        self.assertEqual(forced_snaps, 0)

    def test_override_wins_over_real_in_stock_same_watch(self):
        watch = Watch(
            id="squeeezy-cheese", name="Squeeezy Cheese", msrp_cap=6.99,
            sources=("https://www.target.com/p/x/-/A-1",),
        )
        conn = _make_conn()
        notifier = FakeNotifier()
        # Even with a real in-stock fetch staged, the override `continue` should
        # short-circuit before the fetch happens — exactly one notifier call.
        source = FakeSource([_ok(in_stock=True, price=5.99, sku="real-1")])
        with tempfile.TemporaryDirectory() as td:
            orch = self._orch(watch, conn, notifier, source, td)
            with mock.patch.dict(__import__("os").environ, {"TEST_FORCE_IN_STOCK": "squeeezy-cheese"}, clear=False):
                orch.run_once(dry_run=False)
        self.assertEqual(len(notifier.calls), 1)
        # Real source.fetch was never called because override `continue`'d first.
        self.assertEqual(source.fetches, [])
        alerts = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE kind='stock'"
        ).fetchone()[0]
        self.assertEqual(alerts, 0)

    def test_re_firing_within_60min_is_not_cooldown_blocked(self):
        watch = Watch(
            id="squeeezy-cheese", name="Squeeezy Cheese", msrp_cap=6.99,
            sources=("https://www.target.com/p/x/-/A-1",),
        )
        conn = _make_conn()
        notifier = FakeNotifier()
        source = FakeSource([_ok(in_stock=False), _ok(in_stock=False)])
        clock = FrozenClock(datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc))
        with tempfile.TemporaryDirectory() as td:
            orch = self._orch(watch, conn, notifier, source, td)
            with mock.patch.dict(__import__("os").environ, {"TEST_FORCE_IN_STOCK": "squeeezy-cheese"}, clear=False), \
                 mock.patch.object(orchestrator_module, "_now", clock):
                orch.run_once(dry_run=False)
                clock.advance(10 * 60)  # 10 min — well inside the 60-min cooldown
                orch.run_once(dry_run=False)
        self.assertEqual(len(notifier.calls), 2)
        alerts = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE kind='stock'"
        ).fetchone()[0]
        self.assertEqual(alerts, 0)


if __name__ == "__main__":
    unittest.main()
