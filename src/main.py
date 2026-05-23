"""Launchd entrypoint for toy-restock-monitor.

Wires argparse + logging + config + DB bootstrap + the Phase-2 orchestrator
(spec §4 / plan task #8). One short-lived process per launchd fire; the
orchestrator's pidfile flock guards against overlap.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import pathlib
import sys

from src import db
from src.config import ConfigError, load as load_config
from src.notify.factory import build_notifier
from src.orchestrator import Orchestrator
from src.sources.base import HttpClient
from src.sources.target import TargetSource


DEFAULT_CONFIG_PATH = pathlib.Path("config.yaml")
DEFAULT_DB_PATH = pathlib.Path("data/state.db")
DEFAULT_LOG_DIR = pathlib.Path("logs")
DEFAULT_LOG_FILE = DEFAULT_LOG_DIR / "app.log"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="toy-restock-monitor")
    parser.add_argument(
        "--once",
        action="store_true",
        default=True,
        help="Run one tick and exit (currently the only supported mode).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Capture would-be notifications to logs/dry-run.jsonl; send none.",
    )
    parser.add_argument(
        "--config",
        type=pathlib.Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to config.yaml (default: {DEFAULT_CONFIG_PATH}).",
    )
    parser.add_argument(
        "--db-path",
        type=pathlib.Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to SQLite DB (default: {DEFAULT_DB_PATH}).",
    )
    parser.add_argument(
        "--log-file",
        type=pathlib.Path,
        default=DEFAULT_LOG_FILE,
        help=f"Path to rotating log file (default: {DEFAULT_LOG_FILE}).",
    )
    return parser.parse_args(argv)


def _configure_logging(log_file: pathlib.Path, level: str) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    # avoid duplicate handlers when called repeatedly under unittest
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    handler = logging.handlers.RotatingFileHandler(
        filename=str(log_file),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)
    try:
        root.setLevel(level.upper())
    except (TypeError, ValueError):
        root.setLevel(logging.INFO)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    _configure_logging(args.log_file, cfg.log_level)
    log = logging.getLogger("main")
    log.info(
        "startup: config=%s dry_run=%s watches=%d",
        args.config, args.dry_run, len(cfg.watches),
    )

    args.db_path.parent.mkdir(parents=True, exist_ok=True)
    args.log_file.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(args.db_path)
    try:
        db.bootstrap(conn)

        if not cfg.watches:
            log.info("no watches configured")
            return 0

        data_dir = args.db_path.parent
        logs_dir = args.log_file.parent
        http_client = HttpClient(data_dir=data_dir)
        target_source = TargetSource()
        # Both apex and www variants resolve to the same parser — Target serves
        # both, callers may configure either, and using one shared TargetSource
        # keeps the per-instance visitor_id and warm-up state consistent.
        sources_by_host = {
            "target.com": target_source,
            "www.target.com": target_source,
        }

        try:
            notifier = build_notifier(cfg)
        except ConfigError as e:
            print(f"config error: {e}", file=sys.stderr)
            return 2

        orch = Orchestrator(
            cfg,
            conn,
            notifier,
            sources_by_host,
            http_client=http_client,
            data_dir=data_dir,
            logs_dir=logs_dir,
        )
        orch.run_once(dry_run=args.dry_run)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
