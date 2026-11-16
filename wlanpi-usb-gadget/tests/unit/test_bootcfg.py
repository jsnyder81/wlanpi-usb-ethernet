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


if __name__ == "__main__":
    unittest.main()
