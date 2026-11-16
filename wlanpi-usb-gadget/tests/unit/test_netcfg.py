"""Unit tests for systemd-networkd file generation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wlanpi_usb_gadget.config import validate_config
from wlanpi_usb_gadget.netcfg import remove_networkd_files, render_networkd_files, write_networkd_files
from wlanpi_usb_gadget.profiles import get_profile


class TestNetcfg(unittest.TestCase):
    def test_render_legacy_with_fallback(self) -> None:
        cfg = validate_config({})
        files = render_networkd_files(cfg, get_profile("legacy"), "02:de:ad:be:ef:01")
        self.assertIn("50-wlanpi-usb-gadget-bridge.netdev", files)
        self.assertIn("50-wlanpi-usb-gadget-bridge.network", files)
        self.assertIn("50-wlanpi-usb-gadget-member-usbrndis0.network", files)
        self.assertIn("50-wlanpi-usb-gadget-member-usbecm0.network", files)
        self.assertIn("50-wlanpi-usb-gadget.link", files)

        br_net = files["50-wlanpi-usb-gadget-bridge.network"]
        self.assertIn("Address=198.18.42.1/24", br_net)
        self.assertIn("Address=169.254.42.1/16", br_net)
        self.assertIn("Address=fe80::1/64", br_net)
        self.assertIn("DHCPServer=yes", br_net)
        self.assertIn("EmitDNS=no", br_net)
        self.assertIn("EmitRouter=no", br_net)
        self.assertNotIn("KeepConfiguration=", br_net)
        self.assertNotIn("IPForward=", br_net)

    def test_render_without_fallback(self) -> None:
        cfg = validate_config({"FALLBACK_LINKLOCAL": "no"})
        files = render_networkd_files(cfg, get_profile("ncm"), "02:de:ad:be:ef:01")
        br_net = files["50-wlanpi-usb-gadget-bridge.network"]
        self.assertNotIn("169.254.42.1", br_net)
        self.assertNotIn("fe80::1", br_net)

    def test_write_prunes_stale_and_remove_cleans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            net_dir = Path(tmp)
            cfg = validate_config({})
            write_networkd_files(cfg, get_profile("legacy"), "02:de:ad:be:ef:01", network_dir=net_dir)
            self.assertTrue((net_dir / "50-wlanpi-usb-gadget-member-usbrndis0.network").exists())

            write_networkd_files(cfg, get_profile("ncm"), "02:de:ad:be:ef:01", network_dir=net_dir)
            self.assertFalse((net_dir / "50-wlanpi-usb-gadget-member-usbrndis0.network").exists())
            self.assertTrue((net_dir / "50-wlanpi-usb-gadget-member-usbncm0.network").exists())

            removed = remove_networkd_files(network_dir=net_dir)
            self.assertGreaterEqual(removed, 3)
            self.assertEqual(list(net_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
