"""Maps `notifier:` config value to a concrete Notifier instance."""

from __future__ import annotations

from src.config import Config, ConfigError

from .cloud_ntfy import CloudNtfyNotifier


def build_notifier(config: Config):
    mode = config.notifier
    if mode == "ntfy":
        return CloudNtfyNotifier(config.ntfy_topic_url)
    raise ConfigError(f"unknown notifier mode: {mode!r}")
