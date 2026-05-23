import io
import socket
import unittest
import urllib.error
from unittest.mock import patch, MagicMock

from src.notify.ntfy import NtfyNotifier


def _fake_resp(status=200):
    m = MagicMock()
    m.status = status
    m.getcode.return_value = status
    m.__enter__.return_value = m
    m.__exit__.return_value = False
    return m


class TestNtfySend(unittest.TestCase):
    def test_200_is_success(self):
        with patch("src.notify.ntfy.urllib.request.urlopen", return_value=_fake_resp(200)):
            n = NtfyNotifier("https://ntfy.sh/test-topic")
            r = n.send("hello")
        self.assertTrue(r.success)
        self.assertIsNone(r.error)
        self.assertEqual(r.channel, "ntfy")

    def test_http_error_404(self):
        err = urllib.error.HTTPError(
            url="https://ntfy.sh/x", code=404, msg="Not Found",
            hdrs=None, fp=io.BytesIO(b""),
        )
        with patch("src.notify.ntfy.urllib.request.urlopen", side_effect=err):
            r = NtfyNotifier("https://ntfy.sh/x").send("hello")
        self.assertFalse(r.success)
        self.assertIn("HTTPError", r.error)
        self.assertIn("404", r.error)

    def test_url_error(self):
        with patch(
            "src.notify.ntfy.urllib.request.urlopen",
            side_effect=urllib.error.URLError("name resolution failed"),
        ):
            r = NtfyNotifier("https://ntfy.sh/x").send("hello")
        self.assertFalse(r.success)
        self.assertIn("URLError", r.error)

    def test_socket_timeout(self):
        with patch(
            "src.notify.ntfy.urllib.request.urlopen",
            side_effect=socket.timeout("timed out"),
        ):
            r = NtfyNotifier("https://ntfy.sh/x").send("hello")
        self.assertFalse(r.success)
        self.assertIn("timeout", r.error.lower())

    def test_request_body_and_content_type(self):
        captured = {}

        def _capture(req, timeout=None):
            captured["data"] = req.data
            captured["headers"] = dict(req.header_items())
            captured["method"] = req.get_method()
            captured["url"] = req.full_url
            return _fake_resp(200)

        with patch("src.notify.ntfy.urllib.request.urlopen", side_effect=_capture):
            NtfyNotifier("https://ntfy.sh/topic-abc").send("hi there")

        self.assertEqual(captured["data"], "hi there".encode("utf-8"))
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["url"], "https://ntfy.sh/topic-abc")
        # urllib title-cases header names; check case-insensitively.
        ct = {k.lower(): v for k, v in captured["headers"].items()}.get("content-type")
        self.assertEqual(ct, "text/plain; charset=utf-8")


if __name__ == "__main__":
    unittest.main()
