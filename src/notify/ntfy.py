"""ntfy.sh diagnostic mirror per spec §5.2 — unauthenticated POST."""

from __future__ import annotations

import socket
import urllib.error
import urllib.request

from .base import NotifyResult


_CHANNEL = "ntfy"


class NtfyNotifier:
    def __init__(self, topic_url: str, timeout_seconds: float = 10.0) -> None:
        self.topic_url = topic_url
        self.timeout_seconds = timeout_seconds

    def send(self, text: str) -> NotifyResult:
        req = urllib.request.Request(
            self.topic_url,
            method="POST",
            data=text.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                status = getattr(resp, "status", None)
                if status is None:
                    status = resp.getcode()
                if 200 <= status < 300:
                    return NotifyResult(channel=_CHANNEL, success=True, error=None)
                return NotifyResult(
                    channel=_CHANNEL,
                    success=False,
                    error=f"ntfy non-2xx status: {status}",
                )
        except urllib.error.HTTPError as e:
            return NotifyResult(
                channel=_CHANNEL,
                success=False,
                error=f"HTTPError: {e.code} {e.reason}",
            )
        except urllib.error.URLError as e:
            return NotifyResult(
                channel=_CHANNEL,
                success=False,
                error=f"URLError: {e.reason}",
            )
        except socket.timeout as e:
            return NotifyResult(
                channel=_CHANNEL,
                success=False,
                error=f"socket.timeout: {e}",
            )
