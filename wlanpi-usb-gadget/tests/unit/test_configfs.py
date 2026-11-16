"""Unit tests for configfs tree planning, apply, and ordered teardown."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wlanpi_usb_gadget.configfs import (
    ForeignGadgetError,
    apply_gadget,
    plan_gadget_tree,
    teardown_gadget,
)
from wlanpi_usb_gadget.mac import DerivedMacs
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

            res1 = apply_gadget(plan, "fe980000.usb", configfs_root=root, hash_path=hash_p, lock_path=lock_p)
            self.assertEqual(res1, "applied")
            self.assertTrue((root / "wlanpi-usb-gadget" / "UDC").exists())
            self.assertEqual((root / "wlanpi-usb-gadget" / "UDC").read_text().strip(), "fe980000.usb")
            self.assertTrue((root / "wlanpi-usb-gadget" / "configs" / "c.1" / "rndis.usb0").is_symlink())

            res2 = apply_gadget(plan, "fe980000.usb", configfs_root=root, hash_path=hash_p, lock_path=lock_p)
            self.assertEqual(res2, "unchanged")

            removed = teardown_gadget(configfs_root=root, hash_path=hash_p, lock_path=lock_p)
            self.assertTrue(removed)
            self.assertFalse((root / "wlanpi-usb-gadget").exists())
            self.assertFalse(hash_p.exists())

    def test_foreign_gadget_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "usb_gadget"
            foreign = root / "wlanpi"
            foreign.mkdir(parents=True)
            (foreign / "UDC").write_text("fe980000.usb\n")
            plan = plan_gadget_tree(get_profile("legacy"), self.macs, "10000000abcdef12")
            with self.assertRaises(ForeignGadgetError):
                apply_gadget(
                    plan,
                    "fe980000.usb",
                    configfs_root=root,
                    hash_path=Path(tmp) / "gadget.hash",
                    lock_path=Path(tmp) / "configfs.lock",
                )

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
