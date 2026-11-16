"""Unit tests for board-scoped config.txt management."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wlanpi_usb_gadget.bootcfg import check_bootcfg, ensure_bootcfg, remove_bootcfg


class TestBootcfg(unittest.TestCase):
    def test_go_ensure_idempotent_and_remove_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            cfg_txt = t / "config.txt"
            model_p = t / "wlanpi-model"
            state_p = t / "bootcfg.changed"
            original = "[all]\ndtparam=spi=on\n"
            cfg_txt.write_text(original, encoding="utf-8")
            model_p.write_text("WLAN Pi Go\n", encoding="utf-8")

            r1 = ensure_bootcfg(cfg_txt, wlanpi_model_path=model_p, dt_model_path=t / "dt", boot_state_path=state_p)
            self.assertTrue(r1.is_go)
            self.assertTrue(r1.changed)
            self.assertTrue(r1.block_present)
            self.assertTrue(r1.reboot_required)
            after_ensure = cfg_txt.read_text(encoding="utf-8")
            self.assertEqual(after_ensure.count("# BEGIN wlanpi-usb-gadget"), 1)

            r2 = ensure_bootcfg(cfg_txt, wlanpi_model_path=model_p, dt_model_path=t / "dt", boot_state_path=state_p)
            self.assertFalse(r2.changed)
            self.assertEqual(cfg_txt.read_text(encoding="utf-8"), after_ensure)

            chk = check_bootcfg(cfg_txt, wlanpi_model_path=model_p, dt_model_path=t / "dt", boot_state_path=state_p)
            self.assertTrue(chk.block_present)
            self.assertTrue(chk.reboot_required)

            r3 = remove_bootcfg(cfg_txt, wlanpi_model_path=model_p, dt_model_path=t / "dt", boot_state_path=state_p)
            self.assertTrue(r3.changed)
            self.assertFalse(r3.block_present)
            self.assertEqual(cfg_txt.read_text(encoding="utf-8"), original)

    def test_non_go_board_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            cfg_txt = t / "config.txt"
            model_p = t / "wlanpi-model"
            state_p = t / "bootcfg.changed"
            original = "[all]\ndtparam=spi=on\n"
            cfg_txt.write_text(original, encoding="utf-8")
            model_p.write_text("WLAN Pi M4+\n", encoding="utf-8")

            r = ensure_bootcfg(cfg_txt, wlanpi_model_path=model_p, dt_model_path=t / "dt", boot_state_path=state_p)
            self.assertFalse(r.is_go)
            self.assertFalse(r.changed)
            self.assertEqual(cfg_txt.read_text(encoding="utf-8"), original)

    def test_repairs_emptied_block_preserves_crlf_and_ignores_out_of_order_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            cfg_txt = t / "config.txt"
            model_p = t / "wlanpi-model"
            state_p = t / "bootcfg.changed"
            model_p.write_text("WLAN Pi Go\n", encoding="utf-8")

            # Emptied block (e.g. if overlay line was manually deleted)
            cfg_txt.write_bytes(b"[all]\r\n# BEGIN wlanpi-usb-gadget\r\n# END wlanpi-usb-gadget\r\n")
            r = ensure_bootcfg(cfg_txt, wlanpi_model_path=model_p, dt_model_path=t / "dt", boot_state_path=state_p)
            self.assertTrue(r.changed)
            raw = cfg_txt.read_bytes()
            self.assertIn(b"\r\n", raw)
            self.assertIn(b"dtoverlay=dwc2 # managed by wlanpi-usb-gadget\r\n", raw)
            self.assertIn(b"[all]\r\n# END wlanpi-usb-gadget\r\n", raw)

            # Old package sed '/^dtoverlay=dwc2$/d' does not match our line
            after_sed = "\n".join(
                ln for ln in cfg_txt.read_text(encoding="utf-8").splitlines() if ln != "dtoverlay=dwc2"
            ) + "\n"
            self.assertIn("dtoverlay=dwc2 # managed by wlanpi-usb-gadget", after_sed)

            # Out-of-order markers (END before BEGIN) must not strip user lines
            from wlanpi_usb_gadget.bootcfg import strip_marked_block

            oo = "# END wlanpi-usb-gadget\n# BEGIN wlanpi-usb-gadget\ndtparam=i2c=on\n"
            self.assertEqual(strip_marked_block(oo), oo)


if __name__ == "__main__":
    unittest.main()
