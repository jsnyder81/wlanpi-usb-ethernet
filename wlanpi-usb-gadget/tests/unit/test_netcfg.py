"""Unit tests for systemd-networkd file generation."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wlanpi_usb_gadget.config import validate_config
from wlanpi_usb_gadget.netcfg import (
    enslave_and_up_bridge,
    remove_networkd_files,
    render_networkd_files,
    write_networkd_files,
)
from wlanpi_usb_gadget.profiles import get_profile


class TestNetcfg(unittest.TestCase):
    def test_render_legacy_with_fallback(self) -> None:
        cfg = validate_config({})
        files = render_networkd_files(cfg, get_profile("legacy"), "02:de:ad:be:ef:01")
        # No .netdev file emitted: avoids networkd EEXIST creation race.
        self.assertNotIn("50-wlanpi-usb-gadget-bridge.netdev", files)
        self.assertIn("50-wlanpi-usb-gadget-bridge.network", files)
        self.assertIn("50-wlanpi-usb-gadget-member-usbrndis0.network", files)
        self.assertIn("50-wlanpi-usb-gadget-member-usbecm0.network", files)
        self.assertIn("50-wlanpi-usb-gadget.link", files)

        ecm_net = files["50-wlanpi-usb-gadget-member-usbecm0.network"]
        self.assertNotIn("Bridge=", ecm_net)
        self.assertIn("ConfigureWithoutCarrier=yes", ecm_net)
        self.assertIn("LinkLocalAddressing=no", ecm_net)
        self.assertIn("IPv6AcceptRA=no", ecm_net)

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

    def test_enslave_and_up_bridge_order(self) -> None:
        calls: list[list[str]] = []

        def _fake_run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch("wlanpi_usb_gadget.netcfg.subprocess.run", side_effect=_fake_run):
            enslave_and_up_bridge("usb0", ["usbecm0", "usbrndis0", "usb0", ""])

        self.assertEqual(
            calls,
            [
                ["ip", "link", "set", "dev", "usbecm0", "master", "usb0", "up"],
                ["ip", "link", "set", "dev", "usbrndis0", "master", "usb0", "up"],
                ["ip", "link", "set", "dev", "usb0", "up"],
            ],
        )


if __name__ == "__main__":
    unittest.main()
