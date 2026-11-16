"""Event-driven USB gadget monitor daemon (sd_notify, poll, rtnetlink)."""

from __future__ import annotations

import os
import select
import signal
import socket
import time
from pathlib import Path

from wlanpi_usb_gadget.config import GadgetConfig, load_config
from wlanpi_usb_gadget.configfs import (
    DEFAULT_CONFIGFS_ROOT,
    GADGET_NAME,
    apply_gadget,
    plan_gadget_tree,
    read_bound_udc,
    teardown_gadget,
    write_attr,
)
from wlanpi_usb_gadget.deadman import (
    DEFAULT_PROFILE_STATE_PATH,
    load_profile_state,
    revert_trial,
    tick_trial,
)
from wlanpi_usb_gadget.events import record_event
from wlanpi_usb_gadget.journal import (
    PRIORITY_ERR,
    PRIORITY_INFO,
    PRIORITY_WARNING,
    log_journal,
)
from wlanpi_usb_gadget.mac import derive_all_macs, read_board_serial
from wlanpi_usb_gadget.netcfg import reload_networkd, write_networkd_files
from wlanpi_usb_gadget.netlink import LinkEvent, NeighEvent, NetlinkListener
from wlanpi_usb_gadget.profiles import get_profile
from wlanpi_usb_gadget.recovery import (
    RecoveryController,
    ReplugKnockDetector,
    TokenBucket,
)
from wlanpi_usb_gadget.statemachine import Signals, StateMachine
from wlanpi_usb_gadget.status import write_runtime_state
from wlanpi_usb_gadget.udc import (
    UDC_CLASS_DIR,
    detect_controller,
    list_udcs,
    read_udc_attr,
    soft_reconnect,
)

SAFETY_POLL_MS = 10000


def sd_notify(message: str) -> None:
    """Send a systemd sd_notify datagram if NOTIFY_SOCKET is set."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(message.encode("utf-8"), addr)
    except OSError:
        pass


def _read_carrier_ifaces(names: tuple[str, ...] = ("usbncm0", "usbecm0", "usbrndis0")) -> tuple[str, ...]:
    up: list[str] = []
    for name in names:
        p = Path("/sys/class/net") / name / "carrier"
        if p.exists():
            try:
                if p.read_text(encoding="utf-8", errors="ignore").strip() == "1":
                    up.append(name)
            except OSError:
                pass
    return tuple(up)


def _has_bridge_neighbour(bridge_name: str) -> bool:
    # Fast check via /proc/net/arp or ip neigh; fallback to False on error
    arp_path = Path("/proc/net/arp")
    if arp_path.exists():
        for line in arp_path.read_text(encoding="utf-8", errors="ignore").splitlines()[1:]:
            cols = line.split()
            if len(cols) >= 6 and cols[5] == bridge_name and cols[2] != "0x0":
                return True
    return False


def _execute_recovery_action(
    action: str,
    *,
    udc_name: str,
    cfg: GadgetConfig,
    profile_name: str,
    recovery: RecoveryController,
) -> None:
    if action == "soft_reconnect" and udc_name:
        soft_reconnect(udc_name)
    elif action == "rebind" and udc_name:
        udc_file = DEFAULT_CONFIGFS_ROOT / GADGET_NAME / "UDC"
        write_attr(udc_file, "")
        try:
            write_attr(udc_file, udc_name)
            recovery.last_bind_succeeded = True
        except Exception:
            recovery.last_bind_succeeded = False
            raise
    elif action == "teardown_apply" and udc_name:
        teardown_gadget()
        macs = derive_all_macs()
        serial, _ = read_board_serial()
        prof = get_profile(profile_name, serial_console=cfg.serial_console)
        plan = plan_gadget_tree(prof, macs, serial)
        write_networkd_files(cfg, prof, macs.bridge)
        reload_networkd()
        try:
            apply_gadget(plan, udc_name, force=True)
            recovery.last_bind_succeeded = True
        except Exception:
            recovery.last_bind_succeeded = False
            raise


def run_monitor() -> int:
    """Run the event-driven monitor daemon."""
    cfg = load_config()
    trial_state = load_profile_state(default_profile=cfg.profile)
    active_profile = trial_state.active

    recovery = RecoveryController(
        bucket=TokenBucket(budget=cfg.reset_budget, window_sec=cfg.reset_window_sec)
    )
    knock = ReplugKnockDetector()
    sm = StateMachine(initial_state="DETACHED", now_mono=time.monotonic())

    running = True
    reload_requested = False

    def _on_term(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    def _on_hup(_signum: int, _frame: object) -> None:
        nonlocal reload_requested
        reload_requested = True

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    signal.signal(signal.SIGHUP, _on_hup)

    netlink: NetlinkListener | None = None
    try:
        netlink = NetlinkListener()
    except OSError:
        netlink = None

    udc_fd: int | None = None
    udc_name = ""

    def _open_udc_fd() -> None:
        nonlocal udc_fd, udc_name
        if udc_fd is not None:
            try:
                os.close(udc_fd)
            except OSError:
                pass
            udc_fd = None
        udcs = list_udcs()
        if udcs:
            udc_name = udcs[0]
            state_path = UDC_CLASS_DIR / udc_name / "state"
            if state_path.exists():
                try:
                    udc_fd = os.open(state_path, os.O_RDONLY)
                except OSError:
                    udc_fd = None

    _open_udc_fd()
    sd_notify("READY=1")
    last_tick = time.monotonic()
    last_udc_state = ""

    try:
        while running:
            if reload_requested:
                reload_requested = False
                sd_notify("RELOADING=1")
                cfg = load_config()
                recovery.bucket.budget = cfg.reset_budget
                recovery.bucket.window_sec = cfg.reset_window_sec
                recovery.clear_hold()
                sd_notify("READY=1")

            poller = select.poll()
            if netlink is not None:
                poller.register(netlink.fileno(), select.POLLIN)
            if udc_fd is not None:
                poller.register(udc_fd, select.POLLPRI | select.POLLERR)

            events = poller.poll(SAFETY_POLL_MS)
            sd_notify("WATCHDOG=1")

            for fd, _mask in events:
                if netlink is not None and fd == netlink.fileno():
                    for ev in netlink.read_events():
                        if isinstance(ev, (LinkEvent, NeighEvent)):
                            pass
                elif udc_fd is not None and fd == udc_fd:
                    try:
                        os.lseek(udc_fd, 0, os.SEEK_SET)
                        os.read(udc_fd, 64)
                    except OSError:
                        _open_udc_fd()

            now = time.monotonic()
            dt = max(0.0, now - last_tick)
            last_tick = now

            ctrl = detect_controller()
            udcs = list_udcs()
            udc_name = udcs[0] if udcs else ""
            if udc_name and udc_fd is None:
                _open_udc_fd()
            udc_state = (read_udc_attr(udc_name, "state") if udc_name else None) or ""
            bound = bool(read_bound_udc(DEFAULT_CONFIGFS_ROOT / GADGET_NAME))

            # Detect physical detach -> attach for replug knock & HOLD exit
            if last_udc_state in ("", "not attached") and udc_state not in ("", "not attached"):
                recovery.on_physical_detach_reattach()
                if knock.record_attach(now):
                    log_journal(
                        f"Replug knock detected; reverting to SAFE_PROFILE={cfg.safe_profile}",
                        priority=PRIORITY_WARNING,
                        GADGET_EVENT="replug_knock",
                        GADGET_PROFILE=cfg.safe_profile,
                    )
                    record_event("replug_knock", state=sm.state, profile=cfg.safe_profile)
                    recovery.clear_hold()
                    trial_state, active_profile = revert_trial(
                        safe_profile=cfg.safe_profile, current_default=cfg.profile
                    )
                    try:
                        _execute_recovery_action(
                            "teardown_apply",
                            udc_name=udc_name,
                            cfg=cfg,
                            profile_name=active_profile,
                            recovery=recovery,
                        )
                    except Exception as exc:
                        log_journal(f"Replug knock apply failed: {exc}", priority=PRIORITY_ERR)
            last_udc_state = udc_state

            carriers = _read_carrier_ifaces()
            has_neigh = _has_bridge_neighbour(cfg.bridge_name) if carriers else False

            sig = Signals(
                dr_mode=ctrl.dr_mode,
                udc_present=bool(udc_name),
                gadget_bound=bound,
                udc_state=udc_state,
                carrier_ifaces=carriers,
                has_neighbour=has_neigh,
                hold=recovery.hold,
            )
            tr = sm.update(sig, now)
            if tr is not None:
                log_journal(
                    f"State {tr.prev_state} -> {tr.new_state}",
                    priority=PRIORITY_INFO,
                    GADGET_EVENT="transition",
                    GADGET_STATE=tr.new_state,
                    GADGET_PREV_STATE=tr.prev_state,
                    GADGET_PROFILE=active_profile,
                    GADGET_UDC_STATE=tr.udc_state,
                    GADGET_IFACE=tr.active_iface,
                )
                record_event(
                    "transition",
                    state=tr.new_state,
                    prev_state=tr.prev_state,
                    profile=active_profile,
                    udc_state=tr.udc_state,
                    iface=tr.active_iface,
                )
                if tr.new_state in ("LINK_UP", "ONLINE"):
                    recovery.on_healthy()

            # Dead-man profile trial tick
            online_dur = (now - sm.online_since_mono) if sm.online_since_mono is not None else None
            trial_res = tick_trial(
                trial_state,
                gadget_state=sm.state,
                dt_sec=dt,
                online_duration_sec=online_dur,
                trial_timeout_sec=cfg.trial_timeout_sec,
                path=DEFAULT_PROFILE_STATE_PATH,
            )
            if trial_res == "commit":
                log_journal(
                    f"Committed trial profile '{active_profile}'",
                    priority=PRIORITY_INFO,
                    GADGET_EVENT="deadman_commit",
                    GADGET_PROFILE=active_profile,
                )
                record_event("deadman_commit", state=sm.state, profile=active_profile)
            elif trial_res == "revert":
                trial_state, active_profile = revert_trial(
                    safe_profile=cfg.safe_profile, current_default=cfg.profile
                )
                log_journal(
                    f"Trial timed out; reverting to '{active_profile}'",
                    priority=PRIORITY_ERR,
                    GADGET_EVENT="deadman_revert",
                    GADGET_PROFILE=active_profile,
                )
                record_event("deadman_revert", state=sm.state, profile=active_profile)
                if udc_name:
                    try:
                        _execute_recovery_action(
                            "teardown_apply",
                            udc_name=udc_name,
                            cfg=cfg,
                            profile_name=active_profile,
                            recovery=recovery,
                        )
                    except Exception as exc:
                        log_journal(f"Deadman revert apply failed: {exc}", priority=PRIORITY_ERR)

            # Bounded recovery ladder
            action = recovery.next_action(
                state=sm.state,
                time_in_state_sec=sm.time_in_state(now),
                now_mono=now,
                enabled=cfg.monitor_recovery,
            )
            if action == "enter_hold":
                log_journal(
                    "Recovery budget exhausted; entering HOLD",
                    priority=PRIORITY_ERR,
                    GADGET_EVENT="hold",
                    GADGET_STATE="HOLD",
                    GADGET_INCIDENT=str(recovery.incident_id),
                )
                record_event("hold", state="HOLD", profile=active_profile, incident=str(recovery.incident_id))
            elif action is not None:
                rem = recovery.bucket.remaining(now)
                log_journal(
                    f"Recovery action={action} step={recovery.ladder_step} budget_remaining={rem}",
                    priority=PRIORITY_WARNING,
                    GADGET_EVENT="recovery",
                    GADGET_STATE=sm.state,
                    GADGET_INCIDENT=str(recovery.incident_id),
                )
                record_event(
                    "recovery",
                    state=sm.state,
                    profile=active_profile,
                    incident=str(recovery.incident_id),
                    detail=f"{action} (remaining={rem})",
                )
                try:
                    _execute_recovery_action(
                        action,
                        udc_name=udc_name,
                        cfg=cfg,
                        profile_name=active_profile,
                        recovery=recovery,
                    )
                except Exception as exc:
                    log_journal(f"Recovery {action} failed: {exc}", priority=PRIORITY_ERR)
                    record_event("recovery_error", state=sm.state, detail=str(exc))

            write_runtime_state(
                {
                    "state": sm.state,
                    "prev_state": sm.prev_state,
                    "time_in_state_sec": round(sm.time_in_state(now), 2),
                    "udc_name": udc_name,
                    "udc_state": udc_state,
                    "active_iface": sm.active_iface,
                    "profile": active_profile,
                    "profile_status": "trial" if trial_state.in_trial else "committed",
                    "reset_budget_remaining": recovery.bucket.remaining(now),
                }
            )
    finally:
        if udc_fd is not None:
            try:
                os.close(udc_fd)
            except OSError:
                pass
        if netlink is not None:
            netlink.close()
    return 0
