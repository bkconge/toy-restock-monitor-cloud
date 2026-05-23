"""Mirror notifier — sends to all children, each independently."""

from __future__ import annotations

import logging

from .base import NotifyResult


_log = logging.getLogger(__name__)


class MirrorNotifier:
    def __init__(self, *notifiers: object) -> None:
        self._notifiers = notifiers

    def send(self, text: str) -> list[NotifyResult]:
        results: list[NotifyResult] = []
        for n in self._notifiers:
            try:
                r = n.send(text)
            except Exception as e:
                # Defensive: a child notifier raising should not stop the others.
                channel = type(n).__name__
                r = NotifyResult(
                    channel=channel,
                    success=False,
                    error=f"unhandled {type(e).__name__}: {e}",
                )
            if isinstance(r, list):
                for sub in r:
                    _log.info("notify result: %s", sub)
                results.extend(r)
            else:
                _log.info("notify result: %s", r)
                results.append(r)
        return results
