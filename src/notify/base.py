"""Notifier interface + result dataclass per spec §5."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class NotifyResult:
    channel: str
    success: bool
    error: str | None = None


@runtime_checkable
class Notifier(Protocol):
    def send(self, text: str) -> NotifyResult | list[NotifyResult]:
        ...
