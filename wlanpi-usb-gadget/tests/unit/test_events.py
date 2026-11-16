"""Unit tests for bounded on-disk event history and journal encoding."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wlanpi_usb_gadget.events import read_events, record_event
from wlanpi_usb_gadget.journal import encode_journal_datagram


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
