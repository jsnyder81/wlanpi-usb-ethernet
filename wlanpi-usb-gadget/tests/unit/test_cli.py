"""Unit tests for CLI argument parsing and non-privileged commands."""

from __future__ import annotations

import unittest

from wlanpi_usb_gadget.__main__ import main


class TestCli(unittest.TestCase):
    def test_config_check(self) -> None:
        rc = main(["config", "check"])
        self.assertEqual(rc, 0)

    def test_profile_list(self) -> None:
        rc = main(["profile", "list"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
