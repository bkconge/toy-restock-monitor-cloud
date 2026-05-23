"""HttpClient tests — UA rotation, cookie persistence, backoff, redirects.

All HTTP is mocked at urlopen; no live network calls.
"""

from __future__ import annotations

import io
import pathlib
import random
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src.sources.base import (
    HttpClient,
    USER_AGENTS,
    _MissingLocationError,
    snapshot_from_missing_location,
    _ua_hash,
)


class _FakeRespCM:
    """Mimics what `opener.open(...)` returns as a context manager."""

    def __init__(self, status: int, headers: dict[str, str], body: bytes, url: str = ""):
        self.status = status
        self._headers = headers
        self._body = body
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def getheaders(self):
        return list(self._headers.items())

    def read(self):
        return self._body


def _patch_open(monkey_open):
    return patch(
        "urllib.request.OpenerDirector.open",
        new=lambda self, req, timeout=None: monkey_open(req, timeout),
    )


class TestUARotation(unittest.TestCase):
    def test_select_ua_returns_at_least_three_unique_in_100_calls(self):
        with tempfile.TemporaryDirectory() as td:
            client = HttpClient(td, rng=random.Random(7))
            picks = {client.select_ua() for _ in range(100)}
            self.assertGreaterEqual(len(picks), 3, f"only {len(picks)} unique UAs from 100 picks: {picks}")
            self.assertTrue(picks.issubset(set(USER_AGENTS)))


class TestCookieJarPersistence(unittest.TestCase):
    def test_jar_persists_across_client_instances(self):
        import http.cookiejar

        ua = USER_AGENTS[0]

        with tempfile.TemporaryDirectory() as td:
            data_dir = pathlib.Path(td)

            c1 = HttpClient(data_dir)
            jar = c1._get_jar(ua)
            cookie = http.cookiejar.Cookie(
                version=0, name="TGTSID", value="fake-cookie-value",
                port=None, port_specified=False,
                domain=".target.com", domain_specified=True, domain_initial_dot=True,
                path="/", path_specified=True,
                secure=False, expires=None, discard=False,
                comment=None, comment_url=None, rest={}, rfc2109=False,
            )
            jar.set_cookie(cookie)
            c1._save_jar(ua)

            jar_path = data_dir / f"cookies-{_ua_hash(ua)}.txt"
            self.assertTrue(jar_path.exists(), "cookie jar file should be created after _save_jar")
            content = jar_path.read_text()
            self.assertIn("TGTSID", content)

            c2 = HttpClient(data_dir)
            jar2 = c2._get_jar(ua)
            cookie_names = {ck.name for ck in jar2}
            self.assertIn("TGTSID", cookie_names)


class TestBackoffStateMachine(unittest.TestCase):
    def test_429_sequence_grows_then_caps_and_200_resets(self):
        ua = USER_AGENTS[0]
        host = "redsky.target.com"
        url = f"https://{host}/path"

        fixed_now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)

        def now_func():
            return fixed_now

        with tempfile.TemporaryDirectory() as td:
            client = HttpClient(td, now_func=now_func)

            def fake_open_429(req, timeout):
                # urlopen raises HTTPError on 4xx/5xx
                raise urllib.error.HTTPError(
                    url=req.full_url,
                    code=429,
                    msg="Too Many Requests",
                    hdrs=None,
                    fp=io.BytesIO(b""),
                )

            with _patch_open(fake_open_429):
                expected = [60, 120, 240, 480, 960, 1800]
                actual = []
                for _ in range(6):
                    client.request(url, ua=ua)
                    actual.append(int((client.next_allowed_at(host, ua) - fixed_now).total_seconds()))
                self.assertEqual(actual, expected)

                # 7th 429 still capped
                client.request(url, ua=ua)
                self.assertEqual(
                    int((client.next_allowed_at(host, ua) - fixed_now).total_seconds()),
                    1800,
                )

            def fake_open_200(req, timeout):
                return _FakeRespCM(status=200, headers={}, body=b"ok", url=req.full_url)

            with _patch_open(fake_open_200):
                client.request(url, ua=ua)
                # Reset means next_allowed_at == now (no backoff state)
                self.assertEqual(client.next_allowed_at(host, ua), fixed_now)


class TestRedirectWithoutLocation(unittest.TestCase):
    def test_302_without_location_raises_missing_location(self):
        ua = USER_AGENTS[0]
        url = "https://www.target.com/p/foo/-/A-12345"

        with tempfile.TemporaryDirectory() as td:
            client = HttpClient(td)

            def fake_open_302(req, timeout):
                return _FakeRespCM(
                    status=302,
                    headers={"content-type": "text/html"},
                    body=b"",
                    url=req.full_url,
                )

            with _patch_open(fake_open_302):
                with self.assertRaises(_MissingLocationError) as cm:
                    client.request(url, ua=ua)
                snap = snapshot_from_missing_location(cm.exception)
                self.assertEqual(snap.http_status, 302)
                self.assertFalse(snap.in_stock)
                self.assertIsNotNone(snap.error)
                self.assertIn("Location", snap.error)


class TestGzipDecode(unittest.TestCase):
    def test_gzip_content_encoding_is_transparently_decoded(self):
        import gzip
        ua = USER_AGENTS[0]
        payload = b'{"hello":"world"}'
        compressed = gzip.compress(payload)

        with tempfile.TemporaryDirectory() as td:
            client = HttpClient(td)

            def fake_open(req, timeout):
                return _FakeRespCM(
                    status=200,
                    headers={"content-encoding": "gzip", "content-type": "application/json"},
                    body=compressed,
                    url=req.full_url,
                )

            with _patch_open(fake_open):
                resp = client.request("https://redsky.target.com/x", ua=ua)
                self.assertEqual(resp.body, payload)


if __name__ == "__main__":
    unittest.main()
