"""Unit tests for configfs tree planning, apply, and ordered teardown."""

from __future__ import annotations

import errno
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wlanpi_usb_gadget import configfs
from wlanpi_usb_gadget.config import validate_config
from wlanpi_usb_gadget.configfs import (
    ForeignGadgetError,
    apply_gadget,
    discover_member_ifnames,
    plan_gadget_tree,
    teardown_gadget,
)
from wlanpi_usb_gadget.mac import DerivedMacs
from wlanpi_usb_gadget.netcfg import ensure_bridge_exists, render_networkd_files
from wlanpi_usb_gadget.profiles import get_profile


class TestConfigfs(unittest.TestCase):
    def setUp(self) -> None:
        self.macs = DerivedMacs(
            host="02:11:22:33:44:55",
            device="02:aa:bb:cc:dd:ee",
            bridge="02:de:ad:be:ef:01",
            source="devicetree-serial",
        )

    def test_plan_determinism_and_ifname(self) -> None:
        p1 = plan_gadget_tree(get_profile("ncm", serial_console=True), self.macs, "10000000abcdef12")
        p2 = plan_gadget_tree(get_profile("ncm", serial_console=True), self.macs, "10000000abcdef12")
        self.assertEqual(p1.tree_hash, p2.tree_hash)
        attr_dict = dict(p1.attrs)
        self.assertEqual(attr_dict["functions/ncm.usb0/ifname"], "usbncm%d")
        self.assertEqual(attr_dict["functions/ncm.usb0/os_desc/interface.ncm/compatible_id"], "WINNCM")

    def test_legacy_matches_old_device_class_and_no_acm_by_default(self) -> None:
        leg = get_profile("legacy")
        plan = plan_gadget_tree(leg, self.macs, "10000000abcdef12")
        attr_dict = dict(plan.attrs)
        self.assertNotIn("bDeviceClass", attr_dict)
        self.assertNotIn("functions/acm.gs0", plan.dirs)

    def test_apply_idempotent_and_ordered_teardown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "usb_gadget"
            root.mkdir()
            hash_p = Path(tmp) / "gadget.hash"
            lock_p = Path(tmp) / "configfs.lock"
            plan = plan_gadget_tree(get_profile("legacy"), self.macs, "10000000abcdef12")

            res1 = apply_gadget(
                plan,
                "fe980000.usb",
                configfs_root=root,
                hash_path=hash_p,
                lock_path=lock_p,
                first_bind_marker=Path(tmp) / "first_bind_done",
            )
            self.assertEqual(res1, "applied")
            self.assertTrue((root / "wlanpi-usb-gadget" / "UDC").exists())
            self.assertEqual((root / "wlanpi-usb-gadget" / "UDC").read_text().strip(), "fe980000.usb")
            # Legacy config numbering must match wlanpi-usb-ethernet: c.1=ECM, c.2=RNDIS+os_desc
            self.assertTrue((root / "wlanpi-usb-gadget" / "configs" / "c.1" / "ecm.usb0").is_symlink())
            self.assertFalse((root / "wlanpi-usb-gadget" / "configs" / "c.1" / "rndis.usb0").exists())
            self.assertTrue((root / "wlanpi-usb-gadget" / "configs" / "c.2" / "rndis.usb0").is_symlink())
            self.assertFalse((root / "wlanpi-usb-gadget" / "configs" / "c.2" / "ecm.usb0").exists())
            self.assertTrue((root / "wlanpi-usb-gadget" / "os_desc" / "c.2").is_symlink())
            self.assertFalse((root / "wlanpi-usb-gadget" / "os_desc" / "c.1").exists())

            res2 = apply_gadget(
                plan,
                "fe980000.usb",
                configfs_root=root,
                hash_path=hash_p,
                lock_path=lock_p,
                first_bind_marker=Path(tmp) / "first_bind_done",
            )
            self.assertEqual(res2, "unchanged")

            removed = teardown_gadget(configfs_root=root, hash_path=hash_p, lock_path=lock_p)
            self.assertTrue(removed)
            self.assertFalse((root / "wlanpi-usb-gadget").exists())
            self.assertFalse(hash_p.exists())

    def test_foreign_gadget_refused_for_non_legacy_or_after_first_bind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "usb_gadget"
            foreign = root / "other-gadget"
            foreign.mkdir(parents=True)
            (foreign / "UDC").write_text("fe980000.usb\n")
            plan = plan_gadget_tree(get_profile("legacy"), self.macs, "10000000abcdef12")
            marker = Path(tmp) / "first_bind_done"
            with self.assertRaises(ForeignGadgetError):
                apply_gadget(
                    plan,
                    "fe980000.usb",
                    configfs_root=root,
                    hash_path=Path(tmp) / "gadget.hash",
                    lock_path=Path(tmp) / "configfs.lock",
                    first_bind_marker=marker,
                )

            # Even 'wlanpi' is refused once first_bind_marker exists
            foreign.rename(root / "wlanpi")
            marker.write_text("1\n")
            with self.assertRaises(ForeignGadgetError):
                apply_gadget(
                    plan,
                    "fe980000.usb",
                    configfs_root=root,
                    hash_path=Path(tmp) / "gadget.hash",
                    lock_path=Path(tmp) / "configfs.lock",
                    first_bind_marker=marker,
                )

    def test_legacy_wlanpi_gadget_torn_down_on_fresh_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "usb_gadget"
            legacy = root / "wlanpi"
            (legacy / "functions" / "ecm.usb0").mkdir(parents=True)
            (legacy / "configs" / "c.1" / "strings" / "0x409").mkdir(parents=True)
            (legacy / "configs" / "c.1" / "ecm.usb0").symlink_to(legacy / "functions" / "ecm.usb0")
            (legacy / "UDC").write_text("fe980000.usb\n")
            plan = plan_gadget_tree(get_profile("legacy"), self.macs, "10000000abcdef12")
            marker = Path(tmp) / "first_bind_done"

            with mock.patch.object(configfs, "record_event"), mock.patch.object(configfs, "log_journal"):
                res = apply_gadget(
                    plan,
                    "fe980000.usb",
                    configfs_root=root,
                    hash_path=Path(tmp) / "gadget.hash",
                    lock_path=Path(tmp) / "configfs.lock",
                    first_bind_marker=marker,
                )
            self.assertEqual(res, "applied")
            self.assertFalse(legacy.exists())
            self.assertTrue(marker.exists())

    def test_ifname_einval_falls_back_to_kernel_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "usb_gadget"
            root.mkdir()
            sys_net = Path(tmp) / "sys_net"
            # Mock /sys/class/net/usb1/device -> .../gadget.0/
            gadget_dev = Path(tmp) / "devices" / "fe980000.usb" / "gadget.0"
            gadget_dev.mkdir(parents=True)
            (sys_net / "usb1").mkdir(parents=True)
            (sys_net / "usb1" / "device").symlink_to(gadget_dev)
            (sys_net / "usb0" / "bridge").mkdir(parents=True)  # bridge itself must be ignored

            plan = plan_gadget_tree(get_profile("legacy"), self.macs, "10000000abcdef12")
            orig_write = configfs.write_attr

            def _mock_write(path: Path, value: str) -> None:
                if path.parts[-2:] == ("rndis.usb0", "ifname"):
                    raise configfs.ConfigfsError("einval") from OSError(errno.EINVAL, "Invalid argument")
                orig_write(path, value)

            with (
                mock.patch.object(configfs, "write_attr", side_effect=_mock_write),
                mock.patch.object(configfs, "record_event") as rec_ev,
                mock.patch.object(configfs, "log_journal"),
            ):
                res = apply_gadget(
                    plan,
                    "fe980000.usb",
                    configfs_root=root,
                    hash_path=Path(tmp) / "gadget.hash",
                    lock_path=Path(tmp) / "configfs.lock",
                    first_bind_marker=Path(tmp) / "first_bind_done",
                )
            self.assertEqual(res, "applied")
            rec_ev.assert_any_call("ifname_unsupported", profile="legacy", detail="functions/rndis.usb0/ifname=usbrndis%d")

            discovered = discover_member_ifnames(configfs_root=root, sys_class_net=sys_net, bridge_name="usb0")
            self.assertIn("usbecm%d", discovered)
            self.assertIn("usb1", discovered)
            self.assertNotIn("usb0", discovered)

            files = render_networkd_files(validate_config({}), get_profile("legacy"), self.macs.bridge, member_ifnames=discovered)
            self.assertIn("50-wlanpi-usb-gadget-member-usb1.network", files)
            self.assertIn("Kind=!bridge", files["50-wlanpi-usb-gadget-member-usb1.network"])

    def test_all_ifnames_honored_and_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "usb_gadget"
            root.mkdir()
            sys_net = Path(tmp) / "sys_net"
            sys_net.mkdir()
            plan = plan_gadget_tree(get_profile("legacy"), self.macs, "10000000abcdef12")
            orig_write = configfs.write_attr

            # Simulate kernel expanding %d -> 0 on readback
            def _kernel_expand(path: Path, value: str) -> None:
                if path.name == "ifname" and "%d" in value:
                    orig_write(path, value.replace("%d", "0"))
                else:
                    orig_write(path, value)

            with (
                mock.patch.object(configfs, "write_attr", side_effect=_kernel_expand),
                mock.patch.object(configfs, "log_journal"),
            ):
                apply_gadget(
                    plan,
                    "fe980000.usb",
                    configfs_root=root,
                    hash_path=Path(tmp) / "gadget.hash",
                    lock_path=Path(tmp) / "configfs.lock",
                    first_bind_marker=Path(tmp) / "first_bind_done",
                )

            discovered = discover_member_ifnames(configfs_root=root, sys_class_net=sys_net, bridge_name="usb0")
            self.assertEqual(sorted(discovered), ["usbecm0", "usbrndis0"])

    def test_kernel_assigns_usb0_collision_resolved_by_ensure_bridge_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sys_net = Path(tmp) / "sys_net"
            # Simulate a non-bridge function netdev holding usb0
            (sys_net / "usb0").mkdir(parents=True)
            import subprocess as _sp

            calls: list[list[str]] = []

            def _fake_run(cmd: list[str], **_kw: object) -> _sp.CompletedProcess[str]:
                calls.append(cmd)
                return _sp.CompletedProcess(cmd, 0, "", "")

            with mock.patch("wlanpi_usb_gadget.netcfg.subprocess.run", side_effect=_fake_run):
                ensure_bridge_exists("usb0", self.macs.bridge, sys_class_net=sys_net)

            self.assertIn(["ip", "link", "set", "dev", "usb0", "down"], calls)
            self.assertIn(["ip", "link", "set", "dev", "usb0", "name", "usbgadget0"], calls)
            self.assertIn(
                ["ip", "link", "add", "name", "usb0", "address", self.macs.bridge, "type", "bridge", "stp_state", "0"],
                calls,
            )

    def test_udc_ebusy_reclaims_legacy_g_ether_and_refuses_other(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "usb_gadget"
            root.mkdir()
            udc_dir = Path(tmp) / "udc" / "fe980000.usb"
            udc_dir.mkdir(parents=True)
            (udc_dir / "current_drivers").write_text("g_ether\n")
            (udc_dir / "UDC").write_text("fe980000.usb\n")

            plan = plan_gadget_tree(get_profile("legacy"), self.macs, "10000000abcdef12")
            orig_write = configfs.write_attr
            bind_attempts = 0

            def _mock_write(path: Path, value: str) -> None:
                nonlocal bind_attempts
                if path == root / "wlanpi-usb-gadget" / "UDC" and value == "fe980000.usb":
                    bind_attempts += 1
                    if bind_attempts == 1:
                        raise configfs.ConfigfsError("ebusy") from OSError(errno.EBUSY, "Device or resource busy")
                orig_write(path, value)

            with (
                mock.patch.object(configfs, "write_attr", side_effect=_mock_write),
                mock.patch("wlanpi_usb_gadget.configfs.subprocess.run") as sub_run,
                mock.patch.object(configfs, "record_event") as rec_ev,
                mock.patch.object(configfs, "log_journal"),
            ):
                res = apply_gadget(
                    plan,
                    "fe980000.usb",
                    configfs_root=root,
                    hash_path=Path(tmp) / "gadget.hash",
                    lock_path=Path(tmp) / "configfs.lock",
                    first_bind_marker=Path(tmp) / "first_bind_done",
                    udc_class_dir=Path(tmp) / "udc",
                )

            self.assertEqual(res, "applied")
            self.assertEqual(bind_attempts, 2)
            self.assertEqual((udc_dir / "UDC").read_text().strip(), "")
            sub_run.assert_called_once_with(["rmmod", "g_ether"], capture_output=True, text=True, check=False)
            rec_ev.assert_any_call("udc_reclaimed", udc_state="busy", detail="reclaimed fe980000.usb from g_ether")

            # Non-legacy holder must be refused without unbinding
            (udc_dir / "current_drivers").write_text("some_other_gadget\n")
            (udc_dir / "UDC").write_text("fe980000.usb\n")

            def _always_busy(path: Path, value: str) -> None:
                if path == root / "wlanpi-usb-gadget" / "UDC" and value == "fe980000.usb":
                    raise configfs.ConfigfsError("ebusy") from OSError(errno.EBUSY, "Device or resource busy")
                orig_write(path, value)

            with (
                mock.patch.object(configfs, "write_attr", side_effect=_always_busy),
                mock.patch("wlanpi_usb_gadget.configfs.subprocess.run") as sub_run2,
                mock.patch.object(configfs, "record_event") as rec_ev2,
                mock.patch.object(configfs, "log_journal"),
            ):
                with self.assertRaises(ForeignGadgetError):
                    apply_gadget(
                        plan,
                        "fe980000.usb",
                        force=True,
                        configfs_root=root,
                        hash_path=Path(tmp) / "gadget.hash",
                        lock_path=Path(tmp) / "configfs.lock",
                        first_bind_marker=Path(tmp) / "first_bind_done",
                        udc_class_dir=Path(tmp) / "udc",
                    )
            sub_run2.assert_not_called()
            self.assertEqual((udc_dir / "UDC").read_text().strip(), "fe980000.usb")
            rec_ev2.assert_any_call("udc_busy", udc_state="busy", detail="fe980000.usb held by some_other_gadget")

    def test_teardown_partial_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "usb_gadget"
            partial = root / "wlanpi-usb-gadget"
            (partial / "functions" / "ecm.usb0").mkdir(parents=True)
            (partial / "strings" / "0x409").mkdir(parents=True)
            removed = teardown_gadget(
                configfs_root=root,
                hash_path=Path(tmp) / "gadget.hash",
                lock_path=Path(tmp) / "configfs.lock",
            )
            self.assertTrue(removed)
            self.assertFalse(partial.exists())


if __name__ == "__main__":
    unittest.main()
