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
    ensure_bridge_exists,
    maybe_restart_avahi_on_ifindex_change,
    read_iface_ifindex,
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
        self.assertIn("50-wlanpi-usb-gadget-bridge.link", files)
        br_link = files["50-wlanpi-usb-gadget-bridge.link"]
        self.assertIn("Kind=bridge", br_link)
        self.assertIn("MACAddressPolicy=none", br_link)
        self.assertIn("MACAddress=02:de:ad:be:ef:01", br_link)

        for member in (
            "50-wlanpi-usb-gadget-member-usbecm0.network",
            "50-wlanpi-usb-gadget-member-usbrndis0.network",
        ):
            member_net = files[member]
            self.assertNotIn("Bridge=", member_net)
            self.assertIn("KeepMaster=yes", member_net)
            self.assertIn("ConfigureWithoutCarrier=yes", member_net)
            self.assertIn("LinkLocalAddressing=no", member_net)
            self.assertIn("IPv6AcceptRA=no", member_net)

        br_net = files["50-wlanpi-usb-gadget-bridge.network"]
        self.assertIn("Kind=bridge", br_net)
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

    def test_ensure_bridge_exists_pins_deterministic_mac_on_existing_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sys_net = Path(tmp)
            br_dir = sys_net / "usb0"
            (br_dir / "bridge").mkdir(parents=True)
            (br_dir / "address").write_text("0e:35:4e:d2:55:17\n", encoding="utf-8")
            calls: list[list[str]] = []

            def _fake_run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
                calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with mock.patch("wlanpi_usb_gadget.netcfg.subprocess.run", side_effect=_fake_run):
                ensure_bridge_exists("usb0", "02:de:ad:be:ef:01", sys_class_net=sys_net)

            self.assertEqual(calls, [["ip", "link", "set", "dev", "usb0", "address", "02:de:ad:be:ef:01"]])

    def test_avahi_restart_on_ifindex_change_and_nonfatal_guards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sys_net = Path(tmp)
            br_dir = sys_net / "usb0"
            br_dir.mkdir(parents=True)
            (br_dir / "ifindex").write_text("12\n", encoding="utf-8")
            self.assertEqual(read_iface_ifindex("usb0", sys_class_net=sys_net), 12)

            # Unchanged ifindex: no systemctl calls
            with mock.patch("wlanpi_usb_gadget.netcfg.subprocess.run") as sub_run:
                self.assertFalse(maybe_restart_avahi_on_ifindex_change("usb0", 12, sys_class_net=sys_net))
            sub_run.assert_not_called()

            # Changed ifindex + avahi disabled: is-enabled checked, try-restart skipped
            with mock.patch(
                "wlanpi_usb_gadget.netcfg.subprocess.run",
                return_value=subprocess.CompletedProcess(["systemctl"], 1, "", ""),
            ) as sub_run:
                self.assertFalse(maybe_restart_avahi_on_ifindex_change("usb0", 7, sys_class_net=sys_net))
            sub_run.assert_called_once_with(
                ["systemctl", "is-enabled", "--quiet", "avahi-daemon"],
                capture_output=True,
                text=True,
                check=False,
            )

            # Changed ifindex + avahi enabled: try-restart called + event recorded
            calls: list[list[str]] = []

            def _fake_run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
                calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with (
                mock.patch("wlanpi_usb_gadget.netcfg.subprocess.run", side_effect=_fake_run),
                mock.patch("wlanpi_usb_gadget.netcfg.record_event") as rec_ev,
                mock.patch("wlanpi_usb_gadget.netcfg.log_journal"),
            ):
                self.assertTrue(maybe_restart_avahi_on_ifindex_change("usb0", 7, sys_class_net=sys_net, profile="ncm"))
            self.assertEqual(
                calls,
                [
                    ["systemctl", "is-enabled", "--quiet", "avahi-daemon"],
                    ["systemctl", "try-restart", "avahi-daemon"],
                ],
            )
            rec_ev.assert_called_once_with("avahi_restart", profile="ncm", detail="bridge=usb0 ifindex 7->12 rc=0")

            # Missing systemctl binary: never fatal
            with (
                mock.patch("wlanpi_usb_gadget.netcfg.subprocess.run", side_effect=FileNotFoundError("systemctl")),
                mock.patch("wlanpi_usb_gadget.netcfg.log_journal"),
            ):
                self.assertFalse(maybe_restart_avahi_on_ifindex_change("usb0", 7, sys_class_net=sys_net))


if __name__ == "__main__":
    unittest.main()
