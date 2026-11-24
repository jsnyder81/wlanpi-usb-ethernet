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

    def test_member_up_carrier_and_teardown_apply_enslaves_after_bind(self) -> None:
        from wlanpi_usb_gadget.mac import DerivedMacs
        from wlanpi_usb_gadget.recovery import RecoveryController

        with tempfile.TemporaryDirectory() as tmp:
            sys_net = Path(tmp) / "sys_net"
            (sys_net / "usbecm0").mkdir(parents=True)
            (sys_net / "usbecm0" / "carrier").write_text("1\n")

            orig_path = monitor.Path

            def _mock_path(p: str) -> Path:
                if p == "/sys/class/net":
                    return sys_net
                return orig_path(p)

            with mock.patch.object(monitor, "Path", side_effect=_mock_path):
                self.assertEqual(monitor._read_carrier_ifaces(names=("usbecm0", "usbrndis0")), ("usbecm0",))

        order: list[str] = []
        macs = DerivedMacs("02:11:22:33:44:55", "02:aa:bb:cc:dd:ee", "02:de:ad:be:ef:01", "test")
        cfg = GadgetConfig(profile="legacy", safe_profile="legacy")
        rec = RecoveryController()

        def _fake_apply(plan, udc, *, force=False, before_bind=None):
            if before_bind is not None:
                before_bind()
            order.append("apply_gadget")
            return "applied"

        with (
            mock.patch.object(monitor, "teardown_gadget", side_effect=lambda: order.append("teardown") or True),
            mock.patch.object(monitor, "derive_all_macs", return_value=macs),
            mock.patch.object(monitor, "read_board_serial", return_value=("10000000abcdef12", "test")),
            mock.patch.object(monitor, "apply_gadget", side_effect=_fake_apply),
            mock.patch.object(monitor, "write_networkd_files", side_effect=lambda *_a, **_kw: order.append("write_netcfg") or []),
            mock.patch.object(monitor, "ensure_bridge_exists", side_effect=lambda *_a, **_kw: order.append("ensure_bridge")),
            mock.patch.object(monitor, "discover_member_ifnames", return_value=["usbecm0", "usbrndis0"]),
            mock.patch.object(monitor, "enslave_and_up_bridge", side_effect=lambda *_a, **_kw: order.append("enslave_bridge")),
            mock.patch.object(monitor, "reload_networkd", side_effect=lambda: order.append("reload_netd")),
            mock.patch.object(monitor, "maybe_restart_avahi", side_effect=lambda *_a, **_kw: order.append("restart_avahi") or True),
        ):
            monitor._execute_recovery_action(
                "teardown_apply",
                udc_name="fe980000.usb",
                cfg=cfg,
                profile_name="legacy",
                recovery=rec,
            )

        self.assertTrue(rec.last_bind_succeeded)
        self.assertLess(order.index("apply_gadget"), order.index("ensure_bridge"))
        self.assertLess(order.index("ensure_bridge"), order.index("enslave_bridge"))
        self.assertLess(order.index("enslave_bridge"), order.index("reload_netd"))
        self.assertLess(order.index("reload_netd"), order.index("restart_avahi"))

    def test_consume_reapply_flag_runs_apply_and_removes_flag(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            flag = Path(tmp) / "reapply-pending"

            with mock.patch.object(monitor.subprocess, "run") as mock_run:
                self.assertFalse(monitor._consume_reapply_flag(flag))
                mock_run.assert_not_called()

            flag.write_text("", encoding="utf-8")
            with (
                mock.patch.object(monitor, "log_journal"),
                mock.patch.object(
                    monitor.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess(["wlanpi-usb-gadget", "apply"], 0, "", ""),
                ) as mock_run,
            ):
                self.assertTrue(monitor._consume_reapply_flag(flag))
                self.assertFalse(flag.exists())
                mock_run.assert_called_once()
                self.assertEqual(mock_run.call_args[0][0], ["/usr/bin/wlanpi-usb-gadget", "apply"])

    def test_rebind_while_carrier_stable_and_edge_logging(self) -> None:
        from wlanpi_usb_gadget.netlink import LinkEvent

        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            profile_path = t / "profile.json"
            cfg = GadgetConfig(profile="legacy", safe_profile="legacy")

            # Simulate UDC configured -> not attached -> configured while member carrier stays '1',
            # plus a bridge carrier 1 -> 0 -> 1 edge and a netlink LinkEvent bounce while UDC stays configured.
            udc_states = iter(["configured", "not attached", "configured", "configured"])
            bridge_carriers = iter([True, True, False, True])
            has_neigh_seq = iter([True, True, False, True])

            class _FakeNetlink:
                def __init__(self) -> None:
                    self.calls = 0

                def fileno(self) -> int:
                    return 99

                def read_events(self) -> list[LinkEvent]:
                    self.calls += 1
                    if self.calls == 1:
                        return [
                            LinkEvent(kind="new", ifindex=5, ifname="usb0", flags=1, carrier=False),
                            LinkEvent(kind="new", ifindex=5, ifname="usb0", flags=0x10001, carrier=True),
                        ]
                    return []

                def close(self) -> None:
                    return None

            fake_nl = _FakeNetlink()
            poll_calls = 0

            def _fake_poll() -> list[tuple[int, int]]:
                nonlocal poll_calls
                poll_calls += 1
                if poll_calls == 4:
                    import os
                    import signal

                    os.kill(os.getpid(), signal.SIGTERM)
                    return [(99, 1)]
                return []

            recorded: list[tuple[str, dict[str, object]]] = []

            with (
                mock.patch.object(monitor, "load_config", return_value=cfg),
                mock.patch.object(monitor, "NetlinkListener", return_value=fake_nl),
                mock.patch.object(monitor, "detect_controller", return_value=ControllerInfo("fe980000.usb", "peripheral", "module")),
                mock.patch.object(monitor, "list_udcs", return_value=["fe980000.usb"]),
                mock.patch.object(monitor, "read_udc_attr", side_effect=lambda _u, _a: next(udc_states, "configured")),
                mock.patch.object(monitor, "read_bound_udc", return_value="fe980000.usb"),
                mock.patch.object(monitor, "_read_carrier_ifaces", return_value=("usbrndis0",)),
                mock.patch.object(monitor, "_read_bridge_carrier", side_effect=lambda _b: next(bridge_carriers, True)),
                mock.patch.object(monitor, "_has_bridge_neighbour", side_effect=lambda _b: next(has_neigh_seq, True)),
                mock.patch.object(monitor, "DEFAULT_PROFILE_STATE_PATH", profile_path),
                mock.patch.object(monitor, "load_profile_state", side_effect=lambda default_profile="legacy": load_profile_state(default_profile=default_profile, path=profile_path)),
                mock.patch.object(monitor, "write_runtime_state"),
                mock.patch.object(monitor, "record_event", side_effect=lambda ev, **kw: recorded.append((ev, kw))),
                mock.patch.object(monitor, "log_journal"),
                mock.patch.object(monitor, "sd_notify"),
                mock.patch.object(monitor, "maybe_restart_avahi") as mock_avahi,
                mock.patch("select.poll", return_value=_FakePoller(_fake_poll)),
            ):
                rc = monitor.run_monitor()

            self.assertEqual(rc, 0)
            mock_avahi.assert_not_called()
            udc_edges = [kw["detail"] for ev, kw in recorded if ev == "udc_edge"]
            self.assertEqual(udc_edges, ["configured->not attached", "not attached->configured"])

            transitions = [(kw["prev_state"], kw["state"]) for ev, kw in recorded if ev == "transition"]
            self.assertIn(("ONLINE", "DETACHED"), transitions)
            self.assertIn(("DETACHED", "LINK_UP"), transitions)
            self.assertIn(("LINK_UP", "CONFIGURED_NO_LINK"), transitions)
            self.assertIn(("CONFIGURED_NO_LINK", "ONLINE"), transitions)

            bridge_edges = [kw["detail"] for ev, kw in recorded if ev == "bridge_carrier"]
            self.assertIn("carrier=1", bridge_edges)
            self.assertIn("carrier=0", bridge_edges)

    def test_udc_edge_ignores_intermediate_states(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as tmp:
            profile_path = Path(tmp) / "profile-state.json"
            profile_path.write_text(json.dumps({"active": "legacy", "committed": "legacy"}), encoding="utf-8")
            cfg = GadgetConfig(monitor_recovery="off")

            # Intermediate states (powered/default/addressed/suspended) must not swallow
            # the not attached -> configured or configured -> not attached edge.
            udc_states = iter(["not attached", "powered", "default", "addressed", "configured", "suspended", "not attached"])

            class _FakePoller:
                def __init__(self, cb: object) -> None:
                    self._cb = cb

                def register(self, _fd: int, _mask: int) -> None:
                    return None

                def poll(self, _timeout_ms: int) -> list[tuple[int, int]]:
                    return self._cb()  # type: ignore[operator]

            class _FakeNetlink:
                def fileno(self) -> int:
                    return 99

                def read_events(self) -> list[object]:
                    return []

                def close(self) -> None:
                    return None

            poll_calls = 0

            def _fake_poll() -> list[tuple[int, int]]:
                nonlocal poll_calls
                poll_calls += 1
                if poll_calls == 7:
                    import os
                    import signal

                    os.kill(os.getpid(), signal.SIGTERM)
                return []

            recorded: list[tuple[str, dict[str, object]]] = []

            with (
                mock.patch.object(monitor, "load_config", return_value=cfg),
                mock.patch.object(monitor, "NetlinkListener", return_value=_FakeNetlink()),
                mock.patch.object(monitor, "detect_controller", return_value=ControllerInfo("fe980000.usb", "peripheral", "module")),
                mock.patch.object(monitor, "list_udcs", return_value=["fe980000.usb"]),
                mock.patch.object(monitor, "read_udc_attr", side_effect=lambda _u, _a: next(udc_states, "not attached")),
                mock.patch.object(monitor, "read_bound_udc", return_value="fe980000.usb"),
                mock.patch.object(monitor, "_read_carrier_ifaces", return_value=()),
                mock.patch.object(monitor, "_read_bridge_carrier", return_value=False),
                mock.patch.object(monitor, "_has_bridge_neighbour", return_value=False),
                mock.patch.object(monitor, "DEFAULT_PROFILE_STATE_PATH", profile_path),
                mock.patch.object(monitor, "load_profile_state", side_effect=lambda default_profile="legacy": load_profile_state(default_profile=default_profile, path=profile_path)),
                mock.patch.object(monitor, "write_runtime_state"),
                mock.patch.object(monitor, "record_event", side_effect=lambda ev, **kw: recorded.append((ev, kw))),
                mock.patch.object(monitor, "log_journal"),
                mock.patch.object(monitor, "sd_notify"),
                mock.patch.object(monitor, "maybe_restart_avahi") as mock_avahi,
                mock.patch("select.poll", return_value=_FakePoller(_fake_poll)),
            ):
                rc = monitor.run_monitor()

            self.assertEqual(rc, 0)
            mock_avahi.assert_not_called()
            udc_edges = [kw["detail"] for ev, kw in recorded if ev == "udc_edge"]
            self.assertEqual(udc_edges, ["not attached->configured", "configured->not attached"])


if __name__ == "__main__":
    unittest.main()
