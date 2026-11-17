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
from wlanpi_usb_gadget.netcfg import render_networkd_files
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
        self.assertEqual(attr_dict["functions/ncm.usb0/ifname"], "usbncm0")
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
            self.assertTrue((root / "wlanpi-usb-gadget" / "configs" / "c.1" / "rndis.usb0").is_symlink())

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
            rec_ev.assert_any_call("ifname_unsupported", profile="legacy", detail="functions/rndis.usb0/ifname=usbrndis0")

            discovered = discover_member_ifnames(configfs_root=root, sys_class_net=sys_net, bridge_name="usb0")
            self.assertIn("usbecm0", discovered)
            self.assertIn("usb1", discovered)
            self.assertNotIn("usb0", discovered)

            files = render_networkd_files(validate_config({}), get_profile("legacy"), self.macs.bridge, member_ifnames=discovered)
            self.assertIn("50-wlanpi-usb-gadget-member-usb1.network", files)
            self.assertIn("Kind=!bridge", files["50-wlanpi-usb-gadget-member-usb1.network"])

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
