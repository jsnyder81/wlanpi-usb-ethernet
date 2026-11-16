"""Unit tests for the monotonic-clock state machine."""

from __future__ import annotations

import unittest

from wlanpi_usb_gadget.statemachine import Signals, StateMachine


class TestStateMachine(unittest.TestCase):
    def test_all_primary_transitions(self) -> None:
        sm = StateMachine(initial_state="DETACHED", now_mono=10.0)

        t = sm.update(Signals(udc_state="powered"), now_mono=11.0)
        self.assertIsNotNone(t)
        self.assertEqual(sm.state, "ENUMERATING")

        sm.update(Signals(udc_state="configured", carrier_ifaces=()), now_mono=12.0)
        self.assertEqual(sm.state, "CONFIGURED_NO_LINK")

        sm.update(Signals(udc_state="configured", carrier_ifaces=("usbncm0",)), now_mono=13.0)
        self.assertEqual(sm.state, "LINK_UP")

        sm.update(
            Signals(udc_state="configured", carrier_ifaces=("usbncm0",), has_neighbour=True),
            now_mono=14.0,
        )
        self.assertEqual(sm.state, "ONLINE")
        self.assertEqual(sm.online_since_mono, 14.0)

        sm.update(Signals(udc_state="suspended"), now_mono=20.0)
        self.assertEqual(sm.state, "SUSPENDED")
        self.assertIsNone(sm.online_since_mono)

        sm.update(Signals(dr_mode="host"), now_mono=21.0)
        self.assertEqual(sm.state, "HOST_MODE")

        sm.update(Signals(udc_present=False), now_mono=22.0)
        self.assertEqual(sm.state, "NO_UDC")

        sm.update(Signals(gadget_bound=False), now_mono=23.0)
        self.assertEqual(sm.state, "UNBOUND")


if __name__ == "__main__":
    unittest.main()
