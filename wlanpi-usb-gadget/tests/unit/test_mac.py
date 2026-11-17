"""Unit tests for deterministic MAC derivation and validation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wlanpi_usb_gadget.configfs import plan_gadget_tree
from wlanpi_usb_gadget.mac import (
    MacDerivationError,
    derive_all_macs,
    derive_mac,
    validate_mac,
)
from wlanpi_usb_gadget.profiles import get_profile


class TestMac(unittest.TestCase):
    def test_derive_mac_format_and_bits(self) -> None:
        for role in ("host", "device", "bridge", "ecm", "rndis", "ncm"):
            mac = derive_mac("10000000abcdef12", role)
            validate_mac(mac)
            first_octet = int(mac.split(":")[0], 16)
            self.assertEqual(first_octet & 0x01, 0, "multicast bit must be 0")
            self.assertEqual(first_octet & 0x02, 0x02, "locally administered bit must be 1")

    def test_derive_mac_deterministic_and_distinct_per_role(self) -> None:
        h1 = derive_mac("10000000abcdef12", "host")
        h2 = derive_mac("10000000abcdef12", "host")
        d1 = derive_mac("10000000abcdef12", "device")
        b1 = derive_mac("10000000abcdef12", "bridge")
        e1 = derive_mac("10000000abcdef12", "ecm")
        r1 = derive_mac("10000000abcdef12", "rndis")
        n1 = derive_mac("10000000abcdef12", "ncm")
        self.assertEqual(h1, h2)
        self.assertEqual(len({h1, d1, b1, e1, r1, n1}), 6)

    def test_per_function_macs_distinct_in_gadget_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            serial_p = Path(tmp) / "serial-number"
            serial_p.write_bytes(b"10000000abcdef12\x00")
            macs = derive_all_macs(serial_path=serial_p, machine_id_path=Path(tmp) / "mid")
            self.assertEqual(len({macs.bridge, macs.ecm, macs.rndis, macs.ncm, macs.host}), 5)

            plan = plan_gadget_tree(get_profile("legacy"), macs, "10000000abcdef12")
            attrs = dict(plan.attrs)
            self.assertEqual(attrs["functions/ecm.usb0/dev_addr"], macs.ecm)
            self.assertEqual(attrs["functions/rndis.usb0/dev_addr"], macs.rndis)
            self.assertNotEqual(attrs["functions/ecm.usb0/dev_addr"], attrs["functions/rndis.usb0/dev_addr"])

    def test_lowest_serial_byte_affects_mac(self) -> None:
        m1 = derive_mac("10000000abcdef12", "host")
        m2 = derive_mac("10000000abcdef13", "host")
        self.assertNotEqual(m1, m2)

    def test_serial_and_machine_id_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            serial_p = Path(tmp) / "serial-number"
            mid_p = Path(tmp) / "machine-id"
            with self.assertRaises(MacDerivationError):
                derive_all_macs(serial_path=serial_p, machine_id_path=mid_p)

            mid_p.write_text("0123456789abcdef0123456789abcdef\n", encoding="utf-8")
            macs = derive_all_macs(serial_path=serial_p, machine_id_path=mid_p)
            self.assertEqual(macs.source, "machine-id")

            serial_p.write_bytes(b"10000000deadbeef\x00")
            macs2 = derive_all_macs(serial_path=serial_p, machine_id_path=mid_p)
            self.assertEqual(macs2.source, "devicetree-serial")
            self.assertNotEqual(macs.host, macs2.host)

    def test_validate_mac_rejects_bad(self) -> None:
        for bad in ("0200:00:ab:cd:ef:12", "03:00:00:ab:cd:ef", "02:00:00:ab:cd", "ZZ:00:00:00:00:00"):
            with self.assertRaises(MacDerivationError):
                validate_mac(bad)


if __name__ == "__main__":
    unittest.main()
