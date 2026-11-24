"""CLI entry point for wlanpi-usb-gadget."""

from __future__ import annotations

import argparse
import json
import sys

from wlanpi_usb_gadget.bootcfg import check_bootcfg, ensure_bootcfg, remove_bootcfg
from wlanpi_usb_gadget.config import ConfigError, load_config
from wlanpi_usb_gadget.configfs import (
    bind_gadget_udc,
    build_gadget_tree,
    discover_member_ifnames,
    plan_gadget_tree,
    teardown_gadget,
)
from wlanpi_usb_gadget.deadman import (
    commit_trial,
    load_profile_state,
    revert_trial,
    start_trial,
)
from wlanpi_usb_gadget.events import read_events, record_event
from wlanpi_usb_gadget.httpd import serve_status_http
from wlanpi_usb_gadget.journal import PRIORITY_ERR, PRIORITY_INFO, log_journal
from wlanpi_usb_gadget.mac import derive_all_macs, read_board_serial
from wlanpi_usb_gadget.monitor import run_monitor
from wlanpi_usb_gadget.netcfg import (
    enslave_and_up_bridge,
    ensure_bridge_exists,
    maybe_restart_avahi,
    reload_networkd,
    remove_networkd_files,
    write_networkd_files,
)
from wlanpi_usb_gadget.profiles import get_profile, list_profiles
from wlanpi_usb_gadget.report import write_report_tgz
from wlanpi_usb_gadget.status import (
    collect_status,
    format_status_human,
    run_selftest,
    status_exit_code,
    write_runtime_state,
)
from wlanpi_usb_gadget.udc import detect_controller, ensure_kernel_module, wait_for_udc


def _cmd_apply(force: bool) -> int:
    cfg = load_config()
    trial = load_profile_state(default_profile=cfg.profile)
    profile_name = trial.active

    ctrl = detect_controller()
    if ctrl.dr_mode == "host":
        log_journal("Controller is in host mode (dr_mode=host); skipping gadget setup", priority=PRIORITY_INFO)
        write_runtime_state({"state": "HOST_MODE", "profile": profile_name})
        return 0

    try:
        prof = get_profile(profile_name, serial_console=cfg.serial_console)
        macs = derive_all_macs()
        serial, _ = read_board_serial()

        # 1. Load libcomposite and build the COMPLETE configfs tree BEFORE loading dwc2.
        #    Pre-registering configfs prevents the kernel's usb_udc modalias uevent
        #    from auto-binding legacy g_ether when dwc2 registers the UDC.
        ensure_kernel_module("libcomposite")
        plan = plan_gadget_tree(prof, macs, serial)
        build_status = build_gadget_tree(plan, force=force)

        if build_status == "unchanged":
            res = "unchanged"
            udc_name = wait_for_udc(timeout_sec=5.0)
        else:
            # 2. Now load dwc2 and wait for the UDC.
            if ctrl.dwc2_mode in ("not_loaded", "module"):
                ensure_kernel_module("dwc2")
            udc_name = wait_for_udc(timeout_sec=5.0)

            # 3. Write networkd/link files AFTER foreign-gadget check and BEFORE UDC bind
            #    so udev .link rules are in place when function netdevs appear.
            write_networkd_files(cfg, prof, macs.bridge)

            # 4. Bind UDC (reclaims legacy g_ether on EBUSY; refuses other holders).
            res = bind_gadget_udc(plan, udc_name)

        # 5. Create bridge (moving aside any fallback usb0 collision), enslave + UP members & bridge.
        ensure_bridge_exists(cfg.bridge_name, macs.bridge)
        actual_ifnames = discover_member_ifnames(bridge_name=cfg.bridge_name)
        enslave_and_up_bridge(cfg.bridge_name, actual_ifnames)
        write_networkd_files(
            cfg,
            prof,
            macs.bridge,
            member_ifnames=actual_ifnames or None,
        )
        reload_networkd()
        maybe_restart_avahi(cfg.bridge_name, profile=profile_name)
        log_journal(
            f"apply {res} profile={profile_name} udc={udc_name} dwc2={ctrl.dwc2_mode}",
            priority=PRIORITY_INFO,
            GADGET_EVENT="apply",
            GADGET_PROFILE=profile_name,
        )
        record_event("apply", state="DETACHED", profile=profile_name, detail=f"{res} udc={udc_name}")
        return 0
    except Exception as exc:
        log_journal(f"apply failed: {exc}", priority=PRIORITY_ERR, GADGET_EVENT="apply_error")
        record_event("apply_error", state="UNBOUND", profile=profile_name, detail=str(exc))
        write_runtime_state({"state": "UNBOUND", "profile": profile_name, "error": str(exc)})
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _cmd_teardown() -> int:
    existed = teardown_gadget()
    remove_networkd_files()
    reload_networkd()
    log_journal(f"teardown complete (existed={existed})", priority=PRIORITY_INFO, GADGET_EVENT="teardown")
    record_event("teardown", state="UNBOUND", detail=f"existed={existed}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wlanpi-usb-gadget")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="Show gadget status")
    p_status.add_argument("--json", action="store_true", help="Emit JSON output")

    sub.add_parser("selftest", help="Run diagnostic self-tests")
    sub.add_parser("report", help="Write diagnostic .tar.gz bundle to stdout")

    p_events = sub.add_parser("events", help="Show persisted event history")
    p_events.add_argument("--limit", type=int, default=50)

    p_apply = sub.add_parser("apply", help="Build configfs gadget and networkd files")
    p_apply.add_argument("--force", action="store_true")

    sub.add_parser("teardown", help="Ordered configfs teardown")
    sub.add_parser("recover", help="Leave HOLD and re-apply gadget")

    p_prof = sub.add_parser("profile", help="Manage gadget composition profiles")
    prof_sub = p_prof.add_subparsers(dest="profile_cmd", required=True)
    prof_sub.add_parser("list")
    p_pset = prof_sub.add_parser("set")
    p_pset.add_argument("name", choices=["legacy", "ncm", "ncm-ecm"])
    prof_sub.add_parser("commit")
    prof_sub.add_parser("revert")

    p_cfg = sub.add_parser("config", help="Configuration commands")
    cfg_sub = p_cfg.add_subparsers(dest="config_cmd", required=True)
    cfg_sub.add_parser("check")

    p_boot = sub.add_parser("bootcfg", help="Manage Go config.txt gadget-mode block")
    boot_sub = p_boot.add_subparsers(dest="bootcfg_cmd", required=True)
    boot_sub.add_parser("ensure")
    boot_sub.add_parser("remove")
    boot_sub.add_parser("check")

    sub.add_parser("monitor", help="Run event-driven monitor daemon")

    p_http = sub.add_parser("httpd", help="Run socket-activated status HTTP server")
    p_http.add_argument("--bind", default="198.18.42.1")
    p_http.add_argument("--port", type=int, default=8088)
    p_http.add_argument("--oneshot", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "status":
        st = collect_status()
        if args.json:
            print(json.dumps(st, indent=2))
        else:
            print(format_status_human(st))
        return status_exit_code(st)

    if args.command == "selftest":
        checks = run_selftest()
        rc = 0
        for c in checks:
            print(f"[{c.status.upper():4s}] {c.name}: {c.detail}")
            if c.hint and c.status in ("fail", "warn"):
                print(f"       hint: {c.hint}")
            if c.status == "fail":
                rc = 1
        return rc

    if args.command == "report":
        write_report_tgz(sys.stdout.buffer)
        return 0

    if args.command == "events":
        for ev in read_events(limit=args.limit, newest_first=True):
            print(json.dumps(ev))
        return 0

    if args.command == "apply":
        return _cmd_apply(force=args.force)

    if args.command == "teardown":
        return _cmd_teardown()

    if args.command == "recover":
        return _cmd_apply(force=True)

    if args.command == "profile":
        cfg = load_config()
        if args.profile_cmd == "list":
            st = load_profile_state(default_profile=cfg.profile)
            for p in list_profiles(serial_console=cfg.serial_console):
                marker = "*" if p.name == st.active else " "
                print(f"{marker} {p.name:8s} bcdDevice={p.bcd_device}  {p.summary}")
            return 0
        if args.profile_cmd == "set":
            start_trial(args.name, current_default=cfg.profile)
            print(f"Started trial for profile '{args.name}' (timeout {cfg.trial_timeout_sec}s)")
            return _cmd_apply(force=True)
        if args.profile_cmd == "commit":
            st = commit_trial(current_default=cfg.profile)
            print(f"Committed profile '{st.committed}'")
            return 0
        if args.profile_cmd == "revert":
            st, target = revert_trial(safe_profile=cfg.safe_profile, current_default=cfg.profile)
            print(f"Reverted to profile '{target}'")
            return _cmd_apply(force=True)

    if args.command == "config" and args.config_cmd == "check":
        try:
            cfg = load_config()
        except ConfigError as exc:
            print(f"Config error: {exc}", file=sys.stderr)
            return 2
        for k, v in cfg.as_dict().items():
            print(f"{k}={v}")
        for w in cfg.warnings:
            print(f"WARNING: {w}", file=sys.stderr)
        return 0

    if args.command == "bootcfg":
        if args.bootcfg_cmd == "ensure":
            res = ensure_bootcfg()
        elif args.bootcfg_cmd == "remove":
            res = remove_bootcfg()
        else:
            res = check_bootcfg()
        print(res.message)
        return 0

    if args.command == "monitor":
        return run_monitor()

    if args.command == "httpd":
        serve_status_http(bind_host=args.bind, port=args.port, oneshot=args.oneshot)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
