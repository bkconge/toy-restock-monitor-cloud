"""TargetSource tests — RedSky parsing, fallback paths, 429 backoff.

All HTTP mocked. Fixtures under tests/fixtures/ were captured via
scripts/_capture_target_fixtures.py from live Target endpoints on the
build day (Squeeezy items were OOS at capture time; the in-stock fixture
was synthesized by flipping availability and injecting a price block).
"""

from __future__ import annotations

import io
import json
import pathlib
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from src.sources.base import HttpClient, USER_AGENTS, _ua_hash
from src.sources.target import TargetSource


FIXTURES = pathlib.Path(__file__).parent / "fixtures"


class _FakeRespCM:
    def __init__(self, status, headers, body, url=""):
        self.status = status
        self._headers = headers
        self._body = body
        self.url = url
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def getheaders(self): return list(self._headers.items())
    def read(self): return self._body


def _opener_patch(reply_func):
    return patch(
        "urllib.request.OpenerDirector.open",
        new=lambda self, req, timeout=None: reply_func(req, timeout),
    )


def _stamp_warmups(td: pathlib.Path) -> None:
    for ua in USER_AGENTS:
        (td / f"cookies-{_ua_hash(ua)}.warmup").touch()


class TestPDPParsing(unittest.TestCase):
    def setUp(self):
        self.td_ctx = tempfile.TemporaryDirectory()
        self.td = pathlib.Path(self.td_ctx.name)
        _stamp_warmups(self.td)
        self.client = HttpClient(self.td)
        self.ua = USER_AGENTS[0]
        self.source = TargetSource()
        self.url = "https://www.target.com/p/sunny-days-squeezy-strawberry/-/A-94757072"

    def tearDown(self):
        self.td_ctx.cleanup()

    def _serve_body(self, body: bytes, *, status: int = 200):
        def reply(req, timeout):
            return _FakeRespCM(status=status, headers={"content-type": "application/json"}, body=body, url=req.full_url)
        return _opener_patch(reply)

    def test_pdp_in_stock_fixture(self):
        body = (FIXTURES / "target_redsky_in_stock.json").read_bytes()
        with self._serve_body(body):
            snap = self.source.fetch(self.url, http_client=self.client, user_agent=self.ua)
        self.assertTrue(snap.in_stock)
        self.assertEqual(snap.price, 5.99)
        self.assertEqual(snap.sku, "94757072")
        self.assertFalse(snap.parse_error)
        self.assertEqual(snap.http_status, 200)
        self.assertIsNone(snap.error)

    def test_pdp_oos_fixture(self):
        body = (FIXTURES / "target_redsky_oos.json").read_bytes()
        with self._serve_body(body):
            snap = self.source.fetch(self.url, http_client=self.client, user_agent=self.ua)
        self.assertFalse(snap.in_stock)
        self.assertEqual(snap.sku, "94757072")
        self.assertFalse(snap.parse_error)
        self.assertEqual(snap.http_status, 200)

    def test_pdp_captcha_does_not_count_as_parse_error(self):
        # Target's RedSky returns 200 with a captcha JSON when bot detection
        # fires. The fix: detect this and record it WITHOUT setting parse_error,
        # so the orchestrator's parse_fail counter doesn't increment and no
        # parser_broken alert spam happens during sustained captcha periods.
        captcha_body = b'{"captchaRelativeURL":"/captcha?trackingId=abc","captchaAbsoluteURL":"https://redsky.target.com/captcha?trackingId=abc"}'
        with self._serve_body(captcha_body):
            snap = self.source.fetch(self.url, http_client=self.client, user_agent=self.ua)
        self.assertFalse(snap.in_stock)
        self.assertEqual(snap.sku, "94757072")
        self.assertFalse(snap.parse_error, "captcha must NOT count as parse_error (orchestrator would alert)")
        self.assertEqual(snap.http_status, 200)
        self.assertIsNotNone(snap.error)
        self.assertIn("captcha", snap.error.lower())

    def test_pdp_malformed_records_blocked_snapshot_no_parse_error(self):
        # Post-2026-05 fix: malformed/non-JSON RedSky response (typical of
        # Akamai HTML challenges served at 200) records a 'blocked' snapshot
        # with parse_error=False so the parser_broken alert path stays quiet
        # during sustained bot detection.
        malformed = (FIXTURES / "target_redsky_malformed.json").read_bytes()

        def reply(req, timeout):
            if "redsky.target.com" in req.full_url:
                return _FakeRespCM(status=200, headers={}, body=malformed, url=req.full_url)
            raise AssertionError("HTML fallback should NOT be called anymore")

        with _opener_patch(reply):
            snap = self.source.fetch(self.url, http_client=self.client, user_agent=self.ua)
        self.assertFalse(snap.parse_error)
        self.assertEqual(snap.http_status, 200)
        self.assertIsNotNone(snap.error)
        self.assertIn("non-JSON", snap.error)
        self.assertFalse(snap.in_stock)

    def test_pdp_redsky_500_records_blocked_no_parse_error(self):
        # Post-2026-05 fix: RedSky non-200 (500, 403, 429-after-backoff)
        # records a blocked snapshot WITHOUT triggering HTML fallback (which
        # always parse-errors against modern Target PDPs that don't ship
        # JSON-LD blocks).
        def reply(req, timeout):
            if "redsky.target.com" in req.full_url:
                raise urllib.error.HTTPError(
                    url=req.full_url, code=500, msg="Server Error",
                    hdrs=None, fp=io.BytesIO(b"server error"),
                )
            raise AssertionError("HTML fallback should NOT be called anymore")

        with _opener_patch(reply):
            snap = self.source.fetch(self.url, http_client=self.client, user_agent=self.ua)
        self.assertFalse(snap.in_stock)
        self.assertEqual(snap.http_status, 500)
        self.assertFalse(snap.parse_error)
        self.assertIsNotNone(snap.error)
        self.assertIn("upstream blocked", snap.error)

    def test_pdp_429_records_status_and_bumps_backoff(self):
        from datetime import datetime, timezone
        host = "redsky.target.com"

        body_429 = (FIXTURES / "target_429.json").read_bytes()

        def reply(req, timeout):
            if "redsky.target.com" in req.full_url:
                raise urllib.error.HTTPError(
                    url=req.full_url, code=429, msg="Too Many Requests",
                    hdrs=None, fp=io.BytesIO(body_429),
                )
            # Any subsequent HTML fallback call: 429 too
            raise urllib.error.HTTPError(
                url=req.full_url, code=429, msg="Too Many Requests",
                hdrs=None, fp=io.BytesIO(body_429),
            )

        before = self.client.next_allowed_at(host, self.ua)
        with _opener_patch(reply):
            snap = self.source.fetch(self.url, http_client=self.client, user_agent=self.ua)
        after = self.client.next_allowed_at(host, self.ua)
        self.assertEqual(snap.http_status, 429)
        self.assertFalse(snap.in_stock)
        self.assertIsNotNone(snap.error)
        self.assertGreater(after, before, "backoff should have advanced after a 429")


class TestPLPParsing(unittest.TestCase):
    def setUp(self):
        self.td_ctx = tempfile.TemporaryDirectory()
        self.td = pathlib.Path(self.td_ctx.name)
        _stamp_warmups(self.td)
        self.client = HttpClient(self.td)
        self.ua = USER_AGENTS[0]
        self.source = TargetSource()

    def tearDown(self):
        self.td_ctx.cleanup()

    def test_plp_search_returns_first_in_stock_match(self):
        body = (FIXTURES / "target_plp_search_v2.json").read_bytes()

        def reply(req, timeout):
            return _FakeRespCM(status=200, headers={}, body=body, url=req.full_url)

        url = "https://www.target.com/s?searchTerm=nee+doh"
        with _opener_patch(reply):
            snap = self.source.fetch(url, http_client=self.client, user_agent=self.ua)
        # Fixture has actual product entries; expect first one's availability
        # surfaced. PLP fixture was captured live so the result depends on
        # what Target served — assert structural fields rather than specific
        # values to remain robust to retailer-side drift.
        self.assertEqual(snap.http_status, 200)
        self.assertIsNotNone(snap.sku)
        self.assertFalse(snap.parse_error)


class TestHtmlFallbackDirect(unittest.TestCase):
    def setUp(self):
        self.td_ctx = tempfile.TemporaryDirectory()
        self.td = pathlib.Path(self.td_ctx.name)
        _stamp_warmups(self.td)
        self.client = HttpClient(self.td)
        self.ua = USER_AGENTS[0]
        self.source = TargetSource()

    def tearDown(self):
        self.td_ctx.cleanup()

    def test_redsky_empty_data_records_blocked_no_parse_error(self):
        # Post-2026-05 fix: RedSky 200 with empty data.product_summaries
        # (or any shape that fails _parse_pdp_payload) records a blocked
        # snapshot instead of triggering the always-failing HTML fallback.
        def reply(req, timeout):
            if "redsky.target.com" in req.full_url:
                return _FakeRespCM(status=200, headers={}, body=b'{"data":{}}', url=req.full_url)
            raise AssertionError("HTML fallback should NOT be called anymore")

        url = "https://www.target.com/p/sunny-days-squeezy-strawberry/-/A-94757072"
        with _opener_patch(reply):
            snap = self.source.fetch(url, http_client=self.client, user_agent=self.ua)
        self.assertFalse(snap.in_stock)
        self.assertFalse(snap.parse_error)
        self.assertEqual(snap.http_status, 200)
        self.assertIsNotNone(snap.error)
        self.assertIn("unexpected shape", snap.error)
        self.assertEqual(snap.sku, "94757072")


if __name__ == "__main__":
    unittest.main()
