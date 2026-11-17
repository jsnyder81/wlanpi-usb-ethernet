"""Unit tests for bounded on-disk event history and journal encoding."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wlanpi_usb_gadget.config import validate_config
from wlanpi_usb_gadget.events import read_events, record_event
from wlanpi_usb_gadget.journal import encode_journal_datagram
from wlanpi_usb_gadget.status import collect_status, format_status_human


class TestEventsAndJournal(unittest.TestCase):
    def test_events_bounded_to_max(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "events.jsonl"
            for i in range(15):
                record_event("transition", state=f"S{i}", events_path=p, max_events=10)
            items = read_events(events_path=p, newest_first=True)
            self.assertEqual(len(items), 10)
            self.assertEqual(items[0]["state"], "S14")
            self.assertEqual(items[-1]["state"], "S5")

    def test_events_file_is_world_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "events.jsonl"
            record_event("transition", state="ONLINE", events_path=p)
            mode = stat.S_IMODE(os.stat(p).st_mode)
            self.assertEqual(mode, 0o644)

    def test_read_events_permission_error_degrades_gracefully(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "events.jsonl"
            p.write_text('{"event":"apply"}\n', encoding="utf-8")
            with mock.patch.object(Path, "read_text", side_effect=PermissionError("denied")):
                items = read_events(events_path=p)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["event"], "permission_denied")
            self.assertIn("events: permission denied (use sudo)", items[0]["detail"])

            cfg = validate_config({})
            with mock.patch.object(Path, "read_text", side_effect=PermissionError("denied")):
                st = collect_status(cfg, state_path=Path(tmp) / "missing.json", events_path=p, configfs_root=Path(tmp) / "cfs")
            self.assertEqual(st.get("events_note"), "events: permission denied (use sudo)")
            self.assertEqual(st["recoveries"], [])
            human = format_status_human(st)
            self.assertIn("events: permission denied (use sudo)", human)

    def test_journal_datagram_encoding(self) -> None:
        raw = encode_journal_datagram(
            "State changed",
            fields={"GADGET_EVENT": "transition", "GADGET_STATE": "ONLINE"},
        )
        self.assertIn(b"MESSAGE=State changed\n", raw)
        self.assertIn(b"GADGET_EVENT=transition\n", raw)
        self.assertIn(b"GADGET_STATE=ONLINE\n", raw)


if __name__ == "__main__":
    unittest.main()
