"""Unit tests for USB gadget composition profiles."""

from __future__ import annotations

import unittest

from wlanpi_usb_gadget.profiles import PRODUCT_ID, VENDOR_ID, get_profile, list_profiles


class TestProfiles(unittest.TestCase):
    def test_shared_vid_pid_and_distinct_bcd_device(self) -> None:
        self.assertEqual(VENDOR_ID, "0x1209")
        self.assertEqual(PRODUCT_ID, "0x2042")
        profiles = list_profiles()
        bcds = [p.bcd_device for p in profiles]
        self.assertEqual(len(bcds), len(set(bcds)))

    def test_legacy_profile(self) -> None:
        p = get_profile("legacy", serial_console=False)
        self.assertEqual(len(p.configs), 2)
        self.assertEqual(p.configs[0].index, 1)
        self.assertFalse(p.configs[0].os_desc_target)
        self.assertEqual(p.configs[0].functions[0].ifname, "usbecm%d")
        self.assertEqual(p.configs[1].index, 2)
        self.assertTrue(p.configs[1].os_desc_target)
        self.assertEqual(p.configs[1].functions[0].ifname, "usbrndis%d")
        self.assertEqual(p.configs[1].functions[0].os_desc_compatible_id, "RNDIS")

    def test_ncm_profile(self) -> None:
        p = get_profile("ncm", serial_console=True)
        self.assertEqual(len(p.configs), 1)
        self.assertTrue(p.configs[0].os_desc_target)
        self.assertEqual([f.dir_name for f in p.configs[0].functions], ["ncm.usb0", "acm.gs0"])
        self.assertEqual(p.configs[0].functions[0].ifname, "usbncm%d")
        self.assertEqual(p.configs[0].functions[0].os_desc_compatible_id, "WINNCM")

    def test_ncm_ecm_distinct_function_instances(self) -> None:
        p = get_profile("ncm-ecm", serial_console=True)
        all_dirs = [f.dir_name for c in p.configs for f in c.functions]
        self.assertEqual(len(all_dirs), len(set(all_dirs)), "no function instance shared across configs")


if __name__ == "__main__":
    unittest.main()
