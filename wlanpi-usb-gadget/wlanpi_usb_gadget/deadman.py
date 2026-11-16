"""Dead-man switch for profile trials (/var/lib/wlanpi-usb-gadget/profile.json)."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from wlanpi_usb_gadget.statemachine import HOST_PRESENT_STATES

DEFAULT_PROFILE_STATE_PATH = Path("/var/lib/wlanpi-usb-gadget/profile.json")
ONLINE_COMMIT_SEC = 30.0


@dataclass
class ProfileTrialState:
    """Persisted profile and dead-man trial state."""

    active: str = "legacy"
    committed: str = "legacy"
    in_trial: bool = False
    trial_elapsed_sec: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def load_profile_state(
    default_profile: str = "legacy",
    path: Path = DEFAULT_PROFILE_STATE_PATH,
) -> ProfileTrialState:
    """Load profile trial state from disk, or initialize from default_profile."""
    if not path.exists():
        return ProfileTrialState(active=default_profile, committed=default_profile, in_trial=False)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not a dict")
        return ProfileTrialState(
            active=str(data.get("active", default_profile)),
            committed=str(data.get("committed", default_profile)),
            in_trial=bool(data.get("in_trial", False)),
            trial_elapsed_sec=float(data.get("trial_elapsed_sec", 0.0)),
        )
    except (OSError, ValueError, TypeError):
        return ProfileTrialState(active=default_profile, committed=default_profile, in_trial=False)


def save_profile_state(
    state: ProfileTrialState,
    path: Path = DEFAULT_PROFILE_STATE_PATH,
) -> None:
    """Atomically save profile trial state to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".profile.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state.to_dict(), fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def start_trial(
    new_profile: str,
    *,
    current_default: str = "legacy",
    path: Path = DEFAULT_PROFILE_STATE_PATH,
) -> ProfileTrialState:
    """Begin a trial for `new_profile`, keeping the last committed profile."""
    st = load_profile_state(default_profile=current_default, path=path)
    if st.in_trial:
        committed = st.committed
    else:
        committed = st.active
    updated = ProfileTrialState(
        active=new_profile,
        committed=committed,
        in_trial=True,
        trial_elapsed_sec=0.0,
    )
    save_profile_state(updated, path=path)
    return updated


def commit_trial(
    *,
    current_default: str = "legacy",
    path: Path = DEFAULT_PROFILE_STATE_PATH,
) -> ProfileTrialState:
    """Commit the active profile."""
    st = load_profile_state(default_profile=current_default, path=path)
    updated = ProfileTrialState(
        active=st.active,
        committed=st.active,
        in_trial=False,
        trial_elapsed_sec=0.0,
    )
    save_profile_state(updated, path=path)
    return updated


def revert_trial(
    *,
    safe_profile: str = "legacy",
    current_default: str = "legacy",
    path: Path = DEFAULT_PROFILE_STATE_PATH,
) -> tuple[ProfileTrialState, str]:
    """Revert trial to committed profile (or safe_profile if already on committed)."""
    st = load_profile_state(default_profile=current_default, path=path)
    target = st.committed if st.committed != st.active else safe_profile
    updated = ProfileTrialState(
        active=target,
        committed=target,
        in_trial=False,
        trial_elapsed_sec=0.0,
    )
    save_profile_state(updated, path=path)
    return updated, target


def tick_trial(
    st: ProfileTrialState,
    *,
    gadget_state: str,
    dt_sec: float,
    online_duration_sec: float | None,
    trial_timeout_sec: int,
    path: Path = DEFAULT_PROFILE_STATE_PATH,
) -> str | None:
    """Advance host-present trial clock. Returns 'commit', 'revert', or None."""
    if not st.in_trial:
        return None

    if gadget_state == "ONLINE" and online_duration_sec is not None and online_duration_sec >= ONLINE_COMMIT_SEC:
        commit_trial(current_default=st.committed, path=path)
        st.committed = st.active
        st.in_trial = False
        st.trial_elapsed_sec = 0.0
        return "commit"

    # Only count time while a host is physically present (plan 4.5)
    if gadget_state in HOST_PRESENT_STATES and dt_sec > 0:
        st.trial_elapsed_sec += dt_sec
        if st.trial_elapsed_sec >= trial_timeout_sec:
            return "revert"
        save_profile_state(st, path=path)

    return None
