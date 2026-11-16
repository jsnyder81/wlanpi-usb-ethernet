"""Unit tests for bounded recovery ladder, token bucket, HOLD, and replug knock."""

from __future__ import annotations

import unittest

from wlanpi_usb_gadget.recovery import RecoveryController, ReplugKnockDetector, TokenBucket


class TestRecovery(unittest.TestCase):
    def test_no_recovery_when_detached_or_suspended_or_disabled(self) -> None:
        rc = RecoveryController()
        self.assertIsNone(rc.next_action(state="CONFIGURED_NO_LINK", time_in_state_sec=20.0, now_mono=20.0, enabled=False))
        self.assertIsNone(rc.next_action(state="DETACHED", time_in_state_sec=100.0, now_mono=100.0, enabled=True))
        self.assertIsNone(rc.next_action(state="SUSPENDED", time_in_state_sec=100.0, now_mono=100.0, enabled=True))
        self.assertIsNone(rc.next_action(state="LINK_UP", time_in_state_sec=100.0, now_mono=100.0, enabled=True))

    def test_ladder_progression_and_hold_on_budget_exhausted(self) -> None:
        rc = RecoveryController(bucket=TokenBucket(budget=3, window_sec=600))
        # Step 1: soft_reconnect after 5s
        self.assertEqual(rc.next_action(state="CONFIGURED_NO_LINK", time_in_state_sec=6.0, now_mono=10.0, enabled=True), "soft_reconnect")
        # Too soon for step 2 (15s backoff)
        self.assertIsNone(rc.next_action(state="CONFIGURED_NO_LINK", time_in_state_sec=15.0, now_mono=15.0, enabled=True))
        # Step 2: rebind
        self.assertEqual(rc.next_action(state="CONFIGURED_NO_LINK", time_in_state_sec=26.0, now_mono=26.0, enabled=True), "rebind")
        # Step 3: teardown_apply after 60s
        self.assertEqual(rc.next_action(state="CONFIGURED_NO_LINK", time_in_state_sec=90.0, now_mono=90.0, enabled=True), "teardown_apply")
        # Budget exhausted -> HOLD
        self.assertEqual(rc.next_action(state="CONFIGURED_NO_LINK", time_in_state_sec=160.0, now_mono=160.0, enabled=True), "enter_hold")
        self.assertTrue(rc.hold)
        # Physical detach/reattach leaves HOLD
        rc.on_physical_detach_reattach()
        self.assertFalse(rc.hold)

    def test_failed_bind_skips_rebind_step(self) -> None:
        rc = RecoveryController(bucket=TokenBucket(budget=5, window_sec=600), last_bind_succeeded=False)
        self.assertEqual(rc.next_action(state="CONFIGURED_NO_LINK", time_in_state_sec=6.0, now_mono=10.0, enabled=True), "soft_reconnect")
        self.assertEqual(rc.next_action(state="CONFIGURED_NO_LINK", time_in_state_sec=26.0, now_mono=26.0, enabled=True), "teardown_apply")

    def test_unbound_skips_soft_reconnect_and_rebind(self) -> None:
        rc = RecoveryController(bucket=TokenBucket(budget=3, window_sec=600))
        self.assertEqual(
            rc.next_action(state="UNBOUND", time_in_state_sec=6.0, now_mono=10.0, enabled=True),
            "teardown_apply",
        )

    def test_replug_knock_3_in_20s(self) -> None:
        kd = ReplugKnockDetector()
        self.assertFalse(kd.record_attach(10.0))
        self.assertFalse(kd.record_attach(15.0))
        self.assertTrue(kd.record_attach(25.0))
        # Old timestamps outside 20s window expire
        self.assertFalse(kd.record_attach(50.0))
        self.assertFalse(kd.record_attach(75.0))


if __name__ == "__main__":
    unittest.main()
