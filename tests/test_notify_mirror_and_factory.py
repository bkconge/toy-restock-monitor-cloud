import unittest

from src.config import Config, ConfigError, Watch
from src.notify.cloud_ntfy import CloudNtfyNotifier
from src.notify.factory import build_notifier


def _cfg(notifier_mode: str) -> Config:
    return Config(
        notifier=notifier_mode,
        ntfy_topic_url="https://ntfy.sh/brian-restocks-test",
        redsky_key="abcdef0123456789abcdef0123456789abcdef01",
        poll_interval_seconds=180,
        cooldown_minutes=60,
        parse_fail_threshold=3,
        http_read_timeout_seconds=30,
        tick_wall_clock_cap_seconds=120,
        log_level="INFO",
        watches=(
            Watch(id="x", name="x", msrp_cap=6.99, sources=("https://example.com",)),
        ),
        per_source_overrides={},
    )


class TestFactory(unittest.TestCase):
    def test_ntfy_returns_cloud_ntfy(self) -> None:
        n = build_notifier(_cfg("ntfy"))
        self.assertIsInstance(n, CloudNtfyNotifier)
        self.assertEqual(n.topic_url, "https://ntfy.sh/brian-restocks-test")

    def test_unknown_mode_raises_config_error(self) -> None:
        bad_cfg = _cfg("ntfy")
        object.__setattr__(bad_cfg, "notifier", "bogus_mode")
        with self.assertRaises(ConfigError) as ctx:
            build_notifier(bad_cfg)
        self.assertIn("bogus_mode", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
