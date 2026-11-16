"""Unit tests exercising run_monitor() loop with mocked sysfs and poll."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wlanpi_usb_gadget import monitor
from wlanpi_usb_gadget.config import GadgetConfig
from wlanpi_usb_gadget.deadman import load_profile_state, start_trial
from wlanpi_usb_gadget.udc import ControllerInfo


class _FakePoller:
    def __init__(self, on_poll: object) -> None:
        self._on_poll = on_poll

    def register(self, _fd: int, _mask: int) -> None:
        return None

    def poll(self, _timeout_ms: int) -> list[tuple[int, int]]:
        return self._on_poll()


class TestMonitorLoop(unittest.TestCase):
    def test_run_monitor_reloads_trial_and_writes_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            profile_path = t / "profile.json"
            state_path = t / "state.json"
            events_path = t / "events.jsonl"
            cfg = GadgetConfig(profile="legacy", safe_profile="legacy")

            # Start a trial on disk after monitor would have started
            start_trial("ncm", current_default="legacy", path=profile_path)

            udc_states = iter(["not attached", "configured", "configured"])
            poll_calls = 0

            def _fake_poll() -> list[tuple[int, int]]:
                nonlocal poll_calls
                poll_calls += 1
                if poll_calls >= 3:
                    # Trigger clean exit via SIGTERM handler simulation
                    import os
                    import signal

                    os.kill(os.getpid(), signal.SIGTERM)
                return []

            with (
                mock.patch.object(monitor, "load_config", return_value=cfg),
                mock.patch.object(monitor, "NetlinkListener", side_effect=OSError("no netlink")),
                mock.patch.object(monitor, "detect_controller", return_value=ControllerInfo("fe980000.usb", "peripheral", "module")),
                mock.patch.object(monitor, "list_udcs", return_value=["fe980000.usb"]),
                mock.patch.object(monitor, "read_udc_attr", side_effect=lambda _u, _a: next(udc_states, "configured")),
                mock.patch.object(monitor, "read_bound_udc", return_value="fe980000.usb"),
                mock.patch.object(monitor, "_read_carrier_ifaces", return_value=("usbncm0",)),
                mock.patch.object(monitor, "_has_bridge_neighbour", return_value=True),
                mock.patch.object(monitor, "DEFAULT_PROFILE_STATE_PATH", profile_path),
                mock.patch.object(monitor, "load_profile_state", side_effect=lambda default_profile="legacy": load_profile_state(default_profile=default_profile, path=profile_path)),
                mock.patch.object(monitor, "write_runtime_state", side_effect=lambda d: state_path.write_text(str(d["state"]) + ":" + str(d["profile"]))),
                mock.patch.object(monitor, "record_event", side_effect=lambda *a, **kw: events_path.write_text("ok")),
                mock.patch.object(monitor, "log_journal"),
                mock.patch.object(monitor, "sd_notify"),
                mock.patch("select.poll", return_value=_FakePoller(_fake_poll)),
            ):
                rc = monitor.run_monitor()

            self.assertEqual(rc, 0)
            self.assertEqual(state_path.read_text(), "ONLINE:ncm")


if __name__ == "__main__":
    unittest.main()
