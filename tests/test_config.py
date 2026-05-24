import os
import pathlib
import tempfile
import textwrap
import unittest
import unittest.mock

from src.config import Config, ConfigError, Watch, _resolve_env, load


VALID_YAML = textwrap.dedent("""
    notifier: ntfy
    ntfy_topic_url: "https://ntfy.sh/brian-restocks-abc123"
    redsky_key: "abcdef0123456789abcdef0123456789abcdef01"
    poll_interval_seconds: 180
    cooldown_minutes: 60
    parse_fail_threshold: 3
    http_read_timeout_seconds: 30
    tick_wall_clock_cap_seconds: 120
    log_level: INFO
    per_source_overrides:
      "https://www.target.com/p/sunny-days-squeezy-cheese-block/-/A-1003785284":
        poll_interval_seconds: 60
    watches:
      - id: neeoh-any
        name: "Nee Doh — any variant"
        msrp_cap: 6.99
        sources:
          - https://www.target.com/s?searchTerm=nee+doh
      - id: squeeezy-strawberry
        name: "Squeeezy Strawberry"
        msrp_cap: 6.99
        sources:
          - https://www.target.com/p/sunny-days-squeezy-strawberry/-/A-94757072
""").strip()


def _write_tmp(text: str) -> pathlib.Path:
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    )
    tmp.write(text)
    tmp.close()
    return pathlib.Path(tmp.name)


class ConfigLoadHappyPathTests(unittest.TestCase):
    def test_load_valid_config(self) -> None:
        path = _write_tmp(VALID_YAML)
        cfg = load(path)
        self.assertIsInstance(cfg, Config)
        self.assertEqual(cfg.notifier, "ntfy")
        self.assertEqual(cfg.ntfy_topic_url, "https://ntfy.sh/brian-restocks-abc123")
        self.assertEqual(cfg.redsky_key, "abcdef0123456789abcdef0123456789abcdef01")
        self.assertEqual(cfg.poll_interval_seconds, 180)
        self.assertEqual(cfg.cooldown_minutes, 60)
        self.assertEqual(cfg.parse_fail_threshold, 3)
        self.assertEqual(cfg.log_level, "INFO")
        self.assertEqual(len(cfg.watches), 2)
        self.assertIsInstance(cfg.watches[0], Watch)
        self.assertEqual(cfg.watches[0].id, "neeoh-any")
        self.assertEqual(cfg.watches[0].msrp_cap, 6.99)
        self.assertEqual(
            cfg.watches[0].sources[0],
            "https://www.target.com/s?searchTerm=nee+doh",
        )
        self.assertIn(
            "https://www.target.com/p/sunny-days-squeezy-cheese-block/-/A-1003785284",
            cfg.per_source_overrides,
        )

    def test_watch_is_frozen(self) -> None:
        path = _write_tmp(VALID_YAML)
        cfg = load(path)
        with self.assertRaises(Exception):
            cfg.watches[0].msrp_cap = 9.99  # type: ignore[misc]

    def test_config_is_frozen(self) -> None:
        path = _write_tmp(VALID_YAML)
        cfg = load(path)
        with self.assertRaises(Exception):
            cfg.notifier = "ntfy"  # type: ignore[misc]


class ConfigLoadErrorTests(unittest.TestCase):
    def test_missing_file(self) -> None:
        missing = pathlib.Path(tempfile.gettempdir()) / "definitely-not-there-xyz.yaml"
        if missing.exists():
            missing.unlink()
        with self.assertRaises(ConfigError) as ctx:
            load(missing)
        self.assertIn(str(missing), str(ctx.exception))

    def test_missing_top_level_key(self) -> None:
        bad = VALID_YAML.replace("cooldown_minutes: 60\n", "")
        path = _write_tmp(bad)
        with self.assertRaises(ConfigError) as ctx:
            load(path)
        self.assertIn("cooldown_minutes", str(ctx.exception))

    def test_malformed_yaml(self) -> None:
        path = _write_tmp("notifier: ntfy\n  ntfy_topic_url: oops\n:::::\n")
        with self.assertRaises(ConfigError) as ctx:
            load(path)
        self.assertIn("malformed", str(ctx.exception).lower())

    def test_msrp_cap_wrong_type(self) -> None:
        bad = VALID_YAML.replace("msrp_cap: 6.99", "msrp_cap: \"six\"")
        path = _write_tmp(bad)
        with self.assertRaises(ConfigError) as ctx:
            load(path)
        self.assertIn("msrp_cap", str(ctx.exception))

    def test_poll_interval_wrong_type(self) -> None:
        bad = VALID_YAML.replace("poll_interval_seconds: 180", "poll_interval_seconds: \"slow\"")
        path = _write_tmp(bad)
        with self.assertRaises(ConfigError) as ctx:
            load(path)
        self.assertIn("poll_interval_seconds", str(ctx.exception))

    def test_msrp_cap_bool_rejected(self) -> None:
        bad = VALID_YAML.replace("msrp_cap: 6.99", "msrp_cap: true")
        path = _write_tmp(bad)
        with self.assertRaises(ConfigError) as ctx:
            load(path)
        self.assertIn("msrp_cap", str(ctx.exception))

    def test_watch_missing_required_key(self) -> None:
        # drop the `id:` line entirely; remaining watch is still valid YAML
        # but missing a required key.
        bad = VALID_YAML.replace("  - id: neeoh-any\n    name:", "  - name:")
        path = _write_tmp(bad)
        with self.assertRaises(ConfigError) as ctx:
            load(path)
        self.assertIn("id", str(ctx.exception))

    def test_unknown_notifier_value(self) -> None:
        bad = VALID_YAML.replace("notifier: ntfy", "notifier: telegram")
        path = _write_tmp(bad)
        with self.assertRaises(ConfigError) as ctx:
            load(path)
        self.assertIn("notifier", str(ctx.exception))

    def test_imessage_modes_rejected(self) -> None:
        for mode in ("imessage", "imessage_mirror_ntfy"):
            bad = VALID_YAML.replace("notifier: ntfy", f"notifier: {mode}")
            path = _write_tmp(bad)
            with self.assertRaises(ConfigError) as ctx:
                load(path)
            self.assertIn("notifier", str(ctx.exception))

    def test_empty_sources_list(self) -> None:
        bad = VALID_YAML.replace(
            "    sources:\n      - https://www.target.com/s?searchTerm=nee+doh\n",
            "    sources: []\n",
        )
        path = _write_tmp(bad)
        with self.assertRaises(ConfigError) as ctx:
            load(path)
        self.assertIn("sources", str(ctx.exception))


class ResolveEnvTests(unittest.TestCase):
    def test_happy_path_substitution(self) -> None:
        with unittest.mock.patch.dict(
            os.environ, {"NTFY_TOPIC_URL": "https://ntfy.sh/resolved-xyz"}, clear=False
        ):
            yaml_text = VALID_YAML.replace(
                'ntfy_topic_url: "https://ntfy.sh/brian-restocks-abc123"',
                "ntfy_topic_url: env:NTFY_TOPIC_URL",
            )
            path = _write_tmp(yaml_text)
            cfg = load(path)
            self.assertEqual(cfg.ntfy_topic_url, "https://ntfy.sh/resolved-xyz")

    def test_missing_env_var_raises_config_error_naming_var(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "DEFINITELY_MISSING_XYZ"}
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            yaml_text = VALID_YAML.replace(
                'ntfy_topic_url: "https://ntfy.sh/brian-restocks-abc123"',
                "ntfy_topic_url: env:DEFINITELY_MISSING_XYZ",
            )
            path = _write_tmp(yaml_text)
            with self.assertRaises(ConfigError) as ctx:
                load(path)
            self.assertIn("DEFINITELY_MISSING_XYZ", str(ctx.exception))

    def test_env_prefix_with_empty_name_raises(self) -> None:
        with self.assertRaises(ConfigError):
            _resolve_env("env:")

    def test_env_var_set_to_empty_string_raises(self) -> None:
        # GitHub Actions substitutes ${{ secrets.MISSING }} with "" rather
        # than leaving the var unset; treat empty as missing to fail fast.
        with unittest.mock.patch.dict(os.environ, {"EMPTY_SECRET": ""}, clear=True):
            with self.assertRaises(ConfigError) as ctx:
                _resolve_env("env:EMPTY_SECRET")
            self.assertIn("EMPTY_SECRET", str(ctx.exception))

    def test_non_env_string_passes_through(self) -> None:
        self.assertEqual(_resolve_env("plain-value"), "plain-value")
        self.assertEqual(_resolve_env("https://example.com"), "https://example.com")


if __name__ == "__main__":
    unittest.main()
