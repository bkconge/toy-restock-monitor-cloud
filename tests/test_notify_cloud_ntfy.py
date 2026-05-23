import io
import unittest
import unittest.mock

from src.notify.cloud_ntfy import CloudNtfyNotifier


class _FakeResp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def getcode(self):
        return 200


class CloudNtfyPrefixTests(unittest.TestCase):
    def test_prepends_cloud_prefix(self) -> None:
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = req.data.decode("utf-8")
            return _FakeResp()

        with unittest.mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            n = CloudNtfyNotifier("https://ntfy.sh/test")
            result = n.send("hello world")
        self.assertTrue(result.success)
        self.assertEqual(captured["body"], "[Cloud] hello world")

    def test_prefix_added_even_when_text_already_starts_with_cloud(self) -> None:
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = req.data.decode("utf-8")
            return _FakeResp()

        with unittest.mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            n = CloudNtfyNotifier("https://ntfy.sh/test")
            n.send("[Cloud] already prefixed")
        self.assertEqual(captured["body"], "[Cloud] [Cloud] already prefixed")


if __name__ == "__main__":
    unittest.main()
