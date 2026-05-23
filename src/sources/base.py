"""Source base classes + HttpClient with full polite-polling.

Spec §6 (anti-bot hygiene) and §4 (failure handling) live here so every
concrete Source can rely on UA rotation, matched client-hint headers,
per-UA cookie jars, hourly Target homepage warm-up, and per-(host, ua)
exponential backoff for free.
"""

from __future__ import annotations

import gzip
import hashlib
import http.cookiejar
import io
import logging
import pathlib
import random
import secrets
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StockSnapshot:
    """Result of one Source.fetch.

    Invariant:
        ``parse_error=True`` implies ``http_status == 200`` and the
        retailer's structured payload could not be parsed. Any non-200
        outcome is signalled via ``http_status`` + ``error`` and leaves
        ``parse_error=False`` (so the parser-broken 3-strike counter in
        the orchestrator does not bump on plain network failures).
    """

    in_stock: bool
    price: float | None
    title: str | None
    sku: str | None
    http_status: int | None
    error: str | None
    parse_error: bool


class Source(Protocol):
    def fetch(
        self,
        url: str,
        *,
        http_client: "HttpClient",
        user_agent: str,
    ) -> StockSnapshot: ...


# Four realistic recent Chrome-on-macOS UAs (Chrome 131, late 2024 / early 2025
# stable channel — what a real Brian-on-Sequoia browser would emit).
USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6_1) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_1_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_2_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
)

_HTTP_READ_TIMEOUT_SECONDS = 30
_BACKOFF_INITIAL_SECONDS = 60
_BACKOFF_MAX_SECONDS = 1800
_WARMUP_INTERVAL_SECONDS = 3600


def _chrome_major_from_ua(ua: str) -> str:
    # The matched sec-ch-ua header has to reference the same major version as
    # the UA string; mismatched values are a strong fingerprinting signal.
    marker = "Chrome/"
    i = ua.find(marker)
    if i == -1:
        return "131"
    tail = ua[i + len(marker):]
    j = tail.find(".")
    return tail[:j] if j != -1 else "131"


def _headers_for_ua(ua: str, *, sec_fetch_mode: str = "navigate") -> dict[str, str]:
    major = _chrome_major_from_ua(ua)
    sec_ch_ua = (
        f'"Google Chrome";v="{major}", "Chromium";v="{major}", '
        f'"Not_A Brand";v="24"'
    )
    return {
        "User-Agent": ua,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip",
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-site": "none" if sec_fetch_mode == "navigate" else "same-origin",
        "sec-fetch-mode": sec_fetch_mode,
        "sec-fetch-dest": "document" if sec_fetch_mode == "navigate" else "empty",
        "Upgrade-Insecure-Requests": "1",
    }


def _ua_hash(ua: str) -> str:
    return hashlib.sha256(ua.encode("utf-8")).hexdigest()[:16]


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str


class HttpClient:
    """Polite-polling HTTP client.

    Owns spec §6 in full: UA rotation, matched client-hint headers, per-UA
    cookie jars, hourly Target homepage warm-up, per-(host, ua) backoff,
    transparent gzip decode, redirects-with-missing-Location detection.
    Per-instance random visitor_id is exposed for RedSky callers (one Brian,
    one tab — within a tick all calls share it).
    """

    def __init__(
        self,
        data_dir: pathlib.Path | str = pathlib.Path("data"),
        *,
        rng: random.Random | None = None,
        now_func=None,
    ) -> None:
        self._data_dir = pathlib.Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._rng = rng if rng is not None else random.SystemRandom()
        self._now = now_func or (lambda: datetime.now(timezone.utc))
        self._backoff: dict[tuple[str, str], dict[str, Any]] = {}
        self._jars: dict[str, http.cookiejar.LWPCookieJar] = {}
        self._opened_jar_paths: set[pathlib.Path] = set()
        self.visitor_id: str = secrets.token_hex(16)

    def select_ua(self) -> str:
        return self._rng.choice(USER_AGENTS)

    def _jar_path(self, ua: str) -> pathlib.Path:
        return self._data_dir / f"cookies-{_ua_hash(ua)}.txt"

    def _warmup_path(self, ua: str) -> pathlib.Path:
        return self._data_dir / f"cookies-{_ua_hash(ua)}.warmup"

    def _get_jar(self, ua: str) -> http.cookiejar.LWPCookieJar:
        if ua in self._jars:
            return self._jars[ua]
        jar_path = self._jar_path(ua)
        jar = http.cookiejar.LWPCookieJar(filename=str(jar_path))
        if jar_path.exists():
            try:
                jar.load(ignore_discard=True, ignore_expires=True)
            except (OSError, http.cookiejar.LoadError):
                log.warning("could not load cookie jar at %s; starting fresh", jar_path)
        self._jars[ua] = jar
        return jar

    def _save_jar(self, ua: str) -> None:
        jar = self._jars.get(ua)
        if jar is None:
            return
        try:
            jar.save(ignore_discard=True, ignore_expires=True)
        except OSError as e:
            log.warning("could not save cookie jar for ua_hash=%s: %s", _ua_hash(ua), e)

    def _build_opener(self, ua: str) -> urllib.request.OpenerDirector:
        jar = self._get_jar(ua)
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar)
        )

    def next_allowed_at(self, host: str, ua: str) -> datetime:
        state = self._backoff.get((host, ua))
        if state is None:
            return self._now()
        return state["next_allowed_at"]

    def _bump_backoff(self, host: str, ua: str) -> None:
        key = (host, ua)
        current = self._backoff.get(key, {"delay": 0, "next_allowed_at": self._now()})
        prev = current["delay"]
        new_delay = _BACKOFF_INITIAL_SECONDS if prev == 0 else min(prev * 2, _BACKOFF_MAX_SECONDS)
        self._backoff[key] = {
            "delay": new_delay,
            "next_allowed_at": self._now() + timedelta(seconds=new_delay),
        }

    def _reset_backoff(self, host: str, ua: str) -> None:
        self._backoff.pop((host, ua), None)

    def ensure_target_warmup(self, ua: str) -> None:
        warmup_path = self._warmup_path(ua)
        try:
            mtime = warmup_path.stat().st_mtime
            last = datetime.fromtimestamp(mtime, tz=timezone.utc)
        except FileNotFoundError:
            last = None
        now = self._now()
        if last is not None and (now - last).total_seconds() < _WARMUP_INTERVAL_SECONDS:
            return
        try:
            self.request("https://www.target.com/", ua=ua, method="HEAD", _is_warmup=True)
        except Exception as e:
            log.warning("target warmup failed (continuing): %s", e)
            return
        warmup_path.touch()

    def request(
        self,
        url: str,
        *,
        ua: str,
        method: str = "GET",
        extra_headers: dict[str, str] | None = None,
        _is_warmup: bool = False,
    ) -> HttpResponse:
        host = urllib.parse.urlsplit(url).hostname or ""
        headers = _headers_for_ua(
            ua,
            sec_fetch_mode="navigate" if method in ("GET", "HEAD") else "cors",
        )
        if extra_headers:
            headers.update(extra_headers)
        opener = self._build_opener(ua)
        req = urllib.request.Request(url, method=method, headers=headers)
        try:
            with opener.open(req, timeout=_HTTP_READ_TIMEOUT_SECONDS) as resp:
                status = resp.status
                resp_headers = {k.lower(): v for k, v in resp.getheaders()}
                raw = resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            resp_headers = {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
            try:
                raw = e.read() or b""
            except Exception:
                raw = b""
            # Treat 3xx that surface as HTTPError without a Location as the
            # documented missing-Location error path.
            if 300 <= status < 400 and "location" not in resp_headers:
                self._save_jar(ua)
                raise _MissingLocationError(status=status, url=url, headers=resp_headers)

        if 300 <= status < 400 and "location" not in resp_headers:
            self._save_jar(ua)
            raise _MissingLocationError(status=status, url=url, headers=resp_headers)

        if resp_headers.get("content-encoding", "").lower() == "gzip":
            try:
                raw = gzip.decompress(raw)
            except (OSError, EOFError):
                pass

        if not _is_warmup:
            if status == 429 or status == 503:
                self._bump_backoff(host, ua)
            elif status == 200:
                self._reset_backoff(host, ua)

        self._save_jar(ua)
        return HttpResponse(status=status, headers=resp_headers, body=raw, url=url)

    def record_failure(self, host: str, ua: str) -> None:
        """Public hook for callers that want to penalize a host without going
        through ``request`` — e.g. an exception path where the response
        object never existed but the source decided this counts as a 429-ish
        signal. Currently unused but intentional surface area."""
        self._bump_backoff(host, ua)


class _MissingLocationError(Exception):
    def __init__(self, *, status: int, url: str, headers: dict[str, str]):
        self.status = status
        self.url = url
        self.headers = headers
        super().__init__(f"3xx response from {url} without Location header (status={status})")


def snapshot_from_missing_location(err: _MissingLocationError) -> StockSnapshot:
    return StockSnapshot(
        in_stock=False,
        price=None,
        title=None,
        sku=None,
        http_status=err.status,
        error=f"3xx without Location header (status={err.status})",
        parse_error=False,
    )


def snapshot_from_network_error(exc: BaseException, *, http_status: int | None = None) -> StockSnapshot:
    return StockSnapshot(
        in_stock=False,
        price=None,
        title=None,
        sku=None,
        http_status=http_status,
        error=f"{type(exc).__name__}: {exc}",
        parse_error=False,
    )


# urllib.parse imported lazily; declared here to avoid name shadowing in the
# class body where `urllib.request` is also referenced.
import urllib.parse  # noqa: E402
