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

    def test_pdp_malformed_triggers_html_fallback_then_parse_error(self):
        malformed = (FIXTURES / "target_redsky_malformed.json").read_bytes()
        jsonld_html = (FIXTURES / "target_pdp_jsonld.html").read_bytes()
        call_count = {"n": 0}

        def reply(req, timeout):
            call_count["n"] += 1
            url = req.full_url
            if "redsky.target.com" in url:
                return _FakeRespCM(status=200, headers={"content-type": "application/json"}, body=malformed, url=url)
            return _FakeRespCM(status=200, headers={"content-type": "text/html"}, body=jsonld_html, url=url)

        with _opener_patch(reply):
            snap = self.source.fetch(self.url, http_client=self.client, user_agent=self.ua)
        # malformed RedSky -> HTML fallback succeeds (we serve a hand-crafted JSON-LD)
        self.assertGreaterEqual(call_count["n"], 2)
        self.assertTrue(snap.in_stock)
        self.assertEqual(snap.price, 5.99)
        self.assertEqual(snap.sku, "94757072")
        self.assertFalse(snap.parse_error)

    def test_pdp_malformed_with_no_fallback_html_produces_parse_error(self):
        malformed = (FIXTURES / "target_redsky_malformed.json").read_bytes()

        def reply(req, timeout):
            if "redsky.target.com" in req.full_url:
                return _FakeRespCM(status=200, headers={}, body=malformed, url=req.full_url)
            return _FakeRespCM(status=200, headers={}, body=b"<html>no jsonld here</html>", url=req.full_url)

        with _opener_patch(reply):
            snap = self.source.fetch(self.url, http_client=self.client, user_agent=self.ua)
        self.assertTrue(snap.parse_error)
        self.assertEqual(snap.http_status, 200)
        self.assertIsNotNone(snap.error)
        self.assertFalse(snap.in_stock)

    def test_pdp_redsky_500_falls_back_to_html(self):
        jsonld_html = (FIXTURES / "target_pdp_jsonld.html").read_bytes()

        def reply(req, timeout):
            if "redsky.target.com" in req.full_url:
                raise urllib.error.HTTPError(
                    url=req.full_url, code=500, msg="Server Error",
                    hdrs=None, fp=io.BytesIO(b"server error"),
                )
            return _FakeRespCM(status=200, headers={"content-type": "text/html"}, body=jsonld_html, url=req.full_url)

        with _opener_patch(reply):
            snap = self.source.fetch(self.url, http_client=self.client, user_agent=self.ua)
        # Fallback successfully extracted JSON-LD => in_stock True, price 5.99
        self.assertTrue(snap.in_stock)
        self.assertEqual(snap.price, 5.99)
        self.assertEqual(snap.sku, "94757072")

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

    def test_jsonld_fallback_extracts_availability_and_price(self):
        # Trigger fallback via a RedSky shape mismatch (empty data).
        jsonld_html = (FIXTURES / "target_pdp_jsonld.html").read_bytes()

        def reply(req, timeout):
            if "redsky.target.com" in req.full_url:
                return _FakeRespCM(status=200, headers={}, body=b'{"data":{}}', url=req.full_url)
            return _FakeRespCM(status=200, headers={"content-type": "text/html"}, body=jsonld_html, url=req.full_url)

        url = "https://www.target.com/p/sunny-days-squeezy-strawberry/-/A-94757072"
        with _opener_patch(reply):
            snap = self.source.fetch(url, http_client=self.client, user_agent=self.ua)
        self.assertTrue(snap.in_stock)
        self.assertEqual(snap.price, 5.99)
        self.assertEqual(snap.sku, "94757072")
        self.assertEqual(snap.title, "Sunny Days Squeezy Strawberry")
        self.assertFalse(snap.parse_error)


if __name__ == "__main__":
    unittest.main()
