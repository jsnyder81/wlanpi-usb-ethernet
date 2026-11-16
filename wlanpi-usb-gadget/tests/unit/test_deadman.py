"""Unit tests for dead-man profile trial, commit, and revert logic."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wlanpi_usb_gadget.deadman import (
    load_profile_state,
    revert_trial,
    start_trial,
    tick_trial,
)


class TestDeadman(unittest.TestCase):
    def test_trial_does_not_count_detached_and_commits_on_online_30s(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "profile.json"
            st = start_trial("ncm", current_default="legacy", path=p)
            self.assertTrue(st.in_trial)
            self.assertEqual(st.committed, "legacy")

            # DETACHED time does not count
            res = tick_trial(
                st,
                gadget_state="DETACHED",
                dt_sec=500.0,
                online_duration_sec=None,
                trial_timeout_sec=300,
                path=p,
            )
            self.assertIsNone(res)
            self.assertEqual(st.trial_elapsed_sec, 0.0)

            # ONLINE for 30s auto-commits
            res = tick_trial(
                st,
                gadget_state="ONLINE",
                dt_sec=5.0,
                online_duration_sec=30.0,
                trial_timeout_sec=300,
                path=p,
            )
            self.assertEqual(res, "commit")
            reloaded = load_profile_state(path=p)
            self.assertFalse(reloaded.in_trial)
            self.assertEqual(reloaded.committed, "ncm")

    def test_trial_reverts_after_timeout_and_survives_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "profile.json"
            st = start_trial("ncm", current_default="legacy", path=p)
            tick_trial(
                st,
                gadget_state="CONFIGURED_NO_LINK",
                dt_sec=200.0,
                online_duration_sec=None,
                trial_timeout_sec=300,
                path=p,
            )
            # Simulate reboot mid-trial
            st2 = load_profile_state(path=p)
            self.assertTrue(st2.in_trial)
            self.assertEqual(st2.trial_elapsed_sec, 200.0)

            res = tick_trial(
                st2,
                gadget_state="CONFIGURED_NO_LINK",
                dt_sec=110.0,
                online_duration_sec=None,
                trial_timeout_sec=300,
                path=p,
            )
            self.assertEqual(res, "revert")
            reverted, target = revert_trial(safe_profile="legacy", path=p)
            self.assertEqual(target, "legacy")
            self.assertFalse(reverted.in_trial)


if __name__ == "__main__":
    unittest.main()
