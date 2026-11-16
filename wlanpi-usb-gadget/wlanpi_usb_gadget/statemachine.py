"""Pure, monotonic-clock-injected state machine for USB gadget health."""

from __future__ import annotations

from dataclasses import dataclass

STATES = (
    "NO_UDC",
    "HOST_MODE",
    "UNBOUND",
    "DETACHED",
    "ENUMERATING",
    "CONFIGURED_NO_LINK",
    "LINK_UP",
    "ONLINE",
    "SUSPENDED",
    "RECOVERING",
    "HOLD",
)

HOST_PRESENT_STATES = ("ENUMERATING", "CONFIGURED_NO_LINK", "LINK_UP", "ONLINE")


@dataclass(frozen=True)
class Signals:
    """Point-in-time hardware and link signals."""

    dr_mode: str = "peripheral"
    udc_present: bool = True
    gadget_bound: bool = True
    udc_state: str = "not attached"
    carrier_ifaces: tuple[str, ...] = ()
    has_neighbour: bool = False
    recovering: bool = False
    hold: bool = False


@dataclass(frozen=True)
class Transition:
    """Emitted when the state machine changes state."""

    prev_state: str
    new_state: str
    now_mono: float
    udc_state: str
    active_iface: str


def compute_target_state(sig: Signals) -> str:
    """Pure mapping from current signals to the state machine state."""
    if sig.dr_mode == "host":
        return "HOST_MODE"
    if not sig.udc_present:
        return "NO_UDC"
    if not sig.gadget_bound:
        return "UNBOUND"
    if sig.hold:
        return "HOLD"
    if sig.recovering:
        return "RECOVERING"

    udc = sig.udc_state.strip().lower()
    if udc in ("", "not attached"):
        return "DETACHED"
    if udc == "suspended":
        return "SUSPENDED"
    if udc in ("attached", "powered", "default", "addressed"):
        return "ENUMERATING"
    if udc == "configured":
        if not sig.carrier_ifaces:
            return "CONFIGURED_NO_LINK"
        if sig.has_neighbour:
            return "ONLINE"
        return "LINK_UP"
    return "ENUMERATING"


class StateMachine:
    """Tracks gadget state transitions using monotonic timestamps."""

    def __init__(self, initial_state: str = "DETACHED", now_mono: float = 0.0) -> None:
        self.state: str = initial_state
        self.prev_state: str = initial_state
        self.entered_at_mono: float = now_mono
        self.online_since_mono: float | None = now_mono if initial_state == "ONLINE" else None
        self.active_iface: str = ""
        self.udc_state: str = ""

    def update(self, sig: Signals, now_mono: float) -> Transition | None:
        """Advance state machine; return Transition if state changed."""
        target = compute_target_state(sig)
        self.udc_state = sig.udc_state
        self.active_iface = sig.carrier_ifaces[0] if sig.carrier_ifaces else ""

        if target == "ONLINE":
            if self.online_since_mono is None:
                self.online_since_mono = now_mono
        else:
            self.online_since_mono = None

        if target == self.state:
            return None

        prev = self.state
        self.prev_state = prev
        self.state = target
        self.entered_at_mono = now_mono
        return Transition(
            prev_state=prev,
            new_state=target,
            now_mono=now_mono,
            udc_state=sig.udc_state,
            active_iface=self.active_iface,
        )

    def time_in_state(self, now_mono: float) -> float:
        return max(0.0, now_mono - self.entered_at_mono)
