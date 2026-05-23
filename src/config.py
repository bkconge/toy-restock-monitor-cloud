"""Config loader for toy-restock-monitor.

Loads `config.yaml` per spec §8, validates required keys and types, and
returns frozen dataclasses. Raises ConfigError naming the offending key
on any failure — main.py turns this into a non-zero exit + stderr line.
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field
from typing import Any

import yaml


class ConfigError(Exception):
    """Raised when config.yaml is missing, malformed, or fails validation."""


@dataclass(frozen=True)
class Watch:
    id: str
    name: str
    msrp_cap: float
    sources: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    notifier: str
    ntfy_topic_url: str
    redsky_key: str
    poll_interval_seconds: int
    cooldown_minutes: int
    parse_fail_threshold: int
    http_read_timeout_seconds: int
    tick_wall_clock_cap_seconds: int
    log_level: str
    watches: tuple[Watch, ...]
    per_source_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)


_REQUIRED_TOP_KEYS: dict[str, type | tuple[type, ...]] = {
    "notifier": str,
    "ntfy_topic_url": str,
    "redsky_key": str,
    "poll_interval_seconds": int,
    "cooldown_minutes": int,
    "parse_fail_threshold": int,
    "http_read_timeout_seconds": int,
    "tick_wall_clock_cap_seconds": int,
    "log_level": str,
    "watches": list,
}

_REQUIRED_WATCH_KEYS: dict[str, type | tuple[type, ...]] = {
    "id": str,
    "name": str,
    # accept int as a convenience but represent as float
    "msrp_cap": (int, float),
    "sources": list,
}

_ALLOWED_NOTIFIERS = {"ntfy"}


def _resolve_env(value: Any) -> Any:
    if not isinstance(value, str) or not value.startswith("env:"):
        return value
    name = value[4:]
    if not name:
        raise ConfigError("config: 'env:' prefix without variable name")
    try:
        return os.environ[name]
    except KeyError:
        raise ConfigError(f"config: env var {name!r} is not set") from None


def _check_type(key: str, value: Any, expected: type | tuple[type, ...]) -> None:
    # bool is a subclass of int in Python — exclude it explicitly so a stray
    # `msrp_cap: true` doesn't sneak past the (int, float) gate.
    if isinstance(value, bool) and bool not in (expected if isinstance(expected, tuple) else (expected,)):
        raise ConfigError(f"key '{key}' has wrong type: expected {expected}, got bool")
    if not isinstance(value, expected):
        raise ConfigError(
            f"key '{key}' has wrong type: expected {expected}, got {type(value).__name__}"
        )


def _parse_watch(raw: Any, index: int) -> Watch:
    if not isinstance(raw, dict):
        raise ConfigError(f"watches[{index}] is not a mapping")
    for key, expected in _REQUIRED_WATCH_KEYS.items():
        if key not in raw:
            raise ConfigError(f"watches[{index}] missing required key '{key}'")
        _check_type(f"watches[{index}].{key}", raw[key], expected)
    sources = raw["sources"]
    if not sources:
        raise ConfigError(f"watches[{index}].sources is empty")
    for j, src in enumerate(sources):
        if not isinstance(src, str):
            raise ConfigError(
                f"watches[{index}].sources[{j}] is not a string"
            )
    return Watch(
        id=raw["id"],
        name=raw["name"],
        msrp_cap=float(raw["msrp_cap"]),
        sources=tuple(sources),
    )


def load(path: pathlib.Path | str) -> Config:
    p = pathlib.Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found at path '{p}'")
    try:
        raw_text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"failed to read config at '{p}': {e}") from e
    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as e:
        raise ConfigError(f"config at '{p}' is malformed YAML: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(
            f"config at '{p}' is not a mapping at top level (got {type(data).__name__})"
        )

    # Resolve env: prefixes on top-level scalar strings BEFORE type checks
    # so the resolved value (always a str) goes through validation. Fail-fast
    # at load-time on any missing env var (spec §6.1).
    for k, v in list(data.items()):
        if isinstance(v, str):
            data[k] = _resolve_env(v)

    for key, expected in _REQUIRED_TOP_KEYS.items():
        if key not in data:
            raise ConfigError(f"missing required key '{key}'")
        _check_type(key, data[key], expected)

    if data["notifier"] not in _ALLOWED_NOTIFIERS:
        raise ConfigError(
            f"key 'notifier' has invalid value '{data['notifier']}'; "
            f"allowed: {sorted(_ALLOWED_NOTIFIERS)}"
        )

    per_source_overrides = data.get("per_source_overrides") or {}
    if not isinstance(per_source_overrides, dict):
        raise ConfigError(
            "key 'per_source_overrides' has wrong type: expected dict"
        )

    watches = tuple(
        _parse_watch(w, i) for i, w in enumerate(data["watches"])
    )

    return Config(
        notifier=data["notifier"],
        ntfy_topic_url=data["ntfy_topic_url"],
        redsky_key=data["redsky_key"],
        poll_interval_seconds=int(data["poll_interval_seconds"]),
        cooldown_minutes=int(data["cooldown_minutes"]),
        parse_fail_threshold=int(data["parse_fail_threshold"]),
        http_read_timeout_seconds=int(data["http_read_timeout_seconds"]),
        tick_wall_clock_cap_seconds=int(data["tick_wall_clock_cap_seconds"]),
        log_level=data["log_level"],
        watches=watches,
        per_source_overrides=per_source_overrides,
    )
