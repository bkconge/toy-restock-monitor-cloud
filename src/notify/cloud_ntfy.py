"""v2 ntfy notifier — prepends `[Cloud] ` to distinguish from v1 alerts
on the shared topic (spec §2, §5.1)."""

from __future__ import annotations

from .ntfy import NtfyNotifier


class CloudNtfyNotifier(NtfyNotifier):
    def send(self, text):
        return super().send(f"[Cloud] {text}")
