"""Unit tests for configuration parsing and validation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wlanpi_usb_gadget.config import (
    ConfigError,
    GadgetConfig,
    load_config,
    parse_env_text,
    validate_config,
)


class TestConfig(unittest.TestCase):
    def test_defaults(self) -> None:
        cfg = validate_config({})
        self.assertEqual(cfg.profile, "legacy")
        self.assertEqual(cfg.safe_profile, "legacy")
        self.assertEqual(cfg.address, "198.18.42.1/24")
        self.assertEqual(cfg.bridge_name, "usb0")
        self.assertTrue(cfg.fallback_linklocal)
        self.assertTrue(cfg.serial_console)
        self.assertTrue(cfg.status_page)
        self.assertFalse(cfg.monitor_recovery)
        self.assertEqual(cfg.reset_budget, 3)
        self.assertEqual(cfg.reset_window_sec, 600)
        self.assertEqual(cfg.trial_timeout_sec, 300)
        self.assertEqual(cfg.log_level, "info")
        self.assertEqual(cfg.warnings, ())

    def test_parse_env_text_comments_and_quotes(self) -> None:
        raw = parse_env_text(
            """
            # comment
            PROFILE="ncm"
            ADDRESS='198.18.44.1/24'
            MONITOR_RECOVERY=on
            RESET_BUDGET=5
            """
        )
        cfg = validate_config(raw)
        self.assertEqual(cfg.profile, "ncm")
        self.assertEqual(cfg.address, "198.18.44.1/24")
        self.assertTrue(cfg.monitor_recovery)
        self.assertEqual(cfg.reset_budget, 5)

    def test_bt_pan_overlap_warning(self) -> None:
        cfg = validate_config({"ADDRESS": "198.18.43.1/24"})
        self.assertTrue(any("198.18.43.0/24" in w for w in cfg.warnings))

    def test_invalid_profile(self) -> None:
        with self.assertRaises(ConfigError):
            validate_config({"PROFILE": "bogus"})

    def test_invalid_address(self) -> None:
        with self.assertRaises(ConfigError):
            validate_config({"ADDRESS": "198.18.42.0/24"})
        with self.assertRaises(ConfigError):
            validate_config({"ADDRESS": "198.18.42.1/31"})

    def test_load_missing_file_returns_defaults(self) -> None:
        cfg = load_config(Path("/nonexistent/path/config"))
        self.assertEqual(cfg, GadgetConfig())

    def test_load_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config"
            p.write_text("PROFILE=ncm-ecm\nFALLBACK_LINKLOCAL=no\n", encoding="utf-8")
            cfg = load_config(p)
            self.assertEqual(cfg.profile, "ncm-ecm")
            self.assertFalse(cfg.fallback_linklocal)


if __name__ == "__main__":
    unittest.main()
