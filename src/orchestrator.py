"""Orchestrator state machine (plan.md task #8).

Owns the one-tick loop body: pidfile lock, wall-clock cap, per-source poll
gating, backoff gate, fetch + record, stock + parser-broken alert paths
with the spec §4 dedup queries, and the 30-day snapshot retention purge.

All timestamps written via `_now()` — patchable from tests — and always
ISO8601 UTC; never SQLite's local-time CURRENT_TIMESTAMP (spec §4).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import pathlib
import socket
import sqlite3
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

from src.config import Config, Watch
from src.notify.base import NotifyResult
from src.sources.base import HttpClient, StockSnapshot, snapshot_from_network_error


log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


_SNAPSHOT_RETENTION_DAYS = 30


def _host_of(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def _format_stock_alert(watch: Watch, snapshot: StockSnapshot, url: str) -> str:
    price_str = f"${snapshot.price:.2f}" if snapshot.price is not None else "$?"
    retailer = _host_of(url) or "target.com"
    return (
        f"\U0001F7E2 IN STOCK at MSRP — {watch.name}\n"
        f"{price_str} at {retailer}\n"
        f"{url}"
    )


def _format_parser_broken_alert(source_url: str, consecutive: int) -> str:
    return (
        f"⚠️ Parser broken — {source_url}\n"
        f"{consecutive} consecutive parse failures; please check"
    )


class Orchestrator:
    """One-tick orchestrator wired against an existing DB connection,
    a configured Notifier, and a host-keyed Source registry."""

    def __init__(
        self,
        config: Config,
        conn: sqlite3.Connection,
        notifier: Any,
        sources_by_host: dict[str, Any],
        *,
        http_client: HttpClient | None = None,
        data_dir: pathlib.Path | str = pathlib.Path("data"),
        logs_dir: pathlib.Path | str = pathlib.Path("logs"),
    ) -> None:
        self.config = config
        self.conn = conn
        self.notifier = notifier
        self.sources_by_host = sources_by_host
        self.data_dir = pathlib.Path(data_dir)
        self.logs_dir = pathlib.Path(logs_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.http_client = http_client or HttpClient(data_dir=self.data_dir)
        self.dry_run = False

    # ----- public entry point -----
    def run_once(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        pid_path = self.data_dir / "orchestrator.pid"
        try:
            pid_file = open(pid_path, "a+")
        except OSError as e:
            log.error("could not open pidfile %s: %s", pid_path, e)
            return
        try:
            try:
                fcntl.flock(pid_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                log.info("another tick already holds %s; exiting", pid_path)
                return
            try:
                pid_file.seek(0)
                pid_file.truncate()
                import os
                pid_file.write(f"{os.getpid()}\n")
                pid_file.flush()
            except OSError:
                pass

            self._tick()
        finally:
            try:
                fcntl.flock(pid_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            pid_file.close()

    # ----- tick body -----
    def _tick(self) -> None:
        tick_start = _now()
        wall_cap = self.config.tick_wall_clock_cap_seconds or 120
        ua = self.http_client.select_ua()
        log.info("tick start ua_len=%d watches=%d", len(ua), len(self.config.watches))

        forced = os.environ.get("TEST_FORCE_IN_STOCK", "").strip()

        for watch in self.config.watches:
            for source_url in watch.sources:
                elapsed = (_now() - tick_start).total_seconds()
                if elapsed >= wall_cap:
                    log.warning(
                        "wall-clock cap %.0fs reached; aborting remaining sources",
                        wall_cap,
                    )
                    self._purge_old_snapshots()
                    return

                # Spec §6.2 — TEST_FORCE_IN_STOCK smoke hook. Synthesizes a
                # cheap in-stock snapshot, fires the notifier, but SKIPS the
                # alerts table write so the 60-min cooldown for the real
                # watch_id is not poisoned by the smoke.
                if forced and watch.id == forced:
                    synth = StockSnapshot(
                        in_stock=True, price=0.01,
                        title="TEST_FORCE_IN_STOCK override",
                        sku="forced", http_status=200, error=None,
                        parse_error=False,
                    )
                    self._insert_snapshot(watch, source_url, synth)
                    self._reset_parse_counter(watch, source_url)
                    msg = _format_stock_alert(watch, synth, source_url)
                    if self.dry_run:
                        self._write_dry_run_entry(
                            watch_id=watch.id, source_url=source_url,
                            sku=synth.sku, price=synth.price, message=msg,
                        )
                    else:
                        try:
                            result = self.notifier.send(msg)
                        except Exception as e:
                            log.exception("notifier raised under override: %s", e)
                            result = NotifyResult(channel="notifier", success=False, error=str(e))
                        log.info("TEST_FORCE_IN_STOCK notify result=%s", result)
                    log.warning(
                        "TEST_FORCE_IN_STOCK fired for %s; alerts row NOT written",
                        watch.id,
                    )
                    continue

                if self._is_too_recent(watch, source_url):
                    continue

                host = _host_of(source_url)
                # HttpClient.next_allowed_at returns "now" when no backoff is
                # active, so a strict `<` comparison would race on microseconds.
                # Only skip when the gate is meaningfully in the future.
                gate = self.http_client.next_allowed_at(host, ua)
                if (gate - _now()).total_seconds() > 1:
                    log.info(
                        "source %s host=%s in backoff until %s; skipping",
                        source_url, host, gate.isoformat(),
                    )
                    continue

                source = self.sources_by_host.get(host)
                if source is None:
                    log.warning(
                        "no source registered for host=%s url=%s; recording error snapshot",
                        host, source_url,
                    )
                    snap = StockSnapshot(
                        in_stock=False, price=None, title=None, sku=None,
                        http_status=None,
                        error=f"no source registered for host {host}",
                        parse_error=False,
                    )
                else:
                    try:
                        snap = source.fetch(
                            source_url,
                            http_client=self.http_client,
                            user_agent=ua,
                        )
                    except socket.timeout as e:
                        snap = snapshot_from_network_error(e)
                    except Exception as e:
                        log.exception("fetch crashed for %s: %s", source_url, e)
                        snap = snapshot_from_network_error(e)

                self._insert_snapshot(watch, source_url, snap)

                if snap.http_status == 200 and not snap.parse_error and snap.error is None:
                    self._reset_parse_counter(watch, source_url)

                if (
                    snap.in_stock
                    and snap.price is not None
                    and snap.price <= watch.msrp_cap
                ):
                    self._maybe_stock_alert(watch, source_url, snap)

                if snap.parse_error:
                    self._maybe_parser_broken_alert(watch, source_url)

        self._purge_old_snapshots()

    # ----- gating -----
    def _is_too_recent(self, watch: Watch, source_url: str) -> bool:
        override = (self.config.per_source_overrides or {}).get(source_url) or {}
        interval = int(
            override.get("poll_interval_seconds")
            or self.config.poll_interval_seconds
        )
        cur = self.conn.execute(
            "SELECT MAX(fetched_at) FROM snapshots WHERE watch_id=? AND source_url=?",
            (watch.id, source_url),
        )
        row = cur.fetchone()
        last_iso = row[0] if row else None
        if not last_iso:
            return False
        try:
            last = datetime.fromisoformat(last_iso)
        except ValueError:
            return False
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (_now() - last).total_seconds() < interval

    # ----- snapshot writes -----
    def _insert_snapshot(self, watch: Watch, source_url: str, snap: StockSnapshot) -> None:
        self.conn.execute(
            """
            INSERT INTO snapshots
              (watch_id, source_url, sku, fetched_at, in_stock,
               price, title, http_status, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                watch.id,
                source_url,
                snap.sku,
                _now().isoformat(),
                1 if snap.in_stock else 0,
                snap.price,
                snap.title,
                snap.http_status,
                snap.error,
            ),
        )
        self.conn.commit()

    # ----- stock alert path -----
    def _maybe_stock_alert(
        self, watch: Watch, source_url: str, snap: StockSnapshot
    ) -> None:
        cooldown_threshold = (
            _now() - timedelta(minutes=self.config.cooldown_minutes)
        ).isoformat()
        cur = self.conn.execute(
            """
            SELECT 1 FROM alerts
             WHERE watch_id = ?
               AND source_url = ?
               AND COALESCE(sku, '') = COALESCE(?, '')
               AND kind = 'stock'
               AND fired_at >= ?
             LIMIT 1
            """,
            (watch.id, source_url, snap.sku, cooldown_threshold),
        )
        if cur.fetchone() is not None:
            return

        message = _format_stock_alert(watch, snap, source_url)
        self._dispatch(
            message,
            watch=watch,
            source_url=source_url,
            sku=snap.sku,
            price=snap.price,
            kind="stock",
        )

    # ----- parser-broken alert path -----
    def _reset_parse_counter(self, watch: Watch, source_url: str) -> None:
        self.conn.execute(
            """
            INSERT INTO parse_fail_counters
              (watch_id, source_url, consecutive_failures, last_updated)
            VALUES (?, ?, 0, ?)
            ON CONFLICT(watch_id, source_url) DO UPDATE SET
              consecutive_failures = 0,
              last_updated = excluded.last_updated
            """,
            (watch.id, source_url, _now().isoformat()),
        )
        self.conn.commit()

    def _maybe_parser_broken_alert(self, watch: Watch, source_url: str) -> None:
        self.conn.execute(
            """
            INSERT INTO parse_fail_counters
              (watch_id, source_url, consecutive_failures, last_updated)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(watch_id, source_url) DO UPDATE SET
              consecutive_failures = consecutive_failures + 1,
              last_updated = excluded.last_updated
            """,
            (watch.id, source_url, _now().isoformat()),
        )
        self.conn.commit()

        cur = self.conn.execute(
            "SELECT consecutive_failures FROM parse_fail_counters "
            "WHERE watch_id=? AND source_url=?",
            (watch.id, source_url),
        )
        row = cur.fetchone()
        consecutive = row[0] if row else 0
        if consecutive < self.config.parse_fail_threshold:
            return

        now = _now()
        day_start = datetime(
            now.year, now.month, now.day, tzinfo=timezone.utc
        ).isoformat()
        cur = self.conn.execute(
            """
            SELECT 1 FROM alerts
             WHERE source_url = ?
               AND kind = 'parser_broken'
               AND fired_at >= ?
             LIMIT 1
            """,
            (source_url, day_start),
        )
        if cur.fetchone() is not None:
            return

        message = _format_parser_broken_alert(source_url, consecutive)
        self._dispatch(
            message,
            watch=watch,
            source_url=source_url,
            sku=None,
            price=None,
            kind="parser_broken",
        )

    # ----- dispatch + alerts row -----
    def _dispatch(
        self,
        message: str,
        *,
        watch: Watch | None,
        source_url: str,
        sku: str | None,
        price: float | None,
        kind: str,
    ) -> None:
        if self.dry_run:
            self._write_dry_run_entry(
                watch_id=(watch.id if watch is not None else None),
                source_url=source_url,
                sku=sku,
                price=price,
                message=message,
            )
        else:
            try:
                result = self.notifier.send(message)
            except Exception as e:
                log.exception("notifier raised: %s", e)
                result = NotifyResult(channel="notifier", success=False, error=str(e))
            log.info("notify dispatch kind=%s result=%s", kind, result)

        self.conn.execute(
            """
            INSERT INTO alerts
              (watch_id, source_url, sku, kind, fired_at, price, message)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (watch.id if watch is not None else None),
                source_url,
                sku,
                kind,
                _now().isoformat(),
                price,
                message,
            ),
        )
        self.conn.commit()

    def _write_dry_run_entry(
        self,
        *,
        watch_id: str | None,
        source_url: str,
        sku: str | None,
        price: float | None,
        message: str,
    ) -> None:
        entry = {
            "timestamp": _now().isoformat(),
            "watch_id": watch_id,
            "source_url": source_url,
            "sku": sku,
            "price": price,
            "message": message,
        }
        path = self.logs_dir / "dry-run.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ----- retention -----
    def _purge_old_snapshots(self) -> None:
        threshold = (
            _now() - timedelta(days=_SNAPSHOT_RETENTION_DAYS)
        ).isoformat()
        self.conn.execute(
            "DELETE FROM snapshots WHERE fetched_at < ?",
            (threshold,),
        )
        self.conn.commit()
