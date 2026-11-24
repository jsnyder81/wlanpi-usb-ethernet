"""Unit tests for CLI argument parsing and non-privileged commands."""

from __future__ import annotations

import unittest
from unittest import mock

from wlanpi_usb_gadget import __main__ as cli
from wlanpi_usb_gadget.__main__ import main
from wlanpi_usb_gadget.config import GadgetConfig
from wlanpi_usb_gadget.configfs import ForeignGadgetError
from wlanpi_usb_gadget.mac import DerivedMacs
from wlanpi_usb_gadget.udc import ControllerInfo


class TestCli(unittest.TestCase):
    def test_config_check(self) -> None:
        rc = main(["config", "check"])
        self.assertEqual(rc, 0)

    def test_profile_list(self) -> None:
        rc = main(["profile", "list"])
        self.assertEqual(rc, 0)

    def test_apply_builds_configfs_before_dwc2_and_bridge_after_foreign_check(self) -> None:
        order: list[str] = []
        macs = DerivedMacs("02:11:22:33:44:55", "02:aa:bb:cc:dd:ee", "02:de:ad:be:ef:01", "test")

        with (
            mock.patch.object(cli, "load_config", return_value=GadgetConfig()),
            mock.patch.object(cli, "detect_controller", return_value=ControllerInfo("fe980000.usb", "peripheral", "module")),
            mock.patch.object(cli, "derive_all_macs", return_value=macs),
            mock.patch.object(cli, "read_board_serial", return_value=("10000000abcdef12", "test")),
            mock.patch.object(cli, "ensure_kernel_module", side_effect=lambda m: order.append(f"modprobe:{m}")),
            mock.patch.object(cli, "build_gadget_tree", side_effect=lambda *_a, **_kw: order.append("build_tree") or "built"),
            mock.patch.object(cli, "wait_for_udc", side_effect=lambda **_kw: order.append("wait_udc") or "fe980000.usb"),
            mock.patch.object(cli, "write_networkd_files", side_effect=lambda *_a, **_kw: order.append("write_netcfg") or []),
            mock.patch.object(cli, "bind_gadget_udc", side_effect=lambda *_a, **_kw: order.append("bind_udc") or "applied"),
            mock.patch.object(cli, "ensure_bridge_exists", side_effect=lambda *_a, **_kw: order.append("ensure_bridge")),
            mock.patch.object(cli, "discover_member_ifnames", return_value=["usbecm0", "usbrndis0"]),
            mock.patch.object(cli, "enslave_and_up_bridge", side_effect=lambda *_a, **_kw: order.append("enslave_bridge")),
            mock.patch.object(cli, "reload_networkd", side_effect=lambda: order.append("reload_netd")),
            mock.patch.object(cli, "log_journal"),
            mock.patch.object(cli, "record_event"),
        ):
            rc = cli._cmd_apply(force=False)

        self.assertEqual(rc, 0)
        self.assertLess(order.index("build_tree"), order.index("modprobe:dwc2"))
        self.assertLess(order.index("modprobe:dwc2"), order.index("bind_udc"))
        self.assertLess(order.index("bind_udc"), order.index("ensure_bridge"))
        self.assertLess(order.index("ensure_bridge"), order.index("enslave_bridge"))
        self.assertLess(order.index("enslave_bridge"), order.index("reload_netd"))

    def test_apply_refusal_never_touches_bridge(self) -> None:
        macs = DerivedMacs("02:11:22:33:44:55", "02:aa:bb:cc:dd:ee", "02:de:ad:be:ef:01", "test")
        with (
            mock.patch.object(cli, "load_config", return_value=GadgetConfig()),
            mock.patch.object(cli, "detect_controller", return_value=ControllerInfo("fe980000.usb", "peripheral", "module")),
            mock.patch.object(cli, "derive_all_macs", return_value=macs),
            mock.patch.object(cli, "read_board_serial", return_value=("10000000abcdef12", "test")),
            mock.patch.object(cli, "ensure_kernel_module"),
            mock.patch.object(cli, "build_gadget_tree", side_effect=ForeignGadgetError("busy")),
            mock.patch.object(cli, "ensure_bridge_exists") as ensure_br,
            mock.patch.object(cli, "enslave_and_up_bridge") as enslave_br,
            mock.patch.object(cli, "write_networkd_files") as write_net,
            mock.patch.object(cli, "log_journal"),
            mock.patch.object(cli, "record_event"),
            mock.patch.object(cli, "write_runtime_state"),
        ):
            rc = cli._cmd_apply(force=False)

        self.assertEqual(rc, 1)
        ensure_br.assert_not_called()
        enslave_br.assert_not_called()
        write_net.assert_not_called()

    def test_selftest_avahi_allow_interfaces_check(self) -> None:
        import tempfile
        from pathlib import Path
        from wlanpi_usb_gadget.config import validate_config
        from wlanpi_usb_gadget.status import run_selftest

        cfg = validate_config({})
        with tempfile.TemporaryDirectory() as tmp:
            avahi = Path(tmp) / "avahi-daemon.conf"
            avahi.write_text("[server]\nallow-interfaces=usb0, eth0, wlan0, wlan1\nuse-ipv6=no\n", encoding="utf-8")
            checks = {c.name: c for c in run_selftest(cfg, configfs_root=Path(tmp) / "cfs", network_dir=Path(tmp) / "net", avahi_conf=avahi)}
            self.assertEqual(checks["avahi_allow_interfaces"].status, "pass")

            avahi.write_text("[server]\nallow-interfaces=eth0, wlan0\nuse-ipv6=no\n", encoding="utf-8")
            checks = {c.name: c for c in run_selftest(cfg, configfs_root=Path(tmp) / "cfs", network_dir=Path(tmp) / "net", avahi_conf=avahi)}
            self.assertEqual(checks["avahi_allow_interfaces"].status, "warn")
            self.assertIn("usb0 missing", checks["avahi_allow_interfaces"].detail)
            self.assertIn("Add usb0 to allow-interfaces", checks["avahi_allow_interfaces"].hint)

    def test_apply_restarts_avahi_with_stable_bridge_ifindex(self) -> None:
        import subprocess
        from types import SimpleNamespace
        from wlanpi_usb_gadget import netcfg

        cfg = GadgetConfig()
        macs = DerivedMacs("02:11:22:33:44:55", "02:aa:bb:cc:dd:ee", "02:de:ad:be:ef:01", "test")
        systemctl_calls: list[list[str]] = []

        def _fake_systemctl(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            systemctl_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with (
            mock.patch.object(cli, "load_config", return_value=cfg),
            mock.patch.object(cli, "load_profile_state", return_value=SimpleNamespace(active="ncm")),
            mock.patch.object(cli, "detect_controller", return_value=ControllerInfo("fe980000.usb", "peripheral", "module")),
            mock.patch.object(cli, "derive_all_macs", return_value=macs),
            mock.patch.object(cli, "read_board_serial", return_value=("10000000abcdef12", "serial")),
            mock.patch.object(cli, "ensure_kernel_module"),
            mock.patch.object(cli, "build_gadget_tree", return_value="applied"),
            mock.patch.object(cli, "wait_for_udc", return_value="fe980000.usb"),
            mock.patch.object(cli, "write_networkd_files"),
            mock.patch.object(cli, "bind_gadget_udc", return_value="applied"),
            mock.patch.object(cli, "ensure_bridge_exists"),
            mock.patch.object(cli, "discover_member_ifnames", return_value=["usbncm0"]),
            mock.patch.object(cli, "enslave_and_up_bridge"),
            mock.patch.object(cli, "reload_networkd"),
            mock.patch.object(cli, "log_journal"),
            mock.patch.object(cli, "record_event"),
            mock.patch.object(netcfg, "read_iface_ifindex", return_value=4),
            mock.patch("wlanpi_usb_gadget.netcfg.subprocess.run", side_effect=_fake_systemctl),
            mock.patch.object(netcfg, "record_event") as net_rec,
            mock.patch.object(netcfg, "log_journal"),
        ):
            rc = cli._cmd_apply(force=True)

        self.assertEqual(rc, 0)
        self.assertEqual(
            systemctl_calls,
            [
                ["systemctl", "is-enabled", "--quiet", "avahi-daemon"],
                ["systemctl", "try-restart", "avahi-daemon"],
            ],
        )
        net_rec.assert_called_once_with("avahi_restart", profile="ncm", detail="bridge=usb0 ifindex=4 rc=0")


if __name__ == "__main__":
    unittest.main()
