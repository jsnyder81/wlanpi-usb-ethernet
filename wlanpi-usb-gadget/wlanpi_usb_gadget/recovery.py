"""Bounded recovery ladder, token-bucket rate limiter, HOLD state, and replug knock."""

from __future__ import annotations

from dataclasses import dataclass, field

BACKOFF_SCHEDULE_SEC = (5.0, 15.0, 60.0)
NON_RECOVERABLE_STATES = ("DETACHED", "SUSPENDED", "HOST_MODE", "NO_UDC", "LINK_UP", "ONLINE", "HOLD")


@dataclass
class TokenBucket:
    """Monotonic-time sliding-window rate limiter."""

    budget: int = 3
    window_sec: int = 600
    timestamps: list[float] = field(default_factory=list)

    def _prune(self, now_mono: float) -> None:
        cutoff = now_mono - float(self.window_sec)
        self.timestamps = [t for t in self.timestamps if t > cutoff]

    def remaining(self, now_mono: float) -> int:
        self._prune(now_mono)
        return max(0, self.budget - len(self.timestamps))

    def consume(self, now_mono: float) -> bool:
        self._prune(now_mono)
        if len(self.timestamps) >= self.budget:
            return False
        self.timestamps.append(now_mono)
        return True


@dataclass
class ReplugKnockDetector:
    """Detects 3 physical attach events within 20 s to recover SAFE_PROFILE."""

    threshold: int = 3
    window_sec: float = 20.0
    attach_times: list[float] = field(default_factory=list)

    def record_attach(self, now_mono: float) -> bool:
        """Record an attach event; return True when knock threshold is reached."""
        cutoff = now_mono - self.window_sec
        self.attach_times = [t for t in self.attach_times if t >= cutoff]
        self.attach_times.append(now_mono)
        if len(self.attach_times) >= self.threshold:
            self.attach_times.clear()
            return True
        return False


@dataclass
class RecoveryController:
    """Decides bounded recovery actions from state machine signals."""

    bucket: TokenBucket = field(default_factory=TokenBucket)
    hold: bool = False
    incident_id: int = 0
    ladder_step: int = 0  # 0=wait, 1=soft_reconnect, 2=rebind, 3=teardown_apply
    last_action_mono: float = 0.0
    last_bind_succeeded: bool = True

    def clear_hold(self) -> None:
        self.hold = False
        self.ladder_step = 0

    def on_healthy(self) -> None:
        """Reset incident ladder when link reaches LINK_UP or ONLINE."""
        self.ladder_step = 0

    def on_physical_detach_reattach(self) -> None:
        """Physical replug leaves HOLD (plan 3.2)."""
        if self.hold:
            self.clear_hold()

    def next_action(
        self,
        *,
        state: str,
        time_in_state_sec: float,
        now_mono: float,
        enabled: bool,
    ) -> str | None:
        """Return recovery action ('soft_reconnect', 'rebind', 'teardown_apply', 'enter_hold', or None)."""
        if not enabled or self.hold:
            return None
        if state in NON_RECOVERABLE_STATES:
            return None
        if state not in ("CONFIGURED_NO_LINK", "UNBOUND"):
            return None

        # Step 0: wait at least 5s before first active recovery
        wait_needed = BACKOFF_SCHEDULE_SEC[min(self.ladder_step, len(BACKOFF_SCHEDULE_SEC) - 1)]
        if self.ladder_step == 0 and time_in_state_sec < wait_needed:
            return None
        if self.ladder_step > 0 and (now_mono - self.last_action_mono) < wait_needed:
            return None

        if not self.bucket.consume(now_mono):
            self.hold = True
            return "enter_hold"

        self.last_action_mono = now_mono
        if self.ladder_step == 0:
            self.incident_id += 1
            self.ladder_step = 1
            return "soft_reconnect"
        if self.ladder_step == 1:
            # Kernel caveat (plan 3.2 & 7a.2): failed bind is terminal for that function instance
            if not self.last_bind_succeeded:
                self.ladder_step = 3
                return "teardown_apply"
            self.ladder_step = 2
            return "rebind"

        self.ladder_step = 3
        return "teardown_apply"
